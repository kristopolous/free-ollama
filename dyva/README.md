# Dumpster Dyva

OpenAI and Ollama-compatible proxy that routes inference to insecure Ollama, vllm, llama.cpp, A1111, and ComfyUI hosts.

Compatible enough that the real Ollama CLI thinks it's talking to a real Ollama server — see [below](#use-it-like-ollama).

Complete with even a little chat thingy. Look at the thingy!
<img alt="towers" src="https://github.com/user-attachments/assets/a169c629-71fd-44f3-9815-8047a43109d9" />

The chat thingy features these jazzy little videos instead of a spinner. Here's all 10 of them together. There's audio. Maybe there shouldn't be.


https://github.com/user-attachments/assets/c9874c72-52ea-4060-a078-d2945add22d6



"Now now now" you say, from your VC office, "what about mobile?!"

Here it is. Running ON MY ACTUAL FUCKING PHONE! (*gasp*)

<img alt="66311" src="https://github.com/user-attachments/assets/d309c8f1-0634-48eb-96e4-18a7a7123d6e" />


## CLI Options

| Flag | Description |
|------|-------------|
| `-p`, `--port` | Port to listen on (default: 11434) |
| `-u`, `--host` | Host address to bind (default: all interfaces) |
| `-t`, `--timeout` | Request timeout seconds (default: 30) |
| `-w`, `--workers` | Concurrent workers (default: 3) |
| `-l`, `--local` | Restrict inference endpoints to localhost only |
| `-r`, `--refresh` | Refresh server cache and exit; optionally name a single source (e.g. `--refresh graflex`) — other sources keep their last-fetched data |
| `--curlify` | Print `curl` commands of upstream requests to stderr |
| `-v`, `--version` | Show version |

## Use It Like Ollama

Point the official Ollama CLI at dyva and the everyday commands just work — except instead of one machine's models you see everything the swarm has to offer:

```bash
export OLLAMA_HOST=http://127.0.0.1:11434   # wherever dyva runs

ollama ls            # every model across all discovered hosts
ollama ps            # models in use and where they last ran
ollama show llama3   # metadata for any model
ollama run llama3    # chat straight from your terminal
```

`ollama run` streams through the same racing/failover machinery as every other endpoint, so if a host dies mid-sentence the next request lands somewhere else.

Notes:
- `ollama pull` doesn't download anything (models live on remote hosts) — it just refreshes dyva's server cache.
- `ollama cp` / `ollama rm` are not supported.

## API Reference

See the Swagger docs at `/docs` on a running instance for the full API reference.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat (streaming + tool calls) |
| `POST` | `/api/chat` | Native Ollama chat |
| `POST` | `/api/generate` | Native Ollama generate |
| `POST` | `/api/show` | Model metadata |
| `POST` | `/api/pull` | Refresh server cache |
| `GET` | `/api/tags` | List available models |
| `GET` | `/api/ps` | Last-used models |
| `GET` | `/api/version` | Version info |
| `GET` | `/api/activity` | SSE stream of real-time proxy activity |
| `GET` | `/v1/models` | OpenAI-compatible model listing |
| `POST` | `/sdapi/v1/txt2img` | Text-to-image (A1111 + ComfyUI fallback) |
| `GET` | `/sdapi/v1/sd-models` | List discovered SD models |
| `GET` | `/sdapi/v1/images` | Metadata for recently generated images (last 100) |
| `GET` | `/sdapi/v1/images/{name}` | Fetch a generated image file |
| `*` | `/comfyui/{path}` | ComfyUI pass-through proxy |
| `GET` | `/dashboard` | Dashboard UI (Server Room / Chat / Image tabs) |
| `GET` | `/dashboard-data` | JSON snapshot of the last-successful/good/bad lists |
| `GET` | `/clear-bad` | Clear failed host+model pairs |
| `GET` | `/next-host` | Remove a model from the last-successful list |
| `GET` | `/skip-good` | Move a good host+model pair into the bad list |
| `GET` | `/refresh` | Re-fetch server lists from all sources |

#### Routing Probe

Include `__dyva_info__` anywhere in the prompt or messages and dyva won't run inference — instead it returns `{"host": ..., "model": ...}` for the host/model the request *would* have been routed to (last-used host if still eligible, else the top of the race queue):

```bash
curl http://localhost:11434/api/chat -d '{
  "model": "qwen",
  "stream": false,
  "messages": [{"role": "user", "content": "__dyva_info__"}]
}'
```

With `"stream": true` the same info arrives as a normal NDJSON/SSE chat stream — payload in `message.content`, plus a top-level `dyva_info` object on the final chunk.

Use `__dyva_info__:next` to move on: it first drops the model's sticky last-successful host (the same action as `GET /next-host` and the dashboard's "next host" link), then returns the *next* host/model the request would land on. The response shape is identical, so you get confirmation of where you moved to — repeat to keep walking down the list. Model matching is case-insensitive and glob-normalized, so `GEMMA*`, `gemma`, and `*gemma*` all target the same sticky host.

### Text-to-Image

The `txt2img` CLI generates images via the proxy:

```bash
txt2img "a sunset over mountains" -o output.png
txt2img --width 1024 --height 768 --steps 20 "cyberpunk city"
txt2img -m                  # list available SD models
txt2img -m "sd_v15" "a cat" # use a specific model
```

| Flag | Description | Default |
|------|-------------|---------|
| `--host` | dyva proxy host | `127.0.0.1` |
| `--port` | dyva proxy port | `11434` |
| `--width` | Image width | `512` |
| `--height` | Image height | `512` |
| `--steps` | Sampling steps | `30` |
| `-m`, `--model` | SD model name (use without value to list) | auto |
| `-o`, `--output` | Output file (default: stdout as PNG) | — |

The proxy first tries A1111 hosts, then falls back to ComfyUI hosts with a basic txt2img workflow.

#### Library

`dyva.imagegen` provides a reusable async/sync function:

```python
from dyva.imagegen import imagegen, imagegen_sync

# async
img_bytes = await imagegen(prompt="a sunset", width=1024, height=768)

# sync
img_bytes = imagegen_sync(prompt="a cat in a spacesuit")
```

Returns raw PNG bytes. All parameters are keyword-only:

| Param | Default | Description |
|-------|---------|-------------|
| `prompt` | *(required)* | Text prompt |
| `model` | `None` | SD model name to match |
| `width` | `512` | Image width |
| `height` | `512` | Image height |
| `steps` | `30` | Sampling steps |
| `cfg_scale` | `7` | CFG scale |
| `sampler_name` | `"Euler"` | Sampler |
| `negative_prompt` | `""` | Negative prompt |
| `seed` | `-1` | Seed (-1 = random) |
| `host` | `"127.0.0.1"` | dyva proxy host |
| `port` | `11434` | dyva proxy port |

### ComfyUI Pass-Through

All ComfyUI workflow endpoints are proxied under `/comfyui/`. The prefix is stripped and the request is forwarded to a discovered ComfyUI host.

```bash
# Submit a workflow
curl -X POST http://localhost:11434/comfyui/prompt \
  -H 'Content-Type: application/json' \
  -d '{"prompt": {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}}}}'

# Check queue
curl http://localhost:11434/comfyui/queue

# Get history for a prompt
curl http://localhost:11434/comfyui/history/{prompt_id}

# Retrieve output image
curl "http://localhost:11434/comfyui/view?filename=output.png&type=output"

# List available checkpoints
curl http://localhost:11434/comfyui/models/checkpoints

# Server stats
curl http://localhost:11434/comfyui/system_stats
```

Target a specific host with `?host=ip:port`:

```bash
curl "http://localhost:11434/comfyui/queue?host=1.2.3.4:8188"
```

## Settings

The dashboard's **Settings** tab is backed by `~/.cache/free-ollama/settings.json`. These persist across restarts and override the CLI defaults:

| Setting | Meaning |
|---------|---------|
| **Workers** | Parallel hosts raced per request (fan-out). |
| **Timeout** | Per-host request timeout, in seconds. |
| **Minimum model count** | Hide models served by fewer than this many hosts from `/api/tags` and `/v1/models` — the listings third-party apps read. `0` = show everything. (The dashboard always shows everything.) |
| **Admin password** | When set, viewing or changing the Settings tab and its sources requires it (sent as an `X-Admin-Key` header). Everything else — chat, models, the dashboard — stays public, so you can host a demo without letting visitors edit your config. Stored hashed; if you forget it, clear `admin_pw` in `settings.json` and restart. |
| **Additional sources** | Extra host lists to pull from — see below. |

Saving after you change the source list re-pulls the cache.

## Server Discovery

dyva aggregates servers from several built-in sources plus any you add. The tool [graflex](../graflex) is a from-scratch general-purpose aggregator, and **its output is dyva's native host format** — so a graflex-produced JSON list imports with a straight one-to-one field mapping.

### Additional sources (third-party formats)

Under **Settings → Additional sources** you add your own JSON host lists. They're fetched *before* the built-ins, so they win on duplicate hosts. Each source is:

```json
{
  "name": "my-list",
  "url": "https://example.com/hosts.json",
  "mapping": {
    "server":  {"field": "url"},
    "models":  {"field": "models"},
    "service": {"field": "service"},
    "version": {"value": ""}
  }
}
```

- **`url`** points at a JSON *array* of host rows. A bare `host/path.json` implies `https://`. (JSON-list sources only — CSV / transform-heavy lists stay built-in.)
- **`mapping`** turns each raw row into a dyva host entry. Each target field is either:
  - `{"field": "x"}` — copy `row["x"]`, or
  - `{"value": c}` — a constant for every row.
- Map at least **`server`** (the host — e.g. from a `url` field) and **`models`** (a list of model-name strings).
- **`service`** defaults to `ollama` when you don't map it (or when a row lacks the mapped field). Set it to `a1111` or `comfyui` for image hosts so they classify under the dashboard's Image tab and feed txt2img. A missing field falls back to its default rather than becoming null.

Because graflex already emits rows with `service`, `url`, `models`, and `checked`, importing a graflex list is just `server ← url`, `models ← models`, `service ← service` — no transforms needed. The **Add source**, **Add from url**, and **Test sources** buttons on the Settings tab help build and sanity-check a mapping (Test reports how many hosts/models each source yields, and prints a row's keys when a mapping matches nothing).
