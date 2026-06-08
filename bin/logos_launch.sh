#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: logos_launch.sh [options]

Start a tmux dashboard for the full Logos stack.

Options:
  --session NAME        tmux session name, default: logos
  --workspace NAME      default cognition workspace, default: Logos
  --auto-cog            start cognition without waiting for Enter
  --delay SECONDS       pause between pane launches, default: 3
  --display DISPLAY     X display for gnome-terminal/browser, default: $DISPLAY or :0
  --no-terminal         create tmux session but do not open gnome-terminal
  --attach              attach in the current terminal after creating the session
  --reset               kill an existing tmux session with the same name first
  --no-browser          do not open http://localhost:5000 before cognition
  --no-face             skip the face terminal helper
  --no-nav              skip navigation launch
  --no-idle             skip idle state indicator
  -h, --help            show this help

Environment:
  LOGOS_MAIN_TERMINAL_PROFILE   gnome-terminal profile for the main dashboard
  LOGOS_MAIN_TERMINAL_GEOMETRY  gnome-terminal geometry, default: 160x48+0+0
  LOGOS_TMUX_WIDTH              detached tmux window width, default: 160
  LOGOS_TMUX_HEIGHT             detached tmux window height, default: 48
  LOGOS_COG_PANE_WIDTH          main cognition pane width, default: 72
USAGE
}

die() {
  printf 'logos_launch.sh: %s\n' "$*" >&2
  exit 1
}

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "$script_dir/.." && pwd)"
setup_file="$workspace_root/devel/setup.bash"
workspace_parent="$HOME/robot_workspaces"

session="logos"
workspace_name="Logos"
auto_cog=0
delay_seconds=3
open_terminal=1
attach_current=0
reset_session=0
open_browser=1
launch_face=1
launch_nav=1
launch_idle=1
display_value="${DISPLAY:-:0}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --session)
      [ "$#" -ge 2 ] || die "--session requires a name"
      session="$2"
      shift
      ;;
    --workspace)
      [ "$#" -ge 2 ] || die "--workspace requires a name"
      workspace_name="$2"
      shift
      ;;
    --auto-cog)
      auto_cog=1
      ;;
    --delay)
      [ "$#" -ge 2 ] || die "--delay requires a number"
      delay_seconds="$2"
      shift
      ;;
    --display)
      [ "$#" -ge 2 ] || die "--display requires a value"
      display_value="$2"
      shift
      ;;
    --no-terminal)
      open_terminal=0
      ;;
    --attach)
      attach_current=1
      open_terminal=0
      ;;
    --reset)
      reset_session=1
      ;;
    --no-browser)
      open_browser=0
      ;;
    --no-face)
      launch_face=0
      ;;
    --no-nav)
      launch_nav=0
      ;;
    --no-idle)
      launch_idle=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
  shift
done

case "$workspace_name" in
  */*|.*|*..*)
    die "workspace name must be a single directory name under ~/robot_workspaces"
    ;;
esac

command -v tmux >/dev/null 2>&1 || die "tmux was not found"

export DISPLAY="$display_value"
if [ -z "${XAUTHORITY:-}" ] && [ -f "$HOME/.Xauthority" ]; then
  export XAUTHORITY="$HOME/.Xauthority"
fi

if [ "$open_terminal" -eq 1 ]; then
  command -v gnome-terminal >/dev/null 2>&1 || die "gnome-terminal was not found; rerun with --attach or --no-terminal"
fi

if [ "$reset_session" -eq 1 ] && tmux has-session -t "$session" 2>/dev/null; then
  tmux kill-session -t "$session"
fi

if tmux has-session -t "$session" 2>/dev/null; then
  printf 'Logos tmux session already exists: %s\n' "$session"
else
  if [ -f "$setup_file" ]; then
    source_prefix="source '$setup_file' 2>/dev/null || true; export PATH='$script_dir':\$PATH; cd '$workspace_root'; "
  else
    source_prefix="export PATH='$script_dir':\$PATH; cd '$workspace_root'; "
  fi

  pane_count=0

  new_pane() {
    local title="$1"
    local command="$2"
    local history_command="$3"
    local history_entry
    local shell_command
    local tmux_command

    history_entry="$(shell_quote "$history_command")"
    shell_command="${source_prefix}printf '\\033]2;%s\\033\\\\' '$title'; echo '=== $title ==='; $command; status=\$?; echo; echo '=== $title exited with status '\$status' ==='; printf '%s\n' $history_entry >> \"\${HISTFILE:-\$HOME/.bash_history}\" 2>/dev/null || true; exec bash -i"
    tmux_command="bash -lc $(shell_quote "$shell_command")"

    if [ "$pane_count" -eq 0 ]; then
      tmux new-session \
        -d \
        -x "${LOGOS_TMUX_WIDTH:-160}" \
        -y "${LOGOS_TMUX_HEIGHT:-48}" \
        -s "$session" \
        -n bringup \
        "$tmux_command"
    else
      tmux split-window -t "${session}:0" "$tmux_command"
      tmux select-layout -t "${session}:0" tiled >/dev/null
    fi

    pane_count=$((pane_count + 1))
    sleep "$delay_seconds"
  }

  new_hold_pane() {
    local title="$1"
    new_pane "$title" "echo 'Ready.'" "echo 'Ready.'"
  }

  # Keep one simple shell alive for the lifetime of the dashboard. This also
  # anchors the tmux server before any ROS workload has a chance to exit.
  new_hold_pane "spare shell"

  new_pane "roscore" "roscore" "roscore"
  new_pane "chroma" "'$script_dir/logos_chroma.sh'" "$script_dir/logos_chroma.sh"
  new_pane "core" "LOGOS_FACE_TERM=0 '$script_dir/logos_core.sh'" "LOGOS_FACE_TERM=0 $script_dir/logos_core.sh"

  if [ "$launch_face" -eq 1 ]; then
    new_pane "face terminal" "'$script_dir/logos_face_term.sh' --quiet" "$script_dir/logos_face_term.sh --quiet"
  else
    new_hold_pane "face skipped"
  fi

  new_pane "speech to text" "'$script_dir/logos_stt.sh'" "$script_dir/logos_stt.sh"

  if [ "$launch_nav" -eq 1 ]; then
    new_pane "navigation" "'$script_dir/logos_nav.sh'" "$script_dir/logos_nav.sh"
  else
    new_hold_pane "navigation skipped"
  fi

  if [ "$launch_idle" -eq 1 ]; then
    new_pane "idle indicator" "'$script_dir/logos_idle.sh'" "$script_dir/logos_idle.sh"
  else
    new_hold_pane "idle skipped"
  fi

  cog_ready_channel="logos-cog-ready-$$"
  cog_intro_command="tmux wait-for '$cog_ready_channel'; clear; "
  cog_intro_command+="if command -v figlet >/dev/null 2>&1; then figlet 'LOOK HERE!'; else printf '\n==== LOOK HERE! ====\n\n'; fi; "
  cog_intro_command+="printf '%s\n' 'This pane launches Logos cognition.' 'Logos/ is the master API directory: ~/robot_workspaces/Logos/' 'Cloned workspaces conventionally use names matching Logos_*.' ''; "
  cog_intro_command+="printf '%s\n' 'Available cloned workspaces (Logos_*):'; "
  cog_intro_command+="if [ -d '$workspace_parent' ]; then matches=\$(find '$workspace_parent' -mindepth 1 -maxdepth 1 -type d -name 'Logos_*' -printf '  %f\n' | sort); if [ -n \"\$matches\" ]; then printf '%s\n' \"\$matches\"; else printf '  (none yet)\n'; fi; else printf '  (no ~/robot_workspaces directory found)\n'; fi; "
  cog_intro_command+="printf '\n%s\n%s\n\n' 'Enter an existing workspace to launch it, or type a new name to create a clone.' 'Leave blank to use the default shown below.'; "

  cog_command=""
  if [ "$open_browser" -eq 1 ]; then
    cog_command+="(sleep 4; xdg-open http://localhost:5000 >/dev/null 2>&1 || true) & "
  fi

  if [ "$auto_cog" -eq 1 ]; then
    new_pane "cognition" "$cog_intro_command$cog_command'$script_dir/logos_cog.sh' '$workspace_name'" "$script_dir/logos_cog.sh $workspace_name"
  else
    cog_prompt_command="printf 'Default workspace: %s\n' '$workspace_name'; read -r -p 'Workspace name: ' ws; ws=\${ws:-'$workspace_name'}; "
    new_pane "cognition ready" "$cog_intro_command$cog_prompt_command$cog_command'$script_dir/logos_cog.sh' \"\$ws\"" "$script_dir/logos_cog.sh $workspace_name"
  fi

  cognition_pane_index=$((pane_count - 1))
  tmux swap-pane -s "${session}:0.${cognition_pane_index}" -t "${session}:0.0"
  tmux select-pane -t "${session}:0.0"
  tmux select-layout -t "${session}:0" main-vertical >/dev/null
  tmux resize-pane -t "${session}:0.0" -x "${LOGOS_COG_PANE_WIDTH:-72}" >/dev/null 2>&1 || true
  tmux wait-for -S "$cog_ready_channel"
fi

if [ "$attach_current" -eq 1 ]; then
  exec tmux attach-session -t "$session"
fi

if [ "$open_terminal" -eq 1 ]; then
  geometry="${LOGOS_MAIN_TERMINAL_GEOMETRY:-160x48+0+0}"
  profile_args=()
  if [ -n "${LOGOS_MAIN_TERMINAL_PROFILE:-}" ]; then
    profile_args=(--profile="$LOGOS_MAIN_TERMINAL_PROFILE")
  fi

  gnome-terminal \
    --geometry="$geometry" \
    "${profile_args[@]}" \
    --title="Logos Launch" \
    -- bash -lc "tmux attach-session -t '$session'"
else
  printf 'Logos tmux session is ready: %s\n' "$session"
  printf 'Attach with: tmux attach -t %s\n' "$session"
fi
