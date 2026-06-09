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

PORTS = [8188, 7860, 9090]
TIMEOUT = 10
SUBNET = 24

FOFA_EMAIL = os.getenv("FOFA_EMAIL", "")
FOFA_KEY = os.getenv("FOFA_KEY", "")
FOFA_API = "https://fofa.info/api/v1/search/all"

SERVICES = {
    8188: "comfyui",
    7860: "a1111",
    9090: "invokeai",
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


async def _check_host(session, host, port):
    service = SERVICES.get(port, "unknown")
    url = f"http://{host}:{port}"
    try:
        if port == 8188:
            resp = await asyncio.wait_for(
                session.get(f"{url}/system_stats"), timeout=TIMEOUT
            )
            if resp.status != 200:
                await resp.release()
                return None
            data = await resp.json()
            await resp.release()
            return {
                "service": service,
                "host": f"{host}:{port}",
                "gpu": data.get("device", {}).get("name", ""),
                "vram_total": data.get("device", {}).get("vram_total", 0),
                "vram_free": data.get("device", {}).get("vram_free", 0),
                "last_checked": time.time(),
            }
        elif port == 7860:
            resp = await asyncio.wait_for(
                session.get(f"{url}/sdapi/v1/memory"), timeout=TIMEOUT
            )
            if resp.status != 200:
                await resp.release()
                return None
            data = await resp.json()
            await resp.release()
            return {
                "service": service,
                "host": f"{host}:{port}",
                "gpu": data.get("gpu", {}).get("name", ""),
                "vram_total": data.get("gpu", {}).get("cuda", {}).get("total", 0),
                "vram_free": data.get("gpu", {}).get("cuda", {}).get("free", 0),
                "last_checked": time.time(),
            }
        else:
            resp = await asyncio.wait_for(
                session.get(url), timeout=TIMEOUT
            )
            if resp.status != 200:
                await resp.release()
                return None
            await resp.release()
            return {
                "service": service,
                "host": f"{host}:{port}",
                "last_checked": time.time(),
            }
    except (asyncio.TimeoutError, OSError, json.JSONDecodeError):
        return None
    except Exception:
        return None


def fetch(dry=False):
    if not FOFA_EMAIL or not FOFA_KEY:
        log.error("FOFA_EMAIL and FOFA_KEY must be set in .env")
        return

    import base64
    import urllib.request
    import urllib.parse
    import ipaddress

    hosts = []
    for port in PORTS:
        query = f'port="{port}"'
        params = urllib.parse.urlencode({
            "email": FOFA_EMAIL,
            "key": FOFA_KEY,
            "qbase64": base64.b64encode(query.encode()).decode(),
            "fields": "ip,port,host",
            "size": 10000,
        })
        url = f"{FOFA_API}?{params}"
        try:
            resp = urllib.request.urlopen(url, timeout=30)
            data = json.loads(resp.read())
            if data.get("error"):
                log.error(f"FOFA error: {data.get('error')}")
                continue
            for row in data.get("results", []):
                ip_addr, port_num, hostname = row[0], row[1], row[2]
                hosts.append({
                    "service": SERVICES.get(int(port_num), "unknown"),
                    "host": f"{ip_addr}:{port_num}",
                })
        except Exception as e:
            log.error(f"FOFA request failed for port {port}: {e}")

    if dry:
        for h in hosts:
            log.info(f"  {h['service']:>8}  {h['host']}")
        log.info(f"fetch: {len(hosts)} hosts found (dry run)")
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
                    "service": SERVICES.get(port, "unknown"),
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
    if aiohttp is None:
        log.error("check requires aiohttp — install with: pip install aiohttp")
        return

    hosts = _load_json(DOORKNOCK_FILE)
    if not hosts:
        log.warning("check: no hosts — run scan first")
        return

    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
        tasks = []
        for entry in hosts:
            host_port = entry["host"].split(":")
            h = host_port[0]
            p = int(host_port[1]) if len(host_port) > 1 else 8188
            tasks.append(_check_host(session, h, p))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    working = []
    for r in results:
        if r and not isinstance(r, Exception):
            working.append(r)

    existing = _load_json(WORKING_FILE)
    now = time.time()
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

    _save_json(WORKING_FILE, existing)
    log.info(f"check: {len(working)} working, {len(existing)} total in working registry")


def check():
    asyncio.run(_check_all())


STEPS = {"fetch": fetch, "scan": scan, "check": check}
VALID_SEQUENCES = [
    ("check",),
    ("scan", "check"),
    ("fetch", "scan", "check"),
]


def main():
    load_dotenv()

    global FOFA_EMAIL, FOFA_KEY
    FOFA_EMAIL = os.getenv("FOFA_EMAIL", "")
    FOFA_KEY = os.getenv("FOFA_KEY", "")

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOGLEVEL", "INFO").upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="Discover public image-generation servers")
    parser.add_argument("command", nargs="?", help="check | scan-check | fetch-scan-check")
    parser.add_argument("--dry", action="store_true", help="report what fetch would do without saving")
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    raw = args.command
    parts = raw.split("-")

    if tuple(parts) not in VALID_SEQUENCES:
        valid = " | ".join("-".join(s) for s in VALID_SEQUENCES)
        print(f"usage: graflex {{{valid}}}", file=sys.stderr)
        sys.exit(1)

    for step in parts:
        log.info(f"--- {step} ---")
        if step == "fetch":
            STEPS[step](dry=args.dry)
        else:
            STEPS[step]()
