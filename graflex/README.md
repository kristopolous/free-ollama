# graflex

Discover public internet hosts via FOFA search engine without paying FOFA. Because let's be real, it wouldn't be free ollama if you had to pay Shodan/FOFA.

This is used as a [dyva/freeollama source](https://9ol.es/tmp/ollama-working.json)

## Setup

```bash
pip install -e .
```

Create a `.env` file:

```env
# Required — FOFA API key
FOFA_KEY=your_fofa_api_key

# Required — FOFA Authorization header token
FOFA_AUTHORIZATION=your_fofa_authorization_token

# Optional — Cookie header from browser (for web method fallback)
FOFA_COOKIE="fofa_theme=dark; fofa_token=...; fofa_result_page_size=50; ..."
```

## Usage

```bash
# Standard image-gen services (A1111 / ComfyUI)
graflex -s a1111 -a fetch-check
graflex -s comfyui -a fetch-check

# Custom FOFA query — saves results to ollama-hosts.json
graflex -q 'body="ollama"' -a fetch -n ollama -p 11434

# Custom query with full axis overrides
graflex -q "body='ollama is running'" -a fetch -n ollama \
  -p 11434,8080,80,443,8983 \
  --servers 'nginx,cloudflare,Apache' \
  -c 'US,AU,IN,JP,DE,CA,BR,CN'

# Filter by specific FIDs (comma-separated)
graflex -s comfyui -a fetch -f "xxx,yyy"

# Check hosts from a named cache file
graflex -s ollama -a check -n ollama

# Check without explicit service (infers from data)
graflex -a check -n ollama

# Check only new hosts (skip all previously-tested hosts)
graflex -s ollama -a check-new -n ollama

# Check all hosts (recheck everything, ignore previous results)
graflex -s ollama -a check-all -n ollama

# Dry run — shows plaintext queries instead of hitting FOFA
graflex -s a1111 -a fetch -d
# Print curl commands for FOFA API requests
gralex -s a1111 -a fetch --curlify -m api
gralex -q 'body="ollama"' -a fetch -n ollama --curlify

# Resume a run that hit the daily usage limit (use the run_ts from the error message)
graflex -s ollama -a fetch \
  -c "TH,MX,MY,NZ,..." -p "11434,..." --servers "nginx,..." \
  --session 20260718120000
```

## Shell script

`graflex.sh` wraps the fetch command with curated defaults per service:

```bash
# Fetch hosts for ollama (or comfyui / a1111)
./graflex.sh ollama
```

Sources at `graflex/graflex.sh`. Fair warning: fetching the ollama takes about 18 hours. The tests over it takes a while as well. It's re-entrent and cam be run in parallel. Doing so *shouldn't* affect results or performance.  Famous last words...


## Services

| Service | Default Port | Check Endpoint |
|---------|-------------|----------------|
| `a1111` | 7860 | `/sdapi/v1/sd-models` |
| `comfyui` | 8188 | `/models/checkpoints` |
| `ollama` | 11434 | `/api/tags` |

## How it works

### Fetch

Queries FOFA in cycles across all combinations of country, port, and server to
maximize coverage. Each axis can be overridden with comma-separated values
(via `-c`, `-p`, `--servers`) — a `None` entry for "no filter" is always
prepended.

If FOFA's daily usage limit is hit (3000 on the free tier), graflex exits with a
message showing the session timestamp. Resume later with `--session <run_ts>` to
skip all previously-fetched combinations and pick up where you left off.

Results saved to `~/.cache/free-ollama/{name}-hosts.json` (default: `image-gen-hosts.json`).

### Check

Probes each host from the seed list. After each host, the result is written to
disk atomically (write to `.tmp` then `os.replace`) so partial progress is never
lost on crash.

Each result includes a `checked` field with an ISO 8601 timestamp.

| Action | Behavior |
|--------|----------|
| `check` | Skips hosts already in the working file and hosts with `result: "error"` in the not-working file. Rechecks `unreachable` hosts. |
| `check-new` | Skips all hosts with any previous record (working or not-working). Only checks hosts never tested before. |
| `check-all` | Rechecks every host regardless of previous status. |

Working: `~/.cache/free-ollama/{name}-working.json` (default: `image-gen-working.json`)
Failed:  `~/.cache/free-ollama/{name}-notworking.json` (default: `image-gen-notworking.json`)

## Options

| Flag | Description |
|------|-------------|
| `-s`, `--service` | Service to search for (`a1111`, `comfyui`, `ollama`) |
| `-a`, `--action` | Action: `fetch`, `check`, `check-new`, `check-all`, or `fetch-check` |
| `-d`, `--dry` | Print what would be done without making requests |
| `--curlify` | Print curl command instead of executing (useful for debugging API requests) |
| `-l`, `--limit` | Max results per query (default: 2) |
| `-m`, `--method` | Fetch method: `api` or `web` (default: web) |
| `-q`, `--query` | Custom FOFA query (requires `--name`) |
| `-n`, `--name` | Cache file name prefix (default: `image-gen`) |
| `-c`, `--countries` | Comma-separated country codes to cycle |
| `-p`, `--ports` | Comma-separated port values to cycle |
| `-f`, `--fid` | Comma-separated FID values to filter by |
| `--servers` | Comma-separated server values to cycle |
| `--session` | Resume a previous session by providing its run timestamp (the `run_ts` from the log) |
| `--ct`, `--check-timeout` | Per-host check timeout in seconds (default: 60) |
| `--sleep` | Seconds to sleep between requests (default: 4) |
