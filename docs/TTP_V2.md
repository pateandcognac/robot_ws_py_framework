# TTP v2 — Text-to-Performance Pipeline

The performance pipeline turns emoji-punctuated text into synchronized
speech + face + arm performances, now with live face generation by a tiny
fine-tuned on-board LLM (see `TINY_FACE_DEPLOYMENT.md`). Three roles:

```
emote.ttp("Hello! 👋 ...")                emote.gesture(text="...")
        │ Speak action                            │ /face/emoji_command
        ▼                                         ▼
┌──────────────────────┐  cue_announce   ┌─────────────────────┐
│ Performance Director │ ───────────────▶│    Face Animator    │
│ (tts_action_server)  │  (before TTS!)  │ (face_animator_node)│
│ split at emoji, TTS  │                 │ lut→saved→generate  │
└──────────┬───────────┘                 │ Ollama, streaming   │
           │ /face/tts_chunk             └──────────┬──────────┘
           │ (SpeechData + cue_id)                  │ /performance/face_track
           ▼                                        ▼ (frames stream in)
┌──────────────────────────────────────────────────────────────┐
│              Performance Sequencer (single clock)            │
│  cue queue → audio (master clock) + face + arms per cue      │
│  face: animator track ▸ semantic LUT cold-open ▸ idle        │
│  streamed frames can join mid-cue (~switch_threshold)        │
└──────────────────────────────────────────────────────────────┘
        │ typed face topics        │ /arm/command      │ audio out
```

Replaced (kept on disk for revert): `audio_and_face_playback.py`,
`arm_playback_node.py`.

## Formats

- **Semantic face format** (`animations/face_semantic/`, one JSON object per
  file): `{emoji, name?, ideation?, frames: [{beat, eyes{left/right/both},
  mouth}]}` with sparse carry-forward frames. This is now the *runtime*
  format, the training format, and the tiny model's output format.
  `animations/face/` (legacy compiled) is no longer read at runtime.
- **Semantic arm format** (`animations/arms_semantic/`): same shape with
  `arms{left/right/both: {joint1, joint2, wrist}}`. Produced by
  `tools/annotate_arm_beats.py` (gemini-2.5-flash-lite writes only the beat
  strings; poses convert programmatically). Runtime arm playback still uses
  legacy `animations/arms/` until an arm model lands.
- **Generated faces** (`animations/face_generated/face_gen_<emoji-slug>__<ts>.json`):
  rolling per-emoji library of tiny-model takes, at most `~store_cap` (5)
  per emoji — oldest rolls out, so the library accumulates and refreshes
  over time. Only emoji-keyed takes are saved; plain-text-inspired results
  stay ephemeral. Never mixed into the LUT dirs (those stay the training
  source of truth).

Canonical schema code: `src/logos_hardware/scripts/performance_lib/`
(`face_schema.py`, `face_gen_client.py`, `luts.py`);
`tools/face_animation_schema.py` is a re-export shim.
Arm equivalent: `tools/arm_animation_schema.py`.

## Topics (JSON payloads in std_msgs/String unless noted)

| Topic | Direction | Payload |
|---|---|---|
| `/performance/cue_announce` | director → animator | `{utterance_id, engine, performance{...}, cues:[{cue_id,index,text,emoji}]}` published *before* synthesis |
| `/face/tts_chunk` | director → sequencer | `SpeechData` (typed; + `cue_id`) |
| `/performance/face_track` | animator → sequencer | `{cue_id, source: lut/saved/generated, status: partial/complete/failed, frames:[...], append?}` |
| `/performance/cue_done` | sequencer → animator | `{cue_id}` (skip late generations) |
| `/face/emoji_command` | anyone → sequencer+animator | `{emoji?, text?, duration, cue_id?, expect_track?, policy?, temperature?}` — any string works, not just emoji |
| `/arm/emoji_command` | anyone → sequencer | `{emoji, duration}` (legacy-compatible) |

`/tts/is_speaking`, `/arm/command`, and all typed face topics unchanged.

## Policy cascade & knobs

The director chunks utterances at emoji *and* by sentence/long clause
(~80 chars soft, 100 hard; breaks at sentence enders, then clause
punctuation, then whitespace — `performance_lib/chunking.py`), so
emoji-less prose still gets per-sentence face cues.

Animator resolves each cue through an ordered cascade; each step either
handles the cue or falls through:

- `lut` — pure-emoji cue with a LUT entry → publish a status="lut" signal
  (sequencer plays its own copy); cues carrying prose fall through
- `saved` — replay a random take from the per-emoji rolling library
- `generate` — run the tiny model (streamed frame-by-frame by default);
  on failure fall back to a LUT face: the cue's emoji, a recently
  performed emoji, or `~fallback_emoji` (💬)

Defaults: TTS cues `generate,saved,lut` (fresh bespoke faces first; LUT
cold-open covers late generations — speech never blocks). Command cues
`lut,saved,generate`.

Animator params: `~model` (q4_K_M), `~temperature` (0.3), `~seed`
(0=fresh takes), `~tts_policy`, `~command_policy`, `~save_generations`
(true), `~store_cap` (5), `~stream` (true), `~generate_even_if_late`
(false), `~gen_timeout_s` (30 — runaway guard only; streaming means it
never adds latency), `~fallback_emoji`.
Sequencer params: `~track_wait_s` (15, silent expect_track gestures),
`~switch_threshold` (0.6), `~face_lut_dir`, `~arm_lut_dir`.

Logos-facing API (deliberately slim; knobs above stay backend):
`emote.ttp(text, face="lut"/"generate,saved,lut"/...)` and
`emote.gesture(text, duration, channel, policy=)` where `text` is one
string — emoji, prose, or both; the backend extracts the emoji for
LUT/arm lookups and prompts the face model with the string as given.

The "sane mode" is `face_policy="lut"` everywhere (zero compute, original
behavior). The "already cool" mode is `saved` — replaying the model's
greatest hits for free.

## Timing (measured on-robot)

- LUT: instant. Saved: instant.
- Generation (`smollm2-135m-face-lora-34k:q2_K`): first streamed frame
  ~0.8–1.3s, complete 2–6s typical. Sequencer paces streamed frames across
  the cue and holds the last pose on stalls.

## Testing

- Lib: `/usr/bin/python3 src/test_stuff/test_performance_lib.py [--live]`
- Nodes launch via `logos_hardware.launch` (revert lines in comments).
- Bench checks without TTS: publish `/face/emoji_command` /
  `/performance/cue_announce` + a `SpeechData` chunk by hand — see git log
  of this feature for exact commands.
