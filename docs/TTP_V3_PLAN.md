# TTP v3 — Design & Implementation Plan

**Status: PLAN — not yet implemented.** Written 2026-07-06 at the end of the
TTP v2.x build session, as the handoff brief for a fresh session to execute.
Read `docs/TTP_V2.md` first for the current (v2) architecture; this doc only
covers what *changes*.

## The one-sentence philosophy

Go all in on **low-latency, loose-goosey, best-attempt synced playback** of
asynchronously generated TTS + face + arm tracks, all of which have wildly
variable latency, duration, and size — fudge them into the same rough
timeframe elegantly rather than blocking anything on anything else.

## What v2 already does (don't rebuild)

- Director (`tts_action_server.py`) splits at emoji + sentence/clause,
  announces all cues on `/performance/cue_announce` *before* synthesis
  (with per-cue `est_duration` from `chunking.estimate_speech_duration()`),
  then synthesizes chunks **sequentially and blocking**, publishing each
  finished chunk as `SpeechData` on `/face/tts_chunk`.
- Face + arm animator nodes resolve cues through generate/saved/lut
  cascades and stream frames to `/performance/face_track` /
  `/performance/arm_track`. Streaming works; first frame ~0.7–2.5s under
  normal load.
- Sequencer plays one TTS cue at a time (audio = master clock). v2 has a
  LUT "cold-open" with mid-cue track join before `~switch_threshold`
  (0.6); arms on a parallel thread. `sync=True` (`performance.sync`) adds
  a bounded first-frame wait (`~sync_wait_s`, 4s) per cue.
  **DECIDED 2026-07-06: v3 drops the sequencer-side cold-open/LUT-switch
  logic entirely.** Live generation lands in 1–2s; the effort goes into
  elegantly fudging playback of the live-generated track instead. The LUT
  survives as the *animator cascade* fallback (generate → saved → lut
  still produces a track when generation fails), so the robot never
  depends on a generation succeeding — the sequencer just no longer
  second-guesses the track source mid-cue.
- Arm rambling cutoff at `MAX_ACCEPTED_FRAMES=7` (stream closed early,
  truncated takes never saved).
- Rolling per-emoji GenStores (`animations/face_generated/`,
  `animations/arm_generated/`, cap 5), semantic LUTs for both channels.

## What v2 explicitly does NOT do (v3's core work)

The sequencer never sees a cue until its audio is **fully synthesized**.
The director's per-chunk synthesis loop is sequential and blocking, so:

- A slow engine (kokoro) gates every cue on synthesis latency even when
  face/arm frames are ready and waiting.
- Nothing can move until audio exists, so "face pose appears while speech
  is still rendering" is impossible today.
- Captions (`face_hud_bridge_node.py` subscribes to `/face/tts_chunk`)
  fire at **synthesis completion**, not playback start — synthesis runs
  ahead of playback, so captions visibly lead the audio. This is the
  known caption/speech/face desync bug; it falls out of the same
  restructure for free (see Workstream D).

---

## Workstream A — Distributed Ollama (config + client changes)

Ollama is plain HTTP, so distributing inference across the LAN is trivial
compared to distributing ROS. Per **model role** (face, arms), allow an
ordered preference list of servers, each entry naming the server URL and
the model tag (so the beefy machine can run q8_0 while a Pi runs q4_K_M
and the robot itself is the last-resort q4_K_M).

### Config

New JSON file, path via ROS param `~ollama_servers_config` on both
animator nodes; default `/home/robot/robot_ws/config/ollama_servers.json`
(create `config/` — it doesn't exist in robot_ws yet). **Mark's call:
commit the real config with his LAN hostnames** — no example/gitignore
dance needed.

```jsonc
{
  "face": [
    {"url": "http://beefy.local:11434", "model": "smollm2-135m-face-lora-34k:q8_0"},
    {"url": "http://pi4.local:11434",   "model": "smollm2-135m-face-lora-34k:q4_K_M"},
    {"url": "http://localhost:11434",   "model": "smollm2-135m-face-lora-34k:q4_K_M"}
  ],
  "arms": [
    {"url": "http://beefy.local:11434", "model": "smollm2-135m-arm-lora-38k:q8_0"},
    {"url": "http://localhost:11434",   "model": "smollm2-135m-arm-lora-38k:q4_K_M"}
  ],
  "probe_timeout_s": 2.0
}
```

### Probing — background thread, never on the hot path (Mark's refined call)

Mark's rule: periodic probing is fine **as long as it never adds latency
to a generation call** — no blocking timeout waits for a Pi that's turned
off. So:

- At animator startup: walk the list in order; first server that responds
  to `GET /api/tags` within `probe_timeout_s` **and** has the named model
  in its tag list wins. Log the choice loudly.
- A daemon thread re-runs the same probe on a slow period
  (`~probe_interval_s`, default ~60s) and atomically swaps the cached
  `(url, model)` choice. Generation calls only ever read the cache — zero
  added latency. This gives both directions for free: a dead server gets
  dropped within a minute, and when the beefy machine comes back up the
  node upgrades back to it. Log on every switch.
- Belt-and-suspenders: after 3 consecutive `ArmGenError`/`FaceGenError`
  transport failures, trigger an immediate out-of-band re-probe instead
  of waiting for the next tick (still off the request thread).
- A generation failure still falls through the cascade (saved → lut), so
  the robot degrades gracefully regardless.

### Code changes

- `face_gen_client.py` / `arm_gen_client.py`: constructor already takes
  `url` + `model` — add a small shared helper (new
  `performance_lib/ollama_pool.py`?) that loads the config, probes, and
  returns `(url, model)` for a role. Keep the existing single-server
  defaults as the zero-config fallback so nothing breaks when the config
  file is absent.
- Animator nodes: resolve `(url, model)` at startup via the helper;
  `~model` ROS param becomes an override that skips the pool entirely
  (back-compat).
- If different quants are behind different URLs, the GenStore `model`
  metadata field already records which model produced each take — no
  change needed there.

Future hook (brainstormed, noted, not built): battery level / CPU temp as an input to
policy selection (e.g. hot CPU → prefer `saved,lut` over `generate`).
Design the pool helper so a "server score" function could slot in later,
but do not implement sensors now.

---

## Workstream B — Director restructure: concurrent synthesis

`tts_action_server.py::_process_goal` becomes: announce cues (unchanged),
then **fire all chunk syntheses concurrently** (ThreadPoolExecutor, ~2-3
workers — the Larynx server is one box; don't stampede it), publishing
each `SpeechData` **in cue order as it completes** (chunk N may finish
before N-1; hold it until N-1 is published so the sequencer's queue stays
ordered). Feedback publishing stays per-chunk in order.

Key point: the sequencer should learn about a cue's *existence and
estimated timing* from `cue_announce` (it already does, for sync mode)
and receive audio *whenever it lands*. That means:

- `SpeechData` keeps its exact wire shape (back-compat: the old
  face_hud_bridge and interface_helper nodes still subscribe).
- The action result semantics stay the same (success after all chunks
  sent).
- espeak/festival will make this look like a no-op (synthesis is instant);
  kokoro is where it pays — chunk 2's synthesis overlaps chunk 1's
  playback *and* chunk 1's generation head-start grows.

Cancellation: the existing status check between chunks becomes a check
before dispatching each synthesis + cooperative shutdown of the pool.

---

## Workstream C — Sequencer restructure: the provisional cue timeline

This is the heart of v3. Today `process_cue()` is strictly: pop cue (which
already has audio) → maybe sync-wait → play audio + face for `duration` →
done. Replace with a **per-cue state machine** driven by whatever arrives
first:

### Cue lifecycle

1. **Announced** (`cue_announce`): sequencer creates a `PendingCue` with
   `est_duration` (already published per-cue by the director), text,
   emoji, sync flag. Face/arm generation is already racing (animators also
   got the announce).
2. **Pre-speech staging**: when this cue is next in line to play and its
   audio hasn't arrived yet, the sequencer may **pose the first face frame
   and first arm frame** (from whichever source the cascade has already
   delivered — streamed track frame 1, saved take frame 1, or LUT frame 1).
   *Only the first frame* — a held pose, like an actor hitting their mark
   before the curtain. **No further animatronic playback until speech
   audio actually starts playing**. This makes
   waiting-for-kokoro look intentional instead of dead.
3. **Audio lands** (`SpeechData` arrives): real `duration` replaces
   `est_duration`. Audio starts, face/arm timelines start pacing.
4. **Live timeline fudging** during playback: the pacing math currently
   estimates `est_remaining` frames from a fixed guess (`max(len+2, 6)`).
   Generalize: each time new frames arrive (or `status:"complete"` lands),
   recompute frame pacing over the *remaining* cue time — i.e. the
   timeline continuously re-stretches in place based on new information.
   This is mostly refactoring the existing loop bodies of
   `play_face_for_cue` / `play_arms_for_cue` into one shared
   `TrackPlayback` helper (they're already near-identical twins) with the
   re-estimation done every iteration instead of per-frame-guess.
5. **Done**: unchanged (`cue_done`, pops, mark_done). Fix the known minor
   race while in here: `process_cue` should not `pop`/`mark_done` the arm
   track until the arm channel confirms it's finished with that cue (a
   simple per-cue `threading.Event` the arm channel sets; see the
   ttp-v2-pipeline memory note).

### Timing rules (the "fudge contract")

- Audio remains the master clock **once it exists**; `est_duration` is
  the provisional clock before that.
- If face/arm first-frames beat audio: hold pose (rule 2).
- If audio beats first-frames: play the track from frame 1 whenever it
  arrives, re-stretched over the remaining cue time (no LUT cold-open —
  the animator cascade already substitutes LUT frames as the track when
  generation fails, so there is always *a* track eventually).
- A cue whose audio is late by more than `~audio_wait_s` (new param,
  suggest ~10s) plays out silently on `est_duration` (face/arm only) and
  logs a warning — don't wedge the queue behind a hung synthesis.
- `sync=True` keeps its current meaning (bounded first-frame wait) but
  under v3 it will rarely need to actually wait, since pre-speech staging
  gives generation the synthesis window as a head start even on fast
  engines... **only when audio is late.** On espeak, audio is instant, so
  sync's bounded wait is still what buys generation time. Keep both.

### What "no other animatronic playback until speech starts" covers

First-frame face + arm staging: allowed pre-audio. Frame 2+: gated on
audio start. Gestures (`emoji_command`, no audio at all): unaffected,
they have no speech to wait for.

---

## Workstream D — Playback-time events (fixes captions)

New topic: `/performance/cue_playing` (String JSON), published by the
sequencer **at the moment a cue's audio actually starts** (or at silent
cue start): `{cue_id, text, emoji, duration, index, total}`.

- `face_hud_bridge_node.py` switches its caption source from
  `/face/tts_chunk` to `/performance/cue_playing` → captions align with
  the actual audio, fixing the observed caption/speech desync.
- Check `interface_helper_node.py` too — same subscription, likely wants
  the same switch (verify what it uses the text for first).
- Keep publishing `/face/tts_chunk` unchanged for anything else listening.
- **`SpeakTask` playhead upgrade (Mark: yes! — verified 2026-07-06).**
  `SpeakTask` in the Logos workspace's `emote.py` *dead-reckons* playback:
  `_feedback_cb` anchors `_playback_start_time` to the first chunk's
  **synthesis completion** (+0.1s) and marches a wall-clock playhead
  through chunk durations assuming instant, gapless playback. Every
  sequencer-side delay (sync first-frame waits up to 4s/cue, track waits,
  and v3's pre-speech staging / silent playout) makes the reckoned
  playhead drift ahead of actual audio, and drift accumulates across cues.
  Fix — **no action-semantics change needed**: `emote.py` subscribes to
  `/performance/cue_playing` and re-anchors the playhead per cue from real
  playback events (matching on cue index/text), keeping dead reckoning as
  the fallback when no event arrives (sequencer not running). Pure
  workspace-side change; `current_emoji()`/`current_text()`/`progress()`
  all get honest for free.

---

## Workstream E — Robust streaming JSON + arm cap at source

1. **Cap at source, cleanly.** Today the arm stream cutoff closes the HTTP
   connection at frame 7 and rebuilds `{emoji, frames}` from the collected
   frames + regex-recovered emoji. Formalize: give `StreamingFrameParser`
   a `synthesize_close()` method that appends the minimal `]}`/quote
   fixups to make `parser.buf` valid JSON at any frame boundary, so the
   cut-short path goes through the same `json.loads` as the normal path
   (one code path, no regex emoji recovery). Behavior is identical —
   this is hardening, and it also becomes reusable for *any* future
   mid-stream abort (deadline hits, cue_done arrives mid-generation).
2. **Abort-on-late generation.** While in there: animators currently
   check `cue_is_done` only *before* starting a generation. With
   `synthesize_close()` it becomes cheap to also abort a *streaming*
   generation mid-flight when its cue completes (`cue_done` arrives) —
   stop wasting inference on output that will be dropped. This directly
   addresses the "arm generation finishes after its cue and gets binned"
   waste documented in TTP_V2.md timing notes.
3. **Parser robustness.** `StreamingFrameParser` already handles strings/
   escapes/nesting. Add defensive handling for: whitespace/BOM prefixes,
   a model emitting the frames array before the emoji key, and truncated
   final objects (drop, don't crash). Unit-test with adversarial chunk
   splits (the existing test feeds 1/3/7-char chunks — extend it).
4. **Face cap too — YES, at 9 (Mark's call).** Face model has
   `MAX_FRAMES=12` in schema and no runtime cap. Apply the same shared
   cutoff mechanism to face generations with `MAX_ACCEPTED_FRAMES=9`.
---

## Suggested execution order (each step commits + leaves system runnable)

1. **E** (parser hardening + `synthesize_close` + shared cutoff) — pure
   lib work, fully unit-testable offline, everything else builds on it.
2. **A** (Ollama pool) — self-contained, immediately useful, low risk.
3. **B** (concurrent director) — moderate risk, test with kokoro
   multi-chunk utterances; captions will temporarily lead *more* until D.
4. **C** (sequencer state machine) — the big one. Build `TrackPlayback`
   helper first as a pure refactor (existing behavior, tests), then add
   provisional timeline + pre-speech staging on top.
5. **D** (cue_playing + caption switch) — small, do immediately after C
   since C creates the natural publish point.

## Testing checklist for the fresh session

- `/usr/bin/python3 src/test_stuff/test_performance_lib.py --live` after
  every lib change (extend with parser-abuse + synthesize_close tests).
- Live A/B per engine: `tools/cycle_gemini_phrases.py --speak --engine
  {espeak,piper,festival,kokoro} [--no-sync] [--mutate]` — this script is
  the designated timing/aesthetics evaluation harness (sync defaults on).
- Watch for: captions aligned with audio (D), face pose appearing during
  kokoro synthesis silence (C rule 2), no animatronics running frame 2+
  before audio (C gate), chunk 2 audio ready the instant chunk 1 ends (B),
  arm generations aborting when their cue is already done (E2).
- Multi-chunk long utterance on kokoro is the acid test for B+C together.
- The three nodes are being run ad-hoc via `rosrun` in this dev cycle;
  `logos_hardware.launch` has all four TTP nodes for the supervised path.

## Invariants to preserve (Mark's standing rules)

- Backward compatible / revertable at every commit; old nodes stay on
  disk; launch-file revert lines stay commented in place.
- LUT fallback must always exist — the robot must never depend on a
  generation succeeding (or a remote server being up) to have a face/arms.
- JSON-in-String topics preferred over new typed msgs (except where a
  typed msg already exists — don't change `SpeechData`'s wire shape).
- Keep the Logos-facing API (`emote.ttp` / `emote.gesture`) slim; new
  knobs stay backend (ROS params / config files) unless Mark asks.
- Commit often, one behavior per commit; test before claiming done.
- Don't commit machine-specific hostnames/credentials without asking.
  (Asked: Mark OK'd committing the real Ollama server config.)

## Open questions — ANSWERED by Mark 2026-07-06

1. Ollama config: **commit the REAL one** with LAN hostnames.
2. Re-probe resilience: **background periodic probing OK** as long as it
   never adds latency to a generation call (see revised Probing section);
   3-consecutive-failure immediate re-probe also approved.
3. Face-side frame cap: **yes, 9**, same shared mechanism as arms.
4. `~audio_wait_s` ≈ 10s silent-playout fallback: **approved**.
5. `SpeakTask` playback-progress upgrade: **yes** — see the verified
   design in Workstream D (subscribe-side fix in `emote.py`, no action
   semantics change, dead-reckoning fallback retained).

6. Cold-open behavior: **drop the sequencer-side LUT cold-open/switch
   logic entirely** (see the DECIDED note in the v2 summary above).
