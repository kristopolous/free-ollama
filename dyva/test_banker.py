"""Tests for the banker. Plain asserts, no pytest — `python3 dyva/test_banker.py`.

Time is injected everywhere, so TTL behaviour is tested without sleeping.
"""
import os
import sys

# import the module directly, not via the dyva package — dyva/__init__.py pulls in
# aiohttp and the whole server, and the banker deliberately depends on neither
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from banker import Banker   # noqa: E402

T = 1_000_000.0      # a fixed "now" to build on
FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def test_depth_is_exclusive_by_default():
    b = Banker()
    w = b.withdraw("qwen", ["h1", "h2"], 2, now=T)
    check("two free hosts both lease", w.hosts == ["h1", "h2"], w)
    # a second asker gets nothing from the same two
    w2 = b.withdraw("qwen", ["h1", "h2"], 2, now=T)
    check("same hosts cannot be leased twice", w2.hosts == [], w2)
    check("...and that counts as starved", w2.starved is True, w2)


def test_underfill_rather_than_fail():
    b = Banker()
    b.withdraw("qwen", ["h1", "h2"], 2, now=T)          # take both
    w = b.withdraw("qwen", ["h1", "h2", "h3"], 3, now=T)  # only h3 is free
    check("asks for 3, gets the 1 that's free", w.hosts == ["h3"], w)
    check("under-fill is flagged starved", w.starved is True, w)


def test_starved_vs_no_candidates():
    b = Banker()
    none = b.withdraw("qwen", [], 3, now=T)
    check("no candidates at all is NOT starved (fatal, not retryable)",
          none.hosts == [] and none.starved is False, none)
    b.withdraw("qwen", ["h1"], 1, now=T)
    all_busy = b.withdraw("qwen", ["h1"], 1, now=T)
    check("candidates exist but all leased IS starved (come back later)",
          all_busy.hosts == [] and all_busy.starved is True, all_busy)


def test_deposit_frees():
    b = Banker()
    b.withdraw("qwen", ["h1"], 1, now=T)
    check("leased host is busy", b.busy("h1", now=T) is True)
    b.deposit("h1", now=T)
    check("deposited host is free again", b.busy("h1", now=T) is False)
    check("and can be leased by the next asker",
          b.withdraw("qwen", ["h1"], 1, now=T).hosts == ["h1"])


def test_leaked_lease_self_heals():
    """The failure mode that killed the old 'active hosts set' idea: something
    raises on an unusual path and nobody ever deposits. Expiry must fix it."""
    b = Banker(lease_ttl=300)
    b.withdraw("qwen", ["h1"], 1, now=T)          # ...and then never deposit
    check("still held just before expiry", b.busy("h1", now=T + 299) is True)
    check("released by itself after the lease TTL", b.busy("h1", now=T + 301) is False)


def test_mru_is_earned_only_by_served():
    b = Banker()
    b.withdraw("qwen", ["h1"], 1, now=T)
    check("a withdraw alone earns no MRU", b.warm("qwen", now=T) == [])
    b.served("qwen", "h1", now=T)
    check("real work earns MRU", b.warm("qwen", now=T) == ["h1"])


def test_mru_orders_warm_first_most_recent_first():
    b = Banker()
    b.served("qwen", "h1", now=T)
    b.served("qwen", "h2", now=T + 1)
    b.served("qwen", "h3", now=T + 2)
    check("most recently served first", b.warm("qwen", now=T + 3) == ["h3", "h2", "h1"])
    got = b.order("qwen", ["h9", "h1", "h8", "h3"], now=T + 3)
    check("order() puts warm first, keeps the rest", got == ["h3", "h1", "h9", "h8"], got)
    check("order() DROPS NOTHING (advisory only)", len(got) == 4, got)


def test_mru_expires_at_keepalive():
    """A stale entry is worse than none: it sends an agent to a cold host while
    telling it the host is warm."""
    b = Banker(mru_ttl=270)
    b.served("qwen", "h1", now=T)
    check("warm inside the window", b.warm("qwen", now=T + 269) == ["h1"])
    check("forgotten past the window", b.warm("qwen", now=T + 271) == [])


def test_mru_is_per_model():
    b = Banker()
    b.served("qwen", "h1", now=T)
    check("warm for qwen", b.warm("qwen", now=T) == ["h1"])
    check("not warm for gemma", b.warm("gemma", now=T) == [])


def test_warm_host_still_respects_depth():
    """Preference and capacity are orthogonal. A warm host must not become a magnet
    that everyone piles onto — that is the bug we are fixing, not a feature."""
    b = Banker(depth=1)
    b.served("qwen", "h1", now=T)
    first = b.withdraw("qwen", ["h1", "h2"], 1, now=T)
    check("warm host goes out first", first.hosts == ["h1"], first)
    second = b.withdraw("qwen", ["h1", "h2"], 1, now=T)
    check("second asker does NOT also get the warm host", second.hosts == ["h2"], second)


def test_the_actual_pileup_scenario():
    """15 agents, 4 hosts, launched together — the worker dump that started this.
    Before: all 15 read 'idle' and 3 machines took 4-6 jobs each at 0.4 t/s."""
    b = Banker(depth=1)
    hosts = ["h1", "h2", "h3", "h4"]
    got, starved = [], 0
    for _ in range(15):
        w = b.withdraw("muse", hosts, 1, now=T)
        if w.hosts:
            got.append(w.hosts[0])
        if w.starved:
            starved += 1
    per_host = {h: got.count(h) for h in set(got)}
    check("only as many agents placed as there are slots", len(got) == 4, got)
    check("no machine took more than one job", max(per_host.values()) == 1, per_host)
    check("the other 11 are told 'come back later', not 'no hosts'", starved == 11, starved)
    # and as races resolve, the freed hosts go straight back out
    b.deposit_all(["h1", "h2"], now=T + 1)
    nxt = b.withdraw("muse", hosts, 2, now=T + 1)
    check("deposited hosts are immediately re-issued", sorted(nxt.hosts) == ["h1", "h2"], nxt)


def test_depth_above_one():
    b = Banker(depth=2)
    check("first two fit", [b.withdraw("q", ["h1"], 1, now=T).hosts for _ in range(2)]
          == [["h1"], ["h1"]])
    check("third does not", b.withdraw("q", ["h1"], 1, now=T).hosts == [])
    check("load reports 2", b.load("h1", now=T) == 2)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n{t.__name__}")
        t()
    print(f"\n{'=' * 60}")
    if FAILED:
        print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
        return 1
    print(f"all {sum(1 for _ in tests)} test groups passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
