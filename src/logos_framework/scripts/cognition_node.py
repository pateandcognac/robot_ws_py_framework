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
            api_key = os.environ.get("GEMINI_API_KEY")
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
        self.ui_state_pub = rospy.Publisher('/cognition/ui_state', StringMsg, queue_size=1, latch=True)
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

    def _input_callback(self, msg: CognitionInput):
        if msg.type == 'context' and self.state in [CognitionState.GATHERING_CONTEXT, CognitionState.AWAITING_RESPONSE]:
            hook_name = msg.filename
            if hook_name:
                self.context_results[hook_name] = msg.content
                hook_config = next((s for s in self.context.header_hooks + self.context.footer_hooks if s['name'] == hook_name), None)
                if hook_config and hook_config.get('ttl', 0) < 0:
                    self.context.cached_output[hook_name] = msg.content
                    rospy.loginfo(f"Cached output for '{hook_name}'.")

                self.context_requests_pending -= 1
                if self.context_requests_pending <= 0:
                    self.context_gathering_complete.set()
            else:
                rospy.logwarn(f"Received context message without a filename. Cannot process.")
            return
        
        # Feedback: Got Input (ignore context inputs)
        # Simple heuristic: if it's not type 'context', it's a meaningful input trigger
        if msg.type != 'context':
            self._send_feedback("got_input", "", "got_input", "green", "slant")

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
            
            default_system_hint = "<!-- system: Logos, please prepare your response. Wrap your output in <me> tags for proper parsing. -->\n\n<me>"
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
        def embed_image_base64(match):
            relative_path = match.group(2) 
            full_path = self.workspace_path / relative_path
            
            try:
                with PIL.Image.open(full_path) as img:
                    buffered = io.BytesIO()
                    img_format = img.format if img.format else 'PNG'
                    img.save(buffered, format=img_format)
                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
                    # This logic correctly APPENDS to the tag for the UI, which is fine for a UI.
                    return f'{match.group(1)}{match.group(3)}</file>\n<img src="data:image/{img_format.lower()};base64,{img_str}">'
            except Exception as e:
                rospy.logerr(f"UI State: Could not process image {full_path}: {e}")
                return f'{match.group(1)}{match.group(3)}</file>\n<!-- Error loading image: {e} -->'

        # Use a regex that captures the whole tag block for consistency
        image_pattern = re.compile(r'(<file\s+path="([^"]+)"[^>]*>)(.*?)(</file>)', re.DOTALL)

        header_str_with_images = re.sub(image_pattern, embed_image_base64, header_str)
        io_buffer_str_with_images = re.sub(image_pattern, embed_image_base64, io_buffer_str)
        footer_str_with_images = re.sub(image_pattern, embed_image_base64, footer_str)

        ui_state = {
            "header": header_str_with_images,
            "io_buffer": io_buffer_str_with_images,
            "footer": footer_str_with_images
        }
        
        try:
            json_payload = json.dumps(ui_state)
            self.ui_state_pub.publish(StringMsg(data=json_payload))
            rospy.loginfo("Published UI state update.")
        except Exception as e:
            rospy.logerr(f"Failed to create or publish UI state: {e}")

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
                show_ttl = cfg.get('show_hook_ttl', False)

                for item in items:
                    hook_name = item['config'].get('name', 'unnamed')
                    content = item['content']
                    ttl = item['config'].get('ttl', 0)
                    token_count = len(content) // divisor
                    total_tokens += token_count
                    if show_ttl:
                        content_str += f'<{hook_name} ttl="{ttl}">\n{content}\n</{hook_name}>\n\n'
                    else:
                        content_str += f'<{hook_name}>\n{content}\n</{hook_name}>\n\n'
                
                if show_stats:
                    return f'<{section_name} hooks="{len(items)}" tokens="{total_tokens}">\n{content_str.strip()}\n</{section_name}>'
                else:
                    return f'<{section_name}>\n{content_str.strip()}\n</{section_name}>'

            elif section_type == 'io_buffer':
                section_name = 'io_buffer'
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

    def _construct_prompt_and_images(self, header_hooks_data, footer_hooks_data):
            """
            Builds the final prompt for the LLM and the display strings for the UI.
            This refactored method correctly INLINES image content and omission notes.
            """
            file_tag_pattern = re.compile(r'(<file\s+path="([^"]+)"[^>]*>)(.*?)(</file>)', re.DOTALL)

            # --- Build the initial text content using the new helper ---
            header_str = self._format_prompt_section('header', header_hooks_data)
            io_buffer_messages = self.io.read_buffer()
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
                    self._send_feedback("calling_hooks", hook_names, "calling_hooks", "yellow", "digital")

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
                
                with self.state_lock:
                    self.state = CognitionState.AWAITING_RESPONSE
                    rospy.loginfo("State transition to AWAITING_RESPONSE. Assembling prompt.")
                
                header_hooks_data = []
                for s in self.context.header_hooks:
                    content = self.context_results.get(s['name'], self.context.cached_output.get(s.get('name')))
                    if content is not None:
                        header_hooks_data.append({'config': s, 'content': content})

                footer_hooks_data = []
                for s in self.context.footer_hooks:
                    content = self.context_results.get(s['name'], self.context.cached_output.get(s.get('name')))
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
                        time.sleep(self.api_delay_budget)
                
                self.last_api_call_time = time.time()

                # --- API Call with Retry Logic ---
                retry_cfg = model_cfg.get('retry_config', {})
                max_retries = retry_cfg.get('max_retries', 3)
                backoff_factor = retry_cfg.get('backoff_factor_s', 2)
                
                stream = None
                for attempt in range(max_retries):
                    try:
                        rospy.loginfo(f"Calling Gemini API (Attempt {attempt + 1}/{max_retries})...")
                        
                        # Feedback: API Call
                        self._send_feedback("api_call", "", "api_call", "bright_cyan", "mini")

                        # This is the original API call logic, now inside the loop
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
                        media_res_str = model_cfg.get('media_resolution', 'MEDIA_RESOLUTION_UNSPECIFIED')
                        gen_config = types.GenerateContentConfig(
                            safety_settings=safety_settings,
                            thinking_config=types.ThinkingConfig(
                                thinking_budget=tk_cfg.get('thinking_budget', -1),
                                include_thoughts=tk_cfg.get('include_thoughts', False),
                            ),
                            temperature=model_cfg.get('temperature', 0.7),
                            stop_sequences=model_cfg.get('stop_sequences', []),                
                            max_output_tokens=model_cfg.get('max_output_tokens', 8192),
                            media_resolution=getattr(types.MediaResolution, media_res_str, types.MediaResolution.MEDIA_RESOLUTION_UNSPECIFIED),
                        )
                        stream = self.genai_client.models.generate_content_stream(
                            model=model_cfg['model'], contents=final_contents, config=gen_config
                        )
                        rospy.loginfo("API call successful, beginning stream processing.")
                        break # Success! Exit the retry loop.

                    except Exception as e:
                        rospy.logwarn(f"Gemini API call failed on attempt {attempt + 1}: {e}")
                        
                        # Feedback: Error
                        self._send_feedback("api_error", str(e), "error", "bright_red", "5x7")

                        if attempt + 1 == max_retries:
                            rospy.logerr("All Gemini API retries failed. Aborting cognition cycle.")
                            # Abort the cycle. The 'finally' block will still run to reset state.
                            return 
                        
                        delay = (backoff_factor * (2 ** attempt)) + random.uniform(0, 1)
                        rospy.loginfo(f"Waiting {delay:.2f}s before next retry.")
                        time.sleep(delay)

                # --- Stream Processing  ---
                complete_response_text = ""
                if stream:
                    for chunk in stream:
                        if not chunk.candidates: continue
                        candidate = chunk.candidates[0]
                        if not candidate or not candidate.content: continue

                        for part in candidate.content.parts:
                            text = getattr(part, 'text', None)
                            if not text: continue

                            if getattr(part, "thought", False):
                                # Feedback: Thinking (Trigger once per cycle)
                                if not self.has_thought_started:
                                    self._send_feedback("thinking", "", "thinking", "bright_blue", "small")
                                    self.has_thought_started = True

                                self.output_pub.publish(CognitionOutput(type='thoughts', content=text))
                            else:
                                self.output_pub.publish(CognitionOutput(type='chunk', content=text))
                                complete_response_text += text
                    
                    if 'chunk' in locals() and getattr(chunk, 'usage_metadata', None):
                        md = chunk.usage_metadata
                        rospy.loginfo(f"Token usage — prompt: {md.prompt_token_count}, thoughts: {md.thoughts_token_count}, response: {md.candidates_token_count}, total: {md.total_token_count}")

                rospy.loginfo("API stream finished.")
                
                complete_response_text = complete_response_text.strip()
                if complete_response_text.startswith("<me>"): complete_response_text = complete_response_text[4:].lstrip()
                if complete_response_text.endswith("</me>"): complete_response_text = complete_response_text[:-5].rstrip()
                complete_response_text = complete_response_text.strip()

                new_msg_id = self.io.append_message(msg_type='me', content=complete_response_text)
                final_output = CognitionOutput(type='me', content=complete_response_text, filename=new_msg_id)
                self.output_pub.publish(final_output)
            
            finally:
                self.context.update_and_save_configs()
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