#!/usr/bin/env python3
"""
Arm Animator: resolves arm-animation tracks for performance cues using the
tiny fine-tuned arm model (via local Ollama), the saved-generation store,
and the semantic arm LUT. Mirrors face_animator_node.py; see that module's
docstring for the general shape of this pattern.

Listens to /performance/cue_announce (published by the performance director
before TTS synthesis) and to /arm/emoji_command (for cues carrying a
cue_id), resolves each cue through a configurable policy cascade, and
publishes resolved tracks on /performance/arm_track for the sequencer.

Policy cascade steps (ordered, comma-separated in params or lists in
per-call overrides):
- "generate": run the tiny arm model (source="generated", streamed by
              default). Generations are capped at
              arm_gen_client.MAX_ACCEPTED_FRAMES frames -- past that point
              the model is presumed to be rambling rather than performing
              deliberate choreography, so the stream is cut short (or the
              blocking result truncated) and NEVER saved to the rolling
              library. On outright failure, fall through to the next step.
- "saved":    publish a random take from the per-emoji rolling GenStore
              library (source="saved").
- "lut":      pure-emoji cues whose emoji exists in the semantic arm LUT
              stop here (a status="lut" signal releases any sequencer wait;
              the sequencer plays its own LUT copy). Cues carrying prose
              fall through (there's nothing further to fall through to by
              default, so cascade exhaustion here still yields a fallback
              LUT face -- err, arm pose).

Default cascade for both TTS and command cues: "generate,saved,lut" (fresh
bespoke arm motion first; the sequencer's LUT cold-open covers us if we're
late, so speech never blocks on generation). Tweakable globally via ROS
params (~tts_policy, ~command_policy) and per-call via the cue-announce
"performance" dict's "arm_policy" key or the command payload's "policy" key.
"""

import json
import threading
import time
from collections import deque

import rospy
from std_msgs.msg import String

from performance_lib import luts
from performance_lib.chunking import find_emoji, strip_emoji
from performance_lib.arm_gen_client import (
    DEFAULT_MODEL,
    DEFAULT_STORE_DIR,
    DEFAULT_STORE_CAP,
    DEFAULT_TIMEOUT_S,
    ArmGenClient,
    ArmGenError,
    GenStore,
    frame_count_hint,
)

VALID_STEPS = ("generate", "saved", "lut")


def parse_policy(value, default):
    """Accept 'a,b,c' or ['a','b','c']; keep only known steps."""
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [s.strip() for s in value.split(",")]
    steps = [s for s in value if s in VALID_STEPS]
    return steps if steps else list(default)


class GenJob:
    def __init__(self, cue_id, gen_text, emoji, policy, n_frames, temperature, seed, model, save,
                 has_plain_text=False):
        self.cue_id = cue_id
        self.gen_text = gen_text
        self.emoji = emoji
        self.policy = policy
        self.n_frames = n_frames
        self.temperature = temperature
        self.seed = seed
        self.model = model
        self.save = save
        # True when the cue carries prose beyond emoji: the "lut" cascade
        # step only swallows pure-emoji cues, so text always gets a chance
        # to shape a generated motion.
        self.has_plain_text = has_plain_text


class ArmAnimatorNode:
    def __init__(self):
        rospy.init_node('arm_animator_node', anonymous=False)

        self.default_model = rospy.get_param('~model', DEFAULT_MODEL)
        self.default_temperature = float(rospy.get_param('~temperature', 0.3))
        # seed <= 0 means "unseeded": every generation is a fresh take
        self.default_seed = int(rospy.get_param('~seed', 0))
        self.tts_policy = parse_policy(
            rospy.get_param('~tts_policy', 'generate,saved,lut'),
            ['generate', 'saved', 'lut'])
        self.command_policy = parse_policy(
            rospy.get_param('~command_policy', 'generate,saved,lut'),
            ['generate', 'saved', 'lut'])
        self.save_generations = bool(rospy.get_param('~save_generations', True))
        # Stream frames to the sequencer as they decode.
        self.use_streaming = bool(rospy.get_param('~stream', True))
        self.gen_timeout_s = float(rospy.get_param('~gen_timeout_s', DEFAULT_TIMEOUT_S))
        self.generate_even_if_late = bool(rospy.get_param('~generate_even_if_late', False))
        self.max_pending_jobs = int(rospy.get_param('~max_pending_jobs', 12))
        # Cue with no usable emoji whose generation fails: borrow a recently
        # performed emoji's LUT pose, or this default, so the arms never
        # just freeze mid-cue.
        self.fallback_emoji = rospy.get_param('~fallback_emoji', '🧍')

        self.arm_lut = luts.load_semantic_arm_lut(
            rospy.get_param('~arm_lut_dir', luts.DEFAULT_ARM_SEMANTIC_DIR))
        self.store = GenStore(
            rospy.get_param('~store_path', DEFAULT_STORE_DIR),
            cap_per_emoji=int(rospy.get_param('~store_cap', DEFAULT_STORE_CAP)))
        self.recent_emojis = deque(maxlen=8)
        self.clients = {}
        self.clients_lock = threading.Lock()

        self.track_pub = rospy.Publisher('/performance/arm_track', String, queue_size=20)

        self.jobs = deque()
        self.jobs_cond = threading.Condition()
        self.done_cues = set()
        self.done_lock = threading.Lock()

        rospy.Subscriber('/performance/cue_announce', String, self.cue_announce_cb)
        rospy.Subscriber('/arm/emoji_command', String, self.arm_command_cb)
        rospy.Subscriber('/performance/cue_done', String, self.cue_done_cb)

        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()

        rospy.loginfo(
            "Arm Animator online. model=%s temp=%.2f tts_policy=%s command_policy=%s "
            "save=%s store=%d entries, LUT=%d",
            self.default_model, self.default_temperature, self.tts_policy,
            self.command_policy, self.save_generations, len(self.store), len(self.arm_lut))

    # ─── Inputs ──────────────────────────────────────────────────────

    def cue_announce_cb(self, msg):
        try:
            announce = json.loads(msg.data)
        except json.JSONDecodeError as e:
            rospy.logerr("Bad cue_announce payload: %s", e)
            return
        perf = announce.get("performance") or {}
        policy = parse_policy(perf.get("arm_policy"), self.tts_policy)
        for cue in announce.get("cues", []):
            text = (cue.get("text") or "").strip()
            emoji = (cue.get("emoji") or "").strip()
            if not text and not emoji:
                continue
            gen_text = " ".join(x for x in (text, emoji) if x)
            self.enqueue(GenJob(
                cue_id=cue.get("cue_id", ""),
                gen_text=gen_text,
                emoji=emoji,
                policy=policy,
                n_frames=perf.get("arm_n_frames") or frame_count_hint(gen_text),
                temperature=float(perf.get("temperature", self.default_temperature)),
                seed=perf.get("seed", self.default_seed),
                model=perf.get("model", self.default_model),
                save=bool(perf.get("save", self.save_generations)),
                has_plain_text=bool(text),
            ))

    def arm_command_cb(self, msg):
        """
        Arm cue. Only cues carrying a cue_id involve the animator; a plain
        {"emoji"/"text", "duration"} payload with no cue_id is the legacy
        fast path the sequencer already handles alone (straight LUT lookup,
        no generation, no waiting) -- unaffected by this node.
        """
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        cue_id = data.get("cue_id", "")
        if not cue_id:
            return  # plain LUT command; sequencer handles it alone
        text = (data.get("text") or "").strip()
        emoji = (data.get("emoji") or "").strip()
        if not text and not emoji:
            return
        # Commands arrive as one merged string: emoji, prose, or both.
        if not emoji:
            emoji = find_emoji(text, self.arm_lut.keys()) or ""
        gen_text = text if emoji in text else " ".join(x for x in (text, emoji) if x)
        has_plain_text = bool(strip_emoji(text, self.arm_lut.keys()))
        policy = parse_policy(data.get("policy"), self.command_policy)
        # Fast path: a pure-emoji LUT command needs no worker time -- release
        # the sequencer's wait immediately, even if a generation is in flight.
        if policy and policy[0] == "lut" and emoji and not has_plain_text \
                and emoji in self.arm_lut:
            self.publish_track(cue_id, None, "lut", "lut")
            return
        self.enqueue(GenJob(
            cue_id=cue_id,
            gen_text=gen_text,
            emoji=emoji,
            policy=policy,
            n_frames=data.get("n_frames") or frame_count_hint(gen_text),
            temperature=float(data.get("temperature", self.default_temperature)),
            seed=data.get("seed", self.default_seed),
            model=data.get("model", self.default_model),
            save=bool(data.get("save", self.save_generations)),
            has_plain_text=has_plain_text,
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
                rospy.logerr("Arm animator job for cue %s failed: %s", job.cue_id, e)
                self.publish_track(job.cue_id, None, "error", "failed")

    def cue_is_done(self, cue_id):
        with self.done_lock:
            return cue_id in self.done_cues

    def get_client(self, model):
        with self.clients_lock:
            if model not in self.clients:
                self.clients[model] = ArmGenClient(model=model, timeout_s=self.gen_timeout_s)
            return self.clients[model]

    def process_job(self, job):
        if job.emoji:
            self.recent_emojis.append(job.emoji)
        for step in job.policy:
            if step == "generate":
                if self.cue_is_done(job.cue_id) and not self.generate_even_if_late:
                    rospy.loginfo("Cue %s already played; skipping generation.", job.cue_id)
                    return
                if self.run_generation(job):
                    return
            elif step == "saved":
                animation = self.store.pick(job.emoji)
                if animation:
                    rospy.loginfo("Cue %s: saved arm take for %s", job.cue_id, job.emoji)
                    self.publish_track(job.cue_id, animation.get("frames"), "saved", "complete")
                    return
            elif step == "lut":
                # Only pure-emoji cues stop here; prose deserves a chance to
                # shape a generated motion further up the cascade already.
                if job.emoji and not job.has_plain_text and job.emoji in self.arm_lut:
                    self.publish_track(job.cue_id, None, "lut", "lut")
                    return
        self.publish_fallback(job)

    def publish_fallback(self, job):
        """
        Cascade exhausted (usually a failed generation for a text-only cue):
        borrow a LUT pose -- the cue's own emoji, a recently performed one,
        or the configured default -- so the arms never just freeze.
        """
        for emoji in [job.emoji] + list(reversed(self.recent_emojis)) + [self.fallback_emoji]:
            if emoji and emoji in self.arm_lut:
                rospy.logwarn("Cue %s: falling back to LUT arm pose %s for '%s'",
                              job.cue_id, emoji, job.gen_text)
                self.publish_track(
                    job.cue_id, self.arm_lut[emoji]["frames"], "lut_fallback", "complete")
                return
        self.publish_track(job.cue_id, None, "none", "failed")

    def run_generation(self, job):
        client = self.get_client(job.model)
        seed = None if not job.seed or int(job.seed) <= 0 else int(job.seed)
        t0 = time.time()
        if not self.use_streaming:
            try:
                obj = client.generate(job.gen_text, n_frames=job.n_frames,
                                      temperature=job.temperature, seed=seed)
            except ArmGenError as e:
                rospy.logwarn("Cue %s: arm generation failed (%s)", job.cue_id, e)
                return False
            frames = obj.get("frames", [])
            rospy.loginfo("Cue %s: generated %d arm frames in %.1fs for '%s'%s",
                          job.cue_id, len(frames), time.time() - t0, job.gen_text,
                          " [truncated]" if obj.get("_truncated") else "")
            self.publish_track(job.cue_id, frames, "generated", "complete")
            self.maybe_save(job, obj, seed)
            return True

        # Streaming: push each frame to the sequencer the moment it decodes.
        frames_sent = 0
        obj = None
        try:
            for kind, payload in client.generate_stream(
                    job.gen_text, n_frames=job.n_frames, temperature=job.temperature, seed=seed):
                if kind == "frame":
                    frames_sent += 1
                    self.publish_track(job.cue_id, [payload], "generated", "partial",
                                       append=True)
                    if frames_sent == 1:
                        rospy.loginfo("Cue %s: first streamed arm frame at %.1fs for '%s'",
                                      job.cue_id, time.time() - t0, job.gen_text)
                else:
                    obj = payload
        except ArmGenError as e:
            rospy.logwarn("Cue %s: streaming arm generation failed after %d frames (%s)",
                          job.cue_id, frames_sent, e)
            if frames_sent:
                self.publish_track(job.cue_id, None, "generated", "complete")
                return True
            return False

        frames = obj.get("frames", []) if obj else []
        rospy.loginfo("Cue %s: streamed %d arm frames in %.1fs for '%s'%s",
                      job.cue_id, len(frames), time.time() - t0, job.gen_text,
                      " [truncated]" if obj and obj.get("_truncated") else "")
        # Final authoritative track (replaces the appended partials).
        self.publish_track(job.cue_id, frames, "generated", "complete")
        if obj:
            self.maybe_save(job, obj, seed)
        return True

    def maybe_save(self, job, obj, seed):
        """
        Persist only emoji-keyed, non-truncated takes. Truncated generations
        (past MAX_ACCEPTED_FRAMES) are presumed to be the model rambling
        rather than deliberate choreography, so they're played once and
        discarded -- never added to the rolling library.
        """
        if obj.get("_truncated"):
            return
        if job.save and job.emoji:
            self.store.save(job.emoji, obj, model=job.model, text=job.gen_text,
                            n_frames_requested=job.n_frames,
                            temperature=job.temperature, seed=seed)

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
        node = ArmAnimatorNode()
        rospy.spin()
    except rospy.ROSInterruptException:
        pass


if __name__ == '__main__':
    main()
