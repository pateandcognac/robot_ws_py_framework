#!/home/robot/robot_ws/.venv/bin/python3.11
"""
Compatibility shim: the canonical schema module moved to
src/logos_hardware/scripts/performance_lib/arm_schema.py so the runtime
nodes and these tools share one implementation. This re-exports everything.
"""

import os
import sys

_SCRIPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "logos_hardware", "scripts")
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from performance_lib.arm_schema import *  # noqa: F401,F403
from performance_lib.arm_schema import (  # noqa: F401
    ArmSchemaError,
    compile_semantic_to_legacy,
    expand_semantic_arm_frames,
    legacy_to_semantic,
    validate_semantic_arm_sequence,
)
