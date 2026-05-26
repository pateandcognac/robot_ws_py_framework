#!/usr/bin/env python3

import json
import os
import queue
import random
import re
import threading

import rospy
from pydub import AudioSegment
from pydub.playback import _play_with_simpleaudio

from logos_msgs.msg import SpeechData
from logos_framework.msg import CognitionInput, CognitionOutput
from std_msgs.msg import String


SUPPORTED_EXTENSIONS = ['.mp3', '.wav', '.ogg', '.flac', '.aac']
ROOT_SOUND_DIR = os.path.expanduser("~/robot_workspaces/shared/sound_files")


class FaceHudBridgeNode:
    def __init__(self):
        rospy.init_node('face_hud_bridge_node', anonymous=True)

        self.event_pub = rospy.Publisher('/face/hud/event', String, queue_size=50)
        rospy.Subscriber('/face/tts_chunk', SpeechData, self.tts_callback)
        rospy.Subscriber('/cognition/input', CognitionInput, self.cognition_input_callback)
        rospy.Subscriber('/cognition/output', CognitionOutput, self.cognition_output_callback)

        self.msg_queue = queue.Queue()
        self.playback_lock = threading.Lock()
        self.current_playback = None

        self.processing_thread = threading.Thread(target=self.process_messages)
        self.processing_thread.daemon = True
        self.processing_thread.start()

        rospy.loginfo("Face HUD Bridge Node started.")

    def publish_event(self, pane, kind, text="", color="bright_white", bg_color="black", font=None, **extra):
        payload = {
            "pane": pane,
            "kind": kind,
            "text": text,
            "color": color,
            "bg_color": bg_color,
        }
        if font:
            payload["font"] = font
        payload.update(extra)

        msg = String()
        msg.data = json.dumps(payload)
        self.event_pub.publish(msg)

    def tts_callback(self, msg):
        self.stop_current_playback()
        self.msg_queue.put(('tts_chunk', msg))

    def cognition_input_callback(self, msg):
        self.msg_queue.put(('input', msg))

    def cognition_output_callback(self, msg):
        self.msg_queue.put(('output', msg))

    def process_messages(self):
        while not rospy.is_shutdown():
            try:
                msg_type, msg = self.msg_queue.get(timeout=1)
                if msg_type == 'tts_chunk':
                    self.handle_tts_chunk(msg)
                elif msg_type == 'input':
                    self.handle_cognition_input(msg)
                elif msg_type == 'output':
                    self.handle_cognition_output(msg)
                self.msg_queue.task_done()
            except queue.Empty:
                continue

    def handle_tts_chunk(self, msg):
        text = msg.text_snippet.strip()
        if not text:
            return

        self.publish_event(
            "status",
            "caption",
            text=text,
            color="bright_magenta",
            font="thick",
            duration=msg.duration,
            current_chunk_index=msg.current_chunk_index,
            total_chunks=msg.total_chunks,
        )

    def handle_cognition_input(self, msg):
        content = msg.content.strip()
        if not content:
            return

        if msg.type in ["py_result", "py_async"]:
            color = "bright_green" if msg.loop_cognition else "bright_red"
        elif msg.type == "system":
            color = "bright_blue" if msg.loop_cognition else "bright_cyan"
        elif msg.type in ["human", "human_stt"]:
            color = "bright_white"
        elif msg.type == "context":
            return
        else:
            return

        formatted = f"<{msg.type}>\n{content}\n</{msg.type}>"
        self.publish_event("status", "text", text=formatted, color=color)

    def handle_cognition_output(self, msg):
        if msg.type == "chunk":
            content = msg.content.strip()
            if content:
                self.publish_event("status", "text", text=content, color="bright_magenta")
            return

        if msg.type == "me":
            return

        if msg.type == "feedback":
            try:
                self.render_feedback(json.loads(msg.content))
            except json.JSONDecodeError:
                rospy.logerr(f"Malformed JSON in feedback: {msg.content}")
            return

        if msg.type == "thoughts":
            self.render_thoughts(msg.content)

    def render_feedback(self, data):
        header = data.get("header")
        body = data.get("body")
        header_color = data.get("header_color", "magenta")
        body_color = data.get("body_color", "white")
        font = data.get("font", "standard")
        sound_path = data.get("sound_path")

        if sound_path:
            self.trigger_sound(sound_path)

        if header:
            self.publish_event(
                "status",
                "figlet",
                text=str(header),
                color=self.normalize_color_name(header_color),
                font=font,
            )

        if body:
            self.publish_event(
                "status",
                "text",
                text=str(body),
                color=self.normalize_color_name(body_color),
            )

    def render_thoughts(self, content):
        match = re.match(r'\*\*(.*?)\*\*\s*(.*)', content, re.DOTALL)
        header_text = ""
        body_text = content

        if match:
            header_text = match.group(1)
            body_text = match.group(2)

        if header_text:
            self.publish_event(
                "status",
                "figlet",
                text=header_text,
                color="bright_blue",
                font="computer",
            )

        if body_text.strip():
            self.publish_event("status", "text", text=body_text.strip(), color="white")

    def normalize_color_name(self, color):
        value = str(color or "white").strip().lower()
        aliases = {
            "grey": "gray",
            "bright_grey": "bright_white",
            "light_blue": "bright_blue",
            "light_green": "bright_green",
            "light_cyan": "bright_cyan",
            "light_red": "bright_red",
            "light_magenta": "bright_magenta",
            "light_yellow": "bright_yellow",
        }
        return aliases.get(value, value)

    def trigger_sound(self, path_spec):
        full_path = path_spec
        if not os.path.exists(full_path):
            full_path = os.path.join(ROOT_SOUND_DIR, path_spec)

        if not os.path.exists(full_path):
            return

        target_file = None
        if os.path.isdir(full_path):
            files = [
                f for f in os.listdir(full_path)
                if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
            ]
            if files:
                target_file = os.path.join(full_path, random.choice(files))
        elif os.path.isfile(full_path):
            target_file = full_path

        if target_file:
            self.play_sound(target_file)

    def play_sound(self, filepath, volume=0.85):
        with self.playback_lock:
            if self.current_playback and self.current_playback.is_playing():
                self.current_playback.stop()

            try:
                audio = AudioSegment.from_file(filepath)
                adjusted_audio = audio + (volume * 100 - 100)
                self.current_playback = _play_with_simpleaudio(adjusted_audio)
            except Exception as exc:
                rospy.logerr(f"Audio Error {filepath}: {exc}")

    def stop_current_playback(self):
        with self.playback_lock:
            if self.current_playback and self.current_playback.is_playing():
                self.current_playback.stop()
                self.current_playback = None

    def run(self):
        rospy.spin()


if __name__ == '__main__':
    try:
        node = FaceHudBridgeNode()
        node.run()
    except rospy.ROSInterruptException:
        pass
