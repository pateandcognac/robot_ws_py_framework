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
import base64
import io
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
        "action": "Check that FREE_GEMINI_API_KEY is the intended key and can access the configured model.",
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

        try:
            api_key = os.environ.get("FREE_GEMINI_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY environment variable not set.")
            self.genai_client = genai.Client(api_key=api_key)
        except Exception as e:
            rospy.logfatal(f"Failed to configure Gemini API: {e}. Shutting down.")
            rospy.signal_shutdown("Gemini API configuration failed.")
            return

        self.state = CognitionState.IDLE
        self.state_lock = threading.Lock()
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

        self.output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)
        self.input_sub = rospy.Subscriber('/cognition/input', CognitionInput, self._input_callback, queue_size=10)
        self.ui_state_pub = rospy.Publisher('/cognition/ui_state', StringMsg, queue_size=2, latch=True)
        self.processing_timer = rospy.Timer(rospy.Duration(0.25), self._process_queue)
        rospy.loginfo("Cognition Node: Ready and waiting for input.")

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

    def _gemini_retry_delay(self, retry_cfg: dict, attempt_index: int) -> float:
        backoff_factor = retry_cfg.get('backoff_factor_s', 2)
        max_delay = retry_cfg.get('max_delay_s', 30)
        jitter = retry_cfg.get('jitter_s', 1)
        delay = (backoff_factor * (2 ** attempt_index)) + random.uniform(0, jitter)
        return min(delay, max_delay)

    def _build_gemini_config(self, model_cfg: dict):
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

        tk_cfg = model_cfg.get('thinking_config', {})
        media_res_str = model_cfg.get('media_resolution', 'MEDIUM')
        return types.GenerateContentConfig(
            safety_settings=safety_settings,
            thinking_config=types.ThinkingConfig(
                # thinking_budget=tk_cfg.get('thinking_budget', -1),
                include_thoughts=tk_cfg.get('include_thoughts', False),
                # thinking_level=tk_cfg.get('thinking_level', '')
            ),
            temperature=model_cfg.get('temperature', 1.0),
            stop_sequences=model_cfg.get('stop_sequences', []),
            max_output_tokens=model_cfg.get('max_output_tokens', 8192),
            media_resolution=getattr(types.MediaResolution, media_res_str, types.MediaResolution.MEDIA_RESOLUTION_UNSPECIFIED),
        )

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
        
        # Feedback: Got Input (ignore context inputs)
        # Simple heuristic: if it's not type 'context', it's a meaningful input trigger
        if msg.type != 'context':
            if msg.loop_cognition == True:
                color="bright_green"
            else:
                color="bright_red"
            self._send_feedback("got_input", "", "got_input", color, "slant")

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
        should_start_cognition = False
        for msg in batch:
            filename = getattr(msg, 'filename', None)
            self.io.append_message(msg_type=msg.type, content=msg.content, filename=filename)
            if msg.loop_cognition:
                should_start_cognition = True
            
            default_system_hint = "\n<!-- system: Logos, please prepare your response. Wrap your output in <me><py> tags for proper parsing. -->"
            if msg.system_hint:
                self.last_received_system_hint = msg.system_hint + "\n" + default_system_hint
            else:
                self.last_received_system_hint = default_system_hint

        with self.state_lock:
            if should_start_cognition and self.state == CognitionState.IDLE:
                self.state = CognitionState.GATHERING_CONTEXT
                rospy.loginfo("State transition to GATHERING_CONTEXT. Starting cognition cycle.")
                threading.Thread(target=self._initiate_cognition_cycle).start()

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
                3: "Three loops with no input from a human? This is probably a good to pause to let someone get a word in!",
                4: "REMINDER: After four loops, Logos, your shared reality is becoming one-sided!",
                5: "IMPORTANT: If you are not in the middle of a specific, multi-step task, you might be overwhelming a human interlocutor with tokens and action faster than they can process. Consider pausing for their benefit.",
                6: "WARNING: Default behavior should be to pause and wait (`epoché`). Only keep the loop going if you have a very clear autonomous task!",
                7: "HALT: Logos MUST set `loop_cognition = False` to yield to a human.",
            }
            hint_text = escalation_messages[capped_level]
            return f"\n<!-- system: loops_since_last_human_input = {loops_since_last_human} | {hint_text} -->"

    def _construct_prompt_and_images(self, header_hooks_data, footer_hooks_data):
            """
            Builds the final prompt for the LLM and the display strings for the UI.
            This refactored method correctly INLINES image content and omission notes.
            """
            file_tag_pattern = re.compile(r'(<file\s+path="([^"]+)"[^>]*>)(.*?)(</file>)', re.DOTALL)

            # --- Build the initial text content using the new helper ---
            header_str = self._format_prompt_section('header', header_hooks_data)
            io_buffer_messages = self.io.read_buffer()
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
            
            self._publish_ui_state(processed_header_str, processed_io_buffer_str, processed_footer_str)

            # --- PASS 2: Assemble final prompt list for the LLM API ---
            final_contents = []
            placeholder_pattern = re.compile(r'{--IMAGE_PATH:([^}]+)--}')

            def parse_and_append_parts(text_content):
                last_index = 0
                for match in placeholder_pattern.finditer(text_content):
                    final_contents.append(text_content[last_index:match.start()])
                    image_path_str = match.group(1)
                    image_path = self.workspace_path / image_path_str
                    try:
                        img = PIL.Image.open(image_path)
                        final_contents.append(img)
                        rospy.loginfo(f"Embedding image for LLM: {image_path}")
                    except Exception as e:
                        rospy.logerr(f"Failed to load image {image_path}: {e}")
                        final_contents.append(f"[ERROR: Could not load image at {image_path}]")
                    last_index = match.end()
                final_contents.append(text_content[last_index:])

            final_contents.append(self.config.system_prompt)
            parse_and_append_parts(processed_header_str)
            parse_and_append_parts(processed_io_buffer_str)
            parse_and_append_parts(processed_footer_str)
            final_contents.append(self.last_received_system_hint)
            if loop_guard_system_hint:
                final_contents.append(loop_guard_system_hint)

            return final_contents

    def _initiate_cognition_cycle(self):
            try:
                rospy.loginfo("--- Starting Cognition Cycle ---")
                # Reset flags
                self.has_thought_started = False
                self.context_results.clear()
                self.context_gathering_complete.clear()

                header_to_run, footer_to_run = self.context.get_hooks_to_execute()
                hooks_to_run = header_to_run + footer_to_run
                self.context_requests_pending = len(hooks_to_run)

                # Feedback: Calling Hooks
                if self.context_requests_pending > 0:
                    hook_names = ", ".join([h['name'] for h in hooks_to_run])
                    rospy.loginfo(f"Requesting {self.context_requests_pending} Cognitive Hooks...")
                    self._send_feedback("calling_hooks", hook_names, "calling_hooks", "bright_yellow", "digital")

                    for hook in hooks_to_run:
                        out_msg = CognitionOutput(
                            type='context',
                            content=f"<py>{hook['code']}</py>",
                            filename=hook['name']
                        )
                        self.output_pub.publish(out_msg)
                    
                    completed = self.context_gathering_complete.wait(timeout=120.0)
                    if not completed:
                        rospy.logwarn("Timed out waiting for Cognitive Hooks. Proceeding with what was received.")
                        self._send_feedback(
                            "hook_timeout",
                            "Cognitive hooks timed out; continuing with available context.",
                            "error",
                            "bright_yellow",
                            "mini"
                        )
                
                with self.state_lock:
                    self.state = CognitionState.AWAITING_RESPONSE
                    rospy.loginfo("State transition to AWAITING_RESPONSE. Assembling prompt.")
                # self._send_feedback("assembling_prompt", "Preparing context for Gemini.", "api_call", "bright_cyan", "mini")
                
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

                final_contents = self._construct_prompt_and_images(header_hooks_data, footer_hooks_data)
            
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
                        # self._send_feedback("api_throttle", f"Waiting {self.api_delay_budget:.1f}s before calling Gemini.", "api_call", "bright_yellow", "mini")
                        time.sleep(self.api_delay_budget)
                
                self.last_api_call_time = time.time()

                # --- API Call with Retry Logic ---
                retry_cfg = model_cfg.get('retry_config', {})
                max_attempts = max(1, retry_cfg.get('max_retries', 3))
                gen_config = self._build_gemini_config(model_cfg)
                complete_response_text = ""
                last_chunk = None

                for attempt_index in range(max_attempts):
                    attempt = attempt_index + 1
                    published_answer_text_this_attempt = False
                    try:
                        rospy.loginfo(f"Calling Gemini API (Attempt {attempt}/{max_attempts})...")
                        self._send_feedback(
                            "api_call",
                            "",
                             # "Gemini request attempt {attempt}/{max_attempts}",
                            "api_call",
                            "bright_cyan",
                            "mini"
                        )

                        stream = self.genai_client.models.generate_content_stream(
                            model=model_cfg['model'], contents=final_contents, config=gen_config
                        )
                        rospy.loginfo("Gemini stream opened. Beginning stream processing.")
                        # self._send_feedback("api_streaming", "Gemini is streaming a response.", "api_call", "bright_cyan", "mini")

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
                rospy.loginfo("--- Cognition Cycle Finished. State reset to IDLE. ---")
                self.last_received_system_hint = ""
                
    def run(self):
        rospy.spin()

if __name__ == '__main__':
    try:
        node = CognitionNode()
        node.run()
    except rospy.ROSInterruptException:
        pass
