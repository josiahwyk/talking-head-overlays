#!/usr/bin/env python3
"""Split layout and captions for build_timeline.py.

Split layout: while a visual is on screen, the frame becomes a background colour with the
visual in one area and the speaker in the other (vertical video: visual on top, speaker below;
horizontal: visual left, speaker right). Between visuals the speaker is full frame. The speaker
is cropped, never scaled, so there's no zoom.

Captions: the spoken words, a few at a time, drawn with Pillow (any font file, size, colour,
outline, optional box and highlighted current word) and fed to ffmpeg as one image track.

Style comes from a JSON file (see style.example.json), merged with overlays.json "style".
"""
import json, re
from pathlib import Path

from find_phrase import norm

DEFAULT_STYLE = {
    "layout": "center",            # "center" (overlay over the speaker, original behaviour) or "split";
                                   # or per orientation: {"vertical": "split", "horizontal": "center"}
    "split_mode": "continuous",    # "continuous": split from the first visual to the last (intro/outro full frame)
                                   # (or per orientation: {"vertical": "always", "horizontal": "continuous"})
                                   # "always": split the whole video; "visual": split only while a visual is up
                                   # (overlays.json "split_spans": [[start, end], ...] sets exact chapters instead)
    "background": "#E4D5BE",
    "split": {"vertical": 0.44, "horizontal": 0.5},  # share of the frame given to the visual
    "fill_gaps": {"vertical": "hold", "horizontal": "empty"},  # in the split: "hold" keeps each visual up
                                   # until the next one replaces it; "empty" leaves the area blank between visuals
    "margin": 48,                  # space around the visual inside its area (px at 1080)
    "corner_radius": 24,           # rounded corners on screenshots in split layout (0 = square)
    "frame": {"enabled": False, "color": "#C8553D", "width": 8, "padding": 24},
    "face": {"x": 0.5, "y": 0.42}, # where the face is in the source frame (0-1); set per video
    "title": {"color": "#1F1F1F", "size": 84},  # intro title card (overlays.json "intro_title"), caption font
    "captions": {
        "enabled": False,
        "font": "/System/Library/Fonts/Avenir Next.ttc", "font_index": 8,  # 8 = Avenir Next Heavy
        "size": 64,                # px at 1080 wide (scaled for other sizes)
        "color": "#FFFFFF", "outline": "#000000", "outline_width": 5,
        "box": None,               # e.g. "#000000AA" for a box behind the text instead of an outline
        "highlight": None,         # e.g. "#9BE564" to colour the word being spoken
        "max_words": 4, "max_chars": 26, "uppercase": False,
        "y_full": 0.72,            # caption centre (share of height) when the speaker is full frame (vertical)
        "y_full_horizontal": 0.9,  # same, horizontal video (low enough to clear the chin)
        "y_split_vertical": None,  # None = just inside the top of the speaker area
        "y_split_horizontal": 0.9,
    },
}

def merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out

def load_style(path, overrides=None):
    style = DEFAULT_STYLE
    if path and Path(path).expanduser().exists():
        style = merge(style, json.load(open(Path(path).expanduser())))
    return merge(style, overrides)

def rgba(hexc):
    h = hexc.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in range(0, len(h), 2)) + ((255,) if len(h) == 6 else ())

# ---------------------------------------------------------------- split geometry

def layout_for(style, W, H):
    lay = style["layout"]
    return lay.get("vertical" if H >= W else "horizontal", "center") if isinstance(lay, dict) else lay

def split_spans(items, style, total, explicit=None, W=0, H=1):
    """When the split layout is on screen. Fewer layout switches looks more natural: by default it
    stays on from the first visual to the last, so only the intro and outro are full frame."""
    if explicit:
        return [(float(a), float(b)) for a, b in explicit]
    if not items:
        return []
    mode = style.get("split_mode", "continuous")
    if isinstance(mode, dict):
        mode = mode.get("vertical" if H >= W else "horizontal", "continuous")
    if mode == "always":
        return [(0.0, total)]
    if mode == "visual":
        return [(it["start"], it["end"]) for it in items]
    return [(min(it["start"] for it in items), max(it["end"] for it in items))]

def hold_visuals(items, spans, W, H, style):
    """With fill_gaps "hold", each visual stays up until the next one (inside the split spans), so the
    visual area is never blank. Only the MP4 changes: appearance times and the Resolve timeline don't."""
    if style.get("fill_gaps", {}).get("vertical" if H >= W else "horizontal") != "hold":
        return
    for it, nxt in zip(items, items[1:] + [None]):
        span = next(((a, b) for a, b in spans if a <= it["start"] < b), None)
        if span:
            it["show_end"] = min(nxt["start"], span[1]) if nxt and nxt["start"] < span[1] else span[1]

def geometry(W, H, style):
    """Visual area, speaker area (both x, y, w, h on the output frame) and the source crop."""
    fx, fy = style["face"]["x"], style["face"]["y"]
    even = lambda v: int(v) // 2 * 2
    if H >= W:  # vertical: visual on top
        top = even(H * style["split"]["vertical"])
        vis, spk = (0, 0, W, top), (0, top, W, H - top)
        ch = H - top  # face sits ~40% down the speaker area, so captions/chin have room
        cy = min(max(0, int(fy * H - 0.4 * ch)), H - ch)
        crop = (W, ch, 0, even(cy))
    else:       # horizontal: visual on the left
        left = even(W * style["split"]["horizontal"])
        vis, spk = (0, 0, left, H), (left, 0, W - left, H)
        cw = W - left
        cx = min(max(0, int(fx * W - cw / 2)), W - cw)
        crop = (cw, H, even(cx), 0)
    return {"visual": vis, "speaker": spk, "crop": crop}

def prep_split_image(src, dst, area, style, unit):
    """Screenshot fitted inside its area (rounded corners, optional frame) on a transparent
    canvas the size of the area."""
    from PIL import Image, ImageDraw, ImageOps
    _, _, aw, ah = area
    m = int(style["margin"] * unit)
    fr = style["frame"]
    pad = int(fr["padding"] * unit) if fr["enabled"] else 0
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGBA")
    img = ImageOps.contain(img, (max(1, aw - 2 * (m + pad)), max(1, ah - 2 * (m + pad))), Image.LANCZOS)
    r = int(style["corner_radius"] * unit)
    if r:
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), r, fill=255)
        img.putalpha(Image.composite(img.getchannel("A"), mask, mask))
    canvas = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    x, y = (aw - img.width) // 2, (ah - img.height) // 2
    if fr["enabled"]:
        d = ImageDraw.Draw(canvas)
        d.rounded_rectangle((x - pad, y - pad, x + img.width + pad, y + img.height + pad), r + pad,
                            fill=(255, 255, 255, 255), outline=rgba(fr["color"]), width=int(fr["width"] * unit))
    canvas.alpha_composite(img, (x, y))
    canvas.save(dst)

def title_card(text, dst, area, style, unit):
    """Intro title card for the visual area: big text in the caption font, wrapped, centred.
    Fills the split's visual area before the first visual is mentioned."""
    from PIL import Image, ImageDraw, ImageFont
    _, _, aw, ah = area
    cs, ts = style["captions"], style["title"]
    font = ImageFont.truetype(cs["font"], int(ts["size"] * unit), index=cs.get("font_index", 0))
    img = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    maxw, lines = aw - 2 * int(style["margin"] * unit) * 2, [[]]
    for w in text.split():
        if lines[-1] and d.textlength(" ".join(lines[-1] + [w]), font=font) > maxw:
            lines.append([])
        lines[-1].append(w)
    lh = int(ts["size"] * unit * 1.2)
    for n, ln in enumerate(lines):
        d.text((aw / 2, ah / 2 + (n - (len(lines) - 1) / 2) * lh), " ".join(ln), font=font,
               fill=rgba(ts["color"]), anchor="mm")
    img.save(dst)

# ---------------------------------------------------------------- captions

def apply_fixes(words, fixes):
    """{"key studio": "Keyestudio"}: replace misheard word runs, keeping their timing."""
    words = [dict(w) for w in words]
    for wrong, right in (fixes or {}).items():
        toks = [norm(t) for t in wrong.split() if norm(t)]
        new = right.split()
        i = 0
        while i + len(toks) <= len(words):
            if [norm(w["word"]) for w in words[i:i + len(toks)]] == toks:
                span = words[i:i + len(toks)]
                t0, t1 = span[0]["start"], span[-1]["end"]
                step = (t1 - t0) / len(new)
                tail = re.sub(r"^.*?([.,?!]*)$", r"\1", span[-1]["word"])
                words[i:i + len(toks)] = [{"word": w + (tail if k == len(new) - 1 else ""), "start": t0 + k * step,
                                           "end": t0 + (k + 1) * step} for k, w in enumerate(new)]
                i += len(new)
            else:
                i += 1
    return words

def caption_groups(words, cs):
    groups, cur = [], []
    for w in words:
        txt = w["word"].strip()
        if not txt:
            continue
        if cur and (len(cur) >= cs["max_words"]
                    or len(" ".join(x["word"] for x in cur + [w])) > cs["max_chars"]
                    or w["start"] - cur[-1]["end"] > 0.6
                    or cur[-1]["word"].endswith((".", "?", "!"))):
            groups.append(cur); cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    out = []
    for k, g in enumerate(groups):
        nxt = groups[k + 1][0]["start"] if k + 1 < len(groups) else None
        end = g[-1]["end"] + 0.4
        if nxt is not None and nxt - g[-1]["end"] <= 1.0:
            end = nxt  # flow straight into the next caption
        out.append({"start": max(0.0, g[0]["start"] - 0.05), "end": end, "words": g})
    return out

def clean(word, upper):
    w = re.sub(r"[.,?!;:]+$", "", word.strip())
    return w.upper() if upper else w

def draw_caption(W, H, words, hot, cx, cy, maxw, cs, unit):
    """Full-frame transparent PNG with one caption; wraps to two lines if it doesn't fit."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(cs["font"], int(cs["size"] * unit), index=cs.get("font_index", 0))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    toks = [clean(w["word"], cs["uppercase"]) for w in words]
    sp = d.textlength(" ", font=font)
    width = lambda ts: sum(d.textlength(t, font=font) for t in ts) + sp * (len(ts) - 1)
    lines = [list(range(len(toks)))]
    if width(toks) > maxw and len(toks) > 1:
        best = min(range(1, len(toks)), key=lambda k: max(width(toks[:k]), width(toks[k:])))
        lines = [list(range(best)), list(range(best, len(toks)))]
    lh = int(cs["size"] * unit * 1.2)
    sw = int(cs["outline_width"] * unit)
    for n, line in enumerate(lines):
        y = cy + (n - (len(lines) - 1) / 2) * lh
        x = cx - width([toks[i] for i in line]) / 2
        if cs.get("box"):
            pb = int(18 * unit)
            d.rounded_rectangle((x - pb * 1.5, y - lh / 2, x + width([toks[i] for i in line]) + pb * 1.5, y + lh / 2),
                                pb, fill=rgba(cs["box"]))
        for i in line:
            col = cs["highlight"] if (cs.get("highlight") and i == hot) else cs["color"]
            d.text((x, y), toks[i], font=font, fill=rgba(col), anchor="lm",
                   stroke_width=0 if cs.get("box") else sw, stroke_fill=rgba(cs["outline"]))
            x += d.textlength(toks[i], font=font) + sp
    return img

def build_captions(words, W, H, style, split_spans, geo, out_dir, total):
    """Render caption images and an ffconcat list that plays them as one track."""
    from PIL import Image
    cs = style["captions"]
    unit = min(W, H) / 1080
    capdir = Path(out_dir) / "captions"
    capdir.mkdir(exist_ok=True)
    for old in capdir.glob("*.png"):
        old.unlink()
    in_split = lambda t: any(a <= t < b for a, b in split_spans)
    def place(split):
        if not split:
            return W / 2, (cs["y_full"] if H >= W else cs["y_full_horizontal"]) * H, W * 0.88
        sx, sy, sw_, sh = geo["speaker"]
        if H >= W:
            y = cs["y_split_vertical"] * H if cs["y_split_vertical"] else sy + int(cs["size"] * unit * 1.4)
            return W / 2, y, W * 0.88
        return sx + sw_ / 2, cs["y_split_horizontal"] * H, sw_ * 0.88
    # each caption (or each highlighted word) is cut at split/full boundaries so it sits in the right place
    cuts = sorted({t for s in split_spans for t in s})
    entries = []
    for g in caption_groups(words, cs):
        steps = [(g["start"], g["end"], None)]
        if cs.get("highlight"):
            ws = g["words"]
            steps = [(g["start"] if k == 0 else ws[k]["start"],
                      ws[k + 1]["start"] if k + 1 < len(ws) else g["end"], k) for k in range(len(ws))]
        for a, b, hot in steps:
            pts = [a] + [c for c in cuts if a < c < b] + [b]
            for s, e in zip(pts, pts[1:]):
                if e - s > 0.02:
                    entries.append((s, e, g["words"], hot, in_split((s + e) / 2)))
    blank = capdir / "blank.png"
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)
    lines, t, cache = ["ffconcat version 1.0"], 0.0, {}
    for n, (s, e, ws, hot, split) in enumerate(entries):
        if s > t + 0.001:
            lines += [f"file '{blank}'", f"duration {s - t:.3f}"]
        key = (tuple(w["word"] for w in ws), hot, split)
        if key not in cache:
            cx, cy, maxw = place(split)
            p = capdir / f"cap{n:04d}.png"
            draw_caption(W, H, ws, hot, cx, cy, maxw, cs, unit).save(p)
            cache[key] = p
        lines += [f"file '{cache[key]}'", f"duration {e - max(s, t):.3f}"]
        t = e
    lines += [f"file '{blank}'", f"duration {max(0.1, total - t):.3f}", f"file '{blank}'"]
    lst = capdir / "captions.ffconcat"
    lst.write_text("\n".join(lines) + "\n")
    return lst, entries

def write_srt(words, style, path):
    """Plain captions file (for YouTube upload or Resolve)."""
    fmt = lambda t: f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},{int(t * 1000 % 1000):03d}"
    rows = []
    for n, g in enumerate(caption_groups(words, style["captions"]), 1):
        rows.append(f"{n}\n{fmt(g['start'])} --> {fmt(g['end'])}\n"
                    + " ".join(clean(w["word"], style["captions"]["uppercase"]) for w in g["words"]) + "\n")
    Path(path).write_text("\n".join(rows))
