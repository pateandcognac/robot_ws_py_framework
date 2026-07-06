#!/home/robot/robot_ws/.venv/bin/python3.11
"""
Debug/fun tool: cycle through the Gemini-generated phrase bank at
~/src/ft_gemma_face/data/augmented/gemini_phrases/ (one JSON list of
natural-language phrases per emoji, e.g. emoji_face_seq__VS_button_.json),
run each phrase through the tiny fine-tuned face model, and save the
result into the same rolling per-emoji GenStore library the live pipeline
reads from (animations/face_generated/).

Good for: exercising the generation pipeline outside of ROS/TTS, building
up a starter library of saved faces before the robot has spoken much, and
watching frame-by-frame generation output live on stdout (--verbose,
default on) -- beat text, arrival latency, and any pre-clamp range
overshoot, straight from performance_lib.face_gen_client's verbose mode.

The phrase-bank filenames match animations/face_semantic/*.json 1:1, so
each phrase file's emoji is resolved by reading the 'emoji' field out of
the corresponding semantic LUT file rather than parsing the filename.

Usage:
    tools/cycle_gemini_phrases.py --count 5                # quick look
    tools/cycle_gemini_phrases.py --emoji 🆚                # one emoji
    tools/cycle_gemini_phrases.py --shuffle --count 20
    tools/cycle_gemini_phrases.py --dry-run                # list only
    tools/cycle_gemini_phrases.py --phrases-per-emoji 2 --temperature 0.5
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "src", "logos_hardware", "scripts")))

from performance_lib.face_gen_client import (
    DEFAULT_MODEL,
    DEFAULT_STORE_CAP,
    DEFAULT_STORE_DIR,
    FaceGenClient,
    FaceGenError,
    GenStore,
)

PHRASE_DIR = Path("/home/robot/src/ft_gemma_face/data/augmented/gemini_phrases")
FACE_LUT_DIR = Path("/home/robot/robot_ws/animations/face_semantic")


def resolve_emoji(phrase_path: Path) -> str:
    """Look up the emoji for a phrase file via its same-named LUT file."""
    lut_path = FACE_LUT_DIR / phrase_path.name
    if not lut_path.exists():
        return ""
    try:
        return json.load(open(lut_path, encoding="utf-8")).get("emoji", "")
    except Exception:
        return ""


def load_items(args) -> list:
    """Return [(phrase_path, emoji, [phrases...])], filtered/shuffled per args."""
    paths = sorted(glob.glob(str(PHRASE_DIR / "*.json")))
    items = []
    for p in paths:
        path = Path(p)
        emoji = resolve_emoji(path)
        if not emoji:
            continue
        if args.emoji and emoji != args.emoji:
            continue
        try:
            phrases = json.load(open(path, encoding="utf-8"))
        except Exception as exc:
            print(f"WARN: unreadable {path.name}: {exc}")
            continue
        if not isinstance(phrases, list) or not phrases:
            continue
        items.append((path, emoji, phrases))

    if args.shuffle:
        random.shuffle(items)
    if args.count:
        items = items[: args.count]
    return items


def pick_phrases(phrases: list, n: int) -> list:
    if n >= len(phrases):
        return list(phrases)
    return random.sample(phrases, n)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=0, help="limit number of emoji files (0 = all)")
    ap.add_argument("--emoji", default="", help="only this one emoji")
    ap.add_argument("--phrases-per-emoji", type=int, default=1)
    ap.add_argument("--shuffle", action="store_true", help="randomize emoji file order")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0, help="0 = fresh/unseeded each call")
    ap.add_argument("--no-stream", action="store_true", help="use blocking generate() instead of streaming")
    ap.add_argument("--store-dir", default=DEFAULT_STORE_DIR)
    ap.add_argument("--store-cap", type=int, default=DEFAULT_STORE_CAP)
    ap.add_argument("--quiet", action="store_true", help="suppress per-frame stdout detail")
    ap.add_argument("--dry-run", action="store_true", help="show what would run, no generation")
    args = ap.parse_args()

    items = load_items(args)
    total_phrases = sum(min(args.phrases_per_emoji, len(ph)) for _, _, ph in items)
    print(f"{len(items)} emoji file(s) matched, {total_phrases} phrase(s) queued "
          f"(model={args.model}, temp={args.temperature}, "
          f"seed={'fresh' if args.seed <= 0 else args.seed}, "
          f"{'blocking' if args.no_stream else 'streaming'}).")

    if args.dry_run:
        for path, emoji, phrases in items:
            chosen = pick_phrases(phrases, args.phrases_per_emoji)
            print(f"  {emoji}  {path.name}  -> {chosen}")
        return

    client = FaceGenClient(model=args.model)
    store = GenStore(args.store_dir, cap_per_emoji=args.store_cap)
    before = len(store)

    ok, failed = 0, 0
    run_t0 = time.time()

    for path, emoji, phrases in items:
        for phrase in pick_phrases(phrases, args.phrases_per_emoji):
            seed = None if args.seed <= 0 else args.seed
            print(f"\n=== {emoji}  \"{phrase}\"  ({path.name}) ===")
            t0 = time.time()
            try:
                if args.no_stream:
                    obj = client.generate(phrase, temperature=args.temperature, seed=seed,
                                          verbose=not args.quiet)
                else:
                    obj = None
                    for kind, payload in client.generate_stream(
                            phrase, temperature=args.temperature, seed=seed,
                            verbose=not args.quiet):
                        if kind == "done":
                            obj = payload
            except FaceGenError as exc:
                print(f"  FAILED: {exc}")
                failed += 1
                continue

            elapsed = time.time() - t0
            saved_name = store.save(emoji, obj, model=args.model, text=phrase,
                                    temperature=args.temperature, seed=seed)
            print(f"  saved -> {saved_name}  ({len(obj.get('frames', []))} frames, {elapsed:.1f}s)")
            ok += 1

    print(f"\nDone: {ok} saved, {failed} failed, {time.time() - run_t0:.1f}s total. "
          f"Store: {before} -> {len(store)} entries.")


if __name__ == "__main__":
    main()
