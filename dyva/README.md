# dyva

OpenAI-compatible proxy that routes to free Ollama servers, plus a Stable Diffusion txt2img proxy.

## Setup

```bash
pip install -e .
```

## Usage

```bash
dyva -p 8080
```

### Ollama API

Any OpenAI or Ollama chat client works:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3", "messages": [{"role": "user", "content": "hello"}]}'

curl http://localhost:8080/api/chat \
  -d '{"model": "llama3", "messages": [{"role": "user", "content": "hello"}]}'
```

### Stable Diffusion (txt2img)

Proxies to discovered A1111 hosts from `~/.cache/free-ollama/image-gen-working.json`:

```bash
curl http://localhost:8080/sdapi/v1/txt2img \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a cat", "steps": 20}'
```

Response contains base64-encoded images under the `images` key, matching the AUTOMATIC1111 API format.

### Dashboard

Open `http://localhost:8080/` in a browser.
