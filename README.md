# free-ollama  
*Because paying for cloud GPUs is for chumps with self-respect.*  

---

## What Is This?

Have you ever wanted an **unreliable**, **ethically-questionable**, and **gloriously free** way to get tokens on ~900 low-end models?  
How about running **135m smollm2** or **270m gemma3** on someone else's server?  
Now you can. Let’s not ask too many questions.

---

## Features (Or: “What Does This Even Do?”)

- **Server Discovery**: Automatically scrapes a list of public Ollama servers (some may or may not be running Windows XP).  
- **Model Filtering**: Find that one server that *claims* to have `mario:latest` (spoiler: it doesn’t).  
- **Performance Sorting**: Sort by TPS so you can choose the *least* slow server.  
- **Health Testing**: Optional `llcat` probe to see if the server is actually alive or just a ghost in the shell.  
- **Zero-Config**: Works until it doesn’t. Caching means you can pretend the internet is fast.

---

## Dependencies

- `bash` (yes, that one)  
- `jq` (for JSON, because JSON is everywhere)  
- `curl` (to fetch things you don’t own)  
- `find` (because why not)  
- [`llcat`](https://github.com/day50-dev/llcat/) (for the “test” command, if you’re into that)

---

## Quick Start

### 1. See what models are floating out there
```bash
./free-ollama
```
*Outputs a sorted list of models by how often they appear in the wild. Spoiler: `smollm2:135m` is shockingly common.*

### 2. Find servers with a specific model (good luck)
```bash
./free-ollama gemma3:latest
```
*Lists all servers offering `gemma3:latest`, sorted by TPS. Fastest might be 0.1 TPS. Enjoy.*

### 3. Pick the “top” performers by index
```bash
./free-ollama gemma3:latest {0..10}
```
*Shows the 11 fastest servers (indices 0–10). “Fastest” is a strong word.*

### 4. Find servers with *multiple* models (unicorn hunting)
```bash
./free-ollama llama2:13b codellama:7b {0..5}
```
*Finds servers offering *both* models. Top 6 results will probably be 6 lies.*

---

## Output Format

Default output is **space-delimited** so you can pipe it into your bash graveyard:
```
<tps> <server-address> <model1> <model2> ...
```
Example:
```
42 http://34.120.89.11:11434 gemma3:latest
128 http://15.164.98.22:11434 llama2:13b codellama:7b
```

Yes, TPS is transactions per second. No, it’s not *real* TPS. It’s a guess.

---

## Pipeline Integration (For the Bash Masochists)

```bash
# Get top 10 servers with phi3, extract IPs only
./free-ollama phi3:mini {0..9} | awk '{print $2}' > server-list.txt
# Now you have a list of IPs that may or may not work tomorrow. Cool.

# Build a Redis server pool (requires redis-cli, because why not)
./free-ollama mistral:7b {0..20} | \
  sort -k1,1nr | \
  cut -d' ' -f2 | \
  xargs -I {} redis-cli rpush server-pool "{}"
# You’re welcome, production.

# Sort by TPS descending (lol) and pick server+model pairs
./free-ollama llama2:7b | sort -k1,1nr | cut -d' ' -f2-3
```

---

## Testing Servers (Because Trust Is Overrated)

```bash
# Test all servers with a specific model
./free-ollama test qwen3
```
Bad host/model pairs get stored in `~/.cache/free-ollama-bad-hosts.txt` and filtered out later (until they magically work again).

**Testing output:**
```
2.34s 34.120.89.11:11434 gemma3:latest
1.87s 15.164.98.22:11434 llama2:13b codellama:7b
Bad -- 192.168.1.5:11434 phi3:mini
```
*“Bad --” means either the server is dead, the model isn’t there, or it hates you personally.*

---

## Advanced Usage

### Custom index selection
```bash
# Non-sequential indices (because you’re special)
./free-ollama mistral:7b 2 5 7 9

# Range expansion (Bash brace expansion)
./free-ollama llama2:13b {5..15..2}   # Every other from 5 to 15
```

### Combining with parallel tools (why suffer alone?)
```bash
# Using parallel (GNU parallel)
./free-ollama codellama {0..50} | parallel -j4 ./test-server.sh

# Using xpanes for multi-pane testing (look busy)
./free-ollama glm-4.7-flash:q4_K_M {0..9} | xpanes -c "./test-and-log.sh {}"
```

---

## Cache Management

- **Cache location**: `~/.cache/free-ollama.json` (your digital hoard)
- **Auto-refresh**: Every 24 hours (1440 minutes of hope)
- **Force refresh**: Delete the cache file
```bash
rm ~/.cache/free-ollama.json
./free-ollama  # Will fetch fresh data (maybe)
```

---

## Data Source

The server list is curated from [awesome-ollama-server](https://awesome-ollama-server.vercel.app/) (public community-maintained list of “hey look at my server!”).  
Data includes:
- Server address (might be a toaster)
- TPS rating (a prayer)
- Available models (lies, all lies)
- Last seen timestamp (“last Tuesday?”)

---

## Troubleshooting (Or: “Why Is Everything Broken?”)

### “Updating cache...” appears repeatedly
- Check network connectivity (are you behind a corporate firewall that hates fun?)
- Verify `curl` is installed and not `curl.exe` from 1998
- Check if the data source is available: `curl -I https://awesome-ollama-server.vercel.app/data.json` (it’s probably down)

### No servers found for a model
- The model name might be different (try `gemma` instead of `gemma3:latest`)
- No servers in the current list offer that model (shockingly rare)
- Try a broader search: `./free-ollama llama` (partial match—because precision is for the weak)

### Testing fails with “Bad --”
- Server may be offline, or its owner finally noticed.
- Firewall restrictions (common with cloud providers who hate free riders like you).
- Model not actually available despite being listed (data is stale, imagine that).
- Check `~/.cache/free-ollama-bad-hosts.txt` for your personal hall of shame.

---

## Disclaimer (Do We Need This?)

Oh I shouldn’t have to say anything here. But lawyers are boring:  
This tool scrapes public lists. Some servers may not want to be scraped. Some may collapse under your query. Some may log your IP and report you to authorities. So go do it at McDonalds.
**Use responsibly. Or don’t. I’m not your mom.**

---

## License

MIT License. See [LICENSE](LICENSE) file.  
*Because even questionable tools deserve a permissive license.*

---

## Current Model Hall of Shame / Fame

Based on actual data (yes, really):

```
95  gemma3:1b
96  deepseek-r1:7b
102 deepseek-v3.1:671b-cloud   (sure, Jan)
103 deepseek-r1:latest
103 llama3-backup:latest       (backup to what?)
116 llama3.1:8b-instruct-q4_K_M
122 llama3:latest
129 gpt-oss:120b               (ah, the 120b “open” weights. Totally.)
140 mattw/pygmalion:latest     (roleplay server? I’m shocked.)
148 mistral:latest
155 mario:latest               (plumber or mushroom?)
164 gemma3:latest
180 llama3.2:3b-instruct-q5_K_M
196 lukashabtoch/plutotext-r3-emotional:latest   (emotional model? Cute.)
209 nomic-embed-text:latest
223 gemma3:270m               (the tiny one you wanted!)
238 deepseek-r1:1.5b
248 llama3.1:8b
315 llama3.2:latest
1142 smollm2:135m             (the champion of tiny models!)
```

Yes, `smollm2:135m` appears **1142 times**. That’s a lot of tiny models. Have fun.
