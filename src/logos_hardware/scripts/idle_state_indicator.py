#!/usr/bin/env python3

import rospy
import random
import time
import json 
import glob 
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
        self.min_fps = rospy.get_param('~min_fps', 4)
        self.def_fps = rospy.get_param('~default_fps', 16)
        self.post_activity_duration = rospy.get_param('~post_activity_duration', 10.0) # Delay before going idle
        self.fps_step_interval = rospy.get_param('~fps_reduction_step_interval', 60.0)

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
        self.arm_cmd_pub = rospy.Publisher('/arm/emoji_command', RosString, queue_size=5)

        # --- Subscribers ---
        # New boolean topic for speech status
        self.is_speaking_sub = rospy.Subscriber('/tts/is_speaking', Bool, self.handle_is_speaking)
        self.cognition_state_sub = rospy.Subscriber('/cognition/state', RosString, self.handle_cognition_state)

        # --- Dynamic Reconfigure Setup ---
        self.dyn_client = None
        self.current_fps = self.def_fps
        try:
            self.dyn_client = Client("logos_face", timeout=5)
            config = self.dyn_client.get_configuration()
            self.current_fps = config.get('fps', self.def_fps)
            rospy.loginfo(f"Connected to logos_face. Current FPS: {self.current_fps}")
        except Exception as e:
            rospy.logwarn(f"Could not connect to logos_face: {e}. Running in open-loop mode.")

        # --- State Management ---
        self.is_speaking = False
        self.last_activity_time = time.time()
        
        # Tracks when we started the gradual FPS reduction logic
        self.idle_sequence_start_time = None 
      


        rospy.loginfo("Face Ambience Node initialized.")

    # --- Callbacks ---

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

    # --- Core Logic ---

    def reset_activity_timer(self):
        """Called whenever the robot speaks or thinks."""
        self.last_activity_time = time.time()
        self.idle_sequence_start_time = None # Reset the FPS reduction logic
        
        # Immediate reaction: High FPS, Active Rendering
        if self.current_render_mode != "active":
            self.set_face_config(active_mode=True)

    def set_face_config(self, active_mode=True, specific_fps=None):
        """
        active_mode=True: High FPS, Shades, Ordered8
        active_mode=False: ASCII, Random (FPS handled separately)
        """
        if self.dyn_client is None: return

        params = {}
        
        if active_mode:
            params = {
                "fps": self.def_fps,
                "dither_charset": "ascii",
                "dither_algorithm": "ordered8"
            }
            self.current_render_mode = "active"
            self.current_fps = self.def_fps
        else:
            # Entering idle mode rendering style
            # We do NOT set FPS here, because FPS drops gradually in the loop
            if self.current_render_mode != "idle":
                params = {
                    "dither_charset": "ascii",
                    "dither_algorithm": "random"
                }
                self.current_render_mode = "idle"
                self._publish_arm_emoji_command("🧍", duration=3.0)
                # Send feedback one time when switching to idle
                self._send_feedback("- - - idle - - -")

        # If a specific FPS is requested (during gradual reduction), override it
        if specific_fps is not None:
            params['fps'] = specific_fps
            self.current_fps = specific_fps

        if params:
            try:
                self.dyn_client.update_configuration(params)
            except Exception as e:
                rospy.logdebug(f"Failed to update config: {e}")

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

    # --- Animation Generators (Preserved) ---

    def random_hex_color(self):
        return '#{:06x}'.format(random.randint(0, 0x0000FF))

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

    def _send_feedback(self, header):
        # Lightweight feedback helper
        payload = {"header": header, "body": "", "header_color": "bright_blue", "font": "standard"}
        self.output_pub.publish(CognitionOutput(type='feedback', content=json.dumps(payload)))

    def _publish_arm_emoji_command(self, emoji, duration=3.0):
        payload = json.dumps({"emoji": emoji, "duration": duration})
        try:
            self.arm_cmd_pub.publish(RosString(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish arm emoji command: {e}")

    def run(self):
        rospy.loginfo("Face Ambience Node running...")
        
        # Ensure we start in a known state
        self.reset_activity_timer()
        
        # Parameters for the loop sleep
        idle_anim_min = rospy.get_param('~idle_animation_interval_min', 2.0)
        idle_anim_max = rospy.get_param('~idle_animation_interval_max', 6.0)

        while not rospy.is_shutdown():
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
                if random.random() < 0.6:
                    self.publish_random_idle_sine_wave()
                if random.random() < 1.0:
                    self.publish_random_idle_eye_parameters()

                sleep_duration = random.uniform(idle_anim_min, idle_anim_max)
                
                rospy.sleep(sleep_duration)

if __name__ == '__main__':
    try:
        FaceAmbienceNode().run()
    except rospy.ROSInterruptException:
        pass
