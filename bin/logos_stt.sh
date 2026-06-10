#!/usr/bin/env bash

set -e

backend="${1:-${LOGOS_STT_BACKEND:-whisper}}"

case "$backend" in
  -h|--help)
    cat <<'EOF'
Usage: logos_stt.sh [whisper|nemotron]

Starts one Logos ear node:
  whisper   Faster-Whisper backend (default)
  nemotron  Nemotron 3.5 INT4 ONNX streaming backend

LOGOS_STT_BACKEND may set the default when no argument is supplied.
EOF
    ;;
  whisper|faster-whisper)
    exec rosrun logos_ui stt_node.py
    ;;
  nemotron)
    exec rosrun logos_ui nemotron_stt_node.py
    ;;
  *)
    printf 'Usage: %s [whisper|nemotron]\n' "$0" >&2
    exit 2
    ;;
esac
