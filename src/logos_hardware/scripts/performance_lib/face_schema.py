"""
Shared schema utilities for Logos face animation generation.

The LLM-facing schema is a sparse, semantic keyframe format. The robot-facing
schema is the existing ROS-ish list-of-state-objects format used by the current
face animation runtime.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Dict, List, Tuple


EYE_SIDES = frozenset({"left", "right", "both"})
CONCRETE_EYE_SIDES = ("left", "right")

EYE_KEYS = (
    "gaze_x",
    "gaze_y",
    "scale_x",
    "scale_y",
    "lid_height",
    "lid_angle",
    "color",
)

MOUTH_KEYS = (
    "frequency",
    "amplitude",
    "phase",
    "phase_increment",
    "color",
)

EYE_STATE_BY_KEY = {
    "gaze_x": "EyeGazeX",
    "gaze_y": "EyeGazeY",
    "scale_x": "EyeScaleX",
    "scale_y": "EyeScaleY",
    "lid_height": "EyeLidHeight",
    "lid_angle": "EyeLidAngle",
    "color": "EyeColor",
}

EYE_KEY_BY_STATE = {state: key for key, state in EYE_STATE_BY_KEY.items()}

VALID_STATES = frozenset(
    {
        *EYE_KEY_BY_STATE.keys(),
        "MouthSine",
    }
)

NUMERIC_RANGES = {
    "gaze_x": (-1.0, 1.0),
    "gaze_y": (-1.0, 1.0),
    "scale_x": (0.0, 1.0),
    "scale_y": (0.0, 1.0),
    "lid_height": (-1.0, 1.0),
    "lid_angle": (-45.0, 45.0),
    "frequency": (0.01, 20.0),
    "amplitude": (0.0, 1.0),
    "phase": (-3.15, 3.15),
    "phase_increment": (-3.14, 3.14),
}

HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

DEFAULT_EYE = {
    "gaze_x": 0.0,
    "gaze_y": 0.0,
    "scale_x": 0.5,
    "scale_y": 0.5,
    "lid_height": 0.5,
    "lid_angle": 0.0,
    "color": "#FFFFFF",
}

DEFAULT_MOUTH = {
    "frequency": 0.5,
    "amplitude": 0.3,
    "phase": 0.0,
    "phase_increment": 0.0,
    "color": "#FFFFFF",
}

DEFAULT_POSE = {
    "eyes": {
        "left": copy.deepcopy(DEFAULT_EYE),
        "right": copy.deepcopy(DEFAULT_EYE),
    },
    "mouth": copy.deepcopy(DEFAULT_MOUTH),
}

MIN_FRAMES = 4
MAX_FRAMES = 9


class AnimationSchemaError(ValueError):
    """Raised when conversion is requested for an invalid animation."""


def normalize_semantic_sequence(
    data: Any,
    *,
    emoji: str | None = None,
    name: str | None = None,
) -> Dict[str, Any]:
    """
    Normalize a model response into one semantic animation object.

    The desired top level is an object. For tolerance while iterating prompts,
    this also accepts list[1] and unwraps it.
    """
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        obj = copy.deepcopy(data[0])
    elif isinstance(data, dict):
        obj = copy.deepcopy(data)
    else:
        got = type(data).__name__
        raise AnimationSchemaError(f"top level must be object or list[1], got {got}")

    if "reasoning" in obj and "ideation" not in obj:
        obj["ideation"] = obj.pop("reasoning")

    if "design_note" in obj and "ideation" not in obj:
        obj["ideation"] = obj.pop("design_note")

    if emoji is not None:
        obj["emoji"] = emoji

    if name is not None:
        obj["name"] = name

    return obj


def validate_semantic_sequence(
    data: Dict[str, Any],
    *,
    expected_emoji: str | None = None,
    strict_keys: bool = True,
) -> List[str]:
    """Return a list of semantic-schema errors. Empty list means valid."""
    errors: List[str] = []

    if not isinstance(data, dict):
        return [f"top level must be object, got {type(data).__name__}"]

    allowed_top_keys = {"emoji", "name", "ideation", "frames"}
    required_top_keys = {"emoji", "ideation", "frames"}

    if strict_keys:
        _check_unknown_keys(data, allowed_top_keys, "top level", errors)

    for key in required_top_keys:
        if key not in data:
            errors.append(f"missing key '{key}'")

    emoji = data.get("emoji")
    if not isinstance(emoji, str) or not emoji:
        errors.append("'emoji' must be a non-empty string")
    elif expected_emoji is not None and emoji != expected_emoji:
        errors.append(f"emoji mismatch: expected {expected_emoji!r}, got {emoji!r}")

    if "name" in data and not isinstance(data["name"], str):
        errors.append("'name' must be a string when present")

    ideation = data.get("ideation")
    if not isinstance(ideation, str) or not ideation.strip():
        errors.append("'ideation' must be a non-empty string")

    frames = data.get("frames")
    if not isinstance(frames, list):
        errors.append(f"'frames' must be a list, got {type(frames).__name__}")
        return errors

    if len(frames) < MIN_FRAMES:
        errors.append(f"need at least {MIN_FRAMES} frames, got {len(frames)}")

    if len(frames) > MAX_FRAMES:
        errors.append(f"use at most {MAX_FRAMES} frames, got {len(frames)}")

    for frame_index, frame in enumerate(frames):
        _validate_semantic_frame(frame, frame_index, errors, strict_keys=strict_keys)

    if frames and isinstance(frames[0], dict):
        _validate_first_frame_completeness(frames[0], errors)

    return errors


def expand_semantic_sequence(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Expand sparse semantic keyframes into full concrete left/right eye poses.

    Omitted values inherit from the previous expanded frame. The first frame is
    expected to be complete by validation, but defaults still make the operation
    deterministic even while debugging imperfect files.
    """
    errors = validate_semantic_sequence(data)
    if errors:
        raise AnimationSchemaError("semantic validation failed: " + "; ".join(errors))

    current = copy.deepcopy(DEFAULT_POSE)
    expanded_frames: List[Dict[str, Any]] = []

    for frame in data["frames"]:
        eyes_patch = frame.get("eyes", {})
        mouth_patch = frame.get("mouth", {})

        if "both" in eyes_patch:
            for side in CONCRETE_EYE_SIDES:
                current["eyes"][side].update(eyes_patch["both"])

        for side in CONCRETE_EYE_SIDES:
            if side in eyes_patch:
                current["eyes"][side].update(eyes_patch[side])

        if mouth_patch:
            current["mouth"].update(mouth_patch)

        expanded_frames.append(
            {
                "beat": frame["beat"],
                "eyes": copy.deepcopy(current["eyes"]),
                "mouth": copy.deepcopy(current["mouth"]),
            }
        )

    return expanded_frames


def compile_semantic_to_legacy(
    data: Dict[str, Any],
    *,
    include_metadata: bool = False,
) -> List[Dict[str, Any]]:
    """Compile one semantic animation object to the existing legacy format."""
    expanded_frames = expand_semantic_sequence(data)
    legacy_frames: List[List[Dict[str, Any]]] = []

    for frame in expanded_frames:
        legacy_frame: List[Dict[str, Any]] = []
        left_eye = frame["eyes"]["left"]
        right_eye = frame["eyes"]["right"]

        for key in EYE_KEYS:
            state = EYE_STATE_BY_KEY[key]
            left_value = left_eye[key]
            right_value = right_eye[key]

            if _values_equal(left_value, right_value):
                legacy_frame.append(
                    {
                        "state": state,
                        "parameters": {
                            "eye_side": "both",
                            key: left_value,
                        },
                    }
                )
            else:
                legacy_frame.extend(
                    [
                        {
                            "state": state,
                            "parameters": {
                                "eye_side": "left",
                                key: left_value,
                            },
                        },
                        {
                            "state": state,
                            "parameters": {
                                "eye_side": "right",
                                key: right_value,
                            },
                        },
                    ]
                )

        legacy_frame.append(
            {
                "state": "MouthSine",
                "parameters": copy.deepcopy(frame["mouth"]),
            }
        )
        legacy_frames.append(legacy_frame)

    obj = {
        "emoji": data["emoji"],
        "ideation": data["ideation"],
        "frames": legacy_frames,
    }

    if include_metadata:
        obj["schema"] = "logos-face-legacy-v1"
        if "name" in data:
            obj["name"] = data["name"]
        obj["semantic_beats"] = [frame["beat"] for frame in expanded_frames]

    return [obj]


def validate_legacy_sequence(
    data: Any,
    *,
    expected_emoji: str | None = None,
    strict_keys: bool = True,
) -> List[str]:
    """Return a list of legacy-schema errors. Empty list means valid."""
    errors: List[str] = []

    if not isinstance(data, list) or len(data) != 1:
        got_len = len(data) if isinstance(data, list) else "?"
        return [f"top level must be list[1], got {type(data).__name__}[{got_len}]"]

    obj = data[0]
    if not isinstance(obj, dict):
        return [f"top-level item must be object, got {type(obj).__name__}"]

    allowed_top_keys = {
        "emoji",
        "ideation",
        "frames",
        "schema",
        "name",
        "semantic_beats",
    }
    required_top_keys = {"emoji", "ideation", "frames"}

    if strict_keys:
        _check_unknown_keys(obj, allowed_top_keys, "top level", errors)

    for key in required_top_keys:
        if key not in obj:
            errors.append(f"missing key '{key}'")

    emoji = obj.get("emoji")
    if not isinstance(emoji, str) or not emoji:
        errors.append("'emoji' must be a non-empty string")
    elif expected_emoji is not None and emoji != expected_emoji:
        errors.append(f"emoji mismatch: expected {expected_emoji!r}, got {emoji!r}")

    ideation = obj.get("ideation")
    if not isinstance(ideation, str) or not ideation.strip():
        errors.append("'ideation' must be a non-empty string")

    frames = obj.get("frames")
    if not isinstance(frames, list):
        errors.append(f"'frames' must be a list, got {type(frames).__name__}")
        return errors

    if len(frames) < MIN_FRAMES:
        errors.append(f"need at least {MIN_FRAMES} frames, got {len(frames)}")

    if len(frames) > MAX_FRAMES:
        errors.append(f"use at most {MAX_FRAMES} frames, got {len(frames)}")

    for frame_index, frame in enumerate(frames):
        _validate_legacy_frame(frame, frame_index, errors, strict_keys=strict_keys)

    return errors


def _validate_semantic_frame(
    frame: Any,
    frame_index: int,
    errors: List[str],
    *,
    strict_keys: bool,
) -> None:
    if not isinstance(frame, dict):
        errors.append(f"frame {frame_index}: must be object, got {type(frame).__name__}")
        return

    allowed_frame_keys = {"beat", "eyes", "mouth"}
    if strict_keys:
        _check_unknown_keys(frame, allowed_frame_keys, f"frame {frame_index}", errors)

    beat = frame.get("beat")
    if not isinstance(beat, str) or not beat.strip():
        errors.append(f"frame {frame_index}: missing non-empty 'beat'")

    has_patch = False

    if "eyes" in frame:
        has_patch = True
        _validate_eyes_patch(frame["eyes"], frame_index, errors, strict_keys=strict_keys)

    if "mouth" in frame:
        has_patch = True
        _validate_mouth_patch(frame["mouth"], frame_index, errors, strict_keys=strict_keys)

    if not has_patch:
        errors.append(f"frame {frame_index}: empty keyframe patch")


def _validate_eyes_patch(
    eyes: Any,
    frame_index: int,
    errors: List[str],
    *,
    strict_keys: bool,
) -> None:
    if not isinstance(eyes, dict) or not eyes:
        errors.append(f"frame {frame_index}: 'eyes' must be a non-empty object")
        return

    for side, patch in eyes.items():
        if side not in EYE_SIDES:
            errors.append(f"frame {frame_index}: invalid eye side {side!r}")
            continue

        if not isinstance(patch, dict) or not patch:
            errors.append(f"frame {frame_index} eyes.{side}: must be a non-empty object")
            continue

        if strict_keys:
            _check_unknown_keys(
                patch,
                set(EYE_KEYS),
                f"frame {frame_index} eyes.{side}",
                errors,
            )

        for key, value in patch.items():
            if key not in EYE_KEYS:
                if not strict_keys:
                    errors.append(f"frame {frame_index} eyes.{side}: unknown key {key!r}")
                continue
            _validate_parameter_value(
                key,
                value,
                f"frame {frame_index} eyes.{side}.{key}",
                errors,
            )


def _validate_mouth_patch(
    mouth: Any,
    frame_index: int,
    errors: List[str],
    *,
    strict_keys: bool,
) -> None:
    if not isinstance(mouth, dict) or not mouth:
        errors.append(f"frame {frame_index}: 'mouth' must be a non-empty object")
        return

    if strict_keys:
        _check_unknown_keys(mouth, set(MOUTH_KEYS), f"frame {frame_index} mouth", errors)

    for key, value in mouth.items():
        if key not in MOUTH_KEYS:
            if not strict_keys:
                errors.append(f"frame {frame_index} mouth: unknown key {key!r}")
            continue
        _validate_parameter_value(key, value, f"frame {frame_index} mouth.{key}", errors)


def _validate_first_frame_completeness(frame: Dict[str, Any], errors: List[str]) -> None:
    eyes = frame.get("eyes")
    mouth = frame.get("mouth")

    if not isinstance(mouth, dict):
        errors.append("frame 0: must define a complete mouth pose")
    else:
        missing_mouth = sorted(set(MOUTH_KEYS) - set(mouth.keys()))
        if missing_mouth:
            errors.append(f"frame 0: mouth missing {missing_mouth}")

    if not isinstance(eyes, dict):
        errors.append("frame 0: must define a complete eye pose")
        return

    effective_keys = {
        "left": set(),
        "right": set(),
    }

    both_patch = eyes.get("both")
    if isinstance(both_patch, dict):
        for side in CONCRETE_EYE_SIDES:
            effective_keys[side].update(both_patch.keys())

    for side in CONCRETE_EYE_SIDES:
        side_patch = eyes.get(side)
        if isinstance(side_patch, dict):
            effective_keys[side].update(side_patch.keys())

    for side in CONCRETE_EYE_SIDES:
        missing_eye = sorted(set(EYE_KEYS) - effective_keys[side])
        if missing_eye:
            errors.append(f"frame 0: {side} eye missing {missing_eye}")


def _validate_legacy_frame(
    frame: Any,
    frame_index: int,
    errors: List[str],
    *,
    strict_keys: bool,
) -> None:
    if not isinstance(frame, list) or not frame:
        errors.append(f"frame {frame_index}: must be a non-empty list")
        return

    for item_index, item in enumerate(frame):
        location = f"frame {frame_index} item {item_index}"

        if not isinstance(item, dict):
            errors.append(f"{location}: must be object, got {type(item).__name__}")
            continue

        if strict_keys:
            _check_unknown_keys(item, {"state", "parameters"}, location, errors)

        state = item.get("state")
        params = item.get("parameters")

        if state not in VALID_STATES:
            errors.append(f"{location}: unknown state {state!r}")
            continue

        if not isinstance(params, dict):
            errors.append(f"{location}: parameters must be object")
            continue

        if state == "MouthSine":
            _validate_legacy_mouth(params, location, errors, strict_keys=strict_keys)
        else:
            _validate_legacy_eye(state, params, location, errors, strict_keys=strict_keys)


def _validate_legacy_eye(
    state: str,
    params: Dict[str, Any],
    location: str,
    errors: List[str],
    *,
    strict_keys: bool,
) -> None:
    expected_key = EYE_KEY_BY_STATE[state]
    allowed_keys = {"eye_side", expected_key}

    if strict_keys:
        _check_unknown_keys(params, allowed_keys, location, errors)

    eye_side = params.get("eye_side")
    if eye_side not in EYE_SIDES:
        errors.append(f"{location}: invalid or missing eye_side {eye_side!r}")

    if expected_key not in params:
        errors.append(f"{location}: missing parameter {expected_key!r}")
        return

    _validate_parameter_value(
        expected_key,
        params[expected_key],
        f"{location}.{expected_key}",
        errors,
    )


def _validate_legacy_mouth(
    params: Dict[str, Any],
    location: str,
    errors: List[str],
    *,
    strict_keys: bool,
) -> None:
    if strict_keys:
        _check_unknown_keys(params, set(MOUTH_KEYS), location, errors)

    for key in MOUTH_KEYS:
        if key not in params:
            errors.append(f"{location}: missing mouth parameter {key!r}")
            continue
        _validate_parameter_value(key, params[key], f"{location}.{key}", errors)


def _validate_parameter_value(
    key: str,
    value: Any,
    location: str,
    errors: List[str],
) -> None:
    if key == "color":
        if not isinstance(value, str) or not HEX_COLOR_RE.match(value):
            errors.append(f"{location}: color must be #RRGGBB, got {value!r}")
        return

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{location}: must be numeric, got {type(value).__name__}")
        return

    if not math.isfinite(float(value)):
        errors.append(f"{location}: must be finite, got {value!r}")
        return

    low, high = NUMERIC_RANGES[key]
    if not low <= float(value) <= high:
        errors.append(f"{location}: {value} out of range [{low}, {high}]")


def _check_unknown_keys(
    obj: Dict[str, Any],
    allowed_keys: set[str],
    location: str,
    errors: List[str],
) -> None:
    unknown = sorted(set(obj.keys()) - allowed_keys)
    if unknown:
        errors.append(f"{location}: unknown keys {unknown}")


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) < 1e-9
    return left == right
