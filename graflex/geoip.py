"""Offline IP -> country / ASN / cloud-provider enrichment for host records.

Everything is local: we download DB-IP's free *Lite* databases (CC BY 4.0, no
account, https://db-ip.com) once a month and look each host IP up in-process.
No host IP is ever sent to a third-party geo/ASN API, which matters because the
pool includes privately-sourced hosts whose IPs must not be disclosed.

Deliberately stdlib-only (csv/gzip/urllib/ipaddress/bisect): this ships as
redistributable software, so it must not require a signup *or* a native mmdb
reader. Country + ASN come from the small Lite CSVs, which load fine. City and
lat/lon (for a future map view) want the compact .mmdb format and are left as
schema-ready fields to fill in later.

Attribution required by CC BY 4.0: "IP data by DB-IP (https://db-ip.com)".
"""
import bisect
import csv
import gzip
import ipaddress
import logging
import os
import re
import socket
import sys
import time
import urllib.request

log = logging.getLogger("dyva.geoip")

GEO_DIR = os.path.join(os.path.expanduser("~/.cache/free-ollama"), "geo")
DBIP_BASE = "https://download.db-ip.com/free"
DBIP_ATTRIBUTION = "IP data by DB-IP (https://db-ip.com) — CC BY 4.0"

# as_org substring -> canonical provider bucket. First match wins, so order
# only matters where one org name contains another (it doesn't here). Anything
# unmatched — including GPU marketplaces like RunPod/Vast that sit on other
# operators' ASNs — is left blank rather than guessed.
_PROVIDERS = [
    ("amazon", "aws"), ("aws", "aws"),
    ("google", "gcp"),
    ("microsoft", "azure"), ("azure", "azure"),
    ("digitalocean", "digitalocean"),
    ("hetzner", "hetzner"),
    ("ovh", "ovh"),
    ("vultr", "vultr"), ("choopa", "vultr"), ("constant company", "vultr"),
    ("linode", "linode"), ("akamai", "linode"),
    ("contabo", "contabo"),
    ("oracle", "oracle"),
    ("scaleway", "scaleway"), ("online s.a.s", "scaleway"), ("online sas", "scaleway"),
    ("leaseweb", "leaseweb"),
    ("alibaba", "alibaba"), ("aliyun", "alibaba"),
    ("tencent", "tencent"),
    ("cloudflare", "cloudflare"),
]


def provider_of(as_org):
    s = (as_org or "").lower()
    for kw, name in _PROVIDERS:
        if kw in s:
            return name
    return ""


class _RangeIndex:
    """Sorted IP-range table with a longest-below-then-verify lookup, split by
    address family so v4 and v6 never collide in one integer space."""

    def __init__(self):
        # each family: (starts[int], ends[int], data[tuple])
        self._v4 = ([], [], [])
        self._v6 = ([], [], [])

    def _fam(self, version):
        return self._v4 if version == 4 else self._v6

    def add(self, start, end, data):
        try:
            a = ipaddress.ip_address(start)
            b = ipaddress.ip_address(end)
        except ValueError:
            return
        if a.version != b.version:
            return
        fam = self._fam(a.version)
        fam[0].append(int(a))
        fam[1].append(int(b))
        fam[2].append(data)

    def finalize(self):
        for fam in (self._v4, self._v6):
            if not fam[0]:
                continue
            order = sorted(range(len(fam[0])), key=lambda i: fam[0][i])
            fam[0][:] = [fam[0][i] for i in order]
            fam[1][:] = [fam[1][i] for i in order]
            fam[2][:] = [fam[2][i] for i in order]

    def lookup(self, ip):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return None
        fam = self._fam(a.version)
        starts, ends, data = fam
        if not starts:
            return None
        n = int(a)
        i = bisect.bisect_right(starts, n) - 1   # last range whose start <= n
        if i >= 0 and ends[i] >= n:
            return data[i]
        return None


def _load_csv(path, ncols):
    """Load a DB-IP Lite CSV (no header) into a range index. Rows are
    start_ip, end_ip, then `ncols-2` payload columns."""
    idx = _RangeIndex()
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            if len(row) < ncols:
                continue
            idx.add(row[0], row[1], tuple(row[2:ncols]))
    idx.finalize()
    return idx


def _download(url, dest):
    tmp = dest + ".tmp"
    log.info(f"geoip: downloading {os.path.basename(dest)}")
    req = urllib.request.Request(url, headers={"User-Agent": "dyva-geoip"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        try:
            total = int(r.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        got = 0
        tty = sys.stderr.isatty()
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if tty:
                if total:
                    frac = got / total
                    bar = "#" * int(frac * 30)
                    sys.stderr.write(f"\r  [{bar:<30}] {frac*100:4.0f}%  "
                                     f"{got/1e6:.1f}/{total/1e6:.1f} MB")
                else:
                    sys.stderr.write(f"\r  {got/1e6:.1f} MB")
                sys.stderr.flush()
        if tty:
            sys.stderr.write("\n")
            sys.stderr.flush()
    os.replace(tmp, dest)


def _ensure(kind):
    """Path to the current DB-IP Lite `kind` CSV, downloading it if missing.
    DB-IP publishes monthly; the current month can lag a day or two into the
    month, so fall back to the previous month. Old months are pruned."""
    os.makedirs(GEO_DIR, exist_ok=True)
    now = time.gmtime()
    months = [time.strftime("%Y-%m", now)]
    prev = (now.tm_year, now.tm_mon - 1) if now.tm_mon > 1 else (now.tm_year - 1, 12)
    months.append("%04d-%02d" % prev)
    # already have one on disk?
    for m in months:
        p = os.path.join(GEO_DIR, f"dbip-{kind}-lite-{m}.csv.gz")
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    # otherwise fetch, newest first
    last_err = None
    for m in months:
        fn = f"dbip-{kind}-lite-{m}.csv.gz"
        p = os.path.join(GEO_DIR, fn)
        try:
            _download(f"{DBIP_BASE}/{fn}", p)
            _prune_old(kind, keep=fn)
            return p
        except Exception as e:
            last_err = e
            log.debug(f"geoip: {fn} not available: {e}")
    raise RuntimeError(f"could not fetch DB-IP {kind} lite: {last_err}")


def _prune_old(kind, keep):
    for f in os.listdir(GEO_DIR):
        if f.startswith(f"dbip-{kind}-lite-") and f != keep:
            try:
                os.remove(os.path.join(GEO_DIR, f))
            except OSError:
                pass


_asn_idx = None


def _asn():
    global _asn_idx
    if _asn_idx is None:
        _asn_idx = _load_csv(_ensure("asn"), 4)           # start,end,asn,org
    return _asn_idx


def _asn_lookup(ip):
    """{asn, as_org, provider} for an IP from the (small) ASN index."""
    out = {}
    a = _asn().lookup(ip)
    if a:
        try:
            out["asn"] = int(a[0])
        except (ValueError, IndexError):
            pass
        org = a[1] if len(a) > 1 else ""
        if org:
            out["as_org"] = org
            p = provider_of(org)
            if p:
                out["provider"] = p
    return out


def _iter_csv(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            yield row


def _sweep_city(targets, path):
    """Assign country/city/lat/lon by a single sorted sweep of the huge city CSV.

    The city DB is far too big to index in memory, but one linear pass against
    the (few thousand) host IPs — sorted, and matched per address family since
    the CSV is sorted by start IP within each — is cheap. `targets` is a list of
    [record, version, ip_int, ...]; records are mutated in place."""
    byv = {4: [], 6: []}
    for t in targets:
        byv[t[1]].append(t)
    for v in byv:
        byv[v].sort(key=lambda t: t[2])
    ptr = {4: 0, 6: 0}
    for row in _iter_csv(path):
        if ptr[4] >= len(byv[4]) and ptr[6] >= len(byv[6]):
            break
        if len(row) < 8:
            continue
        try:
            a = ipaddress.ip_address(row[0]); b = ipaddress.ip_address(row[1])
        except ValueError:
            continue
        if a.version != b.version:
            continue
        lst = byv[a.version]
        i = ptr[a.version]
        if i >= len(lst):
            continue
        lo, hi = int(a), int(b)
        while i < len(lst) and lst[i][2] < lo:   # host in a gap — no data
            i += 1
        while i < len(lst) and lst[i][2] <= hi:
            rec = lst[i][0]
            if row[3]:
                rec["country"] = row[3]
            if row[5]:
                rec["city"] = row[5]
            try:
                rec["lat"] = float(row[6]); rec["lon"] = float(row[7])
            except (ValueError, TypeError):
                pass
            i += 1
        ptr[a.version] = i


_HOST_IP_CACHE = {}


def ip_from_server(server, resolve=False):
    """Pull the IP out of a `http://host:port` string. Raw IPs (the common
    case) are returned directly. A hostname is only DNS-resolved when
    `resolve=True` — off by default, because resolving thousands of hostnames
    serially turns a bulk pass into a multi-minute DNS crawl; the survey targets
    are overwhelmingly raw IPs anyway. Returns None when it can't get an IP."""
    h = re.sub(r"^https?://", "", server or "").strip().split("/")[0]
    if not h:
        return None
    if h.startswith("["):                       # [v6]:port
        host = h[1:].split("]")[0]
    elif h.count(":") == 1:                      # host:port  (v4 or name)
        host = h.split(":")[0]
    else:
        host = h                                 # bare v6, or name without port
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not resolve:
        return None
    if host in _HOST_IP_CACHE:
        return _HOST_IP_CACHE[host]
    ip = None
    prev = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(2)
        ip = socket.gethostbyname(host)
    except Exception:
        ip = None
    finally:
        socket.setdefaulttimeout(prev)          # don't leave a global timeout set
    _HOST_IP_CACHE[host] = ip
    return ip


# fields this module writes onto a record; cleared when re-enriching
GEO_FIELDS = ("country", "city", "lat", "lon", "asn", "as_org", "provider")

# where the host/IP might live — the sources are wildly inconsistent about this
_HOST_KEYS = ("server", "url", "host", "address", "ip", "endpoint")


def _host_field(rec, key=None):
    if key:
        return rec.get(key)
    for k in _HOST_KEYS:
        v = rec.get(k)
        if v:
            return v
    return None


def enrich_records(records, key=None, refresh=False, max_age_days=30, resolve=False):
    """Stamp geo/provider onto host records in place, idempotently.

    Schema-tolerant so it can run over any of the ragged source files: the IP
    comes from `key` if given, else the first of server/url/host/... present,
    else (for a dict keyed by host) the dict key itself. Records already
    enriched within `max_age_days` are skipped unless `refresh`, so it is safe
    and near-free to call anywhere, repeatedly. `records` may be a list of dicts
    or a dict of host -> dict. Returns (looked_up, matched)."""
    now = time.time()
    max_age = max_age_days * 86400
    pairs = records.items() if isinstance(records, dict) else ((None, r) for r in records)
    targets = []      # [record, version, ip_int, ip_str]
    for host_key, r in pairs:
        if not isinstance(r, dict):
            continue
        if not refresh and r.get("geo_checked") and now - r["geo_checked"] < max_age:
            continue
        src = _host_field(r, key)
        if not src and host_key is not None:
            src = host_key          # dict-keyed-by-host file
        ip = ip_from_server(src, resolve=resolve)
        r["geo_checked"] = now
        if not ip:
            continue
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        targets.append([r, a.version, int(a), ip])
    looked = len(targets)
    if not targets:
        return looked, 0
    # country / city / lat / lon: one sweep of the big city DB
    try:
        _sweep_city(targets, _ensure("city"))
    except Exception as e:
        log.warning(f"geoip: city sweep failed: {e}")
    # asn / provider: the small ASN index
    matched = 0
    for r, ver, n, ips in targets:
        try:
            info = _asn_lookup(ips)
        except Exception:
            info = {}
        for k in ("asn", "as_org", "provider"):
            if k in info:
                r[k] = info[k]
        if r.get("country") or r.get("asn"):
            matched += 1
    return looked, matched
