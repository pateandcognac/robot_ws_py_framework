#!/usr/bin/env python3

import rospy
import actionlib
import re
import numpy as np
import glob
import json
import time
import requests
import io
import scipy.io.wavfile as wavfile

from std_msgs.msg import Bool
from logos_msgs.msg import SpeechData
from logos_msgs.msg import SpeakAction, SpeakGoal, SpeakResult, SpeakFeedback 

# Global variable for preset emojis
PRESET_EMOJIS = set()
g_is_speaking_pub = None

# Larynx (Voice synthesizer) Server URL
LARYNX_URL = "http://localhost:5050/v1/audio"

def load_preset_emojis_once():
    """Loads preset emojis from JSON files."""
    global PRESET_EMOJIS
    if PRESET_EMOJIS: return PRESET_EMOJIS

    emojis = set()
    preset_files_path = rospy.get_param('~emoji_preset_path', '/home/robot/robot_ws/animations/face/*.json')
    preset_files = glob.glob(preset_files_path)

    if not preset_files:
        rospy.logwarn(f"No emoji preset files found at path: {preset_files_path}")

    for preset_file in preset_files:
        try:
            with open(preset_file, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    for entry in data:
                        if 'emoji' in entry: emojis.add(entry['emoji'])
                elif isinstance(data, dict):
                    emojis.update(data.keys())
        except Exception as e:
            rospy.logerr(f"Error reading emoji file {preset_file}: {e}")
    
    PRESET_EMOJIS = emojis
    rospy.loginfo(f"Loaded {len(PRESET_EMOJIS)} preset emojis.")
    return PRESET_EMOJIS

def split_text_emoji(text, preset_emojis):
    """Refined splitter logic (Same as your provided code)."""
    if not preset_emojis: return [(text, "")]
    sorted_emojis = sorted(list(preset_emojis), key=len, reverse=True)
    emoji_pattern = '|'.join(re.escape(emoji) for emoji in sorted_emojis)
    pattern = f'({emoji_pattern})'
    parts = re.split(pattern, text)

    final_result = []
    text_buffer = ""
    for segment in parts:
        if segment in preset_emojis:
            final_result.append((text_buffer.strip(), segment))
            text_buffer = ""
        else:
            text_buffer += segment
    
    if text_buffer.strip() or not final_result:
        # Handle trailing text or empty input edge cases
        if text_buffer.strip() or (not final_result and not text_buffer.strip() and text == ""):
             final_result.append((text_buffer.strip(), ""))

    filtered_result = [pair for pair in final_result if pair[0] or pair[1]]
    return filtered_result if filtered_result else [(text, "")]

def synthesize_audio_remote(text, engine, params_json):
    """
    Sends text to the Logos TTS Larynx server and returns (audio_int16, sample_rate).
    """
    if not text.strip():
        return np.array([], dtype=np.int16), 24000

    # Parse params
    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError:
        rospy.logwarn(f"Invalid JSON in engine_params: '{params_json}'. Using defaults.")
        params = {}

    payload = {
        "text": text,
        "engine": engine if engine else "kokoro", # Default to kokoro for now
        "params": params
    }

    try:
        response = requests.post(LARYNX_URL, json=payload, timeout=120.0) # timeout for safety
        
        if response.status_code != 200:
            rospy.logerr(f"TTS Server Error ({response.status_code}): {response.text}")
            return np.array([], dtype=np.int16), 24000

        # Read the WAV data from memory
        # scipy.io.wavfile.read returns (rate, data)
        # using io.BytesIO to simulate a file on disk
        rate, data = wavfile.read(io.BytesIO(response.content))

        # --- AUDIO CONVERSION MAGIC ---
        # Kokoro (and many ML models) often output Float32 (-1.0 to 1.0).
        # ROS messages and simple audio devices often prefer Int16 (-32768 to 32767).
        
        if data.dtype == np.float32 or data.dtype == np.float64:
            # Clip to prevent distortion, scale, and cast
            data = np.clip(data, -1.0, 1.0)
            data = (data * 32767).astype(np.int16)
        elif data.dtype == np.uint8:
            # Convert unsigned 8-bit to signed 16-bit
            data = (data.astype(np.float32) - 128) * 256
            data = data.astype(np.int16)
        # If it's already int16, we do nothing.

        return data, rate

    except requests.exceptions.RequestException as e:
        rospy.logerr(f"Failed to connect to Larynx TTS Server: {e}")
        return np.array([], dtype=np.int16), 24000
    except Exception as e:
        rospy.logerr(f"Error processing audio data: {e}")
        return np.array([], dtype=np.int16), 24000

class SpeakActionServer:
    def __init__(self, name):
        self._action_name = name
        self._speech_data_pub = rospy.Publisher('/face/tts_chunk', SpeechData, queue_size=10)
        
        global g_is_speaking_pub
        g_is_speaking_pub = rospy.Publisher('/tts/is_speaking', Bool, queue_size=1, latch=True)
        g_is_speaking_pub.publish(Bool(data=False)) 

        load_preset_emojis_once()

        self._as = actionlib.SimpleActionServer(self._action_name, SpeakAction, execute_cb=self.execute_cb, auto_start=False)
        self._as.start()
        rospy.loginfo(f"'{self._action_name}' Action Server Ready.")

    def set_is_speaking_state(self, speaking_status):
        global g_is_speaking_pub
        if g_is_speaking_pub:
            g_is_speaking_pub.publish(Bool(data=speaking_status))

    def execute_cb(self, goal: SpeakGoal):
            rospy.loginfo(f"Speak Goal: '{goal.utterance_text[:50]}...' via {goal.engine}")
            
            # 1. Turn flag ON. We do NOT turn it off in this node anymore.
            self.set_is_speaking_state(True)

            feedback = SpeakFeedback()
            result = SpeakResult()
            
            utterance_text = goal.utterance_text.replace('*', '')

            if not utterance_text.strip():
                self._as.set_succeeded(SpeakResult(success=False, final_message="Empty text", total_duration=0.0))
                # If empty, we turn it off immediately because playback node won't get anything
                self.set_is_speaking_state(False)
                return

            if not PRESET_EMOJIS:
                load_preset_emojis_once()

            chunks = split_text_emoji(utterance_text, PRESET_EMOJIS)
            feedback.total_chunks = len(chunks)
            total_calculated_duration = 0.0
            
            # Track success of synthesis
            chunks_sent = 0

            for i, (text_snippet, emoji_snippet) in enumerate(chunks):
                if self._as.is_preempt_requested():
                    self._as.set_preempted()
                    result.success = False
                    result.final_message = "Preempted."
                    # If preempted during synthesis, we must kill the flag
                    self.set_is_speaking_state(False)
                    return

                feedback.current_chunk_index = i
                
                # --- SYNTHESIS ---
                audio_data_np, sample_rate = synthesize_audio_remote(text_snippet, goal.engine, goal.engine_params)
                
                # --- DURATION ---
                if len(audio_data_np) > 0:
                    chunk_duration = len(audio_data_np) / float(sample_rate)
                else:
                    chunk_duration = 0.5
                
                feedback.chunk_duration = chunk_duration
                feedback.text_snippet = text_snippet
                feedback.emoji_snippet = emoji_snippet
                total_calculated_duration += chunk_duration

                # --- PUBLISH SPEECH DATA ---
                msg = SpeechData()
                msg.text_snippet = text_snippet
                msg.emoji = emoji_snippet if emoji_snippet in PRESET_EMOJIS else ""
                msg.audio_data = audio_data_np.tolist()
                msg.sample_rate = sample_rate
                msg.duration = chunk_duration
                # NEW FIELDS
                msg.current_chunk_index = i
                msg.total_chunks = len(chunks)

                self._speech_data_pub.publish(msg)
                
                self._as.publish_feedback(feedback)
                rospy.loginfo(f"  Chunk {i+1}/{len(chunks)} sent to playback.")
                chunks_sent += 1

            # --- IMMEDIATE SUCCESS ---
            # We no longer wait for playback time. Synthesis is done.
            if chunks_sent > 0:
                result.success = True
                result.final_message = "Synthesis Complete. Sent to playback."
            else:
                result.success = False
                result.final_message = "No chunks generated."
                self.set_is_speaking_state(False) # Fail safe

            result.total_duration = total_calculated_duration
            self._as.set_succeeded(result)
            # Note: We do NOT call self.set_is_speaking_state(False) here. 
            # The playback node handles that when the audio finishes.
            
if __name__ == '__main__':
    try:
        rospy.init_node('tts_action_server_node')
        # Params for presets path only
        rospy.set_param('~emoji_preset_path', rospy.get_param('~emoji_preset_path', '/home/robot/robot_ws/animations/face/*.json'))
        
        server = SpeakActionServer("speak")
        rospy.spin()
    except rospy.ROSInterruptException:
        pass