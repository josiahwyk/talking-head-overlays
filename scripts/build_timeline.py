#!/usr/bin/env python3
"""Build a DaVinci Resolve-importable FCPXML timeline: talking-head video on V1,
screenshot and video-clip overlays on V2 at the moments their phrases are spoken.

Usage:
  python build_timeline.py --video talk.mp4 --overlays WORKDIR/overlays.json \
      --words WORKDIR/words.json --out-dir WORKDIR [--preview]

overlays.json:
{
  "defaults": {"duration": 4.0, "scale": 0.8, "lead": 0.15, "gap": 0.1},
  "overlays": [
    {"image": "/abs/or/relative/shot1.png", "phrase": "conversion rate dropped"},
    {"image": "shot2.png", "start": 95.2, "duration": 6},
    {"clip": "demo.mov", "phrase": "patting it", "in": 6.0, "duration": 4}
  ]
}
- "phrase" is looked up in words.json (best fuzzy match) unless "start" is given.
- "trigger": a word inside the phrase to appear on (the word naming what's on screen);
  without it the overlay appears on the phrase's first word.
- "until": words that end the thought about this visual; it leaves "tail" (0.3s) after them,
  but stays at least "min_duration" (2s). Overrides "duration".
- "occurrence": N picks the Nth match (1-based) if the phrase is said more than once.
- End: start + "duration" by default. Opt-in "end": "sentence" in defaults ends each overlay when the sentence containing the phrase finishes (". ? !" or a pause >=
  "pause" s, + "tail"), kept within [min_duration, max_duration]; a per-item "duration" overrides it. Always clipped so overlays
  never overlap.
- "clip" (or any video file under "image") is a video overlay: "in" = seconds into the
  clip to start from (default 0). Duration is capped at what's left of the clip.
  Clip audio is always dropped so the voice track stays clean.
Relative paths resolve against the overlays.json folder.
"""
import argparse, json, os, re, subprocess, sys
from fractions import Fraction
from xml.sax.saxutils import quoteattr
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from find_phrase import search, norm

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mkv", ".webm", ".avi"}

def probe(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", str(video)], capture_output=True, text=True, check=True)
    info = json.loads(out.stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    fps = Fraction(v.get("avg_frame_rate") or v["r_frame_rate"])
    nominal = Fraction(v["r_frame_rate"])
    if fps == 0 or abs(float(fps) - float(nominal)) < 0.01:  # iPhone VFR: 29.998 -> 30
        fps = nominal
    # snap common NTSC rates
    for std in (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001)):
        if abs(float(fps) - float(std)) < 0.01:
            fps = std
    w, h = int(v["width"]), int(v["height"])
    rot = int((v.get("tags", {}) or {}).get("rotate", 0) or 0)
    for sd in v.get("side_data_list", []) or []:
        rot = int(sd.get("rotation", rot) or rot)
    if abs(rot) in (90, 270):
        w, h = h, w
    return {"fps": fps, "width": w, "height": h,
            "duration": float(info["format"]["duration"]),
            "audio_ch": int(a["channels"]) if a else 0,
            "hdr": v.get("color_transfer") in ("arib-std-b67", "smpte2084")}

def t2r(seconds, fps):
    """seconds -> frame-aligned FCPXML rational time string."""
    frames = round(Fraction(seconds).limit_denominator(100000) * fps)
    val = Fraction(frames) / fps
    return "0s" if val == 0 else f"{val.numerator}/{val.denominator}s"

def prep_image(src, dst, w, h, scale):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGBA")
    max_w, max_h = int(w * scale), int(h * scale)
    r = min(max_w / img.width, max_h / img.height)
    img = img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2), img)
    canvas.save(dst)

def prep_clip(src, dst):
    """Silent copy of the clip: stream copy (no re-encode), audio dropped, full length kept
    so Resolve still has handles for sliding the trim."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0",
                    "-c", "copy", "-an", str(dst)], check=True)

def sentence_end(words, t0, d):
    """When the thought that starts at t0 is finished: the first word ending a sentence
    (. ? !) or followed by a pause >= d["pause"], plus a short tail. Clamped to
    [min_duration, max_duration] after t0. Whisper punctuation is patchy, so pauses matter."""
    i = next((k for k, w in enumerate(words) if w["start"] >= t0 - 0.05), None)
    if i is None:
        return None
    end = None
    for k in range(i, len(words)):
        nxt = words[k + 1]["start"] if k + 1 < len(words) else None
        if words[k]["word"].rstrip().endswith((".", "?", "!")) or nxt is None or nxt - words[k]["end"] >= d["pause"]:
            end = words[k]["end"] + d["tail"]
            break
        if words[k]["end"] - t0 > d["max_duration"]:
            break
    end = end if end is not None else t0 + d["max_duration"]
    return min(max(end, t0 + d["min_duration"]), t0 + d["max_duration"])

def resolve_timings(cfg, words, base_dir, total):
    d = {"duration": 4.0, "scale": 0.8, "lead": 0.15, "gap": 0.1,
         "end": "fixed", "pause": 0.5, "tail": 0.3, "min_duration": 2.0, "max_duration": 6.0,
         **cfg.get("defaults", {})}
    items = []
    for o in cfg["overlays"]:
        src = Path(o.get("clip") or o["image"])
        if not src.is_absolute():
            src = (base_dir / src).resolve()
        if not src.exists():
            sys.exit(f"File not found: {src}")
        is_clip = src.suffix.lower() in VIDEO_EXT
        if "start" in o:
            start, conf = float(o["start"]), None
        else:
            if words is None:
                sys.exit("words.json needed for phrase lookup")
            occ = int(o.get("occurrence", 1))
            hits = sorted(search(words, o["phrase"], top=max(3, occ)), key=lambda r: r["start"])
            # only near-best matches count as repeats; a 0.6 lookalike must not beat a later exact hit
            best = max(h["score"] for h in hits)
            good = [h for h in hits if h["score"] >= max(0.6, best - 0.15)] or hits
            hit = good[min(occ, len(good)) - 1]
            t_on = hit["start"]
            if "trigger" in o:  # appear on the key word, not the phrase's lead-in words
                key = norm(o["trigger"].split()[0])
                tw = next((w for w in words if hit["start"] - 0.05 <= w["start"] <= hit["end"] + 0.05
                           and norm(w["word"]) == key), None)
                if tw is None:
                    print(f"WARNING: trigger '{o['trigger']}' not found in '{hit['context']}', using phrase start")
                else:
                    t_on = tw["start"]
            start, conf = max(0.0, t_on - d["lead"]), hit["score"]
            if conf < 0.75:
                print(f"WARNING: weak match ({conf:.2f}) for '{o['phrase']}' -> '{hit['context']}'")
        if "until" in o and words:
            # leave once the thought about this visual is finished: end of the "until" words
            # (first time they're said after the overlay appears) + tail, at least min_duration
            toks = [norm(t) for t in o["until"].split() if norm(t)]
            ws = [w for w in words if w["start"] >= start]
            hit_end = next((ws[k + len(toks) - 1]["end"] for k in range(len(ws) - len(toks) + 1)
                            if [norm(w["word"]) for w in ws[k:k + len(toks)]] == toks), None)
            if hit_end is None:
                print(f"WARNING: until '{o['until']}' not found after {start:.2f}s; using duration")
                dur = float(o.get("duration", d["duration"]))
            else:
                dur = max(hit_end + d["tail"] - start, d["min_duration"])
        elif "duration" in o:
            dur = float(o["duration"])
        elif d["end"] == "sentence" and "phrase" in o and words:
            dur = sentence_end(words, hit["start"], d) - start
        else:
            dur = float(d["duration"])
        it = {"src": src, "clip": is_clip, "start": start, "phrase": o.get("phrase", ""),
              "duration": dur, "end": o.get("end"), "score": conf,
              "scale": float(o.get("scale", d["scale"])),
              "gap": float(o.get("gap", d["gap"]))}  # per-item: 0 for seamless build-up steps
        if is_clip:
            it["meta"] = probe(src)
            it["in"] = float(o.get("in", 0.0))
            left = it["meta"]["duration"] - it["in"]
            if left <= 0:
                sys.exit(f"'in' {it['in']}s is past the end of {src.name} ({it['meta']['duration']:.1f}s)")
            if dur > left:
                print(f"NOTE: {src.name} has only {left:.2f}s after in={it['in']}; duration capped")
                it["duration"] = left
        items.append(it)
    items.sort(key=lambda x: x["start"])
    for i, it in enumerate(items):
        end = float(it["end"]) if it["end"] is not None else it["start"] + it["duration"]
        if it["clip"]:
            end = min(end, it["start"] + it["duration"])  # can't outrun the source footage
        if i + 1 < len(items):
            if it["gap"] == 0:  # build-up step: hold until the next step replaces it
                end = items[i + 1]["start"]
            end = min(end, items[i + 1]["start"] - it["gap"])
        it["end"] = min(end, total)
        if it["end"] - it["start"] < 0.5:
            print(f"WARNING: {it['src'].name} only {it['end'] - it['start']:.2f}s on screen")
    return items

def write_fcpxml(video, meta, items, path, title):
    fps, W, H = meta["fps"], meta["width"], meta["height"]
    fd = 1 / fps
    total = t2r(meta["duration"], fps)
    res = [f'<format id="r1" name="Video" frameDuration="{fd.numerator}/{fd.denominator}s" width="{W}" height="{H}"/>',
           f'<format id="r2" name="FFVideoFormatRateUndefined" width="{W}" height="{H}"/>',
           f'<asset id="r3" name={quoteattr(Path(video).stem)} src={quoteattr(Path(video).resolve().as_uri())} '
           f'start="0s" duration="{total}" hasVideo="1" format="r1"'
           + (f' hasAudio="1" audioSources="1" audioChannels="{meta["audio_ch"]}" audioRate="48000"' if meta["audio_ch"] else "")
           + "/>"]
    clips = []
    for i, it in enumerate(items):
        aid, name = f"r{10 + 2 * i}", quoteattr(it["prepped"].name)
        off, dur = t2r(it["start"], fps), t2r(it["end"] - it["start"], fps)
        if it["clip"]:
            cm, fid = it["meta"], f"r{11 + 2 * i}"
            cfd = 1 / cm["fps"]
            res.append(f'<format id="{fid}" frameDuration="{cfd.numerator}/{cfd.denominator}s" '
                       f'width="{cm["width"]}" height="{cm["height"]}"/>')
            res.append(f'<asset id="{aid}" name={name} src={quoteattr(it["prepped"].resolve().as_uri())} '
                       f'start="0s" duration="{t2r(cm["duration"], cm["fps"])}" hasVideo="1" format="{fid}"/>')
            # Resolve fits the clip to the frame; scale shrinks it like the screenshots
            clips.append(f'<asset-clip ref="{aid}" lane="1" name={name} offset="{off}" '
                         f'start="{t2r(it["in"], cm["fps"])}" duration="{dur}">'
                         f'<adjust-transform scale="{it["scale"]} {it["scale"]}"/></asset-clip>')
        else:
            res.append(f'<asset id="{aid}" name={name} src={quoteattr(it["prepped"].resolve().as_uri())} '
                       f'start="0s" duration="0s" hasVideo="1" format="r2"/>')
            clips.append(f'<video ref="{aid}" lane="1" name={name} offset="{off}" start="0s" duration="{dur}"/>')
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.8">
  <resources>
    {chr(10).join("    " + r for r in res).strip()}
  </resources>
  <library>
    <event name={quoteattr(title)}>
      <project name={quoteattr(title)}>
        <sequence format="r1" duration="{total}" tcStart="0s" tcFormat="NDF">
          <spine>
            <asset-clip ref="r3" name={quoteattr(Path(video).stem)} offset="0s" start="0s" duration="{total}" tcFormat="NDF">
              {chr(10).join("              " + c for c in clips).strip()}
            </asset-clip>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''
    Path(path).write_text(xml)

def render_preview(video, items, out, meta, final=False):
    """Burn the overlays in. Default: quick 540p preview for checking timings.
    final=True: full-resolution, high-quality H.264 + AAC, ready to upload."""
    W, H = meta["width"], meta["height"]
    args = ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    for it in items:
        if it["clip"]:
            args += ["-ss", f"{it['in']:.3f}", "-t", f"{it['end'] - it['start']:.3f}", "-i", str(it["prepped"])]
        else:
            args += ["-i", str(it["prepped"])]
    chain, last = [], "[0:v]"
    for i, it in enumerate(items, 1):
        src = f"[{i}:v]"
        if it["clip"]:
            src = f"[c{i}]"
            chain.append(f"[{i}:v]scale={int(W * it['scale'])}:{int(H * it['scale'])}:force_original_aspect_ratio=decrease,"
                         f"setpts=PTS-STARTPTS+{it['start']:.3f}/TB{src}")
            pos = "(W-w)/2:(H-h)/2:eof_action=pass"
        else:
            pos = "0:0"
        tag = f"[v{i}]"
        chain.append(f"{last}{src}overlay={pos}:enable='between(t,{it['start']:.3f},{it['end']:.3f})'{tag}")
        last = tag
    if final:
        chain.append(f"{last}format=yuv420p[out]")
        enc = ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        if not meta.get("hdr"):  # standard (SDR) video: tag it so players show phone-accurate colour
            enc[enc.index("-c:a"):enc.index("-c:a")] = ["-colorspace", "bt709", "-color_primaries", "bt709",
                                                        "-color_trc", "bt709"]
    else:
        chain.append(f"{last}scale=-2:540[out]")
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac"]
    args += ["-filter_complex", ";".join(chain), "-map", "[out]", "-map", "0:a?"] + enc + [str(out)]
    subprocess.run(args, check=True)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--overlays", required=True)
    p.add_argument("--words")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--title")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--final", action="store_true", help="also render the full-quality video to post")
    a = p.parse_args()

    out = Path(a.out_dir); (out / "overlays_prepped").mkdir(parents=True, exist_ok=True)
    for stale in (out / "overlays_prepped").iterdir():  # our own generated files only
        if stale.is_file():
            stale.unlink()
    meta = probe(a.video)
    cfg = json.load(open(a.overlays))
    words = json.load(open(a.words)) if a.words else None
    items = resolve_timings(cfg, words, Path(a.overlays).resolve().parent, meta["duration"])

    for i, it in enumerate(items, 1):
        stem = re.sub(r"^\d+_", "", it["src"].stem)  # staged copies already carry a number
        ext = it["src"].suffix.lower() if it["clip"] else ".png"
        it["prepped"] = (out / "overlays_prepped" / f"{i:02d}_{stem}{ext}").resolve()
        if it["clip"]:
            prep_clip(it["src"], it["prepped"])
        else:
            prep_image(it["src"], it["prepped"], meta["width"], meta["height"], it["scale"])

    title = a.title or f"{Path(a.video).stem} overlays"
    xml_path = out / f"{Path(a.video).stem}_overlays.fcpxml"
    write_fcpxml(a.video, meta, items, xml_path, title)

    print(f"Video: {meta['width']}x{meta['height']} @ {float(meta['fps']):.3f}fps, {meta['duration']:.1f}s")
    print(f"{'#':>2}  {'start':>8}  {'end':>8}  conf  file [clip window]  <- phrase")
    for i, it in enumerate(items, 1):
        c = f"{it['score']:.2f}" if it["score"] is not None else "  - "
        win = f" [clip {it['in']:.1f}-{it['in'] + it['end'] - it['start']:.1f}s]" if it["clip"] else ""
        print(f"{i:>2}  {it['start']:8.2f}  {it['end']:8.2f}  {c}  {it['src'].name}{win}  <- {it['phrase']}")
    print(f"\nTimeline: {xml_path}")
    if meta["hdr"]:
        print("WARNING: this video was recorded in HDR (iPhone 'HDR Video' on). Previews/exports will look "
              "brighter than on the phone. Turn off Settings > Camera > Record Video > HDR Video for future recordings.")
    if a.final:
        fv = str(out / f"{Path(a.video).stem}_final.mp4")
        render_preview(a.video, items, fv, meta, final=True)
        print(f"Final:    {fv}")
    if a.preview:
        pv = str(out / f"{Path(a.video).stem}_preview.mp4")
        render_preview(a.video, items, pv, meta)
        print(f"Preview:  {pv}")

if __name__ == "__main__":
    main()
