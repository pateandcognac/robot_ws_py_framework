#!/home/robot/robot_ws/.venv/bin/python3.11
"""
Gemini-powered face animation generator for Logos robot.

Generates emoji-keyed semantic keyframe sequences, saves those semantic files
for curation, compiles them to the existing legacy runtime schema, and writes
legacy files into animations/face/.

Usage:
    python3 tools/generate_face_animations.py [options]

Options:
    --dry-run         Show what would be processed without calling API
    --limit N         Process only N emojis (useful for testing)
    --first-run       Move originals to backup, create manifest, empty output dirs
    --retry-failed    Process only emojis listed in failed_emojis.txt
    --image PATH      Image file to upload and include in system prompt (repeatable)
    --model NAME      Override default model
    --start-paid      Start with paid key instead of free key
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import shutil
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from google import genai
from google.genai import types

from face_animation_schema import (
    compile_semantic_to_legacy,
    normalize_semantic_sequence,
    validate_legacy_sequence,
    validate_semantic_sequence,
)


# --- Paths ---
WORKSPACE = Path(__file__).parent.parent
ANIMATIONS = WORKSPACE / "animations" / "face"
SEMANTIC_ANIMATIONS = WORKSPACE / "animations" / "face_semantic"
BACKUP = WORKSPACE / "animations_backup" / "face"
TOOLS = Path(__file__).parent
PROMPT_FILE = TOOLS / "face_animation_prompt.md"
LOG_FILE = TOOLS / "generation_log.jsonl"
MANIFEST_FILE = TOOLS / "emoji_generation_manifest.json"
FAILED_FILE = TOOLS / "failed_emojis.txt"

# --- API config ---
DEFAULT_MODEL = "gemini-3-flash-preview"
MAX_RETRIES = 5
BASE_BACKOFF = 2.0
MAX_BACKOFF = 60.0
RATE_LIMIT_SLEEP = 0.075
QUOTA_FAIL_THRESHOLD = 3

QUOTA_KEYWORDS = frozenset(
    {
        "quota",
        "429",
        "rate",
        "limit",
        "resource exhausted",
        "too many",
    }
)


# ---------------------------------------------------------------------------
# Emoji list / manifest
# ---------------------------------------------------------------------------


def load_emoji_map_from(directory: Path) -> Dict[str, str]:
    """Return {emoji_string: filename} from legacy animation files."""
    mapping: Dict[str, str] = {}

    for path in sorted(directory.glob("emoji_face_seq_*.json")):
        try:
            with open(path, encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, list) or not data:
                continue

            obj = data[0]
            if not isinstance(obj, dict):
                continue

            emoji = obj.get("emoji")
            if isinstance(emoji, str) and emoji:
                mapping[emoji] = path.name

        except Exception as exc:
            print(f"Warning: could not read {path}: {exc}")

    return mapping


def unicode_name(emoji: str) -> str:
    """Return readable Unicode names for the full emoji sequence."""
    names: List[str] = []
    ignored_names = {
        "VARIATION SELECTOR-16",
        "ZERO WIDTH JOINER",
    }

    for char in emoji:
        codepoint = f"U+{ord(char):04X}"

        try:
            name = unicodedata.name(char)
        except ValueError:
            name = "UNKNOWN"

        if name in ignored_names:
            continue

        names.append(f"{name} ({codepoint})")

    return " + ".join(names) if names else "unknown"


def write_manifest(emoji_map: Dict[str, str]) -> None:
    """Write the master emoji generation manifest."""
    entries = [
        {
            "emoji": emoji,
            "filename": filename,
            "unicode_name": unicode_name(emoji),
        }
        for emoji, filename in emoji_map.items()
    ]

    MANIFEST_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_manifest() -> List[Tuple[str, str]]:
    """Return [(emoji_string, filename)] from the manifest."""
    if not MANIFEST_FILE.exists():
        sys.exit(
            f"ERROR: no manifest found at {MANIFEST_FILE}\n"
            "Run once with --first-run to create it."
        )

    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: manifest is not valid JSON: {exc}")

    if not isinstance(data, list):
        sys.exit("ERROR: manifest must be a list.")

    targets: List[Tuple[str, str]] = []

    for index, item in enumerate(data):
        if not isinstance(item, dict):
            sys.exit(f"ERROR: manifest entry {index} is not an object.")

        emoji = item.get("emoji")
        filename = item.get("filename")

        if not isinstance(emoji, str) or not emoji:
            sys.exit(f"ERROR: manifest entry {index} has invalid emoji.")

        if not isinstance(filename, str) or not filename:
            sys.exit(f"ERROR: manifest entry {index} has invalid filename.")

        targets.append((emoji, filename))

    return targets


# ---------------------------------------------------------------------------
# API client / image upload
# ---------------------------------------------------------------------------


def make_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)


def upload_images(client: genai.Client, paths: List[str]) -> List[Tuple[str, str]]:
    """Upload images via Files API; return list of file URIs."""
    uris: List[str] = []

    for path in paths:
        mime_type = mimetypes.guess_type(path)[0] or "image/png"
        print(f"  Uploading {path} ({mime_type}) ...", flush=True)
        uploaded = client.files.upload(
            file=path,
            config=types.UploadFileConfig(display_name=Path(path).name),
        )
        print(f"  -> {uploaded.uri}")
        uris.append((uploaded.uri, mime_type))

    return uris


def build_system_parts(image_uris: List[Tuple[str, str]]):
    """Build system instruction content parts: prompt text + optional images."""
    text = PROMPT_FILE.read_text(encoding="utf-8")

    if not image_uris:
        return text

    parts = [types.Part.from_text(text=text)]
    for uri, mime_type in image_uris:
        parts.append(types.Part.from_uri(file_uri=uri, mime_type=mime_type))

    return parts


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def extract_json(text: str) -> str:
    """Strip markdown fences or accidental prose around a JSON object/array."""
    stripped = text.strip()

    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    if stripped.startswith("{") or stripped.startswith("["):
        return stripped

    starts = [index for index in (stripped.find("{"), stripped.find("[")) if index >= 0]
    if not starts:
        return stripped

    start = min(starts)
    end = max(stripped.rfind("}"), stripped.rfind("]"))
    if end <= start:
        return stripped[start:]

    return stripped[start : end + 1]


def extract_response_text(response) -> str:
    """Return only text parts from a Gemini response, ignoring metadata parts."""
    texts: List[str] = []

    for candidate in response.candidates or []:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content else None

        if not parts:
            continue

        for part in parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)

    return "".join(texts)


def call_api(
    client: genai.Client,
    model: str,
    system_parts,
    emoji: str,
    name: str,
    validation_hint: Optional[str] = None,
    thinking_budget: int = 0,
    include_thoughts: bool = False,
):
    """Single Gemini call. Returns (raw_response_text, usage_metadata)."""
    thinking_kwargs = {}

    if thinking_budget >= 0:
        thinking_kwargs["thinking_budget"] = thinking_budget

    if include_thoughts:
        thinking_kwargs["include_thoughts"] = True

    thinking_config = (
        types.ThinkingConfig(**thinking_kwargs)
        if thinking_kwargs
        else None
    )

    cfg = types.GenerateContentConfig(
        system_instruction=system_parts,
        response_mime_type="application/json",
        thinking_config=thinking_config,
        temperature=1.0,
    )

    prompt = f"Please craft your expressive, creative face animation sequence for: {emoji} ({name})\nHave fun, and thank you for your contribution!"

    if validation_hint:
        prompt += (
            "\n\nYour previous attempt failed validation. Fix the issue and "
            "return one complete valid JSON object only. Validation error:\n"
            f"{validation_hint}"
        )

    response = client.models.generate_content(
        model=model,
        config=cfg,
        contents=prompt,
    )

    return extract_response_text(response), response.usage_metadata

def is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(keyword in msg for keyword in QUOTA_KEYWORDS)


def parse_validate_compile(
    raw_text: str,
    *,
    emoji: str,
    name: str,
) -> Tuple[dict, list, str]:
    """
    Parse a raw Gemini response, validate semantic data, and compile to legacy.

    Returns (semantic_object, legacy_list, detail). Raises ValueError on failure.
    """
    try:
        raw_data = json.loads(extract_json(raw_text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse error: {exc}") from exc

    try:
        semantic = normalize_semantic_sequence(raw_data, emoji=emoji, name=name)
    except Exception as exc:
        raise ValueError(f"normalize error: {exc}") from exc

    semantic_errors = validate_semantic_sequence(semantic, expected_emoji=emoji)
    if semantic_errors:
        raise ValueError("semantic validation: " + "; ".join(semantic_errors[:8]))

    legacy = compile_semantic_to_legacy(semantic)
    legacy_errors = validate_legacy_sequence(legacy, expected_emoji=emoji)
    if legacy_errors:
        raise ValueError("compiled legacy validation: " + "; ".join(legacy_errors[:8]))

    detail = f"{len(semantic['frames'])} semantic frames compiled to legacy"
    return semantic, legacy, detail


# ---------------------------------------------------------------------------
# Progress / logging
# ---------------------------------------------------------------------------


def load_completed() -> set[str]:
    """Return emoji chars that already have status='ok' in the log."""
    done: set[str] = set()

    if not LOG_FILE.exists():
        return done

    with open(LOG_FILE, encoding="utf-8") as file:
        for line in file:
            try:
                entry = json.loads(line)
            except Exception:
                continue

            if entry.get("status") == "ok" and isinstance(entry.get("emoji"), str):
                done.add(entry["emoji"])

    return done


def is_target_completed(emoji: str, filename: str, completed: set[str]) -> bool:
    """Treat a target as complete only if logs and both output files agree."""
    if emoji not in completed:
        return False

    return (ANIMATIONS / filename).exists() and (SEMANTIC_ANIMATIONS / filename).exists()


def log(emoji: str, filename: str, status: str, detail: str) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "emoji": emoji,
        "filename": filename,
        "status": status,
        "detail": detail,
    }

    with open(LOG_FILE, "a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_failed(failures: List[str]) -> None:
    if failures:
        FAILED_FILE.write_text("\n".join(failures) + "\n", encoding="utf-8")
    elif FAILED_FILE.exists():
        FAILED_FILE.unlink()


# ---------------------------------------------------------------------------
# First-run initialization
# ---------------------------------------------------------------------------


def rotate_existing_state_files() -> None:
    """Move old progress files aside so a new first-run starts cleanly."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    for path in (LOG_FILE, FAILED_FILE):
        if not path.exists():
            continue

        rotated = path.with_name(f"{path.stem}_{stamp}{path.suffix}.old")
        path.rename(rotated)
        print(f"Moved old state file {path} -> {rotated}")


def initialize_first_run() -> None:
    """Create manifest, move originals to backup, and reset output dirs."""
    if MANIFEST_FILE.exists():
        sys.exit(f"ERROR: manifest already exists at {MANIFEST_FILE}")

    if BACKUP.exists():
        sys.exit(f"ERROR: backup already exists at {BACKUP}")

    if not ANIMATIONS.exists():
        sys.exit(f"ERROR: animation directory not found at {ANIMATIONS}")

    if SEMANTIC_ANIMATIONS.exists() and any(SEMANTIC_ANIMATIONS.iterdir()):
        sys.exit(f"ERROR: semantic output dir is not empty: {SEMANTIC_ANIMATIONS}")

    emoji_map = load_emoji_map_from(ANIMATIONS)

    if not emoji_map:
        sys.exit(f"ERROR: no emoji animation files found in {ANIMATIONS}")

    print(f"Found {len(emoji_map)} original emoji animation files.")
    print(f"Writing manifest to {MANIFEST_FILE} ...")
    write_manifest(emoji_map)

    rotate_existing_state_files()

    BACKUP.parent.mkdir(parents=True, exist_ok=True)

    print(f"Moving originals {ANIMATIONS} -> {BACKUP} ...")
    shutil.move(str(ANIMATIONS), str(BACKUP))

    print(f"Creating empty legacy output directory {ANIMATIONS} ...")
    ANIMATIONS.mkdir(parents=True, exist_ok=True)

    print(f"Creating empty semantic output directory {SEMANTIC_ANIMATIONS} ...")
    SEMANTIC_ANIMATIONS.mkdir(parents=True, exist_ok=True)

    print("First-run initialization complete.")
    print("Now run again without --first-run to start generation.")



# HELPERS

# Rough Gemini API paid-tier prices, USD per 1M tokens.
# Update these as Google changes preview pricing.
MODEL_PRICES = {
    "gemini-3.1-flash-lite-preview": {
        "input": 0.25,
        "output": 1.50,
        "cached_input": 0.025,
    },
    "gemini-3-flash-preview": {
        "input": 0.50,
        "output": 3.00,
        "cached_input": 0.05,
    },
    "gemini-2.5-flash": {
        "input": 0.30,
        "output": 2.50,
        "cached_input": 0.03,
    },
    "gemini-2.5-flash-lite": {
        "input": 0.10,
        "output": 0.40,
        "cached_input": 0.01,
    },
}


def get_usage_value(usage, name: str) -> int:
    """Safely read a token-count field from Gemini usage metadata."""
    if usage is None:
        return 0

    value = getattr(usage, name, 0)
    return int(value or 0)


def estimate_cost_usd(model: str, usage) -> float:
    """
    Estimate paid-tier API cost from usage metadata.

    Output price includes normal output tokens plus thinking tokens on Gemini pricing.
    Cached input is priced separately only if cached_content_token_count is reported.
    """
    prices = MODEL_PRICES.get(model)

    if not prices or usage is None:
        return 0.0

    prompt_tokens = get_usage_value(usage, "prompt_token_count")
    cached_tokens = get_usage_value(usage, "cached_content_token_count")
    output_tokens = get_usage_value(usage, "candidates_token_count")
    thoughts_tokens = get_usage_value(usage, "thoughts_token_count")

    billable_input_tokens = max(prompt_tokens - cached_tokens, 0)
    billable_output_tokens = output_tokens + thoughts_tokens

    input_cost = billable_input_tokens * prices["input"] / 1_000_000
    cached_cost = cached_tokens * prices["cached_input"] / 1_000_000
    output_cost = billable_output_tokens * prices["output"] / 1_000_000

    return input_cost + cached_cost + output_cost


def format_usage_line(model: str, usage, using_paid: bool) -> str:
    """Return a compact token/cost summary for one API call."""
    if usage is None:
        return "tokens: unavailable"

    prompt_tokens = get_usage_value(usage, "prompt_token_count")
    cached_tokens = get_usage_value(usage, "cached_content_token_count")
    output_tokens = get_usage_value(usage, "candidates_token_count")
    thoughts_tokens = get_usage_value(usage, "thoughts_token_count")
    total_tokens = get_usage_value(usage, "total_token_count")

    estimated_paid_cost = estimate_cost_usd(model, usage)

    parts = [
        f"in={prompt_tokens}",
        f"cached={cached_tokens}",
        f"out={output_tokens}",
        f"thinking={thoughts_tokens}",
        f"total={total_tokens}",
    ]

    if using_paid:
        parts.append(f"est=${estimated_paid_cost:.6f}")
    else:
        parts.append(f"free; saved≈${estimated_paid_cost:.6f}")

    return "tokens: " + ", ".join(parts)



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Logos face animations via Gemini.")
    parser.add_argument("--dry-run", action="store_true", help="No API calls; just show plan")
    parser.add_argument("--first-run", action="store_true", help="Initialize backup and manifest")
    parser.add_argument("--limit", type=int, default=None, metavar="N")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--image", action="append", default=[], metavar="PATH")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--start-paid", action="store_true", help="Begin with paid key")
    parser.add_argument("--thinking-budget", type=int, default=0, metavar="N", help="Thinking budget. 0 disables thinking when supported; -1 omits the setting and lets the model decide; >0 enables it.")
    parser.add_argument("--include-thoughts", action="store_true", help="Request thought parts when supported. Not recommended for JSON-only generation.")
    args = parser.parse_args()

    if args.first_run:
        initialize_first_run()
        return

    free_key = os.environ.get("FREE_GEMINI_API_KEY", "")
    paid_key = os.environ.get("PAID_GEMINI_API_KEY", "")

    if not free_key and not paid_key and not args.dry_run:
        sys.exit("ERROR: set FREE_GEMINI_API_KEY and/or PAID_GEMINI_API_KEY")

    using_paid = args.start_paid or not free_key
    active_key = (paid_key if using_paid else free_key) or paid_key or free_key

    manifest = load_manifest()
    print(f"Loaded {len(manifest)} emojis from {MANIFEST_FILE}")

    completed = load_completed()
    completed_targets = [
        (emoji, filename)
        for emoji, filename in manifest
        if is_target_completed(emoji, filename, completed)
    ]
    print(f"Already completed with files present: {len(completed_targets)}")

    if args.retry_failed:
        if not FAILED_FILE.exists():
            sys.exit(f"No {FAILED_FILE} found.")

        failed_set = {
            line.strip()
            for line in FAILED_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        targets = [(emoji, filename) for emoji, filename in manifest if emoji in failed_set]
        print(f"Retrying {len(targets)} failed emojis.")
    else:
        targets = [
            (emoji, filename)
            for emoji, filename in manifest
            if not is_target_completed(emoji, filename, completed)
        ]
        print(f"Remaining: {len(targets)}")

    if args.limit:
        targets = targets[: args.limit]
        print(f"Limiting to {len(targets)} (--limit {args.limit})")

    if args.dry_run:
        print("\nDRY RUN — first 10 targets:")
        for emoji, filename in targets[:10]:
            print(f"  {emoji}  {filename}")
        if len(targets) > 10:
            print(f"  ... and {len(targets) - 10} more")
        return

    if not targets:
        print("Nothing to do.")
        return

    ANIMATIONS.mkdir(parents=True, exist_ok=True)
    SEMANTIC_ANIMATIONS.mkdir(parents=True, exist_ok=True)

    client = make_client(active_key)
    image_uris: List[Tuple[str, str]] = []

    if args.image:
        print("Uploading reference images once...")
        image_uris = upload_images(client, args.image)

    system_parts = build_system_parts(image_uris)

    failures: List[str] = []
    consecutive_quota = 0

    print(f"\nStarting generation with {'paid' if using_paid else 'free'} key, model={args.model}")
    print("Press Ctrl-C to interrupt safely — progress is logged and resumable.\n")

    for index, (emoji, filename) in enumerate(targets):
        name = unicode_name(emoji)
        semantic_path = SEMANTIC_ANIMATIONS / filename
        legacy_path = ANIMATIONS / filename
        label = f"[{index + 1}/{len(targets)}] {emoji} {name[:42]}"
        print(f"{label}", end="  ", flush=True)

        success = False
        validation_hint: Optional[str] = None

        for attempt in range(MAX_RETRIES):
            try:
                raw, usage = call_api(
                    client,
                    args.model,
                    system_parts,
                    emoji,
                    name,
                    validation_hint=validation_hint,
                    thinking_budget=args.thinking_budget,
                    include_thoughts=args.include_thoughts,
                )
                semantic, legacy, detail = parse_validate_compile(raw, emoji=emoji, name=name)

                semantic_path.write_text(
                    json.dumps(semantic, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                legacy_path.write_text(
                    json.dumps(legacy, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                log(emoji, filename, "ok", detail)
                usage_line = format_usage_line(args.model, usage, using_paid)
                print(f"✓ {detail}  |  {usage_line}")
                consecutive_quota = 0
                success = True
                break

            except ValueError as exc:
                validation_hint = str(exc)[:600]
                print(
                    f"\n  ✗ validation (attempt {attempt + 1}): {validation_hint}",
                    end="  ",
                    flush=True,
                )

            except KeyboardInterrupt:
                print("\n\nInterrupted. Progress saved — re-run to resume.")
                write_failed(failures)
                sys.exit(0)

            except Exception as exc:
                error_text = str(exc)
                quota_hit = is_quota_error(exc)
                print(
                    f"\n  ✗ API error (attempt {attempt + 1}): {error_text[:120]}",
                    end="  ",
                    flush=True,
                )

                if quota_hit:
                    consecutive_quota += 1
                    if (
                        not using_paid
                        and paid_key
                        and consecutive_quota >= QUOTA_FAIL_THRESHOLD
                    ):
                        try:
                            print(f"\n\n  Free key quota exhausted after {consecutive_quota} errors.")
                            input("  Press Enter to switch to paid key, or Ctrl-C to abort: ")
                            client = make_client(paid_key)
                            using_paid = True
                            active_key = paid_key
                            consecutive_quota = 0
                            print("  Switched to paid key.\n")
                        except KeyboardInterrupt:
                            print("\nAborted.")
                            write_failed(failures)
                            sys.exit(0)

            delay = min(
                BASE_BACKOFF * (2**attempt) + random.uniform(0, 1.5),
                MAX_BACKOFF,
            )
            time.sleep(delay)

        if not success:
            log(emoji, filename, "fail", validation_hint or "max retries exceeded")
            failures.append(emoji)
            print("FAILED")

        if index < len(targets) - 1:
            time.sleep(RATE_LIMIT_SLEEP)

    write_failed(failures)
    ok_count = len(targets) - len(failures)
    print(f"\n{'=' * 50}")
    print(f"Done: {ok_count}/{len(targets)} generated successfully.")

    if failures:
        print(f"{len(failures)} failures written to {FAILED_FILE}")
        print("Re-run with --retry-failed to attempt them again.")


if __name__ == "__main__":
    main()
