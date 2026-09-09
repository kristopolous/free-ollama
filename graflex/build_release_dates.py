#!/usr/bin/env python3
"""Build dyva's model release-date lookup from the Artificial Analysis leaderboard.

graflex is the harvest-the-wild-internet tool, so scraping the leaderboard lives
here (`graflex.sh get-dates`). This is the BUILD half: it reads the leaderboard
JSON (the Next.js payload, already extracted by the shell pipeline) on stdin or a
file arg, and writes dyva's lookup to stdout or a file arg:

    { "_source": ..., "_count": N,
      "dates": { "<name with punctuation stripped>": "YYYY-MM-DD", ... } }

dyva matches a pool model name against these keys fuzzily (see _model_release_date).
"""
import json
import re
import sys


def norm(s):
    """Lowercase basename with all non-alphanumerics removed — the match key.
    'Qwen2.5 Coder 32B' and 'qwen2-5-coder-32b-instruct' collapse toward the same
    shape, and a pool name's path/quant cruft doesn't block a substring match."""
    return re.sub(r"[^a-z0-9]", "", str(s).split("/")[-1].split("\\")[-1].lower())


def build(doc):
    """Walk the leaderboard payload for every {slug, releaseDate} and index each
    model's slug/name/shortName (normalized) to its date. First writer wins per
    key, so a more specific name doesn't get clobbered by a vaguer one."""
    found = {}

    def walk(o):
        if isinstance(o, dict):
            if o.get("releaseDate") and o.get("slug"):
                found[o["slug"]] = o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc)
    lut = {}
    for m in found.values():
        for key in (m.get("slug"), m.get("name"), m.get("shortName")):
            if key:
                k = norm(key)
                if len(k) >= 4:
                    lut.setdefault(k, m["releaseDate"])
    return {"_source": "artificialanalysis.ai/leaderboards/models",
            "_count": len(found), "dates": lut}


def main(argv):
    src = open(argv[1], encoding="utf-8") if len(argv) > 1 and argv[1] != "-" else sys.stdin
    doc = json.load(src)
    out = build(doc)
    if not out["dates"]:
        sys.stderr.write("build_release_dates: no models found — the payload shape "
                         "probably changed; tweak the extraction.\n")
        return 1
    if len(argv) > 2:
        with open(argv[2], "w", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"), sort_keys=True)
    else:
        json.dump(out, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stderr.write("build_release_dates: %d models -> %d keys\n"
                     % (out["_count"], len(out["dates"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
