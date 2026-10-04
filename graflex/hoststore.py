"""SQLite store for graflex's per-service host records.

One row per (service, host); `status` is 'working' | 'notworking' | NULL (discovered,
unchecked); `payload` is the exact JSON record the <service>-*.json files hold. The JSON
files are regenerated from this table (see export_files); the table is the working store,
so a per-host write is an O(1) upsert instead of a whole-file load-mutate-rewrite.

Keyed on (service, host) — NOT host alone: the same host:port legitimately appears under
two services (e.g. gradio+ollama, ollama+vllm), and a host-only key would merge them.
"""
import glob as _glob
import json
import logging as _logging
import os as _os
import sqlite3

_log = _logging.getLogger("graflex")

SCHEMA = """
CREATE TABLE IF NOT EXISTS host (
  service TEXT NOT NULL,
  host    TEXT NOT NULL,
  status  TEXT,
  payload TEXT NOT NULL,
  checked TEXT,
  PRIMARY KEY (service, host)
);
CREATE INDEX IF NOT EXISTS host_svc_status ON host(service, status);
"""

_UNSET = object()


def connect(path):
    """Open (creating if needed) the host DB in WAL mode with a busy timeout so a
    concurrent writer (a second scan, or dyva) waits briefly instead of erroring."""
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert(conn, service, host, status, payload, checked):
    """Insert or replace one (service, host) row. O(1); the whole file is never rewritten."""
    conn.execute(
        "INSERT INTO host(service, host, status, payload, checked) VALUES(?,?,?,?,?) "
        "ON CONFLICT(service, host) DO UPDATE SET "
        "status=excluded.status, payload=excluded.payload, checked=excluded.checked",
        (service, host, status, json.dumps(payload, ensure_ascii=False), checked),
    )
    conn.commit()


def discover(conn, service, host, payload):
    """Record a DISCOVERED host (status NULL) without ever downgrading one already
    checked: INSERT OR IGNORE, so a host present as working/notworking keeps its status."""
    conn.execute(
        "INSERT OR IGNORE INTO host(service, host, status, payload, checked) VALUES(?,?,?,?,?)",
        (service, host, None, json.dumps(payload, ensure_ascii=False), None))
    conn.commit()


def rows(conn, service, status=_UNSET):
    """(host, payload_dict, checked) for a service. status=_UNSET -> all rows;
    status=None -> only NULL-status (discovered) rows; a string -> that status."""
    if status is _UNSET:
        cur = conn.execute("SELECT host, payload, checked FROM host WHERE service=?", (service,))
    elif status is None:
        cur = conn.execute("SELECT host, payload, checked FROM host WHERE service=? AND status IS NULL", (service,))
    else:
        cur = conn.execute("SELECT host, payload, checked FROM host WHERE service=? AND status=?", (service, status))
    return [(h, json.loads(p), c) for h, p, c in cur.fetchall()]


def services(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT service FROM host ORDER BY service").fetchall()]


def export_files(conn, save_fn, cache_file_fn):
    """Regenerate the per-service JSON files from the DB, in the exact shapes consumers
    expect: working = list (sorted by checked,host); notworking = dict keyed by host;
    hosts = list of {service, host}. `save_fn(path, data)` / `cache_file_fn(service, suffix)`
    are injected so the real caller passes graflex's _save_json_atomic (keeping the LOBOTOMY
    guard) and _cache_file. Returns the paths written."""
    written = []
    for svc in services(conn):
        work = [p for _h, p, _c in sorted(rows(conn, svc, "working"), key=lambda r: (r[2] or "", r[0]))]
        save_fn(cache_file_fn(svc, "working"), work)
        notwork = {h: p for h, p, _c in rows(conn, svc, "notworking")}
        save_fn(cache_file_fn(svc, "notworking"), notwork)
        hosts = [{"service": svc, "host": h} for h, _p, _c in rows(conn, svc)]
        save_fn(cache_file_fn(svc, "hosts"), hosts)
        written += [cache_file_fn(svc, s) for s in ("working", "notworking", "hosts")]
    return written


def _import_one(conn, service, status, record, host):
    if not host:
        return 0
    payload = json.dumps(record, ensure_ascii=False)
    checked = record.get("checked") if isinstance(record, dict) else None
    if status is None:
        # discovery row: never overwrite an existing working/notworking status
        conn.execute(
            "INSERT OR IGNORE INTO host(service, host, status, payload, checked) VALUES(?,?,?,?,?)",
            (service, host, None, payload, checked))
    else:
        conn.execute(
            "INSERT INTO host(service, host, status, payload, checked) VALUES(?,?,?,?,?) "
            "ON CONFLICT(service, host) DO UPDATE SET status=excluded.status, payload=excluded.payload, checked=excluded.checked",
            (service, host, status, payload, checked))
    return 1


def import_files(conn, cache_dir, load_fn, entry_host_fn):
    """One-time load of the existing <service>-{working,notworking,hosts}.json into the DB.
    Order matters: working then notworking (real status + full payload), then hosts via
    INSERT OR IGNORE so a discovery row never downgrades a host already checked. Bad
    records are logged and skipped, never fatal. Returns the count imported."""
    n = 0
    for suffix, status in (("working", "working"), ("notworking", "notworking"), ("hosts", None)):
        for path in sorted(_glob.glob(_os.path.join(cache_dir, f"*-{suffix}.json"))):
            service = _os.path.basename(path)[: -len(f"-{suffix}.json")]
            try:
                data = load_fn(path, silent=True)
            except Exception as e:
                _log.warning(f"hoststore import: skip {path}: {e}")
                continue
            items = data.values() if isinstance(data, dict) else data
            for rec in items:
                if not isinstance(rec, dict):
                    continue
                try:
                    n += _import_one(conn, service, status, rec, entry_host_fn(rec))
                except Exception as e:
                    _log.warning(f"hoststore import: skip record in {path}: {e}")
    conn.commit()
    return n
