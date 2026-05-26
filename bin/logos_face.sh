#!/usr/bin/env bash
set -e

# rosrun logos_ui face_hud_bridge_node.py &
# bridge_pid=$!

# cleanup() {
#   kill "$bridge_pid" 2>/dev/null || true
# }
# trap cleanup EXIT INT TERM

rosrun logos_face face_hud_node
