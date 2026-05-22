#!/home/robot/robot_ws/.venv/bin/python3
# This runs in a Python 3.11.13 environment
# Despite ROS Noetic being Python 3.8
# Because our node is simply publishing some strings and simple message types, it works okay
# but we won't make a habit of it :D

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

import sys

# Prints a full descriptive string
print(sys.version)
# 3.11.13 (main, Jun  4 2025, 08:57:30) [GCC 9.4.0]

# Prints a tuple
print(sys.version_info)
# sys.version_info(major=3, minor=11, micro=13, releaselevel='final', serial=0)


# ROS Imports
import rospy
from std_msgs.msg import String, Bool, Int32MultiArray
from kobuki_msgs.msg import Sound
from logos_framework.msg import CognitionInput, CognitionOutput

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
DEVICE_NAME = 'pan_tilt_mic' 

# Paths
PORCUPINE_KEY = os.environ.get('PORCUPINE_VOICE_KEY')
KW_PATH = os.path.expanduser('~/robot_ws/porcupine/')

WAKE_KEYWORD_PATH = f'{KW_PATH}Hey-Robot_en_linux_v3_0_0.ppn'
CONTROL_KEYWORD_PATHS = [
    f'{KW_PATH}end-of-line_en_linux_v3_0_0.ppn',  # index 0 in controls
    f'{KW_PATH}edit-input_en_linux_v3_0_0.ppn'    # index 1 in controls
]

# Ambient built-ins (Porcupine defaults you listed)
AMBIENT_BUILTIN_KEYWORDS = [
    'view glass', 'grasshopper', 'snowboy', 'hey google', 'hey siri', 'alexa',
    'porcupine', 'terminator', 'pico clock', 'smart mirror', 'jarvis', 'computer',
    'ok google', 'picovoice', 'blueberry', 'hey barista', 'bumblebee', 'americano',
    'grapefruit'
]

# Optional: VAD-gate ambient built-in hotwords (never gates hey-robot)
GATE_AMBIENT_BUILTINS_WITH_VAD = False

# Hotword debounce
HOTWORD_DEBOUNCE_SEC = 0.0

# Timers (Seconds)
AMBIENT_MAX_DURATION = 120    # 2 minutes hard cap for buffer
AMBIENT_CHECK_INTERVAL = 600  # 10 minutes
RECORDING_TIMEOUT = 60        # 60 second hard limit for user input
MIN_AMBIENT_LENGTH = 10       # Minimum seconds to bother transcribing ambient

# LED Constants
FACE_LED_COUNT = 12

# Ambient History Settings
AMBIENT_HISTORY_MAX_AGE = 7200    # 2 Hours (in seconds)
AMBIENT_HISTORY_MAX_CHARS = 32767 # Max characters before oldest is dropped

# Audio Classifier Settings (MediaPipe YAMNet)
CLASSIFIER_MODEL_PATH      = os.path.expanduser('~/robot_ws/models/yamnet.tflite')
CLASSIFIER_SAMPLE_INTERVAL = 10.0  # seconds between classifier dispatches
CLASSIFIER_SAMPLE_DURATION = 2.5   # seconds of audio per sample
CLASSIFIER_SAMPLE_FRAMES   = int(CLASSIFIER_SAMPLE_DURATION * SAMPLE_RATE / FRAME_LENGTH)  # ~78 frames
CLASSIFIER_BOOST_FACTOR    = 0.5   # temporal confidence boost per repeated detection
CLASSIFIER_TOP_K           = 10    # max YAMNet labels per sample
CLASSIFIER_SCORE_THRESHOLD = 0.05  # minimum score to include in output
CLASSIFIER_BLIP_DURATION   = 2.0   # seconds for amber LED overlay after each sample

class LedState:
    IDLE = 0
    AMBIENT_TRANSCRIBE = 1   # Ambient transcription only (dark blue breather)
    AMBIENT_HOTWORD = 2      # Default hotword listening only (dark green breather)
    AMBIENT_BOTH = 3         # Both active (blue <-> green crossfade, always lit)
    RECORDING = 4
    TRANSCRIBING = 5
    EDIT_INPUT = 6
    EAR_PLUGS = 7

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
        self.hotword_enabled = False     # From external topic
        self.current_state = LedState.IDLE
        self.last_ambient_publish_time = 0


        # --- Buffers & Queues ---
        self.audio_queue = queue.Queue() # From Ear -> Brain
        self.job_queue = queue.Queue()   # From Brain -> Scribe
        
        self.ambient_buffer = []         # List of numpy arrays
        self.ambient_start_time = time.time()
        
        self.recording_buffer = []       # List of numpy arrays
        self.recording_start_time = 0
        
        #  Background Audio Transcription History Storage
        self.ambient_history = []

        # --- Audio Classifier (MediaPipe YAMNet) ---
        self.classifier_enabled          = False
        self.classifier_sampler          = None   # lazy-loaded on first enable
        self.classifier_sample_buffer    = []     # rolling window of pcm_float32 frames
        self.last_classifier_sample_time = 0.0    # for LED blip timing
        self._classifier_job_pending     = False  # prevent queue pile-up

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

        if not PORCUPINE_KEY:
            raise ValueError("PORCUPINE_VOICE_KEY not set!")

        # 1) Wake only (always-on)
        self.porcupine_wake = pvporcupine.create(
            access_key=PORCUPINE_KEY,
            keyword_paths=[WAKE_KEYWORD_PATH],
            sensitivities=[0.15]
        )

        # 2) Controls only (only processed during recording)
        self.porcupine_controls = pvporcupine.create(
            access_key=PORCUPINE_KEY,
            keyword_paths=CONTROL_KEYWORD_PATHS,
            sensitivities=[1.0, 0.75]
        )

        # 3) Ambient built-ins (only processed in ambient mode)
        # Note: built-ins must be provided via keywords= (not keyword_paths=)
        self.porcupine_builtins = pvporcupine.create(
            access_key=PORCUPINE_KEY,
            keywords=AMBIENT_BUILTIN_KEYWORDS,
            sensitivities=[0.5] * len(AMBIENT_BUILTIN_KEYWORDS)
        )

        print(f"Porcupine WAKE: ['hey-robot']")
        print(f"Porcupine CONTROLS: ['end-of-line', 'edit-input']")
        print(f"Porcupine AMBIENT BUILT-INS: {AMBIENT_BUILTIN_KEYWORDS}")
        print(f"All available built-ins (package): {pvporcupine.KEYWORDS}")

        # --- NEW: hotword debounce bookkeeping
        self._last_hotword_time = {}
        


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
        self.whisper_model_name = "small.en"
        self.whisper = WhisperModel(self.whisper_model_name, device="cpu", compute_type="int8")
        
        print(Fore.CYAN + "Models Loaded.")

    def _init_ros(self):
        # Publishers
        self.pub_cognition = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)
        self.pub_ambient = rospy.Publisher('/stt/ambient_listener/transcription', String, queue_size=10, latch=True)
        self.pub_led = rospy.Publisher('/face/rgbled', Int32MultiArray, queue_size=10)
        self.pub_face_cmd = rospy.Publisher('/face/emoji_command', String, queue_size=5)
        self.pub_sound = rospy.Publisher('/mobile_base/commands/sound', Sound, queue_size=1)
        self.output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)
        self.pub_hotword_detections = rospy.Publisher('/stt/hotword_listener/detections', String, queue_size=10)
        self.pub_classifier = rospy.Publisher('/stt/audio_classifier/events', String, queue_size=1, latch=True)

        # Subscribers
        rospy.Subscriber('/tts/is_speaking', Bool, self._cb_is_speaking)
        rospy.Subscriber('/stt/ambient_listener/enable', Bool, self._cb_ambient_enable)
        rospy.Subscriber('/stt/hotword_listener/enable', Bool, self._cb_hotword_enable)
        rospy.Subscriber('/stt/audio_classifier/enable', Bool, self._cb_classifier_enable)

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
                # Keep robot speech and speech-driven servo noise out of the
                # next YAMNet sample window.
                self.classifier_sample_buffer = []
            elif not self.is_speaking and previous_speaking:
                # Returning to normal
                self.current_state = self._resolve_ambient_led_state()

    def _cb_ambient_enable(self, msg):
        should_clear_ambient = False

        with self.state_lock:
            was_enabled = self.ambient_enabled
            self.ambient_enabled = msg.data

            if was_enabled and not self.ambient_enabled:
                self.ambient_buffer = []
                self.ambient_history = []
                self.ambient_start_time = time.time()
                should_clear_ambient = True

            if (
                not self.is_speaking
                and self.current_state not in (LedState.RECORDING, LedState.TRANSCRIBING)
            ):
                self.current_state = self._resolve_ambient_led_state()

            print(Fore.LIGHTBLUE_EX + f"Ambient Listener: {self.ambient_enabled}")

        if should_clear_ambient:
            self.pub_ambient.publish(json.dumps({}))
            print(Fore.LIGHTBLUE_EX + "Ambient transcript cleared.")
            
    def _cb_hotword_enable(self, msg):
        with self.state_lock:
            self.hotword_enabled = msg.data
            if not self.is_speaking and self.current_state not in (LedState.RECORDING, LedState.TRANSCRIBING):
                self.current_state = self._resolve_ambient_led_state()
            print(Fore.LIGHTGREEN_EX + f"Hotword Listener: {self.hotword_enabled}")

    def _cb_classifier_enable(self, msg):
        with self.state_lock:
            was_enabled = self.classifier_enabled
            self.classifier_enabled = msg.data

        if msg.data and not was_enabled:
            self._init_classifier_model()
            print(Fore.YELLOW + f"Audio Classifier: Enabled")
        elif not msg.data and was_enabled:
            if self.classifier_sampler:
                self.classifier_sampler.reset()
            self.classifier_sample_buffer = []
            self._classifier_job_pending = False
            self.pub_classifier.publish(json.dumps({}))
            print(Fore.YELLOW + f"Audio Classifier: Disabled, history cleared")

    def _init_classifier_model(self):
        """Lazy-load MediaPipe YAMNet. Called once on first enable."""
        if self.classifier_sampler is not None:
            return
        try:
            from audio_classifier_sampler import AudioClassifierSampler
            self.classifier_sampler = AudioClassifierSampler(
                model_path=CLASSIFIER_MODEL_PATH,
                boost_factor=CLASSIFIER_BOOST_FACTOR,
                top_k=CLASSIFIER_TOP_K,
                score_threshold=CLASSIFIER_SCORE_THRESHOLD,
            )
            print(Fore.YELLOW + "Audio Classifier: MediaPipe YAMNet loaded.")
        except Exception as e:
            rospy.logerr(f"Audio Classifier: Failed to load model: {e}")
            with self.state_lock:
                self.classifier_enabled = False

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
        last_classifier_sample = time.time()

        while self.running and not rospy.is_shutdown():
            try:
                # Get audio chunk (blocking with timeout to allow shutdown check)
                pcm_int16 = self.audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # --- Pre-processing ---
            pcm_float32 = pcm_int16.astype(np.float32) / 32768.0

            # 1) Wake word (always-on, ungated)
            wake_idx = self.porcupine_wake.process(pcm_int16)  # 0 => hey-robot, -1 => none
            wake_detected = (wake_idx == 0)

            # 2) VAD
            vad_prob = self.vad_model(torch.from_numpy(pcm_float32), SAMPLE_RATE).item()
            is_speech = vad_prob > 0.5
            self.is_speech_detected = is_speech

            # --- State Machine ---
            with self.state_lock:
                state = self.current_state
                ambient_enabled = self.ambient_enabled

            # --- RECORDING STATE ---
            if state == LedState.RECORDING:
                self.recording_buffer.append(pcm_float32)

                # Check timeout
                if (time.time() - self.recording_start_time) > RECORDING_TIMEOUT:
                    print(Fore.RED + "Recording Timeout Reached.")
                    self._finish_recording(reason="timeout")
                    continue

                # Only now do we pay for control hotwords
                ctrl_idx = self.porcupine_controls.process(pcm_int16)  # 0 => eol, 1 => edit, -1 none

                if ctrl_idx == 0:
                    print(Fore.GREEN + "Stop Word: end-of-line")
                    self._publish_hotword("end-of-line")
                    self._play_sound(Sound.OFF)
                    self._finish_recording(reason="normal")

                elif ctrl_idx == 1:
                    print(Fore.YELLOW + "Stop Word: edit-input")
                    self._publish_hotword("edit-input")
                    self._play_sound(Sound.OFF)
                    self._finish_recording(reason="edit")

                continue  # keep recording unless stop/timeout handled

            # --- AMBIENT / IDLE ---
            else:
                # Wake -> publish + proceed (always-on, regardless of listening state)
                if wake_detected:
                    print(Fore.MAGENTA + "Wake Word: Hey-Robot detected!")
                    self._publish_hotword("hey-robot")
                    self._play_sound(Sound.ON)

                    if self.ambient_buffer and ambient_enabled:
                        print(Fore.CYAN + "Flushing Ambient Context...")
                        self._flush_ambient_buffer(wake_trigger=True)

                    with self.state_lock:
                        self.current_state = LedState.RECORDING
                        # Direct speech input takes priority over passive room
                        # classification. Do not let pre-wake audio bleed into
                        # the next YAMNet window.
                        self.classifier_sample_buffer = []
                        self._send_feedback(
                            header="Listening...",
                            body="    Say END-OF-LINE to finish.\n    Say EDIT-INPUT to edit transcript.",
                            header_color="bright_green",
                            body_color="bright_white",
                            font="slant",
                        )
                    self.recording_buffer = []
                    self.recording_start_time = time.time()
                    continue

                # Read hotword_enabled under lock
                with self.state_lock:
                    hotword_enabled = self.hotword_enabled

                # Ambient built-in hotwords: ONLY when hotword_enabled (independent of transcription)
                if hotword_enabled:
                    should_check_builtins = True
                    if GATE_AMBIENT_BUILTINS_WITH_VAD and not is_speech:
                        should_check_builtins = False

                    if should_check_builtins:
                        builtin_idx = self.porcupine_builtins.process(pcm_int16)
                        if builtin_idx >= 0:
                            detected = AMBIENT_BUILTIN_KEYWORDS[builtin_idx]
                            print(Fore.LIGHTGREEN_EX + f"Hotword Detection: {detected}")
                            self._publish_hotword(detected)

                # Ambient transcription buffering: ONLY when ambient_enabled (independent of hotwords)
                if ambient_enabled:
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
                                print(Fore.LIGHTBLUE_EX + f"Auto-transcribing Ambient Buffer ({buffer_duration_approx:.1f}s)")
                                self._play_sound(Sound.RECHARGE)
                                self._send_feedback(header="Transcribing...", body=f"Ambient Buffer: {buffer_duration_approx:.1f}s", header_color="bright_blue", body_color="blue",font="script")
                                self._flush_ambient_buffer()
                            else:
                                # Buffer too small, just discard to prevent drift
                                self.ambient_buffer = []
                                self.ambient_start_time = time.time()

                            last_ambient_check = now

                # --- Audio Classifier Rolling Buffer ---
                # Independent of ambient mode and VAD. Buffers all non-robot
                # audio (speech and non-speech) so YAMNet can classify whatever
                # is in the room without learning Logos's own voice/servo noise.
                # Runs only in AMBIENT/IDLE state — RECORDING branch's `continue`
                # naturally skips this block, keeping user speech out of samples.
                with self.state_lock:
                    classifier_enabled = self.classifier_enabled
                    is_speaking = self.is_speaking

                if classifier_enabled and not is_speaking:
                    self.classifier_sample_buffer.append(pcm_float32)
                    # Keep a rolling window; discard oldest frames beyond sample size
                    if len(self.classifier_sample_buffer) > CLASSIFIER_SAMPLE_FRAMES:
                        self.classifier_sample_buffer = self.classifier_sample_buffer[-CLASSIFIER_SAMPLE_FRAMES:]

                    now = time.time()
                    if (
                        not self._classifier_job_pending
                        and len(self.classifier_sample_buffer) >= CLASSIFIER_SAMPLE_FRAMES
                        and (now - last_classifier_sample) >= CLASSIFIER_SAMPLE_INTERVAL
                    ):
                        sample = np.concatenate(self.classifier_sample_buffer[-CLASSIFIER_SAMPLE_FRAMES:])
                        self.job_queue.put({'type': 'audio_classify', 'audio': sample, 'epoch': now})
                        self._classifier_job_pending = True
                        last_classifier_sample = now
                else:
                    # Free memory when classifier is off, and prevent samples
                    # from spanning across the ear-plugs interval.
                    if self.classifier_sample_buffer:
                        self.classifier_sample_buffer = []

    # -------------------------------------------------------------------------
    # Helper Logic
    # -------------------------------------------------------------------------
    def _flush_ambient_buffer(self, wake_trigger=False):
        """Package ambient buffer and send to Scribe."""
        if not self.ambient_buffer:
            return
            
        full_audio = np.concatenate(self.ambient_buffer)
        job = {
            'type': 'ambient',
            'audio': full_audio,
            'timestamp': datetime.now().strftime("%I:%M %p"),
            'epoch': time.time(), # Added epoch for easy age calculation
            'wake_trigger': wake_trigger # Pass the flag to the job
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
        self._send_feedback(header="Transcribing...", header_color="bright_green", font="script")
        with self.state_lock:
            self.current_state = LedState.TRANSCRIBING

        full_audio = np.concatenate(self.recording_buffer)
        job = {
            'type': 'human_stt',
            'audio': full_audio,
            'edit_mode': (reason == "edit"),
            'stop_reason': reason,
        }
        self.job_queue.put(job)
        
        # We don't reset state here immediately; 
        # The Scribe thread will trigger the LED change when done, 
        # but logic-wise we revert to AMBIENT/IDLE in _reset_state called by Scribe?
        # Actually, simpler: Reset logic state now, let LEDs chase until transcription event?
        # No, let's reset state in the Scribe to keep "Transcribing" LED active.
        pass 

    def _publish_hotword(self, word: str) -> None:
        now = time.time()
        last = self._last_hotword_time.get(word, 0.0)
        if (now - last) < HOTWORD_DEBOUNCE_SEC:
            return

        self._last_hotword_time[word] = now
        try:
            self.pub_hotword_detections.publish(String(data=word))
        except Exception as e:
            rospy.logwarn(f"Failed to publish hotword '{word}': {e}")

    def _reset_state(self):
        with self.state_lock:
            self.current_state = self._resolve_ambient_led_state()

    def _play_sound(self, val):
        self.sound_msg.value = val
        self.pub_sound.publish(self.sound_msg)

    def _resolve_ambient_led_state(self):
        """
        Determine the correct LED state based on ambient_enabled and hotword_enabled.
        Call under state_lock or when you know the flags are stable.
        """
        if self.ambient_enabled and self.hotword_enabled:
            return LedState.AMBIENT_BOTH
        elif self.ambient_enabled:
            return LedState.AMBIENT_TRANSCRIBE
        elif self.hotword_enabled:
            return LedState.AMBIENT_HOTWORD
        else:
            return LedState.IDLE

    def _publish_face_feedback(self, emoji, duration=3.0):
        """Publish emoji-driven face feedback for STT state changes."""
        payload = json.dumps({"emoji": emoji, "duration": duration})
        try:
            self.pub_face_cmd.publish(String(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish face feedback: {e}")

    def _face_feedback_for_header(self, header, body):
        if header == "Listening...":
            return ("🧏‍♂️", 3.0)
        if header == "Transcribing...":
            if "Ambient Buffer" in body:
                return ("🛰️", 3.0)
            return ("📝", 3.0)
        return None

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
            face_feedback = self._face_feedback_for_header(header, body)
            if face_feedback:
                self._publish_face_feedback(*face_feedback)
        except Exception as e:
            rospy.logwarn(f"Failed to publish feedback: {e}")



    # -------------------------------------------------------------------------
    # 3. The Scribe (Whisper Inference Thread)
    # -------------------------------------------------------------------------
    def _scribe_loop(self):
        """
        Waits for audio jobs, runs Faster-Whisper, publishes ROS msgs.
        """
        while self.running and not rospy.is_shutdown():
            try:
                job = self.job_queue.get(timeout=3.0)
            except queue.Empty:
                continue

            # --- Audio Classify jobs bypass Whisper entirely ---
            if job['type'] == 'audio_classify':
                self._classifier_job_pending = False

                if self.classifier_sampler is None:
                    continue

                with self.state_lock:
                    is_speaking = self.is_speaking
                    state = self.current_state

                if is_speaking or state in (LedState.RECORDING, LedState.TRANSCRIBING):
                    continue

                if len(job['audio']) < 15600:   # YAMNet needs at least ~0.975 s
                    continue

                try:
                    raw = self.classifier_sampler.classify(job['audio'])
                    payload = self.classifier_sampler.get_publication_payload()

                    with self.state_lock:
                        is_speaking = self.is_speaking
                        state = self.current_state

                    if is_speaking or state in (LedState.RECORDING, LedState.TRANSCRIBING):
                        continue

                    self.last_classifier_sample_time = time.time()
                    self.pub_classifier.publish(json.dumps(payload))

                    top = ', '.join(
                        f"{c['name']}({c['score']:.2f})"
                        for c in raw['categories'][:3]
                    ) if raw['categories'] else '(none above threshold)'
                    print(Fore.YELLOW + f"Audio Classifier: {top}")
                except Exception as e:
                    rospy.logwarn(f"Audio Classifier: classify() failed: {e}")
                continue

            # Prepare Prompt with Keywords to help jargon and cleanup
            # We specifically include the Porcupine wake words so Whisper recognizes them as distinct "tokens"
            # max prompt length 224 tokens

            if job['type'] == 'ambient':
                prompt = "Great work, thanks! Ok, so this is just some normal background conversation. My name is Mark. Hello, Logos. Hahaha! That's funny! What's it like being a robot? OH NO! This audio doesn't get diarized, so might be a bit confusing. It could include people talking about or to Logos, or it might be overheard YouTube audio. We don't include wake words here to avoid false positives. We do use jargon like: ROS Noetic, Kobuki base, Python, Linux. Hey Tom, where are Mom and Dad? I saw them with Al, Lauren, Stella, Piper, and Rocky earlier."
            elif job['type'] == 'human_stt':
                prompt = "Hello, Logos and palimpsest! It's ROS Noetic Ubuntu Linux with a Kobuki base and Python. \nEND-OF-LINE\n You have pan-tilt, top-down, map3D, astra cameras, with RGB LEDs, servos, laser, palimpsest, chora, phantasma, and phantasmata. \nEDIT-INPUT\n Use speech-to-text and palimpsest. Kobuki has GMapping for SLAM and AMCL navigation. \nEDIT-INPUT\n Voice engines are: Kokoro, Piper, E-Speak. My family is Mom, Dad, Jim, Terri, Mark Al, Tom, Lauren, Stella, Piper, Rocky, and Logos? \nEND-OF-LINE\n Hahaha! Sorry! Try that again? \nEND-OF-LINE\n"

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
            full_text = self._strip_control_phrases(full_text)

            if not full_text:
                print(Fore.YELLOW + "Transcript empty after stripping control phrases.")
                if job['type'] == 'human_stt':
                    self._reset_state()
                continue


            # --- Handling Output ---
            
            if job['type'] == 'ambient':
                
                # 1. Handle Wake Word Annotation
                if job.get('wake_trigger'):
                    full_text += "\n---\n# Wake word detected! Rerouting to <human_stt> channel..."

                # 2. Prepare Payload
                conf = round(np.exp(confidence_sum / count), 2) if count > 0 else 0.0
                
                new_entry = {
                    "time": job['timestamp'],
                    "epoch": job['epoch'], # Kept for internal filtering, useful for debugging
                    "confidence": conf,
                    "transcription": full_text
                }

                # 3. Update History & Prune
                self.ambient_history.append(new_entry)
                
                current_time = time.time()
                
                # A. Age Pruning (Filter out items older than max age)
                self.ambient_history = [
                    entry for entry in self.ambient_history 
                    if (current_time - entry['epoch']) < AMBIENT_HISTORY_MAX_AGE
                ]

                # B. Size Pruning (FIFO based on character count)
                # Calculate total chars
                total_chars = sum(len(e['transcription']) for e in self.ambient_history)
                
                # Pop from front (oldest) until we fit
                while total_chars > AMBIENT_HISTORY_MAX_CHARS and len(self.ambient_history) > 1:
                    removed = self.ambient_history.pop(0)
                    total_chars -= len(removed['transcription'])
                
                # 4. Publish Full History
                # We publish the list. Consumers can grab [-1] for latest, or iterate for context.
                self.pub_ambient.publish(json.dumps(self.ambient_history))
                
                
                self.last_ambient_publish_time = time.time()
                print(Fore.CYAN + f"Ambient Published ({len(self.ambient_history)} items). Latest: {full_text[:40]}...")


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

                final_text = final_text.strip()
                
                # if final text is null, we just skip publishing
                if not final_text:
                    print(Fore.YELLOW + "Final text empty after edit. Skipping publish.")
                    self._reset_state()
                    continue
                    #break

                # calculate full text confidence_score as percentage
                conf = round(np.exp(confidence_sum / count), 2) if count > 0 else 0.0


                stt_header = ""
                if job.get('stop_reason') == "timeout":
                    stt_header = (
                        f"# Note: stt audio recording timed out after {RECORDING_TIMEOUT} seconds. "
                        "This most likely indicates the wake word was accidentally triggered and this transcript may be from background chatter not directed at Logos.\n"
                    ) 

                stt_header += f"# faster-whisper model '{self.whisper_model_name}' confidence: {100*conf:.0f}%"

                final_text = stt_header + "\n# Transcription:\n" + final_text

                
                # Publish to Cognition
                msg = CognitionInput()
                msg.type = "human_stt"
                msg.content = final_text
                msg.system_hint = "<!-- system: If human_stt transcript is nonsensical and you are unable to infer intent, you can tell the human you misheard them and ask them to speak more clearly. Confidence scores above 70% are generally reliable. -->"
                msg.loop_cognition = True
                
                self.pub_cognition.publish(msg)
                print(Fore.GREEN + f"Published <human_stt>: {final_text}")
                
                # Done transcribing/editing, reset state
                self._reset_state()

    def _strip_control_phrases(self, text: str) -> str:
        """
        Remove wake/control phrases (hey-robot, end-of-line, edit-input) anywhere in text.
        Case-insensitive. Also removes adjacent punctuation and normalizes whitespace.
        """
        if not text:
            return ""

        # Allow "hey robot", "hey-robot", "HeyRobot", etc.
        phrases = [
            r"hey\s*[-_]?\s*robot",
            r"end\s*[-_]?\s*of\s*[-_]?\s*line",
            r"edit\s*[-_]?\s*input",
        ]

        # Remove with optional surrounding punctuation/spaces.
        # Examples eaten: "Hey-Robot,", "(end of line.)", "—EDIT-INPUT—"
        punct = r"""[ \t\r\n"'“”‘’()\[\]{}<>*#@~`^=+|\\/,:;.!?—–-]*"""
        pattern = re.compile(rf"(?i){punct}(?:{'|'.join(phrases)}){punct}")

        cleaned = pattern.sub(" ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned


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
                # When the audio classifier is enabled, show a very dim slow amber
                # breath so there's a passive indication that something is listening.
                # Max 1/255 red, 1/255 green — barely visible in daylight.
                if self.classifier_enabled:
                    now = time.time()
                    breath = (np.sin(now * 0.4) + 1) / 2.0   # ~12.5 s period
                    r = int(breath * 1)
                    g = int(breath * 1)
                    b = int(breath * 0)
                    for i in range(FACE_LED_COUNT):
                        color = (r << 16) | (g << 8 ) | b
                        data_list[i] = (i << 24) | color
                
            elif state in (LedState.AMBIENT_TRANSCRIBE, LedState.AMBIENT_HOTWORD, LedState.AMBIENT_BOTH):
                now = time.time()
                
                # 1. Base Breather Phase (shared sine wave)
                breath_phase = (np.sin(now * 2) + 1) / 2.0  # 0.0 -> 1.0

                # 2. Determine base color from state
                if state == LedState.AMBIENT_TRANSCRIBE:
                    # Dark blue breather to off
                    base_r, base_g, base_b = 0, 0, int(breath_phase * 32)
                elif state == LedState.AMBIENT_HOTWORD:
                    # Dark green breather to off
                    base_r, base_g, base_b = 0, int(breath_phase * 32), 0
                else:
                    # AMBIENT_BOTH: Smooth crossfade between blue and green, always lit.
                    # Use a slower oscillation so the color shift is distinct from the brightness pulse.
                    color_phase = (np.sin(now * 0.8) + 1) / 2.0  # 0.0 (green) -> 1.0 (blue)
                    # Brightness never drops to zero — floor at ~50% intensity
                    brightness = 0.5 + 0.5 * breath_phase
                    base_r = 0
                    base_g = int((1.0 - color_phase) * brightness * 32)
                    base_b = int(color_phase * brightness * 32)
                
                # 3. Check VAD (Reactive Shimmer)
                is_hearing_speech = getattr(self, 'is_speech_detected', False)
                
                # 4. Check Transcription Blip (Happens for 3 seconds after publish)
                time_since_publish = now - self.last_ambient_publish_time
                is_blipping = time_since_publish < 3

                data_list = []
                for i in range(FACE_LED_COUNT):
                    r, g, b = base_r, base_g, base_b
                    
                    # If hearing speech: Add a "Shimmer" effect
                    if is_hearing_speech:
                        shimmer = np.random.randint(-30, 15)
                        b = max(5, min(80, b + shimmer))
                        g = max(0, min(80, g + shimmer // 2))
                    
                    # If just transcribed: Add a traveling Cyan pulse
                    if is_blipping:
                        pos = int(now * 15) % FACE_LED_COUNT
                        if i == pos:
                            r, g, b = 0, 32, 32  # Cyan head
                        elif (i - 1) % FACE_LED_COUNT == pos:
                            g, b = 16, 16  # Cyan tail

                    # If audio classifier just ran: Add a warm magenta traveling pulse
                    time_since_classify = now - self.last_classifier_sample_time
                    if 0 < time_since_classify < CLASSIFIER_BLIP_DURATION:
                        pos = int(now * 12) % FACE_LED_COUNT  # slightly slower than cyan
                        if i == pos:
                            r, g, b = 8, 0, 8  
                        elif (i - 1) % FACE_LED_COUNT == pos:
                            r, g, b = 16, 0, 16  

                    color = (r << 16) | (g << 8) | b
                    data_list.append((i << 24) | color)

                
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
                # If Logos is speaking, we want to indicate "muted" state on the LEDs.
                # Shimmery Magenta effect to indicate "Ear Plugs" mode, like the shimmer effect in AMBIENT but with no breather.
                now = time.time()
                for i in range(FACE_LED_COUNT):
                    shimmer = np.random.randint(-24, 8)
                    r = max(0, min(32, 16 + shimmer))
                    b = max(0, min(32, 16 + shimmer))
                    color = (r << 16) | b
                    data_list.append((i << 24) | color)
                       

            # Publish
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

"""
ROS Topics Summary:
  Publishers:
    /cognition/input                     (CognitionInput)   - STT transcription results
    /stt/ambient_listener/transcription  (String, latched)  - Ambient transcription history
    /stt/hotword_listener/detections     (String)           - All hotword detections
    /stt/audio_classifier/events         (String, latched)  - YAMNet audio classification history
    /face/rgbled                         (Int32MultiArray)  - LED control
    /mobile_base/commands/sound          (Sound)            - Kobuki sounds
    /cognition/output                    (CognitionOutput)  - Feedback messages

  Subscribers:
    /tts/is_speaking             (Bool) - Global ear plug
    /stt/ambient_listener/enable (Bool) - Enable/disable ambient Whisper transcription
    /stt/hotword_listener/enable (Bool) - Enable/disable Porcupine built-in hotword detection
    /stt/audio_classifier/enable (Bool) - Enable/disable MediaPipe YAMNet audio classification
"""
