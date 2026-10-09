#!/usr/bin/env python3
"""Copy screenshots into the work folder under descriptive names. Originals are never touched.

Usage: python stage_screenshots.py WORKDIR/names.json --dest WORKDIR/screenshots

names.json (ordered as the overlays will appear; numbering follows this order):
[
  {"src": "/abs/path/IMG_1234.PNG", "name": "stripe-revenue-dashboard"},
  {"src": "/abs/path/Screenshot 2026-10-09.png", "name": "tweet-about-launch"}
]
-> WORKDIR/screenshots/01_stripe-revenue-dashboard.png, 02_tweet-about-launch.png, ...
Prints the src -> copy mapping as JSON so overlays.json can point at the copies.
"""
import argparse, json, re, shutil, sys
from pathlib import Path

def slug(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-") or "screenshot"

def main():
    p = argparse.ArgumentParser()
    p.add_argument("names_json")
    p.add_argument("--dest", required=True)
    a = p.parse_args()

    entries = json.load(open(a.names_json))
    dest = Path(a.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for i, e in enumerate(entries, 1):
        src = Path(e["src"]).resolve()
        if not src.is_file():
            sys.exit(f"Not found: {src}")
        dst = dest / f"{i:02d}_{slug(e['name'])}{src.suffix.lower()}"
        if dst == src:
            sys.exit(f"Refusing to overwrite original: {src}")
        shutil.copy2(src, dst)  # copy only; never move/rename/edit the source
        mapping[str(src)] = str(dst)
    print(json.dumps(mapping, indent=1))

if __name__ == "__main__":
    main()
