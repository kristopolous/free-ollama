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
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty

CACHE_DIR = os.path.expanduser("~/.cache/free-ollama")
CACHE_FILE = os.path.join(CACHE_DIR, "free-ollama.json")
BAD_FILE = os.path.join(CACHE_DIR, "bad-hosts.txt")
GOOD_FILE = os.path.join(CACHE_DIR, "good-hosts.txt")
LAST_FILE = os.path.join(CACHE_DIR, "last-success.json")
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "3"))
_last_cache = None

# LAST_SUCCESS is managed through accessors (get_last/set_last) and persisted
# to LAST_FILE as {model: {host, full, ctime, count}}.

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
    pkg_dir = os.path.dirname(__file__)
    script = os.path.join(os.path.dirname(pkg_dir), "free-ollama")
    if not os.path.exists(script):
        script = os.path.join(pkg_dir, "free-ollama")
    if not os.path.exists(script):
        script = "free-ollama"
    try:
        subprocess.run([script], capture_output=True, timeout=120)
    except FileNotFoundError:
        log.warning("free-ollama script not found — cache refresh skipped")
    except subprocess.TimeoutExpired:
        log.warning("free-ollama script timed out")

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
        if not os.path.exists(CACHE_FILE):
            return []
        with open(CACHE_FILE) as f:
            _servers_cache = json.load(f)
    return _servers_cache or []


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


def load_last():
    """Load LAST_SUCCESS cache from disk into _last_cache."""
    global _last_cache
    if os.path.exists(LAST_FILE):
        with open(LAST_FILE) as f:
            _last_cache = json.load(f)
    else:
        _last_cache = {}

def save_last():
    """Persist _last_cache to disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(LAST_FILE, "w") as f:
        json.dump(_last_cache, f)

def get_last(model):
    """Return (host, full) for model's last-successful server, or None."""
    if _last_cache is None:
        load_last()
    entry = _last_cache.get(model)
    if entry:
        return (entry["host"], entry["full"])
    return None

def set_last(model, host, full):
    """Record last-successful server for model, persisting both ctime and count."""
    if _last_cache is None:
        load_last()
    now = time.time()
    existing = _last_cache.get(model)
    if existing and existing.get("host") == host:
        existing["ctime"] = now
        existing["count"] = existing.get("count", 0) + 1
    else:
        _last_cache[model] = {"host": host, "full": full, "ctime": now, "count": 1}
    save_last()


def match_model(model_name, pattern):
    """Return True if model_name contains pattern, supporting fnmatch wildcards."""
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(model_name, f"*{pattern}*")
    return pattern in model_name


def probe_host(host):
    """Quick check: is the server idle and reachable via /api/ps? Returns True/False."""
    try:
        r = requests.get(f"{host}/api/ps", timeout=TIMEOUT)
        if r.status_code != 200:
            return False
        data = r.json()
        models = data.get("models", [])
        return len(models) == 0
    except Exception:
        return False


def find_servers(sub):
    """Return servers that serve model matching *sub*, sorted last-success then known-good then unknown."""
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
        _last = get_last(sub)
        is_last = _last is not None and host == _last[0]
        matched.append((-2 if is_last else (-1 if is_good else 0), host, ms))
    matched.sort(key=lambda x: x[0])
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


def _race_servers(model, servers, payload, do_stream, endpoint="/api/chat"):
    """Race WORKER_COUNT servers in parallel. Returns winner or None.

    For non-streaming: returns (\"ok\", host, full, response_json).
    For streaming:     returns (\"ok_stream\", host, full, first_line_bytes, first_dict, lines_iter).
    """
    done = threading.Event()
    result_queue = Queue()
    server_iter = iter(servers)
    iter_lock = threading.Lock()

    def worker():
        while not done.is_set():
            with iter_lock:
                try:
                    _, host, ms = next(server_iter)
                except StopIteration:
                    return
            full = ms[0]

            if not probe_host(host):
                add_bad(host, model)
                continue

            tag = f"{host} {full}"
            p = dict(payload, model=full, stream=do_stream)
            try:
                resp = requests.post(f"{host}{endpoint}", json=p, timeout=TIMEOUT, stream=do_stream)
            except Exception:
                add_bad(host, model)
                continue

            if resp.status_code != 200:
                add_bad(host, model)
                continue

            if not do_stream:
                data = resp.json()
                if "error" in data:
                    add_bad(host, model)
                    continue
                
                log.debug(f"  ✓ {tag}")
                set_last(model, host, full)
                add_good(host, model)
                broadcast_activity(host, full, "connected", f"connected to {host}")
                result_queue.put(("ok", host, full, data))
                return

            # Streaming: read first chunk to validate
            it = resp.iter_lines()
            first_line = None
            for line in it:
                if not line:
                    continue
                first_line = line
                break

            if not first_line:
                add_bad(host, model)
                continue

            try:
                first = json.loads(first_line)
            except json.JSONDecodeError:
                add_bad(host, model)
                continue

            if "error" in first:
                add_bad(host, model)
                continue

            log.debug(f"  ✓ {tag}")
            set_last(model, host, full)
            add_good(host, model)
            broadcast_activity(host, full, "connected", f"connected to {host}")
            result_queue.put(("ok_stream", host, full, first_line, first, it))
            return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKER_COUNT)]
    for t in threads:
        t.start()

    try:
        result = result_queue.get(timeout=TIMEOUT + 5)
    except Empty:
        return None

    done.set()
    return result


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
            """Clear all bad hosts."""
            global _bad_cache
            _bad_cache = None
            if os.path.exists(BAD_FILE):
                os.remove(BAD_FILE)
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
            load_last()
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
                f'<div class="model-item"><span class="host-name">{entry["host"]}</span><span class="host-model">{model}</span></div>'
                for model, entry in list(_last_cache.items())[:20]
            ) if _last_cache else '<div style="color:#999;font-size:.85rem">None</div>'
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
            html = html.replace("__WORKER_COUNT__", str(WORKER_COUNT))
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
        self._proxy_chat(model, opayload, do_stream, openai_format=True)

    def _proxy_chat(self, model, opayload, do_stream, openai_format):
        last = get_last(model)
        if last:
            last_host, last_full = last
            if probe_host(last_host):
                log.debug(f"Reusing {last_host} for {model}")
                if do_stream:
                    result = self._try_host(last_host, last_full, model, opayload, do_stream=True)
                    if result:
                        _, first_line, first, it = result
                        self._forward_stream(first_line, it, last_host, last_full, model, openai_format)
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
            result = _race_servers(model, servers, opayload, do_stream=True)
            if result:
                _, host, full, first_line, first, it = result
                self._forward_stream(first_line, it, host, full, model, openai_format)
                return
            if openai_format:
                try:
                    self.wfile.write(sse_str({"error": "all servers failed"}).encode())
                    self.wfile.write(sse_str(sse_chunk("", "", done=True)).encode())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
            else:
                self.send_json(err_obj("all servers failed"), 502)
            self.close_connection = True
            return

        result = _race_servers(model, servers, dict(opayload, stream=False), do_stream=False)
        if result:
            _, host, full, data = result
            if openai_format:
                self.send_json(to_openai(data, model), 200)
            else:
                self.send_json(data, 200)
            return

        self.send_json(err_obj("all servers failed"), 502)

    def _forward_stream(self, first_line, it, host, full, model, openai_format):
        content_type = "text/event-stream" if openai_format else "application/x-ndjson"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.flush()

        if openai_format:
            first = json.loads(first_line)
            content = first.get("message", {}).get("content", "")
            self.wfile.write(sse_str(sse_chunk(full, content)).encode())
        else:
            self.wfile.write(first_line + b"\n")
        self.wfile.flush()

        for line in it:
            if not line:
                continue
            try:
                if openai_format:
                    obj = json.loads(line)
                    content = obj.get("message", {}).get("content", "")
                    if content:
                        self.wfile.write(sse_str(sse_chunk(full, content)).encode())
                    if obj.get("done"):
                        fr = obj.get("done_reason", "stop")
                        self.wfile.write(sse_str(sse_chunk(full, "", done=True, finish_reason=fr)).encode())
                        self.wfile.flush()
                else:
                    self.wfile.write(line + b"\n")
                    obj = json.loads(line)
                    if obj.get("done"):
                        self.wfile.flush()
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                log.debug("Client disconnected during stream")
                return
        self.close_connection = True

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
        endpoint = "/api/generate"

        last = get_last(model)
        if last:
            last_host, last_full = last
            if probe_host(last_host):
                log.debug(f"Reusing {last_host} for {model}")
                if do_stream:
                    result = self._try_host(last_host, last_full, model, body, do_stream=True, endpoint=endpoint)
                    if result:
                        _, first_line, first, it = result
                        self._forward_stream(first_line, it, last_host, last_full, model, openai_format=False)
                        return
                else:
                    p = dict(body, model=last_full, stream=False)
                    try:
                        resp = requests.post(f"{last_host}{endpoint}", json=p, timeout=TIMEOUT)
                    except Exception as e:
                        log.debug(f"  ✗ {last_host} {last_full}  (exception: {e})")
                        add_bad(last_host, model)
                    else:
                        if resp.status_code == 200:
                            data = resp.json()
                            if "error" not in data:
                                set_last(model, last_host, last_full)
                                add_good(last_host, model)
                                self.send_json(data, 200)
                                return
                        add_bad(last_host, model)

        servers = find_servers(model)
        if not servers:
            self.send_json(err_obj(f"no available servers for '{model}'", "model_not_found"), 404)
            return

        if do_stream:
            result = _race_servers(model, servers, body, do_stream=True, endpoint=endpoint)
            if result:
                _, host, full, first_line, first, it = result
                self._forward_stream(first_line, it, host, full, model, openai_format=False)
                return
            self.send_json(err_obj("all servers failed"), 502)
            return

        result = _race_servers(model, servers, dict(body, stream=False), do_stream=False, endpoint=endpoint)
        if result:
            _, host, full, data = result
            self.send_json(data, 200)
            return

        self.send_json(err_obj("all servers failed"), 502)

    def _try_host(self, host, full_model, model, payload, do_stream, endpoint="/api/chat"):
        """Try one host with probe + inference. Returns (host, first_line, first, it) or None."""
        if not probe_host(host):
            add_bad(host, model)
            return None
        tag = f"{host} {full_model}"
        p = dict(payload, model=full_model, stream=do_stream)
        try:
            resp = requests.post(f"{host}{endpoint}", json=p, timeout=TIMEOUT, stream=do_stream)
        except Exception as e:
            log.debug(f"  ✗ {tag}  (exception: {e})")
            add_bad(host, model)
            broadcast_activity(host, full_model, "failed", f"{host} failed ({e})")
            return None
        if resp.status_code != 200:
            log.debug(f"  ✗ {tag}  (status {resp.status_code})")
            add_bad(host, model)
            broadcast_activity(host, full_model, "failed", f"{host} returned {resp.status_code}")
            return None
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
            broadcast_activity(host, full_model, "failed", f"{host} empty response")
            return None
        try:
            first = json.loads(first_line)
        except json.JSONDecodeError:
            log.debug(f"  ✗ {tag}  (bad response)")
            add_bad(host, model)
            broadcast_activity(host, full_model, "failed", f"{host} bad response")
            return None
        if "error" in first:
            log.debug(f"  ✗ {tag}  (error: {first['error']})")
            add_bad(host, model)
            broadcast_activity(host, full_model, "failed", f"{host} error: {first['error']}")
            return None
        log.debug(f"  ✓ {tag}")
        set_last(model, host, full_model)
        add_good(host, model)
        broadcast_activity(host, full_model, "connected", f"connected to {host}")
        return host, first_line, first, it

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
        set_last(model, host, full_model)
        add_good(host, model)
        return data

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


class PoolServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, addr, handler):
        super().__init__(addr, handler)
        self.executor = ThreadPoolExecutor(
            max_workers=WORKER_COUNT,
            thread_name_prefix="dyva-worker",
        )

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def process_request(self, request, client_address):
        self.executor.submit(self.process_request_thread, request, client_address)

    def server_close(self):
        self.executor.shutdown(wait=False)
        super().server_close()


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
        raise

    PORT = srv.server_address[1]
    log.info(f"Starting dumpster-dive on port {PORT}, WORKER_COUNT={WORKER_COUNT}, TIMEOUT={TIMEOUT}")
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
