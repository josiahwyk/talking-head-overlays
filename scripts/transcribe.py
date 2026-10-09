#!/usr/bin/env python3
"""Transcribe a video with word-level timestamps using faster-whisper.

Outputs (next to --out-dir):
  words.json      [{"word": str, "start": float, "end": float}, ...]
  transcript.txt  one line per segment: "[mm:ss.s] text"  (for Claude to read)

Usage: python transcribe.py VIDEO --out-dir WORKDIR [--model small.en]
"""
import argparse, json, os, subprocess, sys

def fmt(t):
    return f"{int(t // 60):02d}:{t % 60:04.1f}"

def load_audio(path, sr=16000):
    """Decode to 16 kHz mono float32 via ffmpeg (avoids faster-whisper/PyAV version clashes)."""
    import numpy as np
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.float32)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--model", default="small.en",
                   help="faster-whisper model: tiny.en, base.en, small.en, medium.en, large-v3")
    p.add_argument("--language", default="en")
    a = p.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("Missing dependency: pip install faster-whisper")

    os.makedirs(a.out_dir, exist_ok=True)
    model = WhisperModel(a.model, device="auto", compute_type="auto")
    segments, _ = model.transcribe(load_audio(a.video), language=a.language,
                                   word_timestamps=True, vad_filter=True)

    words, lines = [], []
    for seg in segments:
        lines.append(f"[{fmt(seg.start)}] {seg.text.strip()}")
        for w in seg.words or []:
            words.append({"word": w.word.strip(), "start": round(w.start, 3),
                          "end": round(w.end, 3)})

    with open(os.path.join(a.out_dir, "words.json"), "w") as f:
        json.dump(words, f, indent=1)
    with open(os.path.join(a.out_dir, "transcript.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{len(words)} words, {len(lines)} segments -> {a.out_dir}")

if __name__ == "__main__":
    main()
