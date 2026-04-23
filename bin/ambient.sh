#!/bin/bash
rostopic pub /tts/is_speaking std_msgs/Bool "data: False" -1
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 bool bool representing ambient transcription and hotword"
    exit 1
fi
rostopic pub /stt/ambient_listener/enable std_msgs/Bool "data: $1" -1
rostopic pub /stt/hotword_listener/enable std_msgs/Bool "data: $2" -1