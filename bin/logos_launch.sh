#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: logos_launch.sh [options]

Start a tmux dashboard for the full Logos stack.

Options:
  --session NAME        tmux session name, default: logos
  --workspace NAME      default cognition workspace, default: Logos
  --time-workspace      use Logos_<crc32(epoch seconds)>; overrides --workspace
  --last-workspace      default to newest existing Logos_* workspace; with
                        interactive cognition, launch after 60 second countdown
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
  --no-login-notification
                        do not show the Ubuntu login reminder
  --boot-voice          narrate startup through progressively richer TTS
  -h, --help            show this help

Environment:
  LOGOS_LOAD_BASHRC             load ~/.bashrc through interactive Bash, default: 1
  LOGOS_BOOT_VOICE              enable narrated startup, default: 0
  LOGOS_LOGIN_NOTIFICATION      show Ubuntu login reminder, default: 1
  LOGOS_LOGIN_PASSWORD          login reminder password, default: robot
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

load_interactive_environment() {
  local script_path
  local reexec_command
  local arg

  [ "${LOGOS_LOAD_BASHRC:-1}" != "0" ] || return 0
  [ "${LOGOS_BASHRC_LOADED:-0}" != "1" ] || return 0
  [ -f "$HOME/.bashrc" ] || return 0

  script_path="$(readlink -f "${BASH_SOURCE[0]}")"
  reexec_command="export LOGOS_BASHRC_LOADED=1; exec $(shell_quote "$script_path")"
  for arg in "$@"; do
    reexec_command+=" $(shell_quote "$arg")"
  done

  exec bash -ic "$reexec_command"
}

load_interactive_environment "$@"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "$script_dir/.." && pwd)"
setup_file="$workspace_root/devel/setup.bash"
workspace_parent="$HOME/robot_workspaces"

session="logos"
workspace_name="Logos"
time_workspace=0
last_workspace=0
auto_cog=0
delay_seconds=3
open_terminal=1
attach_current=0
reset_session=0
open_browser=1
launch_face=1
launch_nav=1
launch_idle=1
boot_voice="${LOGOS_BOOT_VOICE:-0}"
show_login_notification="${LOGOS_LOGIN_NOTIFICATION:-1}"
login_password="${LOGOS_LOGIN_PASSWORD:-robot}"
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
    --time-workspace)
      time_workspace=1
      ;;
    --last-workspace)
      last_workspace=1
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
    --no-login-notification)
      show_login_notification=0
      ;;
    --boot-voice)
      boot_voice=1
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

find_latest_workspace() {
  local latest_workspace

  [ -d "$workspace_parent" ] || return 1
  latest_workspace="$(
    find "$workspace_parent" \
      -mindepth 1 \
      -maxdepth 1 \
      -type d \
      -name 'Logos_*' \
      -printf '%T@ %f\n' 2>/dev/null \
      | sort -nr \
      | head -n 1 \
      | sed 's/^[^ ]* //'
  )"
  [ -n "$latest_workspace" ] || return 1
  printf '%s\n' "$latest_workspace"
}

if [ "$time_workspace" -eq 1 ]; then
  command -v crc32 >/dev/null 2>&1 || die "crc32 was not found"
  epoch_seconds="$(date +%s)"
  workspace_crc="$(printf '%s' "$epoch_seconds" | crc32 /dev/stdin)"
  case "$workspace_crc" in
    [[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]])
      workspace_name="Logos_${workspace_crc}"
      ;;
    *)
      die "crc32 returned an unexpected value: $workspace_crc"
      ;;
  esac
  printf 'Generated time-based workspace: %s\n' "$workspace_name"
elif [ "$last_workspace" -eq 1 ]; then
  if latest_workspace="$(find_latest_workspace)"; then
    workspace_name="$latest_workspace"
    printf 'Selected latest existing workspace: %s\n' "$workspace_name"
  else
    printf 'No existing Logos_* workspace found under %s; using %s\n' "$workspace_parent" "$workspace_name"
  fi
fi

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

if [ "$show_login_notification" -eq 1 ] && command -v notify-send >/dev/null 2>&1; then
  notify-send \
    --urgency=critical \
    --expire-time=30000 \
    --icon=dialog-password \
    "Ubuntu login keyring authentication" \
    "Ubuntu may prompt you to unlock the login keyring during startup.

When it asks for a password, enter: $login_password" \
    >/dev/null 2>&1 || true
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

  boot_voice_stage() {
    [ "$boot_voice" -eq 1 ] || return 0
    "$script_dir/logos_boot_voice.sh" "$@" || true
  }

  boot_voice_stage volume
  boot_voice_stage linux

  # Keep one simple shell alive for the lifetime of the dashboard. This also
  # anchors the tmux server before any ROS workload has a chance to exit.
  new_hold_pane "spare shell"

  boot_voice_stage roscore
  new_pane "roscore" "roscore" "roscore"
  new_pane "chroma" "'$script_dir/logos_chroma.sh'" "$script_dir/logos_chroma.sh"

  boot_voice_stage core
  new_pane "core" "LOGOS_FACE_TERM=0 '$script_dir/logos_core.sh'" "LOGOS_FACE_TERM=0 $script_dir/logos_core.sh"

  if [ "$launch_face" -eq 1 ]; then
    new_pane "face terminal" "'$script_dir/logos_face_term.sh' --quiet" "$script_dir/logos_face_term.sh --quiet"
  else
    new_hold_pane "face skipped"
  fi

  boot_voice_stage piper
  new_pane "speech to text" "'$script_dir/logos_stt.sh' nemotron" "$script_dir/logos_stt.sh nemotron"

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

  boot_voice_stage ambient
  boot_voice_stage kokoro
  if [ "$show_login_notification" -eq 1 ]; then
    boot_voice_stage keyring
  fi
  if [ "$open_browser" -eq 1 ]; then
    boot_voice_stage browser
  fi
  boot_voice_stage workspace "$workspace_name" "$last_workspace" "$auto_cog"
  boot_voice_stage speakme

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
    cog_prompt_command="printf 'Default workspace: %s\n' '$workspace_name'; "
    if [ "$last_workspace" -eq 1 ]; then
      cog_prompt_command+="ws=''; timeout_limit=60; "
      cog_prompt_command+="for ((i=timeout_limit; i>0; i--)); do "
      cog_prompt_command+="printf '\\r[Default in %2ds: %s] Workspace name: \\e[K%s' \"\$i\" '$workspace_name' \"\$ws\"; "
      cog_prompt_command+="if IFS= read -r -s -n 1 -t 1 char; then "
      cog_prompt_command+="if [[ -z \"\$char\" ]]; then break; fi; "
      cog_prompt_command+="case \"\$char\" in \$'\\177'|\$'\\b') ws=\"\${ws%?}\" ;; *) ws+=\"\$char\" ;; esac; "
      cog_prompt_command+="((i++)); "
      cog_prompt_command+="fi; "
      cog_prompt_command+="done; printf '\\n'; ws=\${ws:-'$workspace_name'}; "
    else
      cog_prompt_command+="read -r -p 'Workspace name: ' ws; ws=\${ws:-'$workspace_name'}; "
    fi
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
