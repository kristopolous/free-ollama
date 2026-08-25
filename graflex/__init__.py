import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sys
import time

from dotenv import load_dotenv

try:
    import aiohttp
except ImportError:
    aiohttp = None

log = logging.getLogger("graflex")

CACHE_DIR = os.path.expanduser("~/.cache/free-ollama")
HOSTS_FILE = os.path.join(CACHE_DIR, "image-gen-hosts.json")
WORKING_FILE = os.path.join(CACHE_DIR, "image-gen-working.json")
NOTWORKING_FILE = os.path.join(CACHE_DIR, "image-gen-notworking.json")
CLASSIFIER_FILE = os.path.join(CACHE_DIR, "model-classifier.json")


def _cache_file(name, suffix):
    prefix = name or "image-gen"
    return os.path.join(CACHE_DIR, f"{prefix}-{suffix}.json")

TIMEOUT = 60
BACKOFF = 15
MAX_BACKOFF = 300
SLEEP_DEFAULT = 4
STATS_EVERY = 250


def _clean_cookie(value):
    return value.replace("\\u0021", "!").strip()


FOFA_KEY = os.getenv("FOFA_KEY", "")
FOFA_COOKIE = os.getenv("FOFA_COOKIE", "")
FOFA_AUTHORIZATION = os.getenv("FOFA_AUTHORIZATION", "")
FOFA_API = "https://fofa.info/api/v1/search/all"
FOFA_WEB = "https://en.fofa.info/result"

SHODAN_KEY = _clean_cookie(os.getenv("SHODAN_KEY", ""))
SHODAN_WEB = "https://www.shodan.io/search"
SHODAN_PAGES = 2

# run_ts of the most recent fetch session, so ctrl+c in main() can suggest
# the exact -i value to resume with
_RUN_TS = None

SERVICE_CONFIG = {
    "a1111": {
        "port": 7860,
        "fofa_query": 'icon_hash="2075038152" && body="Stable Diffusion"',
        "check_path": "/sdapi/v1/sd-models",
    },
    "comfyui": {
        "port": 8188,
        "fofa_query": 'title="ComfyUI"',
        "shodan_query": 'http.title:"ComfyUI"',
        "check_path": "/models/checkpoints",
        "stats_path": "/api/system_stats",
    },
    "ollama": {
        "port": 11434,
        "fofa_query": 'body="ollama"',
        "shodan_query": '"ollama is running"',
        "check_path": "/api/tags",
    },
    "llama.cpp": {
        "port": 8080,
        "fofa_query": 'server=="llama.cpp"',
        "check_path": "/v1/models",
    },
    "vllm": {
        "port": 8000,
        "fofa_query": '{"detail": "Not Found"} && server=="uvicorn"',
        "check_path": "/v1/models",
    },
}

# Named searches aren't services: they have a FOFA query but no probe/check
# step. Fetch by name alone (-n <name>); entries land in <name>-hosts.json
# with service set to the name.
NAMED_QUERIES = {
    "gradio": 'icon_hash=="55115683"',
}


def _load_json(path, silent=False):
    if not silent:
        print(f"Loading {path}")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _save_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _entry_host(entry):
    if entry.get("host"):
        return entry["host"].rstrip("/")
    url = entry.get("url") or ""
    if url:
        return url.split("://", 1)[1].rstrip("/")
    return ""


def _value_to_host_port(v):
    from urllib.parse import urlparse

    v = v.strip()
    if v.startswith("http://") or v.startswith("https://"):
        parsed = urlparse(v)
        return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    if ":" in v:
        parts = v.split(":")
        return parts[0], int(parts[1])
    return v, 80


def _fmt_duration(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


def _model_name(model):
    if isinstance(model, str):
        return model
    if isinstance(model, dict):
        for key in ("name", "id", "title"):
            val = model.get(key)
            if isinstance(val, str):
                return val
    return ""


def _filter_models(models):
    return [m for m in models if not _model_name(m).endswith(":cloud")]


def _is_offline_msg(msg):
    msg = str(msg).lower()
    return (
        "temporary failure in name resolution" in msg
        or "failed to resolve" in msg
        or "network is unreachable" in msg
        or "no route to host" in msg
        or "getaddrinfo failed" in msg
    )


def _is_offline_error(exc):
    return type(exc).__name__ == "NameResolutionError" or _is_offline_msg(str(exc))


async def _comfyui_model_tree(session, base_url, timeout=TIMEOUT, workers=8):
    """Traverse /models -> /models/<category> and return entries prefixed with
    their category, e.g. 'checkpoints/majic_v7_sd15.safetensors'. Nested
    backslash paths are normalized to forward slashes. Returns None if the
    host doesn't expose a usable /models index (older ComfyUI)."""
    import urllib.parse

    try:
        resp = await asyncio.wait_for(
            session.get(f"{base_url}models", allow_redirects=False), timeout=timeout
        )
        if resp.status != 200:
            await resp.release()
            return None
        cats = await resp.json()
        await resp.release()
    except Exception:
        return None
    if not isinstance(cats, list) or not cats:
        return None
    cats = [c for c in cats if isinstance(c, str) and c]

    sem = asyncio.Semaphore(workers)

    async def list_folder(cat):
        async with sem:
            try:
                url = f"{base_url}models/{urllib.parse.quote(cat)}"
                r = await asyncio.wait_for(session.get(url, allow_redirects=False), timeout=timeout)
                if r.status != 200:
                    await r.release()
                    return []
                files = await r.json()
                await r.release()
            except Exception:
                return []
            if not isinstance(files, list):
                return []
            model_exts = (".safetensors", ".pth", ".gguf")
            return [
                f"{cat}/{f}".replace("\\", "/")
                for f in files
                if isinstance(f, str) and f.lower().endswith(model_exts)
            ]

    folders = await asyncio.gather(*(list_folder(c) for c in cats))
    seen = set()
    models = []
    for folder in folders:
        for m in folder:
            if m not in seen:
                seen.add(m)
                models.append(m)
    return models or None


async def _check_host(session, host, port, service, timeout=TIMEOUT):
    from datetime import datetime, timezone
    import urllib.parse

    cfg = SERVICE_CONFIG[service]
    path = cfg["check_path"]
    last_error = None

    for start_scheme in ("http", "https"):
        current_scheme = start_scheme
        current_host = host
        current_port = port
        current_path = path
        redirect_count = 0
        max_redirects = 5
        got_http_response = False

        while redirect_count < max_redirects:
            url = f"{current_scheme}://{current_host}:{current_port}{current_path}"
            base_url = f"{current_scheme}://{current_host}:{current_port}/"
            try:
                resp = await asyncio.wait_for(
                    session.get(url, allow_redirects=False), timeout=timeout
                )
                got_http_response = True
                if resp.status in (301, 302, 307, 308):
                    location = resp.headers.get("Location") or ""
                    await resp.release()
                    if not location:
                        last_error = {"error": f"HTTP {resp.status} redirect without Location"}
                        break

                    parsed = urllib.parse.urlparse(location)
                    if parsed.scheme:
                        current_scheme = parsed.scheme
                    if parsed.hostname:
                        current_host = parsed.hostname
                    if parsed.port:
                        current_port = parsed.port
                    elif parsed.scheme == "https":
                        current_port = 443
                    elif parsed.scheme == "http":
                        current_port = 80
                    # Follow real path redirects; preserve original path when
                    # the server only changed scheme/host and lazily dropped
                    # the path (e.g. 'return 301 https://$host/;' in nginx).
                    if parsed.path and parsed.path != "/":
                        current_path = parsed.path
                    if parsed.query:
                        current_path += f"?{parsed.query}"
                    redirect_count += 1
                    continue

                if resp.status != 200:
                    await resp.release()
                    last_error = {"error": f"HTTP {resp.status} ({current_scheme})"}
                    break

                if service == "a1111":
                    data = await resp.json()
                    await resp.release()
                    models = _filter_models([m.get("title", "") for m in data if isinstance(m, dict)])
                    return {
                        "service": service,
                        "url": base_url,
                        "models": models,
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
                elif service == "ollama":
                    data = await resp.json()
                    await resp.release()
                    models = _filter_models([m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)])
                    model = models[0] if models else None
                    if model:
                        show_body = {"model": model}
                        show_url = f"{base_url}api/show"
                        show_resp = await asyncio.wait_for(
                            session.post(show_url, json=show_body), timeout=timeout
                        )
                        if show_resp.status != 200:
                            await show_resp.release()
                            last_error = {"error": f"show HTTP {show_resp.status} ({current_scheme})"}
                            break
                        show_data = await show_resp.json()
                        await show_resp.release()
                        details = show_data.get("details") or {}
                        if not (details.get("family") or details.get("parameter_size") or details.get("quantization_level")):
                            last_error = {"error": f"empty show ({current_scheme})"}
                            break
                    version = None
                    try:
                        ver_resp = await asyncio.wait_for(
                            session.get(f"{base_url}api/version", allow_redirects=False), timeout=timeout
                        )
                        if ver_resp.status == 200:
                            ver_data = await ver_resp.json()
                            version = ver_data.get("version")
                        await ver_resp.release()
                    except Exception:
                        pass
                    result = {
                        "service": service,
                        "url": base_url,
                        "models": models,
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
                    if version:
                        result["version"] = version
                    return result
                elif service == "llama.cpp":
                    props_resp = await asyncio.wait_for(
                        session.get(f"{base_url}props", allow_redirects=False), timeout=timeout
                    )
                    props_status = props_resp.status
                    if props_status == 401:
                        await props_resp.release()
                        await resp.release()
                        last_error = {"error": "auth required"}
                        break
                    props_data = await props_resp.json()
                    await props_resp.release()
                    data = await resp.json()
                    await resp.release()
                    items = data.get("data", []) if isinstance(data, dict) else []
                    models = []
                    seen = set()
                    for m in items:
                        if not isinstance(m, dict):
                            continue
                        name = m.get("id", "")
                        if name and name not in seen:
                            seen.add(name)
                            models.append(name)
                    models = _filter_models(models)
                    if not models:
                        last_error = {"error": "no real models"}
                        break
                    version = props_data.get("build_info") if isinstance(props_data, dict) else None
                    result = {
                        "service": service,
                        "url": base_url,
                        "models": models,
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
                    if version:
                        result["version"] = version
                    return result
                elif service == "comfyui":
                    data = await resp.json()
                    await resp.release()
                    model_exts = (".safetensors", ".pth", ".gguf")
                    raw = data if isinstance(data, list) else []
                    models = _filter_models([m for m in raw if isinstance(m, str) and m.lower().endswith(model_exts)])
                    tree = await _comfyui_model_tree(session, base_url, timeout=timeout)
                    if tree:
                        models = _filter_models(tree)
                        result = {
                            "service": service,
                            "url": base_url,
                            "models": models,
                            "model_tree": True,
                            "checked": datetime.now(timezone.utc).isoformat(),
                        }
                    else:
                        result = {
                            "service": service,
                            "url": base_url,
                            "models": models,
                            "checked": datetime.now(timezone.utc).isoformat(),
                        }
                    try:
                        stats_resp = await asyncio.wait_for(
                            session.get(f"{base_url}{cfg['stats_path'].lstrip('/')}", allow_redirects=False), timeout=timeout
                        )
                        if stats_resp.status == 200:
                            stats = await stats_resp.json()
                            await stats_resp.release()
                            system = stats.get("system") or {}
                            version = system.get("comfyui_version")
                            if version:
                                result["version"] = version
                            devices = stats.get("devices") or []
                            dev = devices[0] if devices and isinstance(devices[0], dict) else {}
                            if dev.get("name"):
                                result["vram_device"] = dev["name"]
                            if dev.get("type"):
                                result["vram_type"] = dev["type"]
                            if dev.get("vram_total") is not None:
                                result["vram_total"] = dev["vram_total"]
                        else:
                            await stats_resp.release()
                    except Exception:
                        pass
                    return result
                elif service == "vllm":
                    data = await resp.json()
                    await resp.release()
                    items = data.get("data", []) if isinstance(data, dict) else []
                    models = []
                    seen = set()
                    for m in items:
                        if not isinstance(m, dict):
                            continue
                        name = m.get("id", "")
                        if name and name not in seen:
                            seen.add(name)
                            models.append(name)
                    models = _filter_models(models)
                    if not models:
                        last_error = {"error": "no real models"}
                        break
                    version = None
                    try:
                        ver_resp = await asyncio.wait_for(
                            session.get(f"{base_url}version", allow_redirects=False), timeout=timeout
                        )
                        if ver_resp.status == 200:
                            ver_data = await ver_resp.json()
                            version = ver_data.get("version")
                        await ver_resp.release()
                    except Exception:
                        pass
                    result = {
                        "service": service,
                        "url": base_url,
                        "models": models,
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
                    if version:
                        result["version"] = version
                    return result
                else:
                    data = await resp.json()
                    await resp.release()
                    models = _filter_models(data if isinstance(data, list) else [])
                    return {
                        "service": service,
                        "url": base_url,
                        "models": models,
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
            except asyncio.TimeoutError:
                last_error = {"error": "timeout"}
                break
            except OSError as e:
                last_error = {"error": f"{e} ({current_scheme})"}
                break
            except json.JSONDecodeError:
                last_error = {"error": f"bad JSON ({current_scheme})"}
                break
            except aiohttp.ContentTypeError:
                last_error = {"error": f"bad JSON ({current_scheme})"}
                break
            except Exception as e:
                last_error = {"error": f"{e} ({current_scheme})"}
                break
        else:
            last_error = {"error": "too many redirects"}
        if got_http_response:
            break

    return last_error


def _fetch_api(dry, limit, service, fid=None, curlify=False, query=None):
    import base64
    import requests
    import curlify as curlify_mod

    if query is None:
        query = SERVICE_CONFIG[service]["fofa_query"]
    if fid:
        query += f' && fid="{fid}"'
    qb64 = base64.b64encode(query.encode()).decode()
    params = {"key": FOFA_KEY, "qbase64": qb64, "size": limit}
    headers = {}
    if FOFA_AUTHORIZATION:
        headers["Authorization"] = FOFA_AUTHORIZATION
    req = requests.Request("GET", FOFA_API, params=params, headers=headers)
    prepared = req.prepare()
    if curlify:
        log.info(curlify_mod.to_curl(prepared))
        return []
    if dry:
        log.info(curlify_mod.to_curl(prepared))
        return []

    backoff = BACKOFF
    while True:
        try:
            with requests.Session() as s:
                resp = s.send(prepared, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                if "network unstable" in data.get("error", "").lower():
                    log.warning(f"network unstable, retrying in {backoff}s")
                    time.sleep(backoff)
                    backoff = min(int(backoff * 1.2), MAX_BACKOFF)
                    continue
                log.error(f"FOFA error: {data.get('error')}")
                return []
            break
        except requests.exceptions.ConnectionError as e:
            if _is_offline_error(e):
                log.warning(f"offline ({e}); retrying in {backoff}s...")
                time.sleep(backoff)
                backoff = min(int(backoff * 1.2), MAX_BACKOFF)
                continue
            log.error(f"FOFA API request failed: {e}")
            return []
        except Exception as e:
            log.error(f"FOFA API request failed: {e}")
            return []

    hosts = []
    for row in data.get("results", []):
        ip_addr, port_num = row[0], row[1]
        entry = {
            "service": service,
            "host": f"{ip_addr}:{port_num}",
            "site": "fofa",
        }
        if fid:
            entry["fid"] = fid
        hosts.append(entry)
    return hosts


def _tag(value):
    """Short filesystem-safe label component (md5 prefix). Used for FID and
    server values whose raw text can contain spaces/slashes/etc."""
    if not value:
        return "any"
    return hashlib.md5(str(value).encode()).hexdigest()[:8]


def _fetch_web(dry, limit, service, combined, country=None, port=None, server=None, run_ts=None, curlify=False, label=""):
    import base64
    import re
    import requests
    import curlify as curlify_mod

    qb64 = base64.b64encode(combined.encode()).decode()
    url = f"{FOFA_WEB}?qbase64={qb64}"

    cookie_header = re.sub(r'fofa_result_page_size=\d+', f'fofa_result_page_size={limit}', FOFA_COOKIE) if FOFA_COOKIE else ""

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    if FOFA_AUTHORIZATION:
        headers["Authorization"] = FOFA_AUTHORIZATION

    if curlify:
        req = requests.Request("GET", url, headers=headers)
        prepared = req.prepare()
        log.info(curlify_mod.to_curl(prepared))
        return []

    if dry:
        log.info(f"# query: {combined}")
        log.info(f"# url: GET {url}")
        log.info(f"# cookie: {cookie_header[:80]}...")
        return []

    backoff = BACKOFF
    attempt = 0
    while True:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            body = resp.text.lower()
            if "daily usage limit" in body:
                tmp_dir = "/tmp/graflex"
                out_path = os.path.join(tmp_dir, f"fofa-results-{label}-{run_ts}.txt")
                if os.path.exists(out_path):
                    os.remove(out_path)
                log.error(f"daily usage limit hit, resume by using --id {run_ts}")
                raise SystemExit(1)
            if "access is temporarily denied" in body:
                log.error("FOFA access denied — IP flagged as a web crawler. Try again later or use a different IP/VPN.")
                raise SystemExit(1)
            if "rate limit" in body or "too many requests" in body or "api request frequency out of limit" in body:
                raise RuntimeError("rate limited")
            if "network unstable" in body:
                raise RuntimeError("network unstable")
            break
        except RuntimeError as e:
            msg = str(e)
            if msg == "network unstable":
                log.warning(f"network unstable, retrying in {backoff}s")
                time.sleep(backoff)
                backoff = int(backoff * 1.2)
                continue
            else:
                if attempt < 2:
                    log.warning(f"rate limited, retrying in {backoff}s")
                    time.sleep(backoff)
                    backoff = int(backoff * 1.2)
                    attempt += 1
                else:
                    log.warning("rate limited")
                    return None
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code in (429, 403) and attempt < 2:
                log.warning(f"rate limited ({code}), retrying in {backoff}s")
                time.sleep(backoff)
                backoff = int(backoff * 1.2)
                attempt += 1
            else:
                if code in (429, 403):
                    log.warning(f"rate limited  ({code})")
                else:
                    log.error(f"FOFA web request failed: {e}")
                return None
        except requests.exceptions.ConnectionError as e:
            if _is_offline_error(e):
                log.warning(f"offline ({e}); retrying in {backoff}s...")
                time.sleep(backoff)
                backoff = min(int(backoff * 1.2), MAX_BACKOFF)
                continue
            if attempt < 2:
                log.warning(f"connection error, retrying in {backoff}s")
                time.sleep(backoff)
                backoff = int(backoff * 1.2)
                attempt += 1
            else:
                log.error(f"FOFA web request failed: {e}")
                return None
        except Exception as e:
            log.error(f"FOFA web request failed: {e}")
            return None

    tmp_dir = "/tmp/graflex"
    os.makedirs(tmp_dir, exist_ok=True)
    out_path = os.path.join(tmp_dir, f"fofa-results-{label}-{run_ts}.txt")
    with open(out_path, "w") as f:
        f.write(resp.text)

    hosts = _parse_fofa_html(out_path, service)
    if not hosts:
        page = getattr(resp, "text", "")
        if "no data for past year" not in page.lower():
            log.warning(f"! no hosts parsed but 'no data for past year' not found in page (code={getattr(resp, 'status_code', '?')}, {len(page)}b, {out_path})")

    return hosts


def _parse_fofa_html(html_path, service):
    import re

    with open(html_path) as f:
        content = f.read()

    values = re.findall(r'data-clipboard-text="([^"]+)"', content)
    if not values:
        log.warning(f"! NO RESULTS from {html_path}")
        return []

    seen = set()
    hosts = []
    for v in values:
        host, port = _value_to_host_port(v)
        key = f"{host}:{port}"
        if not host or not v.strip() or key in seen:
            continue
        seen.add(key)
        hosts.append({
            "service": service,
            "host": key,
            "site": "fofa",
        })

    log.info(f"  {len(hosts)} hosts from {html_path}")
    return hosts


def _parse_shodan_html(html_path, service):
    import re

    with open(html_path) as f:
        content = f.read()

    hrefs = []
    for tag in re.findall(r"<a\b[^>]*>", content):
        if 'rel="noopener noreferrer nofollow"' not in tag:
            continue
        m = re.search(r'href="([^"]+)"', tag)
        if m:
            hrefs.append(m.group(1))

    if not hrefs:
        log.warning(f"! NO RESULTS from {html_path}")
        return []

    seen = set()
    hosts = []
    for href in hrefs:
        if not (href.startswith("http://") or href.startswith("https://")):
            continue
        host, port = _value_to_host_port(href)
        if not host or host == "shodan.io" or host.endswith(".shodan.io"):
            continue
        key = f"{host}:{port}"
        if key in seen:
            continue
        seen.add(key)
        hosts.append({
            "service": service,
            "host": key,
            "site": "shodan",
        })

    log.info(f"  {len(hosts)} hosts from {html_path}")
    return hosts


def _fetch_shodan(dry, svc, combined, page=1, run_ts=None, curlify=False, label=""):
    import requests
    import curlify as curlify_mod
    from urllib.parse import quote

    url = f"{SHODAN_WEB}?query={quote(combined)}"
    if page > 1:
        url += f"&page={page}"

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if SHODAN_KEY:
        headers["Cookie"] = SHODAN_KEY

    if curlify:
        req = requests.Request("GET", url, headers=headers)
        prepared = req.prepare()
        log.info(curlify_mod.to_curl(prepared))
        return []

    if dry:
        log.info(f"# query: {combined} (page {page})")
        log.info(f"# url: GET {url}")
        log.info(f"# cookie: {SHODAN_KEY[:80]}...")
        return []

    backoff = BACKOFF
    attempt = 0
    while True:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            body = resp.text.lower()
            if "rate limit" in body or "too many requests" in body:
                raise RuntimeError("rate limited")
            break
        except RuntimeError as e:
            msg = str(e)
            if attempt < 2:
                log.warning(f"{msg}, retrying in {backoff}s")
                time.sleep(backoff)
                backoff = int(backoff * 1.2)
                attempt += 1
            else:
                log.warning(msg)
                return None
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code in (429, 403) and attempt < 2:
                log.warning(f"rate limited ({code}), retrying in {backoff}s")
                time.sleep(backoff)
                backoff = int(backoff * 1.2)
                attempt += 1
            else:
                if code in (429, 403):
                    log.warning(f"rate limited ({code})")
                else:
                    log.error(f"Shodan request failed: {e}")
                return None
        except requests.exceptions.ConnectionError as e:
            if _is_offline_error(e):
                log.warning(f"offline ({e}); retrying in {backoff}s...")
                time.sleep(backoff)
                backoff = min(int(backoff * 1.2), MAX_BACKOFF)
                continue
            if attempt < 2:
                log.warning(f"connection error, retrying in {backoff}s")
                time.sleep(backoff)
                backoff = int(backoff * 1.2)
                attempt += 1
            else:
                log.error(f"Shodan request failed: {e}")
                return None
        except Exception as e:
            log.error(f"Shodan request failed: {e}")
            return None

    tmp_dir = "/tmp/graflex"
    os.makedirs(tmp_dir, exist_ok=True)
    out_path = os.path.join(tmp_dir, f"shodan-results-{label}-{run_ts}.txt")
    with open(out_path, "w") as f:
        f.write(resp.text)

    return _parse_shodan_html(out_path, svc)


def fetch(dry=False, curlify=False, limit=2, service=None, method="api", query=None, name=None, servers=None, ports=None, countries=None, fids=None, sleep=SLEEP_DEFAULT, session=None, shuffle=False, site="fofa"):
    global _RUN_TS
    hosts_file = _cache_file(name, "hosts")

    if not query and not service and name not in NAMED_QUERIES:
        log.error(f"no query for name '{name}' — pass --query or --service, or use a named query ({', '.join(sorted(NAMED_QUERIES))})")
        return []

    if site == "shodan":
        if not SHODAN_KEY:
            log.error("SHODAN_KEY must be set in .env for --site shodan. See README for instructions.")
            return []
        if servers or fids:
            log.warning("--servers/--fid are ignored with --site shodan")
        from datetime import datetime
        run_ts = session or datetime.now().strftime("%Y%m%d%H%M%S")
        _RUN_TS = run_ts
        if session:
            log.info(f"resuming session {run_ts}")

        base_query = query
        if not base_query and service:
            base_query = SERVICE_CONFIG[service].get("shodan_query")
        if not base_query:
            log.error(f"no shodan query for '{service or name}' — pass --query (shodan syntax differs from fofa)")
            return []

        if isinstance(countries, str):
            country_list = [None] + [s.strip() for s in countries.split(",")]
        else:
            country_list = [None] + ["US", "DE", "CN", "JP"]
        if isinstance(ports, str):
            port_list = [None] + [s.strip() for s in ports.split(",")]
        else:
            port_list = [None]
            if service:
                port_list.append(str(SERVICE_CONFIG[service]["port"]))

        if shuffle:
            random.shuffle(country_list)
            random.shuffle(port_list)

        pool = _load_json(hosts_file)
        seen = {f"{h['service']}@{h['host']}" for h in pool}
        combos = [(c, p) for p in port_list for c in country_list]
        total_reqs = len(combos) * SHODAN_PAGES

        start = time.time()
        done_reqs = 0
        for country, port in combos:
            qparts = [base_query]
            if port:
                qparts.append(f"port:{port}")
            if country:
                qparts.append(f'country:"{country}"')
            combined = " ".join(qparts)
            log.debug(combined)
            if not dry:
                log.info(f"[{done_reqs+1}/{total_reqs}] country={country} port={port}")

            svc = service or name or "unknown"
            for page in range(1, SHODAN_PAGES + 1):
                label = f"{country or 'any'}-{port or 'any'}-p{page}"
                if session:
                    out_path = os.path.join("/tmp/graflex", f"shodan-results-{label}-{run_ts}.txt")
                    if os.path.exists(out_path):
                        if not dry:
                            log.info(f"  skip page {page} (already fetched)")
                        done_reqs += 1
                        continue

                hosts = _fetch_shodan(dry, svc, combined, page=page, run_ts=run_ts, curlify=curlify, label=label)
                done_reqs += 1
                if hosts is None:
                    continue

                for h in hosts:
                    key = f"{h['service']}@{h['host']}"
                    if key not in seen:
                        pool.append(h)
                        seen.add(key)

                if not dry:
                    _save_json(hosts_file, pool)
                    elapsed = time.time() - start
                    eta = elapsed * (total_reqs / done_reqs) - elapsed
                    log.info(f"  total: {len(pool)}    eta: {_fmt_duration(eta)}   lapsed: {_fmt_duration(elapsed)}")

                if not dry and not curlify and done_reqs < total_reqs:
                    time.sleep(sleep)

        hosts = pool
    elif method == "web":
        if not FOFA_COOKIE:
            log.error("FOFA_COOKIE must be set in .env for the web method. See README for instructions on how to obtain it from your browser.")
            return []
        from datetime import datetime
        run_ts = session or datetime.now().strftime("%Y%m%d%H%M%S")
        _RUN_TS = run_ts
        if session:
            log.info(f"resuming session {run_ts}")

        if isinstance(countries, str):
            country_list = [None] + [s.strip() for s in countries.split(",")]
        else:
            country_list = [None] + ["CN", "US", "CA", "JP", "KR"]
        if isinstance(ports, str):
            port_list = [None] + [s.strip() for s in ports.split(",")]
        else:
            port_list = [None]
            if service:
                port_list.append(str(SERVICE_CONFIG[service]["port"]))
        if isinstance(servers, str):
            server_list = [None] + [s.strip() for s in servers.split(",")]
        else:
            server_list = [None]
        if isinstance(fids, str):
            fid_specs = [s.strip() for s in fids.split(",") if s.strip()]
        else:
            fid_specs = []

        if shuffle:
            random.shuffle(country_list)
            random.shuffle(port_list)
            random.shuffle(server_list)

        pool = _load_json(hosts_file)
        index = {f"{h['service']}@{h['host']}": h for h in pool}
        seen = set(index)
        # FIDs are targeted follow-ups: each is fetched as QUERY+fid on its
        # own, never crossed with country/port/server (those intersections
        # are almost always empty). They run FIRST, before the combinatorics,
        # since they're the high-value targeted queries.
        combo_grid = [(c, p, s, None) for s in server_list for p in port_list for c in country_list]
        if shuffle:
            random.shuffle(combo_grid)

        combos = [(None, None, None, fid) for fid in fid_specs] + combo_grid

        start = time.time()
        for i, (country, port, server, fid) in enumerate(combos):
            if query:
                qparts = [query]
            elif service:
                qparts = [SERVICE_CONFIG[service]["fofa_query"]]
            else:
                qparts = [NAMED_QUERIES[name]]
            if port:
                qparts.append(f'port="{port}"')
            if country:
                qparts.append(f'country="{country}"')
            if server:
                qparts.append(f'server="{server}"')
            if fid:
                qparts.append(f'fid="{fid}"')
            combined = " && ".join(qparts)
            log.debug(combined)
            if not dry:
                log.info(f"[{i+1}/{len(combos)}] country={country} port={port} server={server} fid={fid}")

            label = f"{country or 'any'}-{port or 'any'}-{_tag(server)}-{_tag(fid)}"
            if session:
                out_path = os.path.join("/tmp/graflex", f"fofa-results-{label}-{run_ts}.txt")
                if os.path.exists(out_path):
                    if not dry:
                        log.info(f"  skip (already fetched)")
                    continue

            svc = service or name or "unknown"
            hosts = _fetch_web(dry, limit, svc, combined, country, port, server, run_ts, curlify=curlify, label=label)
            if hosts is None:
                continue

            for h in hosts:
                if fid:
                    h["fid"] = fid
                key = f"{h['service']}@{h['host']}"
                if key not in seen:
                    pool.append(h)
                    seen.add(key)
                    index[key] = h
                elif fid and not index[key].get("fid"):
                    index[key]["fid"] = fid

            if not dry:
                _save_json(hosts_file, pool)

                done = i + 1
                elapsed = time.time() - start
                eta = elapsed * (len(combos) / done) - elapsed
                log.info(f"  total: {len(pool)}    eta: {_fmt_duration(eta)}   lapsed: {_fmt_duration(elapsed)}\n")

            if i < len(combos) - 1 and not dry and not curlify:
                time.sleep(sleep)

        hosts = pool
    else:
        if not FOFA_KEY:
            log.error("FOFA_KEY must be set in .env")
            return
        if not service and not query and name not in NAMED_QUERIES:
            log.error("--service is required for API method")
            return
        named_query = query or NAMED_QUERIES.get(name)
        if fids:
            hosts = []
            for fid in [s.strip() for s in fids.split(",")]:
                hosts.extend(_fetch_api(dry, limit, service, fid=fid, curlify=curlify, query=named_query))
        else:
            hosts = _fetch_api(dry, limit, service, curlify=curlify, query=named_query)

    if dry:
        return

    # print(f"Loading {hosts_file}")
    existing = _load_json(hosts_file)
    index = {f"{h['service']}@{h['host']}": h for h in existing}
    seen = set(index)
    for h in hosts:
        key = f"{h['service']}@{h['host']}"
        if key not in seen:
            existing.append(h)
            seen.add(key)
            index[key] = h
        elif h.get("fid") and not index[key].get("fid"):
            index[key]["fid"] = h["fid"]

    _save_json(hosts_file, existing)
    log.info(f"fetch: {len(hosts)} new hosts, {len(existing)} total in seed list")


# Stable, highly-available endpoints used only to decide whether WE have
# working internet/DNS — never the hosts being scanned.
_REACHABILITY_URLS = [
    "https://www.google.com/generate_204",
    "https://cloudflare.com/cdn-cgi/trace",
    "https://example.com/",
]


async def _is_network_reachable(session, check_hosts=None, timeout=10):
    """Return True if WE can reach the internet, by hitting known-good public
    endpoints — NOT the hosts being scanned. A per-host DNS/connect failure
    (a dead target) must not be mistaken for us being offline, or we'd retry
    dead hosts forever instead of moving on."""
    for url in _REACHABILITY_URLS:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status < 500:
                    return True
        except Exception:
            continue
    return False


async def _check_all(service, name=None, check_timeout=60, check_new=False, check_all=False, workers=10):
    from datetime import datetime, timezone

    if name is None:
        name = service

    hosts_file = _cache_file(name, "hosts")
    working_file = _cache_file(name, "working")
    notworking_file = _cache_file(name, "notworking")

    hosts = _load_json(hosts_file)
    if not service and hosts:
        service = hosts[0].get("service", "")
    hosts = [h for h in hosts if h.get("service") == service]
    if not hosts:
        log.warning(f"check: no {service or '?'} hosts - run fetch first")
        return

    existing_working = _load_json(working_file)
    existing_notworking_raw = _load_json(notworking_file)
    if isinstance(existing_notworking_raw, dict):
        existing_notworking = existing_notworking_raw
    elif isinstance(existing_notworking_raw, list):
        existing_notworking = {_entry_host(n): n for n in existing_notworking_raw}
    else:
        existing_notworking = {}
    done = set()
    if not check_all:
        if check_new:
            done = {f"{h['service']}@{_entry_host(h)}" for h in existing_working}
            done.update(f"{n['service']}@{_entry_host(n)}" for n in existing_notworking.values())
        else:
            done = {f"{h['service']}@{_entry_host(h)}" for h in existing_working}
            done.update(f"{n['service']}@{_entry_host(n)}" for n in existing_notworking.values() if n.get("result") == "error")

    to_check = [h for h in hosts if f"{h['service']}@{h['host']}" not in done]
    if not to_check:
        log.info(f"check: all {len(hosts)} hosts already have model data")
        existing_working.sort(key=lambda h: (h.get("checked", ""), _entry_host(h)))
        _save_json_atomic(working_file, existing_working)
        return

    log.info(f"check: {len(to_check)} to check ({len(done)} already done)")

    sem = asyncio.Semaphore(workers)
    wlock = asyncio.Lock()
    start = time.time()
    completed = 0

    async def check_one(entry):
        nonlocal completed
        async with sem:
            host_port = entry["host"].split(":")
            h = host_port[0]
            p = int(host_port[1]) if len(host_port) > 1 else SERVICE_CONFIG[service]["port"]
            backoff = BACKOFF
            while True:
                result = await _check_host(session, h, p, service, timeout=check_timeout)
                if isinstance(result, dict) and "error" in result and _is_offline_msg(result["error"]):
                    reachable = await _is_network_reachable(session, existing_working)
                    if reachable:
                        log.warning(f"~ {entry['host']}: {result['error']} (host unreachable, not offline)")
                        break
                    log.warning(f"~ {entry['host']}: {result['error']} (offline, retrying in {backoff}s...)")
                    await asyncio.sleep(backoff)
                    backoff = min(int(backoff * 1.2), MAX_BACKOFF)
                    continue
                break

        key = f"{service}@{entry['host']}"
        ok = False
        async with wlock:
            if isinstance(result, dict) and "error" not in result:
                result["checked"] = result.get("checked", datetime.now(timezone.utc).isoformat())
                result["host"] = entry["host"]
                working = _load_json(working_file, silent=True)
                found = False
                for i, w in enumerate(working):
                    if f"{w['service']}@{_entry_host(w)}" == key:
                        working[i] = result
                        found = True
                        break
                if not found:
                    working.append(result)
                working.sort(key=lambda h: (h.get("checked", ""), _entry_host(h)))
                _save_json_atomic(working_file, working)
                log.info(f"+ {entry['host']}: {len(result['models'])} models")
                ok = True
            else:
                reason = result.get("error", str(result)) if isinstance(result, dict) else str(result)
                result_type = "error" if (reason.startswith("HTTP ") or reason.startswith("show HTTP ") or reason.startswith("bad JSON") or "no real" in reason or "empty show" in reason or "auth required" in reason) else "unreachable"
                nr = {"service": service, "host": entry["host"], "url": f"http://{entry['host']}", "reason": reason, "result": result_type, "checked": datetime.now(timezone.utc).isoformat()}
                notworking = _load_json(notworking_file, silent=True)
                if not isinstance(notworking, dict):
                    notworking = {}
                nkey = entry["host"]
                notworking[nkey] = nr
                _save_json_atomic(notworking_file, notworking)
                log.info(f"  {entry['host']}: {reason}")
        completed += 1
        if completed % STATS_EVERY == 0:
            elapsed = time.time() - start
            eta = elapsed * (len(to_check) / completed) - elapsed
            log.info(f"Checked: {completed} | Runtime: {_fmt_duration(elapsed)} | Remaining: {len(to_check) - completed} | ETA: {_fmt_duration(eta)}")
        return ok

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=aiohttp.ClientTimeout(total=check_timeout + 5)) as session:
        tasks = [check_one(entry) for entry in to_check]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = sum(1 for r in results if r is True)
        failed = sum(1 for r in results if r is False)
        log.info(f"check: {len(to_check)} checked, {success} working, {failed} notworking")


def check(service, name=None, check_timeout=60, check_new=False, check_all=False, workers=10):
    asyncio.run(_check_all(service, name, check_timeout, check_new, check_all, workers))


# Seed regexes for -a classify; edit ~/.cache/free-ollama/model-classifier.json
# to tune. Categories are evaluated in file order, first matching regex wins,
# and anything unmatched lands in "other".
DEFAULT_CLASSIFIER = {
    "video": [
        r"(wan|video|ltx|mochi|cogvideo|animatediff|svd|seedvr|flashvsr|framepack|dynamicrafter)",
        r"^(diffusion_models|unet_gguf|clip_gguf|tmp_hunyuan_loras|tmp_wanvideo_loras|video_formats|frame_interpolation)/",
    ],
    "image": [
        r"\.safetensors$",
        r"^(sd_1\.5|sdxl_1\.0|pony|flux\.1_d|flux\.1_s|lora_sd_1\.5|lora_sdxl_1\.0|lora_pony|lora_flux\.1_d|aura-sr)/",
    ],
    "audio": [
        r"^(TTS|qwen-tts|fishaudioS2|mmaudio|voxcpm|voxcpm_lora|SenseVoice|sonic|foley|audiodit|audio_encoders|wav2vec2)/",
        r"\.(wav|mp3|flac|ogg|mka)$",
    ],
}


def _load_classifier():
    if not os.path.exists(CLASSIFIER_FILE):
        _save_json(CLASSIFIER_FILE, DEFAULT_CLASSIFIER)
        log.info(f"classify: created {CLASSIFIER_FILE} (edit it to tune)")
    with open(CLASSIFIER_FILE) as f:
        raw = json.load(f)
    compiled = {}
    for ctype, patterns in raw.items():
        regs = []
        for p in patterns or []:
            try:
                regs.append(re.compile(p, re.IGNORECASE))
            except re.error as e:
                log.warning(f"classify: bad regex {p!r} in '{ctype}': {e}")
        if regs:
            compiled[ctype] = regs
    return compiled


def _classify_model(model, compiled):
    i = 0
    for ctype, regs in compiled.items():
        i += 1
        if any(r.search(model) for r in regs):
            return ' ' * i + ctype
    return "....."


def classify(name=None):
    from datetime import datetime, timezone

    name = name or "comfyui"
    input_file = _cache_file(name, "working")
    entries = _load_json(input_file)
    entries = [e for e in entries if isinstance(e, dict)]
    log.info(f"using {CLASSIFIER_FILE}")
    if not entries:
        log.error(f"classify: no hosts in {input_file}")
        return

    compiled = _load_classifier()

    out = []
    printed = set()
    counts = {}
    for entry in entries:
        e = dict(entry)
        classified = {ctype: [] for ctype in list(compiled) + ["....."]}
        for m in entry.get("models") or []:
            ctype = _classify_model(m, compiled)
            classified.setdefault(ctype, []).append(m)
            counts[ctype] = counts.get(ctype, 0) + 1
            key = (ctype, m)
            if key not in printed:
                printed.add(key)
                print(f"{ctype:10s} {m}")
        e["classified"] = classified
        out.append(e)

    root, ext = os.path.splitext(input_file)
    out_file = f"{root}-classified{ext}"
    _save_json(out_file, out)
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    log.info(f"classify: {len(entries)} hosts -> {out_file} ({summary}) [{datetime.now(timezone.utc).isoformat()}]")


def main():
    load_dotenv()

    global FOFA_KEY, FOFA_COOKIE, FOFA_AUTHORIZATION, SHODAN_KEY
    FOFA_KEY = os.getenv("FOFA_KEY", "")
    FOFA_COOKIE = os.getenv("FOFA_COOKIE", "")
    FOFA_AUTHORIZATION = os.getenv("FOFA_AUTHORIZATION", "")
    SHODAN_KEY = _clean_cookie(os.getenv("SHODAN_KEY", ""))

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOGLEVEL", "INFO").upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="Discover public image-generation hosts via FOFA")
    parser.add_argument("--ct", "--check-timeout", dest="check_timeout", type=int, default=60, help="per-host check timeout in seconds (default: 60)")
    parser.add_argument("--curlify", action="store_true", help="print curl command instead of executing")
    parser.add_argument("-a", "--action", choices=["fetch", "check", "check-new", "check-all", "fetch-check", "classify"], required=True, help="action to perform")
    parser.add_argument("-c", "--countries", help="comma-separated country codes to cycle (default: CN,US,CA,JP,KR)")
    parser.add_argument("-d", "--dry", action="store_true", help="report what fetch would do without saving")
    parser.add_argument("-e", "--servers", help="comma-separated server values to cycle (default: uvicorn,nginx)")
    parser.add_argument("-f", "--fid", dest="fids", help="comma-separated FID values to filter by")
    parser.add_argument("-i", "--id", dest="session", help="resume a previous session by providing its run timestamp (the run_ts from the log)")
    parser.add_argument("-l", "--limit", type=int, default=2, help="max results per query (default: 2)")
    parser.add_argument("-m", "--method", choices=["api", "web"], default="web", help="fetch method (default: web)")
    parser.add_argument("-n", "--name", help="cache file name prefix (default: image-gen); a named query (e.g. gradio) also selects its built-in FOFA query for fetch")
    parser.add_argument("-p", "--ports", help="comma-separated port values to cycle")
    parser.add_argument("-q", "--query", help="custom FOFA query (requires --name)")
    parser.add_argument("-r", "--random", dest="shuffle", action="store_true", help="shuffle the ports, servers, countries, and FID lists so the fetch cycles through combinations in random order")
    parser.add_argument("-t", "--site", choices=["fofa", "shodan"], default="fofa", help="site to scrape (default: fofa)")
    parser.add_argument("-s", "--service", choices=list(SERVICE_CONFIG), help="service to search for")
    parser.add_argument("-w", "--workers", type=int, default=10, help="max parallel check workers (default: 10)")
    parser.add_argument("-z", "--sleep", type=int, default=SLEEP_DEFAULT, help=f"seconds to sleep between requests (default: {SLEEP_DEFAULT})")
    args = parser.parse_args()

    if args.query and not args.name:
        parser.error("--query requires --name")
    if not args.service and not args.query and not args.name:
        parser.error("either --service, --query, or --name is required")
    if args.site == "shodan":
        if args.method != "web":
            log.warning("-m/--method is ignored with --site shodan")
        if args.fids:
            log.warning("-f/--fid is ignored with --site shodan")

    parts = args.action.split("-")
    check_new = args.action == "check-new"
    check_all = args.action == "check-all"

    try:
        for step in parts:
            log.info(f"--- {step} ---")
            if step == "fetch":
                fetch(dry=args.dry, curlify=args.curlify, limit=args.limit, service=args.service, method=args.method, query=args.query, name=args.name, servers=args.servers, ports=args.ports, countries=args.countries, fids=args.fids, sleep=args.sleep, session=args.session, shuffle=args.shuffle, site=args.site)
            elif step == "check":
                check(service=args.service, name=args.name, check_timeout=args.check_timeout, check_new=check_new, check_all=check_all, workers=args.workers)
            elif step == "classify":
                classify(name=args.name)
    except SystemExit as e:
        sys.exit(e.code)
    except KeyboardInterrupt:
        base = f"graflex -s {args.service}" if args.service else f"graflex -n {args.name or 'image-gen'}"
        if step == "fetch":
            ts = _RUN_TS or args.session
            hint = f"{base} -a fetch{f' -i {ts}' if ts else ''}"
            log.warning(f"\ninterrupted — already-fetched pages are saved; resume with: {hint}")
        elif step == "check":
            log.warning(f"\ninterrupted — checked hosts are saved; rerun {base} -a check (or -a check-all) to continue")
        else:
            log.warning("\ninterrupted")
        sys.exit(130)
