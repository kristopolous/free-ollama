"""Tests for the `web` tool's search — pytest (`uv run pytest dyva/test_web_search.py`).

Only the pure, offline pieces: mapping Keenable's JSON response to our {title,url,snippet}
shape, the plain-text result formatter, and the per-IP throttle. No network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dyva   # noqa: E402


DATA = {"query": "asyncio", "mode": "pro", "results": [
    {"title": "Python asyncio: A Walkthrough", "url": "https://realpython.com/async-io-python/",
     "snippet": "A hands-on intro to async IO in Python.", "description": "longer desc"},
    {"title": "asyncio — Asynchronous I/O", "url": "https://docs.python.org/3/library/asyncio.html",
     "description": "The standard library reference."},     # no snippet -> use description
    {"title": "no url here"},                                # dropped (no url)
    {"url": "https://example.com/three"},                    # title -> url; empty snippet
]}


def test_keenable_maps_title_url_snippet():
    rows = dyva._parse_keenable(DATA, 8)
    assert rows[0] == {"title": "Python asyncio: A Walkthrough",
                       "url": "https://realpython.com/async-io-python/",
                       "snippet": "A hands-on intro to async IO in Python."}


def test_keenable_snippet_falls_back_to_description():
    doc = [r for r in dyva._parse_keenable(DATA, 8) if r["url"].endswith("/asyncio.html")][0]
    assert doc["snippet"] == "The standard library reference."


def test_keenable_skips_result_without_url():
    urls = [r["url"] for r in dyva._parse_keenable(DATA, 8)]
    assert urls == ["https://realpython.com/async-io-python/",
                    "https://docs.python.org/3/library/asyncio.html",
                    "https://example.com/three"]   # the url-less result is dropped


def test_keenable_title_and_snippet_fallbacks():
    third = [r for r in dyva._parse_keenable(DATA, 8) if r["url"].endswith("/three")][0]
    assert third["title"] == "https://example.com/three"   # no title -> url
    assert third["snippet"] == ""                           # no snippet/description


def test_keenable_caps_result_count():
    assert len(dyva._parse_keenable(DATA, 2)) == 2


def test_keenable_empty():
    assert dyva._parse_keenable({}, 8) == []
    assert dyva._parse_keenable({"results": []}, 8) == []


def test_format_results_numbered_and_actionable():
    out = dyva._format_search_results("asyncio", dyva._parse_keenable(DATA, 8))
    assert "1. Python asyncio: A Walkthrough" in out
    assert "https://realpython.com/async-io-python/" in out
    assert "call web again with its URL" in out


def test_format_empty_results():
    assert 'No web results for "nothing"' in dyva._format_search_results("nothing", [])


def _reset_throttle(monkeypatch, rpm):
    import asyncio
    monkeypatch.setattr(dyva, "WEB_SEARCH_RPM", rpm)
    monkeypatch.setattr(dyva, "_web_search_lock", asyncio.Lock())
    dyva._web_search_hits.clear()


def test_throttle_no_wait_under_limit(monkeypatch):
    import asyncio
    _reset_throttle(monkeypatch, 3)
    clock = [1000.0]
    slept = []
    monkeypatch.setattr(dyva.time, "monotonic", lambda: clock[0])
    async def fake_sleep(s):
        slept.append(s); clock[0] += s
    monkeypatch.setattr(dyva.asyncio, "sleep", fake_sleep)
    async def run():
        for _ in range(3):           # exactly the limit — none should block
            await dyva._web_search_throttle()
    asyncio.run(run())
    assert slept == []


def test_throttle_waits_when_window_full(monkeypatch):
    import asyncio
    _reset_throttle(monkeypatch, 2)
    clock = [1000.0]
    slept = []
    monkeypatch.setattr(dyva.time, "monotonic", lambda: clock[0])
    async def fake_sleep(s):         # advance the fake clock so the window drains
        slept.append(s); clock[0] += s
    monkeypatch.setattr(dyva.asyncio, "sleep", fake_sleep)
    async def run():
        await dyva._web_search_throttle()   # 1/2
        await dyva._web_search_throttle()   # 2/2 — window now full
        await dyva._web_search_throttle()   # 3rd must wait for the oldest to age out
    asyncio.run(run())
    assert len(slept) == 1 and 0 < slept[0] <= 60


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
