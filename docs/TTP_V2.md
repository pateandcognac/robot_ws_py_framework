# TTP v2 — Text-to-Performance Pipeline

The performance pipeline turns emoji-punctuated text into synchronized
speech + face + arm performances, now with live face generation by a tiny
fine-tuned on-board LLM (see `TINY_FACE_DEPLOYMENT.md`). Three roles:

```
emote.ttp("Hello! 👋 ...")                emote.gesture(text="...", channel="both")
        │ Speak action                        │ /face/emoji_command, /arm/emoji_command
        ▼                                     ▼                    ▼
┌──────────────────────┐  cue_announce  ┌─────────────┐    ┌─────────────┐
│ Performance Director │ ──────────────▶│Face Animator│    │ Arm Animator│
│ (tts_action_server)  │  (before TTS!) │lut→saved→gen│    │ gen→saved→lut│
│ split at emoji, TTS  │                │Ollama stream│    │Ollama stream│
└──────────┬───────────┘                └──────┬──────┘    └──────┬──────┘
           │ /face/tts_chunk                   │ /performance/     │ /performance/
           │ (SpeechData + cue_id)             │ face_track        │ arm_track
           ▼                                   ▼ (streamed)        ▼ (streamed)
┌──────────────────────────────────────────────────────────────────────────┐
│                  Performance Sequencer (single clock)                    │
│  cue queue → audio (master clock) + face + arms, own timing channels     │
│  face/arm: animator track ▸ semantic LUT cold-open ▸ idle                │
│  streamed frames can join mid-cue (~switch_threshold)                    │
└──────────────────────────────────────────────────────────────────────────┘
        │ typed face topics              │ /arm/command (joint1/joint2)   │ audio
```

Replaced (kept on disk for revert): `audio_and_face_playback.py`,
`arm_playback_node.py`.

## Formats

- **Semantic face format** (`animations/face_semantic/`, one JSON object per
  file): `{emoji, name?, ideation?, frames: [{beat, eyes{left/right/both},
  mouth}]}` with sparse carry-forward frames. This is now the *runtime*
  format, the training format, and the tiny model's output format.
  `animations/face/` (legacy compiled) is no longer read at runtime.
- **Semantic arm format** (`animations/arms_semantic/`): same shape,
  `arms{left/right/both: {shoulder_roll, shoulder_pitch, wrist}}`. This is
  now the *runtime* LUT format too (as of the arm model landing) — the
  legacy `animations/arms/` list-of-state-objects format is kept as a
  training/tooling artifact (`annotate_arm_beats.py`, `arm_animation_tool.py`)
  but no longer read by the sequencer at runtime.
  `shoulder_roll`/`shoulder_pitch` is a training-side rename of the
  ROS-level `joint1`/`joint2` names (legibility only, see
  `TINY_ARM_DEPLOYMENT.md`) — `performance_lib/arm_schema.py` accepts
  either spelling on read and always emits `joint1`/`joint2` when compiling
  to the legacy/ArmPose format, since that's the real hardware wire name.
- **Generated faces** (`animations/face_generated/face_gen_<emoji-slug>__<ts>.json`)
  and **generated arms** (`animations/arm_generated/arm_gen_<emoji-slug>__<ts>.json`):
  rolling per-emoji libraries of tiny-model takes, at most `~store_cap` (5)
  per emoji — oldest rolls out, so each library accumulates and refreshes
  over time. Only emoji-keyed takes are saved; plain-text-inspired results
  stay ephemeral. Arm generations longer than
  `arm_gen_client.MAX_ACCEPTED_FRAMES` (7) are additionally never saved —
  see "Rambling cutoff" below. Never mixed into the LUT dirs (those stay
  the training source of truth).
- **Single-frame pose examples** (`animations/arms/single_frame_examples_semantic/`):
  36 lexically-varied rewordings (3 per original curated pose) of the 12
  hand-authored single-frame arm poses used as Gemini few-shot examples —
  a diversity pool for future dataset-generation prompts, not consumed at
  runtime.

Canonical schema code: `src/logos_hardware/scripts/performance_lib/`
(`face_schema.py`, `face_gen_client.py`, `arm_schema.py`,
`arm_gen_client.py`, `luts.py`); `tools/face_animation_schema.py` and
`tools/arm_animation_schema.py` are re-export shims.

## Topics (JSON payloads in std_msgs/String unless noted)

| Topic | Direction | Payload |
|---|---|---|
| `/performance/cue_announce` | director → both animators + sequencer | `{utterance_id, engine, performance{...}, cues:[{cue_id,index,text,emoji,est_duration}]}` published *before* synthesis; sequencer only reads `performance.sync` |
| `/face/tts_chunk` | director → sequencer | `SpeechData` (typed; + `cue_id`) |
| `/performance/face_track` | face animator → sequencer | `{cue_id, source: lut/saved/generated, status: partial/complete/failed, frames:[...], append?}` |
| `/performance/arm_track` | arm animator → sequencer | same shape, arm frames |
| `/performance/cue_done` | sequencer → both animators | `{cue_id}` (skip late generations) |
| `/face/emoji_command` | anyone → sequencer+face animator | `{emoji?, text?, duration, cue_id?, expect_track?, policy?, temperature?}` — any string works, not just emoji |
| `/arm/emoji_command` | anyone → sequencer+arm animator | same shape as `/face/emoji_command`; a payload with no `cue_id` is the legacy fast path (straight LUT lookup, no animator involvement) |

`/tts/is_speaking` and all typed face topics unchanged. `/arm/command`
(`ArmPose`) unchanged wire format (`side`, `joint1`, `joint2`, `wrist`).

## Policy cascade & knobs

The director chunks utterances at emoji *and* by sentence/long clause
(~80 chars soft, 100 hard; breaks at sentence enders, then clause
punctuation, then whitespace — `performance_lib/chunking.py`), so
emoji-less prose still gets per-sentence face and arm cues.

Each animator resolves its cues through its own ordered cascade; each step
either handles the cue or falls through:

- `lut` — pure-emoji cue with a LUT entry → publish a status="lut" signal
  (sequencer plays its own copy); cues carrying prose fall through
- `saved` — replay a random take from the per-emoji rolling library
- `generate` — run the tiny model (streamed frame-by-frame by default);
  on failure fall back to a LUT pose: the cue's emoji, a recently
  performed emoji, or `~fallback_emoji` (💬 face / 🧍 arms)

Defaults: face TTS cues `generate,saved,lut`, face command cues
`lut,saved,generate`. **Arms use `generate,saved,lut` for both** TTS and
command cues (fresh bespoke motion first; the LUT cold-open covers
lateness either way — speech never blocks on generation). All four are
independently tweakable via ROS params and, per call, via the cue-announce
"performance" dict's `face_policy`/`arm_policy` keys or a command payload's
`policy` key.

**Rambling cutoff (arms only):** generations past
`arm_gen_client.MAX_ACCEPTED_FRAMES` (7) are presumed to be the model
rambling rather than deliberate choreography — the training data tops out
around 6 frames, and 6+ frame generations (usually at higher temperature)
looked like degenerate drift in spot checks. Streaming generation closes
the connection the instant the 7th frame arrives (saves inference time,
not just playback time); blocking generation truncates the result
afterward. Either way the take is marked truncated and `maybe_save()`
skips it — it plays once and is never added to the rolling library.

Animator params (both nodes): `~model`, `~temperature` (0.3), `~seed`
(0=fresh takes), `~tts_policy`, `~command_policy`, `~save_generations`
(true), `~store_cap` (5), `~stream` (true), `~generate_even_if_late`
(false), `~gen_timeout_s` (30 — runaway guard only; streaming means it
never adds latency), `~fallback_emoji`.
Sequencer params: `~track_wait_s` (15, gestures), `~sync_wait_s` (4,
sync-mode TTS cues — see below), `~switch_threshold` (0.6),
`~face_lut_dir`, `~arm_lut_dir`.

### Sync mode (`emote.ttp(sync=True)`)

TTS cues default to `expect_track=False` — they never wait, so a slow
synthesizer (kokoro) effectively gives generation a free head start (it's
often already streaming in, or even done, by the time the cue starts
playing) while a near-instant synthesizer (piper/espeak/festival) gives
generation no head start at all, and streamed frames almost never arrive
before `~switch_threshold` of a short cue elapses — every such cue quietly
falls back to LUT/idle even though the model would gladly have kept up
given a couple more seconds.

`sync=True` (per call, via `performance.sync` in `engine_params`) opts every
cue in that utterance into the same bounded first-frame wait gestures
already use (`_wait_for_track()` — returns the instant *any* frame streams
in, or a terminal failed/lut status; not full completion), just with a
tighter bound (`~sync_wait_s`, default 4s) than gestures' patient
`~track_wait_s` (15s), since speech shouldn't stall as long as a silent
gesture reasonably can. Face and arm channels wait independently and
proceed the moment their own track is ready — no reason to hold one
channel back for the other. Each spoken chunk pays this wait once, not the
whole utterance.

The director also publishes an `est_duration` per cue in `cue_announce`
(`chunking.estimate_speech_duration()`, a same-ballpark word-count guess
from the literal text, available before synthesis even starts) — currently
informational/for future use (e.g. skip the sync wait outright for cues
too short for it to matter), not yet consulted by the sync-wait logic
itself.

This does not (yet) let playback begin before *audio* is rendered — audio
still only reaches the sequencer once synthesis for that specific chunk
completes (a sequential per-chunk HTTP call in the director), so a slow
engine still gates the whole cue on synthesis latency regardless of `sync`.
Decoupling that would mean restructuring the director into concurrent
per-chunk synthesis with a provisional (est_duration-based) cue timer that
gets corrected once real audio lands — a larger lift, not done here.

Logos-facing API (deliberately slim; knobs above stay backend):
`emote.ttp(text, face="lut"/"generate,saved,lut"/..., sync=False)` and
`emote.gesture(text, duration, channel, policy=)` (gestures are always
sync, by design) where `text` is one string — emoji, prose, or both; the
backend extracts the emoji for LUT lookups and prompts the tiny model(s)
with the string as given. `policy` applies to whichever channel(s)
`channel=` activates.

The "sane mode" is `policy="lut"` everywhere (zero compute, original
behavior). The "already cool" mode is `saved` — replaying a model's
greatest hits for free. The "ultimate cool" mode is `sync=True` with a
`generate`-first cascade on a fast synthesizer — everything live, roughly
overlapped, with a small bounded latency cost per chunk.

## Timing (measured on-robot)

- LUT: instant. Saved: instant.
- Face generation (`smollm2-135m-face-lora-34k:q4_K_M`): first streamed
  frame ~0.8–1.3s, complete 2–6s typical.
- Arm generation (`smollm2-135m-arm-lora-38k:q4_K_M`): first streamed frame
  ~0.7–2.6s, complete ~2–8s typical (5-9 raw frames before any rambling
  cutoff, though the model requests are for 1-6). **Worth noting:** most
  TTS chunks run 0.5–3s, shorter than a typical arm generation — so per-chunk
  arm generation frequently finishes *after* its cue has already ended and
  been marked done, at which point the result is silently dropped (by
  design: speech never blocks). The arms fall back to their LUT cold-open
  (or sit idle) for that cue while the model quietly finishes in the
  background. Not a bug, but worth knowing before assuming every spoken
  chunk gets bespoke arm motion — gestures (`emote.gesture(channel="arms")`)
  with their longer, deliberately-set durations are a better showcase of
  live arm generation than short TTS chunks.
- Sequencer paces streamed frames across the cue and holds the last pose on
  stalls, for both channels independently.

## Testing

- Lib: `/usr/bin/python3 src/test_stuff/test_performance_lib.py [--live]`
- Nodes launch via `logos_hardware.launch` (revert lines in comments).
- Bench checks without TTS: publish `/face/emoji_command` /
  `/performance/cue_announce` + a `SpeechData` chunk by hand — see git log
  of this feature for exact commands.
