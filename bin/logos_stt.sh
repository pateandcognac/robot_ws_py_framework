#!/usr/bin/env bash

set -euo pipefail

backend="whisper"
finish_mode="end_phrases"
vad_silence_timeout="1.5"

usage() {
    cat <<'EOF'
Usage: logos_stt.sh [whisper|nemotron] [--vad [SECONDS] | --end-phrases]

Starts one Logos ear node in the foreground:
  whisper   Faster-Whisper backend (default)
  nemotron  Nemotron 3.5 INT4 ONNX streaming backend

Recording completion:
  --end-phrases   Say the configured end phrase to publish or cancel phrase
                  to discard (default).
  --vad [SECONDS] Publish after speech followed by VAD silence (default: 1.5).

Examples:
  logos_stt.sh whisper
  logos_stt.sh nemotron --vad
  logos_stt.sh whisper --vad 2.0
  logos_stt.sh nemotron --end-phrases
EOF
}

while (($#)); do
    case "$1" in
        whisper|faster-whisper)
            backend="whisper"
            shift
            ;;
        nemotron)
            backend="nemotron"
            shift
            ;;
        --vad)
            finish_mode="vad"
            shift
            if (($#)) && [[ "$1" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then
                vad_silence_timeout="$1"
                shift
            fi
            ;;
        --end-phrases)
            finish_mode="end_phrases"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown STT option: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

exec roslaunch logos_ui logos_stt.launch \
    "backend:=$backend" \
    "finish_mode:=$finish_mode" \
    "vad_silence_timeout:=$vad_silence_timeout"
