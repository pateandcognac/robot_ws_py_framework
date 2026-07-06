#!/usr/bin/env python3
"""
Face Animator: resolves face-animation tracks for performance cues using the
tiny fine-tuned face model (via local Ollama), the saved-generation store,
and knowledge of the semantic LUT.

Listens to /performance/cue_announce (published by the performance director
the moment an utterance is split, before TTS synthesis) and to
/face/emoji_command (for silent gestures carrying a cue_id), resolves each
cue through a configurable policy cascade, and publishes resolved tracks on
/performance/face_track for the sequencer.

Policy cascade steps (ordered, comma-separated in params or lists in
per-call overrides):
- "lut":      if the cue's emoji exists in the master semantic LUT, stop and
              publish nothing -- the sequencer plays the LUT locally for free.
- "saved":    if the saved-generation store has an entry for this cue's
              generation text, publish a random saved take (source="saved").
- "generate": run the tiny model (source="generated"); optionally save the
              result to the store.

Default policies:
- TTS cues (~tts_policy, default "saved,generate"): always try for a bespoke
  text-shaped face; the sequencer's LUT cold-open covers us if we're late,
  so speech never blocks on generation.
- Command cues (~command_policy, default "lut,saved,generate"): emoji-only
  gestures use the LUT when it exists; anything else falls through to the
  model.

Per-call overrides ride in the cue-announce "performance" dict or directly
in the command payload: face_policy, temperature, seed, model, save.
"""

import json
import threading
import time
from collections import deque

import rospy
from std_msgs.msg import String

from performance_lib import luts
from performance_lib.face_gen_client import (
    DEFAULT_MODEL,
    DEFAULT_STORE_PATH,
    FaceGenClient,
    FaceGenError,
    GenStore,
)

VALID_STEPS = ("lut", "saved", "generate")


def parse_policy(value, default):
    """Accept 'a,b,c' or ['a','b','c']; keep only known steps."""
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [s.strip() for s in value.split(",")]
    steps = [s for s in value if s in VALID_STEPS]
    return steps if steps else list(default)


class GenJob:
    def __init__(self, cue_id, gen_text, emoji, policy, temperature, seed, model, save):
        self.cue_id = cue_id
        self.gen_text = gen_text
        self.emoji = emoji
        self.policy = policy
        self.temperature = temperature
        self.seed = seed
        self.model = model
        self.save = save


class FaceAnimatorNode:
    def __init__(self):
        rospy.init_node('face_animator_node', anonymous=False)

        self.default_model = rospy.get_param('~model', DEFAULT_MODEL)
        self.default_temperature = float(rospy.get_param('~temperature', 0.5))
        # seed <= 0 means "unseeded": every generation is a fresh take
        self.default_seed = int(rospy.get_param('~seed', 0))
        self.tts_policy = parse_policy(
            rospy.get_param('~tts_policy', 'saved,generate'), ['saved', 'generate'])
        self.command_policy = parse_policy(
            rospy.get_param('~command_policy', 'lut,saved,generate'),
            ['lut', 'saved', 'generate'])
        self.save_generations = bool(rospy.get_param('~save_generations', True))
        # Stream frames to the sequencer as they decode (first face motion in
        # ~1s instead of after the full 4-15s generation).
        self.use_streaming = bool(rospy.get_param('~stream', True))
        self.gen_timeout_s = float(rospy.get_param('~gen_timeout_s', 45.0))
        self.generate_even_if_late = bool(rospy.get_param('~generate_even_if_late', False))
        self.max_pending_jobs = int(rospy.get_param('~max_pending_jobs', 12))

        self.face_lut = luts.load_semantic_face_lut(
            rospy.get_param('~face_lut_dir', luts.DEFAULT_FACE_SEMANTIC_DIR))
        self.store = GenStore(rospy.get_param('~store_path', DEFAULT_STORE_PATH))
        self.clients = {}
        self.clients_lock = threading.Lock()

        self.track_pub = rospy.Publisher('/performance/face_track', String, queue_size=20)

        self.jobs = deque()
        self.jobs_cond = threading.Condition()
        self.done_cues = set()
        self.done_lock = threading.Lock()

        rospy.Subscriber('/performance/cue_announce', String, self.cue_announce_cb)
        rospy.Subscriber('/face/emoji_command', String, self.face_command_cb)
        rospy.Subscriber('/performance/cue_done', String, self.cue_done_cb)

        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()

        rospy.loginfo(
            "Face Animator online. model=%s temp=%.2f tts_policy=%s command_policy=%s "
            "save=%s store=%d entries, LUT=%d",
            self.default_model, self.default_temperature, self.tts_policy,
            self.command_policy, self.save_generations, len(self.store), len(self.face_lut))

    # ─── Inputs ──────────────────────────────────────────────────────

    def cue_announce_cb(self, msg):
        try:
            announce = json.loads(msg.data)
        except json.JSONDecodeError as e:
            rospy.logerr("Bad cue_announce payload: %s", e)
            return
        perf = announce.get("performance") or {}
        policy = parse_policy(perf.get("face_policy"), self.tts_policy)
        for cue in announce.get("cues", []):
            text = (cue.get("text") or "").strip()
            emoji = (cue.get("emoji") or "").strip()
            if not text and not emoji:
                continue
            self.enqueue(GenJob(
                cue_id=cue.get("cue_id", ""),
                gen_text=" ".join(x for x in (text, emoji) if x),
                emoji=emoji,
                policy=policy,
                temperature=float(perf.get("temperature", self.default_temperature)),
                seed=perf.get("seed", self.default_seed),
                model=perf.get("model", self.default_model),
                save=bool(perf.get("save", self.save_generations)),
            ))

    def face_command_cb(self, msg):
        """Silent gestures. Only cues carrying a cue_id involve the animator."""
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        cue_id = data.get("cue_id", "")
        if not cue_id:
            return  # plain LUT gesture; sequencer handles it alone
        text = (data.get("text") or "").strip()
        emoji = (data.get("emoji") or "").strip()
        if not text and not emoji:
            return
        self.enqueue(GenJob(
            cue_id=cue_id,
            gen_text=" ".join(x for x in (text, emoji) if x),
            emoji=emoji,
            policy=parse_policy(data.get("policy"), self.command_policy),
            temperature=float(data.get("temperature", self.default_temperature)),
            seed=data.get("seed", self.default_seed),
            model=data.get("model", self.default_model),
            save=bool(data.get("save", self.save_generations)),
        ))

    def cue_done_cb(self, msg):
        try:
            cue_id = json.loads(msg.data).get("cue_id")
        except json.JSONDecodeError:
            return
        if cue_id:
            with self.done_lock:
                self.done_cues.add(cue_id)
                if len(self.done_cues) > 512:
                    self.done_cues = set(list(self.done_cues)[-256:])

    # ─── Job handling ────────────────────────────────────────────────

    def enqueue(self, job):
        with self.jobs_cond:
            while len(self.jobs) >= self.max_pending_jobs:
                dropped = self.jobs.popleft()
                rospy.logwarn("Animator backlog full; dropping job for cue %s", dropped.cue_id)
            self.jobs.append(job)
            self.jobs_cond.notify()

    def worker_loop(self):
        while not rospy.is_shutdown():
            with self.jobs_cond:
                while not self.jobs and not rospy.is_shutdown():
                    self.jobs_cond.wait(timeout=0.5)
                if rospy.is_shutdown():
                    return
                job = self.jobs.popleft()
            try:
                self.process_job(job)
            except Exception as e:
                rospy.logerr("Animator job for cue %s failed: %s", job.cue_id, e)
                self.publish_track(job.cue_id, None, "error", "failed")

    def cue_is_done(self, cue_id):
        with self.done_lock:
            return cue_id in self.done_cues

    def get_client(self, model):
        with self.clients_lock:
            if model not in self.clients:
                self.clients[model] = FaceGenClient(model=model, timeout_s=self.gen_timeout_s)
            return self.clients[model]

    def process_job(self, job):
        for step in job.policy:
            if step == "lut":
                if job.emoji and job.emoji in self.face_lut:
                    # Sequencer plays the LUT locally; nothing to publish.
                    return
            elif step == "saved":
                animation = self.store.pick(job.gen_text)
                if animation:
                    rospy.loginfo("Cue %s: saved take for '%s'", job.cue_id, job.gen_text)
                    self.publish_track(job.cue_id, animation.get("frames"), "saved", "complete")
                    return
            elif step == "generate":
                if self.cue_is_done(job.cue_id) and not self.generate_even_if_late:
                    rospy.loginfo("Cue %s already played; skipping generation.", job.cue_id)
                    return
                if self.run_generation(job):
                    return
        # Cascade exhausted without a track: tell the sequencer to stop waiting.
        self.publish_track(job.cue_id, None, "none", "failed")

    def run_generation(self, job):
        client = self.get_client(job.model)
        seed = None if not job.seed or int(job.seed) <= 0 else int(job.seed)
        t0 = time.time()
        if not self.use_streaming:
            try:
                obj = client.generate(job.gen_text, temperature=job.temperature, seed=seed)
            except FaceGenError as e:
                rospy.logwarn("Cue %s: generation failed (%s)", job.cue_id, e)
                return False
            frames = obj.get("frames", [])
            rospy.loginfo("Cue %s: generated %d frames in %.1fs for '%s'",
                          job.cue_id, len(frames), time.time() - t0, job.gen_text)
            self.publish_track(job.cue_id, frames, "generated", "complete")
            if job.save:
                self.store.save(job.gen_text, obj, model=job.model,
                                temperature=job.temperature, seed=seed)
            return True

        # Streaming: push each frame to the sequencer the moment it decodes.
        frames_sent = 0
        obj = None
        try:
            for kind, payload in client.generate_stream(
                    job.gen_text, temperature=job.temperature, seed=seed):
                if kind == "frame":
                    frames_sent += 1
                    self.publish_track(job.cue_id, [payload], "generated", "partial",
                                       append=True)
                    if frames_sent == 1:
                        rospy.loginfo("Cue %s: first streamed frame at %.1fs for '%s'",
                                      job.cue_id, time.time() - t0, job.gen_text)
                else:
                    obj = payload
        except FaceGenError as e:
            rospy.logwarn("Cue %s: streaming generation failed after %d frames (%s)",
                          job.cue_id, frames_sent, e)
            if frames_sent:
                # Close out the partial track so the sequencer stops expecting more.
                self.publish_track(job.cue_id, None, "generated", "complete")
                return True
            return False

        frames = obj.get("frames", []) if obj else []
        rospy.loginfo("Cue %s: streamed %d frames in %.1fs for '%s'",
                      job.cue_id, len(frames), time.time() - t0, job.gen_text)
        # Final authoritative track (replaces the appended partials).
        self.publish_track(job.cue_id, frames, "generated", "complete")
        if job.save and obj:
            self.store.save(job.gen_text, obj, model=job.model,
                            temperature=job.temperature, seed=seed)
        return True

    def publish_track(self, cue_id, frames, source, status, append=False):
        payload = {
            "cue_id": cue_id,
            "source": source,
            "status": status,
        }
        if frames is not None:
            payload["frames"] = frames
        if append:
            payload["append"] = True
        self.track_pub.publish(String(data=json.dumps(payload, ensure_ascii=False)))


def main():
    try:
        node = FaceAnimatorNode()
        rospy.spin()
    except rospy.ROSInterruptException:
        pass


if __name__ == '__main__':
    main()
