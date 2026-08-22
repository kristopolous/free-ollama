# Dumpster Dyva

OpenAI and Ollama-compatible proxy that routes inference to insecure Ollama, vllm, llama.cpp, A1111, and ComfyUI hosts.

Compatible enough that the real Ollama CLI thinks it's talking to a real Ollama server — see [below](#use-it-like-ollama).

Complete with even a little chat thingy. Look at the thingy!
<img width="930" alt="dumpster" src="https://github.com/user-attachments/assets/162b7938-1284-446a-b99e-f56acb895706" />

"Now now now" you say, from your VC office, "what about mobile?!"

Here it is. Running ON MY ACTUAL FUCKING PHONE! (*gasp*)

<img width="384" alt="Screenshot_20260812-005403" src="https://github.com/user-attachments/assets/4dcc41f7-caab-40d1-a00c-23614f396302" />

## CLI Options

| Flag | Description |
|------|-------------|
| `-p`, `--port` | Port to listen on (default: 11434) |
| `-u`, `--host` | Host address to bind (default: all interfaces) |
| `-t`, `--timeout` | Request timeout seconds (default: 30) |
| `-w`, `--workers` | Concurrent workers (default: 3) |
| `-l`, `--local` | Restrict inference endpoints to localhost only |
| `-r`, `--refresh` | Refresh server cache from all sources and exit |
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

## Server Discovery

dyva aggregates servers from multiple sources. The tool [graflex](../graflex) is a from scratch general purpose aggregator that constitutes one of these sources.
