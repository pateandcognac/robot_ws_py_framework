#!/usr/bin/env bash
set -euo pipefail

chroma_runner="${HOME}/src/logos_chroma_server/scripts/run_dev.sh"

if [[ ! -x "$chroma_runner" ]]; then
    printf 'Logos Chroma runner not found or not executable:\n  %s\n' "$chroma_runner" >&2
    exit 1
fi

exec "$chroma_runner"
