# Model query syntax — brainstorm notes

Scratch notes on where the query/filter grammar could go. Not a spec, not a
commitment — just thinking out loud. The north-star example:

```
;country=JP;TPS>10;qwen>2026-02>4gb
```

"Japanese hosts doing >10 tok/s that serve a qwen model newer than 2026-02 and
bigger than 4 GB." One line, mixing host facts, measured performance, and
model predicates.

---

## What exists today (so we build on it, not over it)

Two grammars are already in the codebase:

**1. Model-name token** (used in real routing, `find_servers`):
- glob: `*`, `?`  (`*coder*`, `llama?`)
- trailing `$` anchors the end (`qwen3.8:27b$` excludes `-abliterated`/quant variants)
- size predicate: `>`/`<`/`>=`/`<=` + number + unit (`>10gb`, `<=4gb`); models with
  UNKNOWN size are KEPT (a size filter only drops known-fails)
- release-date predicate: `>`/`<` + `YYYY[-MM[-DD]]` (`>2026-02`); undated models are
  EXCLUDED by a date filter (can't include what we can't date)
- fallback chain: `,` — ordered alternatives (`qwen3.8,gemma4`)
- capability pairing: `/` (`edit/wan`, `vision/qwen`)
- so `qwen>2026-02>4gb` already parses today.

**2. `;`-prefixed meta-token** (currently only in the map/compare view, space-separated,
AND-combined per side):
- `;live` / `;dead` — working vs notworking population
- `;cloud` / `;nocloud` — on a detected big provider vs residential/unknown
- `;<service>` — `;ollama`, `;comfyui`, `;llama.cpp`, …
- `;<provider>` — `;aws`, `;hetzner`, …

The gap the example exposes: the `;` facets are (a) not available in the main
routing query, and (b) only boolean/enum — no `key=value`, no metric comparisons.

---

## The direction: `;facet` as a general filter language

Parse a query into: **one model-name token** (the existing grammar) + **zero or more
`;facets`**. Three facet shapes:

- **boolean** — presence toggles it: `;live`, `;cloud`, `;nocloud`, `;dead`
- **key=value** — `;country=JP`, `;service=ollama`, `;provider=aws`, `;state=good`
- **metric comparison** — `;TPS>10`, `;ttft<500`, `;seen>2026-02` (reuse the same
  `>`/`<`/`>=`/`<=` machinery the size/date predicates already use)

Facets are host/(host,model)-level filters; the bare token stays the model matcher.

### Separator question
The example uses `;` as BOTH prefix and delimiter (`;country=JP;TPS>10;qwen...`),
no spaces. Today facets are space-separated with a `;` prefix. Options:
- **`;` splits** — everything after a `;` up to the next `;` is a facet; the leading
  chunk with no `;` is the model token. Clean, matches the example, shell-safe.
- keep **space-separated** and just allow `key=value`/metric facets.
- allow BOTH (split on `;` OR whitespace) — most forgiving, fits the
  "human-intuitive, not strict" stance.

---

## Candidate facets (name → data source → notes)

| facet | source | notes |
|---|---|---|
| `;country=JP` (or `;cc=JP`) | graflex geoip enrichment (`country`) | 2-letter vs full name? case-insensitive. multi? `;country=JP,KR` |
| `;provider=aws` / `;cloud` / `;nocloud` | enrichment `provider` | already partly exists; datacenters-not-operators caveat |
| `;service=ollama` | host `service` | already exists as `;ollama` |
| `;TPS>10` | host_status `tps` (runtime-measured) | only some (host,model) have it; UNKNOWN → include or exclude? (size-filter precedent: include) |
| `;ttft<500` | host_status `ttft` (ms) | same unknown-handling question |
| `;state=good` / `;bad` / `;untested` | host_status `state` | ties into reputation tiers |
| `;seen>2026-02` / `;fresh<7d` | `updated_at` / `last_good` | needs the new updated_at column; relative durations (`7d`, `24h`) are their own mini-grammar |
| `;asn=...` / `;region=...` | enrichment | finer geo |
| `;smoke=pass` / `;smoke=fail` | `fail_smoke`/`smoke_date` | |
| `;has=vision` / `;has=tools` | caps | overlaps with the `/` capability pairing — pick one home |

---

## Open questions / tensions to resolve later

- **Unknown values.** Size includes unknowns; date excludes them. A `;TPS>10` on a
  host we've never measured — include (optimistic, explore) or exclude (strict)? Probably
  a per-facet call; write the rule down for each.
- **Where it runs.** These filters need host-level data (country, tps, state) that
  `find_servers` has via load_servers + host_status. Some (country/provider) filter the
  HOST; some (tps/ttft/state) are per-(host,model). Precedence when a host has several
  matching models?
- **Routing vs. dashboard.** Should the same string work in the chat model box AND the
  map/compare view AND the model filter? Unifying is the appeal; the map view's
  two-population compare (`a` vs `b` sides) is a different mode though.
- **Cloudskipper interaction.** `;provider=aws` to INCLUDE only aws vs the cloudskipper
  setting that EXCLUDES clouds — make sure include/exclude compose sanely.
- **Negation.** `;!country=JP`, `;country!=JP`, or `;-aws`? Pick one negation form.
- **Case / aliases.** `TPS` vs `tps`, `country` vs `cc`, provider display names. Keep it
  forgiving (the human-syntax stance), not a strict DSL.
- **Empty result vs. error.** A facet nothing matches — return empty (like a too-narrow
  size filter) rather than erroring.
- **Discoverability.** The dashboard filter's tooltip already documents size/date; this
  grows it. A `;` autocomplete/hint would help.

---

## Smallest first step (if we ever act on this)

Probably: add `key=value` + metric facets to the EXISTING `;` parser and let them
also apply in `find_servers` (country/provider/state/tps as an extra host filter,
mirroring how size/date already post-filter). `;country=` and `;TPS>` are the two
in the example and both have ready data sources (geoip enrichment; host_status.tps).
