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
| `GET` | `/dashboard` | Dashboard UI |
| `GET` | `/clear-bad` | Clear failed host+model pairs |
| `GET` | `/refresh` | Re-fetch server lists from all sources |

## Server Discovery

dyva aggregates servers from three sources on startup (and every 24h):

1. [forrany/Awesome-Ollama-Server](https://github.com/forrany/Awesome-Ollama-Server)
2. [PuddinCat/OllamaSpider](https://github.com/PuddinCat/OllamaSpider)
3. [happyshua/ollamalist](https://github.com/happyshua/ollamalist)

Cache files live in `~/.cache/free-ollama/`.
