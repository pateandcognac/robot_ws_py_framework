#!/usr/bin/env python3

import rospy
import random
import time
import re 
import json 
import glob 
from threading import Lock
from dynamic_reconfigure.client import Client
from std_msgs.msg import String as RosString # Renamed to avoid conflict
from logos_msgs.msg import (
    MouthSine, EyeGazeX, EyeScaleX, EyeGazeY,
    EyeScaleY, EyeLidHeight, EyeLidAngle, EyeColor, SpeechData
)
from logos_framework.msg import CognitionOutput


# Global for preset emojis, loaded once
PRESET_EMOJIS = set()

# --- Emoji Parsing Logic  ---
def load_preset_emojis_once():
    global PRESET_EMOJIS
    if PRESET_EMOJIS: return

    emojis = set()
    preset_files_path = rospy.get_param('~emoji_preset_path', '/home/robot/robot_ws/animations/face/*.json')
    preset_files = glob.glob(preset_files_path)
    if not preset_files:
        rospy.logwarn(f"No emoji preset files found at path: {preset_files_path} for FaceAmbienceNode.")

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
    rospy.loginfo(f"FaceAmbienceNode: Loaded {len(PRESET_EMOJIS)} preset emojis.")

def split_text_emoji(text, preset_emojis_set):
    """
    Simple splitter for "text_content emoji" style input, or just emoji.
    Assumes /cognition/state messages are simple.
    Returns a list of (text_part, emoji_part) tuples.
    """
    if not preset_emojis_set: # No emojis to split by
        return [(text.strip(), "")]

    # Try to find an emoji at the end of the string
    found_emoji = ""
    stripped_text = text.strip()
    
    # Check for known emojis (longest first for greedy matching if necessary, though less critical here)
    sorted_emojis = sorted(list(preset_emojis_set), key=len, reverse=True)
    for emoji_candidate in sorted_emojis:
        if stripped_text.endswith(emoji_candidate):
            found_emoji = emoji_candidate
            stripped_text = stripped_text[:-len(emoji_candidate)].strip()
            break # Found the emoji

    if stripped_text or found_emoji:
        return [(stripped_text, found_emoji)]
    elif text: # Original text had content but didn't parse into text/emoji (e.g. only text, no known emoji)
        return [(text.strip(), "")]
    else: # Empty input
        return []


class FaceAmbienceNode:
    def __init__(self):
        rospy.init_node('face_ambience_node', anonymous=False)
        rospy.loginfo("Initializing Face Ambience Node...")

        # Load emojis
        load_preset_emojis_once()

        # Publishers for idle animations
        self.sine_wave_pub = rospy.Publisher('/face/mouth/sine_wave', MouthSine, queue_size=10)
        self.gaze_x_pub = rospy.Publisher('face/eye_gaze_x', EyeGazeX, queue_size=10)
        self.scale_x_pub = rospy.Publisher('face/eye_scale_x', EyeScaleX, queue_size=10)
        self.gaze_y_pub = rospy.Publisher('face/eye_gaze_y', EyeGazeY, queue_size=10)
        self.scale_y_pub = rospy.Publisher('face/eye_scale_y', EyeScaleY, queue_size=10)
        self.lid_height_pub = rospy.Publisher('face/eye_lid_height', EyeLidHeight, queue_size=10)
        self.lid_angle_pub = rospy.Publisher('face/eye_lid_angle', EyeLidAngle, queue_size=10)
        self.color_pub = rospy.Publisher('face/eye_color', EyeColor, queue_size=10)

        # Publisher for state monitor (silent animations)
        self.state_mon_pub = rospy.Publisher('/face/state_mon', SpeechData, queue_size=10)

        # Subscriber for actual SpeechData messages (from TTS Action Server)
        self.tts_chunk_sub = rospy.Subscriber('/face/tts_chunk', SpeechData, self.handle_tts_chunk_activity)
        # Subscriber for /cognition/state messages
        self.cognition_state_sub = rospy.Subscriber('/cognition/state', RosString, self.handle_cognition_state)
        self.output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)



        # Dynamic reconfigure client for setting FPS
        try:
            self.dyn_client = Client("logos_face", timeout=5) # Connect to your C++ face node
            self.face_node_params = self.dyn_client.get_configuration()
            self.current_fps = self.face_node_params.get('fps',8)
            self.min_fps = rospy.get_param('~min_fps', 4) # Get from param or default
            self.def_fps = rospy.get_param('~default_fps', 16) # Get from param or default
            rospy.loginfo(f"Connected to logos_face. Initial FPS: {self.current_fps}, Min FPS: {self.min_fps}, Default FPS: {self.def_fps}")
        except Exception as e:
            rospy.logerr(f"Failed to connect to logos_face for dynamic reconfigure or get params: {e}. Using default FPS values.")
            self.dyn_client = None
            self.current_fps = 8
            self.min_fps = 1
            self.def_fps = 8


        # Timer variables for pausing idle animations
        self.activity_pause_timer = 0.0 # Time until idle animations can resume
        self.fps_reduction_start_time = None  # Timestamp when FPS reduction can begin
        self.timer_lock = Lock()
        
        self.loop_rate = rospy.Rate(5)  # Main loop update rate (e.g., 5 Hz for managing timers)

        rospy.loginfo("Face Ambience Node initialized.")

    def random_hex_color(self):
        return '#{:06x}'.format(random.randint(0, 0x0000FF))

    def generate_random_sine_wave(self):
        msg = MouthSine()
        msg.frequency = random.uniform(0.0001, 6.0)
        msg.amplitude = random.uniform(0.35, 1.0)
        msg.phase = random.uniform(-2 * 3.14159, 2 * 3.14159)
        msg.phase_increment = random.uniform(-0.25*3.14, 0.25*3.14)
        msg.duration = random.uniform(2.0, 6.0)
        msg.color = self.random_hex_color()
        return msg

    def publish_random_idle_sine_wave(self):
        sine_wave_msg = self.generate_random_sine_wave()
        self.sine_wave_pub.publish(sine_wave_msg)
                      
    def publish_random_idle_eye_parameters(self):
        eye_side = random.choice(['both'])
        gaze_x = random.uniform(-1, 1)
        gaze_y = random.uniform(-1, 1)
        scale_x = random.uniform(0.3, .75)
        scale_y = random.uniform(0.3, .75)
        lid_height = random.uniform(-1.0, 0.75)
        lid_angle = random.randint(-15, 30)
        color = self.random_hex_color()
        # duration = random.uniform(0.5, self.current_fps / 2.25 if self.current_fps > 0 else 1.0) # guard for current_fps
        duration = random.uniform(0.5, 6)

        self.gaze_x_pub.publish(EyeGazeX(eye_side=eye_side, gaze_x=gaze_x, duration=duration))
        self.gaze_y_pub.publish(EyeGazeY(eye_side=eye_side, gaze_y=gaze_y, duration=duration))
        duration = random.uniform(0.5, 6)
        self.scale_x_pub.publish(EyeScaleX(eye_side=eye_side, scale_x=scale_x, duration=duration))
        self.scale_y_pub.publish(EyeScaleY(eye_side=eye_side, scale_y=scale_y, duration=duration))
        duration = random.uniform(0.5, 6)
        self.lid_height_pub.publish(EyeLidHeight(eye_side=eye_side, lid_height=lid_height, duration=duration))
        self.color_pub.publish(EyeColor(eye_side=eye_side, color=color, duration=duration))
        eye_side = random.choice(['left', 'right', 'both'])
        duration = random.uniform(0.5, 6)
        self.lid_angle_pub.publish(EyeLidAngle(eye_side=eye_side, lid_angle=lid_angle, duration=duration))

    def _trigger_activity_pause(self, duration: float):
        """Common function to pause idle animations and reset FPS."""
        with self.timer_lock:
            self.activity_pause_timer = time.time() + duration
            self._set_face_fps(self.def_fps)  # Set FPS to default (e.g., 16) immediately
            self.fps_reduction_start_time = None  # Reset FPS reduction countdown
        rospy.loginfo(f"Activity detected. Pausing idle animations for {duration:.2f}s. FPS set to {self.current_fps}.")

    def handle_tts_chunk_activity(self, msg: SpeechData):
        """Handles activity from actual speech chunks."""
        # Pause for slightly longer than the chunk itself to allow animations to finish smoothly
        # And to prevent idle animations from starting too abruptly between short speech chunks.
        pause_extension = rospy.get_param('~tts_pause_extension', 5.0) # extra seconds per chunk
        effective_pause_duration = (msg.duration * 2) + pause_extension
        self._trigger_activity_pause(effective_pause_duration)

    def handle_cognition_state(self, msg: RosString):
        """Handles /cognition/state messages for silent animations."""
        rospy.loginfo(f"Received cognition state: '{msg.data}'")
        state_text_full = msg.data.strip()

        if not state_text_full:
            rospy.logwarn("Received empty cognition state message.")
            return

        # Use the emoji parsing logic
        parsed_chunks = split_text_emoji(state_text_full, PRESET_EMOJIS)

        if not parsed_chunks:
            rospy.logwarn(f"Could not parse cognition state '{state_text_full}' into text/emoji.")
            # Optionally, still trigger a generic pause or use a default emoji/text
            # For now, we'll only proceed if we have a valid chunk.
            return

        total_pause_for_state = 0.0
        fixed_state_duration = rospy.get_param('~fixed_state_indicator_duration', 4.0)

        for text_part, emoji_part in parsed_chunks:
            if not text_part and not emoji_part:
                continue

            if emoji_part and emoji_part not in PRESET_EMOJIS:
                rospy.logwarn(f"State indicator uses emoji '{emoji_part}' not in presets. Animation might not occur.")
            
            speech_data_for_state = SpeechData()
            speech_data_for_state.text_snippet = text_part
            speech_data_for_state.emoji = emoji_part # Send even if not in presets, playback node might handle
            speech_data_for_state.audio_data = [0]  # No audio
            speech_data_for_state.duration = fixed_state_duration

            self.state_mon_pub.publish(speech_data_for_state)
            rospy.loginfo(f"Published state indicator to /face/state_mon: Text='{text_part}', Emoji='{emoji_part}', Duration={fixed_state_duration:.2f}s")
            total_pause_for_state += fixed_state_duration # Accumulate if a state message somehow had multiple chunks

        if total_pause_for_state > 0:
            self._trigger_activity_pause(total_pause_for_state)


    def _set_face_fps(self, fps_value: int):
        """Update the FPS of the logos_face using dynamic reconfigure."""
        if self.dyn_client is None:
            #rospy.logwarn_throttle(10, "Dynamic reconfigure client not available. Cannot set FPS.")
            return
        
        if self.current_fps == fps_value: # No change needed
            return

        try:
            self.dyn_client.update_configuration({"fps": fps_value})
            self.current_fps = fps_value
            rospy.loginfo(f"Face FPS set to {fps_value}.")
        except rospy.ServiceException as e:
            rospy.logwarn(f"Failed to set face FPS to {fps_value}: {e}")
        except Exception as e: # Catch other potential errors like client not connected
            rospy.logerr(f"Error updating face FPS (client may be disconnected): {e}")

    def _send_feedback(self, header, body="", sound_path=None, header_color="cyan", body_color="white", font="standard"):
        """Helper to send feedback state to the UI/Subtitler."""
        payload = {
            "header": header,
            "body": body,
            "sound_path": sound_path,
            "header_color": header_color,
            "body_color": body_color,
            "font": font
        }
        try:
            self.output_pub.publish(CognitionOutput(type='feedback', content=json.dumps(payload)))
        except Exception as e:
            rospy.logwarn(f"Failed to publish feedback: {e}")



    def manage_fps_reduction_timer(self):
        """Manage the gradual FPS reduction when idle."""
        current_time = time.time()
        with self.timer_lock:
            # If activity pause is over AND FPS reduction hasn't started, initiate it
            if self.activity_pause_timer <= current_time and self.fps_reduction_start_time is None:
                self.fps_reduction_start_time = current_time
                rospy.loginfo(f"Idle period started. FPS reduction countdown initiated from {self.current_fps} FPS.")
                self._send_feedback(header="Idle.", header_color="bright_blue", font="standard")

            # If FPS reduction is active
            if self.fps_reduction_start_time is not None:
                elapsed_since_reduction_start = current_time - self.fps_reduction_start_time
                
                # Define how long to wait at current FPS before reducing further
                # Example: wait 60s before first reduction, then 60s for each subsequent step
                time_before_next_reduction_step = rospy.get_param('~fps_reduction_step_interval', 15.0)

                if elapsed_since_reduction_start >= time_before_next_reduction_step and self.current_fps > self.min_fps:
                    new_fps = max(self.current_fps - 1, self.min_fps)
                    self._set_face_fps(new_fps)
                    self.fps_reduction_start_time = current_time # Reset timer for the next step down
                elif self.current_fps <= self.min_fps:
                    # Already at min FPS, no need to keep resetting fps_reduction_start_time
                    # It will stay None until next activity.
                    pass


    def run(self):
        rospy.loginfo("Face Ambience Node running...")
        # Initial FPS set based on dynamic reconfigure or default
        self._set_face_fps(self.def_fps) 

        idle_animation_interval_min = rospy.get_param('~idle_animation_interval_min', 2.0) # Min seconds between idle bursts
        idle_animation_interval_max = rospy.get_param('~idle_animation_interval_max', 6.0) # Max seconds

        while not rospy.is_shutdown():
            current_time = time.time()
            self.manage_fps_reduction_timer()

            perform_idle_animation = False
            with self.timer_lock:
                if self.activity_pause_timer <= current_time: # Check if pause duration has elapsed
                    perform_idle_animation = True
            
            if perform_idle_animation:
                # Publish random idle face states
                if random.random() < 0.6: # Chance to publish mouth wave
                    self.publish_random_idle_sine_wave()
                if random.random() < 1.0: # Chance to publish eye params
                    self.publish_random_idle_eye_parameters()
                
                # Sleep for a random interval before next idle animation burst
                # This interval should be influenced by current_fps to make it feel more "active" at higher FPS
                # Base interval + factor of current_fps
                base_sleep = random.uniform(idle_animation_interval_min, idle_animation_interval_max)
                # Make sleep shorter if FPS is higher, longer if FPS is lower
                # Inverse relationship: (def_fps / current_fps) can be a multiplier
                # Ensure current_fps is not zero
                fps_factor = (self.def_fps / self.current_fps) if self.current_fps > 0 else 1.0
                actual_sleep_duration = base_sleep * fps_factor
                # actual_sleep_duration = max(0.5, actual_sleep_duration) # Ensure some minimum sleep
                actual_sleep_duration = max(0.5, 6) # Ensure some minimum sleep

                rospy.sleep(actual_sleep_duration) 
            
            self.loop_rate.sleep() # Maintain overall loop rate for timer checks

if __name__ == '__main__':
    try:
        # Add ROS parameters for paths if not already handled by launch file
        rospy.set_param('~emoji_preset_path', rospy.get_param('~emoji_preset_path', '/home/robot/robot_ws/animations/face/*.json'))
        node = FaceAmbienceNode()
        node.run()
    except rospy.ROSInterruptException:
        rospy.loginfo("Face Ambience Node shutting down.")
    except Exception as e:
        rospy.logfatal(f"Critical error in FaceAmbienceNode: {e}\n{traceback.format_exc()}")