#!/usr/bin/env python3
"""
Smoke tests for src/logos_hardware/scripts/performance_lib.

Offline tests always run. Pass --live to also hit the local Ollama face
model (blocking + streaming generation).
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "logos_hardware", "scripts")))

from performance_lib import luts
from performance_lib.face_schema import expand_semantic_sequence
from performance_lib.face_gen_client import (
    FaceGenClient,
    GenStore,
    StreamingFrameParser,
    clamp_semantic_obj,
    expand_frames_lenient,
)


def test_streaming_parser():
    doc = json.dumps({
        "emoji": "🎉",
        "frames": [
            {"beat": "a", "eyes": {"both": {"gaze_x": 0.1, "color": "#FF0000"}},
             "mouth": {"frequency": 2.0, "amplitude": 0.5}},
            {"beat": "b {tricky\"} string", "eyes": {"left": {"gaze_x": -1.0}}},
            {"beat": "c"},
        ],
    }, ensure_ascii=False)
    # Feed in awkward small chunks to exercise incremental state.
    for chunk_size in (1, 3, 7, len(doc)):
        parser = StreamingFrameParser()
        frames = []
        for i in range(0, len(doc), chunk_size):
            frames.extend(parser.feed(doc[i:i + chunk_size]))
        assert len(frames) == 3, (chunk_size, frames)
        assert frames[0]["beat"] == "a"
        assert frames[1]["beat"] == 'b {tricky"} string'
        assert json.loads(parser.buf)["emoji"] == "🎉"
    print("ok: streaming parser")


def test_lenient_expansion_and_clamp():
    obj = {
        "emoji": "🧪",
        "frames": [
            {"beat": "wide", "eyes": {"both": {"gaze_x": 1.5, "scale_x": 0.9}},
             "mouth": {"amplitude": 1.3}},
            {"beat": "sparse follow-up"},
            {"beat": "asym", "eyes": {"left": {"gaze_x": -2.0}}},
        ],
    }
    clamped = clamp_semantic_obj(obj)
    assert clamped["frames"][0]["eyes"]["both"]["gaze_x"] == 1.0
    assert clamped["frames"][0]["mouth"]["amplitude"] == 1.0
    expanded = expand_frames_lenient(obj["frames"])
    assert len(expanded) == 3
    # carry-forward: frame 2 inherits frame 1's values
    assert expanded[1]["eyes"]["left"]["scale_x"] == 0.9
    assert expanded[1]["mouth"]["amplitude"] == 1.0
    # asym patch applies to left only, right carries forward
    assert expanded[2]["eyes"]["left"]["gaze_x"] == -1.0
    assert expanded[2]["eyes"]["right"]["gaze_x"] == 1.0
    # every expanded frame is a full pose
    for f in expanded:
        assert set(f["eyes"]["left"]) == {"gaze_x", "gaze_y", "scale_x", "scale_y",
                                          "lid_height", "lid_angle", "color"}
        assert set(f["mouth"]) == {"frequency", "amplitude", "phase",
                                   "phase_increment", "color"}
    print("ok: lenient expansion + clamp")


def test_luts_and_strict_expansion():
    face = luts.load_semantic_face_lut()
    arms = luts.load_arm_lut()
    assert len(face) > 1500, len(face)
    assert len(arms) > 1400, len(arms)
    # strict expansion works on a real LUT entry
    expanded = expand_semantic_sequence(face["🆚"])
    assert 4 <= len(expanded) <= 9
    print("ok: LUTs loaded (face={}, arms={}), strict expansion".format(len(face), len(arms)))


def test_gen_store():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "store.jsonl")
        store = GenStore(path)
        assert store.pick("hello there 👋") is None
        anim = {"emoji": "👋", "frames": [{"beat": "wave"}]}
        store.save("hello  there 👋", anim, model="m1", temperature=0.5, seed=7)
        # whitespace-normalized key matches
        assert store.pick("hello there 👋") == anim
        assert store.pick("hello there 👋", model="other") is None
        # reload from disk
        store2 = GenStore(path)
        assert len(store2) == 1
        assert store2.pick("hello there 👋") == anim
    print("ok: gen store round-trip")


def test_live_generation():
    client = FaceGenClient()
    t0 = time.time()
    obj = client.generate("🥱 I could use a recharge")
    dt = time.time() - t0
    assert obj.get("frames"), obj
    expanded = expand_frames_lenient(obj["frames"])
    assert expanded
    print("ok: live blocking gen ({} frames in {:.1f}s)".format(len(obj["frames"]), dt))

    t0 = time.time()
    n = 0
    first_frame_t = None
    for kind, payload in client.generate_stream("victory! 🏆", temperature=0.5):
        if kind == "frame":
            n += 1
            if first_frame_t is None:
                first_frame_t = time.time() - t0
        else:
            done_obj = payload
    dt = time.time() - t0
    assert n == len(done_obj["frames"]), (n, len(done_obj["frames"]))
    print("ok: live streaming gen ({} frames, first at {:.1f}s, done {:.1f}s)".format(
        n, first_frame_t, dt))


if __name__ == "__main__":
    test_streaming_parser()
    test_lenient_expansion_and_clamp()
    test_luts_and_strict_expansion()
    test_gen_store()
    if "--live" in sys.argv:
        test_live_generation()
    print("all tests passed")
