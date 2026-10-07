import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import socket
import ssl
import sys
import tempfile
import time

from dotenv import load_dotenv

try:
    import aiohttp
except ImportError:
    aiohttp = None

log = logging.getLogger("graflex")


def _permissive_ssl():
    """A maximally-lenient client TLS context for CHECKING exposed hosts: they run
    self-signed / expired / mismatched certs and old TLS with weak ciphers/keys, so
    accept ANY cert (no verify, no hostname check) and the WIDEST handshake (legacy
    renegotiation, low cipher security level, old protocol versions). Not just
    `ssl=False` — that skips the cert but still fails the handshake on such servers."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for _apply in (
        lambda: setattr(ctx, "minimum_version", ssl.TLSVersion.MINIMUM_SUPPORTED),
        lambda: ctx.set_ciphers("DEFAULT@SECLEVEL=0"),
        lambda: ctx.__setattr__("options", ctx.options | ssl.OP_LEGACY_SERVER_CONNECT),
    ):
        try:
            _apply()
        except Exception:
            pass
    return ctx


INSECURE_SSL = _permissive_ssl()

CACHE_DIR = os.path.expanduser("~/.cache/free-ollama")
# geo/provider fields carried across record rebuilds (see geoip.GEO_FIELDS)
_GEO_CARRY = ("geo_checked", "country", "city", "lat", "lon", "asn", "as_org", "provider")
HOSTS_FILE = os.path.join(CACHE_DIR, "image-gen-hosts.json")
WORKING_FILE = os.path.join(CACHE_DIR, "image-gen-working.json")
NOTWORKING_FILE = os.path.join(CACHE_DIR, "image-gen-notworking.json")
CLASSIFIER_FILE = os.path.join(os.path.dirname(__file__), os.pardir, "graflex", "model-classifier.json")

# Confirmed honeypot clone fingerprints (see bogus-fingerprints.json): a cloned
# honeypot image carries the SAME (model, nanosecond timestamp) across every host
# it's deployed on, while a real host has its own. Scoring this is a cheap,
# reconstitutable classifier over the check snapshots — no live probing, and
# unlike the behavioural smoke test it can't be defeated by a host canning a
# "blue" answer (the honeypots already pass that). Per-host additive score.
BOGUS_FINGERPRINTS_FILE = os.path.join(os.path.dirname(__file__), "bogus-fingerprints.json")
_bogus_fp_cache = None


def load_bogus_fingerprints():
    """Confirmed clone fingerprints as a set of (model, timestamp-string) pairs. Cached."""
    global _bogus_fp_cache
    if _bogus_fp_cache is not None:
        return _bogus_fp_cache
    pairs = set()
    try:
        with open(BOGUS_FINGERPRINTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for model, tslist in (data.get("timestamps") or {}).items():
            for ts in tslist or []:
                pairs.add((model, str(ts)))
    except Exception as e:
        log.warning(f"bogus-fingerprints: failed to load {BOGUS_FINGERPRINTS_FILE}: {e}")
    _bogus_fp_cache = pairs
    return pairs


def _fp_model_name(m):
    if isinstance(m, dict):
        return m.get("name") or m.get("model") or m.get("id")
    return m if isinstance(m, str) else None


def _fp_model_timestamp(m):
    """Fingerprint timestamp for a raw model object, either dialect: ollama
    `modified_at` or openai `created`/`created_at`. Returned as a string."""
    if not isinstance(m, dict):
        return None
    ts = m.get("modified_at")
    if ts is None:
        ts = m.get("created", m.get("created_at"))
    return None if ts is None else str(ts)


def bogus_score_from_models(models):
    """How many of a host's RAW model objects (name + modified_at/created) match a
    confirmed bogus fingerprint. Per host, additive; 0 = clean. Needs the raw
    objects, not the name-only list the working.json keeps — so it runs on the
    probe payload / check snapshot."""
    fps = load_bogus_fingerprints()
    if not fps:
        return 0
    n = 0
    for m in models or []:
        name, ts = _fp_model_name(m), _fp_model_timestamp(m)
        if name and ts is not None and (name, ts) in fps:
            n += 1
    return n


def bogus_score_from_payload(payload):
    """bogus_score for a raw check payload — ollama /api/tags (`models`) or openai
    /v1/models (`data`)."""
    if not isinstance(payload, dict):
        return 0
    models = payload.get("models")
    if models is None:
        models = payload.get("data")
    return bogus_score_from_models(models or [])


def _latest_check_dir(session=None):
    base = "/tmp/graflex"
    if session:
        d = os.path.join(base, session, "check")
        return d if os.path.isdir(d) else ""
    best, best_mtime = "", -1
    try:
        for entry in os.listdir(base):
            d = os.path.join(base, entry, "check")
            if os.path.isdir(d):
                mt = os.path.getmtime(d)
                if mt > best_mtime:
                    best, best_mtime = d, mt
    except OSError:
        pass
    return best


def score_bogus(session=None, name=None):
    """Stamp a per-host `bogus_score` onto the working file(s) from confirmed honeypot
    clone fingerprints (bogus-fingerprints.json) matched against a check session's raw
    snapshots. Purely ADDITIVE and RECONSTITUTABLE — it only writes the bogus_score
    field, never drops a host or any other field, and the value is recomputed from the
    fingerprint file + snapshots every run. So there's nothing to guard and no dry-run:
    it just does it. A host not seen this session keeps its prior bogus_score (0 if
    none). `name` picks one service's working file; None / "all" stamps every one.
    dyva routes bogus-score-ascending (0 first)."""
    check_dir = _latest_check_dir(session)
    if not check_dir:
        log.error("score-bogus: no /tmp/graflex/*/check snapshots found"
                  + (f" for session {session}" if session else ""))
        return
    scores = {}
    for fn in os.listdir(check_dir):
        if not fn.endswith(".json"):
            continue
        snap = _load_json(os.path.join(check_dir, fn), silent=True)
        if not isinstance(snap, dict):
            continue
        host = snap.get("host")
        if host:
            scores[host.rstrip("/")] = bogus_score_from_payload(snap.get("payload") or {})
    dist = {}
    for s in scores.values():
        dist[s] = dist.get(s, 0) + 1
    flagged = sum(v for k, v in dist.items() if k > 0)
    log.info(f"score-bogus: {len(scores)} hosts from {check_dir} | "
             f"clean={dist.get(0, 0)} bogus={flagged} | dist={dict(sorted(dist.items()))}")
    # Full bogus list to a file (sorted worst-first) so the hundreds are inspectable
    # without scrolling the terminal.
    bogus_sorted = sorted(((s, h) for h, s in scores.items() if s), reverse=True)
    report_path = os.path.join(os.getcwd(), "bogus-report.txt")
    try:
        with open(report_path, "w") as fh:
            for s, h in bogus_sorted:
                fh.write("%d\t%s\n" % (s, h))
        log.info(f"score-bogus: {len(bogus_sorted)} bogus hosts -> {report_path}")
    except Exception as e:
        log.warning(f"score-bogus: could not write {report_path}: {e}")
    targets = list(SERVICE_CONFIG) if (not name or name == "all") else [name]
    for svc in targets:
        wf = _cache_file(svc, "working")
        entries = _load_json(wf, silent=True)
        if not isinstance(entries, list):
            continue   # service not surveyed — skip silently
        stamped = 0
        for e in entries:
            if not isinstance(e, dict):
                continue
            e["bogus_score"] = scores.get(_entry_host(e), e.get("bogus_score", 0))
            if e["bogus_score"]:
                stamped += 1
        _save_json(wf, entries)
        log.info(f"score-bogus: {svc}: stamped bogus_score on {len(entries)} records ({stamped} bogus) -> {wf}")


def _cache_file(name, suffix):
    prefix = name or "image-gen"
    return os.path.join(CACHE_DIR, f"{prefix}-{suffix}.json")


from . import hoststore as _hoststore   # noqa: E402  (store backing the per-service json)

# Lazily-opened handle to graflex.db — the working store behind working/notworking/hosts.
# A per-host result is a single upsert here, not a whole-file rewrite; the json files are
# regenerated from it by export(). WAL + busy_timeout, so a second scan or dyva reading
# concurrently waits briefly instead of erroring.
_HOSTSTORE = None


def _store():
    global _HOSTSTORE
    if _HOSTSTORE is None:
        _HOSTSTORE = _hoststore.connect(os.path.join(CACHE_DIR, "graflex.db"))
    return _HOSTSTORE


def _store_record(service, host, status, record):
    """Upsert one host RESULT row. status: 'working'|'notworking'. Overwrites status."""
    _hoststore.upsert(_store(), service, host, status, record,
                      record.get("checked") if isinstance(record, dict) else None)


def _store_discovery(service, host):
    """Record a discovered host (status NULL) WITHOUT downgrading one already checked."""
    _hoststore.discover(_store(), service, host, {"service": service, "host": host})


def _export_offthread(service):
    """export() for the periodic mid-check flush, run in a thread executor so the ~32MB
    write can't block the event loop. Uses its own short-lived connection because the
    module _HOSTSTORE handle is bound to the event-loop thread (WAL allows the concurrent
    read while checks keep upserting on the main connection)."""
    conn = _hoststore.connect(os.path.join(CACHE_DIR, "graflex.db"))
    try:
        _hoststore.export_files(conn, _save_json_atomic, _cache_file, service)
    finally:
        conn.close()


def export(service=None):
    """Regenerate <service>-{working,notworking,hosts}.json from the DB, in the exact shapes
    dyva and resume consume. Called at the end of a check (and periodically during one) so the
    json files stay a faithful, byte-shape-identical view of the store. service=None does every
    service (migration/standalone); a name does only that service's three files (periodic flush)."""
    return _hoststore.export_files(_store(), _save_json_atomic, _cache_file, service)


def _import_if_empty():
    """One-time migration: if the DB has no rows yet, load the existing per-service json
    files into it so the first check after the switch starts from today's known hosts."""
    conn = _store()
    if conn.execute("SELECT 1 FROM host LIMIT 1").fetchone() is None:
        # Allowlist to the real services only, so known-hosts.json / image-gen-hosts.json
        # (same -hosts.json suffix, not per-service) are never imported or re-exported.
        n = _hoststore.import_files(conn, CACHE_DIR, _load_json, _entry_host,
                                    valid_services=set(SERVICE_CONFIG))
        if n:
            log.info(f"hoststore: imported {n} records from existing json files")

# Model knowledge base mined from the probe logs (shared with dyva, which loads
# it read-only). Keyed by model name; general/extensible — currently size+digest.
SURVEY_FILE = os.path.join(CACHE_DIR, "survey.json")

TIMEOUT = 60
BACKOFF = 11
MAX_BACKOFF = 300
SLEEP_DEFAULT = 4
STATS_EVERY = 50
# Partial results reach dyva by re-exporting the current service's json mid-sweep, but the
# notworking file can be ~32MB — so the flush is throttled to wall-clock, not per-N-hosts
# (which on fast-failing hosts fires far too often and reintroduces whole-file I/O).
EXPORT_EVERY_SEC = 15
_LAST_EXPORT = 0.0


def _clean_cookie(value):
    return value.replace("\\u0021", "!").strip()


FOFA_COOKIE = os.getenv("FOFA_COOKIE", "")
FOFA_WEB = "https://en.fofa.info/result"
# Appended to the FOFA result URL to bias toward recently-seen (likelier-alive)
# hosts. Trial — set to "" to back out. It does NOT affect the cache-file label
# (which is built from the query axes), so a resumed session still skips pages it
# already fetched under the old, unfiltered URL.
FOFA_FILTER = "&filter_type=last_month"

SHODAN_KEY = _clean_cookie(os.getenv("SHODAN_KEY", ""))
SHODAN_WEB = "https://www.shodan.io/search"
SHODAN_PAGES = 2

# ZoomEye's JSON search API. The session cookie is a SECRET, read only from the
# environment (ZOOMEYE_COOKIE in .env) — never hard-code it or commit it.
ZOOMEYE_API = "https://www.zoomeye.ai/api/search"
# ZoomEye serves only the first ~250 results (pageSize 50 -> 5 pages); page 6+ errors.
ZOOMEYE_MAX_PAGES = 5
# Always constrain to hosts SEEN in the last N days (after=today-N, before=future),
# so the limited points budget is spent on fresh, likelier-alive hosts.
ZOOMEYE_SEEN_DAYS = 21


def _zoomeye_cookie():
    # Read at CALL time, not import: main() runs load_dotenv() after this module is
    # imported, and the cookie may be a freshly-added .env line the shell hasn't
    # exported yet — a module-level `os.getenv` would capture "" and never see it.
    return os.getenv("ZOOMEYE_COOKIE", "")


# Jiasule/加速乐 binds the __jsl_clearance_s clearance cookie to the User-Agent (and the
# egress IP), so the request UA MUST match the browser that generated the cookie — a
# mismatch gets re-challenged (HTTP 521 + the __jsl_clearance_s JS wall) even with a
# valid cookie. The operator sets ZOOMEYE_UA to their browser's UA alongside
# ZOOMEYE_COOKIE; this default matches the previously-captured request. (Same pattern
# as CENSYS_UA.)
ZOOMEYE_UA_DEFAULT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"


def _zoomeye_ua():
    return os.getenv("ZOOMEYE_UA", "") or ZOOMEYE_UA_DEFAULT


# Censys platform search (JSON API behind Cloudflare). The session cookie is a
# SECRET — read only from the environment (CENSYS_COOKIE in .env), never hard-coded
# or committed. It must be human-generated on THIS machine's egress IP: Cloudflare
# binds the clearance to the IP that solved the challenge, so a cookie made
# elsewhere gets bounced. Once it has passed the challenge once, further search.data
# requests on that session are NOT re-gated, so a run can iterate several queries.
CENSYS_API = "https://platform.censys.io/api/search.data"


def _censys_cookie():
    # Read at CALL time (see _zoomeye_cookie), for the same load_dotenv() ordering.
    return os.getenv("CENSYS_COOKIE", "")


# Cloudflare binds the cf_clearance to the User-Agent as well as the IP, so the
# request UA MUST match the browser that generated the cookie or the challenge
# bounces it. The operator sets CENSYS_UA to their browser's UA alongside the
# cookie; this default matches the request captured in censys.txt.
CENSYS_UA_DEFAULT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"


def _censys_ua():
    return os.getenv("CENSYS_UA", "") or CENSYS_UA_DEFAULT


# hunter.how search (POST JSON API). The session cookie is a SECRET, read only from
# the environment (HUNTER_COOKIE in .env) — never hard-coded or committed. The
# plaintext query is base64'd into the request's `search` field (hunter.how's
# encoding). Response is clean JSON: {"code":200,"data":{"list":[{ip,port,...}]}}.
HUNTER_API = "https://hunter.how/api/search"
# hunter.how's coverage mechanism is PAGINATION (not country fan-out): one query,
# walked page by page. The API REQUIRES page_size == 10 (a larger value 400s with
# "The optional range of page size is 10"), so 40 pages = up to 400 results/query.
# We stop early at the first empty page.
HUNTER_PAGE_SIZE = 10
HUNTER_MAX_PAGES = 40
# Constrain to hosts seen in the last N days (fresher, likelier alive), like ZoomEye.
HUNTER_WINDOW_DAYS = 30


def _hunter_cookie():
    # Read at CALL time (see _zoomeye_cookie), for the same load_dotenv() ordering.
    return os.getenv("HUNTER_COOKIE", "")


def _hunter_auth():
    # hunter.how's search API also wants an `Authorization` header (a Bearer token);
    # deeper pages 401 with "coming soon" without it. HUNTER_AUTH is the FULL header
    # value, copied verbatim from a logged-in request (e.g. "Bearer eyJ..."), so we
    # never have to guess whether to prepend "Bearer". '' if not set.
    return os.getenv("HUNTER_AUTH", "").strip()

# run_ts of the most recent fetch session, so ctrl+c in main() can suggest
# the exact -i value to resume with
_RUN_TS = None

SERVICE_CONFIG = {
    "a1111": {
        "port": 7860,
        "fofa_query": 'icon_hash="2075038152" && body="Stable Diffusion"',
        "check_path": "/sdapi/v1/sd-models",
    },
    "fooocus": {
        "port": 7865,
        "fofa_query": '("fooocus") && icon_hash=="2075038152"',
        "check_path": "/v1/engines/all-models",
    },
    "sillytavern": {
        "port": 8000,
        "fofa_query": '(app="sillytavern") && icon_hash=="358928722" && title=="SillyTavern"',
        "ports": "8000,443,80,8001",
        "countries": "CN,US,HK,SG,JP,DE,KR,RU,TW",
        "check_path": "/",
    },
    "gradio": {
        # Not a real inference service — a family of arbitrary Gradio apps. The
        # "check" is fetching /config; `classify` buckets the manifests. Fetch
        # defaults (ports/countries/fids) live in NAMED_QUERIES["gradio"].
        "port": 7860,
        "fofa_query": 'icon_hash=="55115683"',
        "check_path": "/config",
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
        "fofa_query": ['app="ollama"', 'body="ollama is running"'],
        "shodan_query": '"ollama is running"',
        "zoomeye_query": 'app="ollama"',
        "censys_query": '"ollama is running"',
        "hunter_query": 'product.name=="Ollama Server"',
        "check_path": "/api/tags",
    },
    "llama.cpp": {
        "port": 8080,
        "fofa_query": 'server=="llama.cpp"',
        "shodan_query": 'product:llama.cpp',
        "zoomeye_query": 'http.header.server="llama.cpp"',
        "hunter_query": 'header.server=="llama.cpp"',
        "censys_query": 'web.endpoints.http.headers.value="llama.cpp"',
        "check_path": "/v1/models",
    },
    "vllm": {
        # NB: this FOFA body is the GENERIC uvicorn 404, not vllm-specific — so this
        # bucket is really "any uvicorn-fronted OpenAI inference server" (vllm, but
        # also mlx_lm.server / MLC-LLM, ktransformers, ...). They can only be told
        # apart at CHECK time by /v1/models `owned_by` (e.g. "vllm", "MLC-LLM"). To
        # reach the non-8000 ones, broaden the port sweep in graflex.sh, not here.
        "port": 8000,
        "fofa_query": '{"detail": "Not Found"} && server=="uvicorn"',
        "check_path": "/v1/models",
    },
    "lmstudio": {
        "port": 1234,
        "fofa_query": 'body="Unexpected endpoint or method. (GET /)"',
        "zoomeye_query": '"Unexpected endpoint or method"',
        "hunter_query": 'web.body="Unexpected endpoint or method. (GET /)"',
        "check_path": "/v1/models",
    },
    "ds4": {
        # antirez's ds4 (DeepSeek-serving inference server; models report
        # owned_by "ds4.c"). OpenAI dialect: the root and any unknown path return
        # this exact error body, which is the FOFA fingerprint, while /v1/models
        # lists the real models (e.g. deepseek-v4-flash) and /v1/chat/completions
        # serves them. Few in the wild; US/CN only per the user.
        "port": 8080,
        "fofa_query": "'{\"error\":{\"message\":\"unknown endpoint\",\"type\":\"invalid_request_error\"}}'",
        "countries": "US,CN",
        "check_path": "/v1/models",
    },
    "localai": {
        # LocalAI (github.com/mudler/LocalAI): an OpenAI-compatible inference engine whose
        # web UI page title is "LocalAI" (the FOFA fingerprint). /v1/models lists the loaded
        # models and lands in the OpenAI-dialect else branch of _check_host (data[].id).
        "port": 8080,
        "fofa_query": 'title="localai"',
        "check_path": "/v1/models",
    },
}

# Named searches aren't services: they have a FOFA query but no probe/check
# step. Fetch by name alone (-n <name>); entries land in <name>-hosts.json
# with service set to the name.
NAMED_QUERIES = {
    # Ported from graflex.sh's gradio case so `-n gradio -a fetch` finds the
    # same hosts standalone (the Gradio UI commonly runs on 7860). A named
    # query may be a bare string, or a dict with default ports/countries/fids
    # that the CLI can still override.
    "gradio": {
        "query": 'icon_hash=="55115683"',
        "ports": "80,443,8080,7860",
        "countries": "US,CN,DE,IN,JP,KR,BR,GB,FR,HK,TW,CA,AU,RU,NL,SG,ID,VN,IT,ES",
        "fids": ["CfOOPt6Nd3WtpgTJF1CZMQ==", "SKGUqQuUlkehGS8jB/cz3w==",
                 "bkoVAuNuNwTuBfCjZ+d4xw==", "sGe21936bIKF2zWmLyb7fQ==",
                 "t5OB7B8z43gJDAyGFPredQ==", "zO99w44qU6me2LeJntB/xw==",
                 "N/Vkkdevw+ddMZQvyu4UHw=="],
    },
}


def _named_query(name):
    """(query_string, defaults_dict) for a named query, tolerating both the
    bare-string and dict forms."""
    v = NAMED_QUERIES.get(name)
    if isinstance(v, dict):
        return v.get("query"), v
    return v, {}

# Services that cache a raw model-list snapshot per host during check, so a
# resume (-i) run can skip hosts already snapshotted this session.
SNAPSHOT_SERVICES = {"ollama", "vllm", "lmstudio", "llama.cpp", "localai"}


def _load_json(path, silent=False):
    if not silent:
        print(f"Loading {path}")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    # There is no reason for a second, non-atomic writer to exist: a reader
    # during this write used to see a half-written file.
    _save_json_atomic(path, data)


def _host_ident(hostport):
    """Readable, reversible snapshot filename for a host — so you can find a host's
    file by eye (1.2.3.4_11434, ollama.example.com_8080) instead of running an md5
    yourself. Keeps dots/dashes; turns ':' and any other unsafe char into '_'. A
    distinct host:port yields a distinct name, so (unlike the old _tag md5[:8] stub)
    there are no filename collisions."""
    return re.sub(r'[^A-Za-z0-9._-]', '_', str(hostport))


def _save_check_snapshot(host, port, data):
    """Cache a raw model-list API response (ollama /api/tags, vllm /v1/models,
    lmstudio /v1/models, llama.cpp /v1/models) to
    /tmp/graflex/{date}/check/{ident}.json following the same /tmp/graflex
    dir and %Y%m%d%H%M%S date convention as the fetch result files. The filename
    is a readable, reversible encoding of the full host:port (see _host_ident) so a
    host's snapshot is findable by eye; the real host (with port), a unix
    check_time, and the raw payload are all stored in a super-structure inside the
    file. The date is the active session run_ts when resuming (-i) so already-saved
    hosts are skippable."""
    from datetime import datetime, timezone
    hostport = f"{host}:{port}"
    ident = _host_ident(hostport)
    date = _RUN_TS or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    tmp_dir = os.path.join("/tmp/graflex", date, "check")
    os.makedirs(tmp_dir, exist_ok=True)
    with open(os.path.join(tmp_dir, f"{ident}.json"), "w", encoding="utf-8") as f:
        json.dump({
            "host": hostport,
            "check_time": time.time(),
            "payload": data,
        }, f, indent=2)


def _check_snapshot_exists(host, port, run_ts):
    """True if a snapshot for host:port was already saved in this session, so a
    resumed -i run skips hosts it already checked."""
    ident = _host_ident(f"{host}:{port}")
    return os.path.exists(os.path.join("/tmp/graflex", run_ts, "check", f"{ident}.json"))


def _check_snapshot_idents(run_ts):
    """The set of host idents (see _host_ident) that already have a success snapshot this
    session, read with ONE os.listdir. The resume-skip loops use this instead of an
    os.path.exists PER host: on a large pool over slow/contended storage, N stat() calls
    serialize into minutes of disk I/O before the first probe (the 'check sits silent' hang)."""
    d = os.path.join("/tmp/graflex", run_ts, "check")
    try:
        return {n[:-5] for n in os.listdir(d) if n.endswith(".json")}
    except OSError:
        return set()


def _check_failed_path(run_ts):
    """Session-scoped record of hosts that failed their check, alongside the
    per-host success snapshots under /tmp/graflex/{run_ts}/."""
    from datetime import datetime, timezone
    date = run_ts or _RUN_TS or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return os.path.join("/tmp/graflex", date, "failed.json")


def _save_check_failure(host, port, reason):
    """Record a host that failed its check this session (unreachable, auth
    required, bad JSON, ...) so a resumed (-i) run skips it instead of
    re-probing a box already known bad for this run — the failure counterpart of
    _save_check_snapshot. Only successes were remembered before, so failed hosts
    were the whole cost of every resume. Called under the caller's write-lock,
    so the read-modify-write of the single file is safe."""
    if not _RUN_TS:
        return
    path = _check_failed_path(_RUN_TS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    failed = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                failed = json.load(f)
            if not isinstance(failed, dict):
                failed = {}
        except (ValueError, OSError):
            failed = {}
    failed[f"{host}:{port}"] = {"reason": reason, "check_time": time.time()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(failed, f, indent=2)


def _load_check_failed(run_ts):
    """Set of host:port strings that already failed this session (empty if the
    file is absent or unreadable)."""
    path = _check_failed_path(run_ts)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.keys()) if isinstance(data, dict) else set()
    except (ValueError, OSError):
        return set()


# Lobotomy guard: a failed pass must never shrink a populated cache to a stub.
# Below this many existing records we don't bother (new/tiny files change freely);
# at or above it, a drop of more than this fraction is refused (see _save_json_atomic).
LOBOTOMY_FLOOR = 10
LOBOTOMY_MAX_DROP = 0.40


def _record_count(x):
    """Number of records in a cache payload — list length or dict size; anything
    else counts as 0 so the guard treats it as 'no records to protect'."""
    return len(x) if isinstance(x, (list, dict)) else 0


def _save_json_atomic(path, data):
    """Write JSON so a concurrent writer can't corrupt the result.

    The temp name has to be unique per write. With a fixed `path + ".tmp"`,
    two writers of the same cache share one temp file: the second
    open(..., "w") truncates it while the first is still writing, both then
    write at their own offsets, and os.replace publishes the mixture. When the
    second dump is shorter you get a complete-looking document followed by the
    tail of the longer one — a valid object and then garbage on the end. That
    is the corruption; it has nothing to do with file size or the json module,
    which parses a 129 MB document without complaint.
    """
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    # LOBOTOMY GUARD. A failed run — 7000 hosts loaded, 3 answer — must not write
    # its handful of results over a full pool. Compare the incoming record count
    # against what's already on disk; a drop of more than LOBOTOMY_MAX_DROP is
    # almost always a bad pass, not a real change, so REFUSE and keep the file.
    # (Normal operation only appends, so it never trips; and refusing self-heals —
    # the next read sees the intact file and re-appends correctly.)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                prev_n = _record_count(json.load(f))
            new_n = _record_count(data)
            if prev_n >= LOBOTOMY_FLOOR and new_n < prev_n * (1 - LOBOTOMY_MAX_DROP):
                log.error(
                    f"REFUSING to write {os.path.basename(path)}: {new_n} records would "
                    f"replace {prev_n} on disk — a {1 - new_n / prev_n:.0%} drop looks like a "
                    f"failed pass lobotomizing the file, not a real change. File left intact.")
                return
    except (ValueError, OSError):
        pass   # existing file unreadable/corrupt — nothing intact to protect, proceed
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())     # a crash mustn't publish a short file
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def _is_ip_literal(h):
    """True if h is a bare IP address (v4 or v6), i.e. not a hostname to resolve."""
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


async def _resolve_ipv4(host, timeout=None):
    """First IPv4 A record for a hostname, or None. Runs getaddrinfo off the
    event loop, BOUNDED by `timeout`: a hostname whose resolver is dead or slow
    must not hang the check past its --ct. getaddrinfo has no native async
    cancel, so it is wrapped in wait_for and abandoned (the orphaned lookup
    finishes on its own) — returning None just means the directip fallback is
    skipped for this host, not that the host is failed."""
    try:
        coro = asyncio.get_event_loop().getaddrinfo(
            host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        infos = await (asyncio.wait_for(coro, timeout) if timeout else coro)
    except (socket.gaierror, OSError, UnicodeError, asyncio.TimeoutError):
        return None
    for info in infos:
        if info[0] == socket.AF_INET:
            return info[4][0]
    return None


async def _doorknock(session, entry, service, check_timeout, existing_working=None):
    """Probe an entry through the doorknock ladder, stopping at the first vector
    that answers as the real service:

      1. claimed host : claimed port            (the default)
      2. resolved IP  : claimed port            }  only when the claimed host is
      3. resolved IP  : canonical service port  }  a NAME that resolves elsewhere

    Reaching the service by IP where the name would gate it is the `directip`
    strategy (see graflex README "Recording how a host was reached"). Returns
    (result, method): `method` is the directip tag when a non-default vector won,
    else None; `result` is the winning probe result, or the claimed host's own
    error when nothing answered. The offline/back-off retry from the old inline
    loop is preserved per knock."""
    host_port = entry["host"].split(":")
    h = host_port[0]
    p = int(host_port[1]) if len(host_port) > 1 else SERVICE_CONFIG[service]["port"]
    canon = SERVICE_CONFIG[service]["port"]

    async def knock(kh, kp):
        backoff = BACKOFF
        while True:
            result = await _check_host(session, kh, kp, service, timeout=check_timeout)
            if isinstance(result, dict) and "error" in result and _is_offline_msg(result["error"]):
                reachable = await _is_network_reachable(session, existing_working)
                if reachable:
                    log.warning(f"~ {entry['host']}: {result['error']} (host unreachable, not offline)")
                    return result
                log.warning(f"~ {entry['host']}: {result['error']} (offline, retrying in {backoff}s...)")
                await asyncio.sleep(backoff)
                backoff = min(int(backoff * 1.2), MAX_BACKOFF)
                continue
            return result

    # 1. the claimed host:port (the default). A working host answers here and
    # never pays for DNS or the fallback ladder below.
    default_result = await knock(h, p)
    if isinstance(default_result, dict) and "error" not in default_result:
        return default_result, None

    # 2/3. only when the claimed NAME didn't answer: it may be gated while the
    # raw IP is open. Resolve NOW — bounded by check_timeout so a dead resolver
    # can't hang the check past --ct — then try IP:claimed-port, IP:canonical.
    if not _is_ip_literal(h):
        ip = await _resolve_ipv4(h, timeout=check_timeout)
        if ip and ip != h:
            for kp in ([p] if p == canon else [p, canon]):
                result = await knock(ip, kp)
                if isinstance(result, dict) and "error" not in result:
                    return result, {"strategy": "directip",
                                    "params": {"hostname": h, "ip": ip, "port": kp}}
    return default_result, None


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


MODEL_EXTS = (".safetensors", ".sft", ".ckpt", ".pth", ".pt", ".bin", ".gguf", ".onnx")


def _iter_models(models):
    """Yield (name, meta_dict) from any `models` shape — a list of name strings, a
    list of raw /api/tags model dicts (whose `capabilities` become `caps`), or an
    existing Form A object keyed by name. Best-effort and fail-safe: an item it
    can't read a name from is skipped, never raised."""
    if isinstance(models, dict):
        for name, meta in models.items():
            if isinstance(name, str) and name:
                yield name, (dict(meta) if isinstance(meta, dict) else {})
    elif isinstance(models, (list, tuple)):
        for m in models:
            name = _model_name(m)
            if not name:
                continue
            caps = m.get("capabilities") if isinstance(m, dict) else None
            yield name, ({"caps": list(caps)} if isinstance(caps, list) else {})


def _filter_models(models):
    """Normalize any input to the canonical Form A object — {name: {caps:[...]}} or
    {name: {}} — with :cloud proxy entries dropped. Accepts a list of names, a list
    of raw /api/tags model dicts (capabilities captured as caps), or an existing
    Form A object. An empty {} for a model means 'caps unknown from this source',
    never 'has no caps'."""
    out = {}
    for name, meta in _iter_models(models):
        if name.endswith(":cloud"):
            continue
        if name in out:
            out[name].update(meta)
        else:
            out[name] = meta
    return out


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


async def _comfyui_folders_direct(session, base_url, folders, timeout=TIMEOUT):
    """Fallback for comfyui hosts whose /models category index is unavailable (older
    or locked ComfyUI): probe the named model folders DIRECTLY — their per-folder
    endpoints answer even when /models doesn't — and return category-prefixed model
    files (e.g. 'diffusion_models/flux1-dev.safetensors'), matching the tree's shape.
    Best-effort: a folder that errors or doesn't return a list is skipped."""
    out = []
    for folder in folders:
        try:
            resp = await asyncio.wait_for(
                session.get(f"{base_url}models/{folder}", allow_redirects=False),
                timeout=timeout)
            if resp.status != 200:
                await resp.release()
                continue
            got = await resp.json()
            await resp.release()
        except Exception:
            continue
        if isinstance(got, list):
            for m in got:
                if isinstance(m, str) and m.lower().endswith(MODEL_EXTS):
                    out.append(f"{folder}/" + m.replace("\\", "/"))
    return out


async def _comfyui_model_tree(session, base_url, timeout=TIMEOUT, workers=8):
    """Traverse /models -> /models/<category> and return entries prefixed with
    their category, e.g. 'checkpoints/majic_v7_sd15.safetensors'. Nested
    backslash paths are normalized to forward slashes. Returns None if the
    host doesn't expose a usable /models index (older ComfyUI)."""
    import urllib.parse

    prefix = None
    cats = None
    for pfx in ("", "api/"):
        try:
            resp = await asyncio.wait_for(
                session.get(f"{base_url}{pfx}models", allow_redirects=False), timeout=timeout
            )
            if resp.status != 200:
                await resp.release()
                continue
            got = await resp.json()
            await resp.release()
        except Exception:
            continue
        if isinstance(got, list) and got:
            prefix, cats = pfx, got
            break
    if cats is None:
        return None
    cats = [c for c in cats if isinstance(c, str) and c]

    sem = asyncio.Semaphore(workers)

    async def list_folder(cat):
        async with sem:
            try:
                url = f"{base_url}{prefix}models/{urllib.parse.quote(cat)}"
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
            return [
                f"{cat}/{f}".replace("\\", "/")
                for f in files
                if isinstance(f, str) and f.lower().endswith(MODEL_EXTS)
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
    http_error = None

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
                start_time = time.time()
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
                elif service == "fooocus":
                    # Fooocus-API: {"model_filenames": [...], "lora_filenames": [...]}
                    data = await resp.json()
                    await resp.release()
                    if not isinstance(data, dict) or "model_filenames" not in data:
                        last_error = {"error": f"not a fooocus API ({current_scheme})"}
                        break
                    models = _filter_models(
                        [m for m in data.get("model_filenames", []) if isinstance(m, str)])
                    return {
                        "service": service,
                        "url": base_url,
                        "models": models,
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
                elif service == "sillytavern":
                    # Open instances serve the app HTML with <title>SillyTavern
                    # </title>; a secured one answers a login page (different
                    # title) and is correctly recorded as not-working. models
                    # stays empty ON PURPOSE — a SillyTavern is a front-end for
                    # someone else's (often paid) backend, so it must never gain
                    # a model list that dyva could match and route to.
                    raw = await resp.read()
                    await resp.release()
                    html = raw.decode("utf-8", "replace")
                    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
                    title = (tm.group(1).strip() if tm else "")
                    if "sillytavern" not in title.lower():
                        last_error = {"error": f"not an open SillyTavern "
                                      f"(title {title[:40]!r}) ({current_scheme})"}
                        break
                    return {
                        "service": service,
                        "url": base_url,
                        "models": [],
                        "title": title,
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
                elif service == "gradio":
                    # Liveness = a parseable Gradio /config manifest. Gradio
                    # embeds raw control chars in user strings, so read the text
                    # and json.loads(strict=False) rather than resp.json().
                    raw = await resp.read()
                    await resp.release()
                    try:
                        cfg = json.loads(raw.decode("utf-8", "replace"), strict=False)
                    except Exception:
                        last_error = {"error": f"bad /config json ({current_scheme})"}
                        break
                    if not isinstance(cfg, dict) or not ("components" in cfg or "title" in cfg):
                        last_error = {"error": f"not a gradio /config ({current_scheme})"}
                        break
                    return {
                        "service": service,
                        "url": base_url,
                        "models": [],
                        "gradio": _gradio_summary(cfg),
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
                elif service == "ollama":
                    data = await resp.json()
                    await resp.release()
                    _save_check_snapshot(host, port, data)
                    # Pass the RAW /api/tags entries (not just names) so each model's
                    # `capabilities` is captured as `caps` in the Form A object.
                    models = _filter_models(data.get("models", []))
                    model = next(iter(models), None)
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

                    # sglang doesn't respond to /api/version but otherwise it's very ollama-like.
                    _service = 'sglang'
                    try:
                        ver_resp = await asyncio.wait_for(
                            session.get(f"{base_url}api/version", allow_redirects=False), timeout=timeout
                        )
                        if ver_resp.status == 200:
                            # 200 on /api/version = ollama (sglang doesn't answer it).
                            _service = 'ollama'
                            # Parse content-type-agnostically: many ollama builds / fronting
                            # proxies serve /api/version as text/plain, which aiohttp's strict
                            # resp.json() rejects with ContentTypeError — the same reason the
                            # gradio path reads text + json.loads. That exception was being
                            # swallowed here, leaving ~90% of ollama hosts with no version.
                            raw = await ver_resp.read()
                            try:
                                vd = json.loads(raw.decode("utf-8", "replace"), strict=False)
                                version = vd.get("version") if isinstance(vd, dict) else None
                            except Exception as e:
                                log.debug(f"/api/version unparseable {host}:{port}: {type(e).__name__}: {e}: {raw[:80]!r}")
                        await ver_resp.release()
                    except Exception as e:
                        log.debug(f"/api/version failed {host}:{port}: {type(e).__name__}: {e}")

                    result = {
                        "service": _service,
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
                    _save_check_snapshot(host, port, data)
                    items = data.get("data", []) if isinstance(data, dict) else []
                    models = []
                    owners = set()
                    seen = set()
                    for m in items:
                        if not isinstance(m, dict):
                            continue
                        name = m.get("id", "")
                        ob = m.get("owned_by")
                        if isinstance(ob, str) and ob.strip():
                            owners.add(ob.strip())
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
                    # `vllm` is a catch-all for uvicorn-fronted OpenAI servers; the only
                    # thing that says WHICH harness a host actually is, is the model
                    # objects' `owned_by` (vllm, MLC-LLM, ds4.c, ... or anything). Adopt
                    # it verbatim AS the service so any per-harness difference is
                    # actionable later — a check-time relabel like ollama -> sglang.
                    # Catch-all only; explicitly-fetched services keep their identity.
                    if service == "vllm" and len(owners) == 1:
                        result["service"] = next(iter(owners))
                    if version:
                        result["version"] = version
                    return result
                elif service == "comfyui":
                    data = await resp.json()   # /models/checkpoints (the check_path)
                    await resp.release()
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
                        # /models category index unavailable (older/locked ComfyUI).
                        # Don't settle for checkpoints/ alone — most gen models live in
                        # diffusion_models/ or unet/ (flux, qwen-image, z-image, wan), so
                        # a checkpoints-only read reported "0 models" for ~60% of hosts.
                        # Enumerate those folders directly; category-prefixed like the tree.
                        found = ["checkpoints/" + str(m).replace("\\", "/")
                                 for m in (data if isinstance(data, list) else [])
                                 if isinstance(m, str) and m.lower().endswith(MODEL_EXTS)]
                        found += await _comfyui_folders_direct(
                            session, base_url, ("diffusion_models", "unet"), timeout=timeout)
                        models = _filter_models(found)
                        result = {
                            "service": service,
                            "url": base_url,
                            "models": models,
                            "model_tree": False,
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
                    _save_check_snapshot(host, port, data)
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
                elif service == "lmstudio":
                    data = await resp.json()
                    await resp.release()
                    _save_check_snapshot(host, port, data)
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
                    return {
                        "service": service,
                        "url": base_url,
                        "models": models,
                        "checked": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    data = await resp.json()
                    await resp.release()
                    _save_check_snapshot(host, port, data)
                    if isinstance(data, dict) and isinstance(data.get("data"), list):
                        # OpenAI /v1/models shape: {"data":[{"id":...}, ...]}. The ds4
                        # backend and any future OpenAI-dialect service land here
                        # (data[].id), so a real model list isn't read as "0 models".
                        seen = set()
                        names = []
                        for m in data["data"]:
                            if isinstance(m, dict) and m.get("id") and m["id"] not in seen:
                                seen.add(m["id"])
                                names.append(m["id"])
                        models = _filter_models(names)
                    else:
                        models = _filter_models(data if isinstance(data, list) else [])
                    if not models:
                        last_error = {"error": "no real models"}
                        break
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
        if start_scheme == "http":
            # Remember http's verdict, but DON'T stop here just because http answered: a
            # TLS host behind a proxy replies to a plaintext GET on :443 with "HTTP 400"
            # (plain HTTP sent to an HTTPS port) while the service actually lives on https —
            # so still fall through and try it.
            http_error = last_error

    # Both schemes failed (a success returns inline). Prefer the http error: https is only
    # a fallback, and its WRONG_VERSION/400 masks the real reason (http timed out, or the
    # host answered http with an error).
    final = http_error if http_error is not None else last_error
    if final is not None:
        final['lapse'] = time.time() - start_time
    return final


def _tag(value):
    """Short filesystem-safe label component (md5 prefix). Used for FID and
    server values whose raw text can contain spaces/slashes/etc."""
    if not value:
        return "any"
    return hashlib.md5(str(value).encode()).hexdigest()[:8]


def _fofa_path(label, run_ts, svc="any"):
    return os.path.join("/tmp/graflex", run_ts, "fofa", f"{svc}-{label}.txt")


def _shodan_path(label, run_ts, svc="any"):
    return os.path.join("/tmp/graflex", run_ts, "shodan", f"{svc}-{label}.txt")


def _fetch_web(dry, service, combined, country=None, port=None, server=None, run_ts=None, curlify=False, label="", pname="any"):
    import base64
    import re
    import requests
    import curlify as curlify_mod

    qb64 = base64.b64encode(combined.encode()).decode()
    url = f"{FOFA_WEB}?qbase64={qb64}{FOFA_FILTER}"

    cookie_header = FOFA_COOKIE

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

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
            # Cookie-expired = FOFA served the logged-OUT page, which renders a
            # "Log in" button (class "el-button login-button"). Verified against 10,945
            # captured FOFA pages: `login-button` appears in every logged-out response
            # (444, all zero-result) and NEVER in a page that returned results (5,887) or
            # a valid empty query (4,614). (NOT "logout" — that's the sign-out nav link
            # present on EVERY logged-in page.) Hard stop; nothing works until the cookie
            # is refreshed. Exit code 3.
            if "login-button" in body:
                log.error(f"FOFA cookie expired (logged out) — refresh FOFA_COOKIE in .env, "
                          f"then resume with --id {run_ts}")
                raise SystemExit(3)
            if "daily usage limit" in body:
                out_path = _fofa_path(label, run_ts, pname)
                if os.path.exists(out_path):
                    os.remove(out_path)
                log.error(f"daily usage limit hit, resume by using --id {run_ts}")
                raise SystemExit(2)
            if "access is temporarily denied" in body:
                log.error("FOFA access denied — IP flagged as a web crawler. Try again later or use a different IP/VPN.")
                raise SystemExit(2)
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
        except requests.exceptions.Timeout as e:
            # Read/connect timeouts are transient network conditions — retry
            # with backoff instead of silently dropping the query.
            log.warning(f"timeout ({type(e).__name__}), retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(int(backoff * 1.2), MAX_BACKOFF)
            continue
        except Exception as e:
            log.error(f"FOFA web request failed: {e}")
            return None

    tmp_dir = os.path.dirname(_fofa_path(label, run_ts, pname))
    os.makedirs(tmp_dir, exist_ok=True)
    out_path = _fofa_path(label, run_ts, pname)
    with open(out_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(resp.text)

    hosts = _parse_fofa_html(out_path, service)
    if not hosts:
        page = getattr(resp, "text", "")
        # FOFA's legit-empty marker is "No data for past <span>" — <span> varies with
        # the filter_type (year/month/week/day), so match the period generically.
        if not re.search(r"no data for past \w+", page.lower()):
            log.warning(f"! no hosts parsed but 'no data for past <period>' not found in page (code={getattr(resp, 'status_code', '?')}, {len(page)}b, {out_path})")

    return hosts


_last_result_file = ""


def _parse_fofa_html(html_path, service):
    import re

    global _last_result_file
    _last_result_file = html_path

    with open(html_path, encoding="utf-8", errors="replace") as f:
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

    return hosts


def _parse_shodan_html(html_path, service):
    import re

    global _last_result_file
    _last_result_file = html_path

    with open(html_path, encoding="utf-8", errors="replace") as f:
        content = f.read()

    # A results page carries result rows; an `alert-error` block is not emitted over
    # the wire unless an ACTUAL error occurred (invalid/expired key -> logged out,
    # rate limit, changed markup, ...). We key on that container rather than a "Log
    # in"/account string, which is unreliable — it can be present in the HTML and
    # merely hidden by CSS. Stop HARD with exit code 3 (the same code graflex.sh
    # catches for the FOFA logged-out stop) and point the operator at the captured
    # page. When the known logged-out message is present we name it; otherwise it's
    # a new/unknown failure mode worth inspecting. (If `alert-error` ever turns up as
    # an inert template fragment on a real results page, we'll find out and refine.)
    if "alert-error" in content:
        if "create a Shodan account" in content:
            why = "logged out — invalid or expired Shodan key/session"
        else:
            why = "Shodan returned an error page (rate limit? changed markup?)"
        log.error(f"Shodan: {why}. No results parsed — inspect the captured page: {html_path}")
        raise SystemExit(3)

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

    return hosts


def _rehydrate_censys_pool(pool):
    """Rehydrate Censys' turbo-stream-style reference pool into a plain object.

    The platform.censys.io search response is a single FLAT array that acts as a
    dedup pool. Index 0 is the root. An object is encoded as {"_<keyIdx>": <valIdx>,
    ...}: the key NAME lives at pool[keyIdx] and the value at pool[valIdx]. A bare int
    anywhere else is likewise an index into the pool; a negative index is a
    turbo-stream sentinel (undefined / a hole) that we resolve to None. Shared refs
    mean the graph can revisit a node, so we carry a `seen` set to guard cycles.

    This lets us extract by STRUCTURE (dereference, then walk the known path) instead
    of scraping scalars by regex/adjacency — the dedup interleaving makes the raw
    array unaddressable, but once rehydrated it is an ordinary nested object."""
    n = len(pool)

    def rez(idx, seen):
        if not isinstance(idx, int) or isinstance(idx, bool):
            return idx
        if idx < 0 or idx >= n:
            return None  # sentinel / out of range
        if idx in seen:
            return None  # cycle guard
        seen = seen | {idx}
        v = pool[idx]
        if isinstance(v, dict):
            out = {}
            for k, vi in v.items():
                if isinstance(k, str) and k[:1] == "_" and k[1:].lstrip("-").isdigit():
                    key = rez(int(k[1:]), seen)
                    out[key if isinstance(key, str) else str(key)] = rez(vi, seen)
                else:
                    out[k] = rez(vi, seen)
            return out
        if isinstance(v, list):
            return [rez(e, seen) for e in v]
        return v

    return rez(0, set())


def _parse_censys_json(path, service):
    """Extract host:port pairs from a platform.censys.io search response.

    We rehydrate the reference pool (see _rehydrate_censys_pool) and then walk the
    exact structural path a search result takes:

        <route>.data.results.hits[*].web.web.{hostname, port}

    The route key mirrors the request's `_routes` param, so we don't hardcode it —
    we take the single top-level entry. Everything past that is a fixed path, so
    there is no regex and no adjacency guessing.

    This schema is Censys' and can change WITHOUT NOTICE, so we throw a fit rather
    than return junk: if we can't reach `results.hits`, or hits are present but none
    yield a usable web.web.hostname:port, we stop hard with exit code 3 (the same
    "stop and inspect the captured file" signal graflex.sh catches for the FOFA /
    Shodan credential stops) and point the operator at the file. A genuinely empty
    result set (hits == []) is a normal no-match, not a schema break, so it only
    warns."""
    global _last_result_file
    _last_result_file = path

    with open(path, encoding="utf-8", errors="replace") as f:
        try:
            pool = json.load(f)
        except ValueError as e:
            log.error(f"Censys: response at {path} is not JSON ({e}) — inspect the captured file.")
            raise SystemExit(3)

    if not isinstance(pool, list):
        log.error(
            f"Censys: expected a turbo-stream array at {path}, got "
            f"{type(pool).__name__} — schema changed. Inspect the captured file."
        )
        raise SystemExit(3)

    root = _rehydrate_censys_pool(pool)

    def dig(obj, *keys):
        for k in keys:
            if not isinstance(obj, dict) or k not in obj:
                return None
            obj = obj[k]
        return obj

    route = next(iter(root.values())) if isinstance(root, dict) and len(root) == 1 else None

    # Censys returns errors in the SAME envelope with an `error` key instead of
    # `data` — e.g. `{"error": ["ErrorResponse", {"code": 100001}, 429, "Internal
    # Server Error"]}`. This is NOT a schema change; it's an API error (a 429 here
    # means rate-limited / out of the monthly query credits). Name it as such and
    # stop, rather than mislabeling it a parser failure.
    if isinstance(route, dict) and "error" in route and "data" not in route:
        flat = json.dumps(route["error"], ensure_ascii=False)
        rate = "429" in flat or "rate" in flat.lower() or "credit" in flat.lower() or "100001" in flat
        why = ("rate-limited / out of monthly query credits (HTTP 429)" if rate
               else "an API error")
        log.error(f"Censys returned {why}, not results: {flat}. Captured: {path}")
        raise SystemExit(3)

    hits = dig(route, "data", "results", "hits")

    if not isinstance(hits, list):
        log.error(
            f"Censys: could not reach data.results.hits in {path} — the response "
            f"schema changed (Censys ships this without notice). Inspect the file."
        )
        raise SystemExit(3)

    # A hit carries results from up to two datasets and one is null depending on
    # which the query hit: the WEB dataset (web.web.{hostname,port} — one endpoint)
    # and the HOST dataset (host.host.ip + host.host.services[] — MANY ports on the
    # box: SSH, MySQL, nginx, ...). For a host hit we must NOT emit every port; we
    # emit only the service(s) whose software Censys fingerprinted as THIS service
    # (vendor/product == the service name, e.g. "ollama"), so port 22/3306/etc. on
    # a matched host never leak into the pool as fake <service> endpoints.
    seen = set()
    hosts = []
    svc_l = str(service).lower()

    def emit(host, port):
        if not isinstance(host, str) or not host:
            return
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            return
        key = f"{host}:{port}"
        if key in seen:
            return
        seen.add(key)
        hosts.append({"service": service, "host": key, "site": "censys"})

    def is_svc_software(sw):
        return isinstance(sw, dict) and svc_l in (
            str(sw.get("vendor", "")).lower(), str(sw.get("product", "")).lower())

    for h in hits:
        if not isinstance(h, dict):
            continue
        ww = dig(h, "web", "web")
        if isinstance(ww, dict):
            emit(ww.get("hostname"), ww.get("port"))
        hh = dig(h, "host", "host")
        if isinstance(hh, dict):
            ip = hh.get("ip")
            for s in (hh.get("services") or []):
                if isinstance(s, dict) and any(is_svc_software(sw) for sw in (s.get("software") or [])):
                    emit(ip, s.get("port"))

    if hits and not hosts:
        log.error(
            f"Censys: {len(hits)} hits present but none yielded a '{service}' endpoint "
            f"(web.web host:port, or a host.host service fingerprinted as '{service}') "
            f"in {path} — the per-hit schema changed or the query matched via a field "
            f"we don't map. Inspect the captured file."
        )
        raise SystemExit(3)

    if not hosts:
        log.warning(f"! NO RESULTS from {path}")
    return hosts


def _censys_path(label, run_ts, svc="any"):
    return os.path.join("/tmp/graflex", run_ts, "censys", f"{svc}-{label}.json")


def _hunter_path(label, run_ts, svc="any"):
    return os.path.join("/tmp/graflex", run_ts, "hunter", f"{svc}-{label}.json")


def _parse_hunter_json(path, service):
    """Extract ip:port host dicts from a hunter.how /api/search response.

    Shape is clean JSON: {"code":200,"data":{"list":[{"ip":..,"port":..,...}]}}.
    A `code` != 200 is an API ERROR envelope (invalid/expired cookie, rate limit),
    NOT results — we name it and stop with exit 3 rather than mislabel it a schema
    change. Returns the same {service, host, site} shape as the other parsers."""
    global _last_result_file
    _last_result_file = path

    with open(path, encoding="utf-8", errors="replace") as f:
        try:
            resp = json.load(f)
        except ValueError as e:
            log.error(f"hunter.how: response at {path} is not JSON ({e}) — inspect the captured file.")
            raise SystemExit(3)

    if not isinstance(resp, dict):
        log.error(f"hunter.how: expected a JSON object at {path}, got {type(resp).__name__} — schema changed.")
        raise SystemExit(3)

    if resp.get("code") != 200:
        detail = {k: resp.get(k) for k in ("code", "message", "msg") if k in resp}
        log.error(
            f"hunter.how returned an API error (not results): "
            f"{json.dumps(detail, ensure_ascii=False)}. Read the message above "
            f"(a bad parameter, an invalid/expired HUNTER_COOKIE, or a rate limit). "
            f"Captured: {path}"
        )
        raise SystemExit(3)

    lst = (resp.get("data") or {}).get("list")
    if not isinstance(lst, list):
        log.error(f"hunter.how: could not reach data.list in {path} — schema changed. Inspect the file.")
        raise SystemExit(3)

    seen = set()
    hosts = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        ip = it.get("ip")
        try:
            port = int(str(it.get("port")))  # hunter.how returns port as a string
        except (TypeError, ValueError):
            continue
        if not isinstance(ip, str) or not ip or not (1 <= port <= 65535):
            continue
        key = f"{ip}:{port}"
        if key in seen:
            continue
        seen.add(key)
        hosts.append({"service": service, "host": key, "site": "hunter"})

    if lst and not hosts:
        log.error(
            f"hunter.how: {len(lst)} results present but none yielded ip:port in "
            f"{path} — the per-result schema changed. Inspect the captured file."
        )
        raise SystemExit(3)

    if not hosts:
        log.warning(f"! NO RESULTS from {path}")
    return hosts


def _fetch_hunter(dry, svc, query, page=1, run_ts=None, curlify=False, label="", pname="any"):
    """Replay one hunter.how /api/search POST with the operator's cookie and hand
    the response to _parse_hunter_json. The plaintext query is base64'd into the
    `search` field (hunter.how's encoding); paging is the `page` payload field.
    Stores the raw response FIRST, always, so a body we can't parse is never
    discarded. Returns the host list, [] for dry/curlify, None on a soft failure."""
    import requests
    import curlify as curlify_mod
    import base64
    from datetime import date, timedelta
    global _last_result_file

    q_b64 = base64.b64encode(query.encode()).decode().rstrip("=")
    today = date.today()
    payload = {
        "search": q_b64,
        "start_time": (today - timedelta(days=HUNTER_WINDOW_DAYS)).isoformat(),
        "end_time": today.isoformat(),
        "page": page,
        "page_size": HUNTER_PAGE_SIZE,
        "is_web": 0,
        "status_code": None,
        "syntax_condition": None,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://hunter.how",
        "Referer": "https://hunter.how/list",
        "Cookie": _hunter_cookie(),
    }
    if _hunter_auth():
        headers["Authorization"] = _hunter_auth()

    if curlify:
        req = requests.Request("POST", HUNTER_API, headers=headers, json=payload)
        log.info(curlify_mod.to_curl(req.prepare()))
        return []
    if dry:
        log.info(f"# query: {query}")
        log.info(f"# POST {HUNTER_API}  search(b64)={q_b64}  page={page} page_size={HUNTER_PAGE_SIZE}")
        return []

    try:
        resp = requests.post(HUNTER_API, headers=headers, json=payload, timeout=30)
    except Exception as e:
        log.error(f"hunter.how request failed: {e}")
        return None

    # Store the raw response FIRST, before any status/parse branching (see the
    # Censys fetch) — never discard a body we couldn't parse.
    tmp_dir = os.path.dirname(_hunter_path(label, run_ts, pname))
    os.makedirs(tmp_dir, exist_ok=True)
    out_path = _hunter_path(label, run_ts, pname)
    with open(out_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(resp.text)

    if resp.status_code in (429, 503):
        # A rate-limit is NOT this page's result — nothing to reprocess, and leaving it at
        # the canonical path makes a resumed run treat the page as fetched and skip it
        # (losing it). Drop it so the page is retried on resume.
        try:
            os.remove(out_path)
        except OSError:
            pass
        log.warning(f"hunter.how rate limited ({resp.status_code}) — not cached; page retries on resume")
        return None

    try:
        return _parse_hunter_json(out_path, svc)
    except SystemExit:
        # The API returned an error (or an unexpected shape). Make it ACTIONABLE:
        # show the EXACT request we sent — the decoded query verbatim, the full JSON
        # payload (page included), and the exact curl — so it can be reproduced /
        # diffed, then propagate the stop.
        log.error(f"  query (decoded, exact):  <<<{query}>>>")
        log.error(f"  POST {HUNTER_API}")
        log.error(f"  payload (exact):         {json.dumps(payload, ensure_ascii=False)}")
        log.error("  exact curl equivalent:\n" + curlify_mod.to_curl(
            requests.Request("POST", HUNTER_API, headers=headers, json=payload).prepare()))
        raise


# The Cloudflare "Just a moment..." interstitial (see error.txt) served when the
# cookie is stale or was minted on a different IP. These strings appear on that
# challenge page and never in a real JSON result.
_CENSYS_CHALLENGE_MARKERS = ("just a moment", "challenge-platform", "cf_chl",
                             "enable javascript and cookies")


def _fetch_censys(dry, svc, query, run_ts=None, curlify=False, label="", pname="any"):
    """Replay one platform.censys.io search.data request with the operator's cookie
    and hand the response to _parse_censys_json. Returns the host list, [] for
    dry/curlify, or None on a transient soft failure (rate limit / network).

    Censys fronts the endpoint with Cloudflare. A stale cookie — or one generated on
    a DIFFERENT egress IP than this machine — gets the CF 'Just a moment...' HTML
    interstitial instead of JSON; that is credential death, so we capture it and stop
    HARD with exit code 3 (the same signal graflex.sh catches for the FOFA/Shodan
    logged-out stops), telling the operator to regenerate CENSYS_COOKIE here."""
    import requests
    import curlify as curlify_mod
    from urllib.parse import urlencode
    global _last_result_file

    url = f"{CENSYS_API}?" + urlencode({"q": query, "_routes": "routes/api.search"})
    headers = {
        "User-Agent": _censys_ua(),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://platform.censys.io/search",
        "Cookie": _censys_cookie(),
    }

    if curlify:
        req = requests.Request("GET", url, headers=headers)
        log.info(curlify_mod.to_curl(req.prepare()))
        return []
    if dry:
        log.info(f"# query: {query}")
        log.info(f"# url: GET {url}")
        return []

    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except Exception as e:
        log.error(f"Censys request failed: {e}")
        return None

    # Store the raw response FIRST — ALWAYS, before any status/parse branching, so
    # a body we can't parse (a rate-limit envelope, an error, a changed schema) is
    # NEVER discarded. These queries cost scarce monthly credits; if it's JSON we
    # can reprocess it offline. (Same raw dir the other sites use.)
    tmp_dir = os.path.dirname(_censys_path(label, run_ts, pname))
    os.makedirs(tmp_dir, exist_ok=True)
    out_path = _censys_path(label, run_ts, pname)
    with open(out_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(resp.text)

    if resp.status_code in (429, 503):
        # A rate-limit is NOT this page's result — nothing to reprocess, and leaving it at
        # the canonical path makes a resumed run treat the page as fetched and skip it
        # (losing it). Drop it so the page is retried on resume.
        try:
            os.remove(out_path)
        except OSError:
            pass
        log.warning(f"Censys rate limited ({resp.status_code}) — not cached; page retries on resume")
        return None

    ctype = resp.headers.get("Content-Type", "").lower()
    head = resp.text[:4096].lower()
    if resp.status_code == 403 or "application/json" not in ctype \
            or any(m in head for m in _CENSYS_CHALLENGE_MARKERS):
        cap = out_path
        log.error(
            f"Censys: Cloudflare challenge / non-JSON response (HTTP {resp.status_code}, "
            f"Content-Type {resp.headers.get('Content-Type', '?')}). The cf_clearance "
            f"triple didn't line up — the cookie must be generated on THIS machine's "
            f"egress IP AND with the same browser whose UA is in CENSYS_UA. Captured: {cap}"
        )
        # The EXACT bytes we sent, verbatim between <<< >>> (no summarizing, no
        # truncation) — a failure here is almost always a single byte off (a trailing
        # space, a smart quote, a stray newline from a bad paste), so show the whole
        # string and let the operator diff it against the browser. The query is shown
        # decoded, exactly as the server receives it after URL-decoding.
        log.error(f"  query  (decoded, as the server sees it): <<<{query}>>>")
        log.error(f"  User-Agent sent:                         <<<{headers['User-Agent']}>>>")
        log.error(f"  Cookie sent:                             <<<{headers['Cookie']}>>>")
        # The exact curl equivalent of what we sent, so it can be replayed / diffed
        # against the working browser request byte-for-byte.
        log.error("  exact curl equivalent:\n" + curlify_mod.to_curl(requests.Request("GET", url, headers=headers).prepare()))
        raise SystemExit(3)

    return _parse_censys_json(out_path, svc)


def _fetch_shodan(dry, svc, combined, page=1, run_ts=None, curlify=False, label="", pname="any"):
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

    tmp_dir = os.path.dirname(_shodan_path(label, run_ts, pname))
    os.makedirs(tmp_dir, exist_ok=True)
    out_path = _shodan_path(label, run_ts, pname)
    with open(out_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(resp.text)

    return _parse_shodan_html(out_path, svc)


def _zoomeye_path(label, run_ts, svc="any"):
    return os.path.join("/tmp/graflex", run_ts, "zoomeye", f"{svc}-{label}.json")


def _zoomeye_token(cookie):
    """The JWT the API also wants as a Cube-Authorization header lives inside the
    cookie as `token=…`. Pull it out; '' if absent. String parsing, no regex."""
    for part in (cookie or "").split(";"):
        part = part.strip()
        if part.startswith("token="):
            return part[len("token="):].strip()
    return ""


def _parse_zoomeye(data, service):
    """Extract unified ip:port host dicts from a ZoomEye /api/search JSON page —
    the SAME shape fofa/shodan return: {service, host, site}. Empty list when the
    page carries no matches (the natural end of pagination)."""
    hosts, seen = [], set()
    for m in (data.get("matches") or []):
        ip = m.get("ip")
        port = (m.get("portinfo") or {}).get("port")
        if not ip or not port:
            continue
        # ZoomEye's `ip` is usually a string but can be a LIST (e.g. a Cloudflare-
        # fronted host resolving to several edge IPs) — make one host per IP, not
        # "['1.2.3.4', '5.6.7.8']:port".
        ips = ip if isinstance(ip, list) else [ip]
        cc = (((m.get("geoinfo") or {}).get("country") or {}).get("code"))
        for one in ips:
            if not one:
                continue
            key = f"{one}:{port}"
            if key in seen:
                continue
            seen.add(key)
            entry = {"service": service, "host": key, "site": "zoomeye"}
            if cc:
                entry["country"] = cc
            hosts.append(entry)
    return hosts


def _fetch_zoomeye(dry, svc, base_query, page=1, run_ts=None, curlify=False, label="", pname="any"):
    """One ZoomEye /api/search page as JSON, or None to STOP. The query is base64'd
    (their `q` encoding); auth is the session cookie + the JWT echoed as
    Cube-Authorization — both from ZOOMEYE_COOKIE, never hard-coded. No retry:
    an HTTP error or unparsable body means we stop and die, by design."""
    import requests
    import curlify as curlify_mod
    import base64
    global _last_result_file

    qb64 = base64.b64encode(base_query.encode()).decode()
    # pageSize 50 matches the web UI (the API defaults to 10) — ~5 pages to the
    # ~250 cap instead of ~25, so far fewer requests per country slice.
    params = {"q": qb64, "page": page, "pageSize": 50, "t": "v4+v6+web"}
    headers = {
        "User-Agent": _zoomeye_ua(),   # MUST match the browser that made the cookie (UA-bound clearance)
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.zoomeye.ai/searchResult?q={qb64}",
    }
    cookie = _zoomeye_cookie()
    if cookie:
        headers["Cookie"] = cookie
        tok = _zoomeye_token(cookie)
        if tok:
            headers["Cube-Authorization"] = tok

    if curlify:
        req = requests.Request("GET", ZOOMEYE_API, headers=headers, params=params)
        log.info(curlify_mod.to_curl(req.prepare()))
        return None
    if dry:
        log.info(f"# query: {base_query} (page {page})")
        log.info(f"# url: GET {ZOOMEYE_API}?q={qb64}&page={page}&t=v4+v6+web")
        return None

    # Surface the REAL error kind, not a bare status: ZoomEye puts the reason
    # (expired/invalid cookie, insufficient points, rate limit) in the response
    # body, and can even return HTTP 200 with an error `status`/`message` in JSON.
    try:
        resp = requests.get(ZOOMEYE_API, headers=headers, params=params, timeout=30)
    except requests.exceptions.RequestException as e:
        log.warning(f"zoomeye: stopping — request failed ({type(e).__name__}): {e}")
        return None
    if resp.status_code != 200:
        log.warning(f"zoomeye: stopping — HTTP {resp.status_code}: {(resp.text or '')[:300]}")
        # Dump the EXACT request we sent (resp.request = the real PreparedRequest, every
        # header/cookie/UA included) so it can be diffed against the browser's request to
        # see what the anti-bot wall is keying on. Contains the cookie — local debug only.
        try:
            import curlify as curlify_mod
            log.warning("zoomeye: the exact request we sent (compare with your browser) —\n"
                        + curlify_mod.to_curl(resp.request))
        except Exception as _e:
            log.debug(f"zoomeye: could not curlify the failed request: {_e}")
        return None
    try:
        data = resp.json()
    except ValueError:
        log.warning(f"zoomeye: stopping — unparsable body (HTTP {resp.status_code}): {(resp.text or '')[:200]}")
        return None
    st = data.get("status")
    if st is not None and st != 200:
        msg = data.get("message") or data.get("error") or data.get("msg") or ""
        log.warning(f"zoomeye: stopping — API status {st}: {msg}")
        if st == 402:
            # Out of credits — every remaining query would fail the same way, so
            # abort the whole run (not just this query) rather than burn requests.
            log.error("zoomeye: out of credits — aborting run")
            sys.exit(2)
        return None

    if run_ts:
        out_path = _zoomeye_path(label, run_ts, pname)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8", errors="replace") as f:
            json.dump(data, f)
        _last_result_file = out_path
    else:
        _last_result_file = "zoomeye"
    return data


def fetch(dry=False, curlify=False, service=None, query=None, name=None, servers=None, ports=None, countries=None, fids=None, sleep=SLEEP_DEFAULT, session=None, shuffle=False, site="fofa", check_batch_fn=None):
    global _RUN_TS
    hosts_file = _cache_file(name, "hosts")

    if not query and not service and not (name in NAMED_QUERIES and _named_query(name)[0]):
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

        pool = []
        seen = _service_host_set(service or name or "unknown")
        combos = [(c, p) for p in port_list for c in country_list]
        total_reqs = len(combos) * SHODAN_PAGES

        start = time.time()
        done_reqs = 0
        skipped_reqs = 0
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
                    out_path = _shodan_path(label, run_ts, service or name or "any")
                    if os.path.exists(out_path):
                        if not dry:
                            log.info(f"  skip page {page} (already fetched)")
                        done_reqs += 1
                        skipped_reqs += 1
                        continue

                hosts = _fetch_shodan(dry, svc, combined, page=page, run_ts=run_ts, curlify=curlify, label=label, pname=service or name or "any")
                done_reqs += 1
                if hosts is None:
                    continue

                fresh = 0
                fresh_hosts = []
                for h in hosts:
                    key = _entry_host(h)
                    if key not in seen:
                        pool.append(h)
                        seen.add(key)
                        fresh += 1
                        fresh_hosts.append(h)
                if hosts and not dry:
                    log.info(f"  {len(hosts)} hosts (+{fresh} new) from {_last_result_file}")

                if not dry:
                    pass  # hosts.json is regenerated by export(); fetch no longer writes it
                    elapsed = time.time() - start
                    worked = done_reqs - skipped_reqs
                    eta = elapsed * (total_reqs - done_reqs) / worked
                    log.info(f"  total: {len(seen)}    eta: {_fmt_duration(eta)}   lapsed: {_fmt_duration(elapsed)}")

                if check_batch_fn and fresh_hosts:
                    check_batch_fn(fresh_hosts)

                if not dry and not curlify and done_reqs < total_reqs:
                    time.sleep(sleep)

        hosts = pool
    elif site == "zoomeye":
        if not _zoomeye_cookie():
            log.error("ZOOMEYE_COOKIE must be set in .env for --site zoomeye. See README for how to copy it from your browser.")
            return []
        if servers or fids:
            log.warning("--servers/--fid are ignored with --site zoomeye")
        from datetime import datetime, timedelta
        run_ts = session or datetime.now().strftime("%Y%m%d%H%M%S")
        _RUN_TS = run_ts
        if session:
            log.info(f"resuming session {run_ts}")

        base_query = query
        if not base_query and service:
            base_query = SERVICE_CONFIG[service].get("zoomeye_query")
        if not base_query:
            log.error(f"no zoomeye query for '{service or name}' — pass --query (zoomeye syntax, e.g. app=\"ollama\")")
            return []

        # ZoomEye caps a single query at ~250 retrievable results (payload `max`),
        # so narrow by COUNTRY to slice past that: each country is its own ≤250
        # pass, plus one broad (unnarrowed) pass. Deliberately NO port filter —
        # ollama runs on many ports, so narrowing by port would miss most of them.
        # Country codes are ISO alpha-2 (e.g. GB, not UK).
        if isinstance(countries, str):
            country_list = [None] + [c.strip() for c in countries.split(",") if c.strip()]
        else:
            country_list = [None]
        if shuffle:
            random.shuffle(country_list)
        # Always restrict to hosts seen recently: after = today - ZOOMEYE_SEEN_DAYS,
        # before = 30 days out (a lazy future buffer so timezone skew can't exclude
        # "today" regardless of ZoomEye's clock).
        _today = datetime.now().date()
        _window = (f'after="{(_today - timedelta(days=ZOOMEYE_SEEN_DAYS)).isoformat()}"'
                   f' && before="{(_today + timedelta(days=30)).isoformat()}"')
        queries = []
        for c in country_list:
            parts = [base_query]
            if c:
                parts.append(f'country="{c}"')
            parts.append(_window)
            queries.append(" && ".join(parts))

        pool = []
        seen = _service_host_set(service or name or "unknown")
        svc = service or name or "unknown"
        start = time.time()
        for qi, combined in enumerate(queries):
            page = 1
            got = 0
            while page <= ZOOMEYE_MAX_PAGES:    # ZoomEye only serves pages 1-5; p6+ errors
                label = f"{_tag(combined)}-p{page}"
                cached = _zoomeye_path(label, run_ts, svc)
                # Resume (-i): a page whose raw JSON is already cached this session was
                # fetched on a prior run. REPROCESS it from disk — never re-fetch (burns
                # ZoomEye points) and never blindly skip: the page may have been saved but
                # its hosts not fully pooled/checked if the prior run was interrupted, so
                # we still run it through the rest of the pipeline (parse -> pool -> check).
                # Skipping fetch is NOT skipping processing. (Same rule as Censys/Hunter.)
                was_fetched = True
                if session and not dry and not curlify and os.path.exists(cached):
                    log.info(f"[{qi+1}/{len(queries)}] {combined} p{page}: cached, reprocessing")
                    try:
                        data = _load_json(cached, silent=True)
                        was_fetched = False
                    except Exception as e:
                        log.warning(f"  cached page unreadable ({e}); re-fetching: {cached}")
                        data = _fetch_zoomeye(dry, svc, combined, page=page, run_ts=run_ts,
                                              curlify=curlify, label=label, pname=svc)
                else:
                    data = _fetch_zoomeye(dry, svc, combined, page=page, run_ts=run_ts,
                                          curlify=curlify, label=label, pname=svc)
                if curlify or dry or data is None:
                    break                       # dry / curlify / http error / unparsable -> stop
                t_fetched = time.time()         # the check that follows counts toward --sleep pacing
                page_hosts = _parse_zoomeye(data, svc)
                if not page_hosts:
                    break                       # no more results (hit the cap) -> stop
                fresh, fresh_hosts = 0, []
                for h in page_hosts:
                    key = _entry_host(h)
                    if key not in seen:
                        pool.append(h)
                        seen.add(key)
                        fresh += 1
                        fresh_hosts.append(h)
                got += len(page_hosts)
                if not dry:
                    pass  # hosts.json is regenerated by export(); fetch no longer writes it
                    total = data.get("total")
                    elapsed = time.time() - start
                    eta = elapsed * (len(queries) - (qi + 1)) / (qi + 1)
                    log.info(f"[{qi+1}/{len(queries)}] {combined} p{page}: "
                             f"{len(page_hosts)} hosts (+{fresh} new), {got} this query"
                             + (f" of {total} available" if total else ""))
                    log.info(f"  pool: {len(seen)}   eta: {_fmt_duration(eta)}   lapsed: {_fmt_duration(elapsed)}")
                # Fresh fetch: check the NEW hosts. Reprocess: the pool already holds this
                # page's hosts (so fresh is empty), hand the WHOLE page to the check — it
                # skips already-checked hosts and probes only the ones left unchecked.
                _to_check = fresh_hosts if was_fetched else page_hosts
                if check_batch_fn and _to_check:
                    check_batch_fn(_to_check)
                page += 1
                if page > ZOOMEYE_MAX_PAGES:    # ZoomEye only serves pages 1-5; p6+ errors
                    break
                if not curlify and was_fetched:
                    # Pace only REAL fetches (a reprocess hits no network). The check above
                    # already spent wall-clock time; count it toward --sleep pacing and
                    # only sleep the remainder (>=0).
                    remaining = sleep - (time.time() - t_fetched)
                    if remaining > 0:
                        time.sleep(remaining)
        hosts = pool
    elif site == "censys":
        if not _censys_cookie():
            log.error("CENSYS_COOKIE must be set in .env for --site censys — generate it on THIS machine's egress IP (load platform.censys.io in a browser here and copy the request Cookie). See README.")
            return []
        if servers or fids:
            log.warning("--servers/--fid are ignored with --site censys")
        from datetime import datetime
        run_ts = session or datetime.now().strftime("%Y%m%d%H%M%S")
        _RUN_TS = run_ts
        if session:
            log.info(f"resuming session {run_ts}")

        base = query or (SERVICE_CONFIG.get(service, {}).get("censys_query") if service else None)
        if not base:
            log.error(f"no censys query for '{service or name}' — pass --query (Censys syntax, e.g. '\"ollama is running\"')")
            return []
        # A service may carry several censys queries (e.g. broad + narrowed); the
        # cookie isn't re-gated after the first success, so we iterate them all in
        # one session, deduping into the pool. Page size is generous — no paging.
        base_queries = list(base) if isinstance(base, list) else [base]

        # Censys is CREDIT-METERED and the quota is tiny (as few as ~5 queries a
        # month), so every query must be deliberate: the count is EXACTLY what was
        # asked, with no automatic extra passes. Countries given (CSV via
        # `-c/--countries`) -> one query per country; none -> a single broad query.
        # Country names are filled verbatim into the Censys `host.location.country`
        # field, which wants FULL NAMES ("United States", "China"), NOT ISO codes.
        if isinstance(countries, str) and countries.strip():
            country_list = [c.strip() for c in countries.split(",") if c.strip()]
        else:
            country_list = [None]
        if shuffle:
            random.shuffle(country_list)

        # Query cycles innermost (all base queries per country), same as FOFA.
        combos = [(bq, c) for c in country_list for bq in base_queries]
        queries = []
        for bq, c in combos:
            queries.append(bq if not c else f'({bq}) and host.location.country = "{c}"')
        log.info(f"Censys: this run will spend {len(queries)} "
                 f"quer{'y' if len(queries) == 1 else 'ies'} (credit-metered — spend wisely).")

        pool = []
        seen = _service_host_set(service or name or "unknown")
        svc = service or name or "unknown"
        start = time.time()
        rate_backoff = 0
        for qi, combined in enumerate(queries):
            label = _tag(combined)
            cached = _censys_path(label, run_ts, svc)
            if not dry and not curlify and os.path.exists(cached):
                # We already have this query's raw response stored. REPROCESS it from
                # disk — never re-fetch (that spends a scarce monthly credit) and never
                # blindly skip: the parser has been buggy, so a file that "errored"
                # before may parse fine now. A parse failure on ONE stored file must
                # not abort the batch (else one bad file blocks recovering the rest) —
                # state the failure with its path and move on.
                log.info(f"[{qi+1}/{len(queries)}] reprocessing stored response: {cached}")
                try:
                    hosts = _parse_censys_json(cached, svc)
                except SystemExit as e:
                    log.error(f"  failed to parse (exit {e.code}); left in place, continuing: {cached}")
                    continue
            else:
                hosts = _fetch_censys(dry, svc, combined, run_ts=run_ts, curlify=curlify, label=label, pname=svc)
            if curlify or dry:
                continue
            if hosts is None:
                # Rate-limited / soft failure: back off (escalating) before the next
                # request. The plain `continue` skips the inter-query sleep below, so
                # without this it re-hits the limit with zero delay and only gains one
                # page per run. Each consecutive 429 waits longer, capped at MAX_BACKOFF.
                rate_backoff = min(max(rate_backoff * 2, sleep * 2), MAX_BACKOFF)
                log.info(f"  rate limited — backing off {rate_backoff:.0f}s before the next query")
                time.sleep(rate_backoff)
                continue
            rate_backoff = 0   # a success resets the backoff
            fresh, fresh_hosts = 0, []
            for h in hosts:
                key = _entry_host(h)
                if key not in seen:
                    pool.append(h)
                    seen.add(key)
                    fresh += 1
                    fresh_hosts.append(h)
            if not dry:
                pass  # hosts.json is regenerated by export(); fetch no longer writes it
                elapsed = time.time() - start
                log.info(f"[{qi+1}/{len(queries)}] {len(hosts)} hosts (+{fresh} new) from {combined!r}")
                log.info(f"  pool: {len(seen)}   lapsed: {_fmt_duration(elapsed)}")
            if check_batch_fn and fresh_hosts:
                check_batch_fn(fresh_hosts)
            if not dry and not curlify and qi + 1 < len(queries):
                time.sleep(sleep)
        hosts = pool
    elif site == "hunter":
        if not _hunter_cookie():
            log.error("HUNTER_COOKIE must be set in .env for --site hunter — copy the Cookie header from a logged-in hunter.how request. See README.")
            return []
        if servers or fids:
            log.warning("--servers/--fid are ignored with --site hunter")
        from datetime import datetime
        run_ts = session or datetime.now().strftime("%Y%m%d%H%M%S")
        _RUN_TS = run_ts
        if session:
            log.info(f"resuming session {run_ts}")

        base = query or (SERVICE_CONFIG.get(service, {}).get("hunter_query") if service else None)
        if not base:
            log.error(f"no hunter query for '{service or name}' — pass --query (hunter.how syntax, e.g. 'product.name==\"Ollama Server\"')")
            return []
        # hunter.how's coverage is PAGINATION, not country fan-out: one query, walked
        # page by page. (Narrow by country if you want with an explicit --query, e.g.
        # ... and ip.country=="United States".) A service may still carry several base
        # queries; each is paginated in turn.
        base_queries = list(base) if isinstance(base, list) else [base]
        log.info(f"hunter.how: up to {HUNTER_MAX_PAGES} pages/query x {len(base_queries)} "
                 f"quer{'y' if len(base_queries) == 1 else 'ies'} (stops at the first empty page).")

        pool = []
        seen = _service_host_set(service or name or "unknown")
        svc = service or name or "unknown"
        start = time.time()
        for combined in base_queries:
            for page in range(1, HUNTER_MAX_PAGES + 1):
                label = f"{_tag(combined)}-p{page}"
                cached = _hunter_path(label, run_ts, svc)
                hosts = None
                reused = False
                if not dry and not curlify and os.path.exists(cached):
                    # Reprocess a VALID stored page from disk instead of re-fetching
                    # (spends a metered query). But a stored ERROR/unparseable page has
                    # no data to preserve, so fall through and RE-FETCH it rather than
                    # skip — otherwise a page that errored once (e.g. a since-fixed
                    # request bug) is lost forever.
                    log.info(f"[p{page}] reprocessing stored response: {cached}")
                    try:
                        hosts = _parse_hunter_json(cached, svc)
                        reused = True
                    except SystemExit as e:
                        log.warning(f"  stored page failed to parse (exit {e.code}); re-fetching: {cached}")
                if not reused:
                    hosts = _fetch_hunter(dry, svc, combined, page=page, run_ts=run_ts, curlify=curlify, label=label, pname=svc)
                if curlify or dry:
                    break                       # preview one page's query/payload only
                if hosts is None:
                    break                       # http error / rate limit -> stop paginating
                if not hosts:
                    break                       # empty page -> end of results
                fresh, fresh_hosts = 0, []
                for h in hosts:
                    key = _entry_host(h)
                    if key not in seen:
                        pool.append(h)
                        seen.add(key)
                        fresh += 1
                        fresh_hosts.append(h)
                if not dry:
                    pass  # hosts.json is regenerated by export(); fetch no longer writes it
                    elapsed = time.time() - start
                    log.info(f"[p{page}] {len(hosts)} hosts (+{fresh} new) from {combined!r}")
                    log.info(f"  pool: {len(seen)}   lapsed: {_fmt_duration(elapsed)}")
                if check_batch_fn and fresh_hosts:
                    check_batch_fn(fresh_hosts)
                if not curlify:
                    time.sleep(sleep)
        hosts = pool
    else:
        if not FOFA_COOKIE:
            log.error("FOFA_COOKIE must be set in .env for the web method. See README for instructions on how to obtain it from your browser.")
            return []
        from datetime import datetime
        run_ts = session or datetime.now().strftime("%Y%m%d%H%M%S")
        _RUN_TS = run_ts
        if session:
            log.info(f"resuming session {run_ts}")
        # Seed the RNG with run_ts so --random produces the SAME shuffle order
        # every time this session is resumed via -i <run_ts>. Without this the
        # grid is scrambled differently each run, so the already-fetched pages
        # get scattered across the whole grid and resume has to re-scan all of
        # them instead of skipping the exact already-done prefix.
        random.seed(run_ts)

        _nq = (SERVICE_CONFIG.get(service) or {}) if service else \
              (_named_query(name)[1] if name in NAMED_QUERIES else {})
        if not isinstance(countries, str) and _nq.get("countries"):
            countries = _nq["countries"]
        if not isinstance(ports, str) and _nq.get("ports"):
            ports = _nq["ports"]
        if not fids and _nq.get("fids"):
            fids = ",".join(_nq["fids"])
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

        pool = []
        index = {}
        seen = _service_host_set(service or name or "unknown")
        # Resolve the base FOFA query/queries. A service may define several
        # (e.g. ollama: app="ollama" then body="ollama is running"); each is
        # cycled across the full country/port/server combinatorics, just like
        # country/port cycling — several independent passes over the same space.
        if query:
            base_queries = [query]
        elif service:
            q = SERVICE_CONFIG[service]["fofa_query"]
            base_queries = q if isinstance(q, list) else [q]
        else:
            nq = _named_query(name)[0]
            base_queries = nq if isinstance(nq, list) else [nq]

        # The query is one axis of the country/port/server cross product. It
        # cycles INNERMOST, so each (server,port,country) combo iterates all
        # base queries in a row (e.g. app="ollama" then body="..." for a fixed
        # country), then the next combo advances.
        fid_combos = [(bq, None, None, None, fid) for fid in fid_specs for bq in base_queries]
        combo_grid = [(bq, c, p, s, None)
                      for s in server_list for p in port_list for c in country_list
                      for bq in base_queries]
        if shuffle:
            random.shuffle(fid_combos)
            random.shuffle(combo_grid)

        combos = fid_combos + combo_grid

        start = time.time()
        skipped_combos = 0
        for i, (base_query, country, port, server, fid) in enumerate(combos):
            qparts = [base_query] if base_query else []
            if not qparts:
                continue
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
                log.info(f"[{i+1}/{len(combos)}] query={base_query} country={country} port={port} server={server} fid={fid}")

            label = f"{_tag(base_query)}-{country or 'any'}-{port or 'any'}-{_tag(server)}-{_tag(fid)}"
            if session:
                out_path = _fofa_path(label, run_ts, service or name or "any")
                if os.path.exists(out_path):
                    if not dry:
                        log.info(f"  skip ({out_path})")
                    skipped_combos += 1
                    continue

            svc = service or name or "unknown"
            hosts = _fetch_web(dry, svc, combined, country, port, server, run_ts, curlify=curlify, label=label, pname=service or name or "any")
            if hosts is None:
                continue

            fresh = 0
            fresh_hosts = []
            for h in hosts:
                if fid:
                    h["fid"] = fid
                key = _entry_host(h)
                if key not in seen:
                    pool.append(h)
                    seen.add(key)
                    index[key] = h
                    fresh += 1
                    fresh_hosts.append(h)
                elif fid and key in index and not index[key].get("fid"):
                    index[key]["fid"] = fid
            if hosts and not dry:
                log.info(f"  {len(hosts)} hosts (+{fresh} new) from {_last_result_file}")

            if not dry:
                pass  # hosts.json is regenerated by export(); fetch no longer writes it

                done = i + 1
                worked = done - skipped_combos
                elapsed = time.time() - start
                eta = elapsed * (len(combos) - done) / worked
                log.info(f"  total: {len(seen)}    eta: {_fmt_duration(eta)}   lapsed: {_fmt_duration(elapsed)}\n")

            if check_batch_fn and fresh_hosts:
                check_batch_fn(fresh_hosts)

            if i < len(combos) - 1 and not dry and not curlify:
                time.sleep(sleep)

        hosts = pool

    if dry:
        return

    # Fetch persists nothing itself: the raw pages are saved in the session (the source of
    # truth) and check reads the session to record hosts. The "+new" count is an expectation
    # signal — if a following check then finds nothing, the counts disagree and that's a bug.
    log.info(f"fetch: {len(hosts)} new host(s) this run (not yet checked)")


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


def _recorded_hosts(service):
    """Hosts that already have a check RESULT in the DB (status set) — the running
    record check-new skips against. A discovered-but-unchecked NULL host is NOT a
    record, so it still gets checked."""
    cur = _store().execute("SELECT host FROM host WHERE service=? AND status IS NOT NULL", (service,))
    return {h for (h,) in cur.fetchall()}


def _service_host_set(service):
    """Every host:port already in the DB for a service (any status) — fetch's dedup/seen
    set. A set of strings, so it costs a fraction of the memory of loading the whole
    hosts.json into Python objects (which is what fetch used to do, twice)."""
    cur = _store().execute("SELECT host FROM host WHERE service=?", (service,))
    return {h for (h,) in cur.fetchall()}


def _session_hosts(session, service, site=None, base="/tmp/graflex"):
    """Hosts fetched for `service` in `session`, reparsed from that session's raw fetch
    files (the survey's source of truth) with the existing per-site parsers — no network,
    no JSON transports. `site` (from -t) restricts to one source; otherwise every source
    in the session is reparsed. Dedup by host:port; a file that won't parse (an API error
    envelope, schema drift, an empty page) is skipped, never fatal."""
    import glob
    import logging
    import concurrent.futures
    parsers = {
        "fofa":    lambda p: _parse_fofa_html(p, service),
        "shodan":  lambda p: _parse_shodan_html(p, service),
        "censys":  lambda p: _parse_censys_json(p, service),
        "hunter":  lambda p: _parse_hunter_json(p, service),
        "zoomeye": lambda p: _parse_zoomeye(json.load(open(p, encoding="utf-8", errors="replace")), service),
    }
    jobs = []   # (path, parser)
    for st, parse in parsers.items():
        if site and st != site:
            continue
        d = os.path.join(base, session, st)
        if not os.path.isdir(d):
            continue
        for fn in sorted(glob.glob(os.path.join(d, f"{service}-*"))):
            jobs.append((fn, parse))
    if not jobs:
        return []

    def _parse_one(job):
        fn, parse = job
        try:
            return parse(fn) or []
        except (SystemExit, Exception):
            return []   # error envelope / schema drift / empty page — not fatal

    # These raw files can be hundreds of MB of fofa HTML; read them in parallel (I/O-bound,
    # the GIL is released during the read) instead of one-at-a-time, and announce it so a
    # long reparse doesn't look hung. Quiet the parsers' per-file "! NO RESULTS" flood.
    log.info(f"check-new: reparsing {len(jobs)} session file(s)"
             + (f" from {site}" if site else "") + " …")
    out = {}
    prev_level = log.level
    log.setLevel(logging.ERROR)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(jobs))) as ex:
            for parsed in ex.map(_parse_one, jobs):
                for h in parsed:
                    eh = _entry_host(h)
                    if eh:
                        out.setdefault(eh, h)
    finally:
        log.setLevel(prev_level)
    log.info(f"check-new: {len(out)} host(s) from {len(jobs)} session file(s)")
    return list(out.values())


async def _check_all(service, name=None, check_timeout=60, check_new=False, check_all=False, workers=10, session=None, site=None):
    from datetime import datetime, timezone

    global _RUN_TS
    if session:
        _RUN_TS = session
    elif not _RUN_TS:
        # A bare check(-all) run gets a stable session id too, so its success
        # snapshots and failure records land in one dir and it can be resumed.
        _RUN_TS = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        log.info(f"check session {_RUN_TS} — resume with -i {_RUN_TS}")

    if name is None:
        name = service

    # First run after the sqlite switch: seed the DB from the existing json files so
    # this sweep starts from today's known hosts (no-op once the DB has rows).
    _import_if_empty()

    hosts_file = _cache_file(name, "hosts")
    working_file = _cache_file(name, "working")
    notworking_file = _cache_file(name, "notworking")

    if check_new and session and service:
        # Session-scoped check-new: input is the session's fetched hosts (reparsed from
        # the raw fetch files — the survey's source of truth); the DB is the running record
        # we skip against. No JSON transports are read here. (Other check modes still read
        # the JSON pool — see the migration TODO; this is the first path moved onto the DB.)
        hosts = _session_hosts(session, service, site)
        if not hosts:
            log.warning(f"check-new: no {service} hosts fetched in session {session}"
                        + (f" via {site}" if site else ""))
            return
        recorded = _recorded_hosts(service)
        to_check = [h for h in hosts if _entry_host(h) not in recorded]
        # Shared downstream (pool-report line, _check_hosts call) expects these set in
        # both branches: `done` = session hosts already in the running record; and let
        # _check_hosts load the working set itself.
        done = {_entry_host(h) for h in hosts} & recorded
        existing_working = None
    else:
        # Candidates come from the DB (the running record), not the hosts.json transport.
        # check-all re-probes everything; plain check re-probes the unchecked (NULL) and
        # the previously-UNREACHABLE, skipping working + hard-errored; check-new without
        # -i (rare) re-probes only the never-recorded.
        all_rows = _hoststore.rows(_store(), service)
        if not all_rows:
            log.warning(f"check: no {service or '?'} hosts in the store - run fetch first")
            return
        hosts = [{"service": service, "host": h} for h, _p, _c in all_rows]
        if check_all:
            done = set()
        elif check_new:
            done = _recorded_hosts(service)
        else:
            done = {h for h, _p, _c in _hoststore.rows(_store(), service, "working")}
            done |= {h for h, p, _c in _hoststore.rows(_store(), service, "notworking")
                     if isinstance(p, dict) and p.get("result") == "error"}
        to_check = [h for h in hosts if _entry_host(h) not in done]
        existing_working = None
    if session:
        failed_set = _load_check_failed(session)
        if failed_set:
            log.info(f"check: {len(failed_set)} failed hosts recorded in session {session}")
        resumed = failed = 0
        kept = []
        snap = _check_snapshot_idents(session)
        for h in to_check:
            host_port = h["host"].split(":")
            hh = host_port[0]
            pp = int(host_port[1]) if len(host_port) > 1 else SERVICE_CONFIG[service]["port"]
            if service in SNAPSHOT_SERVICES and _host_ident(f"{hh}:{pp}") in snap:
                resumed += 1
                continue
            if f"{hh}:{pp}" in failed_set:   # already failed this session — don't re-probe
                failed += 1
                continue
            kept.append(h)
        to_check = kept
        if resumed or failed:
            log.info(f"check: skipping {resumed} hosts already snapshot"
                     + (f" and {failed} that already failed" if failed else "")
                     + f" in session {session}")
    if not to_check:
        log.info(f"check: all {len(hosts)} hosts already have model data")
        export(service)   # regenerate this service's json from the DB (working sorted, notworking, hosts)
        return

    # Report the FULL pool, not just the remainder — otherwise "64 to check" reads as
    # "that's all there is" when the pool was 968 and 904 were skipped as
    # already-tried-THIS-session (a resumed -i run) or already-done. Make the total and
    # the skip legible so a small "to check" on a resume isn't mistaken for a bug.
    _pool = len(hosts)
    _skipped = _pool - len(to_check)
    _why = []
    if done:
        _why.append(f"{len(done)} already recorded")
    if session and _skipped - len(done) > 0:
        _why.append(f"{_skipped - len(done)} already tried this session (resume -i {session})")
    log.info(f"check{'-all' if check_all else ''}: {len(to_check)} to check of {_pool} in pool"
             + (f" ({'; '.join(_why)})" if _why else ""))
    await _check_hosts(to_check, service, working_file, notworking_file, check_timeout, workers, existing_working)
    export(service)   # final regeneration of this service's json from the DB after the sweep


async def _check_hosts(hosts, service, working_file, notworking_file, check_timeout=60, workers=10, existing_working=None):
    """Run the worker-pool probe over an explicit list of host entries and
    record results to the working/notworking files. Shared by _check_all and
    the interleaved fetch-check pipeline (which drains a bounded batch of
    freshly-fetched hosts between page fetches)."""
    from datetime import datetime, timezone

    # The choke point for every DB write below: the fetch-check path reaches here without
    # going through _check_all, so run the one-time import HERE — before the first probe
    # write makes the DB non-empty and permanently pre-empts the migration.
    _import_if_empty()

    if existing_working is None:
        existing_working = _load_json(working_file)

    to_check = hosts
    sem = asyncio.Semaphore(workers)
    wlock = asyncio.Lock()
    completed = 0

    async def check_one(entry):
        nonlocal completed
        start = time.time()
        async with sem:
            # A probe that times out or errors RAISES (notably the session-level
            # ClientTimeout -> asyncio.TimeoutError). Catch it here and turn it into
            # an error result, or gather(return_exceptions=True) swallows it silently:
            # the host records as neither working nor notworking and the per-host
            # failure line never prints ("N checked, 0 working, 0 notworking").
            try:
                result, method = await _doorknock(session, entry, service,
                                                  check_timeout, existing_working)
            except asyncio.TimeoutError:
                result, method = {"error": "timeout"}, None
            except (aiohttp.ClientError, OSError) as e:
                result, method = {"error": (str(e) or type(e).__name__)[:200]}, None
            except Exception as e:
                result, method = {"error": f"{type(e).__name__}: {e}"[:200]}, None
            # Honeypot gate, INSIDE the check: a host that probes OK but whose whole
            # freshly-listed catalog is the phantom frozen set gets one benign
            # /wp-login.php GET (tiny subset). 200 => record as honeypot, not working.
            honeypot = (isinstance(result, dict) and "error" not in result
                        and _host_models(result) and _host_models(result) <= HONEYPOT_MODELS
                        and await _is_honeypot(session, entry["host"], check_timeout))

        key = _entry_host(entry)
        ok = False
        async with wlock:
            if honeypot:
                # No "host"/"service": host is the dict key (and in url); service is
                # implicit in the file name (<service>-notworking.json).
                nr = {"url": f"http://{entry['host']}",
                      "reason": "honeypot", "result": "honeypot",
                      "checked": datetime.now(timezone.utc).isoformat()}
                _store_record(service, entry["host"], "notworking", nr)
                log.warning(f"~ {entry['host']}: honeypot (phantom catalog + /wp-login.php 200)")
            elif isinstance(result, dict) and "error" not in result:
                result["checked"] = result.get("checked", datetime.now(timezone.utc).isoformat())
                # Identity stays the claimed host (stable survey key); how it was
                # actually reached lives in `method` (absent = the default way).
                result["host"] = entry["host"]
                if method:
                    result["method"] = method
                else:
                    result.pop("method", None)
                _store_record(service, entry["host"], "working", result)
                note = ""
                if service == "comfyui" and not result["models"]:
                    # "0 models" is ambiguous on its own: no index at all reads
                    # very differently from an index that listed nothing usable
                    note = (" (index listed no model files)" if result.get("model_tree")
                            else " (no /models index; /models/checkpoints was empty)")
                log.info(f"+ {entry['host']}: {len(result['models'])} models{note}")
                ok = True
            else:
                reason = result.get("error", str(result)) if isinstance(result, dict) else str(result)
                result_type = "error" if (reason.startswith("HTTP ") or reason.startswith("show HTTP ") or reason.startswith("bad JSON") or "no real" in reason or "empty show" in reason or "auth required" in reason) else "unreachable"
                nr = {"url": f"http://{entry['host']}", "reason": reason, "result": result_type, "checked": datetime.now(timezone.utc).isoformat()}
                _store_record(service, entry["host"], "notworking", nr)
                # host:port for the -i resume skip-list — `h`/`p` were never defined
                # in this scope, so this line used to NameError and get swallowed by
                # gather(return_exceptions=True), taking the print below down with it.
                _hp = entry["host"].split(":")
                _save_check_failure(_hp[0], (_hp[1] if len(_hp) > 1 else ""), reason)
                log.info(f"  {entry['host']}: {reason}")
        completed += 1
        now = time.time()
        if completed % STATS_EVERY == 0:
            elapsed = now - start
            eta = elapsed * (len(to_check) / completed) - elapsed
            log.info(f"Checked: {completed} | Runtime: {_fmt_duration(elapsed)} | Remaining: {len(to_check) - completed} | ETA: {_fmt_duration(eta)}")
        global _LAST_EXPORT
        if now - _LAST_EXPORT >= EXPORT_EVERY_SEC:
            _LAST_EXPORT = now        # claim the slot first so concurrent coroutines don't all flush
            # Run the mid-check flush OFF the event loop: for ollama it's a ~32MB read+write,
            # and doing it inline froze every in-flight probe long enough that fast hosts hit
            # their timeout, fell to the https knock, and got recorded with a bogus
            # WRONG_VERSION. In a thread (own read connection) the probes keep running.
            await asyncio.get_running_loop().run_in_executor(None, _export_offthread, service)
        return ok

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=INSECURE_SSL, force_close=True, limit=0), timeout=aiohttp.ClientTimeout(total=check_timeout + 5)) as client:
        session = client
        tasks = [check_one(entry) for entry in to_check]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = sum(1 for r in results if r is True)
        failed = sum(1 for r in results if r is False)
        log.info(f"check: {len(to_check)} checked, {success} working, {failed} notworking")


def check(service, name=None, check_timeout=60, check_new=False, check_all=False, workers=10, session=None, site=None):
    asyncio.run(_check_all(service, name, check_timeout, check_new, check_all, workers, session, site))


def check_batch(hosts, service, name=None, check_timeout=60, workers=10, session=None):
    """Check an explicit list of host entries — the fresh hosts from one fetch-check
    page (all of them; `workers` is the probe concurrency, not a cap). Hosts already
    recorded in working/notworking, and already-snapshotted hosts when resuming -i,
    are skipped so only never-tested hosts are probed."""
    if not hosts:
        return
    if name is None:
        name = service
    if not service and hosts:
        service = hosts[0].get("service", "")
    working_file = _cache_file(name, "working")
    notworking_file = _cache_file(name, "notworking")

    # Skip hosts that already have a RESULT in the DB (the running record) — the DB
    # replaced the working/notworking JSON transports as the skip interface, same as
    # check-new. Only never-recorded hosts get probed.
    done = _recorded_hosts(service)
    to_check = [h for h in hosts if _entry_host(h) not in done]
    if session:
        failed_set = _load_check_failed(session)
        kept = []
        snap = _check_snapshot_idents(session)
        for h in to_check:
            host_port = h["host"].split(":")
            hh = host_port[0]
            pp = int(host_port[1]) if len(host_port) > 1 else SERVICE_CONFIG[service]["port"]
            if service in SNAPSHOT_SERVICES and _host_ident(f"{hh}:{pp}") in snap:
                continue
            if f"{hh}:{pp}" in failed_set:
                continue
            kept.append(h)
        to_check = kept
    if not to_check:
        return

    asyncio.run(_check_hosts(to_check, service, working_file, notworking_file, check_timeout, workers))


# woahllama's phantom-responder frozen catalog (day50.dev/woahllama): the fakes serve
# ONLY a subset of this short list, "sizes constant to the byte." We gate on that
# signature so an ordinary host is never touched — a real server has models beyond
# this set — then confirm with ONE benign GET /wp-login.php (a standard page a real
# Ollama 404s). We deliberately do NOT probe /.git/config or /.env: dangling-secret
# scanning is itself invasive. A 200 on /wp-login.php = honeypot.
HONEYPOT_MODELS = {
    # woahllama's canonical frozen catalog (the five named in the writeup)
    "llama2:latest", "llama3:latest", "openchat:7b", "codellama:13b", "qwen2.5:1.5b",
    # live extras observed on confirmed honeypots (2026-09-10) — the catalog has
    # drifted; the HF-style "org/Repo" names are themselves a tell (real Ollama tags
    # are never org/repo). Extend as the catalog is re-mapped.
    "deepseek-r1:latest", "deepseek-ai/DeepSeek-R1", "mistralai/Mistral-Large-Instruct-2411",
}


def _host_models(entry):
    out = set()
    for m in (entry.get("models") or []):
        n = m if isinstance(m, str) else (m.get("name") if isinstance(m, dict) else None)
        if n:
            out.add(n)
    return out


async def _is_honeypot(session, host, timeout):
    """One benign GET /wp-login.php — a standard page a real Ollama 404s. 200 =
    honeypot. Called from the check phase ONLY for a host whose whole freshly-listed
    catalog is the phantom frozen set, so it's a tiny subset and never touches an
    ordinary host. We deliberately do NOT probe /.git/config or /.env (invasive)."""
    try:
        async with session.get(f"http://{host}/wp-login.php",
                               timeout=aiohttp.ClientTimeout(total=min(timeout, 10)),
                               allow_redirects=False) as r:
            return r.status == 200
    except Exception:
        return False


async def _check_working(service, name=None, check_timeout=60, workers=10, session=None,
                         only_zero=False):
    """Re-survey the currently-working hosts: refresh their model list (people
    keep downloading new models) and prune hosts that no longer respond.

    only_zero (check-zero): re-probe ONLY the hosts recorded with 0 models — to
    correct erroneous zeros after a discovery fix — while preserving every
    non-empty host untouched."""
    from datetime import datetime, timezone

    global _RUN_TS
    if session:
        _RUN_TS = session

    if name is None:
        name = service
    working_file = _cache_file(name, "working")
    notworking_file = _cache_file(name, "notworking")
    svc = service or name or "ollama"

    # Working pool from the DB (the running record), not working.json.
    working = [p for _h, p, _c in _hoststore.rows(_store(), svc, "working")]
    if not service:
        service = svc
    # Not filtered by service, for the same reason as _check_all: re-labelled
    # hosts (ollama -> sglang) must still be rescanned.
    if not working:
        log.warning(f"check-working: no working {service or '?'} hosts to rescan")
        return

    # check-zero: re-probe ONLY the 0-model hosts (correcting erroneous zeros after a
    # discovery fix). Every non-empty host is moved to `preserved` and written back
    # untouched — never dropped.
    zero_nonempty = []
    if only_zero:
        zeros = []
        for w in working:
            (zeros if not (w.get("models") or {}) else zero_nonempty).append(w)
        log.info(f"check-zero: {len(zeros)} of {len(working)} hosts have 0 models — "
                 f"re-checking those, preserving {len(zero_nonempty)} non-empty")
        working = zeros
        if not working:
            log.info("check-zero: no 0-model hosts to re-check; pool left intact")
            return

    # Resume: hosts already snapshotted THIS session aren't re-probed — but they
    # are still working hosts, so they must be PRESERVED in the pool, not dropped.
    # Writing back only the rescanned subset is exactly what nuked 4408 -> 11.
    preserved = list(zero_nonempty)
    if session:
        to_scan = []
        snap = _check_snapshot_idents(session)
        for w in working:
            hp = w["host"].split(":")
            hh = hp[0]
            pp = int(hp[1]) if len(hp) > 1 else SERVICE_CONFIG[service]["port"]
            if service in SNAPSHOT_SERVICES and _host_ident(f"{hh}:{pp}") in snap:
                preserved.append(w)
                continue
            to_scan.append(w)
        working = to_scan
        if preserved:
            log.info(f"check-working: keeping {len(preserved)} hosts already snapshot "
                     f"in session {session} (not re-probed)")
    if not working:
        log.info("check-working: no hosts left to rescan; pool left intact")
        return

    log.info(f"check-working: rescanning {len(working)} working hosts to refresh models / prune dead")

    sem = asyncio.Semaphore(workers)
    start = time.time()
    completed = 0

    async def probe(entry):
        nonlocal completed
        async with sem:
            # Same doorknock ladder as the initial check, so a host recorded via
            # `directip` (its FQDN is gated, only the IP answers) is re-probed by
            # IP on rescan and not pruned as dead. `method` is re-derived fresh.
            try:
                result, method = await _doorknock(session, entry, service, check_timeout)
            except Exception as e:
                result, method = {"error": str(e)}, None
            if isinstance(result, dict) and "error" not in result:
                result["checked"] = result.get("checked", datetime.now(timezone.utc).isoformat())
                if method:
                    result["method"] = method
                else:
                    result.pop("method", None)
                return ("keep", entry, result)
            reason = result.get("error", str(result)) if isinstance(result, dict) else str(result)
            return ("dead", entry, reason)

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=INSECURE_SSL, force_close=True, limit=0), timeout=aiohttp.ClientTimeout(total=check_timeout + 5)) as session:
        done = await asyncio.gather(*[probe(e) for e in working], return_exceptions=True)

    kept = []
    removed = 0
    pruned = []   # (host, record) for hosts that went dark this pass -> DB notworking
    for outcome in done:
        if isinstance(outcome, Exception):
            outcome = ("dead", None, str(outcome))
        status, entry, payload = outcome
        host = _entry_host(entry) if entry else "?"
        if status == "keep":
            rec = dict(payload)
            rec["host"] = entry["host"]
            # the probe result rebuilds the record, so carry geo across it
            # rather than re-deriving it (and losing it on every re-survey)
            for _gk in _GEO_CARRY:
                if _gk in entry and _gk not in rec:
                    rec[_gk] = entry[_gk]
            kept.append(rec)
        else:
            removed += 1
            if entry:
                nw = {
                    # No "host"/"service": host is the dict key (and in url); service
                    # is implicit in the file name (<service>-notworking.json).
                    "url": f"http://{entry['host']}",
                    "reason": payload,
                    "result": "unreachable",
                    "checked": datetime.now(timezone.utc).isoformat(),
                }
                # keep the geo on a host that has gone dark — a dead host's last
                # known location is exactly what the longitudinal study wants
                for _gk in _GEO_CARRY:
                    if _gk in entry:
                        nw[_gk] = entry[_gk]
                pruned.append((entry["host"], nw))
            log.info(f"~ {host} dead: {payload}")

    # CIRCUIT BREAKER: a re-survey that "kills" more than half the pool in one pass
    # is almost always a LOCAL problem (network blip, too-tight --ct, the box itself),
    # not mass host death — and _check_working overwrites working.json wholesale, so
    # one such pass silently guts the pool (this is what took ollama 1662 -> 11). When
    # the prune fraction is implausibly high, DON'T prune: refresh the model lists of
    # the hosts that did answer, leave the rest in the pool untouched, and warn loudly.
    PRUNE_ABORT_FRAC = 0.5
    if working and removed > 20 and removed / len(working) > PRUNE_ABORT_FRAC:
        by_host = {_entry_host(w): w for w in preserved}      # never re-probed this pass
        by_host.update({_entry_host(w): w for w in working})  # + the rescanned subset
        for rec in kept:                                      # refresh the responders
            by_host[_entry_host(rec)] = rec
        merged = sorted(by_host.values(), key=lambda h: (h.get("checked", ""), _entry_host(h)))
        # Refusing to prune: only the refreshed responders change in the DB; the dead
        # hosts keep their working status. Export regenerates working.json.
        for rec in kept:
            _store_record(svc, rec["host"], "working", rec)
        export(svc)                                           # notworking NOT touched
        log.warning(f"check-working: {removed}/{len(working)} ({removed/len(working):.0%}) failed this "
                    f"pass — that's a bad pass (network / tight --ct / local), not mass host death. "
                    f"REFUSING to prune; pool left intact ({len(merged)}), {len(kept)} models refreshed. "
                    f"Re-run with a larger --ct or fix connectivity.")
        return
    # The written pool is the rescanned survivors PLUS the preserved (not-re-probed)
    # hosts — never just the subset we happened to scan this pass.
    final = {_entry_host(w): w for w in preserved}
    for rec in kept:
        final[_entry_host(rec)] = rec
    out = sorted(final.values(), key=lambda h: (h.get("checked", ""), _entry_host(h)))
    # Survivors -> working, pruned -> notworking, in the DB; export regenerates the json.
    for rec in kept:
        _store_record(svc, rec["host"], "working", rec)
    for host, nw in pruned:
        _store_record(svc, host, "notworking", nw)
    export(svc)
    log.info(f"check-working: {len(working)} rescanned, {len(kept)} still working "
             f"(models refreshed), {removed} pruned, {len(preserved)} preserved — "
             f"pool now {len(out)}")


def check_working(service, name=None, check_timeout=60, workers=10, session=None,
                  only_zero=False):
    asyncio.run(_check_working(service, name, check_timeout, workers, session, only_zero))


def reconstruct(name=None, session=None):
    """Rebuild <name>-working.json from a check session's cached probe snapshots.

    Recovery for a working pool that got truncated. Every host that ANSWERED
    during a check session has its raw model-list response cached under
    /tmp/graflex/<session>/check/*.json (the same snapshots a resumed `-i` run
    skips), so the pool can be rebuilt OFFLINE — no re-probing the fleet.

    Records are rebuilt with the same model parsing the live check uses
    (_filter_models over the listed names); hosts that listed nothing usable are
    dropped, and obvious honeypots — whose ENTIRE catalog is woahllama's phantom
    frozen set — are dropped too. The result is MERGED into the existing working
    file, never clobbered: newest `checked` wins, so fresh survivors and
    geo-enriched records already on disk are preserved. This is offline recovery,
    not a live vet — follow it with `check-working` (re-probe, prune dead, live
    honeypot gate) and dyva `--cleanse` to reach the fully vetted pool."""
    from datetime import datetime, timezone
    if not session:
        log.error("reconstruct: -i <session-id> is required — the run_ts whose "
                  "/tmp/graflex/<session>/check snapshots to rebuild from")
        return
    snap_dir = os.path.join("/tmp/graflex", session, "check")
    if not os.path.isdir(snap_dir):
        log.error(f"reconstruct: no snapshot dir {snap_dir}")
        return

    working_file = _cache_file(name, "working")
    svc = name or "ollama"
    # Existing working pool from the DB (the running record), not working.json.
    existing = [p for _h, p, _c in _hoststore.rows(_store(), svc, "working")]
    by_host = {_entry_host(e): e for e in existing}

    files = [fn for fn in os.listdir(snap_dir) if fn.endswith(".json")]
    rebuilt = added = refreshed = empty = honeypots = 0
    changed = []
    for fn in files:
        try:
            with open(os.path.join(snap_dir, fn), encoding="utf-8") as fh:
                snap = json.load(fh)
        except (ValueError, OSError):
            continue
        host = snap.get("host")
        payload = snap.get("payload")
        if not host or not isinstance(payload, dict):
            continue
        # Same name extraction as the live check: ollama /api/tags (models[].name),
        # falling back to the OpenAI /v1/models shape (data[].id) for the hosts that
        # answered that way. _filter_models drops the :cloud passthroughs.
        raw = payload.get("models")
        if not isinstance(raw, list):
            raw = payload.get("data") if isinstance(payload.get("data"), list) else []
        # Pass the RAW entries so /api/tags `capabilities` is preserved as caps in
        # the rebuilt Form A object (the snapshots carry it); _filter_models also
        # drops the :cloud passthroughs.
        models = _filter_models(raw)
        if not models:
            empty += 1
            continue
        if set(models) <= HONEYPOT_MODELS:   # whole catalog is the frozen set => honeypot
            honeypots += 1
            continue
        rebuilt += 1
        port = host.rsplit(":", 1)[-1] if ":" in host else ""
        scheme = "https" if port == "443" else "http"
        checked = datetime.fromtimestamp(snap.get("check_time", 0), timezone.utc).isoformat()
        rec = {"service": "ollama", "url": f"{scheme}://{host}/",
               "models": models, "checked": checked, "host": host}
        prev = by_host.get(host)
        if prev is None:
            by_host[host] = rec
            changed.append(rec)
            added += 1
        elif checked > (prev.get("checked") or ""):
            # newer than what's on disk — refresh, but carry geo across the rebuild
            for gk in _GEO_CARRY:
                if gk in prev and gk not in rec:
                    rec[gk] = prev[gk]
            by_host[host] = rec
            changed.append(rec)
            refreshed += 1
        # else: on-disk record is newer/equal — keep it (fresh survivor / enriched)

    merged = sorted(by_host.values(), key=lambda h: (h.get("checked", ""), _entry_host(h)))
    # Fold the rebuilt/refreshed records into the DB (the running record); export then
    # regenerates working.json. Only the changed records are upserted, not the whole pool.
    for rec in changed:
        _store_record(svc, rec["host"], "working", rec)
    export(svc)
    log.info(f"reconstruct: {len(files)} snapshots — {rebuilt} rebuilt "
             f"({empty} listed nothing usable, {honeypots} honeypots dropped); "
             f"{added} new, {refreshed} refreshed; pool now {len(merged)} "
             f"(was {len(existing)}) in {os.path.basename(working_file)}")


def _load_classifier():
    with open(CLASSIFIER_FILE, encoding="utf-8") as f:
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
    """Return (category, depth). `category` is a CLEAN key with no leading spaces,
    so it is safe as a JSON key; `depth` is the 1-based match position used ONLY to
    indent the printed scan for the human eye — it must never reach the JSON."""
    for depth, (ctype, regs) in enumerate(compiled.items(), 1):
        if any(r.search(model) for r in regs):
            return ctype, depth
    return ".....", 0


def _classify_gradio_hosts(name, entries):
    """Bucket Gradio hosts by app purpose using their /config manifest, the way
    `classify` buckets comfyui hosts by their models. Same input (the working
    file), same -classified output, different classifier."""
    from datetime import datetime, timezone
    taxonomy = _load_gradio_taxonomy()
    counts = {}
    out = []
    for entry in entries:
        e = dict(entry)
        summ = entry.get("gradio") or {}
        bucket = _classify_gradio(summ, taxonomy)
        e["app_type"] = bucket
        counts[bucket] = counts.get(bucket, 0) + 1
        out.append(e)
    input_file = _cache_file(name, "working")
    root, ext = os.path.splitext(input_file)
    out_file = f"{root}-classified{ext}"
    _save_json(out_file, out)
    for bucket, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{bucket:18s} {c}")
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    log.info(f"classify: {len(entries)} gradio hosts -> {out_file} ({summary}) "
             f"[{datetime.now(timezone.utc).isoformat()}]")


def _consolidate_image_edit(classified):
    """Redistribute the `image_edit` bucket into `image` and `edit`, then drop it.
    Dual-purpose models (Flux.2) generate AND edit, so they belong in both; the
    exclusive first-match classifier files them under a single `image_edit` bucket
    that is split here."""
    if "image_edit" not in classified:
        return
    classified.setdefault("image", [])
    classified.setdefault("edit", [])
    for m in classified.pop("image_edit"):
        if m not in classified["image"]:
            classified["image"].append(m)
        if m not in classified["edit"]:
            classified["edit"].append(m)


# The publishable "sample platter": a small stratified draw from THIS box's
# reachability survey (the per-service working files) so a demo user can point
# dyva at one file and get media/text generation working without shipping the
# whole survey. Deliberately the "shitty version": graflex (the probe, in NJ) is
# isolated from dyva (the router, in LA), so there is NO runtime-reputation cross
# check — capability comes from the hand-curated classifier, reachability from
# graflex's own working files. When gossiper bridges the two boxes this can be
# reputation-informed. Counts are per-bucket (not a flat %), so no capability
# comes up empty. All tunable.
SAMPLE_OUT = os.path.join(CACHE_DIR, "graflex-mini.json")
# bucket -> (services to draw from, how many, the classifier kind for comfyui
# media buckets — None means "no per-model kind gate for this bucket").
SAMPLE_BUCKETS = {
    "text":   (["ollama", "llama.cpp", "vllm", "lmstudio", "localai"], 30, None),
    "image":  (["a1111", "comfyui"], 8, "image"),
    "edit":   (["comfyui"], 4, "edit"),
    "video":  (["comfyui"], 8, "video"),
    "music":  (["comfyui"], 4, "music"),
    "speech": (["comfyui"], 3, "audio"),
}


def _load_working(svc):
    """The <svc>-working.json records, or [] if the file is absent/unreadable.
    READ-ONLY — sample never writes back to a working file."""
    p = _cache_file(svc, "working")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f"sample: could not read {p}: {e}")
        return []
    return data if isinstance(data, list) else []


def _host_media_kinds(rec, compiled):
    """The set of media CAPABILITY kinds a comfyui host offers, per the
    hand-curated classifier. Parts (lora / vision-encoder / other / unmatched)
    are NOT capabilities, so they don't count; image_edit folds into both."""
    kinds = set()
    for m in rec.get("models") or []:
        cat, _ = _classify_model(str(m), compiled)
        if cat == "image_edit":
            kinds.update(("image", "edit"))
        elif cat in ("image", "edit", "video", "music", "audio"):
            kinds.add(cat)
    return kinds


def _host_can_chat(rec):
    """True if a text-service host lists at least one non-embedding model — a
    cheap name-only filter (no cross-check) so a pure-embedding host doesn't get
    sampled into the text demo where it can't chat."""
    return any("embed" not in str(m).lower() for m in (rec.get("models") or []))


def sample():
    """Draw the stratified sample platter and write graflex-mini.json, then exit.
    Read-only over every survey file; the only write is the new output."""
    import random
    from datetime import datetime, timezone
    compiled = _load_classifier()
    working = {}   # svc -> records, loaded once
    picked = {}    # host -> record (deduped across buckets)
    counts = {}
    for bucket, (services, n, kind) in SAMPLE_BUCKETS.items():
        pool = {}   # host -> record, unique within this bucket
        for svc in services:
            if svc not in working:
                working[svc] = _load_working(svc)
            for rec in working[svc]:
                if bucket == "text":
                    if not _host_can_chat(rec):
                        continue
                elif svc == "comfyui":
                    if kind not in _host_media_kinds(rec, compiled):
                        continue
                # a1111 hosts are all image-gen -> no per-model gate
                h = rec.get("host") or rec.get("url")
                if h:
                    pool.setdefault(h, rec)
        chosen = random.sample(list(pool.values()), min(n, len(pool)))
        counts[bucket] = (len(chosen), len(pool))
        for rec in chosen:
            h = rec.get("host") or rec.get("url")
            slot = picked.setdefault(h, dict(rec, buckets=[]))
            if bucket not in slot["buckets"]:
                slot["buckets"].append(bucket)
    out = list(picked.values())
    _save_json(SAMPLE_OUT, out)
    summary = ", ".join(f"{b}: {got}/{avail}" for b, (got, avail) in counts.items())
    log.info(f"sample: {len(out)} unique hosts -> {SAMPLE_OUT} ({summary}) "
             f"[{datetime.now(timezone.utc).isoformat()}]")


def classify(name=None):
    from datetime import datetime, timezone

    name = name or "comfyui"
    input_file = _cache_file(name, "working")
    entries = _load_json(input_file)
    entries = [e for e in entries if isinstance(e, dict)]
    if not entries:
        log.error(f"classify: no hosts in {input_file}")
        return
    # Gradio hosts carry a /config manifest, not a model list — bucket by app
    # purpose against the gradio taxonomy instead of the model classifier.
    if name == "gradio" or any(e.get("service") == "gradio" or "gradio" in e for e in entries):
        _classify_gradio_hosts(name, entries)
        return
    log.info(f"using {CLASSIFIER_FILE}")

    compiled = _load_classifier()

    out = []
    printed = set()
    counts = {}
    for entry in entries:
        e = dict(entry)
        classified = {ctype: [] for ctype in list(compiled) + ["....."]}
        for m in entry.get("models") or []:
            ctype, depth = _classify_model(m, compiled)
            classified.setdefault(ctype, []).append(m)
            counts[ctype] = counts.get(ctype, 0) + 1
            key = (ctype, m)
            if key not in printed:
                printed.add(key)
                print(f"{' ' * depth}{ctype:13s} {m}")   # indent aids the human scan; the JSON key stays clean
        _consolidate_image_edit(classified)
        e["classified"] = classified
        out.append(e)

    ie_total = 0
    for k in [k for k in counts if k.strip() == "image_edit"]:
        ie_total += counts.pop(k)
    if ie_total:
        for nm in ("image", "edit"):
            ck = next((k for k in counts if k.strip() == nm), nm)
            counts[ck] = counts.get(ck, 0) + ie_total
    root, ext = os.path.splitext(input_file)
    out_file = f"{root}-classified{ext}"
    _save_json(out_file, out)
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    log.info(f"classify: {len(entries)} hosts -> {out_file} ({summary}) [{datetime.now(timezone.utc).isoformat()}]")




def enrich_file(path, key=None, refresh=False):
    """Geo-enrich an arbitrary JSON file in place (list of dicts, or dict keyed
    by host). `key` names the host field; auto-detected when None. `refresh`
    re-stamps records already enriched (needed after the DB gains new fields,
    e.g. adding city/lat/lon on top of a country-only pass)."""
    from . import geoip
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.error(f"enrich: cannot read {path}: {e}")
        return 1
    if not isinstance(data, (list, dict)):
        log.error(f"enrich: {path} is not a JSON list or object")
        return 1
    try:
        looked, matched = geoip.enrich_records(data, key=key, refresh=refresh)
    except Exception as e:
        log.error(f"enrich: lookup failed for {path}: {e}")
        return 1
    # Write via a temp file + atomic replace, and surface any failure loudly —
    # a silent non-write (disk full, permissions, a kill mid-write) otherwise
    # looks like enrich "did nothing" after minutes of work.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        log.error(f"enrich: FAILED to write {path}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 1
    n = len(data)
    log.info(f"enrich: wrote {n} records to {os.path.basename(path)}, "
             f"{looked} looked up, {matched} matched")
    return 0


# Derived geo enrichment written by `enrich` (geoip.py). Safe to strip because it
# rebuilds for free with `-a enrich` — removing it is a memory saving, not data loss.
_GEO_KEYS = ("country", "city", "lat", "lon", "asn", "asn_org", "as_org", "provider", "geo_checked")


def strip_cache():
    """Shrink ~/.cache/free-ollama/*.json to save memory by removing fields that
    rebuild trivially — not data loss:
      - derived geo (country/city/lat/lon/asn/asn_org/as_org/provider/geo_checked),
        which re-derives with `-a enrich`;
      - `service` and `host` in *-notworking.json — service is implicit in the file
        name, and host is the record's own dict key (and lives in `url`).
    Rewrites each touched file atomically."""
    import glob
    total_saved = 0
    for path in sorted(glob.glob(os.path.join(CACHE_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                before = f.read()
            data = json.loads(before)
        except (ValueError, OSError) as e:
            log.warning(f"strip: skip {os.path.basename(path)} ({e})")
            continue
        if isinstance(data, list):
            recs = data
        elif isinstance(data, dict):
            recs = list(data.values())
        else:
            continue
        keys = set(_GEO_KEYS)
        if os.path.basename(path).endswith("-notworking.json"):
            keys.update(("service", "host"))   # service=file name, host=the dict key/url
        hit = 0
        for r in recs:
            if isinstance(r, dict):
                for k in keys:
                    if k in r:
                        del r[k]
                        hit += 1
        if not hit:
            continue
        saved = len(before) - len(json.dumps(data))
        total_saved += saved
        _save_json_atomic(path, data)
        log.info(f"strip: {os.path.basename(path)}: -{hit} fields, ~{saved // 1024} KB")
    log.info(f"strip: freed ~{total_saved / 1048576:.1f} MB across the cache "
             f"(geo rebuilds with -a enrich; notworking service is the file name)")


def enrich(name=None):
    """Stamp country / ASN / cloud-provider onto a service's working hosts.

    Runs on `<name>-working.json` — the file `graflex.sh combine` ships as the
    dyva feed — so the enrichment is baked into what dyva serves. Offline: the
    only network is DB-IP's monthly database download (and a DNS lookup for the
    rare hostname-not-IP host). Idempotent; re-stamps only stale records."""
    from . import geoip
    wf = _cache_file(name, "working")
    entries = _load_json(wf)
    if not isinstance(entries, list) or not entries:
        log.error(f"enrich: no hosts in {wf}")
        return
    try:
        looked, matched = geoip.enrich_records(entries)
    except Exception as e:
        log.error(f"enrich: {e}")
        return
    _save_json_atomic(wf, entries)
    log.info(f"enrich: {len(entries)} hosts in {os.path.basename(wf)}, "
             f"{looked} looked up, {matched} matched")


# ---- Gradio classification -------------------------------------------------
# The gradio icon (icon_hash 55115683) is not one app — it is every Gradio app
# anyone exposed. There is no shared inference API, so the *check* for a gradio
# host is fetching its /config manifest (title, component labels, function
# names) and the *classify* step buckets those manifests by app purpose. Both
# reuse the normal check/classify machinery; only the parsing differs.
GRADIO_TAXONOMY_FILE = os.path.join(os.path.dirname(__file__), "gradio-taxonomy.json")


def _gradio_summary(cfg, host=None):
    """The bits of a Gradio /config that say what the app is for."""
    labels = []
    for c in cfg.get("components", []) or []:
        if not isinstance(c, dict):
            continue
        lab = (c.get("props") or {}).get("label")
        if isinstance(lab, str) and lab.strip():
            labels.append(lab.strip())
    api = [d.get("api_name") for d in (cfg.get("dependencies") or [])
           if isinstance(d, dict) and d.get("api_name")]
    return {
        "title": cfg.get("title"),
        "version": cfg.get("version"),
        "n_components": len(cfg.get("components", []) or []),
        "labels": list(dict.fromkeys(labels)),
        "api_names": list(dict.fromkeys(a for a in api if a)),
    }


_gradio_taxonomy_cache = None


def _load_gradio_taxonomy():
    """Ordered buckets of keyword lists; first match wins, so specific buckets
    come before generic ones. Editable data, not code."""
    global _gradio_taxonomy_cache
    if _gradio_taxonomy_cache is None:
        try:
            with open(GRADIO_TAXONOMY_FILE, encoding="utf-8") as f:
                _gradio_taxonomy_cache = json.load(f)
        except Exception as e:
            log.warning(f"gradio taxonomy: {e}")
            _gradio_taxonomy_cache = []
    return _gradio_taxonomy_cache


def _classify_gradio(summary, taxonomy=None):
    taxonomy = taxonomy if taxonomy is not None else _load_gradio_taxonomy()
    hay = " ".join(filter(None, [
        summary.get("title") or "",
        " ".join(summary.get("labels") or []),
        " ".join(summary.get("api_names") or []),
    ])).lower()
    for bucket in taxonomy:
        for kw in bucket.get("keywords", []):
            if kw.lower() in hay:
                return bucket["name"]
    return "unknown"


def survey(run_ts=None):
    """Mine the saved model-list probe snapshots into survey.json — a per-model
    knowledge base built from real observations in the logs, not guessed from
    names. Records disk size (bytes) and digest per model from the ollama
    /api/tags payloads; extensible for more derived facts (quantization, etc.)
    later. survey.json ACCUMULATES, so with `-i <session>` it folds in just that
    sweep's fresh snapshots (the normal case, right after a check); with no -i it
    rescans every session for a full rebuild."""
    import glob
    _SHA = re.compile(r'^(sha256:)?[0-9a-f]{64}$', re.I)
    # Incremental (-i) merges into the existing survey; a full rebuild (no -i)
    # starts fresh so earlier pollution is dropped.
    data = {}
    if run_ts and os.path.exists(SURVEY_FILE):
        try:
            with open(SURVEY_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            data = {}
    files = glob.glob(os.path.join("/tmp/graflex", run_ts if run_ts else "*", "check", "*.json"))
    obs = 0
    skipped_dyva = 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                snap = json.load(f)
        except (ValueError, OSError):
            continue
        payload = (snap or {}).get("payload")
        # payloads come in several shapes: ollama /api/tags -> {"models":[…]},
        # openai/vllm/lmstudio/llama.cpp /v1/models -> {"data":[…]}, and some are
        # a bare list. Normalise to a list of model dicts.
        if isinstance(payload, dict):
            items = payload.get("models") or payload.get("data") or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        if not isinstance(items, list) or not items:
            continue
        # ouroboros guard: a real ollama host's digests are sha256 hex; a dyva
        # instance (many run in the wild) echoes the pool back with ASCII-art
        # "digests" and byte-sized "sizes". If this snapshot's present digests
        # are mostly non-hex, it's a dyva/proxy — skip it so we don't survey
        # ourselves.
        digs = [str(m.get("digest")) for m in items if isinstance(m, dict) and m.get("digest")]
        if digs and sum(1 for d in digs if not _SHA.match(d)) >= len(digs) / 2:
            skipped_dyva += 1
            continue
        for m in items:
            if not isinstance(m, dict):
                continue
            name = m.get("name") or m.get("id") or m.get("model") or m.get("model_name")
            if not name:
                continue
            rec = data.setdefault(name, {})
            rec["seen"] = int(rec.get("seen") or 0) + 1
            obs += 1
            sz = m.get("size")
            dg = m.get("digest")
            # A size only counts from a real (sha256) entry that's plausibly
            # large (≥1MB — no real model is smaller). Each size bucket carries
            # its digest, so near-identical sizes that are really the same build
            # (metadata-layer noise) can be told apart from genuinely different
            # weights. The reported `size`/`digest` is the MODE bucket, so a
            # stray value can't beat the majority.
            if isinstance(sz, (int, float)) and sz >= 1_000_000 and dg and _SHA.match(str(dg)):
                hist = rec.setdefault("sizes", {})
                key = str(int(sz))
                b = hist.get(key)
                if not isinstance(b, dict):   # tolerate a legacy int bucket
                    b = {"count": int(b) if isinstance(b, int) else 0, "digest": dg}
                    hist[key] = b
                b["count"] = int(b.get("count") or 0) + 1
                b["digest"] = str(dg)
                mode = max(hist, key=lambda k: hist[k].get("count", 0) if isinstance(hist[k], dict) else (hist[k] or 0))
                rec["size"] = int(mode)
                mb = hist.get(mode)
                rec["digest"] = mb.get("digest") if isinstance(mb, dict) else rec.get("digest")
    os.makedirs(CACHE_DIR, exist_ok=True)
    _save_json_atomic(SURVEY_FILE, data)
    sized = sum(1 for r in data.values() if isinstance(r, dict) and r.get("size"))
    log.info(f"survey: {len(data)} models ({sized} with size), {obs} observations "
             f"across {len(files)} snapshots ({skipped_dyva} dyva/proxy skipped) -> {SURVEY_FILE}")
    return data


def _install_mem_dump():
    """Wire SIGUSR1 to dump a memory snapshot of the live process — RSS plus a gc
    object-count histogram (what types dominate: dicts = the pooled records, etc.),
    and, if GRAFLEX_TRACEMALLOC=1 was set at startup, the tracemalloc allocation top.
    Zero standing cost — it only runs when you `kill -USR1 <pid>`. Dumps to
    /tmp/graflex-mem-<pid>-<hhmmss>.txt and logs the path. Best-effort: silently
    skips if signals aren't available (e.g. not the main thread)."""
    import signal

    def _dump(signum, frame):
        import gc
        import collections
        from datetime import datetime as _dt
        lines = [f"# graflex mem dump pid={os.getpid()} {_dt.now().isoformat()}"]
        try:
            with open("/proc/self/status") as f:
                for ln in f:
                    if ln.startswith(("VmRSS", "VmHWM", "VmSize")):
                        lines.append(ln.rstrip())
        except OSError:
            pass
        objs = gc.get_objects()
        lines.append(f"gc tracked objects: {len(objs)}")
        for tname, n in collections.Counter(type(o).__name__ for o in objs).most_common(25):
            lines.append(f"  {n:>10} {tname}")
        try:
            import tracemalloc
            if tracemalloc.is_tracing():
                cur, peak = tracemalloc.get_traced_memory()
                lines.append(f"tracemalloc current={cur // 1024} KB peak={peak // 1024} KB — top 20 by line:")
                for st in tracemalloc.take_snapshot().statistics("lineno")[:20]:
                    lines.append(f"  {st}")
        except Exception:
            pass
        path = f"/tmp/graflex-mem-{os.getpid()}-{_dt.now():%H%M%S}.txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            log.warning(f"memory dump -> {path}")
        except OSError as e:
            log.warning(f"memory dump failed: {e}")

    try:
        signal.signal(signal.SIGUSR1, _dump)
        if os.getenv("GRAFLEX_TRACEMALLOC") == "1":
            import tracemalloc
            tracemalloc.start(25)
    except (ValueError, OSError, AttributeError):
        pass  # no SIGUSR1 (Windows) / not main thread — skip quietly

    # Auto-capture the SPIKE: GRAFLEX_MEM_DUMP_MB=400 starts a daemon thread that
    # polls RSS every few seconds and fires the dump the first time it crosses the
    # threshold (re-arming once RSS falls 10% back below), so an intermittent OOM
    # snapshots itself at the peak instead of you trying to time `kill -USR1`.
    limit_mb = os.getenv("GRAFLEX_MEM_DUMP_MB")
    if limit_mb and limit_mb.isdigit():
        import threading

        def _rss_mb():
            try:
                with open("/proc/self/status") as f:
                    for ln in f:
                        if ln.startswith("VmRSS"):
                            return int(ln.split()[1]) // 1024
            except (OSError, ValueError):
                pass
            return 0

        def _watch(limit):
            armed = True
            while True:
                rss = _rss_mb()
                if armed and rss >= limit:
                    log.warning(f"RSS {rss} MB >= {limit} MB — auto memory dump")
                    _dump(None, None)
                    armed = False
                elif not armed and rss < limit * 0.9:
                    armed = True
                time.sleep(3)

        threading.Thread(target=_watch, args=(int(limit_mb),), daemon=True).start()


def main():
    load_dotenv()

    global FOFA_COOKIE, SHODAN_KEY
    FOFA_COOKIE = os.getenv("FOFA_COOKIE", "")
    SHODAN_KEY = _clean_cookie(os.getenv("SHODAN_KEY", ""))

    # a Windows console codepage can't encode the — in our log lines; replace
    # the character rather than dying partway through a long scan
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOGLEVEL", "INFO").upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
    )
    _install_mem_dump()   # kill -USR1 <pid> for a live memory snapshot

    parser = argparse.ArgumentParser(description="Discover public image-generation hosts via FOFA")
    parser.add_argument("--ct", "--check-timeout", dest="check_timeout", type=int, default=60, help="per-host check timeout in seconds (default: 60)")
    parser.add_argument("--curlify", action="store_true", help="print curl command instead of executing")
    parser.add_argument("-a", "--action", choices=["fetch", "check", "check-new", "check-all", "check-working", "check-zero", "fetch-check", "classify", "enrich", "survey", "reconstruct", "score-bogus", "sample", "strip"], required=True, help="action to perform")
    parser.add_argument("-c", "--countries", help="comma-separated country codes to cycle (default: CN,US,CA,JP,KR)")
    parser.add_argument("-d", "--dry", action="store_true", help="report what fetch would do without saving")
    parser.add_argument("-e", "--servers", help="comma-separated server values to cycle (default: uvicorn,nginx)")
    parser.add_argument("-f", "--fid", dest="fids", help="comma-separated FID values to filter by")
    parser.add_argument("-i", "--id", dest="session", help="resume a previous session by providing its run timestamp (the run_ts from the log)")
    parser.add_argument("-n", "--name", help="cache file name prefix (default: image-gen); a named query (e.g. gradio) also selects its built-in FOFA query for fetch; 'all' runs the action across every service (for cron automation), same as -s all")
    parser.add_argument("-p", "--ports", help="comma-separated port values to cycle")
    parser.add_argument("-q", "--query", help="custom FOFA query (requires --name)")
    parser.add_argument("-r", "--random", dest="shuffle", action="store_true", help="shuffle the ports, servers, countries, and FID lists so the fetch cycles through combinations in random order")
    parser.add_argument("-t", "--site", choices=["fofa", "shodan", "zoomeye", "censys", "hunter"], default="fofa", help="site to scrape (default: fofa)")
    parser.add_argument("-s", "--service", choices=list(SERVICE_CONFIG) + ["all"],
                        help="service to search for; 'all' runs the action across every service (each gets its own <service>-*.json cache), for automating the full pipeline")
    parser.add_argument("-w", "--workers", type=int, default=10, help="max parallel check workers (default: 10)")
    parser.add_argument("-z", "--sleep", type=int, default=SLEEP_DEFAULT, help=f"seconds to sleep between requests (default: {SLEEP_DEFAULT})")
    parser.add_argument("enrich_args", nargs="*", metavar="FILE [KEY] [refresh]",
                        help="for -a enrich: a JSON file to geo-enrich in place, optionally the "
                             "field holding the host (server/url/host; auto-detected when omitted), "
                             "and the literal 'refresh' to re-stamp already-enriched records (e.g. "
                             "to backfill city/lat/lon over a country-only pass). Without a file, "
                             "-a enrich runs over the per-service working files (use -s all).")
    args = parser.parse_args()

    # survey mines the saved probe logs into survey.json — no service/name needed.
    # -i <session> folds in just that sweep's snapshots; no -i rescans everything.
    if args.action == "survey":
        survey(run_ts=args.session)
        return

    # score-bogus mines the check snapshots for honeypot clone fingerprints and stamps
    # bogus_score onto the working file(s) — like survey, needs no service to run.
    # -i picks a session's snapshots; -n/-s picks one service, else all get stamped.
    if args.action == "score-bogus":
        _all = args.service == "all" or (args.name or "").strip().lower() == "all"
        score_bogus(session=args.session, name=(None if _all else (args.name or args.service)))
        return

    # sample mines the per-service working files into one small graflex-mini.json
    # sample platter — no service/name needed, reads across every bucket's services.
    if args.action == "sample":
        sample()
        return

    # strip removes derived geo fields from the cache JSONs to save memory; rebuild
    # with -a enrich. No service/name needed — it sweeps ~/.cache/free-ollama/*.json.
    if args.action == "strip":
        strip_cache()
        return

    if args.query and not args.name:
        parser.error("--query requires --name")
    if (not args.service and not args.query and not args.name
            and not (args.action == "enrich" and args.enrich_args)):
        parser.error("either --service, --query, or --name is required")
    if args.site == "shodan":
        if args.fids:
            log.warning("-f/--fid is ignored with --site shodan")

    parts = args.action.split("-")
    check_new = args.action == "check-new"
    check_all = args.action == "check-all"

    # "all services" for the whole pipeline, so the 5-day cron run is one
    # invocation per action. Triggered by `-s all` or `-n all` (the user reaches
    # for both spellings). Each service has its own query/ports/working file, so
    # this is just the single-service path run once per service, with name left
    # to each service's own defaults.
    all_services = args.service == "all" or (args.name or "").strip().lower() == "all"
    pipe_services = list(SERVICE_CONFIG) if all_services else [args.service]
    if args.query and all_services:
        parser.error("--query cannot be combined with 'all' services")

    # `-a enrich <file> [key]`: geo-enrich an arbitrary JSON file in place. The
    # catch-all for foreign-source hosts (run it on free-ollama.json after a
    # dyva refresh) and for ragged legacy files whose host field isn't `server`.
    if args.action == "enrich" and args.enrich_args:
        fargs = args.enrich_args
        rest = fargs[1:]
        refresh = "refresh" in rest
        keyarg = next((x for x in rest if x != "refresh"), None)
        sys.exit(enrich_file(fargs[0], keyarg, refresh=refresh))

    if args.action == "reconstruct":
        log.info("--- reconstruct ---")
        for svc in pipe_services:
            reconstruct(name=(svc if all_services else (args.name or args.service)),
                        session=args.session)
        return

    if args.action in ("check-working", "check-zero"):
        only_zero = args.action == "check-zero"
        label = args.action
        log.info(f"--- {label} ---")
        # "-s all" re-surveys every service in turn — each has its own working
        # file, so this is just the single-service pass run once per service.
        # check-zero re-probes only that file's 0-model hosts (correcting erroneous
        # zeros, e.g. after the comfyui folder-discovery fix), preserving the rest.
        services = pipe_services
        try:
            for svc in services:
                if all_services:
                    log.info(f"--- {label}: {svc} ---")
                check_working(service=svc, name=(None if all_services else args.name),
                              check_timeout=args.check_timeout, workers=args.workers,
                              session=args.session, only_zero=only_zero)
        except KeyboardInterrupt:
            base = f"graflex -s {args.service}" if args.service else f"graflex -n {args.name or 'image-gen'}"
            ts = _RUN_TS or args.session
            hint = f"{base} -a {label}{f' -i {ts}' if ts else ''}"
            log.warning(f"\ninterrupted — tag snapshots are saved; resume with: {hint}")
            sys.exit(130)
        return

    try:
        for _svc in pipe_services:
            # Use the service as the cache-file prefix for all-services runs.
            # check/check_working fall back name->service internally, but fetch
            # does not, so without this every service's fetch would collide into
            # image-gen-hosts.json. Each service gets its own <svc>-*.json.
            _nm = _svc if all_services else args.name
            if all_services:
                log.info(f"=== service: {_svc} ===")
            if args.action == "fetch-check":
                log.info("--- fetch-check (interleaved) ---")

                def batch_cb(fresh_hosts, _svc=_svc, _nm=_nm):
                    # Check ALL new hosts from this page — not just the first
                    # `workers`. `workers` is the probe CONCURRENCY (applied inside
                    # check_batch), not a per-page cap; slicing to it silently left
                    # every new host beyond the 65th unchecked, piling up a backlog
                    # you then had to chase with a manual check-new. fetch-check must
                    # check everything it fetches.
                    log.info(f"  check batch: {len(fresh_hosts)} new host(s) (interleaved)")
                    check_batch(fresh_hosts, _svc, _nm, args.check_timeout, args.workers, args.session)

                fetch(dry=args.dry, curlify=args.curlify, service=_svc, query=args.query,
                      name=_nm, servers=args.servers, ports=args.ports, countries=args.countries,
                      fids=args.fids, sleep=args.sleep, session=args.session, shuffle=args.shuffle,
                      site=args.site, check_batch_fn=batch_cb)

                if not args.dry and not args.curlify:
                    log.info("--- drain remaining new hosts ---")
                    check(service=_svc, name=_nm, check_timeout=args.check_timeout,
                          check_new=True, check_all=False, workers=args.workers, session=args.session, site=args.site)
                    # Fold every PROBED host into the working pool from the session's
                    # /check snapshots. The interleaved check deliberately SKIPS a host it
                    # already has a probe record for (a resumed run, or a shared-ZFS session
                    # probed on another box) — so without this those results are never
                    # written to <name>-working.json: the "+40 new in the pool, +0 working"
                    # gap. Offline, no re-probe — the snapshot (+ failed.json) IS the
                    # verdict. reconstruct merges newest-wins and never drops existing rows,
                    # and is ollama-shaped, so gate it on the ollama service.
                    if args.session and _svc == "ollama":
                        log.info("--- fold probed hosts into working (from check snapshots) ---")
                        reconstruct(name=_nm, session=args.session)
            else:
                for step in parts:
                    # log.info(f"--- {step} ---")
                    if step == "fetch":
                        fetch(dry=args.dry, curlify=args.curlify, service=_svc, query=args.query, name=_nm, servers=args.servers, ports=args.ports, countries=args.countries, fids=args.fids, sleep=args.sleep, session=args.session, shuffle=args.shuffle, site=args.site)
                    elif step == "check":
                        check(service=_svc, name=_nm, check_timeout=args.check_timeout, check_new=check_new, check_all=check_all, workers=args.workers, session=args.session, site=args.site)
                    elif step == "classify":
                        classify(name=_nm)
                    elif step == "enrich":
                        enrich(name=_nm or _svc)
    except SystemExit as e:
        sys.exit(e.code)
    except KeyboardInterrupt:
        base = f"graflex -s {args.service}" if args.service else f"graflex -n {args.name or 'image-gen'}"
        ts = _RUN_TS or args.session
        if args.action == "fetch-check":
            hint = f"{base} -a fetch-check{f' -i {ts}' if ts else ''}"
            log.warning(f"\ninterrupted — fetched pages and tag snapshots are saved; resume with: {hint}")
        elif step == "fetch":
            hint = f"{base} -a fetch{f' -i {ts}' if ts else ''}"
            log.warning(f"\ninterrupted — already-fetched pages are saved; resume with: {hint}")
        elif step == "check":
            hint = f"{base} -a check{f' -i {ts}' if ts else ''}"
            log.warning(f"\ninterrupted — checked hosts (working snapshots and failures) are saved; resume with: {hint}")
        else:
            log.warning("\ninterrupted")
        sys.exit(130)
