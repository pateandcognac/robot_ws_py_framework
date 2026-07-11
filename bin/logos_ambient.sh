#!/usr/bin/env python3
"""Quickly configure Logos's passive listening helpers.

Usage:
    logos_ambient.sh ambient_bool classifier_bool hotwords_json_list
"""

import sys
import time

import rospy
from std_msgs.msg import Bool, String


CONNECTION_WAIT_SECONDS = 0.25
FLUSH_WAIT_SECONDS = 0.05


def usage():
    print("Usage: {} ambient_bool classifier_bool hotwords_json_list".format(sys.argv[0]))
    print("Example: {} true true '[\"jarvis\", \"computer\"]'".format(sys.argv[0]))


def parse_bool(value, name):
    normalized = value.strip().lower()
    if normalized in ("true", "1"):
        return True
    if normalized in ("false", "0"):
        return False
    raise ValueError("{} must be true/false or 1/0 (got {!r})".format(name, value))


def wait_for_subscribers(publishers):
    """Give already-running ears a brief chance to complete TCPROS handshakes."""
    deadline = time.monotonic() + CONNECTION_WAIT_SECONDS
    while time.monotonic() < deadline and not rospy.is_shutdown():
        if all(publisher.get_num_connections() for publisher in publishers):
            return
        rospy.sleep(0.01)


def main():
    if len(sys.argv) != 4:
        usage()
        return 1

    try:
        ambient_enabled = parse_bool(sys.argv[1], "ambient_bool")
        classifier_enabled = parse_bool(sys.argv[2], "classifier_bool")
    except ValueError as exc:
        print("logos_ambient.sh: {}".format(exc), file=sys.stderr)
        usage()
        return 1

    rospy.init_node("logos_ambient", anonymous=True, disable_signals=True)
    publishers = (
        rospy.Publisher("/tts/is_speaking", Bool, queue_size=1),
        rospy.Publisher("/stt/ambient_listener/enable", Bool, queue_size=1),
        rospy.Publisher("/stt/audio_classifier/enable", Bool, queue_size=1),
        rospy.Publisher("/stt/hotword_listener/enable", String, queue_size=1),
    )

    wait_for_subscribers(publishers)
    publishers[0].publish(Bool(data=False))
    publishers[1].publish(Bool(data=ambient_enabled))
    publishers[2].publish(Bool(data=classifier_enabled))
    publishers[3].publish(String(data=sys.argv[3]))

    # publish() writes synchronously to connected peers; keep a tiny grace
    # period for transport threads without the old multi-second rostopic delay.
    rospy.sleep(FLUSH_WAIT_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
