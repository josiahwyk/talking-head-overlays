#!/usr/bin/env python3
"""Fix warm/yellow room lighting and clean up the voice before building overlays.

Writes a corrected copy (the original is never touched) plus things to compare by eye and ear:
  <name>_fixed.mp4         colour-corrected + cleaned audio, full quality; use it as --video from now on
  <name>_compare.jpg       the same moments before (top) and after (bottom)
  <name>_before_after.mp4  a short clip played twice: BEFORE, then AFTER (540p)

Colour: estimates the colour of the room light from things that should be white or grey
(walls, blinds, clothes), then shifts the whole video so they come out neutral. One correction
for the whole video, so it works best when the camera's white balance didn't drift while
recording. --strength 0.8 (default) keeps a little warmth so skin doesn't look cold; 1.0 is
fully neutral, 0 is off.

Audio: rumble cut (80 Hz high-pass), gentle hiss reduction, light compression to even out
loud and quiet words, then loudness set to -14 LUFS (what YouTube/Instagram play at), peaks
kept below -1.5 dB. It can't repair clipped (distorted) audio or remove room echo.

Usage: python fix_video.py VIDEO --out-dir WORKDIR [--strength 0.8] [--no-colour] [--no-audio]
"""
import argparse, json, subprocess, sys
from pathlib import Path

import numpy as np

def frames(video, duration, n=16, h=180):
    """n small RGB frames spread through the video (skipping the very start and end)."""
    out = []
    for k in range(n):
        t = duration * (0.05 + 0.9 * k / max(1, n - 1))
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                              "-vf", f"scale=-2:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        if raw:
            out.append(np.frombuffer(raw, np.uint8).reshape(h, -1, 3))
    return out

def light_colour(imgs):
    """Colour of the room light, as RGB (0-1). Starts from the average of all mid-to-bright pixels,
    then keeps only pixels that come out close to grey after correcting for it, and repeats.
    Skin, wood and coloured objects drop out; walls, blinds and white/grey clothes remain."""
    px = np.concatenate([im.reshape(-1, 3) for im in imgs]).astype(np.float64) / 255
    luma = px @ [0.2126, 0.7152, 0.0722]
    keep = (luma > np.percentile(luma, 30)) & (px.max(1) < 0.97)  # ignore shadows and blown-out spots
    px = px[keep]
    ill = px.mean(0)
    for _ in range(4):
        c = px / ill
        spread = (c.max(1) - c.min(1)) / c.mean(1)
        near_grey = px[spread < 0.12]
        if len(near_grey) < 500:  # not enough neutral surfaces to go on
            break
        ill = near_grey.mean(0)
    return ill / ill.mean(), len(near_grey) / len(px)

def gains_for(ill, strength):
    g = 1 / ill
    g = 1 + strength * (g - 1)
    g /= g @ [0.2126, 0.7152, 0.0722]  # keep overall brightness the same
    return np.clip(g, 0.6, 1.6)

def audio_filter(video):
    """Clean-up chain + two-pass loudness: measure first, then apply the exact correction."""
    clean = "highpass=f=80,afftdn=nr=10:nf=-40,acompressor=threshold=-20dB:ratio=2.5:attack=10:release=150:makeup=2"
    target = "I=-14:TP=-1.5:LRA=11"
    log = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-vn",
                          "-af", f"{clean},loudnorm={target}:print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = json.loads(log[log.rindex("{"):log.rindex("}") + 1])
    return (f"{clean},loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,"
            "aresample=48000"), float(m["input_i"])

def label(text, w, h, path):
    """A BEFORE/AFTER tag as a transparent PNG (this ffmpeg build has no drawtext)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", h // 14, index=8)
    except OSError:
        font = ImageFont.load_default()
    d.text((w // 20, h // 30), text, font=font, fill="white", stroke_width=max(2, h // 160), stroke_fill="black")
    im.save(path)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--strength", type=float, default=0.8, help="0 = no colour change, 1 = fully neutral")
    p.add_argument("--no-colour", action="store_true")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--sample", type=float, default=None, help="where the before/after clip starts (s)")
    a = p.parse_args()

    video, out = Path(a.video), Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                      str(video)], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    duration = float(info["format"]["duration"])
    if v.get("color_transfer") in ("arib-std-b67", "smpte2084"):
        sys.exit("This video is HDR. Convert it with avconvert first (see SKILL.md), then fix the _sdr copy.")

    vf = "null"
    if not a.no_colour and a.strength > 0:
        ill, neutral = light_colour(frames(video, duration))
        g = gains_for(ill, a.strength)
        warmth = ill[0] / ill[2]
        print(f"Light colour: R {ill[0]:.3f}  G {ill[1]:.3f}  B {ill[2]:.3f}  (red/blue {warmth:.2f}; "
              f"1.00 = neutral, higher = warmer/yellower); {neutral:.0%} of pixels used as white/grey reference")
        print(f"Correction (strength {a.strength}): red x{g[0]:.3f}  green x{g[1]:.3f}  blue x{g[2]:.3f}")
        if abs(warmth - 1) < 0.04:
            print("NOTE: lighting already looks neutral; the colour change will be very small.")
        if neutral < 0.05:
            print("WARNING: few white/grey surfaces found, so the colour estimate is rough. Check the comparison.")
        vf = f"colorchannelmixer=rr={g[0]:.4f}:gg={g[1]:.4f}:bb={g[2]:.4f}"
    af = None
    if has_audio and not a.no_audio:
        af, loud = audio_filter(video)
        print(f"Audio loudness: {loud:.1f} LUFS before -> -14 LUFS after")

    fixed = out / f"{video.stem}_fixed.mp4"
    args = ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"{vf},format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    args += (["-af", af, "-c:a", "aac", "-b:a", "256k"] if af else ["-c:a", "copy"])
    subprocess.run(args + ["-movflags", "+faststart", str(fixed)], check=True)
    print(f"Fixed:    {fixed}")

    # stills: 3 moments, before on top, after below
    cmp = out / f"{video.stem}_compare.jpg"
    times = [duration * f for f in (0.2, 0.5, 0.8)]
    sel = "+".join(f"between(t,{t:.2f},{t + 0.04:.2f})" for t in times)
    row = f"select='{sel}',scale=-2:480,tile=3x1"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(fixed), "-filter_complex",
                    f"[0:v]{row}[b];[1:v]{row}[a];[b][a]vstack", "-frames:v", "1", "-fps_mode", "passthrough",
                    "-q:v", "3", str(cmp)], check=True)
    print(f"Compare:  {cmp}  (top = before, bottom = after)")

    # the same 8 seconds twice: before, then after, so the difference is easy to hear and see
    s = a.sample if a.sample is not None else min(duration * 0.3, max(0, duration - 8))
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(fixed)], capture_output=True, text=True).stdout.split(",")
    w = round(int(probe[0]) * 540 / int(probe[1]) / 2) * 2
    for t in ("BEFORE", "AFTER"):
        label(t, w, 540, out / f"_label_{t}.png")
    ba = out / f"{video.stem}_before_after.mp4"
    chain = (f"[0:v]scale={w}:540,format=yuv420p[v0];[v0][2:v]overlay[b];"
             f"[1:v]scale={w}:540,format=yuv420p[v1];[v1][3:v]overlay[f];")
    if has_audio:
        chain += "[0:a]aresample=48000[a0];[1:a]aresample=48000[a1];[b][a0][f][a1]concat=n=2:v=1:a=1[v][a]"
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        chain += "[b][f]concat=n=2:v=1:a=0[v]"
        maps = ["-map", "[v]"]
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.2f}", "-t", "8", "-i", str(video),
                    "-ss", f"{s:.2f}", "-t", "8", "-i", str(fixed),
                    "-i", str(out / "_label_BEFORE.png"), "-i", str(out / "_label_AFTER.png"),
                    "-filter_complex", chain, *maps, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", str(ba)], check=True)
    for t in ("BEFORE", "AFTER"):
        (out / f"_label_{t}.png").unlink()  # our own temporary files
    print(f"Before/after clip: {ba}  ({s:.0f}s-{s + 8:.0f}s, played twice)")

if __name__ == "__main__":
    main()
