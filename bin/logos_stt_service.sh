#!/usr/bin/env bash
# Manage the priority-enabled Logos STT system service.

set -euo pipefail

readonly SERVICE_NAME="logos-stt.service"
readonly UNIT_SOURCE="/home/robot/robot_ws/systemd/${SERVICE_NAME}"
readonly UNIT_DESTINATION="/etc/systemd/system/${SERVICE_NAME}"
readonly DROP_IN_DIR="/etc/systemd/system/${SERVICE_NAME}.d"

usage() {
    cat <<'EOF'
Usage: logos_stt_service.sh <command> [value]

Manage the Logos STT service, which runs as robot with a modest priority boost.

Commands:
  install                 Install the unit, enable it now and at boot, then start it.
  start|stop|restart      Control the service.
  status                  Show service status.
  logs                    Show the most recent service logs (100 by default).
  follow                  Follow service logs live (Ctrl-C to stop).
  enable|disable          Enable or disable automatic start at boot.
  set-backend <name>      Persist whisper or nemotron as the service backend.
  set-nice <level>        Persist a niceness level from -20 to 19; -5 is a good start.
  set-vad-only [seconds]  Finish after VAD silence (default 1.5 seconds).
  use-wakeword-finish     Restore end-of-line and cancel-that completion.

Examples:
  logos_stt_service.sh install
  logos_stt_service.sh set-backend nemotron
  logos_stt_service.sh set-nice -5
  logos_stt_service.sh set-vad-only 1.5
  logos_stt_service.sh restart
EOF
}

require_unit_source() {
    if [[ ! -f "$UNIT_SOURCE" ]]; then
        printf 'Service unit is missing: %s\n' "$UNIT_SOURCE" >&2
        exit 1
    fi
}

validate_nice_level() {
    local level="$1"
    if ! [[ "$level" =~ ^-?[0-9]+$ ]] || (( level < -20 || level > 19 )); then
        printf 'Niceness must be an integer between -20 and 19 (got %q)\n' "$level" >&2
        exit 2
    fi
}

write_drop_in() {
    local filename="$1"
    local setting="$2"

    sudo install -d -m 0755 "$DROP_IN_DIR"
    printf '[Service]\n%s\n' "$setting" | sudo tee "$DROP_IN_DIR/$filename" >/dev/null
    sudo systemctl daemon-reload
}

command_name="${1:-}"
case "$command_name" in
    install)
        require_unit_source
        sudo install -D -m 0644 "$UNIT_SOURCE" "$UNIT_DESTINATION"
        sudo systemctl daemon-reload
        sudo systemctl enable --now "$SERVICE_NAME"
        ;;
    start|stop|restart|status|enable|disable)
        sudo systemctl "$command_name" "$SERVICE_NAME"
        ;;
    logs)
        sudo journalctl -u "$SERVICE_NAME" -n "${LOGOS_STT_LOG_LINES:-100}" --no-pager
        ;;
    follow)
        sudo journalctl -u "$SERVICE_NAME" -f
        ;;
    set-backend)
        backend="${2:-}"
        case "$backend" in
            whisper|nemotron)
                write_drop_in backend.conf "Environment=LOGOS_STT_BACKEND=$backend"
                printf 'Set %s backend to %s. Run %s restart to apply it.\n' \
                    "$SERVICE_NAME" "$backend" "$0"
                ;;
            *)
                printf 'Backend must be whisper or nemotron.\n' >&2
                exit 2
                ;;
        esac
        ;;
    set-nice)
        nice_level="${2:-}"
        validate_nice_level "$nice_level"
        write_drop_in priority.conf "Nice=$nice_level"
        printf 'Set %s niceness to %s. Run %s restart to apply it.\n' \
            "$SERVICE_NAME" "$nice_level" "$0"
        ;;
    set-vad-only)
        vad_silence_timeout="${2:-1.5}"
        if ! [[ "$vad_silence_timeout" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then
            printf 'VAD silence timeout must be a positive number of seconds.\n' >&2
            exit 2
        fi
        write_drop_in vad-finish.conf $'Environment=LOGOS_STT_VAD_ONLY=1\nEnvironment=LOGOS_STT_VAD_SILENCE_TIMEOUT='"$vad_silence_timeout"
        printf 'Set VAD-only recording completion to %s seconds. Run %s restart to apply it.\n' \
            "$vad_silence_timeout" "$0"
        ;;
    use-wakeword-finish)
        sudo rm -f "$DROP_IN_DIR/vad-finish.conf"
        sudo systemctl daemon-reload
        printf 'Restored end-of-line and cancel-that completion. Run %s restart to apply it.\n' \
            "$0"
        ;;
    -h|--help|help|'')
        usage
        ;;
    *)
        printf 'Unknown command: %s\n\n' "$command_name" >&2
        usage >&2
        exit 2
        ;;
esac
