#!/usr/bin/env python3
"""Baseline benchmark: does concurrency actually buy throughput on the live pool?

End-to-end measurement against dyva as it really runs — heterogeneous pool, cold
loads, whatever reputation state exists. It deliberately does NOT try to isolate
variables or hold the pool still: the question is "if I run 10 agents instead of 1,
do I finish sooner in practice", and the conflation is part of the thing being
measured.

Shape: a cell is (model, concurrency, repetition). Each cell runs TASKS identical
requests through CONCURRENCY workers pulling from one queue, so an uneven split
(15 tasks / 10 workers) falls out naturally. No tools — several pool models don't
support them and would fail for the wrong reason.

The headline number is BATCH WALL-CLOCK per cell, and speedup vs the 1-worker cell
for the same model. Perfect scaling at 10 workers would be 10x; the hypothesis is
that today it's ~1x because of cold loads, one sticky host, and several agents
landing on the same machine.

Every request is appended to a JSONL as it completes, so a partial run is still
usable and `--analyze` can re-summarise without re-running anything.
"""
import argparse
import asyncio
import glob
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time

import aiohttp

PROMPT = "Who was the first US president?"
MODELS = ["qwen*3.8", "qwen*3.6", "gemma*4"]
CONCURRENCY = [1, 3, 5, 10]
TASKS = 15
REPS = 20   # n=20 per cell: enough samples that the spread is meaningful
HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_DIR = os.path.join(HERE, "benchmarks")   # gitignored: results are local artifacts


def git_version():
    """`git describe` of the tree this ran against. A baseline is worthless if you
    can't say what it was a baseline OF — every record carries it, so results from
    before and after a change can never be silently compared."""
    for cmd in (["git", "describe", "--tags", "--always", "--dirty"],
                ["git", "rev-parse", "--short", "HEAD"]):
        try:
            r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return "unknown"


async def one_request(session, base, model, prompt, timeout, meta):
    """One chat completion. Never raises — a failure is a record like any other."""
    rec = dict(meta, model_query=model, start=time.time())
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "stream": False}
    try:
        async with session.post(f"{base}/v1/chat/completions", json=payload,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            body = await r.text()
            rec["status"] = r.status
            # dyva reports which machine actually served it — that's how we see pileup
            rec["host"] = r.headers.get("X-Dyva-Host")
            rec["rmodel"] = r.headers.get("X-Dyva-Model")
            rec["service"] = r.headers.get("X-Dyva-Service")
            if r.status == 200:
                try:
                    obj = json.loads(body)
                    usage = obj.get("usage") or {}
                    rec["ptoks"] = usage.get("prompt_tokens")
                    rec["ctoks"] = usage.get("completion_tokens")
                    ch = (obj.get("choices") or [{}])[0]
                    rec["reply"] = ((ch.get("message") or {}).get("content") or "")[:120]
                    rec["ok"] = True
                except Exception as e:
                    rec["ok"] = False
                    rec["error"] = f"parse: {e}"
            else:
                rec["ok"] = False
                rec["error"] = " ".join(body.split())[:300]
    except Exception as e:
        rec["ok"] = False
        rec["error"] = f"{type(e).__name__}: {' '.join(str(e).split())[:200]}"
    rec["end"] = time.time()
    rec["secs"] = round(rec["end"] - rec["start"], 3)
    return rec


async def run_cell(session, base, model, conc, tasks, prompt, timeout, meta, sink):
    """TASKS requests through `conc` workers pulling a shared queue."""
    q = asyncio.Queue()
    for i in range(tasks):
        q.put_nowait(i)
    out = []

    async def worker(wid):
        while True:
            try:
                idx = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            rec = await one_request(session, base, model, prompt, timeout,
                                    dict(meta, task=idx, worker=wid))
            out.append(rec)
            sink(rec)

    t0 = time.time()
    await asyncio.gather(*[worker(w) for w in range(conc)])
    return out, round(time.time() - t0, 3)


def summarize(records):
    """Per (model, concurrency): batch wall-clock, speedup vs 1 worker, and how
    concentrated the hosts were (the pileup signal)."""
    cells = {}
    for r in records:
        cells.setdefault((r["model_query"], r["conc"]), {}).setdefault(r["rep"], []).append(r)

    walls = {}          # (model, conc) -> [wall per rep]
    for (m, c), reps in cells.items():
        w = []
        for _rep, rs in reps.items():
            # wall-clock of the batch = last end minus first start
            w.append(max(x["end"] for x in rs) - min(x["start"] for x in rs))
        walls[(m, c)] = w

    vers = sorted({r.get("ver") for r in records if r.get("ver")})
    runs = sorted({r.get("run") for r in records if r.get("run")})
    print()
    if vers or runs:
        print(f"version: {', '.join(vers) or '?'}    run: {', '.join(runs) or '?'}")
        if len(vers) > 1:
            print("  !! records span MORE THAN ONE tree version — do not compare these "
                  "as one baseline")
    hdr = (f"{'model':10} {'conc':>4} {'reps':>4} {'ok':>5} {'fail':>4} "
           f"{'wall(s)':>8} {'+/-sd':>7} {'speedup':>7} {'p50 req':>8} {'p95 req':>8} "
           f"{'hosts':>5} {'max/host':>8}")
    print(hdr)
    print("-" * len(hdr))
    for m in dict.fromkeys(r["model_query"] for r in records):
        base_wall = statistics.median(walls[(m, 1)]) if (m, 1) in walls else None
        for c in sorted({cc for (mm, cc) in walls if mm == m}):
            rs = [r for r in records if r["model_query"] == m and r["conc"] == c]
            ok = [r for r in rs if r.get("ok")]
            lat = sorted(r["secs"] for r in ok)
            med_wall = statistics.median(walls[(m, c)])
            spd = (base_wall / med_wall) if (base_wall and med_wall) else float("nan")
            hosts = [r.get("host") for r in ok if r.get("host")]
            uniq = len(set(hosts))
            top = max((hosts.count(h) for h in set(hosts)), default=0)
            p50 = statistics.median(lat) if lat else float("nan")
            # nearest-rank: ceil(0.95*n)-1. int() floors, and flooring AND
            # subtracting 1 lands a rank too low whenever 0.95*n isn't whole — at
            # n=2 that returned the minimum, printing a p95 below the p50.
            p95 = lat[min(len(lat) - 1, max(0, math.ceil(0.95 * len(lat)) - 1))] if lat else float("nan")
            sd = statistics.stdev(walls[(m, c)]) if len(walls[(m, c)]) > 1 else 0.0
            print(f"{m:10} {c:>4} {len(walls[(m, c)]):>4} {len(ok):>5} "
                  f"{len(rs) - len(ok):>4} {med_wall:>8.1f} {sd:>7.1f} {spd:>7.2f} "
                  f"{p50:>8.1f} {p95:>8.1f} {uniq:>5} {top:>8}")
    print("\nspeedup = median batch wall-clock at 1 worker / at N workers "
          "(N would be perfect scaling, ~1.0 means concurrency bought nothing)")
    print("+/-sd    = stdev of the per-rep batch wall-clock. If the gap between two "
          "speedups is\n           smaller than these, the pool's variance ate it "
          "and the result is inconclusive.")
    print("max/host = most requests a single machine served in that cell "
          "(high = the pileup)")


def load(path):
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


async def main_async(a):
    records = []
    f = open(a.out, "a", buffering=1)

    def sink(rec):
        f.write(json.dumps(rec) + "\n")

    # unlimited connector: our own client must never be the bottleneck being measured
    conn = aiohttp.TCPConnector(limit=0, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        cells = [(rep, m, c) for rep in range(a.reps) for m in a.models for c in a.concurrency]
        if a.shuffle:
            random.shuffle(cells)
        for n, (rep, m, c) in enumerate(cells, 1):
            meta = {"rep": rep, "conc": c, "run": a.label, "ver": a.ver}
            print(f"[{n}/{len(cells)}] rep={rep} model={m} workers={c} ...",
                  end="", flush=True)
            rs, wall = await run_cell(session, a.base, m, c, a.tasks, a.prompt,
                                      a.timeout, meta, sink)
            ok = sum(1 for r in rs if r.get("ok"))
            print(f" {wall:.1f}s  ok={ok}/{len(rs)}")
            records.extend(rs)
            if a.cooldown:
                await asyncio.sleep(a.cooldown)
    f.close()
    summarize(records)


def main():
    p = argparse.ArgumentParser(description="dyva concurrency baseline")
    p.add_argument("--base", default="http://127.0.0.1:11434", help="dyva base URL")
    p.add_argument("--models", nargs="*", default=MODELS)
    p.add_argument("--concurrency", nargs="*", type=int, default=CONCURRENCY)
    p.add_argument("--tasks", type=int, default=TASKS, help="requests per cell")
    p.add_argument("--reps", type=int, default=REPS)
    p.add_argument("--prompt", default=PROMPT)
    p.add_argument("--timeout", type=float, default=300, help="per-request seconds")
    p.add_argument("--cooldown", type=float, default=0, help="sleep between cells")
    p.add_argument("--shuffle", action="store_true",
                   help="randomise cell order (default: run them in order)")
    p.add_argument("--label", default=time.strftime("%Y%m%dT%H%M%S"))
    p.add_argument("--out", default=None,
                   help="results JSONL (default: benchmarks/bench-<label>.jsonl)")
    p.add_argument("--analyze", action="store_true",
                   help="just re-summarise an existing --out file, run nothing")
    a = p.parse_args()
    a.ver = git_version()
    if a.out is None:
        if a.analyze:                      # newest result file, so --analyze needs no path
            got = sorted(glob.glob(os.path.join(BENCH_DIR, "*.jsonl")))
            if not got:
                print(f"no result files in {BENCH_DIR}", file=sys.stderr)
                return 1
            a.out = got[-1]
        else:
            os.makedirs(BENCH_DIR, exist_ok=True)
            a.out = os.path.join(BENCH_DIR, f"bench-{a.label}.jsonl")
    if a.analyze:
        recs = load(a.out)
        if not recs:
            print(f"no records in {a.out}", file=sys.stderr)
            return 1
        print(f"{len(recs)} requests from {a.out}")
        summarize(recs)
        return 0
    total = len(a.models) * len(a.concurrency) * a.reps * a.tasks
    print(f"{total} requests: {len(a.models)} models x {len(a.concurrency)} "
          f"concurrencies x {a.reps} reps x {a.tasks} tasks")
    print(f"version: {a.ver}    run: {a.label}    -> {a.out}")
    try:
        asyncio.run(main_async(a))
    except KeyboardInterrupt:
        print("\ninterrupted — partial results are in "
              f"{a.out}; re-summarise with --analyze")
    return 0


if __name__ == "__main__":
    sys.exit(main())
