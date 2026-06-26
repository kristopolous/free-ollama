# graflex

Discover public image-generation servers via FOFA.

## Setup

```bash
pip install -e .
```

Create a `.env` file:

```env
# For API method (free tier doesn't allow this)
FOFA_KEY=your_fofa_api_key

# For web method — paste the full Cookie header from your browser
# while logged into en.fofa.info
FOFA_WEB_HEADER="fofa_theme=dark; fofa_token=...; fofa_result_page_size=50; ..."
```

## Usage

```bash
# API method
graflex -s a1111 -a fetch-check

# Web method (paste Cookie header in FOFA_WEB_HEADER)
graflex -s a1111 -a fetch-check --method web

# Dry run — shows plaintext queries instead of hitting FOFA
graflex -s a1111 -a fetch -d

# Custom FOFA query with named cache files
graflex -a fetch -q 'body="ollama"' -n ollama -p 11434

# Override the country/port/server cycling axes
graflex -s a1111 -a fetch -d --countries 'DE,FR,GB' -p '8080,9090' --servers 'apache,nginx'

# Just check existing hosts
graflex -s a1111 -a check

# Options
graflex -s a1111 -a fetch-check --limit 10
graflex -s comfyui -a fetch-check
```

The `fetch` step queries FOFA, cycling through combinations of country, port, and server
to maximize results. Results are saved to `~/.cache/free-ollama/{name}-hosts.json`
(default: `image-gen-hosts.json`).

The `check` step probes each host to find working endpoints and saves results to
`~/.cache/free-ollama/{name}-working.json` (default: `image-gen-working.json`).

### Options

| Flag | Description |
|------|-------------|
| `-s`, `--service` | Service to search for (`a1111` or `comfyui`) |
| `-a`, `--action` | Action: `fetch`, `check`, or `fetch-check` |
| `-d`, `--dry` | Print what would be done without making requests |
| `-l`, `--limit` | Max results per query (default: 2) |
| `-m`, `--method` | Fetch method: `api` or `web` (default: web) |
| `-q`, `--query` | Custom FOFA query (requires `--name`) |
| `-n`, `--name` | Cache file name prefix (requires `--query`) |
| `-c`, `--countries` | Comma-separated country codes to cycle |
| `-p`, `--ports` | Comma-separated port values to cycle |
| `--servers` | Comma-separated server values to cycle |

Each cycling axis (`--countries`, `--ports`, `--servers`) automatically prepends an
unfiltered `None` entry so queries without that filter are also tried.
