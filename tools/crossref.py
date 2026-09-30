#!/usr/bin/env python3
"""crossref.py — hunt for NEW host-list sources by reverse-looking-up our own survey
on public code (GitHub, via `gh search code`).

Our known survey sources dwindle; this finds fresh ones by turning our own data into
bait. Two orthogonal search techniques, one back-end:

  1. DURABLE HOSTS. Our longest-lived good hosts (host_status), searched as "ip:port".
     A host that's been reachable-and-good for a week-plus is stable enough that other
     surveyors have found it too, so a repo that lists one is a candidate source. (A
     transient host never makes it onto anyone's list and would be dead by now anyway,
     which is why longevity is the selection signal.)

  2. HONEYPOT-SIGNATURE MODEL NAMES. Junk names like "verif_sys:latest" /
     "ops-verify:latest" that pepper the scans and that NO real user pulls. A file
     containing one is almost certainly a scan dump, so this finds host lists
     regardless of whether they overlap with our hosts — a purer "this file is a
     survey" fingerprint, and higher-yield. (Fake COMMERCIAL names like "gpt-4:latest"
     are deliberately NOT used: they drown in legitimate references.)

Both feed the same back-end: cluster hits by repo, dedup, and rank repos by how many
of our probes each one contains. A repo carrying several of our hosts / several
honeypot names is a strong candidate new source.

Reads host_status READ-ONLY (never writes/mutates the reputation DB). Needs `gh auth`.
Writes a report; nothing is printed but a short summary + the report path.
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time

DB_DEFAULT = os.path.expanduser("~/.cache/free-ollama/host-status.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crossref")

# Curated honeypot-SIGNATURE model names (technique 2). EXACT confirmed strings only —
# the same "exact denylist, never a pattern" stance as the cleanse. These are the junk
# names that appear across huge numbers of scanned hosts and that a real operator would
# never pull, so a GitHub file containing one is almost certainly a host-list dump. Add
# more as you confirm them (e.g. by eyeballing the most-ubiquitous names in the pool).
HONEYPOT_NAMES = [
    "verif_sys:latest",
    "ops-verify:latest",
]


def durable_hosts(db_path, live_days, limit):
    """Our longest-lived good hosts, most-recently-good first. Host-level (MIN
    created_at / MAX last_good grouped by host, so per-model rows don't double-count),
    filtered to real, non-deleted rows. Opened read-only — this never writes."""
    if not os.path.exists(db_path):
        sys.exit(f"no reputation DB at {db_path} (is dyva installed / has it run?)")
    con = sqlite_ro(db_path)
    try:
        rows = con.execute(
            "SELECT host, julianday(MAX(last_good)) - julianday(MIN(created_at)) AS live_days "
            "FROM host_status "
            "WHERE is_query=0 AND deleted_at IS NULL AND last_good IS NOT NULL "
            "GROUP BY host HAVING live_days > ? "
            "ORDER BY MAX(last_good) DESC LIMIT ?",
            (live_days, limit)).fetchall()
    finally:
        con.close()
    return [h for h, _ in rows]


def sqlite_ro(path):
    import sqlite3
    # mode=ro: the OS/driver refuses any write, so a bug here can never touch the live
    # reputation DB (see the never-mutate-live-reputation-db rule).
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def host_term(host, with_port):
    """The search string for a host record. host_status stores e.g.
    'http://1.2.3.4:11434'; strip the scheme (and the port unless kept) — 'ip:port' is
    the signature of a host list, a bare IP is too noisy."""
    s = re.sub(r"^https?://", "", host or "").rstrip("/")
    if not with_port:
        s = s.split(":", 1)[0]
    return s


def _is_rate_limit(stderr):
    """GitHub signals a hit limit several ways: a 429, the primary 'API rate limit
    exceeded', or the stricter 'secondary rate limit' on search. Match any."""
    low = (stderr or "").lower()
    return ("429" in low or "rate limit" in low or "secondary" in low
            or "please wait" in low or "try again later" in low)


def gh_search(term, per_term, sleep, retries):
    """Run one `gh search code` and return [{repo, path, url}].

    Backs off and RETRIES on a rate limit (429 / primary / secondary) with EXPONENTIAL
    backoff, so we ride out a limit instead of getting hammered by it or silently
    dropping the term — GitHub's code-search limit is ~10/min authenticated and the
    secondary limit is stricter still. A non-rate-limit failure (no matches, bad auth)
    returns [] without retrying. gh emits JSON with --json, so we parse structure."""
    backoff = max(sleep, 30.0)          # first rate-limit wait; doubles each retry, capped
    for attempt in range(retries + 1):
        try:
            p = subprocess.run(
                ["gh", "search", "code", term, "--json", "repository,path,url",
                 "--limit", str(per_term)],
                capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            sys.exit("`gh` not found — install the GitHub CLI and run `gh auth login`.")
        except subprocess.TimeoutExpired:
            if attempt < retries:
                continue
            return []
        if p.returncode == 0:
            try:
                data = json.loads(p.stdout or "[]")
            except Exception:
                return []
            out = []
            for item in data:
                repo = (item.get("repository") or {}).get("nameWithOwner") \
                    or (item.get("repository") or {}).get("name")
                if repo:
                    out.append({"repo": repo, "path": item.get("path"), "url": item.get("url")})
            return out
        err = " ".join((p.stderr or "").split())[:200]
        if _is_rate_limit(err) and attempt < retries:
            wait = min(backoff * (2 ** attempt), 300.0)   # 30,60,120,240,300… cap 5 min
            print(f"  ! rate-limited on {term!r}; backing off {wait:.0f}s "
                  f"(retry {attempt + 1}/{retries})", file=sys.stderr)
            time.sleep(wait)
            continue
        print(f"  ! search failed for {term!r}: {err}", file=sys.stderr)
        return []
    return []


def main():
    ap = argparse.ArgumentParser(description="find candidate host-list sources via gh code search")
    ap.add_argument("--db", default=DB_DEFAULT, help="host_status DB (read-only)")
    ap.add_argument("--limit", type=int, default=50, help="durable-host pool to sample from")
    ap.add_argument("--sample", type=int, default=10, help="how many durable hosts to actually search")
    ap.add_argument("--live-days", type=float, default=7, help="min host longevity (last_good - created_at)")
    ap.add_argument("--sleep", type=float, default=7, help="seconds between gh calls (stay under code search's ~10/min)")
    ap.add_argument("--retries", type=int, default=4, help="exponential-backoff retries on a 429/rate-limit")
    ap.add_argument("--per-term", type=int, default=30, help="gh --limit per query")
    ap.add_argument("--no-port", action="store_true", help="search bare IP instead of ip:port (wider, noisier)")
    ap.add_argument("--no-hosts", action="store_true", help="skip technique 1 (durable-host search)")
    ap.add_argument("--no-names", action="store_true", help="skip technique 2 (honeypot-name search)")
    a = ap.parse_args()

    # Build the probe list: sampled durable hosts + the honeypot names.
    probes = []   # [(kind, display, term)]
    if not a.no_hosts:
        pool = durable_hosts(a.db, a.live_days, a.limit)
        random.shuffle(pool)
        for h in pool[:a.sample]:
            probes.append(("host", h, host_term(h, not a.no_port)))
    if not a.no_names:
        for n in HONEYPOT_NAMES:
            probes.append(("name", n, n))
    if not probes:
        sys.exit("nothing to search (both techniques disabled, or no durable hosts found)")

    print(f"{len(probes)} probes ({sum(1 for k,_,_ in probes if k=='host')} hosts, "
          f"{sum(1 for k,_,_ in probes if k=='name')} names); ~{a.sleep}s between — "
          f"~{len(probes)*a.sleep/60:.1f} min")

    # repo -> {probes that hit it}, and the raw per-probe hits for the report.
    repos = {}          # repo -> {"probes": set(), "paths": set()}
    by_probe = []       # [{kind, probe, hits:[...]}]
    for i, (kind, display, term) in enumerate(probes, 1):
        print(f"[{i}/{len(probes)}] {kind}: {term}", file=sys.stderr)
        hits = gh_search(term, a.per_term, a.sleep, a.retries)
        by_probe.append({"kind": kind, "probe": display, "term": term, "hits": hits})
        for h in hits:
            r = repos.setdefault(h["repo"], {"probes": set(), "paths": set()})
            r["probes"].add(display)
            if h.get("path"):
                r["paths"].add(h["path"])
        if i < len(probes):
            time.sleep(a.sleep)

    # Rank candidate sources: a repo hit by MANY of our probes is the strongest lead.
    ranked = sorted(repos.items(), key=lambda kv: (-len(kv[1]["probes"]), kv[0]))

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    base = os.path.join(OUT_DIR, f"crossref-{stamp}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"probes": [{"kind": k, "probe": d, "term": t} for k, d, t in probes],
                   "by_probe": by_probe,
                   "repos": {r: {"probes": sorted(v["probes"]), "paths": sorted(v["paths"])}
                             for r, v in repos.items()}}, f, indent=2)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(f"# crossref {stamp}\n\n")
        f.write(f"{len(probes)} probes -> {len(repos)} repos.\n\n")
        f.write("## candidate source repos (ranked by probes matched)\n\n")
        if ranked:
            f.write("| repo | probes | example paths |\n|---|---|---|\n")
            for repo, v in ranked:
                paths = ", ".join(sorted(v["paths"])[:3])
                f.write(f"| {repo} | {len(v['probes'])} | {paths} |\n")
        else:
            f.write("_no hits._\n")
        f.write("\n## hits by probe\n\n")
        for bp in by_probe:
            f.write(f"- **{bp['kind']}** `{bp['probe']}` -> "
                    + (", ".join(sorted({h['repo'] for h in bp['hits']})) or "_none_") + "\n")

    top = ranked[0] if ranked else None
    print(f"\n{len(repos)} repos across {len(probes)} probes -> {base}.md")
    if top:
        print(f"top candidate: {top[0]} ({len(top[1]['probes'])} probes)")


if __name__ == "__main__":
    main()
