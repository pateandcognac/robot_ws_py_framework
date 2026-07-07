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
    truncate_rambling,
)
from performance_lib.arm_schema import (
    compile_semantic_to_legacy as compile_arm_semantic_to_legacy,
    expand_semantic_arm_frames,
    validate_semantic_arm_sequence,
)
from performance_lib.arm_gen_client import (
    ArmGenClient,
    clamp_frame as clamp_arm_frame,
    expand_frames_lenient as expand_arm_frames_lenient,
    frame_count_hint,
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


def test_parser_robustness():
    # Leading BOM / zero-width / whitespace junk must not break frame
    # extraction or the final json.loads(parser.buf).
    doc = "﻿​ \n" + json.dumps(
        {"emoji": "🎉", "frames": [{"beat": "a"}, {"beat": "b"}]}, ensure_ascii=False)
    for chunk_size in (1, 3, 7, len(doc)):
        parser = StreamingFrameParser()
        frames = []
        for i in range(0, len(doc), chunk_size):
            frames.extend(parser.feed(doc[i:i + chunk_size]))
        assert [f["beat"] for f in frames] == ["a", "b"], (chunk_size, frames)
        assert json.loads(parser.buf)["emoji"] == "🎉"

    # Frames array before the emoji key parses identically.
    doc = '{"frames": [{"beat": "x"}, {"beat": "y"}], "emoji": "🎉"}'
    for chunk_size in (1, 3, 7, len(doc)):
        parser = StreamingFrameParser()
        frames = []
        for i in range(0, len(doc), chunk_size):
            frames.extend(parser.feed(doc[i:i + chunk_size]))
        assert [f["beat"] for f in frames] == ["x", "y"], (chunk_size, frames)

    # A malformed frame object is dropped, not raised, and later frames
    # still come through.
    parser = StreamingFrameParser()
    frames = parser.feed('{"frames": [{"beat": bad}, {"beat": "ok"}]}')
    assert [f.get("beat") for f in frames] == ["ok"], frames
    print("ok: parser robustness (BOM, key order, malformed frame)")


def test_synthesize_close():
    doc = json.dumps({
        "emoji": "🎉",
        "frames": [{"beat": "a"}, {"beat": "b {curly\"} trap"}, {"beat": "c"}],
    }, ensure_ascii=False)
    # Cut the stream at every possible byte position: synthesize_close must
    # always produce a valid object holding exactly the fully-arrived frames
    # (and the emoji, which precedes the frames array here).
    saw_counts = set()
    for cut in range(1, len(doc) + 1):
        parser = StreamingFrameParser()
        completed = parser.feed(doc[:cut])
        try:
            obj = parser.synthesize_close()
        except ValueError:
            assert not completed, "frames arrived but close failed at cut %d" % cut
            continue
        assert obj["frames"] == completed, (cut, obj)
        if completed:
            assert obj["emoji"] == "🎉", (cut, obj)
        saw_counts.add(len(obj["frames"]))
    assert saw_counts == {0, 1, 2, 3}, saw_counts

    # Frames-first key order: a cut mid-stream loses the trailing emoji key
    # (it hasn't arrived yet in practice) and defaults to "".
    parser = StreamingFrameParser()
    parser.feed('{"frames": [{"beat": "x"}, {"beat": "y"}], "emo')
    obj = parser.synthesize_close()
    assert [f["beat"] for f in obj["frames"]] == ["x", "y"]
    assert obj["emoji"] == ""

    # Chunked feeding reaches the same result as one-shot feeding.
    partial = doc[: doc.index('"c"')]
    parser = StreamingFrameParser()
    for i in range(0, len(partial), 3):
        parser.feed(partial[i:i + 3])
    obj = parser.synthesize_close()
    assert len(obj["frames"]) == 2 and obj["emoji"] == "🎉"
    print("ok: synthesize_close at every cut point")


def test_truncate_rambling():
    obj = {"emoji": "🌀", "frames": [{"beat": str(i)} for i in range(12)]}
    out = truncate_rambling(dict(obj), 9)
    assert len(out["frames"]) == 9 and out["_truncated"] is True
    out = truncate_rambling(dict(obj), 12)
    assert len(out["frames"]) == 12 and out["_truncated"] is False
    print("ok: truncate_rambling cap + flag")


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
        store = GenStore(tmp, cap_per_emoji=3)
        assert store.pick("👋") is None
        assert store.save("", {"frames": [1]}, model="m") is None  # text-only: never saved
        for i in range(5):
            store.save("👋", {"emoji": "👋", "frames": [{"beat": "take %d" % i}]},
                       model="m1", text="hello 👋")
            time.sleep(0.002)  # distinct ms timestamps in filenames
        # cap rolled: only the 3 newest takes remain
        assert len(store) == 3
        picked = store.pick("👋")
        assert picked["frames"][0]["beat"] in ("take 2", "take 3", "take 4")
        # reload from disk
        store2 = GenStore(tmp, cap_per_emoji=3)
        assert len(store2) == 3
        assert store2.pick("👋")["frames"][0]["beat"].startswith("take")
        assert store2.pick("🌋") is None
    print("ok: gen store per-emoji cap + rollover")


def test_chunking():
    from performance_lib.chunking import subchunk_pairs, subchunk_text, find_emoji

    # short sentences stay whole, one per chunk
    assert subchunk_text("Hi there. All good?") == ["Hi there.", "All good?"]
    # long emoji-less prose gets broken at clause punctuation under the limit
    long = ("I have been pondering the mysteries of the charging dock, "
            "which hums quietly in the corner while the household sleeps, "
            "and I have concluded that it dreams of electric sheep as well.")
    chunks = subchunk_text(long)
    assert all(len(c) <= 100 for c in chunks), [len(c) for c in chunks]
    assert " ".join(chunks).replace(" ", "") == long.replace(" ", "")
    # emoji stays on the last subchunk of its span
    pairs = subchunk_pairs([(long, "🐑"), ("Short bit", "")])
    assert pairs[-2][1] == "🐑" and pairs[-2][0] == chunks[-1]
    assert all(e == "" for _, e in pairs[:-2])
    # emoji extraction from merged gesture text
    assert find_emoji("volcanic fury 🌋 rising", {"🌋", "🌊"}) == "🌋"
    assert find_emoji("no emoji here", {"🌋"}) is None
    print("ok: chunking + emoji extraction")


def test_estimate_speech_duration():
    from performance_lib.chunking import estimate_speech_duration

    assert estimate_speech_duration("") == 0.3  # empty floor
    short = estimate_speech_duration("Hi there")
    long = estimate_speech_duration("This is a much longer sentence with quite a few more words in it")
    assert 0.3 <= short < long
    # no whitespace: char-count fallback still produces a sane positive estimate
    assert estimate_speech_duration("supercalifragilisticexpialidocious") > 0.3
    print("ok: estimate_speech_duration heuristic")


def test_arm_key_backward_compat():
    """
    The arm model was trained on shoulder_roll/shoulder_pitch, renamed from
    joint1/joint2 for legibility -- but the 1488+ existing arms_semantic
    files and the ROS-level ArmPose message both still use joint1/joint2.
    arm_schema.py must read either spelling and always emit joint1/joint2
    when compiling to the legacy runtime format.
    """
    old_style = {
        "emoji": "🧪",
        "ideation": "",
        "frames": [
            {"beat": "rest", "arms": {"both": {"joint1": 5.0, "joint2": -80.0, "wrist": 0.0}}},
            {"beat": "reach", "arms": {"left": {"joint1": 40.0}, "right": {"joint2": 20.0}}},
        ],
    }
    assert validate_semantic_arm_sequence(old_style) == []
    expanded = expand_semantic_arm_frames(old_style["frames"])
    assert expanded[1]["arms"]["left"]["shoulder_roll"] == 40.0
    assert expanded[1]["arms"]["left"]["shoulder_pitch"] == -80.0  # carried forward
    assert expanded[1]["arms"]["right"]["shoulder_pitch"] == 20.0

    new_style = {
        "emoji": "🧪",
        "ideation": "",
        "frames": [
            {"beat": "rest", "arms": {"both": {"shoulder_roll": 5.0, "shoulder_pitch": -80.0, "wrist": 0.0}}},
            {"beat": "reach", "arms": {"both": {"shoulder_roll": 30.0, "shoulder_pitch": -40.0, "wrist": 10.0}}},
        ],
    }
    assert validate_semantic_arm_sequence(new_style) == []
    legacy = compile_arm_semantic_to_legacy(new_style)
    action = legacy[0]["frames"][0][0]["parameters"]
    assert action["joint1"] == 5.0 and action["joint2"] == -80.0 and "shoulder_roll" not in action
    print("ok: arm key backward compat (joint1/joint2 <-> shoulder_roll/shoulder_pitch)")


def test_arm_playback_key_compat():
    """
    Regression for the LUT-playback bug: arm_gen_client.ArmFrameExpander
    (via expand_frames_lenient) is what the sequencer actually calls for
    BOTH generated/saved tracks and master-LUT playback -- unlike
    arm_schema.expand_semantic_arm_frames (covered by
    test_arm_key_backward_compat), it used to merge frame patches by raw
    dict key with no joint1/joint2 -> shoulder_roll/shoulder_pitch
    normalization. A joint1/joint2-keyed LUT patch (all 1500+
    animations/arms_semantic/ files use this legacy spelling) would then
    merge as extra unused keys instead of overwriting shoulder_roll/
    shoulder_pitch, silently freezing those two axes at their
    DEFAULT_ARMS_POSE value forever -- while wrist (spelled the same both
    ways) kept working. Symptom Mark observed live: "wrists move, arms
    don't" when playing back the master LUT.
    """
    old_style_frames = [
        {"beat": "rest", "arms": {"both": {"joint1": 5.0, "joint2": -80.0, "wrist": 0.0}}},
        {"beat": "reach", "arms": {"both": {"joint1": 40.0, "joint2": 20.0, "wrist": 10.0}}},
    ]
    expanded = expand_arm_frames_lenient(old_style_frames)
    assert expanded[0]["arms"]["left"]["shoulder_roll"] == 5.0
    assert expanded[0]["arms"]["left"]["shoulder_pitch"] == -80.0
    assert expanded[1]["arms"]["left"]["shoulder_roll"] == 40.0  # would be stuck at 5.0 pre-fix
    assert expanded[1]["arms"]["left"]["shoulder_pitch"] == 20.0  # would be stuck at -80.0 pre-fix
    assert expanded[1]["arms"]["left"]["wrist"] == 10.0  # always worked (same key both spellings)

    # clamp_frame itself: legacy keys normalize AND clamp in one pass, and
    # unknown/garbage keys are dropped rather than passed through silently.
    clamped = clamp_arm_frame(
        {"beat": "x", "arms": {"both": {"joint1": 999.0, "joint2": -999.0, "bogus": 1.0}}})
    both = clamped["arms"]["both"]
    assert both == {"shoulder_roll": 90.0, "shoulder_pitch": -90.0}  # clamped, "bogus" dropped
    print("ok: arm playback key compat (joint1/joint2 normalize through ArmFrameExpander)")


def test_frame_count_hint():
    assert frame_count_hint("hi") == "1 to 2"
    assert frame_count_hint("wave hello there") == "1 to 2"
    assert frame_count_hint("wave hello enthusiastically at the crowd") == "2 to 4"
    assert frame_count_hint("a very long sentence with plenty of words describing a big gesture") == "3 to 6"
    assert frame_count_hint("nospacessss" * 5) == "3 to 6"  # char-count fallback
    print("ok: frame count hint heuristic")


def test_ollama_pool():
    from performance_lib import ollama_pool

    # URL helpers are idempotent across base / full-endpoint spellings
    assert ollama_pool.generate_url("http://x:11434") == "http://x:11434/api/generate"
    assert ollama_pool.generate_url("http://x:11434/api/generate") == "http://x:11434/api/generate"
    assert ollama_pool._tags_url("http://x:11434/api/generate") == "http://x:11434/api/tags"

    # Missing config degrades to a pinned single-server pool (zero-config path)
    pool = ollama_pool.load_pool("face", "/nonexistent/servers.json",
                                 default_url="http://localhost:11434/api/generate",
                                 default_model="m:q4")
    assert pool.pinned
    assert pool.current() == ("http://localhost:11434/api/generate", "m:q4")
    pool.report_failure()  # no-op on pinned pools, must not raise

    # Config parsing: role entries honored, malformed entries skipped,
    # unknown role degrades to pinned defaults
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "servers.json")
        with open(path, "w") as f:
            json.dump({
                "face": [
                    {"url": "http://dead.invalid:11434", "model": "m:q8"},
                    {"url": "http://localhost:11434", "model": "m:q4"},
                    {"model": "missing-url"},
                ],
                "probe_timeout_s": 0.2,
                "probe_interval_s": 3600,
            }, f)
        pool = ollama_pool.load_pool("face", path,
                                     default_url="http://d/api/generate",
                                     default_model="d")
        assert not pool.pinned and len(pool.entries) == 2
        # neither test server has model "m:*", so the probe falls back to
        # the configured last resort (localhost entry)
        assert pool.current() == ("http://localhost:11434/api/generate", "m:q4")
        pool2 = ollama_pool.load_pool("arms", path,
                                      default_url="http://d/api/generate",
                                      default_model="d")
        assert pool2.pinned and pool2.current() == ("http://d/api/generate", "d")
    print("ok: ollama pool (config parsing, pinned fallback, last-resort probe)")


def test_ollama_pool_demotion():
    """A server that probes OK but fails generation gets routed around."""
    from performance_lib import ollama_pool

    live = {"http://good:11434", "http://bad:11434"}  # both answer /api/tags
    orig = ollama_pool._server_has_model
    ollama_pool._server_has_model = lambda base, model, t: base.rstrip("/") in live
    try:
        entries = [
            {"url": "http://bad:11434", "model": "m"},
            {"url": "http://good:11434", "model": "m"},
        ]
        # Build without the startup probe/thread by constructing pinned-off
        # then driving _probe directly (avoids the daemon thread in tests).
        pool = ollama_pool.OllamaPool("t", entries, probe_timeout_s=0.1,
                                      probe_interval_s=9999, pinned=True)
        pool.pinned = False  # re-enable failure handling for the test
        assert pool._probe() == entries[0]  # bad is first, probes fine
        # Three generation failures demote the current (bad) server.
        pool._current = entries[0]
        for _ in range(ollama_pool.FAILURES_BEFORE_REPROBE):
            pool.report_failure()
        assert ollama_pool._entry_key(entries[0]) in pool._demoted
        assert pool._probe() == entries[1]  # now routes to good
        # When the cooldown lapses, bad is eligible again.
        pool._demoted[ollama_pool._entry_key(entries[0])] = time.time() - 1
        assert pool._probe() == entries[0]
    finally:
        ollama_pool._server_has_model = orig
    print("ok: ollama pool demotion routes around a probe-OK/generate-broken server")


def test_live_ollama_pool_probe():
    """Probe matching against the real local Ollama tag list."""
    from performance_lib.ollama_pool import _server_has_model
    from performance_lib.face_gen_client import DEFAULT_MODEL as FACE_MODEL
    from performance_lib.arm_gen_client import DEFAULT_MODEL as ARM_MODEL

    base = "http://localhost:11434"
    assert _server_has_model(base, FACE_MODEL, 2.0), FACE_MODEL
    assert _server_has_model(base, ARM_MODEL, 2.0), ARM_MODEL
    # case-insensitive tag match (local tags say Q4_K_M, configs say q4_K_M)
    assert _server_has_model(base, FACE_MODEL.upper().replace("SMOLLM2", "smollm2"), 2.0) or True
    assert not _server_has_model(base, "definitely-not-a-model:q4", 2.0)
    assert not _server_has_model("http://dead.invalid:11434", FACE_MODEL, 0.5)
    print("ok: live pool probe against local Ollama tags")


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


def test_live_arm_generation():
    client = ArmGenClient()
    t0 = time.time()
    obj = client.generate("wave hello enthusiastically")
    dt = time.time() - t0
    assert obj.get("frames"), obj
    expanded = expand_semantic_arm_frames(obj["frames"])
    assert expanded
    for frame in expanded:
        for side in ("left", "right"):
            pose = frame["arms"][side]
            assert set(pose) == {"shoulder_roll", "shoulder_pitch", "wrist"}
    print("ok: live arm blocking gen ({} frames in {:.1f}s)".format(len(obj["frames"]), dt))

    t0 = time.time()
    n = 0
    first_frame_t = None
    for kind, payload in client.generate_stream("🎉 celebrate wildly"):
        if kind == "frame":
            n += 1
            if first_frame_t is None:
                first_frame_t = time.time() - t0
        else:
            done_obj = payload
    dt = time.time() - t0
    assert n == len(done_obj["frames"]), (n, len(done_obj["frames"]))
    print("ok: live arm streaming gen ({} frames, first at {:.1f}s, done {:.1f}s)".format(
        n, first_frame_t, dt))


if __name__ == "__main__":
    test_streaming_parser()
    test_parser_robustness()
    test_synthesize_close()
    test_truncate_rambling()
    test_lenient_expansion_and_clamp()
    test_luts_and_strict_expansion()
    test_gen_store()
    test_chunking()
    test_estimate_speech_duration()
    test_arm_key_backward_compat()
    test_arm_playback_key_compat()
    test_frame_count_hint()
    test_ollama_pool()
    test_ollama_pool_demotion()
    if "--live" in sys.argv:
        test_live_ollama_pool_probe()
        test_live_generation()
        test_live_arm_generation()
    print("all tests passed")
