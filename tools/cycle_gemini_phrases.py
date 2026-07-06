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

    # --speak: actually say each phrase through the real Speak action
    # (director -> animator -> sequencer, the full running pipeline) so you
    # can watch synced face/arm playback and cue queueing live on the robot,
    # instead of only generating+saving in isolation. Requires ROS and a
    # running tts_action_server/performance_sequencer/face_animator(/arm_animator).
    # --sync defaults on here (see TTP_V2.md) so fast engines actually get to
    # show off live generation instead of always falling back to the LUT --
    # this script is exactly the timing/aesthetics evaluation case it's for.
    tools/cycle_gemini_phrases.py --speak --engine kokoro --count 10
    tools/cycle_gemini_phrases.py --speak --engine piper --pause 2.5
    tools/cycle_gemini_phrases.py --speak --engine espeak --face-policy lut
    tools/cycle_gemini_phrases.py --speak --engine espeak --no-sync  # compare vs. old behavior

    # --mutate: add entropy to each training-set phrase (word jostle, 1-2
    # random system-dictionary words, misspelling, casing, emoji position)
    # so runs aren't just verbatim recall of what the model saw in training.
    tools/cycle_gemini_phrases.py --speak --mutate --count 10
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


ENGINES = ("kokoro", "piper", "espeak", "festival")

DICT_PATHS = (
    "/usr/share/dict/words",
    "/usr/share/dict/american-english",
    "/usr/share/dict/british-english",
)
_DICT_WORDS_CACHE = None


def dict_words() -> list:
    """Linux system dictionary word list (cached), filtered to plain a-z words."""
    global _DICT_WORDS_CACHE
    if _DICT_WORDS_CACHE is None:
        _DICT_WORDS_CACHE = []
        for p in DICT_PATHS:
            path = Path(p)
            if not path.exists():
                continue
            try:
                _DICT_WORDS_CACHE = [
                    w for w in (line.strip() for line in path.read_text(
                        encoding="utf-8", errors="ignore").splitlines())
                    if w.isalpha() and 2 <= len(w) <= 10
                ]
            except Exception:
                continue
            if _DICT_WORDS_CACHE:
                break
    return _DICT_WORDS_CACHE


def _misspell(word: str) -> str:
    """Swap two adjacent interior letters -- a plausible-looking typo."""
    if len(word) < 4:
        return word
    i = random.randrange(1, len(word) - 1)
    chars = list(word)
    chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return "".join(chars)


def _randcase(word: str) -> str:
    r = random.random()
    if r < 0.34:
        return word.upper()
    if r < 0.67:
        return word.lower()
    return word.capitalize()


def mutate_phrase(phrase: str, emoji: str) -> str:
    """
    Add entropy to a training-set phrase so playback isn't just verbatim
    recall: jostle word order slightly, sprinkle 1-2 real dictionary words,
    misspell a word, randomize casing on a couple words, and move the
    emoji to a random position (start/end/mid) instead of always trailing.
    """
    words = phrase.split()
    if len(words) >= 3 and random.random() < 0.5:
        i = random.randrange(len(words) - 1)
        words[i], words[i + 1] = words[i + 1], words[i]

    dw = dict_words()
    if dw and random.random() < 0.6:
        for _ in range(random.randint(1, 2)):
            words.insert(random.randrange(len(words) + 1), random.choice(dw))

    if words and random.random() < 0.5:
        i = random.randrange(len(words))
        words[i] = _misspell(words[i])

    for _ in range(random.randint(0, 2)):
        if words:
            i = random.randrange(len(words))
            words[i] = _randcase(words[i])

    text = " ".join(words)

    if emoji:
        pos = random.choice(("start", "end", "mid"))
        if pos == "start":
            text = f"{emoji} {text}"
        elif pos == "end":
            text = f"{text} {emoji}"
        else:
            ws = text.split()
            i = random.randrange(len(ws) + 1) if ws else 0
            ws.insert(i, emoji)
            text = " ".join(ws)
    return text


def run_speak_mode(args, items):
    """
    Send each phrase through the real 'speak' action -- the full running
    TTP v2 pipeline (director -> animator -> sequencer) -- instead of
    calling the face model directly. Lets you watch actual synced
    speech+face(+arm) playback and cue queueing on the robot. Requires ROS
    and a live tts_action_server / performance_sequencer / face_animator.
    """
    import rospy
    import actionlib
    from logos_msgs.msg import SpeakAction, SpeakGoal

    rospy.init_node("cycle_gemini_phrases_speaker", anonymous=True, disable_signals=True)
    client = actionlib.SimpleActionClient("speak", SpeakAction)
    print(f"Waiting for 'speak' action server (engine={args.engine})...")
    if not client.wait_for_server(rospy.Duration(8.0)):
        sys.exit("No 'speak' action server found -- is tts_action_server running?")

    performance = {}
    if args.face_policy:
        performance["face_policy"] = args.face_policy
    if args.temperature is not None:
        performance["temperature"] = args.temperature
    if args.sync:
        performance["sync"] = True

    ok, failed = 0, 0
    run_t0 = time.time()

    try:
        for path, emoji, phrases in items:
            for phrase in pick_phrases(phrases, args.phrases_per_emoji):
                # Emoji trails the phrase, same convention as emote.ttp()
                # docstrings: it marks the clause just spoken, not the next.
                text = mutate_phrase(phrase, emoji) if args.mutate else f"{phrase} {emoji}".strip()
                print(f"\n=== speaking ({args.engine}): \"{text}\"  ({path.name}) ===")

                goal = SpeakGoal()
                goal.utterance_text = text
                goal.engine = args.engine
                goal.engine_params = json.dumps({"performance": performance} if performance else {})

                def _fb(f):
                    print(f"  chunk {f.current_chunk_index + 1}/{f.total_chunks}: "
                          f"'{f.text_snippet}' {f.emoji_snippet}  ({f.chunk_duration:.2f}s)")

                client.send_goal(goal, feedback_cb=_fb)
                finished = client.wait_for_result(rospy.Duration(args.wait_timeout))
                result = client.get_result()

                if finished and result and result.success:
                    print(f"  done: {result.total_duration:.1f}s spoken.")
                    ok += 1
                else:
                    print(f"  FAILED or timed out: "
                          f"{result.final_message if result else 'no result'}")
                    failed += 1

                # The throttle: a deliberate gap between utterances so cues
                # (and any queueing) are easy to watch rather than blasted
                # back-to-back. Set --pause 0 to stress-test queueing instead.
                if args.pause > 0:
                    time.sleep(args.pause)
    except KeyboardInterrupt:
        print("\nInterrupted; canceling in-flight goal.")
        client.cancel_goal()

    print(f"\nDone: {ok} spoken, {failed} failed, {time.time() - run_t0:.1f}s total.")


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
    ap.add_argument("--mutate", action="store_true",
                     help="add entropy to each phrase before use: jostle word order, "
                          "sprinkle 1-2 random dictionary words, misspell a word, randomize "
                          "casing, and move the emoji to a random position -- so playback isn't "
                          "just verbatim training-set recall")

    speak_group = ap.add_argument_group("--speak mode (real Speak action, full pipeline)")
    speak_group.add_argument("--speak", action="store_true",
                             help="say each phrase via the real Speak action instead of "
                                  "generating+saving directly; watch synced playback live")
    speak_group.add_argument("--engine", choices=ENGINES, default="kokoro",
                             help="TTS engine to speak with (default kokoro)")
    speak_group.add_argument("--pause", type=float, default=1.5,
                             help="throttle: seconds to wait after each utterance finishes "
                                  "before sending the next (default 1.5; 0 = back-to-back)")
    speak_group.add_argument("--face-policy", default="",
                             help="override the animator's face policy cascade for this run, "
                                  "e.g. 'lut' or 'generate,saved,lut' (default: system default)")
    speak_group.add_argument("--wait-timeout", type=float, default=60.0,
                             help="max seconds to wait for each utterance to finish speaking")
    speak_group.add_argument("--sync", action=argparse.BooleanOptionalAction, default=True,
                             help="opt cues into the bounded first-frame wait "
                                  "(performance.sync; see TTP_V2.md) so fast engines "
                                  "(piper/espeak/festival) actually get to show off live "
                                  "generation instead of always falling back to the LUT. "
                                  "Default on -- this script is exactly the "
                                  "timing/aesthetics evaluation case sync mode is for. "
                                  "Use --no-sync to compare against the old zero-wait behavior.")
    args = ap.parse_args()

    items = load_items(args)
    total_phrases = sum(min(args.phrases_per_emoji, len(ph)) for _, _, ph in items)

    if args.speak:
        print(f"{len(items)} emoji file(s) matched, {total_phrases} phrase(s) queued "
              f"(engine={args.engine}, pause={args.pause}s, "
              f"face_policy={args.face_policy or 'default'}, sync={args.sync}).")
        if args.dry_run:
            for path, emoji, phrases in items:
                for phrase in pick_phrases(phrases, args.phrases_per_emoji):
                    text = mutate_phrase(phrase, emoji) if args.mutate else f"{phrase} {emoji}".strip()
                    print(f"  [{args.engine}] \"{text}\"  ({path.name})")
            return
        run_speak_mode(args, items)
        return

    print(f"{len(items)} emoji file(s) matched, {total_phrases} phrase(s) queued "
          f"(model={args.model}, temp={args.temperature}, "
          f"seed={'fresh' if args.seed <= 0 else args.seed}, "
          f"{'blocking' if args.no_stream else 'streaming'}).")

    if args.dry_run:
        for path, emoji, phrases in items:
            chosen = pick_phrases(phrases, args.phrases_per_emoji)
            if args.mutate:
                chosen = [mutate_phrase(p, emoji) for p in chosen]
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
            prompt_text = mutate_phrase(phrase, emoji) if args.mutate else phrase
            print(f"\n=== {emoji}  \"{prompt_text}\"  ({path.name}) ===")
            t0 = time.time()
            try:
                if args.no_stream:
                    obj = client.generate(prompt_text, temperature=args.temperature, seed=seed,
                                          verbose=not args.quiet)
                else:
                    obj = None
                    for kind, payload in client.generate_stream(
                            prompt_text, temperature=args.temperature, seed=seed,
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
