#!/usr/bin/env python3
import rospy
import os
import re
import json
import time
import threading
import string # NEW
from pathlib import Path
from collections import deque
from ruamel.yaml import YAML
from enum import Enum # Import Enum

# Gemini API and Image Handling
from google import genai
from google.genai import types
import PIL.Image

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
                    self.header_snippets = [] # MODIFIED: Ensure list is empty if file disappears
                    rospy.logwarn(f"ContextManager: Header config not found at {self.header_config_path}")

                if self.footer_config_path.exists():
                    with open(self.footer_config_path, 'r') as f:
                        self.footer_snippets = yaml.load(f) or []
                else:
                    self.footer_snippets = [] # MODIFIED: Ensure list is empty if file disappears
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
            # MODIFIED: Reload configs at the start of every check to ensure they are fresh.
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
                
                # If a cached snippet's TTL just reached zero, invalidate its cache.
                if new_ttl == 0 and s.get('name') in self.cached_output:
                    rospy.loginfo(f"ContextManager: Invalidating cache for EOL snippet '{s['name']}'.")
                    del self.cached_output[s['name']]
                    configs_changed = True # While not a config change, it's a state change worth noting.


                # Removal logic
                if s['ttl'] == 0 and self.config.get('remove_header_at_eol', False):
                    rospy.loginfo(f"ContextManager: Removing EOL header snippet '{s['name']}'.")
                    configs_changed = True
                    continue # Skip appending it to the updated list
                updated_header_snippets.append(s)
            self.header_snippets = updated_header_snippets

            # Process footer snippets
            updated_footer_snippets = []
            for s in self.footer_snippets:
                new_ttl, changed = self._get_updated_ttl(s)
                s['ttl'] = new_ttl
                if changed:
                    configs_changed = True

                # Removal logic
                if s['ttl'] == 0 and self.config.get('remove_footer_at_eol', False):
                    rospy.loginfo(f"ContextManager: Removing EOL footer snippet '{s['name']}'.")
                    configs_changed = True
                    continue
                updated_footer_snippets.append(s)
            self.footer_snippets = updated_footer_snippets

            # If any TTLs were changed or snippets removed, write back to the file
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
        
        # Pinned snippets don't have their TTLs changed
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
            with open(my_config_path, 'r') as f: self.my_config = yaml.load(f)
        
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
    def __init__(self, workspace_path: Path, framework_config: dict): # MODIFIED
        state_path = workspace_path / "state"
        state_path.mkdir(exist_ok=True)
        self.history_file = state_path / "io_history.jsonl"
        self.buffer_file = state_path / "io_buffer.jsonl"
        self._lock = threading.Lock()
        # NEW: Store config for token estimation
        self.framework_config = framework_config
        self.id_counter = 0
        self._initialize_id_counter()

    # NEW: Helper function for base36 encoding
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

    # NEW: Initialize the message ID counter from the history file
    def _initialize_id_counter(self):
        """Reads the last message ID from the history file to set the counter."""
        with self._lock:
            if not self.history_file.exists():
                self.id_counter = 0
                rospy.loginfo("IOManager: History file not found. Starting message ID from 0.")
                return

            try:
                with open(self.history_file, 'rb') as f:
                    # Seek to the end, then back to find the last newline
                    f.seek(0, os.SEEK_END)
                    if f.tell() == 0: # File is empty
                        self.id_counter = 0
                        return

                    f.seek(-2, os.SEEK_END) # Go back 2 bytes to get past the last \n
                    while f.read(1) != b'\n':
                        if f.tell() < 3: # at the beginning of the file
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

    def append_message(self, msg_type: str, content: str, filename: str = None): # MODIFIED
        with self._lock:
            # MODIFIED: Use sequential base36 ID
            msg_id = f"msg-{self._base36_encode(self.id_counter)}"
            self.id_counter += 1

            # NEW: Calculate token count
            divisor = self.framework_config.get('context', {}).get('token_estimation_divisor', 5)
            token_count = len(content) // divisor

            message_data = {
                "id": msg_id,
                "type": msg_type,
                "timestamp": time.time(),
                "token_count": token_count, # NEW
                "content": content
            }
            if filename: # NEW
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
        
        # MODIFIED: Pass framework config to IOManager
        self.io = IOManager(self.workspace_path, self.config.framework)

        self.context = ContextManager(self.workspace_path, self.config.framework['context'])

        # Gemini API Setup
        try:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY environment variable not set.")
            self.genai_client = genai.Client(api_key=api_key)
        except Exception as e:
            rospy.logfatal(f"Failed to configure Gemini API: {e}. Shutting down.")
            rospy.signal_shutdown("Gemini API configuration failed.")
            return


        # State management
        self.state = CognitionState.IDLE
        self.state_lock = threading.Lock()

        self.incoming_queue = deque()
        self.queue_lock = threading.Lock()
        self.last_received_system_hint = ""

        # Context gathering synchronization
        self.context_results = {}
        self.context_requests_pending = 0
        self.context_gathering_complete = threading.Event()

        # Publishers and Subscribers
        self.output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)
        self.input_sub = rospy.Subscriber('/cognition/input', CognitionInput, self._input_callback, queue_size=10)
        self.processing_timer = rospy.Timer(rospy.Duration(0.25), self._process_queue)
        rospy.loginfo("Cognition Node: Ready and waiting for input.")

    def _input_callback(self, msg: CognitionInput):
        # MODIFIED: Use the new 'filename' field for context snippet identification
        if msg.type == 'context' and self.state in [CognitionState.GATHERING_CONTEXT, CognitionState.AWAITING_RESPONSE]:
            snippet_name = msg.filename
            if snippet_name:
                # Store the full output, but also add to the manager's cache if it's a cacheable snippet
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
        if self.state == CognitionState.AWAITING_RESPONSE:
            return

        with self.queue_lock:
            if not self.incoming_queue:
                return
            batch = list(self.incoming_queue); self.incoming_queue.clear()
        
        rospy.loginfo(f"Processing batch of {len(batch)} messages in state {self.state.name}.")
        should_start_cognition = False
        for msg in batch:
            # Safely get the filename attribute, defaulting to None if it doesn't exist
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


    def _construct_prompt_and_images(self, header_snippets_data, footer_snippets_data):
            """Builds the final prompt string with verbose formatting and separates image parts."""
            cfg = self.config.framework['context']
            prompt_parts = [self.config.system_prompt]

            # 1. Add Header
            if header_snippets_data:
                header_str = ""
                total_tokens = 0
                for item in header_snippets_data:
                    snippet_name = item['config'].get('name', 'unnamed')
                    content = item['content']
                    ttl = item['config'].get('ttl', 0)
                    
                    # Estimate tokens for this snippet
                    token_count = len(content) // cfg.get('token_estimation_divisor', 5)
                    total_tokens += token_count

                    # Format snippet tag with optional TTL
                    if cfg.get('show_snippet_ttl', False):
                        header_str += f'<{snippet_name} ttl="{ttl}">\n{content}\n</{snippet_name}>\n'
                    else:
                        header_str += f'<{snippet_name}>\n{content}\n</{snippet_name}>\n'
                
                # Format main header tag with optional stats
                header_name = cfg.get('header_name', 'header')
                if cfg.get('show_header_stats', False):
                    prompt_parts.append(f'<{header_name} snippets="{len(header_snippets_data)}" tokens="{total_tokens}">\n{header_str.strip()}\n</{header_name}>')
                else:
                    prompt_parts.append(f'<{header_name}>\n{header_str.strip()}\n</{header_name}>')

            # 2. Add IO Buffer
            io_buffer_str = ""
            buffer_messages = self.io.read_buffer()
            buffer_total_tokens = 0
            for i, msg in enumerate(buffer_messages):
                msg_type = msg.get("type", "unknown")
                msg_id = msg.get("id", "no-id")
                content = msg.get("content", "")
                token_count = msg.get("token_count", 0)
                buffer_total_tokens += token_count

                if msg_type == 'system':
                    io_buffer_str += f'<!-- <{msg_type}>: {content} -->\n'
                else:
                    # Format cell tag with optional stats
                    if cfg.get('show_io_cell_stats', False):
                        io_buffer_str += f'<{msg_type} cell="{i}" id="{msg_id}" tokens="{token_count}">\n{content}\n</{msg_type}>\n'
                    else:
                        io_buffer_str += f'<{msg_type}>\n{content}\n</{msg_type}>\n'
            
            # Format main io_buffer tag with optional stats
            if cfg.get('show_io_buffer_stats', False):
                prompt_parts.append(f'<io_buffer cells="{len(buffer_messages)}" tokens="{buffer_total_tokens}">\n{io_buffer_str.strip()}\n</io_buffer>')
            else:
                prompt_parts.append(f'<io_buffer>\n{io_buffer_str.strip()}\n</io_buffer>')

            # 3. Add Footer (similar logic to header)
            if footer_snippets_data:
                footer_str = ""
                total_tokens = 0
                for item in footer_snippets_data:
                    snippet_name = item['config'].get('name', 'unnamed')
                    content = item['content']
                    ttl = item['config'].get('ttl', 0)
                    token_count = len(content) // cfg.get('token_estimation_divisor', 5)
                    total_tokens += token_count
                    if cfg.get('show_snippet_ttl', False):
                        footer_str += f'<{snippet_name} ttl="{ttl}">\n{content}\n</{snippet_name}>\n'
                    else:
                        footer_str += f'<{snippet_name}>\n{content}\n</{snippet_name}>\n'
                
                footer_name = cfg.get('footer_name', 'footer')
                if cfg.get('show_footer_stats', False):
                    prompt_parts.append(f'<{footer_name} snippets="{len(footer_snippets_data)}" tokens="{total_tokens}">\n{footer_str.strip()}\n</{footer_name}>')
                else:
                    prompt_parts.append(f'<{footer_name}>\n{footer_str.strip()}\n</{footer_name}>')

            # 4. Add any system hint received with the latest input message
            if self.last_received_system_hint:
                prompt_parts.append(self.last_received_system_hint)

            # Join all text parts of the prompt
            prompt = "\n".join(prompt_parts)

            # Image parsing and content construction
            final_contents = []
            last_index = 0
            image_pattern = re.compile(r'<file\s+path="([^"]+)"[^>]*>')
            for match in image_pattern.finditer(prompt):
                final_contents.append(prompt[last_index:match.start()])
                image_path = self.workspace_path / match.group(1)
                try:
                    img = PIL.Image.open(image_path)
                    final_contents.append(img)
                    rospy.loginfo(f"Embedding image: {image_path}")
                except FileNotFoundError:
                    rospy.logerr(f"Image file not found: {image_path}")
                    final_contents.append(f"[ERROR: Image at {image_path} not found]")
                except Exception as e:
                    rospy.logerr(f"Failed to load image {image_path}: {e}")
                    final_contents.append(f"[ERROR: Could not load image at {image_path}]")
                last_index = match.end()
            
            final_contents.append(prompt[last_index:])
            return final_contents

    def _initiate_cognition_cycle(self):
        try:
            rospy.loginfo("--- Starting Cognition Cycle ---")
            
            # 1. GATHER CONTEXT
            self.context_results.clear()
            self.context_gathering_complete.clear()

            # Let the manager determine what needs to run
            header_to_run, footer_to_run = self.context.get_snippets_to_execute()
            snippets_to_run = header_to_run + footer_to_run
            self.context_requests_pending = len(snippets_to_run)

            if self.context_requests_pending > 0:
                rospy.loginfo(f"Requesting {self.context_requests_pending} context snippets...")
                for snippet in snippets_to_run:
                    # We pass the snippet name in the new 'filename' field.
                    out_msg = CognitionOutput(
                        type='context',
                        content=f"<py>{snippet['code']}</py>",
                        filename=snippet['name'] # NEW
                    )
                    self.output_pub.publish(out_msg)
                
                completed = self.context_gathering_complete.wait(timeout=15.0)
                if not completed:
                    rospy.logwarn("Timed out waiting for context snippets. Proceeding with what was received.")
            
            with self.state_lock:
                self.state = CognitionState.AWAITING_RESPONSE
                rospy.loginfo("State transition to AWAITING_RESPONSE. Assembling prompt.")
            
            # Assemble structured data from results and cache for formatting
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

            # 2. ASSEMBLE PROMPT AND IMAGES
            final_contents = self._construct_prompt_and_images(header_snippets_data, footer_snippets_data)
        
            # 3. CALL GEMINI API
            # VERY IMPORTANT: DO NOT CHANGE THIS API CONSTRUCT
            # **It is accurate to the latest Gemini SDK**
            cfg = self.config.framework['main_model']

            # Safety: disable filtering 
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

            # Thinking config from your framework_config.json
            tk_cfg = cfg.get('thinking_config', {})
            thinking_config = types.ThinkingConfig(
                thinking_budget=tk_cfg.get('thinking_budget', -1),
                include_thoughts=tk_cfg.get('include_thoughts', False),
            )

            max_tokens = cfg.get('max_output_tokens', 8192)  # pick your default
            mr_str = cfg.get('media_resolution', 'MEDIA_RESOLUTION_UNSPECIFIED')

            # Map string -> enum (SDK expects the enum)
            media_res = getattr(
                types.MediaResolution,
                mr_str,
                types.MediaResolution.MEDIA_RESOLUTION_UNSPECIFIED,
            )

            # Build generation config once
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
                    contents=final_contents,  # strings + PIL.Image objects is OK
                    config=gen_config,
                )

                complete_response_text = ""
                for chunk in stream:
                    # Streamed chunks come as candidates -> content -> parts
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
                            self.output_pub.publish(
                                CognitionOutput(type='thoughts', content=text)
                            )
                        else:
                            self.output_pub.publish(
                                CognitionOutput(type='chunk', content=text)
                            )
                            complete_response_text += text

                # Optionally: usage metadata is on the last chunk
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

            # 4. PROCESS RESPONSE
            rospy.loginfo("API stream finished.")
            # MODIFIED: Get the new message ID to use as the filename for the python worker
            new_msg_id = self.io.append_message(msg_type='me', content=complete_response_text)
            final_output = CognitionOutput(type='llm', content=complete_response_text, filename=new_msg_id)
            self.output_pub.publish(final_output)
        
        finally:
            # 5. UPDATE AND SAVE CONTEXT CONFIGS
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