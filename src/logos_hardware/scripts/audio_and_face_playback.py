#!/usr/bin/env python3

import rospy
import json
import glob
import numpy as np
import sounddevice as sd
from queue import Queue
import threading

from logos_msgs.msg import (
    SpeechData,
    EyeGazeX,
    EyeGazeY,
    EyeScaleX,
    EyeScaleY,
    EyeLidHeight,
    EyeLidAngle,
    EyeColor,
    AudioWave,
    MouthSine
)
from std_msgs.msg import String, Bool

class EmotivePlaybackNode:
    def __init__(self):
        rospy.init_node('face_and_audio_playback_node', anonymous=False)
        
        self.message_queue = Queue()
        self.emoji_presets = self.load_emoji_presets()
        
        # Publishers for facial animations (Existing)
        self.pubs = {
            'eyegazex': rospy.Publisher('face/eye_gaze_x', EyeGazeX, queue_size=10),
            'eyegazey': rospy.Publisher('face/eye_gaze_y', EyeGazeY, queue_size=10),
            'eyescalex': rospy.Publisher('face/eye_scale_x', EyeScaleX, queue_size=10),
            'eyescaley': rospy.Publisher('face/eye_scale_y', EyeScaleY, queue_size=10),
            'eyelidheight': rospy.Publisher('face/eye_lid_height', EyeLidHeight, queue_size=10),
            'eyelidangle': rospy.Publisher('face/eye_lid_angle', EyeLidAngle, queue_size=10),
            'eyecolor': rospy.Publisher('face/eye_color', EyeColor, queue_size=10),
            'mouthsine': rospy.Publisher('face/mouth/sine_wave', MouthSine, queue_size=10),
        }
        
        # Publisher for audio wave 
        self.audio_wave_pub = rospy.Publisher('/face/mouth/audio_wave', AudioWave, queue_size=10)
        
        # Is Speaking Publisher (to turn it off)
        self.is_speaking_pub = rospy.Publisher('/tts/is_speaking', Bool, queue_size=1, latch=True)

        # Subscriber for speech sequence
        rospy.Subscriber('/face/tts_chunk', SpeechData, self.speech_sequence_callback)
        
        # Subscriber for direct emoji commands
        rospy.Subscriber('/face/emoji_command', String, self.emoji_command_callback)

        rospy.loginfo("Emotive Playback Node Online. Waiting for speech or commands...")


    def load_emoji_presets(self):
        """Load preset emojis from JSON files."""
        presets = {}
        # Ensure this path matches your new workspace structure
        preset_files = glob.glob('/home/robot/robot_ws/animations/face/*.json')
        
        for preset_file in preset_files:
            try:
                with open(preset_file, 'r') as f:
                    preset_data = json.load(f)
                    if isinstance(preset_data, list):
                        for entry in preset_data:
                            emoji = entry.get("emoji")
                            frames = entry.get("frames")
                            if emoji and frames:
                                presets[emoji] = frames
                    else:
                        rospy.logwarn(f"Unexpected JSON structure in {preset_file}")
            except Exception as e:
                rospy.logerr(f"Error reading preset {preset_file}: {e}")
        
        rospy.loginfo(f"Total loaded emoji presets: {len(presets)}")
        return presets

    def speech_sequence_callback(self, msg):
        """Enqueues incoming SpeechData messages."""
        self.message_queue.put(msg)

    def play_audio(self, audio_data, sample_rate):
        """
        Plays the audio data using sounddevice.
        Crucial: Uses the specific sample_rate from the message.
        """
        try:
            # If sample_rate is 0 or missing, fallback to standard Piper rate
            if sample_rate <= 0:
                sample_rate = 22050

            # publish is_speaking True
            self.is_speaking_pub.publish(Bool(data=True))

            # Convert incoming tuple/list to numpy int16 array
            audio_array = np.array(audio_data, dtype=np.int16)
            
            # Blocking playback (safe because this runs in a thread)
            sd.play(audio_array, samplerate=sample_rate)
            sd.wait()
            
        except Exception as e:
            rospy.logerr(f"Error playing audio: {str(e)}")

    def publish_animation(self, animation, duration):
        """
        Publishes facial animation commands based on frames.

        Behavior:
        - All animation frames are spread evenly across the full duration.
        - No looping.
        - MouthSine uses the same timing style as before: half of the frame step.
        """
        if not animation:
            return

        if duration <= 0.0:
            duration = 0.5

        frame_count = len(animation)
        if frame_count == 0:
            return

        step_duration = duration / frame_count

        for frame in animation:
            for state_obj in frame:
                try:
                    state_type = state_obj["state"]
                    params = state_obj["parameters"]

                    msg_class = globals().get(state_type)
                    if not msg_class:
                        rospy.logwarn(f"Unknown state type: {state_type}")
                        continue

                    msg = msg_class(**params)

                    if state_type.lower() == "mouthsine":
                        msg.duration = max(0.05, step_duration) # / 2.0)
                    else:
                        msg.duration = step_duration

                    pub_key = state_type.lower()
                    if pub_key in self.pubs:
                        self.pubs[pub_key].publish(msg)
                    else:
                        rospy.logwarn(f"No publisher found for state type: {state_type}")

                except KeyError as e:
                    rospy.logerr(f"Malformed animation state, missing key {e}: {state_obj}")
                except Exception as e:
                    rospy.logerr(f"Error publishing animation frame: {e}")

            rospy.sleep(step_duration)

    def play_audio_threaded(self, audio_data, sample_rate):
        """Spawns a thread for audio playback."""
        thread = threading.Thread(target=self.play_audio, args=[audio_data, sample_rate])
        thread.start()
        return thread

    def emoji_command_callback(self, msg):
        """
        Parses JSON command and injects it into the queue as a silent SpeechData msg.
        Expected format: {"emoji": "🤖", "duration": 3.0}
        """
        try:
            data = json.loads(msg.data)
            emoji = data.get("emoji", "")
            duration = float(data.get("duration", 1.0))
            
            if emoji:
                # Create a synthetic SpeechData message
                # We use total_chunks = 0 to indicate this is NOT part of a TTS stream
                # so we don't mess with the is_speaking flag.
                silent_msg = SpeechData()
                silent_msg.emoji = emoji
                silent_msg.duration = duration
                silent_msg.audio_data = [] # Silence
                silent_msg.sample_rate = 0
                silent_msg.current_chunk_index = 0
                silent_msg.total_chunks = 0 
                
                self.message_queue.put(silent_msg)
                rospy.loginfo(f"Queued manual emoji: {emoji} for {duration}s")
        except json.JSONDecodeError:
            rospy.logerr(f"Invalid JSON in emoji_command: {msg.data}")
        except Exception as e:
            rospy.logerr(f"Error processing emoji command: {e}")

    def process_message(self, msg):
        """
        Orchestrates Audio + Animation.
        """
        # 1. Publish Audio Wave (if audio exists)
        if msg.sample_rate > 0 and len(msg.audio_data) > 0:
            audio_wave_msg = AudioWave()
            audio_wave_msg.data = msg.audio_data
            audio_wave_msg.sample_rate = msg.sample_rate
            self.audio_wave_pub.publish(audio_wave_msg)

        # 2. Determine Animation
        animation = []
        if msg.emoji in self.emoji_presets:
            animation = self.emoji_presets[msg.emoji]
        elif msg.emoji:
            rospy.logwarn_throttle(5, f"Emoji '{msg.emoji}' requested but not found in presets.")
        
        # 3. Start Execution
        # Start Audio Thread (only if data exists)
        audio_thread = None
        if len(msg.audio_data) > 0:
            # Fallback rate
            sr = msg.sample_rate if msg.sample_rate > 0 else 22050
            audio_thread = self.play_audio_threaded(msg.audio_data, sr)
        
        # Play Animation (Main Thread)
        if animation:
            self.publish_animation(animation, msg.duration)
        else:
            # No animation? Just wait.
            if msg.duration > 0:
                rospy.sleep(msg.duration)

        # 4. Sync
        if audio_thread:
            audio_thread.join()

        # 5. CHECK FOR END OF STREAM
        # Only toggle flag if this was a valid TTS stream (total_chunks > 0)
        if msg.total_chunks > 0:
            # If this was the last chunk (indices are 0-based)
            if msg.current_chunk_index == msg.total_chunks - 1:
                rospy.loginfo("Speech sequence finished. Clearing is_speaking flag.")
                self.is_speaking_pub.publish(Bool(data=False))
                
    def run(self):
        """Main Loop."""
        rate = rospy.Rate(20)  # Increased to 20Hz for snappier queue pickup
        while not rospy.is_shutdown():
            if not self.message_queue.empty():
                msg = self.message_queue.get()
                self.process_message(msg)
            else:
                # Sleep to save CPU when idle
                rate.sleep()

def main():
    try:
        node = EmotivePlaybackNode()
        node.run()
    except rospy.ROSInterruptException:
        pass

if __name__ == '__main__':
    main()