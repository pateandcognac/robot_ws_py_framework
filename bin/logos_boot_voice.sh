#!/usr/bin/env bash
set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "$script_dir/.." && pwd)"
ttp_script="$workspace_root/src/logos_utils/logos_ttp.py"
speakme_file="${LOGOS_SPEAKME_FILE:-$workspace_root/docs/SPEAKME.txt}"
kokoro_voice="${LOGOS_BOOT_KOKORO_VOICE:-0.5*am_onyx + 0.25*bm_lewis + 0.25*bf_alice}"

desktop_notify() {
  local title="$1"
  local body="$2"

  command -v notify-send >/dev/null 2>&1 || return 0
  notify-send \
    --urgency=critical \
    --expire-time=30000 \
    --icon=dialog-warning \
    "$title" \
    "$body" \
    >/dev/null 2>&1 || true
}

set_max_volume() {
  local attempt

  if command -v pactl >/dev/null 2>&1; then
    for attempt in $(seq 1 15); do
      if pactl set-sink-mute @DEFAULT_SINK@ 0 >/dev/null 2>&1 \
        && pactl set-sink-volume @DEFAULT_SINK@ 100% >/dev/null 2>&1; then
        return 0
      fi
      sleep 1
    done
  fi

  if command -v amixer >/dev/null 2>&1; then
    amixer -q set Master 100% unmute >/dev/null 2>&1 || true
  fi
}

speak_espeak() {
  command -v espeak >/dev/null 2>&1 || return 0
  espeak "$1" >/dev/null 2>&1 || true
}

speak_festival() {
  command -v festival >/dev/null 2>&1 || return 0
  printf '%s\n' "$1" | festival --tts >/dev/null 2>&1 || true
}

wait_for_ros_master() {
  local attempt

  for attempt in $(seq 1 30); do
    if rostopic list >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

check_kobuki() {
  local device="${TURTLEBOT_SERIAL_PORT:-/dev/kobuki}"
  local warning

  if [ ! -e "$device" ]; then
    warning="The Kobuki base is not connected. Turn it on using the switch next to the charging cord."
  elif [ ! -r "$device" ]; then
    warning="The Kobuki device exists, but I cannot read it. Check its permissions and power."
  else
    stty -F "$device" 115200 raw -echo >/dev/null 2>&1 || true
    if timeout 2 dd if="$device" of=/dev/null bs=1 count=1 status=none 2>/dev/null; then
      return 0
    fi
    warning="The Kobuki device is present but silent. Turn on the base using the switch next to the charging cord."
  fi

  printf 'logos_boot_voice.sh: %s\n' "$warning" >&2
  desktop_notify "Logos base needs attention" "$warning"
  speak_festival "$warning"
  return 1
}

perform_ttp() {
  local engine="$1"
  local voice="$2"
  local text="$3"
  local attempt
  local args=(
    "$ttp_script"
    --quiet
    --server-timeout 4
    --result-timeout 120
    --engine "$engine"
  )

  if [ -n "$voice" ]; then
    args+=(--voice "$voice")
  fi
  args+=("$text")

  for attempt in $(seq 1 8); do
    if /usr/bin/python3 "${args[@]}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  printf 'logos_boot_voice.sh: TTP stage failed using %s.\n' "$engine" >&2
  return 1
}

enable_ambient() {
  local attempt

  for attempt in $(seq 1 30); do
    if rosnode list 2>/dev/null | grep -Fxq "/logos_ears_node"; then
      "$script_dir/logos_ambient.sh" 1 1 '[]' >/dev/null 2>&1 || true
      return 0
    fi
    sleep 1
  done

  printf 'logos_boot_voice.sh: STT did not become ready; ambient mode was not enabled.\n' >&2
  return 1
}

case "${1:-}" in
  volume)
    set_max_volume
    ;;
  linux)
    speak_espeak "Linux user session online. Beginning Logos startup."
    ;;
  roscore)
    speak_espeak "Launching R O S core."
    ;;
  core)
    wait_for_ros_master || true
    check_kobuki || true
    speak_festival "R O S core is online. Starting Logos core hardware and voice systems."
    ;;
  piper)
    perform_ttp \
      piper \
      en_US-joe-medium \
      "My core systems are online. I am bringing my remaining senses online now. 🔆" \
      || true
    ;;
  ambient)
    enable_ambient || true
    ;;
  kokoro)
    perform_ttp \
      kokoro \
      "$kokoro_voice" \
      "My ears are online. 🎙️ Say hey robot to wake me, then say end of line when your request is complete. That explicit ending works better than waiting for a voice timeout. 🗣️ The small metal spring on the right side of my head is my microphone mute switch. Its red and green light shows whether I can hear you. 💡" \
      || true
    ;;
  keyring)
    perform_ttp \
      kokoro \
      "$kokoro_voice" \
      "If Ubuntu asks for login keyring authentication, enter the robot password. The password is robot." \
      || true
    ;;
  browser)
    perform_ttp \
      kokoro \
      "$kokoro_voice" \
      "Launching the Logos browser interface at localhost port five thousand." \
      || true
    ;;
  workspace)
    workspace_name="${2:-Logos}"
    last_workspace="${3:-0}"
    auto_cog="${4:-0}"
    if [ "$auto_cog" = "1" ]; then
      perform_ttp \
        kokoro \
        "$kokoro_voice" \
        "Launching Logos cognition now with workspace ${workspace_name}." \
        || true
    elif [ "$last_workspace" = "1" ]; then
      perform_ttp \
        kokoro \
        "$kokoro_voice" \
        "Look here. In the main terminal, enter a workspace now, or wait one minute to use the most recent workspace by default: ${workspace_name}." \
        || true
    else
      perform_ttp \
        kokoro \
        "$kokoro_voice" \
        "Look here. In the main terminal, enter a Logos workspace, or press enter to use the displayed default: ${workspace_name}." \
        || true
    fi
    ;;
  speakme)
    if [ -s "$speakme_file" ]; then
      perform_ttp kokoro "$kokoro_voice" "$(cat "$speakme_file")" || true
    fi
    ;;
  *)
    printf 'Usage: %s {volume|linux|roscore|core|piper|ambient|kokoro|keyring|browser|workspace|speakme}\n' "$0" >&2
    exit 2
    ;;
esac
