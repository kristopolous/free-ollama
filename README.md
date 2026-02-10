# free-ollama

**Discover, filter, and test public Ollama servers**  
A command-line utility for finding and benchmarking Ollama-compatible servers based on model availability and performance.

## Features

- **Server Discovery**: Automatically fetches and caches a curated list of public Ollama servers
- **Model Filtering**: Find servers by model name(s) with stack-based syntax
- **Performance Sorting**: Sort servers by TPS (transactions per second)
- **Health Testing**: Optional live testing with [`llcat`](https://github.com/day50-dev/llcat/) to verify server responsiveness
- **Zero-Config**: Works out-of-the-box with sensible caching and defaults


### Dependencies

- `bash` , `jq`, `curl`, `find`
-  [`llcat`](https://github.com/day50-dev/llcat/) for server testing

## Quick Start

### 1. View model frequency distribution
```bash
./free-ollama
```
Outputs a sorted list of models by frequency in the server database.

### 2. Find servers with a specific model
```bash
./free-ollama gemma3:latest
```
Lists all servers offering `gemma3:latest`, sorted by TPS (ascending).

### 3. Select top performers by index
```bash
./free-ollama gemma3:latest {0..10}
```
Shows the 11 fastest servers (indices 0-10 after TPS sorting) running `gemma3:latest`.

### 4. Find servers with multiple models
```bash
./free-ollama llama2:13b codellama:7b {0..5}
```
Finds servers offering **both** `llama2:13b` **and** `codellama:7b`, showing the top 6.

## Output Format

The default output is **space-delimited** in this format:
```
<tps> <server-address> <model1> <model2> ...
```
Example:
```
42 http://34.120.89.11:11434 gemma3:latest
128 http://15.164.98.22:11434 llama2:13b codellama:7b
```

### Pipeline Integration
The space-delimited format makes it easy to pipe into other tools:

```bash
# Get top 10 servers with phi3, extract IPs only
./free-ollama phi3:mini {0..9} | awk '{print $2}' > server-list.txt

# Build a Redis server pool (requires redis-cli)
./free-ollama mistral:7b {0..20} | \
  sort -k1,1nr | \
  cut -d' ' -f2 | \
  xargs -I {} redis-cli rpush server-pool "{}"

# Sort by TPS descending and select server+model pairs
./free-ollama llama2:7b | sort -k1,1nr | cut -d' ' -f2-3
```

## Testing Servers

Use the `test` flag to verify server responsiveness with `llcat`:

```bash
# Test all servers with a specific model
./free-ollama test qwen3
```
Bad host/model pairs get stored in `~/.cache/free-ollama-bad-hosts.txt` and filtered out later

### Testing output:
```
2.34s 34.120.89.11:11434 gemma3:latest
1.87s 15.164.98.22:11434 llama2:13b codellama:7b
Bad -- 192.168.1.5:11434 phi3:mini
```

## Advanced Usage

### Custom index selection
```bash
# Non-sequential indices
./free-ollama mistral:7b 2 5 7 9

# Range expansion (Bash brace expansion)
./free-ollama llama2:13b {5..15..2}   # Every other from 5 to 15
```

### Combining with parallel tools
```bash
# Using parallel (GNU parallel)
./free-ollama codellama {0..50} | parallel -j4 ./test-server.sh

# Using xpanes for multi-pane testing
./free-ollama glm-4.7-flash:q4_K_M {0..9} | xpanes -c "./test-and-log.sh {}"
```

## Cache Management

- **Cache location**: `~/.cache/free-ollama.json`
- **Auto-refresh**: Every 24 hours (1440 minutes)
- **Force refresh**: Delete the cache file
```bash
rm ~/.cache/free-ollama.json
./free-ollama  # Will fetch fresh data
```

## Data Source

The server list is curated from [awesome-ollama-server](https://awesome-ollama-server.vercel.app/) (public community-maintained list).  
Data includes:
- Server address
- TPS rating
- Available models
- Last seen timestamp

## Troubleshooting

### "Updating cache..." appears repeatedly
- Check network connectivity
- Verify `curl` is installed and accessible
- Check if the data source is available: `curl -I https://awesome-ollama-server.vercel.app/data.json`

### No servers found for a model
- The model name might be different (try without version suffix)
- No servers in the current list offer that model
- Try a broader search: `./free-ollama llama` (partial match)

### Testing fails with "Bad --"
- Server may be offline or blocked
- Firewall restrictions (common with cloud providers)
- Model not actually available despite being listed
- Check `~/.cache/free-ollama-bad-hosts.txt` for persistent failures

## Disclaimer

Oh I shouldn't have to say anything here. Hush hush.

## License

MIT License. See [LICENSE](LICENSE) file.
