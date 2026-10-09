#!/usr/bin/env python3
"""Contact sheet of a video clip so Claude can pick which seconds to use as an overlay.

Usage: python clip_frames.py CLIP --out WORKDIR/contact/clipname.jpg [--every 0.5]
Grabs a frame every N seconds, tiles them with the timestamp printed on each, and
prints the clip's length and whether it has an audio track (speech in a clip can be
transcribed with transcribe.py to help pick the window).
"""
import argparse, json, subprocess, tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

def main():
    p = argparse.ArgumentParser()
    p.add_argument("clip")
    p.add_argument("--out", required=True)
    p.add_argument("--every", type=float, default=0.5)
    p.add_argument("--cols", type=int, default=6)
    p.add_argument("--thumb", type=int, default=260, help="thumbnail long edge in px")
    a = p.parse_args()

    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                      "-of", "json", a.clip], capture_output=True, text=True, check=True).stdout)
    dur = float(info["format"]["duration"])
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["ffmpeg", "-v", "error", "-i", a.clip, "-vf",
                        f"fps=1/{a.every},scale='if(gt(iw,ih),{a.thumb},-2)':'if(gt(iw,ih),-2,{a.thumb})'",
                        f"{tmp}/f_%04d.jpg"], check=True)
        frames = sorted(Path(tmp).glob("f_*.jpg"))
        if not frames:
            raise SystemExit("No frames extracted")
        tw, th = Image.open(frames[0]).size
        rows = -(-len(frames) // a.cols)
        sheet = Image.new("RGB", (tw * a.cols, th * rows), "black")
        font = ImageFont.load_default(size=max(14, th // 12))
        for i, f in enumerate(frames):
            im = Image.open(f).convert("RGB")
            d = ImageDraw.Draw(im)
            label = f"{i * a.every:.1f}s"
            d.rectangle([0, 0, d.textlength(label, font=font) + 10, font.size + 8], fill="black")
            d.text((5, 3), label, fill="yellow", font=font)
            sheet.paste(im, ((i % a.cols) * tw, (i // a.cols) * th))
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        sheet.save(a.out, quality=85)
    print(f"{Path(a.clip).name}: {dur:.2f}s, audio={'yes' if has_audio else 'no'}, "
          f"{len(frames)} frames every {a.every}s -> {a.out}")

if __name__ == "__main__":
    main()
