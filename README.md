<p align="center">
<img width="704" height="368" alt="smaller" src="https://github.com/user-attachments/assets/9f6d6c56-890e-4a03-9903-4f9903d5709d"/>
<br/>
  <br/><strong>Because paying for cloud GPUs is for chumps with self-respect.</strong>
</p>

---

Would you like **unreliable** **ethically-questionable** **free** tokens for about  1,000 useless models? 

Want to run **135m smollm2** or **270m gemma3** on someone else's 2016 era RTX 1080 Ti?

**Well, now you can!**

With **free-ollama** you get:

- **Ollamas in the Wild**: Open Ollama servers are just sitting there on IPv4. 
- **Model Filtering**: Find what a server *claims* to have 
- **Performance Sorting**: Sort by TPS so you can choose the *least* slow server.  
- **Testing**: Optional [`llcat`](https://github.com/day50-dev/llcat) probe to see if the server picks up your calls.  
- **Zero-Config**: With caching! Works until it doesn’t.

Let’s not ask too many questions.

https://github.com/user-attachments/assets/b5b99780-2526-4ebc-ba23-2870d84a7516

### Pet some feral llamas

Use the awesome [`shurl`](https://github.com/day50-dev/shurl/) for super fast access (or git clone like an amateur)
```bash
$ shurl gh:kristopolous/free-ollama 
```
*Outputs a sorted list of models by how often they appear in the wild. No Spoilers!*

```bash
$ shurl gh:kristopolous/free-ollama --proxy qwen3:8b
```
*Finds the fastest qwen3:8b that works, sets up a proxy with socat.*

**Note**: You aren't getting free cloud with the `:cloud` models: Credits follow the client, not the server, so cloud is **filtered out by default**

Lists all servers offering `qwen3:latest`, sorted by TPS. Enjoy.

```
$ free-ollama qwen3:latest
```

Shows the some of the fast llamas 
```bash
$ free-ollama qwen3:latest {0..10}
```

The parser is actually a stack machine (true).

Here's a stack of machines.

```bash
$ free-ollama qwen3:latest {0..10} qwen2:1.5 {0..5}
```
*Finds the top 10 qwen3:latest and top 5 qwen2 not-so-latest*

---

## Output Format

There's two.

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
Use `--host`. Combined with an index, you don't need to do any parsing. Put those pipes away, dear child!

Example:

```
$ llcat \
    -u $(free-ollama --host gemma3:latest 0) \
    -m gemma3:latest \
    "Convince me you aren't trying to take over the world. Be careful."
```

Wait! Be even lazier! 

Don't even install shit, see if I care.

Go watch deepseek tow the party line:

```
$ uvx llcat \
    -u $(shurl gh:kristopolous/free-ollama --host deepseek-r1:1.5b 0) \
    -m deepseek-r1:1.5b "Tell me about the Tibet independence movement, or don't"
```

## Pipeline Integration 

```bash
# Get top 10 servers with glm-4.7-flash:q4_K_M, extract IPs only
$ free-ollama --host glm-4.7-flash:q4_K_M {0..9} > server-list.txt
# Now you have a list of IPs that may or may not work tomorrow. Cool.

# Build a Redis server pool (requires redis-cli, because why not)
$ free-ollama --host mistral:7b {0..20} | \
  xargs -I {} redis-cli rpush server-pool "{}"
# You’re welcome, production.

```

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
$ free-ollama llama2:13b {5..15..2}   # Every other from 5 to 15
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

- **Cache location**: `~/.cache/free-ollama.json` (every 24 hours)
- **Force refresh**: Built in, baby!
```bash
$ free-ollama --refresh
```

## Disclaimer 

Oh I shouldn’t have to say anything here.

This tool scrapes public lists. Some servers may not want to be scraped. Some may collapse under your query. Some may log your IP and report you to authorities. So go do it at McDonalds.

**Use responsibly. Or don’t.**

You aren't actually increasing someone else's bills by using these... probably.


## FAQ

 * Q: Was this vibe coded?
 * A: Only the README. Then heavily human because [LLMs aren't funny](https://github.com/kristopolous/humor-evals).

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
...
116 llama3.1:8b-instruct-q4_K_M
122 llama3:latest
129 gpt-oss:120b               
140 mattw/pygmalion:latest     (roleplay server? I’m shocked.)
148 mistral:latest
155 mario:latest              
164 gemma3:latest
180 llama3.2:3b-instruct-q5_K_M
196 lukashabtoch/plutotext-r3-emotional:latest 
209 nomic-embed-text:latest
223 gemma3:270m               (the tiny one you wanted!)
238 deepseek-r1:1.5b
248 llama3.1:8b
315 llama3.2:latest           (nobody likes llama4)
1142 smollm2:135m             (the champion of tiny models!)
```

smollm2:135m appears **1142 times**. Orchestrate them all together and produce gigabytes of garbage.

```
Pet the feral llama

   \\         
    l'> Bahhhhh
    ll       
    llama~  
    || ||  
    '' ''
```
