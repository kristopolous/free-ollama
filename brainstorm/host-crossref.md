# Host cross-reference — finding new sources by reverse-lookup

**Status: BUILT** as `crossref.py` (repo root). This note is the design record.

## Why

Our known survey sources dwindle (they lock down, go private, or die), so we have to
keep finding new ones. Instead of hunting blind, turn our own data into bait: take
things we know appear in host lists and search public code for them. A repo that
contains them is a candidate **new source**.

## Two techniques (orthogonal, same back-end)

### 1. Durable hosts as bait → `ip:port` search
Search GitHub for our own **longest-lived good hosts**. The selection is the point: a
host that's been reachable-and-good for a week-plus has persisted long enough that
*other* surveyors will have found and published it too; a transient host never makes
it onto anyone's list and would be dead by the time we searched. So **longevity is a
proxy for "notorious enough to be cross-referenced."** Search the `ip:port` form (the
signature of a host list) — a bare IP is too noisy. A repo listing one of our durable
hosts is a candidate source; how widely a host appears is also a rough exposure signal.

Front-end query (host-level, so per-model rows don't double-count; read-only,
`_LIVE_REAL`-filtered):

```sql
SELECT host,
       julianday(MAX(last_good)) - julianday(MIN(created_at)) AS live_days
FROM host_status
WHERE is_query=0 AND deleted_at IS NULL AND last_good IS NOT NULL
GROUP BY host
HAVING live_days > 7
ORDER BY MAX(last_good) DESC
LIMIT 50;
```

Then shuffle and take a small sample (default 10) so `gh` rate limits stay sane and
which hosts get burned on a search **rotates** each run.

### 2. Honeypot-signature model names → junk-name search
Search for the junk model names that pepper the scans and that **no real user pulls** —
`verif_sys:latest`, `ops-verify:latest`, and the like. A file containing one is almost
certainly a scan dump, so this finds host lists **regardless of whether they overlap
with our hosts** — a purer "this file is a survey" fingerprint, and higher-yield than
technique 1.

These names are **near-zero-noise** search terms: a web search for one turns up
essentially nothing except scan dumps and honeypot discussion (confirmed — a Google
search for one returned a single result, someone noting "we found these snooping
around"). That rarity is exactly what makes them good fingerprints.

Curated as an EXACT list (`HONEYPOT_NAMES` in `crossref.py`) — the same "exact
confirmed denylist, never a pattern" stance as the cleanse. Deliberately NOT the fake
*commercial* names from the cleanse denylist (`gpt-4:latest` etc.): those drown in
legitimate references and would be useless search terms. Extend the list as more bait
names are confirmed; a good way to surface candidates is to eyeball the most-ubiquitous
model names in the pool (the honeypot catalog is cloned across huge numbers of hosts).

## Back-end (the payoff)

Both techniques' hits feed one place: **cluster by repo, dedup, and rank repos by how
many of our probes each contains.** A repo carrying several of our durable hosts and/or
several honeypot names is a strong candidate new source. Output is a report
(`crossref/crossref-<ts>.{md,json}`): "candidate source repos, ranked" + "hits by
probe." Nothing is dumped to the terminal but a short summary and the path.

## Guardrails

- **Reads `host_status` READ-ONLY** (opened `mode=ro` so a bug can't touch the live
  reputation DB — cf. never-mutate-live-reputation-db).
- Needs `gh auth`. **Rate-limit aware, avoids 429:** a fixed sleep between calls
  (default 7s, to stay under code search's ~10/min primary limit) plus, on a
  429 / primary / secondary rate-limit, **exponential backoff with retry** of the same
  term (30→60→120→240s, capped 5 min, `--retries` tries) — it rides out a limit rather
  than hammering past it or silently dropping the term. A non-rate-limit failure (no
  matches, bad auth) returns empty without retrying; one failed query never aborts the
  sweep.
- `gh search code --json repository,path,url` — parses structure, never scrapes.

## Flags

`--limit` (durable pool, 50) · `--sample` (hosts searched, 10) · `--live-days` (7) ·
`--sleep` (6) · `--per-term` (gh `--limit`, 30) · `--no-port` (widen to bare IP) ·
`--no-hosts` / `--no-names` (run one technique only).

## Possible next steps

- Auto-suggest new `HONEYPOT_NAMES` from the most-ubiquitous non-real names in the pool.
- Feed a confirmed candidate repo straight into the source config (`{name, url,
  mapping}`) as a new graflex source, closing the loop from discovery to ingestion.
