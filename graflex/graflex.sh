#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:-ollama}"

case "$SERVICE" in
  ollama)
    QUERY="body='ollama is running'"
    PORTS="11434,8080,80,443,8983"
    SERVERS="nginx,cloudflare,Apache"
    COUNTRIES="US,AU,IN,JP,DE,CA,BR,CN"
    ;;
  comfyui)
    QUERY='title="ComfyUI"'
    PORTS="8188,8080,80,443"
    SERVERS="nginx,cloudflare"
    COUNTRIES="US,AU,IN,JP,DE,CA,BR,CN"
    ;;
  a1111)
    QUERY='icon_hash="2075038152" && body="Stable Diffusion"'
    PORTS="7860,8080,80,443"
    SERVERS="nginx,cloudflare"
    COUNTRIES="US,AU,IN,JP,DE,CA,BR,CN"
    ;;
  *)
    echo "Usage: $0 [ollama|comfyui|a1111]" >&2
    exit 1
    ;;
esac

exec python -m graflex -q "$QUERY" -a fetch -n "$SERVICE" -p "$PORTS" --servers "$SERVERS" -c "$COUNTRIES"
