#!/usr/bin/env bash
# PHOSPHOR face: vector-oscilloscope alternative to logos_face.sh.
# Same topics, same bridge; see src/logos_face/scripts/face_phosphor_README.md
set -e

rosrun logos_face face_phosphor_node.py "$@"
