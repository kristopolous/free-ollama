#!/usr/bin/env python3
import argparse
import asyncio
import fnmatch
import csv
import json
import logging
import os
import subprocess
import sys
import time
import requests
import importlib.metadata

import aiohttp
from aiohttp import web
from aiohttp_swagger3 import SwaggerDocs, SwaggerInfo, SwaggerUiSettings

LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOGLEVEL, logging.INFO),
    format="%(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("dumpster-dyva")

CACHE_DIR = os.path.expanduser("~/.cache/free-ollama")
CACHE_FILE = os.path.join(CACHE_DIR, "free-ollama.json")
BAD_FILE = os.path.join(CACHE_DIR, "bad-hosts.txt")
GOOD_FILE = os.path.join(CACHE_DIR, "good-hosts.txt")
LAST_FILE = os.path.join(CACHE_DIR, "last-success.json")
GRAFLEX_WORKING = os.path.join(CACHE_DIR, "image-gen-working.json")
_last_cache = None
_LOCAL = False
_CURLIFY = False

PORT = 11434
TIMEOUT = 30
VERSION = "0"

_bad_cache = None
_good_cache = None
_servers_cache = None

_activity_queues = []

GITHUB_URL = "https://github.com/kristopolous/free-ollama"
_activity_history = []
_activity_lock = asyncio.Lock()
_ACTIVITY_HISTORY_MAX = 500


async def broadcast_activity(host, model, status, message, duration=None, wid=None):
    if wid:
        message = f"<{wid.replace('Task-','')}> {message}"
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
    _db = f'{CACHE_DIR}/free-ollama.json'

    for url, loc in [
       ( 'https://raw.githubusercontent.com/forrany/Awesome-Ollama-Server/refs/heads/main/public/data.json', f"{_db}-forrany.tmp" ),
       ( 'https://raw.githubusercontent.com/PuddinCat/OllamaSpider/refs/heads/main/url_models.json', f"{_db}-spider.tmp" ),
       ( 'https://raw.githubusercontent.com/happyshua/ollamalist/refs/heads/main/output_with_models.csv', f"{_db}-happyshua.tmp" )
    ]:
      try:
        response = requests.get(url)
        logging.info(f"Grabbing {url}")
      except Exception as ex:
        logging.warning(f"Unable to get {url}: {ex}")

      with open(loc, "w") as f:
        f.write(response.text)

    host_map = {}

    with open(f'{_db}-forrany.tmp', 'r') as f:
      try:
        for row in json.loads(f.read()):
          host_map[row.get('server')] = row
      except Exception as ex:
        logging.warning(f"Unable to parse {_db}-forrany.tmp: {ex}")

    with open(f"{_db}-happyshua.tmp", 'r') as csvfile:
      for r in csv.reader(csvfile):
        ip = r[0]
        models = [m.strip() for m in r[1].split(',')]
        if ip not in host_map:
          host_map[ip] = {'tps': 0, 'models': [], 'server': ip}
  
        host_map[ip]['models'] += models

    if os.path.exists(f'{CACHE_DIR}/image-gen-working.json'):
      with open(f'{CACHE_DIR}/image-gen-working.json', 'r') as f:
        for row in json.loads(f.read()):
          if 'host' in row:
            row['server'] = row['host']
          host_map[row.get('server')] = row

    with open(f'{_db}-spider.tmp', 'r') as f:
      for row in json.loads(f.read()):
        ip = row.get('url')
        models = [ n.get('name') for n in row.get('models') ]
        if ip not in host_map:
          host_map[ip] = {'tps': 0, 'models': [], 'server': ip}

        host_map[ip]['models'] += models

    for k,v in host_map.items():
      if 'service' not in v:
        v['service'] = 'ollama'
      v['models'] = list(set(v['models']))

    with open(_db, 'w') as f:
      json.dump(list(host_map.values()), f)


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
    if '/' in sub:
        res = []
        for model in sub.split('/'):
            res += find_servers(model)
        return res

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
            matched.append((1, host, ms))
        else:
            _last = get_last(sub)
            is_last = _last is not None and host == _last[0]
            matched.append((-2 if is_last else (-1 if key in good else 0), host, ms))
    matched.sort(key=lambda x: x[0])
    return matched


def all_models():
    servers = load_servers()
    seen = {}
    for s in servers:
        for m in s.get("models", []):
            if ":cloud" in m or len(m) == 0:
                continue
            if m not in seen:
                seen[m] = {'id': m, 'count': 1}
            else:
                seen[m]['count'] += 1
    if True: #int(time.time() % 2) == 0:
        sorty = sorted(seen.values(), key=lambda x: x.get('count'))
    else:
        sorty = sorted(seen.values(), key=lambda x: x.get('id').lower())
    return list(sorty)


def to_ollama(body):
    if "max_tokens" in body:
        body["num_predict"] = body["max_tokens"]
    if "response_format" in body:
        rf = body.pop("response_format")
        tp = rf.get("type", "") if isinstance(rf, dict) else ""
        if tp == "json_object":
            body["format"] = "json"
        elif tp == "json_schema":
            js = rf.get("json_schema", {})
            if isinstance(js, dict) and "schema" in js:
                body["format"] = js["schema"]
            else:
                body["format"] = rf
        elif tp == "text":
            pass
        else:
            body["format"] = rf
    for m in body.get("messages", []):
        if m.get("content") is None:
            m["content"] = ""
    return body


def _fmt_tool_calls(tcs):
    out = []
    for i, tc in enumerate(tcs):
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, dict):
            args = json.dumps(args)
        out.append({
            "id": f"call_{int(time.time())}_{i}",
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": args},
        })
    return out


def to_openai(resp, model):
    msg = dict(resp.get("message", {}))
    tcs = msg.pop("tool_calls", None)
    if tcs:
        msg["tool_calls"] = _fmt_tool_calls(tcs)
    fr = "tool_calls" if tcs else "stop"
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": fr}],
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


def sse_chunk(model, delta, done=False, finish_reason="stop"):
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason if done else None}],
    }


def sse_str(obj):
    return f"data: {json.dumps(obj)}\n\n"


def _curlify(method, url, json_body):
    if not _CURLIFY:
        return
    try:
        import requests as req_lib
        import curlify
        r = req_lib.Request(method.upper(), url, json=json_body, headers={"Content-Type": "application/json"})
        s = req_lib.Session()
        p = s.prepare_request(r)
        logging.info(curlify.to_curl(p))
    except Exception as e:
        logging.debug(f"curlify failed: {e}")


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

            wid = asyncio.current_task().get_name()
            await broadcast_activity(host, model, "trying",
                f"trying: {host} for {model}", wid=wid)

            if not await probe_host(session, host):
                continue

            start = time.time()
            tag = f"{host} {full}"
            p = dict(payload, model=full, stream=do_stream)
            _curlify("POST", f"{host}{endpoint}", p)
            try:
                resp = await asyncio.wait_for(
                    session.post(f"{host}{endpoint}", json=p),
                    timeout=TIMEOUT,
                )
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
                dur = time.time() - start
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - {type(e).__name__}", duration=dur, wid=wid)
                continue

            if resp.status != 200:
                dur = time.time() - start
                code = resp.status
                await resp.release()
                resp = None
                add_bad(host, model)
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - status {code}", duration=dur, wid=wid)
                continue

            if not do_stream:
                try:
                    data = await resp.json()
                except asyncio.TimeoutError:
                    dur = time.time() - start
                    await resp.release()
                    resp = None
                    await broadcast_activity(host, model, "failed",
                        f"failure: {host} for {model} - timeout", duration=dur, wid=wid)
                    continue
                except json.JSONDecodeError:
                    dur = time.time() - start
                    await resp.release()
                    resp = None
                    add_bad(host, model)
                    await broadcast_activity(host, model, "failed",
                        f"failure: {host} for {model} - bad response", duration=dur, wid=wid)
                    continue
                await resp.release()
                if "error" in data:
                    dur = time.time() - start
                    add_bad(host, model)
                    await broadcast_activity(host, model, "failed",
                        f"failure: {host} for {model} - error: {data['error']}", duration=dur, wid=wid)
                    continue
                dur = time.time() - start
                log.debug(f"  \u2713 {tag}")
                set_last(model, host, full)
                add_good(host, model)
                await broadcast_activity(host, model, "connected",
                    f"success: {host} for {model}", duration=dur, wid=wid)
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
                    f"failure: {host} for {model} - empty response", duration=dur, wid=wid)
                continue

            try:
                first = json.loads(first_line)
            except json.JSONDecodeError:
                dur = time.time() - start
                await resp.release()
                resp = None
                add_bad(host, model)
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - bad response", duration=dur, wid=wid)
                continue

            if "error" in first:
                dur = time.time() - start
                await resp.release()
                resp = None
                add_bad(host, model)
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - error: {first['error']}", duration=dur, wid=wid)
                continue

            if done.is_set():
                await resp.release()
                return

            dur = time.time() - start
            log.debug(f"  \u2713 {tag}")
            set_last(model, host, full)
            add_good(host, model)
            await broadcast_activity(host, model, "connected",
                f"success: {host} for {model}", duration=dur, wid=wid)
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
    wid = asyncio.current_task().get_name()
    await broadcast_activity(host, model, "trying",
        f"trying: {host} for {model}", wid=wid)
    tag = f"{host} {full_model}"
    start = time.time()
    payload = dict(opayload, model=full_model, stream=False)
    _curlify("POST", f"{host}/api/chat", payload)
    try:
        resp = await asyncio.wait_for(
            session.post(f"{host}/api/chat", json=payload),
            timeout=TIMEOUT,
        )
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
        dur = time.time() - start
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - {type(e).__name__}", duration=dur, wid=wid)
        return None

    if resp.status != 200:
        await resp.release()
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (status {resp.status})")
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - status {resp.status}", duration=dur, wid=wid)
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
            f"failure: {host} for {model} - bad response", duration=dur, wid=wid)
        return None
    await resp.release()

    if "error" in data:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (error: {data['error']})")
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - error: {data['error']}", duration=dur, wid=wid)
        return None

    dur = time.time() - start
    log.debug(f"  \u2713 {tag}")
    set_last(model, host, full_model)
    add_good(host, model)
    await broadcast_activity(host, model, "connected",
        f"success: {host} for {model}", duration=dur, wid=wid)
    return data


async def _try_host(session, host, full_model, model, payload, do_stream, endpoint="/api/chat"):
    wid = asyncio.current_task().get_name()
    await broadcast_activity(host, model, "trying",
        f"trying: {host} for {model}", wid=wid)
    tag = f"{host} {full_model}"
    start = time.time()
    p = dict(payload, model=full_model, stream=do_stream)
    _curlify("POST", f"{host}{endpoint}", p)
    try:
        resp = await asyncio.wait_for(
            session.post(f"{host}{endpoint}", json=p),
            timeout=TIMEOUT,
        )
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
        dur = time.time() - start
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - {type(e).__name__}", duration=dur, wid=wid)
        return None

    if resp.status != 200:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (status {resp.status})")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - status {resp.status}", duration=dur, wid=wid)
        return None

    try:
        it = resp.content
        first_line = await it.readline()
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
        dur = time.time() - start
        await resp.release()
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - {type(e).__name__}", duration=dur, wid=wid)
        return None

    if not first_line or not first_line.strip():
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (empty response)")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - empty response", duration=dur, wid=wid)
        return None
    try:
        first = json.loads(first_line)
    except json.JSONDecodeError:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (bad response)")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - bad response", duration=dur, wid=wid)
        return None
    if "error" in first:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (error: {first['error']})")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - error: {first['error']}", duration=dur, wid=wid)
        return None
    dur = time.time() - start
    log.debug(f"  \u2713 {tag}")
    set_last(model, host, full_model)
    add_good(host, model)
    await broadcast_activity(host, model, "connected",
        f"success: {host} for {model}", duration=dur, wid=wid)
    return resp, first_line, first


async def _forward_stream(request, response, resp, first_line, host, full, model, openai_format):
    content_type = "text/event-stream" if openai_format else "application/x-ndjson"
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-cache"
    await response.prepare(request)

    try:
        if openai_format:
            first = json.loads(first_line)
            msg = dict(first.get("message", {}))
            tcs = msg.pop("tool_calls", None)
            if tcs:
                msg["tool_calls"] = _fmt_tool_calls(tcs)
            await response.write(sse_str(sse_chunk(full, msg)).encode())
        else:
            await response.write(first_line + b"\n")

        async for line in resp.content:
            if not line or line == b"\n":
                continue
            line = line.rstrip(b"\n\r")
            try:
                if openai_format:
                    obj = json.loads(line)
                    if obj.get("done"):
                        fr = obj.get("done_reason", "stop")
                        await response.write(sse_str(sse_chunk(full, {}, done=True, finish_reason=fr)).encode())
                    else:
                        msg = dict(obj.get("message", {}))
                        tcs = msg.pop("tool_calls", None)
                        if tcs:
                            msg["tool_calls"] = _fmt_tool_calls(tcs)
                        await response.write(sse_str(sse_chunk(full, msg)).encode())
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
                stream_resp = web.StreamResponse()
                await _forward_stream(request, stream_resp, resp, first_line, last_host, last_full, model, openai_format)
                return stream_resp
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
            stream_resp = web.StreamResponse()
            await _forward_stream(request, stream_resp, resp, first_line, host, full, model, openai_format)
            return stream_resp
        if openai_format:
            return web.Response(
                text=sse_str({"error": "all servers failed"}) + sse_str(sse_chunk("", {}, done=True)),
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
                stream_resp = web.StreamResponse()
                await _forward_stream(request, stream_resp, resp, first_line, last_host, last_full, model, openai_format=False)
                return stream_resp
        else:
            wid = asyncio.current_task().get_name()
            await broadcast_activity(last_host, model, "trying",
                f"trying: {last_host} for {model}", wid=wid)
            start = time.time()
            p = dict(body, model=last_full, stream=False)
            _curlify("POST", f"{last_host}{endpoint}", p)
            try:
                r = await asyncio.wait_for(
                    session.post(f"{last_host}{endpoint}", json=p),
                    timeout=TIMEOUT,
                )
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
                dur = time.time() - start
                await broadcast_activity(last_host, model, "failed",
                    f"failure: {last_host} for {model} - {type(e).__name__}", duration=dur, wid=wid)
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
                            f"success: {last_host} for {model}", duration=dur, wid=wid)
                        return web.json_response(data)
                    dur = time.time() - start
                    add_bad(last_host, model)
                    await broadcast_activity(last_host, model, "failed",
                        f"failure: {last_host} for {model} - bad response", duration=dur, wid=wid)
                else:
                    dur = time.time() - start
                    await r.release()
                    add_bad(last_host, model)
                    await broadcast_activity(last_host, model, "failed",
                        f"failure: {last_host} for {model} - status {r.status}", duration=dur, wid=wid)

    servers = find_servers(model)
    if not servers:
        return web.json_response(err_obj(f"no available servers for '{model}'", "model_not_found"), status=404)

    if do_stream:
        result = await _race_servers(session, model, servers, body, do_stream=True, endpoint=endpoint)
        if result:
            _, host, full, resp, first_line, first = result
            stream_resp = web.StreamResponse()
            await _forward_stream(request, stream_resp, resp, first_line, host, full, model, openai_format=False)
            return stream_resp
        return web.json_response(err_obj("all servers failed"), status=502)

    result = await _race_servers(session, model, servers, dict(body, stream=False), do_stream=False, endpoint=endpoint)
    if result:
        _, host, full, data = result
        return web.json_response(data)

    return web.json_response(err_obj("all servers failed"), status=502)


async def handle_dashboard(request):
    """
    Dashboard (HTML)
    ---
    tags: [UI]
    summary: Dashboard with server status, models, and activity
    responses:
      '200':
        description: HTML dashboard page
        content:
          text/html:
            schema:
              type: string
    """
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
        for h in sorted(good, key=lambda x: (x.split(" ", 1)[1], x.split(" ", 1)[0]))[:30]
    )
    good_more = f'<div class="more">... and {len(good) - 30} more</div>' if len(good) > 30 else ""
    bad_rows = "".join(
        f'<div class="model-item"><span class="host-name">{h.split(" ", 1)[0]}</span><span class="host-model">{h.split(" ", 1)[1]}</span></div>'
        for h in sorted(bad)[:30]
    )
    bad_more = f'<div class="more">... and {len(bad) - 30} more</div>' if len(bad) > 30 else ""
    html = html.replace("__PORT__", str(PORT))
    html = html.replace("__WORKER_COUNT__", str(WORKER_COUNT))
    html = html.replace("__TIMEOUT__", str(TIMEOUT))
    html = html.replace("__SERVER_COUNT__", str(len(servers)))
    html = html.replace("__MODEL_COUNT__", str(len(models)))
    html = html.replace("__DYVA_VERSION__", VERSION)
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
    html = html.replace("__GOOD_HOSTS_DATA__", json.dumps(sorted(good)))
    html = html.replace("__BAD_HOSTS_DATA__", json.dumps(sorted(bad)))
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def handle_v1_models(request):
    """
    List models (OpenAI-compatible)
    ---
    tags: [Models]
    summary: List available models (OpenAI /v1/models format)
    responses:
      '200':
        description: List of models
        content:
          application/json:
            schema:
              type: object
              properties:
                object:
                  type: string
                data:
                  type: array
                  items:
                    type: object
                    properties:
                      id:
                        type: string
                      count:
                        type: integer
    """
    resp = _check_local(request)
    if resp:
        return resp
    models = all_models()
    return web.json_response({
        "object": "list",
        "data": models,
    })


async def handle_clear_bad(request):
    """
    Clear bad hosts list
    ---
    tags: [Admin]
    summary: Clear all failed host+model pairs and redirect to dashboard
    responses:
      '302':
        description: Redirect to dashboard
    """
    global _bad_cache
    _bad_cache = None
    if os.path.exists(BAD_FILE):
        os.remove(BAD_FILE)
    raise web.HTTPFound("/")


async def handle_api_tags(request):
    """
    List models (Ollama-compatible)
    ---
    tags: [Models]
    summary: List available models (Ollama /api/tags format)
    responses:
      '200':
        description: List of models
        content:
          application/json:
            schema:
              type: object
              properties:
                models:
                  type: array
                  items:
                    type: object
                    properties:
                      name:
                        type: string
                      model:
                        type: string
                      modified_at:
                        type: string
                      size:
                        type: integer
                      digest:
                        type: string
    """
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
                "digest": f"{graffiti[m % len(graffiti)]}       {m:05}a{VERSION}000000000000000000000000000000000000000000",
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
    """
    Get server version
    ---
    tags: [Info]
    summary: Return the dyva proxy version
    responses:
      '200':
        description: Version info
        content:
          application/json:
            schema:
              type: object
              properties:
                version:
                  type: string
    """
    return web.json_response({"version": f"DYVA-{VERSION}"})


async def handle_api_activity(request):
    """
    Activity stream (SSE)
    ---
    tags: [Monitoring]
    summary: Server-sent events stream of real-time proxy activity
    responses:
      '200':
        description: SSE stream of activity events
        content:
          text/event-stream:
            schema:
              type: string
    """
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
            if entry is None:
                break
            try:
                await response.write(f"data: {json.dumps(entry)}\n\n".encode())
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
    finally:
        await _remove_activity_listener(q)
    return response


async def handle_refresh(request):
    """
    Refresh server cache
    ---
    tags: [Admin]
    summary: Re-fetch server lists from all sources
    responses:
      '200':
        description: Cache refreshed
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                message:
                  type: string
    """
    await broadcast_activity("", "", "searching", "refreshing server cache...")
    refresh_cache()
    return web.json_response({"status": "ok", "message": "cache refreshed"})


async def handle_api_ps(request):
    """
    Running models (Ollama-compatible)
    ---
    tags: [Models]
    summary: List currently cached models (Ollama /api/ps format)
    responses:
      '200':
        description: List of running models
        content:
          application/json:
            schema:
              type: object
              properties:
                models:
                  type: array
                  items:
                    type: object
    """
    models_list = []
    if _last_cache:
        for i, (model, entry) in enumerate(_last_cache.items()):
            expires_at = "9999-12-31T23:59:59.000000Z"
            models_list.append({
                "name": model,
                "model": model,
                "size": 0,
                "digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "llama",
                    "families": None,
                    "parameter_size": "N/A",
                    "quantization_level": "N/A"
                },
                "expires_at": expires_at,
                "size_vram": 0
            })
    return web.json_response({"models": models_list})


async def handle_api_show(request):
    """
    Show model info (Ollama-compatible)
    ---
    tags: [Models]
    summary: Get details about a specific model (Ollama /api/show format)
    requestBody:
      required: false
      content:
        application/json:
          schema:
            type: object
            properties:
              model:
                type: string
              name:
                type: string
      description: Model name in JSON body or ?model= query param
    responses:
      '200':
        description: Model details
        content:
          application/json:
            schema:
              type: object
    """
    model = ""
    ct = request.content_type or ""
    if ct == "application/json" or ct == "application/x-ndjson":
        try:
            body = await request.json()
            model = body.get("name", body.get("model", ""))
        except (json.JSONDecodeError, Exception):
            pass

    if not model:
        model = request.query.get("model", "")

    hosts = [h[1].rstrip("/v1") for h in find_servers(model)]
    system_val = " ".join(hosts) if hosts else "No hosts available for this model."
    details = {
        "parent_model": "",
        "format": "gguf",
        "family": "",
        "families": None,
        "parameter_size": "",
        "quantization_level": "",
    }
    return web.json_response({
        "modelfile": f"# free-ollama proxy — model: {model}",
        "parameters": "",
        "template": "",
        "system": system_val,
        "details": details,
        "model_info": {},
        "capabilities": ["completion", "vision", "audio", "tools", "thinking"]
    })


async def handle_api_pull(request):
    """
    Pull/refresh models (Ollama-compatible)
    ---
    tags: [Admin]
    summary: Refresh server cache (Ollama /api/pull format)
    responses:
      '200':
        description: Cache refreshed
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
    """
    refresh_cache()
    return web.json_response({"status": "success"})




def _check_local(request):
    if not _LOCAL:
        return None
    remote = request.remote
    if remote in ("127.0.0.1", "::1", "localhost"):
        return None
    return web.json_response({"error": f"{GITHUB_URL} — Or execute 'uvx dyva' to run your own"}, status=403)


async def handle_ollama_chat(request):
    """
    Chat completion (Ollama format)
    ---
    tags: [Chat]
    summary: Chat completion using Ollama /api/chat format. Proxies to upstream Ollama servers.
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required: [model]
            properties:
              model:
                type: string
                description: Model name
              messages:
                type: array
                description: Chat messages
                items:
                  type: object
                  properties:
                    role:
                      type: string
                      enum: [system, user, assistant]
                    content:
                      type: string
              stream:
                type: boolean
                default: false
              options:
                type: object
                properties:
                  temperature:
                    type: number
                  num_predict:
                    type: integer
    responses:
      '200':
        description: Chat completion response (NDJSON stream or JSON)
        content:
          application/x-ndjson:
            schema:
              type: string
          application/json:
            schema:
              type: object
      '400':
        description: Invalid request
      '404':
        description: Model not found
      '502':
        description: All servers failed
    """
    resp = _check_local(request)
    if resp:
        return resp
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
    """
    Chat completion (OpenAI format)
    ---
    tags: [Chat]
    summary: Chat completion using OpenAI /v1/chat/completions format. Translates to Ollama format and proxies upstream.
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            required: [model, messages]
            properties:
              model:
                type: string
                description: Model name
              messages:
                type: array
                description: Chat messages
                items:
                  type: object
                  properties:
                    role:
                      type: string
                      type: string
                    content:
                      type: string
              stream:
                type: boolean
                default: false
              temperature:
                type: number
              max_tokens:
                type: integer
              top_p:
                type: number
    responses:
      '200':
        description: Chat completion response (SSE stream or JSON)
        content:
          text/event-stream:
            schema:
              type: string
          application/json:
            schema:
              type: object
      '400':
        description: Invalid request
      '404':
        description: Model not found
      '502':
        description: All servers failed
    """
    resp = _check_local(request)
    if resp:
        return resp
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
    """
    Text generation (Ollama format)
    ---
    tags: [Generation]
    summary: Text completion using Ollama /api/generate format. Proxies to upstream Ollama servers.
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            properties: {}
    responses:
      '200':
        description: Generation response
      '400':
        description: Invalid request
      '502':
        description: All servers failed
    """
    resp = _check_local(request)
    if resp:
        return resp
    async with request.app["semaphore"]:
        return await _proxy_generate(request, request.app["session"])


async def handle_sd_models(request):
    """
    List available SD models across all A1111 hosts
    ---
    tags: [Image]
    summary: List Stable Diffusion models available on discovered A1111 hosts
    responses:
      '200':
        description: List of SD models
        content:
          application/json:
            schema:
              type: object
              properties:
                models:
                  type: array
                  items:
                    type: string
    """
    servers = load_servers()
    seen = {}
    for s in servers:
        if s.get("service") != "a1111":
            continue
        for m in s.get("models", []):
            title = m.split(" [")[0] if " [" in m else m
            if title not in seen:
                seen[title] = {"id": title, "hosts": [], "count": 0}
            seen[title]["count"] += 1
            h = s.get("server", "")
            if h not in seen[title]["hosts"]:
                seen[title]["hosts"].append(h)
    return web.json_response({
        "object": "list",
        "data": sorted(seen.values(), key=lambda x: -x["count"]),
    })


async def handle_txt2img(request):
    """
    Text-to-image generation (A1111 format)
    ---
    tags: [Image]
    summary: Generate images from text prompts. Proxies to A1111 Stable Diffusion WebUI servers.
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              model:
                type: string
                description: SD model name to match against (optional)
              prompt:
                type: string
                description: Text prompt
              negative_prompt:
                type: string
              steps:
                type: integer
                default: 20
              width:
                type: integer
                default: 512
              height:
                type: integer
                default: 512
              cfg_scale:
                type: number
                default: 7
              sampler_name:
                type: string
                default: Euler
              seed:
                type: integer
                default: -1
              batch_size:
                type: integer
                default: 1
    responses:
      '200':
        description: Generated images (base64-encoded)
        content:
          application/json:
            schema:
              type: object
              properties:
                images:
                  type: array
                  items:
                    type: string
                    description: Base64-encoded PNG
                parameters:
                  type: string
                info:
                  type: string
      '400':
        description: Invalid request
      '502':
        description: Upstream error
      '503':
        description: No available image-gen hosts
    """
    resp = _check_local(request)
    if resp:
        return resp
    session = request.app["session"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    IMG_KEY = "__a1111__"

    model_filter = body.pop("model", None)
    activity_label = (model_filter or body.get("prompt", "txt2img"))[:60]

    last = get_last(IMG_KEY)
    if last:
        last_host, _ = last
        log.debug(f"Reusing {last_host} for txt2img")
        await broadcast_activity(last_host, activity_label, "trying",
            f"txt2img: {activity_label} on {last_host}")
        try:
            async with session.post(
                f"http://{last_host}/sdapi/v1/txt2img", json=body,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    await broadcast_activity(last_host, activity_label, "connected",
                        f"txt2img ✓", duration=0)
                    return web.json_response(data)
                add_bad(last_host, IMG_KEY)
            await broadcast_activity(last_host, activity_label, "failed",
                f"txt2img: HTTP {r.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as _e:
            await broadcast_activity(last_host, activity_label, "failed",
                f"txt2img: {type(_e).__name__}")

    servers = load_servers()
    candidates = [s for s in servers if s.get("service") == "a1111"]
    if model_filter:
        candidates = [
            s for s in candidates
            if any(match_model(m.split(" [")[0] if " [" in m else m, model_filter)
                   for m in s.get("models", []))
        ]
    hosts = [s.get("server") for s in candidates]
    if not hosts:
        return web.json_response({"error": "no available image-gen hosts"}, status=503)

    good = load_good()
    hosts.sort(key=lambda h: 0 if f"{h} {IMG_KEY}" in good else 1)

    async def _race(host_list):
        done = asyncio.Event()
        result_queue = asyncio.Queue()
        host_iter = iter(host_list)
        iter_lock = asyncio.Lock()

        async def worker():
            while not done.is_set():
                async with iter_lock:
                    try:
                        host = next(host_iter)
                    except StopIteration:
                        return
                await broadcast_activity(host, activity_label, "trying",
                    f"txt2img: {activity_label}")
                try:
                    t0 = time.time()
                    async with session.post(
                        f"http://{host}/sdapi/v1/txt2img", json=body,
                        timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            set_last(IMG_KEY, host, "")
                            add_good(host, IMG_KEY)
                            await broadcast_activity(host, activity_label, "connected",
                                f"txt2img ✓", duration=time.time() - t0)
                            await result_queue.put(data)
                            done.set()
                            return
                        add_bad(host, IMG_KEY)
                        await broadcast_activity(host, activity_label, "failed",
                            f"txt2img: HTTP {r.status}")
                except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as _e:
                    await broadcast_activity(host, activity_label, "failed",
                        f"txt2img: {type(_e).__name__}")

        tasks = [asyncio.create_task(worker()) for _ in range(WORKER_COUNT)]
        try:
            while True:
                try:
                    return result_queue.get_nowait()
                except asyncio.QueueEmpty:
                    if all(t.done() for t in tasks):
                        return None
                await asyncio.sleep(0.1)
        finally:
            done.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # Phase 1: try good + untested hosts (skip known-bad)
    live_hosts = [h for h in hosts if f"{h} {IMG_KEY}" not in load_bad()]
    data = await _race(live_hosts)
    if data:
        return web.json_response(data)

    # Phase 2: exhausted — try previously bad hosts
    bad_hosts = [h for h in hosts if f"{h} {IMG_KEY}" in load_bad()]
    if bad_hosts:
        data = await _race(bad_hosts)
        if data:
            return web.json_response(data)

    # Phase 3: try comfyui hosts
    comfy_candidates = [s for s in servers if s.get("service") == "comfyui"]
    if model_filter:
        comfy_candidates = [
            s for s in comfy_candidates
            if any(match_model(m.split(" [")[0] if " [" in m else m, model_filter)
                   for m in s.get("models", []))
        ]
    comfy_hosts = [s.get("server") for s in comfy_candidates if f"{s.get('server')} {IMG_KEY}" not in load_bad()]
    if comfy_hosts:
        async def _race_comfy(host_list):
            done = asyncio.Event()
            result_queue = asyncio.Queue()
            host_iter = iter(host_list)
            iter_lock = asyncio.Lock()

            async def worker():
                while not done.is_set():
                    async with iter_lock:
                        try:
                            host = next(host_iter)
                        except StopIteration:
                            return
                    await broadcast_activity(host, activity_label, "trying",
                        f"txt2img (comfy): {activity_label}")
                    t0 = time.time()
                    data = await _txt2img_comfyui(session, host, body)
                    if data:
                        set_last(IMG_KEY, host, "")
                        add_good(host, IMG_KEY)
                        await broadcast_activity(host, activity_label, "connected",
                            f"txt2img (comfy) ✓", duration=time.time() - t0)
                        await result_queue.put(data)
                        done.set()
                        return
                    await broadcast_activity(host, activity_label, "failed",
                        f"txt2img (comfy): no response")

            tasks = [asyncio.create_task(worker()) for _ in range(min(3, len(host_list)))]
            try:
                while True:
                    try:
                        return result_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        if all(t.done() for t in tasks):
                            return None
                    await asyncio.sleep(0.1)
            finally:
                done.set()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        data = await _race_comfy(comfy_hosts)
        if data:
            return web.json_response(data)

    return web.json_response({"error": "all image-gen hosts failed"}, status=502)


async def _txt2img_comfyui(session, host, body):
    import uuid as uuid_mod

    try:
        checkpoints_resp = await session.get(
            f"http://{host}/models/checkpoints",
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if checkpoints_resp.status != 200:
            await checkpoints_resp.release()
            return None
        checkpoints = await checkpoints_resp.json()
        await checkpoints_resp.release()
        if not isinstance(checkpoints, list) or not checkpoints:
            return None
        ckpt = checkpoints[0]
    except Exception:
        return None

    prompt_text = body.get("prompt", "")
    negative_prompt = body.get("negative_prompt", "")
    width = body.get("width", 512)
    height = body.get("height", 512)
    steps = body.get("steps", 20)
    cfg = body.get("cfg_scale", 7)
    seed = body.get("seed", -1)
    if seed == -1:
        import random
        seed = random.randint(0, 2**31 - 1)
    sampler = body.get("sampler_name", "euler")

    workflow = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "EmptyLatentImage", "inputs": {"batch_size": 1, "height": height, "width": width}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": prompt_text}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": negative_prompt}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0],
            "latent_image": ["2", 0], "seed": seed, "steps": steps,
            "cfg": cfg, "sampler_name": sampler, "scheduler": "normal", "denoise": 1,
        }},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "dyva_output", "images": ["6", 0]}},
    }

    client_id = str(uuid_mod.uuid4())
    try:
        prompt_resp = await session.post(
            f"http://{host}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if prompt_resp.status != 200:
            await prompt_resp.release()
            return None
        prompt_data = await prompt_resp.json()
        await prompt_resp.release()
        prompt_id = prompt_data.get("prompt_id")
        if not prompt_id:
            return None
    except Exception:
        return None

    deadline = time.time() + 120
    while time.time() < deadline:
        await asyncio.sleep(2)
        try:
            hist_resp = await session.get(
                f"http://{host}/history/{prompt_id}",
                timeout=aiohttp.ClientTimeout(total=30),
            )
            if hist_resp.status != 200:
                await hist_resp.release()
                continue
            hist = await hist_resp.json()
            await hist_resp.release()
        except Exception:
            continue
        entry = hist.get(prompt_id)
        if not entry:
            continue
        if entry.get("status", {}).get("completed"):
            outputs = entry.get("outputs", {})
            for node_id, out in outputs.items():
                images = out.get("images", [])
                for img in images:
                    try:
                        view_resp = await session.get(
                            f"http://{host}/view",
                            params={"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")},
                            timeout=aiohttp.ClientTimeout(total=30),
                        )
                        if view_resp.status == 200:
                            raw = await view_resp.read()
                            await view_resp.release()
                            import base64
                            b64 = base64.b64encode(raw).decode()
                            return {"images": [b64], "parameters": "{}", "info": json.dumps({"prompt": body})}
                        await view_resp.release()
                    except Exception:
                        pass
            break
        if entry.get("status", {}).get("status_str") == "error":
            break

    return None


def make_app():
    app = web.Application()

    async def on_startup(app):
        app["session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None),
        )
        app["semaphore"] = asyncio.Semaphore(WORKER_COUNT)

    async def on_shutdown(app):
        async with _activity_lock:
            for q in _activity_queues:
                await q.put(None)
            _activity_queues.clear()
        await app["session"].close()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    swagger = SwaggerDocs(
        app,
        info=SwaggerInfo(title="dyva", version=VERSION, description="Ollama/OpenAI-compatible proxy that routes to free servers"),
        swagger_ui_settings=SwaggerUiSettings(path="/docs"),
    )

    swagger.add_get("/", handle_dashboard)
    swagger.add_get("/dashboard", handle_dashboard)
    swagger.add_get("/v1/models", handle_v1_models)
    swagger.add_get("/clear-bad", handle_clear_bad)
    swagger.add_get("/api/tags", handle_api_tags)
    swagger.add_get("/api/ps", handle_api_ps)
    swagger.add_get("/api/version", handle_api_version)
    swagger.add_get("/api/activity", handle_api_activity)

    swagger.add_get("/refresh", handle_refresh)

    swagger.add_post("/api/show", handle_api_show)
    swagger.add_post("/api/pull", handle_api_pull)
    swagger.add_post("/api/chat", handle_ollama_chat)
    swagger.add_post("/api/generate", handle_ollama_generate)
    swagger.add_post("/v1/chat/completions", handle_openai_chat)
    swagger.add_get("/sdapi/v1/sd-models", handle_sd_models)
    swagger.add_post("/sdapi/v1/txt2img", handle_txt2img)

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.router.add_static("/", static_dir, show_index=False)

    return app

def banner():
    global VERSION
    try:
        VERSION=importlib.metadata.version('dyva')
    except Exception as e:
        VERSION="(git)"
    print(f"""
\\\\       DDDDd.  YY  yY Vv    vV   aa     //
 l'>      DD  Dd  YyyY   Vv  vV   aAAa   <-l
 ll       DD  Dd   yY     VvvV   aA  Aa   ll
 llama~  DDDDd"   yY       VV   aA    Aa  llama~
 || ||               v{VERSION}               || ||
 '' ''               𝗔𝗟𝗣𝗔𝗖𝗔               '' ''
""")

def main():
    global TIMEOUT, PORT, WORKER_COUNT, _LOCAL, _CURLIFY

    parser = argparse.ArgumentParser(description="dumpster-dyva - Like the Ollama :cloud models, but you don't pay.")
    parser.add_argument("-p", "--port",     type=int, default=PORT, help=f"port to listen on (default: {PORT})")
    parser.add_argument("-u", "--host",     type=str, default="", help="host address to bind to (default: all interfaces)")
    parser.add_argument("-t", "--timeout",  type=int, default=30, help="request timeout in seconds (default: 30)")
    parser.add_argument("-r", "--refresh",  action="store_true", help="refresh cache")
    parser.add_argument("-w", "--workers",  type=int, default=3, help="number of workers (default: 3)")
    parser.add_argument("-l", "--local",    action="store_true", help="restrict inference endpoints to localhost only")
    parser.add_argument("--curlify", action="store_true", help="print curl commands of upstream requests to stderr")
    parser.add_argument("-v", "--version",  action="store_true", help="show version information")
    args = parser.parse_args()

    if args.refresh:
        refresh_cache()
        print("Refreshed cache")
        sys.exit(0)

    banner()
    if args.version:
        sys.exit(0)

    WORKER_COUNT = args.workers
    TIMEOUT = args.timeout
    PORT = args.port
    _LOCAL = args.local
    _CURLIFY = args.curlify
    log.info(f"Starting dumpster-dyva on port {PORT}, WORKER_COUNT={WORKER_COUNT}, TIMEOUT={TIMEOUT}, LOCAL={_LOCAL}")

    app = make_app()

    def stop():
        log.debug("Shutting down")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        web.run_app(app, host=args.host or "0.0.0.0", port=PORT, print=lambda *a: None)

    except (KeyboardInterrupt, SystemExit):
        log.info("Server exiting by keyboard interrupt")
        pass

    except Exception as e:
        parser.print_help()
        print(f"\n  ----- ERROR -----\n [ Unable to Start ]\n  ==-------------==\n\n{e}\n\n")


if __name__ == "__main__":
    main()
