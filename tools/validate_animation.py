#!/home/robot/robot_ws/.venv/bin/python3.11
"""
Validate Logos face animation JSON files.

Supports both schemas:
    semantic: sparse LLM-facing object with frames[].beat/eyes/mouth
    legacy:   existing runtime list[1] with frames as state-object arrays

Usage:
    python3 tools/validate_animation.py <file> [file ...]
    python3 tools/validate_animation.py animations/face/*.json
    python3 tools/validate_animation.py --all
    python3 tools/validate_animation.py --semantic-all
    python3 tools/validate_animation.py --all --summary
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Tuple

from face_animation_schema import (
    normalize_semantic_sequence,
    validate_legacy_sequence,
    validate_semantic_sequence,
)


WORKSPACE = Path(__file__).parent.parent
LEGACY_ANIMATIONS = WORKSPACE / "animations" / "face"
SEMANTIC_ANIMATIONS = WORKSPACE / "animations" / "face_semantic"


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def detect_schema(data: Any) -> str:
    if isinstance(data, list):
        return "legacy"
    if isinstance(data, dict):
        return "semantic"
    return "unknown"


def validate_file(path: Path, schema: str = "auto") -> Tuple[str, List[str]]:
    """Return (detected_schema, errors)."""
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        return schema, [f"JSON parse error: {exc}"]
    except Exception as exc:
        return schema, [f"Read error: {exc}"]

    detected = detect_schema(data) if schema == "auto" else schema

    if detected == "semantic":
        try:
            semantic = normalize_semantic_sequence(data)
        except Exception as exc:
            return detected, [f"normalize error: {exc}"]
        return detected, validate_semantic_sequence(semantic)

    if detected == "legacy":
        return detected, validate_legacy_sequence(data)

    return detected, [f"unknown schema for top-level {type(data).__name__}"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Logos face animation JSON files.")
    parser.add_argument("files", nargs="*", help="JSON files to validate")
    parser.add_argument("--all", action="store_true", help=f"Validate all legacy files in {LEGACY_ANIMATIONS}")
    parser.add_argument("--semantic-all", action="store_true", help=f"Validate all semantic files in {SEMANTIC_ANIMATIONS}")
    parser.add_argument("--summary", action="store_true", help="Only print counts, not per-file details")
    parser.add_argument(
        "--schema",
        choices=("auto", "legacy", "semantic"),
        default="auto",
        help="Force a schema instead of detecting from the top-level JSON shape",
    )
    args = parser.parse_args()

    paths: List[Path] = []
    if args.all:
        paths.extend(sorted(LEGACY_ANIMATIONS.glob("emoji_face_seq_*.json")))
    if args.semantic_all:
        paths.extend(sorted(SEMANTIC_ANIMATIONS.glob("emoji_face_seq_*.json")))
    if not args.all and not args.semantic_all:
        paths = [Path(file_name) for file_name in args.files]

    if not paths:
        parser.print_help()
        sys.exit(1)

    ok_count = 0
    bad_count = 0

    for path in paths:
        detected_schema, errors = validate_file(path, schema=args.schema)
        label = f"{path.name} [{detected_schema}]"

        if errors:
            bad_count += 1
            if not args.summary:
                print(f"FAIL  {label}")
                for error in errors:
                    print(f"      {error}")
        else:
            ok_count += 1
            if not args.summary and len(paths) <= 40:
                print(f"OK    {label}")

    print(f"\n{ok_count} valid, {bad_count} invalid  (of {len(paths)} files)")
    sys.exit(0 if bad_count == 0 else 1)


if __name__ == "__main__":
    main()
