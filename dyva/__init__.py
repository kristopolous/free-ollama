#!/usr/bin/env python3
import argparse
import asyncio
import datetime
import fnmatch
import csv
import json
import hashlib
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import requests
import sqlite3
import importlib.metadata

import aiohttp
from aiohttp import web
from aiohttp_swagger3 import SwaggerDocs, SwaggerInfo, SwaggerUiSettings

LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()

LOG_FORMAT = "%(asctime)s %(srcaddr)s %(levelname)s %(message)s"


class ApacheStyleFormatter(logging.Formatter):
    # logging.Formatter's `defaults=` kwarg is 3.10+, so supply the missing
    # srcaddr here instead — records logged without extra={} still format.
    def format(self, record):
        if not hasattr(record, "srcaddr"):
            record.srcaddr = "-"
        return super().format(record)

    def formatTime(self, record, datefmt=None):
        tz = datetime.timezone(datetime.timedelta(seconds=time.localtime().tm_gmtoff))
        return datetime.datetime.fromtimestamp(record.created, tz=tz).strftime("[%d/%b/%Y:%H:%M:%S %z]")


logging.basicConfig(
    level=getattr(logging, LOGLEVEL, logging.INFO),
    handlers=[logging.StreamHandler(sys.stderr)],
)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(ApacheStyleFormatter(LOG_FORMAT))
log = logging.getLogger("dumpster-dyva")

access_logger = logging.getLogger("aiohttp.access")
access_logger.propagate = False
access_logger.addHandler(logging.StreamHandler(sys.stderr))
access_logger.handlers[-1].setFormatter(logging.Formatter("%(message)s"))


def log_upstream(code, host, endpoint, body, remote=None):
    log.warning(
        f"upstream error {code} from {host}{endpoint}: {body}",
        extra={"srcaddr": remote or "-"},
    )

CACHE_DIR = os.path.expanduser("~/.cache/free-ollama")
CACHE_FILE = os.path.join(CACHE_DIR, "free-ollama.json")
BAD_FILE = os.path.join(CACHE_DIR, "bad-hosts.txt")
GOOD_FILE = os.path.join(CACHE_DIR, "good-hosts.txt")
STATUS_DB = os.path.join(CACHE_DIR, "host-status.db")
# sentinel "model" for a host-wide unreachable mark (a host dead at the
# connection level is dead for every model, so it must not be re-probed
# per-model). \x00 can't occur in a real canon'd model name.
UNREACHABLE_KEY = "\x00unreachable"
LAST_FILE = os.path.join(CACHE_DIR, "last-success.json")
KNOWN_FILE = os.path.join(CACHE_DIR, "known-hosts.json")
IMG_DIR = os.path.join(CACHE_DIR, "images")
IMG_HISTORY_FILE = os.path.join(IMG_DIR, "history.json")
THUMB_DIR = os.path.join(IMG_DIR, "thumbs")
CHATS_FILE = os.path.join(CACHE_DIR, "chats.json")   # legacy blob, migrated into CHATS_DB
CHATS_DB = os.path.join(CACHE_DIR, "chats.db")
IMG_HISTORY_MAX = 100
CAP_REFRESH_TTL = 3600
_last_cache = None
_knowns_cache = None
_LOCAL = False
_CURLIFY = False

PORT = 11434
TIMEOUT = 30
WORKER_COUNT = 10
MIN_COUNT = 0   # hide models served by fewer than this many hosts (0/1 = show all)
ADMIN_PW = ""   # sha256 hex of the admin password; when set, viewing/changing
                # settings & sources requires it (localhost always exempt).
VERSION = "0"
SETTINGS_FILE = os.path.join(CACHE_DIR, "settings.json")

# The ComfyUI model classifier (shared with graflex) lives in this package so
# it can be version-controlled: each regex category -> class of model. Used to
# derive OpenAI-style modalities for /v1/models and to route /v1/videos jobs.
CLASSIFIER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "graflex", "model-classifier.json")
_classifier_cache = None

# class -> output modalities (OpenRouter style) surfaced on /v1/models. Models
# that classify into image/video/audio/etc. advertise the corresponding output
# modality so clients can filter with ?output_modalities=.
_CLASS_MODALITIES = {
    "image": ["image"],
    "video": ["video"],
    "audio": ["audio"],
    "music": ["audio"],
    "vision": ["image", "text"],
    "edit": ["image"],
    "lora": ["image"],
    "t2v": ["video"],
    "i2v": ["video"],
    "t2a": ["audio"],
}
_VIDEO_CLASSES = {"video", "t2v", "i2v"}
_AUDIO_CLASSES = {"audio", "music", "t2a"}

# Async ComfyUI video-generation jobs (OpenRouter /v1/videos compatible).
VIDEO_KEY = "__video__"
VIDEO_JOBS_FILE = os.path.join(CACHE_DIR, "video-jobs.json")
VIDEO_JOBS_DIR = os.path.join(CACHE_DIR, "video-jobs")
_VIDEO_JOBS = {}
_VIDEO_JOB_ID_CTR = 0
VIDEO_JOB_MAX = 200
VIDEO_JOB_TTL = 3600


def load_classifier():
    """Compile the ComfyUI model-classifier regexes (category -> [regex])."""
    global _classifier_cache
    if _classifier_cache is not None:
        return _classifier_cache
    compiled = {}
    if os.path.exists(CLASSIFIER_FILE):
        try:
            with open(CLASSIFIER_FILE) as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for ctype, patterns in raw.items():
                    regs = []
                    for p in patterns or []:
                        try:
                            regs.append(re.compile(p, re.IGNORECASE))
                        except re.error as e:
                            log.warning(f"classifier: bad regex {p!r} in '{ctype}': {e}")
                    if regs:
                        compiled.setdefault(ctype, []).extend(regs)
        except Exception as e:
            log.warning(f"classifier: failed to load {CLASSIFIER_FILE}: {e}")
    _classifier_cache = compiled
    return compiled


def classify_model(model):
    """Return the classifier category for a model path (first matching regex)."""
    compiled = load_classifier()
    for ctype, regs in compiled.items():
        if any(r.search(model) for r in regs):
            return ctype
    return None


def model_modalities(model):
    """Derive OpenRouter-style output modalities for a model path."""
    ctype = classify_model(model)
    if ctype and ctype in _CLASS_MODALITIES:
        return list(_CLASS_MODALITIES[ctype])
    if ctype == "other" or ctype is None:
        return []
    # any recognized-but-unmapped category defaults to text
    return ["text"]


def load_settings():
    """Apply persisted runtime settings (workers/timeout/min_count/local) over
    the CLI defaults, so changes made in the dashboard survive restarts."""
    global WORKER_COUNT, TIMEOUT, MIN_COUNT, ADMIN_PW, _LOCAL
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
    except Exception:
        return
    if isinstance(s.get("workers"), int) and s["workers"] > 0:
        WORKER_COUNT = s["workers"]
    if isinstance(s.get("timeout"), int) and s["timeout"] > 0:
        TIMEOUT = s["timeout"]
    if isinstance(s.get("min_count"), int) and s["min_count"] >= 0:
        MIN_COUNT = s["min_count"]
    if isinstance(s.get("admin_pw"), str):
        ADMIN_PW = s["admin_pw"]
    if isinstance(s.get("local"), bool):
        _LOCAL = s["local"]


def save_settings(extra=None):
    # merge into the existing file so unrelated keys survive
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.update({"workers": WORKER_COUNT, "timeout": TIMEOUT,
                 "min_count": MIN_COUNT, "local": _LOCAL,
                 "admin_pw": ADMIN_PW})
    if isinstance(extra, dict):
        data.update(extra)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _stored_sources():
    """Raw sources list as stored (for the editor), before validation."""
    if not os.path.exists(SETTINGS_FILE):
        return []
    try:
        with open(SETTINGS_FILE) as f:
            cfg = json.load(f)
    except Exception:
        return []
    src = cfg.get("sources") if isinstance(cfg, dict) else None
    return src if isinstance(src, list) else []


def _config_sources():
    """Additional user-configured fetch sources from settings.json.
    Each entry: {"name", "url", "mapping"} where mapping maps a target field to
    {"field": <source field>} (copy from the row) or {"value": <constant>}.
    Only JSON-list sources are supported here; CSV / transform-heavy sources
    stay built-in. Configured sources are fetched FIRST and take precedence."""
    if not os.path.exists(SETTINGS_FILE):
        return []
    try:
        with open(SETTINGS_FILE) as f:
            cfg = json.load(f)
    except Exception:
        return []
    src = cfg.get("sources") if isinstance(cfg, dict) else None
    return [s for s in src if isinstance(s, dict) and s.get("url")] if isinstance(src, list) else []


def _source_slug(s):
    return re.sub(r'[^a-z0-9]+', '-', str(s or '').lower()).strip('-') or 'source'


def _normalize_url(u):
    """Imply https:// when no scheme is given (e.g. '9ol.es/x.json')."""
    u = str(u or "").strip()
    if u and not re.match(r'^[a-z][a-z0-9+.-]*://', u, re.I):
        u = "https://" + u
    return u


SOURCE_DEF_EXAMPLE = """expected a JSON list of source *definitions*, e.g.

[{
  "name": "graflex",
  "url": "https://9ol.es/graflex-4a8621dd9470.json",
  "mapping": {
    "server":  {"field": "url"},
    "models":  {"field": "models"},
    "version": {"field": "version"},
    "service": {"value": "ollama"}
  }
}]

Each definition needs a "url" (where the host rows live) and a "mapping" that
says which field of each row holds what. A mapping entry is either
{"field": "<row key>"} to copy from the row or {"value": "<constant>"}, and
"server" (or "url") is required so hosts can be keyed."""


def validate_source_defs(defs):
    """Raise ValueError unless every entry is a source *definition*
    ({name, url, mapping}) rather than a host row. Pointing --source add at a
    host list (rows that happen to carry a 'url') would otherwise fill
    settings.json with junk sources that yield nothing."""
    problems = []
    for i, d in enumerate(defs):
        where = f"[{i}]"
        if not isinstance(d, dict):
            problems.append(f"{where} is {type(d).__name__}, not an object")
            continue
        name = d.get("name")
        if name:
            where = f"[{i}] ({name})"
        if not str(d.get("url") or "").strip():
            problems.append(f"{where} has no \"url\"")
        m = d.get("mapping")
        if m is None:
            problems.append(f"{where} has no \"mapping\" — is this a list of hosts "
                            f"rather than a list of sources?")
            continue
        if not isinstance(m, dict) or not m:
            problems.append(f"{where} \"mapping\" must be a non-empty object")
            continue
        if not (m.get("server") or m.get("url")):
            problems.append(f"{where} \"mapping\" needs a \"server\" (or \"url\") entry, "
                            f"got: {', '.join(sorted(m.keys()))}")
        for target, spec in m.items():
            if not isinstance(spec, dict) or not ("field" in spec or "value" in spec):
                problems.append(f'{where} mapping.{target} must be '
                                f'{{"field": "..."}} or {{"value": "..."}}')
    if problems:
        raise ValueError("\n".join(["  - " + p for p in problems]) + "\n\n" + SOURCE_DEF_EXAMPLE)


def fetch_source_defs(url):
    """Fetch a URL that returns a JSON list of source definitions, the same way
    the dashboard's "add from URL" button does. A bare dict is treated as a
    one-element list. Raises ValueError on anything else."""
    url = _normalize_url(url)
    if not url:
        raise ValueError("url required")
    data = json.loads(requests.get(url, timeout=20).text)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("url did not return a JSON list.\n\n" + SOURCE_DEF_EXAMPLE)
    if not data:
        raise ValueError("url returned an empty list.\n\n" + SOURCE_DEF_EXAMPLE)
    validate_source_defs(data)
    return data


def add_sources_from_url(url):
    """Append the source definitions found at `url` to settings.json.
    Definitions whose url is already configured are skipped.
    Returns (added, skipped)."""
    defs = fetch_source_defs(url)
    existing = _stored_sources()
    have = {_normalize_url(s.get("url")) for s in existing if isinstance(s, dict)}
    added, skipped = [], []
    for d in defs:
        u = _normalize_url(d.get("url"))
        if u in have:
            skipped.append(d)
            continue
        have.add(u)
        added.append(d)
    if added:
        save_settings({"sources": existing + added})
    return added, skipped


def _source_line(s):
    name = s.get("name") or "(unnamed)"
    mapped = ", ".join(sorted(s.get("mapping").keys())) if isinstance(s.get("mapping"), dict) else ""
    line = f"  {name}\n      url:     {_normalize_url(s.get('url'))}"
    if mapped:
        line += f"\n      mapping: {mapped}"
    return line


def source_cli(argv):
    """Handle `--source list` / `--source add <url>`. Returns an exit code."""
    load_settings()   # so save_settings() doesn't clobber persisted values
    cmd = str(argv[0]).lower()
    if cmd == "list":
        srcs = _stored_sources()
        if not srcs:
            print("No additional sources configured.")
            return 0
        print(f"{len(srcs)} additional source{'' if len(srcs) == 1 else 's'} "
              f"(from {SETTINGS_FILE}):")
        for s in srcs:
            print(_source_line(s) if isinstance(s, dict) else f"  {s!r}  (not a source object)")
        return 0

    if cmd == "add":
        urls = argv[1:]
        if not urls:
            print("usage: dyva --source add <url>", file=sys.stderr)
            return 1
        rc, total = 0, 0
        for url in urls:
            try:
                added, skipped = add_sources_from_url(url)
            except Exception as ex:
                print(f"{url}: {ex}", file=sys.stderr)
                rc = 1
                continue
            total += len(added)
            print(f"{url}: added {len(added)}, skipped {len(skipped)} (already configured)")
            for s in added:
                print(_source_line(s))
        if total:
            print("Refreshing cache...")
            refresh_cache()
        return rc

    print(f"unknown --source command '{cmd}' (expected 'list' or 'add')", file=sys.stderr)
    return 1


def _apply_source_mapping(row, mapping):
    """Turn a raw source row into a host entry per the mapping."""
    out = {}
    if isinstance(mapping, dict):
        for target, spec in mapping.items():
            if not isinstance(spec, dict):
                continue
            if "value" in spec:
                out[target] = spec["value"]
            elif "field" in spec:
                val = row.get(spec["field"])
                # Leave a missing field unset (not None) so downstream
                # setdefault()s apply — e.g. service falls back to 'ollama'
                # instead of becoming None and never classifying as a1111/comfyui.
                if val is not None:
                    out[target] = val
    return out

TRIAL_IMG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
TRIAL_SEE_RE = re.compile(r"\bred\b", re.I)

_status_db = None
_servers_cache = None

_activity_queues = []

GITHUB_URL = "https://github.com/kristopolous/free-ollama"
_activity_history = []
_activity_lock = asyncio.Lock()
_ACTIVITY_HISTORY_MAX = 500


async def broadcast_activity(host, model, status, message, duration=None, wid=None):
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


def refresh_cache(source=None):
    global _servers_cache
    _servers_cache = None

    os.makedirs(CACHE_DIR, exist_ok=True)
    log.debug("Refreshing server cache...")
    _db = f'{CACHE_DIR}/free-ollama.json'

    extra_sources = _config_sources()
    downloads = [
       (_normalize_url(s.get("url")), f"{_db}-extra-{_source_slug(s.get('name'))}.tmp", str(s.get('name') or '')) for s in extra_sources
    ] + [
       ( 'https://raw.githubusercontent.com/forrany/Awesome-Ollama-Server/refs/heads/main/public/data.json', f"{_db}-forrany.tmp", 'forrany' ),
       ( 'https://raw.githubusercontent.com/PuddinCat/OllamaSpider/refs/heads/main/url_models.json', f"{_db}-spider.tmp", 'spider' ),
       ( 'https://raw.githubusercontent.com/happyshua/ollamalist/refs/heads/main/output_with_models.csv', f"{_db}-happyshua.tmp", 'happyshua' ),
    ]
    if source:
        want = _source_slug(source)
        matched = [d for d in downloads if _source_slug(d[2]) == want]
        if not matched:
            log.error(f"unknown source '{source}' (available: {', '.join(d[2] for d in downloads)})")
            return False
        downloads = matched
        log.info(f"Refreshing only source '{matched[0][2]}'")
    for url, loc, _ in downloads:
      try:
        response = requests.get(url)
        logging.info(f"Grabbing {url}")
        # If this fails we try to use the existing one
        with open(loc, "w") as f:
          f.write(response.text)
      except Exception as ex:
        logging.warning(f"Unable to get {url}: {ex}")


    host_map = {}

    # Additional (user-configured) sources are parsed FIRST so they take
    # precedence; every source below only fills hosts not already claimed.
    for s in extra_sources:
      loc = f"{_db}-extra-{_source_slug(s.get('name'))}.tmp"
      if not os.path.exists(loc):
        continue
      try:
        with open(loc) as f:
          data = json.loads(f.read())
      except Exception as ex:
        logging.warning(f"Unable to parse {loc}: {ex}")
        continue
      if not isinstance(data, list):
        continue
      mapping = s.get('mapping', {})
      name = s.get('name', 'source')
      for row in data:
        if not isinstance(row, dict):
          continue
        entry = _apply_source_mapping(row, mapping)
        ip = str(entry.get('server') or entry.get('url') or '').rstrip('/')
        if not ip:
          continue
        entry['server'] = ip
        entry.setdefault('source', name)
        entry.setdefault('version', '')
        entry.setdefault('service', 'ollama')
        if not isinstance(entry.get('models'), list):
          entry['models'] = []
        if ip not in host_map:
          host_map[ip] = entry

    if os.path.exists(f'{_db}-forrany.tmp'):
      with open(f'{_db}-forrany.tmp', 'r') as f:
        try:
          for row in json.loads(f.read()):
            if row.get('server') not in host_map:
                row['source'] = 'forrany'
                host_map[row.get('server')] = row
        except Exception as ex:
          logging.warning(f"Unable to parse {_db}-forrany.tmp: {ex}")

    if os.path.exists(f"{_db}-happyshua.tmp"):
      with open(f"{_db}-happyshua.tmp", 'r') as csvfile:
        for r in csv.reader(csvfile):
          ip = re.sub(r'/?v1', '', r[0])
          models = [m.strip() for m in r[1].split(',')]
          if ip not in host_map:
            host_map[ip] = {'source': 'happyshua', 'models': [], 'server': ip, 'version': ''}
    
          host_map[ip]['models'] += models

    if os.path.exists(f"{_db}-spider.tmp"):
      with open(f'{_db}-spider.tmp', 'r') as f:
        for row in json.loads(f.read()):
          ip = re.sub(r'/?v1', '', row.get('url'))
          models = [ n.get('name') for n in row.get('models') ]
          if ip not in host_map:
            host_map[ip] = {'source': 'spider', 'models': [], 'server': ip, 'version': ''}

          host_map[ip]['models'] += models


    for k,v in host_map.items():
      if 'service' not in v:
        v['service'] = 'ollama'
      try:
        v['models'] = list(set(v.get('models') or []))
      except TypeError:
        v['models'] = list(v.get('models') or [])

    with open(_db, 'w') as f:
      json.dump(list(host_map.values()), f)

    by_source = {}
    for v in host_map.values():
        by_source[v.get('source') or 'unknown'] = by_source.get(v.get('source') or 'unknown', 0) + 1
    total = sum(by_source.values())
    log.info(f"cache survey ({total} hosts by source): " + ", ".join(
        f"{src}={n}" for src, n in sorted(by_source.items(), key=lambda x: -x[1])
    ))

    return True


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
            _servers_cache = []
        else:
            with open(CACHE_FILE) as f:
                _servers_cache = json.load(f)
    return _servers_cache or []


# Host reputation lives in a SQLite table (host, model) -> state + metadata.
# SQLite gives real record UPDATEs (last_good, failure_streak) plus WAL-mode
# crash-safety, which a rewritten JSON blob can't. Tiers, best-first:
#   recent (last-success) -> good -> maybe_good -> unknown (absent) -> bad
# A failure downgrades a good host to maybe_good; maybe_good currently stays put
# (secondary downgrade strategy TBD once real-world data is collected).
def _get_db():
    global _status_db
    if _status_db is None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        need_migrate = not os.path.exists(STATUS_DB)
        _status_db = sqlite3.connect(STATUS_DB, check_same_thread=False)
        _status_db.execute("PRAGMA journal_mode=WAL")
        _status_db.execute("PRAGMA synchronous=NORMAL")
        _status_db.execute(
            "CREATE TABLE IF NOT EXISTS host_status("
            "host TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL, "
            "last_good TEXT, failure_streak INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(host, model))")
        _status_db.commit()
        if need_migrate:
            _migrate_status_from_txt()
    return _status_db


def _migrate_status_from_txt():
    """One-time import of the legacy good/bad .txt line files. The .txt files
    are left untouched for the legacy free-ollama bash tool; dyva uses the DB."""
    db = _status_db

    def _lines(path, state):
        if not os.path.exists(path):
            return
        with open(path) as f:
            for line in f:
                key = line.strip()
                if not key:
                    continue
                host, _, model = key.partition(" ")
                yield (host, model, state)

    # good first so a host listed in both keeps good (good beats bad)
    for row in _lines(GOOD_FILE, "good"):
        db.execute("INSERT OR IGNORE INTO host_status(host,model,state,last_good,"
                   "failure_streak) VALUES(?,?,?,NULL,0)", row)
    for row in _lines(BAD_FILE, "bad"):
        db.execute("INSERT OR IGNORE INTO host_status(host,model,state,last_good,"
                   "failure_streak) VALUES(?,?,?,NULL,0)", row)
    db.commit()


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _state_of(host, model):
    row = _get_db().execute(
        "SELECT state FROM host_status WHERE host=? AND model=?",
        (host, model)).fetchone()
    return row[0] if row else None


def _keys_with_state(state):
    return {f"{h} {m}" for h, m in _get_db().execute(
        "SELECT host, model FROM host_status WHERE state=?", (state,)).fetchall()}


def load_bad():
    return _keys_with_state("bad")


def load_good():
    return _keys_with_state("good")


def load_maybe():
    return _keys_with_state("maybe_good")


def add_good(host, model):
    """Successful inference: host is good, stamp last_good, reset failure_streak.
    Also clears any host-wide unreachable mark, since the host clearly answered."""
    model = canon_pattern(model)
    db = _get_db()
    db.execute(
        "INSERT INTO host_status(host,model,state,last_good,failure_streak)"
        " VALUES(?,?, 'good', ?, 0)"
        " ON CONFLICT(host,model) DO UPDATE SET"
        " state='good', last_good=excluded.last_good, failure_streak=0",
        (host, model, _now_iso()))
    db.execute("DELETE FROM host_status WHERE host=? AND model=?", (host, UNREACHABLE_KEY))
    db.commit()


def add_bad(host, model):
    """Failure: good -> maybe_good, maybe_good stays, unknown -> bad, bad stays.
    failure_streak is incremented in every case (recorded for later analysis;
    no secondary downgrade action is taken yet)."""
    model = canon_pattern(model)
    db = _get_db()
    cur = _state_of(host, model)
    if cur is None:
        new_state = "bad"
    elif cur == "good":
        new_state = "maybe_good"
    else:
        new_state = cur
    db.execute(
        "INSERT INTO host_status(host,model,state,last_good,failure_streak)"
        " VALUES(?,?,?,NULL,1)"
        " ON CONFLICT(host,model) DO UPDATE SET"
        " state=?, failure_streak=failure_streak+1",
        (host, model, new_state, new_state))
    db.commit()


def force_bad(host, model):
    """Explicit user skip: send straight to bad regardless of current tier."""
    model = canon_pattern(model)
    db = _get_db()
    db.execute(
        "INSERT INTO host_status(host,model,state,last_good,failure_streak)"
        " VALUES(?,?, 'bad', NULL, 0)"
        " ON CONFLICT(host,model) DO UPDATE SET state='bad'",
        (host, model))
    db.commit()


def clear_bad_state():
    db = _get_db()
    db.execute("DELETE FROM host_status WHERE state='bad'")
    db.commit()


def mark_unreachable(host):
    """Record a host as unreachable at the connection level (dead for all
    models). Recorded once per host, not per model, so it isn't re-probed for
    every distinct model query."""
    db = _get_db()
    db.execute(
        "INSERT INTO host_status(host,model,state,last_good,failure_streak)"
        " VALUES(?,?, 'bad', NULL, 1)"
        " ON CONFLICT(host,model) DO UPDATE SET state='bad', failure_streak=failure_streak+1",
        (host, UNREACHABLE_KEY))
    db.commit()


def load_unreachable():
    return {h for (h,) in _get_db().execute(
        "SELECT host FROM host_status WHERE model=? AND state='bad'",
        (UNREACHABLE_KEY,)).fetchall()}


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
    model = canon_pattern(model)
    if _last_cache is None:
        load_last()
    entry = _last_cache.get(model)
    if entry:
        return (entry["host"], entry["full"])
    return None


def set_last(model, host, full):
    model = canon_pattern(model)
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
        return fnmatch.fnmatch(model_name.lower(), f"*{pattern.lower()}*")
    return pattern.lower() in model_name.lower()


def canon_pattern(pattern):
    """Collapse equivalent model queries to one canonical form so good/bad/last
    state is shared: 'GEMMA*', 'gemma', '*gemma*' all become 'gemma';
    '*', '**' and '' all become '' (= any). Safe because match_model() wraps
    patterns in *...* anyway, so edge stars never change the match set."""
    return (pattern or "").strip().lower().strip("*").strip()


def needs_caps(messages):
    """Scan a conversation's messages for required model capabilities."""
    caps = {"completion"}
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("images"):
            caps.add("vision")
        if m.get("tool_calls"):
            caps.add("tools")
        if m.get("role") == "tool":
            caps.add("tools")
        if m.get("audio"):
            caps.add("audio")
        content = m.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    t = blk.get("type")
                    if t in ("image_url", "file"):
                        caps.add("vision")
                    elif t in ("audio", "input_audio"):
                        caps.add("audio")
    return sorted(caps)


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


def load_knowns():
    global _knowns_cache
    if _knowns_cache is None:
        _knowns_cache = {}
        if os.path.exists(KNOWN_FILE):
            try:
                with open(KNOWN_FILE) as f:
                    _knowns_cache = json.load(f)
            except Exception:
                _knowns_cache = {}
    return _knowns_cache


def save_knowns():
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(KNOWN_FILE, "w") as f:
        json.dump(_knowns_cache, f, indent=2)


def _knowns_refresh_due(host):
    entry = load_knowns().get(host)
    return not entry or not entry.get("refreshed") or time.time() - entry["refreshed"] > CAP_REFRESH_TTL


async def refresh_host_caps(session, host):
    """Pull {host}/api/tags and store per-model capabilities in known-hosts.json."""
    if not _knowns_refresh_due(host):
        return
    try:
        resp = await asyncio.wait_for(session.get(f"{host}/api/tags"), timeout=TIMEOUT)
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
        return
    if resp.status != 200:
        await resp.release()
        return
    try:
        data = await resp.json()
    except Exception:
        await resp.release()
        return
    await resp.release()
    prev = (load_knowns().get(host) or {}).get("models", {})
    models = {}
    for m in data.get("models") or []:
        name = m.get("name")
        if not name:
            continue
        models[name] = sorted(set((m.get("capabilities") or [])) | set(prev.get(name) or []))
    if not models:
        return
    load_knowns()[host] = {"refreshed": time.time(), "models": models}
    save_knowns()


def _known_has(host, model, cap):
    caps_map = (load_knowns().get(host) or {}).get("models", {})
    return cap in (caps_map.get(model) or [])


def _mark_known(host, model, caps_list):
    load_knowns().setdefault(host, {}).setdefault("models", {})[model] = caps_list
    save_knowns()


def mark_vision(host, model):
    caps_map = (load_knowns().get(host) or {}).get("models", {})
    cur = caps_map.get(model) or []
    if "vision" not in cur:
        _mark_known(host, model, sorted(set(cur) | {"vision"}))


def mark_no_vision(host, model):
    caps_map = (load_knowns().get(host) or {}).get("models", {})
    cur = caps_map.get(model) or []
    new_caps = sorted(set(cur) - {"vision"}) or ["completion"]
    if new_caps != sorted(cur):
        _mark_known(host, model, new_caps)


async def trial_balloon(session, host, full, model):
    """Cheap vision probe: send a 1x1 red pixel with no prompt.

    A model that actually processed the image says it's red; a model whose
    server dropped the image either admits nothing arrived or hallucinates
    something else. Returns True (sees), False (no vision), or None (error).
    """
    payload = {
        "model": full,
        "messages": [{"role": "user", "images": [TRIAL_IMG]}],
        "stream": False,
    }
    start = time.time()
    _curlify("POST", f"{host}/api/chat", payload)
    try:
        resp = await asyncio.wait_for(session.post(f"{host}/api/chat", json=payload), timeout=TIMEOUT)
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
        return None
    if resp.status != 200:
        await resp.release()
        return None
    try:
        data = await resp.json()
    except Exception:
        await resp.release()
        return None
    await resp.release()
    content = (data.get("message") or {}).get("content") or ""
    sees = TRIAL_SEE_RE.search(content) is not None
    await broadcast_activity(host, model, "trial",
        f"trial balloon {full}: {'sees red' if sees else 'no vision'}", duration=time.time() - start)
    return sees


def _model_capable(known_caps, required):
    if not known_caps:
        return False
    return all(r in known_caps for r in required)


def _known_capable(host, model, caps):
    if not caps:
        return True
    caps_map = (load_knowns().get(host) or {}).get("models", {})
    known = caps_map.get(model)
    if not known:
        return True
    return _model_capable(known, caps)


def _checked_rank(s):
    """Sort key for the unknown tier: most-recently-checked first. Returns the
    negated epoch of the entry's `checked` timestamp (so newer sorts earlier);
    hosts with no/invalid timestamp fall to the back (inf)."""
    ck = s.get("checked")
    if not ck:
        return float("inf")
    try:
        return -datetime.datetime.fromisoformat(ck).timestamp()
    except Exception:
        return float("inf")


def find_servers(sub, caps=None):
    if '/' in sub:
        res = []
        for model in sub.split('/'):
            res += find_servers(model, caps)
        return res

    sub = canon_pattern(sub)
    knowns = load_knowns() if caps else {}
    servers = load_servers()
    bad = load_bad()
    good = load_good()
    maybe = load_maybe()
    unreachable = load_unreachable()
    # For "any" (empty sub) queries, collapse each host's per-model marks into a
    # single best-first state so the host inherits everything dyva knows about it.
    host_state = None
    if not sub:
        rank = {"good": 3, "maybe_good": 2, "bad": 1}
        host_state = {}
        for src, st in ((good, "good"), (maybe, "maybe_good"), (bad, "bad")):
            for k in src:
                h = k.split(" ", 1)[0]
                if rank[st] > rank.get(host_state.get(h, ""), 0):
                    host_state[h] = st
    matched = []
    for s in servers:
        if s.get("service") in ("comfyui", "a1111"):
            continue
        models = s.get("models", [])
        ms = [m for m in models if match_model(m, sub)]
        if not ms:
            continue
        if any(re.search(r"[:-]cloud", m) for m in ms) and not re.search(r"[:-]cloud", sub):
            continue
        host = s.get("server", "")
        if caps:
            caps_map = (knowns.get(host) or {}).get("models", {})
            known = [(m, caps_map[m]) for m in ms if caps_map.get(m)]
            capable = [m for m, c in known if _model_capable(c, caps)]
            incapable = [m for m, c in known if not _model_capable(c, caps)]
            if incapable and len(incapable) == len(ms):
                continue
            unknown = [m for m in ms if not caps_map.get(m)]
            if capable or unknown:
                ms = capable + unknown + incapable
        key = f"{host} {sub}"
        if sub:
            # Reputation is keyed by the exact model that succeeded (e.g.
            # "qwen3-vl:8b"), not the broad query ("qwen"). So a pattern query
            # must also honor marks on the concrete models it resolves to (ms) —
            # otherwise a host proven good at "qwen2.5:7b" looks "unknown" to a
            # "qwen" query and gets probed cold alongside the dead ones.
            keys = [key] + [f"{host} {canon_pattern(m)}" for m in ms]
            in_good = any(k in good for k in keys)
            in_maybe = any(k in maybe for k in keys)
            in_bad = any(k in bad for k in keys)
        else:
            st = host_state.get(host)
            in_good = st == "good"
            in_maybe = st == "maybe_good"
            in_bad = st == "bad"
        # Priority tiers, best first:
        #   recent (last success) -> good -> maybe_good -> unknown -> bad
        # last/good/maybe win over a stale bad mark so a host that had one
        # transient failure isn't buried behind the unreachable junk.
        _last = get_last(sub)
        if _last is None and not sub:
            if _last_cache is None:
                load_last()
            _last = next(
                ((v["host"], v.get("full", "")) for v in _last_cache.values()
                 if v.get("host") == host),
                None)
        is_last = _last is not None and host == _last[0]
        if host in unreachable:
            # dead at the connection level -> bad tier for every model, so it
            # isn't re-probed as "unknown" for each new model query
            prio = 1
        elif is_last:
            prio = -3
        elif in_good:
            prio = -2
        elif in_maybe:
            prio = -1
        elif in_bad:
            prio = 1
        else:
            prio = 0
        # Within the UNKNOWN tier only, try the most-recently-checked hosts
        # first (recently reachable => likelier still up). Other tiers keep
        # their existing order via a constant secondary key (stable sort).
        crank = _checked_rank(s) if prio == 0 else 0
        matched.append((prio, crank, host, ms))
    matched.sort(key=lambda x: (x[0], x[1]))
    return [(p, h, m) for p, c, h, m in matched]


def all_models():
    servers = load_servers()
    seen = {}
    for s in servers:
        if s.get("service") in ("comfyui", "a1111"):
            continue
        for m in s.get("models", []):
            if re.search(r"[:-]cloud", m) or len(m) == 0:
                continue
            if m not in seen:
                seen[m] = {'id': m, 'count': 1}
            else:
                seen[m]['count'] += 1
    for m in seen.values():
        m["modalities"] = model_modalities(m["id"])
    if True: #int(time.time() % 2) == 0:
        sorty = sorted(seen.values(), key=lambda x: x.get('count'))
    else:
        sorty = sorted(seen.values(), key=lambda x: x.get('id').lower())
    return list(sorty)


def listed_models():
    """Models for third-party enumeration (/api/tags, /v1/models), with the
    optional MIN_COUNT floor applied. The dashboard uses all_models() directly
    and always shows everything."""
    models = all_models()
    if MIN_COUNT > 1:
        models = [m for m in models if m.get('count', 0) >= MIN_COUNT]
    return models


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
        tcs = m.get("tool_calls")
        if tcs:
            out = []
            for tc in tcs:
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        pass
                out.append({"function": {"name": fn.get("name", ""), "arguments": args}})
            m["tool_calls"] = out
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
        msg["content"] = None
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


def _host_url(host, path):
    """Build a URL from a host string that may or may not include a scheme."""
    if re.match(r"^https?://", host):
        return f"{host.rstrip('/')}{path}"
    return f"http://{host}{path}"


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


async def _race_servers(session, model, servers, payload, do_stream, endpoint="/api/chat", remote=None, caps=None):
    done = asyncio.Event()
    result_queue = asyncio.Queue()
    errors = []
    errors_lock = asyncio.Lock()
    server_iter = iter(servers)
    iter_lock = asyncio.Lock()

    async def _collect_err(msg):
        async with errors_lock:
            errors.append(msg)

    async def worker():
        resp = None
        while not done.is_set():
            async with iter_lock:
                try:
                    prio, host, ms = next(server_iter)
                except StopIteration:
                    return

            full = ms[0]

            wid = asyncio.current_task().get_name()
            await broadcast_activity(host, model, "trying",
                f"trying: {host} for {model}", wid=wid)

            # last-used / known-good hosts go straight to the request;
            # untested ones get probed first, caps refreshed only if needed
            trusted = prio < 0
            if not trusted and not await probe_host(session, host):
                mark_unreachable(host)
                await broadcast_activity(host, model, "failed",
                    f"unreachable: {host}")
                continue
            if not trusted and caps and caps != {"completion"}:
                await refresh_host_caps(session, host)

            if "vision" in (caps or []) and not _known_has(host, full, "vision"):
                sees = await trial_balloon(session, host, full, model)
                if sees is None:
                    continue
                if sees:
                    mark_vision(host, full)
                    set_last(model, host, full)
                    await broadcast_activity(host, model, "trying",
                        f"trial balloon: {full} has vision", wid=wid)
                else:
                    mark_no_vision(host, full)
                    await broadcast_activity(host, model, "failed",
                        f"trial balloon: {full} has no vision", wid=wid)
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
                add_bad(host, model)
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - {type(e).__name__}", duration=dur, wid=wid)
                continue

            if resp.status != 200:
                dur = time.time() - start
                code = resp.status
                try:
                    raw = await resp.read()
                    body = raw.decode('utf-8', errors='replace')[:500]
                    log_upstream(code, host, endpoint, body, remote=remote)
                    await _collect_err(f"{host}: {body}")
                except Exception:
                    pass
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
                    await _collect_err(f"{host}: {data['error']}")
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
                await _collect_err(f"{host}: {first['error']}")
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

    tasks = [asyncio.create_task(worker()) for _ in range(min(WORKER_COUNT, len(servers)))]

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

    return result, errors


async def _try_one(session, host, model, full_model, opayload, remote=None):
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
        return None, None

    if resp.status != 200:
        dur = time.time() - start
        err_msg = None
        try:
            raw = await resp.read()
            err_msg = raw.decode('utf-8', errors='replace')[:500]
            log_upstream(resp.status, host, "/api/chat", err_msg, remote=remote)
        except Exception:
            pass
        await resp.release()
        log.debug(f"  \u2717 {tag}  (status {resp.status})")
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - status {resp.status}", duration=dur, wid=wid)
        return None, err_msg

    try:
        data = await resp.json()
    except asyncio.TimeoutError:
        await resp.release()
        return None, None
    except json.JSONDecodeError:
        await resp.release()
        dur = time.time() - start
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - bad response", duration=dur, wid=wid)
        return None, None
    await resp.release()

    if "error" in data:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (error: {data['error']})")
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - error: {data['error']}", duration=dur, wid=wid)
        return None, data["error"]

    dur = time.time() - start
    log.debug(f"  \u2713 {tag}")
    set_last(model, host, full_model)
    add_good(host, model)
    await broadcast_activity(host, model, "connected",
        f"success: {host} for {model}", duration=dur, wid=wid)
    return data, None


async def _try_host(session, host, full_model, model, payload, do_stream, endpoint="/api/chat", remote=None):
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
        return None, None

    if resp.status != 200:
        dur = time.time() - start
        err_msg = None
        try:
            raw = await resp.read()
            err_msg = raw.decode('utf-8', errors='replace')[:500]
            log_upstream(resp.status, host, endpoint, err_msg, remote=remote)
        except Exception:
            pass
        await resp.release()
        log.debug(f"  \u2717 {tag}  (status {resp.status})")
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - status {resp.status}", duration=dur, wid=wid)
        return None, err_msg

    try:
        it = resp.content
        first_line = await it.readline()
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
        dur = time.time() - start
        await resp.release()
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - {type(e).__name__}", duration=dur, wid=wid)
        return None, None

    if not first_line or not first_line.strip():
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (empty response)")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - empty response", duration=dur, wid=wid)
        return None, None
    try:
        first = json.loads(first_line)
    except json.JSONDecodeError:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (bad response)")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - bad response", duration=dur, wid=wid)
        return None, None

    if "error" in first:
        dur = time.time() - start
        log.debug(f"  \u2717 {tag}  (error: {first['error']})")
        await resp.release()
        add_bad(host, model)
        await broadcast_activity(host, model, "failed",
            f"failure: {host} for {model} - error: {first['error']}", duration=dur, wid=wid)
        return None, first["error"]

    dur = time.time() - start
    log.debug(f"  \u2713 {tag}")
    set_last(model, host, full_model)
    add_good(host, model)
    await broadcast_activity(host, model, "connected",
        f"success: {host} for {model}", duration=dur, wid=wid)
    return (resp, first_line, first), None


async def _forward_stream(request, response, resp, first_line, host, full, openai_format):
    content_type = "text/event-stream" if openai_format else "application/x-ndjson"
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Dyva-Host"] = re.sub(r"^https?://", "", host)
    response.headers["X-Dyva-Model"] = full
    try:
        await response.prepare(request)
    except (BrokenPipeError, ConnectionResetError, aiohttp.ClientConnectionResetError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
        log.debug("Client disconnected before response headers sent")
        await resp.release()
        return

    try:
        if openai_format:
            first = json.loads(first_line)
            msg = dict(first.get("message", {}))
            tcs = msg.pop("tool_calls", None)
            if tcs:
                msg["tool_calls"] = _fmt_tool_calls(tcs)
                msg["content"] = None
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
                            msg["content"] = None
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


def chat_fmt(data, model, openai_format):
    if openai_format:
        return web.json_response(to_openai(data, model))
    else:
        return web.json_response(data)

DYVA_INFO_TAG = "__dyva_info__"


def _content_contains(payload, needle):
    """True if `needle` appears in the standalone prompt or the LAST message
    only. Deliberately does not scan the whole history: `__dyva_info__` is meant
    to act on the current turn, so a tag stored deep in a past message must not
    keep triggering the diagnostic on every later request."""
    if needle in str(payload.get("prompt") or ""):
        return True
    msgs = payload.get("messages") or []
    if not msgs:
        return False
    m = msgs[-1]
    c = m.get("content")
    if isinstance(c, str) and needle in c:
        return True
    if isinstance(c, list):
        for part in c:
            if isinstance(part, dict) and needle in str(part.get("text") or ""):
                return True
    return False


def _has_info_tag(payload):
    return _content_contains(payload, DYVA_INFO_TAG)


def _info_wants_next(payload):
    """`__dyva_info__:next` — same diagnostic, but first skip the model's sticky
    last-successful host (like the /next-host endpoint) so the reported host is
    the *next* one a real request would land on."""
    return _content_contains(payload, DYVA_INFO_TAG + ":next")


def _norm_host(h):
    return re.sub(r"^https?://", "", h or "")


def _drop_last_host(model):
    """Remove a model's sticky last-successful host so another host is tried.
    Shared by the /next-host endpoint and the `__dyva_info__:next` directive."""
    key = canon_pattern(model)
    if _last_cache is None:
        load_last()
    if key in _last_cache:
        del _last_cache[key]
        save_last()


def _would_use(model_in, req_caps, exclude=None):
    """Resolve which host/model a request would be routed to, without sending it.
    When `exclude` (a host, with or without scheme) is given, that host is passed
    over so the caller sees the next candidate instead."""
    ex = _norm_host(exclude) if exclude else None
    last = get_last(model_in)
    if last and (ex is None or _norm_host(last[0]) != ex) and _known_capable(last[0], last[1], req_caps) and (
            "vision" not in req_caps or _known_has(last[0], last[1], "vision")):
        return {"host": _norm_host(last[0]), "model": last[1]}
    for _prio, host, ms in find_servers(model_in, req_caps):
        if ex is not None and _norm_host(host) == ex:
            continue
        if ms:
            return {"host": _norm_host(host), "model": ms[0]}
    return None


def _info_response(model_in, info, do_stream=False, openai_format=False):
    if not do_stream:
        return web.json_response(info)
    now = time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}Z"
    payload = json.dumps(info)
    if openai_format:
        text = (sse_str(sse_chunk(model_in, {"role": "assistant", "content": payload})) +
                sse_str(sse_chunk(model_in, {}, done=True)))
        return web.Response(text=text, content_type="text/event-stream")
    first = {
        "model": model_in, "created_at": now,
        "message": {"role": "assistant", "content": payload},
        "done": False,
    }
    last = {
        "model": model_in, "created_at": now,
        "message": {"role": "assistant", "content": ""},
        "done": True, "done_reason": "stop",
        "total_duration": 0, "load_duration": 0,
        "prompt_eval_count": 0, "prompt_eval_duration": 0,
        "eval_count": 0, "eval_duration": 0,
        "dyva_info": info,
    }
    return web.Response(
        text=json.dumps(first) + "\n" + json.dumps(last) + "\n",
        content_type="application/x-ndjson")


async def _proxy_chat(request, session, model_in, opayload, do_stream, openai_format):
    if '/' in model_in:
        model_list = model_in.split('/')
    else:
        model_list = [model_in]

    req_caps = needs_caps(opayload.get("messages", []))
    if opayload.get("tools"):
        req_caps = sorted(set(req_caps) | {"tools"})

    if _has_info_tag(opayload):
        exclude = None
        if _info_wants_next(opayload):
            prev = get_last(model_list[0])
            exclude = prev[0] if prev else None
            _drop_last_host(model_list[0])
        info = _would_use(model_list[0], req_caps, exclude=exclude)
        if info is None:
            return web.json_response(err_obj(f"no available servers for '{model_in}'", "model_not_found"), status=404)
        return _info_response(model_list[0], info, do_stream=do_stream, openai_format=openai_format)

    last = get_last(model_list[0])
    last_host_err = None
    if last and _known_capable(last[0], last[1], req_caps) and ("vision" not in req_caps or _known_has(last[0], last[1], "vision")):
        last_host, last_full = last
        log.debug(f"Reusing {last_host} for {model_list[0]}")
        if do_stream:
            result, last_host_err = await _try_host(session, last_host, last_full, model_list[0], opayload, do_stream=True, remote=request.remote)
            if result:
                resp, first_line, first = result
                stream_resp = web.StreamResponse()
                await _forward_stream(request, stream_resp, resp, first_line, last_host, last_full, openai_format)
                return stream_resp
        else:
            data, last_host_err = await _try_one(session, last_host, model_list[0], last_full, opayload, remote=request.remote)
            if data:
                resp = chat_fmt(data, model_list[0], openai_format)
                resp.headers["X-Dyva-Host"] = re.sub(r"^https?://", "", last_host)
                resp.headers["X-Dyva-Model"] = last_full
                return resp

    servers = find_servers(model_in, req_caps)
    if not servers:
        err_msg = f"no available servers for '{model_in}'"
        if last_host_err:
            err_msg += f": {last_host_err}"
        return web.json_response(err_obj(err_msg, "model_not_found"), status=404)

    for model in model_list:
        if do_stream:
            result, errors = await _race_servers(session, model, servers, opayload, do_stream=True, remote=request.remote, caps=req_caps)
            if result:
                _, host, full, resp, first_line, first = result
                stream_resp = web.StreamResponse()
                await _forward_stream(request, stream_resp, resp, first_line, host, full, openai_format)
                return stream_resp
            msg = "all servers failed"
            if errors:
                msg += ": " + "; ".join(dict.fromkeys(errors))
            if openai_format:
                return web.Response(
                    text=sse_str({"error": msg}) + sse_str(sse_chunk("", {}, done=True)),
                    content_type="text/event-stream",
                )
        else:
            result, errors = await _race_servers(session, model, servers, dict(opayload, stream=False), do_stream=False, remote=request.remote, caps=req_caps)

            if result:
                _, host, full, data = result
                resp = chat_fmt(data, model, openai_format)
                resp.headers["X-Dyva-Host"] = re.sub(r"^https?://", "", host)
                resp.headers["X-Dyva-Model"] = full
                return resp

    msg = "all servers failed"
    if errors:
        msg += ": " + "; ".join(dict.fromkeys(errors))
    return web.json_response(err_obj(msg), status=502)


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

    req_caps = needs_caps(body.get("messages", []))
    if body.get("images"):
        req_caps = sorted(set(req_caps) | {"vision"})
    if body.get("tools"):
        req_caps = sorted(set(req_caps) | {"tools"})

    if _has_info_tag(body):
        exclude = None
        if _info_wants_next(body):
            prev = get_last(model)
            exclude = prev[0] if prev else None
            _drop_last_host(model)
        info = _would_use(model, req_caps, exclude=exclude)
        if info is None:
            return web.json_response(err_obj(f"no available servers for '{model}'", "model_not_found"), status=404)
        return _info_response(model, info, do_stream=do_stream)

    last = get_last(model)
    last_host_err = None
    if last and _known_capable(last[0], last[1], req_caps) and ("vision" not in req_caps or _known_has(last[0], last[1], "vision")):
        last_host, last_full = last
        log.debug(f"Reusing {last_host} for {model}")
        if do_stream:
            result, last_host_err = await _try_host(session, last_host, last_full, model, body, do_stream=True, endpoint=endpoint, remote=request.remote)
            if result:
                resp, first_line, first = result
                stream_resp = web.StreamResponse()
                await _forward_stream(request, stream_resp, resp, first_line, last_host, last_full, openai_format=False)
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
                    if data and "error" in data:
                        last_host_err = data["error"]
                    await broadcast_activity(last_host, model, "failed",
                        f"failure: {last_host} for {model} - bad response", duration=dur, wid=wid)
                else:
                    dur = time.time() - start
                    try:
                        raw = await r.read()
                        last_host_err = raw.decode('utf-8', errors='replace')[:500]
                        log_upstream(r.status, last_host, endpoint, last_host_err, remote=request.remote)
                    except Exception:
                        pass
                    await r.release()
                    add_bad(last_host, model)
                    await broadcast_activity(last_host, model, "failed",
                        f"failure: {last_host} for {model} - status {r.status}", duration=dur, wid=wid)

    servers = find_servers(model, req_caps)
    if not servers:
        err_msg = f"no available servers for '{model}'"
        if last_host_err:
            err_msg += f": {last_host_err}"
        return web.json_response(err_obj(err_msg, "model_not_found"), status=404)

    if do_stream:
        result, errors = await _race_servers(session, model, servers, body, do_stream=True, endpoint=endpoint, remote=request.remote, caps=req_caps)
        if result:
            _, host, full, resp, first_line, first = result
            stream_resp = web.StreamResponse()
            await _forward_stream(request, stream_resp, resp, first_line, host, full, openai_format=False)
            return stream_resp
        msg = "all servers failed"
        if errors:
            msg += ": " + "; ".join(dict.fromkeys(errors))
        return web.json_response(err_obj(msg), status=502)

    result, errors = await _race_servers(session, model, servers, dict(body, stream=False), do_stream=False, endpoint=endpoint, remote=request.remote, caps=req_caps)
    if result:
        _, host, full, data = result
        return web.json_response(data)

    msg = "all servers failed"
    if errors:
        msg += ": " + "; ".join(dict.fromkeys(errors))
    return web.json_response(err_obj(msg), status=502)


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
    servers = load_servers()
    models = all_models()
    tpl_dir = os.path.join(os.path.dirname(__file__), "static")
    with open(os.path.join(tpl_dir, "dashboard.html")) as f:
        html = f.read()
    # Only small scalars and the SD-model datalist are inlined; the Server Room's
    # heavy data (per-model hosts, good/bad/last lists, checked timestamps) AND
    # the chat model list are fetched from /dashboard-models + /dashboard-data and
    # rendered client-side, so nothing large is duplicated in the document.
    sd_models = set()
    for s in servers:
        if s.get("service") != "a1111":
            continue
        for m in s.get("models", []):
            title = m.split(" [")[0] if " [" in m else m
            sd_models.add(title)
    sd_options = "".join(f'<option value="{h}"></option>' for h in sorted(sd_models))
    html = html.replace("__PORT__", str(PORT))
    html = html.replace("__WORKER_COUNT__", str(WORKER_COUNT))
    html = html.replace("__TIMEOUT__", str(TIMEOUT))
    html = html.replace("__SERVER_COUNT__", str(len(servers)))
    html = html.replace("__MODEL_COUNT__", str(len(models)))
    html = html.replace("__DYVA_VERSION__", VERSION)
    html = html.replace("__SD_MODELS__", sd_options)
    return web.Response(text=html, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-cache"})


async def handle_dashboard_models(request):
    """
    Dashboard models (JSON)
    ---
    tags: [UI]
    summary: Normalized server list (each host once) for client-side model/host rendering
    responses:
      '200':
        description: Server list with per-host service, models, and checked time
        content:
          application/json:
            schema:
              type: object
    """
    servers = load_servers()
    out = []
    for s in servers:
        host = s.get("server", "")
        if not host:
            continue
        entry = {"host": host, "service": s.get("service"), "models": s.get("models", [])}
        ck = s.get("checked")
        if ck:
            entry["checked"] = ck
        out.append(entry)
    body = json.dumps({"servers": out})
    # This payload is large but changes slowly; serve it with an ETag so the
    # browser revalidates and gets a tiny 304 instead of re-downloading it.
    etag = '"' + hashlib.md5(body.encode("utf-8")).hexdigest() + '"'
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return web.Response(text=body, content_type="application/json",
                        headers={"ETag": etag, "Cache-Control": "no-cache"})


async def handle_dashboard_data(request):
    """
    Dashboard data (JSON)
    ---
    tags: [UI]
    summary: Current last-successful, good, and bad host lists for live dashboard updates
    responses:
      '200':
        description: Current list state
        content:
          application/json:
            schema:
              type: object
    """
    bad = load_bad()
    good = load_good()
    maybe = load_maybe()
    load_last()
    last = [{"host": entry["host"], "model": model}
            for model, entry in list(_last_cache.items())[:20]]
    # The host-wide unreachable mark is stored under a \x00-prefixed sentinel
    # "model" so it can't collide with a real model name; strip that for display
    # (the raw null byte otherwise renders as a tofu box in the dashboard).
    bad_display = sorted(k.replace(UNREACHABLE_KEY, "(unreachable)") for k in bad)
    return web.json_response({
        "last": last,
        "good": sorted(good),
        "maybe": sorted(maybe),
        "bad": bad_display,
        "good_count": len(good),
        "maybe_count": len(maybe),
        "bad_count": len(bad),
    })


async def handle_settings_get(request):
    """
    Get runtime settings (JSON)
    ---
    tags: [UI]
    summary: Current worker count, timeout, and minimum model count
    responses:
      '200':
        description: Current settings
    """
    if not _is_admin(request):
        # Non-admins get no settings and no sources — just enough for the UI to
        # show a password prompt instead of the controls.
        return web.json_response({"admin": False, "admin_pw_set": True}, status=403)
    return web.json_response({"workers": WORKER_COUNT, "timeout": TIMEOUT,
                              "min_count": MIN_COUNT, "local": _LOCAL,
                              "admin_pw_set": bool(ADMIN_PW),
                              "admin": True, "sources": _stored_sources()})


async def handle_settings_post(request):
    """
    Update runtime settings (JSON)
    ---
    tags: [UI]
    summary: Set worker count, timeout, and/or minimum model count (persisted)
    responses:
      '200':
        description: Updated settings
    """
    global WORKER_COUNT, TIMEOUT, MIN_COUNT, ADMIN_PW, _LOCAL
    resp = _check_local(request) or _check_admin(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if isinstance(body.get("workers"), int) and body["workers"] > 0:
        WORKER_COUNT = min(body["workers"], 200)
    if isinstance(body.get("timeout"), int) and body["timeout"] > 0:
        TIMEOUT = min(body["timeout"], 600)
    if isinstance(body.get("min_count"), int) and body["min_count"] >= 0:
        MIN_COUNT = body["min_count"]
    if isinstance(body.get("local"), bool):
        _LOCAL = body["local"]
    # admin_pw: only when the key is present. A non-empty value sets/changes the
    # password (stored hashed); an explicit empty string clears protection. The
    # plaintext is never stored or echoed back.
    if "admin_pw" in body and isinstance(body["admin_pw"], str):
        pw = body["admin_pw"]
        ADMIN_PW = _hash_pw(pw) if pw else ""
    extra = None
    sources_changed = False
    if "sources" in body:
        if not isinstance(body["sources"], list):
            return web.json_response({"error": '"sources" must be a list'}, status=400)
        sources_changed = body["sources"] != _stored_sources()
        extra = {"sources": body["sources"]}
    save_settings(extra)
    # if the source list changed, re-pull the cache so the new hosts are fetched
    if sources_changed:
        await asyncio.get_event_loop().run_in_executor(None, refresh_cache)
    return web.json_response({"workers": WORKER_COUNT, "timeout": TIMEOUT,
                              "min_count": MIN_COUNT, "local": _LOCAL,
                              "admin_pw_set": bool(ADMIN_PW),
                              "admin": True, "sources": _stored_sources(),
                              "refreshed": sources_changed})


async def handle_settings_test(request):
    """
    Test additional sources (JSON)
    ---
    tags: [UI]
    summary: Fetch each configured source and report how many servers/models it yields
    responses:
      '200':
        description: Per-source results
    """
    resp = _check_local(request) or _check_admin(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    sources = body.get("sources")
    if not isinstance(sources, list):
        return web.json_response({"error": '"sources" must be a list'}, status=400)
    loop = asyncio.get_event_loop()
    results = []
    for s in sources:
        s = s if isinstance(s, dict) else {}
        r = {"name": s.get("name") or "(unnamed)"}
        url = _normalize_url(s.get("url"))
        r["url"] = url
        mapping = s.get("mapping", {})
        if not url:
            r["error"] = "no url given"
            results.append(r)
            continue
        try:
            text = await loop.run_in_executor(None, lambda u=url: requests.get(u, timeout=20).text)
            data = json.loads(text)
        except Exception as ex:
            r["error"] = f"couldn't fetch/parse: {ex}"
            results.append(r)
            continue
        if not isinstance(data, list):
            r["error"] = "the URL returned JSON, but not a list of rows"
            results.append(r)
            continue
        r["rows"] = len(data)
        servers = 0
        models_all = set()
        first = None
        for row in data:
            if not isinstance(row, dict):
                continue
            entry = _apply_source_mapping(row, mapping)
            ip = str(entry.get("server") or entry.get("url") or "").rstrip("/")
            if not ip:
                continue
            ms = entry.get("models")
            ms = ms if isinstance(ms, list) else []
            servers += 1
            for m in ms:
                if isinstance(m, str):
                    models_all.add(m)
            if first is None:
                first = {"host": ip, "models": len(ms)}
        r["servers"] = servers
        r["models"] = len(models_all)
        if first:
            r["first_host"] = first["host"]
            r["first_models"] = first["models"]
        elif r["rows"] > 0:
            keys = next((list(row.keys()) for row in data if isinstance(row, dict)), [])
            r["sample_keys"] = keys
            r["hint"] = ("0 hosts. The first entry has these keys: "
                         + (", ".join(keys) if keys else "(none)")
                         + " — point your 'server' (or 'url') mapping at one of them.")
        results.append(r)
    return web.json_response({"results": results})


async def handle_settings_import(request):
    """
    Import source definitions from a URL (JSON)
    ---
    tags: [UI]
    summary: Fetch a URL that returns a JSON list of source dicts (server-side, avoids CORS)
    responses:
      '200':
        description: The fetched list of sources
    """
    resp = _check_local(request) or _check_admin(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    url = _normalize_url(body.get("url"))
    if not url:
        return web.json_response({"error": "url required"}, status=400)
    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: fetch_source_defs(url))
    except ValueError as ex:
        # shape problems: say what we expected instead of silently importing junk
        return web.json_response({"error": str(ex)}, status=400)
    except Exception as ex:
        return web.json_response({"error": f"fetch/parse failed: {ex}"}, status=400)
    return web.json_response({"sources": data})


async def handle_server_count(request):
    """
    Matching server count (JSON)
    ---
    tags: [UI]
    summary: Number of servers whose models match the given chat model string
    parameters:
      - in: query
        name: q
        schema:
          type: string
        required: false
        description: Model substring to match against
    responses:
      '200':
        description: Count of matching servers
        content:
          application/json:
            schema:
              type: object
    """
    q = request.query.get("q", "").strip()
    servers = len(find_servers(q)) if q else 0
    return web.json_response({"q": q, "servers": servers})



async def handle_v1_models(request):
    """
    List models (OpenAI-compatible)
    ---
    tags: [Models]
    summary: List available models (OpenAI /v1/models format)
    description: |
      Returns the catalog in the OpenAI shape. Each model additionally carries
      `output_modalities` and `input_modalities` (OpenRouter-style) derived from
      the ComfyUI model classifier, so clients can discover what each model can
      produce. Filter the returned models with `?output_modalities=image,video`.
    parameters:
      - in: query
        name: output_modalities
        schema:
          type: string
        required: false
        description: Comma-separated output modalities to keep (image, video, audio, text)
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
                      object:
                        type: string
                      created:
                        type: integer
                      owned_by:
                        type: string
                      count:
                        type: integer
                      input_modalities:
                        type: array
                        items:
                          type: string
                      output_modalities:
                        type: array
                        items:
                          type: string
    """
    resp = _check_local(request)
    if resp:
        return resp
    models = listed_models()
    filter_mods = None
    fm = request.query.get("output_modalities")
    if fm:
        filter_mods = {m.strip().lower() for m in fm.split(",") if m.strip()}
    now = int(time.time())
    out = []
    for m in models:
        mods = m.get("modalities") or ["text"]
        id_ = m["id"]
        if filter_mods and not (set(mods) & filter_mods):
            continue
        out.append({
            "id": id_,
            "object": "model",
            "created": now,
            "owned_by": "dyva",
            "count": m.get("count", 1),
            "input_modalities": mods,
            "output_modalities": mods,
        })
    return web.json_response({
        "object": "list",
        "data": out,
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
    clear_bad_state()
    raise web.HTTPFound("/")


async def handle_next_host(request):
    """
    Skip a model's last successful host, removing it from the last-successful list so another host is tried.
    ---
    tags: [Admin]
    summary: Remove a model's last successful host from the last-successful list
    parameters:
      - in: query
        name: model
        schema:
          type: string
        required: true
        description: Model name to remove from the last-successful list
    responses:
      '200':
        description: Success
    """
    model = request.query.get("model")
    if not model:
        return web.json_response({"error": "missing model parameter"}, status=400)

    prev = get_last(model)
    from_host = prev[0] if prev else None
    from_model = prev[1] if prev else None
    _drop_last_host(model)

    accept = request.headers.get("Accept", "")
    if "application/json" not in accept:
        raise web.HTTPFound("/")

    to_host = to_model = None
    for _prio, host, ms in find_servers(model):
        if ms:
            to_host = host
            to_model = ms[0]
            break

    return web.json_response({
        "from": {"host": from_host, "model": from_model} if from_host else None,
        "to": {"host": to_host, "model": to_model} if to_host else None,
    })


async def handle_skip_good(request):
    """
    Skip a good host, moving it from the good list into the bad list.
    ---
    tags: [Admin]
    summary: Move a good host+model pair into the bad list
    parameters:
      - in: query
        name: host
        schema:
          type: string
        required: true
        description: Host to skip
      - in: query
        name: model
        schema:
          type: string
        required: true
        description: Model to skip the host for
    responses:
      '200':
        description: Success
    """
    host = request.query.get("host")
    model = request.query.get("model")
    if not host or not model:
        return web.json_response({"error": "missing host or model parameter"}, status=400)

    force_bad(host, model)

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
    models = listed_models()
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
            full = entry.get("full", model)
            host = entry.get("host", "unknown")
            digest = re.sub(r"^https?://", "", host)
            models_list.append({
                "name": full,
                "model": full,
                "size": 0,
                "digest": digest,
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
    last = get_last(model)
    license_val = f"{re.sub(r'^https?://', '', last[0])} {last[1]}" if last else ""
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
        "capabilities": ["completion", "vision", "audio", "tools", "thinking"],
        "license": license_val,
    })


async def handle_ollama_stop(request):
    """
    Stop the dyva server
    ---
    tags: [Admin]
    summary: Shut down the dyva proxy server immediately
    responses:
      '200':
        description: Server shutting down
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
    """
    resp = _check_local(request)
    if resp:
        return resp
    loop = asyncio.get_event_loop()
    loop.call_soon(loop.stop)
    return web.json_response({"status": "shutting down"})


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


def _hash_pw(pw):
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()


def _is_admin(request):
    """True if this request may view/change settings & sources. When no admin
    password is set everyone is an admin (the default, open behavior). When one
    is set, the request MUST carry it in the X-Admin-Key header — no localhost
    exemption, since behind a reverse proxy every request's peer is 127.0.0.1
    and that exemption would hand admin to everyone (the whole reason we use a
    password instead of an IP allowlist). If the password is ever forgotten,
    recover by clearing "admin_pw" in settings.json and restarting."""
    if not ADMIN_PW:
        return True
    return _hash_pw(request.headers.get("X-Admin-Key", "")) == ADMIN_PW


def _check_admin(request):
    if _is_admin(request):
        return None
    return web.json_response({"error": "admin password required"}, status=403)


async def handle_ollama_chat(request):
    """
    Chat completion (Ollama format)
    ---
    tags: [Chat]
    summary: Chat completion using Ollama /api/chat format. Proxies to upstream Ollama servers.
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


def _resolve_sd_model(data, body):
    """Best-effort name of the SD model that actually produced the images."""
    model = data.get("_dyva_model")
    if model:
        return model
    try:
        info = data.get("info")
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except Exception:
                info = {}
        if isinstance(info, dict):
            m = info.get("sd_model_name")
            if m:
                return str(m)
            # some A1111 forks omit sd_model_name but include it in infotexts
            for text in info.get("infotexts") or []:
                m = re.search(r"(?:^|,\s)Model: ([^,]+)", str(text))
                if m:
                    name = m.group(1).strip()
                    if name and name.lower() != "none":
                        return name
    except Exception:
        pass
    return body.get("model") or ""


def _save_image_history(data, body, host="", requested_model=""):
    """Persist generated images to the on-disk thumbnail history (last IMG_HISTORY_MAX)."""
    import base64
    import uuid
    try:
        os.makedirs(IMG_DIR, exist_ok=True)
        try:
            with open(IMG_HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            history = []
        info = data.get("info")
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except Exception:
                info = {}
        if not isinstance(info, dict):
            info = {}
        seed_used = data.pop("_dyva_seed", None)
        if seed_used is None:
            try:
                seed_used = int(info.get("seed"))
            except (TypeError, ValueError):
                seed_used = None
        saved = []
        for b64 in (data.get("images") or []):
            try:
                raw = base64.b64decode(b64)
            except Exception:
                continue
            name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
            with open(os.path.join(IMG_DIR, name), "wb") as f:
                f.write(raw)
            saved.append(name)
            history.insert(0, {
                "file": name,
                "host": host,
                "model": _resolve_sd_model(data, body) or requested_model,
                "prompt": (body.get("prompt") or "")[:200],
                "negative_prompt": (body.get("negative_prompt") or "")[:200],
                "seed": seed_used if seed_used is not None else body.get("seed"),
                "steps": body.get("steps"),
                "cfg_scale": body.get("cfg_scale"),
                "sampler_name": body.get("sampler_name"),
                "width": body.get("width"),
                "height": body.get("height"),
                "when": time.time(),
            })
        data.pop("_dyva_model", None)
        data["_dyva_files"] = saved
        for entry in history[IMG_HISTORY_MAX:]:
            try:
                os.remove(os.path.join(IMG_DIR, entry["file"]))
            except OSError:
                pass
        history = history[:IMG_HISTORY_MAX]
        with open(IMG_HISTORY_FILE, "w") as f:
            json.dump(history, f)
    except Exception as e:
        log.debug(f"image history: {e}")


async def handle_image_history(request):
    """
    Recent generated images (JSON)
    ---
    tags: [Image]
    summary: Metadata list of recently generated images, newest first
    responses:
      '200':
        description: Image history entries
        content:
          application/json:
            schema:
              type: array
              items:
                type: object
    """
    try:
        with open(IMG_HISTORY_FILE) as f:
            history = json.load(f)
    except Exception:
        history = []
    return web.json_response(history)


THUMB_SIZE = 72   # the only size the gallery renders (object-fit: cover, square)


def _thumb_path(name):
    return os.path.join(THUMB_DIR, name + ".webp")


def _make_thumb(name):
    """Lazily produce a 72x72 lossy WebP thumbnail (center cover-crop, matching
    the gallery's CSS) for a stored PNG and cache it on disk, so the gallery
    loads ~1-2 KB webps instead of full PNGs. Returns the thumbnail path, or None
    if it can't be made. Blocking (PIL); run in an executor."""
    src = os.path.join(IMG_DIR, name)
    if not os.path.isfile(src):
        return None
    dst = _thumb_path(name)
    try:
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
        from PIL import Image, ImageOps
        os.makedirs(THUMB_DIR, exist_ok=True)
        with Image.open(src) as im:
            im = ImageOps.fit(im.convert("RGB"), (THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            im.save(dst, "WEBP", quality=72, method=6)
        return dst
    except Exception as ex:
        logging.warning(f"thumbnail failed for {name}: {ex}")
        return None


async def handle_image_file(request):
    name = os.path.basename(request.match_info["name"])
    path = os.path.join(IMG_DIR, name)
    if not name.endswith(".png") or not os.path.isfile(path):
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(path)


async def handle_image_thumb(request):
    # /sdapi/v1/images/thumb/{name}.webp  ->  72x72 webp of {name}.png
    thumb = os.path.basename(request.match_info["name"])
    base = re.sub(r"\.webp$", "", thumb)
    name = base + ".png"
    dst = await asyncio.get_event_loop().run_in_executor(None, _make_thumb, name)
    if dst:
        return web.FileResponse(dst)
    full = os.path.join(IMG_DIR, name)
    if os.path.isfile(full):
        return web.FileResponse(full)   # fall back to the full image
    return web.json_response({"error": "not found"}, status=404)


async def handle_image_delete(request):
    """
    Remove an image from the recent history (JSON)
    ---
    tags: [Image]
    summary: Delete one generated image and its history entry
    parameters:
      - in: query
        name: name
        schema:
          type: string
        required: true
        description: Image filename as listed by /sdapi/v1/images
    responses:
      '200':
        description: Deletion result
    """
    name = os.path.basename(request.query.get("name", ""))
    if not name.endswith(".png"):
        return web.json_response({"error": "bad name"}, status=400)
    try:
        os.remove(os.path.join(IMG_DIR, name))
    except OSError:
        pass
    try:
        os.remove(_thumb_path(name))
    except OSError:
        pass
    removed = False
    try:
        with open(IMG_HISTORY_FILE) as f:
            history = json.load(f)
        n = len(history)
        history = [e for e in history if e.get("file") != name]
        if len(history) != n:
            with open(IMG_HISTORY_FILE, "w") as f:
                json.dump(history, f)
            removed = True
    except Exception:
        pass
    return web.json_response({"removed": removed})


CHATS_KEEP = 200


_chats_db = None


def _get_chats_db():
    """Chats live in SQLite, one row per chat, so the sidebar can load a cheap
    list of (id, title) without pulling every chat's messages, and each chat is
    saved/loaded individually. A whole-file JSON rewrite could tear and lose
    everything on a crash; a per-row upsert can't. WAL for crash-safety."""
    global _chats_db
    if _chats_db is None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        _chats_db = sqlite3.connect(CHATS_DB, check_same_thread=False)
        _chats_db.execute("PRAGMA journal_mode=WAL")
        _chats_db.execute("PRAGMA synchronous=NORMAL")
        cols = [r[1] for r in _chats_db.execute("PRAGMA table_info(chats)").fetchall()]
        if cols and ("created" not in cols or "title" not in cols):
            # A prior build used a different, disposable schema — drop it. Chat
            # content up to this transition is explicitly throwaway test data.
            _chats_db.execute("DROP TABLE chats")
        _chats_db.execute(
            "CREATE TABLE IF NOT EXISTS chats("
            "id TEXT PRIMARY KEY, title TEXT, created REAL, data TEXT NOT NULL)")
        _chats_db.commit()
    return _chats_db


def _chats_list():
    """Lightweight sidebar list: id, title, createdAt only — no message bodies."""
    return [{"id": r[0], "title": r[1] or "", "createdAt": r[2]}
            for r in _get_chats_db().execute(
                "SELECT id, title, created FROM chats ORDER BY created DESC").fetchall()]


def _chat_get(cid):
    row = _get_chats_db().execute(
        "SELECT data FROM chats WHERE id=?", (str(cid),)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _chat_upsert(cid, obj):
    db = _get_chats_db()
    title = (obj.get("title") or "") if isinstance(obj, dict) else ""
    try:
        created = float(obj.get("createdAt"))
    except (TypeError, ValueError, AttributeError):
        created = time.time() * 1000.0
    # created is preserved on update (only title/data change), keeping the
    # newest-first ordering stable across edits.
    db.execute(
        "INSERT INTO chats(id,title,created,data) VALUES(?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET title=excluded.title, data=excluded.data",
        (str(cid), title, created, json.dumps(obj, ensure_ascii=False)))
    db.execute(
        "DELETE FROM chats WHERE id NOT IN "
        "(SELECT id FROM chats ORDER BY created DESC LIMIT ?)", (CHATS_KEEP,))
    db.commit()


def _chat_delete(cid):
    db = _get_chats_db()
    db.execute("DELETE FROM chats WHERE id=?", (str(cid),))
    db.commit()


async def handle_chats_get(request):
    """List saved chats (id, title, createdAt) — no message bodies, for the sidebar."""
    resp = _check_local(request)
    if resp:
        return resp
    return web.json_response({"chats": _chats_list()})


async def handle_chat_get(request):
    """One chat's full content by id."""
    resp = _check_local(request)
    if resp:
        return resp
    chat = _chat_get(request.match_info["cid"])
    if chat is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(chat)


async def handle_chat_post(request):
    """Upsert one chat by id."""
    resp = _check_local(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected a chat object"}, status=400)
    _chat_upsert(request.match_info["cid"], body)
    return web.json_response({"ok": True})


async def handle_chat_delete(request):
    """Delete one chat by id."""
    resp = _check_local(request)
    if resp:
        return resp
    _chat_delete(request.match_info["cid"])
    return web.json_response({"ok": True})


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
    requested_model = model_filter or ""
    activity_label = (model_filter or body.get("prompt", "txt2img"))[:60]

    last = get_last(IMG_KEY)
    if last:
        last_host, _ = last
        last_override = None
        if model_filter:
            last_models = next(
                (s.get("models", []) for s in load_servers()
                 if s.get("server") == last_host and s.get("service") == "a1111"),
                [])
            last_match = next(
                (m for m in last_models
                 if match_model(m.split(" [")[0] if " [" in m else m, model_filter)),
                None)
            if last_match is None:
                log.debug(f"txt2img: {last_host} has no model matching "
                          f"{model_filter!r}; not reusing")
                last = None
            else:
                last_override = {"sd_model_checkpoint": last_match}
    if last:
        payload = dict(body)
        if last_override:
            payload["override_settings"] = last_override
            payload["override_settings_restore_afterwards"] = True
        log.debug(f"Reusing {last_host} for txt2img")
        await broadcast_activity(last_host, activity_label, "trying",
            f"txt2img: {activity_label} on {last_host}")
        try:
            async with session.post(
                _host_url(last_host, "/sdapi/v1/txt2img"), json=payload,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    await broadcast_activity(last_host, activity_label, "connected",
                        f"txt2img ✓", duration=0)
                    _save_image_history(data, body, last_host, requested_model)
                    return web.json_response(data)
                add_bad(last_host, IMG_KEY)
            await broadcast_activity(last_host, activity_label, "failed",
                f"txt2img: HTTP {r.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as _e:
            add_bad(last_host, IMG_KEY)
            await broadcast_activity(last_host, activity_label, "failed",
                f"txt2img: {type(_e).__name__}")

    servers = load_servers()
    candidates = [s for s in servers if s.get("service") == "a1111"]
    overrides = {}
    if model_filter:
        filtered = []
        for s in candidates:
            mm = next(
                (m for m in s.get("models", [])
                 if match_model(m.split(" [")[0] if " [" in m else m, model_filter)),
                None)
            if mm is not None:
                filtered.append(s)
                overrides[s.get("server")] = {"sd_model_checkpoint": mm}
        candidates = filtered
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
                    ov = overrides.get(host)
                    payload = (
                        {**body, "override_settings": ov,
                         "override_settings_restore_afterwards": True}
                        if ov else body
                    )
                    async with session.post(
                        _host_url(host, "/sdapi/v1/txt2img"), json=payload,
                        timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            set_last(IMG_KEY, host, "")
                            add_good(host, IMG_KEY)
                            await broadcast_activity(host, activity_label, "connected",
                                f"txt2img ✓", duration=time.time() - t0)
                            data["_dyva_host"] = host
                            await result_queue.put(data)
                            done.set()
                            return
                        add_bad(host, IMG_KEY)
                        await broadcast_activity(host, activity_label, "failed",
                            f"txt2img: HTTP {r.status}")
                except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as _e:
                    add_bad(host, IMG_KEY)
                    await broadcast_activity(host, activity_label, "failed",
                        f"txt2img: {type(_e).__name__}")

        tasks = [asyncio.create_task(worker()) for _ in range(min(WORKER_COUNT, len(host_list)))]
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

    # A host-wide unreachable mark (dead at the connection level) must exclude a
    # host from image routing too — it's stored under a sentinel "model", so the
    # per-model IMG_KEY check alone would miss it and keep trying dead hosts.
    _bad = load_bad()
    _un = load_unreachable()
    def _img_bad(h):
        return h in _un or f"{h} {IMG_KEY}" in _bad

    # Phase 1: try good + untested hosts (skip known-bad and unreachable)
    live_hosts = [h for h in hosts if not _img_bad(h)]
    data = await _race(live_hosts)
    if data:
        _save_image_history(data, body, data.pop("_dyva_host", ""), requested_model)
        return web.json_response(data)

    # Phase 2: exhausted — try previously bad / unreachable hosts (recovery path)
    bad_hosts = [h for h in hosts if _img_bad(h)]
    if bad_hosts:
        data = await _race(bad_hosts)
        if data:
            _save_image_history(data, body, data.pop("_dyva_host", ""), requested_model)
            return web.json_response(data)

    # Phase 3: try comfyui hosts
    comfy_candidates = [s for s in servers if s.get("service") == "comfyui"]
    if model_filter:
        comfy_candidates = [
            s for s in comfy_candidates
            if any(match_model(m.split(" [")[0] if " [" in m else m, model_filter)
                   for m in s.get("models", []))
        ]
    comfy_hosts = [s.get("server") for s in comfy_candidates if not _img_bad(s.get("server"))]
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
                    data = await _txt2img_comfyui(session, host, body, model_filter)
                    if data:
                        set_last(IMG_KEY, host, "")
                        add_good(host, IMG_KEY)
                        await broadcast_activity(host, activity_label, "connected",
                            f"txt2img (comfy) ✓", duration=time.time() - t0)
                        data["_dyva_host"] = host
                        await result_queue.put(data)
                        done.set()
                        return
                    add_bad(host, IMG_KEY)
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
            _save_image_history(data, body, data.pop("_dyva_host", ""), requested_model)
            return web.json_response(data)

    return web.json_response({"error": "all image-gen hosts failed"}, status=502)


async def _txt2img_comfyui(session, host, body, model_filter=None):
    import uuid as uuid_mod

    try:
        checkpoints_resp = await session.get(
            _host_url(host, "/models/checkpoints"),
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if checkpoints_resp.status != 200:
            await checkpoints_resp.release()
            return None
        checkpoints = await checkpoints_resp.json()
        await checkpoints_resp.release()
        if not isinstance(checkpoints, list) or not checkpoints:
            return None
        ckpt = None
        if model_filter:
            ckpt = next(
                (c for c in checkpoints if match_model(c, model_filter)), None)
        if ckpt is None:
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
            _host_url(host, "/prompt"),
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
                _host_url(host, f"/history/{prompt_id}"),
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
                            _host_url(host, "/view"),
                            params={"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")},
                            timeout=aiohttp.ClientTimeout(total=30),
                        )
                        if view_resp.status == 200:
                            raw = await view_resp.read()
                            await view_resp.release()
                            import base64
                            b64 = base64.b64encode(raw).decode()
                            return {"images": [b64], "_dyva_model": ckpt,
                                    "_dyva_seed": seed, "parameters": "{}",
                                    "info": json.dumps({"prompt": body})}
                        await view_resp.release()
                    except Exception:
                        pass
            break
        if entry.get("status", {}).get("status_str") == "error":
            break

    return None


COMFYUI_KEY = "__comfyui__"


def _find_comfyui_host(model_filter=None):
    servers = load_servers()
    candidates = [s for s in servers if s.get("service") == "comfyui"]
    if model_filter:
        candidates = [
            s for s in candidates
            if any(match_model(m.split(" [")[0] if " [" in m else m, model_filter)
                   for m in s.get("models", []))
        ]
    hosts = [s.get("server") for s in candidates if s.get("server")]
    bad = load_bad()
    good = load_good()
    hosts.sort(key=lambda h: 0 if f"{h} {COMFYUI_KEY}" in good else (1 if f"{h} {COMFYUI_KEY}" not in bad else 2))
    last = get_last(COMFYUI_KEY)
    if last:
        lh = last[0]
        hosts = [lh] + [h for h in hosts if h != lh]
    return hosts


async def handle_comfyui_proxy(request):
    """
    ComfyUI pass-through proxy
    ---
    tags: [ComfyUI]
    summary: Proxy any ComfyUI API request to a discovered host. Supports /prompt, /queue, /history, /view, /models, /system_stats, etc.
    description: |
      Forward ComfyUI workflow requests to a discovered ComfyUI host.

      Strip the `/comfyui` prefix and proxy to a working host. Use `?host=ip:port` to target a specific host.

      Supported endpoints:
      - `POST /comfyui/prompt` — Submit a workflow
      - `GET  /comfyui/queue` — View queue state
      - `POST /comfyui/queue` — Modify queue (clear, delete)
      - `POST /comfyui/interrupt` — Cancel current execution
      - `GET  /comfyui/history` — Full execution history
      - `GET  /comfyui/history/{prompt_id}` — History for a specific prompt
      - `GET  /comfyui/view?filename=X&type=output` — Retrieve output image
      - `GET  /comfyui/models/{type}` — List models (checkpoints, loras, etc.)
      - `GET  /comfyui/system_stats` — Server info
      - `GET  /comfyui/object_info` — Full node catalogue
    parameters:
      - in: query
        name: host
        schema:
          type: string
        description: Target a specific ComfyUI host (ip:port)
    responses:
      '200':
        description: Proxied response from ComfyUI host
      '502':
        description: Upstream error
      '503':
        description: No available ComfyUI hosts
    """
    resp = _check_local(request)
    if resp:
        return resp

    session = request.app["session"]
    tail = request.match_info.get("tail", "")
    target_host = request.query.get("host")

    if target_host:
        hosts = [target_host]
    else:
        hosts = _find_comfyui_host()

    if not hosts:
        return web.json_response({"error": "no available ComfyUI hosts"}, status=503)

    upstream_path = f"/{tail}" if tail else "/"
    body = await request.read()
    query = str(request.query_string)

    last = get_last(COMFYUI_KEY)
    if last and not target_host:
        hosts = [last[0]] + [h for h in hosts if h != last[0]]

    last_err = None
    for host in hosts:
        url = _host_url(host, upstream_path)
        if query:
            url += f"?{query}"

        is_prompt = (tail.rstrip("/") == "prompt" and request.method == "POST")

        try:
            async with session.request(
                request.method, url,
                data=body if body else None,
                headers={"Content-Type": request.content_type} if request.content_type else None,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as upstream:
                resp_body = await upstream.read()
                if upstream.status >= 400:
                    last_err = f"HTTP {upstream.status}"
                    continue
                if is_prompt:
                    set_last(COMFYUI_KEY, host, "")
                return web.Response(
                    body=resp_body,
                    status=upstream.status,
                    content_type=upstream.content_type,
                )
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            last_err = str(e)
            continue

    return web.json_response({"error": f"all ComfyUI hosts failed: {last_err}"}, status=502)


TTS_KEY = "__tts__"

NODE_CLASSIFIER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node-classifier.json")
_node_classifier_cache = None


def load_node_classifier():
    """Compile the ComfyUI node-classifier (family -> regex list + field hints).
    Mirrors model-classifier.json: surveyable, popularity-seeded data — not a
    boutique hardcoded list. Only the predictably-named, high-cardinality node
    families matter for building an available server pool."""
    global _node_classifier_cache
    if _node_classifier_cache is not None:
        return _node_classifier_cache
    compiled = []
    if os.path.exists(NODE_CLASSIFIER_FILE):
        try:
            with open(NODE_CLASSIFIER_FILE) as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for spec in raw.get("tts") or []:
                    regs = []
                    for p in spec.get("patterns") or []:
                        try:
                            regs.append(re.compile(p))
                        except re.error as e:
                            log.warning(f"node-classifier: bad regex {p!r}: {e}")
                    if regs:
                        compiled.append({
                            "name": spec.get("name", "Generic"),
                            "regs": regs,
                            "text": spec.get("text", "text"),
                            "voice": spec.get("voice"),
                            "lang": spec.get("lang"),
                        })
        except Exception as e:
            log.warning(f"node-classifier: failed to load {NODE_CLASSIFIER_FILE}: {e}")
    _node_classifier_cache = compiled
    return compiled


def _tts_node_family(node_class):
    """Return the surveyed TTS-family spec whose name-pattern matches a node
    class name, or None if the node isn't a recognized (popular) TTS node."""
    for spec in load_node_classifier():
        if any(r.search(node_class) for r in spec["regs"]):
            return spec
    return None


_TTS_NODE_CACHE = {}
_TTS_NODE_CACHE_TTL = 300

_TTS_MIME = {
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".m4a": "audio/mp4",
}


class _TtsError(Exception):
    pass


async def _tts_node_for(session, host):
    """Find a host's TTS node by classifying its installed node-class names
    against the node-classifier (data-driven, popularity-seeded) — never a
    hardcoded boutique list and never structural model probing."""
    hit = _TTS_NODE_CACHE.get(host)
    if hit and time.time() - hit[1] < _TTS_NODE_CACHE_TTL:
        return hit[0]
    try:
        r = await session.get(
            _host_url(host, "/object_info"),
            timeout=aiohttp.ClientTimeout(total=20),
        )
        if r.status != 200:
            await r.release()
            return None
        info = await r.json(content_type=None)
        await r.release()
    except Exception:
        return None
    if not isinstance(info, dict):
        return None

    # family priority wins over host node order (QwenTTS before Generic TTS)
    for fam in load_node_classifier():
        for node_class in info:
            if not any(r.search(node_class) for r in fam["regs"]):
                continue
            required = ((info[node_class].get("input") or {}).get("required")) or {}
            spec = {
                "class": node_class,
                "text": fam["text"],
                "voice": fam["voice"],
                "lang": fam["lang"],
            }
            voice_field = fam["voice"]
            if voice_field:
                entry = required.get(voice_field)
                opts = entry[0] if isinstance(entry, list) and isinstance(entry[0], list) else None
                if isinstance(opts, list) and not opts:
                    continue
            _TTS_NODE_CACHE[host] = (spec, time.time())
            return spec
    return None


def _tts_workflow(spec, schema, input_text, voice):
    cls = spec["class"]
    inputs = {spec["text"]: input_text}
    required = ((schema.get(cls) or {}).get("input") or {}).get("required") or {}

    voice_field = spec.get("voice")
    lang_field = spec.get("lang")
    if voice and voice_field:
        entry = required.get(voice_field)
        opts = entry[0] if isinstance(entry, list) and isinstance(entry[0], list) else []
        if isinstance(opts, list) and (not opts or voice in opts):
            inputs[voice_field] = voice

    if lang_field:
        entry = required.get(lang_field)
        opts = entry[0] if isinstance(entry, list) and isinstance(entry[0], list) else []
        lang = "zh" if any("\u4e00" <= c <= "\u9fff" for c in input_text) else "en"
        if opts and lang not in opts:
            lang = opts[0]
        inputs[lang_field] = lang

    for name, entry in required.items():
        if name in inputs or not isinstance(entry, list) or len(entry) < 2:
            continue
        attrs = entry[1] if isinstance(entry[1], dict) else {}
        if "default" in attrs:
            inputs[name] = attrs["default"]
        elif isinstance(entry[0], list) and entry[0]:
            inputs[name] = entry[0][0]

    return {
        "1": {"class_type": cls, "inputs": inputs},
        "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": "dyva/tts"}},
    }


async def _tts_comfyui(session, host, input_text, voice=None):
    import uuid as uuid_mod

    spec = await _tts_node_for(session, host)
    if not spec:
        raise _TtsError("no known TTS node")

    try:
        info_resp = await session.get(
            _host_url(host, f"/object_info/{spec['class']}"),
            timeout=aiohttp.ClientTimeout(total=15),
        )
        if info_resp.status != 200:
            await info_resp.release()
            raise _TtsError(f"object_info HTTP {info_resp.status}")
        schema = await info_resp.json(content_type=None)
        await info_resp.release()
    except _TtsError:
        raise
    except Exception as e:
        raise _TtsError(str(e))

    workflow = _tts_workflow(spec, schema, input_text, voice)
    client_id = str(uuid_mod.uuid4())
    try:
        prompt_resp = await session.post(
            _host_url(host, "/prompt"),
            json={"prompt": workflow, "client_id": client_id},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if prompt_resp.status != 200:
            body = await prompt_resp.text()
            await prompt_resp.release()
            raise _TtsError(f"prompt rejected: {body[:200]}")
        prompt_data = await prompt_resp.json(content_type=None)
        await prompt_resp.release()
    except _TtsError:
        raise
    except Exception as e:
        raise _TtsError(str(e))
    prompt_id = prompt_data.get("prompt_id")
    if not prompt_id:
        raise _TtsError("no prompt_id")

    deadline = time.time() + 180
    while time.time() < deadline:
        await asyncio.sleep(2)
        try:
            hist_resp = await session.get(
                _host_url(host, f"/history/{prompt_id}"),
                timeout=aiohttp.ClientTimeout(total=30),
            )
            if hist_resp.status != 200:
                await hist_resp.release()
                continue
            hist = await hist_resp.json(content_type=None)
            await hist_resp.release()
        except Exception:
            continue
        entry = hist.get(prompt_id)
        if not entry:
            continue
        audio_files = []
        for out in (entry.get("outputs") or {}).values():
            audio_files.extend(out.get("audio") or [])
        if audio_files:
            af = audio_files[0]
            try:
                view_resp = await session.get(
                    _host_url(host, "/view"),
                    params={
                        "filename": af.get("filename"),
                        "subfolder": af.get("subfolder", ""),
                        "type": af.get("type", "output"),
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                )
                if view_resp.status != 200:
                    await view_resp.release()
                    raise _TtsError(f"view HTTP {view_resp.status}")
                raw = await view_resp.read()
                await view_resp.release()
            except _TtsError:
                raise
            except Exception as e:
                raise _TtsError(str(e))
            return raw, af.get("filename") or "tts.flac"
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            msgs = [str(m) for m in (status.get("messages") or [])]
            detail = "; ".join(msgs)[:200]
            raise _TtsError(f"execution error{': ' + detail if detail else ''}")
        if status.get("completed"):
            break
    raise _TtsError("timeout waiting for audio")


def _find_tts_hosts(target_host=None):
    """ComfyUI hosts that expose a model classified as a speech/audio generator,
    matching the classifier-driven routing used for video. A host must actually
    survey an `audio`-class model (e.g. a qwen-tts/voxcpm checkpoint) to be a
    TTS candidate — we never assume capability from a node-name list."""
    if target_host:
        if _host_has_class(target_host, _AUDIO_CLASSES):
            return [target_host]
        return []
    servers = load_servers()
    hosts = [s.get("server") for s in servers
             if s.get("service") == "comfyui" and s.get("server")
             and _host_has_class(s.get("server"), _AUDIO_CLASSES)]
    bad, good = load_bad(), load_good()
    hosts.sort(key=lambda h: 0 if f"{h} {TTS_KEY}" in good else (1 if f"{h} {TTS_KEY}" not in bad else 2))
    last = get_last(TTS_KEY)
    if last and last[0]:
        hosts = [last[0]] + [h for h in hosts if h != last[0]]
    return hosts


def _host_has_class(host, classes):
    """True if any of a ComfyUI host's cached models classify into `classes`."""
    for s in load_servers():
        if s.get("server") != host:
            continue
        for m in s.get("models") or []:
            if classify_model(m) in classes:
                return True
    return False


def _find_video_hosts(target_host=None):
    """ComfyUI hosts that expose a model classified as a video generator."""
    if target_host:
        if _host_has_class(target_host, _VIDEO_CLASSES):
            return [target_host]
        return []
    servers = load_servers()
    hosts = [s.get("server") for s in servers
             if s.get("service") == "comfyui" and s.get("server")
             and _host_has_class(s.get("server"), _VIDEO_CLASSES)]
    bad, good = load_bad(), load_good()
    hosts.sort(key=lambda h: 0 if f"{h} {VIDEO_KEY}" in good else (1 if f"{h} {VIDEO_KEY}" not in bad else 2))
    last = get_last(VIDEO_KEY)
    if last and last[0]:
        hosts = [last[0]] + [h for h in hosts if h != last[0]]
    return hosts


# ---- Async ComfyUI video-generation jobs (OpenRouter /v1/videos shape) ----
# Job lifecycle: pending -> in_progress -> completed | failed. `unsigned_urls`
# points at /v1/videos/{id}/content so clients can stream the mp4. Jobs run as
# background asyncio tasks because diffusion video is slow.


def _video_job_save():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        # bytes (raw video) are stored on disk, not in the JSON index
        serializable = {}
        for k, v in _VIDEO_JOBS.items():
            if isinstance(v, dict):
                serializable[k] = {kk: vv for kk, vv in v.items() if not isinstance(vv, (bytes, bytearray))}
            else:
                serializable[k] = v
        with open(VIDEO_JOBS_FILE, "w") as f:
            json.dump(serializable, f, indent=2)
    except Exception as e:
        log.warning(f"video jobs: failed to persist: {e}")


def _load_video_jobs():
    global _VIDEO_JOBS, _VIDEO_JOB_ID_CTR
    if _VIDEO_JOBS:
        return
    if os.path.exists(VIDEO_JOBS_FILE):
        try:
            with open(VIDEO_JOBS_FILE) as f:
                _VIDEO_JOBS = json.load(f)
            for k, v in _VIDEO_JOBS.items():
                m = re.match(r"^v(\d+)$", k)
                if m:
                    _VIDEO_JOB_ID_CTR = max(_VIDEO_JOB_ID_CTR, int(m.group(1)))
        except Exception:
            _VIDEO_JOBS = {}
    # cull expired/failed-and-old jobs
    now = time.time()
    drop = [k for k, v in _VIDEO_JOBS.items()
            if now - (v.get("created") or 0) > VIDEO_JOB_TTL]
    for k in drop:
        cp = (_VIDEO_JOBS[k] or {}).get("content_path")
        if cp and os.path.exists(cp):
            try:
                os.remove(cp)
            except Exception:
                pass
        del _VIDEO_JOBS[k]


def _video_job_new(job):
    global _VIDEO_JOB_ID_CTR
    _load_video_jobs()
    _VIDEO_JOB_ID_CTR += 1
    jid = f"v{_VIDEO_JOB_ID_CTR}"
    _VIDEO_JOBS[jid] = job
    # keep the store bounded
    if len(_VIDEO_JOBS) > VIDEO_JOB_MAX:
        oldest = sorted(_VIDEO_JOBS, key=lambda k: _VIDEO_JOBS[k].get("created") or 0)[:len(_VIDEO_JOBS) - VIDEO_JOB_MAX]
        for k in oldest:
            del _VIDEO_JOBS[k]
    _video_job_save()
    return jid


async def _run_video_job(session, jid):
    job = _VIDEO_JOBS.get(jid)
    if not job:
        return
    job["status"] = "in_progress"
    _video_job_save()
    host = job["host"]
    model_path = job["model"]
    label = f"video: {job.get('prompt', '')[:40]}"
    t0 = time.time()
    await broadcast_activity(host, label, "trying", label)
    try:
        prompt_id = await _submit_video_workflow(session, host, model_path, job)
        if not prompt_id:
            raise _VideoError("failed to submit workflow to host")
        job["comfyui_prompt_id"] = prompt_id
        job["status"] = "in_progress"
        _video_job_save()
        content, filename = await _poll_video_result(session, host, prompt_id, job)
        content_path = os.path.join(VIDEO_JOBS_DIR, f"{jid}.bin")
        os.makedirs(VIDEO_JOBS_DIR, exist_ok=True)
        with open(content_path, "wb") as f:
            f.write(content)
        job["content_path"] = content_path
        job["filename"] = filename
        job["status"] = "completed"
        job["unsigned_urls"] = [f"/v1/videos/{jid}/content"]
    except (_VideoError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
        job["status"] = "failed"
        job["error"] = str(e) or type(e).__name__
        add_bad(host, VIDEO_KEY)
        await broadcast_activity(host, label, "failed", f"{label}: {job['error']}", duration=time.time() - t0)
        _video_job_save()
        return
    add_good(host, VIDEO_KEY)
    set_last(VIDEO_KEY, host, model_path)
    await broadcast_activity(host, label, "done", label, duration=time.time() - t0)
    _video_job_save()


class _VideoError(Exception):
    pass


def _pick_video_model(host, model_filter=None):
    """Pick a concrete video-class model filename for a host, honoring `model_filter`."""
    for s in load_servers():
        if s.get("server") != host:
            continue
        for m in s.get("models") or []:
            if classify_model(m) not in _VIDEO_CLASSES:
                continue
            if model_filter and not match_model(m, model_filter):
                continue
            return m
    return None


def _detect_video_family(model_path):
    """Best-effort family detection from the model path/filename. Used to pick
    a workflow builder. Returns one of 'wan', 'ltx', 'mochi', or None."""
    p = str(model_path or "").lower().split("/")[-1]
    if "wan" in p:
        return "wan"
    if "ltx" in p or "lightning" in p:
        return "ltx"
    if "mochi" in p or "mochi" in str(model_path or "").lower():
        return "mochi"
    return None


async def _get_object_info(session, host):
    """Return the live host's /object_info dict, or None."""
    try:
        oi = await session.get(
            _host_url(host, "/object_info"),
            timeout=aiohttp.ClientTimeout(total=20),
        )
        if oi.status == 200:
            info = await oi.json(content_type=None)
            await oi.release()
            return info or {}
        await oi.release()
    except Exception:
        return None
    return None


def _build_ltx_workflow(params, model_path, steps, cfg, width, height, length, seed):
    """Text-to-video graph for LTX-2.x diffusion models. Wiring:
    DiffusionModelLoader -> MODEL/CLIP/VAE, CLIPLoader -> CLIP, CLIPTextEncode,
    LTXVConditioning -> pos/neg conditioning + latent, EmptyLTXVLatentVideo,
    LTXVScheduler -> SIGMAS, KSamplerSelect -> SAMPLER, CFGGuider -> GUIDER,
    RandomNoise -> NOISE, SamplerCustomAdvanced -> latent, VAEDecode -> images,
    SaveVideo."""
    prompt = params.get("prompt", "")
    workflow = {}
    def add(nid, cls, inputs):
        workflow[str(nid)] = {"class_type": cls, "inputs": inputs}

    add(1, "DiffusionModelLoader", {"unet_name": model_path})
    add(2, "CLIPLoader", {"clip_name": "gemma-2-9b-gguf.safetensors", "type": "gemma"})
    add(3, "CLIPTextEncode", {"clip": [2, 0], "text": prompt})
    add(4, "CLIPTextEncode", {"clip": [2, 0], "text": ""})
    add(5, "LTXVConditioning", {
        "positive": [3, 0], "negative": [4, 0],
        "width": 768, "height": 512, "frame_rate": 25,
    })
    add(6, "EmptyLTXVLatentVideo", {
        "width": width, "height": height, "length": length, "batch_size": 1,
    })
    add(7, "LTXVScheduler", {"sigma_max": 1.0, "sigma_min": 0.03, "rho": 7.0})
    add(8, "KSamplerSelect", {"sampler_name": "euler"})
    add(9, "CFGGuider", {
        "model": [1, 0], "positive": [5, 0], "negative": [5, 1], "cfg": cfg,
    })
    add(10, "RandomNoise", {"noise_seed": seed})
    add(11, "SamplerCustomAdvanced", {
        "noise": [10, 0], "guider": [9, 0], "sampler": [8, 0],
        "sigmas": [7, 0], "latent_image": [6, 0],
    })
    add(12, "VAEDecode", {"samples": [11, 0], "vae": [1, 2]})
    add(13, "SaveVideo", {"filename_prefix": "dyva_video", "images": [12, 0]})
    return workflow


def _build_wan_workflow(params, model_path, steps, cfg, width, height, length, seed):
    """Text-to-video graph for Wan 2.x diffusion models. Wiring:
    UNETLoader -> MODEL, CLIPLoader(umt5) -> CLIP, CLIPTextEncode,
    EmptyHunyuanLatentVideo -> latent, ModelSamplingSD3 optional,
    VideoLinearCFGGuidance -> model, KSampler -> latent, VAEDecode -> images,
    SaveVideo."""
    prompt = params.get("prompt", "")
    workflow = {}
    def add(nid, cls, inputs):
        workflow[str(nid)] = {"class_type": cls, "inputs": inputs}

    add(1, "UNETLoader", {"unet_name": model_path, "weight_dtype": "default"})
    add(2, "CLIPLoader", {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan"})
    add(3, "CLIPTextEncode", {"clip": [2, 0], "text": prompt})
    add(4, "CLIPTextEncode", {"clip": [2, 0], "text": ""})
    add(5, "EmptyHunyuanLatentVideo", {
        "width": width, "height": height, "length": length, "batch_size": 1,
    })
    add(6, "VideoLinearCFGGuidance", {
        "model": [1, 0], "min_cfg": 1.0,
    })
    add(7, "KSampler", {
        "model": [6, 0], "positive": [3, 0], "negative": [4, 0],
        "latent_image": [5, 0], "seed": seed, "steps": steps, "cfg": cfg,
        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
    })
    add(8, "VAEDecode", {"samples": [7, 0], "vae": [1, 2]})
    add(9, "SaveVideo", {"filename_prefix": "dyva_video", "images": [8, 0]})
    return workflow


def _build_mochi_workflow(params, model_path, steps, cfg, width, height, length, seed):
    """Text-to-video graph for Mochi-1 (MochiAsync) diffusion models."""
    prompt = params.get("prompt", "")
    workflow = {}
    def add(nid, cls, inputs):
        workflow[str(nid)] = {"class_type": cls, "inputs": inputs}

    add(1, "MochiModelLoader", {"model": model_path})
    add(2, "CLIPLoader", {"clip_name": "t5xxl_fp8_e4m3fn.safetensors", "type": "sd3"})
    add(3, "CLIPTextEncode", {"clip": [2, 0], "text": prompt})
    add(4, "CLIPTextEncode", {"clip": [2, 0], "text": ""})
    add(5, "EmptyMochiLatentVideo", {
        "width": width, "height": height, "length": length, "batch_size": 1,
    })
    add(6, "MochiSamplingSettings", {
        "model": [1, 0], "positive": [3, 0], "negative": [4, 0],
        "latent_image": [5, 0], "seed": seed, "steps": steps, "cfg": cfg,
    })
    add(7, "VAEDecode", {"samples": [6, 0], "vae": [1, 2]})
    add(8, "SaveVideo", {"filename_prefix": "dyva_video", "images": [7, 0]})
    return workflow


async def _submit_video_workflow(session, host, model_path, job):
    """Build and submit a text-to-video ComfyUI workflow for the model. Returns
    the ComfyUI prompt_id, or None on submission failure. The builder is chosen
    by the model's family (wan/ltx/mochi); unrecognized families fail cleanly
    rather than sending a garbage graph."""
    import uuid as uuid_mod

    params = job.get("params") or {}
    steps = int(params.get("steps", 20))
    cfg = float(params.get("cfg", 6.0))
    width = int(params.get("width", 768))
    height = int(params.get("height", 512))
    length = int(params.get("length", 25))
    seed = int(params.get("seed", -1))
    if seed == -1:
        import random
        seed = random.randint(0, 2**31 - 1)

    family = _detect_video_family(model_path)
    if family == "wan":
        workflow = _build_wan_workflow(params, model_path, steps, cfg, width, height, length, seed)
    elif family == "ltx":
        workflow = _build_ltx_workflow(params, model_path, steps, cfg, width, height, length, seed)
    elif family == "mochi":
        workflow = _build_mochi_workflow(params, model_path, steps, cfg, width, height, length, seed)
    else:
        raise _VideoError(f"unsupported video model family for {model_path!r}")

    client_id = str(uuid_mod.uuid4())
    try:
        p = await session.post(
            _host_url(host, "/prompt"),
            json={"prompt": workflow, "client_id": client_id},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if p.status != 200:
            await p.release()
            return None
        data = await p.json()
        await p.release()
        return data.get("prompt_id")
    except Exception:
        return None


async def _poll_video_result(session, host, prompt_id, job):
    """Poll ComfyUI /history/{prompt_id} until the video is saved; return
    (bytes, filename)."""
    deadline = time.time() + 600
    while time.time() < deadline:
        await asyncio.sleep(3)
        try:
            r = await session.get(
                _host_url(host, f"/history/{prompt_id}"),
                timeout=aiohttp.ClientTimeout(total=30),
            )
            if r.status != 200:
                await r.release()
                continue
            hist = await r.json()
            await r.release()
        except Exception:
            continue
        entry = hist.get(prompt_id)
        if not entry:
            continue
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            msgs = [str(m) for m in (status.get("messages") or [])]
            raise _VideoError("execution error" + (": " + "; ".join(msgs)[:200] if msgs else ""))
        if status.get("completed"):
            outputs = entry.get("outputs", {})
            for node_id, out in outputs.items():
                vids = out.get("gifs") or out.get("videos") or []
                for vid in vids:
                    try:
                        vr = await session.get(
                            _host_url(host, "/view"),
                            params={"filename": vid["filename"], "subfolder": vid.get("subfolder", ""), "type": vid.get("type", "output")},
                            timeout=aiohttp.ClientTimeout(total=60),
                        )
                        if vr.status == 200:
                            raw = await vr.read()
                            await vr.release()
                            return raw, vid["filename"]
                        await vr.release()
                    except Exception:
                        pass
            raise _VideoError("saved video not found in outputs")
    raise _VideoError("timeout waiting for video")


async def handle_videos_post(request):
    """
    Submit a video generation job (OpenRouter-compatible)
    ---
    tags: [Video]
    summary: POST /v1/videos — start an async text-to-video job on a ComfyUI host
    description: |
      Mirrors the OpenRouter `/v1/videos` contract. You get an id and polling_url
      immediately; poll GET /v1/videos/{id} until status is `completed`, then
      download from unsigned_urls[0] (also GET /v1/videos/{id}/content).
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              model:
                type: string
                description: Video model name (optional, defaults to first video-class model)
              prompt:
                type: string
                required: true
              duration:
                type: integer
              resolution:
                type: string
              size:
                type: string
              seed:
                type: integer
    responses:
      '202':
        description: Job submitted
      '400':
        description: Missing prompt
      '503':
        description: No video-capable hosts
    """
    resp = _check_local(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    prompt_text = str(body.get("prompt") or "").strip()
    if not prompt_text:
        return web.json_response({"error": "'prompt' is required"}, status=400)

    hosts = _find_video_hosts(request.query.get("host"))
    if not hosts:
        return web.json_response(
            {"error": "no ComfyUI hosts with a video-capable model available"}, status=503)

    model_filter = body.get("model") or ""
    # pick the best host that actually has a matching video model
    chosen_host = None
    chosen_model = None
    for h in hosts:
        m = _pick_video_model(h, model_filter)
        if m:
            chosen_host = h
            chosen_model = m
            break
    if not chosen_host:
        return web.json_response(
            {"error": "no host has a video model matching requested model"}, status=404)

    params = dict(body)
    params["prompt"] = prompt_text
    job = {
        "id": None,
        "status": "pending",
        "host": chosen_host,
        "model": chosen_model,
        "prompt": prompt_text,
        "params": params,
        "created": time.time(),
        "unsigned_urls": [],
    }
    jid = _video_job_new(job)
    job["id"] = jid
    job["polling_url"] = f"/v1/videos/{jid}"
    _video_job_save()
    asyncio.get_event_loop().create_task(_run_video_job(request.app["session"], jid))
    return web.json_response({"id": jid, "polling_url": job["polling_url"], "status": "pending"}, status=202)


async def handle_videos_get(request):
    """
    Poll a video generation job (OpenRouter-compatible)
    ---
    tags: [Video]
    summary: GET /v1/videos/{id} — poll a video job until completed
    parameters:
      - in: path
        name: id
        schema:
          type: string
        required: true
    responses:
      '200':
        description: Job status
      '404':
        description: Unknown job id
    """
    resp = _check_local(request)
    if resp:
        return resp
    jid = request.match_info.get("id")
    _load_video_jobs()
    job = _VIDEO_JOBS.get(jid)
    if not job:
        return web.json_response({"error": "unknown job"}, status=404)
    out = {
        "id": jid,
        "status": job["status"],
        "model": job["model"],
        "polling_url": f"/v1/videos/{jid}",
    }
    if job["status"] == "completed":
        out["unsigned_urls"] = job.get("unsigned_urls") or []
    if job["status"] == "failed":
        out["error"] = job.get("error")
    return web.json_response(out)


async def handle_videos_content(request):
    """
    Download a finished video (OpenRouter-compatible)
    ---
    tags: [Video]
    summary: GET /v1/videos/{id}/content — stream the generated mp4
    parameters:
      - in: path
        name: id
        schema:
          type: string
        required: true
      - in: query
        name: index
        schema:
          type: integer
        required: false
    responses:
      '200':
        description: Video bytes
      '404':
        description: Job missing or not complete
    """
    resp = _check_local(request)
    if resp:
        return resp
    jid = request.match_info.get("id")
    _load_video_jobs()
    job = _VIDEO_JOBS.get(jid)
    if not job or job["status"] != "completed" or not job.get("content_path"):
        return web.json_response({"error": "no completed video for this job"}, status=404)
    content_path = job["content_path"]
    if not os.path.exists(content_path):
        return web.json_response({"error": "no completed video for this job"}, status=404)
    content_type = "video/mp4"
    fn = job.get("filename") or "output.mp4"
    ext = os.path.splitext(fn)[1].lower()
    if ext == ".webm":
        content_type = "video/webm"
    with open(content_path, "rb") as f:
        body = f.read()
    return web.Response(body=body, content_type=content_type)


async def handle_tts_speech(request):
    """
    OpenAI-compatible text-to-speech
    ---
    tags: [Audio]
    summary: POST /v1/audio/speech — generate speech from text on a discovered ComfyUI host
    description: |
      Accepts the OpenAI `/v1/audio/speech` shape (`model`, `input`, `voice`,
      `response_format`, `speed`) and returns binary audio. Backed by ComfyUI
      hosts with known TTS custom nodes (MegaTTS3, VoxCPM, QwenTTS, ...).
      `response_format` and `speed` are accepted but ignored — you get the
      host-native format (usually flac).
    parameters:
      - in: query
        name: host
        schema:
          type: string
        description: Target a specific ComfyUI host (ip:port)
    responses:
      '200':
        description: Audio bytes
      '400':
        description: Missing/invalid input
      '502':
        description: All hosts failed
      '503':
        description: No available hosts
    """
    resp = _check_local(request)
    if resp:
        return resp

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    input_text = str(body.get("input") or "").strip()
    if not input_text:
        return web.json_response({"error": "'input' is required"}, status=400)
    voice = body.get("voice")
    if not isinstance(voice, str) or not voice.strip():
        voice = None
    else:
        voice = voice.strip()

    hosts = _find_tts_hosts(request.query.get("host"))
    if not hosts:
        return web.json_response({"error": "no ComfyUI hosts available for tts"}, status=503)

    session = request.app["session"]
    snippet = input_text[:60] + ("..." if len(input_text) > 60 else "")
    last_err = None
    for host in hosts:
        label = f"tts: {snippet}"
        t0 = time.time()
        await broadcast_activity(host, label, "trying", label)
        try:
            raw, filename = await _tts_comfyui(session, host, input_text, voice)
        except (_TtsError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            last_err = str(e) or type(e).__name__
            add_bad(host, TTS_KEY)
            await broadcast_activity(host, label, "failed", f"{label}: {last_err}", duration=time.time() - t0)
            continue
        add_good(host, TTS_KEY)
        set_last(TTS_KEY, host, "")
        await broadcast_activity(host, label, "done", label, duration=time.time() - t0)
        ext = os.path.splitext(filename)[1].lower()
        return web.Response(body=raw, content_type=_TTS_MIME.get(ext, "application/octet-stream"))

    return web.json_response({"error": f"tts failed: {last_err}"}, status=502)


async def handle_audio_voices(request):
    """
    List available TTS voices/nodes across hosts
    ---
    tags: [Audio]
    summary: GET /v1/audio/voices — probe discovered ComfyUI hosts for TTS nodes and their voices
    parameters:
      - in: query
        name: host
        schema:
          type: string
        description: Target a specific ComfyUI host (ip:port)
    responses:
      '200':
        description: Voice catalogue
      '503':
        description: No available hosts
    """
    resp = _check_local(request)
    if resp:
        return resp

    session = request.app["session"]
    hosts = _find_tts_hosts(request.query.get("host"))
    if not hosts:
        return web.json_response({"error": "no ComfyUI hosts available"}, status=503)

    async def probe(host):
        found = []
        try:
            r = await session.get(
                _host_url(host, "/object_info"),
                timeout=aiohttp.ClientTimeout(total=20),
            )
            if r.status != 200:
                await r.release()
                return host, found
            info = await r.json(content_type=None)
            await r.release()
        except Exception:
            return host, found
        if not isinstance(info, dict):
            return host, found
        seen = set()
        for fam in load_node_classifier():
            for node_class in info:
                if node_class in seen:
                    continue
                if not any(r.search(node_class) for r in fam["regs"]):
                    continue
                seen.add(node_class)
                required = ((info[node_class].get("input") or {}).get("required")) or {}
                voices = []
                voice_field = fam["voice"]
                entry = required.get(voice_field) if voice_field else None
                if isinstance(entry, list) and isinstance(entry[0], list):
                    voices = [v for v in entry[0] if isinstance(v, str)]
                found.append({"node": node_class, "voices": voices})
        return host, found

    results = await asyncio.gather(*(probe(h) for h in hosts), return_exceptions=True)
    voices, details = [], []
    seen = set()
    for res in results:
        if not isinstance(res, tuple):
            continue
        host, nodes = res
        if not nodes:
            continue
        details.append({"host": host, "nodes": nodes})
        for n in nodes:
            for v in n["voices"]:
                if v not in seen:
                    seen.add(v)
                    voices.append(v)
    return web.json_response({"voices": voices, "hosts": details})


def make_app():
    app = web.Application(client_max_size=128 * 1024 * 1024)

    async def on_startup(app):
        app["session"] = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
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
    swagger.add_get("/dashboard-data", handle_dashboard_data)
    swagger.add_get("/dashboard-models", handle_dashboard_models)
    swagger.add_get("/settings", handle_settings_get)
    swagger.add_post("/settings", handle_settings_post)
    swagger.add_post("/settings/test", handle_settings_test)
    swagger.add_post("/settings/import", handle_settings_import)
    swagger.add_get("/dashboard/server-count", handle_server_count)
    swagger.add_get("/v1/models", handle_v1_models)
    swagger.add_get("/clear-bad", handle_clear_bad)
    swagger.add_get("/next-host", handle_next_host)
    swagger.add_get("/skip-good", handle_skip_good)
    swagger.add_get("/api/tags", handle_api_tags)
    swagger.add_get("/api/ps", handle_api_ps)
    swagger.add_get("/api/version", handle_api_version)
    swagger.add_get("/api/activity", handle_api_activity)

    swagger.add_get("/refresh", handle_refresh)

    swagger.add_post("/api/show", handle_api_show)
    swagger.add_post("/api/stop", handle_ollama_stop)
    swagger.add_post("/api/pull", handle_api_pull)
    swagger.add_post("/api/chat", handle_ollama_chat)
    swagger.add_post("/api/generate", handle_ollama_generate)
    swagger.add_post("/v1/chat/completions", handle_openai_chat)
    swagger.add_post("/chat/completions", handle_openai_chat)
    swagger.add_get("/sdapi/v1/sd-models", handle_sd_models)
    swagger.add_post("/sdapi/v1/txt2img", handle_txt2img)
    swagger.add_get("/sdapi/v1/images", handle_image_history)
    swagger.add_get("/sdapi/v1/images/delete", handle_image_delete)
    swagger.add_get("/sdapi/v1/images/thumb/{name}", handle_image_thumb)
    swagger.add_get("/sdapi/v1/images/{name}", handle_image_file)
    swagger.add_post("/v1/audio/speech", handle_tts_speech)
    swagger.add_get("/v1/audio/voices", handle_audio_voices)
    # Registered on the plain router (not swagger) so path params and the
    # sendBeacon POST body aren't subject to swagger request validation.
    app.router.add_get("/api/chats", handle_chats_get)
    app.router.add_get("/api/chats/{cid}", handle_chat_get)
    app.router.add_post("/api/chats/{cid}", handle_chat_post)
    app.router.add_post("/api/chats/{cid}/delete", handle_chat_delete)

    app.router.add_route("*", "/comfyui/{tail:.*}", handle_comfyui_proxy)
    app.router.add_post("/v1/videos", handle_videos_post)
    app.router.add_get("/v1/videos/{id}", handle_videos_get)
    app.router.add_get("/v1/videos/{id}/content", handle_videos_content)

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
 l'>      DD  dD  YyyY   Vv  vV   aAAa   <-l
 ll       DD  dD   yY     VvvV   aA  Aa   ll
 llama~  DDDDd"   yY       VV   aA    Aa  llama~
 || ||               v{VERSION}               || ||
 '' ''               dibatag              '' ''
""")

def main():
    global TIMEOUT, PORT, WORKER_COUNT, _LOCAL, _CURLIFY

    parser = argparse.ArgumentParser(description="dumpster-dyva - Like the Ollama :cloud models, but you don't pay.")
    parser.add_argument("-p", "--port",     type=int, default=PORT, help=f"port to listen on (default: {PORT})")
    parser.add_argument("-u", "--host",     type=str, default="", help="host address to bind to (default: all interfaces)")
    parser.add_argument("-t", "--timeout",  type=int, default=30, help="request timeout in seconds (default: 30)")
    parser.add_argument("-r", "--refresh", nargs="?", const=True, default=False, metavar="SOURCE", help="refresh cache, optionally limited to one source name (e.g. graflex, forrany, spider, happyshua)")
    parser.add_argument("-w", "--workers",  type=int, default=3, help="number of workers (default: 3)")
    parser.add_argument("-l", "--local",    action="store_true", help="restrict inference endpoints to localhost only")
    parser.add_argument("--source", nargs="+", metavar=("CMD"),
                        help="manage additional host sources: '--source list' or '--source add <url>' "
                             "(the url returns a JSON list of source definitions, same as the dashboard's add-by-URL)")
    parser.add_argument("--curlify", action="store_true", help="print curl commands of upstream requests to stderr")
    parser.add_argument("-v", "--version",  action="store_true", help="show version information")
    args = parser.parse_args()

    if args.source:
        sys.exit(source_cli(args.source))

    if args.refresh:
        ok = refresh_cache(args.refresh if isinstance(args.refresh, str) else None)
        log.info("Refreshed cache" if ok else "Refresh failed")
        sys.exit(0 if ok else 1)

    banner()
    if args.version:
        sys.exit(0)

    WORKER_COUNT = args.workers
    TIMEOUT = args.timeout
    PORT = args.port
    _LOCAL = args.local
    _CURLIFY = args.curlify
    load_settings()   # persisted dashboard settings override the CLI defaults
    if args.local:       # an explicit -l flag always wins over saved settings
        _LOCAL = True
    log.info(f"Starting dumpster-dyva on port {PORT}, WORKER_COUNT={WORKER_COUNT}, TIMEOUT={TIMEOUT}, MIN_COUNT={MIN_COUNT}, LOCAL={_LOCAL}")

    app = make_app()

    def stop():
        log.debug("Shutting down")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        web.run_app(
            app,
            host=args.host or "0.0.0.0",
            port=PORT,
            access_log_format='%t %a "%r" %s %b "%{Referer}i" "%{User-Agent}i"',
            print=lambda *a: None,
        )

    except (KeyboardInterrupt, SystemExit):
        log.info("Server exiting by keyboard interrupt")
        pass

    except Exception as e:
        parser.print_help()
        print(f"\n  ----- ERROR -----\n [ Unable to Start ]\n  ==-------------==\n\n{e}\n\n")


if __name__ == "__main__":
    main()
