# stt_node — Logos Ears

The STT node is Logos's ears. It handles all microphone input in one process: OpenWakeWord wake phrase detection, passive hotword listening, speech-to-text transcription, ambient audio transcription, and background audio classification. Everything stays in one node to avoid ROS serialization overhead on the audio stream.

---

## Quick Start

```bash
# One-time dependency setup for the Python 3.11 robot_ws venv
/home/robot/robot_ws/.venv/bin/python3 -m pip install openwakeword

# Enable ambient transcription
rostopic pub /stt/ambient_listener/enable std_msgs/Bool "data: true"

# Enable passive hotword detection for selected model directories
rostopic pub /stt/hotword_listener/enable std_msgs/String "data: '[\"jarvis\", \"computer\"]'"

# Enable audio classifier (MediaPipe YAMNet)
rostopic pub /stt/audio_classifier/enable std_msgs/Bool "data: true"

# Watch what's happening
rostopic echo /stt/hotword_listener/detections
rostopic echo /stt/ambient_listener/transcription
rostopic echo /stt/audio_classifier/events
```

Wake phrase (`Hey Robot`) is **always on** - it does not need to be enabled. During a wake-recording window, `end of line` finishes and `cancel that` abandons the recording without transcribing or publishing anything. The optional edit stop word is disabled by default and can be enabled with the private ROS param `~enable_edit_wakeword` when an `edit_input` model is available. Drop Logos-trained replacements into matching subdirectories under `wakewords/custom`; that tree is searched first. Shared OpenWakeWord feature models live in `wakewords/openwakeword-feature-models` so startup does not fetch model resources at runtime.

---

## Topics

### Subscribers (inputs)

| Topic | Type | Description |
|---|---|---|
| `/tts/is_speaking` | `Bool` | When `true`, mic input is muted ("ear plugs") for STT and audio classification |
| `/stt/ambient_listener/enable` | `Bool` | Enable/disable ambient Whisper transcription |
| `/stt/hotword_listener/enable` | `String` (JSON) | Set the passive OpenWakeWord model directory list; `[]` disables and unloads passive models |
| `/stt/audio_classifier/enable` | `Bool` | Enable/disable MediaPipe YAMNet audio classifier |

### Publishers (outputs)

| Topic | Type | Latched | Description |
|---|---|---|---|
| `/cognition/input` | `CognitionInput` | No | Transcribed user speech, ready for the LLM |
| `/stt/ambient_listener/transcription` | `String` (JSON) | Yes | Ambient transcription history |
| `/stt/hotword_listener/detections` | `String` | No | Each hotword detection as a plain string |
| `/stt/audio_classifier/events` | `String` (JSON) | Yes | YAMNet audio classification history |
| `/cognition/output` | `CognitionOutput` | No | Feedback/status messages for the UI |
| `/face/rgbled` | `Int32MultiArray` | No | LED animations |
| `/mobile_base/commands/sound` | `Sound` | No | Kobuki beeps |

---

## User Interaction Flow

1. Say **"Hey robot"** - OpenWakeWord detects the wake phrase
2. Logos beeps and starts recording (LEDs: green VU meter)
3. Say what you want, then say **"end of line"** to finish
4. Logos transcribes and publishes to `/cognition/input`
5. Say **"Cancel that"** instead to abandon the recording and return to listening

`RECORDING_TIMEOUT` is 60 seconds — if you don't say a stop word, recording stops automatically.

---

## JSON Schemas

### `/stt/ambient_listener/transcription`

Publishes a JSON array of transcription history entries. Oldest first. Max 2-hour age, max ~32K characters total (oldest pruned by character count).

Publishes `{}` (empty object) when ambient is disabled or cleared.

```json
[
  {
    "time": "02:34 PM",
    "epoch": 1747500000.0,
    "confidence": 0.81,
    "transcription": "Hey Tom, did you see the game last night? Yeah, it was pretty good..."
  },
  {
    "time": "02:41 PM",
    "epoch": 1747500420.0,
    "confidence": 0.76,
    "transcription": "---\n# Wake word detected! Rerouting to <human_stt> channel..."
  }
]
```

| Field | Type | Notes |
|---|---|---|
| `time` | string | Human-readable wall clock time of the transcription |
| `epoch` | float | Unix timestamp — use for age calculations |
| `confidence` | float | Whisper avg logprob converted to a 0–1 probability; above ~0.7 is reliable |
| `transcription` | string | Raw Whisper output, stripped of wake/control phrases. May include a wake-word annotation line. |

**Typical consumer pattern:**
```python
import json, rospy
from std_msgs.msg import String

def cb(msg):
    history = json.loads(msg.data)
    if not history:
        return
    latest = history[-1]['transcription']   # most recent
    context = '\n'.join(e['transcription'] for e in history)  # full context

rospy.Subscriber('/stt/ambient_listener/transcription', String, cb)
```

---

### `/stt/hotword_listener/detections`

Plain string - the configured spoken label for the detected model. One message per detection (not latched, debounced by `HOTWORD_DEBOUNCE_SEC`).
Passive detections are published for the model directories requested by the latest `/stt/hotword_listener/enable` JSON list. Publish `[]` to disable passive hotwords and unload their models.

---

### `/stt/audio_classifier/events`

Publishes a JSON object with two sections: a 10-minute rolling history of per-minute aggregated classifications, and a short list of the most recent raw samples. When `/tts/is_speaking` is true, the classifier pauses and clears any partial sample window so Logos's own voice and speech-driven servo noise are not classified. Direct `ok computer` speech input also takes priority: the wake phrase clears any partial sample window, and queued classifier samples are dropped while user input is recording or transcribing.

Publishes `{}` (empty object) when classifier is disabled or cleared.

```json
{
  "per_minute": [
    {
      "start_epoch": 1747500000.0,
      "end_epoch":   1747500058.3,
      "categories": [
        {
          "name": "Speech",
          "avg_score": 0.72,
          "count": 6,
          "boosted_score": 1.583
        },
        {
          "name": "Television",
          "avg_score": 0.31,
          "count": 4,
          "boosted_score": 0.529
        },
        {
          "name": "Music",
          "avg_score": 0.18,
          "count": 1,
          "boosted_score": 0.18
        }
      ]
    }
  ],
  "recent": [
    {
      "epoch": 1747500058.3,
      "categories": [
        {"name": "Speech",                      "score": 0.8812},
        {"name": "Male speech, man speaking",   "score": 0.6201},
        {"name": "Conversation",                "score": 0.4103}
      ]
    }
  ]
}
```

#### `per_minute` entries

Each entry covers one wall-clock minute. Up to 10 entries (last 10 minutes). Oldest first.

| Field | Type | Notes |
|---|---|---|
| `start_epoch` | float | Unix timestamp, floored to the minute boundary |
| `end_epoch` | float | Timestamp of the last sample that fell in this minute |
| `categories` | array | Sorted by `boosted_score` descending; filtered to `>= 0.05` |

#### `per_minute[].categories` fields

| Field | Type | Notes |
|---|---|---|
| `name` | string | Raw YAMNet / AudioSet label |
| `avg_score` | float | Mean confidence across all samples in this minute |
| `count` | int | How many samples detected this label |
| `boosted_score` | float | `avg_score × (1 + log1p(count) × 0.5)` — can exceed 1.0; higher means more persistent |

`boosted_score` is the key field for ranking. A label seen 6 times at 0.72 confidence outranks a one-off at 0.90.

#### `recent` entries

Last 10 individual samples (one sample = 2.5s of audio, taken every 10s). Oldest first.

| Field | Type | Notes |
|---|---|---|
| `epoch` | float | Unix timestamp of the sample |
| `categories` | array | Raw per-sample scores, sorted descending, no boosting applied |

**Typical consumer pattern:**
```python
import json, rospy
from std_msgs.msg import String

def cb(msg):
    data = json.loads(msg.data)
    if not data:
        return

    # What's been most prominent in the last 10 minutes?
    all_cats = {}
    for minute in data.get('per_minute', []):
        for c in minute['categories']:
            name = c['name']
            if name not in all_cats or c['boosted_score'] > all_cats[name]:
                all_cats[name] = c['boosted_score']
    top = sorted(all_cats.items(), key=lambda x: -x[1])[:5]

    # What just happened?
    recent = data.get('recent', [])
    if recent:
        latest_labels = [c['name'] for c in recent[-1]['categories'][:3]]

rospy.Subscriber('/stt/audio_classifier/events', String, cb)
```

---

### `/cognition/input` (CognitionInput)

Published after each successful speech-to-text transcription. Fields set by this node:

| Field | Type | Value |
|---|---|---|
| `type` | string | `"human_stt"` |
| `content` | string | Whisper transcript, prefixed with a confidence header |
| `system_hint` | string | Instructions for the LLM on handling low-confidence transcripts |
| `loop_cognition` | bool | `true` |

Example `content`:
```
# faster-whisper model 'small.en' confidence: 84%
# Transcription:
Hey Logos, what time is it?
```

If recording timed out (no stop word spoken), an additional note is prepended:
```
# Note: stt audio recording timed out after 60 seconds. This most likely indicates
# the wake word was accidentally triggered...
# faster-whisper model 'small.en' confidence: 61%
# Transcription:
...
```

---

## LED States

| State | Trigger | Visual |
|---|---|---|
| Idle | Nothing enabled | Off (or dim slow amber breath if audio classifier is on) |
| Ambient transcribe | `/stt/ambient_listener/enable: true` | Dark blue slow breath |
| Hotword listening | `/stt/hotword_listener/enable: ["jarvis"]` | Dark green slow breath |
| Both ambient + hotword | Both enabled | Blue ↔ green slow crossfade, always lit |
| Recording | Wake word detected | Green→red VU meter, center-out |
| Transcribing | After recording ends | Green chaser |
| Ear plugs | `/tts/is_speaking: true` | Magenta shimmer |
| Ambient publish blip | After ambient transcription completes | Cyan traveling pulse (3s) |
| Classifier sample blip | After YAMNet sample completes | Magenta traveling pulse (2s) |

---

## Configuration Constants

Key values at the top of `stt_node.py` you might want to tune:

| Constant | Default | Description |
|---|---|---|
| `CORE_WAKEWORDS` | role map | Wake, finish, and cancel directory names; directory names become published spoken labels |
| `CORE_WAKEWORD_THRESHOLDS` | role map | Separate OpenWakeWord score thresholds for the default core roles |
| `OPTIONAL_CORE_WAKEWORDS` | role map | Optional core controls, currently `edit_input`, gated by ROS params and disabled by default |
| `PASSIVE_HOTWORD_THRESHOLD` | 0.5 | Shared OpenWakeWord score threshold for passive hotwords requested through `/stt/hotword_listener/enable` |
| `WAKEWORD_MODEL_ROOTS` | asset paths | Model directories; `wakewords/custom` wins over the vendored community tree |
| `OPENWAKEWORD_FEATURE_PATH` | asset path | Shared melspectrogram and embedding models for ONNX and TFLite inference |
| `AMBIENT_VAD_THRESHOLD` | 0.5 | Silero threshold for ambient Whisper buffering |
| `WAKEWORD_VAD_THRESHOLD` | 0.15 | Loose Silero threshold for OpenWakeWord activation filtering |
| `RECORDING_TIMEOUT` | 60s | Hard stop if user doesn't say `end of line` |
| `AMBIENT_CHECK_INTERVAL` | 600s | How often the ambient buffer is auto-flushed |
| `AMBIENT_MAX_DURATION` | 120s | Hard cap on ambient buffer before force-flush |
| `AMBIENT_HISTORY_MAX_AGE` | 7200s | How long to keep ambient history (2 hours) |
| `CLASSIFIER_SAMPLE_INTERVAL` | 10s | How often YAMNet samples are taken |
| `CLASSIFIER_SAMPLE_DURATION` | 2.5s | Length of each YAMNet sample |
| `CLASSIFIER_BOOST_FACTOR` | 0.5 | Strength of temporal confidence boost |
| `CLASSIFIER_TOP_K` | 10 | Max labels per sample |
| `HOTWORD_DEBOUNCE_SEC` | 1.0 | Min seconds between repeated hotword publishes |

Each configured wakeword points at a subdirectory, not a filename. The node
prefers ONNX when a directory has both ONNX and TFLite files. That is the
preferred format for the current Logos venv; TFLite-only directories are
discovered too, but their predictor still depends on a working local
`tflite_runtime`. When a directory has multiple files of one format, the
lexicographically last filename is selected so common `v2` names win over `v1`
without pinning the collection's filenames in code. Whisper prompt hints and
transcript cleanup phrases are built from the loaded core directory labels
automatically.
