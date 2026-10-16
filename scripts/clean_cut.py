#!/usr/bin/env python3
"""Clean cut: take out fillers, long pauses, retakes and take cues BEFORE overlays are placed.

Two steps, with the user's approval in between:

  python clean_cut.py detect VIDEO --words WORKDIR/words.json --out-dir WORKDIR
      -> cuts.json      proposed cuts (source-video seconds), editable
         cuts_table.md  the table to show the user: what goes, why, time saved,
                        take comparison, and every untranscribed sound between words
  python clean_cut.py apply WORKDIR/cuts.json
      -> edl.json             kept pieces of the original video, in order
         words_cut.json       word times on the EDITED timeline (feed this to build_timeline.py)
         transcript_cut.txt   what the edited video says

Editing cuts.json after review:
  - "skip": true on a cut keeps that material (e.g. to use an earlier, better take).
  - "manual": [{"start": s, "end": e, "why": "..."}] adds cuts (source seconds).

How things are found:
  - Fillers: um/uh/ah/er words in the transcript, plus voiced sounds BETWEEN transcribed words
    (whisper usually leaves fillers out). Each such sound is re-transcribed on its own, with and
    without a filler-friendly prompt; only sounds that come back as a filler are cut.
  - Long pauses: any gap over --max-pause is shortened to --keep-pause (a short breath).
  - Take cues: "take two", "take 3", "start again", "start over", "try that again", "from the top".
  - Retakes: a sentence that is said again later (most of its words recur, in order, within
    --window seconds) is an earlier take; the LAST version is kept. Short fragments that the next
    sentence restarts are false starts. Between two take cues, a take that is mostly superseded
    is cut whole (stray "so…" included).
  - Stutters: a word or 2–3 word run said twice in a row ("and and and", "how I how I've").
  The last take isn't always the best: takes/sentences are scored (fillers, stutters, restarts,
  long pauses, completeness) and any earlier version that looks cleaner is flagged.
"""
import argparse, json, os, re, subprocess, sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from find_phrase import norm

FILLERS = {"um", "umm", "uh", "uhh", "uhm", "ah", "ahh", "er", "erm", "hmm", "hm", "mm", "mhm"}
NUMS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"}
NUMWORDS = {"ninety": "90s", "eighty": "80s", "seventy": "70s", "sixty": "60s", "fifty": "50s"}
CUE_PHRASES =[["start", "again"], ["start", "over"], ["try", "that", "again"], ["from", "the", "top"]]
OK_DOUBLES = {"much", "very", "so", "really", "no", "bye", "ha", "yeah", "had", "that"}
FILLER_PROMPT = "Umm, let me think like, hmm... Okay, uh, here's what I'm, like, thinking. Er, so, ah, yeah."
SR = 16000

def fmt(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"

def canon(w):
    """Token for comparing takes: lower-case, no punctuation/apostrophes, 1990s == 90s."""
    t = norm(w).replace("'", "")
    return re.sub(r"^(19|20)(\d\ds?)$", r"\2", t)

def load_audio(path):
    import numpy as np
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.float32)

def energy_db(audio):
    """Loudness per 10 ms frame, in dB."""
    import numpy as np
    hop = SR // 100
    n = len(audio) // hop
    return 20 * np.log10(np.sqrt((audio[:n * hop].reshape(n, hop) ** 2).mean(1)) + 1e-6)

def coverage(a, b):
    """Share of tokens in a that recur in b in order (runs of 2+ words; single words only for short a)."""
    if not a or not b:
        return 0.0
    m = SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    return sum(x.size for x in m if x.size >= 2 or len(a) <= 3) / len(a)

def stutter_eq(a, b):
    return a == b or (min(len(a), len(b)) >= 3 or min(a, b, key=len) == "i") and (a.startswith(b) or b.startswith(a))

def find_stutters(toks):
    """Indices to drop so 'and and and to' -> 'and to', 'how i how ive' -> 'how ive'."""
    drop, i, n = set(), 0, len(toks)
    while i < n:
        for k in (3, 2, 1):
            if i + 2 * k <= n and all(stutter_eq(toks[i + j], toks[i + k + j]) for j in range(k)) \
                    and not (k == 1 and toks[i] in OK_DOUBLES):
                drop.update(range(i, i + k))
                i += k
                break
        else:
            i += 1
    return drop

# ---------------------------------------------------------------- detect

def find_cues(words):
    toks = [canon(w["word"]) for w in words]
    cues = set()
    for i, t in enumerate(toks):
        if t == "take" and i + 1 < len(toks) and (toks[i + 1] in NUMS or toks[i + 1].isdigit()):
            cues.update({i, i + 1})
        for ph in CUE_PHRASES:
            if toks[i:i + len(ph)] == ph:
                cues.update(range(i, i + len(ph)))
    for i in sorted(cues):  # an aside right after the cue ("take four, I think…") before a pause
        if i + 1 in cues or i + 1 >= len(words):
            continue
        for n in (1, 2, 3):
            j = i + n
            if j + 1 < len(words) and words[j + 1]["start"] - words[j]["end"] >= 0.5 \
                    and words[j]["end"] - words[i]["end"] < 1.5:
                cues.update(range(i + 1, j + 1))
                break
    return cues

def chunk(words, cues, gap):
    """Split into sentence-ish chunks at pauses, sentence ends and take cues."""
    chunks, cur = [], []
    for i, w in enumerate(words):
        brk = cur and (w["start"] - words[cur[-1]]["end"] >= gap
                       or words[cur[-1]]["word"].rstrip().endswith((".", "?", "!"))
                       or (i in cues) != (cur[-1] in cues))
        if brk:
            chunks.append(cur); cur = []
        cur.append(i)
    if cur:
        chunks.append(cur)
    def split(c):  # whisper often runs a whole take together: break long runs at their biggest pause
        if len(c) <= 25:
            return [c]
        k = max(range(4, len(c) - 4), key=lambda j: words[c[j]]["start"] - words[c[j - 1]]["end"])
        return split(c[:k]) + split(c[k:])
    return [p for c in chunks for p in split(c)]

def sound_check(audio, db, words, s):
    """Voiced sounds between transcribed words, each re-transcribed on its own."""
    import numpy as np
    lo = np.percentile(db, 10)
    spoken = np.median([db[int((w["start"] + w["end"]) * 50)] for w in words if int((w["start"] + w["end"]) * 50) < len(db)])
    thr = lo + 0.5 * (spoken - lo)
    runs = []
    for a, b in zip(words, words[1:]):
        nb = {canon(a["word"]), canon(b["word"])}
        g0, g1 = a["end"] + 0.05, b["start"] - 0.05
        if g1 - g0 < 0.15:
            continue
        v = db[int(g0 * 100):int(g1 * 100)] > thr
        k = 0
        while k < len(v):
            if not v[k]:
                k += 1; continue
            e = k
            while e < len(v) and (v[e] or v[e:e + 8].any()):  # bridge dips < 80 ms
                e += 1
            if (e - k) / 100 >= s["min_sound"]:
                runs.append({"start": round(g0 + k / 100, 2), "end": round(g0 + e / 100, 2), "nb": nb})
            k = e
    if not runs:
        return []
    from faster_whisper import WhisperModel
    model = WhisperModel(s["model"], device="auto", compute_type="auto")
    pad = np.zeros(int(0.3 * SR), np.float32)
    for r in runs:
        clip = np.concatenate([pad, audio[int((r["start"] - 0.05) * SR):int((r["end"] + 0.05) * SR)], pad])
        heard = []
        for prompt in (None, FILLER_PROMPT):
            segs, _ = model.transcribe(clip, language="en", vad_filter=False, initial_prompt=prompt,
                                       suppress_tokens=[], condition_on_previous_text=False)
            heard.append(" ".join(x.text.strip() for x in segs).strip())
        r["plain"], r["prompted"] = heard
        nb = r.pop("nb")
        # drop "[BLANK_AUDIO]"/"(wind blowing)" tags and bits of the neighbouring words that bled in
        toks = [[canon(t) for t in re.sub(r"\[[^]]*\]|\([^)]*\)", " ", h).split() if canon(t)] for h in heard]
        own = lambda t: t not in FILLERS and not any(
            x and (x == t or SequenceMatcher(None, x, NUMWORDS.get(t, t)).ratio() >= 0.6) for x in nb)
        has_fill = [any(x in FILLERS for x in t) for t in toks]
        real = [x for x in toks[0] if own(x)]
        if all(has_fill) and not real:
            r["verdict"], r["conf"] = "filler", "high"
        elif any(has_fill) and not real:
            r["verdict"], r["conf"] = "filler", "medium"
        elif real:
            r["verdict"], r["conf"] = "words?", "-"   # possibly a real word whisper dropped: never cut
        else:
            r["verdict"], r["conf"] = "breath/noise", "-"
    return runs

def snap(db, t, lo, hi, win=0.06):
    """Move a cut point to the quietest 10 ms within +-win, staying inside [lo, hi]."""
    a, b = max(lo, t - win), min(hi, t + win)
    if b <= a:
        return t
    i0, i1 = int(a * 100), int(b * 100) + 1
    seg = db[i0:i1]
    return round((i0 + int(seg.argmin())) / 100, 3) if len(seg) else t

def take_stats(words, idx, sounds, rel_toks=None):
    ws = [words[i] for i in idx]
    toks = [canon(w["word"]) for w in ws]
    t0, t1 = ws[0]["start"], ws[-1]["end"]
    fill = sum(t in FILLERS for t in toks) + sum(1 for r in sounds if r["verdict"] == "filler" and t0 <= r["start"] <= t1)
    stut = len(find_stutters([t for t in toks if t not in FILLERS]))
    longp = sum(1 for a, b in zip(ws, ws[1:]) if b["start"] - a["end"] > 1.0)
    p = [w["p"] for w in ws if "p" in w]
    return {"start": t0, "end": t1, "words": len(ws), "fillers": fill, "stutters": stut, "long_pauses": longp,
            "conf": round(sum(p) / len(p), 2) if p else None,
            "complete": round(coverage(rel_toks, toks), 2) if rel_toks else None}

def detect(a):
    words = json.load(open(a.words))
    s = {"max_pause": a.max_pause, "keep_pause": a.keep_pause, "head": a.head, "tail": a.tail, "gap": 0.5,
         "cover": a.cover, "window": a.window, "min_sound": 0.12, "model": a.model}
    from build_timeline import probe
    total = probe(a.video)["duration"]
    audio = load_audio(a.video)
    db = energy_db(audio)
    toks = [canon(w["word"]) for w in words]
    why = {}  # word index -> (kind, note)

    cues = find_cues(words)
    for i in cues:
        why[i] = ("take cue", "")
    for i, t in enumerate(toks):
        if t in FILLERS:
            why[i] = ("filler", "")

    chunks = chunk(words, cues, s["gap"])
    content = lambda c: [toks[i] for i in c if toks[i] not in FILLERS and i not in cues]
    final_of = {}  # retake chunk -> the chunk that replaces it
    kept = []      # chunk numbers kept so far (walking backwards)
    for ci in range(len(chunks) - 1, -1, -1):
        c = chunks[ci]
        a_toks = content(c)
        if not a_toks:
            continue
        best, match = 0.0, None
        ahead = kept[::-1]  # later kept chunks, in time order
        for k, kj in enumerate(ahead):
            if words[chunks[kj][0]]["start"] - words[c[-1]]["end"] > s["window"]:
                break
            later = []  # a stretch of later speech about as long as this chunk
            for j in ahead[k:]:
                later += content(chunks[j])
                if len(later) >= 1.5 * len(a_toks) + 3:
                    break
            cov = coverage(a_toks, later)
            if cov > best:
                best, match = cov, kj
        nxt = kept[-1] if kept else None
        restart = (nxt is not None and len(a_toks) < 4
                   and words[chunks[nxt][0]]["start"] - words[c[-1]]["end"] < 10
                   and coverage(a_toks, content(chunks[nxt])[:len(a_toks) + 2]) >= 0.6)
        if len(a_toks) >= 4 and best >= s["cover"]:
            final_of[ci] = match
            for i in c:
                why.setdefault(i, ("retake", f"said again at {fmt(words[chunks[match][0]]['start'])}"))
        elif restart:
            final_of[ci] = nxt
            for i in c:
                why.setdefault(i, ("false start", f"restarted at {fmt(words[chunks[nxt][0]]['start'])}"))
        else:
            kept.append(ci)

    # takes between cues: a take that's mostly superseded goes whole (stray "so…" included)
    cue_chunks = [ci for ci, c in enumerate(chunks) if c[0] in cues]
    bounds = [-1] + cue_chunks + [len(chunks)]
    takes = []
    for z0, z1 in zip(bounds, bounds[1:]):
        zones = [[]]  # a silence over 5s also starts a new attempt
        for ci in range(z0 + 1, z1):
            if not content(chunks[ci]):
                continue
            if zones[-1] and z1 != len(chunks) and words[chunks[ci][0]]["start"] - words[chunks[zones[-1][-1]][-1]]["end"] > 5:
                zones.append([])
            zones[-1].append(ci)
        for zn, zone in enumerate(zones):
            if zone:
                takes.append({"chunks": zone, "idx": [i for ci in zone for i in chunks[ci]],
                              "last": z1 == len(chunks) and zn == len(zones) - 1})
        if z1 == len(chunks) or not cue_chunks:
            continue
        zone = [ci for ci in range(z0 + 1, z1) if content(chunks[ci])]
        if not zone:
            continue
        n = sum(len(content(chunks[ci])) for ci in zone)
        gone = sum(len(content(chunks[ci])) for ci in zone if ci in final_of)
        if gone / n >= 0.5:
            for ci in zone:
                if ci not in final_of and len(content(chunks[ci])) < 8:
                    for i in chunks[ci]:
                        why.setdefault(i, ("retake", "rest of a superseded take"))

    # stutters in what's left
    left = [i for i in range(len(words)) if i not in why and toks[i] not in FILLERS]
    for j in find_stutters([toks[i] for i in left]):
        if j + 1 < len(left) and words[left[j + 1]]["start"] - words[left[j]]["end"] < 1.0:
            why[left[j]] = ("stutter", "")

    # untranscribed sounds between words that we keep (fillers whisper left out)
    keep_idx = [i for i in range(len(words)) if i not in why]
    sounds = [] if a.no_sound_check else sound_check(audio, db, words, s)
    items = [dict(words[i], i=i, gone=i in why, kind=why.get(i, ("", ""))[0], note=why.get(i, ("", ""))[1])
             for i in range(len(words))]
    for r in sounds:
        if r["verdict"] == "filler":
            items.append({"word": f"({r['plain'] or r['prompted']})", "start": r["start"], "end": r["end"],
                          "gone": True, "kind": "filler (untranscribed)", "note": f"confidence {r['conf']}"})
    items.sort(key=lambda w: w["start"])
    kept_items = [w for w in items if not w["gone"]]
    if not kept_items:
        sys.exit("Everything would be cut; check the transcript.")

    # kept speech -> cuts between it
    cuts = []
    def add(c0, c1, between):
        if c1 - c0 >= 0.05:
            cuts.append({"start": round(c0, 3), "end": round(c1, 3), "items": between})
    first, last = kept_items[0], kept_items[-1]
    pre = [w for w in items if w["gone"] and w["start"] < first["start"]]
    add(0.0, max(first["start"] - s["head"], max([w["end"] for w in pre], default=0)), pre)
    for x, y in zip(kept_items, kept_items[1:]):
        between = [w for w in items if w["gone"] and x["start"] < w["start"] < y["start"]]
        g = y["start"] - x["end"]
        if not between and g <= s["max_pause"]:
            continue
        h = s["keep_pause"] / 2
        if between:
            c0 = min(x["end"] + h, between[0]["start"])
            c1 = max(y["start"] - h, max(w["end"] for w in between))
            c0, c1 = snap(db, c0, x["end"], between[0]["start"]), snap(db, c1, max(w["end"] for w in between), y["start"])
        else:
            c0, c1 = snap(db, x["end"] + h, x["end"], y["start"]), snap(db, y["start"] - h, x["end"], y["start"])
        add(c0, c1, between)
    post = [w for w in items if w["gone"] and w["start"] > last["start"]]
    add(min(last["end"] + s["tail"], min([w["start"] for w in post], default=total)), total, post)

    order = ["take cue", "retake", "false start", "stutter", "filler", "filler (untranscribed)"]
    for n, c in enumerate(cuts, 1):
        kinds = sorted({w["kind"] for w in c["items"]}, key=order.index)
        notes = sorted({w["note"] for w in c["items"] if w["note"] and w["kind"] in ("retake", "false start")})
        if c["end"] - c["start"] > 10 and notes:
            notes = [f"earlier attempts; kept version starts {fmt(c['end'])}"]
        c.update({"id": n, "saves": round(c["end"] - c["start"], 2), "why": ", ".join(kinds) or "long pause",
                  "removed": " ".join(w["word"] for w in c["items"]), "note": "; ".join(notes[:2])})
        del c["items"]

    # is the last version really the best?
    flags, take_rows = [], []
    if len(takes) > 1 and cue_chunks:
        fin = next(t for t in reversed(takes) if t["last"])
        rel = [toks[i] for i in fin["idx"] if toks[i] not in FILLERS and i not in cues]
        for n, t in enumerate(takes, 1):
            st = take_stats(words, [i for i in t["idx"] if i not in cues], sounds, rel)
            restarts = sum(1 for k, ci in enumerate(t["chunks"]) if len(content(chunks[ci])) >= 3 and any(
                coverage(content(chunks[ci]), content(chunks[cj])) >= s["cover"] for cj in t["chunks"][k + 1:]))
            st.update({"take": n, "restarts": restarts, "kept": t is fin})
            st["issues"] = st["fillers"] + st["stutters"] + st["long_pauses"] + st["restarts"]
            take_rows.append(st)
        f = next(r for r in take_rows if r["kept"])
        for r in take_rows:
            if not r["kept"] and r["complete"] >= 0.8 and r["issues"] < f["issues"]:
                flags.append(f"Take {r['take']} ({fmt(r['start'])}–{fmt(r['end'])}) has {r['issues']} issues vs "
                             f"{f['issues']} in the kept take {f['take']}, and covers {r['complete']:.0%} of its words. "
                             f"Listen to both before approving.")
    for ci, kj in final_of.items():  # sentence level, for videos without cues too
        while kj in final_of:
            kj = final_of[kj]
        ea = [i for i in chunks[ci] if i not in cues]
        if len(content(chunks[ci])) < 4:
            continue
        mine = take_stats(words, ea, sounds, content(chunks[kj]))
        theirs = take_stats(words, chunks[kj], sounds)
        bad = lambda t: t["fillers"] + t["stutters"] + t["long_pauses"]
        if mine["complete"] >= 0.9 and bad(mine) < bad(theirs):
            flags.append(f"Sentence at {fmt(mine['start'])} (\"{' '.join(words[i]['word'] for i in ea)[:60]}…\") "
                         f"is cleaner ({bad(mine)} vs {bad(theirs)} issues) than the kept version at {fmt(theirs['start'])}.")

    out = Path(a.out_dir)
    saved = sum(c["saves"] for c in cuts)
    doc = {"video": str(Path(a.video).resolve()), "words": str(Path(a.words).resolve()), "duration": total,
           "settings": s, "cuts": cuts, "manual": [], "sounds": sounds, "takes": take_rows, "flags": flags}
    json.dump(doc, open(out / "cuts.json", "w"), indent=1)
    (out / "cuts_table.md").write_text(table(doc, saved))
    print(table(doc, saved))
    print(f"\n-> {out / 'cuts.json'}\n-> {out / 'cuts_table.md'}")

def table(doc, saved):
    total = doc["duration"]
    L = [f"**{fmt(total)} → {fmt(total - saved)}** (saves {saved:.1f}s, {len(doc['cuts'])} cuts)", "",
         "| # | From → to | Removed | Why | Saves |", "|---|---|---|---|---|"]
    pauses = [c for c in doc["cuts"] if c["why"] == "long pause"]
    for c in doc["cuts"]:
        if c["why"] == "long pause":
            continue
        why = c["why"] + (f" ({c['note']})" if c["note"] else "")
        txt = c["removed"] if len(c["removed"]) <= 90 else c["removed"][:60] + " … " + c["removed"][-25:]
        L.append(f"| {c['id']} | {fmt(c['start'])} → {fmt(c['end'])} | {txt or '(silence)'} | {why} | {c['saves']:.1f}s |")
    if pauses:
        L.append(f"| – | {len(pauses)} long pauses shortened to {doc['settings']['keep_pause']}s | "
                 + ", ".join(f"#{c['id']} {fmt(c['start'])} (−{c['saves']:.1f})" for c in pauses)
                 + f" | long pause | {sum(c['saves'] for c in pauses):.1f}s |")
    if doc["takes"]:
        L += ["", "**Takes**", "", "| Take | Time | Words | Fillers | Stutters | Restarts | Long pauses | Covers final | Kept |",
              "|---|---|---|---|---|---|---|---|---|"]
        for r in doc["takes"]:
            L.append(f"| {r['take']} | {fmt(r['start'])}–{fmt(r['end'])} | {r['words']} | {r['fillers']} | {r['stutters']} | "
                     f"{r['restarts']} | {r['long_pauses']} | {r['complete']:.0%} | {'✅' if r['kept'] else ''} |")
    if doc["flags"]:
        L += ["", "**⚠️ Earlier version may be better**", ""] + [f"- {f}" for f in doc["flags"]]
    if doc["sounds"]:
        L += ["", "**Sounds between words (filler check)**", "", "| Time | Length | Heard alone | With filler prompt | Verdict |",
              "|---|---|---|---|---|"]
        for r in doc["sounds"]:
            L.append(f"| {fmt(r['start'])} | {r['end'] - r['start']:.2f}s | {r['plain'] or '–'} | {r['prompted'] or '–'} | "
                     f"{r['verdict']}{' (' + r['conf'] + ')' if r['conf'] != '-' else ''} |")
    return "\n".join(L) + "\n"

# ---------------------------------------------------------------- apply

def apply(a):
    doc = json.load(open(a.cuts))
    from build_timeline import probe
    meta = probe(doc["video"])
    fps, total = meta["fps"], doc["duration"]
    spans = sorted([(c["start"], c["end"]) for c in doc["cuts"] if not c.get("skip")]
                   + [(m["start"], m["end"]) for m in doc.get("manual", [])])
    merged = []
    for s0, s1 in spans:
        if merged and s0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s1)
        else:
            merged.append([s0, s1])
    keep, t = [], 0.0
    for s0, s1 in merged + [[total, total]]:
        if s0 > t:
            keep.append((t, s0))
        t = max(t, s1)
    fr = lambda x: round(x * fps)  # frame-aligned so render, FCPXML and word times agree
    segs, out_f = [], 0
    last_f = int(total * fps)
    for s0, s1 in keep:
        f0, f1 = fr(s0), min(fr(s1), last_f)
        if f1 - f0 < 2:
            continue
        segs.append({"src_in": float(f0 / fps), "src_out": float(f1 / fps), "frames": f1 - f0,
                     "out_in": float(out_f / fps), "out_out": float((out_f + f1 - f0) / fps)})
        out_f += f1 - f0
    words = json.load(open(doc["words"]))
    cut_words = []
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        sg = next((g for g in segs if g["src_in"] <= mid < g["src_out"]), None)
        if sg:
            m = lambda x: round(sg["out_in"] + min(max(x, sg["src_in"]), sg["src_out"]) - sg["src_in"], 3)
            cut_words.append(dict(w, start=m(w["start"]), end=m(w["end"])))
    out = Path(a.cuts).parent
    edl = {"video": doc["video"], "fps": f"{fps.numerator}/{fps.denominator}", "source_duration": total,
           "duration": float(out_f / fps), "segments": segs}
    json.dump(edl, open(out / "edl.json", "w"), indent=1)
    json.dump(cut_words, open(out / "words_cut.json", "w"), indent=1)
    lines, cur = [], []
    for k, w in enumerate(cut_words):
        cur.append(w)
        nxt = cut_words[k + 1] if k + 1 < len(cut_words) else None
        if nxt is None or w["word"].endswith((".", "?", "!")) or nxt["start"] - w["end"] > 0.5:
            lines.append(f"[{fmt(cur[0]['start'])}] " + " ".join(x["word"] for x in cur)); cur = []
    (out / "transcript_cut.txt").write_text("\n".join(lines) + "\n")
    print(f"{len(segs)} pieces, {fmt(total)} -> {fmt(edl['duration'])}\n-> {out / 'edl.json'}\n"
          f"-> {out / 'words_cut.json'} (use with build_timeline.py --edl)\n-> {out / 'transcript_cut.txt'}")

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("video")
    d.add_argument("--words", required=True)
    d.add_argument("--out-dir", required=True)
    d.add_argument("--max-pause", type=float, default=0.6, help="shorten gaps longer than this (s)")
    d.add_argument("--keep-pause", type=float, default=0.3, help="breath left where a gap/cut was (s)")
    d.add_argument("--head", type=float, default=0.2, help="silence kept before the first word")
    d.add_argument("--tail", type=float, default=0.5, help="kept after the last word")
    d.add_argument("--cover", type=float, default=0.6, help="share of a sentence said again to count as a retake")
    d.add_argument("--window", type=float, default=180, help="how far ahead (s) to look for the retake")
    d.add_argument("--model", default="small.en", help="whisper model for the sound check")
    d.add_argument("--no-sound-check", action="store_true", help="skip re-transcribing sounds between words")
    ap = sub.add_parser("apply")
    ap.add_argument("cuts")
    a = p.parse_args()
    detect(a) if a.cmd == "detect" else apply(a)

if __name__ == "__main__":
    main()
