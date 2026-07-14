#!/usr/bin/env python3

import rospy
import random
import time
import json 
import glob 
import os
import re
from threading import Lock
from dynamic_reconfigure.client import Client
from std_msgs.msg import String as RosString, Bool
from logos_msgs.msg import (
    MouthSine, EyeGazeX, EyeScaleX, EyeGazeY,
    EyeScaleY, EyeLidHeight, EyeLidAngle, EyeColor, SpeechData
)
from logos_framework.msg import CognitionOutput

# --- Emoji Parsing Logic (Preserved) ---
PRESET_EMOJIS = set()

"""
AMBIENT_TERMINAL_COLORS = [
    "bright_green", "bright_cyan", "bright_blue", "bright_magenta",
    "bright_yellow", "white", "green", "cyan"
]

AMBIENT_CODE_EXTENSIONS = {
    ".py", ".cpp", ".c", ".cc", ".h", ".hpp", ".launch", ".xml", ".cfg",
    ".yaml", ".yml", ".json", ".md", ".txt", ".msg", ".srv", ".action",
    ".cmake", ".sh", ".bash", ".html", ".css", ".js", ".ts"
}
"""
AMBIENT_TERMINAL_COLORS = [ # contrast with blue-ish idle face
    "bright_green", # "bright_cyan", "bright_blue", "bright_magenta", "bright_yellow",
    "grey", "green", "red" #, "cyan"
]


AMBIENT_CODE_EXTENSIONS = {
    ".py", ".cpp", ".launch", ".xml", ".cfg",
    ".yaml", ".msg", ".srv", ".action",
    ".cmake", ".sh", ".bash", ".html", ".css", ".js", ".ts"
}

AMBIENT_CODE_FILENAMES = {
    "CMakeLists.txt", "package.xml", "Dockerfile", "Makefile"
}

AMBIENT_CODE_EXCLUDED_DIRS = {
    ".git", ".venv", "__pycache__", "build", "devel", "install", "logs",
    "node_modules", ".pytest_cache", ".mypy_cache", ".catkin_tools"
}


def load_preset_emojis_once():
    global PRESET_EMOJIS
    if PRESET_EMOJIS: return

    emojis = set()
    preset_files_path = rospy.get_param('~emoji_preset_path', '/home/robot/robot_ws/animations/face/*.json')
    preset_files = glob.glob(preset_files_path)
    
    for preset_file in preset_files:
        try:
            with open(preset_file, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    for entry in data:
                        if 'emoji' in entry: emojis.add(entry['emoji'])
                elif isinstance(data, dict): emojis.update(data.keys())
        except Exception as e:
            rospy.logerr(f"Error loading emoji preset {preset_file}: {e}")
    PRESET_EMOJIS = emojis

def split_text_emoji(text, preset_emojis_set):
    if not preset_emojis_set: return [(text.strip(), "")]
    found_emoji = ""
    stripped_text = text.strip()
    sorted_emojis = sorted(list(preset_emojis_set), key=len, reverse=True)
    for emoji_candidate in sorted_emojis:
        if stripped_text.endswith(emoji_candidate):
            found_emoji = emoji_candidate
            stripped_text = stripped_text[:-len(emoji_candidate)].strip()
            break
    if stripped_text or found_emoji: return [(stripped_text, found_emoji)]
    elif text: return [(text.strip(), "")]
    else: return []

class FaceAmbienceNode:
    def __init__(self):
        rospy.init_node('face_ambience_node', anonymous=False)
        load_preset_emojis_once()

        # State tracking to prevent spamming the dynamic reconfigure server
        self.current_render_mode = "unknown" # 'active' or 'idle'

        # --- Parameters ---
        self.min_fps = rospy.get_param('~min_fps', 5)
        self.def_fps = rospy.get_param('~default_fps', 16)
        self.post_activity_duration = rospy.get_param('~post_activity_duration', 4.0) # Delay before going idle
        self.fps_step_interval = rospy.get_param('~fps_reduction_step_interval', 30.0)
        self.dyn_reconnect_interval = rospy.get_param('~dyn_reconnect_interval', 10.0)
        self.dyn_reconnect_timeout = rospy.get_param('~dyn_reconnect_timeout', 0.25)
        self.dyn_connect_timeout = rospy.get_param('~dyn_connect_timeout', 2.0)
        self.dyn_server_names = self._get_dyn_server_names()
        self.ambient_terminal_enabled = rospy.get_param('~ambient_terminal_enabled', True)
        self.ambient_terminal_burst_chance = rospy.get_param('~ambient_terminal_burst_chance', 0.60)
        self.ambient_terminal_clear_chance = rospy.get_param('~ambient_terminal_clear_chance', 0.01)
        self.ambient_terminal_min_interval = rospy.get_param('~ambient_terminal_min_interval', 0.05)
        self.ambient_terminal_line_duration = rospy.get_param('~ambient_terminal_line_duration', 0.0)
        self.ambient_terminal_max_output_chars = rospy.get_param('~ambient_terminal_max_output_chars', 320)
        self.ambient_terminal_min_burst_lines = rospy.get_param('~ambient_terminal_min_burst_lines', 1)
        self.ambient_terminal_max_burst_lines = rospy.get_param('~ambient_terminal_max_burst_lines', 8)
        self.ambient_terminal_idle_sleep_min = rospy.get_param('~ambient_terminal_idle_sleep_min', 0.025)
        self.ambient_terminal_idle_sleep_max = rospy.get_param('~ambient_terminal_idle_sleep_max', 1.0)
        self.ambient_terminal_max_indent = rospy.get_param('~ambient_terminal_max_indent', 34)
        # Chance of one visual blank line between adjacent terminal lines.
        # Set to 0.0 for dense output or 1.0 to space every line pair.
        self.ambient_terminal_blank_line_chance = rospy.get_param(
            '~ambient_terminal_blank_line_chance', 0.20)
        self.ambient_code_scan_root = rospy.get_param('~ambient_code_scan_root', '/home/robot/robot_ws')
        self.ambient_code_cache_files = rospy.get_param('~ambient_code_cache_files', 48)
        self.ambient_code_max_file_bytes = rospy.get_param('~ambient_code_max_file_bytes', 180000)
        self.ambient_code_max_lines_per_file = rospy.get_param('~ambient_code_max_lines_per_file', 360)
        self.idle_reactions_enabled = rospy.get_param('~idle_reactions_enabled', True)
        self.idle_reaction_ambient_topic = rospy.get_param(
            '~idle_reaction_ambient_topic', '/stt/ambient_listener/events')
        self.idle_reaction_classifier_topic = rospy.get_param(
            '~idle_reaction_classifier_topic', '/stt/audio_classifier/events')
        self.idle_reaction_classifier_extra_topic = rospy.get_param(
            '~idle_reaction_classifier_extra_topic', '/stt/audio_classifier')
        self.idle_reaction_duration = float(rospy.get_param('~idle_reaction_duration', 4.0))
        self.idle_reaction_min_interval = float(rospy.get_param('~idle_reaction_min_interval', 1.0))
        self.idle_reaction_ambient_max_age = float(rospy.get_param('~idle_reaction_ambient_max_age', 8.0))
        self.idle_reaction_classifier_max_age = float(rospy.get_param('~idle_reaction_classifier_max_age', 120.0))
        self.idle_reaction_classifier_min_score = float(rospy.get_param(
            '~idle_reaction_classifier_min_score', 0.3))
        self.idle_reaction_max_text_chars = int(rospy.get_param('~idle_reaction_max_text_chars', 160))
        self.idle_reaction_policy = rospy.get_param('~idle_reaction_policy', 'fuzzy,lut')
        # Debounce: an input must be quiet (no newer fragment/tick) for this
        # long before it triggers a reaction, so a burst of ASR fragments
        # settles into one reaction to the latest text instead of firing on
        # every partial. Transcription always outranks classifier sound.
        self.idle_reaction_debounce = float(rospy.get_param('~idle_reaction_debounce', 0.4))
        # Repeat suppression (classifier sounds only): once we react to a
        # sound label, mute that same label for this long so persistent /
        # common sounds (fans, typing, hum) don't re-trigger endlessly.
        self.ambient_sound_repeat_window = float(rospy.get_param(
            '~ambient_sound_repeat_window', 180.0))
        # Labels the classifier reaction must never publish (case-insensitive).
        self.ambient_sound_blocklist_path = rospy.get_param(
            '~ambient_sound_blocklist_path',
            '/home/robot/robot_ws/config/ambient_sound_blocklist.txt')
        self.ambient_terminal_burst_chance = min(1.0, max(0.0, float(self.ambient_terminal_burst_chance)))
        self.ambient_terminal_clear_chance = min(1.0, max(0.0, float(self.ambient_terminal_clear_chance)))
        self.ambient_terminal_min_interval = max(0.05, float(self.ambient_terminal_min_interval))
        self.ambient_terminal_line_duration = max(0.0, float(self.ambient_terminal_line_duration))
        self.ambient_terminal_max_output_chars = max(40, int(self.ambient_terminal_max_output_chars))
        self.ambient_terminal_min_burst_lines = max(1, int(self.ambient_terminal_min_burst_lines))
        self.ambient_terminal_max_burst_lines = max(
            self.ambient_terminal_min_burst_lines,
            int(self.ambient_terminal_max_burst_lines)
        )
        self.ambient_terminal_idle_sleep_min = max(0.05, float(self.ambient_terminal_idle_sleep_min))
        self.ambient_terminal_idle_sleep_max = max(
            self.ambient_terminal_idle_sleep_min,
            float(self.ambient_terminal_idle_sleep_max)
        )
        self.ambient_terminal_max_indent = max(0, int(self.ambient_terminal_max_indent))
        self.ambient_terminal_blank_line_chance = min(
            1.0, max(0.0, float(self.ambient_terminal_blank_line_chance)))
        self.ambient_code_cache_files = max(1, int(self.ambient_code_cache_files))
        self.ambient_code_max_file_bytes = max(1024, int(self.ambient_code_max_file_bytes))
        self.ambient_code_max_lines_per_file = max(20, int(self.ambient_code_max_lines_per_file))
        self.idle_reaction_duration = max(0.1, self.idle_reaction_duration)
        self.idle_reaction_min_interval = max(0.1, self.idle_reaction_min_interval)
        self.idle_reaction_ambient_max_age = max(0.0, self.idle_reaction_ambient_max_age)
        self.idle_reaction_classifier_max_age = max(0.0, self.idle_reaction_classifier_max_age)
        self.idle_reaction_classifier_min_score = min(
            1.0, max(0.0, self.idle_reaction_classifier_min_score))
        self.idle_reaction_max_text_chars = max(16, self.idle_reaction_max_text_chars)
        self.idle_reaction_debounce = max(0.0, self.idle_reaction_debounce)
        self.ambient_sound_repeat_window = max(0.0, self.ambient_sound_repeat_window)
        self.ambient_sound_blocklist = self._load_ambient_sound_blocklist(
            self.ambient_sound_blocklist_path)

        # --- Publishers ---
        self.sine_wave_pub = rospy.Publisher('/face/mouth/sine_wave', MouthSine, queue_size=10)
        self.gaze_x_pub = rospy.Publisher('face/eye_gaze_x', EyeGazeX, queue_size=10)
        self.scale_x_pub = rospy.Publisher('face/eye_scale_x', EyeScaleX, queue_size=10)
        self.gaze_y_pub = rospy.Publisher('face/eye_gaze_y', EyeGazeY, queue_size=10)
        self.scale_y_pub = rospy.Publisher('face/eye_scale_y', EyeScaleY, queue_size=10)
        self.lid_height_pub = rospy.Publisher('face/eye_lid_height', EyeLidHeight, queue_size=10)
        self.lid_angle_pub = rospy.Publisher('face/eye_lid_angle', EyeLidAngle, queue_size=10)
        self.color_pub = rospy.Publisher('face/eye_color', EyeColor, queue_size=10)
        self.state_mon_pub = rospy.Publisher('/face/state_mon', SpeechData, queue_size=10)
        self.output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)
        self.face_cmd_pub = rospy.Publisher('/face/emoji_command', RosString, queue_size=5)
        self.arm_cmd_pub = rospy.Publisher('/arm/emoji_command', RosString, queue_size=5)
        self.hud_event_pub = rospy.Publisher('/face/hud/event', RosString, queue_size=5)

        # --- State Management ---
        # Subscribers can fire immediately, so callback-visible fields must
        # exist before rospy.Subscriber objects are constructed.
        self.is_speaking = False
        self.last_activity_time = time.time()
        self.idle_sequence_start_time = None
        self.ambient_terminal_active = False
        self.next_ambient_terminal_time = 0.0
        self.ambient_code_cache = self._load_ambient_code_cache()
        self.latest_ambient_words = None
        self.latest_ambient_words_time = 0.0
        self.latest_classifier_sound = None
        self.latest_classifier_score = 0.0
        self.latest_classifier_time = 0.0
        self.last_idle_reaction_time = 0.0
        self.idle_reaction_seq = 0
        # Guards the latest_* inputs and recent_sound_reactions, which are
        # touched by subscriber callbacks and the reaction timer thread.
        self.idle_reaction_lock = Lock()
        # Normalized sound label -> last publish time (repeat suppression).
        self.recent_sound_reactions = {}
        self.dyn_client = None
        self.dyn_server_name = None
        self.last_dyn_connect_attempt = 0.0
        self.current_fps = self.def_fps

        # --- Subscribers ---
        # New boolean topic for speech status
        self.is_speaking_sub = rospy.Subscriber('/tts/is_speaking', Bool, self.handle_is_speaking)
        self.cognition_state_sub = rospy.Subscriber('/cognition/state', RosString, self.handle_cognition_state)
        self.python_interrupt_sub = rospy.Subscriber('/python/interrupt', RosString, self.handle_python_interrupt)
        if self.idle_reactions_enabled:
            self.ambient_listener_sub = rospy.Subscriber(
                self.idle_reaction_ambient_topic, RosString, self.handle_ambient_listener_event)
            self.audio_classifier_subs = [
                rospy.Subscriber(topic, RosString, self.handle_audio_classifier_event)
                for topic in self._idle_reaction_classifier_topics()
            ]

        # --- Dynamic Reconfigure Setup ---
        self._connect_dynamic_reconfigure(timeout=self.dyn_connect_timeout, log_warning=True)

        self._clear_status_hud()

        # Idle reactions publish from one fixed-cadence tick (not from the
        # input callbacks), so debounce + throttle + source-priority + repeat
        # suppression all live in a single place.
        if self.idle_reactions_enabled:
            self.idle_reaction_timer = rospy.Timer(
                rospy.Duration(0.1), self._tick_idle_reactions)

        rospy.loginfo("Face Ambience Node initialized.")

    # --- Callbacks ---

    def _get_dyn_server_names(self):
        server_names = rospy.get_param('~dynamic_reconfigure_servers', None)
        if server_names is None:
            server_name = rospy.get_param('~dynamic_reconfigure_server', None)
            if server_name:
                server_names = [server_name]
            else:
                # The split-pane HUD node is named logos_face_hud. Keep the
                # legacy logos_face fallback for the older face_node variants.
                server_names = ['logos_face_hud', 'logos_face']
        elif isinstance(server_names, str):
            server_names = [server_names]

        return [str(name).strip() for name in server_names if str(name).strip()]

    def _connect_dynamic_reconfigure(self, timeout=None, log_warning=False):
        timeout = self.dyn_reconnect_timeout if timeout is None else timeout
        self.last_dyn_connect_attempt = time.time()
        errors = []

        for server_name in self.dyn_server_names:
            try:
                client = Client(server_name, timeout=timeout)
                config = client.get_configuration()
                self.dyn_client = client
                self.dyn_server_name = server_name
                self.current_fps = config.get('fps', self.def_fps)
                rospy.loginfo(
                    f"Connected to dynamic reconfigure server {server_name}. "
                    f"Current FPS: {self.current_fps}"
                )
                return True
            except Exception as e:
                errors.append(f"{server_name}: {e}")

        self.dyn_client = None
        self.dyn_server_name = None
        if log_warning:
            rospy.logwarn(
                "Could not connect to face dynamic reconfigure server(s): "
                f"{'; '.join(errors)}. Running in open-loop mode."
            )
        return False

    def _ensure_dynamic_reconfigure(self):
        if self.dyn_client is not None:
            return True
        if time.time() - self.last_dyn_connect_attempt < self.dyn_reconnect_interval:
            return False
        return self._connect_dynamic_reconfigure(log_warning=False)

    def _restore_face_config_after_reconnect(self):
        had_client = self.dyn_client is not None
        if had_client or not self._ensure_dynamic_reconfigure():
            return

        if self.current_render_mode == "active":
            self.set_face_config(active_mode=True)
        elif self.current_render_mode == "idle":
            self.set_face_config(active_mode=False, specific_fps=self.current_fps, force_style=True)

    def handle_is_speaking(self, msg: Bool):
        self.is_speaking = msg.data
        if self.is_speaking:
            # We are currently speaking; reset idle timers
            self.reset_activity_timer()
        else:
            # We just stopped speaking; start the countdown now
            self.reset_activity_timer()

    def handle_cognition_state(self, msg: RosString):
        """
        Parses state text for emojis and treats cognition updates as 'activity'
        to keep the face responsive.
        """
        state_text = msg.data.strip()
        if not state_text: return

        # Treat cognition updates as activity
        self.reset_activity_timer()

        parsed = split_text_emoji(state_text, PRESET_EMOJIS)
        for text_part, emoji_part in parsed:
            if not text_part and not emoji_part: continue
            
            # Publish to state monitor (legacy behavior preserved)
            sd = SpeechData()
            sd.text_snippet = text_part
            sd.emoji = emoji_part
            sd.duration = 4.0 # Default fixed duration for state display
            self.state_mon_pub.publish(sd)

    def handle_python_interrupt(self, msg: RosString):
        interrupt_json = msg.data.strip()
        if interrupt_json:
            self._publish_status_hud_text(interrupt_json, color="bright_yellow")

        if self._is_terminal_chatter_state():
            self.reset_activity_timer()

    def handle_ambient_listener_event(self, msg: RosString):
        # Record only -- the reaction timer decides when/whether to publish.
        text = self._clean_idle_reaction_text(msg.data)
        if not text or text.lower() == "null":
            return
        with self.idle_reaction_lock:
            self.latest_ambient_words = text
            self.latest_ambient_words_time = time.time()

    def handle_audio_classifier_event(self, msg: RosString):
        # Record only. Blocklisted labels are already dropped in
        # _clean_classifier_label, so they never reach here.
        label, score = self._parse_audio_classifier_event(msg.data)
        if not label:
            return
        with self.idle_reaction_lock:
            self.latest_classifier_sound = label
            self.latest_classifier_score = score
            self.latest_classifier_time = time.time()

    # --- Core Logic ---

    def _is_terminal_chatter_state(self):
        return self.ambient_terminal_active and self.current_fps <= self.min_fps

    def _is_idle_window(self, now=None):
        now = time.time() if now is None else now
        return (not self.is_speaking) and (now - self.last_activity_time >= self.post_activity_duration)

    def reset_activity_timer(self):
        """Called whenever the robot speaks or thinks."""
        self.last_activity_time = time.time()
        self.idle_sequence_start_time = None # Reset the FPS reduction logic
        
        # Immediate reaction: High FPS, Active Rendering
        if self.current_render_mode != "active":
            self.set_face_config(active_mode=True)

    def set_face_config(self, active_mode=True, specific_fps=None, force_style=False):
        """
        active_mode=True: High FPS, Shades, Ordered8
        active_mode=False: ASCII, Random (FPS handled separately)
        """
        params = {}
        
        if active_mode:
            leaving_terminal_chatter = (
                self.ambient_terminal_active and
                self.current_fps <= self.min_fps
            )
            params = {
                "fps": self.def_fps,
                "dither_charset": "ascii",
                "dither_algorithm": "random"
            }
            self.current_render_mode = "active"
            self.current_fps = self.def_fps
            self._stop_ambient_terminal(clear_hud=leaving_terminal_chatter)
        else:
            # Entering idle mode rendering style
            # We do NOT set FPS here, because FPS drops gradually in the loop
            entering_idle = self.current_render_mode != "idle"
            if entering_idle or force_style:
                params = {
                    "dither_charset": "shades",
                    "dither_algorithm": "random"
                }
            if entering_idle:
                self.current_render_mode = "idle"
                self._publish_arm_emoji_command("🧍", duration=2.0)
                self._send_feedback("[IDLE]")

        # If a specific FPS is requested (during gradual reduction), override it
        if specific_fps is not None:
            params['fps'] = specific_fps
            self.current_fps = specific_fps

        if params:
            if not self._ensure_dynamic_reconfigure():
                return

            try:
                self.dyn_client.update_configuration(params)
            except Exception as e:
                rospy.logdebug(f"Failed to update {self.dyn_server_name} config: {e}")
                self.dyn_client = None
                self.dyn_server_name = None

    def manage_idle_state(self):
        """
        Handles the gradual FPS reduction. 
        Only called when we are safely in the 'Idle' time window.
        """
        now = time.time()

        # 1. Initialize idle sequence if not started
        if self.idle_sequence_start_time is None:
            self.idle_sequence_start_time = now
            # Switch rendering style to ASCII/Random immediately
            self.set_face_config(active_mode=False)
            return

        # 2. Check if it's time to drop FPS
        # We calculate how many steps of reduction *should* have happened by now
        elapsed_idle = now - self.idle_sequence_start_time
        steps_taken = int(elapsed_idle / self.fps_step_interval)
        
        # Calculate target FPS based on steps
        target_fps = max(self.def_fps - steps_taken, self.min_fps)

        if target_fps != self.current_fps:
            rospy.loginfo(f"Reducing FPS to {target_fps} (Idle for {elapsed_idle:.1f}s)")
            self.set_face_config(active_mode=False, specific_fps=target_fps)

            if target_fps == self.min_fps:
                # clear hud 
                self._clear_status_hud()
                self._clear_face_hud_layer(0)
                self._start_ambient_terminal()

        if target_fps == self.min_fps:
            self._start_ambient_terminal()

    # --- Animation Generators (Preserved) ---

    def random_hex_color(self):
        # Return a cyan color with equal green and blue components (no red).
        # Components range from 0xBB to 0xFF.
        gb = random.randint(0x11, 0xFF)
        return '#00{0:02x}{0:02x}'.format(gb)
    

    def publish_random_idle_sine_wave(self):
        msg = MouthSine()
        msg.frequency = random.uniform(0.0001, 6.0)
        msg.amplitude = random.uniform(0.35, 1.0)
        msg.phase = random.uniform(-6.28, 6.28)
        msg.phase_increment = random.uniform(-0.78, 0.78)
        msg.duration = random.uniform(2.0, 6.0)
        msg.color = self.random_hex_color()
        self.sine_wave_pub.publish(msg)

    def publish_random_idle_eye_parameters(self):
        eye_side = random.choice(['both'])
        dur = random.uniform(0.5, 6)
        
        self.gaze_x_pub.publish(EyeGazeX(eye_side=eye_side, gaze_x=random.uniform(-1, 1), duration=dur))
        self.gaze_y_pub.publish(EyeGazeY(eye_side=eye_side, gaze_y=random.uniform(-1, 1), duration=dur))
        
        dur = random.uniform(0.5, 6)
        self.scale_x_pub.publish(EyeScaleX(eye_side=eye_side, scale_x=random.uniform(0.3, .75), duration=dur))
        self.scale_y_pub.publish(EyeScaleY(eye_side=eye_side, scale_y=random.uniform(0.3, .75), duration=dur))
        
        dur = random.uniform(0.5, 6)
        self.lid_height_pub.publish(EyeLidHeight(eye_side=eye_side, lid_height=random.uniform(-0.5, 0.85), duration=dur))
        self.color_pub.publish(EyeColor(eye_side=eye_side, color=self.random_hex_color(), duration=dur))
        
        eye_side_lid = random.choice(['left', 'right', 'both'])
        self.lid_angle_pub.publish(EyeLidAngle(eye_side=eye_side_lid, lid_angle=random.randint(-15, 30), duration=random.uniform(0.5, 6)))

    def _start_ambient_terminal(self):
        if not self.ambient_terminal_enabled or self.ambient_terminal_active:
            return

        self.ambient_terminal_active = True
        self.next_ambient_terminal_time = 0.0
        lines = [
            self._indent_ambient_terminal_line("[idle] terminal ambience online"),
            self._indent_ambient_terminal_line("[idle] cached {} source files".format(len(self.ambient_code_cache))),
        ]
        self._publish_face_terminal_lines(lines, color="bright_green")

    def _stop_ambient_terminal(self, clear_hud=False):
        if not self.ambient_terminal_active:
            return

        self.ambient_terminal_active = False
        self.next_ambient_terminal_time = 0.0
        if clear_hud:
            self._clear_face_hud_layer(0)

    def maybe_publish_ambient_terminal_burst(self):
        if not self.ambient_terminal_active or self.current_fps > self.min_fps:
            return

        now = time.time()
        if now < self.next_ambient_terminal_time:
            return

        self.next_ambient_terminal_time = now + random.uniform(
            self.ambient_terminal_min_interval,
            max(self.ambient_terminal_min_interval, self.ambient_terminal_min_interval * 6.0)
        )

        if random.random() < self.ambient_terminal_clear_chance:
            self._clear_face_hud_layer(0)
            if random.random() < 0.55:
                return

        if random.random() > self.ambient_terminal_burst_chance:
            return

        line_budget = random.randint(
            self.ambient_terminal_min_burst_lines,
            self.ambient_terminal_max_burst_lines
        )
        lines = self._read_random_ambient_code_chunk(line_budget)

        self._publish_face_terminal_lines(lines, color=random.choice(AMBIENT_TERMINAL_COLORS))

    def _load_ambient_code_cache(self):
        candidates = self._ambient_code_candidates()
        if not candidates:
            rospy.logwarn("Ambient terminal code cache found no source files.")
            return []

        random.shuffle(candidates)
        cache = []
        for path in candidates:
            entry = self._read_ambient_code_file(path)
            if entry:
                cache.append(entry)
            if len(cache) >= self.ambient_code_cache_files:
                break

        rospy.loginfo(f"Ambient terminal cached {len(cache)} source files.")
        return cache

    def _ambient_code_candidates(self):
        root = os.path.abspath(os.path.expanduser(str(self.ambient_code_scan_root)))
        candidates = []
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in AMBIENT_CODE_EXCLUDED_DIRS and not d.startswith(".")
            ]
            for filename in filenames:
                path = os.path.join(current_root, filename)
                if not self._is_ambient_code_file(path, filename):
                    continue
                try:
                    if os.path.getsize(path) > self.ambient_code_max_file_bytes:
                        continue
                except OSError:
                    continue
                candidates.append(path)
        return candidates

    def _is_ambient_code_file(self, path, filename):
        if filename in AMBIENT_CODE_FILENAMES:
            return True
        _, ext = os.path.splitext(filename)
        return ext.lower() in AMBIENT_CODE_EXTENSIONS

    def _read_ambient_code_file(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw_lines = f.readlines(self.ambient_code_max_file_bytes)
        except Exception as e:
            rospy.logdebug(f"Ambient terminal could not read {path}: {e}")
            return None

        lines = []
        for line in raw_lines[:self.ambient_code_max_lines_per_file]:
            line = line.rstrip("\r\n")
            if line.strip():
                lines.append(line)

        if not lines:
            return None

        relpath = os.path.relpath(path, os.path.abspath(os.path.expanduser(str(self.ambient_code_scan_root))))
        return {
            "path": relpath,
            "lines": lines,
            "cursor": random.randrange(len(lines)),
        }

    def _read_random_ambient_code_chunk(self, line_budget):
        if not self.ambient_code_cache:
            return self._clean_ambient_output("code", ["[code] cache empty"])

        entry = random.choice(self.ambient_code_cache)
        lines = entry["lines"]
        start = entry["cursor"]
        chunk = []

        if random.random() < 0.18:
            chunk.append("// " + entry["path"])

        code_line_count = max(1, line_budget - len(chunk))
        for offset in range(code_line_count):
            chunk.append(lines[(start + offset) % len(lines)])

        entry["cursor"] = (start + code_line_count) % len(lines)
        if random.random() < 0.2:
            entry["cursor"] = random.randrange(len(lines))

        return self._clean_ambient_output(entry["path"], chunk[:line_budget])

    def _clean_ambient_output(self, name, lines):
        cleaned = []
        max_chars = self.ambient_terminal_max_output_chars
        for line in lines:
            line = str(line).replace("\t", "    ").rstrip()
            if not line.strip():
                continue
            cleaned.append(self._indent_ambient_terminal_line(line[:max_chars]))
        if cleaned:
            return cleaned
        return [self._indent_ambient_terminal_line("[{}] ok".format(name))]

    def _indent_ambient_terminal_line(self, line):
        if self.ambient_terminal_max_indent <= 0:
            return line

        if random.random() < 0.18:
            indent = random.randint(0, self.ambient_terminal_max_indent)
        else:
            indent = int(random.triangular(0, self.ambient_terminal_max_indent, 6))

        prefix = " " * indent
        if random.random() < 0.22:
            prefix += random.choice(["| ", ":: ", "> ", "... ", "    "])
        return prefix + line

    def _publish_face_terminal_lines(self, lines, color=None):
        if not lines:
            return

        # Keep spacing subtle: at most one empty row and never at either edge
        # of a terminal payload, so the HUD does not drift or look sparse.
        spaced_lines = [lines[0]]
        for line in lines[1:]:
            if random.random() < self.ambient_terminal_blank_line_chance:
                spaced_lines.append("")
            spaced_lines.append(line)

        payload = json.dumps({
            "pane": "face",
            "layer": 0,
            "kind": "text",
            "effect": "terminal",
            "text": "\n".join(spaced_lines),
            "color": color or random.choice(AMBIENT_TERMINAL_COLORS),
            "duration": self.ambient_terminal_line_duration,
        })
        try:
            self.hud_event_pub.publish(RosString(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish ambient terminal HUD event: {e}")

    def _clear_status_hud(self):
        payload = json.dumps({"pane": "status", "kind": "clear"})
        try:
            self.hud_event_pub.publish(RosString(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish status HUD clear: {e}")

    def _publish_status_hud_text(self, text, color="bright_white"):
        payload = json.dumps({
            "pane": "status",
            "kind": "text",
            "text": text,
            "color": color,
        })
        try:
            self.hud_event_pub.publish(RosString(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish status HUD text: {e}")

    def _clear_face_hud(self):
        payload = json.dumps({"pane": "face", "kind": "clear"})
        try:
            self.hud_event_pub.publish(RosString(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish face HUD clear: {e}")

    def _clear_face_hud_layer(self, layer):
        payload = json.dumps({"pane": "face", "layer": int(layer), "kind": "clear"})
        try:
            self.hud_event_pub.publish(RosString(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish face HUD layer clear: {e}")

    def _send_feedback(self, header):
        # Lightweight feedback helper
        payload = {"header": header, "body": "", "header_color": "bright_blue", "font": "terminal"}
        self.output_pub.publish(CognitionOutput(type='feedback', content=json.dumps(payload)))

    def _publish_arm_emoji_command(self, emoji, duration=3.0):
        payload = json.dumps({"emoji": emoji, "duration": duration})
        try:
            self.arm_cmd_pub.publish(RosString(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish arm emoji command: {e}")

    def _clean_idle_reaction_text(self, text):
        text = "" if text is None else str(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:self.idle_reaction_max_text_chars]

    def _parse_audio_classifier_event(self, raw):
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            rospy.logdebug(f"Ignoring malformed audio classifier event: {raw}")
            return None, 0.0

        best_label = None
        best_score = 0.0
        recent = data.get("recent") if isinstance(data, dict) else None
        if isinstance(recent, list):
            for sample in reversed(recent):
                if not isinstance(sample, dict):
                    continue
                sample_label = None
                sample_score = 0.0
                for category in sample.get("categories") or []:
                    label = self._clean_classifier_label(category.get("name"))
                    score = self._classifier_score(category)
                    if label and score > sample_score:
                        sample_label = label
                        sample_score = score
                if sample_label:
                    best_label = sample_label
                    best_score = sample_score
                    break

        if not best_label:
            categories = data.get("categories") if isinstance(data, dict) else None
            if isinstance(categories, list):
                best_label, best_score = self._best_classifier_category(categories)

        if not best_label and isinstance(data, dict):
            label = self._clean_classifier_label(data.get("name") or data.get("label"))
            score = self._classifier_score(data)
            if label:
                best_label = label
                best_score = score

        if not best_label:
            for minute in data.get("per_minute", []) if isinstance(data, dict) else []:
                if not isinstance(minute, dict):
                    continue
                for category in minute.get("categories") or []:
                    label = self._clean_classifier_label(category.get("name"))
                    score = self._classifier_score(category, aggregate=True)
                    if label and score > best_score:
                        best_label = label
                        best_score = score

        if best_score < self.idle_reaction_classifier_min_score:
            return None, best_score
        return best_label, best_score

    def _best_classifier_category(self, categories):
        best_label = None
        best_score = 0.0
        for category in categories:
            if not isinstance(category, dict):
                continue
            label = self._clean_classifier_label(category.get("name") or category.get("label"))
            score = self._classifier_score(category)
            if label and score > best_score:
                best_label = label
                best_score = score
        return best_label, best_score

    def _idle_reaction_classifier_topics(self):
        topics = []
        for topic in (
                self.idle_reaction_classifier_topic,
                self.idle_reaction_classifier_extra_topic):
            topic = str(topic).strip()
            if topic and topic not in topics:
                topics.append(topic)
        return topics

    def _clean_classifier_label(self, label):
        label = self._clean_idle_reaction_text(label)
        if not label:
            return None
        if self._normalize_sound_label(label) in self.ambient_sound_blocklist:
            return None
        return label

    @staticmethod
    def _classifier_score(category, aggregate=False):
        if not isinstance(category, dict):
            return 0.0
        keys = ("score", "boosted_score", "avg_score") if not aggregate else (
            "boosted_score", "avg_score", "score")
        for key in keys:
            try:
                return float(category[key])
            except (KeyError, TypeError, ValueError):
                continue
        return 0.0

    def _tick_idle_reactions(self, _event=None):
        """
        Fixed-cadence reaction gate (10 Hz). Publishes at most one face
        reaction per throttle window, to a settled (debounced) input, with
        transcription outranking classifier sound and recently-seen sounds
        suppressed. Callbacks only stash the latest input; all the timing
        policy lives here.
        """
        if not self.idle_reactions_enabled:
            return
        now = time.time()
        if not self._is_idle_window(now):
            return
        if now - self.last_idle_reaction_time < self.idle_reaction_min_interval:
            return

        with self.idle_reaction_lock:
            candidate = self._pick_settled_reaction_locked(now)
            if not candidate:
                return
            source, text = candidate
            # Consume the latch so the same detection can't re-publish.
            if source == "ambient_words":
                self.latest_ambient_words = None
            else:
                self.latest_classifier_sound = None
                self._remember_sound_reaction_locked(text, now)

        self._publish_face_reaction(text, source)
        self.last_idle_reaction_time = now

    def _pick_settled_reaction_locked(self, now):
        """
        Choose the input to react to (caller holds idle_reaction_lock).
        Priority: a settled transcription snippet first; a pending-but-not-yet
        settled one holds the slot (classifier does NOT jump the queue). Then
        a settled, non-repeated classifier sound. Stale latches are dropped.
        """
        # Priority 1: transcription snippet.
        if self.latest_ambient_words:
            age = now - self.latest_ambient_words_time
            if age > self.idle_reaction_ambient_max_age:
                self.latest_ambient_words = None  # too old, drop it
            elif age >= self.idle_reaction_debounce:
                return "ambient_words", self.latest_ambient_words
            else:
                # Fresh transcription still settling -- hold, keep its priority
                # over sound rather than letting the classifier win the tick.
                return None

        # Priority 2: classifier sound.
        if self.latest_classifier_sound:
            age = now - self.latest_classifier_time
            if age > self.idle_reaction_classifier_max_age:
                self.latest_classifier_sound = None
            elif age >= self.idle_reaction_debounce and \
                    not self._sound_recently_reacted_locked(
                        self.latest_classifier_sound, now):
                return "ambient_sound", self.latest_classifier_sound
        return None

    @staticmethod
    def _normalize_sound_label(label):
        return re.sub(r"\s+", " ", str(label)).strip().lower()

    def _sound_recently_reacted_locked(self, label, now):
        if self.ambient_sound_repeat_window <= 0.0:
            return False
        ts = self.recent_sound_reactions.get(self._normalize_sound_label(label))
        return ts is not None and (now - ts) < self.ambient_sound_repeat_window

    def _remember_sound_reaction_locked(self, label, now):
        self.recent_sound_reactions[self._normalize_sound_label(label)] = now
        if len(self.recent_sound_reactions) > 256:
            cutoff = now - self.ambient_sound_repeat_window
            self.recent_sound_reactions = {
                k: t for k, t in self.recent_sound_reactions.items() if t >= cutoff}

    def _load_ambient_sound_blocklist(self, path):
        """
        Read the never-react classifier labels from a plain-text file (one
        label per line, # comments and blank lines ignored). "silence" and
        "speech" are always blocked even if the file is missing.
        """
        labels = {"silence", "speech"}
        try:
            with open(os.path.expanduser(str(path)), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.split("#", 1)[0]
                    norm = self._normalize_sound_label(line)
                    if norm:
                        labels.add(norm)
            rospy.loginfo("Ambient sound blocklist: %d labels from %s",
                          len(labels), path)
        except FileNotFoundError:
            rospy.logwarn("Ambient sound blocklist %s not found; using defaults %s.",
                          path, sorted(labels))
        except Exception as e:
            rospy.logwarn("Failed to load ambient sound blocklist %s: %s", path, e)
        return labels

    def _publish_face_reaction(self, text, source):
        self.idle_reaction_seq += 1
        cue_id = "idle_reaction_{}_{}".format(int(time.time() * 1000), self.idle_reaction_seq)
        payload = {
            "cue_id": cue_id,
            "text": text,
            "duration": self.idle_reaction_duration,
            "policy": self.idle_reaction_policy,
            "sync": 0.0,
            "save": False,
            "source": source,
        }
        try:
            self.face_cmd_pub.publish(RosString(data=json.dumps(payload, ensure_ascii=False)))
            rospy.loginfo("Idle face reaction from %s: %s", source, text)
        except Exception as e:
            rospy.logwarn(f"Failed to publish idle face reaction: {e}")

    def run(self):
        rospy.loginfo("Face Ambience Node running...")
        
        # Ensure we start in a known state
        self.reset_activity_timer()
        
        # Parameters for the loop sleep
        idle_anim_min = rospy.get_param('~idle_animation_interval_min', 2.0)
        idle_anim_max = rospy.get_param('~idle_animation_interval_max', 6.0)

        while not rospy.is_shutdown():
            self._restore_face_config_after_reconnect()
            current_time = time.time()
            
            # Determine if we are in the "Active" window or "Idle" window
            # Active if: Currently Speaking OR (Time since last activity < Delay)
            time_since_activity = current_time - self.last_activity_time
            is_active_window = self.is_speaking or (time_since_activity < self.post_activity_duration)

            if is_active_window:
                # Ensure we are in high performance mode
                if self.current_render_mode != "active":
                    self.set_face_config(active_mode=True)
                
                # While active, we loop quickly to remain responsive
                rospy.sleep(0.1)
                
            else:
                # We are in the Idle window
                self.manage_idle_state() # Handle FPS drop

                # Perform Idle Animations
                terminal_idle = self.ambient_terminal_active and self.current_fps <= self.min_fps
                sine_chance = 0.2 if terminal_idle else 0.6
                eye_chance = 0.35 if terminal_idle else 1.0
                if random.random() < sine_chance:
                    self.publish_random_idle_sine_wave()
                if random.random() < eye_chance:
                    self.publish_random_idle_eye_parameters()
                self.maybe_publish_ambient_terminal_burst()

                if terminal_idle:
                    sleep_duration = random.uniform(
                        self.ambient_terminal_idle_sleep_min,
                        self.ambient_terminal_idle_sleep_max
                    )
                else:
                    sleep_duration = random.uniform(idle_anim_min, idle_anim_max)
                
                rospy.sleep(sleep_duration)

if __name__ == '__main__':
    try:
        FaceAmbienceNode().run()
    except rospy.ROSInterruptException:
        pass
