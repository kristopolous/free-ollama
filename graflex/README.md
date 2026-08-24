# graflex

Discover public internet hosts via search-engine scraping (FOFA or Shodan) without paying for an API. Because let's be real, it wouldn't be free ollama if you had to pay Shodan/FOFA.

This is used as a [dyva/freeollama source](https://9ol.es/tmp/ollama-working.json)

Fair warning, running all the probes takes about 10 days. Days with a d. 

Speedup is possible with multiple accounts and probably proxying through an ip pool but the main governor here is the 3k/daily limit per account. I haven't had an ip blacklisted but I haven't tried account cycling to bypass the 3k limit. If I were the policy author for FOFA I'd do like a 5k ip blacklist limit with like a 14 day window. Not that they'd do that but you know, presume reasonability and work around it.

## Setup

```bash
pip install -e .
```

Create a `.env` file:

```env
# Required for -t fofa (default) — FOFA API key
FOFA_KEY=your_fofa_api_key

# Required — FOFA Authorization header token
FOFA_AUTHORIZATION=your_fofa_authorization_token

# Optional — Cookie header from browser (required for web method)
FOFA_COOKIE="fofa_theme=dark; fofa_token=...; fofa_result_page_size=50; ..."

# Required for -t shodan — shodan.io session cookie.
# Copy the polito cookie from your browser (or from a curl -b invocation).
# The \u0021 shell escape curl prints is handled automatically.
SHODAN_KEY='polito="3edd633c..."'
```

## Usage

```bash
# Standard image-gen services (A1111 / ComfyUI)
graflex -s a1111 -a fetch-check
graflex -s comfyui -a fetch-check

# llama.cpp hosts (server=="llama.cpp")
graflex -s llama.cpp -a fetch-check

# vLLM hosts (uvicorn default response on port 8000)
graflex -s vllm -a fetch-check

# Custom FOFA query — saves results to ollama-hosts.json
graflex -q 'body="ollama"' -a fetch -n ollama -p 11434

# Custom query with full axis overrides
graflex -q "body='ollama is running'" -a fetch -n ollama \
  -p 11434,8080,80,443,8983 \
  --servers 'nginx,cloudflare,Apache' \
  -c 'US,AU,IN,JP,DE,CA,BR,CN'

# Filter by specific FIDs (comma-separated; each runs as QUERY+fid on its own)
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

# Shodan site — scrapes shodan.io web results instead of FOFA
graflex -t shodan -s ollama -a fetch -n ollama-shodan

# Shodan with a custom query (note: shodan syntax — country:"US", port:8080)
graflex -t shodan -q '"ollama is running"' -n ollama-shodan \
  -c 'US,DE' -p '11434,8080'
```

## Shodan site

`-t shodan` scrapes the shodan.io web search the same way the `web`
method scrapes FOFA. Differences from the FOFA flow:

- Query syntax is shodan's: `country:"US"` (quoted code), `port:8080` (bare
  int), terms joined by spaces instead of `&&`. `--fid` and `--servers` are
  ignored.
- Up to 2 pages of results are fetched per query (page N is `&page=N` on the
  URL).
- Results are the hrefs of the `<a rel="noopener noreferrer nofollow">` links
  on the results page.
- Requires `SHODAN_KEY` in `.env` — your shodan session cookie (`polito="..."`).
- `-l/--limit` does not apply (page size is whatever shodan serves).
- Only some services have built-in shodan queries (`ollama`, `comfyui`);
  others require an explicit `--query` in shodan syntax.

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
| `comfyui` | 8188 | `/models/checkpoints` + `/api/system_stats` |
| `ollama` | 11434 | `/api/tags` |
| `llama.cpp` | 8080 | `/v1/models` |
| `vllm` | 8000 | `/v1/models` |

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

Progress is reported every 250 completed hosts:
`Checked: <n> | Runtime: <duration> | Remaining: <n> | ETA: <duration>`.

For `ollama`, a host that answers `/api/tags` but has no pullable (non-`:cloud`)
models is still recorded as working with an empty `models` list — it is running
ollama and reachable, so it counts as a good host. `version` comes from
`/api/version` when available.

For `llama.cpp`, model ids are taken from `/v1/models` `data[].id` verbatim (e.g.
`/models/.../DeepSeek-V3-Bf16-256x20B-BF16-00001-of-00035.gguf`). The full id
is kept because some instances serve multiple models and the id is what
disambiguates them.

For `vllm`, FOFA discovers candidates by matching the uvicorn default response
(`{"detail": "Not Found"}`) on port 8000. Each candidate is then probed at
`/v1/models` — most won't respond (hit rate is low), but the ones that do
reveal their loaded models in the same OpenAI-compatible format as `llama.cpp`.

For `llama.cpp`, hosts are also probed at `/props`; a `401` means the instance
is locked down with an API key and is rejected (`auth required`).

For `comfyui`, after listing checkpoints the host is also probed at
`/api/system_stats` to survey its hardware. When available, the result records
`version` (`system.comfyui_version`) and, from the first entry in `devices[]`,
`vram_device` (`name`), `vram_type` (`type`), and `vram_total` (bytes). A
failed or missing stats response never fails the check — models are still
recorded.

| Action | Behavior |
|--------|----------|
| `check` | Skips hosts already in the working file (including empty-model ollama hosts) and hosts with `result: "error"` in the not-working file. Rechecks `unreachable` hosts. |
| `check-new` | Skips all hosts with any previous record (working or not-working). Only checks hosts never tested before. |
| `check-all` | Rechecks every host regardless of previous status. |

Working: `~/.cache/free-ollama/{name}-working.json` (default: `image-gen-working.json`)
Failed:  `~/.cache/free-ollama/{name}-notworking.json` (default: `image-gen-notworking.json`)

## Options

| Flag | Description |
|------|-------------|
| `-s`, `--service` | Service to search for (`a1111`, `comfyui`, `ollama`, `llama.cpp`, `vllm`) |
| `-a`, `--action` | Action: `fetch`, `check`, `check-new`, `check-all`, or `fetch-check` |
| `-d`, `--dry` | Print what would be done without making requests |
| `--curlify` | Print curl command instead of executing (useful for debugging API requests) |
| `-l`, `--limit` | Max results per query (default: 2) |
| `-m`, `--method` | Fetch method: `api` or `web` (default: web) |
| `-q`, `--query` | Custom FOFA query (requires `--name`) |
| `-n`, `--name` | Cache file name prefix (default: `image-gen`) |
| `-c`, `--countries` | Comma-separated country codes to cycle |
| `-p`, `--ports` | Comma-separated port values to cycle |
| `-f`, `--fid` | Comma-separated FID values; each is fetched as `QUERY + fid="..."` on its own (not crossed with countries/ports/servers) |
| `--servers` | Comma-separated server values to cycle |
| `-i`, `--id` | Resume a previous session by providing its run timestamp (the `run_ts` from the log) |
| `-w`, `--workers` | Max parallel check workers (default: 10) |
| `--ct`, `--check-timeout` | Per-host check timeout in seconds (default: 60) |
| `-z`, `--sleep` | Seconds to sleep between requests (default: 4) |
| `-r`, `--random` | Shuffle the combination list (countries × ports × servers plus the FID follow-ups) so the fetch cycles in random order |
| `-t`, `--site` | Site to scrape: `fofa` (default) or `shodan`; recorded as `site` on each host entry |

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `FOFA_COOKIE must be set in .env for the web method` | The web fetch method requires a browser cookie to authenticate with FOFA's web interface. | Log into [fofa.info](https://en.fofa.info), open DevTools > Network, copy the `Cookie` header from any request, and set it as `FOFA_COOKIE` in `.env`. |
| `FOFA access denied — IP flagged as a web crawler` | FOFA has rate-limited or blocked your IP. The response contains `[-3000] IP access is abnormal`. | Wait a while, switch IPs (VPN/proxy), or try again later. This is a fatal error — graflex will not retry. |
| `daily usage limit hit` | Free tier FOFA accounts are limited to 3000 queries/day. | Resume later with `--id <run_ts>` from the error message. |
