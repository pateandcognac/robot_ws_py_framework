#!/home/robot/robot_ws/.venv/bin/python3
"""Live CPU benchmark for Nemotron 3.5 streaming ASR."""

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf


DEFAULT_MODEL = Path(
    "models/nemotron-3.5-asr-streaming-0.6b-onnx-int4"
)
DEFAULT_VOCAB = Path("config/nemotron_custom_vocabulary.json")
DEFAULT_DEVICES = ("logos_mic", "pan_tilt_mic")
DEVICE_ENV = "LOGOS_STT_AUDIO_DEVICES"
DEFAULT_CAPTURE_BLOCK_MS = 128
LANGUAGE = "en-US"
LANGUAGE_ID = 0


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark true cache-aware Nemotron streaming ASR from the Logos "
            "microphone. This is an experimental terminal tool; it does not "
            "publish ROS topics."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --list-devices
  %(prog)s --duration 30
  %(prog)s --device logos_mic --duration 60 --save-wav
  %(prog)s --audio-file /tmp/test.wav

Use --duration 0 to run until Ctrl-C. The downloaded INT4 model fixes the
inference format at 16 kHz mono and 560 ms chunks.
""",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="microphone capture duration in seconds; 0 means until Ctrl-C",
    )
    parser.add_argument(
        "--device",
        help=(
            "PortAudio input device name or numeric index; default tries "
            "LOGOS_STT_AUDIO_DEVICES, then logos_mic and pan_tilt_mic"
        ),
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=16000,
        help="capture sample rate (must match the model; default: 16000)",
    )
    parser.add_argument(
        "--chunk-ms",
        type=int,
        default=560,
        help="streaming chunk duration (must match the model; default: 560)",
    )
    parser.add_argument(
        "--capture-block-ms",
        type=int,
        default=DEFAULT_CAPTURE_BLOCK_MS,
        help=(
            "microphone read size in milliseconds (default: 128); larger "
            "values reduce PortAudio wakeups without changing ASR chunking"
        ),
    )
    parser.add_argument(
        "--audio-latency",
        default="high",
        help=(
            "PortAudio latency setting: low, high, or seconds "
            "(default: high)"
        ),
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL,
        help=f"local ONNX Runtime GenAI model directory (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--vocab-file",
        type=Path,
        default=DEFAULT_VOCAB,
        help=(
            "JSON aliases for canonical final-transcript spellings "
            f"(default: {DEFAULT_VOCAB})"
        ),
    )
    parser.add_argument(
        "--no-vocab-normalization",
        action="store_true",
        help="do not apply custom vocabulary aliases to the final transcript",
    )
    parser.add_argument(
        "--use-vad",
        action="store_true",
        help="enable the model bundle's Silero VAD before ASR",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=0,
        help="ONNX intra-op threads per model session; 0 uses runtime default",
    )
    parser.add_argument(
        "--save-wav",
        nargs="?",
        const="",
        metavar="PATH",
        help="save captured audio; omit PATH for a timestamped file under logs/",
    )
    parser.add_argument(
        "--audio-file",
        type=Path,
        help="benchmark an existing WAV instead of opening a microphone",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="print PortAudio devices and exit without loading the model",
    )
    return parser.parse_args()


def numeric_device(device):
    if device is None:
        return None
    try:
        return int(device)
    except ValueError:
        return device


def device_candidates(explicit_device):
    if explicit_device is not None:
        return [numeric_device(explicit_device)]
    configured = os.environ.get(DEVICE_ENV, "")
    candidates = [item.strip() for item in configured.split(",") if item.strip()]
    return candidates or list(DEFAULT_DEVICES)


def read_model_config(model_path):
    config_path = model_path / "genai_config.json"
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Missing {config_path}. Download the model as described in "
            "docs/nemotron_asr_poc_notes.md."
        )
    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    model = config["model"]
    return {
        "sample_rate": int(model["sample_rate"]),
        "chunk_samples": int(model["chunk_samples"]),
        # These cache dimensions describe the exported encoder graph. Changing
        # them in JSON does not resize ONNX inputs and is therefore unsafe.
        "left_context": int(model["left_context"]),
        "conv_context": int(model["conv_context"]),
        "pre_encode_cache_size": int(model["pre_encode_cache_size"]),
        "subsampling_factor": int(model["subsampling_factor"]),
        "hop_length": int(model["hop_length"]),
    }


def load_vocabulary(path):
    if path is None:
        return []
    path = path.expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Custom vocabulary file not found: {path}")
    with path.open(encoding="utf-8") as vocab_file:
        data = json.load(vocab_file)

    entries = data.get("entries", data)
    if not isinstance(entries, list):
        raise ValueError("Custom vocabulary JSON must contain an 'entries' list")

    vocabulary = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Vocabulary entry {index} must be an object")
        canonical = entry.get("canonical")
        aliases = entry.get("aliases", [])
        if not isinstance(canonical, str):
            raise ValueError(
                f"Vocabulary entry {index} needs a 'canonical' string"
            )
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias for alias in aliases
        ):
            raise ValueError(
                f"Vocabulary entry {index} needs a list of non-empty aliases"
            )
        vocabulary.append((canonical, aliases))
    return vocabulary


def strip_language_tags(text):
    """Remove emitted tags such as <en-US>; this benchmark is pinned to English."""
    return re.sub(r"<[a-z]{2}(?:-[A-Z]{2})?>", "", text)


class LanguageTagStripper:
    """Strip language tags even when a tokenizer splits them across pieces."""

    def __init__(self):
        self.pending = ""

    def feed(self, text):
        output = []
        for character in text:
            if self.pending:
                self.pending += character
                if character == ">":
                    if not re.fullmatch(
                        r"<[a-z]{2}(?:-[A-Z]{2})?>", self.pending
                    ):
                        output.append(self.pending)
                    self.pending = ""
                elif len(self.pending) > 8:
                    output.append(self.pending)
                    self.pending = ""
            elif character == "<":
                self.pending = character
            else:
                output.append(character)
        return "".join(output)


def normalize_vocabulary(text, vocabulary):
    """Apply spelling aliases after ASR; this is not decoder score boosting."""
    normalized = strip_language_tags(text)
    for canonical, aliases in vocabulary:
        for alias in sorted(aliases, key=len, reverse=True):
            pattern = rf"(?<![\w-]){re.escape(alias)}(?![\w-])"
            normalized = re.sub(
                pattern,
                lambda match, replacement=canonical: replacement,
                normalized,
                flags=re.IGNORECASE,
            )
    return re.sub(r"\s+", " ", normalized).strip()


class ResourceMonitor:
    def __init__(self):
        self.cpu_samples = []
        self.peak_rss = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        try:
            import psutil
        except ImportError:
            return

        process = psutil.Process()

        def sample():
            process.cpu_percent(interval=None)
            while not self._stop.wait(0.25):
                try:
                    self.cpu_samples.append(process.cpu_percent(interval=None))
                    self.peak_rss = max(self.peak_rss, process.memory_info().rss)
                except psutil.Error:
                    return

        self.peak_rss = process.memory_info().rss
        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    @property
    def mean_cpu(self):
        if not self.cpu_samples:
            return None
        return sum(self.cpu_samples) / len(self.cpu_samples)


class NemotronStream:
    def __init__(self, model_path, use_vad, threads):
        try:
            import onnxruntime_genai as og
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime-genai is not installed. See "
                "docs/nemotron_asr_poc_notes.md."
            ) from exc

        config = og.Config(str(model_path))
        config.clear_providers()
        if threads:
            session_options = {
                "intra_op_num_threads": threads,
                "inter_op_num_threads": 1,
            }
            config.overlay(
                json.dumps(
                    {
                        "model": {
                            component: {"session_options": session_options}
                            for component in ("encoder", "decoder", "joiner", "vad")
                        }
                    }
                )
            )
        self.model = og.Model(config)
        self.processor = og.StreamingProcessor(self.model)
        self.processor.set_option("use_vad", "true" if use_vad else "false")
        self.tokenizer = og.Tokenizer(self.model)
        self.tokenizer_stream = self.tokenizer.create_stream()
        self.language_tag_stripper = LanguageTagStripper()
        self.params = og.GeneratorParams(self.model)
        self.generator = og.Generator(self.model, self.params)
        self.generator.set_runtime_option("lang_id", str(LANGUAGE_ID))
        self.transcript = ""

    def _decode_available(self):
        text = ""
        while not self.generator.is_done():
            self.generator.generate_next_token()
            tokens = self.generator.get_next_tokens()
            if len(tokens) > 0:
                piece = self.language_tag_stripper.feed(
                    self.tokenizer_stream.decode(tokens[0])
                )
                if piece:
                    print(piece, end="", flush=True)
                    text += piece
        self.transcript += text
        return text

    def process(self, chunk):
        inputs = self.processor.process(chunk.astype(np.float32, copy=False))
        if inputs is None:
            return ""
        self.generator.set_inputs(inputs)
        return self._decode_available()

    def flush(self):
        inputs = self.processor.flush()
        if inputs is None:
            return ""
        self.generator.set_inputs(inputs)
        return self._decode_available()


def wav_output_path(argument):
    if argument is None:
        return None
    if argument:
        return Path(argument).expanduser()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path("logs/nemotron_asr") / f"nemotron-mic-{stamp}.wav"


def load_audio_file(path, sample_rate):
    audio, file_rate = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if file_rate != sample_rate:
        raise ValueError(
            f"{path} is {file_rate} Hz; this PoC requires {sample_rate} Hz WAV audio"
        )
    return np.asarray(audio, dtype=np.float32)


class MicrophoneCapture:
    def __init__(self, stream, audio_queue, capture_stats, block_samples):
        self.stream = stream
        self.audio_queue = audio_queue
        self.capture_stats = capture_stats
        self.block_samples = block_samples
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)

    def start(self):
        self.stream.start()
        self._thread.start()

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                audio, overflowed = self.stream.read(self.block_samples)
            except Exception:
                if self._stop.is_set():
                    return
                raise
            if overflowed:
                self.capture_stats["overflows"] += 1
            self.audio_queue.put(
                (
                    time.monotonic(),
                    np.asarray(audio[:, 0], dtype=np.float32).copy(),
                )
            )

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1.0)
        if self._thread.is_alive():
            self.stream.abort()
            self._thread.join(timeout=1.0)
        else:
            self.stream.stop()

    def close(self):
        self.stream.close()


def open_input_stream(
    candidates, sample_rate, audio_queue, block_samples, latency
):
    import sounddevice as sd

    errors = []
    capture_stats = {"overflows": 0}

    for device in candidates:
        try:
            stream = sd.InputStream(
                samplerate=sample_rate,
                blocksize=block_samples,
                channels=1,
                dtype="float32",
                device=device,
                latency=latency,
            )
            capture = MicrophoneCapture(
                stream, audio_queue, capture_stats, block_samples
            )
            capture.start()
            return capture, device, capture_stats, stream.latency
        except Exception as exc:
            errors.append(f"{device!r}: {exc}")
    raise RuntimeError(
        "No configured input device could be opened:\n  " + "\n  ".join(errors)
    )


def print_header(
    args, model_config, device, capture_block_samples=None, actual_latency=None,
):
    sample_rate = model_config["sample_rate"]
    chunk_samples = model_config["chunk_samples"]
    encoder_frame_samples = (
        model_config["hop_length"] * model_config["subsampling_factor"]
    )
    chunk_frames = chunk_samples // encoder_frame_samples
    right_context = chunk_frames - 1
    print("Nemotron mic benchmark")
    print(f"Device: {device}")
    print(f"Sample rate: {sample_rate} Hz mono")
    print(f"Chunk size: {chunk_samples} samples ({args.chunk_ms} ms)")
    print(f"Model: {args.model}")
    print("Backend: ONNX Runtime GenAI CPU, INT4")
    print(f"Language: {LANGUAGE} (fixed prompt ID {LANGUAGE_ID})")
    print("Strip language tags: on")
    print(
        "Attention context: "
        f"[{model_config['left_context']}, {right_context}] "
        f"({1000 * encoder_frame_samples / sample_rate:.0f} ms frames; "
        "fixed by ONNX export)"
    )
    print(
        "Other encoder cache: "
        f"conv={model_config['conv_context']}, "
        f"pre-encode={model_config['pre_encode_cache_size']}, "
        f"subsampling={model_config['subsampling_factor']}"
    )
    print(
        "Confidence: unavailable in ONNX Runtime GenAI 0.14 "
        "(RNNT joiner scores are not exposed)"
    )
    print(f"VAD: {'on' if args.use_vad else 'off'}")
    print(f"ONNX threads: {args.threads or 'runtime default'}")
    if capture_block_samples is not None:
        print(
            "Capture block: "
            f"{capture_block_samples} samples "
            f"({1000 * capture_block_samples / sample_rate:.0f} ms)"
        )
        print(f"PortAudio input latency: {actual_latency:.3f} s")


def run(args):
    model_path = args.model.expanduser().resolve()
    model_config = read_model_config(model_path)
    model_rate = model_config["sample_rate"]
    chunk_samples = model_config["chunk_samples"]
    model_chunk_ms = round(1000 * chunk_samples / model_rate)
    if args.sample_rate != model_rate:
        raise ValueError(
            f"--sample-rate must be {model_rate} for this model, got "
            f"{args.sample_rate}"
        )
    if args.chunk_ms != model_chunk_ms:
        raise ValueError(
            f"--chunk-ms must be {model_chunk_ms} for this exported model, got "
            f"{args.chunk_ms}"
        )

    monitor = ResourceMonitor()
    monitor.start()
    load_started = time.perf_counter()
    recognizer = NemotronStream(model_path, args.use_vad, args.threads)
    load_seconds = time.perf_counter() - load_started

    captured_parts = []
    inference_times = []
    max_backlog_seconds = 0.0
    last_audio_time = None
    stream = None
    capture_stats = {"overflows": 0}
    capture_block_samples = round(
        args.capture_block_ms * model_rate / 1000
    )

    if args.audio_file:
        audio = load_audio_file(args.audio_file.expanduser(), model_rate)
        print_header(args, model_config, f"WAV: {args.audio_file}")
        chunks = [
            (time.monotonic(), audio[start:start + chunk_samples])
            for start in range(0, len(audio), chunk_samples)
        ]
    else:
        audio_queue = queue.Queue()
        stream, active_device, capture_stats, actual_latency = open_input_stream(
            device_candidates(args.device),
            model_rate,
            audio_queue,
            capture_block_samples,
            args.audio_latency,
        )
        print_header(
            args,
            model_config,
            active_device,
            capture_block_samples,
            actual_latency,
        )
        print(
            f"Capturing for {'Ctrl-C' if args.duration == 0 else f'{args.duration:g} s'}..."
        )
        chunks = None

    print("[partial] ", end="", flush=True)
    process_started = time.perf_counter()
    pending = np.empty(0, dtype=np.float32)
    captured_samples = 0
    capture_started = time.monotonic()
    capture_done = chunks is not None

    try:
        while True:
            if chunks is not None:
                if not chunks:
                    break
                captured_at, block = chunks.pop(0)
            else:
                if (
                    not capture_done
                    and args.duration > 0
                    and time.monotonic() - capture_started >= args.duration
                ):
                    stream.stop()
                    stream.close()
                    stream = None
                    capture_done = True
                try:
                    captured_at, block = audio_queue.get(timeout=0.1)
                except queue.Empty:
                    if capture_done:
                        break
                    continue

            captured_parts.append(block)
            captured_samples += len(block)
            last_audio_time = captured_at
            pending = np.concatenate((pending, block))

            while len(pending) >= chunk_samples:
                chunk = pending[:chunk_samples]
                pending = pending[chunk_samples:]
                infer_started = time.perf_counter()
                recognizer.process(chunk)
                inference_times.append(time.perf_counter() - infer_started)
                if chunks is None:
                    backlog = (
                        audio_queue.qsize()
                        * capture_block_samples
                        / model_rate
                    )
                    max_backlog_seconds = max(max_backlog_seconds, backlog)
    except KeyboardInterrupt:
        print("\nStopping capture...", file=sys.stderr)
    finally:
        if stream is not None:
            stream.stop()
            stream.close()

    if pending.size:
        infer_started = time.perf_counter()
        recognizer.process(
            np.pad(pending, (0, chunk_samples - len(pending))).astype(np.float32)
        )
        inference_times.append(time.perf_counter() - infer_started)

    flush_started = time.perf_counter()
    recognizer.flush()
    flush_seconds = time.perf_counter() - flush_started
    completed_at = time.monotonic()
    total_wall = time.perf_counter() - process_started
    monitor.stop()
    raw_transcript = strip_language_tags(recognizer.transcript).strip()
    vocabulary = []
    if not args.no_vocab_normalization:
        vocabulary = load_vocabulary(args.vocab_file)
    final_transcript = normalize_vocabulary(raw_transcript, vocabulary)
    print(f"\n[final] {final_transcript}")
    if final_transcript != raw_transcript:
        print(f"[raw] {raw_transcript}")

    audio_seconds = captured_samples / model_rate
    total_inference = sum(inference_times) + flush_seconds
    rtf = total_inference / audio_seconds if audio_seconds else 0.0
    mean_inference = (
        sum(inference_times) / len(inference_times) if inference_times else 0.0
    )
    max_inference = max(inference_times, default=0.0)
    end_latency = None
    if args.audio_file is None and last_audio_time is not None:
        end_latency = completed_at - last_audio_time
    fell_behind = (
        max_backlog_seconds > args.chunk_ms / 1000.0
        or max_inference > args.chunk_ms / 1000.0
        or rtf > 1.0
        or capture_stats["overflows"] > 0
    )

    output_path = wav_output_path(args.save_wav)
    if output_path is not None and captured_parts:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(
            output_path,
            np.concatenate(captured_parts)[:captured_samples],
            model_rate,
            subtype="PCM_16",
        )

    print("\nBenchmark:")
    print(f"Load time: {load_seconds:.2f} s")
    print(f"Audio captured: {audio_seconds:.2f} s")
    print(f"Wall time after load: {total_wall:.2f} s")
    print(f"Total inference time: {total_inference:.2f} s")
    print(f"Realtime factor: {rtf:.3f} (inference/audio; lower is better)")
    print(f"Mean chunk inference: {mean_inference * 1000:.1f} ms")
    print(f"Max chunk inference: {max_inference * 1000:.1f} ms")
    print(f"Flush time: {flush_seconds * 1000:.1f} ms")
    if end_latency is None:
        print("Approx end-to-final latency: n/a (WAV replay is not realtime)")
    else:
        print(f"Approx end-to-final latency: {end_latency:.3f} s")
    print(f"Max queued audio: {max_backlog_seconds:.3f} s")
    print(f"PortAudio input overflows: {capture_stats['overflows']}")
    if monitor.mean_cpu is not None:
        print(f"Mean process CPU: {monitor.mean_cpu:.1f}% (100% = one core)")
        print(f"Peak RSS: {monitor.peak_rss / (1024 ** 3):.2f} GiB")
    else:
        print("CPU/RAM: unavailable (install psutil)")
    print(f"Fell behind realtime: {'yes' if fell_behind else 'no'}")
    if output_path is not None:
        print(f"Saved WAV: {output_path}")


def main():
    args = parse_args()
    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return
    if args.duration < 0:
        raise ValueError("--duration must be zero or positive")
    if args.threads < 0:
        raise ValueError("--threads must be zero or positive")
    if args.capture_block_ms < 1:
        raise ValueError("--capture-block-ms must be positive")
    try:
        if args.audio_latency not in ("low", "high"):
            args.audio_latency = float(args.audio_latency)
            if args.audio_latency <= 0:
                raise ValueError
    except ValueError:
        raise ValueError(
            "--audio-latency must be low, high, or a positive number"
        )
    run(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
