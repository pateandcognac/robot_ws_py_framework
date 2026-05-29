#!/home/robot/robot_ws/.venv/bin/python3
# file: ~/robot_ws/src/logos_framework/scripts/cognition_node.py

import rospy
import sys
import os
import re
import json
import time
import random
import threading
import concurrent.futures
import base64
import io
import mimetypes
from pathlib import Path
from collections import deque
from enum import Enum
import PIL.Image

# Add the directory containing this script to sys.path so we can import local modules
# even when run via the ROS devel-space wrapper.
script_dir = Path(__file__).resolve().parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

# Google GenAI
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

# ROS Messages
from std_msgs.msg import String as StringMsg
from logos_framework.msg import CognitionInput, CognitionOutput

# Library Imports
from cognition_lib.config_manager import ConfigManager
from cognition_lib.io_manager import IOManager
from cognition_lib.context_manager import ContextManager

import sys

print("cog node sys.version:", sys.version)
print("cog node sys.executable:", sys.executable)
print("cog node PATH:", os.environ.get("PATH"))

class CognitionState(Enum):
    IDLE = 0
    GATHERING_CONTEXT = 1
    AWAITING_RESPONSE = 2

API_KEY_ENV_BY_PROFILE = {
    "free": "FREE_GEMINI_API_KEY",
    "paid": "PAID_GEMINI_API_KEY",
}

VALID_THINKING_LEVELS = {"minimal", "low", "medium", "high"}

THINKING_BUDGET_BY_LEVEL = {
    "minimal": 0,
    "low": 512,
    "medium": 4096,
    "high": 8192,
}

MODEL_PRESETS = [
    {"label": "Gemini 3.5 Flash", "model": "gemini-3.5-flash"},
    {"label": "Gemini 3.1 Pro Preview", "model": "gemini-3.1-pro-preview"},
    {"label": "Gemini 3 Flash Preview", "model": "gemini-3-flash-preview"},
    {"label": "Gemini 3.1 Flash-Lite Preview", "model": "gemini-3.1-flash-lite-preview"},
    {"label": "Robotics-ER 1.6 Preview", "model": "gemini-robotics-er-1.6-preview"},
    {"label": "Gemma 4 31B", "model": "gemma-4-31b-it"},
    {"label": "Gemma 4 26B MoE", "model": "gemma-4-26b-a4b-it"},
    {"label": "Gemini 2.5 Flash", "model": "gemini-2.5-flash"},
]

MEDIA_RESOLUTION_VALUES = [
    "MEDIA_RESOLUTION_UNSPECIFIED",
    "MEDIA_RESOLUTION_LOW",
    "MEDIA_RESOLUTION_MEDIUM",
    "MEDIA_RESOLUTION_HIGH",
]

GEMINI_ERROR_GUIDANCE = {
    (400, "INVALID_ARGUMENT"): {
        "summary": "The Gemini request was malformed.",
        "action": "Check the request body, model settings, API version, and any enabled features.",
        "retryable": False,
    },
    (400, "FAILED_PRECONDITION"): {
        "summary": "Gemini API access is not available for this key/project state.",
        "action": "Enable billing or use a project/key that can access Gemini from this region.",
        "retryable": False,
    },
    (403, "PERMISSION_DENIED"): {
        "summary": "The Gemini API key does not have the required permissions.",
        "action": "Check that the selected Gemini API profile can access the configured model.",
        "retryable": False,
    },
    (404, "NOT_FOUND"): {
        "summary": "Gemini could not find the requested resource.",
        "action": "Check the model name, API version, and any referenced files or resources.",
        "retryable": False,
    },
    (429, "RESOURCE_EXHAUSTED"): {
        "summary": "Gemini rate limit or quota was exceeded.",
        "action": "Wait before retrying, reduce request rate, or request more quota.",
        "retryable": True,
    },
    (500, "INTERNAL"): {
        "summary": "Gemini hit an internal backend error.",
        "action": "Retry after a short delay. If it persists, reduce context or try a lighter model.",
        "retryable": True,
    },
    (503, "UNAVAILABLE"): {
        "summary": "Gemini is temporarily overloaded or unavailable.",
        "action": "Retry after a short delay or temporarily switch to a lighter model.",
        "retryable": True,
    },
    (504, "DEADLINE_EXCEEDED"): {
        "summary": "Gemini could not finish before the request deadline.",
        "action": "Retry, reduce prompt/context size, or configure a larger client timeout.",
        "retryable": True,
    },
}

class CognitionNode:
    def __init__(self):
        rospy.init_node('cognition_node')
        workspace_param = rospy.get_param('~workspace_path')
        if not workspace_param:
            rospy.logfatal("Required parameter '~workspace_path' is not set! Shutting down.")
            return
        self.workspace_path = Path(workspace_param).expanduser()
        rospy.loginfo(f"Cognition Node: Initializing with workspace: {self.workspace_path}")

        self.config = ConfigManager(self.workspace_path)
        if not self.config.load_configs():
            rospy.signal_shutdown("Failed to load critical configurations.")
            return
        
        self.io = IOManager(self.workspace_path, self.config.framework)
        self.context = ContextManager(self.workspace_path, self.config.framework['context'])

        self.state = CognitionState.IDLE
        self.state_lock = threading.Lock()
        self.runtime_lock = threading.RLock()
        self.genai_client = None
        self.files_cache = {}
        self.runtime_status = {
            "last_error": "",
            "last_failover": "",
            "files_api_last_event": "",
            "last_paid_key_notice": "",
        }
        self.incoming_queue = deque()
        self.queue_lock = threading.Lock()
        self.last_received_system_hint = ""
        self.context_results = {}
        self.context_requests_pending = 0
        self.context_gathering_complete = threading.Event()
        self.api_delay_budget = 0.0
        self.last_api_call_time = time.time()
        
        # State tracking for feedback
        self.has_thought_started = False

        # Interrupt prefetch state
        self._prefetch_lock = threading.Lock()
        self._prefetch_in_progress = False
        self._prefetch_context_results = {}
        self._prefetch_context_pending = 0
        self._prefetch_context_event = threading.Event()
        self._prefetch_results = None        # dict hook_name->content once complete, or None
        self._prefetch_timestamp = 0.0
        PREFETCH_VALID_SECS = 60.0
        self._prefetch_valid_secs = PREFETCH_VALID_SECS

        self.output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)
        self.runtime_config_pub = rospy.Publisher('/cognition/runtime_config/state', StringMsg, queue_size=2, latch=True)
        self.input_sub = rospy.Subscriber('/cognition/input', CognitionInput, self._input_callback, queue_size=10)
        self.runtime_config_sub = rospy.Subscriber(
            '/cognition/runtime_config/set',
            StringMsg,
            self._runtime_config_callback,
            queue_size=5,
        )
        self.interrupt_sub = rospy.Subscriber('/python/interrupt', StringMsg, self._interrupt_callback, queue_size=5)
        self.ui_state_pub = rospy.Publisher('/cognition/ui_state', StringMsg, queue_size=2, latch=True)
        self.face_cmd_pub = rospy.Publisher('/face/emoji_command', StringMsg, queue_size=5)

        self.runtime_config = self._initial_runtime_config()
        try:
            self._configure_genai_client(self.runtime_config["api_profile"])
        except Exception as e:
            rospy.logfatal(f"Failed to configure Gemini API client: {e}. Shutting down.")
            rospy.signal_shutdown("Gemini API configuration failed.")
            return
        self._publish_runtime_config_state()

        self.processing_timer = rospy.Timer(rospy.Duration(0.10), self._process_queue)
        rospy.loginfo("Cognition Node: Ready and waiting for input.")

    def _parse_bool(self, value, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _normalize_api_profile(self, value, default="free"):
        profile = str(value or default).strip().lower()
        if profile not in API_KEY_ENV_BY_PROFILE:
            rospy.logwarn(f"Unknown Gemini API profile '{profile}', falling back to '{default}'.")
            return default
        return profile

    def _normalize_thinking_level(self, value, default="low"):
        level = str(value or default).strip().lower()
        if level not in VALID_THINKING_LEVELS:
            rospy.logwarn(f"Unknown thinking level '{level}', falling back to '{default}'.")
            return default
        return level

    def _normalize_media_resolution(self, value, default="MEDIA_RESOLUTION_MEDIUM"):
        media_resolution = str(value or default).strip().upper()
        if media_resolution in {"UNSPECIFIED", "LOW", "MEDIUM", "HIGH"}:
            media_resolution = f"MEDIA_RESOLUTION_{media_resolution}"
        if media_resolution not in MEDIA_RESOLUTION_VALUES:
            rospy.logwarn(f"Unknown media resolution '{media_resolution}', falling back to '{default}'.")
            return default
        return media_resolution

    def _initial_runtime_config(self):
        model_cfg = self.config.framework.get('main_model', {})
        tk_cfg = model_cfg.get('thinking_config', {})

        api_profile = self._normalize_api_profile(rospy.get_param('~api_profile', 'free'))
        fallback_api_profile = self._normalize_api_profile(
            rospy.get_param('~fallback_api_profile', 'paid'),
            default='paid',
        )
        model = str(rospy.get_param('~model', '') or model_cfg.get('model', 'gemini-3.5-flash')).strip()
        thinking_level = self._normalize_thinking_level(
            rospy.get_param('~thinking_level', tk_cfg.get('thinking_level', 'low'))
        )
        media_resolution = self._normalize_media_resolution(
            rospy.get_param('~media_resolution', model_cfg.get('media_resolution', 'MEDIA_RESOLUTION_MEDIUM'))
        )

        return {
            "api_profile": api_profile,
            "fallback_api_profile": fallback_api_profile,
            "key_failover": self._parse_bool(rospy.get_param('~key_failover', True), default=True),
            "model": model,
            "thinking_level": thinking_level,
            "media_resolution": media_resolution,
            "use_files_api": self._parse_bool(rospy.get_param('~use_files_api', True), default=True),
        }

    def _safe_runtime_config(self):
        state = dict(self.runtime_config)
        state.update({
            "api_profiles": list(API_KEY_ENV_BY_PROFILE.keys()),
            "api_key_available": {
                profile: bool(os.environ.get(env_name))
                for profile, env_name in API_KEY_ENV_BY_PROFILE.items()
            },
            "model_presets": MODEL_PRESETS,
            "thinking_levels": sorted(VALID_THINKING_LEVELS),
            "media_resolutions": MEDIA_RESOLUTION_VALUES,
            "files_cache_entries": len(self.files_cache),
            "status": dict(self.runtime_status),
        })
        return state

    def _publish_runtime_config_state(self):
        if not getattr(self, "runtime_config_pub", None):
            return
        try:
            with self.runtime_lock:
                payload = json.dumps(self._safe_runtime_config())
            self.runtime_config_pub.publish(StringMsg(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish runtime Gemini config state: {e}")

    def _runtime_config_callback(self, msg: StringMsg):
        try:
            requested = json.loads(msg.data)
        except json.JSONDecodeError as e:
            rospy.logwarn(f"Ignoring malformed runtime config update: {e}")
            return

        with self.runtime_lock:
            current_profile = self.runtime_config["api_profile"]

            if "api_profile" in requested:
                self.runtime_config["api_profile"] = self._normalize_api_profile(requested["api_profile"])
            if "fallback_api_profile" in requested:
                self.runtime_config["fallback_api_profile"] = self._normalize_api_profile(
                    requested["fallback_api_profile"],
                    default='paid',
                )
            if "key_failover" in requested:
                self.runtime_config["key_failover"] = self._parse_bool(requested["key_failover"], default=True)
            if "model" in requested:
                model = str(requested["model"]).strip()
                if model:
                    self.runtime_config["model"] = model
            if "thinking_level" in requested:
                self.runtime_config["thinking_level"] = self._normalize_thinking_level(requested["thinking_level"])
            if "media_resolution" in requested:
                self.runtime_config["media_resolution"] = self._normalize_media_resolution(requested["media_resolution"])
            if "use_files_api" in requested:
                self.runtime_config["use_files_api"] = self._parse_bool(requested["use_files_api"], default=True)

            new_profile = self.runtime_config["api_profile"]

        if new_profile != current_profile:
            try:
                self._configure_genai_client(new_profile, publish_state=(new_profile != "paid"))
                if new_profile == "paid":
                    self._announce_paid_key_switch(current_profile, "manual runtime switch")
            except Exception as e:
                with self.runtime_lock:
                    self.runtime_config["api_profile"] = current_profile
                    self.runtime_status["last_error"] = str(e)
                rospy.logwarn(f"Could not switch Gemini API profile to '{new_profile}': {e}")
                try:
                    self._configure_genai_client(current_profile)
                except Exception as restore_error:
                    rospy.logerr(f"Could not restore Gemini API profile '{current_profile}': {restore_error}")
        else:
            self._publish_runtime_config_state()

    def _configure_genai_client(self, api_profile, publish_state=True):
        profile = self._normalize_api_profile(api_profile)
        env_name = API_KEY_ENV_BY_PROFILE[profile]
        api_key = os.environ.get(env_name)
        if not api_key:
            raise ValueError(f"{env_name} environment variable not set for '{profile}' profile.")

        client = genai.Client(api_key=api_key)
        with self.runtime_lock:
            self.genai_client = client
            self.runtime_config["api_profile"] = profile
            self.runtime_status["last_error"] = ""
        rospy.loginfo(f"Gemini API client configured with '{profile}' profile ({env_name}).")
        if publish_state:
            self._publish_runtime_config_state()

    def _announce_paid_key_switch(self, previous_profile, reason):
        if previous_profile == "paid":
            return

        notice = f"Paid Gemini API key is now active ({reason})."
        with self.runtime_lock:
            self.runtime_status["last_paid_key_notice"] = notice
        rospy.logwarn(notice)
        self._send_feedback("api_paid_key", notice, "api_call", "bright_red", "mini")
        self._publish_runtime_config_state()

    def _runtime_snapshot(self):
        with self.runtime_lock:
            return dict(self.runtime_config)

    def _publish_face_feedback(self, emoji, duration=3.0):
        """Publish emoji-driven face feedback for cognition state changes."""
        payload = json.dumps({"emoji": emoji, "duration": duration})
        try:
            self.face_cmd_pub.publish(StringMsg(data=payload))
        except Exception as e:
            rospy.logwarn(f"Failed to publish face feedback: {e}")

    def _face_feedback_for_header(self, header):
        feedback_map = {
            "got_input": ("📥", 2.0),
            "calling_hooks": ("🔎", 3.0),
            "hook_timeout": ("⚠️", 3.0),
            "api_call": ("🤖", 3.0),
            "thinking": ("🤔", 3.0),
            "api_error": ("😵", 4.0),
            "api_retry": ("🔁", 3.0),
            "api_paid_key": ("💳", 5.0),
        }
        return feedback_map.get(header)

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
            face_feedback = self._face_feedback_for_header(header)
            if face_feedback:
                self._publish_face_feedback(*face_feedback)
        except Exception as e:
            rospy.logwarn(f"Failed to publish feedback: {e}")

    def _gemini_error_info(self, exc: Exception) -> dict:
        """Normalize Gemini/backend/network errors into UI-friendly guidance."""
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None)
        message = getattr(exc, "message", None)

        if isinstance(exc, genai_errors.APIError):
            details = getattr(exc, "details", None)
            if isinstance(details, dict):
                error_details = details.get("error", details)
                code = code or error_details.get("code")
                status = status or error_details.get("status")
                message = message or error_details.get("message")

        if isinstance(code, str) and code.isdigit():
            code = int(code)

        guidance = GEMINI_ERROR_GUIDANCE.get((code, status))
        if guidance is None:
            guidance = next(
                (
                    item
                    for (known_code, known_status), item in GEMINI_ERROR_GUIDANCE.items()
                    if known_code == code and (not status or known_status == status)
                ),
                None
            )

        if guidance is None:
            if isinstance(code, int) and 500 <= code < 600:
                guidance = {
                    "summary": "Gemini returned a backend server error.",
                    "action": "Retry after a short delay. If it persists, reduce context or try another model.",
                    "retryable": True,
                }
            elif isinstance(code, int) and 400 <= code < 500:
                guidance = {
                    "summary": "Gemini rejected the request.",
                    "action": "Check API key permissions, model name, request body, and configured API features.",
                    "retryable": False,
                }
            elif self._looks_like_retryable_transport_error(exc):
                guidance = {
                    "summary": "The Gemini request hit a transport or timeout error.",
                    "action": "Retry after a short delay. Check network connectivity if this repeats.",
                    "retryable": True,
                }
            else:
                guidance = {
                    "summary": "The Gemini request failed unexpectedly.",
                    "action": "Check ROS logs for the full exception details.",
                    "retryable": False,
                }

        return {
            "code": code,
            "status": status,
            "message": message or str(exc),
            "summary": guidance["summary"],
            "action": guidance["action"],
            "retryable": guidance["retryable"],
            "exception_type": type(exc).__name__,
        }

    def _looks_like_retryable_transport_error(self, exc: Exception) -> bool:
        retryable_names = (
            "ConnectError",
            "ConnectionError",
            "ConnectTimeout",
            "NetworkError",
            "ReadError",
            "ReadTimeout",
            "RemoteProtocolError",
            "TimeoutException",
            "WriteError",
            "WriteTimeout",
        )
        return any(cls.__name__ in retryable_names for cls in type(exc).mro())

    def _format_gemini_error_body(self, info: dict, attempt: int, max_attempts: int) -> str:
        error_id_parts = []
        if info.get("code"):
            error_id_parts.append(str(info["code"]))
        if info.get("status"):
            error_id_parts.append(str(info["status"]))
        error_id = " ".join(error_id_parts) or info["exception_type"]

        lines = [
            f"Attempt {attempt}/{max_attempts}: {error_id}",
            info["summary"],
            info["action"],
        ]
        if info.get("message"):
            lines.append(f"Message: {info['message']}")
        return "\n".join(lines)

    def _is_quota_or_rate_limit_error(self, info: dict) -> bool:
        if info.get("code") == 429 or info.get("status") == "RESOURCE_EXHAUSTED":
            return True
        message = str(info.get("message", "")).lower()
        return any(term in message for term in ("quota", "rate limit", "resource exhausted"))

    def _gemini_retry_delay(self, retry_cfg: dict, attempt_index: int) -> float:
        backoff_factor = retry_cfg.get('backoff_factor_s', 2)
        max_delay = retry_cfg.get('max_delay_s', 30)
        jitter = retry_cfg.get('jitter_s', 1)
        delay = (backoff_factor * (2 ** attempt_index)) + random.uniform(0, jitter)
        return min(delay, max_delay)

    def _model_uses_thinking_level(self, model_name: str) -> bool:
        return str(model_name or "").startswith("gemini-3")

    def _model_uses_thinking_budget(self, model_name: str) -> bool:
        name = str(model_name or "")
        return name.startswith("gemini-2.5") or name.startswith("gemini-robotics-er-")

    def _build_thinking_config(self, model_name: str, runtime_cfg: dict, model_cfg: dict):
        tk_cfg = model_cfg.get('thinking_config', {})
        include_thoughts = tk_cfg.get('include_thoughts', True)
        thinking_level = runtime_cfg.get("thinking_level", "low")

        if self._model_uses_thinking_level(model_name):
            try:
                return types.ThinkingConfig(
                    thinking_level=thinking_level,
                    include_thoughts=include_thoughts,
                )
            except Exception as e:
                # Older google-genai SDKs do not know thinking_level yet.
                rospy.logwarn(f"SDK rejected thinking_level; falling back to thinking_budget: {e}")

        if self._model_uses_thinking_budget(model_name):
            return types.ThinkingConfig(
                thinking_budget=THINKING_BUDGET_BY_LEVEL.get(thinking_level, 512),
                include_thoughts=include_thoughts,
            )

        return None

    def _build_gemini_config(self, model_cfg: dict, runtime_cfg: dict):
        safety_settings = [
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
        ]

        model_name = runtime_cfg.get('model') or model_cfg.get('model')
        media_res_str = runtime_cfg.get('media_resolution') or model_cfg.get('media_resolution', 'MEDIA_RESOLUTION_MEDIUM')
        config_kwargs = dict(
            safety_settings=safety_settings,
            temperature=model_cfg.get('temperature', 1.0),
            stop_sequences=model_cfg.get('stop_sequences', []),
            max_output_tokens=model_cfg.get('max_output_tokens', 8192),
            media_resolution=getattr(types.MediaResolution, media_res_str, types.MediaResolution.MEDIA_RESOLUTION_UNSPECIFIED),
        )
        thinking_config = self._build_thinking_config(model_name, runtime_cfg, model_cfg)
        if thinking_config is not None:
            config_kwargs["thinking_config"] = thinking_config

        return types.GenerateContentConfig(**config_kwargs)

    def _files_cache_key(self, image_path: Path, api_profile: str):
        stat = image_path.stat()
        return (
            api_profile,
            str(image_path.resolve()),
            stat.st_size,
            getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)),
        )

    def _inline_image_part(self, image_path: Path):
        img = PIL.Image.open(image_path)
        rospy.loginfo(f"Embedding image inline for LLM: {image_path}")
        return img

    def _upload_or_reuse_file_part(self, image_path: Path, runtime_cfg: dict):
        api_profile = runtime_cfg.get("api_profile", "free")
        cache_key = self._files_cache_key(image_path, api_profile)
        cached_part = self.files_cache.get(cache_key)
        if cached_part is not None:
            self.runtime_status["files_api_last_event"] = f"reused {image_path.name}"
            return cached_part

        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        try:
            with self.runtime_lock:
                client = self.genai_client
            _upload_t0 = time.time()
            uploaded = client.files.upload(file=str(image_path))
            _upload_dur = time.time() - _upload_t0
            file_uri = getattr(uploaded, "uri", None)
            uploaded_mime_type = getattr(uploaded, "mime_type", None) or mime_type
            if not file_uri:
                raise ValueError("Files API upload returned no URI.")

            part = types.Part.from_uri(file_uri=file_uri, mime_type=uploaded_mime_type)
            self.files_cache[cache_key] = part
            self.runtime_status["files_api_last_event"] = f"uploaded {image_path.name}"
            rospy.loginfo(f"[Timing] Files API upload {image_path.name}: {_upload_dur:.3f}s")
            self._publish_runtime_config_state()
            return part
        except Exception as e:
            self.runtime_status["files_api_last_event"] = f"fallback inline for {image_path.name}"
            rospy.logwarn(f"Files API upload failed for {image_path}; falling back to inline image part: {e}")
            self._publish_runtime_config_state()
            return self._inline_image_part(image_path)

    def _image_part_for_path(self, image_path: Path, runtime_cfg: dict):
        if runtime_cfg.get("use_files_api", True):
            return self._upload_or_reuse_file_part(image_path, runtime_cfg)
        return self._inline_image_part(image_path)

    def _maybe_failover_api_key(self, info: dict, published_answer_text: bool) -> bool:
        with self.runtime_lock:
            runtime_cfg = dict(self.runtime_config)

        if published_answer_text:
            return False
        if not runtime_cfg.get("key_failover", True):
            return False
        if not self._is_quota_or_rate_limit_error(info):
            return False

        current_profile = runtime_cfg.get("api_profile", "free")
        fallback_profile = runtime_cfg.get("fallback_api_profile", "paid")
        if current_profile == fallback_profile:
            return False
        if current_profile != "free":
            return False

        try:
            self._configure_genai_client(fallback_profile, publish_state=(fallback_profile != "paid"))
        except Exception as e:
            with self.runtime_lock:
                self.runtime_status["last_error"] = f"key failover failed: {e}"
            rospy.logwarn(f"Gemini key failover to '{fallback_profile}' failed: {e}")
            self._publish_runtime_config_state()
            return False

        failover_msg = f"Switched Gemini API profile from {current_profile} to {fallback_profile} after quota/rate-limit error."
        with self.runtime_lock:
            self.runtime_status["last_failover"] = failover_msg
        rospy.logwarn(failover_msg)
        if fallback_profile == "paid":
            self._announce_paid_key_switch(current_profile, "automatic quota failover")
        self._send_feedback("api_retry", failover_msg, "api_call", "bright_yellow", "mini")
        self._publish_runtime_config_state()
        return True

    def _input_callback(self, msg: CognitionInput):
        if msg.type == 'context' and self.state in [CognitionState.GATHERING_CONTEXT, CognitionState.AWAITING_RESPONSE]:
            hook_name = msg.filename
            if hook_name:
                self.context_results[hook_name] = msg.content
                self.context_requests_pending -= 1
                if self.context_requests_pending <= 0:
                    self.context_gathering_complete.set()
            else:
                rospy.logwarn(f"Received context message without a filename. Cannot process.")
            return

        if msg.type == 'context' and self._prefetch_in_progress:
            hook_name = msg.filename
            if hook_name:
                self._prefetch_context_results[hook_name] = msg.content
                self._prefetch_context_pending -= 1
                if self._prefetch_context_pending <= 0:
                    self._prefetch_context_event.set()
            return
        
        # Feedback: Got Input (ignore context inputs)
        # Simple heuristic: if it's not type 'context', it's a meaningful input trigger
        if msg.type != 'context':
            if msg.loop_cognition == True:
                color="bright_green"
                self._send_feedback("Looping input", "", "got_input", color, "slant")
            else:
                color="bright_red"
            self._send_feedback("Queued input", "", "got_input", color, "slant")

        with self.queue_lock:
            self.incoming_queue.append(msg)

    def _process_queue(self, event=None):
        if self.state == CognitionState.AWAITING_RESPONSE:
            return

        with self.queue_lock:
            if not self.incoming_queue:
                return
            batch = list(self.incoming_queue); self.incoming_queue.clear()
        
        rospy.loginfo(f"Processing batch of {len(batch)} messages in state {self.state.name}.")
        should_start_cycle = False
        should_call_gemini = False
        should_run_hooks = False
        skip_gemini_types = {'debug', 'codex_tool'}
        for msg in batch:
            filename = getattr(msg, 'filename', None)
            self.io.append_message(msg_type=msg.type, content=msg.content, filename=filename)

            if msg.type == 'hook_refresh':
                should_start_cycle = True
                should_run_hooks = True
            elif msg.loop_cognition:
                should_start_cycle = True
                should_run_hooks = True
                if msg.type not in skip_gemini_types:
                    should_call_gemini = True
            
            self.last_received_system_hint = msg.system_hint or ""

        with self.state_lock:
            if should_start_cycle and self.state == CognitionState.IDLE:
                self.state = CognitionState.GATHERING_CONTEXT
                debug_only = not should_call_gemini
                rospy.loginfo("State transition to GATHERING_CONTEXT. Starting cognition cycle.")
                threading.Thread(
                    target=self._initiate_cognition_cycle,
                    args=(debug_only, should_run_hooks),
                ).start()

    def _publish_ui_state(self, header_str, io_buffer_str, footer_str):
            def embed_image_url(match):
                relative_path = match.group(2) 
                # Simply point to a local web server route we will create in the UI node!
                return f'{match.group(1)}{match.group(3)}</file>\n<img src="/workspace/{relative_path}">'

            # Use a regex that captures the whole tag block for consistency
            image_pattern = re.compile(r'(<file\s+path="([^"]+)"[^>]*>)(.*?)(</file>)', re.DOTALL)

            header_str_with_images = re.sub(image_pattern, embed_image_url, header_str)
            io_buffer_str_with_images = re.sub(image_pattern, embed_image_url, io_buffer_str)
            footer_str_with_images = re.sub(image_pattern, embed_image_url, footer_str)

            ui_state = {
                "header": header_str_with_images,
                "io_buffer": io_buffer_str_with_images,
                "footer": footer_str_with_images
            }
            
            try:
                json_payload = json.dumps(ui_state)
                self.ui_state_pub.publish(StringMsg(data=json_payload))
                rospy.loginfo("Published UI state update (Using file URLs).")
            except Exception as e:
                rospy.logerr(f"Failed to create or publish UI state: {e}")

    def _format_hook_name_list(self, hook_names: list) -> str:
            """Formats hook names for the empty-output summary line."""
            if not hook_names:
                return ""
            if len(hook_names) == 1:
                return hook_names[0]
            if len(hook_names) == 2:
                return f"{hook_names[0]} and {hook_names[1]}"
            return f"{', '.join(hook_names[:-1])}, and {hook_names[-1]}"

    def _build_hook_section_content(self, items: list, divisor: int) -> tuple[str, int]:
            """Builds hook section content and summarizes hooks that returned only whitespace."""
            non_empty_chunks = []
            empty_hook_names = []
            total_tokens = 0

            for item in items:
                hook_name = item['config'].get('name', 'unnamed')
                content = item['content']
                total_tokens += len(content) // divisor

                if content.strip():
                    non_empty_chunks.append(f'<{hook_name}>\n{content}\n</{hook_name}>')
                else:
                    empty_hook_names.append(hook_name)

            section_chunks = []
            if empty_hook_names:
                hook_label = "Hook" if len(empty_hook_names) == 1 else "Hooks"
                section_chunks.append(
                    f"# {hook_label} {self._format_hook_name_list(empty_hook_names)} produced no output."
                )
            section_chunks.extend(non_empty_chunks)

            return "\n\n".join(section_chunks), total_tokens

    def _format_prompt_section(self, section_type: str, items: list) -> str:
            """Helper to format a section (header, footer, io_buffer) for the prompt."""
            if not items:
                return ""

            cfg = self.config.framework['context']
            divisor = cfg.get('token_estimation_divisor', 5)
            section_name = ""
            content_str = ""
            total_tokens = 0

            if section_type in ['header', 'footer']:
                section_name = cfg.get(f'{section_type}_name', section_type)
                show_stats = cfg.get(f'show_{section_type}_stats', False)
                content_str, total_tokens = self._build_hook_section_content(items, divisor)
                
                if show_stats:
                    return f'<{section_name} hooks="{len(items)}" tokens="{total_tokens}">\n{content_str.strip()}\n</{section_name}>'
                else:
                    return f'<{section_name}>\n{content_str.strip()}\n</{section_name}>'

            elif section_type == 'io_buffer':
                section_name = cfg.get('io_buffer_name', 'io_buffer')
                show_stats = cfg.get('show_io_buffer_stats', False)
                show_cell_stats = cfg.get('show_io_cell_stats', False)

                for i, msg in enumerate(items):
                    msg_type, msg_id = msg.get("type", "unknown"), msg.get("id", "no-id")
                    content, token_count = msg.get("content", ""), msg.get("token_count", 0)
                    total_tokens += token_count
                    if msg_type == 'system':
                        content_str += f'<!-- {msg_type}: {content} -->\n\n'
                    else:
                        if show_cell_stats:
                            content_str += f'<{msg_type} cell="{i}" id="{msg_id}" tokens="{token_count}">\n{content}\n</{msg_type}>\n\n'
                        else:
                            content_str += f'<{msg_type} cell="{i}">\n{content}\n</{msg_type}>\n\n'
                
                if show_stats:
                    return f'<{section_name} cells="{len(items)}" tokens="{total_tokens}">\n{content_str.strip()}\n</{section_name}>'
                else:
                    return f'<{section_name}>\n{content_str.strip()}\n</{section_name}>'
            
            return ""

    def _count_loops_since_last_human_input(self, io_buffer_messages: list) -> int:
            """Count `me` messages since the last human turn."""
            human_types = {'human', 'human_stt'}
            ignored_types = {'context', 'system'}

            loops = 0
            found_human_turn = False
            for msg in reversed(io_buffer_messages):
                msg_type = msg.get("type", "")
                if msg_type in human_types:
                    found_human_turn = True
                    break
                if msg_type in ignored_types:
                    continue
                if msg_type != 'me':
                    continue
                loops += 1

            return loops if found_human_turn else 0

    def _build_loop_guard_system_hint(self, io_buffer_messages: list) -> str:
            """Build an escalating system hint to discourage runaway autonomous loops."""
            loops_since_last_human = self._count_loops_since_last_human_input(io_buffer_messages)
            if loops_since_last_human < 2:
                return ""

            capped_level = min(loops_since_last_human, 7)
            escalation_messages = {
                2: "Do you need to pause and give someone a chance to reply? Or insert a natural break in your actions?",
                3: "Three loops with no input from a human? This may be a good point to pause to let someone get a word in!",
                4: "REMINDER: After four loops, Logos, your shared reality is becoming one-sided!",
                5: "IMPORTANT: If you are not in the middle of a specific, multi-step task, you might be overwhelming a human interlocutor with tokens and action faster than they can process. Consider pausing for their benefit.",
                6: "IMPORTANT: Only keep the loop going if you have a very clear autonomous task!",
                7: "IMPORTANT: Logos should consider setting `loop_cognition = False` — IF there isn't an autonomous task that you're not actively engaged with.",
            }
            hint_text = escalation_messages[capped_level]
            return f"\n<!-- system: loops_since_last_human_input = {loops_since_last_human} | {hint_text} -->"

    def _build_response_system_hint(self, last_msg_type: str, last_cell_num: int) -> str:
            default_system_hint = (
                "\n<!-- system: Logos, please prepare your response to last palimpsest entry: "
                f"`<{last_msg_type} cell=\"{last_cell_num}\">`. Wrap your output in <me><py> tags "
                "for proper parsing. -->"
            )
            if self.last_received_system_hint:
                return self.last_received_system_hint + "\n" + default_system_hint
            return default_system_hint

    def _append_system_hints_to_footer(self, footer_str: str, system_hints: list) -> str:
            visible_hints = [hint.strip() for hint in system_hints if hint and hint.strip()]
            if not visible_hints:
                return footer_str

            hints_block = "\n".join(visible_hints)
            if footer_str and footer_str.strip():
                return footer_str.rstrip() + "\n\n" + hints_block
            return hints_block

    def _parallel_prefetch_images(self, text_parts: list, runtime_cfg: dict):
        """Upload all uncached images referenced in text_parts in parallel, warming files_cache."""
        if not runtime_cfg.get("use_files_api", True):
            return
        placeholder_pattern = re.compile(r'{--IMAGE_PATH:([^}]+)--}')
        paths_to_upload = []
        seen = set()
        for text in text_parts:
            for m in placeholder_pattern.finditer(text):
                p = self.workspace_path / m.group(1)
                if p not in seen:
                    seen.add(p)
                    api_profile = runtime_cfg.get("api_profile", "free")
                    try:
                        cache_key = self._files_cache_key(p, api_profile)
                    except Exception:
                        continue
                    if cache_key not in self.files_cache:
                        paths_to_upload.append(p)

        if not paths_to_upload:
            return

        _par_t0 = time.time()
        rospy.loginfo(f"[Timing] Uploading {len(paths_to_upload)} image(s) in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._upload_or_reuse_file_part, p, runtime_cfg): p
                       for p in paths_to_upload}
            for fut in concurrent.futures.as_completed(futures):
                p = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    rospy.logwarn(f"Parallel upload failed for {p}: {e}")
        rospy.loginfo(f"[Timing] Parallel image upload batch complete: {time.time() - _par_t0:.3f}s for {len(paths_to_upload)} file(s)")

    def _construct_prompt_and_images(self, header_hooks_data, footer_hooks_data, runtime_cfg, include_api_parts=True):
            """
            Builds the final prompt for the LLM and the display strings for the UI.
            This refactored method correctly INLINES image content and omission notes.
            """
            _build_t0 = time.time()
            file_tag_pattern = re.compile(r'(<file\s+path="([^"]+)"[^>]*>)(.*?)(</file>)', re.DOTALL)

            # --- Build the initial text content using the new helper ---
            header_str = self._format_prompt_section('header', header_hooks_data)
            io_buffer_messages = self.io.read_buffer()
            last_cell_num = max(len(io_buffer_messages) - 1, 0)
            last_msg_type = io_buffer_messages[-1].get("type", "msg_type") if io_buffer_messages else "msg_type"
            response_system_hint = self._build_response_system_hint(last_msg_type, last_cell_num)
            loop_guard_system_hint = self._build_loop_guard_system_hint(io_buffer_messages)
            io_buffer_str = self._format_prompt_section('io_buffer', io_buffer_messages)
            footer_str = self._format_prompt_section('footer', footer_hooks_data)
            
            # --- PASS 1: Process the IO Buffer for image limiting and create final text ---
            my_config_limit = self.config.my_config.get('io_buffer', {}).get('max_io_buffer_media', 8)
            global_limit = self.config.framework.get('agent_settings', {}).get('global_max_io_buffer_media', 32)
            max_images_to_process = min(my_config_limit, global_limit)
            rospy.loginfo(f"Applying image limit: max {max_images_to_process} (my_config: {my_config_limit}, global: {global_limit})")
            
            all_io_buffer_matches = list(file_tag_pattern.finditer(io_buffer_str))
            matches_to_keep = all_io_buffer_matches[-max_images_to_process:]
            tags_to_keep = {match.group(0) for match in matches_to_keep}
            rospy.loginfo(f"Keeping {len(tags_to_keep)} images out of {len(all_io_buffer_matches)} found in IO buffer.")


            def selective_replacer(match):
                full_tag_text = match.group(0)
                optional_text = match.group(3).strip()
                separator = '\n' if optional_text else ''
                if full_tag_text in tags_to_keep:
                    return f'{match.group(1)}{optional_text}{separator}{{--IMAGE_PATH:{match.group(2)}--}}{match.group(4)}'
                else:
                    return f'{match.group(1)}{optional_text}{separator}(Image omitted per my_config.yaml){match.group(4)}'

            def unlimited_replacer(match):
                optional_text = match.group(3).strip()
                separator = '\n' if optional_text else ''
                return f'{match.group(1)}{optional_text}{separator}{{--IMAGE_PATH:{match.group(2)}--}}{match.group(4)}'

            processed_io_buffer_str = file_tag_pattern.sub(selective_replacer, io_buffer_str)
            processed_header_str = file_tag_pattern.sub(unlimited_replacer, header_str)
            processed_footer_str = file_tag_pattern.sub(unlimited_replacer, footer_str)
            ui_footer_str = self._append_system_hints_to_footer(
                processed_footer_str,
                [response_system_hint, loop_guard_system_hint],
            )
            
            self._publish_ui_state(processed_header_str, processed_io_buffer_str, ui_footer_str)

            if not include_api_parts:
                rospy.loginfo(f"[Timing] Prompt build (UI only): {time.time() - _build_t0:.3f}s")
                return []

            # --- PASS 2: Assemble final prompt list for the LLM API ---
            # Pre-upload all referenced images in parallel before sequential assembly.
            self._parallel_prefetch_images(
                [processed_header_str, processed_io_buffer_str, processed_footer_str],
                runtime_cfg,
            )

            final_contents = []
            placeholder_pattern = re.compile(r'{--IMAGE_PATH:([^}]+)--}')

            def parse_and_append_parts(text_content):
                last_index = 0
                for match in placeholder_pattern.finditer(text_content):
                    final_contents.append(text_content[last_index:match.start()])
                    image_path_str = match.group(1)
                    image_path = self.workspace_path / image_path_str
                    try:
                        final_contents.append(self._image_part_for_path(image_path, runtime_cfg))
                    except Exception as e:
                        rospy.logerr(f"Failed to load image {image_path}: {e}")
                        final_contents.append(f"[ERROR: Could not load image at {image_path}]")
                    last_index = match.end()
                final_contents.append(text_content[last_index:])

            final_contents.append(self.config.system_prompt)
            parse_and_append_parts(processed_header_str)
            parse_and_append_parts(processed_io_buffer_str)
            parse_and_append_parts(processed_footer_str)
            final_contents.append(response_system_hint)
            if loop_guard_system_hint:
                final_contents.append(loop_guard_system_hint)

            rospy.loginfo(f"[Timing] Prompt build total: {time.time() - _build_t0:.3f}s")
            return final_contents

    def _interrupt_callback(self, msg: StringMsg):
        try:
            data = json.loads(msg.data) if msg.data.strip() else {}
        except Exception:
            data = {}
        reason = data.get("reason", "unspecified")
        with self.state_lock:
            current_state = self.state
        if current_state != CognitionState.IDLE:
            rospy.loginfo(f"[Prefetch] /python/interrupt received (reason: {reason}) but state is {current_state.name}; ignoring.")
            return
        with self._prefetch_lock:
            if self._prefetch_in_progress:
                rospy.loginfo(f"[Prefetch] /python/interrupt received but prefetch already in progress; ignoring.")
                return
        rospy.loginfo(f"[Prefetch] /python/interrupt received (reason: {reason}). Spawning prefetch thread.")
        threading.Thread(target=self._run_prefetch, daemon=True).start()

    def _run_prefetch(self):
        time.sleep(0.1)
        with self.state_lock:
            if self.state != CognitionState.IDLE:
                rospy.loginfo("[Prefetch] State changed before prefetch could start; aborting.")
                return
        with self._prefetch_lock:
            if self._prefetch_in_progress:
                return
            self._prefetch_in_progress = True
            self._prefetch_context_results.clear()
            self._prefetch_context_event.clear()

        prefetch_t0 = time.time()
        rospy.loginfo("[Prefetch] Starting hook prefetch...")
        try:
            header_to_run, footer_to_run = self.context.get_hooks_to_execute()
            hooks_to_run = header_to_run + footer_to_run
            self._prefetch_context_pending = len(hooks_to_run)

            if not hooks_to_run:
                rospy.loginfo("[Prefetch] No hooks configured; nothing to prefetch.")
                return

            for hook in hooks_to_run:
                out_msg = CognitionOutput(
                    type='context',
                    content=f"<py>{hook['code']}</py>",
                    filename=hook['name']
                )
                self.output_pub.publish(out_msg)

            completed = self._prefetch_context_event.wait(timeout=30.0)
            if not completed:
                rospy.logwarn("[Prefetch] Timed out waiting for hook results; storing partial results.")

            hook_results = dict(self._prefetch_context_results)
            rospy.loginfo(f"[Prefetch] Hooks done in {time.time() - prefetch_t0:.3f}s ({len(hook_results)}/{len(hooks_to_run)} results).")

            # Build hook data structures to pre-upload referenced images via Files API.
            runtime_cfg = self._runtime_snapshot()
            if runtime_cfg.get("use_files_api", True):
                header_data = [{'config': s, 'content': hook_results[s['name']]}
                                for s in header_to_run if s['name'] in hook_results]
                footer_data = [{'config': s, 'content': hook_results[s['name']]}
                                for s in footer_to_run if s['name'] in hook_results]
                # include_api_parts=False triggers parallel image upload without a Gemini call.
                self._construct_prompt_and_images(header_data, footer_data, runtime_cfg, include_api_parts=False)

            with self._prefetch_lock:
                self._prefetch_results = hook_results
                self._prefetch_timestamp = time.time()

            rospy.loginfo(f"[Prefetch] Complete in {time.time() - prefetch_t0:.3f}s. Context valid for {self._prefetch_valid_secs:.0f}s.")
        except Exception as e:
            rospy.logwarn(f"[Prefetch] Error during prefetch: {e}")
        finally:
            with self._prefetch_lock:
                self._prefetch_in_progress = False

    def _initiate_cognition_cycle(self, debug_only=False, run_hooks=True):
            try:
                cycle_start_t = time.time()
                if debug_only and run_hooks:
                    cycle_kind = "Hook Refresh Cycle"
                elif debug_only:
                    cycle_kind = "UI Refresh Cycle"
                else:
                    cycle_kind = "Cognition Cycle"
                rospy.loginfo(f"--- Starting {cycle_kind} ---")
                # Reset flags
                self.has_thought_started = False
                self.context_results.clear()
                self.context_gathering_complete.clear()

                if run_hooks:
                    header_to_run, footer_to_run = self.context.get_hooks_to_execute()
                else:
                    header_to_run, footer_to_run = [], []
                hooks_to_run = header_to_run + footer_to_run
                self.context_requests_pending = len(hooks_to_run)

                # --- Check for valid interrupt prefetch ---
                hook_phase_used_prefetch = False
                if run_hooks and hooks_to_run:
                    with self._prefetch_lock:
                        prefetch_age = time.time() - self._prefetch_timestamp
                        if self._prefetch_results is not None and prefetch_age < self._prefetch_valid_secs:
                            rospy.loginfo(f"[Prefetch] Using prefetched hook context ({prefetch_age:.1f}s old). Skipping hook dispatch.")
                            self.context_results = dict(self._prefetch_results)
                            self._prefetch_results = None
                            hook_phase_used_prefetch = True
                        else:
                            if self._prefetch_results is not None:
                                rospy.loginfo(f"[Prefetch] Prefetched context expired ({prefetch_age:.1f}s old). Re-running hooks.")
                            self._prefetch_results = None

                # Feedback: Calling Hooks
                hook_phase_t0 = time.time()
                if self.context_requests_pending > 0 and not hook_phase_used_prefetch:
                    hook_names = ", ".join([h['name'] for h in hooks_to_run])
                    rospy.loginfo(f"Requesting {self.context_requests_pending} Cognitive Hooks...")
                    self._send_feedback("Calling hooks", hook_names, "calling_hooks", "bright_yellow", "digital")

                    for hook in hooks_to_run:
                        out_msg = CognitionOutput(
                            type='context',
                            content=f"<py>{hook['code']}</py>",
                            filename=hook['name']
                        )
                        self.output_pub.publish(out_msg)

                    completed = self.context_gathering_complete.wait(timeout=30.0)
                    if not completed:
                        rospy.logwarn("Timed out waiting for Cognitive Hooks. Proceeding with what was received.")
                        self._send_feedback(
                            "hook_timeout",
                            "Cognitive hooks timed out; continuing with available context.",
                            "error",
                            "bright_yellow",
                            "mini"
                        )

                hook_phase_dur = time.time() - hook_phase_t0
                if hooks_to_run:
                    src = "prefetch" if hook_phase_used_prefetch else "live"
                    rospy.loginfo(f"[Timing] Hook phase ({src}): {hook_phase_dur:.3f}s")
                
                with self.state_lock:
                    self.state = CognitionState.AWAITING_RESPONSE
                    rospy.loginfo("State transition to AWAITING_RESPONSE. Assembling prompt.")
                self._send_feedback("Prompting", "", "", "bright_cyan", "mini")

                header_hooks_data = []
                for s in header_to_run:
                    content = self.context_results.get(s['name'])
                    if content is not None:
                        header_hooks_data.append({'config': s, 'content': content})

                footer_hooks_data = []
                for s in footer_to_run:
                    content = self.context_results.get(s['name'])
                    if content is not None:
                        footer_hooks_data.append({'config': s, 'content': content})

                runtime_cfg = self._runtime_snapshot()
                final_contents = self._construct_prompt_and_images(
                    header_hooks_data,
                    footer_hooks_data,
                    runtime_cfg,
                    include_api_parts=not debug_only,
                )
                pre_api_t = time.time()
                rospy.loginfo(f"[Timing] Hook→API-call gap (hook done to prompt built): {pre_api_t - hook_phase_t0 - hook_phase_dur:.3f}s (prompt build included above)")

                if debug_only:
                    rospy.loginfo(f"{cycle_kind} refreshed UI state and skipped Gemini API call.")
                    return
            
                # --- API Throttling Logic ---
                model_cfg = self.config.framework['main_model']
                throttle_cfg = model_cfg.get('api_throttling', {})
                if throttle_cfg.get('enabled', False):
                    time_since_last_call = time.time() - self.last_api_call_time
                    self.api_delay_budget = max(0, self.api_delay_budget - time_since_last_call)
                    
                    delay_per_call = throttle_cfg.get('delay_per_call_s', 0.5)
                    max_delay = throttle_cfg.get('max_delay_s', 10.0)
                    self.api_delay_budget = min(max_delay, self.api_delay_budget + delay_per_call)

                    if self.api_delay_budget > 0.01: # Avoid sleeping for tiny fractions
                        rospy.loginfo(f"Throttling API call by {self.api_delay_budget:.2f}s.")
                        self._send_feedback("API throttle", f"Waiting {self.api_delay_budget:.1f}s.", "api_call", "bright_yellow", "mini")
                        time.sleep(self.api_delay_budget)
                
                self.last_api_call_time = time.time()

                # --- API Call with Retry Logic ---
                retry_cfg = model_cfg.get('retry_config', {})
                max_attempts = max(1, retry_cfg.get('max_retries', 3))
                complete_response_text = ""
                last_chunk = None

                for attempt_index in range(max_attempts):
                    attempt = attempt_index + 1
                    published_answer_text_this_attempt = False
                    try:
                        runtime_cfg = self._runtime_snapshot()
                        gen_config = self._build_gemini_config(model_cfg, runtime_cfg)
                        model_name = runtime_cfg.get('model') or model_cfg['model']
                        rospy.loginfo(
                            f"Calling Gemini API (Attempt {attempt}/{max_attempts}) "
                            f"with model '{model_name}' and '{runtime_cfg.get('api_profile')}' profile..."
                        )
                        self._send_feedback(
                            "api_call",
                            "",
                             # "Gemini request attempt {attempt}/{max_attempts}",
                            "api_call",
                            "bright_cyan",
                            "mini"
                        )

                        api_call_t0 = time.time()
                        stream = self.genai_client.models.generate_content_stream(
                            model=model_name, contents=final_contents, config=gen_config
                        )
                        rospy.loginfo("Gemini stream opened. Beginning stream processing.")
                        # self._send_feedback("api_streaming", "Gemini is streaming a response.", "api_call", "bright_cyan", "mini")
                        first_token_logged = False

                        for chunk in stream:
                            last_chunk = chunk
                            if not chunk.candidates:
                                continue
                            candidate = chunk.candidates[0]
                            if not candidate or not candidate.content:
                                continue

                            for part in candidate.content.parts:
                                text = getattr(part, 'text', None)
                                if not text:
                                    continue

                                if getattr(part, "thought", False):
                                    # Feedback: Thinking (Trigger once per cycle)
                                    if not self.has_thought_started:
                                        self._send_feedback("thinking", "", "thinking", "bright_blue", "small")
                                        self.has_thought_started = True
                                        print("\n\n=== THOUGHTS ===\n\n")
                                    self.output_pub.publish(CognitionOutput(type='thoughts', content=text))
                                    print(text, end="", flush=True)
                                else:
                                    if not first_token_logged:
                                        rospy.loginfo(f"[Timing] API→first token: {time.time() - api_call_t0:.3f}s")
                                        first_token_logged = True
                                    if complete_response_text == "":
                                        print("\n\n=== FINAL RESPONSE ===\n\n")
                                    print(text, end="", flush=True)
                                    self.output_pub.publish(CognitionOutput(type='chunk', content=text))
                                    complete_response_text += text
                                    published_answer_text_this_attempt = True

                        if last_chunk and getattr(last_chunk, 'usage_metadata', None):
                            md = last_chunk.usage_metadata
                            rospy.loginfo(f"Token usage — prompt: {md.prompt_token_count}, thoughts: {md.thoughts_token_count}, response: {md.candidates_token_count}, total: {md.total_token_count}, CACHED: {md.cached_content_token_count}")

                        rospy.loginfo("Gemini API stream finished.")
                        # self._send_feedback("api_done", "Gemini response complete.", "api_call", "bright_green", "mini")
                        break

                    except Exception as e:
                        info = self._gemini_error_info(e)
                        error_body = self._format_gemini_error_body(info, attempt, max_attempts)
                        rospy.logwarn(f"Gemini API failed on attempt {attempt}/{max_attempts}: {error_body}")
                        self._send_feedback("api_error", error_body, "error", "bright_red", "5x7")

                        failover_retry = (
                            attempt < max_attempts
                            and self._maybe_failover_api_key(info, published_answer_text_this_attempt)
                        )
                        if failover_retry:
                            runtime_cfg = self._runtime_snapshot()
                            final_contents = self._construct_prompt_and_images(
                                header_hooks_data,
                                footer_hooks_data,
                                runtime_cfg,
                            )
                            continue

                        can_retry = info["retryable"] and attempt < max_attempts and not published_answer_text_this_attempt
                        if published_answer_text_this_attempt and info["retryable"]:
                            rospy.logwarn("Gemini stream failed after answer text was published; not retrying to avoid duplicate chunks.")

                        if not can_retry:
                            rospy.logerr("Gemini API request failed without a remaining safe retry. Aborting cognition cycle.")
                            return

                        delay = self._gemini_retry_delay(retry_cfg, attempt_index)
                        retry_body = f"Retrying Gemini request in {delay:.1f}s after {info.get('status') or info['exception_type']}."
                        rospy.loginfo(retry_body)
                        self._send_feedback("api_retry", retry_body, "api_call", "bright_yellow", "mini")
                        time.sleep(delay)
                
                complete_response_text = complete_response_text.strip()
                if complete_response_text.startswith("<me>"): complete_response_text = complete_response_text[4:].lstrip()
                if complete_response_text.endswith("</me>"): complete_response_text = complete_response_text[:-5].rstrip()
                complete_response_text = complete_response_text.strip()

                new_msg_id = self.io.append_message(msg_type='me', content=complete_response_text)
                final_output = CognitionOutput(type='me', content=complete_response_text, filename=new_msg_id)
                self.output_pub.publish(final_output)
            
            finally:
                with self.state_lock:
                    self.state = CognitionState.IDLE
                rospy.loginfo(f"--- Cognition Cycle Finished. Total: {time.time() - cycle_start_t:.3f}s. State reset to IDLE. ---")
                self.last_received_system_hint = ""
                
    def run(self):
        rospy.spin()

if __name__ == '__main__':
    try:
        node = CognitionNode()
        node.run()
    except rospy.ROSInterruptException:
        pass
