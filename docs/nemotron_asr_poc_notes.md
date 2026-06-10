# Nemotron Streaming ASR Proof of Concept

## Scope

`tools/nemotron_mic_benchmark.py` is an experimental terminal benchmark. It
does not replace `stt_node.py`, OpenWakeWord, or any ROS publisher.

It matches the production STT capture path where practical:

- `sounddevice` / PortAudio input
- `logos_mic`, then `pan_tilt_mic`
- 16 kHz mono audio
- 512-sample capture blocks

The ASR backend is true cache-aware streaming, not sliding-window
pseudo-streaming. The selected ONNX export consumes non-overlapping 8,960
sample chunks (560 ms).

## Model And Runtime

- Model: `onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4`
- Backend: ONNX Runtime GenAI 0.14.0, CPU
- Model size on disk: about 793 MB
- Default language: `en-US`

The older English-only INT4 artifact is intended for `parakeet-rs`. The
multilingual 3.5 artifact was selected because Microsoft publishes a matching
Python `StreamingProcessor` implementation in ONNX Runtime GenAI.

## Install

Use the workspace Python 3.11 environment:

```bash
cd /home/robot/robot_ws
.venv/bin/python3 -m pip install onnxruntime-genai==0.14.0 psutil
.venv/bin/hf download \
  onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4 \
  --local-dir models/nemotron-3.5-asr-streaming-0.6b-onnx-int4
```

The model directory and timestamped benchmark WAV files are gitignored.

## Run

List PortAudio devices:

```bash
.venv/bin/python3 tools/nemotron_mic_benchmark.py --list-devices
```

Run the default 30-second microphone test:

```bash
.venv/bin/python3 tools/nemotron_mic_benchmark.py
```

Run until Ctrl-C and retain the input audio:

```bash
.venv/bin/python3 tools/nemotron_mic_benchmark.py \
  --duration 0 \
  --save-wav
```

Override the microphone or test an existing 16 kHz mono WAV:

```bash
.venv/bin/python3 tools/nemotron_mic_benchmark.py \
  --device pan_tilt_mic \
  --duration 60

.venv/bin/python3 tools/nemotron_mic_benchmark.py \
  --audio-file /tmp/test-16khz.wav
```

The report includes model load time, chunk inference times, inference/audio
real-time factor, queued audio, end-to-final latency, process CPU, peak RSS,
PortAudio overflows, and a simple fell-behind verdict. `--threads N` can limit
each ONNX session for follow-up scheduling experiments; zero uses the runtime
default.

## Current Results

As of June 10, 2026:

- The Python 3.11 runtime and INT4 model are installed locally.
- Host device enumeration found `pan_tilt_mic` at PortAudio index 17 and
  `logos_mic` at index 18.
- A live `logos_mic` test correctly transcribed:
  `One two three testing one two three`.
- Live model load time was about 2.3 seconds.
- Live inference RTF was 0.580 to 0.606.
- Mean chunk inference was about 300 ms for each 560 ms chunk.
- Approximate end-to-final latency was about 0.4 seconds.
- Peak RSS was 0.93 GiB.
- The live run reported PortAudio input overflows and delivered only 3.62
  seconds of audio during a nominal 5-second capture. This is a real failure
  condition even though model inference itself stayed faster than realtime.
- Matching the production node's blocking 512-sample read pattern reproduced
  the overflows. Limiting ONNX to four threads also did not eliminate them.

A 9.97-second synthesized speech test ran at 0.556 RTF with 0.93 GiB peak
RSS. It produced:

```text
Hello Rose, this is a streaming speech recognition benchmark on the turtle
mot robot can Nemotron understand this sentence in real time
```

The source said "Hello Logos" and "TurtleBot robot", so the test showed two
recognition errors on robotic synthesized speech.

## Interpretation

For this laptop, the first gate is a real-time factor below 1.0 without an
ever-growing queued-audio value. A practical interaction target is preferably
well below 1.0, because wake-word handling, VAD, ROS nodes, and the rest of
Logos also need CPU time.

Recommendation: continue, but first investigate the PortAudio overflows and
repeat a longer fixed-script live test while the normal Logos stack is
running. Do not change production STT yet. Raw model throughput and memory are
promising on the T580, but reliable capture and quality versus Faster-Whisper
are not established.

## References

- NVIDIA model:
  <https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b>
- INT4 ONNX model:
  <https://huggingface.co/onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4>
- ONNX Runtime GenAI:
  <https://github.com/microsoft/onnxruntime-genai>
- On-device ASR paper:
  <https://arxiv.org/abs/2604.14493>
