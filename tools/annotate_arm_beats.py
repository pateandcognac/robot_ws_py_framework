#!/home/robot/robot_ws/.venv/bin/python3.11
"""
Annotate Logos's legacy arm animation LUT with per-keyframe "beat" text and
save the result in the semantic arm format (animations/arms_semantic/).

Uses gemini-2.5-flash-lite with two curated few-shot examples. The model only
writes the beat strings -- pose numbers are converted programmatically by
arm_animation_schema.legacy_to_semantic(), so it cannot corrupt motion data.

Usage:
    tools/annotate_arm_beats.py --limit 3          # pilot
    tools/annotate_arm_beats.py                    # full run (resumable)
    tools/annotate_arm_beats.py --dry-run          # show prompts, no API calls
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from arm_animation_schema import (
    legacy_to_semantic,
    expand_semantic_arm_frames,
    validate_semantic_arm_sequence,
)

WORKSPACE = Path(__file__).parent.parent
ARMS_DIR = WORKSPACE / "animations" / "arms"
OUT_DIR = WORKSPACE / "animations" / "arms_semantic"
LOG_PATH = Path(__file__).parent / "arm_beats_log.jsonl"

MODEL = "gemini-2.5-flash-lite"

INSTRUCTIONS = """\
You are annotating keyframe "beats" for a small robot's arm animations.
The robot (Logos) has two simple tube arms; each arm has two shoulder
joints (joint1, joint2) and a wrist rotation, all in degrees -90..90.
The rest pose is roughly joint1=10, joint2=-85, wrist=0 (arms hanging
relaxed at the sides). Higher joint2 raises the arm; wrist spins the tube.

For each keyframe of the animation you will write ONE short "beat": a
present-tense stage direction describing what the arms are doing and why,
in the spirit of the emoji being performed. Beats read like shot notes in
a storyboard: concrete, physical, evocative, lowercase, no more than ~12
words. Do not mention joint numbers or degrees. Return exactly one beat
per keyframe, in order, as JSON: {"beats": ["...", "..."]}."""

# Two curated few-shot examples built from real LUT sequences.
FEWSHOT = [
    {
        "input": """\
emoji: 🌊 (water wave)
ideation: This sequence mimics the gentle, flowing motion of a wave. The arms start in a relaxed position and roll through a swell.
keyframes: 4
frame 0: left(j1=10, j2=-85, w=0) right(j1=10, j2=-85, w=0)
frame 1: left(j1=-20, j2=60, w=90) right(j1=20, j2=-85, w=0)
frame 2: left(j1=20, j2=-60, w=0) right(j1=-10, j2=-80, w=0)
frame 3: left(j1=10, j2=-85, w=0) right(j1=10, j2=-85, w=0)""",
        "output": {"beats": [
            "arms rest at the sides, calm water",
            "the left arm sweeps up and curls like a rising swell",
            "the crest rolls down and across toward the right",
            "both arms settle back into stillness",
        ]},
    },
    {
        "input": """\
emoji: 🤔 (thinking face)
ideation: This sequence captures the essence of thinking deeply, using the left hand to tap the chin while the right arm stays tucked.
keyframes: 6
frame 0: left(j1=25, j2=90, w=-90) right(j1=-20, j2=-90, w=-10)
frame 1: left(j1=20, j2=75, w=-90) right(j1=-20, j2=-90, w=-10)
frame 2: left(j1=25, j2=90, w=-90) right(j1=-20, j2=-90, w=-10)
frame 3: left(j1=30, j2=70, w=-90) right(j1=-20, j2=-90, w=-10)
frame 4: left(j1=20, j2=90, w=-90) right(j1=25, j2=-60, w=-10)
frame 5: left(j1=20, j2=-90, w=0) right(j1=20, j2=-90, w=0)""",
        "output": {"beats": [
            "left hand rises to the chin, right arm tucked away",
            "a slow pondering dip as the thought turns over",
            "the hand taps the chin again, digging deeper",
            "leaning into the idea, weighing it carefully",
            "the right arm stirs as an idea sparks",
            "both arms drop, the conclusion is reached",
        ]},
    },
]

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "beats": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["beats"],
}

_log_lock = threading.Lock()


def emoji_slug(emoji: str) -> str:
    parts = []
    for ch in emoji:
        try:
            parts.append(unicodedata.name(ch).lower())
        except ValueError:
            parts.append("u{:04x}".format(ord(ch)))
    slug = "_".join(parts)
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug[:80] or "unknown"


def load_legacy_entries():
    """{emoji: legacy entry}, glob-order overwrite like the runtime loader."""
    entries = {}
    for path in sorted(glob.glob(str(ARMS_DIR / "emoji_arm_seq_*.json"))):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as exc:
            print(f"WARN: unreadable {path}: {exc}")
            continue
        for entry in (data if isinstance(data, list) else [data]):
            if isinstance(entry, dict) and entry.get("emoji") and entry.get("frames"):
                entries[entry["emoji"]] = entry
    return entries


def render_input(semantic) -> str:
    expanded = expand_semantic_arm_frames(semantic["frames"])
    lines = [
        "emoji: {}".format(semantic["emoji"]),
        "ideation: {}".format(semantic["ideation"].strip()),
        "keyframes: {}".format(len(expanded)),
    ]
    for i, frame in enumerate(expanded):
        l, r = frame["arms"]["left"], frame["arms"]["right"]
        lines.append(
            "frame {}: left(j1={:g}, j2={:g}, w={:g}) right(j1={:g}, j2={:g}, w={:g})".format(
                i, l["joint1"], l["joint2"], l["wrist"],
                r["joint1"], r["joint2"], r["wrist"]))
    return "\n".join(lines)


def build_prompt(semantic) -> str:
    blocks = [INSTRUCTIONS, ""]
    for ex in FEWSHOT:
        blocks += ["EXAMPLE INPUT:", ex["input"], "EXAMPLE OUTPUT:",
                   json.dumps(ex["output"], ensure_ascii=False), ""]
    blocks += ["INPUT:", render_input(semantic), "OUTPUT:"]
    return "\n".join(blocks)


def request_beats(client, semantic, retries=2):
    from google.genai import types
    prompt = build_prompt(semantic)
    n = len(semantic["frames"])
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                    temperature=0.4,
                ),
            )
            beats = json.loads(resp.text)["beats"]
            if len(beats) == n and all(isinstance(b, str) and b.strip() for b in beats):
                return [b.strip() for b in beats], None
            last_err = "beat count mismatch: want {}, got {}".format(n, len(beats))
        except Exception as exc:
            last_err = str(exc)
            time.sleep(1.5 * (attempt + 1))
    return None, last_err


def log_result(record):
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_entry(client, emoji, entry, dry_run=False):
    semantic = legacy_to_semantic(entry)
    out_path = OUT_DIR / "emoji_arm_seq_{}.json".format(emoji_slug(emoji))

    if out_path.exists():
        try:
            existing = json.load(open(out_path, encoding="utf-8"))
            if all(f.get("beat") for f in existing.get("frames", [])):
                return "skip", str(out_path)
        except Exception:
            pass

    if dry_run:
        print(build_prompt(semantic)[:1200])
        return "dry", str(out_path)

    beats, err = request_beats(client, semantic)
    if beats is None:
        log_result({"ts": time.time(), "emoji": emoji, "status": "error", "error": err})
        return "error", err

    for frame, beat in zip(semantic["frames"], beats):
        frame["beat"] = beat

    errors = validate_semantic_arm_sequence(semantic)
    status = "ok" if not errors else "ok_with_range_warnings"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(semantic, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log_result({"ts": time.time(), "emoji": emoji, "status": status,
                "beats": beats, "validation": errors[:5], "file": out_path.name})
    return status, str(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--emoji", help="annotate a single emoji")
    args = ap.parse_args()

    entries = load_legacy_entries()
    print(f"{len(entries)} legacy arm sequences loaded.")
    items = list(entries.items())
    if args.emoji:
        items = [(e, v) for e, v in items if e == args.emoji]
    if args.limit:
        items = items[: args.limit]

    client = None
    if not args.dry_run:
        from google import genai
        api_key = (os.environ.get("GEMINI_API_KEY")
                   or os.environ.get("PAID_GEMINI_API_KEY")
                   or os.environ.get("FREE_GEMINI_API_KEY"))
        if not api_key:
            sys.exit("No GEMINI_API_KEY / PAID_GEMINI_API_KEY / FREE_GEMINI_API_KEY in env.")
        client = genai.Client(api_key=api_key)

    counts = {}
    if args.dry_run or len(items) <= 2:
        for emoji, entry in items:
            status, info = process_entry(client, emoji, entry, dry_run=args.dry_run)
            counts[status] = counts.get(status, 0) + 1
            print(f"{emoji} -> {status} {info if status=='error' else ''}")
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process_entry, client, e, v): e for e, v in items}
            done = 0
            for fut in as_completed(futures):
                emoji = futures[fut]
                try:
                    status, info = fut.result()
                except Exception as exc:
                    status, info = "error", str(exc)
                counts[status] = counts.get(status, 0) + 1
                done += 1
                if status == "error":
                    print(f"{emoji} ERROR: {info}")
                if done % 50 == 0:
                    print(f"... {done}/{len(items)} ({counts})")

    print("Done:", counts)


if __name__ == "__main__":
    main()
