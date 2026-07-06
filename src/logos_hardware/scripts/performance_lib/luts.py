"""
Animation LUT loaders for the Logos performance pipeline.

The face LUT is the semantic-format dataset in animations/face_semantic/
(one JSON object per file: {emoji, name, ideation, frames}). The legacy
animations/face/ directory is a compiled training artifact and is no longer
loaded at runtime.

The arm LUT is the semantic-format dataset in animations/arms_semantic/
(same shape, {emoji, ideation, frames}, now that the arm model has landed --
see TINY_ARM_DEPLOYMENT.md). The legacy animations/arms/ directory (the
list-of-state-objects ArmPose format) is the compiled training artifact,
kept for reference/tooling and for load_arm_lut() below, but no longer
loaded at runtime by the sequencer.
"""

import glob
import json
import os
from typing import Any, Dict, List

DEFAULT_FACE_SEMANTIC_DIR = "/home/robot/robot_ws/animations/face_semantic"
DEFAULT_ARM_SEMANTIC_DIR = "/home/robot/robot_ws/animations/arms_semantic"
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


def load_semantic_arm_lut(dirpath: str = DEFAULT_ARM_SEMANTIC_DIR) -> Dict[str, Dict[str, Any]]:
    """Load {emoji: semantic arm animation object} from animations/arms_semantic/."""
    return load_semantic_face_lut(dirpath)  # identical shape/loader logic


def load_arm_lut(dirpath: str = DEFAULT_ARM_DIR) -> Dict[str, List[Any]]:
    """
    Load {emoji: legacy arm frames} from animations/arms/emoji_arm_seq_*.json.
    Kept for tooling (e.g. annotate_arm_beats.py); the runtime sequencer uses
    load_semantic_arm_lut() instead.
    """
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
