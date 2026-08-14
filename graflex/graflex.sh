#!/usr/bin/env bash
#set -euo pipefail

SLEEP=10
SERVICE="${1:-ollama}"
shift 2>/dev/null || true
EXTRA_ARGS=("$@")
FID=
SERVERS=

case "$SERVICE" in
  combine)
    set -x
    cd ~/.cache/free-ollama
    jq -s 'add' *-working.json | ssh 9ol.es "cat > www/graflex.json"
    curl -s 9ol.es/graflex.json > ~/.cache/free-ollama/free-ollama.json-graflex.tmp
    exit
    ;;

  ollama)
    QUERY="body='ollama is running'"
    PORTS="11434,10443,8085,10001,8080,80,443,1194,110,8983,28017,21,5060,5601"
    #SERVERS="nginx,cloudflare,Apache"
    COUNTRIES="TH,MX,MY,NZ,BH,SG,KR,GB,AE,US,AU,IN,JP,DE,CA,RU,IL,BR,CN,IE,FR,ES,ID,IT,CH,ZA,HK,PL"
    #FID="UsjT+U+5J1db7DhwVOaRww==,gcg43SR+B8fEyFZyAZswOw==,WG5T78I2x/yKvDq/9kLayA==" #,vWJAw3jTn47wQTBzzr6Y5A==" #,IddNyyDw+Bero+vJOQnxFQ=="

    ;;
    
  comfyui)
    QUERY='title="ComfyUI"'
    PORTS="8188,8080,80,443"
    SERVERS="nginx,cloudflare"
    COUNTRIES="US,AU,IN,JP,DE,CA,BR,CN"
    FID="2zn7oqmRiwaUu3+PzyTjvw==,7aBY0X9WxdeghtrJGx1MEQ==,MJ7K0wma6lKOVne5ksgrSw==,yhjkkd4AnCsogP9Ms1QgVA=="
    ;;

  a1111)
    QUERY='icon_hash="2075038152" && body="Stable Diffusion"'
    PORTS="7860,7861,8080,80,443,10000"
    #SERVERS="nginx,cloudflare,uvicorn"
    COUNTRIES="TH,MX,MY,NZ,BH,SG,KR,GB,AE,US,AU,IN,JP,DE,CA,RU,IL,BR,CN,IE,FR,ES,ID,IT,CH,ZA,HK,PL"
    #FID="4OeA79EXS7Z+DdzkAvrBag==,WPcuJSTXuzZQIeov/h9jgA==,NCPjfTODiuNabsua2LTY7Q==,SWCeWGsQp4gTPi4YvgrIdQ=="
    ;;

  llama.cpp)
    QUERY='server=="llama.cpp"'
    PORTS="8080,8000,8081,8001,8082"
    COUNTRIES="US,CN,IR,DE,RU,FR,KR,PL,FI,IN,CA"
    FID="9fQvFf/gWpbFiiDwLkiXOw==,I2Yn1XFvazj+4lCwZ3R86w=="
    ;;

  *)
    echo "Usage: $0 [ollama|comfyui|a1111|llama.cpp|combine]" >&2
    exit 1
    ;;
esac

fid=()
servers=()

[[ -n "$FID" ]] && fid=( "--fid" $FID )
[[ -n "$SERVERS" ]] && servers=( "--servers" $SERVERS )

set -x

exec ./graflex.py \
      --action "fetch" \
      --countries "$COUNTRIES" \
      "${EXTRA_ARGS[@]}" "${fid[@]}" \
      --ports "$PORTS" \
      --query "$QUERY" \
      --random \
      --name    "$SERVICE" "${servers[@]}" \
      --service "$SERVICE" \
      --sleep   "$SLEEP" \
