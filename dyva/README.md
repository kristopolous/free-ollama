# Dumpster Dyva

OpenAI and Ollama-compatible proxy that routes inference to insecure Ollama, vllm, LM Studio, SGLang, llama.cpp, A1111, and ComfyUI hosts using Shodan and FOFA.

Compatible enough that the real Ollama CLI thinks it's talking to a real Ollama server — see [below](#use-it-like-ollama).

Complete with even a little chat thingy. Look at the thingy!
<img alt="towers" src="https://github.com/user-attachments/assets/a169c629-71fd-44f3-9815-8047a43109d9" />

The chat thingy features these jazzy little videos instead of a spinner. Here's all 10 of them together. There's audio. Maybe there shouldn't be.


https://github.com/user-attachments/assets/c9874c72-52ea-4060-a078-d2945add22d6



"Now now now" you say, from your VC office, "what about mobile?!"

Here it is. See all the tools. This interacts with all the discovered infrastructure, tool calling across dynamic hosts seamlessly. it's pretty magical...

<img  alt="67098" src="https://github.com/user-attachments/assets/65660bfc-2302-4f7e-886f-a4ae6e78cabe" />


## CLI Options

| Flag | Description |
|------|-------------|
| `-p`, `--port` | Port to listen on (default: 11434) |
| `-u`, `--host` | Host address to bind (default: all interfaces) |
| `-t`, `--timeout` | Request timeout seconds (default: 30) |
| `-w`, `--workers` | Concurrent workers (default: 3) |
| `-l`, `--local` | Restrict inference endpoints to localhost only |
| `-r`, `--refresh` | Refresh server cache and exit; optionally name a single source (e.g. `--refresh graflex`) — other sources keep their last-fetched data |
| `--source` | Manage host sources: `--source list`, `--source add <url>`, `--source disable <name>\|all`, `--source enable <name>\|all` — see [below](#running-it-over-your-own-machines) |
| `--hosts` | Inspect or prune the host reputation table — see [below](#host-reputation) |
| `--curlify` | Print `curl` commands of upstream requests to stderr |
| `-v`, `--version` | Show version |

All of these run and exit; none of them start the server.

## Sources, and Running It Over Your Own Machines

Where the host list comes from is a setting, not an assumption. The built-in
sources are public lists that are no longer aggressively refreshed — a small
and fairly inert set — and the additional-source mechanism exists so a *private*
list (graflex's own output, say) can be pointed at dyva without being published
for other people to abuse.

The routing doesn't care about any of that. The racing, failover, reputation
tiers and single OpenAI/Ollama endpoint work the same over a pool you own, so
the sources can be switched off entirely:

```bash
dyva --source disable all          # discover nothing
dyva --source add https://example.com/my-hosts.json
dyva --source list                 # confirm what is live
```

With every built-in source disabled dyva finds no hosts at all; add your own
list and it is a load balancer over your own machines and nothing else.
Individual sources go off by name (`--source disable forrany`) and back on with
`--source enable`. State lives in `disabled_sources` in `settings.json`, and a
change refreshes the cache immediately, so hosts a disabled source contributed
actually go away instead of lingering in the cached list.

## Host Reputation

Every host+key pair dyva has tried carries a state — `good`, `maybe_good`, or
`bad` — in `~/.cache/free-ollama/host-status.db`. The key is usually a model
name. The non-chat capabilities use `__video__`, `__music__`, and `__unreachable__`
(host-wide, so a dead host isn't re-probed once per model). **Speech and image
editing are keyed per model** — `tts`, `tts/vibevoice`, `edit`, `edit/flux2`,
`edit/qwen` — because a host that can't run Flux.2 may be perfectly good at
Qwen-Image-Edit, and one bucket for all of them condemned hosts far too
broadly. The key is the query you searched with, canonicalised, so `flux*2`,
`flux-2` and `FLUX.2` all share one record.

Note the absence of a `*` in the unfiltered form: reputation keys pass through
`canon_pattern()`, which strips a trailing `*`, so a key like `edit/*` would be
*written* as `edit/` and *read* as `edit/*` — marks that never match, and hosts
that are retried no matter how often they fail.

```bash
dyva --hosts                      # counts per state
dyva --hosts bad                  # which keys are marked bad, and how many hosts each
dyva --hosts bad __tts__          # the hosts carrying that mark
dyva --hosts bad __tts__ del      # clear them
dyva --hosts bad/__tts__ del      # same thing, if you prefer it joined
```

Arguments narrow left to right and **the verb comes last**, so reading is the
default: `--hosts bad` shows you the bad ones rather than needing a word in
front of them. `del` always needs a named key — there is deliberately no way to
clear a whole state at once, because reputation is expensive to rebuild (every
mark cost a real probe of a real host). The bare `--hosts bad` is the survey
that shows what you'd be discarding:

```
122 host marks in 'bad', across 10 keys:

  __tts__              32
  __edit__             23
  qwen3.6:27b          20
  __unreachable__       8
```

Clearing a key leaves those hosts unranked, so they get retried on the next
request. This is the thing to reach for when you've fixed something and want
the hosts that failed for the old reason reconsidered.

### Bad pairings

Some failures are facts about a **(host, model) pairing** rather than about
either one alone. A host whose `flux-2-klein-4b.safetensors` and text encoder
don't actually belong together dies with `mat1 and mat2 shapes cannot be
multiplied` every time, while the same host runs its other edit models fine.

Recording that against the host doesn't work. The submit succeeded — and by
design that is what host selection judges — so the host was already marked
`good`; the later render failure calls `add_bad`, which only demotes `good` to
`maybe_good`, and `maybe_good` still sorts near the top. The same doomed
pairing was chosen again on every request.

So a structural render failure also writes a mark under `<capability>!<model>`,
forced to `bad` rather than demoted, and the planner starts each attempt with
`exclude=bad_pairs_for(host, "edit")`:

```
edit!flux2klein4b    bad    https://198.51.100.7:443
```

The model name is canonicalised the same way everything else is, so path
prefixes and separator variants collapse to one record. The host keeps its
general reputation for edit work and simply stops being offered that one
checkpoint.

### Busy hosts

ComfyUI runs one prompt at a time per GPU, so handing a second job to a host
already rendering just puts it in that host's queue. `is_active(host)` answers
whether one of our own jobs is on a host, and `_idle_first()` moves those to
the back of every candidate list — edit, TTS, video, music, and both phases of
txt2img.

There is deliberately no "active hosts" set. A set like that needs whoever put
a host in to take it back out, and the first time something raises on an
unusual path that host is stuck marked busy forever; heartbeats and timeouts
are the usual answers and they are all worse than not having the problem.
Instead the answer is derived from the live `/workers` registry, which already
records which host each job is talking to and is unwound by the context manager
that created it, so it cannot leak.

It is a demotion, not a filter: a busy host is still better than no host, and
with a single good ComfyUI on the network a filter would turn "wait your turn"
into "no hosts available". There is a race — a host only appears once a job has
taken it — but the window is milliseconds against renders that run for minutes,
and losing it merely costs the old behaviour.

## Use It Like Ollama

Point the official Ollama CLI at dyva and the everyday commands just work — except instead of one machine's models you see everything the swarm has to offer:

```bash
export OLLAMA_HOST=http://127.0.0.1:11434   # wherever dyva runs

ollama ls            # every model across all discovered hosts
ollama ps            # models in use and where they last ran
ollama show llama3   # metadata for any model
ollama run llama3    # chat straight from your terminal
```

`ollama run` streams through the same racing/failover machinery as every other endpoint, so if a host dies mid-sentence the next request lands somewhere else.

Notes:
- `ollama pull` doesn't download anything (models live on remote hosts) — it just refreshes dyva's server cache.
- `ollama cp` / `ollama rm` are not supported.

## API Reference

See the Swagger docs at `/docs` on a running instance for the full API reference.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat (streaming + tool calls) |
| `POST` | `/api/chat` | Native Ollama chat |
| `POST` | `/api/generate` | Native Ollama generate |
| `POST` | `/api/show` | Model metadata |
| `POST` | `/api/pull` | Refresh server cache |
| `GET` | `/api/tags` | List available models |
| `GET` | `/api/ps` | Last-used models |
| `GET` | `/api/version` | Version info |
| `GET` | `/api/activity` | SSE stream of real-time proxy activity (also mirrored to stderr) |
| `GET` | `/v1/models` | OpenAI-compatible model listing |
| `POST` | `/sdapi/v1/txt2img` | Text-to-image (A1111 + ComfyUI fallback) |
| `POST` | `/v1/images/edits` | OpenAI-compatible image editing (prompt + 0..N reference images) |
| `GET` | `/sdapi/v1/sd-models` | List discovered SD models |
| `POST` | `/v1/audio/speech` | OpenAI-compatible text-to-speech (ComfyUI TTS nodes) |
| `GET` | `/v1/audio/voices` | List TTS voices/nodes across discovered hosts |
| `GET` | `/v1/audio/clips/{name}` | Fetch a previously generated speech clip |
| `GET` | `/sdapi/v1/images` | Metadata for recently generated images (last 100) |
| `GET` | `/sdapi/v1/images/{name}` | Fetch a generated image file |
| `POST` | `/v1/web/fetch` | Read one explicit URL as text, or save it if it is an image |
| `*` | `/comfyui/{path}` | ComfyUI pass-through proxy |
| `GET` | `/dashboard` | Dashboard UI (Server Room / Chat / Image tabs) |
| `GET` | `/dashboard-data` | JSON snapshot of the last-successful/good/bad lists |
| `GET` | `/clear-bad` | Clear failed host+model pairs |
| `GET` | `/next-host` | Remove a model from the last-successful list |
| `GET` | `/skip-good` | Move a good host+model pair into the bad list |
| `GET` | `/refresh` | Re-fetch server lists from all sources |

#### Routing Probe

Include `__dyva_info__` anywhere in the prompt or messages and dyva won't run inference — instead it returns `{"host": ..., "model": ...}` for the host/model the request *would* have been routed to (last-used host if still eligible, else the top of the race queue):

```bash
curl http://localhost:11434/api/chat -d '{
  "model": "qwen",
  "stream": false,
  "messages": [{"role": "user", "content": "__dyva_info__"}]
}'
```

With `"stream": true` the same info arrives as a normal NDJSON/SSE chat stream — payload in `message.content`, plus a top-level `dyva_info` object on the final chunk.

Use `__dyva_info__:next` to move on: it first drops the model's sticky last-successful host (the same action as `GET /next-host` and the dashboard's "next host" link), then returns the *next* host/model the request would land on. The response shape is identical, so you get confirmation of where you moved to — repeat to keep walking down the list. Model matching is case-insensitive and glob-normalized, so `GEMMA*`, `gemma`, and `*gemma*` all target the same sticky host.

Use `__dyva_info__:test` when a match looks bogus. Instead of just reporting the routing choice, dyva actually *probes* the candidates: it sends each one the quick factual question *"What is the name of the first United States President?"* and keeps the first host that answers with "Washington" or "George". Any host that answers wrong (or fails outright) is marked bad and skipped, so walking one suspicious model culls all its deadbeat duplicates. The response shape is the same as `__dyva_info__`, with the host/model of the first passing server:

```bash
curl http://localhost:11434/api/chat -d '{
  "model": "openchat",
  "stream": false,
  "messages": [{"role": "user", "content": "__dyva_info__:test"}]
}'
```

Because a real inference runs per candidate, `:test` is deliberately opt-in and scoped: it only tests the hosts serving the one model you asked for, never the whole catalog.

### Text-to-Image

The `txt2img` CLI generates images via the proxy:

```bash
txt2img "a sunset over mountains" -o output.png
txt2img --width 1024 --height 768 --steps 20 "cyberpunk city"
txt2img -m                  # list available SD models
txt2img -m "sd_v15" "a cat" # use a specific model
```

| Flag | Description | Default |
|------|-------------|---------|
| `--host` | dyva proxy host | `127.0.0.1` |
| `--port` | dyva proxy port | `11434` |
| `--width` | Image width | `512` |
| `--height` | Image height | `512` |
| `--steps` | Sampling steps | `30` |
| `-m`, `--model` | SD model name (use without value to list) | auto |
| `-o`, `--output` | Output file (default: stdout as PNG) | — |

The proxy first tries A1111 hosts, then falls back to ComfyUI hosts with a basic txt2img workflow.

#### Library

`dyva.imagegen` provides a reusable async/sync function:

```python
from dyva.imagegen import imagegen, imagegen_sync

# async
img_bytes = await imagegen(prompt="a sunset", width=1024, height=768)

# sync
img_bytes = imagegen_sync(prompt="a cat in a spacesuit")
```

Returns raw PNG bytes. All parameters are keyword-only:

| Param | Default | Description |
|-------|---------|-------------|
| `prompt` | *(required)* | Text prompt |
| `model` | `None` | SD model name to match |
| `width` | `512` | Image width |
| `height` | `512` | Image height |
| `steps` | `30` | Sampling steps |
| `cfg_scale` | `7` | CFG scale |
| `sampler_name` | `"Euler"` | Sampler |
| `negative_prompt` | `""` | Negative prompt |
| `seed` | `-1` | Seed (-1 = random) |
| `host` | `"127.0.0.1"` | dyva proxy host |
| `port` | `11434` | dyva proxy port |

### Image Editing

Distinct from text-to-image: an **edit** model takes a prompt plus *zero or
more* reference images — `{"put the feather cap on the dog", dog.jpg,
cap.jpg}`. `POST /v1/images/edits` speaks OpenAI's image-edit shape and races
it across ComfyUI hosts running an edit model. (img2img is not offered; nobody
uses it any more.)

```bash
# multipart, as the OpenAI clients send it
curl -X POST http://localhost:11434/v1/images/edits \
  -F prompt="put the feather cap on the dog" \
  -F image=@dog.jpg -F image=@feather-cap.jpg

# or JSON with base64 / data: URLs
curl -X POST http://localhost:11434/v1/images/edits \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "a dog in a feathered cap", "image": ["<base64>"]}'
```

Returns `{"created": ..., "data": [{"b64_json": ...}], "model": ..., "host": ...}`.

**Reference images are uploaded to the chosen host first.** ComfyUI's
`LoadImage` reads a filename from the host's own input directory — there is no
way to hand a graph raw pixels — so each image goes to `POST /upload/image` on
whichever host won the race, and the graph references what comes back.

Two node shapes cover what is actually deployed, declared in
[`node-classifier.json`](node-classifier.json) under `edit`:

| family | how references get in |
|---|---|
| Qwen-Image-Edit | native multi-image: `TextEncodeQwenImageEditPlus(clip, prompt, vae, image1..3)` |
| Flux Kontext / Flux.2 | no multi-image node — each reference is `LoadImage → FluxKontextImageScale → VAEEncode → ReferenceLatent`, chained through the conditioning, then `FluxGuidance` |

Which model, CLIP and VAE to load is resolved against the host's **live loader
enums** (`CheckpointLoaderSimple`, `UNETLoader`, `CLIPLoader`/`DualCLIPLoader`,
`VAELoader`) — the authoritative list of what is on that host's disk — so a
host is only accepted when every file the graph needs actually exists there.
With no reference images the same graph runs as a plain generation on the edit
model.

The dashboard's Image tab has a **Generate / Edit** toggle; in Edit mode the
negative prompt is hidden (edit models don't take one) and a drop zone accepts
reference images.

### Leaving Little Behind

These are strangers' machines, and every job we run writes to their disk.
ComfyUI has no API for deleting a generated file, so dyva does the two things
that are actually available.

**Write to temp, not output.** The graphs end in `PreviewImage` / `PreviewAudio`
rather than `SaveImage` / `SaveAudio` wherever the host has them. Preview nodes
write into ComfyUI's temp directory, and ComfyUI's own `main.py` calls
`cleanup_temp()` — an `rmtree` of that directory — both at startup and in the
`finally` block at shutdown. The file removes itself, and in the meantime it
never appears in the host's output gallery. Retrieval is unchanged: the history
entry reports `type: "temp"` and `/view` is already handed whatever type it
names. A host with only the save nodes still gets those, and one with neither
isn't a viable host at all.

**Forget the job.** After the bytes are collected, `comfy_forget()` posts
`{"delete": [prompt_id]}` to `/history`. That removes the record, not the file
— no core endpoint deletes output — but it takes our work out of the host's
queue view. It is best-effort: a host that refuses hasn't failed at the thing
the caller asked for. Every capability collects through `comfy_collect()`, so
one call covers images, edits, video, speech and music.

What this does **not** solve is uploads. `/upload/image` puts each edit
reference image into the host's `input/` directory permanently, `LoadImage`
reads from there, and there is no preview equivalent. That is the larger
footprint of the two and there is currently nothing to be done about it from
the client side.

### ComfyUI Pass-Through

All ComfyUI workflow endpoints are proxied under `/comfyui/`. The prefix is stripped and the request is forwarded to a discovered ComfyUI host.

```bash
# Submit a workflow
curl -X POST http://localhost:11434/comfyui/prompt \
  -H 'Content-Type: application/json' \
  -d '{"prompt": {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}}}}'

# Check queue
curl http://localhost:11434/comfyui/queue

# Get history for a prompt
curl http://localhost:11434/comfyui/history/{prompt_id}

# Retrieve output image
curl "http://localhost:11434/comfyui/view?filename=output.png&type=output"

# List available checkpoints
curl http://localhost:11434/comfyui/models/checkpoints

# Server stats
curl http://localhost:11434/comfyui/system_stats
```

Target a specific host with `?host=ip:port`:

```bash
curl "http://localhost:11434/comfyui/queue?host=1.2.3.4:8188"
```

### Text-to-Speech

`POST /v1/audio/speech` speaks the OpenAI TTS shape and runs it through the
**same race as text generation** — one bounded worker pool over a ranked host
list, first success wins, losers cancelled, and the same `trying` / `failure` /
`success` lines in the activity feed. It shows up in the worker view with a
working stop button like any other job. `response_format` and `speed` are
accepted but ignored; you get the host-native format, usually flac.

```bash
curl -X POST http://localhost:11434/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "tts-1", "input": "Hello world", "voice": "alloy"}' \
  --output speech.flac
```

#### How a host is chosen

TTS is a *node* feature, not a model-file feature — a host can run
`Qwen3TTSEngineNode` with no audio checkpoint on disk, and a host stuffed with
music models can have no TTS node at all. So every ComfyUI host is a candidate
and the ordering carries the signal: an explicit `?host=`, then hosts a
previous probe found a node on, the last host that spoke, good reputation,
hosts with audio-class models, everything else, and known-bad last.

#### How a node is chosen

Node classes are matched against the families in
[`node-classifier.json`](node-classifier.json) — surveyable, popularity-seeded
data, not a hardcoded boutique list. But **matching a name is not enough**, so a
candidate is only accepted if the whole three-node graph can actually be built
from what that host has installed:

- every required input can be filled inline — an enum, or a type with a
  default, or a plain primitive. Anything else (`AUDIO`, `MODEL`,
  `ELEVENLABS_VOICE`, a bare `COMBO`) is a *socket* that needs an upstream node
  to feed it, and a node needing one can't be driven by this graph.
- the node actually emits `AUDIO` — or emits `TTS_ENGINE`, in which case
  `UnifiedTTSTextNode` has to be installed to do the synthesis.
- the host has a real audio saver. `SaveAudio` is not universal.

Everything else is read off the host's live `/object_info` rather than assumed:
the saver is wired to the *index* of the producer's `AUDIO` output, not to slot
0, and an unspecified voice skips past zero-shot/custom placeholder options
(picking one and then supplying no reference audio just fails at execution).
Hosts that don't pass are rejected during detection — before a prompt is
submitted, so no GPU time is spent finding out.

A host's verdict is cached for five minutes, negatively too, so a request
doesn't re-probe the whole network. `/object_info` runs to several megabytes on
a busy host, which is why the timeout on it is generous.

The response carries provenance headers — `X-Dyva-Host` (which host spoke) and
`X-Dyva-Node` (the node class that produced it). Both are exposed via
`Access-Control-Expose-Headers`, so a browser client can read them cross-origin.
These report what *actually* ran, which is not the same as the `model` you
asked for — that's only a hint.

`GET /v1/audio/voices` returns what TTS nodes exist per host and their voice
options (`{"voices": [...], "hosts": [...], "surveying": n}`). It answers from
a **stored survey**, not a live sweep: the `host_nodes` table in
`host-status.db` records, per host, which TTS node was found and what voices it
advertises. Probing that means pulling a multi-megabyte `/object_info` from
every candidate, which is why the list used to take a while to appear.

Hosts that have never been surveyed are probed in the background and show up on
the next call; `surveying` says how many are still outstanding. `?refresh=1`
re-probes and waits instead. A row with a null node means "surveyed, has no TTS
node" — a finding, not a gap, and one that keeps hundreds of hosts out of the
speech race. Entries are re-surveyed after a day.

The survey also feeds host ranking, so once it has run, dyva knows from a cold
start which handful of hosts out of several hundred can do speech at all.

Host selection ends the moment a host returns a `prompt_id`: that acceptance is
what marks the host good, and the render is waited out afterwards, outside the
race. A host that took the job and then rendered slowly is not a host-selection
failure and is no longer marked as one.

Each attempt reports a verdict rather than a bool — `accepted`, `unsuitable`
(structurally can't run this graph), `unreachable`, `failed`, or `timeout` — and
the engine turns that into the reputation mark. `unsuitable` and `failed` used
to be the same `add_bad`, which lost the distinction between "this host has no
TTS node" and "this host was down".

If speech fails, the error names how many hosts were tried, the verdict tally,
and the grouped reasons — `tts failed on 12 hosts (9 unsuitable, 3 timeout):
9x no known TTS node; 3x timeout` — and
ComfyUI's rejection envelope is parsed rather than truncated, so you get
`ElevenLabsTextToSpeech: required_input_missing: voice` instead of a clipped
blob of JSON.

### Chat Tools

The chat's wrench menu enables tools the model can call. Each is independent
and remembered across sessions:

| Tool | What the model can do |
|------|------------------------|
| **Image generation** | `generate_image` — render a picture from a description |
| **Image editing** | `edit_image` — change one picture, or combine several |
| **Video** | `generate_video` — a few seconds of silent footage |
| **Speech** | `generate_speech` — speak a line of dialog out loud |
| **Ask** | `ask_user` — put one multiple-choice question to the user |
| **Web** | `fetch_url` — read an explicit URL |
| **Documents** | create/edit/rename/search/replace documents in a side panel |

#### Assets

Every file in a conversation — attached by the user or produced by a tool — is
registered under a short name, and `list_assets` hands the model that list as
JSON — `[{"name": "garden.jpg", "kind": "image", "source": "generated"}]`,
oldest first — so the `name` field goes straight into the next tool call
instead of having to be picked out of a prose line.
This is what makes *"add this lady to the garden"* work: the model calls
`list_assets`, sees `lady.jpg` and `garden.jpg`, and passes both to
`edit_image` in `images`. The names are bookkeeping only and have nothing to do
with what anything is called on disk; the point is that the model never has to
reason about `20260903T081311Z-4f1c.png`.

The media tools therefore take a `name` for what they produce
(`{prompt: "a beautiful flowing garden", name: "garden.jpg"}`), and the tool
result reports the name actually assigned — which may differ, since collisions
get a `-2` suffix. `list_assets` is offered automatically whenever any media
tool is enabled; it has no toggle of its own.

A name is only useful if it survives the turn it was minted in. The tool result
carrying it is context for the request in flight and nothing more, so each
creation also writes a hidden line into the stored transcript
(`[Edited image saved as garden_with_lady.jpg.]`) and the image's markdown alt
text becomes the name. Without both, a later turn sees an image with no handle
on it and starts guessing.

Conversations that predate all this are backfilled on load: attachments, and
any `sdapi/v1/images/...`, clip or video link in the transcript, are rebuilt
into the list, named from the alt text or link text that is already there. The
backfill skips anything already registered, so a chat that is half old and half
new comes out whole.

#### Reading URLs

`fetch_url` reads one explicit address — it is not a search engine. The page is
rendered by the best engine installed, in this order:

1. **[lightpanda](https://lightpanda.io/)** — a headless browser engine built
   for this; it runs the page's JavaScript and emits Markdown directly
   (`lightpanda fetch <url> --dump markdown --json`).
2. **headless Chrome** — `--headless=new --dump-dom`, then the HTML is reduced
   to text here.
3. **curl** — the honest floor: it gets the bytes, tags are stripped locally.

A browser engine can fail on a page curl reads fine, so the order is a real
fallback chain and not just a preference: if the chosen engine errors, the
fetch is retried with curl before giving up. `/v1/web/fetch` exposes the same
thing over HTTP and reports which backend answered.

A URL that turns out to be an image is downloaded into the generated-image
store rather than rendered, so it appears in the Image gallery and becomes a
named asset — which means a picture found on the web can go straight into
`edit_image`.

Passing more than one image narrows host selection: an encode-style edit family
has a fixed number of image slots (`QwenImageEditPlus` takes three,
`QwenImageEdit` one), so a host whose only edit model can't hold them all is
skipped rather than silently rendering with the extras dropped.

`generate_speech` goes through `/v1/audio/speech` like everything else, but the
clip is also written to disk and referenced in the transcript by URL
(`/v1/audio/clips/{name}`) — a saved conversation can hold a link, not audio
bytes. The last 200 clips are kept. In the transcript the link is upgraded to
the same themed player the Speech tab uses.

### Slash Commands

Typed into the chat composer; they leave a note in the transcript rather than a
conversational turn, and are never sent to a model.

| Command | Does |
|---------|------|
| `/list` | This chat's system prompt, enabled tools, answering host and files |
| `/system [prompt]` | Set or show the chat's system prompt |
| `/info` | Ask which host and model are answering |
| `/next` | Drop this model's sticky host and move to the next one |
| `/test` | Probe hosts with the routing probe and cull the liars |
| `/retitle` | Regenerate the chat title |
| `/download` | Download the chat as JSON |
| `/delete` | Delete the current chat |
| `/help` | List the commands |

`/list` is the one to reach for when a conversation is behaving oddly — it
shows, in one place, what the model was told to be, what it is allowed to do,
who answered last, and every file it can name. `/listassets` and `/assets`
still work, as aliases for the whole summary.

## Settings

The dashboard's **Settings** tab is backed by `~/.cache/free-ollama/settings.json`. These persist across restarts and override the CLI defaults:

| Setting | Meaning |
|---------|---------|
| **Workers** | Parallel hosts raced per request (fan-out). |
| **Timeout** | Per-host request timeout, in seconds. |
| **Minimum model count** | Hide models served by fewer than this many hosts from `/api/tags` and `/v1/models` — the listings third-party apps read. `0` = show everything. (The dashboard always shows everything.) |
| **Admin password** | When set, viewing or changing the Settings tab and its sources requires it (sent as an `X-Admin-Key` header). Everything else — chat, models, the dashboard — stays public, so you can host a demo without letting visitors edit your config. Stored hashed; if you forget it, clear `admin_pw` in `settings.json` and restart. |
| **Additional sources** | Extra host lists to pull from — see below. |

Saving after you change the source list re-pulls the cache.

## Server Discovery

dyva aggregates servers from several built-in sources plus any you add. The tool [graflex](../graflex) is a from-scratch general-purpose aggregator, and **its output is dyva's native host format** — so a graflex-produced JSON list imports with a straight one-to-one field mapping.

### Additional sources (third-party formats)

Under **Settings → Additional sources** you add your own JSON host lists. They're fetched *before* the built-ins, so they win on duplicate hosts. Each source is:

```json
{
  "name": "my-list",
  "url": "https://example.com/hosts.json",
  "mapping": {
    "server":  {"field": "url"},
    "models":  {"field": "models"},
    "service": {"field": "service"},
    "version": {"value": ""}
  }
}
```

- **`url`** points at a JSON *array* of host rows. A bare `host/path.json` implies `https://`. (JSON-list sources only — CSV / transform-heavy lists stay built-in.)
- **`mapping`** turns each raw row into a dyva host entry. Each target field is either:
  - `{"field": "x"}` — copy `row["x"]`, or
  - `{"value": c}` — a constant for every row.
- Map at least **`server`** (the host — e.g. from a `url` field) and **`models`** (a list of model-name strings).
- **`service`** defaults to `ollama` when you don't map it (or when a row lacks the mapped field). Set it to `a1111` or `comfyui` for image hosts so they classify under the dashboard's Image tab and feed txt2img. A missing field falls back to its default rather than becoming null.

Because graflex already emits rows with `service`, `url`, `models`, and `checked`, importing a graflex list is just `server ← url`, `models ← models`, `service ← service` — no transforms needed. The **Add source**, **Add from url**, and **Test sources** buttons on the Settings tab help build and sanity-check a mapping (Test reports how many hosts/models each source yields, and prints a row's keys when a mapping matches nothing).
