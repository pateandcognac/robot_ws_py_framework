#!/usr/bin/env python3
"""
Face Animator: resolves face-animation tracks for performance cues using the
tiny fine-tuned face model (via local Ollama), the saved-generation store,
knowledge of the semantic LUT, and optional fuzzy semantic LUT lookup.

Listens to /performance/cue_announce (published by the performance director
the moment an utterance is split, before TTS synthesis) and to
/face/emoji_command (for silent gestures carrying a cue_id), resolves each
cue through a configurable policy cascade, and publishes resolved tracks on
/performance/face_track for the sequencer.

Policy cascade steps (ordered, comma-separated in params or lists in
per-call overrides):
- "lut":      pure-emoji cues whose emoji exists in the master semantic LUT
              stop here (a status="lut" signal releases any sequencer wait;
              the sequencer plays its own LUT copy). Cues carrying prose
              fall through so text can shape a generated face.
- "fuzzy":    query the Logos Chroma semantic LUT index for the closest
              master LUT entry, then publish that LUT entry's frames as a
              complete source="fuzzy" track.
- "saved":    publish a random take from the per-emoji rolling GenStore
              library (source="saved").
- "generate": run the tiny model (source="generated", streamed); emoji-keyed
              results are saved to the rolling library, plain-text results
              stay ephemeral. On failure, fall back to a LUT face (the cue's
              emoji, a recent emoji, or ~fallback_emoji).

Default policies:
- TTS cues (~tts_policy, default "fuzzy,lut,saved,generate"): prefer the
  Chroma-backed fuzzy master LUT match, then exact LUT, saved takes, and
  finally fresh generation.
- Command cues (~command_policy, default "generate,saved,fuzzy,lut"): silent
  gestures lead with fresh/saved bespoke motion, then fuzzy/exact LUT.

Per-call overrides ride in the cue-announce "performance" dict or directly
in the command payload: face_policy, temperature, seed, model, save.
"""

import json
import threading
import time
from collections import deque

import rospy
from std_msgs.msg import String

from performance_lib import fuzzy_lut, luts, ollama_pool
from performance_lib.chunking import find_emoji, strip_emoji
from performance_lib.face_gen_client import (
    DEFAULT_MODEL,
    DEFAULT_STORE_DIR,
    DEFAULT_STORE_CAP,
    DEFAULT_TIMEOUT_S,
    MAX_ACCEPTED_FRAMES,
    OLLAMA_URL,
    FaceGenClient,
    FaceGenError,
    GenStore,
)

VALID_STEPS = ("lut", "fuzzy", "saved", "generate")


def parse_policy(value, default):
    """Accept 'a,b,c' or ['a','b','c']; keep only known steps."""
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [s.strip() for s in value.split(",")]
    steps = [s for s in value if s in VALID_STEPS]
    return steps if steps else list(default)


class GenJob:
    def __init__(self, cue_id, gen_text, emoji, policy, temperature, seed, model, save,
                 has_plain_text=False):
        self.cue_id = cue_id
        self.gen_text = gen_text
        self.emoji = emoji
        self.policy = policy
        self.temperature = temperature
        self.seed = seed
        self.model = model
        self.save = save
        # True when the cue carries prose beyond emoji: the "lut" cascade
        # step only swallows pure-emoji cues, so text always gets a chance
        # to shape a generated face.
        self.has_plain_text = has_plain_text


class FaceAnimatorNode:
    def __init__(self):
        rospy.init_node('face_animator_node', anonymous=False)

        # Server/model selection: ~model pins the old single-server path
        # (back-compat override); otherwise the pool config decides, with
        # background probing across the LAN (see performance_lib/ollama_pool).
        model_override = rospy.get_param('~model', '')
        if model_override:
            self.pool = ollama_pool.pinned(OLLAMA_URL, model_override)
        else:
            self.pool = ollama_pool.load_pool(
                'face',
                rospy.get_param('~ollama_servers_config', ollama_pool.DEFAULT_CONFIG_PATH),
                default_url=OLLAMA_URL, default_model=DEFAULT_MODEL,
                log=rospy.loginfo)
        self.default_temperature = float(rospy.get_param('~temperature', 0.3))
        # seed <= 0 means "unseeded": every generation is a fresh take
        self.default_seed = int(rospy.get_param('~seed', 0))
        self.tts_policy = parse_policy(
            rospy.get_param('~tts_policy', 'fuzzy,lut,saved,generate'),
            ['fuzzy', 'lut', 'saved', 'generate'])
        self.command_policy = parse_policy(
            rospy.get_param('~command_policy', 'generate,saved,fuzzy,lut'),
            ['generate', 'saved', 'fuzzy', 'lut'])
        self.save_generations = bool(rospy.get_param('~save_generations', True))
        # Stream frames to the sequencer as they decode (first face motion in
        # ~1s instead of after the full 4-15s generation).
        self.use_streaming = bool(rospy.get_param('~stream', True))
        self.gen_timeout_s = float(rospy.get_param('~gen_timeout_s', DEFAULT_TIMEOUT_S))
        # Rambling cutoff: generations past this many frames get cut short
        # mid-stream (or truncated when blocking) and are never saved.
        self.max_accepted_frames = int(
            rospy.get_param('~max_accepted_frames', MAX_ACCEPTED_FRAMES))
        self.generate_even_if_late = bool(rospy.get_param('~generate_even_if_late', False))
        # A context dump can become many small cues at announce time. Jobs are
        # lightweight, so keep a generous FIFO backlog; 0/negative means
        # unlimited for bench testing.
        self.max_pending_jobs = int(rospy.get_param('~max_pending_jobs', 128))
        # Cue with no usable emoji whose generation fails: borrow a recently
        # performed emoji's LUT face, or this default, so the face never
        # just goes blank.
        self.fallback_emoji = rospy.get_param('~fallback_emoji', '💬')

        self.face_lut = luts.load_semantic_face_lut(
            rospy.get_param('~face_lut_dir', luts.DEFAULT_FACE_SEMANTIC_DIR))
        self.fuzzy = fuzzy_lut.FuzzyLutClient(
            server_url=rospy.get_param('~fuzzy_chroma_url', fuzzy_lut.DEFAULT_SERVER_URL),
            collection=rospy.get_param('~fuzzy_collection', fuzzy_lut.DEFAULT_COLLECTION),
            provider=rospy.get_param('~fuzzy_embedding_provider', fuzzy_lut.DEFAULT_PROVIDER),
            model=rospy.get_param('~fuzzy_embedding_model', fuzzy_lut.DEFAULT_MODEL),
            timeout_s=float(rospy.get_param('~fuzzy_timeout_s', fuzzy_lut.DEFAULT_TIMEOUT_S)),
            n_results=int(rospy.get_param('~fuzzy_n_results', 3)),
        )
        self.store = GenStore(
            rospy.get_param('~store_path', DEFAULT_STORE_DIR),
            cap_per_emoji=int(rospy.get_param('~store_cap', DEFAULT_STORE_CAP)))
        self.recent_emojis = deque(maxlen=8)
        self.clients = {}
        self.clients_lock = threading.Lock()

        self.track_pub = rospy.Publisher('/performance/face_track', String, queue_size=100)

        self.jobs = deque()
        self.jobs_cond = threading.Condition()
        self.done_cues = set()
        self.done_lock = threading.Lock()

        rospy.Subscriber('/performance/cue_announce', String, self.cue_announce_cb)
        rospy.Subscriber('/face/emoji_command', String, self.face_command_cb)
        rospy.Subscriber('/performance/cue_done', String, self.cue_done_cb)

        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()

        pool_url, pool_model = self.pool.current()
        rospy.loginfo(
            "Face Animator online. server=%s model=%s temp=%.2f tts_policy=%s "
            "command_policy=%s save=%s store=%d entries, LUT=%d",
            pool_url, pool_model, self.default_temperature, self.tts_policy,
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
                model=perf.get("model") or "",  # "" -> pool's current choice
                save=bool(perf.get("save", self.save_generations)),
                has_plain_text=bool(text),
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
        # Gestures arrive as one merged string: emoji, prose, or both.
        # Resolve the LUT-relevant emoji ourselves.
        if not emoji:
            emoji = find_emoji(text, self.face_lut.keys()) or ""
        gen_text = text if emoji in text else " ".join(x for x in (text, emoji) if x)
        has_plain_text = bool(strip_emoji(text, self.face_lut.keys()))
        policy = parse_policy(data.get("policy"), self.command_policy)
        # Fast path: a pure-emoji LUT gesture needs no worker time -- release
        # the sequencer's wait immediately, even if a generation is in flight.
        if policy and policy[0] == "lut" and emoji and not has_plain_text \
                and emoji in self.face_lut:
            self.publish_track(cue_id, None, "lut", "lut")
            return
        self.enqueue(GenJob(
            cue_id=cue_id,
            gen_text=gen_text,
            emoji=emoji,
            policy=policy,
            temperature=float(data.get("temperature", self.default_temperature)),
            seed=data.get("seed", self.default_seed),
            model=data.get("model") or "",  # "" -> pool's current choice
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
        overflow_job = None
        with self.jobs_cond:
            if self.max_pending_jobs > 0 and len(self.jobs) >= self.max_pending_jobs:
                # Preserve already-queued FIFO work: older cues are nearer to
                # playback. The rejected cue gets an immediate fallback below
                # so the sequencer never waits on a result that will not come.
                overflow_job = job
            else:
                self.jobs.append(job)
                self.jobs_cond.notify()
        if overflow_job is not None:
            rospy.logwarn(
                "Face animator backlog full (%d jobs); using fallback for cue %s",
                self.max_pending_jobs, overflow_job.cue_id)
            self.publish_fallback(overflow_job)

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

    def get_client(self, model_override=""):
        """
        Client for the pool's current (url, model) -- or the per-job model
        override on the same server. Cached per (url, model) pair so a pool
        server switch transparently gets a fresh client.
        """
        url, model = self.pool.current()
        if model_override:
            model = model_override
        key = (url, model)
        with self.clients_lock:
            if key not in self.clients:
                self.clients[key] = FaceGenClient(
                    model=model, url=url, timeout_s=self.gen_timeout_s,
                    max_frames=self.max_accepted_frames)
            return self.clients[key]

    def process_job(self, job):
        if job.emoji:
            self.recent_emojis.append(job.emoji)
        for step in job.policy:
            if step == "lut":
                # Only pure-emoji cues stop here; prose deserves a chance to
                # shape a generated face further down the cascade.
                if job.emoji and not job.has_plain_text and job.emoji in self.face_lut:
                    # Sequencer plays its LUT locally; just release its wait.
                    self.publish_track(job.cue_id, None, "lut", "lut")
                    return
            elif step == "saved":
                animation = self.store.pick(job.emoji)
                if animation:
                    rospy.loginfo("Cue %s: saved take for %s", job.cue_id, job.emoji)
                    self.publish_track(job.cue_id, animation.get("frames"), "saved", "complete")
                    return
            elif step == "fuzzy":
                if self.resolve_fuzzy(job):
                    return
            elif step == "generate":
                if self.cue_is_done(job.cue_id) and not self.generate_even_if_late:
                    rospy.loginfo("Cue %s already played; skipping generation.", job.cue_id)
                    return
                if self.run_generation(job):
                    return
        self.publish_fallback(job)

    def publish_fallback(self, job):
        """
        Cascade exhausted (usually a failed generation for a text-only cue):
        borrow a LUT face -- the cue's own emoji, a recently performed one,
        or the configured default -- so the face never goes blank.
        """
        for emoji in [job.emoji] + list(reversed(self.recent_emojis)) + [self.fallback_emoji]:
            if emoji and emoji in self.face_lut:
                rospy.logwarn("Cue %s: falling back to LUT face %s for '%s'",
                              job.cue_id, emoji, job.gen_text)
                self.publish_track(
                    job.cue_id, self.face_lut[emoji]["frames"], "lut_fallback", "complete")
                return
        self.publish_track(job.cue_id, None, "none", "failed")

    def resolve_fuzzy(self, job):
        try:
            match = self.fuzzy.query(job.gen_text, "face")
        except fuzzy_lut.FuzzyLutError as e:
            rospy.logwarn_throttle(5, "Fuzzy face LUT lookup unavailable: %s", e)
            return False
        if not match:
            return False
        entry = self.face_lut.get(match.emoji)
        if not entry:
            rospy.logwarn("Cue %s: fuzzy face matched %s but it is not in the local LUT",
                          job.cue_id, match.emoji)
            return False
        rospy.loginfo("Cue %s: fuzzy face matched %s via %s (distance=%s)",
                      job.cue_id, match.emoji, match.document_id, match.distance)
        self.publish_track(
            job.cue_id,
            entry.get("frames"),
            "fuzzy",
            "complete",
            matched_emoji=match.emoji,
            match_distance=match.distance,
            match_id=match.document_id,
        )
        return True

    def run_generation(self, job):
        client = self.get_client(job.model)
        seed = None if not job.seed or int(job.seed) <= 0 else int(job.seed)
        t0 = time.time()
        if not self.use_streaming:
            try:
                obj = client.generate(job.gen_text, temperature=job.temperature, seed=seed)
            except FaceGenError as e:
                rospy.logwarn("Cue %s: generation failed (%s)", job.cue_id, e)
                self.pool.report_failure()
                return False
            self.pool.report_success()
            frames = obj.get("frames", [])
            rospy.loginfo("Cue %s: generated %d frames in %.1fs for '%s'",
                          job.cue_id, len(frames), time.time() - t0, job.gen_text)
            self.publish_track(job.cue_id, frames, "generated", "complete")
            self.maybe_save(job, obj, seed, client.model)
            return True

        # Streaming: push each frame to the sequencer the moment it decodes.
        frames_sent = 0
        obj = None
        aborted_late = False
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
                    if job.cue_id and self.cue_is_done(job.cue_id) \
                            and not self.generate_even_if_late:
                        # Cue already finished playing: every further frame is
                        # inference spent on output that will be dropped.
                        aborted_late = True
                        break
                else:
                    obj = payload
        except FaceGenError as e:
            rospy.logwarn("Cue %s: streaming generation failed after %d frames (%s)",
                          job.cue_id, frames_sent, e)
            self.pool.report_failure()
            if frames_sent:
                # Close out the partial track so the sequencer stops expecting more.
                self.publish_track(job.cue_id, None, "generated", "complete")
                return True
            return False
        self.pool.report_success()

        if aborted_late:
            rospy.loginfo("Cue %s: already played; aborted generation mid-stream "
                          "after %d frames (%.1fs)", job.cue_id, frames_sent,
                          time.time() - t0)
            self.publish_track(job.cue_id, None, "generated", "complete")
            return True

        frames = obj.get("frames", []) if obj else []
        rospy.loginfo("Cue %s: streamed %d frames in %.1fs for '%s'%s",
                      job.cue_id, len(frames), time.time() - t0, job.gen_text,
                      " [truncated]" if obj and obj.get("_truncated") else "")
        # Final authoritative track (replaces the appended partials).
        self.publish_track(job.cue_id, frames, "generated", "complete")
        if obj:
            self.maybe_save(job, obj, seed, client.model)
        return True

    def maybe_save(self, job, obj, seed, model):
        """
        Persist only emoji-keyed, non-truncated takes. Truncated generations
        (past the rambling cutoff or a deadline) are played once and
        discarded -- never added to the rolling library.
        """
        if obj.get("_truncated"):
            return
        if job.save and job.emoji:
            animation = {k: v for k, v in obj.items() if not k.startswith("_")}
            self.store.save(job.emoji, animation, model=model, text=job.gen_text,
                            temperature=job.temperature, seed=seed)

    def publish_track(self, cue_id, frames, source, status, append=False, **extra):
        payload = {
            "cue_id": cue_id,
            "source": source,
            "status": status,
        }
        payload.update({k: v for k, v in extra.items() if v is not None})
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
