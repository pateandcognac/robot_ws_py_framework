#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: logos_face_term.sh [--force] [--quiet]

Launch the Logos face HUD in a fullscreen gnome-terminal on the face monitor.

Environment overrides:
  LOGOS_FACE_MONITOR             xrandr monitor name, default: DP-1
  LOGOS_FACE_TERMINAL_PROFILE    gnome-terminal profile, default: robot_face_03
USAGE
}

force=0
quiet=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --force)
      force=1
      ;;
    --quiet)
      quiet=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "logos_face_term.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "$script_dir/.." && pwd)"
face_command="$script_dir/logos_face.sh"

monitor="${LOGOS_FACE_MONITOR:-DP-1}"
profile="${LOGOS_FACE_TERMINAL_PROFILE:-robot_face_03}"
title="${LOGOS_FACE_TERMINAL_TITLE:-Logos Face HUD}"

log() {
  if [ "$quiet" -eq 0 ]; then
    echo "$@"
  fi
}

if [ "$force" -eq 0 ] && pgrep -u "${USER:-$(id -un)}" -f '[f]ace_hud_node' >/dev/null 2>&1; then
  log "Logos face HUD already appears to be running; not opening another terminal."
  exit 0
fi

if [ -z "${DISPLAY:-}" ]; then
  echo "logos_face_term.sh: DISPLAY is not set; cannot launch gnome-terminal." >&2
  exit 1
fi

if ! command -v gnome-terminal >/dev/null 2>&1; then
  echo "logos_face_term.sh: gnome-terminal was not found." >&2
  exit 1
fi

geometry="80x24+1920+0"
if command -v xrandr >/dev/null 2>&1; then
  offset="$(
    xrandr --listactivemonitors 2>/dev/null \
      | awk -v monitor="$monitor" '$NF == monitor {
          if (match($3, /[+-][0-9]+[+-][0-9]+$/)) {
            print substr($3, RSTART, RLENGTH)
          }
        }'
  )"
  if [ -n "$offset" ]; then
    geometry="80x24${offset}"
  else
    log "Monitor '$monitor' was not found by xrandr; using default face offset +1920+0."
  fi
fi

terminal_command=$(
  printf 'source %q 2>/dev/null || true; ' "$workspace_root/devel/setup.bash"
  printf 'echo "Logos face HUD waiting for ROS master..."; '
  printf 'until rosnode list >/dev/null 2>&1; do sleep 1; done; '
  printf 'echo "Starting Logos face HUD."; '
  printf 'exec %q' "$face_command"
)

log "Opening Logos face HUD on monitor '$monitor' with terminal profile '$profile'."
gnome-terminal \
  --geometry="$geometry" \
  --full-screen \
  --profile="$profile" \
  --title="$title" \
  -- bash -lc "$terminal_command"
