# Nemotron Streaming ASR Proof of Concept

## Scope

`tools/nemotron_mic_benchmark.py` remains the standalone terminal benchmark.
Production ROS integration now lives in
`src/logos_ui/scripts/nemotron_stt_node.py` and can replace `stt_node.py` while
preserving the same OpenWakeWord, classifier, LED, and ROS topic contract.

It matches the production STT capture path where practical:

- `sounddevice` / PortAudio input
- `logos_mic`, then `pan_tilt_mic`
- 16 kHz mono audio
- blocking microphone reads on a dedicated thread

The benchmark defaults to 2,048-sample capture blocks (128 ms) and PortAudio's
`high` latency setting. Production STT currently reads 512 samples (32 ms) at
a time. The larger benchmark block was necessary on this CPU to prevent the
ONNX workload from starving PortAudio; it does not change the model's ASR
chunk size.

The ASR backend is true cache-aware streaming, not sliding-window
pseudo-streaming. The selected ONNX export consumes non-overlapping 8,960
sample chunks (560 ms).

## Model And Runtime

- Model: `onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4`
- Backend: ONNX Runtime GenAI 0.14.0, CPU
- Model size on disk: about 793 MB
- Language prompt: fixed to `en-US` (`lang_id=0`)
- Language-tag stripping: enabled

The older English-only INT4 artifact is intended for `parakeet-rs`. The
multilingual 3.5 artifact was selected because Microsoft publishes a matching
Python `StreamingProcessor` implementation in ONNX Runtime GenAI.

## Custom Vocabulary And Word Boosting

NVIDIA NeMo supports real decoder-time phrase boosting for RNN-T models. That
feature rescales token scores during decoding. The ONNX Runtime GenAI 0.14
`StreamingProcessor` used by this PoC does not expose phrase lists, boost
scores, negative scores, beam decoding, or RNNT logits. Its runtime options are
limited to VAD controls. A Whisper-style prompt would therefore be misleading
here: this ONNX path cannot currently perform true word boosting.

The model card's phrase "prompt-conditioned" refers specifically to its
language-ID prompt, not a Whisper-style free-text vocabulary prompt.

The PoC instead includes an editable final-transcript normalization file:

```text
config/nemotron_custom_vocabulary.json
```

It contains aliases and the canonical spelling to emit:

```json
{
  "canonical": "HEY-ROBOT",
  "aliases": ["hey robot", "hey, robot"]
}
```

This is useful for deterministic wake-word formatting, names, acronyms, and
robotics jargon. It runs after recognition and does not improve acoustic
detection probability. Set `"canonical": ""` to remove an exact alias as a
post-transcription suppression rule; this is not negative decoder boosting.

Use another file or disable normalization:

```bash
.venv/bin/python3 tools/nemotron_mic_benchmark.py \
  --vocab-file /path/to/vocabulary.json

.venv/bin/python3 tools/nemotron_mic_benchmark.py \
  --no-vocab-normalization
```

When normalization changes the transcript, the tool prints both `[final]`
and `[raw]` so the effect remains visible during evaluation.

The model is explicitly prompted with `en-US`; it does not use automatic
language detection. Emitted tags such as `<en-US>` are stripped from both
partial and final text. This is the local equivalent of
`strip_lang_tags=True`; ONNX Runtime GenAI does not expose that NeMo flag.

## Cache Context

The tool prints the exported cache parameters at startup:

```text
attention_context=[56, 6]
conv_context=8
pre_encode_cache_size=9
subsampling_factor=8
```

NeMo supports attention contexts `[56, 0]`, `[56, 1]`, `[56, 3]`, `[56, 6]`,
and `[56, 13]`, corresponding to 80, 160, 320, 560, and 1,120 ms chunks. The
current artifact is exported for `[56, 6]`. These values describe fixed ONNX
graph input and cache dimensions. Editing `genai_config.json` alone does not
resize the graph and is unsafe. To test another context/latency operating
point, export or download another ONNX graph and point `--model` at it.

## Confidence Scores

Full NeMo supports frame-, token-, word-, and utterance-level confidence.
Depending on configuration, it derives confidence from maximum token
probability or normalized entropy, then aggregates token confidence into words
and utterances.

The INT4 ONNX graphs also compute the required RNNT joiner logits internally.
However, ONNX Runtime GenAI 0.14 takes argmax inside
`NemotronSpeechState::StepToken()` and immediately releases the logits tensor.
It does not expose token, word, or utterance confidence through Python.

The generic Python `Generator.get_logits()` and `get_output("joint_output")`
methods are not valid escape hatches for this transducer model. Local probes
against 0.14 caused a segmentation fault, so the benchmark deliberately does
not call them or manufacture a substitute score.

A proper implementation requires a small ONNX Runtime GenAI C++ change:

1. Compute log-softmax, maximum probability, or normalized entropy while the
   joiner logits are alive in `StepToken()`.
2. Preserve the confidence alongside each emitted non-blank token.
3. Expose those token scores through the C/Python generator API.
4. Aggregate emitted tokens into word and utterance scores.

For an initial streaming display, use a token-weighted mean over every ten
audio chunks rather than averaging ten per-chunk means. Chunks containing only
RNNT blanks or silence should not count as zero-confidence speech. With VAD
enabled, reset and print an utterance aggregate at end-of-speech. These values
would still be model confidence estimates, not calibrated probabilities of
transcription correctness.

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

Capture buffering can be tuned independently of model chunking:

```bash
.venv/bin/python3 tools/nemotron_mic_benchmark.py \
  --capture-block-ms 128 \
  --audio-latency high
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
- Production-sized 32 ms capture reads reported repeated PortAudio overflows
  under inference load. Limiting ONNX to four threads did not eliminate them.
- A 64 ms capture block still reported seven overflows in an 8-second run.
- A 128 ms capture block with PortAudio `high` latency completed an 8-second
  live run with zero overflows, 0.608 RTF, 0.93 GiB peak RSS, and a maximum
  queued-audio value of 0.384 seconds.
- The reliable 128 ms setting adds about 96 ms of capture buffering relative
  to the production 32 ms read size. Nemotron still emits work in fixed 560 ms
  chunks, so this is a modest part of the overall streaming latency.

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

The optional production node uses the proven 128 ms capture block, splits it
back into 32 ms VAD/OpenWakeWord frames, and defaults ONNX Runtime GenAI to one
intra-op thread. Whisper remains the default launcher backend while Nemotron
quality and CPU coexistence are evaluated with the full Logos stack.

## References

- NVIDIA model:
  <https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b>
- INT4 ONNX model:
  <https://huggingface.co/onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4>
- ONNX Runtime GenAI:
  <https://github.com/microsoft/onnxruntime-genai>
- On-device ASR paper:
  <https://arxiv.org/abs/2604.14493>
