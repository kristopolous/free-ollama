# graflex host store: SQLite behind the per-service JSON files

**Date:** 2026-10-04
**Status:** design, pending review

## Problem

graflex persists discovered/checked hosts as per-service JSON files in
`~/.cache/free-ollama/`: `<service>-working.json`, `<service>-notworking.json`,
`<service>-hosts.json` for each of `ollama, a1111, comfyui, gradio, llama.cpp,
lmstudio, vllm, fooocus, sillytavern` (17 files today).

Every per-host result is written by **load whole file → mutate → rewrite whole file**:

- `working` is a JSON **list**, so writing one host does a linear scan to find it
  (`for i,w in enumerate(working): if _entry_host(w)==key`) *and* rewrites the list.
- `notworking` is a dict (O(1) lookup) but still rewrites the whole file — **32 MB /
  75,935 entries** for ollama — on every host.

Result: `check` is O(N²) and crawls for hours on a large pool (the reported hang), and
**two concurrent scans clobber each other** — each does a whole-file read-modify-write, so
the later writer overwrites the earlier scan's hosts (silent lost updates). The same
whole-file model forces multi-MB files into memory (the ~250–300 MB footprint seen
elsewhere). Memory footprint, lost data, atomic-write gymnastics, and I/O performance are
all the **one** root cause: mutable state stored as monolithic JSON blobs.

## Goal / success criteria

1. Per-host writes are O(1), transactional, and durable.
2. Two scans writing + dyva reading run concurrently with no clobbering or corruption.
3. The per-service JSON files are still produced, **byte-shape identical** to today, so
   every current consumer (dyva, anything reading the files) is unchanged.

## Non-goals (explicit)

- `/tmp/graflex` session snapshots + `failed.json` (resume) — **untouched**. Resume keeps
  working exactly as it does; this change is only the cumulative working/notworking/hosts
  store.
- dyva's own stores (`host-status.db`, `known-hosts.json`, `free-ollama.json`) — untouched.
- No column decomposition / analytics schema: the `payload` stays the current JSON record
  **verbatim**. dyva re-derives everything it needs from it.
- Not Redis/Dragonfly. SQLite (local, WAL) covers the real concurrency here (one box: a
  scan writing while dyva reads, or two scans at once). A networked KV is only warranted if
  multiple *machines* ever write one shared DB over a network mount — not the case for a
  local `~/.cache` store. Revisit then, not now.

## Data model

One SQLite DB at `~/.cache/free-ollama/graflex.db`, opened **WAL + `busy_timeout`**.

One table:

```sql
CREATE TABLE host (
  service TEXT NOT NULL,      -- 'ollama', 'comfyui', ...
  host    TEXT NOT NULL,      -- 'ip:port' or 'name:port'
  status  TEXT,              -- 'working' | 'notworking' | NULL (discovered, not yet checked)
  payload TEXT NOT NULL,      -- the current JSON record, serialized verbatim
  checked TEXT,              -- ISO timestamp, mirrors payload.checked (export ordering)
  PRIMARY KEY (service, host)
);
CREATE INDEX host_svc_status ON host(service, status);
```

**Composite key `(service, host)` is required**, not host alone: 1,302 host:ports
legitimately appear under two services (same box recorded as `gradio`+`ollama`,
`ollama`+`vllm`, …). A host-only key would merge those and lose the fidelity we explicitly
want. `status` is the single column that distinguishes the three files; `NULL` = a host
that's been discovered (in `hosts`) but not yet checked.

## Components

1. **Store module** (new, thin, in graflex): `open()` (WAL + busy_timeout, create table if
   absent), `upsert(service, host, status, payload, checked)`, `iter(service, status=…)`,
   `services()`, `counts()`. One clear job: row CRUD keyed by `(service, host)`. No business
   logic.

2. **Write path** — replace the per-host whole-file load/mutate/save in `_check_hosts`
   (and `check_batch`, and the fetch/discovery path that writes `hosts`) with a single
   `INSERT … ON CONFLICT(service, host) DO UPDATE` per host:
   - working host → `status='working'`, payload = the probe result record (as built today).
   - notworking / honeypot → `status='notworking'`, payload = the current notworking record.
   - discovered (pre-check, from a fetch) → `status=NULL`, payload = `{service, host}`.
   Each upsert is its own transaction; `busy_timeout` makes a concurrent scan's write wait a
   few ms rather than error.

3. **Export** — regenerate every file, per service, in the **exact current shapes**:
   - `<svc>-working.json` = **list** of payloads where `status='working'`, sorted
     `(checked, host)` (matches today).
   - `<svc>-notworking.json` = **dict** `{host: payload}` where `status='notworking'`
     (matches today).
   - `<svc>-hosts.json` = **list** of `{service, host}` for all rows of that service.
   Written atomically (temp + rename), as today. Triggered at the end of a `check` run
   (no `graflex.sh` change) and available as a standalone export entrypoint.

4. **One-time import** — populate the table from the existing 17 `*-working/notworking/
   hosts.json`. Run once when the table is empty (or via an explicit `import` entrypoint).
   Per-record parse failures are logged and skipped, never fatal (a bad legacy record must
   not cull the rest).

## Concurrency / durability

- WAL: many readers + one writer; writers serialize via `busy_timeout`, each upsert is
  sub-millisecond so two scans barely contend.
- Crash safety: a killed scan leaves a consistent DB (ACID); re-running upserts is
  idempotent. No half-written-file corruption, ever.
- dyva reads the **exported files**, so it isn't a DB client; WAL covers any future direct
  reader.

## Integration with graflex.sh (do NOT modify graflex.sh)

`graflex.sh` is the orchestration reference and stays untouched. Export is hooked into the
end of the existing `check` action at the Python level, so a normal `-a check` run produces
up-to-date JSON with no script change. A standalone `export` (and `import`) Python
entrypoint is added for manual/one-off use; if a dedicated `-a export` action is wanted
later, that's a one-line addition the user makes to their own `graflex.sh`.

## Error handling

- DB open/transaction failure is loud (the store is the source of truth) — no silent
  fallback to the old whole-file path.
- Import skips and logs unparseable legacy records rather than aborting.
- Export is regenerable from the DB at any time.

## Testing

- **Unit:** upsert idempotency; `(service, host)` uniqueness including a host under two
  services; `status` transitions (NULL→working, NULL→notworking, working↔notworking);
  export shapes match the current files exactly (round-trip a sample).
- **Migration fidelity:** import the existing files → export → diff against the originals
  (modulo list ordering) to prove byte-shape fidelity for each service.
- **Concurrency:** two writers under `busy_timeout` both land their rows (no lost updates);
  a reader during writes sees a consistent snapshot.

## Decisions (resolved; flag on review if you disagree)

- **Export cadence:** at the **end of a `check` run** plus a **periodic flush** (~every
  15 s, the same cadence as the interim fix) so a long survey's partial results reach dyva
  without waiting for the whole run. The flush is a cheap `SELECT`-and-dump; it doesn't
  touch the write path.
- **Discovery rows:** the fetch/discovery path writes `status=NULL` rows **eagerly**, so
  `<svc>-hosts.json` stays a faithful "everything discovered" list and a later check just
  flips the same row's `status`. (The alternative — materializing `hosts` only at export —
  loses the discovered-but-unchecked set if a run is interrupted before check.)
- **DB location:** `~/.cache/free-ollama/graflex.db`, one DB for all services (the table's
  `service` column separates them). Not one DB per service — a single file is simpler to
  back up and lets a cross-service query (the 1,302 dual-service hosts) stay trivial.
