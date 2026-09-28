# graflex sample — the publishable "sample platter"

Notes / design sketch. Not built yet.

## Goal

Let someone clone the demos and get image / video / speech / text generation
working **without** shipping the whole survey (tens of thousands of hosts) and
without concentrating traffic on a hand-picked "best hosts" list. The published
artifact is a small, statistically-sampled subset — a *sample platter* — that
reproduces the authentic dyva experience: it might work, it might not, but it's
representative of what's actually out there.

Explicitly **not** a curated winners list. Cherry-picking the most reliable
hosts would (a) misrepresent dyva and (b) concentrate load/attention on a few
third-party machines, accelerating their discovery and soft-ban. A random
sample spreads exposure and stays honest.

## The command

`graflex sample` — a new subcommand alongside `check` / `combine`. It draws the
platter and **writes a new generated file** (e.g. `graflex-sample.json`).

**It destroys nothing.** All inputs (working pools, combine snapshot, reputation
db) are read **read-only**; the only write is the new output file. Sampling is
*selection into an output*, never deletion from a source — no host, row, or
record is ever removed, soft-deleted, or overwritten by this command. The output
is generated on demand, not committed to git history — so there is also no
permanent public host list living in the repo.

## What goes in it — a stratified platter, not a flat percentage

Fixed small counts per capability bucket (so no bucket ever comes up empty the
way a flat 4% draw could for a rare family like music). All counts tunable:

| bucket  | default count |
|---------|---------------|
| image   | 8             |
| video   | 8             |
| edit    | 4             |
| music   | 4             |
| speech  | 3             |
| text    | 30            |

The user's framing: "8 image, 8 video, a few speech, and then like maybe 30 or
so generative text hosts we've successfully gotten a response from … not
actually 4%, more like a sample platter."

## Source pool per bucket — "hosts we've successfully gotten a response from"

Sample only from hosts with a proven successful response, per bucket:

- **text**: `ollama-working.json` / openai-working, intersected with dyva's
  *good* reputation (`host_status` good `(host, model)` rows, `is_query=0 AND
  deleted_at IS NULL`) where we have it.
- **media** (image/edit/video/music/speech): the per-service working pools
  (comfyui / a1111) filtered by the hand-curated classifier's capability `kind`
  (graflex/model-classifier.json).

Soft spot to decide: for **text**, "got a response" is a clean signal. For
**media**, the strongest signal graflex owns is *reachable + has-the-model-kind*
(working pool). True generation-success is a sparse dyva-runtime fact (recorded
media jobs), not a graflex fact. Two options for the media buckets:
1. sample from working + classified only (simple, what graflex knows), or
2. **prefer** hosts with a recorded successful dyva media job when one exists,
   fall back to working+classified to fill the count.

## Sampling method

Uniform random draw **within each bucket**. Re-running `graflex sample`
re-draws, so which hosts get exposed **rotates** — that rotation is itself the
liability mitigation (no host is permanently "the published one"). No fixed
seed (fresh each run); a `--seed` could be added if reproducibility is ever
wanted.

## Refresh

`graflex sample` runs on the combine cadence. Each run regenerates the platter
off the latest working snapshots — a fresh draw, so newly-working hosts can
appear and hosts that have gone dark simply aren't picked this time (they remain
untouched in the source pools). Exposure rotates. Optional: quick-probe each
pick at publish time and, if it's dead, leave it *out of the generated platter*
(a filter on the output only — the source records are never modified) so the
shipped file is verified-live at that moment (more work per run).

## Output shape

A `load_servers()`-compatible list of server records (service / server / models
/ provider / caps …) so **dyva consumes it with zero translation** — it's just
another pool file in the shape dyva already reads.

## Consumption in dyva

A pluggable pool source: `--pool <path|url>` (or a `pool` setting) that loads the
platter *instead of* the local survey. Local file path now; a **gossiper** URL
drops into the same slot later without changing the consumer. (Gossiper — the
planned sync/reference server — isn't in the code yet; the pluggable source is
what lets us revisit it without redesign.)

## Liability posture

- small per-bucket counts (a few dozen hosts total, not tens of thousands)
- proven-responsive hosts only
- re-sampled / rotating exposure — never pin the same hosts
- generated on demand, not committed to git
- we are redistributing other people's open infra: keep it minimal, keep it
  rotating.

## Open decisions to tune

1. Exact bucket counts (table above are defaults).
2. Media source signal: working+classified only, vs. prefer recorded dyva
   media-job success.
3. Publish-time drop-dead probe: yes/no.
4. Output filename / path and whether dyva's `--pool` accepts a URL from day one
   or file-only until gossiper exists.
