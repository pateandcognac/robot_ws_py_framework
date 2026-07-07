"""
Client for the tiny fine-tuned arm-animation model served by local Ollama.

Text/emoji in (+ a soft frame-count request) -> semantic arm animation
object out, either blocking or streaming frame-by-frame. Also owns the
saved-generation store, mirroring the face pipeline (see face_gen_client.py)
but for arms: sides use shoulder_roll/shoulder_pitch/wrist instead of
eyes/mouth.

Constants (SYSTEM_PROMPT / USER_PROMPT_TEMPLATE) must stay byte-identical to
training. See TINY_ARM_DEPLOYMENT.md and ~/src/ft_gemma_face/ for provenance.
"""

import copy
import json
import os
import random
import re
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from .arm_schema import ARM_KEYS, ARM_RANGE, CONCRETE_ARM_SIDES, DEFAULT_ARMS_POSE, normalize_pose
from .face_gen_client import StreamingFrameParser, emoji_slug, truncate_rambling

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "smollm2-135m-arm-lora-38k:q4_K_M"

# Must match training exactly. Do not edit.
SYSTEM_PROMPT = "Generate only valid JSON for a Logos robot arm animation. No markdown. No explanation."
USER_PROMPT_TEMPLATE = "Create a JSON arm sequence with {n_frames} frames based on this input text: {text}"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCHEMA_PATH = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "schemas", "arm_ollama_response_format.json")
)
DEFAULT_STORE_DIR = "/home/robot/robot_ws/animations/arm_generated"
DEFAULT_STORE_CAP = 5  # rolling generations kept per emoji

# A generation hanging far past normal latency is a degenerate repetition
# loop, not slow progress (see TINY_ARM_DEPLOYMENT.md). Streaming playback
# means this never adds latency; it only kills runaways.
DEFAULT_TIMEOUT_S = 30.0

# Training data only covers 1-6 frame sequences (3-6 main corpus, 1-2
# hand-authored single-frame poses). Requesting outside that range gets
# poor results -- see TINY_ARM_DEPLOYMENT.md TL;DR.
_FRAME_HINT_SHORT = "1 to 2"
_FRAME_HINT_MEDIUM = "2 to 4"
_FRAME_HINT_LONG = "3 to 6"

# Generations past this many frames are presumed to be the model rambling
# (repeating/drifting) rather than a deliberate longer sequence -- the
# training data tops out around 6 frames, and spot checks of 6+ frame
# generations (usually at higher temperature) look like degenerate
# continuation, not intentional choreography. Streaming generation cuts the
# connection the moment the cutoff is hit (saves inference time too);
# blocking generation truncates after the fact. Either way the result is
# marked truncated=True and the caller should never save it to GenStore.
MAX_ACCEPTED_FRAMES = 7


def frame_count_hint(text: str) -> str:
    """
    Word-count heuristic for the soft n_frames request: short inputs get
    fewer frames, longer ones get more, staying within the training data's
    1-6 frame range. Falls back to char count if there's no whitespace.
    """
    text = text.strip()
    if any(ch.isspace() for ch in text):
        n = len(text.split())
    else:
        n = max(1, len(text) // 5)  # no whitespace: ~5 chars/word fallback
    if n <= 3:
        return _FRAME_HINT_SHORT
    if n <= 7:
        return _FRAME_HINT_MEDIUM
    return _FRAME_HINT_LONG


class ArmGenError(RuntimeError):
    """Raised when the arm model fails to produce a usable animation."""


def _clamp(key: str, value: Any) -> Any:
    if key in ARM_KEYS and isinstance(value, (int, float)):
        lo, hi = ARM_RANGE
        return min(hi, max(lo, float(value)))
    return value


def clamp_frame(frame: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a copy of a sparse semantic arm frame with side-patch keys
    normalized to canonical spelling (shoulder_roll/shoulder_pitch/wrist --
    accepts the legacy joint1/joint2 alias still used by the 1500+
    animations/arms_semantic/ LUT files) and numerics clamped in-range.

    Normalizing here, before ArmFrameExpander.feed()'s plain dict.update(),
    matters: without it a joint1/joint2-keyed patch merges as extra unused
    keys instead of overwriting shoulder_roll/shoulder_pitch, silently
    freezing those axes at their DEFAULT_ARMS_POSE value forever (wrist,
    spelled the same both ways, would keep working -- exactly the
    "wrists move, arms don't" symptom this fixes).
    """
    out = copy.deepcopy(frame)
    arms = out.get("arms", {})
    if isinstance(arms, dict):
        for side, side_patch in list(arms.items()):
            if isinstance(side_patch, dict):
                arms[side] = {k: _clamp(k, v) for k, v in normalize_pose(side_patch).items()}
    return out


def clamp_semantic_obj(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp every frame of a semantic arm animation object."""
    out = copy.deepcopy(obj)
    out["frames"] = [clamp_frame(f) for f in out.get("frames", [])]
    return out


def frame_overshoots(frame: Dict[str, Any]) -> List[str]:
    """
    Pre-clamp diagnostic: human-readable messages for numeric values outside
    ARM_RANGE in a raw (unclamped) sparse frame. Schema mode enforces
    structure but not range (see TINY_ARM_DEPLOYMENT.md), so this is the
    one residual failure mode worth surfacing when debugging generations.
    """
    msgs: List[str] = []
    for side, patch in (frame.get("arms") or {}).items():
        if not isinstance(patch, dict):
            continue
        for key, value in patch.items():
            if key in ARM_KEYS and isinstance(value, (int, float)):
                lo, hi = ARM_RANGE
                if not (lo <= value <= hi):
                    msgs.append("arms.{}.{}={} outside [{},{}]".format(side, key, value, lo, hi))
    return msgs


class ArmFrameExpander:
    """
    Stateful sparse-frame expander: feed semantic arm frames one at a time
    (e.g. as they stream out of the model) and get a full clamped left/right
    pose back for each. Carry-forward starts from DEFAULT_ARMS_POSE.
    """

    def __init__(self):
        self.current = copy.deepcopy(DEFAULT_ARMS_POSE)

    def feed(self, frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(frame, dict):
            return None
        frame = clamp_frame(frame)
        arms_patch = frame.get("arms", {}) or {}
        if isinstance(arms_patch.get("both"), dict):
            for side in CONCRETE_ARM_SIDES:
                self.current[side].update(arms_patch["both"])
        for side in CONCRETE_ARM_SIDES:
            if isinstance(arms_patch.get(side), dict):
                self.current[side].update(arms_patch[side])
        return {
            "beat": frame.get("beat", ""),
            "arms": copy.deepcopy(self.current),
        }


def expand_frames_lenient(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Expand sparse semantic arm frames into full left/right poses without
    strict validation. Model output only guarantees a 'beat' per frame;
    anything omitted carries forward from the previous frame (seeded from
    DEFAULT_ARMS_POSE), and numerics are clamped. Always returns one full
    pose per input frame.
    """
    expander = ArmFrameExpander()
    expanded: List[Dict[str, Any]] = []
    for frame in frames:
        pose = expander.feed(frame)
        if pose is not None:
            expanded.append(pose)
    return expanded


class GenStore:
    """
    Rolling per-emoji library of tiny-model arm generations. Same shape and
    rollover policy as face_gen_client.GenStore, kept as a separate instance
    (separate directory + filename prefix) rather than shared code because
    the "animation" payload shape and prefix differ.

    One JSON file per take: arm_gen_<emoji-slug>__<ts_ms>.json, holding
    {emoji, text, model, n_frames_requested, ts, animation}. At most `cap`
    takes kept per emoji; saving beyond the cap deletes the oldest.

    Only emoji-keyed takes are stored -- plain-text-inspired generations are
    ephemeral by design.
    """

    def __init__(self, path: str = DEFAULT_STORE_DIR, cap_per_emoji: int = DEFAULT_STORE_CAP):
        self.dir = path
        self.cap = max(1, int(cap_per_emoji))
        self._lock = threading.Lock()
        self._index: Dict[str, List[str]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.isdir(self.dir):
            return
        for name in sorted(os.listdir(self.dir)):
            if name.startswith("arm_gen_") and name.endswith(".json") and "__" in name:
                slug = name[len("arm_gen_"):].rsplit("__", 1)[0]
                self._index.setdefault(slug, []).append(name)

    def __len__(self) -> int:
        return sum(len(v) for v in self._index.values())

    def pick(self, emoji: str) -> Optional[Dict[str, Any]]:
        """Random saved take for this emoji, or None."""
        if not emoji:
            return None
        with self._lock:
            names = list(self._index.get(emoji_slug(emoji), []))
        random.shuffle(names)
        for name in names:
            try:
                with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                    rec = json.load(f)
                if rec.get("animation"):
                    return rec["animation"]
            except Exception:
                continue
        return None

    def save(
        self,
        emoji: str,
        animation: Dict[str, Any],
        model: str,
        text: str = "",
        n_frames_requested: str = "",
        temperature: float = 0.0,
        seed: Optional[int] = None,
    ) -> Optional[str]:
        """Save a take for an emoji, rolling out the oldest beyond the cap."""
        if not emoji:
            return None  # plain-text takes are never persisted
        slug = emoji_slug(emoji)
        name = "arm_gen_{}__{}.json".format(slug, int(time.time() * 1000))
        rec = {
            "ts": time.time(),
            "emoji": emoji,
            "text": text,
            "model": model,
            "n_frames_requested": n_frames_requested,
            "temperature": temperature,
            "seed": seed,
            "animation": animation,
        }
        with self._lock:
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            names = self._index.setdefault(slug, [])
            names.append(name)
            names.sort()
            while len(names) > self.cap:
                oldest = names.pop(0)
                try:
                    os.remove(os.path.join(self.dir, oldest))
                except OSError:
                    pass
        return name


class ArmGenClient:
    """Blocking and streaming generation against the local Ollama arm model."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        url: str = OLLAMA_URL,
        schema_path: str = DEFAULT_SCHEMA_PATH,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_frames: int = MAX_ACCEPTED_FRAMES,
    ):
        self.model = model
        self.url = url
        self.timeout_s = timeout_s
        self.max_frames = max(1, int(max_frames))
        with open(schema_path, encoding="utf-8") as f:
            self.response_schema = json.load(f)

    def _payload(
        self, text: str, n_frames: str, temperature: float, seed: Optional[int], stream: bool
    ) -> Dict[str, Any]:
        options: Dict[str, Any] = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        return {
            "model": self.model,
            "system": SYSTEM_PROMPT,
            "prompt": USER_PROMPT_TEMPLATE.format(n_frames=n_frames, text=text),
            "stream": stream,
            "format": self.response_schema,
            "options": options,
        }

    def generate(
        self,
        text: str,
        n_frames: Optional[str] = None,
        temperature: float = 0.0,
        seed: Optional[int] = 42,
        timeout_s: Optional[float] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Blocking generation. n_frames is a soft request string like "3 to 6"
        (auto-derived from text length via frame_count_hint() if omitted) --
        the model mostly complies but not exactly; always read
        len(result["frames"]) rather than assuming it matches the request.
        Returns a clamped semantic animation object.
        """
        n_frames = n_frames or frame_count_hint(text)
        timeout_s = timeout_s or self.timeout_s
        t0 = time.time()
        try:
            resp = requests.post(
                self.url,
                json=self._payload(text, n_frames, temperature, seed, stream=False),
                timeout=(5.0, timeout_s),
            )
            resp.raise_for_status()
            obj = json.loads(resp.json()["response"])
        except ArmGenError:
            raise
        except Exception as exc:
            raise ArmGenError("arm generation failed: {}".format(exc))
        if not isinstance(obj, dict) or not obj.get("frames"):
            raise ArmGenError("arm model returned no frames")
        raw_count = len(obj["frames"])
        if verbose:
            print("[arm_gen] requested {} frames, got {} in {:.1f}s".format(
                n_frames, raw_count, time.time() - t0))
            for i, frame in enumerate(obj["frames"]):
                overshoots = frame_overshoots(frame) if isinstance(frame, dict) else []
                beat = frame.get("beat", "") if isinstance(frame, dict) else ""
                print("  frame {}: {}{}".format(
                    i, beat, "  [OVERSHOOT: " + "; ".join(overshoots) + "]" if overshoots else ""))
        obj = truncate_rambling(clamp_semantic_obj(obj), self.max_frames)
        if verbose and obj["_truncated"]:
            print("[arm_gen] truncated {} -> {} frames (looks like rambling; will not be saved)".format(
                raw_count, len(obj["frames"])))
        return obj

    def generate_stream(
        self,
        text: str,
        n_frames: Optional[str] = None,
        temperature: float = 0.0,
        seed: Optional[int] = 42,
        timeout_s: Optional[float] = None,
        verbose: bool = False,
    ) -> Iterator[Tuple[str, Any]]:
        """
        Streaming generation. Yields ("frame", frame_dict) as each frame's
        closing brace arrives (clamped, sparse), then ("done", full_obj) with
        the complete clamped semantic object. Hitting the max_frames rambling
        cutoff or the wall-clock deadline mid-stream closes the connection
        early and still yields ("done", ...) built from the frames that
        arrived, marked _truncated=True (never save those). Raises
        ArmGenError only when nothing usable arrived. The consumer may also
        abandon the generator early (generator .close() / loop break); the
        HTTP connection is released either way.
        """
        n_frames = n_frames or frame_count_hint(text)
        timeout_s = timeout_s or self.timeout_s
        t0 = time.time()
        deadline = t0 + timeout_s
        parser = StreamingFrameParser()
        collected: List[Dict[str, Any]] = []
        cut_reason = None
        resp = None
        try:
            resp = requests.post(
                self.url,
                json=self._payload(text, n_frames, temperature, seed, stream=True),
                stream=True,
                timeout=(5.0, timeout_s),
            )
            resp.raise_for_status()
            for line in resp.iter_lines():
                if time.time() > deadline:
                    if not collected:
                        raise ArmGenError(
                            "arm generation exceeded {}s deadline".format(timeout_s))
                    # Salvage what streamed so far: the deadline guard kills
                    # degenerate repetition loops, but a runaway's early
                    # frames are still playable.
                    cut_reason = "deadline"
                    break
                if not line:
                    continue
                chunk = json.loads(line)
                for frame in parser.feed(chunk.get("response", "")):
                    clamped = clamp_frame(frame)
                    if verbose:
                        overshoots = frame_overshoots(frame)
                        print("[arm_gen] +{:.2f}s frame {}: {}{}".format(
                            time.time() - t0, len(collected), frame.get("beat", ""),
                            "  [OVERSHOOT: " + "; ".join(overshoots) + "]" if overshoots else ""))
                    collected.append(clamped)
                    yield ("frame", clamped)
                    if len(collected) >= self.max_frames:
                        # Rambling cutoff: stop reading the stream entirely --
                        # don't wait out the rest of the generation, it isn't
                        # going to be saved anyway. Saves inference time too.
                        cut_reason = "rambling cutoff"
                        break
                if cut_reason or chunk.get("done"):
                    break
        except ArmGenError:
            raise
        except Exception as exc:
            raise ArmGenError("arm generation stream failed: {}".format(exc))
        finally:
            # Covers cutoffs, errors, and the consumer closing the generator
            # early (mid-stream abort); harmless after a full read.
            if resp is not None:
                resp.close()

        if cut_reason:
            try:
                obj = parser.synthesize_close()
            except (ValueError, json.JSONDecodeError):
                obj = {"emoji": "", "frames": collected}
            obj = clamp_semantic_obj(obj)
            obj["_truncated"] = True
            if verbose:
                print("[arm_gen] done ({}): {} frames in {:.2f}s total "
                      "(will not be saved)".format(cut_reason, len(obj["frames"]),
                                                   time.time() - t0))
            yield ("done", obj)
            return

        try:
            obj = json.loads(parser.buf)
        except json.JSONDecodeError as exc:
            raise ArmGenError("streamed response was not valid JSON: {}".format(exc))
        if not isinstance(obj, dict) or not obj.get("frames"):
            raise ArmGenError("arm model returned no frames")
        if verbose:
            print("[arm_gen] done: requested {}, got {} frames in {:.2f}s total".format(
                n_frames, len(obj["frames"]), time.time() - t0))
        yield ("done", truncate_rambling(clamp_semantic_obj(obj), self.max_frames))
