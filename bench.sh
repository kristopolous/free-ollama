#!/usr/bin/env bash
# Standard dyva concurrency baseline — the invocation we agreed on.
#
# THIS FILE IS THE REFERENCE for the benchmark parameters. Change them here rather
# than in bench.py's defaults, so a run is described by something readable.
#
#   ./bench.sh                              full baseline (3600 requests, hours)
#   ./bench.sh --analyze                    re-summarise the newest run, run nothing
#   REPS=1 TASKS=2 ./bench.sh --concurrency 1      quick smoke before committing
#
# Results go to ./benchmarks/bench-<timestamp>.jsonl (gitignored). Every record is
# stamped with `git describe` and the run timestamp — with an uncommitted tree the
# version reads "-dirty" for every run, so the timestamp is what actually separates
# one baseline from another.

cd "$(dirname "$0")" || exit 1

PY="${PY:-python3}"
BASE="${BASE:-http://127.0.0.1:11434}"
TASKS="${TASKS:-15}"       # requests per cell
REPS="${REPS:-20}"         # repetitions per cell — n=20 to see past the pool's variance
TIMEOUT="${TIMEOUT:-300}"  # per request; long enough to survive a cold platter load
PROMPT="${PROMPT:-Who was the first US president?}"

# Quoted deliberately: these are dyva model QUERIES, not filenames. Unquoted, bash
# would try to expand the globs against the working directory.
MODELS=( 'qwen*3.8' 'qwen*3.6' 'gemma*4' )
CONCURRENCY=( 1 3 5 10 )

exec "$PY" bench.py \
  --base "$BASE" \
  --models "${MODELS[@]}" \
  --concurrency "${CONCURRENCY[@]}" \
  --tasks "$TASKS" \
  --reps "$REPS" \
  --timeout "$TIMEOUT" \
  --prompt "$PROMPT" \
  "$@"
