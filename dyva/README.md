# dyva

OpenAI-compatible proxy that routes inference to free Ollama servers, A1111 hosts, and ComfyUI hosts.

## Quickstart

```bash
pip install -e .
dyva -p 8080
```

## CLI Options

| Flag | Description |
|------|-------------|
| `-p`, `--port` | Port to listen on (default: 8080) |
| `-u`, `--host` | Host address to bind (default: all interfaces) |
| `-t`, `--timeout` | Request timeout seconds (default: 30) |
| `-w`, `--workers` | Concurrent workers (default: 3) |
| `-l`, `--local` | Restrict inference endpoints to localhost only |
| `-r`, `--refresh` | Refresh server cache from all sources and exit |
| `--curlify` | Print `curl` commands of upstream requests to stderr |
| `-v`, `--version` | Show version |

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
| `*` | `/comfyui/{path}` | ComfyUI pass-through proxy |
| `GET` | `/dashboard` | Dashboard UI |
| `GET` | `/clear-bad` | Clear failed host+model pairs |
| `GET` | `/next-host` | Remove a model from the last-successful list |
| `GET` | `/skip-good` | Move a good host+model pair into the bad list |
| `GET` | `/refresh` | Re-fetch server lists from all sources |

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
