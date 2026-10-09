---
name: talking-head-overlays
description: Places screenshot/image and short video-clip overlays on a talking-head video at the exact moment the matching word is spoken, then outputs a quick preview, an upload-ready final MP4, and a DaVinci Resolve timeline (FCPXML). Use whenever the user wants to add screenshots, images, B-roll stills or clips to a YouTube/short-form video, sync images to what they say, "put the screenshots on my video", "time my overlays", "edit my talking head", or mentions tedious screenshot placement in Resolve — even if they don't say "overlay" or "FCPXML".
---

# Talking-Head Overlays

Goal: the user records a talking-head video and has a folder of screenshots (and sometimes short phone clips). Each visual should appear when they say the word that names it and leave when they finish talking about it. Output: a 540p preview to check, a full-quality `_final.mp4` to post, and a Resolve timeline for anyone who wants to fine-tune by hand.

Runs locally (Claude Code with access to the user's machine). Video files are too large for a chat upload.

## 0. Setup check (first run only)

```bash
which ffmpeg ffprobe || echo "brew install ffmpeg"
python3 -c "import faster_whisper, PIL" || pip install -r requirements.txt
```
The first transcription downloads the whisper model (~500 MB for small.en). Pillow ≥ 11.3 reads `.avif`.

## 1. Gather inputs

- **Preferences:** read `~/.claude/skill-preferences/talking-head-overlays.md` if it exists and follow it. Whenever the user gives timing/look feedback while watching a preview, append it there as a dated one-line rule (create the file if missing). Never put personal preferences in this skill folder.
- **Video path:** if the user didn't give one, **ask for the location** before doing anything else. Don't guess or pick a video unless they describe which one (e.g. "my newest video in X folder" — then resolve it and confirm the filename in your reply).
- **Screenshots folder:** ask if not given and not obvious (e.g. a `Screenshots` folder beside the video's folder).
- Use a work folder next to the video: `<video_dir>/_overlays/`.
- **Never modify originals** — the source video and screenshots are read-only. All renamed copies, converted files and outputs go in the work folder.

## 2. Transcribe

```bash
python scripts/transcribe.py VIDEO --out-dir WORKDIR            # small.en default
# use --model medium.en if names/jargon come out wrong
```
Produces `words.json` (word timestamps) and `transcript.txt` (timestamped lines). macOS screenshot filenames contain an invisible narrow no-break space before "am/pm" — list the folder to get real names rather than typing them.

## 3. Match visuals to words (the tedious part — do it carefully)

1. Read `transcript.txt` in full.
2. View every screenshot. Note what it shows (app, numbers, headline, UI). Filenames are strong hints when descriptive.
3. For **video clips**, make a contact sheet and view it:
   `python scripts/clip_frames.py CLIP --out WORKDIR/contact/<name>.jpg` (frame every 0.5s, timestamped).
   Pick the window that shows the thing being talked about (e.g. 6.0–10.0s of a 12s clip) — usually 3–5s, not the whole clip. Skip shaky starts, setup and dead air. If the clip has speech that matters, transcribe it with `transcribe.py`.
4. For each visual choose three things from the transcript:
   - **`phrase`** — 3–6 consecutive words, verbatim, from the *first* time they mention it. Used to find the right spot.
   - **`trigger`** — the one word in that phrase that names what's on screen ("physical" in "came across this physical AI meetup", "kits" in "bought my own kits"). The overlay appears on this word. Without it, it appears on the phrase's first word, which in testing was up to ~2s too early.
   - **`until`** — the last words of the thought about that visual ("…see what it was about", "…plugged in properly"). It leaves 0.3s after them (min 2s). Choose by meaning, not pauses: stop when the speaker moves to a new topic, keep going while they're still describing the thing on screen. (Fixed 4s felt too long; an automatic pause-based end felt worse.)
5. **One visual per mention, first mention wins.** Never place the same file twice by default. If they come back to a topic and nothing new fits, leave it bare and list it under **Visuals worth sourcing** (timestamp + what a fresh screenshot/clip could show). Only reuse a visual if asked, or as a deliberate callback flagged in the table.
6. **Several screenshots for one mention** (e.g. 4 order receipts for "other boards I bought"): don't flicker through them. Stack them into a list that builds up, then holds:
   `python scripts/stack_images.py WORKDIR/screenshots/stacks/<name> img1 img2 img3 img4` → `<name>_step1of4.png` … `_step4of4.png` on one fixed canvas (nothing moves between steps). Add each step as its own overlay with `"gap": 0`, ideally each on the word that names it ("buttons… more boards… more wires… LED screens"); otherwise space them ~1s apart with `"start"`. Give the last step an `until`. Raise `"scale"` (e.g. 0.9) for wide, short receipts so the text stays readable.
7. **Text-heavy screenshots** (chats, prompts, articles) need time to be read — make sure `until` gives them ~6s+, or set `"duration"`.
8. Verify each phrase with `python scripts/find_phrase.py WORKDIR/words.json "phrase"`. If it recurs, set `"occurrence": N`.
9. Show the user a compact table before building:

| # | Visual | Appears at | Phrase (**trigger** in bold) | Leaves after | Clip window | Why |
|---|---|---|---|---|---|---|

Flag any file with no clear match and any match you're unsure of. Then list **Visuals worth sourcing**: repeat mentions and long stretches (~15s+) with nothing on screen. Wait for their OK or corrections.

10. After approval, copy the visuals into the work folder with descriptive names, numbered in timeline order (e.g. `01_stripe-revenue-dashboard.png`). Write `WORKDIR/names.json` (`[{"src": ..., "name": "stripe-revenue-dashboard"}, ...]`, short kebab-case names describing what each shows) and run:
    ```bash
    python scripts/stage_screenshots.py WORKDIR/names.json --dest WORKDIR/screenshots
    ```
    This only copies — originals are never renamed, moved or edited. Point `overlays.json` at the copies.

## 4. Write overlays.json and build

```json
{
  "defaults": {"duration": 4.0, "scale": 0.8, "lead": 0.15, "gap": 0.1},
  "overlays": [
    {"image": "screenshots/01_meetup-page.png", "phrase": "came across this physical AI meetup",
     "trigger": "physical", "until": "meetup"},
    {"image": "screenshots/02_claude-chat.png", "phrase": "take a photo of the wiring",
     "trigger": "photo", "until": "plugged in properly"},
    {"clip": "screenshots/03_button-press.mov", "phrase": "where I press the button",
     "trigger": "press", "until": "in different ways", "in": 1.0}
  ]
}
```
- `trigger` / `until`: see step 3. `until` overrides `duration`.
- `duration`: fixed seconds on screen when there's no `until` (default 4). Overlays are always clipped so they never overlap.
- `scale`: fraction of the frame the visual can fill (0.8 = 80%, centred, aspect kept).
- `lead`: appear this many seconds before the trigger word (default 0.15).
- `gap` (per item): space before the next overlay; `0` for seamless build-up steps (each step holds until the next).
- `in` (clips only): seconds into the clip to start from. Duration is capped at what's left of the clip. Clip audio is always dropped.
- `"start": 95.2` instead of `phrase` for manual timing.
- Optional `"end": "sentence"` in defaults ends overlays at the next pause instead. Tested worse than `until`; only use if asked.

```bash
python scripts/build_timeline.py --video VIDEO --overlays WORKDIR/overlays.json \
  --words WORKDIR/words.json --out-dir WORKDIR --preview
```
Outputs `<name>_overlays.fcpxml`, `overlays_prepped/` (screenshots fitted onto transparent frame-size PNGs; clips as silent full-length stream copies), and a 540p `<name>_preview.mp4`. Report the printed timing table and any WARNING lines. If it warns the video is **HDR** (iPhone "HDR Video" on), tell the user exports will look brighter than on their phone and to turn off **Settings → Camera → Record Video → HDR Video** for future recordings.

**Final export (to post without Resolve):** once the user is happy with the preview, rebuild with `--final` instead of `--preview`. It writes `<name>_final.mp4`: full resolution, H.264 (CRF 18) + AAC 192k, upload-ready, colours matching the source for standard (SDR) video. A 2.5-min vertical video takes ~3 min to render and is ~200 MB.

## 4b. Small edits to an existing video (new session)

If the user names a video that already has a `_overlays/` work folder ("on Test 3, keep the cart up longer"):
1. Read `WORKDIR/overlays.json`, `transcript.txt` and the preferences file. Don't re-transcribe, re-match or re-stage.
2. Back up `overlays.json` (copy to `overlays.prev.json`), then change **only** the overlays the user named — usually `until` (when it leaves) or `trigger` (when it appears). Leave every other approved timing alone.
3. Rebuild (`--preview`), show before → after times for the changed overlays, and send the preview. Export with `--final` when they're happy.
4. Turn the feedback into a general rule in the preferences file if it applies beyond this one overlay.

## 5. Hand-off

- Watch the preview first; fix timings in `overlays.json` and rebuild (fast). When happy, export `--final` and post.
- **Optional — Resolve:** open or create a project first (Project Manager's *Import Project* only takes `.drp`), then on the Edit page **File → Import → Timeline…** (⇧⌘I), click the `.fcpxml` file itself (not its folder), tick *Automatically import source clips into media pool*. Video lands on V1, overlays on V2.
- Don't move/rename the video or `overlays_prepped/` before importing (absolute paths). If media shows offline, relink to `overlays_prepped/`.
- In Resolve: fades = select all V2 clips → Cmd+T. Reposition/zoom via Inspector → Transform. Clips keep their full length as handles: slip them (Trim mode, drag the middle) to use a different part.

## Known limitations

- Overlays are centred, so on horizontal video they can cover the speaker's face (no position option yet).
- Whisper mishears product names ("8-in-1" for "48-in-1"); fuzzy matching usually still lands, else use `start` or `--model medium.en`.
- From a contact sheet Claude can tell what's happening in a clip, but not subtle on-screen detail (e.g. which small animation is playing).
- HDR recordings aren't converted to SDR; record with HDR off.
- Clip scaling in the FCPXML uses `adjust-transform`; confirm the clip size in Resolve's Inspector.
- One video file per run; combining multiple takes isn't supported yet.
