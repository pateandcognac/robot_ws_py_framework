#!/usr/bin/env python3
"""Bridge Logos live face state to the standalone Pico HUB75 protocol."""

import json
import math
import threading

import rospy
import serial
from logos_msgs.msg import AudioWave
from std_msgs.msg import String


class PicoFaceBridge:
    def __init__(self):
        self.device = rospy.get_param("~device", "/dev/ttyACM0")
        self.baud = int(rospy.get_param("~baud", 115200))
        self.brightness = max(0, min(96, int(rospy.get_param("~brightness", 24))))
        self.lock = threading.Lock()
        self.port = None
        self.face_line = None
        self.audio_line = None
        rospy.Subscriber("/face/live_state/json", String, self.face_cb, queue_size=1)
        rospy.Subscriber("/face/mouth/audio_wave", AudioWave, self.audio_cb, queue_size=1)
        self.timer = rospy.Timer(rospy.Duration(1.0 / 30.0), self.tick)

    @staticmethod
    def eye_array(eye):
        return [eye.get(k, d) for k, d in (
            ("gaze_x", 0.0), ("gaze_y", 0.0), ("scale_x", 1.0),
            ("scale_y", 1.0), ("lid_height", 1.0), ("lid_angle", 0.0))]

    def face_cb(self, msg):
        try:
            state = json.loads(msg.data)
            left, right, mouth = state["left_eye"], state["right_eye"], state["mouth"]
            packet = {
                "v": 1, "type": "face",
                "left": self.eye_array(left), "right": self.eye_array(right),
                "left_rgb": left.get("color", "#00ff00"),
                "right_rgb": right.get("color", "#00ff00"),
                "mouth": [mouth.get("frequency", 1.0), mouth.get("amplitude", 0.0),
                          mouth.get("phase", 0.0), mouth.get("phase_increment", 0.0)],
                "mouth_rgb": mouth.get("color", "#00ff00"),
            }
            line = json.dumps(packet, separators=(",", ":")) + "\n"
            with self.lock:
                self.face_line = line
        except (KeyError, TypeError, ValueError) as exc:
            rospy.logwarn_throttle(5.0, "Invalid /face/live_state/json: %s", exc)

    def audio_cb(self, msg):
        samples = list(msg.data)
        bands = []
        for index in range(16):
            start = index * len(samples) // 16
            end = (index + 1) * len(samples) // 16
            chunk = samples[start:end]
            rms = math.sqrt(sum(float(v) * v for v in chunk) / len(chunk)) if chunk else 0.0
            bands.append(min(255, int(rms * 255.0 / 32767.0 * 3.0)))
        line = json.dumps({"v": 1, "type": "audio", "bands": bands},
                          separators=(",", ":")) + "\n"
        with self.lock:
            self.audio_line = line

    def connect(self):
        if self.port and self.port.is_open:
            return True
        try:
            self.port = serial.Serial(self.device, self.baud, timeout=0, write_timeout=0.1)
            self.port.write((json.dumps({"v": 1, "type": "config",
                                         "brightness": self.brightness},
                                        separators=(",", ":")) + "\n").encode())
            rospy.loginfo("Pico face connected at %s (brightness %d/255, cap 96)",
                          self.device, self.brightness)
            return True
        except (OSError, serial.SerialException) as exc:
            self.port = None
            rospy.logwarn_throttle(5.0, "Waiting for Pico face at %s: %s", self.device, exc)
            return False

    def tick(self, _event):
        if not self.connect():
            return
        with self.lock:
            lines = (self.face_line, self.audio_line)
            self.face_line = self.audio_line = None
        try:
            if self.port.in_waiting:
                self.port.read(self.port.in_waiting)
            for line in lines:
                if line:
                    self.port.write(line.encode())
        except (OSError, serial.SerialException):
            try:
                self.port.close()
            except Exception:
                pass
            self.port = None


if __name__ == "__main__":
    rospy.init_node("pico_face_bridge")
    PicoFaceBridge()
    rospy.spin()
