#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:-ollama}"
shift 2>/dev/null || true
EXTRA_ARGS=("$@")

case "$SERVICE" in
  ollama)
    QUERY="body='ollama is running'"
    PORTS="11434,10443,8085,10001,8080,80,443,1194,110,8983,28017,21,5060,5601"
    SERVERS="nginx,cloudflare,Apache"
    COUNTRIES="TH,MX,MY,BH,SG,KR,GB,AE,US,AU,IN,JP,DE,CA,RU,IL,BR,CN,IE,FR,ES,ID,IT,CH,ZA,HK,PL"
    FID="UsjT+U+5J1db7DhwVOaRww==,gcg43SR+B8fEyFZyAZswOw==,WG5T78I2x/yKvDq/9kLayA==,vWJAw3jTn47wQTBzzr6Y5A==,IddNyyDw+Bero+vJOQnxFQ=="
    ;;
    
  comfyui)
    QUERY='title="ComfyUI"'
    PORTS="8188,8080,80,443"
    SERVERS="nginx,cloudflare"
    COUNTRIES="US,AU,IN,JP,DE,CA,BR,CN"
    FID="2zn7oqmRiwaUu3+PzyTjvw==,7aBY0X9WxdeghtrJGx1MEQ==,MJ7K0wma6lKOVne5ksgrSw==,yhjkkd4AnCsogP9Ms1QgVA==
"
    ;;

  a1111)
    QUERY='icon_hash="2075038152" && body="Stable Diffusion"'
    PORTS="7860,7861,8080,80,443,10000"
    SERVERS="nginx,cloudflare,uvicorn"
    COUNTRIES="AE,US,AU,IN,JP,DE,ES,RU,CA,BR,CN,KR"
    FID="4OeA79EXS7Z+DdzkAvrBag==,WPcuJSTXuzZQIeov/h9jgA==,NCPjfTODiuNabsua2LTY7Q==,SWCeWGsQp4gTPi4YvgrIdQ=="
    ;;
  *)
    echo "Usage: $0 [ollama|comfyui|a1111]" >&2
    exit 1
    ;;
esac

exec ./graflex.py -q "$QUERY" -f "$FID" -a fetch -n "$SERVICE" -p "$PORTS" --servers "$SERVERS" -c "$COUNTRIES" "${EXTRA_ARGS[@]}"
