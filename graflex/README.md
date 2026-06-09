# graflex

Discover public image-generation servers via FOFA.

## Setup

```bash
pip install -e .
```

Create a `.env` file:

```env
# For API strategy (free tier doesn't allow this)
FOFA_KEY=your_fofa_api_key

# For web strategy — paste the full Cookie header from your browser
# while logged into en.fofa.info
FOFA_WEB_HEADER="fofa_theme=dark; fofa_token=...; fofa_result_page_size=50; ..."
```

## Usage

```bash
# API strategy (default)
graflex fetch-check
graflex fetch-check --dry

# Web strategy (paste Cookie header in FOFA_WEB_HEADER)
graflex fetch-check --strategy web
graflex fetch-check --strategy web --dry

# Options
graflex fetch-check --limit 10
graflex fetch-check --service comfyui
graflex fetch-check --service a1111
graflex fetch-check --service comfyui a1111
```

The `fetch` step queries FOFA, saves results to `~/.cache/free-ollama/image-gen-hosts.json`,
and for the web strategy also dumps the raw HTML to `fofa-results-{timestamp}.html`.

The `check` step probes each host to find working GPUs and saves results to
`~/.cache/free-ollama/image-gen-working.json`.
