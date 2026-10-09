#!/usr/bin/env python3
"""Stack several screenshots into one vertical list that builds up step by step.
Use when one spoken mention has several screenshots (e.g. a few order receipts).

Usage: python stack_images.py OUT_PREFIX img1 img2 img3 ... [--gap 24] [--final-only]
-> OUT_PREFIX_step1of3.png (img1), OUT_PREFIX_step2of3.png (img1+img2), ... on one fixed,
   transparent canvas sized for the full stack, so items never move or rescale between steps.
Put each step in overlays.json as its own overlay ("gap": 0 so they don't blink).
"""
import argparse
from PIL import Image, ImageOps

def main():
    p = argparse.ArgumentParser()
    p.add_argument("out_prefix")
    p.add_argument("images", nargs="+")
    p.add_argument("--gap", type=int, default=24, help="px between items")
    p.add_argument("--final-only", action="store_true", help="only write the full stack")
    a = p.parse_args()

    ims = [ImageOps.exif_transpose(Image.open(f)).convert("RGBA") for f in a.images]
    w = max(i.width for i in ims)
    ims = [i.resize((w, round(i.height * w / i.width)), Image.LANCZOS) if i.width != w else i for i in ims]
    h = sum(i.height for i in ims) + a.gap * (len(ims) - 1)
    n = len(ims)
    for step in ([n] if a.final_only else range(1, n + 1)):
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        y = 0
        for im in ims[:step]:
            canvas.paste(im, (0, y), im)
            y += im.height + a.gap
        out = f"{a.out_prefix}_step{step}of{n}.png"
        canvas.save(out)
        print(out)

if __name__ == "__main__":
    main()
