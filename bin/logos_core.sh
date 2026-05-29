#!/usr/bin/env bash
set -e

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${LOGOS_FACE_TERM:-1}" != "0" ]; then
  "$script_dir/logos_face_term.sh" --quiet \
    || echo "logos_core.sh: continuing without launching the face terminal." >&2
fi

exec roslaunch logos_bringup logos_core.launch
