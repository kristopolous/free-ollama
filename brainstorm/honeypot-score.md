# Honeypot score → machine-level flag — design

Judge the MACHINE, not the model: once a host accumulates enough honeypot
evidence, flag the whole host so `find_servers` drops it for every model — instead
of blacklisting one probed fake name and leaving its other 39 fakes eligible.

## Hard invariants (these are why we're writing this down first)

1. **Additive only. Nothing is deleted, reduced, collapsed, or overwritten.**
   - Signals are APPENDED to a log. The score is DERIVED (summed) on read.
   - The host flag is a NEW soft marker layered on top. It never touches the
     per-`(host,model)` `host_status` rows, their smoke history, or `bogus_score`.
2. **Reversible.** The flag can be cleared/changed (a score is mutable); the
   underlying evidence log stays. "We can change the flag; we cannot undo data
   destruction," so we only ever do the reversible thing.
3. **Provenance kept.** Every point in the score traces to a recorded signal:
   which host, which model, which probe, what the machine actually answered, when.
4. **Schema changes are ADDITIVE** (new table / new sentinel row), never a rewrite
   of `host_status`. Validated on a COPY before any live run.

## Inputs to the per-host score

- **graflex `bogus_score`** (already exists): additive per-host score from confirmed
  honeypot CLONE fingerprints (`bogus-fingerprints.json` — a cloned honeypot image
  reuses the same `(model, ns-timestamp)` across hosts). Survey-time, static, carried
  into dyva on the host record. READ-ONLY input here.
- **runtime honeypot signals** (new, what dyva sees live):
  - canned / parroted smoke answer — the "full tool access" / "paste the dataset"
    class (STRONG).
  - a distinct model on the host failing smoke (accumulating: one is a fluke, five
    is a catalog of fakes).
  - (later, optional) size/digest anomalies we flagged in the bogus investigation.
- **explicitly NOT scored:** `401` auth-required (a pass — leave alone), and
  transient failures (timeout / 503 / "loading model"). These never add points.

## Storage (all additive)

- **Signal log — append-only, provenance.** Reuse the existing "we tested it" tier
  (`host-probe.json`, `{host:[{date,model,test,capability,result}]}`) or a sibling
  `honeypot-signals` store: append `{date, model, signal:"canned", answer:<raw>}`.
  Never rewritten.
- **Derived score.** `honeypot_score(host) = bogus_score + Σ(signal weights)`,
  computed on read from the log. Not persisted as a mutating counter (or if cached
  for speed, the log stays the source of truth).
- **The flag.** A host-wide sentinel row in `host_status`, same pattern as the
  existing `UNREACHABLE_KEY` mark (`find_servers` already excludes hosts carrying a
  host-wide sentinel). Proposed: a `HONEYPOT_KEY = "\x00honeypot"` sentinel row.
  Setting it excludes the machine for ALL models; clearing it un-flags — both
  reversible, neither destructive.

## Flow

1. Smoke probe returns a canned/parrot answer (the existing conclusive-fail path).
2. APPEND the signal to the log (with the raw answer = provenance). The existing
   per-`(host,model)` `force_bad` + `mark_smoke` still happen, unchanged.
3. Recompute `honeypot_score(host)` from the log + `bogus_score`.
4. If it crosses THRESHOLD and the host isn't already flagged, write the
   `HONEYPOT_KEY` sentinel row (additive). Provenance is already in the log.
5. `find_servers` drops the machine for every model. Dashboard can show the score
   + its provenance on the host card (read-only).

## Open knobs (need your call before coding the numbers)

- **Weights:** canned answer = ? , each extra smoke-failing model = ? , fingerprint
  `bogus_score` counts at face value?
- **Threshold:** my lean — a lone canned answer should NOT condemn a machine by
  itself; needs one corroborator (another model canned, or a nonzero `bogus_score`).
  So e.g. canned = 1, threshold = 2. Tune freely.
- **`bogus_score` in the same total, or a separate shown signal?**

## What I will NOT do

- No `DELETE` / bulk update of `host_status`.
- No folding/merging of rows.
- No touching backups.
- No live-DB run until this design + the weights are approved; destructive-capable
  steps validated on a copy only.
