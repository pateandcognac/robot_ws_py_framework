#!/home/robot/robot_ws/.venv/bin/python3

import os
import time
import queue
import threading
import struct
import json
import re
import readline
from datetime import datetime
import numpy as np
import sounddevice as sd
import soundfile as sf
import torch

# ROS Imports
import rospy
from std_msgs.msg import String, Bool, Int32MultiArray
from kobuki_msgs.msg import Sound
# Assuming your custom message is accessible in the venv
# If not, we treat it as a dynamic import or standard message for now

from logos_framework.msg import CognitionInput

# Porcupine & Whisper
import pvporcupine
from faster_whisper import WhisperModel

# Colorama for terminal output
from colorama import Fore, Style, init as colorama_init
colorama_init(autoreset=True)

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------

# Audio Settings
SAMPLE_RATE = 16000
FRAME_LENGTH = 512  # Porcupine prefers 512
CHANNELS = 1
DEVICE_NAME = 'pan_tilt_mic' # As defined in your sanity check

# Paths (Adjust as needed for your robot's layout)
PORCUPINE_KEY = os.environ.get('PORCUPINE_VOICE_KEY')
KW_PATH = os.path.expanduser('~/robot_ws/porcupine/')
KEYWORD_PATHS = [
    f'{KW_PATH}Hey-Robot_en_linux_v3_0_0.ppn',   # Index 0: Wake
    f'{KW_PATH}end-of-line_en_linux_v3_0_0.ppn', # Index 1: Stop
    f'{KW_PATH}edit-input_en_linux_v3_0_0.ppn'   # Index 2: Edit
]
KEYWORDS = ['Hey-Robot', 'end-of-line', 'edit-input']

# Timers (Seconds)
AMBIENT_MAX_DURATION = 120    # 2 minutes hard cap for buffer
AMBIENT_CHECK_INTERVAL = 600  # 10 minutes
RECORDING_TIMEOUT = 60        # 60 second hard limit for user input
MIN_AMBIENT_LENGTH = 10       # Minimum seconds to bother transcribing ambient

# LED Constants
FACE_LED_COUNT = 12

class LedState:
    IDLE = 0
    AMBIENT = 1
    RECORDING = 2
    TRANSCRIBING = 3
    EDIT_INPUT = 4
    EAR_PLUGS = 5

# -----------------------------------------------------------------------------
# The Node Class
# -----------------------------------------------------------------------------

class LogosEarsNode:
    def __init__(self):
        rospy.init_node('logos_ears_node', anonymous=False)
        
        # --- State Variables ---
        self.state_lock = threading.Lock()
        self.is_speaking = False         # From TTS
        self.ambient_enabled = False     # From external topic
        self.current_state = LedState.IDLE
        
        # --- Buffers & Queues ---
        self.audio_queue = queue.Queue() # From Ear -> Brain
        self.job_queue = queue.Queue()   # From Brain -> Scribe
        
        self.ambient_buffer = []         # List of numpy arrays
        self.ambient_start_time = time.time()
        
        self.recording_buffer = []       # List of numpy arrays
        self.recording_start_time = 0
        
        # --- Load Models ---
        self._init_models()
        
        # --- ROS Setup ---
        self._init_ros()
        
        # --- Threading ---
        self.running = True
        
        # 1. The Ear (Audio Capture)
        self.capture_thread = threading.Thread(target=self._audio_capture_loop, daemon=True)
        
        # 2. The Scribe (Whisper Inference)
        self.scribe_thread = threading.Thread(target=self._scribe_loop, daemon=True)
        
        # 3. The Face (LED Animation)
        self.led_thread = threading.Thread(target=self._led_animation_loop, daemon=True)
        
        # Start threads
        self.capture_thread.start()
        self.scribe_thread.start()
        self.led_thread.start()
        
        print(Fore.GREEN + "Logos Ears are online. Listening...")
        
        # Main Thread becomes "The Brain"
        self._brain_loop()

    def _init_models(self):
        print(Fore.CYAN + "Loading Models...")
        
        # Porcupine
        if not PORCUPINE_KEY:
            raise ValueError("PORCUPINE_VOICE_KEY not set!")
        self.porcupine = pvporcupine.create(
            access_key=PORCUPINE_KEY, 
            keyword_paths=KEYWORD_PATHS
        )
        
        # Silero VAD
        # Loading from torch hub is standard and usually reliable. 
        # If no internet, this requires a local path.
        self.vad_model, utils = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            onnx=True
        )
        (self.get_speech_timestamps, self.save_audio, self.read_audio, self.VADIterator, self.collect_chunks) = utils
        
        # Faster Whisper (Int8 for speed on CPU)
        # Using a small or distilled model is usually sufficient for commands
        # options: tiny.en, tiny, base.en, base, small.en, small, medium.en, medium, large-v1, large-v2, large-v3, large, distil-large-v2, distil-medium.en, distil-small.en, distil-large-v3, distil-large-v3.5, large-v3-turbo, turbo
        self.whisper = WhisperModel("tiny.en", device="cpu", compute_type="int8")
        
        print(Fore.CYAN + "Models Loaded.")

    def _init_ros(self):
        # Publishers
        self.pub_cognition = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)
        self.pub_ambient = rospy.Publisher('/stt/ambient_listener/transcription', String, queue_size=10, latch=True)
        self.pub_led = rospy.Publisher('/face/rgbled', Int32MultiArray, queue_size=10)
        self.pub_sound = rospy.Publisher('/mobile_base/commands/sound', Sound, queue_size=1)
        
        # Subscribers
        rospy.Subscriber('/tts/is_speaking', Bool, self._cb_is_speaking)
        rospy.Subscriber('/stt/ambient_listener/enable', Bool, self._cb_ambient_enable)

        # Kobuki Sound Helper
        self.sound_msg = Sound()

    # -------------------------------------------------------------------------
    # ROS Callbacks
    # -------------------------------------------------------------------------
    def _cb_is_speaking(self, msg):
        """TTS is active. Flip Ear Plugs."""
        with self.state_lock:
            previous_speaking = self.is_speaking
            self.is_speaking = msg.data
            
            if self.is_speaking:
                # Immediate mute effect
                self.current_state = LedState.EAR_PLUGS
            elif not self.is_speaking and previous_speaking:
                # Returning to normal
                self.current_state = LedState.AMBIENT if self.ambient_enabled else LedState.IDLE

    def _cb_ambient_enable(self, msg):
        with self.state_lock:
            self.ambient_enabled = msg.data
            if not self.is_speaking and self.current_state != LedState.RECORDING:
                self.current_state = LedState.AMBIENT if self.ambient_enabled else LedState.IDLE
            print(Fore.BLUE + f"Ambient Listener: {self.ambient_enabled}")

    # -------------------------------------------------------------------------
    # 1. The Ear (Audio Capture Thread)
    # -------------------------------------------------------------------------
    def _audio_capture_loop(self):
        """
        Reads audio from device, calculates RMS (for LEDs), puts in queue.
        Blocking reads to ensure no data loss.
        """
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_LENGTH,
                channels=CHANNELS,
                dtype='int16',
                device=DEVICE_NAME
            ) as stream:
                while self.running and not rospy.is_shutdown():
                    # Read audio
                    pcm, overflow = stream.read(FRAME_LENGTH)
                    if overflow:
                        print(Fore.YELLOW + "Audio Overflow")

                    # Convert to standard format for processing
                    # Porcupine needs 16-bit integers
                    # Whisper/VAD usually like float32 between -1 and 1
                    pcm_int16 = pcm.flatten()
                    
                    # Calculate Volume for VU Meter (LEDs)
                    # We store this in a thread-safe way for the LED thread
                    rms = np.sqrt(np.mean(pcm_int16.astype(float)**2))
                    self.current_volume = rms

                    # EAR PLUGS LOGIC:
                    # If TTS is speaking, we DROP the audio here.
                    # We do not put it in the queue.
                    with self.state_lock:
                        if self.is_speaking:
                            continue

                    self.audio_queue.put(pcm_int16)
                    
        except Exception as e:
            rospy.logerr(f"Audio Capture Error: {e}")
            self.running = False

    # -------------------------------------------------------------------------
    # 2. The Brain (Logic & State Machine Loop)
    # -------------------------------------------------------------------------
    def _brain_loop(self):
        """
        Consumes audio from queue. Checks Wakewords. Checks VAD. Manages Buffers.
        """
        # Timers
        last_ambient_check = time.time()
        
        while self.running and not rospy.is_shutdown():
            try:
                # Get audio chunk (blocking with timeout to allow shutdown check)
                pcm_int16 = self.audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # --- Pre-processing ---
            # Porcupine expects linear PCM (16-bit)
            # Silero expects float32
            pcm_float32 = pcm_int16.astype(np.float32) / 32768.0

            # 1. Check Wake Words
            # Index 0: Hey-Robot, 1: end-of-line, 2: edit-input
            keyword_index = self.porcupine.process(pcm_int16)

            # 2. Check VAD (Voice Activity Detection)
            # Returns confidence 0.0 - 1.0. We use 0.5 threshold.
            # Torch tensor overhead is small enough here for 512 frames
            vad_prob = self.vad_model(torch.from_numpy(pcm_float32), SAMPLE_RATE).item()
            is_speech = vad_prob > 0.5

            # --- State Machine ---
            with self.state_lock:
                state = self.current_state

            # --- RECORDING STATE (Direct Input) ---
            if state == LedState.RECORDING:
                self.recording_buffer.append(pcm_float32)
                
                # Check Timeout
                if (time.time() - self.recording_start_time) > RECORDING_TIMEOUT:
                    print(Fore.RED + "Recording Timeout Reached.")
                    self._finish_recording(reason="timeout")
                    continue

                # Check Stop Words
                if keyword_index == 1: # end-of-line
                    print(Fore.GREEN + "Stop Word: end-of-line")
                    self._play_sound(Sound.OFF)
                    self._finish_recording(reason="normal")
                elif keyword_index == 2: # edit-input
                    print(Fore.YELLOW + "Stop Word: edit-input")
                    self._play_sound(Sound.OFF)
                    self._finish_recording(reason="edit")

            # --- AMBIENT / IDLE STATE ---
            else:
                # Check "Hey-Robot" (Wake up)
                if keyword_index == 0:
                    print(Fore.MAGENTA + "Wake Word: Hey-Robot detected!")
                    self._play_sound(Sound.ON)
                    
                    # === THE CONTEXT FLUSH ===
                    # If we have ambient data, send it to scribe NOW.
                    if self.ambient_buffer and self.ambient_enabled:
                         print(Fore.CYAN + "Flushing Ambient Context...")
                         self._flush_ambient_buffer()
                    
                    # Switch to Recording
                    with self.state_lock:
                        self.current_state = LedState.RECORDING
                    self.recording_buffer = []
                    self.recording_start_time = time.time()
                    continue

                # Ambient Listening Logic
                if self.ambient_enabled and state != LedState.EAR_PLUGS:
                    # Only append if speech is detected (VAD)
                    # This filters out silence and fan noise
                    if is_speech:
                        self.ambient_buffer.append(pcm_float32)

                    # Manage Ambient Buffer Lifecycle
                    now = time.time()
                    
                    # Soft Cut Logic:
                    # Check if we hit time limits
                    buffer_duration_approx = (len(self.ambient_buffer) * FRAME_LENGTH) / SAMPLE_RATE
                    
                    hit_max_length = buffer_duration_approx >= AMBIENT_MAX_DURATION
                    hit_interval = (now - last_ambient_check) > AMBIENT_CHECK_INTERVAL

                    if hit_max_length or hit_interval:
                        # Only flush if we are currently PAUSED (not speech) 
                        # OR if we are way over time (force flush)
                        if not is_speech or buffer_duration_approx > (AMBIENT_MAX_DURATION + 15):
                            if buffer_duration_approx > MIN_AMBIENT_LENGTH:
                                print(Fore.BLUE + f"Auto-transcribing Ambient Buffer ({buffer_duration_approx:.1f}s)")
                                self._flush_ambient_buffer()
                            else:
                                # Buffer too small, just discard to prevent drift
                                self.ambient_buffer = []
                                self.ambient_start_time = time.time()
                            
                            last_ambient_check = now

    # -------------------------------------------------------------------------
    # Helper Logic
    # -------------------------------------------------------------------------
    def _flush_ambient_buffer(self):
        """Package ambient buffer and send to Scribe."""
        if not self.ambient_buffer:
            return
            
        full_audio = np.concatenate(self.ambient_buffer)
        job = {
            'type': 'ambient',
            'audio': full_audio,
            'timestamp': datetime.now().strftime("%I:%M %p")
        }
        self.job_queue.put(job)
        
        # Reset
        self.ambient_buffer = []
        self.ambient_start_time = time.time()

    def _finish_recording(self, reason="normal"):
        """Package recording buffer and send to Scribe."""
        if not self.recording_buffer:
            self._reset_state()
            return

        print(Fore.GREEN + "Processing Input...")
        with self.state_lock:
            self.current_state = LedState.TRANSCRIBING

        full_audio = np.concatenate(self.recording_buffer)
        job = {
            'type': 'human_stt',
            'audio': full_audio,
            'edit_mode': (reason == "edit")
        }
        self.job_queue.put(job)
        
        # We don't reset state here immediately; 
        # The Scribe thread will trigger the LED change when done, 
        # but logic-wise we revert to AMBIENT/IDLE in _reset_state called by Scribe?
        # Actually, simpler: Reset logic state now, let LEDs chase until transcription event?
        # No, let's reset state in the Scribe to keep "Transcribing" LED active.
        pass 

    def _reset_state(self):
        with self.state_lock:
            self.current_state = LedState.AMBIENT if self.ambient_enabled else LedState.IDLE

    def _play_sound(self, val):
        self.sound_msg.value = val
        self.pub_sound.publish(self.sound_msg)

    # -------------------------------------------------------------------------
    # 3. The Scribe (Whisper Inference Thread)
    # -------------------------------------------------------------------------
    def _scribe_loop(self):
        """
        Waits for audio jobs, runs Faster-Whisper, publishes ROS msgs.
        """
        while self.running and not rospy.is_shutdown():
            try:
                job = self.job_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # Prepare Prompt with Keywords to help spelling/cleanup
            # We specifically include the stop words so Whisper recognizes them as words
            prompt = "Hi, Logos! ROS Noetic Ubuntu Linux Hey-Robot end-of-line edit-input"

            segments, info = self.whisper.transcribe(
                job['audio'], 
                beam_size=5, 
                initial_prompt=prompt,
                vad_filter=True # Whisper has internal VAD too, helpful for cleaning
            )

            # Collect text
            text_segments = []
            confidence_sum = 0
            count = 0
            
            for segment in segments:
                text_segments.append(segment.text)
                confidence_sum += segment.avg_logprob # Note: this is logprob, not %
                count += 1
            
            full_text = " ".join(text_segments).strip()
            
            # --- Text Cleanup ---
            # 1. Remove "Hey-Robot" variants from start and end
            full_text = re.sub(r'^(Hey-? ?Robot)[,.]? ?', '', full_text, flags=re.IGNORECASE)
            full_text = re.sub(r'(Hey-? ?Robot)[,.]? $', '', full_text, flags=re.IGNORECASE)
            # 2. Remove "end-of-line" / "edit-input" variants from end
            full_text = re.sub(r'(end-? ?of-? ?line|edit-? ?input)[.]?$', '', full_text, flags=re.IGNORECASE).strip()

            if not full_text:
                print(Fore.YELLOW + "Transcribed empty audio.")
                self._reset_state()
                continue

            # --- Handling Output ---
            
            if job['type'] == 'ambient':
                # Publish to Latched Ambient Topic
                # Simplified confidence score (Whisper gives logprob, converting roughly)
                conf = round(np.exp(confidence_sum / count), 2) if count > 0 else 0.0
                
                payload = {
                    "time": job['timestamp'],
                    "confidence": conf,
                    "transcription": full_text
                }
                # TODO: maintain a history of ambient transcriptions. keep up to 1 hour, and have a max length.
                self.pub_ambient.publish(json.dumps(payload))
                print(Fore.CYAN + f"Ambient Published: {full_text[:60]}...")

            elif job['type'] == 'human_stt':
                
                final_text = full_text

                # Edit Mode
                if job.get('edit_mode'):
                    # Temporarily stop LED animation or set to IDLE for clarity
                    # Actually, we are in a separate thread.
                    print(Fore.LIGHTWHITE_EX + f"\n--- EDIT INPUT ---\nOriginal: {full_text}")
                    
                    # Readline hook for pre-filling
                    def hook():
                        readline.insert_text(full_text)
                        readline.redisplay()
                    
                    readline.set_pre_input_hook(hook)
                    try:
                        final_text = input(f"{Fore.GREEN}Edit > {Style.RESET_ALL}")
                    except:
                        pass
                    readline.set_pre_input_hook(None)

                # Publish to Cognition
                msg = CognitionInput()
                msg.type = "human_stt"
                msg.content = final_text
                msg.system_hint = "<!-- system: human_stt input may contain Whisper transcription errors -->"
                msg.loop_cognition = True
                
                self.pub_cognition.publish(msg)
                print(Fore.GREEN + f"Published Human Command: {final_text}")
                
                # Done transcribing/editing, reset state
                self._reset_state()

    # -------------------------------------------------------------------------
    # 3. The Face (LED Animation Thread)
    # -------------------------------------------------------------------------
    def _led_animation_loop(self):
        """
        Controls RGB LEDs based on current_state and current_volume.
        """
        led_msg = Int32MultiArray()
        idx = 0
        
        while self.running and not rospy.is_shutdown():
            state = self.current_state # atomic read usually fine
            
            data_list = [0] * FACE_LED_COUNT
            
            if state == LedState.IDLE:
                # Dim or Off
                pass 
                
            elif state == LedState.AMBIENT:
                # Slow Blue Breather
                brightness = int((np.sin(time.time() * 2) + 1) * 20) # 0 to 40
                color = 0x000000 | brightness # Blue channel
                data_list = [((i << 24) | color) for i in range(FACE_LED_COUNT)]
                
                
            elif state == LedState.RECORDING:
                # VU Meter logic using self.current_volume
                # Normalize volume (heuristic max 2000)
                vol = getattr(self, 'current_volume', 0)
                level = min(int((vol / 2000.0) * (FACE_LED_COUNT // 2)), FACE_LED_COUNT // 2)
                
                # Center outward
                center_l = (FACE_LED_COUNT // 2) - 1
                center_r = center_l + 1
                
                for i in range(level):
                    # Green to Red gradient
                    r = int((i / (FACE_LED_COUNT//2)) * 255)
                    g = 255 - r
                    color = (r << 16) | (g << 8)
                    
                    if center_l - i >= 0:
                        data_list[center_l - i] = ((center_l - i) << 24) | color
                    if center_r + i < FACE_LED_COUNT:
                        data_list[center_r + i] = ((center_r + i) << 24) | color
                    # clear remaining LEDs
                for j in range(level, FACE_LED_COUNT // 2):
                    if center_l - j >= 0:
                        data_list[center_l - j] = ((center_l - j) << 24) | 0
                    if center_r + j < FACE_LED_COUNT:
                        data_list[center_r + j] = ((center_r + j) << 24) | 0


            elif state == LedState.TRANSCRIBING:
                # Green Chaser
                color = 0x004000
                pos = int(time.time() * 10) % FACE_LED_COUNT
                data_list[pos] = (pos << 24) | color
                
            elif state == LedState.EAR_PLUGS:
                # Solid Dim Red
                color = 0x200000
                data_list = [((i << 24) | color) for i in range(FACE_LED_COUNT)]

            # Publish
            # Filter out 0s to save bandwidth? No, logic expects full array or mapped ints
            # Your legacy code used mapped ints: (index << 24) | color
            # The list needs to contain only the active ones if we filter, 
            # but legacy `set_face_leds` implies we send specific updates.
            # Here I send a full update if list is populated.
            if any(data_list):
                 led_msg.data = [x for x in data_list if x != 0]
                 self.pub_led.publish(led_msg)
            elif state == LedState.IDLE:
                 # Ensure off
                 clear_data = [((i << 24) | 0) for i in range(FACE_LED_COUNT)]
                 led_msg.data = clear_data
                 self.pub_led.publish(led_msg)
            
            time.sleep(0.05)


if __name__ == '__main__':
    try:
        node = LogosEarsNode()
    except rospy.ROSInterruptException:
        pass
    except KeyboardInterrupt:
        pass