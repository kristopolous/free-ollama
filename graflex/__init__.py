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

FOFA_KEY = os.getenv("FOFA_KEY", "")
FOFA_WEB_HEADER = os.getenv("FOFA_WEB_HEADER", "")
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


async def _check_host(session, host, port, service):
    cfg = SERVICE_CONFIG[service]
    url = f"http://{host}:{port}{cfg['check_path']}"
    try:
        resp = await asyncio.wait_for(
            session.get(url), timeout=TIMEOUT
        )
        if resp.status != 200:
            await resp.release()
            return {"error": f"HTTP {resp.status}"}
        if service == "a1111":
            data = await resp.json()
            await resp.release()
            models = [m.get("title", "") for m in data if isinstance(m, dict)]
        else:
            data = await resp.json()
            await resp.release()
            models = data if isinstance(data, list) else []
        return {
            "service": service,
            "host": f"{host}:{port}",
            "models": models,
            "model_count": len(models),
            "last_checked": time.time(),
        }
    except asyncio.TimeoutError:
        return {"error": "timeout"}
    except OSError as e:
        return {"error": str(e)}
    except json.JSONDecodeError:
        return {"error": "bad JSON"}
    except Exception as e:
        return {"error": str(e)}


def _fetch_api(dry, limit, service):
    import base64
    import requests
    import curlify

    cfg = SERVICE_CONFIG[service]
    query = cfg["fofa_query"]
    qb64 = base64.b64encode(query.encode()).decode()
    params = {"key": FOFA_KEY, "qbase64": qb64, "size": limit}
    req = requests.Request("GET", FOFA_API, params=params)
    prepared = req.prepare()
    if dry:
        log.info(curlify.to_curl(prepared))
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


def _fetch_web(dry, limit, service, combined, country=None, port=None, server=None, run_ts=None):
    import base64
    import re
    import requests

    qb64 = base64.b64encode(combined.encode()).decode()
    url = f"{FOFA_WEB}?qbase64={qb64}"

    if not FOFA_WEB_HEADER:
        log.error("FOFA_WEB_HEADER must be set in .env for web method")
        return None

    cookie_header = re.sub(r'fofa_result_page_size=\d+', f'fofa_result_page_size={limit}', FOFA_WEB_HEADER)

    if dry:
        log.info(f"# Web method: GET {url}")
        log.info(f"# Cookie: {cookie_header[:80]}...")
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_header,
    }

    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            body = resp.text.lower()
            if "rate limit" in body or "too many requests" in body:
                raise RuntimeError("rate limited")
            break
        except RuntimeError:
            if attempt < 2:
                log.warning("rate limited, retrying in 12s")
                time.sleep(12)
            else:
                log.warning("rate limited")
                return None
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code in (429, 403) and attempt < 2:
                log.warning(f"rate limited ({code}), retrying in 12s")
                time.sleep(12)
            else:
                if code in (429, 403):
                    log.warning(f"rate limited  ({code})")
                else:
                    log.error(f"FOFA web request failed: {e}")
                return None
        except Exception as e:
            log.error(f"FOFA web request failed: {e}")
            return None

    tmp_dir = "/tmp/graflex"
    os.makedirs(tmp_dir, exist_ok=True)
    label = f"{country or 'any'}-{port or 'any'}-{server or 'any'}"
    out_path = os.path.join(tmp_dir, f"fofa-results-{label}-{run_ts}.txt")
    with open(out_path, "w") as f:
        f.write(resp.text)

    hosts = _parse_fofa_html(out_path, service)
    if not hosts:
        size = len(resp.text)
        log.warning(f"no results  (code={resp.status_code}, {size}b, {out_path})")
    else:
        log.info(f"saved to {out_path}")

    return hosts


def _parse_fofa_html(html_path, service):
    import re
    from urllib.parse import urlparse

    with open(html_path) as f:
        content = f.read()

    values = re.findall(r'data-clipboard-text="([^"]+)"', content)
    if not values:
        log.warning("no results")
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

    log.info(f"parsed {len(hosts)} hosts from {html_path}")
    return hosts


def fetch(dry=False, limit=2, service=None, method="api", query=None, name=None, servers=None, ports=None):
    hosts_file = _cache_file(name, "hosts")

    if method == "web":
        from datetime import datetime
        run_ts = datetime.now().strftime("%Y%m%d%H%M%S")

        countries = [None] + ["CN", "US", "CA", "JP", "KR"]
        if isinstance(ports, str):
            port_list = [s.strip() for s in ports.split(",")]
        else:
            port_list = [None]
            if service:
                port_list.append(str(SERVICE_CONFIG[service]["port"]))
        if isinstance(servers, str):
            server_list = [None] + [s.strip() for s in servers.split(",")]
        else:
            server_list = [None] + ["uvicorn", "nginx"]

        pool = _load_json(hosts_file)
        seen = {f"{h['service']}@{h['host']}" for h in pool}
        combos = [(c, p, s) for c in countries for p in port_list for s in server_list]

        for i, (country, port, server) in enumerate(combos):
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
            combined = " && ".join(qparts)
            if not dry:
                log.info(f"[{i+1}/{len(combos)}] country={country}, port={port}, server={server}  query={combined}")

            svc = service or name or "unknown"
            hosts = _fetch_web(dry, limit, svc, combined, country, port, server, run_ts)
            if hosts is None:
                continue

            for h in hosts:
                key = f"{h['service']}@{h['host']}"
                if key not in seen:
                    pool.append(h)
                    seen.add(key)

            if not dry:
                _save_json(hosts_file, pool)
                log.info(f"  total: {len(pool)} unique hosts")

            if i < len(combos) - 1 and not dry:
                time.sleep(4)

        hosts = pool
    else:
        if not FOFA_KEY:
            log.error("FOFA_KEY must be set in .env")
            return
        if not service:
            log.error("--service is required for API method")
            return
        hosts = _fetch_api(dry, limit, service)

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


async def _check_all(service, name=None):
    hosts_file = _cache_file(name, "hosts")
    working_file = _cache_file(name, "working")
    notworking_file = _cache_file(name, "notworking")

    hosts = _load_json(hosts_file)
    hosts = [h for h in hosts if h.get("service") == service]
    if not hosts:
        log.warning(f"check: no {service} hosts — run fetch first")
        return

    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
        tasks = []
        for entry in hosts:
            host_port = entry["host"].split(":")
            h = host_port[0]
            p = int(host_port[1]) if len(host_port) > 1 else SERVICE_CONFIG[service]["port"]
            tasks.append(_check_host(session, h, p, service))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    now = time.time()
    working = []
    notworking = []
    for entry, r in zip(hosts, results):
        if isinstance(r, dict) and "error" not in r:
            working.append(r)
        else:
            reason = r.get("error", str(r)) if isinstance(r, dict) else str(r)
            notworking.append({
                "service": service,
                "host": entry["host"],
                "reason": reason,
                "last_checked": now,
            })

    existing = _load_json(working_file)
    seen = {f"{h['service']}@{h['host']}" for h in existing}
    for h in working:
        key = f"{h['service']}@{h['host']}"
        h["last_checked"] = now
        if key not in seen:
            existing.append(h)
            seen.add(key)
        else:
            for i, e in enumerate(existing):
                if f"{e['service']}@{e['host']}" == key:
                    existing[i] = h
                    break

    existing.sort(key=lambda h: (h.get("gpu") or "", h.get("host", "")))
    _save_json(working_file, existing)
    _save_json(notworking_file, notworking)
    log.info(f"check: {len(hosts)} checked, {len(working)} working, {len(notworking)} notworking")


def check(service, name=None):
    asyncio.run(_check_all(service, name))


def main():
    load_dotenv()

    global FOFA_KEY, FOFA_WEB_HEADER
    FOFA_KEY = os.getenv("FOFA_KEY", "")
    FOFA_WEB_HEADER = os.getenv("FOFA_WEB_HEADER", "")

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOGLEVEL", "INFO").upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="Discover public image-generation hosts via FOFA")
    parser.add_argument("-s", "--service", choices=list(SERVICE_CONFIG), help="service to search for")
    parser.add_argument("-a", "--action", choices=["fetch", "check", "fetch-check"], required=True, help="action to perform")
    parser.add_argument("-d", "--dry", action="store_true", help="report what fetch would do without saving")
    parser.add_argument("-l", "--limit", type=int, default=2, help="max results per query (default: 2)")
    parser.add_argument("-m", "--method", choices=["api", "web"], default="web", help="fetch method (default: web)")
    parser.add_argument("-q", "--query", help="custom FOFA query (requires --name)")
    parser.add_argument("-n", "--name", help="cache file name (requires --query)")
    parser.add_argument("-p", "--ports", help="comma-separated port values to cycle")
    parser.add_argument("--servers", help="comma-separated server values to cycle (default: uvicorn,nginx)")
    args = parser.parse_args()

    if args.query and not args.name:
        parser.error("--query requires --name")
    if args.name and not args.query:
        parser.error("--name requires --query")
    if not args.service and not args.query:
        parser.error("either --service or --query is required")

    parts = args.action.split("-")

    for step in parts:
        log.info(f"--- {step} ---")
        if step == "fetch":
            fetch(dry=args.dry, limit=args.limit, service=args.service, method=args.method, query=args.query, name=args.name, servers=args.servers, ports=args.ports)
        elif step == "check":
            check(service=args.service, name=args.name)
