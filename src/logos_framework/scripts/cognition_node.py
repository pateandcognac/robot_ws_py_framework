#!/usr/bin/env python3
import rospy
import os
import re
import json
import time
import threading
import string
from pathlib import Path
from collections import deque
from ruamel.yaml import YAML
from enum import Enum

# Gemini API and Image Handling
from google import genai
from google.genai import types
import PIL.Image

import base64
import io
from std_msgs.msg import String as StringMsg

from logos_framework.msg import CognitionInput, CognitionOutput


# Initialize a YAML instance that preserves comments and formatting
yaml = YAML()

class ContextManager:
    """
    Manages the loading, execution logic, and persistence of context snippets.
    """
    def __init__(self, workspace_path: Path, config: dict):
        """
        Initializes the ContextManager.

        Args:
            workspace_path: The root path of the agent's workspace.
            config: The 'context' section of the framework_config.json.
        """
        rospy.loginfo("ContextManager: Initializing...")
        self.workspace_path = workspace_path
        self.state_path = self.workspace_path / "state"
        self.config = config
        self._lock = threading.RLock()

        # Get header/footer names from config
        self.header_name = self.config['header_name']
        self.footer_name = self.config['footer_name']
        self.header_config_path = self.state_path / f"{self.header_name}_config.yaml"
        self.footer_config_path = self.state_path / f"{self.footer_name}_config.yaml"

        # In-memory representation of snippets and their cached outputs
        self.header_snippets = []
        self.footer_snippets = []
        self.cached_output = {}

        self._load_configs() # Initial load
        rospy.loginfo("ContextManager: Initialization complete.")

    def _load_configs(self):
        """Loads the header and footer YAML configuration files into memory."""
        with self._lock:
            try:
                if self.header_config_path.exists():
                    with open(self.header_config_path, 'r') as f:
                        self.header_snippets = yaml.load(f) or []
                else:
                    self.header_snippets = []
                    rospy.logwarn(f"ContextManager: Header config not found at {self.header_config_path}")

                if self.footer_config_path.exists():
                    with open(self.footer_config_path, 'r') as f:
                        self.footer_snippets = yaml.load(f) or []
                else:
                    self.footer_snippets = []
                    rospy.logwarn(f"ContextManager: Footer config not found at {self.footer_config_path}")
            except Exception as e:
                rospy.logerr(f"ContextManager: Error loading snippet configurations: {e}")

    def get_snippets_to_execute(self):
        """
        Determines which snippets need to be run in the current cycle.

        Returns:
            A tuple containing (list of header snippets to run, list of footer snippets to run).
        """
        with self._lock:
            self._load_configs()
            header_to_run = [s for s in self.header_snippets if self._should_run(s)]
            footer_to_run = [s for s in self.footer_snippets if self._should_run(s)]
        return header_to_run, footer_to_run

    def _should_run(self, snippet: dict):
        """
        Logic to decide if a snippet should be executed based on its TTL and cache status.
        - ttl > 0: Dynamic, runs every cycle.
        - ttl < 0: Cached, runs only if not already in cache.
        - ttl == 0: Disabled.
        - ttl == +/-99: Pinned, always runs (dynamic) or runs once and stays forever (cached).
        """
        ttl = snippet.get('ttl', 0)
        name = snippet.get('name', 'unnamed_snippet')

        if ttl == 0:
            return False
        if ttl > 0: # Dynamic snippet
            return True
        if ttl < 0: # Cached snippet
            return name not in self.cached_output

        return False

    def update_and_save_configs(self):
        """
        Updates the TTLs of all snippets and writes the configurations back to their files.
        This should be called once per cognition cycle.
        """
        with self._lock:
            configs_changed = False
            
            # Process header snippets
            updated_header_snippets = []
            for s in self.header_snippets:
                new_ttl, changed = self._get_updated_ttl(s)
                s['ttl'] = new_ttl
                if changed:
                    configs_changed = True
                
                if new_ttl == 0 and s.get('name') in self.cached_output:
                    rospy.loginfo(f"ContextManager: Invalidating cache for EOL snippet '{s['name']}'.")
                    del self.cached_output[s['name']]
                    configs_changed = True


                if s['ttl'] == 0 and self.config.get('remove_header_at_eol', False):
                    rospy.loginfo(f"ContextManager: Removing EOL header snippet '{s['name']}'.")
                    configs_changed = True
                    continue
                updated_header_snippets.append(s)
            self.header_snippets = updated_header_snippets

            # Process footer snippets
            updated_footer_snippets = []
            for s in self.footer_snippets:
                new_ttl, changed = self._get_updated_ttl(s)
                s['ttl'] = new_ttl
                if changed:
                    configs_changed = True

                if s['ttl'] == 0 and self.config.get('remove_footer_at_eol', False):
                    rospy.loginfo(f"ContextManager: Removing EOL footer snippet '{s['name']}'.")
                    configs_changed = True
                    continue
                updated_footer_snippets.append(s)
            self.footer_snippets = updated_footer_snippets

            if configs_changed:
                rospy.loginfo("ContextManager: Snippet TTLs changed, saving configs to disk.")
                try:
                    with open(self.header_config_path, 'w') as f:
                        yaml.dump(self.header_snippets, f)
                    with open(self.footer_config_path, 'w') as f:
                        yaml.dump(self.footer_snippets, f)
                except Exception as e:
                    rospy.logerr(f"ContextManager: Failed to save updated snippet configs: {e}")

    def _get_updated_ttl(self, snippet: dict):
        """Calculates the new TTL for a snippet."""
        ttl = snippet.get('ttl', 0)
        original_ttl = ttl
        
        if ttl in [99, -99, 0]:
            return ttl, False

        if ttl > 0:
            ttl -= 1
        elif ttl < 0:
            ttl += 1
            
        return ttl, ttl != original_ttl



class ConfigManager:
    """Handles loading and accessing all agent configuration files."""
    def __init__(self, workspace_path: Path):
        self.workspace_path = workspace_path
        self.framework = {}
        self.system_prompt = ""
        self.my_config = {}
        
    def load_configs(self):
        rospy.loginfo("ConfigManager: Loading configurations...")
        try:
            framework_path = self.workspace_path / ".system" / "framework_config.json"
            prompt_path = self.workspace_path / ".system" / "system_prompt.txt"
            with open(framework_path, 'r') as f: self.framework = json.load(f)
            with open(prompt_path, 'r') as f: self.system_prompt = f.read()

            my_config_path = self.workspace_path / "state" / "my_config.yaml"
            if my_config_path.exists():
                with open(my_config_path, 'r') as f: self.my_config = yaml.load(f)
            else:
                rospy.logwarn(f"ConfigManager: my_config.yaml not found at {my_config_path}. Using default values.")
                self.my_config = {} # Ensure it's a dict
        
            # Perform templating on the system prompt
            workspace_name = self.workspace_path.name
            agent_settings = self.framework.get('agent_settings', {})
            context_settings = self.framework.get('context', {})

            self.system_prompt = self.system_prompt.replace('{{header_name}}', context_settings.get('header_name', ''))
            self.system_prompt = self.system_prompt.replace('{{footer_name}}', context_settings.get('footer_name', ''))
            self.system_prompt = self.system_prompt.replace('{{workspace_name}}', workspace_name)
            self.system_prompt = self.system_prompt.replace('{{global_max_io_buffer_media}}', str(agent_settings.get('global_max_io_buffer_media', 'N/A')))
            
            rospy.loginfo("ConfigManager: System prompt templating complete.")

            rospy.loginfo("ConfigManager: All configurations loaded successfully.")
            return True

        except Exception as e:
            rospy.logerr(f"ConfigManager: Error loading configurations: {e}")
            return False

class IOManager:
    """Handles thread-safe reading and writing to the agent's I/O files."""
    def __init__(self, workspace_path: Path, framework_config: dict):
        state_path = workspace_path / "state"
        state_path.mkdir(exist_ok=True)
        self.history_file = state_path / "io_history.jsonl"
        self.buffer_file = state_path / "io_buffer.jsonl"
        self._lock = threading.Lock()
        self.framework_config = framework_config
        self.id_counter = 0
        self._initialize_id_counter()

    def _base36_encode(self, number, min_length=4):
        """Converts an integer to a base36 string, zero-padded."""
        alphabet = string.digits + string.ascii_lowercase
        if number == 0:
            return '0'.zfill(min_length)
        base36 = ''
        while number != 0:
            number, i = divmod(number, 36)
            base36 = alphabet[i] + base36
        return base36.zfill(min_length)

    def _initialize_id_counter(self):
        """Reads the last message ID from the history file to set the counter."""
        with self._lock:
            if not self.history_file.exists():
                self.id_counter = 0
                rospy.loginfo("IOManager: History file not found. Starting message ID from 0.")
                return

            try:
                with open(self.history_file, 'rb') as f:
                    f.seek(0, os.SEEK_END)
                    if f.tell() == 0:
                        self.id_counter = 0
                        return

                    f.seek(-2, os.SEEK_END)
                    while f.read(1) != b'\n':
                        if f.tell() < 3:
                           f.seek(0, os.SEEK_SET)
                           break
                        f.seek(-2, os.SEEK_CUR)
                    
                    last_line = f.readline().decode('utf-8')
                    last_msg = json.loads(last_line)
                    last_id_str = last_msg.get('id', 'msg-0').split('-')[-1]
                    self.id_counter = int(last_id_str, 36) + 1
                    rospy.loginfo(f"IOManager: Resuming message ID from {self.id_counter} (last was {last_id_str}).")

            except Exception as e:
                rospy.logerr(f"IOManager: Failed to initialize ID counter from history file: {e}. Starting from 0.")
                self.id_counter = 0

    def append_message(self, msg_type: str, content: str, filename: str = None):
        with self._lock:
            msg_id = f"msg-{self._base36_encode(self.id_counter)}"
            self.id_counter += 1

            divisor = self.framework_config.get('context', {}).get('token_estimation_divisor', 5)
            token_count = len(content) // divisor

            message_data = {
                "id": msg_id,
                "type": msg_type,
                "timestamp": time.time(),
                "token_count": token_count,
                "content": content
            }
            if filename:
                message_data['filename'] = filename

            line = json.dumps(message_data) + '\n'
            try:
                with open(self.history_file, 'a') as f: f.write(line)
                with open(self.buffer_file, 'a') as f: f.write(line)
                rospy.loginfo(f"IOManager: Appended message {msg_id} ({msg_type}).")
                return msg_id
            except Exception as e:
                rospy.logerr(f"IOManager: Failed to write to I/O files: {e}")
                return None

    def read_buffer(self):
        with self._lock:
            if not self.buffer_file.exists():
                return []
            with open(self.buffer_file, 'r') as f:
                return [json.loads(line) for line in f if line.strip()]


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
        self.output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)
        self.input_sub = rospy.Subscriber('/cognition/input', CognitionInput, self._input_callback, queue_size=10)
        self.ui_state_pub = rospy.Publisher('/cognition/ui_state', StringMsg, queue_size=1, latch=True)
        self.processing_timer = rospy.Timer(rospy.Duration(0.25), self._process_queue)
        rospy.loginfo("Cognition Node: Ready and waiting for input.")

    def _input_callback(self, msg: CognitionInput):
        # ... (This method is unchanged and correct)
        if msg.type == 'context' and self.state in [CognitionState.GATHERING_CONTEXT, CognitionState.AWAITING_RESPONSE]:
            snippet_name = msg.filename
            if snippet_name:
                self.context_results[snippet_name] = msg.content
                snippet_config = next((s for s in self.context.header_snippets + self.context.footer_snippets if s['name'] == snippet_name), None)
                if snippet_config and snippet_config.get('ttl', 0) < 0:
                    self.context.cached_output[snippet_name] = msg.content
                    rospy.loginfo(f"Cached output for '{snippet_name}'.")

                self.context_requests_pending -= 1
                if self.context_requests_pending <= 0:
                    self.context_gathering_complete.set()
            else:
                rospy.logwarn(f"Received context message without a filename. Cannot process.")
            return

        with self.queue_lock:
            self.incoming_queue.append(msg)

    def _process_queue(self, event=None):
        # ... (This method is unchanged and correct)
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
            
            default_system_hint = "<!-- <system>: Logos, please prepare your response. Wrap your output in <me> tags for proper parsing. -->\n\n<me>"
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
        image_pattern = re.compile(r'(<file\s+path="([^"]+)"[^>]*>)(.*?)</file>', re.DOTALL)

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

    def _construct_prompt_and_images(self, header_snippets_data, footer_snippets_data):
        """
        Builds the final prompt for the LLM and the display strings for the UI.
        This refactored method correctly INLINES image content and omission notes.
        """
        cfg = self.config.framework['context']
        
        # --- A regex to capture the entire file block ---
        # Group 1: The full opening tag, e.g., '<file path="path/to/file.png">'
        # Group 2: Just the path itself, e.g., 'path/to/file.png'
        # Group 3: Any optional content between the tags. The '?' makes it non-greedy.
        # Group 4: The closing tag, '</file>'
        # re.DOTALL allows '.' to match newline characters.
        file_tag_pattern = re.compile(r'(<file\s+path="([^"]+)"[^>]*>)(.*?)(</file>)', re.DOTALL)

        # --- Build the initial text content for Header, IO Buffer, and Footer ---
        header_str = ""
        if header_snippets_data:
            content_str = ""
            total_tokens = 0
            for item in header_snippets_data:
                snippet_name = item['config'].get('name', 'unnamed')
                content = item['content']
                ttl = item['config'].get('ttl', 0)
                token_count = len(content) // cfg.get('token_estimation_divisor', 5)
                total_tokens += token_count
                if cfg.get('show_snippet_ttl', False):
                    content_str += f'<{snippet_name} ttl="{ttl}">\n{content}\n</{snippet_name}>\n'
                else:
                    content_str += f'<{snippet_name}>\n{content}\n</{snippet_name}>\n'
            header_name = cfg.get('header_name', 'header')
            if cfg.get('show_header_stats', False):
                header_str = f'<{header_name} snippets="{len(header_snippets_data)}" tokens="{total_tokens}">\n{content_str.strip()}\n</{header_name}>'
            else:
                header_str = f'<{header_name}>\n{content_str.strip()}\n</{header_name}>'

        io_buffer_str = ""
        buffer_messages = self.io.read_buffer()
        if buffer_messages:
            content_str = ""
            total_tokens = 0
            for i, msg in enumerate(buffer_messages):
                msg_type, msg_id = msg.get("type", "unknown"), msg.get("id", "no-id")
                content, token_count = msg.get("content", ""), msg.get("token_count", 0)
                total_tokens += token_count
                if msg_type == 'system':
                    content_str += f'<!-- <{msg_type}>: {content} -->\n'
                else:
                    if cfg.get('show_io_cell_stats', False):
                        content_str += f'<{msg_type} cell="{i}" id="{msg_id}" tokens="{token_count}">\n{content}\n</{msg_type}>\n'
                    else:
                        content_str += f'<{msg_type}>\n{content}\n</{msg_type}>\n'
            if cfg.get('show_io_buffer_stats', False):
                io_buffer_str = f'<io_buffer cells="{len(buffer_messages)}" tokens="{total_tokens}">\n{content_str.strip()}\n</io_buffer>'
            else:
                io_buffer_str = f'<io_buffer>\n{content_str.strip()}\n</io_buffer>'

        footer_str = ""
        if footer_snippets_data:
            content_str = ""
            total_tokens = 0
            for item in footer_snippets_data:
                snippet_name = item['config'].get('name', 'unnamed')
                content = item['content']
                ttl = item['config'].get('ttl', 0)
                token_count = len(content) // cfg.get('token_estimation_divisor', 5)
                total_tokens += token_count
                if cfg.get('show_snippet_ttl', False):
                    content_str += f'<{snippet_name} ttl="{ttl}">\n{content}\n</{snippet_name}>\n'
                else:
                    content_str += f'<{snippet_name}>\n{content}\n</{snippet_name}>\n'
            footer_name = cfg.get('footer_name', 'footer')
            if cfg.get('show_footer_stats', False):
                footer_str = f'<{footer_name} snippets="{len(footer_snippets_data)}" tokens="{total_tokens}">\n{content_str.strip()}\n</{footer_name}>'
            else:
                footer_str = f'<{footer_name}>\n{content_str.strip()}\n</{footer_name}>'

        # --- PASS 1: Process the IO Buffer for image limiting and create final text ---
        # This pass will replace <file> tags with either a placeholder or an omission note.
        my_config_limit = self.config.my_config.get('io_buffer', {}).get('max_io_buffer_media', 8)
        global_limit = self.config.framework.get('agent_settings', {}).get('global_max_io_buffer_media', 32)
        max_images_to_process = min(my_config_limit, global_limit)
        rospy.loginfo(f"Applying image limit: max {max_images_to_process} (local: {my_config_limit}, global: {global_limit})")
        
        # We use a list to act as a mutable counter inside the replacer function.
        images_processed_count = [0] 

        def image_tag_handler(match):
            opening_tag = match.group(1)
            # path = match.group(2) # We'll get the path later when processing placeholders
            optional_text = match.group(3).strip()
            closing_tag = match.group(4)
            
            # Add a newline if optional text exists, for clean formatting.
            separator = '\n' if optional_text else ''

            if images_processed_count[0] < max_images_to_process:
                images_processed_count[0] += 1
                # INLINE a placeholder. We will replace this in Pass 2.
                # The placeholder format is arbitrary but should be unique.
                return f'{opening_tag}{optional_text}{separator}{{--IMAGE_PATH:{match.group(2)}--}}{closing_tag}'
            else:
                # INLINE the omission note. This is the desired behavior.
                return f'{opening_tag}{optional_text}{separator}(Image omitted per my_config.yaml){closing_tag}'

        # We find all file tags in the buffer and decide which ones to process from last to first.
        all_file_tags = list(file_tag_pattern.finditer(io_buffer_str))
        tags_to_keep = {m.group(0) for m in all_file_tags[-max_images_to_process:]}

        def selective_replacer(match):
            # This replacer only transforms the tag if it's one of the last N tags.
            # Otherwise, it becomes an "omitted" tag.
            if match.group(0) in tags_to_keep:
                 optional_text = match.group(3).strip()
                 separator = '\n' if optional_text else ''
                 return f'{match.group(1)}{optional_text}{separator}{{--IMAGE_PATH:{match.group(2)}--}}{match.group(4)}'
            else:
                 optional_text = match.group(3).strip()
                 separator = '\n' if optional_text else ''
                 return f'{match.group(1)}{optional_text}{separator}(Image omitted per my_config.yaml){match.group(4)}'

        # Apply the replacement logic to the io_buffer string
        processed_io_buffer_str = re.sub(file_tag_pattern, selective_replacer, io_buffer_str)

        # For the UI, we want to show the final text state before image objects are embedded.
        # We'll use the processed buffer string. Headers/Footers could also have images.
        processed_header_str = re.sub(file_tag_pattern, selective_replacer, header_str)
        processed_footer_str = re.sub(file_tag_pattern, selective_replacer, footer_str)
        
        # We can now publish a clean UI state that reflects what the LLM will see text-wise.
        # The placeholder will just look like text in the UI, which is fine.
        self._publish_ui_state(processed_header_str, processed_io_buffer_str, processed_footer_str)

        # --- PASS 2: Assemble final prompt list for the LLM API ---
        # Now, we parse the processed strings and replace placeholders with PIL.Image objects.
        final_contents = []
        placeholder_pattern = re.compile(r'{--IMAGE_PATH:([^}]+)--}')

        def parse_and_append_parts(text_content):
            last_index = 0
            for match in placeholder_pattern.finditer(text_content):
                # Append text before the placeholder
                final_contents.append(text_content[last_index:match.start()])
                
                # Extract path and append the actual image object
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
            # Append any remaining text after the last placeholder
            final_contents.append(text_content[last_index:])

        # Assemble the final list of parts for the Gemini API
        final_contents.append(self.config.system_prompt)
        parse_and_append_parts(processed_header_str)
        parse_and_append_parts(processed_io_buffer_str)
        parse_and_append_parts(processed_footer_str)
        final_contents.append(self.last_received_system_hint)

        return final_contents


    def _initiate_cognition_cycle(self):
        try:
            rospy.loginfo("--- Starting Cognition Cycle ---")
            
            self.context_results.clear()
            self.context_gathering_complete.clear()

            header_to_run, footer_to_run = self.context.get_snippets_to_execute()
            snippets_to_run = header_to_run + footer_to_run
            self.context_requests_pending = len(snippets_to_run)

            if self.context_requests_pending > 0:
                rospy.loginfo(f"Requesting {self.context_requests_pending} context snippets...")
                for snippet in snippets_to_run:
                    out_msg = CognitionOutput(
                        type='context',
                        content=f"<py>{snippet['code']}</py>",
                        filename=snippet['name']
                    )
                    self.output_pub.publish(out_msg)
                
                completed = self.context_gathering_complete.wait(timeout=15.0)
                if not completed:
                    rospy.logwarn("Timed out waiting for context snippets. Proceeding with what was received.")
            
            with self.state_lock:
                self.state = CognitionState.AWAITING_RESPONSE
                rospy.loginfo("State transition to AWAITING_RESPONSE. Assembling prompt.")
            
            header_snippets_data = []
            for s in self.context.header_snippets:
                content = self.context_results.get(s['name'], self.context.cached_output.get(s.get('name')))
                if content is not None:
                    header_snippets_data.append({'config': s, 'content': content})

            footer_snippets_data = []
            for s in self.context.footer_snippets:
                content = self.context_results.get(s['name'], self.context.cached_output.get(s.get('name')))
                if content is not None:
                    footer_snippets_data.append({'config': s, 'content': content})

            final_contents = self._construct_prompt_and_images(header_snippets_data, footer_snippets_data)
        
            cfg = self.config.framework['main_model']

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

            tk_cfg = cfg.get('thinking_config', {})
            thinking_config = types.ThinkingConfig(
                thinking_budget=tk_cfg.get('thinking_budget', -1),
                include_thoughts=tk_cfg.get('include_thoughts', False),
            )
            max_tokens = cfg.get('max_output_tokens', 8192)
            mr_str = cfg.get('media_resolution', 'MEDIA_RESOLUTION_UNSPECIFIED')
            media_res = getattr(
                types.MediaResolution,
                mr_str,
                types.MediaResolution.MEDIA_RESOLUTION_UNSPECIFIED,
            )
            gen_config = types.GenerateContentConfig(
                safety_settings=safety_settings,
                thinking_config=thinking_config,
                temperature=cfg.get('temperature', 0.7),
                stop_sequences=cfg.get('stop_sequences', []),                
                max_output_tokens=max_tokens,
                media_resolution=media_res,
            )

            try:
                rospy.loginfo("Calling Gemini API...")
                stream = self.genai_client.models.generate_content_stream(
                    model=cfg['model'],
                    contents=final_contents,
                    config=gen_config,
                )

                complete_response_text = ""
                for chunk in stream:
                    if not chunk.candidates: continue
                    candidate = chunk.candidates[0]
                    if not candidate or not candidate.content: continue

                    for part in candidate.content.parts:
                        text = getattr(part, 'text', None)
                        if not text: continue

                        if getattr(part, "thought", False):
                            self.output_pub.publish(
                                CognitionOutput(type='thoughts', content=text)
                            )
                        else:
                            self.output_pub.publish(
                                CognitionOutput(type='chunk', content=text)
                            )
                            complete_response_text += text

                if 'chunk' in locals() and getattr(chunk, 'usage_metadata', None):
                    md = chunk.usage_metadata
                    rospy.loginfo(
                        f"Token usage — prompt: {getattr(md, 'prompt_token_count', 'n/a')}, "
                        f"thoughts: {getattr(md, 'thoughts_token_count', 'n/a')}, "
                        f"response: {getattr(md, 'candidates_token_count', 'n/a')}, "
                        f"total: {getattr(md, 'total_token_count', 'n/a')}"
                    )

            except Exception as e:
                rospy.logerr(f"Gemini API call failed: {e}")
                complete_response_text = f"<py>\n# API Error: {e}\nloop_cognition=False\n</py>"

            rospy.loginfo("API stream finished.")
            new_msg_id = self.io.append_message(msg_type='me', content=complete_response_text)
            final_output = CognitionOutput(type='llm', content=complete_response_text, filename=new_msg_id)
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