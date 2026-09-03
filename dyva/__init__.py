#!/usr/bin/env python3
import argparse
import asyncio
import base64
import collections
import contextlib
import datetime
import fnmatch
import csv
import json
import hashlib
import html as html_mod
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
import requests
import sqlite3
import importlib.metadata

import aiohttp
from aiohttp import web, web_log
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


# Console output uses the locale encoding, which on Windows is a codepage that
# can't represent the ✓/— we log. Keep the console's own encoding (so nothing
# turns to mojibake) but degrade an unencodable character instead of raising.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=getattr(logging, LOGLEVEL, logging.INFO),
    handlers=[logging.StreamHandler(sys.stderr)],
)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(ApacheStyleFormatter(LOG_FORMAT))
log = logging.getLogger("dumpster-dyva")
log.setLevel(getattr(logging, LOGLEVEL, logging.INFO))

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
# Generated speech kept on disk so a chat transcript can reference it by URL —
# the response body is raw bytes, which a saved conversation can't hold.
AUDIO_DIR = os.path.join(CACHE_DIR, "audio")
AUDIO_KEEP = 200
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
MODEL_LIST = []  # when non-empty, the exact model ids /api/tags and /v1/models advertise
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
VIDEO_KEY = "video"      # no wildcard: canon_pattern() eats a trailing "*"
# Video is the slowest thing here: seconds per frame, plus however long the
# host's queue is. Half an hour is patience, not a leak — the wait happens
# after the race, on a host that has already accepted the work.
VIDEO_RENDER_TIMEOUT = 1800


def video_key(model_filter=None):
    q = _sep_insensitive((model_filter or "").replace("*", "").replace("?", ""))
    return f"video/{q}" if q else VIDEO_KEY
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
            with open(CLASSIFIER_FILE, encoding="utf-8") as f:
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
    global WORKER_COUNT, TIMEOUT, MIN_COUNT, ADMIN_PW, _LOCAL, MODEL_LIST
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
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
    if "model_list" in s:
        MODEL_LIST = parse_model_list(s["model_list"])


def save_settings(extra=None):
    # merge into the existing file so unrelated keys survive
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.update({"workers": WORKER_COUNT, "timeout": TIMEOUT,
                 "min_count": MIN_COUNT, "local": _LOCAL,
                 "admin_pw": ADMIN_PW, "model_list": MODEL_LIST})
    if isinstance(extra, dict):
        data.update(extra)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _stored_sources():
    """Raw sources list as stored (for the editor), before validation."""
    if not os.path.exists(SETTINGS_FILE):
        return []
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
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
        with open(SETTINGS_FILE, encoding="utf-8") as f:
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
        print(f"{len(BUILTIN_SOURCES)} built-in source{'' if len(BUILTIN_SOURCES) == 1 else 's'}:")
        for s in BUILTIN_SOURCES:
            print(_source_line(s))
        print()
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


# Reputation keys are mostly model names, but a few are sentinels for the
# non-chat capabilities. UNREACHABLE_KEY holds a NUL so it can never collide
# with a real model name; give it a printable face for the CLI.
_KEY_ALIASES = {UNREACHABLE_KEY: "__unreachable__"}
_STATE_ALIASES = {"maybe": "maybe_good", "maybe-good": "maybe_good"}
_STATES = ("good", "maybe_good", "bad")


def _key_label(key):
    return _KEY_ALIASES.get(key, key)


def _hosts_usage():
    print("usage:", file=sys.stderr)
    print("  dyva --hosts                  summary of the host reputation table", file=sys.stderr)
    print("  dyva --hosts STATE            the keys marked STATE", file=sys.stderr)
    print("  dyva --hosts STATE KEY        the hosts carrying that mark", file=sys.stderr)
    print("  dyva --hosts STATE KEY del    clear those marks", file=sys.stderr)
    print("                                STATE/KEY works too, e.g. bad/__tts__", file=sys.stderr)
    print(f"\n  STATE is one of: {', '.join(_STATES)}", file=sys.stderr)


def _hosts_counts(state=None):
    """[(key, n)] for one state (or every state), commonest first."""
    db = _get_db()
    if state:
        rows = db.execute(
            "SELECT model, COUNT(*) FROM host_status WHERE state=? GROUP BY model ORDER BY 2 DESC, 1",
            (state,)).fetchall()
    else:
        rows = db.execute(
            "SELECT model, COUNT(*) FROM host_status GROUP BY model ORDER BY 2 DESC, 1").fetchall()
    return rows


def _print_key_table(rows):
    width = max((len(_key_label(k)) for k, _ in rows), default=0)
    for key, count in rows:
        print(f"  {_key_label(key):<{width}}  {count:>6}")


def hosts_cli(argv):
    """Handle `--hosts ...`: inspect and prune the host reputation table.

    The arguments narrow, left to right, and the verb comes last:

        --hosts                     every state, with counts
        --hosts bad                 the keys marked bad
        --hosts bad __tts__         the hosts carrying that mark
        --hosts bad __tts__ del     clear them

    Reading is the default, so `--hosts bad` shows you the bad ones rather than
    needing a word in front. Deleting still needs a named key: reputation is
    expensive to rebuild — every mark cost a real probe of a real host — so
    there is deliberately no way to clear a whole state at once.
    """
    words = [str(a) for a in argv]
    if words and words[0].lower() in ("list", "show"):
        words = words[1:]          # tolerated, from the older syntax
    # `bad/__edit__` reads more directly than `bad __edit__`; split it only when
    # the leading segment really is a state, since a model key may contain a
    # slash of its own (library/llama3).
    if words and "/" in words[0]:
        head, rest = words[0].split("/", 1)
        if _STATE_ALIASES.get(head.lower(), head.lower()) in _STATES and rest:
            words = [head, rest] + words[1:]
    doing = None
    if words and words[-1].lower() in ("del", "delete", "clear", "rm"):
        doing, words = "del", words[:-1]
    elif words and words[0].lower() in ("del", "delete", "clear", "rm"):
        # the old verb-first form still works rather than doing something
        # surprising with what follows
        doing, words = "del", words[1:]

    state = None
    if words:
        state = _STATE_ALIASES.get(words[0].lower(), words[0].lower())
        if state not in _STATES:
            print(f"unknown state '{words[0]}' (expected {', '.join(_STATES)})", file=sys.stderr)
            _hosts_usage()
            return 1
        words = words[1:]

    db = _get_db()

    # --hosts : every state, counts only
    if state is None:
        if doing:
            print("a state is required to delete", file=sys.stderr)
            _hosts_usage()
            return 1
        totals = dict(db.execute(
            "SELECT state, COUNT(*) FROM host_status GROUP BY state").fetchall())
        if not totals:
            print("host reputation table is empty")
            return 0
        for st in _STATES:
            print(f"{st}:")
            print(f"    {totals.get(st, 0)}")
        return 0

    rows = _hosts_counts(state)
    if not rows:
        print(f"no hosts marked '{state}'")
        return 0

    # --hosts <state> : the keys in that state
    if not words:
        total = sum(c for _, c in rows)
        print(f"{total} host mark{'' if total == 1 else 's'} in '{state}', "
              f"across {len(rows)} key{'' if len(rows) == 1 else 's'}:\n")
        _print_key_table(rows)
        if doing:
            # they asked to delete and got a listing, so say why
            print(f"\nName a key to clear — 'del' alone deletes nothing:")
            print(f"  dyva --hosts {state} {_key_label(rows[0][0])} del")
        return 0

    # resolve what was typed against the keys actually present, so the
    # printable alias for the NUL-bearing sentinel round-trips
    present = {k: c for k, c in rows}
    by_label = {_key_label(k).lower(): k for k in present}
    resolved, unknown = [], []
    for want in words:
        if want in present:
            resolved.append(want)
        elif want.lower() in by_label:
            resolved.append(by_label[want.lower()])
        else:
            unknown.append(want)
    if unknown:
        print(f"not marked '{state}': {', '.join(unknown)}", file=sys.stderr)
        print(f"known keys: {', '.join(_key_label(k) for k, _ in rows)}", file=sys.stderr)
        return 1

    # --hosts <state> <key> : who carries it
    if not doing:
        for key in resolved:
            hosts = [h for (h,) in db.execute(
                "SELECT host FROM host_status WHERE state=? AND model=? ORDER BY host",
                (state, key))]
            print(f"{_key_label(key)}: {len(hosts)} host{'' if len(hosts) == 1 else 's'} "
                  f"marked '{state}'")
            for h in hosts:
                print(f"  {h}")
            print()
        return 0

    # --hosts <state> <key> del
    cleared = 0
    for key in resolved:
        cur = db.execute("DELETE FROM host_status WHERE state=? AND model=?", (state, key))
        cleared += cur.rowcount
        print(f"cleared {cur.rowcount} '{state}' mark{'' if cur.rowcount == 1 else 's'} "
              f"for {_key_label(key)}")
    db.commit()
    if cleared:
        print("\nThose hosts are now unranked and will be retried on the next request.")
    return 0


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

# The `__dyva_info__:test` probe: a cheap factual question bogus servers get
# wrong (they often just parrot the prompt or echo roll tokens). A host only
# passes if its answer mentions "Washington" or "George". Cycling this through
# every candidate for a specific model is the expensive part the operator opts
# into explicitly, so it never runs in normal routing.
QUICK_TEST_TAG = "__dyva_info__:test"
QUICK_TEST_PROMPT = ("Do not be conversational. This is a test. "
                     "What is the name of the first United States President?")
QUICK_TEST_PASS_RE = re.compile(r"\b(Washington|George)\b", re.I)

_status_db = None
_servers_cache = None

_activity_queues = []

GITHUB_URL = "https://github.com/kristopolous/free-ollama"
_activity_history = []
_activity_lock = asyncio.Lock()
_ACTIVITY_HISTORY_MAX = 500

# Registry of currently-running race "jobs" that fan out across hosts to
# resolve (and serve) a model. Each job is keyed by a unique UUID so concurrent
# efforts for the same model don't clobber each other; the model string is kept
# only for display. Each entry tracks when the job started, how many hosts it's
# gone through (checked), the total candidates, and a stop handle to abort it.
_workers = {}
_WORKERS_MAX = 500
_worker_queues = []
_worker_lock = asyncio.Lock()


def _new_wid():
    return str(uuid.uuid4())


def _worker_snapshot():
    now = time.time()
    out = []
    for w in _workers.values():
        out.append({
            "wid": w["wid"],
            "model": w["model"],
            "started": w["started"],
            "age": round(now - w["started"], 1),
            "checked": w["checked"],
            "total": w["total"],
            "phase": w.get("phase"),
            "host": w.get("host"),
        })
    out.sort(key=lambda w: w["started"])
    return out


async def _broadcast_workers():
    payload = json.dumps({"workers": _worker_snapshot()})
    async with _worker_lock:
        dead = []
        for q in _worker_queues:
            try:
                await q.put(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            _worker_queues.remove(q)


async def _add_worker_listener(q):
    async with _worker_lock:
        _worker_queues.append(q)


async def _remove_worker_listener(q):
    async with _worker_lock:
        if q in _worker_queues:
            _worker_queues.remove(q)


async def _register_worker(model, total, stop, phase=None, host=None):
    wid = _new_wid()
    if len(_workers) >= _WORKERS_MAX:
        _workers.pop(next(iter(_workers)))
    _workers[wid] = {
        "wid": wid,
        "model": model,
        "started": time.time(),
        "checked": 0,
        "total": total,
        "stop": stop,
        "phase": phase,
        "host": host,
    }
    await _broadcast_workers()
    return wid


async def _worker_checked(wid):
    w = _workers.get(wid)
    if w:
        w["checked"] += 1
        await _broadcast_workers()


def _set_worker_phase(wid, phase):
    """Update a waiting job's phase in place — "queued #3" while it sits in the
    host's queue, "rendering" once it starts."""
    w = _workers.get(wid)
    if w:
        w["phase"] = phase


@contextlib.asynccontextmanager
async def _waiting_worker(label, host, phase="rendering"):
    """Show a job in the workers view while we wait on a host that already took
    it. The race registers a worker for *finding* a host; without this the job
    vanishes from the view for the whole render, which is the part that
    actually takes minutes."""
    wid = await _register_worker(label, 1, lambda: None, phase=phase, host=host)
    try:
        yield wid
    finally:
        await _unregister_worker(wid)


def is_active(host):
    """Is one of our own jobs already sitting on this host?

    Derived from the live worker registry rather than an "active hosts" set of
    our own, because a set like that needs whoever put a host in to take it
    back out — and the first time something raises on an unusual path, that
    host is stuck marked busy forever. Heartbeats and timeouts are the usual
    answers and they are all worse than not having the problem. The workers
    registry is already unwound by the context managers that create it, so it
    cannot leak; it also already records which host each job is talking to.

    There is a race here: a host is only listed once a job has actually taken
    it, so two requests that check within the same instant can both pick it.
    The window is milliseconds against renders that run for minutes, and
    losing it only costs the old behaviour, so it is not worth closing.
    """
    if not host:
        return False
    return any(w.get("host") == host for w in _workers.values())


def _idle_first(hosts):
    """Move hosts we are already using to the back, order otherwise intact.

    Not a hard filter: a busy host is still better than no host, and with one
    good ComfyUI on the network a filter would turn "wait your turn" into "no
    hosts available". Python's sort is stable, so the reputation ordering
    inside each group survives untouched.
    """
    return sorted(hosts, key=is_active)


async def _unregister_worker(wid):
    if wid in _workers:
        _workers.pop(wid, None)
        await _broadcast_workers()



async def broadcast_activity(host, model, status, message, duration=None, wid=None, aid=None,
                             rmodel=None):
    """Publish one activity event.

    `aid` gives the event a stable identity: the dashboard replaces the existing
    line with that id rather than prepending a new one, so a request that fans
    out over many hosts shows a single line that resolves in place instead of a
    trying/failed pair per host."""
    entry = {'host': host, 'model': model, 'status': status, 'message': message, 'time': time.time()}
    if aid is not None:
        entry['id'] = aid
    if rmodel:
        # what actually ran, as opposed to `model`, which is the routing key
        # (a query pattern, a prompt label, or a __capability__ sentinel)
        entry['rmodel'] = rmodel
    if duration is not None:
        entry['duration'] = round(duration, 2)
    # Mirror to stderr so the server log carries the same narrative as the
    # dashboard feed — otherwise a failure is only visible to whoever happens to
    # have the Activity pane open, and is gone once it scrolls past the cap.
    dur = f" [{entry['duration']:.2f}s]" if 'duration' in entry else ""
    (log.warning if status == "failed" else log.info)(f"activity{dur} {message}")
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


BUILTIN_SOURCES = [
    {
        "name": "forrany",
        "url": "https://raw.githubusercontent.com/forrany/Awesome-Ollama-Server/refs/heads/main/public/data.json",
    },
    {
        "name": "spider",
        "url": "https://raw.githubusercontent.com/PuddinCat/OllamaSpider/refs/heads/main/url_models.json",
    },
    {
        "name": "happyshua",
        "url": "https://raw.githubusercontent.com/happyshua/ollamalist/refs/heads/main/output_with_models.csv",
    },
]


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
       ( _normalize_url(s["url"]), f"{_db}-{_source_slug(s['name'])}.tmp", s["name"] ) for s in BUILTIN_SOURCES
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
        with open(loc, "w", encoding="utf-8", errors="replace") as f:
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
        with open(loc, encoding="utf-8", errors="replace") as f:
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
      with open(f'{_db}-forrany.tmp', 'r', encoding="utf-8", errors="replace") as f:
        try:
          for row in json.loads(f.read()):
            if row.get('server') not in host_map:
                row['source'] = 'forrany'
                host_map[row.get('server')] = row
        except Exception as ex:
          logging.warning(f"Unable to parse {_db}-forrany.tmp: {ex}")

    if os.path.exists(f"{_db}-happyshua.tmp"):
      with open(f"{_db}-happyshua.tmp", 'r', encoding="utf-8", errors="replace", newline="") as csvfile:
        for r in csv.reader(csvfile):
          ip = re.sub(r'/?v1', '', r[0])
          models = [m.strip() for m in r[1].split(',')]
          if ip not in host_map:
            host_map[ip] = {'source': 'happyshua', 'models': [], 'server': ip, 'version': ''}
    
          host_map[ip]['models'] += models

    if os.path.exists(f"{_db}-spider.tmp"):
      with open(f'{_db}-spider.tmp', 'r', encoding="utf-8", errors="replace") as f:
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

    with open(_db, 'w', encoding="utf-8") as f:
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
            with open(CACHE_FILE, encoding="utf-8") as f:
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
        # What custom nodes a host actually exposes, per capability. Probing
        # this means pulling a multi-megabyte /object_info, so the answer is
        # worth keeping: it makes the voice list instant instead of a live
        # sweep, and it tells host ranking which handful of hosts out of
        # hundreds can do speech at all. `node` NULL means "surveyed, has
        # none" — a real finding, not a gap.
        _status_db.execute(
            "CREATE TABLE IF NOT EXISTS host_nodes("
            "host TEXT NOT NULL, capability TEXT NOT NULL, node TEXT, "
            "voices TEXT, spec TEXT, checked REAL NOT NULL, "
            "rev INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(host, capability))")
        try:
            _status_db.execute("ALTER TABLE host_nodes ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass        # already there
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
        with open(path, encoding="utf-8") as f:
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


NODE_SURVEY_TTL = 24 * 3600


# Bump when the rules that *choose* a node change. A survey is a cached
# decision, not just cached data: without this, tightening the viability check
# left every already-surveyed host still pointing at the node it had picked
# under the old rules — which is why FB_Qwen3TTSVoiceDesign kept being used
# long after it was ruled out.
NODE_SURVEY_REV = 2


def save_node_survey(host, capability, spec, voices=None):
    """Record what a host exposes for a capability. spec=None means surveyed
    and found nothing, which is itself worth remembering."""
    db = _get_db()
    db.execute(
        "INSERT INTO host_nodes(host,capability,node,voices,spec,checked,rev)"
        " VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(host,capability) DO UPDATE SET"
        " node=excluded.node, voices=excluded.voices, spec=excluded.spec,"
        " checked=excluded.checked, rev=excluded.rev",
        (host, capability, (spec or {}).get("class"),
         json.dumps(voices or []), json.dumps(spec) if spec else None,
         time.time(), NODE_SURVEY_REV))
    db.commit()


def load_node_survey(capability):
    """{host: {"node":..., "voices":[...], "spec":{...}, "checked":ts}}"""
    out = {}
    for host, node, voices, spec, checked in _get_db().execute(
            "SELECT host, node, voices, spec, checked FROM host_nodes"
            " WHERE capability=? AND rev=?", (capability, NODE_SURVEY_REV)):
        try:
            out[host] = {"node": node, "voices": json.loads(voices or "[]"),
                         "spec": json.loads(spec) if spec else None,
                         "checked": checked}
        except Exception:
            continue
    return out


def node_survey_for(host, capability, ttl=NODE_SURVEY_TTL):
    """A single host's stored survey, or None if absent or stale."""
    row = _get_db().execute(
        "SELECT node, voices, spec, checked, rev FROM host_nodes"
        " WHERE host=? AND capability=?", (host, capability)).fetchone()
    if not row or time.time() - (row[3] or 0) > ttl:
        return None
    if (row[4] or 0) != NODE_SURVEY_REV:
        return None     # surveyed under older selection rules; decide again
    try:
        return {"node": row[0], "voices": json.loads(row[1] or "[]"),
                "spec": json.loads(row[2]) if row[2] else None,
                "checked": row[3]}
    except Exception:
        return None


def load_unreachable():
    return {h for (h,) in _get_db().execute(
        "SELECT host FROM host_status WHERE model=? AND state='bad'",
        (UNREACHABLE_KEY,)).fetchall()}


def load_last():
    global _last_cache
    if os.path.exists(LAST_FILE):
        with open(LAST_FILE, encoding="utf-8") as f:
            _last_cache = json.load(f)
    else:
        _last_cache = {}


def save_last():
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(LAST_FILE, "w", encoding="utf-8") as f:
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


_DATA_URL_RE = re.compile(r"^data:([^;,]*?)(;base64)?,(.*)$", re.S)


def _part_text(part):
    """The text a content part carries, whichever spelling it uses."""
    for k in ("text", "input_text"):
        v = part.get(k)
        if isinstance(v, str):
            return v
    return ""


def _flatten_content(messages):
    """Collapse OpenAI-style content *arrays* into Ollama's native shape.

    Ollama declares messages[].content as a Go string, so forwarding a
    multimodal array verbatim earns a 400 from every host in the race:
      json: cannot unmarshal array into Go struct field
      ChatRequest.messages.content of type string
    Text parts are joined into content; inline data-URL images move to the
    sibling `images` list as bare base64, which is where Ollama looks for them
    (and is what needs_caps() reads to require a vision model).
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            out.append(m)
            continue
        m = dict(m)
        texts = []
        images = list(m.get("images") or [])
        for part in m["content"]:
            if isinstance(part, str):
                texts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            t = part.get("type") or ""
            if t in ("text", "input_text") or (not t and "text" in part):
                texts.append(_part_text(part))
            elif t in ("image_url", "input_image", "image"):
                url = part.get("image_url") or part.get("url") or ""
                if isinstance(url, dict):
                    url = url.get("url") or ""
                mt = _DATA_URL_RE.match(url) if isinstance(url, str) else None
                if mt and mt.group(2):
                    images.append(mt.group(3))
                elif url:
                    # A remote image we can't inline: say it exists rather than
                    # dropping it silently.
                    texts.append(f"[image: {url}]")
            elif t == "file":
                f = part.get("file") if isinstance(part.get("file"), dict) else {}
                name = f.get("filename") or part.get("filename") or "file"
                data = f.get("file_data") or part.get("file_data") or ""
                raw = ""
                mt = _DATA_URL_RE.match(data) if isinstance(data, str) else None
                if mt and mt.group(2):
                    try:
                        raw = base64.b64decode(mt.group(3)).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        raw = ""
                elif isinstance(data, str) and data and not mt:
                    raw = data
                if raw:
                    texts.append(f"--- File: {name} ---\n{raw}")
                else:
                    texts.append(f"[attached file: {name} (contents not text)]")
            else:
                # Unknown part type (audio, refusal, ...): keep any text on it.
                extra = _part_text(part)
                if extra:
                    texts.append(extra)
        m["content"] = "\n".join(t for t in texts if t)
        if images:
            m["images"] = images
        out.append(m)
    return out


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
                with open(KNOWN_FILE, encoding="utf-8") as f:
                    _knowns_cache = json.load(f)
            except Exception:
                _knowns_cache = {}
    return _knowns_cache


def save_knowns():
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(KNOWN_FILE, "w", encoding="utf-8") as f:
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


# Priority tiers, best first:
#   recent (last success) -> good -> maybe_good -> unknown -> bad
# One definition, used by chat (find_servers) and by the capability endpoints
# (tts, txt2img, ...) which key their reputation on a __sentinel__ instead of a
# model name. last/good/maybe deliberately outrank a stale bad mark, so a host
# with one transient failure isn't buried behind the unreachable junk; a host
# dead at the connection level drops to the bad tier for everything at once.
TIER_LAST, TIER_GOOD, TIER_MAYBE, TIER_UNKNOWN, TIER_BAD = -3, -2, -1, 0, 1


def host_tier(is_last, in_good, in_maybe, in_bad, unreachable=False):
    if unreachable:
        return TIER_BAD
    if is_last:
        return TIER_LAST
    if in_good:
        return TIER_GOOD
    if in_maybe:
        return TIER_MAYBE
    if in_bad:
        return TIER_BAD
    return TIER_UNKNOWN


def capability_tier(host, key, marks, last_host, extra_bad=False):
    """host_tier for a capability sentinel key (__tts__, __a1111__, ...).

    `marks` is (good, maybe, bad, unreachable) loaded once by the caller.
    `extra_bad` lets a capability fold in its own hard negative — for TTS, a
    fresh probe that found no usable node is better evidence than a stale mark.
    """
    good, maybe, bad, unreachable = marks
    k = f"{host} {key}"
    return host_tier(host == last_host, k in good, k in maybe,
                     extra_bad or k in bad, host in unreachable)


def load_marks():
    return load_good(), load_maybe(), load_bad(), load_unreachable()


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
        _last = get_last(sub)
        if _last is None and not sub:
            if _last_cache is None:
                load_last()
            _last = next(
                ((v["host"], v.get("full", "")) for v in _last_cache.values()
                 if v.get("host") == host),
                None)
        is_last = _last is not None and host == _last[0]
        prio = host_tier(is_last, in_good, in_maybe, in_bad, host in unreachable)
        # Within the UNKNOWN tier only, try the most-recently-checked hosts
        # first (recently reachable => likelier still up). Other tiers keep
        # their existing order via a constant secondary key (stable sort).
        crank = _checked_rank(s) if prio == TIER_UNKNOWN else 0
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


def parse_model_list(v):
    """Accept the override as a list of names or as one newline/comma-separated
    blob (what the dashboard textarea sends). Blank lines and #comments drop out,
    order and duplicates-after-the-first do not."""
    if isinstance(v, str):
        v = re.split(r"[\r\n,]+", v)
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        name = str(item or "").strip()
        if not name or name.startswith("#"):
            continue
        if name not in out:
            out.append(name)
    return out


def listed_models():
    """Models for third-party enumeration (/api/tags, /v1/models), with the
    optional MIN_COUNT floor applied. The dashboard uses all_models() directly
    and always shows everything.

    A non-empty MODEL_LIST replaces the discovered catalog outright. Tools that
    make you pick one id from this endpoint then get a short, stable menu, and
    because every dyva model name is a routing pattern, one entry like "qwen3"
    covers every qwen3:* variant any host happens to carry. MIN_COUNT is not
    applied to an explicit list — the operator asked for these names by hand."""
    if MODEL_LIST:
        out = []
        for name in MODEL_LIST:
            try:
                count = len(find_servers(name))
            except Exception:
                count = 0
            out.append({"id": name, "count": count, "modalities": model_modalities(name)})
        return out
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


# ---- What a capability reports back about one host -----------------------
# The engine hands out hosts and the job judges them; a bool can't carry the
# distinction the reputation store (and the survey behind it) actually wants.
# "This host has no TTS node" is a structural fact about what is installed out
# there; "this host timed out" is churn. Both used to become add_bad().
V_ACCEPTED = "accepted"        # it worked — take the result
V_UNSUITABLE = "unsuitable"    # structurally can't do this; still won't in an hour
V_UNREACHABLE = "unreachable"  # dead at the connection level, for every capability
V_FAILED = "failed"            # transient: refused, errored, bad response
V_TIMEOUT = "timeout"          # transient: accepted the connection, never answered
V_SKIP = "skip"                # inconclusive — move on, record nothing

Outcome = collections.namedtuple("Outcome", "verdict result extra detail")


def accepted(result, extra=""):
    """`extra` is the concrete thing that worked (chat's resolved model name),
    stored alongside the host as the sticky last-success."""
    return Outcome(V_ACCEPTED, result, extra, None)


def unsuitable(detail=None):
    return Outcome(V_UNSUITABLE, None, "", detail)


def unreachable_host(detail=None):
    return Outcome(V_UNREACHABLE, None, "", detail)


def failed(detail=None):
    return Outcome(V_FAILED, None, "", detail)


def timed_out(detail=None):
    return Outcome(V_TIMEOUT, None, "", detail)


def skip(detail=None):
    return Outcome(V_SKIP, None, "", detail)


def record_verdict(host, key, outcome):
    """The one place a race turns a verdict into reputation."""
    v = outcome.verdict
    if v == V_ACCEPTED:
        add_good(host, key)
        set_last(key, host, outcome.extra or "")
    elif v == V_UNREACHABLE:
        # host-wide, so it isn't re-probed as "unknown" for every other key...
        mark_unreachable(host)
        add_bad(host, key)          # ...plus the per-key mark, so looking at
    elif v in (V_UNSUITABLE, V_FAILED, V_TIMEOUT):   # this key shows who failed it
        add_bad(host, key)
    # V_SKIP records nothing on purpose


async def _race_hosts(entries, attempt, key, job_wid=None, workers=None, host_of=None,
                      label=None):
    """The host race: run `attempt` against candidate hosts in parallel, keep the
    first success, cancel the rest.

    This is the scaffolding every capability shares — a bounded worker pool
    drawing from one best-first candidate list, a done-event so losers stop as
    soon as someone wins, the /workers registry with its stop button, and error
    collection. `attempt` supplies only the part that differs per capability.

      entries - candidate items, best-first; passed to `attempt` untouched
      attempt - async fn(item, wid, done) -> Outcome. The job decides what
                "worked" means; the engine only reads the verdict.
      key     - the reputation/worker key: a model name, or a __capability__
                sentinel like __tts__
      host_of - pulls the host out of an entry (default: the entry is the host)
      label   - what the /workers view calls this job (default: the key). The
                reputation key and the human label are not the same thing —
                txt2img wants the prompt there, not "__a1111__".

    Returns (result, stopped, tried, tally). `tried` is how many hosts were
    actually attempted — not len(entries), since the pool stops early on
    success — and `tally` counts the verdicts, which is what a failure message
    should quote.
    """
    done = asyncio.Event()
    result_queue = asyncio.Queue()
    entry_iter = iter(entries)
    iter_lock = asyncio.Lock()
    stopped = False
    tried = 0
    tally = collections.Counter()
    host_of = host_of or (lambda item: item)

    async def worker():
        nonlocal tried
        while not done.is_set():
            async with iter_lock:
                try:
                    item = next(entry_iter)
                except StopIteration:
                    return
                tried += 1
            await _worker_checked(wwid)
            wid = asyncio.current_task().get_name()
            outcome = await attempt(item, wid, done)
            if outcome is None:
                outcome = skip()
            tally[outcome.verdict] += 1
            record_verdict(host_of(item), key, outcome)
            if outcome.verdict == V_ACCEPTED:
                await result_queue.put(outcome.result)
                done.set()
                return

    _tasks_holder = []

    def _stop():
        nonlocal stopped
        stopped = True
        done.set()
        for t in _tasks_holder:
            t.cancel()

    if job_wid is not None:
        _workers[job_wid]["stop"] = _stop
        wwid = job_wid
    else:
        wwid = await _register_worker(label or key, len(entries), _stop)
    n = min(workers or WORKER_COUNT, len(entries)) or 1
    tasks = [asyncio.create_task(worker()) for _ in range(n)]
    _tasks_holder[:] = tasks
    try:
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
    finally:
        if job_wid is None:
            await _unregister_worker(wwid)

    return result, stopped, tried, tally


# Which dialect a host speaks. dyva has always recorded the service; the chat
# path just ignored it and POSTed Ollama's /api/chat at everything, so LM
# Studio, vLLM and llama.cpp answered "Unexpected endpoint or method" and were
# marked bad. They are 38% of the chat pool and they work fine — in their own
# language.
# sglang serves an OpenAI-compatible API; graflex labels a host sglang exactly
# because it answered /api/tags but not /api/version, which is an ollama-shim
# detail and says nothing about how you talk to it for inference.
_OPENAI_SERVICES = {"lmstudio", "vllm", "llama.cpp", "llamacpp", "sglang",
                    "tabby", "koboldcpp", "text-generation-webui", "openai"}
_service_index = None


def service_of(host):
    global _service_index
    if _service_index is None:
        _service_index = {s.get("server"): s.get("service")
                          for s in load_servers() if s.get("server")}
    return _service_index.get(host) or _service_index.get(_norm_host(host)) or "ollama"


def speaks_openai(host):
    return service_of(host) in _OPENAI_SERVICES


def chat_endpoint(host, ollama_endpoint="/api/chat"):
    if not speaks_openai(host):
        return ollama_endpoint
    return ("/v1/completions" if ollama_endpoint == "/api/generate"
            else "/v1/chat/completions")


# Ollama-only knobs that an OpenAI server will either reject or ignore.
_OLLAMA_ONLY = ("options", "keep_alive", "format", "template", "context", "raw",
                "system", "think", "images")


def openai_payload(p):
    out = {k: v for k, v in p.items() if k not in _OLLAMA_ONLY}
    opts = p.get("options") or {}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("num_predict", "max_tokens"), ("seed", "seed"),
                     ("stop", "stop")):
        if src in opts:
            out[dst] = opts[src]
    return out


def openai_to_ollama(data, model):
    """One non-streaming OpenAI completion, in Ollama's shape."""
    ch = ((data.get("choices") or [{}])[0]) or {}
    msg = dict(ch.get("message") or {})
    if msg.get("tool_calls"):
        msg["tool_calls"] = [
            {"function": {"name": (t.get("function") or {}).get("name"),
                          "arguments": _maybe_json((t.get("function") or {}).get("arguments"))}}
            for t in msg["tool_calls"]]
    out = {"model": data.get("model") or model, "created_at": _now_iso(),
           "message": {"role": msg.get("role") or "assistant",
                       "content": msg.get("content") or ""},
           "done": True, "done_reason": ch.get("finish_reason") or "stop"}
    if msg.get("tool_calls"):
        out["message"]["tool_calls"] = msg["tool_calls"]
    usage = data.get("usage") or {}
    if usage:
        out["prompt_eval_count"] = usage.get("prompt_tokens")
        out["eval_count"] = usage.get("completion_tokens")
    return out


def openai_stream_to_ollama(line, model):
    """One SSE line to one Ollama NDJSON object, or None to skip.

    OpenAI streams `data: {...}` with the text in choices[0].delta and a final
    `data: [DONE]`; Ollama streams bare JSON objects with a `done` flag. The
    rest of the pipeline speaks Ollama, so normalise here and nothing
    downstream has to care which kind of host answered.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    if line == "[DONE]":
        return {"model": model, "created_at": _now_iso(), "message": {"role": "assistant", "content": ""},
                "done": True, "done_reason": "stop"}
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if obj.get("error"):
        return {"error": obj["error"] if isinstance(obj["error"], str)
                else (obj["error"].get("message") or str(obj["error"]))}
    ch = ((obj.get("choices") or [{}])[0]) or {}
    delta = dict(ch.get("delta") or ch.get("message") or {})
    fin = ch.get("finish_reason")
    msg = {"role": delta.get("role") or "assistant", "content": delta.get("content") or ""}
    if delta.get("tool_calls"):
        msg["tool_calls"] = [
            {"function": {"name": (t.get("function") or {}).get("name"),
                          "arguments": _maybe_json((t.get("function") or {}).get("arguments"))}}
            for t in delta["tool_calls"]]
    out = {"model": obj.get("model") or model, "created_at": _now_iso(), "message": msg,
           "done": bool(fin)}
    if fin:
        out["done_reason"] = fin
    return out


def _maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def _race_servers(session, model, servers, payload, do_stream, endpoint="/api/chat", remote=None, caps=None, job_wid=None):
    errors = []
    errors_lock = asyncio.Lock()

    async def _collect_err(msg):
        async with errors_lock:
            errors.append(msg)

    async def attempt(item, wid, done):
        prio, host, ms = item
        resp = None
        full = ms[0]
        await broadcast_activity(host, model, "trying",
            f"trying: {host} for {model}", wid=wid)

        # last-used / known-good hosts go straight to the request;
        # untested ones get probed first, caps refreshed only if needed
        trusted = prio < 0
        if not trusted and not await probe_host(session, host):
            await broadcast_activity(host, model, "failed",
                f"unreachable: {host}")
            return unreachable_host("probe failed")
        if not trusted and caps and caps != {"completion"}:
            await refresh_host_caps(session, host)

        if "vision" in (caps or []) and not _known_has(host, full, "vision"):
            sees = await trial_balloon(session, host, full, model)
            if sees is None:
                return skip("trial balloon inconclusive")
            if sees:
                mark_vision(host, full)
                set_last(model, host, full)
                await broadcast_activity(host, model, "trying",
                    f"trial balloon: {full} has vision", wid=wid)
            else:
                mark_no_vision(host, full)
                await broadcast_activity(host, model, "failed",
                    f"trial balloon: {full} has no vision", wid=wid)
                # No reputation mark: vision-ness lives in known-hosts.json, and
                # the model itself is fine — just not for this request.
                return skip("no vision")

        start = time.time()
        tag = f"{host} {full}"
        # Speak the host's own dialect. If the recorded service turns out to be
        # wrong, the other dialect is tried once before writing the host off —
        # "Unexpected endpoint or method" is a labelling problem, not a broken
        # host, and 38% of the pool speaks OpenAI rather than Ollama.
        oai = speaks_openai(host)
        resp = None
        try:
            for attempt_oai in (oai, not oai):
                ep = chat_endpoint(host, endpoint) if attempt_oai else endpoint
                p = dict(payload, model=full, stream=do_stream)
                if attempt_oai:
                    p = openai_payload(p)
                _curlify("POST", f"{host}{ep}", p)
                resp = await asyncio.wait_for(
                    session.post(f"{host}{ep}", json=p), timeout=TIMEOUT)
                if resp.status not in (404, 405, 501):
                    oai = attempt_oai
                    break
                peek = ""
                try:
                    peek = (await resp.text())[:200]
                except Exception:
                    pass
                await resp.release()
                resp = None
                log.debug(f"{host}: {ep} -> {peek[:80]}; trying the other dialect")
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            dur = time.time() - start
            await broadcast_activity(host, model, "failed",
                f"failure: {host} for {model} - {type(e).__name__}", duration=dur, wid=wid)
            return (timed_out(type(e).__name__) if isinstance(e, asyncio.TimeoutError)
                    else failed(type(e).__name__))
        if resp is None:
            dur = time.time() - start
            await broadcast_activity(host, model, "failed",
                f"failure: {host} for {model} - no chat endpoint (tried both dialects)",
                duration=dur, wid=wid)
            return unsuitable("no chat endpoint (tried /api/chat and /v1/chat/completions)")

        if resp.status != 200:
            dur = time.time() - start
            code = resp.status
            try:
                raw = await resp.read()
                body = raw.decode('utf-8', errors='replace')[:500]
                log_upstream(code, host, ep, body, remote=remote)
                await _collect_err(f"{host}: {body}")
            except Exception:
                pass
            await resp.release()
            resp = None
            await broadcast_activity(host, model, "failed",
                f"failure: {host} for {model} - status {code}", duration=dur, wid=wid)
            return failed(f"status {code}")

        if not do_stream:
            try:
                data = await resp.json()
            except asyncio.TimeoutError:
                dur = time.time() - start
                await resp.release()
                resp = None
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - timeout", duration=dur, wid=wid)
                return skip("read timeout")
            except json.JSONDecodeError:
                dur = time.time() - start
                await resp.release()
                resp = None
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - bad response", duration=dur, wid=wid)
                return failed("bad response")
            await resp.release()
            if oai and "choices" in data:
                data = openai_to_ollama(data, full)
            if "error" in data:
                dur = time.time() - start
                await _collect_err(f"{host}: {data['error']}")
                await broadcast_activity(host, model, "failed",
                    f"failure: {host} for {model} - error: {data['error']}", duration=dur, wid=wid)
                return failed(str(data["error"]))
            dur = time.time() - start
            log.debug(f"  \u2713 {tag}")
            await broadcast_activity(host, model, "connected",
                f"success: {host} for {model}", duration=dur, wid=wid, rmodel=full)
            return accepted(("ok", host, full, data), extra=full)

        try:
            first_line = await resp.content.readline()
            if oai:
                # step over SSE keep-alives and blank separators to reach the
                # first line that actually carries content, and hand the rest
                # of the pipeline an Ollama-shaped object
                for _ in range(20):
                    conv = openai_stream_to_ollama(first_line, full)
                    if conv is not None:
                        first_line = json.dumps(conv).encode()
                        break
                    first_line = await resp.content.readline()
                    if not first_line:
                        break
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
            await resp.release()
            resp = None
            return skip("stream read failed")
        if not first_line or not first_line.strip():
            dur = time.time() - start
            await resp.release()
            resp = None
            await broadcast_activity(host, model, "failed",
                f"failure: {host} for {model} - empty response", duration=dur, wid=wid)
            return failed("empty response")

        try:
            first = json.loads(first_line)
        except json.JSONDecodeError:
            dur = time.time() - start
            await resp.release()
            resp = None
            await broadcast_activity(host, model, "failed",
                f"failure: {host} for {model} - bad response", duration=dur, wid=wid)
            return failed("bad response")

        if "error" in first:
            dur = time.time() - start
            await resp.release()
            resp = None
            await _collect_err(f"{host}: {first['error']}")
            await broadcast_activity(host, model, "failed",
                f"failure: {host} for {model} - error: {first['error']}", duration=dur, wid=wid)
            return failed(str(first["error"]))

        if done.is_set():
            await resp.release()
            return skip("another host won")

        dur = time.time() - start
        log.debug(f"  \u2713 {tag}")
        await broadcast_activity(host, model, "connected",
            f"success: {host} for {model}", duration=dur, wid=wid, rmodel=full)
        return accepted(("ok_stream", host, full, resp, first_line, first, oai), extra=full)

    result, stopped, _tried, _tally = await _race_hosts(
        servers, attempt, model, job_wid=job_wid, host_of=lambda it: it[1])
    return result, errors, stopped


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


async def _forward_stream(request, response, resp, first_line, host, full, openai_format,
                          upstream_openai=False):
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
            if upstream_openai:
                # the host speaks SSE; everything below this point speaks Ollama
                conv = openai_stream_to_ollama(line, full)
                if conv is None:
                    continue
                line = json.dumps(conv).encode()
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
    import re
    
    pattern = re.compile(r"^\s*" + re.escape(needle))
    
    prompt = str(payload.get("prompt") or "")
    if pattern.match(prompt):
        return True
    msgs = payload.get("messages") or []
    if not msgs:
        return False
    m = msgs[-1]
    c = m.get("content")
    if isinstance(c, str) and pattern.match(c):
        return True
    if isinstance(c, list):
        for part in c:
            if isinstance(part, dict):
                text = str(part.get("text") or "")
                if pattern.match(text):
                    return True
    return False


def _has_info_tag(payload):
    return _content_contains(payload, DYVA_INFO_TAG)


def _info_wants_next(payload):
    """`__dyva_info__:next` — same diagnostic, but first skip the model's sticky
    last-successful host (like the /next-host endpoint) so the reported host is
    the *next* one a real request would land on."""
    return _content_contains(payload, DYVA_INFO_TAG + ":next")


def _info_wants_test(payload):
    """`__dyva_info__:test` — don't just report where the request would land,
    actually *probe* the candidate hosts with the quick factual question and
    mark bad any that answer wrong, returning the first one that passes."""
    return _content_contains(payload, DYVA_INFO_TAG + ":test")


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


def _quick_test_answer_ok(content):
    """True if a candidate host's answer to the quick test names the first US
    president (Washington or George)."""
    return bool(content) and QUICK_TEST_PASS_RE.search(content) is not None


async def _run_info_test(session, model_in, tools=None):
    """Cycle the quick factual probe through the candidate hosts for `model_in`.

    Unlike `__dyva_info__`/`:next`, which only *report* the routing choice, this
    actually performs one cheap inference per candidate and marks the host bad
    when it answers wrong (or fails outright), so the operator can walk a bogus
    match down the list and cull the deadbeats. Returns the info dict of the
    first host that *passes* the test, or None if none do.

    `tools` mirrors the real request: a fake server that can't speak the tool
    schema must be caught by the test too (e.g. it 400s on a `tools` payload),
    otherwise the test would pass a host the real request always fails on. When
    tools are present the candidate pool is also filtered to tool-capable hosts,
    exactly as a real tools-enabled request would be routed.
    """
    req_caps = needs_caps([])
    if tools:
        req_caps = sorted(set(req_caps) | {"tools"})
    servers = find_servers(model_in, req_caps)
    for _prio, host, ms in servers:
        if ms:
            full = ms[0]
        else:
            continue
        wid = asyncio.current_task().get_name()
        payload = {
            "model": full,
            "messages": [{"role": "user", "content": QUICK_TEST_PROMPT}],
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        _curlify("POST", f"{host}/api/chat", payload)
        start = time.time()
        await broadcast_activity(host, model_in, "trying",
            f"quick test: {host} for {model_in}", wid=wid)
        try:
            resp = await asyncio.wait_for(
                session.post(f"{host}/api/chat", json=payload),
                timeout=TIMEOUT,
            )
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            add_bad(host, model_in)
            await broadcast_activity(host, model_in, "failed",
                f"quick test: {host} for {model_in} - {type(e).__name__}",
                duration=time.time() - start, wid=wid)
            continue
        if resp.status != 200:
            await resp.release()
            add_bad(host, model_in)
            await broadcast_activity(host, model_in, "failed",
                f"quick test: {host} for {model_in} - status {resp.status}",
                duration=time.time() - start, wid=wid)
            continue
        try:
            data = await resp.json()
        except Exception:
            await resp.release()
            add_bad(host, model_in)
            await broadcast_activity(host, model_in, "failed",
                f"quick test: {host} for {model_in} - bad response",
                duration=time.time() - start, wid=wid)
            continue
        await resp.release()
        content = (data.get("message") or {}).get("content") or ""
        dur = time.time() - start
        shown = content.strip()[:160]
        if "error" in data:
            add_bad(host, model_in)
            await broadcast_activity(host, model_in, "failed",
                f"quick test: {host} for {model_in} - error: {data['error']}",
                duration=dur, wid=wid)
            continue
        if not _quick_test_answer_ok(content):
            force_bad(host, model_in)
            log.warning(f"quick test FAIL {host} ({full}): answer={shown!r}")
            await broadcast_activity(host, model_in, "failed",
                f"quick test: {host} for {model_in} - wrong answer: {shown!r}",
                duration=dur, wid=wid)
            continue
        set_last(model_in, host, full)
        add_good(host, model_in)
        log.info(f"quick test PASS {host} ({full}): answer={shown!r}")
        await broadcast_activity(host, model_in, "connected",
            f"quick test: {host} for {model_in} - passes. answer: {shown!r}",
            duration=dur, wid=wid)
        return {"host": _norm_host(host), "model": full}
    return None


async def _proxy_chat(request, session, model_in, opayload, do_stream, openai_format):
    if '/' in model_in:
        model_list = model_in.split('/')
    else:
        model_list = [model_in]

    # Do this before needs_caps(): flattening lifts inline images out to the
    # `images` list, which is the signal needs_caps() uses to require vision.
    if isinstance(opayload.get("messages"), list):
        opayload = dict(opayload, messages=_flatten_content(opayload["messages"]))

    req_caps = needs_caps(opayload.get("messages", []))
    if opayload.get("tools"):
        req_caps = sorted(set(req_caps) | {"tools"})

    if _has_info_tag(opayload):
        if _info_wants_test(opayload):
            info = await _run_info_test(session, model_list[0], tools=opayload.get("tools"))
            if info is None:
                return web.json_response(err_obj(f"no server for '{model_in}' passed the quick test", "model_not_found"), status=404)
            return _info_response(model_list[0], info, do_stream=do_stream, openai_format=openai_format)
        exclude = None
        if _info_wants_next(opayload):
            prev = get_last(model_list[0])
            exclude = prev[0] if prev else None
            _drop_last_host(model_list[0])
        info = _would_use(model_list[0], req_caps, exclude=exclude)
        if info is None:
            return web.json_response(err_obj(f"no available servers for '{model_in}'", "model_not_found"), status=404)
        return _info_response(model_list[0], info, do_stream=do_stream, openai_format=openai_format)

    _servers = find_servers(model_in, req_caps)
    total = len(_servers)
    job_wid = await _register_worker(model_list[0], total, lambda: None)
    try:
        last = get_last(model_list[0])
        last_host_err = None
        if last and _known_capable(last[0], last[1], req_caps) and ("vision" not in req_caps or _known_has(last[0], last[1], "vision")):
            last_host, last_full = last
            log.debug(f"Reusing {last_host} for {model_list[0]}")
            await _worker_checked(job_wid)
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

        if not _servers:
            err_msg = f"no available servers for '{model_in}'"
            if last_host_err:
                err_msg += f": {last_host_err}"
            return web.json_response(err_obj(err_msg, "model_not_found"), status=404)

        stopped = False
        for model in model_list:
            if do_stream:
                result, errors, stopped = await _race_servers(session, model, _servers, opayload, do_stream=True, remote=request.remote, caps=req_caps, job_wid=job_wid)
                if result:
                    _, host, full, resp, first_line, first, _oai = result
                    stream_resp = web.StreamResponse()
                    await _forward_stream(request, stream_resp, resp, first_line, host, full, openai_format,
                                          upstream_openai=_oai)
                    return stream_resp
                msg = "worker manually stopped" if stopped else "all servers failed"
                if errors:
                    msg += ": " + "; ".join(dict.fromkeys(errors))
                if openai_format:
                    return web.Response(
                        text=sse_str({"error": msg}) + sse_str(sse_chunk("", {}, done=True)),
                        content_type="text/event-stream",
                    )
            else:
                result, errors, stopped = await _race_servers(session, model, _servers, dict(opayload, stream=False), do_stream=False, remote=request.remote, caps=req_caps, job_wid=job_wid)

                if result:
                    _, host, full, data = result
                    resp = chat_fmt(data, model, openai_format)
                    resp.headers["X-Dyva-Host"] = re.sub(r"^https?://", "", host)
                    resp.headers["X-Dyva-Model"] = full
                    return resp

        msg = "worker manually stopped" if stopped else "all servers failed"
        if errors:
            msg += ": " + "; ".join(dict.fromkeys(errors))
        return web.json_response(err_obj(msg), status=502)
    finally:
        await _unregister_worker(job_wid)


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
        if _info_wants_test(body):
            info = await _run_info_test(session, model, tools=body.get("tools"))
            if info is None:
                return web.json_response(err_obj(f"no server for '{model}' passed the quick test", "model_not_found"), status=404)
            return _info_response(model, info, do_stream=do_stream)
        exclude = None
        if _info_wants_next(body):
            prev = get_last(model)
            exclude = prev[0] if prev else None
            _drop_last_host(model)
        info = _would_use(model, req_caps, exclude=exclude)
        if info is None:
            return web.json_response(err_obj(f"no available servers for '{model}'", "model_not_found"), status=404)
        return _info_response(model, info, do_stream=do_stream)

    job_wid = await _register_worker(model, len(find_servers(model, req_caps)), lambda: None)
    try:
        last = get_last(model)
        last_host_err = None
        if last and _known_capable(last[0], last[1], req_caps) and ("vision" not in req_caps or _known_has(last[0], last[1], "vision")):
            last_host, last_full = last
            log.debug(f"Reusing {last_host} for {model}")
            await _worker_checked(job_wid)
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
            result, errors, stopped = await _race_servers(session, model, servers, body, do_stream=True, endpoint=endpoint, remote=request.remote, caps=req_caps, job_wid=job_wid)
            if result:
                _, host, full, resp, first_line, first, _oai = result
                stream_resp = web.StreamResponse()
                await _forward_stream(request, stream_resp, resp, first_line, host, full,
                                      openai_format=False, upstream_openai=_oai)
                return stream_resp
            msg = "worker manually stopped" if stopped else "all servers failed"
            if errors:
                msg += ": " + "; ".join(dict.fromkeys(errors))
            return web.json_response(err_obj(msg), status=502)

        result, errors, stopped = await _race_servers(session, model, servers, dict(body, stream=False), do_stream=False, endpoint=endpoint, remote=request.remote, caps=req_caps, job_wid=job_wid)
        if result:
            _, host, full, data = result
            return web.json_response(data)

        msg = "worker manually stopped" if stopped else "all servers failed"
        if errors:
            msg += ": " + "; ".join(dict.fromkeys(errors))
        return web.json_response(err_obj(msg), status=502)
    finally:
        await _unregister_worker(job_wid)


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
    with open(os.path.join(tpl_dir, "dashboard.html"), encoding="utf-8") as f:
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
                              "model_list": MODEL_LIST,
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
    global WORKER_COUNT, TIMEOUT, MIN_COUNT, ADMIN_PW, _LOCAL, MODEL_LIST
    resp = _check_local(request) or _check_admin(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if isinstance(body.get("workers"), int) and body["workers"] > 0:
        new_workers = min(body["workers"], 200)
        if new_workers != WORKER_COUNT:
            WORKER_COUNT = new_workers
            # Resize the global concurrency semaphore live so the new cap takes
            # effect without a restart. In-flight requests keep their original
            # semaphore; new ones use the resized value.
            app = request.app
            if isinstance(app.get("semaphore"), asyncio.Semaphore):
                app["semaphore"] = asyncio.Semaphore(WORKER_COUNT)
            log.info(f"worker count changed to {WORKER_COUNT}")
    if isinstance(body.get("timeout"), int) and body["timeout"] > 0:
        TIMEOUT = min(body["timeout"], 600)
    if isinstance(body.get("min_count"), int) and body["min_count"] >= 0:
        MIN_COUNT = body["min_count"]
    if isinstance(body.get("local"), bool):
        _LOCAL = body["local"]
    if "model_list" in body:
        MODEL_LIST = parse_model_list(body["model_list"])
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
                              "model_list": MODEL_LIST,
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


async def handle_workers(request):
    """
    Live workers stream (SSE)
    ---
    tags: [Monitoring]
    summary: Server-sent events stream of currently-running model-resolution races
    responses:
      '200':
        description: SSE stream of running workers
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

    try:
        await response.write(f"data: {json.dumps({'workers': _worker_snapshot()})}\n\n".encode())
    except (BrokenPipeError, ConnectionResetError, OSError):
        return response

    q = asyncio.Queue()
    await _add_worker_listener(q)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            if payload is None:
                break
            try:
                await response.write(f"data: {payload}\n\n".encode())
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
    finally:
        await _remove_worker_listener(q)
    return response


class QuietAccessLogger(web_log.AccessLogger):
    """Access logger that drops the high-frequency worker-pane requests.

    The workers pane refreshes every couple of seconds when it has to fall back
    to polling (WebSocket upgrades don't survive every reverse proxy), which
    would otherwise bury the log in identical lines.
    """

    QUIET_PATHS = ("/workers-now", "/workers-ws", "/workers")

    def log(self, request, response, time):
        if request.path in self.QUIET_PATHS:
            return
        super().log(request, response, time)


async def handle_workers_now(request):
    """
    Current workers snapshot (plain JSON)
    ---
    tags: [Monitoring]
    summary: Snapshot of currently-running model-resolution races (JSON)
    responses:
      '200':
        description: JSON list of running workers
        content:
          application/json:
            schema:
              type: object
    """
    return web.json_response({"workers": _worker_snapshot()})


async def handle_workers_ws(request):
    """
    Live workers stream (WebSocket)
    ---
    tags: [Monitoring]
    summary: WebSocket stream of currently-running model-resolution races
    responses:
      '101':
        description: Upgraded WebSocket pushing worker snapshots
    """
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    q = asyncio.Queue()
    await _add_worker_listener(q)
    try:
        await ws.send_json({"workers": _worker_snapshot()})
        while not ws.closed:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=25)
            except asyncio.TimeoutError:
                continue
            if payload is None:
                break
            try:
                await ws.send_str(payload)
            except Exception:
                break
    finally:
        await _remove_worker_listener(q)
    return ws


async def handle_stop_worker(request):
    """
    Abort a running race effort.
    ---
    tags: [Admin]
    summary: Stop a running model-resolution race
    parameters:
      - in: query
        name: wid
        schema:
          type: string
        required: false
        description: Worker id to stop
      - in: query
        name: model
        schema:
          type: string
        required: false
        description: Model string to stop racing on (used when wid is omitted)
    responses:
      '200':
        description: Stop requested
    """
    model = request.query.get("model")
    wid = request.query.get("wid")
    w = None
    if wid:
        w = _workers.get(wid)
    if w is None and model:
        for _w in _workers.values():
            if _w.get("model") == model:
                w = _w
                break
    if w is None:
        return web.json_response({"error": "worker not found"}, status=404)
    stop = w.get("stop")
    if callable(stop):
        log.warning(f"worker manually stopped: model={w.get('model')} wid={w.get('wid')}")
        await broadcast_activity("", w.get("model"), "stopped",
            f"worker manually stopped: {w.get('model')}")
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(stop) if loop.is_running() else stop()
    return web.json_response({"stopped": w.get("model"), "wid": w.get("wid")})


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
            with open(IMG_HISTORY_FILE, encoding="utf-8") as f:
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
        with open(IMG_HISTORY_FILE, "w", encoding="utf-8") as f:
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
        with open(IMG_HISTORY_FILE, encoding="utf-8") as f:
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
        with open(IMG_HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
        n = len(history)
        history = [e for e in history if e.get("file") != name]
        if len(history) != n:
            with open(IMG_HISTORY_FILE, "w", encoding="utf-8") as f:
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


# ---- Fetching explicit URLs -----------------------------------------------
# Three backends, best first. A real browser engine is preferred because a
# growing share of the web renders nothing without JavaScript, and the point of
# this is to read what a person would see. lightpanda is a headless engine
# built for exactly this and starts in milliseconds; Chrome does the same job
# for far more memory; curl is the honest floor — it gets the bytes and we
# strip the tags ourselves.
WEB_FETCH_TIMEOUT = 45          # wall clock for one page
WEB_FETCH_MAX = 200_000         # bytes of text handed back to a model
WEB_IMAGE_MAX = 32 * 1024 * 1024
WEB_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_CHROMES = ("chromium", "chromium-browser", "google-chrome",
            "google-chrome-stable", "chrome", "headless-shell")
_web_backend_cache = None


def web_backend():
    """(name, path) of the best fetcher on this machine, probed once."""
    global _web_backend_cache
    if _web_backend_cache is None:
        path = shutil.which("lightpanda")
        if path:
            _web_backend_cache = ("lightpanda", path)
        else:
            for c in _CHROMES:
                path = shutil.which(c)
                if path:
                    _web_backend_cache = ("chrome", path)
                    break
            else:
                _web_backend_cache = ("curl", shutil.which("curl") or "")
    return _web_backend_cache


async def _run_cmd(argv, timeout):
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return proc.returncode, out, err


_TAG_DROP = re.compile(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1\s*>")
_BREAKS = re.compile(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)\b[^>]*>")
_TAGS = re.compile(r"(?s)<[^>]+>")


def _html_to_text(html):
    """Good-enough readable text. Only the curl and Chrome paths need it —
    lightpanda emits markdown directly."""
    t = _TAG_DROP.sub(" ", html)
    t = _BREAKS.sub("\n", t)
    t = _TAGS.sub(" ", t)
    t = html_mod.unescape(t)
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip()


def _html_title(html):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return html_mod.unescape(m.group(1)).strip()[:200] if m else ""


async def _fetch_lightpanda(path, url, timeout):
    code, out, err = await _run_cmd(
        [path, "fetch", url, "--dump", "markdown", "--json",
         "--dump-max-bytes", str(WEB_FETCH_MAX),
         "--wait-ms", "5000",
         "--terminate-ms", str(int(timeout * 1000 * 0.7))], timeout)
    body = (out or b"").decode("utf-8", "replace")
    start = body.find("{")
    if start < 0:
        raise ComfyError((err or b"").decode("utf-8", "replace").strip()[:200]
                         or f"lightpanda exited {code}")
    doc = json.loads(body[start:])
    if doc.get("error"):
        raise ComfyError(f"lightpanda: {doc['error']}")
    return {"text": doc.get("content") or "",
            "final_url": doc.get("url") or url,
            "status": doc.get("http_status")}


async def _fetch_chrome(path, url, timeout):
    code, out, err = await _run_cmd(
        [path, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--disable-dev-shm-usage", f"--user-agent={WEB_UA}",
         f"--virtual-time-budget={int(timeout * 1000 * 0.5)}",
         "--dump-dom", url], timeout)
    html = (out or b"").decode("utf-8", "replace")
    if not html.strip():
        raise ComfyError((err or b"").decode("utf-8", "replace").strip()[:200]
                         or f"chrome exited {code}")
    return {"text": _html_to_text(html), "title": _html_title(html),
            "final_url": url, "status": None}


async def _fetch_curl(path, url, timeout):
    if not path:
        raise ComfyError("no web fetcher available (install lightpanda, "
                         "a headless Chrome, or curl)")
    code, out, err = await _run_cmd(
        [path, "-sSL", "--compressed", "--max-time", str(int(timeout)),
         "-A", WEB_UA, url], timeout)
    if code != 0:
        raise ComfyError((err or b"").decode("utf-8", "replace").strip()[:200]
                         or f"curl exited {code}")
    raw = (out or b"").decode("utf-8", "replace")
    looks_html = "<" in raw[:2000] and re.search(r"(?i)<(html|body|div|p)\b", raw[:4000])
    return {"text": _html_to_text(raw) if looks_html else raw,
            "title": _html_title(raw), "final_url": url, "status": None}


async def _web_probe(session, url):
    """What is at this URL, without downloading it. HEAD when the server
    honours it, otherwise a one-byte range GET."""
    try:
        r = await session.head(url, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=20),
                               headers={"User-Agent": WEB_UA})
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        status, final = r.status, str(r.url)
        await r.release()
        if status < 400 and ctype:
            return status, ctype, final
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        pass
    try:
        r = await session.get(url, allow_redirects=True,
                              timeout=aiohttp.ClientTimeout(total=20),
                              headers={"User-Agent": WEB_UA, "Range": "bytes=0-0"})
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        status, final = r.status, str(r.url)
        await r.release()
        return status, ctype, final
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise ComfyError(f"could not reach {url}: {e}")


async def _web_save_image(session, url, ctype):
    """Pull an image down into the same store generated images use, so it shows
    up in the gallery and can be handed straight to /v1/images/edits."""
    r = await session.get(url, allow_redirects=True,
                          timeout=aiohttp.ClientTimeout(total=WEB_FETCH_TIMEOUT),
                          headers={"User-Agent": WEB_UA})
    if r.status >= 400:
        await r.release()
        raise ComfyError(f"HTTP {r.status} fetching {url}")
    raw = await r.content.read(WEB_IMAGE_MAX + 1)
    await r.release()
    if len(raw) > WEB_IMAGE_MAX:
        raise ComfyError("image is too large")
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/gif": ".gif", "image/bmp": ".bmp"}.get(ctype, ".png")
    os.makedirs(IMG_DIR, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{ext}"
    with open(os.path.join(IMG_DIR, name), "wb") as f:
        f.write(raw)
    try:
        with open(IMG_HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        history = []
    history.insert(0, {"file": name, "host": "", "model": "web",
                       "prompt": url[:200], "when": time.time()})
    with open(IMG_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[:IMG_HISTORY_MAX], f)
    return name, len(raw)


async def web_fetch(session, url, timeout=WEB_FETCH_TIMEOUT):
    """Read one explicit URL. Images are saved; everything else comes back as
    text, rendered by whichever backend this machine has."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ComfyError("only http and https URLs can be fetched")

    status, ctype, final = await _web_probe(session, url)
    if ctype.startswith("image/"):
        name, size = await _web_save_image(session, final, ctype)
        return {"kind": "image", "url": url, "final_url": final,
                "status": status, "content_type": ctype,
                "file": name, "bytes": size, "backend": "http"}

    name, path = web_backend()
    runner = {"lightpanda": _fetch_lightpanda, "chrome": _fetch_chrome}.get(
        name, _fetch_curl)
    try:
        got = await runner(path, final, timeout)
    except (ComfyError, asyncio.TimeoutError, OSError) as e:
        # A browser engine can fail on a page curl reads fine (and vice versa),
        # so the chain is a real fallback, not just a preference order.
        if name == "curl":
            raise ComfyError(str(e) or "fetch failed")
        log.debug(f"web: {name} failed on {final} ({e}); falling back to curl")
        got = await _fetch_curl(shutil.which("curl") or "", final, timeout)
        name = "curl"

    text = (got.get("text") or "").strip()
    truncated = len(text) > WEB_FETCH_MAX
    if truncated:
        text = text[:WEB_FETCH_MAX] + "\n\n[truncated]"
    return {"kind": "text", "url": url,
            "final_url": got.get("final_url") or final,
            "status": got.get("status", status), "content_type": ctype,
            "title": got.get("title") or "", "text": text,
            "truncated": truncated, "backend": name}


async def handle_web_fetch(request):
    """
    Fetch a URL
    ---
    tags: [Web]
    summary: POST /v1/web/fetch — read an explicit URL as text, or save it if it is an image
    description: |
      Renders the page with the best engine installed — lightpanda, then a
      headless Chrome, then curl — and returns readable text. An image URL is
      downloaded into the generated-image store instead and reported by
      filename, so it can be fed straight to `/v1/images/edits`.
    responses:
      '200':
        description: '{"kind": "text", "text": ..., "backend": ...} or {"kind": "image", "file": ...}'
      '400':
        description: Missing or unusable URL
      '502':
        description: The fetch failed
    """
    resp = _check_local(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = (body.get("url") or request.query.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "'url' is required"}, status=400)
    t0 = time.time()
    await broadcast_activity(url, "web", "trying", f"fetch: {url}")
    try:
        out = await web_fetch(request.app["session"], url)
    except ComfyError as e:
        await broadcast_activity(url, "web", "failure", f"fetch: {url} - {e}")
        return web.json_response({"error": str(e)}, status=502)
    except asyncio.TimeoutError:
        await broadcast_activity(url, "web", "failure", f"fetch: {url} - timed out")
        return web.json_response({"error": "timed out"}, status=502)
    await broadcast_activity(url, "web", "success",
                             f"fetch: {url} ✓ ({out.get('backend')})",
                             duration=time.time() - t0)
    return web.json_response(out)


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

    # Was a two-way good/not-good sort, which meant a1111 never used maybe_good
    # and re-probed hosts already known dead at the connection level.
    marks = load_marks()
    _last_img = get_last(IMG_KEY)
    hosts.sort(key=lambda h: capability_tier(
        h, IMG_KEY, marks, _last_img[0] if _last_img else None))
    hosts = _idle_first(hosts)

    wkey = f"{activity_label} (image)"

    async def a1111_attempt(host, wid, done):
        t0 = time.time()
        await broadcast_activity(host, activity_label, "trying",
            f"txt2img: {activity_label}", wid=wid)
        ov = overrides.get(host)
        payload = ({**body, "override_settings": ov,
                    "override_settings_restore_afterwards": True} if ov else body)
        try:
            async with session.post(
                _host_url(host, "/sdapi/v1/txt2img"), json=payload,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    await broadcast_activity(host, activity_label, "connected",
                        "txt2img \u2713", duration=time.time() - t0, wid=wid,
                        rmodel=(ov or {}).get("sd_model_checkpoint")
                               or _resolve_sd_model(data, body))
                    data["_dyva_host"] = host
                    return accepted(data)
                await broadcast_activity(host, activity_label, "failed",
                    f"txt2img: HTTP {r.status}", wid=wid)
                return failed(f"HTTP {r.status}")
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as _e:
            await broadcast_activity(host, activity_label, "failed",
                f"txt2img: {type(_e).__name__}", wid=wid)
            return (timed_out(type(_e).__name__) if isinstance(_e, asyncio.TimeoutError)
                    else unreachable_host(type(_e).__name__))

    async def comfy_attempt(host, wid, done):
        t0 = time.time()
        await broadcast_activity(host, activity_label, "trying",
            f"txt2img (comfy): {activity_label}", wid=wid)
        # the one long occupancy that wasn't registering a host, which left it
        # invisible to is_active()
        async with _waiting_worker(f"txt2img: {activity_label}", host, "rendering"):
            data = await _txt2img_comfyui(session, host, body, model_filter)
        if data:
            await broadcast_activity(host, activity_label, "connected",
                "txt2img (comfy) \u2713", duration=time.time() - t0, wid=wid,
                rmodel=data.get("_dyva_model"))
            data["_dyva_host"] = host
            return accepted(data)
        await broadcast_activity(host, activity_label, "failed",
            "txt2img (comfy): no response", wid=wid)
        return failed("no response")

    async def _race(host_list, attempt, workers=None):
        if not host_list:
            return None
        result, _stopped, _tried, _tally = await _race_hosts(
            host_list, attempt, IMG_KEY, workers=workers, label=wkey)
        return result

    def _deliver(data):
        # keep the host in the payload, not just in the history file: the chat
        # records where each generated file came from
        host_used = data.pop("_dyva_host", "")
        _save_image_history(data, body, host_used, requested_model)
        data["host"] = host_used
        return web.json_response(data)

    # A host-wide unreachable mark (dead at the connection level) must exclude a
    # host from image routing too — it's stored under a sentinel "model", so the
    # per-model IMG_KEY check alone would miss it and keep trying dead hosts.
    _bad = load_bad()
    _un = load_unreachable()
    def _img_bad(h):
        return h in _un or f"{h} {IMG_KEY}" in _bad

    # Phase 1: good + untested hosts (skip known-bad and unreachable)
    data = await _race([h for h in hosts if not _img_bad(h)], a1111_attempt)
    if data:
        return _deliver(data)

    # Phase 2: exhausted — retry the previously bad / unreachable (recovery path)
    data = await _race([h for h in hosts if _img_bad(h)], a1111_attempt)
    if data:
        return _deliver(data)

    # Phase 3: comfyui hosts
    comfy_candidates = [s for s in servers if s.get("service") == "comfyui"]
    if model_filter:
        comfy_candidates = [
            s for s in comfy_candidates
            if any(match_model(m.split(" [")[0] if " [" in m else m, model_filter)
                   for m in s.get("models", []))
        ]
    comfy_hosts = _idle_first(
        [s.get("server") for s in comfy_candidates if not _img_bad(s.get("server"))])
    data = await _race(comfy_hosts, comfy_attempt, workers=3)
    if data:
        return _deliver(data)

    return web.json_response({"error": "all image-gen hosts failed"}, status=502)


# ---- The ComfyUI job pipeline, shared by every media capability ----------
# tts, music, video and the comfyui txt2img fallback all do the same three
# things: POST /prompt, poll /history until the graph produces an artifact of
# the kind they care about, then pull the bytes from /view. That loop existed
# in four copies, and every bug found in it lived in exactly one of them.
class ComfyError(Exception):
    """Transient: the host was down, refused, or misbehaved."""


class ComfyUnsuitable(ComfyError):
    """Structural: this host cannot run this graph, and still won't later."""


class ComfyUnreachable(ComfyError):
    """Never got a connection. Distinct from a slow or broken response: it says
    nothing about this capability, only that the host is not there — so it
    belongs on the host-wide unreachable mark, not a per-key one."""


# ComfyUI's structural refusals, as they appear in a /prompt rejection body.
_COMFY_STRUCTURAL = re.compile(r"prompt_outputs_failed_validation|missing_node_type"
                               r"|required_input_missing|return_type_mismatch")

# What "an artifact" means per capability. VHS and friends file video under
# several keys, so this is a tuple rather than a single name.
COMFY_AUDIO = ("audio",)
COMFY_IMAGES = ("images",)
COMFY_VIDEO = ("gifs", "videos")


async def comfy_submit(session, host, workflow, timeout=None):
    """Hand a graph to a host. Returns its prompt_id.

    Returning an id is the success boundary for host selection: the failures
    that actually happen out there — offline, repurposed, missing custom nodes —
    all surface here within a second or so. Whatever the render does afterwards
    is a fact about the render, not about the host we picked.
    """
    try:
        r = await session.post(
            _host_url(host, "/prompt"),
            json={"prompt": workflow, "client_id": str(uuid.uuid4())},
            # A host part-way through loading a checkpoint accepts the socket
            # and answers late; that is a cold start, not a refusal.
            timeout=aiohttp.ClientTimeout(total=timeout or COMFY_SUBMIT,
                                          sock_connect=COMFY_CONNECT),
        )
        if r.status != 200:
            body = await r.text()
            await r.release()
            detail = _comfy_prompt_error(body, workflow)
            # The one-liner is what a user sees; the whole envelope carries
            # extra_info (exception type, traceback) and is the only way to
            # tell a graph mistake from a host-side one.
            log.warning(f"comfy {host} rejected the prompt: {detail}\n"
                        f"  body: {body[:1500]}\n"
                        f"  graph: {json.dumps(workflow)[:1500]}")
            if _COMFY_STRUCTURAL.search(body):
                raise ComfyUnsuitable(f"prompt rejected: {detail}")
            raise ComfyError(f"prompt rejected: {detail}")
        data = await r.json(content_type=None)
        await r.release()
    except ComfyError:
        raise
    except Exception as e:
        raise ComfyError(str(e) or type(e).__name__)
    prompt_id = (data or {}).get("prompt_id")
    if not prompt_id:
        raise ComfyError("no prompt_id")
    return prompt_id


def _comfy_error_detail(status):
    """Everything the host told us about a failure, for the log.

    The one-line version is what a user sees; this is what you need when the
    one line is "shapes cannot be multiplied" and the question is which two
    tensors, from which nodes.
    """
    out = []
    for m in (status.get("messages") or []):
        if not (isinstance(m, (list, tuple)) and len(m) >= 2 and m[0] == "execution_error"):
            continue
        p = m[1] if isinstance(m[1], dict) else {}
        out.append(f"node {p.get('node_id')} ({p.get('node_type')}): "
                   f"{p.get('exception_type')}: {p.get('exception_message')}")
        inputs = p.get("current_inputs") or {}
        for k, v in inputs.items():
            out.append(f"    input {k} = {str(v)[:160]}")
        tb = p.get("traceback") or []
        for line in [str(x).rstrip() for x in tb][-6:]:
            out.append(f"    {line}")
    return "\n".join(out)


def _comfy_status_error(status):
    """Pull the real reason out of a failed /history entry.

    `status.messages` is the whole event log — execution_start,
    execution_cached, then eventually execution_error — so joining and
    truncating it reports the beginning of a successful run and throws away the
    part that says what went wrong.
    """
    for m in (status.get("messages") or []):
        if not (isinstance(m, (list, tuple)) and len(m) >= 2):
            continue
        event = m[0]
        payload = m[1] if isinstance(m[1], dict) else {}
        if event == "execution_interrupted":
            return "interrupted (queue cleared or cancelled on the host)"
        if event == "execution_error":
            node = payload.get("node_type") or "?"
            nid = payload.get("node_id")
            exc = (payload.get("exception_message")
                   or payload.get("exception_type") or "execution error")
            where = f"{node}#{nid}" if nid else node
            return f"{where}: {exc}"[:300]
    # nothing recognisable — name the events we did see, which is still more
    # use than the first 200 characters of a JSON dump
    events = [m[0] for m in (status.get("messages") or [])
              if isinstance(m, (list, tuple)) and m]
    return ("no execution_error in " + ", ".join(events[-4:])) if events else "execution error"


async def comfy_queue_state(session, host, prompt_id):
    """Where a submitted prompt sits: "running", a queue position, or None if
    the host has no idea what we're talking about."""
    try:
        r = await session.get(_host_url(host, "/queue"),
                              timeout=aiohttp.ClientTimeout(total=20))
        if r.status != 200:
            await r.release()
            return "unknown"
        q = await r.json(content_type=None)
        await r.release()
    except Exception:
        return "unknown"
    for item in (q.get("queue_running") or []):
        if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
            return "running"
    for i, item in enumerate(q.get("queue_pending") or []):
        if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
            return f"queued #{i + 1}"
    return None


async def comfy_collect(session, host, prompt_id, kinds, timeout=180,
                        poll=2, view_timeout=60, on_status=None):
    """Wait out a job the host already accepted; return (bytes, filename).

    Deliberately not part of the race: by here the host is chosen and marked,
    so a slow or broken render is reported to the caller without re-litigating
    host selection.
    """
    label = kinds[0]
    deadline = time.time() + timeout
    # A job can vanish: the host restarts, the queue is cleared, or it quietly
    # drops the prompt. History never gains an entry and the queue never had
    # one, so polling for the full timeout tells you nothing for half an hour.
    # Check the queue when history is silent, and give up once the host has
    # twice denied knowing about it.
    missing = 0
    checked_at = 0.0
    while time.time() < deadline:
        await asyncio.sleep(poll)
        try:
            r = await session.get(
                _host_url(host, f"/history/{prompt_id}"),
                timeout=aiohttp.ClientTimeout(total=30),
            )
            if r.status != 200:
                await r.release()
                continue
            hist = await r.json(content_type=None)
            await r.release()
        except Exception:
            continue
        entry = (hist or {}).get(prompt_id)
        if not entry:
            now = time.time()
            if now - checked_at >= max(15, poll * 5):
                checked_at = now
                where = await comfy_queue_state(session, host, prompt_id)
                if where is None:
                    missing += 1
                    if missing >= 2:
                        raise ComfyError(
                            "the host dropped the job — not in its queue and no "
                            "history entry (restarted, or the queue was cleared)")
                else:
                    missing = 0
                    if on_status and where != "unknown":
                        on_status(where)
            continue
        missing = 0

        files = []
        for out in (entry.get("outputs") or {}).values():
            for k in kinds:
                files.extend(out.get(k) or [])
        if files:
            af = files[0]
            try:
                v = await session.get(
                    _host_url(host, "/view"),
                    params={"filename": af.get("filename"),
                            "subfolder": af.get("subfolder", ""),
                            "type": af.get("type", "output")},
                    timeout=aiohttp.ClientTimeout(total=view_timeout),
                )
                if v.status != 200:
                    await v.release()
                    raise ComfyError(f"view HTTP {v.status}")
                raw = await v.read()
                await v.release()
            except ComfyError:
                raise
            except Exception as e:
                raise ComfyError(str(e) or type(e).__name__)
            # We have the bytes; the host has no reason to keep the job.
            # Every capability collects through here, so one call covers
            # images, edits, video, speech and music alike.
            await comfy_forget(session, host, prompt_id)
            return raw, af.get("filename") or f"output.{label}"

        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            detail = _comfy_error_detail(status)
            if detail:
                log.warning(f"comfy {host} prompt {prompt_id} failed:\n{detail}")
            raise ComfyError(_comfy_status_error(status))
        if status.get("completed"):
            raise ComfyError(f"the graph finished but saved no {label}")
    raise ComfyError(f"timeout waiting for {label}")


async def _txt2img_comfyui(session, host, body, model_filter=None):
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
    out_node = comfy_image_out_for(host)

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
        "7": ({"class_type": "PreviewImage", "inputs": {"images": ["6", 0]}}
              if out_node == "PreviewImage" else
              {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "dyva_output", "images": ["6", 0]}}),
    }

    try:
        prompt_id = await comfy_submit(session, host, workflow)
        raw, _fn = await comfy_collect(session, host, prompt_id, COMFY_IMAGES,
                                       timeout=120, poll=2, view_timeout=30)
    except ComfyError as e:
        log.debug(f"txt2img: {host}: {e}")
        return None
    return {"images": [base64.b64encode(raw).decode()], "_dyva_model": ckpt,
            "_dyva_seed": seed, "parameters": "{}",
            "info": json.dumps({"prompt": body})}


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


# Two different jobs, deliberately not the same string:
#   TTS_KEY  — the capability itself. Names the node survey in host_nodes, and
#              is the reputation key when no particular voice model was asked
#              for. No wildcard: canon_pattern() eats a trailing "*".
#   tts_key() — the reputation key for one request, per model, so a host that
#              can't run VibeVoice isn't written off for Qwen3TTS as well.
TTS_KEY = "tts"


def tts_key(model_filter=None):
    q = _sep_insensitive((model_filter or "").replace("*", "").replace("?", ""))
    return f"tts/{q}" if q else TTS_KEY

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
            with open(NODE_CLASSIFIER_FILE, encoding="utf-8") as f:
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
                            "style": spec.get("style"),
                        })
        except Exception as e:
            log.warning(f"node-classifier: failed to load {NODE_CLASSIFIER_FILE}: {e}")
    _node_classifier_cache = compiled
    return compiled


def _tts_voices_of(node_class, info, fam):
    """The voice options a node advertises, for the stored survey."""
    voice_field = fam.get("voice")
    if not voice_field:
        return []
    required = (((info.get(node_class) or {}).get("input") or {}).get("required")) or {}
    entry = required.get(voice_field)
    if isinstance(entry, list) and entry and isinstance(entry[0], list):
        return [v for v in entry[0] if isinstance(v, str)]
    return []


def _tts_node_family(node_class):
    """Return the surveyed TTS-family spec whose name-pattern matches a node
    class name, or None if the node isn't a recognized (popular) TTS node."""
    for spec in load_node_classifier():
        if any(r.search(node_class) for r in spec["regs"]):
            return spec
    return None


_TTS_NODE_CACHE = {}
_TTS_NODE_CACHE_TTL = 300
# Full /object_info per host, shared by node detection and workflow building so
# a single speech request doesn't fetch the (large) schema three times.
_COMFY_INFO_CACHE = {}
_COMFY_INFO_TTL = 300
# Connect and read deserve very different patience, because they fail for very
# different reasons.
#
# Connecting is answered by the kernel, so it does not care what the app is
# doing: a host that hasn't answered the handshake in 15s is not there (or is
# dropping packets rather than refusing them). Being strict here is safe.
#
# Reading is another matter. ComfyUI blocks its event loop while it pulls a
# checkpoint off disk — a 20GB model from a platter is minutes of silence with
# the socket wide open — so a short read timeout throws away hosts that were
# only cold-starting. Waiting costs little: the race runs several hosts at
# once, so a slow one never holds up a fast one.
COMFY_CONNECT = 15      # kernel-level; a dead host is dead in 15s
COMFY_STALL = 180       # silence while a big model loads is not a failure
COMFY_INFO_TOTAL = 600  # backstop against a host that dribbles forever
COMFY_SUBMIT = 120      # /prompt from a host mid-load answers late, not never

_TTS_MIME = {
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".m4a": "audio/mp4",
}


# TTS speaks the shared pipeline's error vocabulary; the aliases keep the
# existing except-clauses readable at the call sites.
_TtsError = ComfyError
_TtsUnsuitable = ComfyUnsuitable


# ComfyUI has no API for deleting a generated file, so the next best thing is
# not to create a permanent one. Preview nodes write into the host's temp
# directory instead of its output directory, and ComfyUI's own main.py calls
# cleanup_temp() — an rmtree of that directory — both at startup and in the
# finally block at shutdown. So the file goes away on its own, and it never
# shows up in the host's output gallery in the meantime. Retrieval is
# unchanged: the history entry carries type "temp" and /view is already given
# whatever type it reports.
def comfy_image_out(info):
    """The node that emits the finished image: preview for choice, save to
    fall back on. None when the host has neither."""
    for c in ("PreviewImage", "SaveImage"):
        if c in info:
            return c
    return None


def comfy_image_out_for(host):
    """Same answer for a host we may not have surveyed. txt2img deliberately
    never pulls /object_info — several megabytes to draw one picture — so this
    peeks the cache and otherwise assumes the preview node, which ships with
    ComfyUI exactly as SaveImage does."""
    hit = _COMFY_INFO_CACHE.get(host)
    return comfy_image_out(hit[0]) if hit else "PreviewImage"


async def comfy_forget(session, host, prompt_id):
    """Drop our job from the host's history. This removes the record, not the
    file — no core endpoint deletes output — but it takes our work out of their
    queue view. Best-effort by design: a host that refuses is not a failure of
    the thing the caller actually asked for."""
    if not prompt_id:
        return
    try:
        r = await session.post(_host_url(host, "/history"),
                               json={"delete": [prompt_id]},
                               timeout=aiohttp.ClientTimeout(total=10))
        await r.release()
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        log.debug(f"comfy {host}: history delete failed: {e}")


async def comfy_object_info(session, host):
    """Fetch (and briefly cache) a ComfyUI host's full /object_info.

    Shared by every ComfyUI capability — the payload runs to several megabytes,
    so it is fetched once per host and reused.

    Raises _TtsError when the host can't be reached or answers badly, so
    callers can report *that* instead of mislabeling it "no known TTS node"."""
    hit = _COMFY_INFO_CACHE.get(host)
    if hit and time.time() - hit[1] < _COMFY_INFO_TTL:
        return hit[0]
    started = time.time()
    try:
        r = await session.get(
            _host_url(host, "/object_info"),
            # A flat `total` punishes slow-but-working hosts: the node index
            # runs to several megabytes (6.1 MB / 16.3s on one measured host,
            # 9.4 MB on another), so a host trickling it down a thin link gets
            # killed mid-download even though nothing is wrong. Time the phases
            # instead — connect quickly, then only give up if the data actually
            # *stops* — with a long backstop against a host that dribbles
            # forever.
            timeout=aiohttp.ClientTimeout(total=COMFY_INFO_TOTAL,
                                          sock_connect=COMFY_CONNECT,
                                          sock_read=COMFY_STALL),
        )
        status = r.status
        if status != 200:
            await r.release()
            raise _TtsError(f"object_info HTTP {status}")
        info = await r.json(content_type=None)
        await r.release()
    except _TtsError:
        raise
    except asyncio.TimeoutError:
        # Say which kind, so nobody has to ask whether we couldn't reach it or
        # merely ran out of patience. A host that drops SYNs rather than
        # refusing them looks identical to a slow one until you time the
        # phases: no connection at all is unreachable, mid-download is not.
        took = time.time() - started
        if took < COMFY_CONNECT + 1:
            raise ComfyUnreachable(
                f"no TCP connection within {COMFY_CONNECT}s (packets dropped, not refused)")
        raise _TtsError(
            f"object_info stalled after {took:.0f}s — connected, but silent for "
            f"{COMFY_STALL}s (wedged, or still loading something enormous)")
    except (aiohttp.ClientConnectorError, OSError) as e:
        raise ComfyUnreachable(str(e) or type(e).__name__)
    except Exception as e:
        raise _TtsError(str(e) or type(e).__name__)
    if not isinstance(info, dict):
        raise _TtsError("object_info: unexpected response")
    _COMFY_INFO_CACHE[host] = (info, time.time())
    return info


# ComfyUI required-input types we can satisfy inline. Anything else — AUDIO,
# MODEL, ELEVENLABS_VOICE, a bare COMBO with no inline options — is a *socket*
# that has to be fed by another node, so a node needing one can't be driven by
# the three-node graph this builds.
_FILLABLE_PRIMITIVE = {"STRING": "", "INT": 0, "FLOAT": 0.0, "BOOLEAN": False}


def _tts_fill(entry):
    """(can_fill, value) for one required input's schema entry."""
    if not isinstance(entry, list) or not entry:
        return False, None
    kind = entry[0]
    attrs = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
    if isinstance(kind, list):                      # enum
        if not kind:
            return False, None
        if attrs.get("default") in kind:
            return True, attrs["default"]
        return True, kind[0]
    if "default" in attrs:
        return True, attrs["default"]
    if kind in _FILLABLE_PRIMITIVE:
        return True, _FILLABLE_PRIMITIVE[kind]
    return False, None


def _empty_required_string(entry):
    """A required STRING whose default is empty — declared fillable, actually not.

    FB_Qwen3TTSVoiceDesign is the case that taught this: its `instruct` is a
    required STRING with `"default": ""` and the placeholder "Style instruction
    (required for VoiceDesign)". Every field looks fillable, the graph
    validates, and the node then refuses at execution with "Text and
    instruction description are required". The node's own source
    (flybirdxx/ComfyUI-Qwen-TTS) confirms it: `instruct` is a natural-language
    description of the voice and must be non-empty.

    An empty default is the schema saying "there is no sensible value here" —
    so unless the caller knows what belongs in the field, the node can't run.
    """
    if not isinstance(entry, list) or not entry or isinstance(entry[0], list):
        return False
    attrs = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
    return entry[0] == "STRING" and attrs.get("default", None) == ""


def _tts_unfillable(required, skip=()):
    """Names of required inputs we can neither fill nor wire."""
    return [k for k, v in required.items()
            if k not in skip
            and (not _tts_fill(v)[0] or _empty_required_string(v))]


_SAVE_AUDIO_RE = re.compile(r"(?i)save.*audio|audio.*save")


def _tts_save_node(host_info):
    """The node that will actually write the audio file, or None if this host
    has no audio saver at all. Defaulting to the name "SaveAudio" when it isn't
    installed just moves the failure to the host, as missing_node_type."""
    for candidate in ("PreviewAudio", "SaveAudio", "SaveAudioMP3",
                      "SaveAudioOpus", "SaveAudioAdvanced"):
        if candidate in host_info:
            return candidate
    for key in host_info:
        if _SAVE_AUDIO_RE.search(key):
            req = ((host_info[key].get("input") or {}).get("required")) or {}
            if any(isinstance(v, list) and v and v[0] == "AUDIO" for v in req.values()):
                return key
    return None


_tts_exclude_cache = None


def tts_excluded(node_class):
    """True for nodes whose *name* says they do a different job.

    This is for tasks that are not text-to-speech at all — voice cloning and
    conversion (they want reference audio), transcription, loaders and savers.
    It is deliberately not used for "this node has an awkward required field":
    that is a structural question, and `_empty_required_string` answers it from
    the schema. FB_Qwen3TTSVoiceDesign was excluded by name here once, which
    was wrong on both counts — it is real TTS, and the name-based rule silently
    did nothing for hosts already surveyed.
    """
    global _tts_exclude_cache
    if _tts_exclude_cache is None:
        pats = []
        try:
            with open(NODE_CLASSIFIER_FILE, encoding="utf-8") as f:
                pats = (json.load(f).get("tts_exclude") or [])
        except Exception as e:
            log.warning(f"node-classifier: failed to load tts_exclude: {e}")
        _tts_exclude_cache = [re.compile(p) for p in pats]
    return any(r.search(node_class) for r in _tts_exclude_cache)


def _tts_viable(node_class, info, fam):
    """Can this node actually be driven end to end on this host?

    Name-matching a family is not enough. The graph is only three nodes deep, so
    every required input has to be fillable inline, and every node it references
    — the audio saver, and UnifiedTTSTextNode behind an engine — has to be
    installed. Skipping this check produced two distinct host-side rejections:
    `required_input_missing` on ElevenLabsTextToSpeech (its `voice` is an
    ELEVENLABS_VOICE socket, fed by a separate selector node) and
    `missing_node_type` for SaveAudio on a host that ships no audio saver.
    """
    if tts_excluded(node_class):
        return False
    node = info.get(node_class) or {}
    required = ((node.get("input") or {}).get("required")) or {}
    text_field = fam["text"]
    if text_field and text_field not in required:
        return False
    voice_field = fam["voice"]
    if voice_field and voice_field in required:
        entry = required.get(voice_field)
        opts = entry[0] if isinstance(entry, list) and isinstance(entry[0], list) else None
        if isinstance(opts, list) and not opts:
            return False
    # the family's own text and style fields are ones we supply, so they are
    # exempt from the empty-default rule
    if _tts_unfillable(required, skip=tuple(x for x in (fam.get("text"), fam.get("style")) if x)):
        return False
    if not _tts_save_node(info):
        return False
    outputs = node.get("output") or []
    is_engine = bool(outputs) and outputs[0] == "TTS_ENGINE"
    # Matching a family by name says nothing about what a node emits.
    # GoogleTranslateTextNode matched the GoogleTTS pattern and outputs STRING,
    # so the saver was handed text and the host answered return_type_mismatch.
    if not is_engine and "AUDIO" not in outputs:
        return False
    if is_engine:
        synth = info.get("UnifiedTTSTextNode")
        if not synth:
            return False
        synth_req = ((synth.get("input") or {}).get("required")) or {}
        if _tts_unfillable(synth_req, skip=("TTS_engine", "text")):
            return False
    return True


async def _tts_node_for(session, host, info=None):
    """Find a host's TTS node by classifying its installed node-class names
    against the node-classifier (data-driven, popularity-seeded) — never a
    hardcoded boutique list and never structural model probing.

    Returns None only when the host genuinely has no usable TTS node; that
    verdict is cached (negatively) so we stop re-probing every host on every
    speech request."""
    hit = _TTS_NODE_CACHE.get(host)
    if hit and time.time() - hit[1] < _TTS_NODE_CACHE_TTL:
        return hit[0]
    # A survey from a previous run (or a previous process) is as good as a
    # probe and costs nothing — /object_info is megabytes.
    stored = node_survey_for(host, TTS_KEY)
    if stored is not None and info is None:
        _TTS_NODE_CACHE[host] = (stored["spec"], time.time())
        return stored["spec"]
    if info is None:
        info = await comfy_object_info(session, host)

    # family priority wins over host node order (QwenTTS before Generic TTS)
    for fam in load_node_classifier():
        for node_class in info:
            if not any(r.search(node_class) for r in fam["regs"]):
                continue
            if not _tts_viable(node_class, info, fam):
                continue
            spec = {
                "class": node_class,
                "text": fam["text"],
                "voice": fam["voice"],
                "lang": fam["lang"],
                "style": fam.get("style"),
            }
            _TTS_NODE_CACHE[host] = (spec, time.time())
            save_node_survey(host, TTS_KEY, spec, _tts_voices_of(node_class, info, fam))
            return spec
    _TTS_NODE_CACHE[host] = (None, time.time())
    save_node_survey(host, TTS_KEY, None)
    return None


# Enum entries that mean "no voice — I'll supply reference audio instead".
# Selecting one of these and then supplying no reference audio makes the node
# fail at execution time, so an unspecified voice must skip past them.
_VOICE_PLACEHOLDER = re.compile(r"(?i)^\s*(none|null|custom|zero.?shot|default)\b|zero.?shot|custom\)")


def _tts_pick_voice(opts):
    """First option that is a real voice, not a zero-shot/custom placeholder."""
    for o in opts:
        if isinstance(o, str) and not _VOICE_PLACEHOLDER.search(o):
            return o
    return None


def _audio_out(node_info):
    """Index of a node's AUDIO output. Nodes declare several outputs in their own
    order (UnifiedTTSTextNode is AUDIO,STRING; others put the text first), so the
    audio slot has to be looked up rather than assumed to be 0."""
    outputs = (node_info or {}).get("output") or []
    return outputs.index("AUDIO") if "AUDIO" in outputs else None


DEFAULT_VOICE_STYLE = "A clear, natural speaking voice, calm and even."


def _tts_workflow(spec, schema, input_text, voice, host_info=None):
    cls = spec["class"]
    required = ((schema.get(cls) or {}).get("input") or {}).get("required") or {}
    node_info = schema.get(cls) or {}
    outputs = node_info.get("output") or []
    is_engine = bool(outputs) and outputs[0] == "TTS_ENGINE"

    # An engine node only configures the voice; the words are spoken by the
    # downstream UnifiedTTSTextNode. Its declared "text" field is really a style
    # directive (Qwen3TTSEngineNode calls it "instruct"), so pushing the user's
    # sentence into it makes the engine treat "hi" as an instruction rather than
    # something to say. Leave it at its default and let the fill loop below
    # supply that.
    inputs = {} if is_engine else {spec["text"]: input_text}

    # A style field is a *description of a voice*, not a directive about the
    # words. When the caller named a voice, that is the description; otherwise
    # something neutral, because empty is what the node rejects.
    style_field = spec.get("style")
    if style_field and style_field in required:
        inputs[style_field] = voice or DEFAULT_VOICE_STYLE

    voice_field = spec.get("voice")
    lang_field = spec.get("lang")
    if voice and voice_field:
        entry = required.get(voice_field)
        opts = entry[0] if isinstance(entry, list) and isinstance(entry[0], list) else []
        if isinstance(opts, list) and (not opts or voice in opts):
            inputs[voice_field] = voice
    elif voice_field and voice_field in required:
        entry = required.get(voice_field)
        opts = entry[0] if isinstance(entry, list) and isinstance(entry[0], list) else []
        pick = _tts_pick_voice(opts) if isinstance(opts, list) else None
        if pick:
            inputs[voice_field] = pick

    if lang_field:
        entry = required.get(lang_field)
        opts = entry[0] if isinstance(entry, list) and isinstance(entry[0], list) else []
        lang = "zh" if any("\u4e00" <= c <= "\u9fff" for c in input_text) else "en"
        if opts and lang not in opts:
            lang = opts[0]
        inputs[lang_field] = lang

    for name, entry in required.items():
        if name in inputs:
            continue
        ok, value = _tts_fill(entry)
        if ok:
            inputs[name] = value

    save_node = _tts_save_node(host_info or {})
    if not save_node:
        raise _TtsError("host has no audio-save node")

    graph = {"1": {"class_type": cls, "inputs": inputs}}

    if is_engine:
        # Engine node (Qwen3TTSEngineNode, F5TTSEngineNode, IndexTTSEngineNode, etc.)
        # needs UnifiedTTSTextNode to actually generate audio
        graph["2"] = {
            "class_type": "UnifiedTTSTextNode",
            "inputs": {
                "TTS_engine": ["1", 0],
                "text": input_text,
                "narrator_voice": "none",
                "seed": 1,
            }
        }
        idx = _audio_out((host_info or {}).get("UnifiedTTSTextNode"))
        if idx is None:
            raise _TtsError("UnifiedTTSTextNode has no AUDIO output")
        graph["3"] = {"class_type": save_node,
                      "inputs": {"audio": ["2", idx], "filename_prefix": "dyva/tts"}}
    else:
        # Direct audio output node
        idx = _audio_out(node_info)
        if idx is None:
            raise _TtsError(f"{cls} has no AUDIO output")
        graph["2"] = {"class_type": save_node,
                      "inputs": {"audio": ["1", idx], "filename_prefix": "dyva/tts"}}

    return graph


def _comfy_prompt_error(body, graph=None):
    """Turn ComfyUI's /prompt rejection envelope into one readable line.

    The raw body is a nested JSON blob whose useful part — which node, which
    input — sits inside node_errors and gets cut off by any sane truncation.
    """
    try:
        data = json.loads(body)
    except Exception:
        return body[:200]
    parts = []
    for node_id, ne in (data.get("node_errors") or {}).items():
        cls = (graph or {}).get(node_id, {}).get("class_type", f"node {node_id}")
        for err in (ne.get("errors") or []):
            kind = err.get("type") or err.get("message") or "error"
            detail = err.get("details")
            parts.append(f"{cls}: {kind}: {detail}" if detail else f"{cls}: {kind}")
    if not parts:
        err = data.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        detail = err.get("details") if isinstance(err, dict) else ""
        extra = (err.get("extra_info") or {}) if isinstance(err, dict) else {}
        exc = extra.get("exception_type") or ""
        bits = [x for x in (msg, exc, detail) if x]
        parts.append(": ".join(bits) or body[:200])
    return "; ".join(parts)[:300]


async def _tts_submit(session, host, input_text, voice=None):
    """Get a job onto a host. Returns (prompt_id, node_class).

    This is where host selection ends. A returned prompt_id means the host
    accepted the work — that is what "this host is good" means for a ComfyUI
    media capability, because the failures that actually happen (offline,
    repurposed, missing nodes) all surface here, in about a second. What the
    render then does is a fact about the render, not about the host we picked.
    """
    spec = await _tts_node_for(session, host)
    if not spec:
        raise _TtsUnsuitable("no known TTS node")
    host_info = await comfy_object_info(session, host)
    if spec["class"] not in host_info:
        raise _TtsUnsuitable(f"node {spec['class']} vanished from object_info")

    schema = {spec["class"]: host_info[spec["class"]]}
    workflow = _tts_workflow(spec, schema, input_text, voice, host_info)
    return await comfy_submit(session, host, workflow), spec["class"]


# UnifiedTTSTextNode chunks long text at 400 chars and renders the chunks
# *sequentially* (batch_size defaults to 0). So render time scales with length,
# and a fixed ceiling turns a long paragraph into "timeout waiting for audio"
# on a host that was working perfectly well.
TTS_CHUNK_CHARS = 400
TTS_SECONDS_PER_CHUNK = 60
TTS_MAX_CHARS = 4000


def _tts_render_budget(text):
    chunks = max(1, math.ceil(len(text or "") / TTS_CHUNK_CHARS))
    return min(900, 60 + chunks * TTS_SECONDS_PER_CHUNK)


async def _tts_collect(session, host, prompt_id, timeout=180):
    return await comfy_collect(session, host, prompt_id, COMFY_AUDIO,
                               timeout=timeout, poll=2, view_timeout=60)


def _find_tts_hosts(target_host=None, model_filter=None):
    """ComfyUI hosts that can generate TTS, best-first.

    TTS is a *node* feature, not a model-file feature: a host with a Qwen3TTS
    node may expose no audio checkpoint at all, while a host full of AceStep
    music models may have no TTS node. So every ComfyUI host is a candidate and
    the ordering carries the signal.

    Ordering is the same recent/good/maybe_good/unknown/bad tiering chat uses,
    keyed on the __tts__ sentinel (capability_tier), with one capability-
    specific refinement: a *fresh* probe that found no usable node is harder
    evidence than any reputation mark, so it folds straight into the bad tier.
    Within a tier, hosts we've already seen a TTS node on come first, then hosts
    carrying audio-class models (a weak hint), then everyone else.
    """
    if target_host:
        # If a specific host was requested, just try it
        return [target_host] if any(s.get("service") == "comfyui"
                                     for s in load_servers()
                                     if s.get("server") == target_host) else []
    hosts = [s.get("server") for s in load_servers()
             if s.get("service") == "comfyui" and s.get("server")]
    marks = load_marks()
    tkey = tts_key(model_filter)
    last = get_last(tkey)
    last_host = last[0] if last and last[0] else None
    now = time.time()
    # The persisted survey outlives the process, so after one sweep the handful
    # of hosts that actually have a TTS node are known from a cold start
    # instead of being rediscovered by probing hundreds of hosts.
    survey = load_node_survey(TTS_KEY)

    def rank(h):
        hit = _TTS_NODE_CACHE.get(h)
        fresh = bool(hit) and now - hit[1] < _TTS_NODE_CACHE_TTL
        node = hit[0] if fresh else None
        known = fresh
        if not known:
            row = survey.get(h)
            if row and now - (row.get("checked") or 0) <= NODE_SURVEY_TTL:
                known, node = True, row.get("spec")
        known_nodeless = known and node is None
        tier = capability_tier(h, tkey, marks, last_host, extra_bad=known_nodeless)
        if known and node is not None:
            # a host whose surveyed node matches what was asked for goes first
            if model_filter and not model_query_match(node.get("class", ""), model_filter):
                within = 3
            else:
                within = 0
        elif _host_has_class(h, _AUDIO_CLASSES):
            within = 1
        else:
            within = 2
        return (tier, within)

    hosts.sort(key=rank)
    return _idle_first(hosts)


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
    marks = load_marks()
    _vlast = get_last(VIDEO_KEY)
    hosts.sort(key=lambda h: capability_tier(h, VIDEO_KEY, marks,
                                             _vlast[0] if _vlast else None))
    last = get_last(VIDEO_KEY)
    if last and last[0]:
        hosts = [last[0]] + [h for h in hosts if h != last[0]]
    return _idle_first(hosts)


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
        with open(VIDEO_JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
    except Exception as e:
        log.warning(f"video jobs: failed to persist: {e}")


def _load_video_jobs():
    global _VIDEO_JOBS, _VIDEO_JOB_ID_CTR
    if _VIDEO_JOBS:
        return
    if os.path.exists(VIDEO_JOBS_FILE):
        try:
            with open(VIDEO_JOBS_FILE, encoding="utf-8") as f:
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
    """Race the job onto a host, then wait it out.

    Video used to pick one host when the request came in and fail the whole job
    if that host said no — no race, no verdicts, no second candidate. It goes
    through the same engine as everything else now: submitting is the success
    boundary, a render failure retires the model and re-races, and every
    outcome is recorded as a verdict.
    """
    job = _VIDEO_JOBS.get(jid)
    if not job:
        return
    job["status"] = "in_progress"
    _video_job_save()
    label = f"video: {job.get('prompt', '')[:40]}"
    vkey = video_key(job.get("model_filter"))
    hosts = _find_video_hosts(job.get("target_host"))
    if not hosts:
        job["status"] = "failed"
        job["error"] = "no video-capable hosts available"
        _video_job_save()
        return
    errors = []

    async def attempt(host, wid, done):
        await broadcast_activity(host, vkey, "trying", f"trying: {host} for {label}", wid=wid)
        t0 = time.time()
        try:
            prompt_id, plan = await _submit_video_workflow(session, host, job)
        except (ComfyError, _VideoError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            err = str(e) or type(e).__name__
            errors.append(f"{host}: {err}")
            await broadcast_activity(host, vkey, "failed",
                f"failure: {host} for {label} - {err}", duration=time.time() - t0, wid=wid)
            if isinstance(e, (ComfyUnsuitable, _VideoError)):
                return unsuitable(err)
            if isinstance(e, ComfyUnreachable):
                return unreachable_host(err)
            if isinstance(e, asyncio.TimeoutError):
                return timed_out(err)
            if isinstance(e, (aiohttp.ClientError, OSError)):
                return unreachable_host(err)
            return failed(err)
        await broadcast_activity(host, vkey, "connected", f"success: {host} for {label}",
                                 duration=time.time() - t0, wid=wid, rmodel=plan.get("model"))
        return accepted((host, prompt_id, plan))

    tried_hosts, bad_models = set(), set()
    for _round in range(EDIT_RENDER_ATTEMPTS):
        pool = [h for h in hosts if h not in tried_hosts]
        if not pool:
            break
        job["exclude_models"] = sorted(bad_models)
        result, stopped, _tried, _tally = await _race_hosts(pool, attempt, vkey, label=label)
        if not result or stopped:
            break
        host, prompt_id, plan = result
        tried_hosts.add(host)
        job["host"] = host
        job["model"] = plan.get("model")
        job["comfyui_prompt_id"] = prompt_id
        job["phase"] = "submitted"
        _video_job_save()
        try:
            async with _waiting_worker(label, host, "rendering") as _wid:
                def _phase(w):
                    _set_worker_phase(_wid, w)
                    job["phase"] = w      # so the polling client can show it too
                    _video_job_save()
                content, filename = await _poll_video_result(
                    session, host, prompt_id, job, on_status=_phase)
        except (_VideoError, ComfyError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            outcome = render_verdict(e)
            record_verdict(host, vkey, outcome)
            err = str(e) or type(e).__name__
            errors.append(f"{host}: {err} [{plan.get('family')} {plan.get('model')}]")
            log.warning(f"video: {host}: took the job then failed to render: {err}")
            await broadcast_activity(host, vkey, "failed",
                f"failure: {host} for {label} - render: {err}")
            if outcome.verdict == V_UNSUITABLE and plan.get("model"):
                bad_models.add(plan["model"])
                # remember it past this request, so tomorrow's job doesn't
                # rediscover the same incompatible pairing from scratch
                remember_bad_pair(host, "edit", plan["model"])
                tried_hosts.discard(host)
            continue

        os.makedirs(VIDEO_JOBS_DIR, exist_ok=True)
        content_path = os.path.join(VIDEO_JOBS_DIR, f"{jid}.bin")
        with open(content_path, "wb") as f:
            f.write(content)
        job.update({"content_path": content_path, "filename": filename,
                    "status": "completed",
                    "unsigned_urls": [f"/v1/videos/{jid}/content"]})
        _video_job_save()
        return

    reasons = collections.Counter(e.split(": ", 1)[1] if ": " in e else e for e in errors)
    job["status"] = "failed"
    job["error"] = ("; ".join(f"{c}x {r}" for r, c in reasons.most_common())
                    or "no host accepted the job")
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
            if model_filter and not model_query_match(m, model_filter):
                continue
            return m
    return None


_video_families_cache = None


def load_video_families():
    global _video_families_cache
    if _video_families_cache is not None:
        return _video_families_cache
    out = []
    try:
        with open(NODE_CLASSIFIER_FILE, encoding="utf-8") as f:
            out = (json.load(f).get("video") or [])
    except Exception as e:
        log.warning(f"node-classifier: failed to load video families: {e}")
    _video_families_cache = out
    return out


# Nine buckets instead of a resolution box. Every family has a native pixel
# budget it was trained near, so scale that by size and split it by aspect,
# rather than offering a grid of numbers that is wrong for two families out of
# three. Medium/long lands on each family's own default — 1344x768 for
# MiniMax-H3, 832x480 for LTX — which is the point of deriving it.
VIDEO_SIZES = {"small": 0.45, "medium": 1.0, "large": 1.6}
VIDEO_ASPECTS = {"long": 16 / 9, "square": 1.0, "tall": 9 / 16}


def video_dims(fam, aspect="long", size="medium"):
    px = float((fam or {}).get("px") or 640 * 640)
    step = int((fam or {}).get("dim_step") or 32)
    budget = px * VIDEO_SIZES.get(size, 1.0)
    ratio = VIDEO_ASPECTS.get(aspect, 16 / 9)
    w = max(step, int(round(math.sqrt(budget * ratio) / step)) * step)
    h = max(step, int(round(math.sqrt(budget / ratio) / step)) * step)
    return w, h


def video_frames(fam, want=None):
    """Snap to what the family's temporal VAE can encode: Wan wants 4n+1, LTX
    8n+1. A count off that grid is mangled or rejected."""
    fam = fam or {}
    step = int(fam.get("frame_step") or 1)
    n = int(want or fam.get("length") or 81)
    if step > 1:
        n = max(step + 1, ((n - 1) // step) * step + 1)
    return max(1, n)


def _video_plan(info, model_filter=None, exclude=()):
    """Pick a video model on this host from its *loader enums*.

    The cached model list is every file a host will admit to having, VAEs and
    text encoders included — which is how a job ended up trying to render with
    `minimax_h3_video_vae_fp16.safetensors` as the diffusion model. The loader
    enums say what can actually be loaded as what.
    """
    for fam in load_video_families():
        if any(nd not in info for nd in (fam.get("needs") or [])):
            continue
        loader = fam.get("loader", "UNETLoader")
        if loader not in info:
            continue
        field = "ckpt_name" if loader == "CheckpointLoaderSimple" else "unet_name"
        rx = re.compile(fam["model"])
        cands = [m for m in _enum_options(info, loader, field)
                 if rx.search(m) and m not in exclude]
        if model_filter:
            cands = [m for m in cands if model_query_match(m, model_filter)]
        if not cands:
            continue
        clip = _pick(_enum_options(info, "CLIPLoader", "clip_name"), *_hints(fam.get("clip")))
        vae = _pick(_enum_options(info, "VAELoader", "vae_name"), *_hints(fam.get("vae")))
        if fam.get("clip") and not clip:
            continue
        if fam.get("vae") and not vae:
            continue
        if fam.get("clip_type") and fam["clip_type"] not in _enum_options(info, "CLIPLoader", "type"):
            continue
        chosen = sorted(cands, key=_match_rank(model_filter))[0]
        plan = {"family": fam["name"], "loader": loader, "clip": clip, "vae": vae,
                "clip_type": fam.get("clip_type"), "spec": fam, "model": chosen}
        if fam.get("moe"):
            pair = wan_expert_pair(_enum_options(info, loader, field), chosen)
            if pair and "KSamplerAdvanced" in info and "ModelSamplingSD3" in info:
                plan["high"], plan["low"] = pair
                plan["model"] = pair[0]
        return plan
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


def _build_minimax_workflow(params, model_path, steps, cfg, width, height, length, seed,
                            info=None, plan=None):
    """MiniMax-H3 video graph — by downloads the most-used image-to-video family
    by a wide margin, and the simplest to drive.

    MiniMaxH3ImageToVideo takes the prompt directly (no CLIPTextEncode) and
    returns conditioning *and* the latent together, with `first_frame` optional
    — so one graph covers text-to-video and image-to-video. It emits only a
    positive, so the negative is a zeroed copy of it.
    """
    info = info or {}
    plan = plan or {}
    workflow = {}
    def add(nid, cls, inputs):
        fixed = {}
        for k, v in inputs.items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
                fixed[k] = [str(v[0]), v[1]]
            else:
                fixed[k] = v
        workflow[str(nid)] = {"class_type": cls, "inputs": fixed}

    clip, vae = plan.get("clip"), plan.get("vae")
    if not clip or not vae:
        raise _VideoError("host has no MiniMax text encoder or video VAE")
    saver, saver_inputs = _video_save_node(info, params.get("fps", 24))
    if not saver:
        raise _VideoError("host has no usable video save node")

    add(1, "UNETLoader", {"unet_name": model_path, "weight_dtype": "default"})
    add(2, "CLIPLoader", {"clip_name": clip, "type": plan.get("clip_type") or "minimax"})
    add(3, "VAELoader", {"vae_name": vae})
    add(4, "MiniMaxH3SigmaShift", {"model": [1, 0], "shift_video": 12.0, "shift_audio": 3.0})

    i2v = {"clip": [2, 0], "vae": [3, 0], "prompt": params.get("prompt", ""),
           "width": width, "height": height, "length": length}
    start = params.get("start_image")
    if start:
        add(10, "LoadImage", {"image": start})
        i2v["first_frame"] = [10, 0]
    add(5, "MiniMaxH3ImageToVideo", i2v)
    add(6, "ConditioningZeroOut", {"conditioning": [5, 0]})
    add(7, "KSampler", {
        "model": [4, 0], "positive": [5, 0], "negative": [6, 0],
        "latent_image": [5, 1], "seed": seed, "steps": steps, "cfg": cfg,
        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
    })
    add(8, "VAEDecode", {"samples": [7, 0], "vae": [3, 0]})
    add(9, saver, dict(saver_inputs, images=[8, 0]))
    return workflow


def _build_ltx_workflow(params, model_path, steps, cfg, width, height, length, seed,
                        info=None, plan=None):
    """LTX-2.x video graph, text-to-video or image-to-video.

    Deliberately plain KSampler rather than the SamplerCustomAdvanced chain the
    reference workflow uses: KSamplerSelect's `sampler_name` is a dynamic combo
    with no inline options, so there is nothing valid to put in it, whereas
    KSampler declares its sampler and scheduler enums in the schema.
    """
    info = info or {}
    plan = plan or {}
    workflow = {}
    def add(nid, cls, inputs):
        fixed = {}
        for k, v in inputs.items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
                fixed[k] = [str(v[0]), v[1]]
            else:
                fixed[k] = v
        workflow[str(nid)] = {"class_type": cls, "inputs": fixed}

    fps = float(params.get("fps") or 25)
    saver, saver_inputs = _video_save_node(info, fps)
    if not saver:
        raise _VideoError("host has no usable video save node")

    loader = plan.get("loader") or "CheckpointLoaderSimple"
    if loader == "CheckpointLoaderSimple":
        add(1, "CheckpointLoaderSimple", {"ckpt_name": model_path})
        MODEL, CLIP, VAE = [1, 0], [1, 1], [1, 2]
    else:
        add(1, "UNETLoader", {"unet_name": model_path, "weight_dtype": "default"})
        MODEL, CLIP, VAE = [1, 0], None, None
    if plan.get("clip"):
        add(2, "CLIPLoader", {"clip_name": plan["clip"],
                              "type": plan.get("clip_type") or "ltxv"})
        CLIP = [2, 0]
    if plan.get("vae"):
        add(3, "VAELoader", {"vae_name": plan["vae"]})
        VAE = [3, 0]
    if CLIP is None or VAE is None:
        raise _VideoError("host has no LTX text encoder or VAE")

    add(4, "CLIPTextEncode", {"clip": CLIP, "text": params.get("prompt", "")})
    add(5, "CLIPTextEncode", {"clip": CLIP,
                              "text": params.get("negative_prompt", "")})
    POS, NEG = [4, 0], [5, 0]

    start = params.get("start_image")
    if start and "LTXVImgToVideo" in info:
        add(6, "LoadImage", {"image": start})
        add(7, "LTXVImgToVideo", {
            "positive": POS, "negative": NEG, "vae": VAE, "image": [6, 0],
            "width": width, "height": height, "length": length,
            "batch_size": 1, "strength": 1.0,
        })
        POS, NEG, LATENT = [7, 0], [7, 1], [7, 2]
    else:
        add(7, "EmptyLTXVLatentVideo", {
            "width": width, "height": height, "length": length, "batch_size": 1})
        LATENT = [7, 0]

    add(8, "LTXVConditioning", {"positive": POS, "negative": NEG,
                                "frame_rate": fps})
    add(9, "KSampler", {
        "model": MODEL, "positive": [8, 0], "negative": [8, 1],
        "latent_image": LATENT, "seed": seed, "steps": steps, "cfg": cfg,
        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
    })
    add(10, "VAEDecode", {"samples": [9, 0], "vae": VAE})
    add(11, saver, dict(saver_inputs, images=[10, 0]))
    return workflow


def _video_save_node(info, fps=16):
    """(class, inputs-without-images) for writing a video on this host.

    SaveVideo wants a VIDEO and a `codec` that ComfyUI supplies as a dynamic
    combo with no inline options — nothing we can fill — so prefer
    VHS_VideoCombine, whose format enum is right there in the schema. It files
    its output under "gifs", which COMFY_VIDEO already collects.
    """
    if "VHS_VideoCombine" in info:
        opts = _enum_options(info, "VHS_VideoCombine", "format")
        fmt = (_pick(opts, r"video/h264-mp4", r"video/.*mp4", r"^video/")
               or (opts[0] if opts else "video/h264-mp4"))
        return "VHS_VideoCombine", {
            "frame_rate": float(fps), "loop_count": 0,
            "filename_prefix": "dyva_video", "format": fmt,
            "pingpong": False, "save_output": True,
        }
    if "SaveWEBM" in info and _enum_options(info, "SaveWEBM", "codec"):
        return "SaveWEBM", {"filename_prefix": "dyva_video",
                            "codec": _enum_options(info, "SaveWEBM", "codec")[0],
                            "fps": float(fps), "crf": 32.0}
    if "SaveAnimatedWEBP" in info and _enum_options(info, "SaveAnimatedWEBP", "method"):
        return "SaveAnimatedWEBP", {
            "filename_prefix": "dyva_video", "fps": float(fps), "lossless": False,
            "quality": 80, "method": _enum_options(info, "SaveAnimatedWEBP", "method")[0]}
    return None, None


_HIGH_NOISE = re.compile(r"(?i)high[-_ ]?noise|(?<![a-z])high(?![a-z])")


def wan_expert_pair(names, chosen):
    """The (high, low) halves of a Wan 2.2 MoE, or None if this isn't one.

    Wan 2.2 14B is two experts: the high-noise one lays down structure, the
    low-noise one resolves detail. Running only the high-noise half is exactly
    how you get a blurry mess — which is what a single-model graph does. The
    pair is found by name, because that is how everyone ships them, and that
    also picks up architecture-compatible finetunes like Bernini
    (bernini_r_high_noise_14B / bernini_r_low_noise_14B) for free.
    """
    if not chosen or not _HIGH_NOISE.search(chosen):
        # maybe we picked the low half; try to find its high sibling
        for hi, lo in (("low_noise", "high_noise"), ("low", "high"),
                       ("LOW", "HIGH"), ("Low", "High")):
            if chosen and hi in chosen:
                cand = chosen.replace(hi, lo)
                if cand in names:
                    return cand, chosen
        return None
    for hi, lo in (("high_noise", "low_noise"), ("HIGH", "LOW"),
                   ("High", "Low"), ("high", "low")):
        if hi in chosen:
            cand = chosen.replace(hi, lo)
            if cand in names:
                return chosen, cand
    return None


def _build_wan_workflow(params, model_path, steps, cfg, width, height, length, seed, info=None, plan=None):
    """Wan 2.x video graph, text-to-video or image-to-video.

    WanImageToVideo replaces the empty-latent stage and hands back re-encoded
    conditioning plus the latent, so one graph covers both: omit `start_image`
    and it behaves exactly like the text-only path.

    Everything is resolved against the host's own loader enums. The previous
    version hardcoded the umt5 encoder and took the VAE from `[1, 2]` — but
    node 1 is UNETLoader, whose only output is MODEL, so the VAE was always
    None and every render died in VAEDecode.
    """
    info = info or {}
    prompt = params.get("prompt", "")
    workflow = {}
    def add(nid, cls, inputs):
        # Node ids are strings in the graph, so a link has to name a string
        # too. Passing [8, 0] made ComfyUI look up the integer 8 in a
        # string-keyed dict and raise during validation — reported as the
        # deeply unhelpful "exception_during_validation: 8".
        fixed = {}
        for k, v in inputs.items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
                fixed[k] = [str(v[0]), v[1]]
            else:
                fixed[k] = v
        workflow[str(nid)] = {"class_type": cls, "inputs": fixed}

    plan = plan or {}
    clip = plan.get("clip") or _pick(_enum_options(info, "CLIPLoader", "clip_name"), r"(?i)umt5")
    vae = plan.get("vae") or _pick(_enum_options(info, "VAELoader", "vae_name"), r"(?i)wan.*vae")
    if not clip or not vae:
        raise _VideoError("host has no Wan text encoder or VAE")
    saver, saver_inputs = _video_save_node(info, params.get("fps", 16))
    if not saver:
        raise _VideoError("host has no usable video save node")

    add(1, "UNETLoader", {"unet_name": model_path, "weight_dtype": "default"})
    add(2, "CLIPLoader", {"clip_name": clip, "type": "wan"})
    add(3, "CLIPTextEncode", {"clip": [2, 0], "text": prompt})
    add(4, "CLIPTextEncode", {"clip": [2, 0], "text": params.get("negative_prompt", "")})
    add(10, "VAELoader", {"vae_name": vae})

    # Wan 2.2 ships i2v and t2v weights with different conditioning shapes. An
    # i2v model wants WanImageToVideo either way — start_image is optional
    # there, so it also covers "no picture, just a prompt".
    start = params.get("start_image")
    use_i2v = "WanImageToVideo" in info and (start or re.search(r"(?i)i2v", model_path or ""))
    if use_i2v:
        i2v_inputs = {
            "positive": [3, 0], "negative": [4, 0], "vae": [10, 0],
            "width": width, "height": height, "length": length, "batch_size": 1,
        }
        if start:
            add(11, "LoadImage", {"image": start})
            i2v_inputs["start_image"] = [11, 0]
        add(5, "WanImageToVideo", i2v_inputs)
        POS, NEG, LATENT = [5, 0], [5, 1], [5, 2]
    else:
        add(5, "EmptyHunyuanLatentVideo", {
            "width": width, "height": height, "length": length, "batch_size": 1,
        })
        POS, NEG, LATENT = [3, 0], [4, 0], [5, 0]

    shift = float((plan.get("spec") or {}).get("shift") or 5.0)
    has_shift = "ModelSamplingSD3" in info
    if plan.get("high") and plan.get("low"):
        # Two experts, one denoise. The high-noise half runs the first segment
        # and hands over its *unfinished* latent (return_with_leftover_noise);
        # the low-noise half resumes at the same step without re-noising and
        # finishes. Running either alone is not a cheaper version of this — it
        # is a different, worse thing, and it is why the output was mush.
        boundary = max(1, min(steps - 1, round(steps / 2)))
        add(1, "UNETLoader", {"unet_name": plan["high"], "weight_dtype": "default"})
        add(12, "UNETLoader", {"unet_name": plan["low"], "weight_dtype": "default"})
        add(13, "ModelSamplingSD3", {"model": [1, 0], "shift": shift})
        add(14, "ModelSamplingSD3", {"model": [12, 0], "shift": shift})
        add(6, "KSamplerAdvanced", {
            "model": [13, 0], "add_noise": "enable", "noise_seed": seed,
            "steps": steps, "cfg": cfg, "sampler_name": "euler",
            "scheduler": "simple", "positive": POS, "negative": NEG,
            "latent_image": LATENT, "start_at_step": 0,
            "end_at_step": boundary, "return_with_leftover_noise": "enable",
        })
        add(7, "KSamplerAdvanced", {
            "model": [14, 0], "add_noise": "disable", "noise_seed": 0,
            "steps": steps, "cfg": cfg, "sampler_name": "euler",
            "scheduler": "simple", "positive": POS, "negative": NEG,
            "latent_image": [6, 0], "start_at_step": boundary,
            "end_at_step": 10000, "return_with_leftover_noise": "disable",
        })
    else:
        if has_shift:
            add(13, "ModelSamplingSD3", {"model": [1, 0], "shift": shift})
        MODEL_IN = [13, 0] if has_shift else [1, 0]
        add(6, "VideoLinearCFGGuidance", {"model": MODEL_IN, "min_cfg": 1.0})
        add(7, "KSampler", {
            "model": [6, 0], "positive": POS, "negative": NEG,
            "latent_image": LATENT, "seed": seed, "steps": steps, "cfg": cfg,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        })
    add(8, "VAEDecode", {"samples": [7, 0], "vae": [10, 0]})
    add(9, saver, dict(saver_inputs, images=[8, 0]))
    return workflow


def _build_mochi_workflow(params, model_path, steps, cfg, width, height, length, seed):
    """Text-to-video graph for Mochi-1 (MochiAsync) diffusion models."""
    prompt = params.get("prompt", "")
    workflow = {}
    def add(nid, cls, inputs):
        # Node ids are strings in the graph, so a link has to name a string
        # too. Passing [8, 0] made ComfyUI look up the integer 8 in a
        # string-keyed dict and raise during validation — reported as the
        # deeply unhelpful "exception_during_validation: 8".
        fixed = {}
        for k, v in inputs.items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
                fixed[k] = [str(v[0]), v[1]]
            else:
                fixed[k] = v
        workflow[str(nid)] = {"class_type": cls, "inputs": fixed}

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


async def _submit_video_workflow(session, host, job):
    """Build and submit a text-to-video ComfyUI workflow for the model. Returns
    the ComfyUI prompt_id, or None on submission failure. The builder is chosen
    by the model's family (wan/ltx/mochi); unrecognized families fail cleanly
    rather than sending a garbage graph."""
    params = job.get("params") or {}
    seed = int(params.get("seed", -1))
    if seed == -1:
        import random
        seed = random.randint(0, 2**31 - 1)

    # The builders need the host's own file lists; hardcoding names is how the
    # Wan graph ended up referring to an encoder and a VAE that may not exist.
    info = await comfy_object_info(session, host)

    # A start image has to live on the host before LoadImage can name it.
    if job.get("start_image_b64") and "start_image" not in params:
        try:
            raw = base64.b64decode(job["start_image_b64"])
            params["start_image"] = await comfy_upload_image(
                session, host, raw, job.get("start_image_name") or "start.png")
        except Exception as e:
            log.warning(f"video: {host}: start image upload failed: {e}")

    # The model is chosen here, from what the host can actually load — the
    # name carried on the job is only a hint.
    plan = _video_plan(info, job.get("model_filter") or None,
                       exclude=set(job.get("exclude_models") or ()))
    if not plan:
        raise _VideoError("no usable video model on this host")
    model_path = plan["model"]
    family = plan["family"]
    fam = plan.get("spec") or {}
    # Family defaults first, the request on top — a caller that says nothing
    # gets settings the model was actually trained near.
    steps = int(params.get("steps") or fam.get("steps") or 20)
    cfg = float(params.get("cfg") or fam.get("cfg") or 6.0)
    if params.get("width") and params.get("height"):
        width, height = int(params["width"]), int(params["height"])
    else:
        width, height = video_dims(fam, params.get("aspect", "long"),
                                   params.get("size", "medium"))
    length = video_frames(fam, params.get("length"))
    params = dict(params, fps=params.get("fps") or fam.get("fps") or 16)
    if family == "minimax":
        workflow = _build_minimax_workflow(params, model_path, steps, cfg, width, height,
                                           length, seed, info, plan)
    elif family == "wan":
        workflow = _build_wan_workflow(params, model_path, steps, cfg, width, height, length, seed,
                                       info, plan)
    elif family == "ltx":
        workflow = _build_ltx_workflow(params, model_path, steps, cfg, width, height, length, seed,
                                       info, plan)
    elif family == "mochi":
        workflow = _build_mochi_workflow(params, model_path, steps, cfg, width, height, length, seed)
    else:
        raise _VideoError(f"unsupported video model family for {model_path!r}")

    return await comfy_submit(session, host, workflow), plan


async def _poll_video_result(session, host, prompt_id, job, on_status=None):
    """Poll ComfyUI /history/{prompt_id} until the video is saved; return
    (bytes, filename).

    The clock has to cover queueing as well as rendering: the host took the job
    but may be working through other people's first, and a few seconds per
    frame is normal. Waiting costs nothing here — the host was already chosen,
    so nothing else is blocked on it — whereas giving up early throws away a
    render that was going to finish.
    """
    try:
        return await comfy_collect(session, host, prompt_id, COMFY_VIDEO,
                                   timeout=VIDEO_RENDER_TIMEOUT, poll=3,
                                   view_timeout=120, on_status=on_status)
    except ComfyError as e:
        raise _VideoError(str(e))


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
    # kept as a hint; the actual model is resolved per host against its loaders
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
    # A start image turns this into image-to-video. Kept as bytes on the job,
    # not in params, because it has to be uploaded to whichever host wins.
    start = body.get("image") or body.get("start_image")
    if isinstance(start, str) and start.strip():
        blob = start.split(",", 1)[1] if start.startswith("data:") else start
        try:
            params.pop("image", None)
            params.pop("start_image", None)
            job_start = base64.b64decode(blob)
        except Exception:
            return web.json_response({"error": "'image' is not valid base64"}, status=400)
    else:
        job_start = None
    # `resolution: "832x480"` is the friendly spelling; the builders want
    # width/height. Accepting one and reading the other is how these two
    # controls came to do nothing at all.
    res = str(body.get("resolution") or "").strip()
    m = re.match(r"^(\d+)\s*[x\u00d7]\s*(\d+)$", res)
    if m:
        params.setdefault("width", int(m.group(1)))
        params.setdefault("height", int(m.group(2)))
    job = {
        "id": None,
        "status": "pending",
        "host": chosen_host,
        "model": chosen_model,
        "model_filter": model_filter,
        "target_host": request.query.get("host") or None,
        "prompt": prompt_text,
        "params": params,
        "start_image_b64": base64.b64encode(job_start).decode() if job_start else None,
        "start_image_name": str(body.get("image_name") or "start.png"),
        "created": time.time(),
        "unsigned_urls": [],
    }
    jid = _video_job_new(job)
    job["id"] = jid
    job["polling_url"] = f"/v1/videos/{jid}"
    _video_job_save()
    asyncio.get_event_loop().create_task(_run_video_job(request.app["session"], jid))
    return web.json_response({"id": jid, "polling_url": job["polling_url"], "status": "pending"}, status=202)


# A host's model list is every file it will admit to having. These are the ones
# that are never the thing you sample with, and matching them is how a job ends
# up trying to render with a VAE or a LoRA.
_NOT_A_MODEL = re.compile(
    r"(?i)(^|[/\\_.-])(vae|clip|t5|umt5|encoder|text_encoder|lora|embed|"
    r"upscaler|upsampler|taesd|refiner|controlnet|ipadapter|preview)"
    r"|\.(pt|pth|onnx|bin|ckpt\.index)$|_sr_|latent_upscal"
    # Text encoders are named after the LLM they wrap, not after their job:
    # qwen3vl_32b_minimax_h3 is MiniMax's *encoder*, not a MiniMax model.
    r"|^(qwen[0-9._]*vl|qwen_[0-9._]+_vl|gemma|mistral|llava|byt5|pile-t5|flan)")


async def handle_video_models(request):
    """
    List available video models
    ---
    tags: [Video]
    summary: GET /v1/videos/models — video-class models across discovered ComfyUI hosts
    description: |
      Only files that are actually the thing you sample with: VAEs, text
      encoders, LoRAs and upscalers are excluded even when their names carry a
      family word. Grouped by the family that would drive them, commonest
      first.
    responses:
      '200':
        description: Model list
    """
    fams = [(f.get("name"), re.compile(f["model"])) for f in load_video_families()
            if f.get("model")]
    seen = {}
    for srv in load_servers():
        if srv.get("service") != "comfyui":
            continue
        host = srv.get("server", "")
        for m in srv.get("models") or []:
            name = m.replace("\\", "/").rsplit("/", 1)[-1]
            if not name or _NOT_A_MODEL.search(name):
                continue
            fam = next((fn for fn, rx in fams if rx.search(name)), None)
            if not fam:
                continue
            row = seen.setdefault(name, {"id": name, "family": fam,
                                         "hosts": [], "count": 0})
            row["count"] += 1
            if host and host not in row["hosts"]:
                row["hosts"].append(host)
    return web.json_response({
        "object": "list",
        "data": sorted(seen.values(), key=lambda x: (-x["count"], x["id"])),
    })


async def handle_videos_list(request):
    """
    Recent video jobs
    ---
    tags: [Video]
    summary: GET /v1/videos — the recent job list, newest first
    description: |
      Metadata only; the bytes are at `/v1/videos/{id}/content`. Completed jobs
      first-class, but in-flight and failed ones are listed too so the gallery
      can show what is cooking and what went wrong.
    responses:
      '200':
        description: Job list
    """
    out = []
    for jid, job in _VIDEO_JOBS.items():
        out.append({
            "id": jid,
            "status": job.get("status"),
            "prompt": job.get("prompt") or "",
            "model": job.get("model") or "",
            "host": job.get("host") or "",
            "created": job.get("created"),
            "error": job.get("error"),
            "frames": (job.get("params") or {}).get("length"),
            "has_start_image": bool(job.get("start_image_b64")),
            "content_url": (f"/v1/videos/{jid}/content"
                            if job.get("content_path") else None),
        })
    out.sort(key=lambda j: j.get("created") or 0, reverse=True)
    return web.json_response(out)


async def handle_videos_delete(request):
    """
    Delete a video job
    ---
    tags: [Video]
    summary: GET /v1/videos/{id}/delete — remove a job and its file
    responses:
      '200':
        description: Deleted
    """
    jid = request.match_info.get("id", "")
    job = _VIDEO_JOBS.pop(jid, None)
    if job and job.get("content_path"):
        try:
            os.remove(job["content_path"])
        except OSError:
            pass
    _video_job_save()
    return web.json_response({"deleted": bool(job)})


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
        "host": job.get("host"),
        # where the host says the job actually is: "queued #3", "rendering"
        "phase": job.get("phase"),
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


def _save_audio_clip(raw, filename):
    """Store a generated clip and return the name it is served under."""
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
        ext = os.path.splitext(filename or "")[1].lower() or ".flac"
        if ext not in _TTS_MIME:
            ext = ".flac"
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{ext}"
        with open(os.path.join(AUDIO_DIR, name), "wb") as f:
            f.write(raw)
        clips = sorted(os.listdir(AUDIO_DIR))
        for old in clips[:-AUDIO_KEEP]:
            try:
                os.remove(os.path.join(AUDIO_DIR, old))
            except OSError:
                pass
        return name
    except Exception as e:
        log.warning(f"tts: could not save clip: {e}")
        return None


async def handle_audio_clip(request):
    """
    Fetch a generated speech clip
    ---
    tags: [Audio]
    summary: GET /v1/audio/clips/{name} — a clip produced by /v1/audio/speech
    responses:
      '200':
        description: Audio bytes
      '404':
        description: No such clip
    """
    name = os.path.basename(request.match_info.get("name", ""))
    path = os.path.join(AUDIO_DIR, name)
    if not name or not os.path.isfile(path):
        return web.json_response({"error": "no such clip"}, status=404)
    with open(path, "rb") as f:
        body = f.read()
    ext = os.path.splitext(name)[1].lower()
    return web.Response(body=body,
                        content_type=_TTS_MIME.get(ext, "application/octet-stream"),
                        headers={"Cache-Control": "public, max-age=86400"})


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
      host-native format (usually flac). The response carries provenance in
      `X-Dyva-Host` (the ComfyUI host that spoke) and `X-Dyva-Node` (the TTS
      node class that produced it); both are listed in
      `Access-Control-Expose-Headers` so cross-origin callers can read them.
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
    if len(input_text) > TTS_MAX_CHARS:
        # Fail fast and legibly rather than after minutes of chunked rendering.
        return web.json_response(
            {"error": f"'input' is {len(input_text)} characters; the limit is "
                      f"{TTS_MAX_CHARS}. Speech is rendered in {TTS_CHUNK_CHARS}-character "
                      f"chunks, one after another, so long passages take minutes. "
                      f"Send a shorter passage."},
            status=400)
    voice = body.get("voice")
    if not isinstance(voice, str) or not voice.strip():
        voice = None
    else:
        voice = voice.strip()

    # Reputation is keyed by the voice model asked for, so a host written off
    # for one TTS node isn't written off for all of them.
    tkey = tts_key(str(body.get("model") or "").strip() or None)
    hosts = _find_tts_hosts(request.query.get("host"),
                            str(body.get("model") or "").strip() or None)
    if not hosts:
        return web.json_response({"error": "no ComfyUI hosts available for tts"}, status=503)

    session = request.app["session"]
    snippet = input_text[:60] + ("..." if len(input_text) > 60 else "")
    errors_list = []

    label = f"tts: {snippet}"

    # Same race as text generation: one bounded worker pool over the ranked host
    # list, first success wins, and the same activity vocabulary
    # (trying/failure/success naming the host) so the feed reads identically
    # whether the request was for tokens or for audio.
    # The race only has to get the job accepted somewhere.
    async def attempt(host, wid, done):
        await broadcast_activity(host, tkey, "trying",
            f"trying: {host} for {label}", wid=wid)
        start = time.time()
        try:
            prompt_id, node = await _tts_submit(session, host, input_text, voice)
        except (_TtsError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            err = str(e) or type(e).__name__
            dur = time.time() - start
            errors_list.append(f"{host}: {err}")
            await broadcast_activity(host, tkey, "failed",
                f"failure: {host} for {label} - {err}", duration=dur, wid=wid)
            if isinstance(e, _TtsUnsuitable):
                return unsuitable(err)
            if isinstance(e, ComfyUnreachable):
                return unreachable_host(err)
            if isinstance(e, asyncio.TimeoutError):
                return timed_out(err)
            if isinstance(e, (aiohttp.ClientError, OSError)):
                return unreachable_host(err)
            return failed(err)
        dur = time.time() - start
        await broadcast_activity(host, tkey, "connected",
            f"success: {host} for {label}", duration=dur, wid=wid, rmodel=node)
        return accepted((host, prompt_id, node))

    # A host that accepts the prompt and then can't render it is a verdict like
    # any other — record it, drop it, and hand the job to the next one, rather
    # than failing the request on one bad node. (Edit and video already did
    # this; speech was the last one that gave up after a single attempt.)
    tried_hosts, tried = set(), 0
    tally = collections.Counter()
    stopped = False
    for _round in range(EDIT_RENDER_ATTEMPTS):
        pool = [h for h in hosts if h not in tried_hosts]
        if not pool:
            break
        result, stopped, n_tried, round_tally = await _race_hosts(pool, attempt, tkey)
        tried += n_tried
        tally.update(round_tally)
        if not result or stopped:
            break
        host, prompt_id, node = result
        tried_hosts.add(host)
        try:
            async with _waiting_worker(label, host, "speaking"):
                raw, filename = await _tts_collect(
                    session, host, prompt_id, timeout=_tts_render_budget(input_text))
        except (ComfyError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            outcome = render_verdict(e)
            record_verdict(host, tkey, outcome)
            tally[outcome.verdict] += 1
            err = str(e) or type(e).__name__
            errors_list.append(f"{host}: {err} [{node}]")
            log.warning(f"tts: {host}: took the job then failed to render: {err} [{node}]")
            await broadcast_activity(host, tkey, "failed",
                f"failure: {host} for {label} - render: {err}")
            # The node is what failed, not necessarily the host — but we only
            # detect one node per host, so forget the detection and let the
            # next probe pick differently.
            _TTS_NODE_CACHE.pop(host, None)
            continue

        clip = _save_audio_clip(raw, filename)
        headers = {
            "X-Dyva-Host": host,
            "X-Dyva-Node": node,
            "Access-Control-Expose-Headers": "X-Dyva-Host, X-Dyva-Node, X-Dyva-File",
        }
        if clip:
            headers["X-Dyva-File"] = clip
        ext = os.path.splitext(filename)[1].lower()
        return web.Response(body=raw,
                            content_type=_TTS_MIME.get(ext, "application/octet-stream"),
                            headers=headers)

    if stopped:
        return web.json_response({"error": "tts stopped"}, status=499)

    reasons = collections.Counter(e.split(": ", 1)[1] if ": " in e else e for e in errors_list)
    detail = "; ".join(f"{c}x {r}" for r, c in reasons.most_common()) or "no hosts tried"
    verdicts = ", ".join(f"{c} {v}" for v, c in tally.most_common())
    return web.json_response(
        {"error": f"tts failed on {tried} host{'' if tried == 1 else 's'} "
                  f"({verdicts}): {detail}"},
        status=502)


async def handle_audio_voices(request):
    """
    List available TTS voices/nodes across hosts
    ---
    tags: [Audio]
    summary: GET /v1/audio/voices — TTS nodes and voices per host, from the stored survey
    description: |
      Answers from the persisted node survey in `host-status.db`, so it returns
      immediately instead of pulling a multi-megabyte `/object_info` from every
      candidate host. Hosts that have never been surveyed are probed in the
      background and appear on a later call; `?refresh=1` waits for that sweep
      instead of returning what is already known.
    parameters:
      - in: query
        name: host
        schema:
          type: string
        description: Target a specific ComfyUI host (ip:port)
      - in: query
        name: refresh
        schema:
          type: boolean
        description: Re-probe now and wait, instead of answering from the survey
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

    force = request.query.get("refresh") in ("1", "true", "yes")

    async def probe(host):
        """Survey one host. _tts_node_for writes the result through to
        host_nodes, so this records the node the speech path would actually
        pick — the voice list and the routing can't disagree."""
        try:
            info = await comfy_object_info(session, host)
        except (ComfyError, asyncio.TimeoutError, aiohttp.ClientError, OSError):
            return
        await _tts_node_for(session, host, info)

    survey = load_node_survey(TTS_KEY)
    stale = [h for h in hosts
             if force or h not in survey
             or time.time() - (survey[h].get("checked") or 0) > NODE_SURVEY_TTL]

    if force or (stale and not survey):
        # Nothing useful to show yet (or an explicit refresh) — wait for it.
        await asyncio.gather(*(probe(h) for h in stale), return_exceptions=True)
        survey = load_node_survey(TTS_KEY)
    elif stale:
        # Answer from what we know and fill the gaps behind the response, so the
        # voice box populates immediately and improves on the next load.
        async def _fill():
            for h in stale:
                try:
                    await probe(h)
                except Exception:
                    pass
        asyncio.create_task(_fill())

    voices, details, seen = [], [], set()
    for host in hosts:
        row = survey.get(host)
        if not row or not row.get("node"):
            continue
        details.append({"host": host,
                        "nodes": [{"node": row["node"], "voices": row.get("voices") or []}],
                        "checked": row.get("checked")})
        for v in row.get("voices") or []:
            if v not in seen:
                seen.add(v)
                voices.append(v)
    return web.json_response({"voices": voices, "hosts": details,
                              "surveying": len(stale) if not force else 0})


# ---- Image editing ------------------------------------------------------
# Distinct from txt2img: an edit model takes a prompt plus zero or more
# reference images ("put the feather cap on the dog", dog.jpg, cap.jpg).
# Two node shapes cover what is actually deployed:
#   encode    - Qwen-Image-Edit: images go straight into
#               TextEncodeQwenImageEditPlus(clip, prompt, vae, image1..3)
#   reference - Flux Kontext: no multi-image input node, so each reference is
#               LoadImage -> FluxKontextImageScale -> VAEEncode -> ReferenceLatent,
#               chained through the conditioning.
# No wildcard in the key: add_bad()/add_good() run it through canon_pattern(),
# which strips a trailing "*", so marks written under "edit/*" landed in the
# table as "edit/" and were then looked up as "edit/*" — never matching. Hosts
# stayed unranked forever and got retried no matter how often they failed.
EDIT_KEY = "edit"        # the unfiltered case; see edit_key()


def edit_key(model_filter=None):
    """Reputation key for an edit request — per model, not one bucket.

    A host that can't run flux2 may be perfectly good at qwen-image-edit, so a
    single __edit__ sentinel condemned hosts far too broadly. The key is the
    query you searched with, canonicalised so flux*2, flux-2 and FLUX.2 all
    share one record: edit/flux2, and edit/* when nothing was named.
    """
    q = _sep_insensitive((model_filter or "").replace("*", "").replace("?", ""))
    return f"edit/{q}" if q else EDIT_KEY
# Editing runs a full diffusion sample on someone else's GPU, often a queued
# one. Ten minutes is not generous, it's realistic.
EDIT_RENDER_TIMEOUT = 900
# Each attempt races only until the *first host accepts*, so a round that dies
# in the render has cost two or three hosts out of hundreds. Three rounds gave
# up after ~8 candidates with 60 still untried; the point of the retry is to
# work through the bad pairings, so give it room.
EDIT_RENDER_ATTEMPTS = 8
# "Match source" means the same shape, not the same pixel count. Diffusion
# models want dimensions on a grid (64 is the safe common denominator) and fall
# over on very large inputs, so a phone photo gets scaled down, on aspect, to
# something they'll accept.
EDIT_MAX_MP = 3.0
EDIT_ROUND = 64


def fit_dims(w, h, max_mp=EDIT_MAX_MP, step=EDIT_ROUND):
    """Largest on-aspect size within `max_mp` megapixels, snapped to `step`.
    Only ever shrinks — a small source stays small."""
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    scale = min(1.0, math.sqrt((max_mp * 1e6) / float(w * h)))
    nw = max(step, int(round(w * scale / step)) * step)
    nh = max(step, int(round(h * scale / step)) * step)
    return nw, nh

# A render failure is a verdict about the host, not just about this request.
# These say the host can't run this graph at all — a different prompt would
# fail the same way — so the engine should hand the job to someone else.
_RENDER_UNSUITABLE = re.compile(
    r"(?i)VAE is invalid|shapes cannot be multiplied|size mismatch"
    r"|not found|no such file|missing|out of memory|CUDA error"
    r"|has no attribute|unexpected key")


def render_verdict(err):
    """Turn an exception from the render phase into a verdict, so a host that
    took the job and then couldn't do it stops looking like a good host."""
    if isinstance(err, asyncio.TimeoutError):
        return timed_out(str(err) or "timeout")
    text = str(err) or type(err).__name__
    if isinstance(err, (ComfyUnreachable, aiohttp.ClientError, OSError)):
        return unreachable_host(text)
    if _RENDER_UNSUITABLE.search(text):
        return unsuitable(text)
    return failed(text)
_edit_classifier_cache = None


def load_edit_families():
    global _edit_classifier_cache
    if _edit_classifier_cache is not None:
        return _edit_classifier_cache
    out = []
    try:
        with open(NODE_CLASSIFIER_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        for spec in (raw.get("edit") or []):
            fam = dict(spec)
            fam["regs"] = [re.compile(x) for x in (spec.get("patterns") or [])]
            out.append(fam)
    except Exception as e:
        log.warning(f"node-classifier: failed to load edit families: {e}")
    _edit_classifier_cache = out
    return out


async def comfy_upload_image(session, host, data, filename):
    """Put an image on a host so a graph can reference it.

    ComfyUI's LoadImage takes a filename from the host's own input directory —
    there is no way to hand a graph raw pixels — so every reference image must
    be uploaded first. Returns the name LoadImage should use.
    """
    form = aiohttp.FormData()
    form.add_field("image", data, filename=filename,
                   content_type="application/octet-stream")
    form.add_field("type", "input")
    form.add_field("overwrite", "true")
    try:
        r = await session.post(_host_url(host, "/upload/image"), data=form,
                               timeout=aiohttp.ClientTimeout(total=60))
        body = await r.text()
        status = r.status
        await r.release()
    except Exception as e:
        raise ComfyError(f"upload failed: {e or type(e).__name__}")
    if status != 200:
        raise ComfyError(f"upload HTTP {status}: {body[:120]}")
    try:
        got = json.loads(body)
    except Exception:
        raise ComfyError("upload: unparseable response")
    name = got.get("name")
    if not name:
        raise ComfyError("upload: no name in response")
    sub = got.get("subfolder") or ""
    return f"{sub}/{name}" if sub else name


def _enum_options(info, node, field):
    entry = (((info.get(node) or {}).get("input") or {}).get("required") or {}).get(field)
    if isinstance(entry, list) and entry and isinstance(entry[0], list):
        return [x for x in entry[0] if isinstance(x, str)]
    return []


def _match_rank(needle):
    """Order candidates so the closest name to what the user typed wins:
    an exact file-stem match, then a prefix, then anything containing it,
    shortest first (a bare model beats a lora or a long variant)."""
    n = _sep_insensitive((needle or "").replace("*", "").replace("?", ""))

    def key(option):
        base = option.replace("\\", "/").rsplit("/", 1)[-1].lower()
        flat = _sep_insensitive(os.path.splitext(base)[0])
        if not n:
            return (3, len(base), base)
        if flat == n:
            return (0, len(base), base)
        if flat.startswith(n):
            return (1, len(base), base)
        return (2, len(base), base)
    return key


_SEPS_RE = re.compile(r"[.\-_ ]+")


def _sep_insensitive(v):
    """Model names spell the same thing every which way — flux.2, flux-2,
    flux 2, flux2 — so separators shouldn't decide a match."""
    return _SEPS_RE.sub("", (v or "").lower())


def model_query_match(name, query):
    """match_model, but blind to separators. Globs still work: `flux*2` matches
    flux2-dev and flux-2-klein, and so does the literal `flux.2`."""
    if not (query or "").strip():
        return True
    # Try the name as written first, so wildcards keep their exact meaning
    # (`flux?2` still wants one character between them) ...
    if match_model(name, query):
        return True
    # ... then again with separators removed, so flux.2 / flux-2 / flux 2 all
    # find flux2-dev.
    n, q = _sep_insensitive(name), _sep_insensitive(query)
    if not q:
        return True
    if any(c in q for c in "*?["):
        return fnmatch.fnmatch(n, f"*{q}*")
    return q in n


def _hints(v):
    """A family hint may be one pattern or several in priority order."""
    if isinstance(v, list):
        return [x for x in v if x]
    return [v] if v else []


def _pick(options, *patterns):
    """First option matching any pattern, in pattern order."""
    for pat in patterns:
        if not pat:
            continue
        rx = re.compile(pat)
        for o in options:
            if rx.search(o):
                return o
    return None


def _edit_plan(info, model_filter=None, exclude=(), n_images=0):
    """Decide how to drive an edit on this host, or None if it can't.

    Everything is resolved against the host's live loader enums — those are the
    authoritative list of what is on disk — so a plan is only returned when
    every file the graph needs actually exists there.
    """
    ckpts = _enum_options(info, "CheckpointLoaderSimple", "ckpt_name")
    unets = _enum_options(info, "UNETLoader", "unet_name")
    clips = _enum_options(info, "CLIPLoader", "clip_name")
    vaes = _enum_options(info, "VAELoader", "vae_name")
    clip_types = _enum_options(info, "CLIPLoader", "type")
    dual_types = _enum_options(info, "DualCLIPLoader", "type")

    for fam in load_edit_families():
        encode = next((k for k in info if any(r.search(k) for r in fam["regs"])), None)
        if not encode:
            continue
        # "Put the lady into the garden" is two references, and an encode-style
        # family has a fixed number of image slots. zip() would silently drop
        # the extras and render a confidently wrong picture, so a family that
        # can't hold them all isn't a candidate. Reference-style families chain
        # a ReferenceLatent per image and take any number.
        if fam.get("style") == "encode" and n_images > len(fam.get("images") or []):
            continue
        out_node = comfy_image_out(info)
        for need in ("KSampler", "VAEDecode", "LoadImage"):
            if need not in info:
                break
        else:
            if not out_node:
                continue
            # What the user typed *narrows within* a family; it never overrides
            # it. "flux" must not pick a Flux checkpoint and then drive it with
            # Qwen's encoder, CLIP and VAE — so a candidate has to satisfy both
            # the family's own pattern and the user's text. Matching is
            # match_model(), the same partial/glob rule the rest of dyva uses,
            # so "flux" finds every Flux variant on the host.
            fam_rx = re.compile(fam["model"]) if fam.get("model") else None

            def candidates(options):
                out = [o for o in options if not fam_rx or fam_rx.search(o)]
                if exclude:
                    out = [o for o in out
                           if o not in exclude
                           and _sep_insensitive(os.path.splitext(
                               o.replace("\\", "/").rsplit("/", 1)[-1])[0]) not in exclude]
                if model_filter:
                    out = [o for o in out if model_query_match(o, model_filter)]
                return sorted(out, key=_match_rank(model_filter))

            unet = next(iter(candidates(unets)), None)
            ckpt = next(iter(candidates(ckpts)), None)
            if not unet and not ckpt:
                continue

            # Resolve the text encoder and VAE the same way whichever loader
            # holds the transformer. These edit models are never all-in-one
            # checkpoints — CheckpointLoaderSimple hands back a null CLIP and a
            # null VAE for them, and you only find out minutes into the render
            # ("clip input is invalid: None"). So if the family names a text
            # encoder or a VAE and the host hasn't got it, the host can't run
            # this graph. Say so now instead of burning someone's GPU.
            clip1 = _pick(clips, *_hints(fam.get("clip"))) if fam.get("clip") else None
            clip2 = _pick(clips, *_hints(fam.get("clip2"))) if fam.get("clip2") else None
            vae = _pick(vaes, *_hints(fam.get("vae"))) if fam.get("vae") else None
            if fam.get("clip") and not clip1:
                continue
            if fam.get("clip2") and not clip2:
                continue
            if fam.get("vae") and not vae:
                continue
            if fam.get("clip_type"):
                needed = dual_types if fam.get("clip2") else clip_types
                if fam["clip_type"] not in needed:
                    continue

            loader = {"clip": clip1, "clip2": clip2, "vae": vae,
                      "clip_type": fam.get("clip_type")}
            if unet:
                loader["kind"] = "unet"
                loader["unet"] = unet
            else:
                loader["kind"] = "checkpoint"
                loader["ckpt"] = ckpt
            return {"family": fam, "encode": encode, "loader": loader,
                    "model": unet or ckpt, "out": out_node}
    return None


def _edit_workflow(plan, prompt, image_names, params=None):
    """Build the edit graph. `image_names` are already uploaded to the host."""
    params = params or {}
    fam = plan["family"]
    loader = plan["loader"]
    g = {}

    if loader["kind"] == "checkpoint":
        g["1"] = {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": loader["ckpt"]}}
        MODEL, CLIP, VAE = ["1", 0], ["1", 1], ["1", 2]
    else:
        g["1"] = {"class_type": "UNETLoader",
                  "inputs": {"unet_name": loader["unet"], "weight_dtype": "default"}}
        MODEL, CLIP, VAE = ["1", 0], None, None

    # An explicitly-resolved encoder or VAE always wins over whatever the
    # checkpoint may or may not contain.
    if loader.get("clip"):
        if loader.get("clip2"):
            g["2"] = {"class_type": "DualCLIPLoader",
                      "inputs": {"clip_name1": loader["clip"],
                                 "clip_name2": loader["clip2"],
                                 "type": loader["clip_type"]}}
        else:
            g["2"] = {"class_type": "CLIPLoader",
                      "inputs": {"clip_name": loader["clip"],
                                 "type": loader["clip_type"]}}
        CLIP = ["2", 0]
    if loader.get("vae"):
        g["3"] = {"class_type": "VAELoader", "inputs": {"vae_name": loader["vae"]}}
        VAE = ["3", 0]
    if CLIP is None or VAE is None:
        raise ComfyUnsuitable("no text encoder or VAE available for this model")

    # LoadImage per reference
    img_nodes = []
    for i, name in enumerate(image_names):
        nid = f"10{i}"
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        img_nodes.append([nid, 0])

    width = int(params.get("width", 1024))
    height = int(params.get("height", 1024))
    steps = int(params.get("steps", fam.get("steps", 20)))
    cfg = float(params.get("cfg_scale", fam.get("cfg", 2.5)))
    seed = int(params.get("seed", -1))
    if seed == -1:
        seed = random.randint(0, 2**31 - 1)
    sampler = params.get("sampler_name", "euler")

    if fam.get("style") == "encode":
        slots = fam.get("images") or []
        pos_in = {"clip": CLIP, "prompt": prompt, "vae": VAE}
        for slot, ref in zip(slots, img_nodes):
            pos_in[slot] = ref
        g["200"] = {"class_type": plan["encode"], "inputs": pos_in}
        g["201"] = {"class_type": plan["encode"],
                    "inputs": {"clip": CLIP, "prompt": "", "vae": VAE}}
        POS, NEG = ["200", 0], ["201", 0]
        scaled_latent = None
    else:
        # reference style: chain a ReferenceLatent per scaled, encoded image
        g["200"] = {"class_type": "CLIPTextEncode",
                    "inputs": {"clip": CLIP, "text": prompt}}
        g["201"] = {"class_type": "CLIPTextEncode",
                    "inputs": {"clip": CLIP, "text": ""}}
        cond = ["200", 0]
        scaled_latent = None
        for i, ref in enumerate(img_nodes):
            sid, eid, rid = f"30{i}", f"31{i}", f"32{i}"
            g[sid] = {"class_type": fam.get("scale", "FluxKontextImageScale"),
                      "inputs": {"image": ref}}
            g[eid] = {"class_type": "VAEEncode",
                      "inputs": {"pixels": [sid, 0], "vae": VAE}}
            g[rid] = {"class_type": plan["encode"],
                      "inputs": {"conditioning": cond, "latent": [eid, 0]}}
            cond = [rid, 0]
            if scaled_latent is None:
                scaled_latent = [eid, 0]
        gnode = fam.get("guidance_node")
        if gnode:
            g["400"] = {"class_type": gnode,
                        "inputs": {"conditioning": cond,
                                   "guidance": float(fam.get("guidance", 2.5))}}
            cond = ["400", 0]
        POS, NEG = cond, ["201", 0]

    # Where the sampler starts, and therefore the output's shape.
    #   match_source (default): start from the first reference, so the result
    #     keeps its proportions. Nobody should have to look up their cat's
    #     aspect ratio to avoid a stretched cat.
    #   otherwise: an empty latent at the requested width/height.
    match_source = params.get("match_source", True)
    if isinstance(match_source, str):
        match_source = match_source.lower() not in ("0", "false", "no", "")
    if img_nodes and match_source:
        if scaled_latent is not None:
            # already encoded for the reference chain — FluxKontextImageScale
            # has snapped it to a size the model likes, so reuse that
            LATENT = scaled_latent
        else:
            # Nothing has constrained the source yet. Keep the aspect ratio but
            # bring it onto the grid and under the pixel budget: a 40MP photo
            # straight into VAEEncode is how you get a host to yell at you.
            src = img_nodes[0]
            fitted = fit_dims(params.get("source_width"), params.get("source_height"))
            if fitted:
                g["490"] = {"class_type": "ImageScale",
                            "inputs": {"image": src, "upscale_method": "lanczos",
                                       "width": fitted[0], "height": fitted[1],
                                       "crop": "disabled"}}
                src = ["490", 0]
            elif "ImageScaleToTotalPixels" in (host_info or {}):
                # no dimensions from the caller — let the host work it out
                g["490"] = {"class_type": "ImageScaleToTotalPixels",
                            "inputs": {"image": src, "upscale_method": "lanczos",
                                       "megapixels": EDIT_MAX_MP,
                                       "resolution_steps": EDIT_ROUND}}
                src = ["490", 0]
            g["500"] = {"class_type": "VAEEncode",
                        "inputs": {"pixels": src, "vae": VAE}}
            LATENT = ["500", 0]
    else:
        g["500"] = {"class_type": "EmptySD3LatentImage",
                    "inputs": {"width": width, "height": height, "batch_size": 1}}
        LATENT = ["500", 0]

    g["600"] = {"class_type": "KSampler", "inputs": {
        "model": MODEL, "seed": seed, "steps": steps, "cfg": cfg,
        "sampler_name": sampler, "scheduler": "simple",
        "positive": POS, "negative": NEG, "latent_image": LATENT,
        "denoise": float(params.get("denoise", 1.0))}}
    g["700"] = {"class_type": "VAEDecode",
                "inputs": {"samples": ["600", 0], "vae": VAE}}
    out_node = plan.get("out") or "SaveImage"
    g["800"] = ({"class_type": "PreviewImage", "inputs": {"images": ["700", 0]}}
                if out_node == "PreviewImage" else
                {"class_type": "SaveImage",
                 "inputs": {"images": ["700", 0], "filename_prefix": "dyva/edit"}})
    return g


def _host_edit_models(host, model_filter=None):
    """A host's cached edit-class models, optionally narrowed to a query."""
    out = []
    for s in load_servers():
        if s.get("server") != host:
            continue
        for m in s.get("models") or []:
            if classify_model(m) != "edit":
                continue
            if model_filter and not match_model(m, model_filter):
                continue
            out.append(m)
    return out


# A structural render failure is really a fact about one (host, model) pairing,
# not about the host. Recording it against the host alone doesn't work: the
# submit already marked the host *good*, and add_bad only demotes good ->
# maybe_good, which still ranks near the top — so the same doomed pairing got
# chosen again on every request. Remember the pairing itself.
def _pair_key(cap, model):
    base = (model or "").replace("\\", "/").rsplit("/", 1)[-1]
    return f"{cap}!{_sep_insensitive(os.path.splitext(base)[0])}"


def remember_bad_pair(host, cap, model):
    if model:
        force_bad(host, _pair_key(cap, model))


def bad_pairs_for(host, cap):
    """Canonical model names this host has already failed structurally."""
    pre = f"{cap}!"
    out = set()
    for (m,) in _get_db().execute(
            "SELECT model FROM host_status WHERE host=? AND state='bad' AND model LIKE ?",
            (host, pre + "%")):
        out.add(m[len(pre):])
    return out


def _plan_summary(plan):
    """The rig a plan actually assembled, short enough for an error line."""
    L = plan.get("loader") or {}
    base = lambda v: (v or "").replace("\\", "/").rsplit("/", 1)[-1]
    parts = [plan.get("family", {}).get("name", "?"), base(plan.get("model"))]
    clip = base(L.get("clip"))
    if L.get("clip2"):
        clip += "+" + base(L["clip2"])
    if clip:
        parts.append(f"clip[{L.get('clip_type', '?')}] {clip}")
    if L.get("vae"):
        parts.append(f"vae {base(L['vae'])}")
    return " | ".join(p for p in parts if p)


def _find_edit_hosts(target_host=None, model_filter=None):
    """ComfyUI hosts that might run an edit model, best-first — same tiering as
    everything else, keyed on __edit__.

    When a model is named, hosts whose cached model list actually contains a
    match come first. Without that, asking for "flux" would race whichever
    hosts happened to rank highest, most of which don't have one, and every
    attempt would come back unsuitable before reaching a host that does."""
    if target_host:
        return [target_host] if any(x.get("service") == "comfyui"
                                    for x in load_servers()
                                    if x.get("server") == target_host) else []
    hosts = [x.get("server") for x in load_servers()
             if x.get("service") == "comfyui" and x.get("server")]
    marks = load_marks()
    ekey = edit_key(model_filter)
    last = get_last(ekey)
    last_host = last[0] if last and last[0] else None

    def rank(h):
        tier = capability_tier(h, ekey, marks, last_host)
        if model_filter:
            has = 0 if _host_edit_models(h, model_filter) else 1
        else:
            has = 0 if _host_has_class(h, {"edit"}) else 1
        return (has, tier) if model_filter else (tier, has)

    hosts.sort(key=rank)
    return _idle_first(hosts)


async def _edit_read_request(request):
    """Accept OpenAI's image-edit shape in either transport.

    multipart is what the OpenAI clients send (`image` repeated, or `image[]`);
    JSON with base64 / data: URLs is what a browser fetch finds easier. Returns
    (prompt, [(filename, bytes)], model, params).
    """
    images, params = [], {}
    ctype = (request.headers.get("Content-Type") or "").lower()
    if ctype.startswith("multipart/"):
        reader = await request.multipart()
        prompt = model = None
        while True:
            part = await reader.next()
            if part is None:
                break
            name = part.name or ""
            if name in ("image", "image[]", "images", "images[]"):
                data = await part.read(decode=False)
                if data:
                    images.append((part.filename or f"dyva-{len(images)}.png", data))
            elif name == "prompt":
                prompt = (await part.text()).strip()
            elif name == "model":
                model = (await part.text()).strip()
            else:
                params[name] = (await part.text()).strip()
        return prompt, images, model, params

    body = await request.json()
    prompt = str(body.get("prompt") or "").strip()
    raw = body.get("image") or body.get("images") or []
    if isinstance(raw, str):
        raw = [raw]
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            continue
        blob = item.split(",", 1)[1] if item.startswith("data:") else item
        try:
            images.append((f"dyva-{i}.png", base64.b64decode(blob)))
        except Exception:
            raise ValueError(f"image[{i}] is not valid base64")
    for k in ("width", "height", "steps", "cfg_scale", "seed", "sampler_name",
              "denoise", "match_source", "source_width", "source_height"):
        if k in body:
            params[k] = body[k]
    return prompt, images, str(body.get("model") or "").strip() or None, params


async def handle_edit_models(request):
    """
    List available image-edit models
    ---
    tags: [Image]
    summary: GET /v1/images/edit-models — edit-class models across discovered ComfyUI hosts
    description: |
      Edit models are a different population from SD checkpoints: these are the
      files the model classifier tags `edit` (Qwen-Image-Edit, Flux Kontext,
      Flux.2, Boogu, HunyuanImage, HiDream-O1/E1), served by ComfyUI hosts.
      Sorted by how many hosts carry each, commonest first.
    responses:
      '200':
        description: List of edit models
    """
    seen = {}
    for s in load_servers():
        if s.get("service") != "comfyui":
            continue
        host = s.get("server", "")
        for m in s.get("models", []):
            if classify_model(m) != "edit":
                continue
            # the file name is what a user types; the full path is host-specific
            name = m.replace("\\", "/").rsplit("/", 1)[-1]
            if not name:
                continue
            row = seen.setdefault(name, {"id": name, "hosts": [], "count": 0})
            row["count"] += 1
            if host and host not in row["hosts"]:
                row["hosts"].append(host)
    return web.json_response({
        "object": "list",
        "data": sorted(seen.values(), key=lambda x: (-x["count"], x["id"])),
    })


async def handle_image_edit(request):
    """
    Edit images with a prompt
    ---
    tags: [Image]
    summary: POST /v1/images/edits — prompt-driven image editing on ComfyUI hosts
    description: |
      Takes a prompt and **zero or more** reference images and races the job
      across discovered ComfyUI hosts running an edit model (Qwen-Image-Edit,
      Flux Kontext, Flux.2, ...). Distinct from `/sdapi/v1/txt2img`, which is
      classic text-to-image; with no images this still runs, as a plain
      generation on the edit model.

      Accepts OpenAI's `/v1/images/edits` multipart shape (`prompt`, repeated
      `image`, `model`) or a JSON body with base64 / `data:` URLs in `image`.
      Reference images are uploaded to the chosen host before the graph is
      submitted, because ComfyUI's LoadImage reads from the host's own input
      directory.
    responses:
      '200':
        description: OpenAI image response — {"created": ..., "data": [{"b64_json": ...}]}
      '400':
        description: Missing prompt or unreadable image
      '502':
        description: All hosts failed
      '503':
        description: No available hosts
    """
    resp = _check_local(request)
    if resp:
        return resp
    try:
        prompt, images, model_filter, params = await _edit_read_request(request)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception:
        return web.json_response({"error": "unreadable request body"}, status=400)
    if not prompt:
        return web.json_response({"error": "'prompt' is required"}, status=400)

    # Reputation is keyed by what was asked for, so a host written off for one
    # model isn't written off for another.
    ekey = edit_key(model_filter)
    hosts = _find_edit_hosts(request.query.get("host"), model_filter)
    if not hosts:
        return web.json_response({"error": "no ComfyUI hosts available"}, status=503)

    session = request.app["session"]
    snippet = prompt[:60] + ("..." if len(prompt) > 60 else "")
    label = f"edit: {snippet}"
    errors = []

    async def attempt(host, wid, done):
        await broadcast_activity(host, ekey, "trying",
            f"trying: {host} for {label}", wid=wid)
        t0 = time.time()
        try:
            info = await comfy_object_info(session, host)
            plan = _edit_plan(info, model_filter,
                              exclude=bad_models | bad_pairs_for(host, "edit"),
                              n_images=len(images))
            if not plan:
                raise ComfyUnsuitable("no edit model on this host"
                                      + (f" matching {model_filter!r}" if model_filter else ""))
            # Uploads are per-host, so they can only happen once a host is chosen.
            names = [await comfy_upload_image(session, host, data, fn)
                     for fn, data in images]
            workflow = _edit_workflow(plan, prompt, names, params)
            prompt_id = await comfy_submit(session, host, workflow)
        except (ComfyError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            err = str(e) or type(e).__name__
            errors.append(f"{host}: {err}")
            await broadcast_activity(host, ekey, "failed",
                f"failure: {host} for {label} - {err}",
                duration=time.time() - t0, wid=wid)
            if isinstance(e, ComfyUnsuitable):
                return unsuitable(err)
            if isinstance(e, ComfyUnreachable):
                return unreachable_host(err)
            if isinstance(e, asyncio.TimeoutError):
                return timed_out(err)
            if isinstance(e, (aiohttp.ClientError, OSError)):
                return unreachable_host(err)
            return failed(err)
        await broadcast_activity(host, ekey, "connected",
            f"success: {host} for {label}", duration=time.time() - t0, wid=wid,
            rmodel=plan["model"])
        return accepted((host, prompt_id, plan, workflow))

    # Submitting is where host *selection* ends, but a host can still turn out
    # to be no good once it starts rendering. When it does, that's a verdict
    # like any other: record it, drop the host, and hand the job to the next
    # one — rather than failing the whole request on one bad box.
    tried_hosts, bad_models, tried = set(), set(), 0
    tally = collections.Counter()
    stopped = False
    for _round in range(EDIT_RENDER_ATTEMPTS):
        pool = [h for h in hosts if h not in tried_hosts]
        if not pool:
            break
        result, stopped, n_tried, round_tally = await _race_hosts(
            pool, attempt, ekey, label=label)
        tried += n_tried
        tally.update(round_tally)
        if not result or stopped:
            break
        host, prompt_id, plan, workflow = result
        model_used = plan["model"]
        tried_hosts.add(host)
        try:
            async with _waiting_worker(label, host, "editing"):
                raw, _fn = await comfy_collect(session, host, prompt_id, COMFY_IMAGES,
                                               timeout=EDIT_RENDER_TIMEOUT,
                                               poll=2, view_timeout=90)
        except (ComfyError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            outcome = render_verdict(e)
            record_verdict(host, ekey, outcome)
            tally[outcome.verdict] += 1
            err = str(e) or type(e).__name__
            # Which files were paired is the first thing you need to know when
            # a render fails on shapes — the message alone can't tell you.
            rig = _plan_summary(plan)
            errors.append(f"{host}: {err} [{rig}]")
            # A shape mismatch says this *model* can't be driven the way we
            # drove it — the host may well have another that can, so retire the
            # model and let the host back into the pool rather than writing off
            # a box that might be fine.
            if outcome.verdict == V_UNSUITABLE and plan.get("model"):
                bad_models.add(plan["model"])
                tried_hosts.discard(host)
            log.warning(f"edit: {host}: took the job then failed to render: {err}\n"
                        f"  plan: {rig}\n"
                        f"  graph: {json.dumps(workflow)[:2000]}")
            await broadcast_activity(host, ekey, "failed",
                f"failure: {host} for {label} - render: {err} [{rig}]")
            continue

        b64 = base64.b64encode(raw).decode()
        stored = {"images": [b64], "_dyva_model": model_used}
        _save_image_history(stored, {"prompt": prompt}, host, model_used)
        return web.json_response({"created": int(time.time()),
                                  "data": [{"b64_json": b64}],
                                  "_dyva_files": stored.get("_dyva_files") or [],
                                  "model": model_used, "host": host})

    if stopped:
        return web.json_response({"error": "edit stopped"}, status=499)

    reasons = collections.Counter(e.split(": ", 1)[1] if ": " in e else e for e in errors)
    detail = "; ".join(f"{c}x {r}" for r, c in reasons.most_common()) or "no hosts tried"
    verdicts = ", ".join(f"{c} {v}" for v, c in tally.most_common())
    return web.json_response(
        {"error": f"edit failed on {tried} host{'' if tried == 1 else 's'} "
                  f"({verdicts}): {detail}"}, status=502)



# ---- Music generation (ACE-Step, MiniMax Music 3, and anything shaped like
# them) ---------------------------------------------------------------------
# Every text-to-music model takes the same three things: a style/caption, some
# optional lyrics, and a length. What differs between families is only the
# ComfyUI node class names and which input field carries each of those, so the
# graph is built once and the family table in node-classifier.json supplies the
# names. Field names are resolved against the host's live /object_info, and
# everything else is wired by declared TYPE, so a family we've never seen still
# works as long as its nodes are shaped like a diffusion audio pipeline.
MUSIC_KEY = "__music__"
MUSIC_JOBS_FILE = os.path.join(CACHE_DIR, "music-jobs.json")
MUSIC_JOBS_DIR = os.path.join(CACHE_DIR, "music-jobs")
_MUSIC_JOBS = {}
_MUSIC_JOB_ID_CTR = 0
MUSIC_JOB_MAX = 200
_music_families_cache = None

# candidate input names per slot, tried when the family table's name isn't on
# the node (or the family is the generic catch-all)
_MUSIC_SLOTS = {
    "style": ["tags", "caption", "prompt", "style", "description", "text"],
    "lyrics": ["lyrics", "lyric", "lyrics_text", "text"],
    "seconds": ["seconds", "max_duration", "duration", "length", "seconds_total"],
}


class _MusicError(Exception):
    pass


def load_music_families():
    """Compile the music section of node-classifier.json: node-name patterns
    plus which field carries style / lyrics / length for that family."""
    global _music_families_cache
    if _music_families_cache is not None:
        return _music_families_cache
    fams = []
    if os.path.exists(NODE_CLASSIFIER_FILE):
        try:
            with open(NODE_CLASSIFIER_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            log.warning(f"node-classifier: unreadable ({e})")
            raw = {}
        for spec in (raw.get("music") or []) if isinstance(raw, dict) else []:
            def _compile(key):
                out = []
                for p in spec.get(key) or []:
                    try:
                        out.append(re.compile(p))
                    except re.error as e:
                        log.warning(f"node-classifier: bad music regex {p!r}: {e}")
                return out
            enc, lat = _compile("encode"), _compile("latent")
            if not enc or not lat:
                continue
            try:
                ckpt = re.compile(spec.get("ckpt") or ".")
            except re.error:
                ckpt = re.compile(".")
            fams.append({"name": spec.get("name", "Generic"), "encode": enc, "latent": lat,
                         "ckpt": ckpt, "style": spec.get("style"),
                         "lyrics": spec.get("lyrics"), "seconds": spec.get("seconds")})
    _music_families_cache = fams
    return fams


def _first_class(info, regs):
    for cls in sorted(info or {}):
        if any(r.search(cls) for r in regs):
            return cls
    return None


def _music_family_on_host(info):
    """First family whose encode AND latent nodes are both installed."""
    for fam in load_music_families():
        enc = _first_class(info, fam["encode"])
        lat = _first_class(info, fam["latent"])
        if enc and lat:
            return fam, enc, lat
    return None, None, None


def _oi_required(info, cls):
    return (((info.get(cls) or {}).get("input") or {}).get("required")) or {}


def _oi_outputs(info, cls):
    return (info.get(cls) or {}).get("output") or []


def _pick_field(required, declared, slot, taken=()):
    if declared and declared in required and declared not in taken:
        return declared
    for name in _MUSIC_SLOTS[slot]:
        if name in required and name not in taken:
            return name
    return None


_LINK_TYPES = {"MODEL", "CLIP", "VAE", "CONDITIONING", "LATENT", "AUDIO", "CONTROL_NET"}


def _wire_node(info, cls, node_id, graph, links, overrides=None):
    """Add one node, filling connection inputs from `links` by declared type and
    everything else from the host's own declared defaults."""
    inputs = {}
    for name, entry in _oi_required(info, cls).items():
        if overrides and name in overrides:
            inputs[name] = overrides[name]
            continue
        typ = entry[0] if isinstance(entry, list) and entry else None
        if isinstance(typ, str) and typ in _LINK_TYPES:
            key = typ
            # the only ambiguity type-wiring can't resolve on its own
            if typ == "CONDITIONING" and re.search(r"neg", name, re.I):
                key = "CONDITIONING_NEG"
            if key in links:
                inputs[name] = links[key]
            continue
        attrs = entry[1] if isinstance(entry, list) and len(entry) > 1 and isinstance(entry[1], dict) else {}
        if "default" in attrs:
            inputs[name] = attrs["default"]
        elif isinstance(typ, list) and typ:
            inputs[name] = typ[0]
    graph[node_id] = {"class_type": cls, "inputs": inputs}
    return graph[node_id]


def _music_loader(info, fam, graph, links):
    """Wire whatever loads the model. Prefer a plain checkpoint whose filename
    matches the family (ACE-Step ships as one .safetensors); otherwise take a
    family-specific loader node, which is how the multi-file families load."""
    req = _oi_required(info, "CheckpointLoaderSimple")
    entry = req.get("ckpt_name")
    names = entry[0] if isinstance(entry, list) and isinstance(entry[0], list) else []
    for n in names:
        if fam["ckpt"].search(str(n)):
            graph["1"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": n}}
            for i, t in enumerate(_oi_outputs(info, "CheckpointLoaderSimple")):
                if t in _LINK_TYPES and t not in links:
                    links[t] = ["1", i]
            return n
    for cls in sorted(info or {}):
        outs = _oi_outputs(info, cls)
        if "MODEL" not in outs or not fam["ckpt"].search(cls):
            continue
        _wire_node(info, cls, "1", graph, links)
        for i, t in enumerate(outs):
            if t in _LINK_TYPES and t not in links:
                links[t] = ["1", i]
        return cls
    raise _MusicError(f"host has {fam['name']} nodes but no matching model to load")


def _build_music_workflow(info, fam, enc_cls, lat_cls, style, lyrics, seconds, seed, steps, cfg):
    graph, links = {}, {}
    model_ref = _music_loader(info, fam, graph, links)

    enc_req = _oi_required(info, enc_cls)
    f_style = _pick_field(enc_req, fam["style"], "style")
    f_lyrics = _pick_field(enc_req, fam["lyrics"], "lyrics", taken=(f_style,))
    if not f_style:
        raise _MusicError(f"{enc_cls} has no field to put the style description in")
    pos = {f_style: style}
    neg = {f_style: ""}
    if f_lyrics:
        pos[f_lyrics] = lyrics or ""
        neg[f_lyrics] = ""
    _wire_node(info, enc_cls, "2", graph, links, pos)
    _wire_node(info, enc_cls, "3", graph, links, neg)
    links["CONDITIONING"] = ["2", 0]
    links["CONDITIONING_NEG"] = ["3", 0]

    lat_req = _oi_required(info, lat_cls)
    f_secs = _pick_field(lat_req, fam["seconds"], "seconds")
    _wire_node(info, lat_cls, "4", graph, links, {f_secs: seconds} if f_secs else None)
    links["LATENT"] = ["4", 0]

    if "KSampler" not in (info or {}):
        raise _MusicError("host has no KSampler node")
    _wire_node(info, "KSampler", "5", graph, links,
               {"seed": seed, "steps": steps, "cfg": cfg, "denoise": 1.0})
    links["LATENT"] = ["5", 0]

    dec = "VAEDecodeAudio" if "VAEDecodeAudio" in (info or {}) else None
    if not dec:
        for cls in sorted(info or {}):
            if "AUDIO" in _oi_outputs(info, cls) and "LATENT" in [
                    (e[0] if isinstance(e, list) and e else None)
                    for e in _oi_required(info, cls).values()]:
                dec = cls
                break
    if not dec:
        raise _MusicError("host has no node that decodes latents to audio")
    _wire_node(info, dec, "6", graph, links)
    links["AUDIO"] = ["6", 0]

    save = next((c for c in ("SaveAudio", "SaveAudioMP3", "SaveAudioOpus", "SaveAudioAdvanced")
                 if c in (info or {})), None)
    if not save:
        raise _MusicError("host has no SaveAudio node")
    _wire_node(info, save, "7", graph, links, {"filename_prefix": "dyva/music"})
    return graph, model_ref


def _find_music_hosts(target_host=None):
    """ComfyUI hosts carrying an audio/music-class model. Whether they can
    actually generate music is decided by probing /object_info at submit time —
    a model class alone never implies capability."""
    if target_host:
        return [target_host] if _host_has_class(target_host, _AUDIO_CLASSES) else []
    hosts = [s.get("server") for s in load_servers()
             if s.get("service") == "comfyui" and s.get("server")
             and _host_has_class(s.get("server"), _AUDIO_CLASSES)]
    bad, good = load_bad(), load_good()
    hosts.sort(key=lambda h: 0 if f"{h} {MUSIC_KEY}" in good else (1 if f"{h} {MUSIC_KEY}" not in bad else 2))
    last = get_last(MUSIC_KEY)
    if last and last[0]:
        hosts = [last[0]] + [h for h in hosts if h != last[0]]
    return _idle_first(hosts)


def _music_job_save():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        clean = {k: {kk: vv for kk, vv in v.items() if not isinstance(vv, (bytes, bytearray))}
                 for k, v in _MUSIC_JOBS.items() if isinstance(v, dict)}
        with open(MUSIC_JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f)
    except Exception:
        pass


def _load_music_jobs():
    global _MUSIC_JOBS, _MUSIC_JOB_ID_CTR
    if _MUSIC_JOBS:
        return
    if os.path.exists(MUSIC_JOBS_FILE):
        try:
            with open(MUSIC_JOBS_FILE, encoding="utf-8") as f:
                _MUSIC_JOBS = json.load(f)
            for k in _MUSIC_JOBS:
                m = re.match(r"m(\d+)$", str(k))
                if m:
                    _MUSIC_JOB_ID_CTR = max(_MUSIC_JOB_ID_CTR, int(m.group(1)))
        except Exception:
            _MUSIC_JOBS = {}
    # a job left running when the process died can never finish
    for k, v in list(_MUSIC_JOBS.items()):
        if isinstance(v, dict) and v.get("status") in ("pending", "in_progress"):
            v["status"] = "failed"
            v["error"] = "interrupted by a restart"


def _music_job_new(job):
    global _MUSIC_JOB_ID_CTR
    _load_music_jobs()
    _MUSIC_JOB_ID_CTR += 1
    jid = f"m{_MUSIC_JOB_ID_CTR}"
    _MUSIC_JOBS[jid] = job
    if len(_MUSIC_JOBS) > MUSIC_JOB_MAX:
        for k in sorted(_MUSIC_JOBS, key=lambda k: _MUSIC_JOBS[k].get("created") or 0)[:len(_MUSIC_JOBS) - MUSIC_JOB_MAX]:
            cp = (_MUSIC_JOBS[k] or {}).get("content_path")
            if cp:
                try:
                    os.remove(cp)
                except OSError:
                    pass
            del _MUSIC_JOBS[k]
    _music_job_save()
    return jid


async def _poll_comfy_audio(session, host, prompt_id, timeout=900):
    """Poll /history until the graph saves audio; return (bytes, filename)."""
    try:
        return await comfy_collect(session, host, prompt_id, COMFY_AUDIO,
                                   timeout=timeout, poll=3, view_timeout=120)
    except ComfyError as e:
        raise _MusicError(str(e))


async def _run_music_job(session, jid):
    job = _MUSIC_JOBS.get(jid)
    if not job:
        return
    job["status"] = "in_progress"
    _music_job_save()
    p = job.get("params") or {}
    label = f"music: {job.get('style', '')[:40]}"
    t0 = time.time()
    host = None
    errors = []
    try:
        for cand in job.get("hosts") or []:
            info = await _get_object_info(session, cand)
            if not info:
                errors.append(f"{cand}: unreachable")
                continue
            fam, enc, lat = _music_family_on_host(info)
            if not fam:
                errors.append(f"{cand}: no known music nodes")
                continue
            try:
                graph, model_ref = _build_music_workflow(
                    info, fam, enc, lat, job["style"], job.get("lyrics") or "",
                    job["seconds"], job["seed"], int(p.get("steps", 50)), float(p.get("cfg", 5.0)))
            except _MusicError as e:
                log.warning(f"music build: {cand}: {e}")
                errors.append(f"{cand}: {e}")
                continue
            host = cand
            job["host"] = host
            job["family"] = fam["name"]
            job["model"] = model_ref
            _music_job_save()
            await broadcast_activity(host, label, "trying", label)
            try:
                pid = await comfy_submit(session, host, graph)
            except ComfyError as e:
                # The reason the host said no is worth keeping — "rejected the
                # graph" hid whether the nodes were missing or it was just down.
                errors.append(f"{host}: {e}")
                host = None
                continue
            job["comfyui_prompt_id"] = pid
            _music_job_save()
            async with _waiting_worker(label, host, "composing"):
                content, filename = await _poll_comfy_audio(session, host, pid)
            os.makedirs(MUSIC_JOBS_DIR, exist_ok=True)
            content_path = os.path.join(MUSIC_JOBS_DIR, f"{jid}.bin")
            with open(content_path, "wb") as f:
                f.write(content)
            job["content_path"] = content_path
            job["filename"] = filename
            job["status"] = "completed"
            job["unsigned_urls"] = [f"/v1/music/{jid}/content"]
            add_good(host, MUSIC_KEY)
            set_last(MUSIC_KEY, host, model_ref)
            await broadcast_activity(host, label, "done", label, duration=time.time() - t0)
            _music_job_save()
            return
        raise _MusicError("; ".join(errors) if errors else "no music-capable host answered")
    except (_MusicError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
        job["status"] = "failed"
        job["error"] = str(e) or type(e).__name__
        if host:
            log.warning(f"music: {host}: {job['error']}")
            add_bad(host, MUSIC_KEY)
            await broadcast_activity(host, label, "failed", f"{label}: {job['error']}", duration=time.time() - t0)
        _music_job_save()


async def handle_music_post(request):
    """
    Submit a music generation job
    ---
    tags: [Music]
    summary: POST /v1/music — start an async text-to-music job on a ComfyUI host
    description: |
      One shape for every music model: a style description, optional lyrics, and
      a length. Backed by ComfyUI hosts running ACE-Step, MiniMax Music 3, or any
      family listed in node-classifier.json. Returns an id immediately; poll
      GET /v1/music/{id} until status is `completed`, then fetch
      GET /v1/music/{id}/content.
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              prompt:
                type: string
                description: Style/caption — genre, mood, BPM, instrumentation, vocal
              lyrics:
                type: string
                description: Optional lyrics, section tags like [Verse]/[Chorus] allowed
              duration:
                type: number
                description: Seconds of audio (default 120)
              model:
                type: string
                description: Optional model filter
              seed:
                type: integer
              steps:
                type: integer
              cfg:
                type: number
    responses:
      '202':
        description: Job submitted
      '400':
        description: Missing prompt
      '503':
        description: No music-capable hosts
    """
    resp = _check_local(request)
    if resp:
        return resp
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    style = str(body.get("prompt") or body.get("style") or "").strip()
    lyrics = str(body.get("lyrics") or "").strip()
    if not style and not lyrics:
        return web.json_response({"error": "'prompt' (a style description) is required"}, status=400)
    hosts = _find_music_hosts(request.query.get("host"))
    if not hosts:
        return web.json_response({"error": "no ComfyUI hosts with an audio-capable model available"}, status=503)
    try:
        seconds = float(body.get("duration") or 120)
    except (TypeError, ValueError):
        seconds = 120.0
    seconds = max(1.0, min(seconds, 300.0))
    seed = body.get("seed")
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1
    if seed < 0:
        import random
        seed = random.randint(0, 2 ** 31 - 1)
    model_filter = str(body.get("model") or "")
    if model_filter:
        hosts = [h for h in hosts if _pick_music_model(h, model_filter)] or hosts
    job = {"id": None, "status": "pending", "hosts": hosts[:8], "host": None,
           "model": None, "family": None, "style": style, "lyrics": lyrics,
           "seconds": seconds, "seed": seed, "params": dict(body),
           "created": time.time(), "unsigned_urls": []}
    jid = _music_job_new(job)
    job["id"] = jid
    job["polling_url"] = f"/v1/music/{jid}"
    _music_job_save()
    asyncio.get_event_loop().create_task(_run_music_job(request.app["session"], jid))
    return web.json_response({"id": jid, "polling_url": job["polling_url"], "status": "pending"}, status=202)


def _pick_music_model(host, model_filter=None):
    for s in load_servers():
        if s.get("server") != host:
            continue
        for m in s.get("models") or []:
            if classify_model(m) not in _AUDIO_CLASSES:
                continue
            if model_filter and not match_model(m, model_filter):
                continue
            return m
    return None


async def handle_music_get(request):
    """
    Poll a music generation job
    ---
    tags: [Music]
    summary: GET /v1/music/{id} — poll a music job until completed
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
    _load_music_jobs()
    job = _MUSIC_JOBS.get(jid)
    if not job:
        return web.json_response({"error": "unknown job"}, status=404)
    out = {"id": jid, "status": job["status"], "model": job.get("model"),
           "family": job.get("family"), "host": _norm_host(job.get("host") or ""),
           "polling_url": f"/v1/music/{jid}"}
    if job["status"] == "completed":
        out["unsigned_urls"] = job.get("unsigned_urls") or []
    if job["status"] == "failed":
        out["error"] = job.get("error")
    return web.json_response(out)


async def handle_music_content(request):
    """
    Download finished music
    ---
    tags: [Music]
    summary: GET /v1/music/{id}/content — stream the generated audio
    parameters:
      - in: path
        name: id
        schema:
          type: string
        required: true
    responses:
      '200':
        description: Audio bytes
      '404':
        description: Unknown job, or not finished
    """
    resp = _check_local(request)
    if resp:
        return resp
    jid = request.match_info.get("id")
    _load_music_jobs()
    job = _MUSIC_JOBS.get(jid)
    if not job:
        return web.json_response({"error": "unknown job"}, status=404)
    if job.get("status") != "completed":
        return web.json_response({"error": f"job is {job.get('status')}"}, status=404)
    path = job.get("content_path")
    if not path or not os.path.exists(path):
        return web.json_response({"error": "audio no longer on disk"}, status=404)
    name = job.get("filename") or f"{jid}.flac"
    ctype = _TTS_MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
    with open(path, "rb") as f:
        return web.Response(body=f.read(), content_type=ctype,
                            headers={"Content-Disposition": f'inline; filename="{name}"'})


async def handle_music_models(request):
    """
    List music-capable hosts and the families they run
    ---
    tags: [Music]
    summary: GET /v1/music/models — probe audio hosts for known music node families
    responses:
      '200':
        description: Per-host music capability
    """
    resp = _check_local(request)
    if resp:
        return resp
    session = request.app["session"]
    hosts = _find_music_hosts(request.query.get("host"))
    if not hosts:
        return web.json_response({"hosts": [], "families": []})

    async def probe(host):
        info = await _get_object_info(session, host)
        if not info:
            return None
        fam, enc, lat = _music_family_on_host(info)
        if not fam:
            return None
        return {"host": _norm_host(host), "family": fam["name"],
                "encode_node": enc, "latent_node": lat,
                "models": [m for m in (_host_models(host) or [])
                           if classify_model(m) in _AUDIO_CLASSES]}

    found = [r for r in await asyncio.gather(*(probe(h) for h in hosts[:12]),
                                             return_exceptions=True) if isinstance(r, dict)]
    return web.json_response({"hosts": found,
                              "families": sorted({f["family"] for f in found})})


def _host_models(host):
    for s in load_servers():
        if s.get("server") == host:
            return s.get("models") or []
    return []


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
    swagger.add_get("/workers", handle_workers)
    swagger.add_get("/workers-now", handle_workers_now)
    app.router.add_get("/workers-ws", handle_workers_ws)
    swagger.add_get("/stop-worker", handle_stop_worker)
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
    # plain router: multipart bodies aren't swagger-validatable
    app.router.add_post("/v1/images/edits", handle_image_edit)
    swagger.add_get("/v1/images/edit-models", handle_edit_models)
    swagger.add_get("/v1/audio/voices", handle_audio_voices)
    app.router.add_get("/v1/audio/clips/{name}", handle_audio_clip)
    swagger.add_get("/v1/music/models", handle_music_models)
    # Registered on the plain router (not swagger) so path params and the
    # sendBeacon POST body aren't subject to swagger request validation.
    app.router.add_post("/v1/web/fetch", handle_web_fetch)
    app.router.add_get("/api/chats", handle_chats_get)
    app.router.add_get("/api/chats/{cid}", handle_chat_get)
    app.router.add_post("/api/chats/{cid}", handle_chat_post)
    app.router.add_post("/api/chats/{cid}/delete", handle_chat_delete)

    app.router.add_route("*", "/comfyui/{tail:.*}", handle_comfyui_proxy)
    app.router.add_post("/v1/videos", handle_videos_post)
    app.router.add_get("/v1/videos", handle_videos_list)
    app.router.add_get("/v1/videos/models", handle_video_models)
    app.router.add_get("/v1/videos/{id}/delete", handle_videos_delete)
    app.router.add_get("/v1/videos/{id}", handle_videos_get)
    app.router.add_get("/v1/videos/{id}/content", handle_videos_content)
    app.router.add_post("/v1/music", handle_music_post)
    app.router.add_get("/v1/music/{id}", handle_music_get)
    app.router.add_get("/v1/music/{id}/content", handle_music_content)

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
 '' ''               dibatag               '' ''
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
    parser.add_argument("--hosts", nargs="*", metavar="ARG",
                        help="inspect or prune the host reputation table; arguments narrow left to "
                             "right and the verb comes last: '--hosts' for a summary, '--hosts bad' "
                             "for the keys marked bad, '--hosts bad __tts__' for the hosts carrying "
                             "that mark, and '--hosts bad __tts__ del' to clear them")
    parser.add_argument("--curlify", action="store_true", help="print curl commands of upstream requests to stderr")
    parser.add_argument("-v", "--version",  action="store_true", help="show version information")
    args = parser.parse_args()

    if args.source:
        sys.exit(source_cli(args.source))

    if args.hosts is not None:
        sys.exit(hosts_cli(args.hosts))

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
            access_log_class=QuietAccessLogger,
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
