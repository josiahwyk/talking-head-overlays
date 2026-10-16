# Experiment: clean cut (shelved)

This branch adds a "clean cut" step that runs before overlays: it removes fillers, long pauses, stutters, take cues and earlier takes, then times overlays on the edited video. It was tested once (14 Oct 2026, a 3-minute video with four takes of the same intro) and **shelved**. It is not part of the skill on `main`.

## What's here
- `scripts/clean_cut.py`
  - `detect` proposes cuts and writes `cuts.json` and `cuts_table.md`
  - `apply` writes `edl.json`, `words_cut.json` (word times on the edited timeline) and `transcript_cut.txt`
- `scripts/build_timeline.py --edl edl.json [--zoom 1.1]` renders the edited video from the kept pieces. `--zoom` punches in on alternate pieces. The FCPXML is written as separate pieces of the original file on V1.
- Without `--edl`, `build_timeline.py` gives byte-identical output to `main` (checked on Test 3).

## Why it was shelved
- **It couldn't judge delivery.** It kept the last take, which was verbally clean but deliberately low-energy. Its "earlier take may be better" flag counted stumbles, not energy.
- **The joins didn't feel natural.** Mid-sentence cuts on a single camera are hard, and whisper's word timings are only accurate to about 0.1–0.2s.
- **Fillers were hard to detect.** No whisper setup transcribed them (`small.en`, `medium.en`, with or without a "verbatim" prompt). Looking for voiced sounds between words finds candidates, but naming them is only moderately reliable, and the prompt sometimes invents words.
- **The cuts table was hard to review.** Source timestamps alone didn't show what would be heard.

## The workflow chosen instead
Record until one take is coherent, trim ums and pauses manually in Recut, export an MP4, then use the overlay skill on that file.

## If revived
- Build a review the user can actually check: a transcript with removed words crossed out, plus a review reel playing each cut in context.
- Only consider a take ranking once delivery measures (loudness range, pitch variation, pace) have been tested against human-labelled takes.
