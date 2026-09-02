#!/usr/bin/env python3
"""One-off repair for the duplicate entries left in <name>-working.json by the
service@host keying bug: _check_hosts matched existing rows on the *queried*
service while the row stored the *detected* one, so any host re-labelled by the
probe (ollama -> sglang) was appended again on every run instead of updated.

Keeps the newest row per host (by "checked") and writes a .bak first.

    python3 tools/dedupe-working.py ollama [comfyui ...]
"""
import json
import os
import shutil
import sys

CACHE_DIR = os.path.expanduser("~/.cache/free-ollama")


def dedupe(name):
    path = os.path.join(CACHE_DIR, f"{name}-working.json")
    if not os.path.exists(path):
        print(f"{name}: no {path}")
        return
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    best = {}
    for r in rows:
        h = (r.get("host") or (r.get("url") or "").split("://", 1)[-1]).rstrip("/")
        cur = best.get(h)
        if cur is None or r.get("checked", "") >= cur.get("checked", ""):
            best[h] = r
    out = sorted(best.values(), key=lambda r: (r.get("checked", ""), r.get("host", "")))
    if len(out) == len(rows):
        print(f"{name}: {len(rows)} rows, no duplicates")
        return
    shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, path)
    print(f"{name}: {len(rows)} -> {len(out)} rows ({len(rows) - len(out)} duplicates removed; backup at {path}.bak)")


if __name__ == "__main__":
    for n in sys.argv[1:] or ["ollama"]:
        dedupe(n)
