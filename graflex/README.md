# graflex

Discover public internet hosts via FOFA search engine without paying FOFA. Because let's be real, it wouldn't be free ollama if you had to pay Shodan/FOFA.

This is used as a [dyva/freeollama source](https://9ol.es/tmp/ollama-working.json)

Fair warning, running all the probes takes about 10 days. Days with a d. 

Speedup is possible with multiple accounts and probably proxying through an ip pool but the main governor here is the 3k/daily limit per account. I haven't had an ip blacklisted but I haven't tried account cycling to bypass the 3k limit. If I were the policy author for FOFA I'd do like a 5k ip blacklist limit with like a 14 day window. Not that they'd do that but you know, presume reasonability and work around it.

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

# llama.cpp hosts (server=="llama.cpp")
graflex -s llama.cpp -a fetch-check

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
  --id 20260718120000
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
| `llama.cpp` | 8080 | `/v1/models` |

## How it works

### Fetch

Queries FOFA in cycles across all combinations of country, port, and server to
maximize coverage. Each axis can be overridden with comma-separated values
(via `-c`, `-p`, `--servers`) — a `None` entry for "no filter" is always
prepended.

If FOFA's daily usage limit is hit (3000 on the free tier), graflex exits with a
message showing the session timestamp. Resume later with `--id <run_ts>` to
skip all previously-fetched combinations and pick up where you left off.

Results saved to `~/.cache/free-ollama/{name}-hosts.json` (default: `image-gen-hosts.json`).

### Check

Probes each host from the seed list. After each host, the result is written to
disk atomically (write to `.tmp` then `os.replace`) so partial progress is never
lost on crash.

Each result includes a `checked` field with an ISO 8601 timestamp.

For `llama.cpp`, model ids from `/v1/models` are full file paths (e.g.
`/models/.../DeepSeek-V3-Bf16-256x20B-BF16-00001-of-00035.gguf`). Only the
basename is kept, and a trailing `-NNNN-of-NNNN.gguf` / `.gguf` is stripped, so
that becomes `DeepSeek-V3-Bf16-256x20B-BF16`.

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
| `-s`, `--service` | Service to search for (`a1111`, `comfyui`, `ollama`, `llama.cpp`) |
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
| `-i`, `--id` | Resume a previous session by providing its run timestamp (the `run_ts` from the log) |
| `-w`, `--workers` | Max parallel check workers (default: 10) |
| `--ct`, `--check-timeout` | Per-host check timeout in seconds (default: 60) |
| `-z`, `--sleep` | Seconds to sleep between requests (default: 4) |
| `-r`, `--random` | Shuffle the ports, servers, countries, and FID lists so the fetch cycles through combinations in random order |
