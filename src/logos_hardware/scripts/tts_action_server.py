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
import threading
from collections import deque
from std_msgs.msg import Bool, String
from logos_msgs.msg import SpeechData
from logos_msgs.msg import SpeakAction, SpeakGoal, SpeakResult, SpeakFeedback

from performance_lib.chunking import estimate_speech_duration, subchunk_pairs

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
    """
    Split text into (text, emoji) pairs where:
    - an emoji applies to the text immediately before it
    - only the first emoji in a consecutive emoji run is kept
    - whitespace-only gaps between emojis are treated as part of the same run
    - leading emojis are ignored
    - trailing text without an emoji is still returned
    - punctuation immediately after an emoji is moved before the emoji split
    """
    if not preset_emojis:
        stripped = text.strip()
        return [(stripped, "")] if stripped else []

    sorted_emojis = sorted(preset_emojis, key=len, reverse=True)
    emoji_pattern = "|".join(re.escape(emoji) for emoji in sorted_emojis)
    parts = re.split(f"({emoji_pattern})", text)

    results = []
    text_buffer = ""
    pending_emoji = ""
    in_emoji_run = False

    trailing_punctuation_pattern = re.compile(r"^(\s*)([.!?,;:…\"'“”‘’)\]\}]+)(\s*)(.*)$")

    def flush_buffer(with_emoji=""):
        nonlocal text_buffer
        stripped = text_buffer.strip()
        if stripped:
            results.append((stripped, with_emoji))
        text_buffer = ""

    i = 0
    while i < len(parts):
        part = parts[i]

        if not part:
            i += 1
            continue

        if part in preset_emojis:
            if text_buffer.strip():
                # First emoji after real text closes that utterance.
                if not pending_emoji:
                    pending_emoji = part

                # Look ahead for punctuation immediately after the emoji.
                if i + 1 < len(parts):
                    next_part = parts[i + 1]

                    if next_part and next_part not in preset_emojis:
                        match = trailing_punctuation_pattern.match(next_part)

                        if match:
                            leading_space, punctuation, after_punctuation_space, remainder = match.groups()

                            # Move punctuation before the emoji split.
                            text_buffer = text_buffer.rstrip() + punctuation

                            # Preserve sensible spacing before any remaining text.
                            #
                            # Example:
                            # "Text ⚛️?! More text"
                            #
                            # becomes:
                            # ("Text?!", "⚛️")
                            # ("More text", "")
                            parts[i + 1] = after_punctuation_space + remainder

                flush_buffer(with_emoji=pending_emoji)
                pending_emoji = ""
                in_emoji_run = True
            else:
                # No text yet: leading emoji, or extra emoji in a run. Ignore it.
                in_emoji_run = True

            i += 1
            continue

        # Non-emoji text
        if in_emoji_run:
            if part.isspace():
                # Whitespace between emojis still counts as same emoji run.
                i += 1
                continue

            in_emoji_run = False

        text_buffer += part
        i += 1

    # Any remaining text becomes an utterance with no emoji.
    flush_buffer(with_emoji="")

    return results

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
        # TTP v2: announce cues (text+emoji chunks) as soon as the utterance is
        # split, *before* synthesis, so the animator can generate face tracks
        # in parallel with TTS. JSON payload, see performance_sequencer_node.py.
        self._cue_announce_pub = rospy.Publisher('/performance/cue_announce', String, queue_size=10)
        self._utterance_counter = 0

        global g_is_speaking_pub
        g_is_speaking_pub = rospy.Publisher('/tts/is_speaking', Bool, queue_size=1, latch=True)
        g_is_speaking_pub.publish(Bool(data=False))

        load_preset_emojis_once()

        # FIFO queue of accepted goal handles
        self._goal_queue = deque()
        self._queue_lock = threading.Lock()
        self._queue_cond = threading.Condition(self._queue_lock)
        self._is_processing = False

        # Lower-level ActionServer gives us manual control over queueing
        self._as = actionlib.ActionServer(
            self._action_name,
            SpeakAction,
            goal_cb=self.goal_cb,
            cancel_cb=self.cancel_cb,
            auto_start=False
        )
        self._as.start()

        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

        rospy.loginfo(f"'{self._action_name}' Action Server Ready (queued mode).")

    def set_is_speaking_state(self, speaking_status):
        global g_is_speaking_pub
        if g_is_speaking_pub:
            g_is_speaking_pub.publish(Bool(data=speaking_status))

    def goal_cb(self, goal_handle):
        """
        Accept every incoming goal and queue it for FIFO processing.
        """
        goal = goal_handle.get_goal()
        preview = goal.utterance_text[:50].replace('\n', ' ')
        rospy.loginfo(f"Queued Speak Goal: '{preview}...' via {goal.engine}")

        goal_handle.set_accepted("Queued for synthesis")

        with self._queue_cond:
            self._goal_queue.append(goal_handle)
            self._queue_cond.notify()

    def cancel_cb(self, goal_handle):
        """
        We do not really use canceling, but handle it cleanly anyway.
        If the goal is still waiting in the queue, remove it immediately.
        If it is already being processed, the worker loop will notice later.
        """
        with self._queue_cond:
            for queued_handle in list(self._goal_queue):
                if queued_handle == goal_handle:
                    self._goal_queue.remove(queued_handle)

                    result = SpeakResult()
                    result.success = False
                    result.final_message = "Canceled while waiting in queue."
                    result.total_duration = 0.0

                    goal_handle.set_canceled(result, "Removed from queue")
                    rospy.loginfo("Canceled queued speech goal before processing.")
                    return

        rospy.loginfo("Cancel requested for active goal; worker will handle if needed.")

    def _worker_loop(self):
        while not rospy.is_shutdown():
            with self._queue_cond:
                while not self._goal_queue and not rospy.is_shutdown():
                    self._queue_cond.wait(timeout=0.5)

                if rospy.is_shutdown():
                    return

                goal_handle = self._goal_queue.popleft()
                self._is_processing = True

            try:
                self._process_goal(goal_handle)
            except Exception as e:
                rospy.logerr(f"Unhandled exception while processing speech goal: {e}")

                result = SpeakResult()
                result.success = False
                result.final_message = f"Exception during synthesis: {e}"
                result.total_duration = 0.0

                try:
                    goal_handle.set_aborted(result, "Speech synthesis failed")
                except Exception as inner_e:
                    rospy.logerr(f"Failed to abort goal cleanly: {inner_e}")
            finally:
                with self._queue_cond:
                    self._is_processing = False

    def _process_goal(self, goal_handle):
        goal = goal_handle.get_goal()
        rospy.loginfo(f"Processing Speak Goal: '{goal.utterance_text[:50]}...' via {goal.engine}")

        self.set_is_speaking_state(True)

        feedback = SpeakFeedback()
        result = SpeakResult()

        utterance_text = goal.utterance_text.replace('*', '')

        if not utterance_text.strip():
            result.success = False
            result.final_message = "Empty text"
            result.total_duration = 0.0
            goal_handle.set_succeeded(result, "Empty utterance")
            self.set_is_speaking_state(False)
            return

        if not PRESET_EMOJIS:
            load_preset_emojis_once()

        # TTP v2: pull performance-pipeline options out of engine_params so
        # they never reach the TTS server. e.g. {"performance": {"face_policy":
        # ["saved", "generate"], "temperature": 0.7}}
        performance_params = {}
        engine_params_json = goal.engine_params
        try:
            parsed_params = json.loads(goal.engine_params) if goal.engine_params else {}
            if isinstance(parsed_params, dict) and "performance" in parsed_params:
                performance_params = parsed_params.pop("performance") or {}
                engine_params_json = json.dumps(parsed_params)
        except json.JSONDecodeError:
            pass

        # Split at emoji, then subdivide long spans by sentence/clause
        # (~80 chars soft, ~100 hard) so every cue is a performable beat
        # even in emoji-less stretches.
        chunks = subchunk_pairs(split_text_emoji(utterance_text, PRESET_EMOJIS))

        self._utterance_counter += 1
        utterance_id = "u{}_{}".format(int(time.time()), self._utterance_counter)
        cue_ids = ["{}:{}".format(utterance_id, i) for i in range(len(chunks))]

        # Announce all cues before any synthesis so face/arm generation for
        # cue N can run while cue 0..N-1 are still in TTS or playback.
        # est_duration is a same-ballpark guess from text length alone (see
        # chunking.estimate_speech_duration) -- available immediately, well
        # before the real (exact) chunk_duration is known post-synthesis.
        announce = {
            "utterance_id": utterance_id,
            "engine": goal.engine,
            "performance": performance_params,
            "cues": [
                {"cue_id": cue_ids[i], "index": i, "text": t, "emoji": e,
                 "est_duration": estimate_speech_duration(t)}
                for i, (t, e) in enumerate(chunks)
            ],
        }
        self._cue_announce_pub.publish(String(data=json.dumps(announce, ensure_ascii=False)))

        feedback.total_chunks = len(chunks)
        total_calculated_duration = 0.0
        chunks_sent = 0

        for i, (text_snippet, emoji_snippet) in enumerate(chunks):
            # You said you won't really cancel/preempt, but this is cheap insurance.
            status = goal_handle.get_goal_status()
            if status and status.status in [2, 6, 7, 8]:
                result.success = False
                result.final_message = "Canceled during synthesis."
                result.total_duration = total_calculated_duration
                goal_handle.set_canceled(result, "Canceled during synthesis")
                self.set_is_speaking_state(False)
                return

            feedback.current_chunk_index = i

            audio_data_np, sample_rate = synthesize_audio_remote(
                text_snippet,
                goal.engine,
                engine_params_json
            )

            if len(audio_data_np) > 0:
                chunk_duration = len(audio_data_np) / float(sample_rate)
            else:
                chunk_duration = 0.5

            feedback.chunk_duration = chunk_duration
            feedback.text_snippet = text_snippet
            feedback.emoji_snippet = emoji_snippet
            total_calculated_duration += chunk_duration

            msg = SpeechData()
            msg.cue_id = cue_ids[i]
            msg.text_snippet = text_snippet
            msg.emoji = emoji_snippet if emoji_snippet in PRESET_EMOJIS else ""
            msg.audio_data = audio_data_np.tolist()
            msg.sample_rate = sample_rate
            msg.duration = chunk_duration
            msg.current_chunk_index = i
            msg.total_chunks = len(chunks)

            self._speech_data_pub.publish(msg)
            goal_handle.publish_feedback(feedback)

            rospy.loginfo(f"  Chunk {i + 1}/{len(chunks)} sent to playback.")
            chunks_sent += 1

        if chunks_sent > 0:
            result.success = True
            result.final_message = "Synthesis Complete. Sent to playback."
        else:
            result.success = False
            result.final_message = "No chunks generated."
            self.set_is_speaking_state(False)

        result.total_duration = total_calculated_duration
        goal_handle.set_succeeded(result, "Synthesis complete")
            
if __name__ == '__main__':
    try:
        rospy.init_node('tts_action_server_node')
        # Params for presets path only
        rospy.set_param('~emoji_preset_path', rospy.get_param('~emoji_preset_path', '/home/robot/robot_ws/animations/face/*.json'))
        
        server = SpeakActionServer("speak")
        rospy.spin()
    except rospy.ROSInterruptException:
        pass