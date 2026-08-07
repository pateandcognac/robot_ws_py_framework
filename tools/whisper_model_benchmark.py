#!/home/robot/robot_ws/.venv/bin/python3
"""Benchmark Faster-Whisper models against a recorded Logos dataset."""

from __future__ import annotations

import argparse
import csv
import gc
import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf


DEFAULT_MODELS = ("small.en", "medium.en", "distil-medium.en")
RUNTIME_PROMPT = (
    "Hello, Logos and palimpsest! It's ROS Noetic Ubuntu Linux with a "
    "Kobuki base and Python. HEY-ROBOT END-OF-LINE CANCEL-THAT. You have "
    "pan-tilt, top-down, map3D, Astra cameras, RGB LEDs, servos, laser, "
    "palimpsest, Chora, phantasma, and phantasmata. Use speech-to-text and "
    "palimpsest. Kobuki has GMapping for SLAM and AMCL navigation. Voice "
    "engines are Kokoro, Piper, E-Speak, and Chora."
)
CONTROL_PHRASES = ("hey robot", "end of line", "cancel that")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run multiple Faster-Whisper models over a dataset made by "
            "record_whisper_benchmark.py and report recognition quality and "
            "CPU latency. Missing Hugging Face models may download on first use."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s logs/stt_benchmark/20260807_150000
  %(prog)s DATASET --models tiny.en base.en small.en medium.en distil-medium.en
  %(prog)s DATASET --vad both
  %(prog)s DATASET --models small.en medium.en --local-files-only

The default model set is small.en, medium.en, and distil-medium.en. Results are
written beside the dataset in a new benchmark_TIMESTAMP directory.
""",
    )
    parser.add_argument(
        "dataset",
        type=Path,
        help="dataset directory or its manifest.jsonl",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=list(DEFAULT_MODELS),
        help="Faster-Whisper model names or local model directories",
    )
    parser.add_argument(
        "--vad",
        choices=("on", "off", "both"),
        default="on",
        help="Faster-Whisper VAD configurations to test (default: on)",
    )
    parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="beam size matching stt_node.py (default: 5)",
    )
    parser.add_argument(
        "--prompt-mode",
        choices=("runtime", "none"),
        default="runtime",
        help="use a compact stt_node.py-style prompt or no prompt",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="CTranslate2 compute type (default: int8)",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=0,
        help="CTranslate2 CPU threads; 0 lets the runtime decide",
    )
    parser.add_argument(
        "--download-root",
        type=Path,
        help="optional Faster-Whisper model download/cache directory",
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="fail instead of downloading models that are not cached",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="result directory; default is benchmark_TIMESTAMP in the dataset",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="benchmark only the first N manifest entries for a quick trial",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the dataset and print the matrix without loading models",
    )
    return parser.parse_args()


def manifest_path(dataset: Path) -> tuple[Path, Path]:
    resolved = dataset.expanduser().resolve()
    if resolved.is_dir():
        return resolved / "manifest.jsonl", resolved
    return resolved, resolved.parent


def load_manifest(path: Path, limit: int | None) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"missing dataset manifest: {path}")
    entries = []
    with path.open(encoding="utf-8") as manifest:
        for line_number, raw_line in enumerate(manifest, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            missing = {
                "pair_id",
                "device",
                "position",
                "scenario",
                "reference_text",
                "audio_path",
            } - entry.keys()
            if missing:
                raise ValueError(
                    f"{path}:{line_number}: missing {sorted(missing)}"
                )
            audio_path = (path.parent / entry["audio_path"]).resolve()
            if not audio_path.is_file():
                raise FileNotFoundError(
                    f"{path}:{line_number}: missing audio {audio_path}"
                )
            entry = dict(entry)
            entry["_audio_path"] = audio_path
            entries.append(entry)
            if limit is not None and len(entries) >= limit:
                break
    if not entries:
        raise ValueError(f"no samples in {path}")
    return entries


def output_directory(dataset_dir: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return dataset_dir / f"benchmark_{timestamp}"


def normalize_words(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9']+", " ", text)
    return [word for word in text.split() if word]


def phrase_pattern(phrase: str) -> str:
    parts = [re.escape(part) for part in phrase.split()]
    return r"\s*[-_]?\s*".join(parts)


def strip_control_phrases(text: str) -> str:
    punct = r"""[ \t\r\n"'“”‘’()\[\]{}<>*#@~`^=+|\\/,:;.!?—–-]*"""
    phrases = "|".join(phrase_pattern(phrase) for phrase in CONTROL_PHRASES)
    cleaned = re.sub(rf"(?i){punct}(?:{phrases}){punct}", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def edit_counts(reference: list[str], hypothesis: list[str]) -> dict[str, int]:
    # Each cell is (total edits, substitutions, deletions, insertions).
    previous = [(j, 0, 0, j) for j in range(len(hypothesis) + 1)]
    for i, ref_word in enumerate(reference, start=1):
        current = [(i, 0, i, 0)]
        for j, hyp_word in enumerate(hypothesis, start=1):
            if ref_word == hyp_word:
                current.append(previous[j - 1])
                continue
            substitution = previous[j - 1]
            deletion = previous[j]
            insertion = current[j - 1]
            candidates = (
                (
                    substitution[0] + 1,
                    substitution[1] + 1,
                    substitution[2],
                    substitution[3],
                ),
                (
                    deletion[0] + 1,
                    deletion[1],
                    deletion[2] + 1,
                    deletion[3],
                ),
                (
                    insertion[0] + 1,
                    insertion[1],
                    insertion[2],
                    insertion[3] + 1,
                ),
            )
            current.append(min(candidates))
        previous = current
    edits, substitutions, deletions, insertions = previous[-1]
    return {
        "word_edits": edits,
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "reference_words": len(reference),
    }


def vad_modes(value: str) -> list[bool]:
    if value == "both":
        return [True, False]
    return [value == "on"]


def model_label(model: str) -> str:
    return Path(model).name.rstrip("/") or model


def process_rss_mb() -> float | None:
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 ** 2)
    except ImportError:
        return None


def transcribe_sample(
    whisper,
    entry: dict,
    model_name: str,
    use_vad: bool,
    args: argparse.Namespace,
    load_seconds: float,
    rss_after_load_mb: float | None,
    sequence_index: int,
) -> dict:
    audio_path = entry["_audio_path"]
    audio_info = sf.info(audio_path)
    audio_duration = audio_info.frames / audio_info.samplerate
    started = time.perf_counter()
    segments, info = whisper.transcribe(
        str(audio_path),
        beam_size=args.beam_size,
        initial_prompt=RUNTIME_PROMPT if args.prompt_mode == "runtime" else None,
        vad_filter=use_vad,
    )
    segment_list = list(segments)
    elapsed = time.perf_counter() - started
    raw_text = " ".join(segment.text for segment in segment_list).strip()
    transcript = strip_control_phrases(raw_text)
    reference = str(entry["reference_text"]).strip()
    reference_words = normalize_words(reference)
    hypothesis_words = normalize_words(transcript)
    counts = edit_counts(reference_words, hypothesis_words)
    confidence = None
    if segment_list:
        confidence = math.exp(
            sum(segment.avg_logprob for segment in segment_list)
            / len(segment_list)
        )

    row = {
        key: value for key, value in entry.items() if not key.startswith("_")
    }
    row.update(
        {
            "model": model_name,
            "model_label": model_label(model_name),
            "compute_type": args.compute_type,
            "cpu_threads": args.cpu_threads,
            "beam_size": args.beam_size,
            "prompt_mode": args.prompt_mode,
            "vad_filter": use_vad,
            "sequence_index": sequence_index,
            "model_load_seconds": load_seconds,
            "rss_after_load_mb": rss_after_load_mb,
            "audio_duration_seconds": audio_duration,
            "transcribe_seconds": elapsed,
            "realtime_factor": elapsed / audio_duration if audio_duration else None,
            "raw_transcript": raw_text,
            "transcript": transcript,
            "empty": not bool(hypothesis_words),
            "exact_match": hypothesis_words == reference_words,
            "confidence": confidence,
            "language": getattr(info, "language", None),
            "language_probability": getattr(info, "language_probability", None),
            "duration_after_vad": getattr(info, "duration_after_vad", None),
            **counts,
            "wer": (
                counts["word_edits"] / counts["reference_words"]
                if counts["reference_words"]
                else None
            ),
        }
    )
    return row


def serializable_row(row: dict) -> dict:
    converted = {}
    for key, value in row.items():
        if isinstance(value, Path):
            converted[key] = str(value)
        elif isinstance(value, np.generic):
            converted[key] = value.item()
        else:
            converted[key] = value
    return converted


def run_benchmarks(
    entries: list[dict], args: argparse.Namespace, results_path: Path
) -> list[dict]:
    from faster_whisper import WhisperModel

    results = []
    for model_number, model_name in enumerate(args.models, start=1):
        print(
            f"\n=== Model {model_number}/{len(args.models)}: {model_name} ===",
            flush=True,
        )
        load_started = time.perf_counter()
        kwargs = {
            "device": "cpu",
            "compute_type": args.compute_type,
            "cpu_threads": args.cpu_threads,
            "local_files_only": args.local_files_only,
        }
        if args.download_root is not None:
            kwargs["download_root"] = str(args.download_root.expanduser())
        whisper = WhisperModel(model_name, **kwargs)
        load_seconds = time.perf_counter() - load_started
        rss_after_load_mb = process_rss_mb()
        print(
            f"Loaded in {load_seconds:.2f}s; RSS "
            + (
                f"{rss_after_load_mb:.0f} MiB"
                if rss_after_load_mb is not None
                else "unavailable"
            )
        )

        sequence_index = 0
        for use_vad in vad_modes(args.vad):
            print(f"VAD: {'on' if use_vad else 'off'}")
            for entry in entries:
                sequence_index += 1
                row = transcribe_sample(
                    whisper,
                    entry,
                    model_name,
                    use_vad,
                    args,
                    load_seconds,
                    rss_after_load_mb,
                    sequence_index,
                )
                row = serializable_row(row)
                results.append(row)
                with results_path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(row, sort_keys=True) + "\n")
                print(
                    f"  {entry['pair_id']} / {entry['device']}: "
                    f"WER {100 * row['wer']:.1f}%, "
                    f"RTF {row['realtime_factor']:.2f}, "
                    f"{row['transcript']!r}",
                    flush=True,
                )

        del whisper
        gc.collect()
    return results


def aggregate(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[key] for key in keys)].append(row)

    summaries = []
    for group_key, group_rows in sorted(groups.items(), key=lambda item: item[0]):
        word_edits = sum(row["word_edits"] for row in group_rows)
        reference_words = sum(row["reference_words"] for row in group_rows)
        summary = dict(zip(keys, group_key))
        summary.update(
            {
                "samples": len(group_rows),
                "wer": word_edits / reference_words if reference_words else None,
                "empty_rate": sum(row["empty"] for row in group_rows)
                / len(group_rows),
                "exact_match_rate": sum(
                    row["exact_match"] for row in group_rows
                )
                / len(group_rows),
                "median_transcribe_seconds": statistics.median(
                    row["transcribe_seconds"] for row in group_rows
                ),
                "median_rtf": statistics.median(
                    row["realtime_factor"] for row in group_rows
                ),
            }
        )
        summaries.append(summary)
    return summaries


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def write_summary(
    output_path: Path,
    manifest: Path,
    args: argparse.Namespace,
    rows: list[dict],
) -> None:
    overall = aggregate(rows, ("model_label", "vad_filter"))
    by_device = aggregate(rows, ("model_label", "vad_filter", "device"))
    by_condition = aggregate(
        rows,
        ("model_label", "vad_filter", "device", "position", "scenario"),
    )

    lines = [
        "# Faster-Whisper hardware benchmark",
        "",
        f"Dataset: `{manifest}`  ",
        f"Generated: {datetime.now().astimezone().isoformat()}  ",
        f"Compute: CPU `{args.compute_type}`, beam size {args.beam_size}, "
        f"prompt `{args.prompt_mode}`",
        "",
        "WER is micro-averaged across reference words. Latency and RTF are "
        "medians; lower is better.",
        "",
        "## Overall",
        "",
        markdown_table(
            ["Model", "VAD", "N", "WER", "Empty", "Exact", "Median s", "Median RTF"],
            [
                [
                    str(row["model_label"]),
                    "on" if row["vad_filter"] else "off",
                    str(row["samples"]),
                    f"{100 * row['wer']:.1f}%",
                    f"{100 * row['empty_rate']:.1f}%",
                    f"{100 * row['exact_match_rate']:.1f}%",
                    f"{row['median_transcribe_seconds']:.2f}",
                    f"{row['median_rtf']:.3f}",
                ]
                for row in overall
            ],
        ),
        "",
        "## By microphone",
        "",
        markdown_table(
            ["Model", "VAD", "Microphone", "N", "WER", "Empty", "Median RTF"],
            [
                [
                    str(row["model_label"]),
                    "on" if row["vad_filter"] else "off",
                    str(row["device"]),
                    str(row["samples"]),
                    f"{100 * row['wer']:.1f}%",
                    f"{100 * row['empty_rate']:.1f}%",
                    f"{row['median_rtf']:.3f}",
                ]
                for row in by_device
            ],
        ),
        "",
        "## By microphone, position, and scenario",
        "",
        markdown_table(
            [
                "Model",
                "VAD",
                "Microphone",
                "Position",
                "Scenario",
                "N",
                "WER",
                "Empty",
                "Median RTF",
            ],
            [
                [
                    str(row["model_label"]),
                    "on" if row["vad_filter"] else "off",
                    str(row["device"]),
                    str(row["position"]),
                    str(row["scenario"]),
                    str(row["samples"]),
                    f"{100 * row['wer']:.1f}%",
                    f"{100 * row['empty_rate']:.1f}%",
                    f"{row['median_rtf']:.3f}",
                ]
                for row in by_condition
            ],
        ),
        "",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run(args: argparse.Namespace) -> Path:
    if args.beam_size < 1:
        raise ValueError("--beam-size must be positive")
    if args.cpu_threads < 0:
        raise ValueError("--cpu-threads cannot be negative")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be positive")

    manifest, dataset_dir = manifest_path(args.dataset)
    entries = load_manifest(manifest, args.limit)
    modes = vad_modes(args.vad)
    total = len(entries) * len(args.models) * len(modes)
    print(f"Dataset: {manifest}")
    print(f"Samples: {len(entries)}")
    print(f"Models: {', '.join(args.models)}")
    print(f"VAD: {', '.join('on' if mode else 'off' for mode in modes)}")
    print(f"Transcriptions: {total}")
    if args.dry_run:
        return output_directory(dataset_dir, args.output_dir)

    output_dir = output_directory(dataset_dir, args.output_dir)
    results_path = output_dir / "results.jsonl"
    if results_path.exists():
        raise FileExistsError(
            f"{results_path} already exists; choose a new --output-dir"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = run_benchmarks(entries, args, results_path)
    write_csv(output_dir / "results.csv", rows)
    write_summary(output_dir / "summary.md", manifest, args, rows)
    print(f"\nResults: {output_dir}")
    print(f"Summary: {output_dir / 'summary.md'}")
    return output_dir


def main() -> None:
    run(parse_args())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
