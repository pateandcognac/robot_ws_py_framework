#!/bin/bash

# 1. Define the path to your Python 3.11 venv
VENV_PYTHON="/home/robot/src/logos_tts_server/.venv/bin/python"

# 2. Define the path to your script
SCRIPT_PATH="/home/robot/src/logos_tts_server/tts_server.py"

# 3. Execute! 
# We do NOT pass "$@" here. This strips the ROS arguments (__name, __log)
# so your non-ROS python script doesn't crash.
exec $VENV_PYTHON $SCRIPT_PATH