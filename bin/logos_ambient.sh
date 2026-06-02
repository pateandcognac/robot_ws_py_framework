#!/bin/bash
rostopic pub /tts/is_speaking std_msgs/Bool "data: False" -1
if [ "$#" -ne 3 ]; then
    echo "Usage: $0 ambient_bool classifier_bool hotwords_json_list"
    echo "Example: $0 true true '[\"jarvis\", \"computer\"]'"
    exit 1
fi
rostopic pub /stt/ambient_listener/enable std_msgs/Bool "data: $1" -1
rostopic pub /stt/audio_classifier/enable std_msgs/Bool "data: $2" -1
rostopic pub /stt/hotword_listener/enable std_msgs/String "data: '$3'" -1
