#!/usr/bin/env python3
"""Extract canonical per-tag model sizes/digests from a LOCAL ollama.com wget mirror.

ollama.com's own tag pages are the authoritative source for what each published
tag actually weighs (and its digest) — unlike the survey, which only sees whatever
strangers happen to host. graflex harvests; this reads the mirror and emits a JSON
dyva can consume (canonical sizes for the size facet, and a reference the cleanse
can compare observed digests/sizes against to spot renamed spoofs).

The mirror is a `wget`-style tree: <mirror>/library/<model>/tags is the tags page,
each tag row rendered as:  <span>DIGEST12</span> • <SIZE> • <context> .

Usage:  build_ollama_sizes.py <mirror_dir> [out.json]
        (out defaults to ../dyva/ollama-latest-sizes.json relative to this file)
"""
import os, re, sys, json, datetime

ANCHOR = re.compile(r'href="/library/([^":/]+):([^"/]+)"')
SIZE = re.compile(r'([0-9]+(?:\.[0-9]+)?)\s*([GMK])B', re.I)
DIGEST = re.compile(r'\b([0-9a-f]{12,64})\b')
_MULT = {"K": 1_000, "M": 1_000_000, "G": 1_000_000_000}


def _bytes(num, unit):
    return int(float(num) * _MULT[unit.upper()])


def parse_tags(path, model):
    """{tag: {size_str, bytes, digest}} for one model's tags page."""
    h = open(path, encoding="utf-8", errors="replace").read()
    anchors = [m for m in ANCHOR.finditer(h) if m.group(1) == model]
    # collapse to the FIRST occurrence of each tag, in order (the page renders two
    # anchors per row — mobile + desktop); each tag's row runs to the next tag's.
    seen, ordered = set(), []
    for m in anchors:
        if m.group(2) not in seen:
            seen.add(m.group(2))
            ordered.append(m)
    out = {}
    for i, m in enumerate(ordered):
        tag = m.group(2)
        seg = h[m.start(): ordered[i + 1].start() if i + 1 < len(ordered) else len(h)]
        sm = SIZE.search(seg)
        if not sm:
            continue
        dm = DIGEST.search(seg)
        out[tag] = {"size_str": f"{sm.group(1)}{sm.group(2).upper()}B",
                    "bytes": _bytes(sm.group(1), sm.group(2)),
                    "digest": dm.group(1) if dm else None}
    return out


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    mirror = argv[0]
    here = os.path.dirname(os.path.abspath(__file__))
    out_path = argv[1] if len(argv) > 1 else os.path.join(here, os.pardir, "dyva", "ollama-latest-sizes.json")
    lib = os.path.join(mirror, "library")
    if not os.path.isdir(lib):
        print(f"no {lib} — is that the mirror root?", file=sys.stderr)
        return 1
    models = {}
    for model in sorted(os.listdir(lib)):
        tags_path = os.path.join(lib, model, "tags")
        if not os.path.isfile(tags_path):
            continue
        tags = parse_tags(tags_path, model)
        if not tags:
            continue
        entry = {"tags": tags}
        if "latest" in tags:
            entry["latest"] = tags["latest"]
        models[model] = entry
    doc = {"_source": "local ollama.com wget mirror",
           "_generated": datetime.datetime.now().isoformat(timespec="seconds"),
           "_count": len(models),
           "models": models}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    withlatest = sum(1 for m in models.values() if "latest" in m)
    print(f"{len(models)} models ({withlatest} with :latest) -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
