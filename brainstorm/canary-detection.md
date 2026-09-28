# Canary detection — finding the hosts that trip Cloudflare soft-blocks

Early brainstorm. NOT being built yet — this is data-collection thinking; attribution
and any action are explicitly deferred until there's data to look at.

## The idea

Some surveyed/routed hosts are canaries: merely connecting to them gets our egress IP
dropped into a Cloudflare (and OVH, etc.) temporary soft-ban pool. We want to find
*which* hosts those are. The plan:

1. Record every host we touch, with a timestamp.
2. Periodically curl a CF-fronted site (imdb / huggingface / cloudflare trace) and
   record whether we're currently soft-blocked.
3. Later, correlate: a block that appears at time T implicates the hosts touched in
   the window leading up to T. Across many block events the real canary floats out.

## Why it can work (mechanics)

- The soft-ban fires on the **TCP connect itself**, not a completed request — so the
  thing to log is the connect, and just *reaching* a canary is enough to get flagged.
- The block lands on **our egress IP**, is **temporary**, and **expires on its own**.
- Corollary: it's **per-egress-IP**, and graflex (NJ) and dyva (LA) are on separate
  machines with separate egress IPs. So each machine logs its OWN touches and probes
  its OWN block-state — the correlation is per-egress, never merged across machines.

## Decisions so far

- **Scope = data collection only.** Build the recording; do not yet attempt
  attribution/bisection/scoring or any remediation ("we can't assess action").
- **Touch log = dyva touches only** (routing / inference / doorknock connects), NOT
  graflex's mass survey. Rationale: dyva is the continuous day-to-day egress that most
  plausibly accretes a ban; a graflex pass is tens of thousands of connects (huge, and
  bursty/known). Revisit if a canary turns out to only ever be hit during a survey.
- Per-machine, per-egress logging (see mechanics).

## Open questions (deliberately unresolved)

- **Probe cadence & detection.** How often to probe, which site(s), and how to detect
  "blocked" (CF `1020` / `403` / challenge/interstitial vs a normal `200`). Fixed timer
  vs adaptive (probe harder right after bursts of fresh-host touches) vs
  transition-only logging. The probe must not itself become a problem (hammering the
  canary site, or getting us blocked *for probing*). Undecided.
- **Attribution / correlation algorithm.** Window-intersection across many block
  events, active isolation/bisection, per-host suspicion scoring — all deferred until
  we have collected data. Whatever we do must be ADDITIVE with provenance, never a
  destructive reduction.
- **Log format / location / rotation.** A touch log is append-only and can grow; it
  must be BOUNDED (rotation / cap) from day one — cf. the failed-payloads log that hit
  ~15 GB. Likely a jsonl per machine ({ts, host, action}). TBD.
- **Touch resolution.** Enough to correlate against block-transition timestamps — at
  minimum {ts, host}; action/kind optional.

## Related

- soft-ban-fires-on-tcp-connect (drop canaries at candidate selection once known —
  cloudskipper is the existing structural avoider).
- honeypot-detection-non-invasive (the existing fake-catalog + one /wp-login.php gate)
  and brainstorm/honeypot-score.md — a different signal (fake catalog), but the same
  end goal of not-touching-the-bad-hosts. Canary detection is the network-reputation
  side; honeypot scoring is the content side.
- Once canaries are identified, the natural sink is the cloudskipper / a canary
  denylist that drops them BEFORE any connect (since the connect is what flags us).
