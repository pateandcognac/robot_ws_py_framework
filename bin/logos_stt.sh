#!/usr/bin/env bash

set -e

backend="${1:-${LOGOS_STT_BACKEND:-whisper}}"
nice_level="${LOGOS_STT_NICE:-0}"
vad_only="${LOGOS_STT_VAD_ONLY:-0}"
vad_silence_timeout="${LOGOS_STT_VAD_SILENCE_TIMEOUT:-}"

if ! [[ "$nice_level" =~ ^-?[0-9]+$ ]] || (( nice_level < -20 || nice_level > 19 )); then
  printf 'LOGOS_STT_NICE must be an integer between -20 and 19 (got %q)\n' \
    "$nice_level" >&2
  exit 2
fi

ros_args=()
case "${vad_only,,}" in
  1|true|yes|on)
    ros_args+=("_recording_vad_only:=true")
    ;;
  0|false|no|off|'')
    ;;
  *)
    printf 'LOGOS_STT_VAD_ONLY must be true or false (got %q)\n' "$vad_only" >&2
    exit 2
    ;;
esac
if [[ -n "$vad_silence_timeout" ]]; then
  ros_args+=("_recording_vad_silence_timeout:=$vad_silence_timeout")
fi

case "$backend" in
  -h|--help)
    cat <<'EOF'
Usage: logos_stt.sh [whisper|nemotron]

Starts one Logos ear node:
  whisper   Faster-Whisper backend (default)
  nemotron  Nemotron 3.5 INT4 ONNX streaming backend

LOGOS_STT_BACKEND may set the default when no argument is supplied.

Set LOGOS_STT_NICE to a niceness value to adjust scheduler priority. Negative
values raise priority (for example, LOGOS_STT_NICE=-5 logos_stt.sh) and may
require permission; the default is 0 (normal priority).

Set LOGOS_STT_VAD_ONLY=1 to finish a wake-recording after VAD silence instead
of listening for end-of-line or cancel-that. LOGOS_STT_VAD_SILENCE_TIMEOUT
sets the quiet interval in seconds (default: 1.5).
EOF
    exit 0
    ;;
  whisper|faster-whisper)
    node=stt_node.py
    ;;
  nemotron)
    node=nemotron_stt_node.py
    ;;
  *)
    printf 'Usage: %s [whisper|nemotron]\n' "$0" >&2
    exit 2
    ;;
esac

if (( nice_level != 0 )); then
  exec nice -n "$nice_level" rosrun logos_ui "$node" "${ros_args[@]}"
fi

exec rosrun logos_ui "$node" "${ros_args[@]}"
