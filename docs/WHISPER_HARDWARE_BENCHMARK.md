# Whisper Hardware Benchmark

This workflow compares Faster-Whisper models using paired recordings from the
real Logos microphones. Each utterance is captured from `logos_mic` and
`pan_tilt_mic` simultaneously, so microphone comparisons do not depend on
repeating a sentence exactly the same way.

The tools do not publish ROS topics or command robot hardware:

- `tools/record_whisper_benchmark.py` creates WAV files plus a JSONL manifest.
- `tools/whisper_model_benchmark.py` replays that fixed dataset through one or
  more Faster-Whisper models and produces JSONL, CSV, and Markdown results.

Datasets and results default to `logs/stt_benchmark/`, which is gitignored.

## 1. Prepare the Robot

Stop the foreground STT node with Ctrl-C so it is not holding either capture
device. Leave the rest of the ROS stack running if representative CPU load is
part of the test.

Confirm the PortAudio names:

```bash
.venv/bin/python3 tools/record_whisper_benchmark.py --list-devices
```

The normal aliases are `logos_mic` and `pan_tilt_mic`.

Choose repeatable positions before recording. A useful starting convention is:

- `close`: about 0.3 m from the robot;
- `near`: about 1 m;
- `far`: about 2.5 m.

Use the same speaking direction and ordinary voice at every position. Do not
try to improve a bad take unless there was an interruption or you said the
wrong text; natural variation is part of the hardware test.

## 2. Record a Paired Dataset

The default session records five prompts at `close`, `near`, and `far`. Both
microphones record every prompt at once, producing 15 paired takes and 30 WAV
files:

```bash
.venv/bin/python3 tools/record_whisper_benchmark.py
```

The terminal shows the exact sentence, counts down, records for 10 seconds,
and reports RMS level, peak level, PortAudio status events, and simple `QUIET`
or `CLIPPING` warnings. It is fine to finish speaking before the 10-second
window ends; every model will receive the same silence.

Two takes per condition provide a better estimate of reliability:

```bash
.venv/bin/python3 tools/record_whisper_benchmark.py --takes 2
```

To compare stationary audio with servo noise:

```bash
.venv/bin/python3 tools/record_whisper_benchmark.py \
  --scenarios stationary servo_motion
```

For `servo_motion`, arrange representative continuous motion in another
terminal before starting each group. The recorder deliberately does not move
the robot.

The resulting directory contains:

```text
logs/stt_benchmark/YYYYMMDD_HHMMSS/
├── session.json
├── manifest.jsonl
└── audio/
    ├── close__stationary__short_command__take_01__logos_mic.wav
    └── close__stationary__short_command__take_01__pan_tilt_mic.wav
```

The spoken prompts include `Hey robot` and `End of line` to reproduce normal
interaction. The manifest's reference text excludes those control phrases,
matching what `stt_node.py` publishes after cleanup.

### Custom prompts

Pass a JSON list when a different vocabulary or a short pilot is useful:

```json
[
  {
    "id": "pilot",
    "spoken_text": "Hey robot, testing one two three. End of line.",
    "reference_text": "Testing one two three."
  }
]
```

```bash
.venv/bin/python3 tools/record_whisper_benchmark.py \
  --prompts-file /tmp/logos-whisper-prompts.json
```

## 3. Benchmark Models

Run the default comparison (`small.en`, `medium.en`, and
`distil-medium.en`) by passing the dataset directory printed by the recorder:

```bash
.venv/bin/python3 tools/whisper_model_benchmark.py \
  logs/stt_benchmark/YYYYMMDD_HHMMSS
```

Missing model weights may download from Hugging Face on first use. Use
`--local-files-only` to prohibit downloads.

A broader model sweep is:

```bash
.venv/bin/python3 tools/whisper_model_benchmark.py \
  logs/stt_benchmark/YYYYMMDD_HHMMSS \
  --models tiny.en base.en small.en medium.en \
           distil-small.en distil-medium.en
```

The production node currently uses Faster-Whisper VAD. To determine whether
its second VAD pass causes empty command results, benchmark both modes against
the exact same WAV files:

```bash
.venv/bin/python3 tools/whisper_model_benchmark.py \
  logs/stt_benchmark/YYYYMMDD_HHMMSS \
  --models small.en medium.en distil-medium.en \
  --vad both
```

For a quick end-to-end trial before a long sweep:

```bash
.venv/bin/python3 tools/whisper_model_benchmark.py DATASET \
  --models small.en --limit 2 --local-files-only
```

Defaults match the important production choices: CPU inference, `int8`, beam
size 5, Faster-Whisper VAD enabled, and a compact version of the runtime
initial prompt. Use `--prompt-mode none` to measure the models without prompt
bias.

## 4. Read the Results

Every run creates a new `benchmark_TIMESTAMP/` directory inside the dataset:

- `results.jsonl`: append-safe detailed result for every WAV/model/VAD tuple;
- `results.csv`: the same rows for spreadsheets or plotting;
- `summary.md`: overall, per-microphone, and per-position/scenario tables.

Important metrics are:

- **WER**: word error rate; lower is better. It can exceed 100% when a model
  inserts many extra words.
- **Empty**: percentage of recordings producing no words. This directly tracks
  the observed wake-command failure.
- **Exact**: normalized word-for-word matches.
- **RTF**: transcription seconds divided by audio seconds; below 1.0 is faster
  than real time. Lower leaves more CPU for ROS and the rest of Logos.
- **Median seconds**: user-visible batch transcription time for one recording.

Prefer the smallest model that has acceptably low WER and empty rate at the
`far` position on both microphones. Check the per-microphone table before
trusting an overall average: a model can hide a weak dedicated microphone by
performing well on the pan-tilt mic, or vice versa.

For servo testing, compare `stationary` and `servo_motion` rows in either the
Markdown condition table or `results.csv`.
