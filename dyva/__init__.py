#!/usr/bin/env python3
import argparse
import asyncio
import fnmatch
import json
import logging
import os
import subprocess
import sys
import time

import aiohttp
from aiohttp import web

LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOGLEVEL, logging.INFO),
    format="%(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("dumpster-dive")

CACHE_DIR = os.path.expanduser("~/.cache/free-ollama")
CACHE_FILE = os.path.join(CACHE_DIR, "free-ollama.json")
BAD_FILE = os.path.join(CACHE_DIR, "bad-hosts.txt")
GOOD_FILE = os.path.join(CACHE_DIR, "good-hosts.txt")
LAST_FILE = os.path.join(CACHE_DIR, "last-success.json")
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "3"))
_last_cache = None

PORT = int(os.environ.get("PORT", "11434"))
TIMEOUT = 30

_bad_cache = None
_good_cache = None
_servers_cache = None

_activity_queues = []
_activity_history = []
_activity_lock = asyncio.Lock()
_ACTIVITY_HISTORY_MAX = 500


async def broadcast_activity(host, model, status, message, duration=None):
    entry = {'host': host, 'model': model, 'status': status, 'message': message, 'time': time.time()}
    if duration is not None:
        entry['duration'] = round(duration, 2)
    async with _activity_lock:
        _activity_history.append(entry)
        if len(_activity_history) > _ACTIVITY_HISTORY_MAX:
            _activity_history[:100] = []
        dead = []
        for q in _activity_queues:
            try:
                await q.put(entry)
            except Exception:
                dead.append(q)
        for q in dead:
            _activity_queues.remove(q)


async def _add_activity_listener(q):
    async with _activity_lock:
        _activity_queues.append(q)


async def _remove_activity_listener(q):
    async with _activity_lock:
        if q in _activity_queues:
            _activity_queues.remove(q)


def refresh_cache():
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
    need = False
    if not os.path.exists(CACHE_FILE) or os.path.getsize(CACHE_FILE) == 0:
        need = True
    elif time.time() - os.path.getmtime(CACHE_FILE) > 86400:
        need = True
    if need:
        refresh_cache()


def load_servers():
    global _servers_cache
    ensure_cache()
    if _servers_cache is None:
        if not os.path.exists(CACHE_FILE):
            return []
        with open(CACHE_FILE) as f:
            _servers_cache = json.load(f)
    return _servers_cache or []


def load_bad():
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
    global _bad_cache
    with open(BAD_FILE, "a") as f:
        f.write(f"{host} {model}\n")
    if _bad_cache is not None:
        _bad_cache.add(f"{host} {model}")


def load_good():
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
    key = f"{host} {model}"
    good = load_good()
    if key not in good:
        with open(GOOD_FILE, "a") as f:
            f.write(f"{key}\n")
        if _good_cache is not None:
            _good_cache.add(key)


def load_last():
    global _last_cache
    if os.path.exists(LAST_FILE):
        with open(LAST_FILE) as f:
            _last_cache = json.load(f)
    else:
        _last_cache = {}


def save_last():
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(LAST_FILE, "w") as f:
        json.dump(_last_cache, f)


def get_last(model):
    if _last_cache is None:
        load_last()
    entry = _last_cache.get(model)
    if entry:
        return (entry["host"], entry["full"])
    return None


def set_last(model, host, full):
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
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(model_name, f"*{pattern}*")
    return pattern in model_name


async def probe_host(session, host):
    try:
        resp = await asyncio.wait_for(session.get(f"{host}/api/ps"), timeout=TIMEOUT)
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
        return False
    if resp.status != 200:
        await resp.release()
        return False
    try:
        await resp.json()
    except Exception:
        await resp.release()
        return False
    await resp.release()
    return True


def find_servers(sub):
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


_sort_toggle = False
def all_models():
    global _sort_toggle
    servers = load_servers()
    seen = {}
    _sort_toggle = not _sort_toggle
    for s in servers:
        for m in s.get("models", []):
            if ":cloud" in m or len(m) == 0:
                continue
            if m not in seen:
                seen[m] = {'id': m, 'count': 1}
            else:
                seen[m]['count'] += 1
    if _sort_toggle:
        sorty = sorted(seen.values(), key=lambda x: x.get('count'))
    else:
        sorty = sorted(seen.values(), key=lambda x: x.get('id').lower())
    return list(sorty)


def to_ollama(body):
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
    e = {"message": msg}
    if code:
        e["code"] = code
    return {"error": e}


def sse_chunk(model, content, done=False, finish_reason="stop"):
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
    return f"data: {json.dumps(obj)}\n\n"


async def _race_servers(session, model, servers, payload, do_stream, endpoint="/api/chat"):
    done = asyncio.Event()
    result_queue = asyncio.Queue()
    server_iter = iter(servers)
    iter_lock = asyncio.Lock()

    async def worker():
        resp = None
        while not done.is_set():
            async with iter_lock:
                try:
                    _, host, ms = next(server_iter)
                except StopIteration:
                    return

            full = ms[0]

            if not await probe_host(session, host):
                continue

            start = time.time()
            tag = f"{host} {full}"
            p = dict(payload, model=full, stream=do_stream)
            try:
                resp = await asyncio.wait_for(
                    session.post(f"{host}{endpoint}", json=p),
                    timeout=TIMEOUT,
                )
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
                dur = time.time() - start
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - {type(e).__name__}", duration=dur)
                continue

            if resp.status != 200:
                dur = time.time() - start
                await resp.release()
                resp = None
                add_bad(host, model)
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - status {resp.status}", duration=dur)
                continue

            if not do_stream:
                try:
                    data = await resp.json()
                except asyncio.TimeoutError:
                    dur = time.time() - start
                    await resp.release()
                    resp = None
                    await broadcast_activity(host, model, "failed",
                        f"failure: {host} for {model} - timeout", duration=dur)
                    continue
                except json.JSONDecodeError:
                    dur = time.time() - start
                    await resp.release()
                    resp = None
                    add_bad(host, model)
                    await broadcast_activity(host, model, "failed",
                        f"failure: {host} for {model} - bad response", duration=dur)
                    continue
                await resp.release()
                if "error" in data:
                    dur = time.time() - start
                    add_bad(host, model)
                    await broadcast_activity(host, model, "failed",
                        f"failure: {host} for {model} - error: {data['error']}", duration=dur)
                    continue
                dur = time.time() - start
                log.debug(f"  \u2713 {tag}")
                set_last(model, host, full)
                add_good(host, model)
                await broadcast_activity(host, model, "connected",
                    f"success: {host} for {model}", duration=dur)
                await result_queue.put(("ok", host, full, data))
                done.set()
                return

            try:
                first_line = await resp.content.readline()
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
                await resp.release()
                resp = None
                continue
            if not first_line or not first_line.strip():
                dur = time.time() - start
                await resp.release()
                resp = None
                add_bad(host, model)
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - empty response", duration=dur)
                continue

            try:
                first = json.loads(first_line)
            except json.JSONDecodeError:
                dur = time.time() - start
                await resp.release()
                resp = None
                add_bad(host, model)
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - bad response", duration=dur)
                continue

            if "error" in first:
                dur = time.time() - start
                await resp.release()
                resp = None
                add_bad(host, model)
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - error: {first['error']}", duration=dur)
                continue

            if done.is_set():
                await resp.release()
                return

            dur = time.time() - start
            log.debug(f"  \u2713 {tag}")
            set_last(model, host, full)
            add_good(host, model)
            await broadcast_activity(host, model, "connected",
                f"success: {host} for {model}", duration=dur)
            await result_queue.put(("ok_stream", host, full, resp, first_line, first))
            done.set()
            return

        if resp is not None:
            await resp.release()

    tasks = [asyncio.create_task(worker()) for _ in range(WORKER_COUNT)]

    while True:
        try:
            result = result_queue.get_nowait()
            break
        except asyncio.QueueEmpty:
            if all(t.done() for t in tasks):
                result = None
                break
        await asyncio.sleep(0.1)

    done.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    return result


async def _try_one(session, host, model, full_model, opayload):
    tag = f"{host} {full_model}"
    start = time.time()
    payload = dict(opayload, model=full_model, stream=False)
    try:
        resp = await asyncio.wait_for(
            session.post(f"{host}/api/chat", json=payload),
            timeout=TIMEOUT,
        )
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
        dur = time.time() - start
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - {type(e).__name__}", duration=dur)
        return None

    if resp.status != 200:
        await resp.release()
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (status {resp.status})")
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - status {resp.status}", duration=dur)
        return None

    try:
        data = await resp.json()
    except asyncio.TimeoutError:
        await resp.release()
        return None
    except json.JSONDecodeError:
        await resp.release()
        dur = time.time() - start
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - bad response", duration=dur)
        return None
    await resp.release()

    if "error" in data:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (error: {data['error']})")
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - error: {data['error']}", duration=dur)
        return None

    dur = time.time() - start
    log.debug(f"  \u2713 {tag}")
    set_last(model, host, full_model)
    add_good(host, model)
    await broadcast_activity(host, model, "connected",
        f"success: {host} for {model}", duration=dur)
    return data


async def _try_host(session, host, full_model, model, payload, do_stream, endpoint="/api/chat"):
    tag = f"{host} {full_model}"
    start = time.time()
    p = dict(payload, model=full_model, stream=do_stream)
    try:
        resp = await asyncio.wait_for(
            session.post(f"{host}{endpoint}", json=p),
            timeout=TIMEOUT,
        )
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
        dur = time.time() - start
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - {type(e).__name__}", duration=dur)
        return None

    if resp.status != 200:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (status {resp.status})")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - status {resp.status}", duration=dur)
        return None

    try:
        it = resp.content
        first_line = await it.readline()
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
        dur = time.time() - start
        await resp.release()
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - {type(e).__name__}", duration=dur)
        return None

    if not first_line or not first_line.strip():
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (empty response)")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - empty response", duration=dur)
        return None
    try:
        first = json.loads(first_line)
    except json.JSONDecodeError:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (bad response)")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - bad response", duration=dur)
        return None
    if "error" in first:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (error: {first['error']})")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - error: {first['error']}", duration=dur)
        return None
    dur = time.time() - start
    log.debug(f"  \u2713 {tag}")
    set_last(model, host, full_model)
    add_good(host, model)
    await broadcast_activity(host, model, "connected",
        f"success: {host} for {model}", duration=dur)
    return resp, first_line, first


async def _forward_stream(request, response, resp, first_line, host, full, model, openai_format):
    content_type = "text/event-stream" if openai_format else "application/x-ndjson"
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-cache"
    await response.prepare(request)

    try:
        if openai_format:
            first = json.loads(first_line)
            content = first.get("message", {}).get("content", "")
            await response.write(sse_str(sse_chunk(full, content)).encode())
        else:
            await response.write(first_line + b"\n")

        async for line in resp.content:
            if not line or line == b"\n":
                continue
            line = line.rstrip(b"\n\r")
            try:
                if openai_format:
                    obj = json.loads(line)
                    content = obj.get("message", {}).get("content", "")
                    if content:
                        await response.write(sse_str(sse_chunk(full, content)).encode())
                    if obj.get("done"):
                        fr = obj.get("done_reason", "stop")
                        await response.write(sse_str(sse_chunk(full, "", done=True, finish_reason=fr)).encode())
                else:
                    await response.write(line + b"\n")
                    obj = json.loads(line)
                    if obj.get("done"):
                        pass
            except (BrokenPipeError, ConnectionResetError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
                log.debug("Client disconnected during stream")
                return
    finally:
        await resp.release()


async def _proxy_chat(request, session, model, opayload, do_stream, openai_format):
    last = get_last(model)
    if last:
        last_host, last_full = last
        log.debug(f"Reusing {last_host} for {model}")
        if do_stream:
            result = await _try_host(session, last_host, last_full, model, opayload, do_stream=True)
            if result:
                resp, first_line, first = result
                await _forward_stream(request, web.StreamResponse(), resp, first_line, last_host, last_full, model, openai_format)
                return
        else:
            data = await _try_one(session, last_host, model, last_full, opayload)
            if data:
                if openai_format:
                    return web.json_response(to_openai(data, model))
                else:
                    return web.json_response(data)

    servers = find_servers(model)
    if not servers:
        return web.json_response(err_obj(f"no available servers for '{model}'", "model_not_found"), status=404)

    if do_stream:
        result = await _race_servers(session, model, servers, opayload, do_stream=True)
        if result:
            _, host, full, resp, first_line, first = result
            await _forward_stream(request, web.StreamResponse(), resp, first_line, host, full, model, openai_format)
            return
        if openai_format:
            return web.Response(
                text=sse_str({"error": "all servers failed"}) + sse_str(sse_chunk("", "", done=True)),
                content_type="text/event-stream",
            )
        return web.json_response(err_obj("all servers failed"), status=502)

    result = await _race_servers(session, model, servers, dict(opayload, stream=False), do_stream=False)
    if result:
        _, host, full, data = result
        if openai_format:
            return web.json_response(to_openai(data, model))
        return web.json_response(data)

    return web.json_response(err_obj("all servers failed"), status=502)


async def _proxy_generate(request, session):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response(err_obj("invalid JSON"), status=400)

    model = body.get("model", "")
    if not model:
        return web.json_response(err_obj("model is required", "missing_model"), status=400)
    do_stream = body.get("stream", False)
    log.debug(f"Ollama generate request: model={model}, stream={do_stream}")
    endpoint = "/api/generate"

    last = get_last(model)
    if last:
        last_host, last_full = last
        log.debug(f"Reusing {last_host} for {model}")
        if do_stream:
            result = await _try_host(session, last_host, last_full, model, body, do_stream=True, endpoint=endpoint)
            if result:
                resp, first_line, first = result
                await _forward_stream(request, web.StreamResponse(), resp, first_line, last_host, last_full, model, openai_format=False)
                return
        else:
            start = time.time()
            p = dict(body, model=last_full, stream=False)
            try:
                r = await asyncio.wait_for(
                    session.post(f"{last_host}{endpoint}", json=p),
                    timeout=TIMEOUT,
                )
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
                dur = time.time() - start
                await broadcast_activity(last_host, model, "failed",
                    f"failure: {last_host} for {model} - {type(e).__name__}", duration=dur)
                pass
            else:
                if r.status == 200:
                    try:
                        data = await r.json()
                    except Exception:
                        data = None
                    await r.release()
                    if data and "error" not in data:
                        dur = time.time() - start
                        set_last(model, last_host, last_full)
                        add_good(last_host, model)
                        await broadcast_activity(last_host, model, "connected",
                            f"success: {last_host} for {model}", duration=dur)
                        return web.json_response(data)
                    dur = time.time() - start
                    add_bad(last_host, model)
                    await broadcast_activity(last_host, model, "failed",
                        f"failure: {last_host} for {model} - bad response", duration=dur)
                else:
                    dur = time.time() - start
                    await r.release()
                    add_bad(last_host, model)
                    await broadcast_activity(last_host, model, "failed",
                        f"failure: {last_host} for {model} - status {r.status}", duration=dur)

    servers = find_servers(model)
    if not servers:
        return web.json_response(err_obj(f"no available servers for '{model}'", "model_not_found"), status=404)

    if do_stream:
        result = await _race_servers(session, model, servers, body, do_stream=True, endpoint=endpoint)
        if result:
            _, host, full, resp, first_line, first = result
            await _forward_stream(request, web.StreamResponse(), resp, first_line, host, full, model, openai_format=False)
            return
        return web.json_response(err_obj("all servers failed"), status=502)

    result = await _race_servers(session, model, servers, dict(body, stream=False), do_stream=False, endpoint=endpoint)
    if result:
        _, host, full, data = result
        return web.json_response(data)

    return web.json_response(err_obj("all servers failed"), status=502)


async def handle_dashboard(request):
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
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def handle_v1_models(request):
    models = all_models()
    return web.json_response({
        "object": "list",
        "data": models,
    })


async def handle_clear_bad(request):
    global _bad_cache
    _bad_cache = None
    if os.path.exists(BAD_FILE):
        os.remove(BAD_FILE)
    raise web.HTTPFound("/")


async def handle_refresh_cache(request):
    refresh_cache()
    raise web.HTTPFound("/")


async def handle_api_tags(request):
    graffiti = [
        ":::::::-.  ",  " ;;,   `';,",  " `[[     [[",  "  $$,    $$",
        "  888_,o8P'",  "  MMMMP'`  ",  " ...    :::",  " ;;     ;;;",
        "[['     [[[",  "$$      $$$",  "88    .d888",  ".'YmmMMMM''",
        ";;,.    ;;;",  "[[[[, ,[[[[",  "$$$$$$$$'$$",  "888 Y88' 88",
        "MMM  M'  'M",  "::::::::::.",  " `;;;```.;;",  "  `]]nnn]]'",
        "   $$$''   ",  "   888o    ",  "   YMMMb   ",  " .::::::. ",
        ";;;`    ` ",  "'[==/[[[[,",  "  '''    $",  " 88b    dP",
        "  'YMmMY' ",  ":::::::::::",  ";;;;;;;;'''",  "     [[    ",
        "     $$    ",  "     88,   ",  "     MMM   ",  ".,::::::  ",
        ";;;;''''  ",  " [[cccc   ",  " $$''''   ",  " 888oo,__ ",
        " ''''YUMMM",  ":::::::..  ",  ";;;;``;;;; ",  " [[[,/[[[' ",
        " $$$$$$c   ",  " 888b '88bo",  " MMMM   'W'",  "           ",
        "           ",  ":::::::-.  ",  " ;;,   `';,",  " `[[     [[",
        "  $$,    $$",  "  888_,o8P'",  "  MMMMP'`  ",  ".-:.     ::",
        " ';;.   ;;;",  "   '[[,[[['",  "     c$$'  ",  "   ,8P'`   ",
        "  mM'      ",  ":::      .:",  "';;,   ,;;;",  " \\[[  .[[/ ",
        "  Y$c.$$'  ",  "   Y88P    ",  "    MP     ",  "  :::.     ",
        "  ;;`;;    ",  " ,[[ '[[,  ",  "c$$$cc$$$c ",  " 888   888,",
        " YMM   ''` ",  "           ",  "           ",  "  -~===~-  ",
        "           ",  "           ",  "  _______ ",  " |   _   |",
        " |.  1___|",  " |.  __)  ",  " |:  |    ",  " |::.|    ",
        " `---'    ",  "  _______ ",  " |   _   \\", " |.  l   /",
        " |.  _   1",  " |:  |   |",  " |::.|:. |",  " `--- ---'",
        "  _______ ",  " |   _   |",  " |.  1___|",  " |.  __)_ ",
        " |:  1   |",  " |::.. . |",  " `-------'",  "  _______ ",
        " |   _   |",  " |.  1___|",  " |.  __)_ ",  " |:  1   |",
        " |::.. . |",  " `-------'",  "          ",  "          ",
        "  _______ ",  " |   _   |",  " |.  |   |",  " |:  1   |",
        " |::.. . |",  " `-------'",  "  ___     ",  " |   |    ",
        " |.  |    ",  " |.  |___ ",  " |:  1   |",  " |::.. . |",
        " `-------'",  "  ___     ",  " |   |    ",  " |.  |    ",
        " |.  |___ ",  " |:  1   |",  " |::.. . |",  " `-------'",
        "  _______ ",  " |   _   |",  " |.  1   |",  " |.  _   |",
        " |:  |   |",  " |::.|:. |",  " `--- ---'",  "  ___ ___ ",
        " |   Y   |",  " |.      |",  " |. \\_/  |",  " |:  |   |",
        " |::.|:. |",  " `--- ---'",  "  _______ ",  " |   _   |",
        " |.  1   |",  " |.  _   |",  " |:  |   |",  " |::.|:. |",
        " `--- ---'",  "          ",  "          ",  "  : :: :  ",
        "          ",  "          "]
    models = all_models()
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.localtime(time.time() - (7 * 24 * 60 * 60)))
    return web.json_response({
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


async def handle_api_version(request):
    return web.json_response({"version": "0.5.0"})


async def handle_api_activity(request):
    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Access-Control-Allow-Origin"] = "*"
    await response.prepare(request)

    async with _activity_lock:
        for entry in _activity_history:
            try:
                await response.write(f"data: {json.dumps(entry)}\n\n".encode())
            except (BrokenPipeError, ConnectionResetError, OSError):
                return response

    q = asyncio.Queue()
    await _add_activity_listener(q)
    try:
        while True:
            try:
                entry = await asyncio.wait_for(q.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                await response.write(f"data: {json.dumps(entry)}\n\n".encode())
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
    finally:
        await _remove_activity_listener(q)
    return response


async def handle_refresh(request):
    await broadcast_activity("", "", "searching", "refreshing server cache...")
    refresh_cache()
    return web.json_response({"status": "ok", "message": "cache refreshed"})


async def handle_api_show(request):
    return web.json_response({
        "modelfile": "# free-ollama proxy",
        "details": {"parent_model": "", "format": "gguf", "family": "", "families": None,
                    "parameter_size": "", "quantization_level": ""},
        "model_info": {},
    })





async def handle_ollama_chat(request):
    async with request.app["semaphore"]:
        session = request.app["session"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(err_obj("invalid JSON"), status=400)
        model = body.get("model", "")
        if not model:
            return web.json_response(err_obj("model is required", "missing_model"), status=400)
        do_stream = body.get("stream", False)
        opayload = body
        log.debug(f"Ollama chat request: model={model}, stream={do_stream}")
        return await _proxy_chat(request, session, model, opayload, do_stream, openai_format=False)


async def handle_openai_chat(request):
    async with request.app["semaphore"]:
        session = request.app["session"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(err_obj("invalid JSON"), status=400)
        model = body.get("model", "")
        if not model:
            return web.json_response(err_obj("model is required", "missing_model"), status=400)
        do_stream = body.get("stream", False)
        opayload = to_ollama(body)
        log.debug(f"OpenAI request: model={model}, stream={do_stream}")
        return await _proxy_chat(request, session, model, opayload, do_stream, openai_format=True)


async def handle_ollama_generate(request):
    async with request.app["semaphore"]:
        return await _proxy_generate(request, request.app["session"])


def make_app():
    app = web.Application()

    async def on_startup(app):
        app["session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None),
        )
        app["semaphore"] = asyncio.Semaphore(WORKER_COUNT)

    async def on_shutdown(app):
        await app["session"].close()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/v1/models", handle_v1_models)
    app.router.add_get("/clear-bad", handle_clear_bad)
    app.router.add_get("/refresh-cache", handle_refresh_cache)
    app.router.add_get("/api/tags", handle_api_tags)
    app.router.add_get("/api/version", handle_api_version)
    app.router.add_get("/api/activity", handle_api_activity)
    app.router.add_get("/refresh", handle_refresh)
    app.router.add_get("/api/show", handle_api_show)
    app.router.add_post("/api/show", handle_api_show)
    app.router.add_post("/api/chat", handle_ollama_chat)
    app.router.add_post("/api/generate", handle_ollama_generate)
    app.router.add_post("/v1/chat/completions", handle_openai_chat)

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.router.add_static("/", static_dir, show_index=False)

    return app


def main():
    global TIMEOUT, PORT

    parser = argparse.ArgumentParser(description="dumpster-dive - OpenAI-compatible proxy for free Ollama servers")
    parser.add_argument("--port", "-p", type=int, default=PORT, help=f"port to listen on (default: {PORT})")
    parser.add_argument("--host", type=str, default="", help="host address to bind to (default: all interfaces)")
    parser.add_argument("--timeout", "-t", type=int, default=30, help="request timeout in seconds (default: 30)")
    args = parser.parse_args()
    TIMEOUT = args.timeout

    PORT = args.port
    log.info(f"Starting dumpster-dive on port {PORT}, WORKER_COUNT={WORKER_COUNT}, TIMEOUT={TIMEOUT}")

    app = make_app()

    def stop():
        log.debug("Shutting down")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        web.run_app(app, host=args.host or "0.0.0.0", port=PORT, print=lambda *a: None)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
