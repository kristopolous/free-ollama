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
# API method (default)
graflex -a fetch-check
graflex -a fetch-check --dry

# Web method (paste Cookie header in FOFA_WEB_HEADER)
graflex -a fetch-check --method web
graflex -a fetch-check --method web --dry

# Just check existing hosts
graflex -a check

# Options
graflex -a fetch-check --limit 10
graflex -a fetch-check -s comfyui
graflex -a fetch-check -s a1111
graflex -a fetch-check -s comfyui a1111
```

The `fetch` step queries FOFA, saves results to `~/.cache/free-ollama/image-gen-hosts.json`,
and for the web method also dumps the raw HTML to `fofa-results-{timestamp}.html`.

The `check` step probes each host to find working GPUs and saves results to
`~/.cache/free-ollama/image-gen-working.json`.
