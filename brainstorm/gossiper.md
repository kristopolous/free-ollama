ok i want to talk about a feature i've been thinking about for a while now. The graflex/dyva divide is nice because the dyva runtime uses the graflex survey time data and so it naturally solves the distribution problem. However, dyva keeps these databases of additional information, such as if a host passes a smoke test and if it's a real endpoint ... this goes back to an issue I had with using sqlite to begin with, syncronizations ... anyway let me lay it out:

  1. I agree that sqlite is a reasonable store for these inference time survey methods
  2. Inference time surveying is valuable data that should be able to be distributed among the hosts
  3. This is inherently two-way, unlike the producer/consumer graflex model
  4. The bad survey problem might be irrelevant. A dyva host can do a fire-and-forget sync mechanism and if they're network is doing some mass false-negative failure they will have a very low chance of
  successfully firing and forgeting their phony update.
  5. There's many ways of doing this - crdt, cr-sqlite, or maybe some really cheap mechanism of a few endpoints that support like glob-model querys GET /realtime?source=spider&model="qwen2.5*" POST /realtime {sqlite row} ...
  6. this leaks host data so we can't just have a global system instead it has to be a graflex source ... currently we have { name, url, mapping } this would now need a { name, url, sync, mapping }
  7. the request and api should be namespaced. I know this isn't real protection ... we can talk about it.

There's only one absolutely positive thing I'd like to do right now: support a -c, --config where you can point to something other than $HOME/.cache/free-ollama ... this is the one split that prevents multiple dyva's from running on a single box.

The other things we should discuss


================================================================================
CONVERGED DESIGN (discussion, not yet built)
================================================================================

STATUS (2026-09-12): PARKED, not building now. In practice hosts are secured fast
(~seconds), so the cold-start pain the gossiper solves isn't real today. Trigger to
revisit: run a cold start with the graflex SOURCE removed and measure — if securing
hosts then takes serious time, the gossiper is worth building; otherwise it stays
parked. The honeypots.json distribution (below/related) rides on this same parked
sync, so it waits too.

-c/--config : DONE. `dyva -c <DIR>` repoints every on-disk store (reputation DB,
jobs/chats DBs, caches, images/audio/video/music dirs, settings, survey, cleansed
log) via _apply_cache_dir(), run right after arg-parse. Package-relative files
(the classifiers) stay put. Lets a second instance run alongside the default.

--- The problem being solved -------------------------------------------------

Two concrete pains, both in the gap between SURVEY time and INFERENCE time (for
our sources that delta is up to ~100h; unknown for others):

  1. Liveness gap. Survey is stale; hosts vanish. At inference dyva pays to
     re-doorknock, find things stale/unreachable, stumble around.
  2. Honeypot / smoke cost. Finding a fake/honeypot endpoint, running the smoke
     test, and tossing it is expensive per instance.

If another dyva doorknocked the same host recently, we shouldn't repeat that.

--- The shape we converged on: pull-on-demand positive leads --------------------

NOT replication, NOT a CRDT, NOT a background sync, NOT a mine.db/shared.db split.
It is a query-time fallback, pulled only when a dyva is about to do expensive
discovery:

  * Endpoint (per the #5 sketch):
      GET /realtime?source=<name>&model=<glob>
        -> recently-good (host, model, last_good) rows for that source, freshest
           first. "yo, hey server, have any news on qwen2.5* ?" -> "try a,b,c,d".
      POST /realtime {row}   (the producer side — see call sites)

  * Two call sites, same endpoint:
      - Inference fanout: when dyva would otherwise begin cold-probing UNKNOWN
        hosts for a model, it first asks the configured peer(s) and splices their
        answers in as high-priority candidates.
      - __dyva_info__:test / :test-all: before probing a batch, ask "know the
        status of any of these?" and skip the ones a peer freshly characterized.

  * :test-all is the natural PRODUCER. It is literally what establishes
    smoke_ok / good, so its verdicts are the freshest status worth handing back
    out (POST). The loop closes: one instance's sweep feeds the next instance's
    sweep and fanout.

--- PUSH trigger: debounced-on-success (NOT the unknown->good edge) -------------

  Why not the edge: "unknown" is the ABSENCE of a verdict row (tier rank is
  recent -> good -> maybe_good -> unknown(absent) -> bad). A good row persists;
  failures demote good->maybe_good->bad (still a row), nothing resets good back to
  unknown on a timer. So `unknown -> good` fires ~ONCE per (host,model) ever — a
  cold-start BURST as a fresh instance warms up over the survey, then near-total
  silence. That silence is the trap: a warm instance never re-crosses the edge, so
  it never re-pushes, so the gossiper is refreshed ONLY by new/cold instances — it
  goes stale for a steady or single operator. Push-rarity and server-freshness are
  the same coin; the edge is too rare to keep the gossiper warm.

  * The trigger is a SUCCESSFUL result, DEBOUNCED to at most once per (host,model)
    per window (e.g. once / 6h). That keeps warm instances re-attesting from actual
    use (so the stack stays fresh), while the debounce is the hard traffic cap —
    not per-request, not per-edge. Usage-driven re-attestation, bounded.
  * Positive-only: we push successes, never failures. A failed attempt produces no
    push (the poisonable, non-self-correcting negative stays off the wire).

--- The store: most-recent-N, no TTL, eviction by displacement ------------------

  * The gossiper is a pure recency stack: most-recent ~N (e.g. 50) per source/model,
    each row carrying its DISCOVERY TIME. A push goes to the top. NO time-based
    expiry, no server-side "too stale" policy — the server is that dumb.
  * Dead nodes drop off NATURALLY BUT SLOWLY, positive-only: nothing tells the
    gossiper a host died (no negative push). A stale entry just sinks as fresh
    successes pile on top and eventually falls past N. Edge case, working as
    intended: client pulls list, tries #1 -> fails, tries #2 -> succeeds -> #2
    re-pushed to the top, #1 drifts down and is eventually buried.
  * Freshness is IMPLICIT, no 36h constant: effective window = N / push-rate. A
    busy source cycles the 50 slots fast (tight freshness); a quiet source cycles
    slow (looser) — the right behavior. The RECEIVER still gets each row's
    timestamp and applies its own "too stale to bother" preference, so displacement
    (store side) + timestamp skip (receiver side) = belt and suspenders, still zero
    server-side time logic.
  * Cost of "slowly": a stale entry near the top costs a few clients one failed
    attempt each (fanout just moves to #2) before successes bury it — and crucially
    NO negative push, so it stays positive-only and unpoisonable.

--- The RECEIVER owns the judgment --------------------------------------------

  * For each returned (host, time) it checks its OWN db: "3.4.5.6 isn't in my
    database — probably offline / not my copy of this source — skip. 2.3.4.5 — I
    have that — fresh-good lead, try it." Skip-if-unknown keeps it in its lane: it
    syncs fresh STATUS on shared-survey hosts, NOT host discovery (importing a
    never-surveyed host would be a separate, bigger feature, out of scope).
  * No echo-back (the sender already knows the host is good), no dedup, no
    coordination. A stale server entry for a host YOU already know costs nothing —
    you read your own db; the gossiper only ever fills blanks.

  * STATELESS / NO PROVENANCE — the keystone that makes it trivial to build.
    Neither side remembers anything beyond its own plain table:
      - The client does NOT tag a host "came from the server." A pulled lead just
        becomes an ordinary candidate and flows through the SAME try-it path as a
        cold-discovered one; a success runs the SAME debounced-on-success push. No
        "is this a sync host?" special case, no "did the server suggest it?" flag.
      - The server does NOT track "I sent X to that client." No per-client state,
        no "already told you" dedup. On a report it writes the row to the top; on
        a query it reads the top-N. That's all.
    Consequence: NO new code paths for "synced" hosts, no bookkeeping tables. The
    feature is just (a) a pull that seeds the normal unknown-candidate list and
    (b) the normal edge push — both reusing the EXISTING host-status machinery.
    (Same principle as dyva's routing: no parallel mechanism, just existing rules
    firing.)

--- Why this is safe (the whole point) -----------------------------------------

We feared the graflex mass-flagging failure (a bad pass gutting a populated pool).
This design sidesteps it by sharing POSITIVE LEADS ONLY:

  * Skip / seed on a peer's POSITIVE ("recently good / smoke_ok"). Re-verifying a
    fresh positive is pure waste, and if the positive is wrong your own attempt
    re-grades it normally (good -> maybe_good -> ...). Self-correcting.
  * ALWAYS establish NEGATIVES yourself. Never skip a host because a peer called
    it bad/fake — that is the sticky, non-self-correcting inheritance (you'd skip
    it forever and never re-test). The hot inference path avoids junk IMPLICITLY
    by only chasing positive leads (the peer simply doesn't list what isn't
    working), so it never stumbles into honeypots in the first place. The
    maintenance sweep re-verifies negatives locally — cheap enough, not hot path.

  Net: the `null -> smoke_fail` sync we agonized over isn't needed. Everything
  rides on sharing POSITIVE "good" status only (pushed debounced-on-success) plus
  the fill-blanks application rule below — both safe and self-correcting.

  Only-fill-blanks rule: a received row is applied IFF local state is the "I don't
  know" state (unknown / null). A peer can never overwrite something you measured,
  so it can't gut a pool of things you actually know.

--- Scope / threat model (#6, #7): "cookies on the top shelf" -------------------

  The goal is DETERRENCE OF LAZY ABUSE, not confidentiality — explicitly a non-goal.
  The harvesting tools are distributed anyway; the private source just raises the
  bar above `uvx dyva` so that grabbing a big host list takes reading payloads and
  patching code (and may trip a HuggingFace block). A public demo server already
  runs with the "secret" source; you just have to do meaningfully more than the
  one-liner to reach it. That friction IS the feature; don't over-engineer it into
  crypto.

  * Per source, enforced at the PRODUCER/PUSH side: an instance only pushes hosts
    from a sync-enabled SHARED source; private / federation-source hosts aren't
    pushed. This is the sensible default (don't casually spray private hosts), NOT
    a confidentiality guarantee — see next point.
  * Skip-if-unknown at the receiver is correctness, not privacy: by the time a
    client says "2.3.4.5, never heard of it, must be stale" the IP already crossed
    the wire. That's fine under THIS threat model (top-shelf, not a vault) — noted
    only so nobody later mistakes it for a security boundary.
  * Source schema grows: { name, url, mapping } -> { name, url, sync, mapping },
    where `sync` = where to ask for that source (its gossiper endpoint).
  * Namespacing by source name partitions the data; not real protection, and not
    meant to be. No malicious-abuse hardening (effectively a single operator). A
    per-source shared secret in .env could gate POST later if ever wanted (never in
    code, no signpost) — out of scope.

--- THE GOSSIPER (the reference sync server) -----------------------------------

  Name, in the dyva/graflex/woahllama spirit: "the gossiper" — the endpoint dyva
  gossips fresh good-host status to and pulls it from.

  * It is a USER-CONFIGURABLE, USER-REMOVABLE setting (sources are a setting, not
    an assumption): point it at the public demo gossiper, run your own, or drop it
    entirely. Per-source `sync` holds the gossiper endpoint for that source.
  * The public demo gossiper is the default reference instance; nothing forces an
    operator to use it or to keep it.

--- Open knobs (still undecided) -----------------------------------------------

  * The fanout THRESHOLD that triggers the ask (exhausted local good hosts for the
    model? first K attempts failed? about to cold-probe unknowns?).
  * The receiver-side freshness preference (how old a discovery time is still worth
    a try — 36h is the working example; a per-instance knob, not a server TTL).
  * The server's response bound N (most-recent ~50 good discoveries per source/model).

RESOLVED: push is edge-triggered on unknown->good (see the PUSH section). That was
the "every time is too much" worry — it's once per rediscovery, not per request.

MVP = the pull (GET) alone, consumed by fanout + :test-all, positive-leads-only,
per source. The edge-triggered POST/producer + server TTL cache layer on next;
thresholds and the cutoff value are the remaining tuning.
