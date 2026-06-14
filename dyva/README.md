# dyva

OpenAI-compatible, comfyui, ollama-compatible, and a1111-compatible proxy that routes inference to free Ollama servers, and A1111 hosts.

## Quickstart

```bash
pip install -e .
dyva -p 8080
```

---

## API Reference

For the latest there's a swagger document on `/docs` of the running instance 

### `POST /v1/chat/completions`

OpenAI-compatible chat completions. Supports streaming and non-streaming.

**Purpose:** Drop-in replacement for OpenAI's chat endpoint. Use any OpenAI SDK/client by pointing the base URL at dyva.

**Request body:**

```json
{
  "model": "llama3",
  "messages": [{"role": "user", "content": "hello"}],
  "stream": false,
  "max_tokens": 100,
  "temperature": 0.7,
  "top_p": 0.9
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | — | Model name to query (required) |
| `messages` | array | — | Chat messages in OpenAI format |
| `stream` | bool | `false` | Enable SSE streaming |
| `max_tokens` | int | — | Maps to Ollama `num_predict` |
| `temperature` | float | — | Sampling temperature |
| `top_p` | float | — | Nucleus sampling parameter |

**Response (non-streaming):**

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "choices": [{"message": {"role": "assistant", "content": "..."}}],
  "model": "llama3"
}
```

**Response (streaming):** SSE stream of `data: {...}` chunks per the OpenAI SSE format.

---

### `POST /api/chat`

Native Ollama chat endpoint.

**Purpose:** Direct Ollama-compatible chat for clients that use Ollama's native API.

**Request body:**

```json
{
  "model": "llama3",
  "messages": [{"role": "user", "content": "hello"}],
  "stream": false
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | — | Model name to query (required) |
| `messages` | array | — | Chat messages |
| `stream` | bool | `false` | Enable NDJSON streaming |

**Response (non-streaming):** Ollama chat response JSON.

**Response (streaming):** NDJSON stream of Ollama response objects, one per line.

---

### `POST /api/generate`

Native Ollama generate endpoint (text completion, not chat).

**Purpose:** Direct Ollama-compatible text generation.

**Request body:**

```json
{
  "model": "llama3",
  "prompt": "Once upon a time",
  "stream": false
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | — | Model name to use (required) |
| `prompt` | string | — | Input text prompt |
| `stream` | bool | `false` | Enable NDJSON streaming |

**Response:** Ollama generate response JSON or NDJSON stream.

---

### `POST /sdapi/v1/txt2img`

Stable Diffusion text-to-image generation, proxied to discovered A1111 hosts.

**Purpose:** Generate images from text prompts using publicly accessible Stable Diffusion WebUI instances discovered via `graflex`.

**Prerequisite:** Run `graflex -a fetch-check --method web` to populate `~/.cache/free-ollama/image-gen-working.json` with working hosts.

**Request body:**

```json
{
  "prompt": "a cat wearing a hat",
  "negative_prompt": "ugly, blurry",
  "steps": 20,
  "width": 512,
  "height": 512,
  "seed": -1,
  "cfg_scale": 7,
  "sampler_name": "Euler a",
  "batch_size": 1,
  "n_iter": 1
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prompt` | string | — | Text prompt (required) |
| `negative_prompt` | string | `""` | Things to avoid |
| `steps` | int | `20` | Sampling steps |
| `width` | int | `512` | Image width |
| `height` | int | `512` | Image height |
| `seed` | int | `-1` | RNG seed (-1 = random) |
| `cfg_scale` | float | `7.0` | Classifier-free guidance scale |
| `sampler_name` | string | `"Euler a"` | Sampler algorithm |
| `batch_size` | int | `1` | Images per batch |
| `n_iter` | int | `1` | Number of batches |

**Response:**

```json
{
  "images": ["base64-encoded-png..."],
  "parameters": {},
  "info": "..."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `images` | array[string] | Base64-encoded PNG images |
| `parameters` | object | Echo of the request parameters |
| `info` | string | JSON-encoded generation metadata |

---

### `GET /api/tags`

List available models across all discovered Ollama servers.

**Response:**

```json
{
  "models": [
    {"name": "llama3:latest", "modified_at": "..."},
    ...
  ]
}
```

---

### `GET /v1/models`

OpenAI-compatible model listing.

**Response:**

```json
{
  "object": "list",
  "data": [
    {"id": "llama3", "object": "model"},
    ...
  ]
}
```

---

### `GET /`

Dashboard with real-time activity, model browser, good/bad host tracking, and discovered image-gen hosts.

---

## CLI Options

```
dyva -p, --port      Port to listen on         (default: 8080)
dyva -u, --host      Host address to bind      (default: all interfaces)
dyva -t, --timeout   Request timeout seconds   (default: 30)
dyva -w, --workers   Concurrent workers        (default: 3)
dyva -l, --local     Restrict inference endpoints (/api/chat, /api/generate,
                     /v1/chat/completions, /sdapi/v1/txt2img) to localhost
                     only. Dashboard and static files remain accessible
                     from any host.
dyva -v, --version   Show version
```
