"""
Client for the tiny fine-tuned face-animation model served by local Ollama.

Text/emoji in -> semantic face animation object out, either blocking or
streaming frame-by-frame. Also owns the saved-generation store (JSONL,
separate from the source-of-truth LUTs in animations/face_semantic/).

Constants (SYSTEM_PROMPT / USER_PROMPT) must stay byte-identical to training.
See TINY_FACE_DEPLOYMENT.md and ~/src/ft_gemma_face/ for provenance.
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

from .face_schema import DEFAULT_POSE, NUMERIC_RANGES, CONCRETE_EYE_SIDES

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "smollm2-135m-face-lora-34k:q4_K_M"

# Must match training exactly. Do not edit.
SYSTEM_PROMPT = "Generate only valid JSON for a Logos robot face animation. No markdown. No explanation."
USER_PROMPT_TEMPLATE = "Generate JSON face animation for text: {text}"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCHEMA_PATH = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "schemas", "ollama_response_format.json")
)
DEFAULT_STORE_DIR = "/home/robot/robot_ws/animations/face_generated"
DEFAULT_STORE_CAP = 5  # rolling generations kept per emoji

# A generation hanging far past normal latency is a degenerate repetition
# loop, not slow progress (see TINY_FACE_DEPLOYMENT.md). Streaming playback
# means this never adds latency; it only kills runaways.
DEFAULT_TIMEOUT_S = 30.0


class FaceGenError(RuntimeError):
    """Raised when the face model fails to produce a usable animation."""


def _clamp(key: str, value: Any) -> Any:
    if key in NUMERIC_RANGES and isinstance(value, (int, float)):
        lo, hi = NUMERIC_RANGES[key]
        return min(hi, max(lo, float(value)))
    return value


def clamp_frame(frame: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of a sparse semantic frame with numerics clamped in-range."""
    out = copy.deepcopy(frame)
    for side_patch in out.get("eyes", {}).values():
        if isinstance(side_patch, dict):
            for k in list(side_patch):
                side_patch[k] = _clamp(k, side_patch[k])
    mouth = out.get("mouth")
    if isinstance(mouth, dict):
        for k in list(mouth):
            mouth[k] = _clamp(k, mouth[k])
    return out


def clamp_semantic_obj(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp every frame of a semantic animation object."""
    out = copy.deepcopy(obj)
    out["frames"] = [clamp_frame(f) for f in out.get("frames", [])]
    return out


def frame_overshoots(frame: Dict[str, Any]) -> List[str]:
    """
    Pre-clamp diagnostic: human-readable messages for numeric values outside
    NUMERIC_RANGES in a raw (unclamped) sparse frame. Schema mode enforces
    structure but not range (see TINY_FACE_DEPLOYMENT.md), so this is the
    one residual failure mode worth surfacing when debugging generations.
    """
    msgs: List[str] = []
    for side, patch in (frame.get("eyes") or {}).items():
        if not isinstance(patch, dict):
            continue
        for key, value in patch.items():
            if key in NUMERIC_RANGES and isinstance(value, (int, float)):
                lo, hi = NUMERIC_RANGES[key]
                if not (lo <= value <= hi):
                    msgs.append("eyes.{}.{}={} outside [{},{}]".format(side, key, value, lo, hi))
    mouth = frame.get("mouth")
    if isinstance(mouth, dict):
        for key, value in mouth.items():
            if key in NUMERIC_RANGES and isinstance(value, (int, float)):
                lo, hi = NUMERIC_RANGES[key]
                if not (lo <= value <= hi):
                    msgs.append("mouth.{}={} outside [{},{}]".format(key, value, lo, hi))
    return msgs


class FrameExpander:
    """
    Stateful sparse-frame expander: feed semantic frames one at a time (e.g.
    as they stream out of the model) and get a full clamped left/right pose
    back for each. Carry-forward starts from DEFAULT_POSE.
    """

    def __init__(self):
        self.current = copy.deepcopy(DEFAULT_POSE)

    def feed(self, frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(frame, dict):
            return None
        frame = clamp_frame(frame)
        eyes_patch = frame.get("eyes", {}) or {}
        if isinstance(eyes_patch.get("both"), dict):
            for side in CONCRETE_EYE_SIDES:
                self.current["eyes"][side].update(eyes_patch["both"])
        for side in CONCRETE_EYE_SIDES:
            if isinstance(eyes_patch.get(side), dict):
                self.current["eyes"][side].update(eyes_patch[side])
        mouth_patch = frame.get("mouth")
        if isinstance(mouth_patch, dict):
            self.current["mouth"].update(mouth_patch)
        return {
            "beat": frame.get("beat", ""),
            "eyes": copy.deepcopy(self.current["eyes"]),
            "mouth": copy.deepcopy(self.current["mouth"]),
        }


def expand_frames_lenient(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Expand sparse semantic frames into full left/right poses without strict
    validation. Model output only guarantees a 'beat' per frame; anything
    omitted carries forward from the previous frame (seeded from DEFAULT_POSE),
    and numerics are clamped. Always returns one full pose per input frame.
    """
    expander = FrameExpander()
    expanded: List[Dict[str, Any]] = []
    for frame in frames:
        pose = expander.feed(frame)
        if pose is not None:
            expanded.append(pose)
    return expanded


class StreamingFrameParser:
    """
    Incremental extractor of completed frame objects from a partial JSON
    response shaped like {"emoji": "...", "frames": [ {...}, {...} ]}.

    Feed it text deltas; it returns each frame dict as soon as its closing
    brace arrives. Any '{' encountered inside a '[' starts a frame capture;
    nested objects (eyes/mouth) are tracked by brace depth.
    """

    def __init__(self):
        self.buf = ""
        self._pos = 0
        self._in_string = False
        self._escape = False
        self._array_depth = 0
        self._capture_start = -1
        self._capture_depth = 0

    def feed(self, delta: str) -> List[Dict[str, Any]]:
        self.buf += delta
        frames: List[Dict[str, Any]] = []
        while self._pos < len(self.buf):
            ch = self.buf[self._pos]
            if self._in_string:
                if self._escape:
                    self._escape = False
                elif ch == "\\":
                    self._escape = True
                elif ch == '"':
                    self._in_string = False
            elif ch == '"':
                self._in_string = True
            elif ch == "[":
                self._array_depth += 1
            elif ch == "]":
                self._array_depth = max(0, self._array_depth - 1)
            elif ch == "{":
                if self._capture_start >= 0:
                    self._capture_depth += 1
                elif self._array_depth > 0:
                    self._capture_start = self._pos
                    self._capture_depth = 1
            elif ch == "}":
                if self._capture_start >= 0:
                    self._capture_depth -= 1
                    if self._capture_depth == 0:
                        raw = self.buf[self._capture_start : self._pos + 1]
                        self._capture_start = -1
                        try:
                            frames.append(json.loads(raw))
                        except json.JSONDecodeError:
                            pass
            self._pos += 1
        return frames


def emoji_slug(emoji: str) -> str:
    """Deterministic filesystem slug for an emoji (unicode names)."""
    import unicodedata
    parts = []
    for ch in emoji:
        try:
            parts.append(unicodedata.name(ch).lower())
        except ValueError:
            parts.append("u{:04x}".format(ord(ch)))
    slug = re.sub(r"[^a-z0-9]+", "_", "_".join(parts)).strip("_")
    return slug[:80] or "unknown"


class GenStore:
    """
    Rolling per-emoji library of tiny-model face generations.

    One JSON file per take: face_gen_<emoji-slug>__<ts_ms>.json in the store
    dir, holding {emoji, text, model, temperature, ts, animation}. At most
    `cap` takes are kept per emoji; saving beyond the cap deletes the oldest,
    so a full library accumulates and rolls over with time. pick(emoji)
    shuffles among that emoji's saved takes.

    Only emoji-keyed takes are stored -- plain-text-inspired generations are
    ephemeral by design. Kept strictly separate from the LUT dirs (the
    Gemini-authored training source of truth); this is the baby model's
    scrapbook.
    """

    def __init__(self, path: str = DEFAULT_STORE_DIR, cap_per_emoji: int = DEFAULT_STORE_CAP):
        self.dir = path
        self.cap = max(1, int(cap_per_emoji))
        self._lock = threading.Lock()
        # slug -> sorted list of filenames (oldest first; ts is in the name)
        self._index: Dict[str, List[str]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.isdir(self.dir):
            return
        for name in sorted(os.listdir(self.dir)):
            if name.startswith("face_gen_") and name.endswith(".json") and "__" in name:
                slug = name[len("face_gen_"):].rsplit("__", 1)[0]
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
        temperature: float = 0.0,
        seed: Optional[int] = None,
    ) -> Optional[str]:
        """Save a take for an emoji, rolling out the oldest beyond the cap."""
        if not emoji:
            return None  # plain-text takes are never persisted
        slug = emoji_slug(emoji)
        name = "face_gen_{}__{}.json".format(slug, int(time.time() * 1000))
        rec = {
            "ts": time.time(),
            "emoji": emoji,
            "text": text,
            "model": model,
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


class FaceGenClient:
    """Blocking and streaming generation against the local Ollama face model."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        url: str = OLLAMA_URL,
        schema_path: str = DEFAULT_SCHEMA_PATH,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self.model = model
        self.url = url
        self.timeout_s = timeout_s
        with open(schema_path, encoding="utf-8") as f:
            self.response_schema = json.load(f)

    def _payload(self, text: str, temperature: float, seed: Optional[int], stream: bool) -> Dict[str, Any]:
        options: Dict[str, Any] = {"temperature": temperature}
        if seed is not None:
            options["seed"] = seed
        return {
            "model": self.model,
            "system": SYSTEM_PROMPT,
            "prompt": USER_PROMPT_TEMPLATE.format(text=text),
            "stream": stream,
            "format": self.response_schema,
            "options": options,
        }

    def generate(
        self,
        text: str,
        temperature: float = 0.0,
        seed: Optional[int] = 42,
        timeout_s: Optional[float] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Blocking generation. Returns a clamped semantic animation object.
        With verbose=True, prints per-frame range overshoots to stdout
        (diagnostic only; does not change the returned/clamped values).
        """
        timeout_s = timeout_s or self.timeout_s
        t0 = time.time()
        try:
            resp = requests.post(
                self.url,
                json=self._payload(text, temperature, seed, stream=False),
                timeout=(5.0, timeout_s),
            )
            resp.raise_for_status()
            obj = json.loads(resp.json()["response"])
        except FaceGenError:
            raise
        except Exception as exc:
            raise FaceGenError("face generation failed: {}".format(exc))
        if not isinstance(obj, dict) or not obj.get("frames"):
            raise FaceGenError("face model returned no frames")
        if verbose:
            print("[face_gen] {} frames in {:.1f}s".format(len(obj["frames"]), time.time() - t0))
            for i, frame in enumerate(obj["frames"]):
                overshoots = frame_overshoots(frame) if isinstance(frame, dict) else []
                beat = frame.get("beat", "") if isinstance(frame, dict) else ""
                print("  frame {}: {}{}".format(
                    i, beat, "  [OVERSHOOT: " + "; ".join(overshoots) + "]" if overshoots else ""))
        return clamp_semantic_obj(obj)

    def generate_stream(
        self,
        text: str,
        temperature: float = 0.0,
        seed: Optional[int] = 42,
        timeout_s: Optional[float] = None,
        verbose: bool = False,
    ) -> Iterator[Tuple[str, Any]]:
        """
        Streaming generation. Yields ("frame", frame_dict) as each frame's
        closing brace arrives (clamped, sparse), then ("done", full_obj) with
        the complete clamped semantic object. Raises FaceGenError on failure
        or wall-clock timeout (degenerate repetition loop guard).

        With verbose=True, prints each frame's beat text, arrival latency,
        and any pre-clamp range overshoot to stdout as it streams in.
        """
        timeout_s = timeout_s or self.timeout_s
        t0 = time.time()
        deadline = t0 + timeout_s
        parser = StreamingFrameParser()
        frame_i = 0
        try:
            resp = requests.post(
                self.url,
                json=self._payload(text, temperature, seed, stream=True),
                stream=True,
                timeout=(5.0, timeout_s),
            )
            resp.raise_for_status()
            for line in resp.iter_lines():
                if time.time() > deadline:
                    resp.close()
                    raise FaceGenError("face generation exceeded {}s deadline".format(timeout_s))
                if not line:
                    continue
                chunk = json.loads(line)
                for frame in parser.feed(chunk.get("response", "")):
                    if verbose:
                        overshoots = frame_overshoots(frame)
                        print("[face_gen] +{:.2f}s frame {}: {}{}".format(
                            time.time() - t0, frame_i, frame.get("beat", ""),
                            "  [OVERSHOOT: " + "; ".join(overshoots) + "]" if overshoots else ""))
                        frame_i += 1
                    yield ("frame", clamp_frame(frame))
                if chunk.get("done"):
                    break
        except FaceGenError:
            raise
        except Exception as exc:
            raise FaceGenError("face generation stream failed: {}".format(exc))

        try:
            obj = json.loads(parser.buf)
        except json.JSONDecodeError as exc:
            raise FaceGenError("streamed response was not valid JSON: {}".format(exc))
        if not isinstance(obj, dict) or not obj.get("frames"):
            raise FaceGenError("face model returned no frames")
        if verbose:
            print("[face_gen] done: {} frames in {:.2f}s total".format(
                len(obj["frames"]), time.time() - t0))
        yield ("done", clamp_semantic_obj(obj))
