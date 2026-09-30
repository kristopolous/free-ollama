# brainstorm/ — design notes index

Working notes for features that are **designed or half-built but not finished**. Each
file is a thinking-out-loud document, not a spec or a commitment; several record
decisions that were later revised in place, so trust the newest line in a file over
an older one. This README is a survey of what's here and where each idea stands.

## Status at a glance

| doc | status | one line |
|---|---|---|
| [honeypot-detection.txt](honeypot-detection.txt) | Signal A **built**, Signal B parked | detect cloned-honeypot hosts (fingerprint + behavioural) |
| [honeypot-score.md](honeypot-score.md) | flag **built**, scoring **designed** | escalate honeypot evidence to a whole-machine flag |
| [canary-detection.md](canary-detection.md) | idea (data-collection only) | find hosts whose connect trips a Cloudflare soft-ban |
| [gossiper.md](gossiper.md) | **parked** (`-c/--config` done) | dyva↔dyva sync of fresh good-host status |
| [graflex-sample.md](graflex-sample.md) | planned, **lower priority** | a publishable rotating "sample platter" of hosts |
| [query-syntax.md](query-syntax.md) | notes | a `;facet` filter grammar unifying the query languages |
| [host-crossref.md](host-crossref.md) | **built** (`crossref.py`) | find new sources by reverse-looking-up our hosts / honeypot names on GitHub |

Status legend: **built** = in the code now · **designed** = written down, ready to
build, not built · **parked** = deliberately on hold with a named trigger to revisit
· **idea** = early thinking, explicitly not being built.

---

## Theme 1 — keeping the tool in the clear (safety / not touching bad hosts)

The survey touches machines other people left open. Two hazards: **honeypots**
(fake endpoints that bait or fingerprint us) and **canaries** (hosts whose mere TCP
connect gets our egress IP soft-banned by Cloudflare/OVH). These notes are about
*detecting and avoiding* both — and about the governing stance that the project stays
"in the clear" by not being loud and not distributing hosts.

### [honeypot-detection.txt](honeypot-detection.txt) — Signal A built, Signal B parked
Detecting cloned-honeypot hosts. The core insight is **constructive symmetry**: any
behavioural question you build to catch a fake, the adversary builds a machine to
answer — so behavioural probes are a losing arms race. The escape is *information
destruction* and *negative testing* (assert bogus, never assert good).
- **Signal A — timestamp fingerprint (BUILT as graflex `score-bogus`)**: a cloned
  disk image reuses the same `(model, nanosecond-timestamp)` across many hosts; a real
  host pulls independently. Validated — 6 fingerprints each shared by 209–243 of 688
  hosts. Consumed by routing as a *deprioritize*, never a ban. This is the only
  structural signal with teeth.
- **Signal B — nonsense→canned negative test (PARKED)**: send dictionary-word
  nonsense; flag only *canned/invariant* replies as bogus. A tripwire for lazy
  fixed-canned fakes, easily beaten by any varied-text generator — so it stays parked;
  build only if a fingerprint-0 host keeps returning canned soup (not observed yet).
- Open thread: per-host vs per-`(host,model)` — a host can serve a fake `codellama`
  *and* a real `llama3`, so the fingerprint flags per-`(host,model)` and behaviour
  arbitrates; host-level is at most a soft tiebreaker.

### [honeypot-score.md](honeypot-score.md) — flag built, scoring designed
Judge the *machine*, not the model: enough honeypot evidence → flag the whole host so
`find_servers` drops it for every model. The **host-wide flag mechanism exists**
(the `HONEYPOT_KEY` sentinel row + `mark_honeypot`, same pattern as `UNREACHABLE_KEY`,
additive and reversible). What's still **design** is the *accumulated score*:
`honeypot_score = bogus_score + Σ(runtime signal weights)`, derived from an
append-only signal log, crossing a threshold to set the flag. Hard invariants:
additive-only, reversible, provenance kept, schema changes additive, validated on a
copy before any live run. Open: the weights and threshold.

### [canary-detection.md](canary-detection.md) — idea, data-collection only
Some hosts are canaries: *connecting* to them (the soft-ban fires on the TCP connect,
not a completed request) drops our egress IP into a temporary Cloudflare/OVH ban pool.
Plan: log every touch with a timestamp, periodically probe a CF-fronted site for our
block state, and later correlate block events back to the hosts touched just before.
Per-egress-IP (graflex and dyva are separate machines). **Scope is deliberately data
collection only** — no attribution/scoring/remediation until there's data. Once
canaries are known, the sink is the existing cloudskipper (drop them before connect).

---

## Theme 2 — distribution & sync (the hard part: sharing without exposing)

dyva's value is inference-time survey data (smoke/honeypot/reputation), which is
genuinely useful to share — but sharing host data is exactly what could make the
project loud. These two notes are the careful ways to share *something* without
shipping the whole host list.

> **Priority note (from the operator):** the sample platter is currently **lower
> priority**. The project stays in the clear precisely *because it does not distribute
> hosts* today — that restraint is load-bearing, not an oversight. So anything that
> publishes hosts (the sample, the gossiper's public instance) is a deliberate,
> not-yet-needed step, weighed against that posture.

### [gossiper.md](gossiper.md) — parked (`-c/--config` shipped)
dyva↔dyva sync of *fresh good-host status* so a cold instance doesn't re-pay discovery
cost. The converged design is deliberately dumb and safe: **pull-on-demand positive
leads** (`GET /realtime?source=&model=`), **debounced-on-success push** (at most once
per `(host,model)` per window, positive-only — never push failures), a **most-recent-N
recency stack** with no TTL (freshness is implicit = N/push-rate), and the **receiver
owns judgment** (only fills blanks in its own DB, never overwrites what it measured).
Safe because it shares positives only — a wrong positive self-corrects on your own
retry; negatives (the poisonable, sticky kind) are never shared. **Parked** because
hosts are secured within seconds today, so the cold-start pain it solves isn't real;
trigger to revisit is measuring a cold start with the graflex source removed. The
`-c/--config` split (run a second instance alongside the default) **shipped**.

### [graflex-sample.md](graflex-sample.md) — planned, lower priority
A `graflex sample` subcommand that draws a small **stratified, rotating** subset
(defaults: 8 image / 8 video / 4 edit / 4 music / 3 speech / 30 text, from
proven-responsive hosts) and writes a new `graflex-sample.json` — a "sample platter"
so someone can clone the demos and get generation working **without** shipping tens of
thousands of hosts and **without** a curated winners list (which would concentrate load
and accelerate discovery of those machines). Destroys nothing (read-only inputs, one
new output, not committed to git); re-running rotates exposure. Consumed by dyva via a
pluggable `--pool <path|url>` — the same slot a gossiper URL would later drop into.

---

## Theme 3 — query / UX

### [query-syntax.md](query-syntax.md) — notes
Where the query grammar could go. Two grammars already exist: the **model-name token**
in routing (glob, `$` anchor, `>`/`<` size & date predicates, `,` fallback, `/`
capability) and the **`;`-prefixed meta-tokens** in the map view (`;live`, `;cloud`,
`;ollama`, `;aws`…). The direction is to make `;facet` a general filter language with
three shapes — boolean (`;live`), key=value (`;country=JP`), and metric comparison
(`;TPS>10`) — usable in routing *and* the dashboard. North-star:
`;country=JP;TPS>10;qwen>2026-02>4gb`. Smallest first step: add key=value + metric
facets to the existing `;` parser and let `;country=`/`;TPS>` post-filter in
`find_servers`, mirroring how size/date already do. Keep it forgiving, not a strict DSL.

---

## Theme 4 — source discovery

### [host-crossref.md](host-crossref.md) — built (`crossref.py`)
As the known survey sources dwindle, hunt for new ones by turning our own data into
bait and searching public code (GitHub via `gh search code`). Two orthogonal
techniques: (1) our **longest-lived good hosts** searched as `ip:port` — longevity is a
proxy for "notorious enough that others catalogued it too"; and (2) **honeypot-signature
model names** (`verif_sys:latest`, `ops-verify:latest`) that pepper the scans and are
near-zero-noise on the open web, so any file containing one is almost certainly a scan
dump. Both cluster by repo and rank candidate **new sources** by how many probes each
matches. Reads `host_status` read-only; rate-limit aware with exponential backoff (no
429s). See the note for the selection query and design.

## Ideas floated, not yet written up

_(none right now — the host cross-reference idea graduated to
[host-crossref.md](host-crossref.md) / `crossref.py`.)_
