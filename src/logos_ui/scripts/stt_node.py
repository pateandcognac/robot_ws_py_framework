#!/usr/bin/env python3
# Note: Ensure this is run with your Python 3.11 venv interpreter

import os
import sys
import time
import json
import queue
import threading
import struct
import numpy as np
import sounddevice as sd
import pvporcupine
from faster_whisper import WhisperModel
import torch # For Silero VAD

# ROS Imports
# We need to ensure we can find rospy even in the venv
try:
    import rospy
    from std_msgs.msg import String, Bool, Int32MultiArray
    from kobuki_msgs.msg import Sound
    # Adjust package name if needed based on your workspace
    from logos_framework.msg import CognitionInput 
except ImportError:
    print("CRITICAL: ROS Python libraries not found. Ensure PYTHONPATH includes /opt/ros/noetic/lib/python3/dist-packages")
    sys.exit(1)

# -------------------------------------------------------------------------
# Configuration & Constants
# -------------------------------------------------------------------------

# Audio Settings
SAMPLE_RATE = 16000
FRAME_LENGTH = 512 # Porcupine prefers 512
CHANNELS = 1
BLOCK_SIZE_MS = 32 # Duration of audio chunk for VAD (approx)

# Limits
AMBIENT_MAX_DURATION_SEC = 120  # 2 minutes
AMBIENT_TIMEOUT_SEC = 600       # 10 minutes
AMBIENT_MIN_TRANSCRIPTION_SEC = 10 

# Porcupine Paths (Update to your actual paths)
KW_PATH = os.path.expanduser('~/robot_ws/porcupine/')
ACCESS_KEY = os.environ.get('PORCUPINE_VOICE_KEY')

KEYWORD_PATHS = [
    f'{KW_PATH}Hey-Robot_en_linux_v3_0_0.ppn',   # Index 0: Start Recording
    f'{KW_PATH}end-of-line_en_linux_v3_0_0.ppn', # Index 1: Stop Recording
    f'{KW_PATH}edit-input_en_linux_v3_0_0.ppn'   # Index 2: Edit (Debug)
]
KEYWORDS = ["Hey-Robot", "end-of-line", "edit-input"]

# Whisper Settings
WHISPER_MODEL_SIZE = "base.en" # or 'small.en' if CPU allows
COMPUTE_TYPE = "int8"

# LED Constants
FACE_LED_COUNT = 12

# -------------------------------------------------------------------------
# Helper Classes
# -------------------------------------------------------------------------

class LedManager:
    """Manages the RGB Strips to indicate state without blocking audio."""
    class State:
        IDLE = 0
        LISTENING = 1       # Ambient / Waiting for wake
        RECORDING = 2       # Human speaking to Logos
        TRANSCRIBING = 3    # Processing
        EDIT_INPUT = 4      # Debug mode

    def __init__(self, pub):
        self.pub = pub
        self._stop_event = threading.Event()
        self._thread = None
        self.current_state = self.State.IDLE
        self.volume_level = 0.0

    def set_state(self, state):
        if self.current_state == state:
            return
        
        self.current_state = state
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join()
        
        self._stop_event.clear()
        
        if state == self.State.LISTENING:
            self._thread = threading.Thread(target=self._anim_breathing, args=(0x000040,), daemon=True) # Blue
        elif state == self.State.RECORDING:
            self._thread = threading.Thread(target=self._anim_vu_meter, daemon=True) 
        elif state == self.State.TRANSCRIBING:
            self._thread = threading.Thread(target=self._anim_chaser, args=(0x004000, 0.1), daemon=True) # Green
        elif state == self.State.EDIT_INPUT:
            self._thread = threading.Thread(target=self._anim_solid, args=(0xFFFF00,), daemon=True) # Yellow
        elif state == self.State.IDLE:
            self._clear_leds()
            return

        if self._thread:
            self._thread.start()

    def update_volume(self, audio_frame):
        # Calculate RMS for VU meter
        self.volume_level = np.sqrt(np.mean(np.square(audio_frame)))

    def _publish(self, colors):
        msg = Int32MultiArray()
        data_list = []
        for i, c in colors.items():
            data_list.append((i << 24) | (c & 0xFFFFFF))
        msg.data = data_list
        self.pub.publish(msg)

    def _clear_leds(self):
        self._publish({i: 0 for i in range(FACE_LED_COUNT)})

    # -- Animations --
    def _anim_breathing(self, color_hex):
        # Simple throb
        t = 0
        while not self._stop_event.is_set():
            intensity = (np.sin(t) + 1) / 2.0 # 0 to 1
            r = int(((color_hex >> 16) & 0xFF) * intensity)
            g = int(((color_hex >> 8) & 0xFF) * intensity)
            b = int((color_hex & 0xFF) * intensity)
            col = (r << 16) | (g << 8) | b
            self._publish({i: col for i in range(FACE_LED_COUNT)})
            time.sleep(0.05)
            t += 0.2

    def _anim_chaser(self, color, delay):
        idx = 0
        while not self._stop_event.is_set():
            self._publish({idx % FACE_LED_COUNT: color})
            time.sleep(delay)
            self._publish({idx % FACE_LED_COUNT: 0})
            idx += 1

    def _anim_solid(self, color):
        self._publish({i: color for i in range(FACE_LED_COUNT)})
        
    def _anim_vu_meter(self):
        # Simplified VU meter logic
        max_vol = 0.5 # Normalize max volume expectation
        while not self._stop_event.is_set():
            norm_vol = min(self.volume_level / max_vol, 1.0)
            leds_lit = int(norm_vol * FACE_LED_COUNT)
            colors = {}
            for i in range(FACE_LED_COUNT):
                if i < leds_lit:
                    # Gradient Green to Red
                    colors[i] = 0x00FF00 if i < 8 else 0xFF0000
                else:
                    colors[i] = 0
            self._publish(colors)
            time.sleep(0.05)


# -------------------------------------------------------------------------
# Main Node Class
# -------------------------------------------------------------------------

class LogosHearingNode:
    def __init__(self):
        rospy.init_node('logos_stt_node', anonymous=False)

        # --- Hardware / Models ---
        self.q = queue.Queue()
        
        # 1. Porcupine
        if not ACCESS_KEY:
            raise ValueError("PORCUPINE_VOICE_KEY not set")
        self.porcupine = pvporcupine.create(access_key=ACCESS_KEY, keyword_paths=KEYWORD_PATHS)
        
        # 2. Faster Whisper
        print("Loading Whisper...")
        self.whisper = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type=COMPUTE_TYPE)
        
        # 3. Silero VAD
        print("Loading Silero VAD...")
        self.vad_model, utils = torch.hub.load(repo_or_dir='snakers4/silero-vad',
                                          model='silero_vad',
                                          force_reload=False,
                                          onnx=False) # ONNX is faster but requires onnxruntime
        (self.get_speech_timestamps, _, self.read_audio, _, _) = utils
        
        # --- ROS Topics ---
        self.pub_cognition = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)
        self.pub_ambient_text = rospy.Publisher('/stt/ambient_listener/transcription', String, queue_size=10, latch=True)
        self.pub_leds = rospy.Publisher('/face/rgbled', Int32MultiArray, queue_size=10)
        self.pub_sound = rospy.Publisher('/mobile_base/commands/sound', Sound, queue_size=1)
        
        rospy.Subscriber('/tts/is_speaking', Bool, self.cb_is_speaking)
        rospy.Subscriber('/stt/ambient_listener/enable', Bool, self.cb_ambient_enable)

        # --- State Variables ---
        self.led_manager = LedManager(self.pub_leds)
        
        self.is_speaking = False # TTS Mute flag
        self.ambient_enabled = False
        self.is_recording_command = False
        
        # Buffers
        self.command_buffer = [] # Float32 for Whisper
        self.ambient_buffer = [] # List of numpy arrays (chunks)
        self.ambient_start_time = time.time()
        self.ambient_history = [] # For JSON publishing
        
        # Threading lock for buffers accessed by inference threads
        self.buffer_lock = threading.Lock()

        # Start Audio Stream
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16', # Capture as int16 for Porcupine
            blocksize=self.porcupine.frame_length,
            callback=self._audio_callback
        )
        self.stream.start()

        print("Logos Hearing Node Initialized. 👂")

    def _audio_callback(self, indata, frames, time, status):
        """Push raw audio into queue."""
        if status:
            print(status, file=sys.stderr)
        self.q.put(indata.copy())

    def cb_is_speaking(self, msg):
        """Mute logic (Earplugs)."""
        self.is_speaking = msg.data
        # If robot starts speaking, paused ambient, but we don't necessarily abort a user command
        # if they interrupt the robot (barge-in).
        # However, to prevent feedback loops, we generally ignore input while speaking.

    def cb_ambient_enable(self, msg):
        self.ambient_enabled = msg.data
        if self.ambient_enabled:
            print("Ambient Listener ENABLED")
            self.led_manager.set_state(LedManager.State.LISTENING)
        else:
            print("Ambient Listener DISABLED")
            self.led_manager.set_state(LedManager.State.IDLE)

    def run(self):
        """Main Processing Loop"""
        rate = rospy.Rate(50) # Fast check loop
        
        while not rospy.is_shutdown():
            try:
                # Process all available audio chunks in queue
                while not self.q.empty():
                    pcm_int16 = self.q.get()
                    
                    # Convert to other formats needed
                    # Porcupine needs 1D list/array of Int16
                    pcm_porcupine = struct.unpack_from("h" * self.porcupine.frame_length, pcm_int16)
                    
                    # Whisper/VAD need Float32 normalized [-1, 1]
                    # int16 range is -32768 to 32767
                    pcm_float32 = pcm_int16.flatten().astype(np.float32) / 32768.0
                    
                    # Update LED VU meter
                    self.led_manager.update_volume(pcm_float32)

                    # ---------------------------------------------------------
                    # 1. EAR PLUGS CHECK
                    # ---------------------------------------------------------
                    if self.is_speaking:
                        # Drop audio frame, don't process triggers
                        # Optional: Could still run VAD to detect "Barge-in" later
                        continue

                    # ---------------------------------------------------------
                    # 2. WAKE WORD DETECTION (Always listening unless Muted)
                    # ---------------------------------------------------------
                    keyword_index = self.porcupine.process(pcm_porcupine)

                    # KEYWORD: HEY-ROBOT (Start Command)
                    if keyword_index == 0: 
                        print("👉 Hey-Robot Detected!")
                        self.trigger_recording()
                        continue # Skip the rest of processing for this frame

                    # KEYWORD: END-OF-LINE / EDIT-INPUT (End Command)
                    elif keyword_index in [1, 2] and self.is_recording_command:
                        print(f"🛑 Stop Word Detected: {KEYWORDS[keyword_index]}")
                        self.stop_recording(edit_mode=(keyword_index == 2))
                        continue

                    # ---------------------------------------------------------
                    # 3. RECORDING STATE (Human talking to Logos)
                    # ---------------------------------------------------------
                    if self.is_recording_command:
                        self.command_buffer.append(pcm_float32)
                        continue # Don't process ambient logic if recording command

                    # ---------------------------------------------------------
                    # 4. AMBIENT LISTENER STATE
                    # ---------------------------------------------------------
                    if self.ambient_enabled:
                        self.process_ambient(pcm_float32)
                        
            except Exception as e:
                print(f"Error in main loop: {e}")
            
            rate.sleep()

    # -------------------------------------------------------------------------
    # Logic Methods
    # -------------------------------------------------------------------------

    def trigger_recording(self):
        """Switch to active recording. Flush ambient context first."""
        self.pub_sound.publish(Sound(value=Sound.ON)) # Beep
        
        # Flush Ambient Buffer to Context BEFORE starting command
        self.flush_ambient_context()
        
        self.is_recording_command = True
        self.command_buffer = [] # Reset buffer
        self.led_manager.set_state(LedManager.State.RECORDING)

    def stop_recording(self, edit_mode=False):
        """Stop active recording, transcribe, and publish."""
        self.pub_sound.publish(Sound(value=Sound.OFF)) # Beep
        self.is_recording_command = False
        self.led_manager.set_state(LedManager.State.TRANSCRIBING)

        # Offload transcription to thread
        audio_data = np.concatenate(self.command_buffer)
        t = threading.Thread(target=self.worker_transcribe_command, args=(audio_data, edit_mode))
        t.start()

    def process_ambient(self, audio_chunk):
        """
        Runs VAD. Accumulates buffer. Checks timeouts.
        """
        # 1. VAD Check (Silero)
        # Convert chunk to tensor. Silero expects (1, N)
        tensor_chunk = torch.from_numpy(audio_chunk).unsqueeze(0)
        
        # We use a simple confidence threshold on the chunk
        speech_prob = self.vad_model(tensor_chunk, SAMPLE_RATE).item()
        
        if speech_prob > 0.5:
            with self.buffer_lock:
                self.ambient_buffer.append(audio_chunk)

        # 2. Check Time / Size Constraints
        current_time = time.time()
        elapsed = current_time - self.ambient_start_time
        
        buffer_duration = (len(self.ambient_buffer) * self.porcupine.frame_length) / SAMPLE_RATE
        
        should_transcribe = False
        
        # Hard cap: 2 minutes of accumulated audio
        if buffer_duration >= AMBIENT_MAX_DURATION_SEC:
            should_transcribe = True
            
        # Time cap: 10 minutes elapsed
        elif elapsed >= AMBIENT_TIMEOUT_SEC:
            # Only transcribe if we have meaningful data (> 10s)
            if buffer_duration >= AMBIENT_MIN_TRANSCRIPTION_SEC:
                should_transcribe = True
            else:
                # Discard sparse audio
                with self.buffer_lock:
                    self.ambient_buffer = []
                self.ambient_start_time = time.time()
                
        if should_transcribe:
            self.cycle_ambient_buffer()

    def cycle_ambient_buffer(self):
        """Move current ambient buffer to worker for transcription and reset."""
        with self.buffer_lock:
            if not self.ambient_buffer:
                return
            data = np.concatenate(self.ambient_buffer)
            self.ambient_buffer = [] # Reset
            self.ambient_start_time = time.time() # Reset timer
            
        # Spawn worker
        t = threading.Thread(target=self.worker_transcribe_ambient, args=(data, "ambient_log"))
        t.start()

    def flush_ambient_context(self):
        """Called when 'Hey Robot' interrupts ambient listening."""
        with self.buffer_lock:
            if not self.ambient_buffer:
                return
            data = np.concatenate(self.ambient_buffer)
            # Don't clear buffer here? actually yes, we consumed it.
            self.ambient_buffer = [] 
            self.ambient_start_time = time.time()

        # Transcribe immediately and send as context
        t = threading.Thread(target=self.worker_transcribe_ambient, args=(data, "context_push"))
        t.start()

    # -------------------------------------------------------------------------
    # Transcription Workers (Threaded)
    # -------------------------------------------------------------------------

    def worker_transcribe_command(self, audio_data, edit_mode):
        """Handles explicit human commands."""
        # Prompt tuning
        initial_prompt = (
            "Hey-Robot, end-of-line, edit-input. "
            "The user is speaking to a robot named Logos. "
            "Context: ROS Noetic, Linux, Python code."
        )

        segments, info = self.whisper.transcribe(
            audio_data, 
            beam_size=5, 
            initial_prompt=initial_prompt,
            vad_filter=False # We handled start/stop manually
        )
        
        text = " ".join([s.text for s in segments]).strip()

        # Clean Wakewords
        for kw in KEYWORDS:
            # Simple case-insensitive removal
            text = text.replace(kw, "").replace(kw.lower(), "").replace(kw.upper(), "")
        
        # Final cleanup
        text = text.strip(" .,")

        if not text:
            print("Transcription empty.")
            self.led_manager.set_state(LedManager.State.IDLE if not self.ambient_enabled else LedManager.State.LISTENING)
            return

        print(f"COMMAND RECIEVED: {text}")

        if edit_mode:
            self.led_manager.set_state(LedManager.State.EDIT_INPUT)
            # This uses standard input, which might be tricky in a detached ROS node.
            # Assuming run in a terminal for now per your sanity check.
            try:
                print(f"--- EDIT MODE ---\nOriginal: {text}")
                new_text = input("Edit > ")
                if new_text.strip():
                    text = new_text
            except Exception:
                pass

        # Publish
        msg = CognitionInput()
        msg.type = "human_stt"
        msg.content = text
        msg.system_hint = f"Transcribed with confidence {info.language_probability:.2f}. If nonsense, politely ask to repeat."
        msg.loop_cognition = True
        
        self.pub_cognition.publish(msg)
        
        # Reset State
        if self.ambient_enabled:
            self.led_manager.set_state(LedManager.State.LISTENING)
        else:
            self.led_manager.set_state(LedManager.State.IDLE)

    def worker_transcribe_ambient(self, audio_data, mode):
        """
        mode: 'ambient_log' (add to json buffer) or 'context_push' (send to cognition)
        """
        if len(audio_data) == 0: return

        segments, _ = self.whisper.transcribe(audio_data, beam_size=2, vad_filter=True) # Use whisper VAD to cleanup edges
        text = " ".join([s.text for s in segments]).strip()
        
        if not text: return

        timestamp = time.strftime("%I:%M %p")

        if mode == "context_push":
            print(f"FLUSHING CONTEXT: {text}")
            msg = CognitionInput()
            msg.type = "context"
            msg.content = f"Ambient audio immediately preceding wake-word: '{text}'"
            msg.system_hint = "Use this context to resolve references like 'that' or 'it' in the user's command."
            msg.loop_cognition = False # Don't trigger a reply, just add to memory
            self.pub_cognition.publish(msg)
            
            # Also add to log history
            self._update_ambient_json_log(timestamp, text)

        elif mode == "ambient_log":
            print(f"AMBIENT LOG: {text}")
            self._update_ambient_json_log(timestamp, text)

    def _update_ambient_json_log(self, timestamp, text):
        """Maintains the FIFO JSON buffer on the latched topic."""
        entry = {"time": timestamp, "transcription": text}
        self.ambient_history.append(entry)
        
        # Pruning logic
        # 1. Size Cap (rough char count)
        total_chars = sum(len(x['transcription']) for x in self.ambient_history)
        while total_chars > 8192 and self.ambient_history:
            removed = self.ambient_history.pop(0)
            total_chars -= len(removed['transcription'])
            
        # 2. Time Cap (2 hours) - simplified implementation
        # (Assuming linear append, pop from front if list gets too long is usually sufficient for hobby)
        if len(self.ambient_history) > 50: # Arbitrary item cap to prevent massive JSONs
             self.ambient_history.pop(0)

        # Publish
        json_str = json.dumps(self.ambient_history)
        self.pub_ambient_text.publish(json_str)

if __name__ == '__main__':
    try:
        node = LogosHearingNode()
        node.run()
    except rospy.ROSInterruptException:
        pass
    except KeyboardInterrupt:
        pass