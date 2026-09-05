#!/usr/bin/env bash
#set -euo pipefail

SLEEP=10
SERVICE="${1:-ollama}"
CMD="$0"
shift 2>/dev/null || true
EXTRA_ARGS=("$@")
FID=
SITE=fofa
SERVERS=
source .env

case "$SERVICE" in
  all)
    for i in ollama ollama-shodan comfyui a1111 llama.cpp vllm lmstudio gradio; do
      echo "--- $i ----"
      $CMD $i ${EXTRA_ARGS[@]}
    done
    exit 0
    ;;

  combine)
    DATE=$(date +%Y%m%d)
    COMBINED_JSON=$(jq -s 'add' ~/.cache/free-ollama/*-working.json)
    echo "$COMBINED_JSON" | ssh $_SERVER "cat > $_SERVER_PATH"
    echo "$COMBINED_JSON" | ssh $_SERVER "cat > ${_SERVER_PATH%.json}-$DATE.json"
    source .venv/bin/activate

    for i in ~/.cache/free-ollama/*-notworking.json; do
      ./graflex.py -a enrich "$i" host
    done

    jq -s 'add' ~/.cache/free-ollama/*-notworking.json > ~/.cache/free-ollama/notworking-consolidated.json

    ./dyva.py --refresh $_SOURCE
    exit
    ;;


  ollama-shodan)
    SERVICE=ollama
    SITE=shodan
    PORTS="11434,9306,5172,5984,8500,50000"
    COUNTRIES="US,AU,JP,IN,CA,SG,IL,DE,BR,HK,ZA,CH,TH,IT"
    ;;

  ollama)
    PORTS="11434,10443,1025,9443,9200,8085,1024,1025,10001,10000,8888,9000,8080,8083,3000,16000,80,443,1194,110,8983,28017,21,5060,5601"
    SERVERS="nginx,cloudflare,Apache"
    COUNTRIES="TH,FI,MX,TR,IR,AR,TW,MA,MY,NZ,BH,SG,AT,KR,PE,GB,AE,US,NL,AU,IN,VN,JP,DE,CA,RU,IL,BR,BE,CN,IE,FR,UA,ES,ID,IT,CH,ZA,HK,PL"
    FID=$({ 
cat << ENDL 
    +J8tx/XRDAFbY7SwBCK/0A==
    01xWmwON//u1XKEyOkkEGw==
    3Q7By8tPfRuNHpFiq0KrqA==
    3mGLf50yvyPWCWfSET9hDw==
    3wJ8qv5ND0OwkVt/Kjq4iQ==
    4vh6LwKdiSJrB3Lrrkt3LA==
    65wLyEcpOlDyDW4X4g0gZw==
    AIB814ld1H6MvBd+6OPcCA==
    AeWKZ15RmMWZotdLMDDv4w==
    IPVd8GL22c99ET8M4xjbnw==
    IQHpmTiP8/HzRTww3q1YrQ==
    IddNyyDw+Bero+vJOQnxFQ==
    J9855+VtkRUZ4OjziYwE/g==
    KLZTw2V6Q8F48jRQERILuA==
    QtId0cK1hK63W7qg3BQGxg==
    RB7MvyQI2lfLDtfXp/oHFA==
    TrVIddAZ1noJcqqjy3K0tQ==
    UsjT+U+5J1db7DhwVOaRww==
    WG5T78I2x/yKvDq/9kLayA==
    WoOGi3QvmGKck8Zfv+/7Yg==
    Zqa7LMrwh7gjCP/4TXKPhw==
    aZKKpLCZfWu36hzRur+Vwg==
    gcg43SR+B8fEyFZyAZswOw==
    hct4Naj+LBOYmss/EnqFgg==
    hoN2c4Ox+oKTWIv5RyANJQ==
    jq1/FUEPBJ5UKh2LOcVWZQ==
    It1vVVEF3pCaBknoGd6c2g==
    nqM+fMcnax/qO2rEglJ72w==
    peMs7fD92YTW7k1jkyd9QQ==
    rI+VQgK8I9EBO92ugxTRhA==
    sMOJSZs5jy4YSnz10gFtxA==
    t03y23hXUHgle7AmLZLjmQ==
    t4uBaTpN5jh8BiX2XaAiqA==
    u2qfmUZoLDxXxJCFU5i6KQ==
    vWJAw3jTn47wQTBzzr6Y5A==
    yrIHyooS22dwg/KebDy5Uw==
    zCj6cvAuYJ+3bXm6Nt+tSA==
    /G4zpfxN3GZzxn5JSlEhkw==
ENDL
} | tr '\n' ',' | tr -d ' '
)
    ;;
    
  comfyui)
    QUERY='title="ComfyUI"'
    PORTS="9200,8983,9200,5060,8089,5601,28017,8188,8080,80,443"
    SERVERS="nginx,cloudflare,Python/3.12 aiohttp/3.11.16,Python/3.12 aiohttp/3.14.1,Python/3.12 aiohttp/3.13.3,Python/3.10 aiohttp/3.12.15,Python/3.13 aiohttp/3.13.3"
    COUNTRIES="US,AU,IN,JP,DE,IE,ID,IT,BH,IL,CA,BR,CN,KR,GB,FR,SG,CH,ZA,BR,HK,SE,ES"
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
    PORTS="8080,8000,8081,8001,8082"
    COUNTRIES="US,CN,IR,TW,DE,RU,FR,KR,PL,GB,HK,JP,NL,BR,VN,FI,IN,CA"
    FID="9fQvFf/gWpbFiiDwLkiXOw==,I2Yn1XFvazj+4lCwZ3R86w=="
    ;;

  vllm)
    PORTS="8080,80,443,8888,8001"
    COUNTRIES="US,CN,DE,RU,IN,FR,NL,SG,HK,JP,FI,BR,IE,VN,CA,AU,SE,ID,TW,IT,DE,KZ,TH,ES,PL,UA,TR,KR,FI,GB"
    ;;

  lmstudio)
    QUERY='body="Unexpected endpoint or method. (GET /)"'
    PORTS="1234,80,443,8080,8000,12345"
    SERVERS="nginx,nginx/1.24.0 (Ubuntu)"
    COUNTRIES="US,CN,TR,KR,RU,TH,RO,TW,DE,HK,JP,BR,VN,CA,FR,ES,IN,SG,CL,IT,NL,GB"
    ;;

  gradio)
    QUERY='icon_hash=="55115683"'
    SITE=fofa
    PORTS="80,443,8080,7860"
    COUNTRIES="US,CN,DE,IN,JP,KR,BR,GB,FR,HK,TW,CA,AU,RU,NL,SG,ID,VN,IT,ES"
    FID=$({ 
cat << ENDL 
CfOOPt6Nd3WtpgTJF1CZMQ==
SKGUqQuUlkehGS8jB/cz3w==
bkoVAuNuNwTuBfCjZ+d4xw==
sGe21936bIKF2zWmLyb7fQ==
t5OB7B8z43gJDAyGFPredQ==
zO99w44qU6me2LeJntB/xw==
N/Vkkdevw+ddMZQvyu4UHw==
ENDL
} | tr '\n' ',' | tr -d ' '
)

    ;;

  *)
    echo "Usage: $0 [ollama|comfyui|a1111|vllm|llama.cpp|lmstudio|gradio|combine]" >&2
    exit 1
    ;;
esac

fid=()
servers=()
query=()

[[ -n "$FID" ]] && fid=( "--fid" $FID )
[[ -n "$SERVERS" ]] && servers=( "--servers" "$SERVERS" )
[[ -n "$QUERY" ]] && query=( "--query" "$QUERY" )

# gradio has no --service entry; -n gradio alone selects the named query
svc_args=( --service "$SERVICE" )
[[ "$SERVICE" == "gradio" ]] && svc_args=()

set -x
exec ./graflex.py \
      --action "fetch-check" \
      --countries "$COUNTRIES" \
      "${EXTRA_ARGS[@]}" "${fid[@]}" \
      --ports "$PORTS" \
      --site  "$SITE" \
      "${query[@]}" --random \
      --name    "$SERVICE" "${servers[@]}" \
      "${svc_args[@]}" \
      --sleep   "$SLEEP" \
