<p align="center">
<img width="700" alt="2026-10-09-03h54m05s_seed947866237_move the llama to a different part of the datacenter by using different servers and a different layo" src="https://github.com/user-attachments/assets/c9dcb985-e84b-4aa0-a928-4e2d621fb173" />
</p>

**Graflex** is for surveying LLM related internet hosts via search-engine scraping such as FOFA, Shodan, Censys, Zoomeye, and Hunter.how.

This is used as a [dyva/freeollama source](https://9ol.es/tmp/ollama-working.json) but there is no output included by default.

Fair warning, running all the probes takes about 4-10 days. Days with a d. First run takes longer than subsequent runs because we assume once a host is dead, it's dead (although you *can* rescan everything ... it's mostly a waste of time)

Each service has a separate query run on each service. These are in resumable sessions and can be seen in graflex.sh.

There's all inclusives for the services such as zoomeye or hunter. 

## Disclaimer

Graflex is a fast track to get you soft-blocked from places like Cloudflare and Huggingface. There's trap servers (jargoned as canaries) that will report your ip if you doorknock them and I haven't found a way of quarantining them yet. 

The dyva runtime does this by allowing you to just completely block out entire clouds. That I guess is possible to do in graflex but as of now, the pipelines are intentionally completely separated. I'll have to think about that. 

Dyva is designed to run comfortably on a 512MB machine and should occupy about 100MB of RSS which means a cheap $3 or $4/month server can run it without an OOM killer knocking it down.

## Setup

```bash
pip install -e .
```

Create a `.env` file:

```env
# Required for -t fofa (default) — cookie header from your browser.
# Omitting fofa_result_page_size makes FOFA default to 10 results/page.
FOFA_COOKIE="fofa_theme=dark; fofa_token=...; fofa_result_page_size=50; ..."

# Required for -t shodan — shodan.io session cookie.
# Copy the polito cookie from your browser (or from a curl -b invocation).
# The \u0021 shell escape curl prints is handled automatically.
SHODAN_KEY='polito="3edd633c..."'

# Required for -t zoomeye — zoomeye.ai session cookie (the whole Cookie header,
# copied from your browser / a curl -b invocation). It is a SESSION token that
# EXPIRES, so refresh it when zoomeye starts failing. The JWT inside it (token=…)
# is reused automatically for the Cube-Authorization header.
ZOOMEYE_COOKIE='__jsluid_s=...; sessionid=...; token=...; ...'

# Required for -t censys — platform.censys.io session cookie (the whole Cookie
# header). Censys fronts the search API with Cloudflare, and the clearance is
# bound to the IP that solved the challenge, so this MUST be generated on THIS
# machine's egress IP (load platform.censys.io in a browser here, copy the request
# Cookie). It EXPIRES; when a run stops with "Cloudflare challenge", regenerate it.
CENSYS_COOKIE='cf_clearance=...; __cf_bm=...; csrf_v2=...; ...'
# Cloudflare also binds the clearance to the User-Agent, so CENSYS_UA MUST be the
# exact UA of the browser that generated the cookie or the challenge bounces you.
# Optional — defaults to the UA in censys.txt; set it to match your browser.
CENSYS_UA='Mozilla/5.0 (X11; Linux x86_64) ... Chrome/152.0.0.0 Safari/537.36'

# Required for -t hunter — hunter.how session cookie (the whole Cookie header) AND
# the Authorization header. Deeper pages 401 with "coming soon" without the bearer.
# HUNTER_AUTH is the FULL Authorization value copied verbatim (keep the "Bearer ").
# Both EXPIRE; refresh when hunter starts returning code!=200.
HUNTER_COOKIE='_ga=...; ack=...; token=...; ...'
HUNTER_AUTH='Bearer eyJ...'
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

# LM Studio hosts (default response body on port 1234)
graflex -s lmstudio -a fetch-check

# ds4 hosts (antirez's DeepSeek server; "unknown endpoint" body, US/CN)
graflex -s ds4 -a fetch-check

# Custom FOFA query — saves results to ollama-hosts.json
graflex -q 'body="ollama"' -a fetch -n ollama -p 11434

# Custom query with full axis overrides
graflex -q "body='ollama is running'" -a fetch -n ollama \
  -p 11434,8080,80,443,8983 \
  --servers 'nginx,cloudflare,Apache' \
  -c 'US,AU,IN,JP,DE,CA,BR,CN'

# Filter by specific FIDs (comma-separated; each runs as QUERY+fid on its own)
graflex -s comfyui -a fetch -f "xxx,yyy"

# Named queries — fetch-only searches that aren't services (no check step).
# Results land in <name>-hosts.json with service set to the name.
# Built-in: gradio (icon_hash=="55115683")
graflex -n gradio -a fetch

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
# Print curl commands instead of executing
graflex -s a1111 -a fetch --curlify
graflex -q 'body="ollama"' -a fetch -n ollama --curlify

# Resume a run that hit the daily usage limit (use the run_ts from the error message)
graflex -s ollama -a fetch \
  -c "TH,MX,MY,NZ,..." -p "11434,..." --servers "nginx,..." \
  --id 20260718120000

# Shodan site — scrapes shodan.io web results instead of FOFA
graflex -t shodan -s ollama -a fetch -n ollama-shodan

# Shodan with a custom query (note: shodan syntax — country:"US", port:8080)
graflex -t shodan -q '"ollama is running"' -n ollama-shodan \
  -c 'US,DE' -p '11434,8080'

# ZoomEye site — scrapes zoomeye.ai's JSON API into the same ollama pool
graflex -t zoomeye -s ollama -n ollama --dry     # print the queries + URL first
graflex -t zoomeye -s ollama -n ollama -c 'CN,US,FR,DE,...'

# Censys site — replays platform.censys.io's search API into the same ollama pool
graflex -t censys -s ollama -n ollama --dry      # print the query + URL first
graflex -t censys -s ollama -n ollama            # 1 query: broad "ollama is running"
graflex -t censys -s ollama -n ollama -c 'United States,China,Germany'   # 3 queries, one per country (FULL names)
graflex -t censys -q '(("ollama is running") and host.services.software.vendor = "ollama") or web.software.vendor = "ollama"' -n ollama

# Hunter site — replays hunter.how's search API into the same ollama pool
graflex -t hunter -s ollama -n ollama --dry      # print the query + payload first
graflex -t hunter -s ollama -n ollama            # product.name=="Ollama Server", paginated
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
- Only some services have built-in shodan queries (`ollama`, `comfyui`);
  others require an explicit `--query` in shodan syntax.

## ZoomEye site

`-t zoomeye` hits zoomeye.ai's JSON search API (`/api/search`) rather than
scraping HTML, and folds the results into the SAME pool as FOFA/Shodan (each
host tagged `site: "zoomeye"`). Notes:

- Query syntax is zoomeye's: `app="ollama"`, `country="CN"`, `port="11434"`,
  joined by `&&`. The query is base64-encoded into the `q` param automatically.
- Auth is `ZOOMEYE_COOKIE` (`.env`) — the whole browser Cookie header. The `token=`
  JWT inside it is echoed as the `Cube-Authorization` header. It is a **session**
  cookie that **expires**: refresh it when zoomeye starts failing.
- ZoomEye returns a huge `total` but caps how deep you can page at ~250 results
  per query (`max` in the payload). So the run **narrows by country** — one broad
  pass plus one pass per `--countries` code — to slice past the cap; each slice
  pages (50/page) until it empties. There is **no port filter** (ollama runs on
  many ports, so filtering by port would miss most of them).
- No retry: an HTTP error or unparsable body stops the run (by design).
- Raw JSON payloads are saved under `/tmp/graflex/<session>/zoomeye/`.
- `ollama` has a built-in zoomeye query (`app="ollama"`); other services need an
  explicit `--query`. Country codes are ISO alpha-2 (`GB`, not `UK`).
- Run it by hand (`graflex.sh ollama-zoomeye`); it's kept out of the daily `all`
  loop because the cookie expires, unlike the stable Shodan key.

## Censys site

`-t censys` replays platform.censys.io's search API (`/api/search.data`) and folds
the results into the SAME pool as FOFA/Shodan/ZoomEye (each host tagged `site:
"censys"`). It surfaces hosts that don't appear in the other sources. Notes:

- Query syntax is Censys': `"ollama is running"`, `host.services.port = "11434"`,
  `host.services.software.vendor = "ollama"`, `web.software.vendor = "ollama"`,
  combined with `and`/`or` and parentheses. A service may carry a **list** of
  queries; the run iterates them all in one session, deduping into the pool.
- **Censys is credit-metered and the quota is tiny** (as few as ~5 queries/month),
  so a run spends **exactly** the number of queries you ask for — no automatic extra
  passes. The run logs that count up front. Country slicing: a `-c/--countries` CSV
  is filled verbatim into the Censys `host.location.country` field — which wants
  **full names** (`United States`, `China`, `Germany`), NOT ISO codes — as **one
  query per country** (`(<query>) and host.location.country = "<name>"`). With no
  `-c` it's a **single broad query**. There is no page-walking (the page is
  generous), so widen coverage only by deliberately spending another query.
- **The response is deduped/obfuscated** (a Remix turbo-stream reference pool), but
  it is fully structural — we rehydrate the pool and read `data.results.hits[]`. No
  regex scraping. A hit carries **two datasets** (one is null depending on the query
  match): the WEB dataset gives one endpoint at `web.web.{hostname, port}`; the HOST
  dataset gives `host.host.ip` plus a list of `host.host.services[]` (SSH, MySQL,
  nginx, ... — many ports on the box). For a host hit we emit **only** the service(s)
  whose `software[].vendor`/`product` Censys fingerprinted as the service being
  searched (e.g. `ollama`), so port 22 / 3306 / etc. never leak into the pool as fake
  endpoints. Because the schema can change without notice, the parser **stops hard
  (exit 3)** if it can't reach the hits or a populated result yields nothing — a loud
  failure over silent garbage. Raw JSON is saved under `/tmp/graflex/<session>/censys/`.
- The page size is generous, so there is **no pagination** — widen coverage by
  iterating queries, not paging.
- Auth is `CENSYS_COOKIE` (`.env`) — the whole browser Cookie header. Censys fronts
  the endpoint with **Cloudflare**, and the clearance is bound to the IP that solved
  the challenge, so the cookie **must be generated on this machine's egress IP** and
  it **expires**. If a run stops with "Cloudflare challenge / non-JSON response
  (exit 3)", regenerate `CENSYS_COOKIE` here. Once it passes the challenge once,
  further queries in the same session are not re-gated.
- `ollama` has a built-in censys query (`"ollama is running"`); other services need
  an explicit `--query`.

## Hunter site

`-t hunter` replays hunter.how's search API (`POST /api/search`) and folds the
results into the SAME pool as the other sites (each host tagged `site: "hunter"`).
Unlike Censys the response is clean JSON — `{"code":200,"data":{"list":[{ip,port,
...}]}}` — so extraction is just `data.list[*].{ip, port}`. Notes:

- Query syntax is hunter.how's: `product.name=="Ollama Server"` (its ollama
  fingerprint), `web.body="ollama is running"`, `ip.port=="11434"`,
  `ip.country=="United States"`, combined with `and`/`or`. The plaintext query is
  **base64-encoded into the request's `search` field** automatically. The built-in
  `ollama` query is the `product.name` fingerprint (all ports); add
  `and ip.port=="11434"` (via `--query`) to restrict to the default port.
- **Coverage is PAGINATION, not country fan-out** — one query, walked page by page
  up to `HUNTER_MAX_PAGES` (40), stopping at the first empty page. hunter.how
  **requires `page_size == 10`** (larger 400s with "The optional range of page size
  is 10"), so a run is up to ~400 results/query. To narrow by country, put it in the
  `--query` yourself (e.g. `... and ip.country=="United States"`); `-c/--countries`
  is not used here.
- On resume, a **valid** stored page is reprocessed from disk (no query spent), but a
  stored **error/unparseable** page is **re-fetched** (it has no data to preserve) —
  so a page that errored once, e.g. from a since-fixed request bug, isn't lost.
- A `code` != 200 is an API error — **not** results — and is stopped hard (exit 3)
  with the EXACT request logged (decoded query, full payload, and a replayable curl)
  so it's actionable, not a mystery. A `401 "coming soon"` on page 2+ means the
  `Authorization` bearer is missing/expired.
- Auth is **two** values in `.env`: `HUNTER_COOKIE` (the whole Cookie header) and
  `HUNTER_AUTH` (the full `Authorization` header value, e.g. `Bearer eyJ...`, copied
  verbatim). Deeper pages 401 without the bearer. Both expire.
- Raw JSON is saved under `/tmp/graflex/<session>/hunter/`, **always, before
  parsing**. On resume (`-i <session>`) a stored response is **reprocessed from
  disk** (no query spent); a per-file parse failure is logged and skipped, never
  fatal to the batch.

## Shell script

`graflex.sh` wraps the fetch command with curated defaults per service:

```bash
# Fetch hosts for ollama (or comfyui / a1111 / vllm / llama.cpp / lmstudio / ds4 / gradio)
./graflex.sh ollama
```

Sources at `graflex/graflex.sh`. Fair warning: fetching the ollama takes about 18 hours. The tests over it takes a while as well. It's re-entrent and cam be run in parallel. Doing so *shouldn't* affect results or performance.  Famous last words...


## Services

| Service | Default Port | Check Endpoint |
|---------|-------------|----------------|
| `a1111` | 7860 | `/sdapi/v1/sd-models` |
| `comfyui` | 8188 | `/models/checkpoints` + `/models` traversal + `/api/system_stats` |
| `ollama` | 11434 | `/api/tags` |
| `llama.cpp` | 8080 | `/v1/models` |
| `vllm` | 8000 | `/v1/models` |
| `lmstudio` | 1234 | `/v1/models` |
| `ds4` | 8080 | `/v1/models` |

## Named queries

Named queries are fetch-only searches that don't fit the service model (no
probe/check step). They live in `NAMED_QUERIES` in `graflex/__init__.py` and
are selected with `-n <name>`:

```bash
graflex -n gradio -a fetch   # icon_hash=="55115683", site fofa
```

Entries are saved to `~/.cache/free-ollama/gradio-hosts.json` with
`service: "gradio"` and no *service* check applies to them.

### Checking and classifying the Gradio family

The Gradio icon is not a service — it is *every* Gradio app anyone exposed:
image generators, background removers, OpenCV demos, protein tools, LLM-key
proxies, hello-world stubs. There is no shared inference API, so it flows
through the normal pipeline with app-specific parsing:

```bash
graflex -n gradio -a fetch     # icon_hash=="55115683" (+ default ports/countries/fids)
graflex -n gradio -a check     # liveness = a parseable /config manifest per host
graflex -n gradio -a classify  # bucket the manifests by app purpose
```

- **check** — for a gradio host the probe is its `/config` (Gradio's own app
  manifest: title, component labels, function names). A host is "working" when
  that parses as a Gradio config; the manifest summary is stored on the working
  entry (Gradio embeds raw control chars, so it is read with a lenient JSON
  parse). Same worker pool, pruning and resume as every other service.
- **classify** — buckets each host's manifest against an **ordered**, editable
  keyword taxonomy in [`gradio-taxonomy.json`](gradio-taxonomy.json)
  (`image-gen`, `video-gen`, `image-edit`, `cv-detect`, `audio`, `llm-chat`,
  `science`, `demo`; first match wins, else `unknown`) and writes
  `gradio-working-classified.json` with an `app_type` per host. Re-runnable, so
  you can grow the taxonomy and re-bucket without re-probing.

This is survey instrumentation — most buckets are apps nothing would ever route
to. Finding *usable* generators for dyva is the opposite job: hunt branded apps
by their own fingerprints (as with the `fooocus` service), not by the Gradio
icon.


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

Hosts discovered via an `--fid` search are tagged with that fingerprint:
`{"service": ..., "host": ..., "site": ..., "fid": "..."}`. If a host was
already known from a non-FID query, the `fid` is backfilled onto the existing
entry the next time an FID query surfaces it. This is what lets hosts be
categorized by the fingerprint they were found under (e.g. gradio).

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

For `lmstudio`, FOFA discovers candidates by matching LM Studio's default
response body (`Unexpected endpoint or method. (GET /)`) on port 1234. Each
candidate is then probed at `/v1/models`, which returns loaded models in the
same OpenAI-compatible format (`data[].id`) as `llama.cpp` and `vllm`.

For `ds4` (antirez's DeepSeek-serving inference server; models report
`owned_by: "ds4.c"`), FOFA discovers candidates by matching the exact error body
its root/unknown paths return — `{"error":{"message":"unknown endpoint",...}}` —
on port 8080, US/CN only (few in the wild). Each candidate is probed at
`/v1/models`, returning models in the same OpenAI format (`data[].id`).

For `llama.cpp`, hosts are also probed at `/props`; a `401` means the instance
is locked down with an API key and is rejected (`auth required`).

For `comfyui`, after listing checkpoints the host's `/models` index is
traversed: each category folder (`checkpoints`, `loras`, `vae`, ...) is listed
and every model is recorded with its category prefix, e.g.
`checkpoints/majic_v7_sd15.safetensors` or `loras/x.safetensors`. Nested
backslash paths are normalized to forward slashes. Hosts that return a usable
index get `model_tree: true`; older ComfyUI builds without it fall back to the
flat `/models/checkpoints` list. The host is also probed at
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
| `check-working` | Re-surveys only the currently-working hosts: refreshes each host's model list (operators keep downloading new models) and prunes hosts that no longer respond, dropping them from the working file and recording them as not-working. On a resumed run (`-i`), hosts already snapshotted this session are kept in the pool untouched, not re-probed — and not dropped. |
| `reconstruct` | Rebuild `{name}-working.json` **offline** from a check session's cached probe snapshots (`-i <session>`), for recovering a pool that got truncated. See *Recovering a truncated working pool*. |
| `classify` | Bucket every model of every host in `{name}-working.json` by type. See below. |

Working: `~/.cache/free-ollama/{name}-working.json` (default: `image-gen-working.json`)
Failed:  `~/.cache/free-ollama/{name}-notworking.json` (default: `image-gen-notworking.json`)

## Recovering a truncated working pool

`{name}-working.json` is a **derived, rebuildable** view — the hosts that passed
the last check — not a primary record; the primary inventory is
`{name}-hosts.json`. Two safeguards keep a bad pass from gutting it, and one
action rebuilds it if something ever does:

- **Write-boundary guard.** Every cache write goes through one atomic writer that,
  before replacing a file, compares the new record count against what's on disk and
  **refuses the write** when it would drop the pool by more than 40% (and the file
  held ≥10 records). A failed run — thousands of hosts loaded, a handful answering —
  therefore can't overwrite the full pool with its stub; the file is left intact and
  the refusal logged loudly. Normal appends only grow the file, so it never trips.
- **check-working circuit breaker.** A single re-survey that would prune more than
  half the pool (and >20 hosts) is treated as a bad pass (network blip, too-tight
  `--ct`, local trouble), not mass host death: it refuses to prune, refreshes the
  hosts that answered, and keeps the rest.
- **`reconstruct`.** If a pool is lost anyway, rebuild it offline from a session's
  cached probe snapshots — the raw model-list response of every host that answered
  is saved under `/tmp/graflex/<session>/check/`:

  ```bash
  graflex -n ollama -a reconstruct -i 20260908154105
  ```

  It re-parses model names the same way the live check does, drops empty catalogs
  and obvious honeypots (whose whole catalog is the phantom frozen set), and
  **merges** into the working file (newest `checked` wins, geo carried, never
  clobbered). It's offline recovery, not a live vet — it restores a permissive
  superset, so follow it with `enrich` (re-stamp geo), `check-working` (re-probe,
  prune dead, live honeypot gate) and dyva `--cleanse` (strip fake model names) to
  converge on the fully vetted pool.

## Classify

```bash
graflex -s comfyui -a classify
```

Reads `~/.cache/free-ollama/{name}-working.json`, matches every model string
against the regexes in `graflex/model-classifier.json`, prints one
`[type] model` line per unique model to stdout, and writes each host back with
an added `classified` map to `{name}-working-classified.json`:

```json
{
  "...": "the regular host definition",
  "classified": {
    "image": ["checkpoints/majic_v7_sd15.safetensors", "..."],
    "video": ["diffusion_models/ltx-2.3-22b-dev-fp8.safetensors"],
    "audio": ["TTS/fish-speech-1.5.pt"],
    "other": ["sam3/sam3.pt"]
  }
}
```

The classifier file maps a type to a list of regexes (created from built-in
defaults on first run — edit it and rerun until the buckets look right):

```json
{
  "image": ["regex", "..."],
  "video": ["..."],
  "audio": ["..."]
}
```

Categories are evaluated top to bottom, first matching regex wins, and
anything unmatched lands in `other`. Invalid regexes are skipped with a
warning.

### The comfy file tree is a downstream input, not just a classifier feed

For a ComfyUI host, the `models` list check records is the host's **full file
tree** — not just checkpoints but `text_encoders/…`, `vae/…`, `loras/…`,
`diffusion_models/…`, and the `custom_nodes/…` paths too. Keep it whole. dyva
reads these working caches to build ComfyUI media workflows against what a host
*actually* has, and the tree is enough to do that authoring and file-validation
**offline**, without a live probe:

- the file names tell dyva which model variant, encoder, VAE and LoRA to load
  (matched by class, so `minimax_h3_ref2va_bf16` and `…_fl2va_pruned_int8` are
  interchangeable candidates for the same slot);
- the `custom_nodes/…` paths reveal which **node pack** is installed — e.g.
  `custom_nodes/ComfyUI-MiniMax-H3-PDD-Acc/…` marks a PDD-Acc host, which needs a
  different graph from a stock MiniMax-H3 host.

The one thing this survey does **not** capture is ComfyUI `/object_info` (the
node *classes* and their input schemas). That is deliberate: it is large, it
changes with every custom node install, and it is only needed at the moment a job
is submitted — when the host is reachable anyway because it was just selected. So
graflex records the durable, cheap thing (what files and packs exist) and leaves
the live, volatile thing (what node classes exist) to submit time.

## Options

| Flag | Description |
|------|-------------|
| `-s`, `--service` | Service to search for (`a1111`, `comfyui`, `ollama`, `llama.cpp`, `vllm`, `lmstudio`, `ds4`) |
| `-a`, `--action` | Action: `fetch`, `check`, `check-new`, `check-all`, `check-working`, `reconstruct`, `fetch-check`, or `classify` |
| `-d`, `--dry` | Print what would be done without making requests |
| `--curlify` | Print curl command instead of executing (useful for debugging requests) |
| `-q`, `--query` | Custom FOFA query (requires `--name`) |
| `-n`, `--name` | Cache file name prefix (default: `image-gen`); for fetch, a named query (`gradio`) also selects its built-in FOFA query |
| `-c`, `--countries` | Comma-separated country codes to cycle |
| `-p`, `--ports` | Comma-separated port values to cycle |
| `-f`, `--fid` | Comma-separated FID values; each is fetched as `QUERY + fid="..."` on its own (not crossed with countries/ports/servers); hosts found this way record the `fid` |
| `--servers` | Comma-separated server values to cycle |
| `-i`, `--id` | Resume a previous session by providing its run timestamp (the `run_ts` from the log); ctrl+c during fetch suggests this automatically |
| `-w`, `--workers` | Max parallel check workers (default: 10) |
| `--ct`, `--check-timeout` | Per-host check timeout in seconds (default: 60) |
| `-z`, `--sleep` | Seconds to sleep between requests (default: 4) |
| `-r`, `--random` | Shuffle the combination list (countries × ports × servers plus the FID follow-ups) so the fetch cycles in random order |
| `-t`, `--site` | Site to scrape: `fofa` (default), `shodan`, or `zoomeye`; recorded as `site` on each host entry |

## Field notes: host exposure patterns

Observations from characterising the discovered population. They matter because a
host that *appears* gated to the survey often is not, which changes how we count
"exposed." These are descriptive research notes, and the project's stance holds:
an auth-gated endpoint is **recorded and left alone, never targeted** — graflex
and dyva do not use these observations to defeat anyone's access controls, and an
auth-required vector stays a pass.

1. **Reverse proxies in front.** Many hosts sit behind nginx / apache / etc., so
   the service answers on a proxy port (commonly :443) rather than its native
   one. (Already known; the checker speaks both the proxied and native shapes.)

2. **The native port is often also open.** On many of these, the underlying
   service port is *also* directly reachable — e.g. :443 proxies to :11434 while
   :11434 itself is open. The proxy is not the only door.

3. **The gate is often only on the proxy.** Some hosts put auth (a login page /
   basic-auth) on the proxied port but leave the native port ungated, so a host
   that looks protected on :443 is open on :11434. For the survey this means
   "gated on the proxy" is not the same as "gated": the true exposure is the
   least-protected vector.

4. **vhost auth keyed by hostname, not address.** Some nginx / apache configs
   apply auth inside a *name-based* virtual host. A request that arrives as
   `somesite.net` matches that vhost and is served the login page; a request to
   the bare IP `1.2.3.4` matches no vhost, falls through to the default server,
   and is served the backend without the gate. The protection is bound to the
   hostname, not the address.

**Implication for the survey:** exposure has to be assessed per *vector*
(address × port × `Host` header), not per host. One machine can present as
"gated" on one vector and "open" on another; record the vector, and let the
woahllama analysis reason over the least-protected one. None of this authorises
acting on a gated vector — it only makes the survey's exposure accounting honest.

### Recording how a host was reached: the `method` contract

The frozen, forever data-model commitment for recording *how* access was obtained
is a single open, extensible shape:

```json
"method": { "strategy": "<uniquename>", "params": { <custom> } }
```

Rules:

- **Absent when it just works.** A host reached the plain/default way (its own
  address on its own port, nothing in the way) carries **no `method` key**. The
  absence *is* the signal.
- **Present only when a strategy was used** that the default wouldn't have
  achieved. `strategy` is an **open vocabulary** (a short unique name); `params`
  is a **strategy-specific bag** — its fields may differ per strategy.
- Because the shape never changes, new strategies added months from now slot in
  with no migration, and historical data stays analysable with a simple
  `GROUP BY method.strategy`.

The first strategy is **`directip`** — access obtained via the IP address where a
name-based (vhost) gate on the hostname would otherwise have blocked it (see
field-note #4). Established as not penetration testing: the IP is already in the
survey result and an ordinary request to it circumvents nothing. Its `params`
carry the specifics (e.g. the source hostname and the ip/port that answered).
More strategies are expected; the contract exists to leave room for them.

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `FOFA_COOKIE must be set in .env for the web method` | The web fetch method requires a browser cookie to authenticate with FOFA's web interface. | Log into [fofa.info](https://en.fofa.info), open DevTools > Network, copy the `Cookie` header from any request, and set it as `FOFA_COOKIE` in `.env`. |
| `FOFA access denied — IP flagged as a web crawler` | FOFA has rate-limited or blocked your IP. The response contains `[-3000] IP access is abnormal`. | Wait a while, switch IPs (VPN/proxy), or try again later. This is a fatal error — graflex will not retry. |
| `daily usage limit hit` | Free tier FOFA accounts are limited to 3000 queries/day. | Resume later with `--id <run_ts>` from the error message. |
| `ZOOMEYE_COOKIE must be set in .env for --site zoomeye` | `-t zoomeye` needs your zoomeye.ai session cookie. | Log into [zoomeye.ai](https://www.zoomeye.ai), copy the whole `Cookie` header (DevTools > Network, or a `curl -b` invocation), and set it as `ZOOMEYE_COOKIE` in `.env`. It's a session token that expires — refresh it when zoomeye starts failing. |
| `zoomeye: stopping — …` | An HTTP error or unparsable body from zoomeye.ai — often an expired cookie (401/403) or a page past the ~250 cap. | If the cookie expired, refresh `ZOOMEYE_COOKIE`. Otherwise it's the normal end of a query's results. |
