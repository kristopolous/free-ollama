"""The banker: single owner of which hosts are in use, and which are warm.

Everything that picks a host today reads shared state and then acts on it — the
classic check-then-act, and `is_active`'s own docstring concedes the race ("two
requests that check within the same instant can both pick it"). That was tolerable
when a collision cost a slower render. With subagents it is not: 15 agents launched
inside 8 seconds all read "idle" and all piled onto the same 3 machines, which then
served 4-6 jobs each at 0.4 t/s.

The banker deletes the pattern rather than narrowing the window. Nobody grabs a host;
they ask, and one object decides. It owns two things:

  LEASES  - who is using a machine right now, capped at `depth` jobs per host. A
            lease EXPIRES. That is not a nicety: the reason dyva derives busyness
            from the worker registry instead of an "active hosts" set is that a set
            needs whoever put a host in to take it back out, and the first unusual
            code path that raises leaves it busy forever. Expiry is what makes this
            safe where that would not be — a deposit is only an early return.

  MRU     - which hosts recently did real work for a model, and are therefore still
            likely to have it resident. ollama holds a model for ~4-5 minutes after
            use, so a host that served 30 seconds ago is warm and free; we just had
            nowhere to write down more than one of them. The existing sticky is a
            depth-1 MRU, which is exactly the wrong depth when 5 agents want the
            same model and 5 hosts are warm: one benefits, four go cold.

MRU credit is earned ONLY by real work (`served`), never by a test. A probe proves a
host is real; work proves it is warm. Keeping that absolute means MRU credit has one
write site, and it makes it structurally impossible for a honeypot's canned reply —
which only ever appears during a probe — to enter the warm set.

Pure data structure: no I/O, no dyva imports, `now` injectable so the TTLs can be
tested without sleeping.
"""
import collections
import time

# A lease outlives a realistic cold start, or we would yank a host mid-load and hand
# it to someone else, and nobody would ever finish loading anything.
LEASE_TTL = 300.0
# Roughly ollama's keep_alive. Past this the model is unloaded and an MRU entry is a
# lie — worse than no entry, because it sends an agent to a cold host while telling
# it the host is warm.
MRU_TTL = 270.0
# Jobs per host. 1 = exclusive.
DEPTH = 1


class Withdrawal:
    """What came back from a withdraw.

    `hosts` may be shorter than asked for — under-filling is deliberate. With 15
    agents against 4 hosts, all-or-nothing deadlocks the whole fleet; 2 hosts to race
    beats none.

    `starved` distinguishes the two empty cases that are conflated today and must not
    be: nothing in the catalog serves this model (fatal) versus every host that does
    is leased right now (come back later). Both currently surface as "no server for
    X", which is a lie half the time.
    """

    __slots__ = ("hosts", "starved")

    def __init__(self, hosts, starved=False):
        self.hosts = hosts
        self.starved = starved

    def __bool__(self):
        return bool(self.hosts)

    def __repr__(self):
        return f"Withdrawal(hosts={self.hosts!r}, starved={self.starved})"


class Banker:
    def __init__(self, depth=DEPTH, lease_ttl=LEASE_TTL, mru_ttl=MRU_TTL):
        self.depth = depth
        self.lease_ttl = lease_ttl
        self.mru_ttl = mru_ttl
        # host -> list of lease expiry timestamps (length == jobs currently on it)
        self._leases = collections.defaultdict(list)
        # model key -> {host: last served ts}, most recent last
        self._mru = collections.defaultdict(collections.OrderedDict)

    # ---- internals ---------------------------------------------------------

    def _now(self, now):
        return time.time() if now is None else now

    def _reap(self, now):
        """Drop expired leases and stale MRU entries. Called on every operation, so
        a leaked lease self-heals without a sweeper task."""
        for host in list(self._leases):
            live = [e for e in self._leases[host] if e > now]
            if live:
                self._leases[host] = live
            else:
                del self._leases[host]
        cutoff = now - self.mru_ttl
        for key in list(self._mru):
            entries = self._mru[key]
            for host in [h for h, ts in entries.items() if ts <= cutoff]:
                del entries[host]
            if not entries:
                del self._mru[key]

    def _free(self, host):
        return len(self._leases.get(host, ())) < self.depth

    # ---- queries -----------------------------------------------------------

    def busy(self, host, now=None):
        now = self._now(now)
        self._reap(now)
        return not self._free(host)

    def load(self, host, now=None):
        """Jobs currently leased on this host."""
        now = self._now(now)
        self._reap(now)
        return len(self._leases.get(host, ()))

    def warm(self, key, now=None):
        """Hosts that did real work for `key` inside the keep_alive window, most
        recently used FIRST."""
        now = self._now(now)
        self._reap(now)
        return list(reversed(list(self._mru.get(key, ()))))

    def order(self, key, hosts, now=None):
        """`hosts` reordered warm-first, caller's order preserved otherwise.

        ADVISORY ONLY. This reorders candidates, it never substitutes for the
        caller's filtering — a condemned or bad host that somehow reached the MRU
        still has to survive selection. An MRU hit must never become an implicit
        authorization; that is precisely how a honeypot with a self-earned 'good'
        state skipped its smoke test.
        """
        now = self._now(now)
        self._reap(now)
        warm = {h: i for i, h in enumerate(self.warm(key, now))}
        return sorted(hosts, key=lambda h: (warm.get(h, len(warm)),))

    # ---- allocation --------------------------------------------------------

    def withdraw(self, key, candidates, n, now=None):
        """Lease up to `n` of `candidates`, warm-first. Under-fills rather than
        failing. `starved` is set when we returned nothing (or less than asked) only
        because everything was already leased."""
        now = self._now(now)
        self._reap(now)
        if not candidates:
            return Withdrawal([], starved=False)
        picked = []
        for host in self.order(key, candidates, now):
            if len(picked) >= n:
                break
            if self._free(host):
                self._leases[host].append(now + self.lease_ttl)
                picked.append(host)
        starved = len(picked) < min(n, len(candidates))
        return Withdrawal(picked, starved=starved)

    def deposit(self, host, now=None):
        """Return one lease. Call this the instant a race RESOLVES, not when the
        request finishes: the losers are still warm and the next fanout should get
        them while they are, and holding them until completion drains the pool."""
        now = self._now(now)
        self._reap(now)
        held = self._leases.get(host)
        if held:
            held.pop(0)
            if not held:
                del self._leases[host]

    def deposit_all(self, hosts, now=None):
        for h in hosts:
            self.deposit(h, now)

    # ---- earning the keep --------------------------------------------------

    def served(self, key, host, now=None):
        """This host produced REAL OUTPUT for `key` — tokens or tool calls. The only
        thing that earns an MRU entry. Tests never call this."""
        now = self._now(now)
        self._reap(now)
        entries = self._mru[key]
        entries.pop(host, None)     # re-insert so ordering stays most-recent-last
        entries[host] = now

    # ---- introspection -----------------------------------------------------

    def snapshot(self, now=None):
        now = self._now(now)
        self._reap(now)
        return {
            "depth": self.depth,
            "leased": {h: len(v) for h, v in self._leases.items()},
            "warm": {k: list(reversed(list(v))) for k, v in self._mru.items()},
        }
