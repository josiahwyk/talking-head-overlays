# talking-head-overlays

A [Claude Code](https://claude.com/claude-code) skill that puts your screenshots and B-roll clips on a talking-head video **at the moment you say the word that names them**, and takes them off when you've finished talking about them.

You give Claude a video and a folder of screenshots. You get back:

- a **match table** to approve before anything is built: which visual, which word brings it on, which words take it off, which part of each clip
- a **540p preview** to check the timing
- an **upload-ready `_final.mp4`** (full resolution, original audio)
- a **DaVinci Resolve timeline** (`.fcpxml`) if you want to fine-tune by hand

No more scrubbing through audio looking for "where did I say *five dollars*?"

## How it works

1. **Transcribe** the video locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), with a timestamp for every word.
2. **Look at every visual.** Claude views each screenshot, and makes a timestamped contact sheet for each video clip so it can pick the useful 3–5 seconds.
3. **Match by meaning.** For each visual Claude picks the phrase where you first mention it, the **trigger** word that brings it on screen, and the **until** words where you finish that thought.
4. **You approve the table.** Weak matches are flagged. It also lists **visuals worth sourcing**: spots where you talk about something you don't have a screenshot for.
5. **Build.** Visuals are copied into a work folder with descriptive names (originals are never touched), fitted to the frame, and rendered.

Extra touches:

- **Several screenshots for one sentence** (e.g. four order receipts) stack into a list that builds up item by item, each one on the word that names it, instead of flickering past.
- **Each visual is used once**, at first mention. Repeat mentions go on the "worth sourcing" list instead of reusing images.
- Clip audio is always dropped so it never competes with your voice.
- **Small fixes in a new session**: "on Test 3, keep the cart up longer" changes just that overlay and rebuilds in about a minute.
- **Preferences that stick**: feedback you give while watching a preview is saved to `~/.claude/skill-preferences/talking-head-overlays.md` and applied to every future video.

## Access

This skill is published for viewing only and is not available for public use. To request permission, see [my GitHub profile](https://github.com/josiahwyk) for contact details.

## Tips

- **iPhone users: turn off HDR video** (Settings → Camera → Record Video → HDR Video). HDR footage looks brighter in editors and exports than on the phone. The skill warns you if a video is HDR.
- Descriptive screenshot names help matching, but aren't required.
- Text-heavy screenshots (chats, articles) are given longer on screen so they can be read.

## Scripts

| Script | Does |
|---|---|
| `transcribe.py` | Word-level transcript (`words.json`, `transcript.txt`) |
| `find_phrase.py` | Fuzzy-find a phrase and its timestamps |
| `clip_frames.py` | Timestamped contact sheet of a video clip |
| `stage_screenshots.py` | Copy visuals into the work folder with numbered, descriptive names |
| `stack_images.py` | Build-up stack of several screenshots |
| `build_timeline.py` | Timing, FCPXML, prepped overlays, preview and final render |

`SKILL.md` has the full workflow Claude follows and the `overlays.json` format.

## Known limitations

- Overlays are centred, so on horizontal video they can cover your face. There's no position option yet.
- Whisper can mishear product names. Matching is fuzzy, and you can switch to `--model medium.en`.
- HDR footage isn't converted to SDR.
- One video file per run. Assembling the best takes from multiple recordings isn't supported yet.

## License

Copyright © 2026 Josiah Lau. All rights reserved. This repository is published for viewing only. You may not use, copy, modify or distribute it without my written permission. To request permission, see [my GitHub profile](https://github.com/josiahwyk) for contact details. See `LICENSE`.
