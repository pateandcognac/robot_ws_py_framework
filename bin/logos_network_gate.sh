#!/usr/bin/env bash
set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
retry_seconds="${LOGOS_INTERNET_RETRY_SECONDS:-5}"
network_voice="${LOGOS_NETWORK_VOICE:-0}"

case "$retry_seconds" in
  ''|*[!0-9]*) retry_seconds=5 ;;
esac
[ "$retry_seconds" -ge 1 ] || retry_seconds=1

internet_available() {
  command -v curl >/dev/null 2>&1 || return 1
  curl \
    --fail \
    --silent \
    --output /dev/null \
    --connect-timeout 4 \
    --max-time 8 \
    https://www.google.com/generate_204
}

speak_status() {
  [ "$network_voice" = "1" ] || return 0
  "$script_dir/logos_boot_voice.sh" "$1" || true
}

open_wifi_settings() {
  if command -v gnome-control-center >/dev/null 2>&1; then
    (gnome-control-center wifi >/dev/null 2>&1 &)
    return 0
  fi
  if command -v nm-connection-editor >/dev/null 2>&1; then
    (nm-connection-editor >/dev/null 2>&1 &)
    return 0
  fi
  return 1
}

if [ "${LOGOS_REQUIRE_INTERNET:-1}" = "0" ]; then
  printf 'Internet check skipped (LOGOS_REQUIRE_INTERNET=0).\n'
  exit 0
fi

printf 'Checking internet connection...\n'
if internet_available; then
  printf 'Internet connection confirmed.\n'
  speak_status network-online
  exit 0
fi

printf '\nNo internet connection detected. Logos cognition needs cloud access.\n'
printf 'Opening Ubuntu Wi-Fi settings and retrying every %s seconds.\n' "$retry_seconds"
printf 'Press O to reopen Wi-Fi settings, or Q to cancel cognition startup.\n\n'
speak_status network-offline
open_wifi_settings || printf 'Could not open a graphical Wi-Fi settings tool.\n'

trap 'printf "\nInternet check cancelled; cognition was not launched.\n"; exit 130' INT TERM

while true; do
  choice=""
  if [ -t 0 ] && IFS= read -r -s -n 1 -t "$retry_seconds" choice; then
    case "$choice" in
      o|O)
        open_wifi_settings || printf '\nCould not open a graphical Wi-Fi settings tool.\n'
        ;;
      q|Q)
        printf '\nInternet check cancelled; cognition was not launched.\n'
        exit 1
        ;;
    esac
  elif [ ! -t 0 ]; then
    sleep "$retry_seconds"
  fi

  printf '\rStill waiting for internet... %-20s' "$(date '+%H:%M:%S')"
  if internet_available; then
    printf '\rInternet connection confirmed. Continuing cognition startup.          \n'
    speak_status network-online
    exit 0
  fi
done
