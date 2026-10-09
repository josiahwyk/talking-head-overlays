#!/usr/bin/env python3
"""Find where a spoken phrase occurs in words.json (fuzzy, punctuation-insensitive).

Usage: python find_phrase.py WORKDIR/words.json "conversion rate dropped" [--top 3]
Prints the best matches with start/end seconds and a context snippet.
"""
import argparse, json, re
from difflib import SequenceMatcher

def norm(s):
    return re.sub(r"[^a-z0-9']", "", s.lower())

def search(words, phrase, top=3):
    target = [norm(t) for t in phrase.split() if norm(t)]
    toks = [norm(w["word"]) for w in words]
    n = len(target)
    results = []
    for i in range(0, max(1, len(toks) - n + 1)):
        window = toks[i:i + n]
        score = SequenceMatcher(None, " ".join(target), " ".join(window)).ratio()
        results.append((score, i))
    results.sort(reverse=True)
    out, used = [], set()
    for score, i in results:
        if any(abs(i - u) < n for u in used):
            continue
        used.add(i)
        j = min(i + n, len(words)) - 1
        ctx = " ".join(w["word"] for w in words[max(0, i - 5):j + 6])
        out.append({"score": round(score, 2), "start": words[i]["start"],
                    "end": words[j]["end"], "context": ctx})
        if len(out) >= top:
            break
    return out

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("words_json")
    p.add_argument("phrase")
    p.add_argument("--top", type=int, default=3)
    a = p.parse_args()
    words = json.load(open(a.words_json))
    for r in search(words, a.phrase, a.top):
        print(f"{r['score']:.2f}  {r['start']:8.2f}s -> {r['end']:8.2f}s  | {r['context']}")
