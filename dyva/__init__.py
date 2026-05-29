#!/usr/bin/env python3
import argparse, errno, fnmatch, json, os, requests, logging, sys, time, threading, signal, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOGLEVEL, logging.INFO),
    format="%(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("dumpster-dive")
from socketserver import ThreadingMixIn
from queue import Queue, Empty

CACHE_DIR = os.path.expanduser("~/.cache/free-ollama")
CACHE_FILE = os.path.join(CACHE_DIR, "free-ollama.json")
BAD_FILE = os.path.join(CACHE_DIR, "bad-hosts.txt")
GOOD_FILE = os.path.join(CACHE_DIR, "good-hosts.txt")
POOL_SIZE = int(os.environ.get("POOL_SIZE", "3"))
LAST_SUCCESS = {}  # model -> (host_url, full_model_name)

# The LAST_SUCCESS cache stores (host, full_model_name) so the second request
# for the same model can try that host DIRECTLY without calling find_servers()
# (which triggers a slow cache refresh and full server list filter/sort).
# Only if the stored host fails do we fall through to the full search.

PORT = int(os.environ.get("PORT", "11434"))
TIMEOUT = 30

_bad_cache = None
_good_cache = None
_servers_cache = None

_activity_listeners = []
_activity_lock = threading.Lock()

def broadcast_activity(host, model, status, message):
    entry = {'host': host, 'model': model, 'status': status, 'message': message, 'time': time.time()}
    with _activity_lock:
        dead = []
        for q in _activity_listeners:
            try:
                q.put_nowait(entry)
            except Exception:
                dead.append(q)
        for q in dead:
            _activity_listeners.remove(q)

def listen_activity():
    q = Queue()
    with _activity_lock:
        _activity_listeners.append(q)
    return q

def unlisten_activity(q):
    with _activity_lock:
        if q in _activity_listeners:
            _activity_listeners.remove(q)


def refresh_cache():
    """Force a refresh of the server cache."""
    global _servers_cache, _bad_cache, _good_cache
    _servers_cache = None
    _bad_cache = None
    _good_cache = None
    os.makedirs(CACHE_DIR, exist_ok=True)
    log.debug("Refreshing server cache...")
    script = os.path.join(os.path.dirname(__file__), "free-ollama")
    if not os.path.exists(script):
        script = "free-ollama"
    subprocess.run([script], capture_output=True, timeout=120)

def ensure_cache():
    """Download the latest server list if cache is missing or >24h old."""
    need = False
    if not os.path.exists(CACHE_FILE) or os.path.getsize(CACHE_FILE) == 0:
        need = True
    elif time.time() - os.path.getmtime(CACHE_FILE) > 86400:
        need = True
    if need:
        refresh_cache()


def load_servers():
    """Return list of server dicts from the cached JSON file."""
    global _servers_cache
    log.debug("Loading servers...")
    ensure_cache()
    if _servers_cache is None:
        with open(CACHE_FILE) as f:
            _servers_cache = json.load(f)
    return _servers_cache


def load_bad():
    """Return set of 'host model' keys that have failed."""
    global _bad_cache
    if _bad_cache is None:
        _bad_cache = set()
        if os.path.exists(BAD_FILE):
            with open(BAD_FILE) as f:
                for line in f:
                    if line.strip():
                        _bad_cache.add(line.strip())
    return _bad_cache


def add_bad(host, model):
    """Record that host+model pair failed, both to file and in-memory."""
    global _bad_cache
    with open(BAD_FILE, "a") as f:
        f.write(f"{host} {model}\n")
    if _bad_cache is not None:
        _bad_cache.add(f"{host} {model}")

def clear_bad():
    """Clear all bad hosts."""
    global _bad_cache
    _bad_cache = None
    if os.path.exists(BAD_FILE):
        os.remove(BAD_FILE)


def load_good():
    """Return set of 'host model' keys that have succeeded."""
    global _good_cache
    if _good_cache is None:
        _good_cache = set()
        if os.path.exists(GOOD_FILE):
            with open(GOOD_FILE) as f:
                for line in f:
                    if line.strip():
                        _good_cache.add(line.strip())
    return _good_cache


def add_good(host, model):
    """Record that host+model pair succeeded, both to file and in-memory."""
    key = f"{host} {model}"
    good = load_good()
    if key not in good:
        with open(GOOD_FILE, "a") as f:
            f.write(f"{key}\n")
        if _good_cache is not None:
            _good_cache.add(key)


def match_model(model_name, pattern):
    """Return True if model_name contains pattern, supporting fnmatch wildcards."""
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(model_name, f"*{pattern}*")
    return pattern in model_name


def find_servers(sub):
    """Return servers that serve model matching *sub*, sorted by success history then TPS.

    Results are sorted so last-successful comes first, then known-good hosts,
    then remaining hosts.
    """
    servers = load_servers()
    bad = load_bad()
    good = load_good()
    matched = []
    for s in servers:
        models = s.get("models", [])
        ms = [m for m in models if match_model(m, sub)]
        if not ms:
            continue
        if any(":cloud" in m for m in models) and ":cloud" not in sub:
            continue
        host = s.get("server", "")
        key = f"{host} {sub}"
        if key in bad:
            continue
        is_good = key in good
        _last = LAST_SUCCESS.get(sub)
        is_last = _last is not None and host == _last[0]
        matched.append((s.get("tps", 0), -2 if is_last else (-1 if is_good else 0), host, ms))
    matched.sort(key=lambda x: (x[1], x[0]))
    return matched


_sort_toggle=False
def all_models():
    """Return deduplicated list of all models across all servers."""
    global _sort_toggle
    servers = load_servers()
    seen = {}
    out = []
    _sort_toggle = not _sort_toggle

    for s in servers:
        for m in s.get("models", []):
            if ":cloud" in m or len(m) == 0:
                continue
            if m not in seen:
                seen[m] = {'id': m, 'count': 1}
            else:
                seen[m]['count'] += 1

    # ollama ls doesn't really have sort so we just toggle the order
    if _sort_toggle:
        sorty = sorted(seen.values(), key=lambda x: x.get('count'))
    else:
        sorty = sorted(seen.values(), key=lambda x: x.get('id').lower())

    return list(sorty)


def to_ollama(body):
    """Translate OpenAI-format request body to Ollama-format request body."""
    msg = body.get("messages", [])
    opts = {}
    if "temperature" in body:
        opts["temperature"] = body["temperature"]
    if "max_tokens" in body:
        opts["num_predict"] = body["max_tokens"]
    if "top_p" in body:
        opts["top_p"] = body["top_p"]
    return {"model": body.get("model", ""), "messages": msg, "options": opts}


def to_openai(resp, model):
    """Translate Ollama non-streaming response to OpenAI-format response dict."""
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": resp.get("message", {}).get("content", ""),
            },
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": resp.get("prompt_eval_count", 0),
            "completion_tokens": resp.get("eval_count", 0),
            "total_tokens": resp.get("prompt_eval_count", 0) + resp.get("eval_count", 0),
        },
    }


def err_obj(msg, code=None):
    """Return an OpenAI-style error dict."""
    e = {"message": msg}
    if code:
        e["code"] = code
    return {"error": e}


def sse_chunk(model, content, done=False, finish_reason="stop"):
    """Build an OpenAI SSE chunk dict for a single token delta."""
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {} if not content else {"content": content},
            "finish_reason": finish_reason if done else None,
        }],
    }


def sse_str(obj):
    """Serialize *obj* to a single SSE ``data: ...`` line."""
    return f"data: {json.dumps(obj)}\n\n"


def sse_status(host, model, message, status):
    """Serialize a status event to SSE ``event: status`` + ``data: ...``."""
    return f"event: status\ndata: {json.dumps({'host': host, 'model': model, 'message': message, 'status': status})}\n\n"


def parse_json(data):
    """Try to parse *data* as JSON, falling back to raw_decode for partial payloads."""
    s = data.decode().strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    try:
        dec = json.JSONDecoder()
        obj, _ = dec.raw_decode(s)
        return obj
    except Exception:
        return None


def try_host(full_model, host, payload, q, done):
    """Try one host for non-streaming, returning result via *q*.

    Puts one of ``("ok", host, resp)``, ``("conn", host)``, or
    ``("serv", host, error)``.
    """
    tag = f"{host} {full_model}"
    broadcast_activity(host, full_model, "trying", f"trying {host}...")
    if done.is_set():
        return
    result = [None]
    exc = [None]

    def make_request():
        try:
            result[0] = requests.post(
                f"{host}/api/chat",
                json=payload,
                timeout=TIMEOUT,
            )
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=make_request)
    t.start()
    t.join(timeout=TIMEOUT + 1)

    if done.is_set():
        log.debug(f"  └─ {tag}  (killed)")
        return

    if exc[0]:
        log.debug(f"  ✗ {tag}  (exception: {exc[0]})")
        broadcast_activity(host, full_model, "failed", f"{host} failed ({exc[0]})")
        q.put(("conn", host))
        return

    if result[0] is None:
        log.debug(f"  ✗ {tag}  (timeout)")
        broadcast_activity(host, full_model, "failed", f"{host} timed out")
        q.put(("conn", host))
        return

    if result[0].status_code != 200:
        log.debug(f"  ✗ {tag}  (status {result[0].status_code})")
        broadcast_activity(host, full_model, "failed", f"{host} returned {result[0].status_code}")
        q.put(("conn", host))
        return

    out = result[0].text
    if not out.strip():
        log.debug(f"  ✗ {tag}  (empty response)")
        broadcast_activity(host, full_model, "failed", f"{host} returned empty response")
        q.put(("conn", host))
        return

    resp = parse_json(out.encode())
    if not isinstance(resp, dict):
        log.debug(f"  ✗ {tag}  (bad response: {out[:120]})")
        broadcast_activity(host, full_model, "failed", f"{host} returned bad response")
        q.put(("conn", host))
        return

    if "error" in resp:
        log.debug(f"  ✗ {tag}  (error: {resp['error']})")
        broadcast_activity(host, full_model, "failed", f"{host} error: {resp['error']}")
        q.put(("serv", host, resp["error"]))
        return

    if "choices" in resp and "message" not in resp:
        choices = resp.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            resp = {"message": msg, "done": True}

    log.debug(f"  ✓ {tag}")
    broadcast_activity(host, full_model, "connected", f"connected to {host}")
    q.put(("ok", host, full_model, resp))


# NOTE: fan_out removed — sequential per-server try replaces it for simplicity.
# Re-introduce parallel racing (POOL_SIZE) here if latency becomes an issue later.
def _try_all(servers, model, opayload):
    """Try servers sequentially until one succeeds. Return (host, data) or (None, None)."""
    for _, _, host, ms in servers:
        full = ms[0]
        payload = dict(opayload, model=full, stream=False)
        try:
            r = requests.post(f"{host}/api/chat", json=payload, timeout=TIMEOUT)
        except Exception:
            add_bad(host, model)
            continue
        if r.status_code != 200:
            add_bad(host, model)
            continue
        data = r.json()
        if "error" in data:
            add_bad(host, model)
            continue
        LAST_SUCCESS[model] = (host, full)
        add_good(host, model)
        return host, data
    return None, None
    """Race the given servers in parallel; return (winning_host, resp, errors)."""
    q = Queue()
    done = threading.Event()
    conn_errors = []
    serv_errors = []
    broadcast_activity("", sub, "searching", f"searching {len(servers)} servers for {sub}...")
    for _, _, host, ms in servers:
        full = ms[0]
        p = dict(payload, model=full)
        t = threading.Thread(target=try_host, args=(full, host, p, q, done))
        t.start()
    pairs = [f"{h} {m[0]}" for (_, _, h, m) in servers]
    log.debug(f"  trying {sub} on {', '.join(pairs)}")
    remaining = len(servers)
    while remaining > 0:
        try:
            result = q.get(timeout=TIMEOUT + 5)
        except Empty:
            break
        remaining -= 1
        tag = result[0]
        if tag == "ok":
            done.set()
            LAST_SUCCESS[sub] = result[1]
            add_good(result[1], sub)
            return result[1], result[2], conn_errors + serv_errors
        if tag == "conn":
            conn_errors.append(result[1])
        if tag == "serv":
            serv_errors.append(result[1])
    done.set()
    return None, None, conn_errors + serv_errors


class Handler(BaseHTTPRequestHandler):
    """OpenAI-compatible HTTP handler that proxies to free Ollama servers."""

    def do_HEAD(self):
        """Handle HEAD requests: same as GET but no body."""
        self.head_request = True
        try:
            self.do_GET()
        finally:
            self.head_request = False

    def do_GET(self):
        if self.path == "/v1/models":
            models = all_models()
            self.send_json({
                "object": "list",
                "data": models
            })
            return

        if self.path == "/clear-bad":
            clear_bad()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if self.path == "/refresh-cache":
            refresh_cache()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if getattr(self, 'head_request', False):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", 0)
            self.end_headers()
            return

        if self.path in ("/", "/dashboard"):
            bad = load_bad()
            good = load_good()
            servers = load_servers()
            models = sorted(all_models(), key=lambda x: -x["count"])
            tpl_dir = os.path.join(os.path.dirname(__file__), "static")
            with open(os.path.join(tpl_dir, "dashboard.html")) as f:
                html = f.read()
            model_rows = "".join(
                f'<div class="model-item" data-name="{m["id"]}"><span class="model-name">{m["id"]}</span><span class="model-count">{m["count"]}</span></div>'
                for m in models
            )
            model_more = ""
            last_rows = "".join(
                f'<div class="model-item"><span class="host-name">{host}</span><span class="host-model">{model}</span></div>'
                for model, (host, _full) in list(LAST_SUCCESS.items())[:20]
            ) if LAST_SUCCESS else '<div style="color:#999;font-size:.85rem">None</div>'
            good_rows = "".join(
                f'<div class="model-item"><span class="host-name">{h.split(" ", 1)[0]}</span><span class="host-model">{h.split(" ", 1)[1]}</span></div>'
                for h in sorted(good)[:30]
            )
            good_more = f'<div class="more">... and {len(good) - 30} more</div>' if len(good) > 30 else ""
            bad_rows = "".join(
                f'<div class="model-item"><span class="host-name">{h.split(" ", 1)[0]}</span><span class="host-model">{h.split(" ", 1)[1]}</span></div>'
                for h in sorted(bad)[:30]
            )
            bad_more = f'<div class="more">... and {len(bad) - 30} more</div>' if len(bad) > 30 else ""
            cache_mtime = os.path.getmtime(CACHE_FILE) if os.path.exists(CACHE_FILE) else 0
            cache_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cache_mtime)) if cache_mtime else "never"
            html = html.replace("__PORT__", str(PORT))
            html = html.replace("__POOL_SIZE__", str(POOL_SIZE))
            html = html.replace("__TIMEOUT__", str(TIMEOUT))
            html = html.replace("__SERVER_COUNT__", str(len(servers)))
            html = html.replace("__MODEL_COUNT__", str(len(models)))
            html = html.replace("__CACHE_UPDATED__", cache_time)
            html = html.replace("__MODEL_ROWS__", model_rows)
            html = html.replace("__MODEL_MORE__", model_more)
            html = html.replace("__LAST_ROWS__", last_rows)
            html = html.replace("__GOOD_COUNT__", str(len(good)))
            html = html.replace("__GOOD_ROWS__", good_rows)
            html = html.replace("__GOOD_MORE__", good_more)
            html = html.replace("__BAD_COUNT__", str(len(bad)))
            html = html.replace("__BAD_ROWS__", bad_rows)
            html = html.replace("__BAD_MORE__", bad_more)
            model_hosts = {}
            for s in servers:
                host = s.get("server", "")
                for m in s.get("models", []):
                    if ":cloud" in m or len(m) == 0:
                        continue
                    model_hosts.setdefault(m, []).append(host)
            for m in model_hosts:
                model_hosts[m].sort()
            html = html.replace("__MODEL_HOSTS_DATA__", json.dumps(model_hosts))
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/tags":
            graffiti = [
                ":::::::-.  ",
                " ;;,   `';,",
                " `[[     [[",
                "  $$,    $$",
                "  888_,o8P'",
                "  MMMMP'`  ",
                " ...    :::",
                " ;;     ;;;",
                "[['     [[[",
                "$$      $$$",
                "88    .d888",
                ".'YmmMMMM''",
                ";;,.    ;;;",
                "[[[[, ,[[[[",
                "$$$$$$$$'$$",
                "888 Y88' 88",
                "MMM  M'  'M",
                "::::::::::.",
                " `;;;```.;;",
                "  `]]nnn]]'",
                "   $$$''   ",
                "   888o    ",
                "   YMMMb   ",
                " .::::::. ",
                ";;;`    ` ",
                "'[==/[[[[,",
                "  '''    $",
                " 88b    dP",
                "  'YMmMY' ",
                ":::::::::::",
                ";;;;;;;;'''",
                "     [[    ",
                "     $$    ",
                "     88,   ",
                "     MMM   ",
                ".,::::::  ",
                ";;;;''''  ",
                " [[cccc   ",
                " $$''''   ",
                " 888oo,__ ",
                " ''''YUMMM",
                ":::::::..  ",
                ";;;;``;;;; ",
                " [[[,/[[[' ",
                " $$$$$$c   ",
                " 888b '88bo",
                " MMMM   'W'",
                "           ",
                "           ",
                ":::::::-.  ",
                " ;;,   `';,",
                " `[[     [[",
                "  $$,    $$",
                "  888_,o8P'",
                "  MMMMP'`  ",
                ".-:.     ::",
                " ';;.   ;;;",
                "   '[[,[[['",
                "     c$$'  ",
                "   ,8P'`   ",
                "  mM'      ",
                ":::      .:",
                "';;,   ,;;;",
                " \\[[  .[[/ ",
                "  Y$c.$$'  ",
                "   Y88P    ",
                "    MP     ",
                "  :::.     ",
                "  ;;`;;    ",
                " ,[[ '[[,  ",
                "c$$$cc$$$c ",
                " 888   888,",
                " YMM   ''` ",
                "           ",
                "           ",
                "  -~===~-  ",
                "           ",
                "           ",
                "  _______ ",
                " |   _   |",
                " |.  1___|",
                " |.  __)  ",
                " |:  |    ",
                " |::.|    ",
                " `---'    ",
                "  _______ ",
                " |   _   \\",
                " |.  l   /",
                " |.  _   1",
                " |:  |   |",
                " |::.|:. |",
                " `--- ---'",
                "  _______ ",
                " |   _   |",
                " |.  1___|",
                " |.  __)_ ",
                " |:  1   |",
                " |::.. . |",
                " `-------'",
                "  _______ ",
                " |   _   |",
                " |.  1___|",
                " |.  __)_ ",
                " |:  1   |",
                " |::.. . |",
                " `-------'",
                "          ",
                "          ",
                "  _______ ",
                " |   _   |",
                " |.  |   |",
                " |:  1   |",
                " |::.. . |",
                " `-------'",
                "  ___     ",
                " |   |    ",
                " |.  |    ",
                " |.  |___ ",
                " |:  1   |",
                " |::.. . |",
                " `-------'",
                "  ___     ",
                " |   |    ",
                " |.  |    ",
                " |.  |___ ",
                " |:  1   |",
                " |::.. . |",
                " `-------'",
                "  _______ ",
                " |   _   |",
                " |.  1   |",
                " |.  _   |",
                " |:  |   |",
                " |::.|:. |",
                " `--- ---'",
                "  ___ ___ ",
                " |   Y   |",
                " |.      |",
                " |. \\_/  |",
                " |:  |   |",
                " |::.|:. |",
                " `--- ---'",
                "  _______ ",
                " |   _   |",
                " |.  1   |",
                " |.  _   |",
                " |:  |   |",
                " |::.|:. |",
                " `--- ---'",
                "          ",
                "          ",
                "  : :: :  ",
                "          ",
                "          "]

            models = all_models()
            now = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.localtime(time.time() - (7 * 24 * 60 * 60)))
            self.send_json({
                "models": [
                    {
                        "name": models[m]["id"],
                        "model": models[m]["id"],
                        "modified_at": now,
                        "size": models[m]['count'],
                        "digest": f"{graffiti[m % len(graffiti)]}       000000000000000000000000000000000000000000000000000000",
                        "details": {
                            "parent_model": "",
                            "format": "gguf",
                            "family": "",
                            "families": None,
                            "parameter_size": "",
                            "quantization_level": "",
                        },
                    }
                    for m in range(len(models))
                ],
            })
            return

        if self.path == "/api/version":
            self.send_json({"version": "0.5.0"})
            return

        if self.path == "/api/activity":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.flush()

            q = listen_activity()
            try:
                while True:
                    entry = q.get()
                    try:
                        self.wfile.write(f"data: {json.dumps(entry)}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
            finally:
                unlisten_activity(q)
            return

        if self.path == "/refresh":
            broadcast_activity("", "", "searching", "refreshing server cache...")
            refresh_cache()
            self.send_json({"status": "ok", "message": "cache refreshed"})
            return

        if self.path == "/api/show":
            self.send_json({
                "modelfile": "# free-ollama proxy",
                "details": {"parent_model": "", "format": "gguf", "family": "", "families": None,
                            "parameter_size": "", "quantization_level": ""},
                "model_info": {},
            })
            return

        static_path = os.path.join(os.path.dirname(__file__), "static", self.path.lstrip("/"))
        if os.path.exists(static_path) and os.path.isfile(static_path):
            data = open(static_path, "rb").read()
            ct = "image/png" if static_path.endswith(".png") else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not getattr(self, 'head_request', False):
                self.wfile.write(data)
            return
        self.send_json(err_obj("not found"), 404)

    def do_POST(self):
        """Route requests to the appropriate handler."""
        if self.path == "/api/show":
            self.send_json({
                "modelfile": "# free-ollama proxy",
                "details": {"parent_model": "", "format": "gguf", "family": "", "families": None,
                            "parameter_size": "", "quantization_level": ""},
                "model_info": {},
            })
            return

        if self.path == "/api/chat":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self.send_json(err_obj("invalid JSON"), 400)
                return
            model = body.get("model", "")
            if not model:
                self.send_json(err_obj("model is required", "missing_model"), 400)
                return
            do_stream = body.get("stream", False)
            opayload = body
            log.debug(f"Ollama chat request: model={model}, stream={do_stream}")
            self._send_status = self.headers.get("X-Dyva-Status") == "1"
            self._proxy_chat(model, opayload, do_stream, openai_format=False)
            return

        if self.path == "/api/generate":
            self._proxy_generate()
            return

        if self.path != "/v1/chat/completions":
            self.send_json(err_obj("not found"), 404)
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_json(err_obj("invalid JSON"), 400)
            return
        model = body.get("model", "")
        if not model:
            self.send_json(err_obj("model is required", "missing_model"), 400)
            return
        do_stream = body.get("stream", False)
        opayload = to_ollama(body)
        log.debug(f"OpenAI request: model={model}, stream={do_stream}")
        self._send_status = self.headers.get("X-Dyva-Status") == "1"
        self._proxy_chat(model, opayload, do_stream, openai_format=True)

    def _proxy_chat(self, model, opayload, do_stream, openai_format):
        """
        Proxy a chat request to an Ollama backend.

        Strategy:
          1. If LAST_SUCCESS[model] is set, try that (host, full_model) DIRECTLY
             — skip find_servers() to avoid a slow cache refresh.
          2. If that fails (or no cache), fall through to find_servers() and try
             sequentially until one works.
        """
        last = LAST_SUCCESS.get(model)
        if last:
            last_host, last_full = last
            log.debug(f"Reusing {last_host} for {model}")
            if do_stream:
                # Build a minimal server entry for the stream helpers
                entry = (0, -2, last_host, [last_full])
                if openai_format:
                    if self._try_stream(model, [entry], opayload):
                        return
                else:
                    if self._try_stream_ollama(model, [entry], opayload):
                        return
            else:
                result = self._try_one(last_host, model, last_full, opayload)
                if result:
                    if openai_format:
                        self.send_json(to_openai(result, model), 200)
                    else:
                        self.send_json(result, 200)
                    return

        servers = find_servers(model)
        if not servers:
            self.send_json(err_obj(f"no available servers for '{model}'", "model_not_found"), 404)
            return

        if do_stream:
            if openai_format:
                self._handle_stream(model, servers, opayload)
            else:
                self._handle_stream_ollama(model, servers, opayload)
            return

        host, data = _try_all(servers, model, dict(opayload, stream=False))
        if host:
            if openai_format:
                self.send_json(to_openai(data, model), 200)
            else:
                self.send_json(data, 200)
            return

        self.send_json(err_obj("all servers failed"), 502)

    def _proxy_generate(self):
        """Proxy an Ollama /api/generate request to a backend server."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_json(err_obj("invalid JSON"), 400)
            return
        model = body.get("model", "")
        if not model:
            self.send_json(err_obj("model is required", "missing_model"), 400)
            return
        do_stream = body.get("stream", False)
        log.debug(f"Ollama generate request: model={model}, stream={do_stream}")

        last = LAST_SUCCESS.get(model)
        if last:
            last_host, last_full = last
            tag = f"{last_host} {last_full}"
            payload = dict(body, model=last_full)
            try:
                resp = requests.post(f"{last_host}/api/generate", json=payload,
                                     timeout=TIMEOUT, stream=do_stream)
            except Exception as e:
                log.debug(f"  ✗ {tag}  (exception: {e})")
                add_bad(last_host, model)
            else:
                if resp.status_code == 200:
                    if not do_stream:
                        data = resp.json()
                        if "error" not in data:
                            log.debug(f"  ✓ {tag}")
                            LAST_SUCCESS[model] = (last_host, last_full)
                            add_good(last_host, model)
                            self.send_json(data, 200)
                            return
                    else:
                        it = resp.iter_lines()
                        first_line = None
                        for line in it:
                            if not line:
                                continue
                            first_line = line
                            break
                        if first_line:
                            try:
                                first = json.loads(first_line)
                            except json.JSONDecodeError:
                                first = None
                            if first and "error" not in first:
                                log.debug(f"  ✓ {tag}")
                                LAST_SUCCESS[model] = (last_host, last_full)
                                add_good(last_host, model)
                                self.send_response(200)
                                self.send_header("Content-Type", "application/x-ndjson")
                                self.send_header("Cache-Control", "no-cache")
                                self.end_headers()
                                self.wfile.flush()
                                self.wfile.write(first_line + b"\n")
                                self.wfile.flush()
                                for line in it:
                                    if not line:
                                        continue
                                    try:
                                        self.wfile.write(line + b"\n")
                                        obj = json.loads(line)
                                        if obj.get("done"):
                                            self.wfile.flush()
                                            self.close_connection = True
                                            return
                                    except (BrokenPipeError, ConnectionResetError, OSError):
                                        log.debug("Client disconnected during stream")
                                        return
                                self.close_connection = True
                                return
                add_bad(last_host, model)

        servers = find_servers(model)
        if not servers:
            self.send_json(err_obj(f"no available servers for '{model}'", "model_not_found"), 404)
            return

        all_failed = []
        for _, _, host, ms in servers:
            full = ms[0]
            tag = f"{host} {full}"
            payload = dict(body, model=full)

            try:
                resp = requests.post(f"{host}/api/generate", json=payload,
                                     timeout=TIMEOUT, stream=do_stream)
            except Exception as e:
                log.debug(f"  ✗ {tag}  (exception: {e})")
                add_bad(host, model)
                all_failed.append(host)
                continue

            if resp.status_code != 200:
                log.debug(f"  ✗ {tag}  (status {resp.status_code})")
                add_bad(host, model)
                all_failed.append(host)
                continue

            if not do_stream:
                data = resp.json()
                if "error" in data:
                    log.debug(f"  ✗ {tag}  (error: {data['error']})")
                    add_bad(host, model)
                    all_failed.append(host)
                    continue
                tried = len(all_failed) + 1
                log.debug(f"  ✓ {tag}")
                LAST_SUCCESS[model] = (host, full)
                add_good(host, model)
                broadcast_activity(host, full, "connected", f"served {model}: {tried}/{len(servers)} servers")
                self.send_json(data, 200)
                return

            it = resp.iter_lines()
            first_line = None
            for line in it:
                if not line:
                    continue
                first_line = line
                break

            if not first_line:
                log.debug(f"  ✗ {tag}  (empty response)")
                add_bad(host, model)
                all_failed.append(host)
                continue

            try:
                first = json.loads(first_line)
            except json.JSONDecodeError:
                log.debug(f"  ✗ {tag}  (bad response)")
                add_bad(host, model)
                all_failed.append(host)
                continue

            if "error" in first:
                log.debug(f"  ✗ {tag}  (error: {first['error']})")
                add_bad(host, model)
                all_failed.append(host)
                continue

            tried = len(all_failed) + 1
            log.debug(f"  ✓ {tag}")
            LAST_SUCCESS[model] = (host, full)
            add_good(host, model)
            broadcast_activity(host, full, "connected", f"served {model}: {tried}/{len(servers)} servers")

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.flush()
            self.wfile.write(first_line + b"\n")
            self.wfile.flush()

            for line in it:
                if not line:
                    continue
                try:
                    self.wfile.write(line + b"\n")
                    obj = json.loads(line)
                    if obj.get("done"):
                        self.wfile.flush()
                        self.close_connection = True
                        return
                except (BrokenPipeError, ConnectionResetError, OSError):
                    log.debug("Client disconnected during stream")
                    return

            self.close_connection = True
            return

        log.debug(f"  ✗ all servers failed: {', '.join(all_failed)}")
        self.send_json(err_obj(f"all servers failed"), 502)

    def _try_one(self, host, model, full_model, opayload):
        """Try one host synchronously (no streaming). Return Ollama response dict or None."""
        tag = f"{host} {full_model}"
        payload = dict(opayload, model=full_model, stream=False)
        try:
            resp = requests.post(f"{host}/api/chat", json=payload, timeout=TIMEOUT)
        except Exception as e:
            log.debug(f"  ✗ {tag}  (exception: {e})")
            add_bad(host, model)
            return None

        if resp.status_code != 200:
            log.debug(f"  ✗ {tag}  (status {resp.status_code})")
            add_bad(host, model)
            return None

        data = resp.json()
        if "error" in data:
            log.debug(f"  ✗ {tag}  (error: {data['error']})")
            add_bad(host, model)
            return None

        log.debug(f"  ✓ {tag}")
        LAST_SUCCESS[model] = (host, full_model)
        add_good(host, model)
        return data

    def _try_stream(self, model, servers, opayload):
        """Try one host for streaming (last-success shortcut). Return True on success.

        Uses a single ``iter_lines()`` generator for both the first and subsequent
        chunks, so generator-internal buffers are never abandoned.
        """
        _, _, host, ms = servers[0]
        full = ms[0]
        tag = f"{host} {full}"
        payload = dict(opayload, model=full, stream=True)
        broadcast_activity(host, full, "trying", f"trying {host}...")

        try:
            resp = requests.post(f"{host}/api/chat", json=payload, timeout=TIMEOUT, stream=True)
        except Exception as e:
            log.debug(f"  ✗ {tag}  (exception: {e})")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} failed ({e})")
            return None

        if resp.status_code != 200:
            log.debug(f"  ✗ {tag}  (status {resp.status_code})")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} returned {resp.status_code}")
            return None

        lines_iter = resp.iter_lines()
        first_line = None
        for line in lines_iter:
            if not line:
                continue
            first_line = line
            break

        if not first_line:
            log.debug(f"  ✗ {tag}  (empty response)")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} empty response")
            return None

        try:
            first = json.loads(first_line)
        except json.JSONDecodeError:
            log.debug(f"  ✗ {tag}  (bad response)")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} bad response")
            return None

        if "error" in first:
            log.debug(f"  ✗ {tag}  (error: {first['error']})")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} error: {first['error']}")
            return None

        log.debug(f"  ✓ {tag}")
        LAST_SUCCESS[model] = (host, full)
        add_good(host, model)
        broadcast_activity(host, full, "connected", f"connected to {host}")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.flush()

        self.wfile.write(sse_str(sse_chunk(full, first.get("message", {}).get("content", ""))).encode())
        self.wfile.flush()

        for line in lines_iter:
            if not line:
                continue
            obj = json.loads(line)
            content = obj.get("message", {}).get("content", "")
            try:
                if content:
                    self.wfile.write(sse_str(sse_chunk(full, content)).encode())
                if obj.get("done"):
                    fr = obj.get("done_reason", "stop")
                    self.wfile.write(sse_str(sse_chunk(full, "", done=True, finish_reason=fr)).encode())
                    self.wfile.flush()
                    self.close_connection = True
                    return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                log.debug("Client disconnected during stream")
                return None

        self.close_connection = True
        return True

    def _try_stream_ollama(self, model, servers, opayload):
        """Try one host for streaming in Ollama NDJSON format (last-success shortcut)."""
        _, _, host, ms = servers[0]
        full = ms[0]
        tag = f"{host} {full}"
        payload = dict(opayload, model=full, stream=True)
        broadcast_activity(host, full, "trying", f"trying {host}...")

        try:
            resp = requests.post(f"{host}/api/chat", json=payload, timeout=TIMEOUT, stream=True)
        except Exception as e:
            log.debug(f"  ✗ {tag}  (exception: {e})")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} failed ({e})")
            return None

        if resp.status_code != 200:
            log.debug(f"  ✗ {tag}  (status {resp.status_code})")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} returned {resp.status_code}")
            return None

        lines_iter = resp.iter_lines()
        first_line = None
        for line in lines_iter:
            if not line:
                continue
            first_line = line
            break

        if not first_line:
            log.debug(f"  ✗ {tag}  (empty response)")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} empty response")
            return None

        try:
            first = json.loads(first_line)
        except json.JSONDecodeError:
            log.debug(f"  ✗ {tag}  (bad response)")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} bad response")
            return None

        if "error" in first:
            log.debug(f"  ✗ {tag}  (error: {first['error']})")
            add_bad(host, model)
            broadcast_activity(host, full, "failed", f"{host} error: {first['error']}")
            return None

        log.debug(f"  ✓ {tag}")
        LAST_SUCCESS[model] = (host, full)
        add_good(host, model)
        broadcast_activity(host, full, "connected", f"connected to {host}")

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.flush()

        self.wfile.write(first_line + b"\n")
        self.wfile.flush()

        for line in lines_iter:
            if not line:
                continue
            try:
                self.wfile.write(line + b"\n")
                obj = json.loads(line)
                if obj.get("done"):
                    self.wfile.flush()
                    self.close_connection = True
                    return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                log.debug("Client disconnected during stream")
                return None

        self.close_connection = True
        return True

    def _handle_stream(self, model, servers, opayload):
        """Try servers sequentially for SSE streaming (OpenAI format)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.flush()

        send_status = getattr(self, "_send_status", False)
        model_str = model or next(iter({ms[0] for _, _, _, ms in servers}), "unknown")
        broadcast_activity("", model_str, "searching", f"searching {len(servers)} servers for {model_str}...")
        self._sse_status(send_status, "", model_str, f"searching {len(servers)} servers for {model_str}...", "searching")

        all_failed = []
        for _, _, host, ms in servers:
            full = ms[0]
            tag = f"{host} {full}"
            broadcast_activity(host, full, "trying", f"trying {host}...")
            self._sse_status(send_status, host, full, f"trying {host}...", "trying")

            try:
                resp = requests.post(f"{host}/api/chat", json=dict(opayload, model=full, stream=True), timeout=TIMEOUT, stream=True)
            except Exception as e:
                log.debug(f"  ✗ {tag}  (exception: {e})")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} failed ({e})")
                self._sse_status(send_status, host, full, f"{host} failed ({e})", "failed")
                all_failed.append(host)
                continue

            if resp.status_code != 200:
                log.debug(f"  ✗ {tag}  (status {resp.status_code})")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} returned {resp.status_code}")
                self._sse_status(send_status, host, full, f"{host} returned {resp.status_code}", "failed")
                all_failed.append(host)
                continue

            it = resp.iter_lines()
            first_line = None
            for line in it:
                if not line:
                    continue
                first_line = line
                break

            if not first_line:
                log.debug(f"  ✗ {tag}  (empty response)")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} empty response")
                all_failed.append(host)
                continue

            try:
                first = json.loads(first_line)
            except json.JSONDecodeError:
                log.debug(f"  ✗ {tag}  (bad response)")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} bad response")
                all_failed.append(host)
                continue

            if "error" in first:
                log.debug(f"  ✗ {tag}  (error: {first['error']})")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} error: {first['error']}")
                all_failed.append(host)
                continue

            tried = len(all_failed) + 1
            log.debug(f"  ✓ {tag}")
            LAST_SUCCESS[model] = (host, full)
            add_good(host, model)
            broadcast_activity(host, full, "connected", f"served {model}: {tried}/{len(servers)} servers")
            self._sse_status(send_status, host, full, f"served {model}: {tried}/{len(servers)} servers", "connected")

            try:
                self.wfile.write(sse_str(sse_chunk(full, first.get("message", {}).get("content", ""))).encode())
            except (BrokenPipeError, ConnectionResetError, OSError):
                log.debug("Client disconnected")
                return

            for line in it:
                if not line:
                    continue
                obj = json.loads(line)
                content = obj.get("message", {}).get("content", "")
                try:
                    if content:
                        self.wfile.write(sse_str(sse_chunk(full, content)).encode())
                    if obj.get("done"):
                        fr = obj.get("done_reason", "stop")
                        self.wfile.write(sse_str(sse_chunk(full, "", done=True, finish_reason=fr)).encode())
                        self.wfile.flush()
                        self.close_connection = True
                        return
                except (BrokenPipeError, ConnectionResetError, OSError):
                    log.debug("Client disconnected during stream")
                    return

            self.close_connection = True
            return

        log.debug(f"  ✗ all servers failed: {', '.join(all_failed)}")
        self._sse_status(send_status, "", model_str, "all servers failed", "failed")
        try:
            self.wfile.write(sse_str({"error": "all servers failed"}).encode())
            self.wfile.write(sse_str(sse_chunk("", "", done=True)).encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.debug("Client disconnected")
        self.close_connection = True

    def _sse_status(self, enabled, host, model, message, status):
        if not enabled:
            return
        try:
            self.wfile.write(sse_status(host, model, message, status).encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.debug("Client disconnected")

    def _handle_stream_ollama(self, model, servers, opayload):
        """Try servers one at a time and passthrough Ollama NDJSON format."""
        all_failed = []
        broadcast_activity("", model, "searching", f"searching {len(servers)} servers for {model}...")
        for _, _, host, ms in servers:
            full = ms[0]
            tag = f"{host} {full}"
            payload = dict(opayload, model=full, stream=True)
            broadcast_activity(host, full, "trying", f"trying {host}...")

            try:
                resp = requests.post(f"{host}/api/chat", json=payload, timeout=TIMEOUT, stream=True)
            except Exception as e:
                log.debug(f"  ✗ {tag}  (exception: {e})")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} failed ({e})")
                all_failed.append(host)
                continue

            if resp.status_code != 200:
                log.debug(f"  ✗ {tag}  (status {resp.status_code})")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} returned {resp.status_code}")
                all_failed.append(host)
                continue

            it = resp.iter_lines()
            first_line = None
            for line in it:
                if not line:
                    continue
                first_line = line
                break

            if not first_line:
                log.debug(f"  ✗ {tag}  (empty response)")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} empty response")
                all_failed.append(host)
                continue

            try:
                first = json.loads(first_line)
            except json.JSONDecodeError:
                log.debug(f"  ✗ {tag}  (bad response)")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} bad response")
                all_failed.append(host)
                continue

            if "error" in first:
                log.debug(f"  ✗ {tag}  (error: {first['error']})")
                add_bad(host, model)
                broadcast_activity(host, full, "failed", f"{host} error: {first['error']}")
                all_failed.append(host)
                continue

            tried = len(all_failed) + 1
            log.debug(f"  ✓ {tag}")
            LAST_SUCCESS[model] = (host, full)
            add_good(host, model)
            broadcast_activity(host, full, "connected", f"served {model}: {tried}/{len(servers)} servers")

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.flush()

            self.wfile.write(first_line + b"\n")
            self.wfile.flush()

            for line in it:
                if not line:
                    continue
                try:
                    self.wfile.write(line + b"\n")
                    obj = json.loads(line)
                    if obj.get("done"):
                        self.wfile.flush()
                        self.close_connection = True
                        return
                except (BrokenPipeError, ConnectionResetError, OSError):
                    log.debug("Client disconnected during stream")
                    return

            self.close_connection = True
            return

        log.debug(f"  ✗ all servers failed: {', '.join(all_failed)}")
        self.send_json(err_obj(f"all servers failed"), 502)

    def send_json(self, data, status=200):
        """Send *data* as a JSON HTTP response."""
        body = json.dumps(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        if not getattr(self, 'head_request', False):
            self.wfile.write(body.encode())

    def log_message(self, fmt, *args):
        log.debug(f"[{self.log_date_time_string()}] {self.client_address[0]} - {fmt % args}")

    def safe_write(self, data):
        """Write *data* to the client, raising on disconnect."""
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.debug("Client disconnected")
            raise


class PoolServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server that reuses the listening address."""
    allow_reuse_address = True
    daemon_threads = True


def main():
    """Parse args and start the HTTP server."""
    global TIMEOUT, PORT
    parser = argparse.ArgumentParser(description="dumpster-dive - OpenAI-compatible proxy for free Ollama servers")
    parser.add_argument("--port", "-p", type=int, default=PORT, help=f"port to listen on (default: {PORT})")
    parser.add_argument("--host", type=str, default="", help="host address to bind to (default: all interfaces)")
    parser.add_argument("--timeout", "-t", type=int, default=30, help="request timeout in seconds (default: 30)")
    args = parser.parse_args()
    TIMEOUT = args.timeout

    addr = (args.host, args.port)
    try:
        srv = PoolServer(addr, Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            log.error(f"Address already in use: {addr[0] or '0.0.0.0'}:{addr[1]}")
            print(f"Address already in use: {addr[0] or '0.0.0.0'}:{addr[1]}", file=sys.stderr)
        raise
    PORT = srv.server_address[1]
    log.debug(f"Starting dumpster-dive on port {PORT}, POOL_SIZE={POOL_SIZE}, TIMEOUT={TIMEOUT}")
    print(f"dumpster-dive :{PORT}  POOL_SIZE={POOL_SIZE}  TIMEOUT={TIMEOUT}", file=sys.stderr)
    def stop(sig, frm):
        log.debug("Shutting down")
        print("\nbye", file=sys.stderr)
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
