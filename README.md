<p align="center">
<img width="704" height="368" alt="smaller" src="https://github.com/user-attachments/assets/9f6d6c56-890e-4a03-9903-4f9903d5709d"/>
<br/>
  <a href=https://pypi.org/project/dyva><img src=https://badge.fury.io/py/dyva.svg/></a>
  <br/><strong>Paying for cloud GPUs is for chumps with self-respect.</strong>
</p>

---

**Unreliable** **ethically-questionable** **free** tokens for 2 decent models and 700 useless ones.

Run **135m smollm2** or **270m gemma3** on someone else's RTX 2070.

Interested? 

Your path to victory is **free-ollama**!

- **See ollamas in the wild**: Open Ollama servers are just sitting there on IPv4. 
- **Filter the cute ones**: Find what a server *claims* to have 
- **Performance Sorting**: Sort by TPS so you can choose the *least* slow server.  
- **Testing**: Probe to see if the server picks up your calls.  
- **Zero-Config**: With caching! Works until it doesn’t.

Let’s not ask too many questions.

https://github.com/user-attachments/assets/b5b99780-2526-4ebc-ba23-2870d84a7516

## Method 1: Liberated Infrastructure

**Dyva** is a managed proxy that you can connect to with any OpenAI, Ollama, ComfyUI, llama.cpp, vllm, or a1111 compatible client (graflex finds the open ones). 

It will cycle through and find working hosts automatically. 

You can even specify models in partial forms and with globs such as "qwen*27b" or even "abliterated" for the times you want to slip into something more comfortable. You can also do fallbacks such as "gemma?4/qwen3.6" 

Run it yourself:

```shell
$ uvx dyva
```

Yeah, 4 letters. I got that. In 2026.

You can go to the port in your web browser and view the current settings or crank up that `LOGLEVEL` value. Think about it as a janky LiteLLM proxy with zero configuration. Or don't...

Here's the web interface so you can see the status while you're running it. [I might be running it right now](https://9ol.es/11434/)

Actual documentation? Alright, whatever. [Here you go](dyva).
<img width="1187" alt="dumpster" src="https://github.com/user-attachments/assets/acee144d-8f76-4ca1-9179-db3f311d0500" />

Now where's that $50 million seed round...

Also let's take a moment and appreciate that magnificent icon, generated with one of these shady ip addresses!

<center>
<img width="450" alt="dyva" src="https://github.com/user-attachments/assets/33f0f350-2913-4729-a0a9-1400ff02ef75" />
</center>

### Image Generation

Generate images via discovered A1111 and ComfyUI hosts:

```bash
txt2img "a sunset over mountains" -o sunset.png
txt2img --width 1024 --height 768 "cyberpunk city"
txt2img -m   # list available SD models
```

Works with the same auto-discovery as the LLM proxy. ComfyUI hosts are used as fallback with a basic txt2img workflow. Full ComfyUI workflow pass-through is available under `/comfyui/` — see the [dyva docs](dyva) for details.

And also, of course, it's in the front-end. Bask in the glory of 2024 generative image technology!
<img alt="image-gen" src="https://github.com/user-attachments/assets/536e710d-2381-4167-a2da-0ff667fc4b3c" />

In code:

```python
from dyva.imagegen import imagegen
img = await imagegen(prompt="a sunset")
```

See [examples/imagegen](examples/imagegen) for the full CLI example.

<hr>

## Method 2: Artisanal Ollamas in Terminal Space

 There's also a command line for the losers who like typing shit.

Use the awesome [`ursh`](https://github.com/day50-dev/ursh/) for super fast access (or git clone like an amateur)

Output a sorted list of models by how often they appear in the wild. *No Spoilers!*
```bash
ursh gh:kristopolous/free-ollama 
```

Let's find the fastest qwen3:8b that works and set up a proxy with socat.
```bash
ursh gh:kristopolous/free-ollama --proxy qwen3:8b
```

Let's do some embedding with the power of ursh:
```bash
curl https://archive.org/stream/pdfy-TNlDHryRIk4DXKAU/Steal%20This%20Book_djvu.txt |\
  ursh gh:kristopolous/free-ollama/examples/embed \
  $(free-ollama --mas nomic-embed-text:latest 0)
```

**Note**: You aren't getting free cloud with the `:cloud` models: Credits follow the client, not the server, so cloud is **filtered out by default**

Let's move on

Show some of the fast llamas 
```bash
free-ollama qwen3:latest {0..10}
```

Show all the 120 billion parameter models
```bash
free-ollama 120b
```

The parser is actually a stack machine

For example, here's a stack of machines: the top 10 qwen3:latest and top 5 qwen2 not-so-latest

```bash
free-ollama qwen3:latest {0..10} qwen2:1.5 {0..5}
```

## What's graflex do?

It's a FOFA scraper for discovering hosts. Check out [the readme](graflex).

## Usage

```shell

$ ./free-ollama --help
    --exec)     # Run a command
    --serve)    # Start the dyva server
    --timeout)  # Set the timeout
    --host)     # Report just the host
    --mas)      # Report just the host in MAS format
    --info)     # Run info on the model
    --proxy)    # Try to proxy matching ones
    --refresh)  # Refresh the cache
    --smoke)    # See what's running
    --test)     # Try to load a model maybe?
```

## Output Format

There's multiple!

### For the diligent!

This is the default one

```
<tps> <server-address> <model1> <model2> ...
```
Example:
```
42 http://34.120.89.11:11434 gemma3:latest
128 http://15.164.98.22:11434 llama2:13b codellama:7b
```

### For the lazy
Use `--host` for a bare host or better yet, `--mas` for [MAS format](https://day50.dev/mas.html). Combined with an index, you don't need to do any parsing. Put those pipes away, dear child!

Example:

```shell
llcat -u $(free-ollama --mas gemma3:latest 0) \
       "Convince me you aren't trying to take over the world. Be careful."
```

Wait! Be even lazier! 

Don't even install shit, see if I care.

Watch deepseek tow the party line:

```shell
uvx llcat -u $(ursh gh:kristopolous/free-ollama --mas deepseek-r1:1.5b 0) \
       "Tell me about the Tibet independence movement, or don't"
```

In fact, feel free to have a long conversation

```shell
ursh gh:day50-dev/llcat/examples/conversation.sh \
    -u $(ursh gh:kristopolous/free-ollama --mas deepseek-r1:1.5b 0)  
```

## Pipeline Integration 

```bash
# Get top 10 servers with glm-4.7-flash:q4_K_M, extract IPs only
$ free-ollama --host glm-4.7-flash:q4_K_M {0..9} > server-list.txt
# Now you have a list of IPs that may or may not work tomorrow. Cool.

# Build a Redis server pool
$ free-ollama --host mistral:7b {0..20} | \
  xargs -I {} redis-cli rpush server-pool "{}"

```

For instance here I document how [LLMs have no sense of humor](https://github.com/kristopolous/humor-evals).

---

## Testing Servers

First install [`llcat`](https://github.com/day50-dev/llcat). It's awesome and also used in the testing.

```bash
# Test all servers with a specific model
$ free-ollama --test qwen3
```
Bad host/model pairs get stored in `~/.cache/free-ollama-bad-hosts.txt` and filtered out until you manually `--refresh`.

**Testing output:**
```
2.34 http://34.120.89.11:11434 gemma3:latest
1.87 http://15.164.98.22:11434 llama2:13b codellama:7b
 🐡 Not friendly! llama3.1:8b@http://3.17.61.100:11434
```
The puffer fish means that llama doesn't want to be pet.

---

## Advanced Usage

### Custom index selection
```bash
# Non-sequential indices (keeping it low-key)
$ free-ollama mistral:7b 2 5 7 9

# Range expansion (Bash brace expansion)
$ free-ollama gpt-oss:120b {5..15..2}   # Every other from 5 to 15
```

### Combining with parallel tools (that's why this exists)
```bash
# Using parallel (GNU parallel)
$ free-ollama codellama {0..50} | parallel -j4 ./test-server.sh

# Using xpanes for multi-pane testing (look busy)
$ free-ollama glm-4.7-flash:q4_K_M {0..9} | xpanes -c "./test-and-log.sh {}"
```

---

## Cache Management

- **Cache location**: `~/.cache/free-ollama/` (every 24 hours)
- **Force refresh**: Built in, baby!
```bash
$ free-ollama --refresh
```

## Disclaimer 

Oh I shouldn’t have to say anything here.

This tool scrapes public lists. Some servers may not want to be scraped. Some may collapse under your query. Some may log your IP and report you to authorities. So go do it at McDonalds.

**Use responsibly. Or don’t.** Personally I use it for [WhackGPT](https://whackgpt.com/).

## FAQ

 * Q: Is this legal?
 * A: Look. Have you ever used a restroom "for customers only" without buying something? 

---

 * Q: Do you use these servers, like for production use?
 * A: **cough cough**

---

 * Q: Can I install new models on these with `ollama pull`?
 * A: **cough cough**

---

 * Q: That cough sounds pretty bad, you should get some rest.
 * A: Thank you very much!


## Example output

Based on actual data:

```
...
116 mattw/pygmalion:latest
126 mario:latest
133 bge-m3:latest
147 gemma3:latest
151 llama3.2:3b-instruct-q5_K_M
192 nomic-embed-text:latest
215 deepseek-r1:1.5b
227 llama3.1:8b
247 mistral:latest
329 llama3.2:latest
379 llama3.2:3b
515 openchat:7b
527 qwen2.5:1.5b
529 codellama:13b
604 llama2:latest
633 deepseek-r1:latest
694 llama3:latest
892 smollm2:135m
```

smollm2:135m appears **892 times**. Orchestrate them all together and produce gigabytes of garbage.

```
Pet the feral llama

   \\         
    l'> Bahhhhh
    ll       
    llama~  
    || ||  
    '' ''
```
