<p align="center">
<img width="704" height="368" alt="smaller" src="https://github.com/user-attachments/assets/9f6d6c56-890e-4a03-9903-4f9903d5709d"/>
<br/>
  <br/><strong>Because paying for cloud GPUs is for chumps with self-respect.</strong>
</p>

---

Have you ever wanted **unreliable**, **ethically-questionable**, **unquestionably free** tokens for about a 1,000 useless models? 

Ever wanted to run **135m smollm2** or **270m gemma3** on someone else's 2016 era RTX 1080 Ti?

**Well, now you can!**

With **free-ollama** you can do:

- **Server Discovery**: Automatically scrapes a list of public Ollama servers 
- **Model Filtering**: Find what a server *claims* to have 
- **Performance Sorting**: Sort by TPS so you can choose the *least* slow server.  
- **Testing**: Optional [`llcat`](https://github.com/day50-dev/llcat) probe to see if the server picks up your calls.  
- **Zero-Config**: With caching! Works until it doesn’t.

Let’s not ask too many questions.

https://github.com/user-attachments/assets/b5b99780-2526-4ebc-ba23-2870d84a7516


## Go ahead, you try.

### 1. See what models are floating out there
```bash
./free-ollama
```
*Outputs a sorted list of models by how often they appear in the wild. Spoiler: `smollm2:135m` is shockingly common.*

### 2. Find servers with a specific model (good luck)
```bash
./free-ollama qwen3:latest
```
*Lists all servers offering `qwen3:latest`, sorted by TPS. Enjoy.*

### 3. Pick the “top” performers by index
```bash
./free-ollama qwen3:latest {0..10}
```
*Shows the 11 fastest servers (indices 0–10).*

### 4. It's actually a stack machine

Here's a stack of machines.

```bash
./free-ollama qwen3:latest {0..10} qwen2:1.5 {0..5}
```
*Finds the top 10 qwen3:latest and top 5 qwen2 not-so-latest*

---

## Output Format

```
<tps> <server-address> <model1> <model2> ...
```
Example:
```
42 http://34.120.89.11:11434 gemma3:latest
128 http://15.164.98.22:11434 llama2:13b codellama:7b
```

Yes, TPS is transactions per second. You can test it with test.

---

## Pipeline Integration 

```bash
# Get top 10 servers with glm-4.7-flash:q4_K_M, extract IPs only
./free-ollama glm-4.7-flash:q4_K_M {0..9} | awk '{print $2}' > server-list.txt
# Now you have a list of IPs that may or may not work tomorrow. Cool.

# Build a Redis server pool (requires redis-cli, because why not)
./free-ollama mistral:7b {0..20} | \
  sort -k1,1nr | \
  cut -d' ' -f2 | \
  xargs -I {} redis-cli rpush server-pool "{}"
# You’re welcome, production.

# Sort by TPS descending and pick server+model pairs
./free-ollama llama2:7b | sort -k1,1nr | cut -d' ' -f2-3
```

---

## Testing Servers

First install [`llcat`](https://github.com/day50-dev/llcat). It's awesome and also used in the testing.

```bash
# Test all servers with a specific model
./free-ollama test qwen3
```
Bad host/model pairs get stored in `~/.cache/free-ollama-bad-hosts.txt` and filtered out forever.

**Testing output:**
```
2.34s http://34.120.89.11:11434 gemma3:latest
1.87s http://15.164.98.22:11434 llama2:13b codellama:7b
Bad -- http://192.168.1.5:11434 phi3:mini
```
*“Bad --” means it's not working. Fancy that...*

---

## Advanced Usage

### Custom index selection
```bash
# Non-sequential indices (keeping it low-key)
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

- **Cache location**: `~/.cache/free-ollama.json` (every 24 hours)
- **Force refresh**: Built in, baby!
```bash
./free-ollama --refresh
```

## Disclaimer 

Oh I shouldn’t have to say anything here.

This tool scrapes public lists. Some servers may not want to be scraped. Some may collapse under your query. Some may log your IP and report you to authorities. So go do it at McDonalds.

**Use responsibly. Or don’t.**

Note: you aren't getting free cloud with the `:cloud` models - those credits follow the client, not the server. These are all reserved instances or owned infra. You aren't actually incurring metered cost, probably.


## FAQ

 * Q: Was this vibe coded?
 * A: Only the README

---

 * Q: Was that with one of these servers?
 * A: **cough cough**

---

 * Q: Can I install new models on these with `ollama pull`?
 * A: **cough cough**

---

 * Q: That cough sounds pretty bad, you should get some rest.
 * A: Thank you very much!


## Example output

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

Yes, `smollm2:135m` appears **1142 times**. That’s a lot of tiny models. Orchestrate them all together. Produce gigabytes of garbage. Have fun.
