#!/home/robot/robot_ws/.venv/bin/python3
"""Record paired real-microphone samples for Logos Whisper benchmarks."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf


SAMPLE_RATE = 16000
DEFAULT_DEVICES = ("logos_mic", "pan_tilt_mic")
DEFAULT_POSITIONS = ("close", "near", "far")
DEFAULT_SCENARIOS = ("stationary",)
DEFAULT_PROMPTS = (
    {
        "id": "short_command",
        "spoken_text": "Hey robot, what is the battery level? End of line.",
        "reference_text": "What is the battery level?",
    },
    {
        "id": "motion_command",
        "spoken_text": (
            "Hey robot, please turn your head to the left and tell me what "
            "you can see. End of line."
        ),
        "reference_text": (
            "Please turn your head to the left and tell me what you can see."
        ),
    },
    {
        "id": "robot_vocabulary",
        "spoken_text": (
            "Hey robot, use the Astra camera, Kobuki base, and ROS Noetic "
            "navigation to inspect the hallway. End of line."
        ),
        "reference_text": (
            "Use the Astra camera, Kobuki base, and ROS Noetic navigation "
            "to inspect the hallway."
        ),
    },
    {
        "id": "names",
        "spoken_text": (
            "Hey robot, tell Mark, Lauren, Stella, Piper, and Rocky that "
            "Logos is ready. End of line."
        ),
        "reference_text": (
            "Tell Mark, Lauren, Stella, Piper, and Rocky that Logos is ready."
        ),
    },
    {
        "id": "conversational",
        "spoken_text": (
            "Hey robot, I was wondering whether you could remember where we "
            "left the charging cable and help me look for it after lunch. "
            "End of line."
        ),
        "reference_text": (
            "I was wondering whether you could remember where we left the "
            "charging cable and help me look for it after lunch."
        ),
    },
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Interactively record the same utterance from multiple Logos "
            "microphones at once. Audio and a JSONL manifest are written "
            "under logs/stt_benchmark/."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --list-devices
  %(prog)s
  %(prog)s --positions close near far --takes 2
  %(prog)s --scenarios stationary servo_motion --prompts-file prompts.json

For a servo_motion scenario, arrange the desired motion separately and keep
it running during the countdown/recording. This tool never commands hardware.
""",
    )
    parser.add_argument(
        "--devices",
        nargs="+",
        default=list(DEFAULT_DEVICES),
        help="PortAudio input device names or indices captured simultaneously",
    )
    parser.add_argument(
        "--positions",
        nargs="+",
        default=list(DEFAULT_POSITIONS),
        help="distance/position labels (default: close near far)",
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=list(DEFAULT_SCENARIOS),
        help="environment labels (default: stationary)",
    )
    parser.add_argument(
        "--takes",
        type=int,
        default=1,
        help="takes per position/scenario/prompt (default: 1)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="seconds captured for every utterance (default: 10)",
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=3,
        help="countdown seconds before each take (default: 3)",
    )
    parser.add_argument(
        "--block-ms",
        type=int,
        default=128,
        help="PortAudio capture block size in milliseconds (default: 128)",
    )
    parser.add_argument(
        "--latency",
        default="high",
        help="PortAudio latency: low, high, or seconds (default: high)",
    )
    parser.add_argument(
        "--prompts-file",
        type=Path,
        help=(
            "JSON list of objects with id, spoken_text, and reference_text; "
            "defaults to five robot-oriented prompts"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="dataset directory; default is a new timestamped directory",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="list PortAudio devices and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate arguments and print the recording matrix without audio",
    )
    return parser.parse_args()


def numeric_device(value: str):
    try:
        return int(value)
    except ValueError:
        return value


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "unnamed"


def load_prompts(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        prompts = [dict(prompt) for prompt in DEFAULT_PROMPTS]
    else:
        with path.expanduser().open(encoding="utf-8") as prompt_file:
            prompts = json.load(prompt_file)
    if not isinstance(prompts, list) or not prompts:
        raise ValueError("prompts must be a non-empty JSON list")

    validated = []
    seen = set()
    for index, prompt in enumerate(prompts, start=1):
        if not isinstance(prompt, dict):
            raise ValueError(f"prompt {index} must be an object")
        prompt_id = safe_slug(str(prompt.get("id", f"prompt_{index:02d}")))
        spoken = str(prompt.get("spoken_text", "")).strip()
        reference = str(prompt.get("reference_text", "")).strip()
        if not spoken or not reference:
            raise ValueError(
                f"prompt {prompt_id!r} needs spoken_text and reference_text"
            )
        if prompt_id in seen:
            raise ValueError(f"duplicate prompt id: {prompt_id}")
        seen.add(prompt_id)
        validated.append(
            {
                "id": prompt_id,
                "spoken_text": spoken,
                "reference_text": reference,
            }
        )
    return validated


def output_directory(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (Path("logs") / "stt_benchmark" / timestamp).resolve()


def parse_latency(value: str):
    if value in ("low", "high"):
        return value
    latency = float(value)
    if latency <= 0:
        raise ValueError("--latency must be low, high, or a positive number")
    return latency


class PairedRecorder:
    def __init__(
        self,
        devices: list[str],
        sample_rate: int,
        block_samples: int,
        latency,
    ):
        import sounddevice as sd

        self.sample_rate = sample_rate
        self.block_samples = block_samples
        self.capturing = threading.Event()
        self.lock = threading.Lock()
        self.buffers: dict[str, list[np.ndarray]] = {}
        self.status_counts: dict[str, int] = {}
        self.streams = []
        self.devices = []

        try:
            for requested in devices:
                device = numeric_device(requested)
                device_info = sd.query_devices(device, "input")
                label = safe_slug(requested)
                if label in self.buffers:
                    raise ValueError(f"duplicate device label: {label}")
                self.buffers[label] = []
                self.status_counts[label] = 0

                def callback(indata, frames, time_info, status, *, key=label):
                    del frames, time_info
                    if self.capturing.is_set():
                        with self.lock:
                            if status:
                                self.status_counts[key] += 1
                            self.buffers[key].append(indata[:, 0].copy())

                stream = sd.InputStream(
                    samplerate=sample_rate,
                    blocksize=block_samples,
                    channels=1,
                    dtype="float32",
                    device=device,
                    latency=latency,
                    callback=callback,
                )
                self.streams.append(stream)
                self.devices.append(
                    {
                        "requested": requested,
                        "label": label,
                        "name": device_info["name"],
                        "index": int(device_info["index"]),
                    }
                )
            for stream in self.streams:
                stream.start()
        except Exception:
            self.close()
            raise

    def record(self, duration: float) -> dict[str, tuple[np.ndarray, int]]:
        target_samples = round(duration * self.sample_rate)
        with self.lock:
            for key in self.buffers:
                self.buffers[key] = []
                self.status_counts[key] = 0
        self.capturing.set()
        started = time.monotonic()
        deadline = started + duration + max(
            1.0, 4.0 * self.block_samples / self.sample_rate
        )
        try:
            while time.monotonic() < deadline:
                with self.lock:
                    complete = all(
                        sum(len(part) for part in parts) >= target_samples
                        for parts in self.buffers.values()
                    )
                if complete:
                    break
                time.sleep(min(0.02, duration))
        finally:
            self.capturing.clear()
        # Allow callbacks already in flight to leave the append section.
        time.sleep(0.02)

        recordings = {}
        with self.lock:
            for key, parts in self.buffers.items():
                audio = (
                    np.concatenate(parts)
                    if parts
                    else np.empty(0, dtype=np.float32)
                )
                recordings[key] = (
                    audio[:target_samples],
                    self.status_counts[key],
                )
        short = {
            key: len(audio)
            for key, (audio, _) in recordings.items()
            if len(audio) < target_samples
        }
        if short:
            details = ", ".join(
                f"{key}={samples}/{target_samples}"
                for key, samples in short.items()
            )
            raise RuntimeError(f"paired capture ended short: {details}")
        return recordings

    def close(self):
        for stream in getattr(self, "streams", []):
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self.streams = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def audio_metrics(audio: np.ndarray) -> dict[str, float]:
    if len(audio) == 0:
        return {
            "rms": 0.0,
            "rms_dbfs": -120.0,
            "peak": 0.0,
            "peak_dbfs": -120.0,
            "clipped_fraction": 0.0,
        }
    absolute = np.abs(audio.astype(np.float64))
    rms = float(np.sqrt(np.mean(absolute ** 2)))
    peak = float(np.max(absolute))
    return {
        "rms": rms,
        "rms_dbfs": 20.0 * math.log10(max(rms, 1e-6)),
        "peak": peak,
        "peak_dbfs": 20.0 * math.log10(max(peak, 1e-6)),
        "clipped_fraction": float(np.mean(absolute >= 0.999)),
    }


def append_manifest(path: Path, entries: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as manifest:
        for entry in entries:
            manifest.write(json.dumps(entry, sort_keys=True) + "\n")
        manifest.flush()


def countdown(seconds: int) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"  Recording in {remaining}...", flush=True)
        time.sleep(1.0)
    print("  RECORDING — speak now", flush=True)


def print_matrix(
    devices: list[str],
    positions: list[str],
    scenarios: list[str],
    prompts: list[dict[str, str]],
    takes: int,
) -> None:
    paired_takes = len(positions) * len(scenarios) * len(prompts) * takes
    print(f"Devices captured together: {', '.join(devices)}")
    print(f"Positions: {', '.join(positions)}")
    print(f"Scenarios: {', '.join(scenarios)}")
    print(f"Prompts: {', '.join(prompt['id'] for prompt in prompts)}")
    print(f"Paired takes: {paired_takes}")
    print(f"WAV files: {paired_takes * len(devices)}")


def run(args: argparse.Namespace) -> Path:
    prompts = load_prompts(args.prompts_file)
    positions = [safe_slug(position) for position in args.positions]
    scenarios = [safe_slug(scenario) for scenario in args.scenarios]
    if args.takes < 1:
        raise ValueError("--takes must be positive")
    if args.duration <= 0:
        raise ValueError("--duration must be positive")
    if args.countdown < 0:
        raise ValueError("--countdown cannot be negative")
    if args.block_ms < 1:
        raise ValueError("--block-ms must be positive")
    latency = parse_latency(args.latency)

    print_matrix(args.devices, positions, scenarios, prompts, args.takes)
    if args.dry_run:
        return output_directory(args.output_dir)

    dataset_dir = output_directory(args.output_dir)
    manifest_path = dataset_dir / "manifest.jsonl"
    if manifest_path.exists():
        raise FileExistsError(
            f"{manifest_path} already exists; choose a new --output-dir"
        )
    dataset_dir.mkdir(parents=True, exist_ok=True)
    block_samples = round(args.block_ms * SAMPLE_RATE / 1000)

    print("\nOpening paired microphones...")
    with PairedRecorder(
        args.devices, SAMPLE_RATE, block_samples, latency
    ) as recorder:
        session = {
            "created_at": datetime.now().astimezone().isoformat(),
            "sample_rate": SAMPLE_RATE,
            "duration_seconds": args.duration,
            "block_ms": args.block_ms,
            "latency": args.latency,
            "devices": recorder.devices,
            "positions": positions,
            "scenarios": scenarios,
            "takes": args.takes,
            "prompts": prompts,
        }
        with (dataset_dir / "session.json").open(
            "w", encoding="utf-8"
        ) as session_file:
            json.dump(session, session_file, indent=2, sort_keys=True)
            session_file.write("\n")

        print("Opened:")
        for device in recorder.devices:
            print(
                f"  {device['requested']} -> index {device['index']}: "
                f"{device['name']}"
            )

        try:
            for scenario in scenarios:
                for position in positions:
                    print(
                        f"\n=== Position: {position} | Scenario: {scenario} ==="
                    )
                    if scenario == "servo_motion":
                        print(
                            "Arrange continuous representative servo motion; "
                            "this recorder will not command it."
                        )
                    input("Get into position, then press Enter...")

                    for prompt in prompts:
                        for take in range(1, args.takes + 1):
                            pair_id = (
                                f"{position}__{scenario}__{prompt['id']}__"
                                f"take_{take:02d}"
                            )
                            print(f"\n[{pair_id}]")
                            print(f"  SAY: {prompt['spoken_text']}")
                            input("  Press Enter when ready...")
                            countdown(args.countdown)
                            recordings = recorder.record(args.duration)
                            print("  stopped")

                            entries = []
                            captured_at = datetime.now().astimezone().isoformat()
                            for device in recorder.devices:
                                key = device["label"]
                                audio, status_count = recordings[key]
                                relative_path = Path("audio") / (
                                    f"{pair_id}__{key}.wav"
                                )
                                audio_path = dataset_dir / relative_path
                                audio_path.parent.mkdir(parents=True, exist_ok=True)
                                sf.write(
                                    audio_path,
                                    audio,
                                    SAMPLE_RATE,
                                    subtype="PCM_16",
                                )
                                metrics = audio_metrics(audio)
                                entry = {
                                    "pair_id": pair_id,
                                    "prompt_id": prompt["id"],
                                    "position": position,
                                    "scenario": scenario,
                                    "take": take,
                                    "device": device["requested"],
                                    "device_label": key,
                                    "device_index": device["index"],
                                    "device_name": device["name"],
                                    "spoken_text": prompt["spoken_text"],
                                    "reference_text": prompt["reference_text"],
                                    "audio_path": str(relative_path),
                                    "sample_rate": SAMPLE_RATE,
                                    "duration_seconds": len(audio) / SAMPLE_RATE,
                                    "capture_status_count": status_count,
                                    "captured_at": captured_at,
                                    **metrics,
                                }
                                entries.append(entry)
                                warning = ""
                                if metrics["clipped_fraction"] > 0.001:
                                    warning = " CLIPPING"
                                elif metrics["rms_dbfs"] < -35.0:
                                    warning = " QUIET"
                                print(
                                    f"  {key}: RMS {metrics['rms_dbfs']:.1f} "
                                    f"dBFS, peak {metrics['peak_dbfs']:.1f} "
                                    f"dBFS, status={status_count}{warning}"
                                )
                            append_manifest(manifest_path, entries)
        except (EOFError, KeyboardInterrupt):
            print("\nRecording stopped; completed takes remain usable.")

    print(f"\nDataset: {dataset_dir}")
    print(f"Manifest: {manifest_path}")
    return dataset_dir


def main() -> None:
    args = parse_args()
    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return
    run(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
