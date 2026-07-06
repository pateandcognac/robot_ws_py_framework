"""
Animation LUT loaders for the Logos performance pipeline.

The face LUT is the semantic-format dataset in animations/face_semantic/
(one JSON object per file: {emoji, name, ideation, frames}). The legacy
animations/face/ directory is a compiled training artifact and is no longer
loaded at runtime.

The arm LUT is still the legacy list-of-state-objects format in
animations/arms/ until the arm semantic sidequest lands.
"""

import glob
import json
import os
from typing import Any, Dict, List

DEFAULT_FACE_SEMANTIC_DIR = "/home/robot/robot_ws/animations/face_semantic"
DEFAULT_ARM_DIR = "/home/robot/robot_ws/animations/arms"


def load_semantic_face_lut(dirpath: str = DEFAULT_FACE_SEMANTIC_DIR) -> Dict[str, Dict[str, Any]]:
    """Load {emoji: semantic animation object} from a directory of JSON files."""
    lut: Dict[str, Dict[str, Any]] = {}
    for path in glob.glob(os.path.join(dirpath, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("emoji") and entry.get("frames"):
                lut[entry["emoji"]] = entry
    return lut


def load_arm_lut(dirpath: str = DEFAULT_ARM_DIR) -> Dict[str, List[Any]]:
    """Load {emoji: legacy arm frames} from animations/arms/emoji_arm_seq_*.json."""
    lut: Dict[str, List[Any]] = {}
    for path in glob.glob(os.path.join(dirpath, "emoji_arm_seq_*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("emoji") and entry.get("frames"):
                lut[entry["emoji"]] = entry["frames"]
    return lut
