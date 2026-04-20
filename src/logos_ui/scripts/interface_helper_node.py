#!/usr/bin/env python3

import rospy
import threading
import queue
import shutil
import os
import random
import time
import json
import re
from pydub import AudioSegment
from pydub.playback import _play_with_simpleaudio
from pydub.exceptions import CouldntDecodeError

# ROS Imports
from logos_msgs.msg import SpeechData
from logos_framework.msg import CognitionInput, CognitionOutput
from std_msgs.msg import String
from pyfiglet import Figlet

# ANSI color codes
COLOR_RESET = "\033[0m"
COLOR_BLACK = "\033[30m"
COLOR_RED = "\033[31m"
COLOR_GREEN = "\033[32m"
COLOR_YELLOW = "\033[33m"
COLOR_BLUE = "\033[34m"
COLOR_MAGENTA = "\033[35m"
COLOR_CYAN = "\033[36m"
COLOR_WHITE = "\033[37m"
COLOR_GREY = "\033[90m"
COLOR_BRIGHT_RED = "\033[91m"
COLOR_BRIGHT_GREEN = "\033[92m"
COLOR_BRIGHT_YELLOW = "\033[93m"
COLOR_BRIGHT_BLUE = "\033[94m"
COLOR_BRIGHT_MAGENTA = "\033[95m"
COLOR_BRIGHT_CYAN = "\033[96m"
COLOR_BRIGHT_WHITE = "\033[97m"

# Map string names to codes for dynamic JSON usage
COLOR_MAP = {
    "red": COLOR_RED, "green": COLOR_GREEN, "yellow": COLOR_YELLOW,
    "blue": COLOR_BLUE, "magenta": COLOR_MAGENTA, "cyan": COLOR_CYAN,
    "white": COLOR_WHITE, "grey": COLOR_GREY, "bright_red": COLOR_BRIGHT_RED,
    "bright_green": COLOR_BRIGHT_GREEN, "bright_yellow": COLOR_BRIGHT_YELLOW,
    "bright_blue": COLOR_BRIGHT_BLUE, "bright_magenta": COLOR_BRIGHT_MAGENTA,
    "bright_cyan": COLOR_BRIGHT_CYAN, "bright_white": COLOR_BRIGHT_WHITE
}

# Supported sound file extensions
SUPPORTED_EXTENSIONS = ['.mp3', '.wav', '.ogg', '.flac', '.aac']
ROOT_SOUND_DIR = os.path.expanduser("~/logos_ws/sound_files")

class InterfaceHelperNode:
    def __init__(self):
        rospy.init_node('interface_helper_node', anonymous=True)

        # Subscribers
        rospy.Subscriber('/face/tts_chunk', SpeechData, self.tts_callback)
        rospy.Subscriber('/cognition/input', CognitionInput, self.cognition_input_callback)
        rospy.Subscriber('/cognition/output', CognitionOutput, self.cognition_output_callback)

        # Message queue
        self.msg_queue = queue.Queue()

        # Locks for thread-safe operations
        self.playback_lock = threading.Lock()
        self.print_lock = threading.Lock()

        # Flag and buffer for handling figlet text interactions
        self.figlet_printing = False
        self.print_buffer = queue.Queue()

        # Current playback object
        self.current_playback = None

        # Start the message processing thread
        self.processing_thread = threading.Thread(target=self.process_messages)
        self.processing_thread.daemon = True
        self.processing_thread.start()

        rospy.loginfo("Interface Helper Node Started. Ready for IO.")

    # --- PRINTING LOGIC ---

    def safe_print(self, text, hold_lock=False, is_figlet=False):
        """
        Thread-safe print method.
        If is_figlet is True, print immediately (locking if requested).
        If not figlet, but a figlet IS printing, buffer it.
        """
        if not is_figlet and self.figlet_printing:
            self.print_buffer.put(text)
        else:
            if hold_lock:
                with self.print_lock:
                    print(text)
            else:
                # Try to acquire lock without blocking, else just print
                if self.print_lock.acquire(blocking=False):
                    try:
                        print(text)
                    finally:
                        self.print_lock.release()
                else:
                    print(text)

    def flush_print_buffer(self):
        """Flush all buffered non-figlet messages."""
        while not self.print_buffer.empty():
            text = self.print_buffer.get()
            with self.print_lock:
                print(text)
            self.print_buffer.task_done()

    # --- CALLBACKS ---

    def tts_callback(self, msg):
        """Handle /face/tts_chunk messages."""
        # Stop audio playback on new speech (voice overrides sound effects)
        self.stop_current_playback()
        self.msg_queue.put(('tts_chunk', msg))

    def cognition_input_callback(self, msg: CognitionInput):
        """Handle inputs to the brain."""
        self.msg_queue.put(('input', msg))

    def cognition_output_callback(self, msg: CognitionOutput):
        """Handle outputs from the brain."""
        self.msg_queue.put(('output', msg))

    # --- MESSAGE PROCESSING ---

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

    def handle_cognition_input(self, msg: CognitionInput):
        """Color-code and print inputs based on type and loop state."""
        content = msg.content.strip()
        if not content:
            return

        color = COLOR_WHITE # Default

        if msg.type in ["py_result", "py_async"]:
            color = COLOR_BRIGHT_GREEN if msg.loop_cognition else COLOR_BRIGHT_RED  
        elif msg.type == "system":
            color = COLOR_BRIGHT_BLUE if msg.loop_cognition else COLOR_BRIGHT_CYAN
        elif msg.type == "human":
            color = COLOR_BRIGHT_WHITE
        elif msg.type == "human_stt":
            color = COLOR_BRIGHT_WHITE
        elif msg.type in ["context"]:
            # Do not print context injections
            return
        else:
            # Unknown type, skip or print grey
            return 

        formatted = f"{color}<{msg.type}>\n{content}\n</{msg.type}>{COLOR_RESET}"
        self.safe_print(formatted)

    def handle_cognition_output(self, msg: CognitionOutput):
        """Handle outputs: chunks, feedback, and thoughts."""
        
        if msg.type == "chunk":
            # final response stream
            formatted = f"{COLOR_BRIGHT_MAGENTA}{msg.content}{COLOR_RESET}"
            self.safe_print(formatted)
            
        elif msg.type == "me":
            # Final message, usually ignored as chunks covered it
            pass

        elif msg.type == "feedback":
            try:
                data = json.loads(msg.content)
                self.render_feedback(data)
            except json.JSONDecodeError:
                rospy.logerr(f"Malformed JSON in feedback: {msg.content}")

        elif msg.type == "thoughts": # abstracted summary of internal monologue
            self.render_thoughts(msg.content)

    # --- RENDERING HELPERS ---

    def render_feedback(self, data):
        """Parse feedback JSON, play sound, render Figlet."""
        header = data.get("header")
        body = data.get("body")
        header_color_name = data.get("header_color", "magenta")
        body_color_name = data.get("body_color", "white")
        font = data.get("font", "standard")
        sound_path_spec = data.get("sound_path")

        # 1. Handle Sound
        if sound_path_spec:
            self.trigger_sound(sound_path_spec)

        # 2. Handle Header (Figlet)
        if header:
            self.figlet_printing = True
            columns, _ = shutil.get_terminal_size(fallback=(80, 20))
            try:
                f = Figlet(font=font, width=columns)
                rendered = f.renderText(header)
            except Exception:
                f = Figlet(font='term', width=columns) # 'term' is plaintext
                rendered = f.renderText(header)

            color_code = COLOR_MAP.get(header_color_name.lower(), COLOR_MAGENTA)
            
            # Print block with lock
            with self.print_lock:
                print(f"{color_code}{rendered}{COLOR_RESET}")
            
            self.figlet_printing = False

        # 3. Handle Body
        if body:
            self.flush_print_buffer() # Ensure order
            b_color = COLOR_MAP.get(body_color_name.lower(), COLOR_WHITE)
            self.safe_print(f"{b_color}{body}{COLOR_RESET}")

    def render_thoughts(self, content):
        """
        Parse thoughts: **Header** Body
        Header: Light Blue Figlet
        Body: Grey
        """
        # Regex to find **Header** and the rest
        match = re.match(r'\*\*(.*?)\*\*\s*(.*)', content, re.DOTALL)
        
        header_text = ""
        body_text = content
        
        if match:
            header_text = match.group(1)
            body_text = match.group(2)
        
        # Render Header
        if header_text:
            self.figlet_printing = True
            columns, _ = shutil.get_terminal_size(fallback=(80, 20))
            
            font_name = 'computer'
            f = Figlet(font=font_name, width=columns)
            rendered = f.renderText(header_text)

            with self.print_lock:
                print(f"{COLOR_BRIGHT_BLUE}{rendered}{COLOR_RESET}")
            
            self.figlet_printing = False
            self.flush_print_buffer()

        # Render Body
        if body_text.strip():
            self.safe_print(f"{COLOR_WHITE}{body_text}{COLOR_RESET}")


    def handle_tts_chunk(self, msg):
        """Process tts_chunk messages (Scrolling Figlet)."""
        text_snippet = msg.text_snippet + "\n"
        duration = msg.duration

        columns, _ = shutil.get_terminal_size(fallback=(80, 20))
        fig = Figlet(font='thick', width=columns) # thick, thin, big, chunky, standard, computer, contessa, cybermedium, doom, fuzzy, nancyj, os2, pebbles, pepper, puffy, roman, rounded, script, slant, slscript, small, smscript, smslant, standard, stop, straight, threepoint  twopoint font for tts caption, 
        rendered_text = fig.renderText(text_snippet)

        lines = rendered_text.splitlines()
        num_lines = len(lines)
        delay_per_line = duration / num_lines if num_lines > 0 else 0

        self.figlet_printing = True

        for line in lines:
            colored_line = f"{COLOR_BRIGHT_MAGENTA}{line}{COLOR_RESET}"
            if line.strip():
                self.safe_print(colored_line, hold_lock=True, is_figlet=True)
            else:
                self.safe_print(colored_line, hold_lock=False, is_figlet=True)
            rospy.sleep(delay_per_line)

        self.figlet_printing = False
        self.flush_print_buffer()

    # --- AUDIO UTILS ---

    def trigger_sound(self, path_spec):
        """Resolves path (file or dir) and plays sound."""
        # Check if absolute path or relative to ROOT_SOUND_DIR
        full_path = path_spec
        if not os.path.exists(full_path):
            full_path = os.path.join(ROOT_SOUND_DIR, path_spec)
        
        if not os.path.exists(full_path):
            # rospy.logwarn(f"Sound path not found: {path_spec}")
            return

        target_file = None

        if os.path.isdir(full_path):
            # Pick random supported file
            files = [f for f in os.listdir(full_path) 
                     if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS]
            if files:
                target_file = os.path.join(full_path, random.choice(files))
        elif os.path.isfile(full_path):
            target_file = full_path

        if target_file:
            self.play_sound(target_file)

    def play_sound(self, filepath, volume=0.85):
        """Play a sound file, stopping previous SFX."""
        with self.playback_lock:
            if self.current_playback and self.current_playback.is_playing():
                self.current_playback.stop()

            try:
                audio = AudioSegment.from_file(filepath)
                adjusted_audio = audio + (volume * 100 - 100) # dBFS
                playback = _play_with_simpleaudio(adjusted_audio)
                self.current_playback = playback
            except Exception as e:
                rospy.logerr(f"Audio Error {filepath}: {e}")

    def stop_current_playback(self):
        """Stop audio."""
        with self.playback_lock:
            if self.current_playback and self.current_playback.is_playing():
                self.current_playback.stop()
                self.current_playback = None

    def run(self):
        rospy.spin()

if __name__ == '__main__':
    try:
        node = InterfaceHelperNode()
        node.run()
    except rospy.ROSInterruptException:
        pass