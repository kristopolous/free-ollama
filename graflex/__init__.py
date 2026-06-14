import argparse
import asyncio
import json
import logging
import os
import subprocess
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
DOORKNOCK_FILE = os.path.join(CACHE_DIR, "image-gen-hosts-doorknock.json")
WORKING_FILE = os.path.join(CACHE_DIR, "image-gen-working.json")
NOTWORKING_FILE = os.path.join(CACHE_DIR, "image-gen-notworking.json")

PORTS = [8188, 7860]
TIMEOUT = 60
SUBNET = 24

FOFA_KEY = os.getenv("FOFA_KEY", "")
FOFA_WEB_HEADER = os.getenv("FOFA_WEB_HEADER", "")
FOFA_API = "https://fofa.info/api/v1/search/all"
FOFA_WEB = "https://en.fofa.info/result"

SERVICES = {
    8188: "comfyui",
    7860: "a1111",
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


async def _check_host(session, host, port, service=None):
    if service is None:
        service = "a1111"
    url = f"http://{host}:{port}"
    try:
        resp = await asyncio.wait_for(
            session.get(f"{url}/sdapi/v1/sd-models"), timeout=TIMEOUT
        )
        if resp.status != 200:
            await resp.release()
            return {"error": f"HTTP {resp.status}"}
        data = await resp.json()
        await resp.release()
        models = [m.get("title", "") for m in data if isinstance(m, dict)]
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


def _fetch_api(dry, limit, queries, combined_query):
    import base64
    import requests
    import curlify

    params = {
        "key": FOFA_KEY,
        "qbase64": base64.b64encode(combined_query.encode()).decode(),
        "size": limit,
    }
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

    if len(queries) == 1:
        known_service = queries[0][1]
    hosts = []
    for row in data.get("results", []):
        ip_addr, port_num, hostname = row[0], row[1], row[2]
        port_num = int(port_num)
        if len(queries) == 1:
            service = known_service
        else:
            for q, service, default_port in queries:
                if port_num == default_port:
                    break
            else:
                service = "a1111"
        hosts.append({
            "service": service,
            "host": f"{ip_addr}:{port_num}",
        })
    return hosts


def _fetch_web(dry, limit, queries, combined_query, country=None, port=None, server=None, run_ts=None):
    import base64
    import re
    import requests

    qbase64 = base64.b64encode(combined_query.encode()).decode()
    url = f"{FOFA_WEB}?qbase64={qbase64}"

    if not FOFA_WEB_HEADER:
        log.error("FOFA_WEB_HEADER must be set in .env for web method (paste the Cookie header from your browser)")
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

    hosts = _parse_fofa_html(out_path, queries)
    if not hosts:
        size = len(resp.text)
        log.warning(f"no results  ({resp.status_code}, {size}b, {out_path})")
    else:
        log.info(f"saved to {out_path}")

    return hosts


def _parse_fofa_html(html_path, queries):
    import re
    from urllib.parse import urlparse

    with open(html_path) as f:
        content = f.read()

    values = re.findall(r'data-clipboard-text="([^"]+)"', content)
    if not values:
        log.warning("no results")
        return []

    if len(queries) == 1:
        known_service = queries[0][1]

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

        if len(queries) == 1:
            service = known_service
        else:
            for q, service, default_port in queries:
                if port == default_port:
                    break
            else:
                service = "a1111"

        hosts.append({
            "service": service,
            "host": f"{host}:{port}",
        })

    log.info(f"parsed {len(hosts)} hosts from {html_path}")
    return hosts


def fetch(dry=False, limit=2, services=None, method="api", notricks=False):
    import base64
    import urllib.parse
    import requests
    import curlify

    queries = [
        ('title="ComfyUI"', "comfyui", 8188),
        ('icon_hash="2075038152" && body="Stable Diffusion"', "a1111", 7860),
    ]
    if services and "all" not in services:
        queries = [q for q in queries if q[1] in services]

    if method == "web" and not notricks:
        from datetime import datetime
        run_ts = datetime.now().strftime("%Y%m%d%H%M%S")

        countries = [None] + ["CN", "US", "CA", "JP", "KR"]
        ports = [None] + ["10000", "7860", "443", "80"]
        servers = [None] + ["uvicorn", "nginx"]

        pool = _load_json(HOSTS_FILE)
        seen = {f"{h['service']}@{h['host']}" for h in pool}
        combos = [(c, p, s) for c in countries for p in ports for s in servers]

        for i, (country, port, server) in enumerate(combos):
            modified = []
            for q, service, default_port in queries:
                parts = [q]
                if port:
                    parts.append(f'port="{port}"')
                if country:
                    parts.append(f'country="{country}"')
                if server:
                    parts.append(f'server="{server}"')
                modified.append(" && ".join(parts))

            combined = " || ".join(f"({q})" for q in modified)

            if not dry:
                log.info(f"[{i+1}/{len(combos)}] country={country}, port={port}, server={server}")

            hosts = _fetch_web(dry, limit, queries, combined, country, port, server, run_ts)
            if hosts is None:
                continue

            for h in hosts:
                key = f"{h['service']}@{h['host']}"
                if key not in seen:
                    pool.append(h)
                    seen.add(key)

            if not dry:
                _save_json(HOSTS_FILE, pool)
                log.info(f"  total: {len(pool)} unique hosts")

            if i < len(combos) - 1 and not dry:
                time.sleep(4)

        hosts = pool
    else:
        combined_query = " || ".join(f"({q})" for q, _, _ in queries)

        if method == "web":
            if not FOFA_WEB_HEADER:
                log.error("FOFA_WEB_HEADER must be set in .env for web method")
                return
            hosts = _fetch_web(dry, limit, queries, combined_query)
            if hosts is None:
                return
        else:
            if not FOFA_KEY:
                log.error("FOFA_KEY must be set in .env")
                return
            hosts = _fetch_api(dry, limit, queries, combined_query)

    if dry:
        return

    existing = _load_json(HOSTS_FILE)
    seen = {f"{h['service']}@{h['host']}" for h in existing}
    for h in hosts:
        key = f"{h['service']}@{h['host']}"
        if key not in seen:
            existing.append(h)
            seen.add(key)

    _save_json(HOSTS_FILE, existing)
    log.info(f"fetch: {len(hosts)} new hosts, {len(existing)} total in seed list")


def scan():
    import ipaddress

    seeds = _load_json(HOSTS_FILE)
    if not seeds:
        log.warning("scan: no seed hosts — run fetch first")
        return

    nets = set()
    for entry in seeds:
        host = entry["host"].split(":")[0]
        try:
            ip = ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError:
            continue
        nets.add(str(ipaddress.IPv4Network(f"{ip}/{SUBNET}", strict=False)))

    log.info(f"scan: expanding {len(seeds)} seeds to {len(nets)} /{SUBNET} networks")

    ports_str = ",".join(str(p) for p in PORTS)
    result = subprocess.run(
        ["nmap", "-p", ports_str, "--open", "-T4", "-oG", "-"] + list(nets),
        capture_output=True, text=True, timeout=600,
    )

    discovered = []
    for line in result.stdout.splitlines():
        if not line.startswith("Host:"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ip = parts[0].replace("Host:", "").strip()
        for port in PORTS:
            if f"{port}/open" in line:
                discovered.append({
                    "service": SERVICES.get(port, "a1111"),
                    "host": f"{ip}:{port}",
                })

    existing = _load_json(DOORKNOCK_FILE)
    seen = {f"{h['service']}@{h['host']}" for h in existing}
    for h in discovered:
        key = f"{h['service']}@{h['host']}"
        if key not in seen:
            existing.append(h)
            seen.add(key)

    _save_json(DOORKNOCK_FILE, existing)
    log.info(f"scan: {len(discovered)} discovered, {len(existing)} total in doorknock file")


async def _check_all():

    hosts = _load_json(DOORKNOCK_FILE) or _load_json(HOSTS_FILE)
    if not hosts:
        log.warning("check: no hosts — run fetch first")
        return

    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
        tasks = []
        for entry in hosts:
            host_port = entry["host"].split(":")
            h = host_port[0]
            p = int(host_port[1]) if len(host_port) > 1 else 8188
            s = entry.get("service")
            tasks.append(_check_host(session, h, p, s))
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
                "service": entry.get("service", "a1111"),
                "host": entry["host"],
                "reason": reason,
                "last_checked": now,
            })

    existing = _load_json(WORKING_FILE)
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
    _save_json(WORKING_FILE, existing)
    _save_json(NOTWORKING_FILE, notworking)
    log.info(f"check: {len(hosts)} checked, {len(working)} working, {len(notworking)} notworking")


def check():
    asyncio.run(_check_all())


STEPS = {"fetch": fetch, "check": check}
VALID_SEQUENCES = [
    ("check",),
    ("fetch", "check"),
]


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
    parser.add_argument("-a", "--action", choices=["check", "fetch-check"], help="action to perform")
    parser.add_argument("-d", "--dry", action="store_true", help="report what fetch would do without saving")
    parser.add_argument("-l", "--limit", type=int, default=2, help="max results per query (default: 2)")
    parser.add_argument("-s", "--service", nargs="+", default=["all"], choices=["all", "comfyui", "a1111"], help="services to search (default: all)")
    parser.add_argument("-m", "--method", choices=["api", "web"], default="web", help="fetch method (default: web)")
    parser.add_argument("--notricks", action="store_true", help="disable country/port query variations")
    args = parser.parse_args()

    if args.action is None:
        parser.print_help()
        sys.exit(1)

    raw = args.action
    parts = raw.split("-")

    if tuple(parts) not in VALID_SEQUENCES:
        valid = " | ".join("-".join(s) for s in VALID_SEQUENCES)
        print(f"usage: graflex {{{valid}}}", file=sys.stderr)
        sys.exit(1)

    for step in parts:
        log.info(f"--- {step} ---")
        if step == "fetch":
            STEPS[step](dry=args.dry, limit=args.limit, services=args.service, method=args.method, notricks=args.notricks)
        else:
            STEPS[step]()
