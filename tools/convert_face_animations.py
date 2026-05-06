#!/home/robot/robot_ws/.venv/bin/python3.11
"""
Convert sparse semantic Logos face animation JSON into the legacy runtime schema.

Usage:
    python3 tools/convert_face_animation.py animations/face_semantic/*.json
    python3 tools/convert_face_animation.py --all
    python3 tools/convert_face_animation.py --all --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

from face_animation_schema import (
    compile_semantic_to_legacy,
    normalize_semantic_sequence,
    validate_legacy_sequence,
    validate_semantic_sequence,
)


WORKSPACE = Path(__file__).parent.parent
SEMANTIC_ANIMATIONS = WORKSPACE / "animations" / "face_semantic"
LEGACY_ANIMATIONS = WORKSPACE / "animations" / "face"


def load_json(path: Path):
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def convert_file(
    source_path: Path,
    output_dir: Path,
    *,
    dry_run: bool = False,
    include_metadata: bool = False,
) -> List[str]:
    """Convert one file. Return errors; empty list means success."""
    try:
        raw_data = load_json(source_path)
        semantic = normalize_semantic_sequence(raw_data)
    except Exception as exc:
        return [f"read/normalize error: {exc}"]

    semantic_errors = validate_semantic_sequence(semantic)
    if semantic_errors:
        return [f"semantic: {error}" for error in semantic_errors]

    try:
        legacy = compile_semantic_to_legacy(
            semantic,
            include_metadata=include_metadata,
        )
    except Exception as exc:
        return [f"compile error: {exc}"]

    legacy_errors = validate_legacy_sequence(legacy)
    if legacy_errors:
        return [f"compiled legacy: {error}" for error in legacy_errors]

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / source_path.name
        output_path.write_text(
            json.dumps(legacy, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Logos face animation JSON.")
    parser.add_argument("files", nargs="*", help="Semantic JSON files to convert")
    parser.add_argument("--all", action="store_true", help=f"Convert all files in {SEMANTIC_ANIMATIONS}")
    parser.add_argument("--output-dir", type=Path, default=LEGACY_ANIMATIONS)
    parser.add_argument("--dry-run", action="store_true", help="Validate and compile without writing files")
    parser.add_argument(
        "--include-metadata",
        action="store_true",
        help="Include name/schema/beat metadata in compiled legacy files",
    )
    args = parser.parse_args()

    if args.all:
        paths = sorted(SEMANTIC_ANIMATIONS.glob("emoji_face_seq_*.json"))
    else:
        paths = [Path(path) for path in args.files]

    if not paths:
        parser.print_help()
        sys.exit(1)

    ok_count = 0
    bad_count = 0

    for path in paths:
        errors = convert_file(
            path,
            args.output_dir,
            dry_run=args.dry_run,
            include_metadata=args.include_metadata,
        )
        if errors:
            bad_count += 1
            print(f"FAIL  {path}")
            for error in errors:
                print(f"      {error}")
        else:
            ok_count += 1
            action = "would convert" if args.dry_run else "converted"
            print(f"OK    {path.name}  ({action})")

    print(f"\n{ok_count} converted, {bad_count} failed  (of {len(paths)} files)")
    sys.exit(0 if bad_count == 0 else 1)


if __name__ == "__main__":
    main()
