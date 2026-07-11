# TTP v3 — Text-to-Performance Pipeline

**This is the current, maintained reference.** Supersedes `TTP_V2.md`
(kept on disk for history — the architecture changed substantially, don't
follow it for anything live). `TTP_V3_PLAN.md` is the pre-build design doc
that led here; keep for provenance/decision history, not as a behavior
reference.

Audience: this doc is written for whoever touches this pipeline next —
Mark, a future Claude/Codex session, or any other agent. It documents the
*current shipped behavior*, the reasoning behind the non-obvious parts, and
one clearly-marked forward-looking section for planned work.

---

## One-paragraph mental model

Logos calls `emote.ttp("text with emoji cues 🎉")` or `emote.gesture(...)`.
The **director** splits the text into small performable cues and hands them
to TTS. The **animators** (one for face, one for arms) resolve each cue
into an animation *track* — via a locally-run tiny fine-tuned LLM (Ollama),
a library of that model's past takes, or the hand-authored master lookup
table (LUT) — independently and in parallel with speech synthesis. The
**sequencer** is the single playback clock: it owns the cue queue, starts
each cue's audio, and paces whatever face/arm track resolved for that cue
across the audio's duration, live-fudging the pacing as more frames arrive.
A **sync dial** (0.0–1.0) controls how much generated motion the sequencer
waits for before starting — trading latency for fidelity, per-utterance.

---

## Architecture diagram

```
emote.ttp("Hello! 👋 ...")              emote.gesture(text="...", channel="both")
        │ Speak action                       │ /face/emoji_command, /arm/emoji_command
        ▼                                    ▼                    ▼
┌───────────────────────┐  cue_announce  ┌─────────────┐    ┌─────────────┐
│  Performance Director  │──────────────▶│Face Animator│    │ Arm Animator│
│  (tts_action_server)   │ (before TTS!) │lut→saved→gen│    │gen→saved→lut│
│  split at emoji+clause │                │Ollama stream│    │Ollama stream│
│  synthesize CONCURRENTLY│               └──────┬──────┘    └──────┬──────┘
└──────────┬─────────────┘                       │ /performance/     │ /performance/
           │ /face/tts_chunk                     │ face_track        │ arm_track
           │ (SpeechData, cue_id, IN ORDER)       ▼ (streamed)        ▼ (streamed)
           ▼                          ┌──────────────────────────────────────┐
           │  ┌──────────────────────▶│      Performance Sequencer           │
           │  │  cue_announce         │      (single playback clock)         │
           └──┼──────────────────────▶│                                     │
              │                       │  queue at ANNOUNCE time (est_duration│
              │                       │  provisional clock); audio attaches  │
              │                       │  whenever it lands and becomes the   │
              │                       │  real clock. sync dial (0.0-1.0)     │
              │                       │  gates how much generated motion to  │
              │                       │  wait for before starting audio.     │
              │                       │  Per-cue: audio (master) + face +    │
              │                       │  arms, own timing channels, one      │
              │                       │  shared pacing loop, live re-stretch.│
              │                       └───────────────┬───────────┬─────────┘
              │                     typed face topics  │  /arm/command       │ audio out
              │                                        │  (joint1/joint2)    │
              │                       /performance/cue_playing (playback START, not
              └────────────────────── synthesis completion) → captions, choreography
```

Ollama serving the two tiny models is itself load-balanced across a LAN
server pool (`performance_lib/ollama_pool.py`) — see its own section below.

Replaced (kept on disk for revert, launch-file lines commented):
`audio_and_face_playback.py`, `arm_playback_node.py`.

---

## The four roles

### 1. Director — `tts_action_server.py`

Receives a `Speak` action goal (`utterance_text`, `engine`, `engine_params`
with an optional `performance` sub-dict). Splits text at emoji
(`split_text_emoji`), then subdivides long emoji-less spans by
sentence/clause (`performance_lib/chunking.py`: ~80 chars soft target, 100
hard limit, breaking at sentence-enders → clause punctuation → whitespace).
Every resulting `(text, emoji)` pair becomes one **cue** with a
`cue_id = "{utterance_id}:{index}"`.

**Before any synthesis**, it publishes `/performance/cue_announce` with the
full cue list (`text`, `emoji`, `index`, and `est_duration` — a same-ballpark
word-count guess from `chunking.estimate_speech_duration()`) plus the
`performance` dict verbatim. This is the starting gun for both animators
*and* the sequencer — everyone downstream learns a cue exists before its
audio is anywhere close to ready.

Then it **synthesizes every chunk concurrently** (`ThreadPoolExecutor`,
`~synth_workers`, default 2 — bounded so a multi-chunk utterance doesn't
stampede the single Larynx TTS server) and **publishes `SpeechData` on
`/face/tts_chunk` strictly in cue order** as each result lands (chunk 2 may
finish before chunk 1; it waits its turn). Feedback publishing is likewise
per-chunk, in order. `SpeakGoal`/`SpeakResult`/`SpeechData` wire shapes are
unchanged from v2 — old subscribers still work.

Cancellation is polled while waiting on each synthesis future; a canceled
goal also publishes `{"utterance_id": ..., "canceled": true}` on
`cue_announce` so the sequencer releases any cues of that utterance still
waiting on audio (see the provisional timeline below) instead of wedging.

### 2. Animators — `face_animator_node.py`, `arm_animator_node.py`

One node per channel, near-identical shape. Each subscribes to
`/performance/cue_announce` (TTS cues) and its own `/face/emoji_command` or
`/arm/emoji_command` (silent gestures carrying a `cue_id`), resolves every
cue through a **policy cascade**, and publishes the result to
`/performance/face_track` / `/performance/arm_track`.

**Cascade steps** (ordered; each either resolves the cue or falls through):

- `"lut"` — pure-emoji cue whose emoji exists in the master semantic LUT:
  publish `status="lut"` (a *signal*, no frames — the sequencer plays its
  own LUT copy). Cues carrying prose fall through so text can shape a
  generated result.
- `"saved"` — replay a random take from that emoji's rolling `GenStore`
  library (`source="saved"`).
- `"fuzzy"` — query the Chroma-backed semantic LUT index and publish the
  matched master LUT frames as `source="fuzzy"`, `status="complete"`.
  This uses model2vec through the existing Chroma sidecar.
- `"generate"` — run the tiny Ollama model, **streamed** by default (first
  frame published the moment it decodes, ~1–3s typically; each further
  frame appended live). On outright failure (Ollama unreachable, bad
  request, etc.) falls through to the next step. On success, emoji-keyed
  non-truncated results are saved to the rolling library.

**Default cascades**: face and arm TTS cues use
`fuzzy,lut,saved,generate` (prefer the Chroma-backed fuzzy master LUT match,
then exact LUT, saved takes, and fresh generation). Face and arm command
cues use `generate,saved,fuzzy,lut` (silent gestures lead with bespoke
motion, then fall back to fuzzy/exact LUT). All four independently
tweakable via ROS params (`~tts_policy`, `~command_policy`) and per-call
via the cue-announce `performance` dict's `face_policy`/`arm_policy` keys
or a command payload's `policy` key.

**Cascade exhaustion / outright failure**: `publish_fallback()` borrows a
LUT pose for the cue's own emoji, a recently-performed emoji, or
`~fallback_emoji` (💬 face / 🧍 arms) — the face/arms must never just go
blank or freeze, even off the end of the cascade.

**Rambling cutoff**: generations past `MAX_ACCEPTED_FRAMES` (arms: 7, face:
9 — a shared mechanism, `performance_lib/face_gen_client.truncate_rambling`)
are presumed to be the model rambling, not deliberate choreography.
Streaming generation closes the HTTP connection the instant the cutoff
frame arrives (saves inference time, not just playback time); blocking
generation truncates after the fact. Either way the result is marked
`_truncated=True` and never saved to `GenStore`.

**Abort-on-late-generation**: while a generation is still streaming, the
animator checks whether `/performance/cue_done` has already fired for that
cue_id (meaning the sequencer already played it out via the LUT/timeout
path) and aborts the stream early rather than burning inference on output
that will be dropped. Bounded by `~generation_wait_s` on the sequencer side
in sync mode (see below), or simply by the fact that the cue already
finished playing in don't-wait mode.

**Model source — the Ollama pool** (`performance_lib/ollama_pool.py`, both
animators): rather than hard-coding one Ollama URL, each animator loads a
per-role (`face`/`arms`) ordered server list from
`config/ollama_servers.json` (committed; real LAN hostnames — Mark's call).
At startup it probes each entry in order (`GET /api/tags` within
`probe_timeout_s`, checking the model tag is present, **case-insensitive**)
and picks the first live one. A background daemon thread re-probes every
`probe_interval_s` (default 60s) and **atomically swaps** the cached
`(url, model)` choice — so a dead server drops out within about a minute
and a better one that comes back up gets picked back up automatically,
**without ever adding latency to a generation call** (the hot path only
ever reads the cached choice). Three consecutive generation failures on
the current server trigger an immediate out-of-band re-probe *and* demote
that server (skip it for `DEMOTE_COOLDOWN_S`, 120s) — this specifically
covers a server that answers `/api/tags` fine but errors on `/api/generate`
(e.g. an Ollama version mismatch not accepting the structured-output
`format` schema; hit live on 2026-07-07). `~model` ROS param on either
animator node pins the old single-server behavior and skips the pool
entirely (back-compat / debugging). No config file → pinned to the
in-code default single server, so nothing breaks with the file absent.

Bare hostnames (`minimint`) do **not** resolve in-process — nsswitch here
only has `mdns4_minimal`, which fires solely for `.local` names. Use
`.local` names or bare IPs in the config, not bare hostnames.

### 3. Sequencer — `performance_sequencer_node.py`

The single playback clock. Owns one `cue_queue` (TTS: audio + its face/arm
animation), one `arm_queue` (arm commands, its own thread — arms never block
face/audio, and vice versa), and — new in **v3.2** — a **face-command lane**
(its own thread, a single latest-wins slot rather than a queue; see below).

**Provisional cue timeline.** TTS cues enter `cue_queue` at **announce**
time — before any audio exists — as a `Cue` with `audio_pending=True` and
`duration=est_duration`. A `pending_audio` registry (keyed by `cue_id`)
holds these; `tts_chunk_cb` looks a cue up there and attaches the real
audio + duration whenever `SpeechData` lands (which can be before, during,
or after the cue reaches the front of the queue), waking anyone waiting on
it via a per-cue `threading.Event`. Real duration replaces the estimate.
If a `SpeechData` arrives with no matching pending entry and no announce
was ever seen (external publisher, or the old direct-publish path), it's
treated as a legacy complete cue — full v2 back-compat.

**Pre-speech staging.** While the front-of-queue cue is waiting on its
audio, the sequencer poses the **first** face frame and **first** arm
frame from whichever source has already resolved (track frame 1, else LUT
frame 1) — a held pose, an actor hitting their mark before the curtain
rises. Frame 2 onward is gated on audio actually starting. Bounded by
`~audio_wait_s` (default 10s): past that, the cue plays out **silently**
on `est_duration` rather than wedging the whole queue behind one hung
synthesis. Audio that arrives *after* a cue already played out silently is
detected and dropped (`TrackStore.is_done`) — the performance already
happened, it doesn't get replayed as a bare audio cue.

**The sync dial** (`performance.sync`, 0.0–1.0 float — see its own section
below) determines, per cue, how much generated motion to wait for before
starting audio. This wait happens **once, combined across both channels,
immediately before audio starts** (`process_cue` → `_wait_for_frames`) —
not inside the per-channel playback loops. (Historical note: an earlier
build placed this wait inside the channel loops, which by then had already
started the audio thread — the entire animation window landed in the
*silence after* speech instead of during it. Caught live, fixed same
session. If you ever see animation trailing audio instead of overlapping
it, this is the first thing to check.)

**`/performance/cue_playing`** fires the instant a cue's audio (or silent
playout) *actually* starts — `{cue_id, text, emoji, duration, index, total,
has_audio}`. This is the caption/choreography sync point; consumers must
use this, not `/face/tts_chunk` (which fires at *synthesis* completion —
with concurrent synthesis, that can be several seconds ahead of real
playback).

**Per-channel playback** (`play_channel_for_cue`, one shared implementation
for face and arms — a bundle of channel-specific pieces: `TrackStore`,
LUT dict, frame-expander class, max frame cap, pose-publish function).
Resolves to exactly one source and plays it out over the cue's duration:

1. **Animator track** — if the animator has produced any usable frames
   (`_track_usable`: non-empty, `status != "failed"`), play it, live-pacing:
   every iteration re-estimates total frame count (exact if
   `status="complete"`, else a "mid-sized sequence" guess so early frames
   don't hog the whole cue) and re-spaces the *remaining* frames over the
   *remaining* cue time. This is the "continuously re-stretch as new
   information lands" fudge contract.
2. **LUT** — only when the animator answered **terminally** with
   `status` in `{"lut", "failed", "none"}` (an explicit LUT signal, or a
   genuine generation failure), the cue has no `cue_id` (legacy no-animator
   commands), or a **sync-waited** cue's animator went completely
   dead-silent past the wait ceiling (treated as "animator down"). Slowness
   alone, on its own, **never** triggers this — see the sync dial section.
3. **Idle** — nothing available; hold the last pose for the duration.

**The face-command lane (v3.2 — face is real-time, latest-wins).** TTS
utterances and arm commands are *first-class*: queued and played in full.
Face commands (`/face/emoji_command`) are *second-class / real-time*. Instead
of a FIFO queue, the sequencer keeps a **single face-command slot** and a
dedicated `face_command_loop` thread. A new face command overwrites the slot
and bumps a generation counter, which **preempts** whatever face sequence is
currently playing (the counter is polled as a `cancel` predicate inside
`play_channel_for_cue`'s pacing loop *and* its up-front sync-wait). So rapid
commands — from an ambient reactivity source watching STT and audio
classification in a chatty/noisy room — **collapse to the most recent** rather
than stacking up, keeping the face live without a backlog.

The lane **defers to TTS**: a `tts_inflight` counter is incremented for every
announced (or legacy) TTS cue and decremented when that cue finishes in
`process_cue`. While it's > 0, the whole utterance owns the face — the lane
both refuses to *start* a face command and *preempts* one already playing
(the cancel predicate is `newer_gen OR tts_owns_face`). Because
`cue_announce_cb` enqueues an utterance's cues all at once, the counter stays
positive across inter-cue gaps and the pre-audio staging/sync waits, so there's
no mid-utterance flicker. When speech ends and the counter drains to 0, the
lane wakes (a `notify`, not just the 0.1s poll) and plays the latest slotted
command — **unless it's stale** (older than `~face_command_ttl_s`, default
1.5s: an ambient reaction to a sound from several seconds ago is meaningless,
so it's dropped rather than played late). A **debounce**
(`~face_command_min_hold_s`, default 0.25s) makes a just-started command ignore
newer ones briefly so a noisy stream can't strobe the face.

Cleanup is **per-lane**: the face lane touches only `face_tracks` +
`/performance/cue_done` for its own `cue_id`; the arm channel likewise cleans
its own standalone gestures; `process_cue` still owns *both* channels for a
TTS cue (one cue_id, both channels). This is what stops a short face gesture
from clobbering a longer arm gesture — see the shared-cue_id note under
Consumers / `gesture()`.

### 4. Consumers

- `face_hud_bridge_node.py`, `interface_helper_node.py` (captions):
  subscribe to `/performance/cue_playing`, not `/face/tts_chunk` (old
  subscription line kept as a commented revert). Fixes the v2
  caption-leads-audio desync, which got *worse* once synthesis went
  concurrent (Workstream B made the desync bigger, D is the fix for it).
  They render only speech/TTS cues, so silent `/face/emoji_command` prompt
  text can still drive animation without appearing as dialogue.
- `~/robot_workspaces/*/src/logos/emote.py`'s `SpeakTask`: re-anchors its
  dead-reckoned playhead (`current_text()`/`current_emoji()`/`progress()`)
  to real `/performance/cue_playing` events per cue, instead of trusting
  first-chunk-synthesis-completion-plus-0.1s dead reckoning, which drifts
  by every sequencer-side delay (staged holds, sync waits, silent-playout
  gaps). Falls back to dead reckoning if no sequencer events arrive.

---

## The sync dial (`performance.sync`, 0.0–1.0)

This is the "loosey-goosey" knob (Mark's phrase) — how much of a cue's
generated motion to wait for before starting its audio, so speech and
movement land together on purpose rather than by luck.

| Value | Behavior |
|---|---|
| `1.0` (**default**, `emote.ttp`'s Python-level default) | Wait for generation to fully **complete** on every active channel (+ audio ready), then play the entire sequence evenly interpolated across the audio. Perfect sync. Highest latency — the wait is exactly how long the model takes to finish (currently ~3–12s depending on model/hardware/channel). |
| `0.0` | Wait for just the **first** generated frame, then start; remaining frames stream in and get fudged into the remaining time by the live pacing. Snappy (starts ~1–3s in), herkier. |
| in between | Wait for `ceil(frac × frame_cap)` frames (`frame_cap` = 9 face / 7 arms) *or* a terminal status, whichever comes first. |
| `None` / omitted at the node level | Node param `~default_sync_frac` (0.0) applies — only relevant to external publishers that skip the `performance.sync` key entirely; `emote.ttp` always sends an explicit value. |
| Python `bool` | Accepted for back-compat: `True → 1.0`, `False → None` (don't wait — legacy v2/v3.0 "never block speech" behavior). |

**The wait is bounded only by `~generation_wait_s`** (default 60s — long
enough to survive real generation, short enough to survive a genuinely
dead animator). This is a deliberate design choice, not an oversight:
***if generation was requested, we wait for it.*** Slowness on its own
never silently swaps in the LUT. The only paths to LUT playback are: an
explicit animator `status="lut"` signal, a real generation **failure**
(`status="failed"` — Ollama unreachable, bad request, no model, etc.), a
legacy no-`cue_id` command, or the sync ceiling being hit with *nothing*
usable having arrived (treated as "the animator is dead," not "it's
slow"). This matters because the whole point of dialing sync up is to
give the (currently experimental, still-improving) tiny models their best
shot at competing with the hand-authored LUT — silently bailing to the
LUT on slowness would defeat that.

`gesture()` currently sends the legacy `expect_track: True` boolean rather
than a `sync` float — the sequencer's `_command_sync_frac` maps that to
`0.0` (wait-for-frame-1), *not* the new `1.0` default. This is a known
asymmetry worth closing (adding a `sync=` kwarg to `gesture()` mirroring
`ttp()`) if/when it matters in practice — not yet done.

**API surface:**
- `emote.ttp(text, face=None, arms=None, sync=1.0, ...)` — `face`/`arms`
  are per-channel cascade overrides (`"lut"`, `"saved"`, `"generate"`, or a
  comma-separated cascade); `sync` is the dial. `arms=` is new in this
  round — previously only `face=` was exposed as a named kwarg, though the
  animator already read `arm_policy` from the cue-announce dict.
- `emote.gesture(text, duration, channel, policy=None)` — `policy` applies
  to whichever channel(s) `channel=` activates. Always effectively
  `sync=0.0`-equivalent today (see asymmetry note above).
- CLI: `src/logos_utils/logos_ttp.py --face POLICY --arms POLICY --sync
  0.0-1.0` — a command-line mirror of `emote.ttp()` for testing without a
  Logos runtime; ships the same `performance` dict shape. Add `--command`
  plus `--channel face|arms|both` to publish silent `/face/emoji_command`
  and/or `/arm/emoji_command` gestures instead of speech.

---

## Data formats

- **Semantic face LUT** (`animations/face_semantic/`, one JSON object per
  file): `{emoji, name?, ideation?, frames: [{beat, eyes{left/right/both},
  mouth}]}`, sparse carry-forward frames. This is the runtime format, the
  training format, and the tiny model's output format all at once.
  `animations/face/` (legacy compiled) is not read at runtime.
- **Semantic arm LUT** (`animations/arms_semantic/`): same shape,
  `arms{left/right/both: {<axis keys>, wrist}}`.
  **Key spelling gotcha (read this before touching arm code):** the
  ~1500 LUT files on disk are written with the *legacy* `joint1`/`joint2`
  key spelling; the fine-tuned arm model was trained on the renamed
  `shoulder_roll`/`shoulder_pitch` (legibility only). `arm_schema.py`
  accepts either spelling via `normalize_pose()`/`LEGACY_ARM_KEY_ALIASES`
  and always emits `joint1`/`joint2` when compiling to the runtime/ArmPose
  format, since **that's the real ROS wire format** — `ArmPose.msg` fields
  and `arm_controller_node.py`'s actual topic names
  (`/arm/left/joint1`, etc.) down to the hardware layer. This is a
  contract, not a style choice — don't rename it.
  **Bug history:** `arm_gen_client.py`'s `ArmFrameExpander` (what the
  sequencer actually calls for *all* arm playback, generated and LUT
  alike) used to merge frame patches with a raw `dict.update()`, bypassing
  that normalization entirely. Generated/saved frames use the model's
  canonical spelling directly, so they worked; LUT frames' `joint1`/
  `joint2` merged in as harmless-looking *extra* keys while
  `shoulder_roll`/`shoulder_pitch` stayed frozen at
  `DEFAULT_ARMS_POSE` forever — symptom: **wrists move, shoulders don't**,
  because `wrist` happens to be spelled the same both ways. Fixed
  2026-07-07 by routing `clamp_frame()` through the shared
  `normalize_pose()`. Covered by `test_arm_playback_key_compat` — if you
  ever see this symptom again, that test should catch it.
- **Generated takes**
  (`animations/face_generated/face_gen_<emoji-slug>__<ts>.json`,
  `animations/arm_generated/arm_gen_<emoji-slug>__<ts>.json`): rolling
  per-emoji `GenStore` libraries, `~store_cap` (5) kept per emoji, oldest
  rolls out. Only emoji-keyed takes are saved; plain-text-inspired results
  stay ephemeral. Truncated (rambling-cutoff) takes are never saved. Kept
  strictly separate from the LUT dirs (training source of truth).
- **Single-frame pose examples**
  (`animations/arms/single_frame_examples_semantic/`): few-shot diversity
  pool for dataset-generation prompts, not consumed at runtime.

Canonical schema code: `src/logos_hardware/scripts/performance_lib/`
(`face_schema.py`, `face_gen_client.py`, `arm_schema.py`,
`arm_gen_client.py`, `luts.py`, `ollama_pool.py`, `chunking.py`).
`tools/face_animation_schema.py` / `tools/arm_animation_schema.py` are
re-export shims for older tooling.

---

## Topics (JSON payloads in `std_msgs/String` unless noted)

| Topic | Direction | Payload |
|---|---|---|
| `/performance/cue_announce` | director → animators + sequencer | `{utterance_id, engine, performance{...}, cues:[{cue_id,index,text,emoji,est_duration}]}` published *before* synthesis; also `{utterance_id, canceled: true}` on cancel |
| `/face/tts_chunk` | director → sequencer | `SpeechData` (typed; `cue_id` field). Published in cue order as concurrent synthesis completes |
| `/performance/face_track` | face animator → sequencer | `{cue_id, source: lut/saved/generated, status: partial/complete/failed/lut/none, frames:[...], append?}` |
| `/performance/arm_track` | arm animator → sequencer | same shape, arm frames |
| `/performance/cue_done` | sequencer → both animators | `{cue_id}` (lets animators abort/skip late generations for a cue that already played) |
| `/performance/cue_playing` | sequencer → HUD/caption/choreography consumers | `{cue_id, text, emoji, duration, index, total, has_audio}` fired at **actual playback start** (audio or silent) |
| `/face/emoji_command` | anyone → sequencer + face animator | `{emoji?, text?, duration, cue_id?, sync? (or legacy expect_track?), policy?, temperature?}` — any string works, not just emoji. **v3.2: real-time / latest-wins** — a new command preempts the currently-playing face sequence and defers to in-flight TTS (see the face-command lane) |
| `/arm/emoji_command` | anyone → sequencer + arm animator | same shape; a payload with no `cue_id` is the legacy fast path (straight LUT lookup, no animator involvement) |

`/tts/is_speaking` and all typed face topics unchanged. `/arm/command`
(`ArmPose`) unchanged wire format (`side`, `joint1`, `joint2`, `wrist`).

---

## Key ROS params

**Director** (`tts_action_server.py`): `~synth_workers` (2), `~emoji_preset_path`.

**Animators** (both, mirror each other): `~model` (empty → use the Ollama
pool; set → pin single-server, back-compat), `~ollama_servers_config`
(defaults to `config/ollama_servers.json`), `~temperature` (0.3), `~seed`
(0 = fresh takes every time), `~tts_policy`, `~command_policy`,
`~save_generations` (true), `~stream` (true), `~gen_timeout_s` (30 —
runaway/degenerate-loop guard only; streaming means it doesn't add
latency to the happy path), `~max_accepted_frames` (face 9 / arms 7),
`~generate_even_if_late` (false), `~max_pending_jobs` (128; 0/negative =
unlimited for bench testing),
`~fallback_emoji` (💬 face / 🧍 arms), `~store_path`, `~store_cap` (5),
`~face_lut_dir` / `~arm_lut_dir`.

Animator backlog overflow preserves already-queued FIFO work because those
cues are closest to playback. If the cap is exceeded, the newly-arrived cue
gets an immediate fallback track instead of being left silent, so the
sequencer does not wait for a result that will never arrive.

**Sequencer**: `~generation_wait_s` (60 — sync-dial ceiling, survives a
dead animator only), `~default_sync_frac` (0.0 — only applies when
`performance.sync` is entirely absent), `~audio_wait_s` (10 — silent
playout fallback for a hung synthesis), `~face_command_ttl_s` (1.5 —
drop a deferred face command older than this when it finally reaches the
front; <=0 disables), `~face_command_min_hold_s` (0.25 — debounce: a
just-started face command ignores newer ones this long; 0 disables),
`~face_lut_dir` / `~arm_lut_dir`.

**Ollama pool** (`config/ollama_servers.json`, not a ROS param):
`probe_timeout_s` (2.0), `probe_interval_s` (60.0), per-role
(`face`/`arms`) ordered `[{url, model}, ...]` list.

---

## Timing (measured on-robot, 2026-07)

- LUT: instant. Saved: instant.
- Generation timing is now **highly variable** — it depends on which
  server in the Ollama pool answered the probe (local CPU vs. a LAN box),
  and Mark is actively experimenting with different base models
  (`smollm2-135m-*-lora`, `smollm2-360m-*`, soon `gemma3-270M`) and
  quantizations. Do not treat old fixed numbers as current truth; if you
  need current timing, drive `tools/cycle_gemini_phrases.py --speak` or
  the acid-test pattern below and read the sequencer's own log lines
  (`"generation ready (sync %.2f) after %.2fs hold"`,
  `"first streamed frame at %.1fs"`, `"streamed %d frames in %.1fs"`).
- As one concrete data point from live testing this round: an espeak
  utterance's first cue started playback at **2.5s** with `sync=0.0`
  versus **12.5s** with `sync=1.0`, on a face model that streamed its
  first frame at ~3s and completed at ~12s. That's the dial's cost/benefit
  in one number, for one model, on one day — expect it to move as the
  models and server pool change.
- Quality verdict as of 2026-07-07 (Mark's read after live use, not just
  reading JSON): face model "pretty good"; arm models "meh" — more base-model
  experimentation planned for arms specifically.

---

## Testing

- Lib/offline: `/usr/bin/python3 src/test_stuff/test_performance_lib.py
  [--live]`. `--live` additionally hits the local Ollama models and the
  pool's live probe — needs a reachable Ollama.
- Nodes launch via `logos_hardware.launch` (revert lines for the old
  playback nodes stay commented in place) or ad-hoc via `rosrun
  logos_hardware <node>.py __name:=<name>` for dev-cycle iteration.
- CLI smoke test without touching Logos: `src/logos_utils/logos_ttp.py
  "text 🎉" --face generate --arms generate --sync 0.5`.
- Acid-test pattern for verifying the provisional timeline / sync dial /
  concurrent synthesis together: subscribe to `cue_announce`,
  `/face/tts_chunk`, and `/performance/cue_playing`; send one multi-emoji
  `Speak` goal; print a timeline of when each event fired relative to goal
  send. (No permanent script exists for this — it was written ad hoc in
  the development session; recreate similarly if needed.)
- Bench-check a single cue by hand without TTS: publish
  `/face/emoji_command` / `/arm/emoji_command` directly, or
  `/performance/cue_announce` + a matching `SpeechData` on `/face/tts_chunk`.

---

## Known asymmetries / open follow-ups (not bugs, just noted)

- `gesture()` doesn't expose a `sync=` kwarg yet (see sync dial section) —
  it's pinned to legacy `expect_track=True` semantics (≈`sync=0.0`).
- No moving-average predictor for expected duration/frame-count per
  engine/model (words→duration, gen-time-per-frame, avg frame count) —
  deliberately deferred; the reactive frame-count wait covers today's
  functional need. Would let sync decisions be proactive instead of purely
  reactive.
- Gestures issued mid-utterance now queue behind the *whole* announced
  utterance (v2: behind only the synthesized-so-far chunks) — a minor
  interleaving change on slow engines with long utterances, not observed
  as a real problem yet.
- **Fixed in v3.2:** `gesture(channel="both")` used to send one shared
  `cue_id` to both `/face/emoji_command` and `/arm/emoji_command`. Face and
  arm are independent cues on separate lanes with different durations, so the
  shorter face cue's `process_cue` cleanup (`arm_tracks.pop`/`mark_done` +
  `cue_done`) clobbered the still-in-flight arm cue's track and aborted its
  generation. Now `emote.gesture()` sends distinct per-channel cue_ids
  (`…:f` / `…:a`) and each lane cleans up only its own channel. Also fixed:
  standalone arm gestures never emitted `cue_done`/cleaned `arm_tracks` (a
  leak + a missed abort-on-late), now handled in `arm_channel_loop`. Old
  checkpoint workspaces still send shared ids, but the per-lane cleanup makes
  that harmless on the sequencer side regardless.

---

## Fuzzy semantic LUT lookup

Today the `"lut"` cascade step is an **exact emoji key match** — a cue
either carries a preset emoji that's a literal key in the LUT dict, or it
doesn't, and prose-only cues with no emoji at all always fall through past
`lut` straight to `saved`/`generate` (or, if those aren't in the policy,
to nothing/idle).

The `"fuzzy"` cascade step semantically matches cue text against a Chroma
collection of master LUT entries and robot-speech phrase examples. It is
now part of the default TTS cascade, and remains selectable anywhere a
policy cascade is accepted, e.g. `"fuzzy,lut,saved,generate"` or
`"generate,saved,fuzzy,lut"`.

Implementation shape:

- `performance_lib/fuzzy_lut.py` calls the existing Logos Chroma sidecar at
  `http://127.0.0.1:8123` and queries
  `logos__shared__performance_fuzzy_lut` with `embedding_provider:
  "model2vec"` and default model `minishlab/potion-base-2M`.
- The index is rebuilt with
  `python3 tools/rebuild_performance_fuzzy_index.py --reset`. It embeds
  each semantic face/arm LUT entry's emoji, name, ideation, and frame
  beats, then adds matching phrase examples from
  `/home/robot/src/ft_gemma_face/data/augmented/gemini_phrases/`.
- When an animator gets a fuzzy match, it publishes the matched local LUT
  frames as a normal complete track:
  `{source:"fuzzy", status:"complete", frames:[...], matched_emoji,
  match_distance, match_id}`. This is intentionally *not* `status="lut"`:
  `lut` only tells the sequencer to play the cue's original emoji, while
  fuzzy needs to carry a different matched animation without changing the
  sequencer.
- Exact `lut` remains deterministic and pure-emoji only. `fuzzy` is for
  prose, missing emoji, unknown emoji, or explicit policy choices that
  prefer nearest-neighbor hand-authored motions over generation.
