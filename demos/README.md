# dyva demos

Small, self-contained web apps built on top of the [dyva](../dyva) router. Each one
talks to a running dyva server over plain `POST /api/chat` and nothing else.

They look like toys — a model drawing dinosaurs, bots playing hangman — but they're
really **tests of dyva itself**: the dynamic routing, context-window handling,
streaming + mid-stream failover, tool-calling, vision, and how all of it behaves
under sustained load. The games and drawings are just a legible way to watch that
machinery work (and to see it *keep* working when a host is slow, cold, or drops).

Every demo is a single HTML file with **no external/CDN dependencies** — all CSS and
JS is inline, so they run straight off disk or from dyva.

## Running them

dyva serves this directory at `/demos`:

```bash
./dyva.py              # start the router
# then open:
http://localhost:11435/demos/
```

- `/demos` redirects to `/demos/` so the pages' relative links resolve correctly.
- It's a plain static mount (no allow-list) — drop a new `.html` file in here and it's
  served immediately.
- Each page has a **dyva URL** field. It defaults to the origin it was served from,
  derived by stripping the `/demos/…` suffix off the right — so it works behind a
  reverse proxy with a path prefix (e.g. `https://9ol.es/11434`), not just on a bare
  host:port. Opened as a local `file://`, it falls back to `http://localhost:11435`.

All of them stream (`stream: true`) and accumulate the Ollama-style `message.content`
as it arrives — streaming only has to bound time-to-first-token, so a big or slow
model never trips the router's request timeout mid-generation.

## The demos

### 🎨 Artist (`artist.html`)
A vision model is given a prompt, draws **one SVG**, the page rasterizes it to a PNG,
and feeds that render back to the model so it can *see its own work* and improve it —
over N rounds. A **grow-context toggle** switches between iterative improvement
(context keeps growing) and independent one-shots; those are different tests —
context-window extension vs. repeated cold routing. Phase indicators distinguish
routing / cold-start / thinking / drawing so a long wait is legible.

### 🖼️ Art Gallery (`art-gallery.html`)
One prompt, **four framed canvases**, each with its own model-query placard, painted
at once (one-shot, no vision) in a 2×2 grid. After the first pass it flips to
**Modify** mode — each frame's current SVG is fed back as an edit instruction
("make the cow blue"). Tests fanning one request out across four different model
queries in parallel, and short edit-turns.

### 🎲 Game Room (`gameroom.html`)
Models play each other turn by turn across six games — **chess, tic-tac-toe,
pictionary, password, wheel of fortune, hangman**. Illegal moves are welcome; the
board just advances. The point is that **nobody stalls, no request drops, and routing
keeps working** over long runs. Every move shows the host that served it.

Each game panel has:
- **model-query inputs**, one per player (2 for most games, 4 for password / wheel of
  fortune's two teams), colour-coded per player. They're read **live** before every
  request, so you can swap a model mid-game; the current player's box is tinted.
- a **`[ ] thinking` checkbox** — unchecking it sends `think: false`, which dyva
  translates into every known no-think lever (Ollama `think`, OpenAI `reasoning_effort`,
  vLLM/SGLang/llama.cpp `enable_thinking`, OpenRouter `reasoning`, plus the Qwen3
  `/no_think` token) so play speeds up.
- one **Play / Stop** button (it toggles), and a per-move log.

Between them the games exercise the full surface:
- **streaming + failover** — all of them; a host that goes silent mid-stream is
  caught by a watchdog and the turn fails over.
- **tool-calling** — chess (`get_board_state`) and wheel of fortune
  (`spin_wheel_and_guess_letter`, `buy_vowel`, `guess_answer`, `get_board`,
  `finish_turn`).
- **vision** — pictionary (the drawer's SVG is rasterized and shown to the guesser).
- **long / soak runs** — password (20 rounds × up to 10 attempts) and wheel of fortune
  keep the router busy for hundreds of calls; Stop aborts any of them.

## Adding a demo

Drop a self-contained `<file>.html` in this directory (it's served automatically) and
add a card for it to `index.html`. Keep to the conventions above: derive the dyva base
by stripping the `/demos/…` suffix, stream `/api/chat`, and inline everything.
