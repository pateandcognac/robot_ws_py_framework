#!/home/robot/robot_ws/.venv/bin/python3.11
"""
Shared schema utilities for Logos arm animation sequences.

Mirrors the face pipeline: the LLM/training-facing schema is a semantic
keyframe format aligned with animations/face_semantic/, and the robot-facing
schema is the legacy list-of-state-objects format used by the current arm
playback runtime.

Semantic arm format (animations/arms_semantic/):
{
  "emoji": "🥇",
  "ideation": "why this motion fits the emoji ...",
  "frames": [
    {"beat": "arms rest at the sides",
     "arms": {"left":  {"joint1": 10.0, "joint2": -85.0, "wrist": 0.0},
              "right": {"joint1": 10.0, "joint2": -85.0, "wrist": 0.0}}},
    ...
  ]
}
"arms" patches may use "left"/"right"/"both" and omit sides or keys; omitted
values carry forward from the previous frame (like the face format).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

ARM_SIDES = frozenset({"left", "right", "both"})
CONCRETE_ARM_SIDES = ("left", "right")
ARM_KEYS = ("joint1", "joint2", "wrist")
ARM_RANGE = (-90.0, 90.0)

# Arms hanging relaxed at the sides.
DEFAULT_ARM = {"joint1": 10.0, "joint2": -85.0, "wrist": 0.0}
DEFAULT_ARMS_POSE = {
    "left": dict(DEFAULT_ARM),
    "right": dict(DEFAULT_ARM),
}

MIN_FRAMES = 2
MAX_FRAMES = 12


class ArmSchemaError(ValueError):
    pass


def legacy_to_semantic(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert one legacy arm sequence ({emoji, reasoning, frames:[[state objs]]})
    into the semantic format. Beats are left empty ("") for annotation.
    """
    frames_out: List[Dict[str, Any]] = []
    for keyframe in entry.get("frames", []):
        arms: Dict[str, Dict[str, float]] = {}
        for action in keyframe:
            if action.get("state") != "ArmPose":
                continue
            params = action.get("parameters", {})
            side = params.get("side", "both")
            if side not in ARM_SIDES:
                continue
            pose = {k: float(params[k]) for k in ARM_KEYS if k in params}
            arms[side] = pose
        frames_out.append({"beat": "", "arms": arms})
    return {
        "emoji": entry.get("emoji", ""),
        "ideation": entry.get("reasoning") or entry.get("ideation") or "",
        "frames": frames_out,
    }


def expand_semantic_arm_frames(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expand sparse semantic arm frames to full left/right poses (lenient)."""
    current = copy.deepcopy(DEFAULT_ARMS_POSE)
    expanded: List[Dict[str, Any]] = []
    for frame in frames:
        arms_patch = frame.get("arms", {}) or {}
        if isinstance(arms_patch.get("both"), dict):
            for side in CONCRETE_ARM_SIDES:
                current[side].update(_clamped(arms_patch["both"]))
        for side in CONCRETE_ARM_SIDES:
            if isinstance(arms_patch.get(side), dict):
                current[side].update(_clamped(arms_patch[side]))
        expanded.append({
            "beat": frame.get("beat", ""),
            "arms": copy.deepcopy(current),
        })
    return expanded


def compile_semantic_to_legacy(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compile one semantic arm object to the legacy runtime format."""
    expanded = expand_semantic_arm_frames(data.get("frames", []))
    legacy_frames: List[List[Dict[str, Any]]] = []
    for frame in expanded:
        left, right = frame["arms"]["left"], frame["arms"]["right"]
        keyframe: List[Dict[str, Any]] = []
        if left == right:
            keyframe.append(_arm_state("both", left))
        else:
            keyframe.append(_arm_state("left", left))
            keyframe.append(_arm_state("right", right))
        legacy_frames.append(keyframe)
    return [{
        "emoji": data.get("emoji", ""),
        "reasoning": data.get("ideation", ""),
        "frames": legacy_frames,
    }]


def validate_semantic_arm_sequence(data: Any) -> List[str]:
    """Return a list of schema errors; empty means valid."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["top level must be an object"]
    if not isinstance(data.get("emoji"), str) or not data.get("emoji"):
        errors.append("'emoji' must be a non-empty string")
    if not isinstance(data.get("ideation"), str):
        errors.append("'ideation' must be a string")
    frames = data.get("frames")
    if not isinstance(frames, list):
        return errors + ["'frames' must be a list"]
    if not (MIN_FRAMES <= len(frames) <= MAX_FRAMES):
        errors.append("need {}..{} frames, got {}".format(MIN_FRAMES, MAX_FRAMES, len(frames)))
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict):
            errors.append("frame {} must be an object".format(i))
            continue
        if not isinstance(frame.get("beat"), str):
            errors.append("frame {} 'beat' must be a string".format(i))
        arms = frame.get("arms", {})
        if not isinstance(arms, dict):
            errors.append("frame {} 'arms' must be an object".format(i))
            continue
        for side, pose in arms.items():
            if side not in ARM_SIDES:
                errors.append("frame {}: unknown side '{}'".format(i, side))
                continue
            if not isinstance(pose, dict):
                errors.append("frame {} side {}: pose must be an object".format(i, side))
                continue
            for key, value in pose.items():
                if key not in ARM_KEYS:
                    errors.append("frame {} {}: unknown key '{}'".format(i, side, key))
                elif not isinstance(value, (int, float)):
                    errors.append("frame {} {}.{}: not a number".format(i, side, key))
                elif not (ARM_RANGE[0] <= value <= ARM_RANGE[1]):
                    errors.append("frame {} {}.{}={} out of range".format(i, side, key, value))
    return errors


def _clamped(pose: Dict[str, Any]) -> Dict[str, float]:
    out = {}
    for key in ARM_KEYS:
        if key in pose and isinstance(pose[key], (int, float)):
            out[key] = max(ARM_RANGE[0], min(ARM_RANGE[1], float(pose[key])))
    return out


def _arm_state(side: str, pose: Dict[str, float]) -> Dict[str, Any]:
    params = {"side": side}
    params.update(pose)
    return {"state": "ArmPose", "parameters": params}
