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
from collections import deque
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
from std_msgs.msg import Empty, String, Bool, Int32MultiArray
from kobuki_msgs.msg import Sound
from logos_framework.msg import CognitionInput, CognitionOutput

# OpenWakeWord & Whisper
try:
    from openwakeword.model import Model as OpenWakeWordModel
except ImportError as exc:
    raise ImportError(
        "OpenWakeWord is required for Logos STT. Install it in the robot_ws "
        "virtualenv with: /home/robot/robot_ws/.venv/bin/python3 -m pip "
        "install openwakeword"
    ) from exc
# Colorama for terminal output
from colorama import Fore, Style, init as colorama_init
colorama_init(autoreset=True)

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------

# Audio Settings
SAMPLE_RATE = 16000
FRAME_LENGTH = 512  # Silero VAD already runs on these 32 ms frames
CHANNELS = 1
AUDIO_DEVICE_CANDIDATES = ('logos_mic', 'pan_tilt_mic')
AUDIO_DEVICE_ENV = 'LOGOS_STT_AUDIO_DEVICES'

# OpenWakeWord assets. `wakewords/custom` is searched first so a Logos-trained
# model can replace a community model by reusing its directory name.
WAKEWORD_MODEL_ROOTS = [
    os.path.expanduser('~/robot_ws/wakewords/custom'),
    os.path.expanduser('~/robot_ws/wakewords/home-assistant-wakewords-collection/en'),
]
OPENWAKEWORD_FEATURE_PATH = os.path.expanduser(
    '~/robot_ws/wakewords/openwakeword-feature-models'
)
OPENWAKEWORD_FEATURE_MODELS = {
    'onnx': {
        'melspec': f'{OPENWAKEWORD_FEATURE_PATH}/melspectrogram.onnx',
        'embedding': f'{OPENWAKEWORD_FEATURE_PATH}/embedding_model.onnx',
    },
    'tflite': {
        'melspec': f'{OPENWAKEWORD_FEATURE_PATH}/melspectrogram.tflite',
        'embedding': f'{OPENWAKEWORD_FEATURE_PATH}/embedding_model.tflite',
    },
}
OPENWAKEWORD_FRAME_LENGTH = 1280  # 80 ms at 16 kHz
OPENWAKEWORD_MODEL_FORMATS = ('onnx', 'tflite')

# Directory names double as the stable wakeword labels. Underscores are
# published as spaces ("ok_computer" -> "ok computer").
CORE_WAKEWORDS = {
    'wake': 'hey_robot',
    'end': 'end_of_line',
    'cancel': 'cancel_that',
}
CORE_WAKEWORD_THRESHOLDS = {
    'wake': 0.75,
    'end': 0.5,
    'cancel': 0.5,
}
OPTIONAL_CORE_WAKEWORDS = {
    'edit': {
        'directory': 'edit_input',
        'threshold': 0.85,
        'enabled_param': '~enable_edit_wakeword',
        'default_enabled': False,
    },
}

# Passive hotwords are selected dynamically through
# `/stt/hotword_listener/enable`; all of them use this confidence threshold.
PASSIVE_HOTWORD_THRESHOLD = 0.5

# Reuse the existing Silero pass with a loose wakeword gate and a stricter
# ambient-transcript gate. A short max window keeps phrase tails from muting a
# valid OpenWakeWord prediction.
AMBIENT_VAD_THRESHOLD = 0.5
WAKEWORD_VAD_THRESHOLD = 0.15
WAKEWORD_VAD_WINDOW_SEC = 0.5
WAKEWORD_VAD_WINDOW_FRAMES = max(1, int(WAKEWORD_VAD_WINDOW_SEC * SAMPLE_RATE / FRAME_LENGTH))

# OpenWakeWord predictions can stay high across adjacent chunks.
HOTWORD_DEBOUNCE_SEC = 1.5

WAKE_TRIGGER_NOTE = "\n---\n# Wake word detected! Rerouting to <human_stt> channel..."

# Timers (Seconds)
AMBIENT_MAX_DURATION = 90    # seconds hard cap for buffer
AMBIENT_SILENCE_TRIGGER = 8  # seconds of VAD silence before flushing ambient
RECORDING_TIMEOUT = 120        # second hard limit for user input
MIN_AMBIENT_LENGTH = 16       # Minimum seconds to bother transcribing ambient

# LED Constants
FACE_LED_COUNT = 12

# Ambient History Settings
AMBIENT_HISTORY_MAX_AGE = 7200    # 2 Hours (in seconds)
AMBIENT_HISTORY_MAX_CHARS = 32767 # Max characters before oldest is dropped

# Audio Classifier Settings (MediaPipe YAMNet)
CLASSIFIER_MODEL_PATH      = os.path.expanduser('~/robot_ws/models/yamnet.tflite')
CLASSIFIER_SAMPLE_INTERVAL = 3.0  # seconds between classifier dispatches
CLASSIFIER_SAMPLE_DURATION = 1.5   # seconds of audio per sample
CLASSIFIER_SAMPLE_FRAMES   = int(CLASSIFIER_SAMPLE_DURATION * SAMPLE_RATE / FRAME_LENGTH)  # ~78 frames
CLASSIFIER_BOOST_FACTOR    = 0.0   # temporal confidence boost per repeated detection
CLASSIFIER_TOP_K           = 10    # max YAMNet labels per sample
CLASSIFIER_SCORE_THRESHOLD = 0.10  # minimum score to include in output
CLASSIFIER_BLIP_DURATION   = 1.0   # seconds for LED overlay after each sample
CLASSIFIER_LABEL_BLACKLIST = {
    'Chewing, mastication',
    'Crunching',
    'Insect',
    'Fly',
    'Mosquito',
    'housefly'
}

class LedState:
    IDLE = 0
    AMBIENT_TRANSCRIBE = 1   # Ambient transcription only (dark blue breather)
    AMBIENT_HOTWORD = 2      # Default hotword listening only (dark green breather)
    AMBIENT_BOTH = 3         # Both active (blue <-> green crossfade, always lit)
    RECORDING = 4
    TRANSCRIBING = 5
    EDIT_INPUT = 6
    EAR_PLUGS = 7
    WAKEWORD_ONLY = 8        # Core hey-robot wakeword only (single green blinker)

# -----------------------------------------------------------------------------
# The Node Class
# -----------------------------------------------------------------------------

class LogosEarsNode:
    def __init__(self):
        rospy.init_node('logos_ears_node', anonymous=False)
        self.audio_device_candidates = self._load_audio_device_candidates()
        self.active_audio_device = None
        
        # --- State Variables ---
        self.state_lock = threading.Lock()
        self.is_speaking = False         # From TTS
        self.ambient_enabled = False     # From external topic
        self.hotword_enabled = False     # True while passive models are loaded
        self.current_state = LedState.IDLE
        self.last_ambient_publish_time = 0
        self.reset_wakewords_pending = False


        # --- Buffers & Queues ---
        self.audio_queue = queue.Queue() # From Ear -> Brain
        self.job_queue = queue.Queue()   # From Brain -> Scribe
        self.openwakeword_audio_buffer = np.array([], dtype=np.int16)
        self.wakeword_vad_history = deque(maxlen=WAKEWORD_VAD_WINDOW_FRAMES)
        
        self.ambient_buffer = []         # List of numpy arrays
        self.ambient_start_time = time.time()
        
        self.recording_buffer = []       # List of numpy arrays
        self.recording_start_time = 0
        
        #  Background Audio Transcription History Storage
        self.ambient_history = []
        self.ambient_history_lock = threading.Lock()
        self._wake_context_seq = 0
        self._active_wake_context_id = None
        self._canceled_wake_context_ids = set()

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

        core_wakewords, core_thresholds = self._configured_core_wakewords()
        self.core_wakeword_models = self._discover_role_models(
            core_wakewords,
            core_thresholds,
        )
        frameworks = {
            model['framework']
            for model in self.core_wakeword_models.values()
        }
        self._validate_feature_models(frameworks)

        self.core_wakewords = self._load_wakeword_models(self.core_wakeword_models)
        self.passive_hotword_directories = ()
        self.passive_wakeword_models = {}
        self.passive_wakewords = {}

        print(
            "OpenWakeWord CORE: "
            f"{[model['label'] for model in self.core_wakeword_models.values()]}"
        )
        print("OpenWakeWord PASSIVE: waiting for requested hotwords.")

        # Hotword debounce bookkeeping
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
        # self.whisper_model_name = "small.en"
        self.whisper_model_name = "distil-medium.en"
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ImportError(
                "faster-whisper is required by stt_node.py. Use "
                "nemotron_stt_node.py for the ONNX Nemotron backend."
            ) from exc
        self.whisper = WhisperModel(
            self.whisper_model_name,
            device="cpu",
            compute_type="int8",
        )
        
        print(Fore.CYAN + "Models Loaded.")

    def _configured_core_wakewords(self):
        """Return active core wakewords, including optional ROS-param controls."""
        wakewords = dict(CORE_WAKEWORDS)
        thresholds = dict(CORE_WAKEWORD_THRESHOLDS)

        for role, config in OPTIONAL_CORE_WAKEWORDS.items():
            enabled = rospy.get_param(
                config['enabled_param'],
                config['default_enabled'],
            )
            if enabled:
                wakewords[role] = config['directory']
                thresholds[role] = config['threshold']

        return wakewords, thresholds

    def _discover_role_models(self, role_labels, thresholds):
        """Resolve configured directory labels into OpenWakeWord assets."""
        return {
            role: self._discover_wakeword_model(label, thresholds[role])
            for role, label in role_labels.items()
        }

    def _discover_wakeword_model(self, directory_label, threshold):
        """
        Find a model in a named wakeword directory.

        ONNX is preferred for the robot's established runtime. Within a
        directory, a lexicographically later filename wins so versioned
        collections naturally select v2 over v1 without pinning filenames.
        """
        candidate_dirs = [
            os.path.join(root, directory_label)
            for root in WAKEWORD_MODEL_ROOTS
        ]

        for model_dir in candidate_dirs:
            if not os.path.isdir(model_dir):
                continue
            for framework in OPENWAKEWORD_MODEL_FORMATS:
                paths = sorted(
                    os.path.join(model_dir, filename)
                    for filename in os.listdir(model_dir)
                    if filename.lower().endswith(f'.{framework}')
                )
                if paths:
                    return {
                        'directory': directory_label,
                        'framework': framework,
                        'label': self._wakeword_label(directory_label),
                        'path': paths[-1],
                        'threshold': threshold,
                    }

        searched = "\n".join(candidate_dirs)
        raise FileNotFoundError(
            f"No ONNX or TFLite model found for OpenWakeWord '{directory_label}'.\n"
            f"Searched directories:\n{searched}"
        )

    def _load_wakeword_models(self, role_models):
        """Load one OpenWakeWord predictor per model format."""
        models = {}
        for framework in OPENWAKEWORD_MODEL_FORMATS:
            framework_roles = {
                role: model
                for role, model in role_models.items()
                if model['framework'] == framework
            }
            if not framework_roles:
                continue
            feature_models = OPENWAKEWORD_FEATURE_MODELS[framework]
            try:
                predictor = OpenWakeWordModel(
                    wakeword_models=[model['path'] for model in framework_roles.values()],
                    inference_framework=framework,
                    melspec_model_path=feature_models['melspec'],
                    embedding_model_path=feature_models['embedding'],
                )
            except Exception as exc:
                paths = [model['path'] for model in framework_roles.values()]
                raise RuntimeError(
                    f"OpenWakeWord failed to load {framework} models {paths}. "
                    "Prefer ONNX wakeword assets for this Logos venv when both "
                    "ONNX and TFLite are available."
                ) from exc
            models[framework] = {
                'predictor': predictor,
                'prediction_roles': self._prediction_roles(framework_roles),
                'role_models': framework_roles,
            }
        return models

    def _prediction_roles(self, role_models):
        """Map OpenWakeWord prediction keys back to stable runtime roles."""
        return {
            os.path.splitext(os.path.basename(model['path']))[0]: role
            for role, model in role_models.items()
        }

    def _validate_feature_models(self, frameworks):
        """Check shared OpenWakeWord feature models before predictor startup."""
        missing = [
            path
            for framework in frameworks
            for path in OPENWAKEWORD_FEATURE_MODELS[framework].values()
            if not os.path.exists(path)
        ]
        if missing:
            raise FileNotFoundError(
                "Shared OpenWakeWord feature models are missing: "
                f"{', '.join(missing)}"
            )

    def _wakeword_label(self, directory_label):
        """Convert asset directory names into spoken labels for ROS output."""
        return re.sub(r'\s+', ' ', directory_label.replace('_', ' ')).strip()

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
        self.pub_python_interrupt = rospy.Publisher('/python/interrupt', String, queue_size=1)
        self.pub_cognition_prefetch = rospy.Publisher('/cognition/prefetch', Empty, queue_size=1)

        # Subscribers
        rospy.Subscriber('/tts/is_speaking', Bool, self._cb_is_speaking)
        rospy.Subscriber('/stt/ambient_listener/enable', Bool, self._cb_ambient_enable)
        rospy.Subscriber('/stt/hotword_listener/enable', String, self._cb_hotword_enable)
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
                self.reset_wakewords_pending = True
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
                self.ambient_start_time = time.time()
                should_clear_ambient = True
                with self.ambient_history_lock:
                    self.ambient_history = []
                    self._active_wake_context_id = None
                    self._canceled_wake_context_ids.clear()

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
        hotword_directories = self._parse_passive_hotword_payload(msg.data)
        if hotword_directories is None:
            return

        hotword_directories = tuple(hotword_directories)
        with self.state_lock:
            if hotword_directories == self.passive_hotword_directories:
                return
            self.passive_hotword_directories = ()
            self.passive_wakeword_models = {}
            self.passive_wakewords = {}
            self.hotword_enabled = False
            self.reset_wakewords_pending = True
            if (
                not self.is_speaking
                and self.current_state not in (LedState.RECORDING, LedState.TRANSCRIBING)
            ):
                self.current_state = self._resolve_ambient_led_state()

        if not hotword_directories:
            print(Fore.LIGHTGREEN_EX + "Hotword Listener: Disabled")
            return

        try:
            passive_wakeword_models = self._discover_role_models(
                {directory: directory for directory in hotword_directories},
                {
                    directory: PASSIVE_HOTWORD_THRESHOLD
                    for directory in hotword_directories
                },
            )
            frameworks = {
                model['framework']
                for model in passive_wakeword_models.values()
            }
            self._validate_feature_models(frameworks)
            passive_wakewords = self._load_wakeword_models(passive_wakeword_models)
        except Exception as exc:
            rospy.logerr(
                "Hotword Listener: failed to load passive hotwords %s: %s",
                list(hotword_directories),
                exc,
            )
            return

        with self.state_lock:
            self.passive_hotword_directories = hotword_directories
            self.passive_wakeword_models = passive_wakeword_models
            self.passive_wakewords = passive_wakewords
            self.hotword_enabled = bool(hotword_directories)
            if not self.is_speaking and self.current_state not in (LedState.RECORDING, LedState.TRANSCRIBING):
                self.current_state = self._resolve_ambient_led_state()
            labels = [
                model['label']
                for model in self.passive_wakeword_models.values()
            ]
            print(Fore.LIGHTGREEN_EX + f"Hotword Listener: {labels or 'Disabled'}")

    def _parse_passive_hotword_payload(self, payload):
        """Return requested passive hotword model directories from JSON."""
        try:
            hotwords = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            rospy.logwarn(f"Hotword Listener: expected JSON hotword list: {exc}")
            return None

        if not isinstance(hotwords, list):
            rospy.logwarn("Hotword Listener: expected JSON list of hotword names.")
            return None

        directories = []
        seen = set()
        for hotword in hotwords:
            if not isinstance(hotword, str) or not hotword.strip():
                rospy.logwarn(
                    "Hotword Listener: hotword list entries must be non-empty strings."
                )
                return None
            directory = hotword.strip()
            if directory not in seen:
                directories.append(directory)
                seen.add(directory)
        return directories

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
                label_blacklist=CLASSIFIER_LABEL_BLACKLIST,
            )
            print(Fore.YELLOW + "Audio Classifier: MediaPipe YAMNet loaded.")
        except Exception as e:
            rospy.logerr(f"Audio Classifier: Failed to load model: {e}")
            with self.state_lock:
                self.classifier_enabled = False

    # -------------------------------------------------------------------------
    # 1. The Ear (Audio Capture Thread)
    # -------------------------------------------------------------------------
    def _load_audio_device_candidates(self):
        """
        Return ordered ALSA/PortAudio device names for capture.

        Defaults prefer the new dedicated Logos mic and fall back to the old
        webcam alias. Override with the private ROS param `~audio_devices` or
        the LOGOS_STT_AUDIO_DEVICES env var, using a comma-separated string or
        a ROS list.
        """
        configured = rospy.get_param('~audio_devices', None)
        if configured is None:
            configured = os.environ.get(AUDIO_DEVICE_ENV)

        candidates = self._parse_audio_device_candidates(configured)
        if not candidates:
            candidates = list(AUDIO_DEVICE_CANDIDATES)

        rospy.loginfo(f"STT audio device preference: {', '.join(candidates)}")
        return candidates

    @staticmethod
    def _parse_audio_device_candidates(configured):
        if configured is None:
            return []

        if isinstance(configured, str):
            raw_candidates = configured.split(',')
        elif isinstance(configured, (list, tuple)):
            raw_candidates = configured
        else:
            rospy.logwarn(
                f"Ignoring unsupported ~audio_devices value {configured!r}; "
                "expected comma-separated string or list"
            )
            return []

        candidates = []
        for candidate in raw_candidates:
            candidate = str(candidate).strip()
            if candidate:
                candidates.append(candidate)
        return candidates

    def _audio_capture_loop(self):
        """
        Reads audio from device, calculates RMS (for LEDs), puts in queue.
        Blocking reads to ensure no data loss.
        """
        while self.running and not rospy.is_shutdown():
            for device_name in self.audio_device_candidates:
                opened_stream = False
                if not self.running or rospy.is_shutdown():
                    return
                try:
                    with sd.InputStream(
                        samplerate=SAMPLE_RATE,
                        blocksize=FRAME_LENGTH,
                        channels=CHANNELS,
                        dtype='int16',
                        device=device_name
                    ) as stream:
                        opened_stream = True
                        self.active_audio_device = device_name
                        rospy.loginfo(f"STT audio capture using device: {device_name}")

                        while self.running and not rospy.is_shutdown():
                            # Read audio
                            pcm, overflow = stream.read(FRAME_LENGTH)
                            if overflow:
                                print(Fore.YELLOW + "Audio Overflow")

                            # Convert to standard format for processing.
                            # OpenWakeWord needs 16-bit integers.
                            # Whisper/VAD usually like float32 between -1 and 1.
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
                    if opened_stream:
                        rospy.logerr(f"Audio Capture Error on {device_name}: {e}")
                    else:
                        rospy.logwarn(f"STT audio device unavailable ({device_name}): {e}")
                    self.active_audio_device = None

            if self.running and not rospy.is_shutdown():
                rospy.logerr(
                    "No configured STT audio capture devices are available; "
                    f"retrying in 2 seconds: {', '.join(self.audio_device_candidates)}"
                )
                time.sleep(2.0)

    # -------------------------------------------------------------------------
    # 2. The Brain (Logic & State Machine Loop)
    # -------------------------------------------------------------------------
    def _brain_loop(self):
        """
        Consumes audio from queue. Checks wakewords. Checks VAD. Manages buffers.
        """
        # Timers
        last_ambient_speech_time = 0.0   # 0 = no speech heard yet this session
        last_classifier_sample = time.time()

        while self.running and not rospy.is_shutdown():
            try:
                # Get audio chunk (blocking with timeout to allow shutdown check)
                pcm_int16 = self.audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            with self.state_lock:
                reset_wakewords_pending = self.reset_wakewords_pending
                self.reset_wakewords_pending = False

            if reset_wakewords_pending:
                self._reset_wakeword_models()

            now = time.time()

            # --- Pre-processing ---
            pcm_float32 = pcm_int16.astype(np.float32) / 32768.0

            # 1) VAD. Ambient and wakeword paths share this inference but use
            # separate thresholds.
            vad_prob = self.vad_model(torch.from_numpy(pcm_float32), SAMPLE_RATE).item()
            self.wakeword_vad_history.append(vad_prob)
            is_ambient_speech = vad_prob > AMBIENT_VAD_THRESHOLD
            is_wakeword_speech = (
                max(self.wakeword_vad_history, default=0.0) > WAKEWORD_VAD_THRESHOLD
            )
            self.is_speech_detected = is_ambient_speech

            # 2) OpenWakeWord gets the same int16 stream in 80 ms windows.
            core_detections, passive_detections = self._predict_wakewords(
                pcm_int16,
                allow_detections=is_wakeword_speech,
            )

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

                if 'end' in core_detections:
                    label = self.core_wakeword_models['end']['label']
                    print(Fore.GREEN + f"Stop Word: {label}")
                    self._publish_hotword(label)
                    self._play_sound(Sound.OFF)
                    self._finish_recording(reason="normal")

                elif 'cancel' in core_detections:
                    label = self.core_wakeword_models['cancel']['label']
                    print(Fore.YELLOW + f"Cancel Word: {label}")
                    self._play_sound(Sound.OFF)
                    self._cancel_active_wake_context_annotation()
                    self._send_feedback(
                        header="Canceled!",
                        body=" - Stopped listening - ",
                        body_color="white",
                        header_color="bright_yellow",
                        font="doom",
                    )
                    self.recording_buffer = []
                    self.recording_start_time = 0
                    self._reset_state()
                    self._reset_wakeword_models()

                elif 'edit' in core_detections:
                    label = self.core_wakeword_models['edit']['label']
                    print(Fore.YELLOW + f"Stop Word: {label}")
                    self._publish_hotword(label)
                    self._play_sound(Sound.OFF)
                    self._finish_recording(reason="edit")

                continue  # keep recording unless stop/timeout handled

            # --- AMBIENT / IDLE ---
            else:
                # Wake -> publish + proceed (always-on, regardless of listening state)
                if 'wake' in core_detections:
                    label = self.core_wakeword_models['wake']['label']
                    print(Fore.MAGENTA + f"Wake Word: {label} detected!")
                    self._publish_hotword(label)
                    self._publish_python_interrupt()
                    self._play_sound(Sound.ON)

                    wake_context_id = None
                    if self.ambient_buffer and ambient_enabled:
                        print(Fore.CYAN + "Flushing Ambient Context...")
                        wake_context_id = self._next_wake_context_id()
                        self._flush_ambient_buffer(
                            wake_trigger=True,
                            wake_context_id=wake_context_id,
                            prefetch_after=True,
                        )
                    else:
                        self._publish_cognition_prefetch()

                    with self.state_lock:
                        self.current_state = LedState.RECORDING
                        # Direct speech input takes priority over passive room
                        # classification. Do not let pre-wake audio bleed into
                        # the next YAMNet window.
                        self.classifier_sample_buffer = []
                        self._send_feedback(
                            header="Listening...",
                            body=self._recording_prompt_text(),
                            header_color="bright_green",
                            body_color="bright_white",
                            font="slant",
                        )
                    with self.ambient_history_lock:
                        self._active_wake_context_id = wake_context_id
                    self.recording_buffer = []
                    self.recording_start_time = time.time()
                    continue

                # Passive hotwords are independent of ambient transcription.
                for role in passive_detections:
                    detected = self._wakeword_label(role)
                    if detected:
                        print(Fore.LIGHTGREEN_EX + f"Hotword Detection: {detected}")
                        self._publish_hotword(detected)

                # Ambient transcription buffering: ONLY when ambient_enabled (independent of hotwords)
                if ambient_enabled:
                    if is_ambient_speech:
                        self.ambient_buffer.append(pcm_float32)
                        last_ambient_speech_time = now

                    # Manage Ambient Buffer Lifecycle
                    buffer_duration_approx = (len(self.ambient_buffer) * FRAME_LENGTH) / SAMPLE_RATE

                    silence_secs = (now - last_ambient_speech_time) if last_ambient_speech_time > 0 else 0.0
                    hit_max_length = buffer_duration_approx >= AMBIENT_MAX_DURATION
                    hit_silence = silence_secs >= AMBIENT_SILENCE_TRIGGER and not is_ambient_speech

                    if hit_max_length or hit_silence:
                        # Only flush if paused (not mid-speech), or hard cap is blown
                        if not is_ambient_speech or buffer_duration_approx > (AMBIENT_MAX_DURATION + 15):
                            if buffer_duration_approx > MIN_AMBIENT_LENGTH:
                                print(Fore.LIGHTBLUE_EX + f"Auto-transcribing Ambient Buffer ({buffer_duration_approx:.1f}s, silence {silence_secs:.0f}s)")
                                self._play_sound(Sound.RECHARGE)
                                self._send_feedback(header="Transcribing...", body=f"Ambient Buffer: {buffer_duration_approx:.1f}s", header_color="bright_blue", body_color="blue", font="script")
                                self._flush_ambient_buffer()
                            else:
                                # Buffer too small, just discard to prevent drift
                                self.ambient_buffer = []
                                self.ambient_start_time = time.time()

                            last_ambient_speech_time = now  # re-arm: need fresh silence after next speech

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
    def _predict_wakewords(self, pcm_int16, allow_detections):
        """
        Feed OpenWakeWord complete 80 ms int16 windows.

        The prediction models always see contiguous audio while each role still
        decides whether a score should act on the current STT state.
        """
        self.openwakeword_audio_buffer = np.concatenate(
            (self.openwakeword_audio_buffer, pcm_int16)
        )
        core_detections = set()
        passive_detections = set()

        while len(self.openwakeword_audio_buffer) >= OPENWAKEWORD_FRAME_LENGTH:
            wakeword_frame = self.openwakeword_audio_buffer[:OPENWAKEWORD_FRAME_LENGTH]
            self.openwakeword_audio_buffer = self.openwakeword_audio_buffer[
                OPENWAKEWORD_FRAME_LENGTH:
            ]

            core_detections.update(
                self._predict_model_groups(
                    self.core_wakewords,
                    wakeword_frame,
                    allow_detections,
                )
            )

            with self.state_lock:
                hotword_enabled = self.hotword_enabled
                passive_wakewords = self.passive_wakewords

            if hotword_enabled:
                passive_detections.update(
                    self._predict_model_groups(
                        passive_wakewords,
                        wakeword_frame,
                        allow_detections,
                    )
                )

        return core_detections, passive_detections

    def _predict_model_groups(self, model_groups, wakeword_frame, allow_detections):
        """Run each OpenWakeWord format group and return runtime roles."""
        detections = set()
        for group in model_groups.values():
            detections.update(
                self._roles_above_threshold(
                    group['predictor'].predict(wakeword_frame),
                    group['prediction_roles'],
                    group['role_models'],
                    allow_detections,
                )
            )
        return detections

    def _roles_above_threshold(
        self,
        predictions,
        prediction_roles,
        role_models,
        allow_detections,
    ):
        if not allow_detections:
            return set()

        detections = set()
        for prediction_name, score in predictions.items():
            role = prediction_roles.get(prediction_name)
            if role and score >= role_models[role]['threshold']:
                detections.add(role)
        return detections

    def _reset_wakeword_models(self):
        """Clear partial audio context across robot speech intervals."""
        self.openwakeword_audio_buffer = np.array([], dtype=np.int16)
        self.wakeword_vad_history.clear()
        for model_groups_name in ('core_wakewords', 'passive_wakewords'):
            for group in getattr(self, model_groups_name, {}).values():
                group['predictor'].reset()

    def _flush_ambient_buffer(self, wake_trigger=False, wake_context_id=None, prefetch_after=False):
        """Package ambient buffer and send to Scribe."""
        if not self.ambient_buffer:
            return
            
        full_audio = np.concatenate(self.ambient_buffer)
        job = {
            'type': 'ambient',
            'audio': full_audio,
            'timestamp': datetime.now().strftime("%I:%M %p"),
            'epoch': time.time(), # Added epoch for easy age calculation
            'wake_trigger': wake_trigger, # Pass the flag to the job
            'wake_context_id': wake_context_id,
            'prefetch_after': prefetch_after,
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

    def _next_wake_context_id(self):
        with self.ambient_history_lock:
            self._wake_context_seq += 1
            return self._wake_context_seq

    def _ambient_history_payload_locked(self):
        return [
            {
                key: value
                for key, value in entry.items()
                if not key.startswith('_')
            }
            for entry in self.ambient_history
        ]

    def _without_wake_trigger_note(self, transcription):
        if transcription.endswith(WAKE_TRIGGER_NOTE):
            return transcription[:-len(WAKE_TRIGGER_NOTE)].rstrip()
        return transcription

    def _cancel_active_wake_context_annotation(self):
        payload = None

        with self.ambient_history_lock:
            wake_context_id = self._active_wake_context_id
            self._active_wake_context_id = None

            if wake_context_id is None:
                return

            self._canceled_wake_context_ids.add(wake_context_id)

            for entry in reversed(self.ambient_history):
                if entry.get('_wake_context_id') != wake_context_id:
                    continue

                transcription = entry.get('transcription', '')
                if transcription.endswith(WAKE_TRIGGER_NOTE):
                    entry['transcription'] = self._without_wake_trigger_note(
                        transcription
                    )
                    self._canceled_wake_context_ids.discard(wake_context_id)
                    payload = self._ambient_history_payload_locked()
                break

        if payload is not None:
            self.pub_ambient.publish(json.dumps(payload))
            print(Fore.CYAN + "Ambient wake annotation removed after cancel.")

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

    def _publish_python_interrupt(self) -> None:
        payload = {
            "source": "human_stt",
            "message": (
                "A human has politely interrupted your Python execution. "
                "Standby for message."
            ),
            "loop_cognition": False,
        }
        try:
            self.pub_python_interrupt.publish(String(data=json.dumps(payload)))
        except Exception as e:
            rospy.logwarn(f"Failed to publish Python interrupt: {e}")

    def _publish_cognition_prefetch(self) -> None:
        try:
            self.pub_cognition_prefetch.publish(Empty())
        except Exception as e:
            rospy.logwarn(f"Failed to publish cognition prefetch trigger: {e}")

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
            return LedState.WAKEWORD_ONLY

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

            # Prepare prompt with keywords to help jargon and cleanup.
            # Include the wake/control phrases so Whisper recognizes them before
            # `_strip_control_phrases` removes them from the human transcript.
            # max prompt length 224 tokens

            if job['type'] == 'ambient':
                prompt = "Great work, thanks! Ok, so this is just some normal background conversation. My name is Mark. Hello, Logos. Hahaha! That's funny! What's it like being a robot? OH NO! This audio doesn't get diarized, so might be a bit confusing. It could include people talking about or to Logos, or it might be overheard YouTube audio. We don't include wake words here to avoid false positives. We do use jargon like: ROS Noetic, Kobuki base, Python, Linux. Hey Tom, where are Mom and Dad? I saw them with Al, Lauren, Stella, Piper, and Rocky earlier."
            elif job['type'] == 'human_stt':
                prompt = (
                    "Hello, Logos and palimpsest! It's ROS Noetic Ubuntu Linux "
                    "with a Kobuki base and Python.\n"
                    f"{self._prompt_phrases_text()}\n"
                    "You have pan-tilt, top-down, map3D, astra cameras, with "
                    "RGB LEDs, servos, laser, palimpsest, Chora, phantasma, "
                    "and phantasmata. Use speech-to-text and palimpsest. "
                    "Kobuki has GMapping for SLAM and AMCL navigation.\n"
                    f"{self._prompt_phrases_text()}\n"
                    "Voice engines are: Kokoro, Piper, E-Speak, Chora. My "
                    "family is Mom, Dad, Jim, Terri, Mark Al, Tom, Lauren, "
                    "Stella, Piper, Rocky, and Logos? Hahaha! Sorry! Try that "
                    "again?"
                )

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
                if job['type'] == 'ambient' and job.get('prefetch_after'):
                    self._publish_cognition_prefetch()
                if job['type'] == 'human_stt':
                    self._reset_state()
                continue


            # --- Handling Output ---
            
            if job['type'] == 'ambient':
                
                # 1. Handle Wake Word Annotation
                wake_context_id = job.get('wake_context_id')
                if job.get('wake_trigger'):
                    full_text += WAKE_TRIGGER_NOTE

                # 2. Prepare Payload
                conf = round(np.exp(confidence_sum / count), 2) if count > 0 else 0.0
                
                new_entry = {
                    "time": job['timestamp'],
                    "epoch": job['epoch'], # Kept for internal filtering, useful for debugging
                    "confidence": conf,
                    "transcription": full_text,
                }
                if wake_context_id is not None:
                    new_entry['_wake_context_id'] = wake_context_id

                # 3. Update History & Prune
                with self.ambient_history_lock:
                    if wake_context_id in self._canceled_wake_context_ids:
                        new_entry['transcription'] = self._without_wake_trigger_note(
                            new_entry['transcription']
                        )
                        self._canceled_wake_context_ids.discard(wake_context_id)

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

                    ambient_payload = self._ambient_history_payload_locked()
                
                # 4. Publish Full History
                # We publish the list. Consumers can grab [-1] for latest, or iterate for context.
                self.pub_ambient.publish(json.dumps(ambient_payload))
                
                
                self.last_ambient_publish_time = time.time()
                print(Fore.CYAN + f"Ambient Published ({len(ambient_payload)} items). Latest: {new_entry['transcription'][:40]}...")
                if job.get('prefetch_after'):
                    self._publish_cognition_prefetch()


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
                conf_pct = 100 * conf

                content_meta = [f" - conf: {conf_pct:.0f}%"]
                hint_parts = [
                    f"<!-- system: Estimated STT confidence: {conf_pct:.0f}%.",
                ]
                if conf_pct < 65:
                    hint_parts.append(
                        "Low confidence: if the transcript is nonsensical and you cannot infer intent, tell the human you misheard them and ask them to speak more clearly."
                    )
                if job.get('stop_reason') == "timeout":
                    content_meta.append(" - timed out!")
                    hint_parts.append(
                        f"The stt audio recording timed out after {RECORDING_TIMEOUT} seconds; "
                        "this may indicate an accidental wake word trigger or background chatter not directed at Logos."
                    )
                hint_parts.append("-->")
                final_text = final_text + "\n\n" + "\n".join(content_meta)

                
                # Publish to Cognition
                msg = CognitionInput()
                msg.type = "human_stt"
                msg.content = final_text
                msg.system_hint = " ".join(hint_parts)
                msg.loop_cognition = True
                
                self.pub_cognition.publish(msg)
                print(Fore.GREEN + f"Published <human_stt>: {final_text}")
                
                # Done transcribing/editing, reset state
                self._reset_state()

    def _strip_control_phrases(self, text: str) -> str:
        """
        Remove configured core wake/control phrases anywhere in text.
        Case-insensitive. Also removes adjacent punctuation and normalizes whitespace.
        """
        if not text:
            return ""

        # Allow phrase forms like "ok computer", "OK-COMPUTER", and
        # "OkComputer" without pinning control phrase text here.
        phrases = [
            self._wakeword_phrase_pattern(model['label'])
            for model in self.core_wakeword_models.values()
        ]

        # Remove with optional surrounding punctuation/spaces.
        # Examples eaten: "OK-COMPUTER,", "(terminator.)", "OK-WIRE-TAP"
        punct = r"""[ \t\r\n"'“”‘’()\[\]{}<>*#@~`^=+|\\/,:;.!?—–-]*"""
        pattern = re.compile(rf"(?i){punct}(?:{'|'.join(phrases)}){punct}")

        cleaned = pattern.sub(" ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _prompt_phrase(self, role):
        """Render a configured core label for Whisper and STT feedback."""
        label = self.core_wakeword_models[role]['label']
        return re.sub(r'[\s_]+', '-', label).upper()

    def _recording_prompt_text(self):
        """Render recording controls that are active in this process."""
        prompts = [f"    Say {self._prompt_phrase('end')} to finish."]
        if 'cancel' in self.core_wakeword_models:
            prompts.append(f"    Say {self._prompt_phrase('cancel')} to cancel.")
        if 'edit' in self.core_wakeword_models:
            prompts.append(f"    Say {self._prompt_phrase('edit')} to edit transcript.")
        return "\n".join(prompts)

    def _prompt_phrases_text(self):
        """Inline all configured core phrases in a Whisper-friendly form."""
        return "\n".join(
            self._prompt_phrase(role)
            for role in self.core_wakeword_models
        )

    def _wakeword_phrase_pattern(self, label):
        """Build a permissive transcript-cleanup regex for one label."""
        parts = [
            re.escape(part)
            for part in re.split(r'[\s_-]+', label)
            if part
        ]
        return r"\s*[-_]?\s*".join(parts)


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

            elif state == LedState.WAKEWORD_ONLY:
                blink_on = int(time.time() * 1) % 2 == 0
                if blink_on:
                    color = 0x004000
                    data_list[0] = (0 << 24) | color
                
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
                    r = int((i / (FACE_LED_COUNT//3)) * 255)
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
            elif state in (LedState.IDLE, LedState.WAKEWORD_ONLY):
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
    /stt/ambient_listener/events         (String, Nemotron) - Live ambient fragments
    /stt/hotword_listener/detections     (String)           - All hotword detections
    /stt/audio_classifier/events         (String, latched)  - YAMNet audio classification history
    /cognition/prefetch                  (Empty)            - Signal cognition to prefetch prompt context
    /face/rgbled                         (Int32MultiArray)  - LED control
    /mobile_base/commands/sound          (Sound)            - Kobuki sounds
    /cognition/output                    (CognitionOutput)  - Feedback messages

  Subscribers:
    /tts/is_speaking             (Bool) - Global ear plug
    /stt/ambient_listener/enable (Bool) - Enable/disable ambient Whisper transcription
    /stt/hotword_listener/enable (String JSON list) - Set passive OpenWakeWord hotwords
    /stt/audio_classifier/enable (Bool) - Enable/disable MediaPipe YAMNet audio classification
"""
