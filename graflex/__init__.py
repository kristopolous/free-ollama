import argparse
import asyncio
import json
import logging
import os
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


def _cache_file(name, suffix):
    prefix = name or "image-gen"
    return os.path.join(CACHE_DIR, f"{prefix}-{suffix}.json")

TIMEOUT = 60
BACKOFF = 15
SLEEP_DEFAULT = 4

FOFA_KEY = os.getenv("FOFA_KEY", "")
FOFA_COOKIE = os.getenv("FOFA_COOKIE", "")
FOFA_AUTHORIZATION = os.getenv("FOFA_AUTHORIZATION", "")
FOFA_API = "https://fofa.info/api/v1/search/all"
FOFA_WEB = "https://en.fofa.info/result"

SERVICE_CONFIG = {
    "a1111": {
        "port": 7860,
        "fofa_query": 'icon_hash="2075038152" && body="Stable Diffusion"',
        "check_path": "/sdapi/v1/sd-models",
    },
    "comfyui": {
        "port": 8188,
        "fofa_query": 'title="ComfyUI"',
        "check_path": "/models/checkpoints",
    },
    "ollama": {
        "port": 11434,
        "fofa_query": 'body="ollama"',
        "check_path": "/api/tags",
    },
}


def _load_json(path):
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
    url = entry.get("url") or ""
    if url:
        return url.split("://", 1)[1]
    return entry.get("host", "")


async def _check_host(session, host, port, service, timeout=TIMEOUT):
    from datetime import datetime, timezone

    cfg = SERVICE_CONFIG[service]
    path = cfg["check_path"]
    last_error = None
    for scheme in ("http", "https"):
        url = f"{scheme}://{host}:{port}{path}"
        base_url = f"{scheme}://{host}:{port}/"
        try:
            resp = await asyncio.wait_for(
                session.get(url), timeout=timeout
            )
            if resp.status != 200:
                await resp.release()
                last_error = {"error": f"HTTP {resp.status} ({scheme})"}
                continue
            if service == "a1111":
                data = await resp.json()
                await resp.release()
                models = [m.get("title", "") for m in data if isinstance(m, dict)]
                return {
                    "service": service,
                    "url": base_url,
                    "models": models,
                    "checked": datetime.now(timezone.utc).isoformat(),
                }
            elif service == "ollama":
                data = await resp.json()
                await resp.release()
                models = [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]
                model = models[0] if models else None
                if not model:
                    last_error = {"error": "no real models"}
                    continue
                show_body = {"model": model}
                show_resp = await asyncio.wait_for(
                    session.post(f"{base_url}api/show", json=show_body), timeout=timeout
                )
                if show_resp.status != 200:
                    await show_resp.release()
                    last_error = {"error": f"show HTTP {show_resp.status} ({scheme})"}
                    continue
                show_data = await show_resp.json()
                await show_resp.release()
                details = show_data.get("details") or {}
                if details.get("family") or details.get("parameter_size") or details.get("quantization_level"):
                    return {
                        "service": service,
                        "url": base_url,
                        "models": models,
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
                last_error = {"error": f"empty show ({scheme})"}
                continue
            else:
                data = await resp.json()
                await resp.release()
                models = data if isinstance(data, list) else []
                return {
                    "service": service,
                    "url": base_url,
                    "models": models,
                    "checked": datetime.now(timezone.utc).isoformat(),
                }
        except asyncio.TimeoutError:
            if scheme == "http":
                return {"error": f"timeout"}
            last_error = {"error": f"timeout"}
        except OSError as e:
            last_error = {"error": f"{e} ({scheme})"}
        except json.JSONDecodeError:
            last_error = {"error": f"bad JSON ({scheme})"}
        except aiohttp.ContentTypeError:
            last_error = {"error": f"bad JSON ({scheme})"}
        except Exception as e:
            last_error = {"error": f"{e} ({scheme})"}
    return last_error


def _fetch_api(dry, limit, service, fid=None, curlify=False):
    import base64
    import requests
    import curlify as curlify_mod

    cfg = SERVICE_CONFIG[service]
    query = cfg["fofa_query"]
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

    try:
        with requests.Session() as s:
            resp = s.send(prepared, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            log.error(f"FOFA error: {data.get('error')}")
            return []
    except Exception as e:
        log.error(f"FOFA API request failed: {e}")
        return []

    hosts = []
    for row in data.get("results", []):
        ip_addr, port_num = row[0], row[1]
        hosts.append({
            "service": service,
            "host": f"{ip_addr}:{port_num}",
        })
    return hosts


def _fetch_web(dry, limit, service, combined, country=None, port=None, server=None, run_ts=None, curlify=False, fid=None, fid_idx=0):
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
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            body = resp.text.lower()
            if "daily usage limit" in body:
                tmp_dir = "/tmp/graflex"
                label = f"{country or 'any'}-{port or 'any'}-{server or 'any'}-{fid_idx}"
                out_path = os.path.join(tmp_dir, f"fofa-results-{label}-{run_ts}.txt")
                if os.path.exists(out_path):
                    os.remove(out_path)
                log.error(f"daily usage limit hit, resume by using --session {run_ts}")
                raise SystemExit(1)
            if "rate limit" in body or "too many requests" in body or "api request frequency out of limit" in body:
                raise RuntimeError("rate limited")
            break
        except RuntimeError:
            if attempt < 2:
                log.warning(f"rate limited, retrying in {backoff}s")
                time.sleep(backoff)
                backoff = int(backoff * 1.2)
            else:
                log.warning("rate limited")
                return None
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code in (429, 403) and attempt < 2:
                log.warning(f"rate limited ({code}), retrying in {backoff}s")
                time.sleep(backoff)
                backoff = int(backoff * 1.2)
            else:
                if code in (429, 403):
                    log.warning(f"rate limited  ({code})")
                else:
                    log.error(f"FOFA web request failed: {e}")
                return None
        except requests.exceptions.ConnectionError as e:
            if attempt < 2:
                log.warning(f"connection error, retrying in {backoff}s")
                time.sleep(backoff)
                backoff = int(backoff * 1.2)
            else:
                log.error(f"FOFA web request failed: {e}")
                return None
        except Exception as e:
            log.error(f"FOFA web request failed: {e}")
            return None

    tmp_dir = "/tmp/graflex"
    os.makedirs(tmp_dir, exist_ok=True)
    label = f"{country or 'any'}-{port or 'any'}-{server or 'any'}-{fid_idx}"
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
    from urllib.parse import urlparse

    with open(html_path) as f:
        content = f.read()

    values = re.findall(r'data-clipboard-text="([^"]+)"', content)
    if not values:
        log.warning("! no results")
        return []

    seen = set()
    hosts = []
    for v in values:
        v = v.strip()
        if not v or v in seen:
            continue
        seen.add(v)

        if v.startswith("http://") or v.startswith("https://"):
            parsed = urlparse(v)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        elif ":" in v:
            parts = v.split(":")
            host = parts[0]
            port = int(parts[1])
        else:
            host = v
            port = 80

        hosts.append({
            "service": service,
            "host": f"{host}:{port}",
        })

    log.info(f"  {len(hosts)} hosts from {html_path}")
    return hosts


def fetch(dry=False, curlify=False, limit=2, service=None, method="api", query=None, name=None, servers=None, ports=None, countries=None, fids=None, sleep=SLEEP_DEFAULT, session=None):
    hosts_file = _cache_file(name, "hosts")

    if method == "web":
        from datetime import datetime
        run_ts = session or datetime.now().strftime("%Y%m%d%H%M%S")
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
            server_list = [None] + ["uvicorn", "nginx"]
        if isinstance(fids, str):
            fid_list = [None] + [s.strip() for s in fids.split(",")]
        else:
            fid_list = [None]

        pool = _load_json(hosts_file)
        seen = {f"{h['service']}@{h['host']}" for h in pool}
        combos = [(c, p, s, f) for f in fid_list for s in server_list for p in port_list for c in country_list]

        for i, (country, port, server, fid) in enumerate(combos):
            if query:
                qparts = [query]
            else:
                qparts = [SERVICE_CONFIG[service]["fofa_query"]]
            if port:
                qparts.append(f'port="{port}"')
            if country:
                qparts.append(f'country="{country}"')
            if server:
                qparts.append(f'server="{server}"')
            if fid:
                qparts.append(f'fid="{fid}"')
            combined = " && ".join(qparts)
            if not dry:
                log.info(f"[{i+1}/{len(combos)}] country={country} port={port} server={server} fid={fid}")

            if session:
                label = f"{country or 'any'}-{port or 'any'}-{server or 'any'}-{i}"
                out_path = os.path.join("/tmp/graflex", f"fofa-results-{label}-{run_ts}.txt")
                if os.path.exists(out_path):
                    if not dry:
                        log.info(f"  skip (already fetched)")
                    continue

            svc = service or name or "unknown"
            hosts = _fetch_web(dry, limit, svc, combined, country, port, server, run_ts, curlify=curlify, fid_idx=i)
            if hosts is None:
                continue

            for h in hosts:
                key = f"{h['service']}@{h['host']}"
                if key not in seen:
                    pool.append(h)
                    seen.add(key)

            if not dry:
                _save_json(hosts_file, pool)
                log.info(f"  total: {len(pool)}\n")

            if i < len(combos) - 1 and not dry and not curlify:
                time.sleep(sleep)

        hosts = pool
    else:
        if not FOFA_KEY:
            log.error("FOFA_KEY must be set in .env")
            return
        if not service:
            log.error("--service is required for API method")
            return
        if fids:
            hosts = []
            for fid in [s.strip() for s in fids.split(",")]:
                hosts.extend(_fetch_api(dry, limit, service, fid=fid, curlify=curlify))
        else:
            hosts = _fetch_api(dry, limit, service, curlify=curlify)

    if dry:
        return

    existing = _load_json(hosts_file)
    seen = {f"{h['service']}@{h['host']}" for h in existing}
    for h in hosts:
        key = f"{h['service']}@{h['host']}"
        if key not in seen:
            existing.append(h)
            seen.add(key)

    _save_json(hosts_file, existing)
    log.info(f"fetch: {len(hosts)} new hosts, {len(existing)} total in seed list")


async def _check_all(service, name=None, check_timeout=60):
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
    done = {f"{h['service']}@{_entry_host(h)}" for h in existing_working if h.get("models")}
    done.update(f"{n['service']}@{_entry_host(n)}" for n in existing_notworking.values() if n.get("result") == "error")

    to_check = [h for h in hosts if f"{h['service']}@{h['host']}" not in done]
    if not to_check:
        log.info(f"check: all {len(hosts)} hosts already have model data")
        existing_working.sort(key=lambda h: (h.get("checked", ""), _entry_host(h)))
        _save_json_atomic(working_file, existing_working)
        return

    log.info(f"check: {len(to_check)} to check ({len(done)} already done)")

    sem = asyncio.Semaphore(10)
    wlock = asyncio.Lock()

    async def check_one(entry):
        async with sem:
            host_port = entry["host"].split(":")
            h = host_port[0]
            p = int(host_port[1]) if len(host_port) > 1 else SERVICE_CONFIG[service]["port"]
            result = await _check_host(session, h, p, service, timeout=check_timeout)

        key = f"{service}@{entry['host']}"
        ok = False
        async with wlock:
            if isinstance(result, dict) and "error" not in result:
                result["checked"] = result.get("checked", datetime.now(timezone.utc).isoformat())
                working = _load_json(working_file)
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
                result_type = "error" if (reason.startswith("HTTP ") or reason.startswith("show HTTP ") or reason == "bad JSON" or "no real" in reason or "empty show" in reason) else "unreachable"
                nr = {"service": service, "url": f"http://{entry['host']}", "reason": reason, "result": result_type, "checked": datetime.now(timezone.utc).isoformat()}
                notworking = _load_json(notworking_file)
                if not isinstance(notworking, dict):
                    notworking = {}
                nkey = entry["host"]
                notworking[nkey] = nr
                _save_json_atomic(notworking_file, notworking)
                log.info(f"- {entry['host']}: {reason}")
        return ok

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=aiohttp.ClientTimeout(total=check_timeout + 5)) as session:
        tasks = [check_one(entry) for entry in to_check]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = sum(1 for r in results if r is True)
        failed = sum(1 for r in results if r is False)
        log.info(f"check: {len(to_check)} checked, {success} working, {failed} notworking")


def check(service, name=None, check_timeout=60):
    asyncio.run(_check_all(service, name, check_timeout))


def main():
    load_dotenv()

    global FOFA_KEY, FOFA_COOKIE, FOFA_AUTHORIZATION
    FOFA_KEY = os.getenv("FOFA_KEY", "")
    FOFA_COOKIE = os.getenv("FOFA_COOKIE", "")
    FOFA_AUTHORIZATION = os.getenv("FOFA_AUTHORIZATION", "")

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOGLEVEL", "INFO").upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="Discover public image-generation hosts via FOFA")
    parser.add_argument("-s", "--service", choices=list(SERVICE_CONFIG), help="service to search for")
    parser.add_argument("-a", "--action", choices=["fetch", "check", "fetch-check"], required=True, help="action to perform")
    parser.add_argument("-d", "--dry", action="store_true", help="report what fetch would do without saving")
    parser.add_argument("--curlify", action="store_true", help="print curl command instead of executing")
    parser.add_argument("-l", "--limit", type=int, default=2, help="max results per query (default: 2)")
    parser.add_argument("-m", "--method", choices=["api", "web"], default="web", help="fetch method (default: web)")
    parser.add_argument("-q", "--query", help="custom FOFA query (requires --name)")
    parser.add_argument("-n", "--name", help="cache file name prefix (default: image-gen)")
    parser.add_argument("-p", "--ports", help="comma-separated port values to cycle")
    parser.add_argument("--servers", help="comma-separated server values to cycle (default: uvicorn,nginx)")
    parser.add_argument("-f", "--fid", dest="fids", help="comma-separated FID values to filter by")
    parser.add_argument("-c", "--countries", help="comma-separated country codes to cycle (default: CN,US,CA,JP,KR)")
    parser.add_argument("--ct", "--check-timeout", dest="check_timeout", type=int, default=60, help="per-host check timeout in seconds (default: 60)")
    parser.add_argument("--sleep", type=int, default=SLEEP_DEFAULT, help=f"seconds to sleep between requests (default: {SLEEP_DEFAULT})")
    parser.add_argument("--session", help="resume a previous session by providing its run timestamp (the run_ts from the log)")
    args = parser.parse_args()

    if args.query and not args.name:
        parser.error("--query requires --name")
    if not args.service and not args.query and not args.name:
        parser.error("either --service, --query, or --name is required")

    parts = args.action.split("-")

    try:
        for step in parts:
            log.info(f"--- {step} ---")
            if step == "fetch":
                fetch(dry=args.dry, curlify=args.curlify, limit=args.limit, service=args.service, method=args.method, query=args.query, name=args.name, servers=args.servers, ports=args.ports, countries=args.countries, fids=args.fids, sleep=args.sleep, session=args.session)
            elif step == "check":
                check(service=args.service, name=args.name, check_timeout=args.check_timeout)
    except SystemExit as e:
        sys.exit(e.code)
