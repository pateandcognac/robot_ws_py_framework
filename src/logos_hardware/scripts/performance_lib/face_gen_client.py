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
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from .face_schema import DEFAULT_POSE, NUMERIC_RANGES, CONCRETE_EYE_SIDES

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "smollm2-135m-face-lora-34k:q2_K"

# Must match training exactly. Do not edit.
SYSTEM_PROMPT = "Generate only valid JSON for a Logos robot face animation. No markdown. No explanation."
USER_PROMPT_TEMPLATE = "Generate JSON face animation for text: {text}"

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCHEMA_PATH = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "schemas", "ollama_response_format.json")
)
DEFAULT_STORE_PATH = "/home/robot/robot_ws/animations/face_generated/face_gen_store.jsonl"

# A generation hanging far past normal latency is a degenerate repetition
# loop, not slow progress (see TINY_FACE_DEPLOYMENT.md).
DEFAULT_TIMEOUT_S = 45.0


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


def expand_frames_lenient(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Expand sparse semantic frames into full left/right poses without strict
    validation. Model output only guarantees a 'beat' per frame; anything
    omitted carries forward from the previous frame (seeded from DEFAULT_POSE),
    and numerics are clamped. Always returns one full pose per input frame.
    """
    current = copy.deepcopy(DEFAULT_POSE)
    expanded: List[Dict[str, Any]] = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        frame = clamp_frame(frame)
        eyes_patch = frame.get("eyes", {}) or {}
        if isinstance(eyes_patch.get("both"), dict):
            for side in CONCRETE_EYE_SIDES:
                current["eyes"][side].update(eyes_patch["both"])
        for side in CONCRETE_EYE_SIDES:
            if isinstance(eyes_patch.get(side), dict):
                current["eyes"][side].update(eyes_patch[side])
        mouth_patch = frame.get("mouth")
        if isinstance(mouth_patch, dict):
            current["mouth"].update(mouth_patch)
        expanded.append(
            {
                "beat": frame.get("beat", ""),
                "eyes": copy.deepcopy(current["eyes"]),
                "mouth": copy.deepcopy(current["mouth"]),
            }
        )
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


class GenStore:
    """
    Append-only JSONL store of tiny-model generations, indexed by input text.

    Kept strictly separate from the LUT dirs: those are the Gemini-authored
    training source of truth, this is the baby model's scrapbook. Multiple
    saved generations for the same text are all kept; pick() returns a random
    one so replayed lines still vary.
    """

    def __init__(self, path: str = DEFAULT_STORE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._index: Dict[str, List[Dict[str, Any]]] = {}
        self._load()

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.split())

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("text") is not None and rec.get("animation"):
                    self._index.setdefault(self._key(rec["text"]), []).append(rec)

    def __len__(self) -> int:
        return sum(len(v) for v in self._index.values())

    def lookup(self, text: str, model: Optional[str] = None) -> List[Dict[str, Any]]:
        recs = self._index.get(self._key(text), [])
        if model:
            recs = [r for r in recs if r.get("model") == model]
        return recs

    def pick(self, text: str, model: Optional[str] = None) -> Optional[Dict[str, Any]]:
        recs = self.lookup(text, model)
        return random.choice(recs)["animation"] if recs else None

    def save(
        self,
        text: str,
        animation: Dict[str, Any],
        model: str,
        temperature: float = 0.0,
        seed: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        rec = {
            "ts": time.time(),
            "model": model,
            "text": text,
            "temperature": temperature,
            "seed": seed,
            "animation": animation,
        }
        if extra:
            rec.update(extra)
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._index.setdefault(self._key(text), []).append(rec)
        return rec


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
    ) -> Dict[str, Any]:
        """Blocking generation. Returns a clamped semantic animation object."""
        timeout_s = timeout_s or self.timeout_s
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
        return clamp_semantic_obj(obj)

    def generate_stream(
        self,
        text: str,
        temperature: float = 0.0,
        seed: Optional[int] = 42,
        timeout_s: Optional[float] = None,
    ) -> Iterator[Tuple[str, Any]]:
        """
        Streaming generation. Yields ("frame", frame_dict) as each frame's
        closing brace arrives (clamped, sparse), then ("done", full_obj) with
        the complete clamped semantic object. Raises FaceGenError on failure
        or wall-clock timeout (degenerate repetition loop guard).
        """
        timeout_s = timeout_s or self.timeout_s
        deadline = time.time() + timeout_s
        parser = StreamingFrameParser()
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
        yield ("done", clamp_semantic_obj(obj))
