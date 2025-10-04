#!/usr/bin/env python3
import rospy
import os
import re
import json
import time
import threading
from pathlib import Path
from collections import deque
from ruamel.yaml import YAML
from enum import Enum # Import Enum

# Gemini API and Image Handling
import google.genai as genai
# from google.generativeai.types import HarmCategory, HarmBlockThreshold
import PIL.Image

from logos_framework.msg import CognitionInput, CognitionOutput
from logos_framework.modules.context_manager import ContextManager

yaml = YAML(typ='safe')

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
            # TODO: replace in system prompt: {{header_name}}, {{footer_name}}, {{workspace_path}}, {{global_max_io_buffer_media}}... what else?

            my_config_path = self.workspace_path / "state" / "my_config.yaml"
            with open(my_config_path, 'r') as f: self.my_config = yaml.load(f)
            
            
            rospy.loginfo("ConfigManager: All configurations loaded successfully.")
            return True
        except Exception as e:
            rospy.logerr(f"ConfigManager: Error loading configurations: {e}")
            return False

class IOManager:
    """Handles thread-safe reading and writing to the agent's I/O files."""
    def __init__(self, workspace_path: Path):
        state_path = workspace_path / "state"
        state_path.mkdir(exist_ok=True)
        self.history_file = state_path / "io_history.jsonl"
        self.buffer_file = state_path / "io_buffer.jsonl"
        self._lock = threading.Lock()

    def append_message(self, msg_type: str, content: str):
        with self._lock:
            msg_id = f"msg-{int(time.time()*1000):x}"
            message_data = {"id": msg_id, "type": msg_type, "timestamp": time.time(), "content": content}
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
        self.io = IOManager(self.workspace_path)
        if not self.config.load_configs():
            rospy.signal_shutdown("Failed to load critical configurations.")
            return

        self.context = ContextManager(self.workspace_path, self.config.framework['context'])

        # Gemini API Client
        try:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY environment variable not set.")
            genai.configure(api_key=api_key)
            self.genai_client = genai.GenerativeModel(self.config.framework['main_model']['model'])
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
        # This logic remains the same for now, but we could improve it with a meta field later
        if msg.type == 'context' and self.state in [CognitionState.GATHERING_CONTEXT, CognitionState.AWAITING_RESPONSE]:
            # Regex to find the snippet name we embedded in the code
            match = re.search(r'<snippet name="([^"]+)"', msg.content)
            if match:
                snippet_name = match.group(1)
                
                # Store the full output, but also add to the manager's cache if it's a cacheable snippet
                self.context_results[snippet_name] = msg.content
                snippet_config = next((s for s in self.context.header_snippets + self.context.footer_snippets if s['name'] == snippet_name), None)
                if snippet_config and snippet_config.get('ttl', 0) < 0:
                    self.context.cached_output[snippet_name] = msg.content
                    rospy.loginfo(f"Cached output for '{snippet_name}'.")

                self.context_requests_pending -= 1
                if self.context_requests_pending <= 0:
                    self.context_gathering_complete.set()
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
            self.io.append_message(msg_type=msg.type, content=msg.content)
            if msg.loop_cognition:
                should_start_cognition = True
            
            default_system_hint = "\n\n<me>"
            if msg.system_hint:
                self.last_received_system_hint = msg.system_hint + "\n" + default_system_hint
            else:
                self.last_received_system_hint = default_system_hint

        with self.state_lock:
            if should_start_cognition and self.state == CognitionState.IDLE:
                self.state = CognitionState.GATHERING_CONTEXT
                rospy.loginfo("State transition to GATHERING_CONTEXT. Starting cognition cycle.")
                threading.Thread(target=self._initiate_cognition_cycle).start()


    def _construct_prompt_and_images(self, header_content, footer_content):
        """Builds the final prompt string and separates image parts."""
        prompt = self.config.system_prompt

        # Add Header
        if header_content:
            prompt += f"\n<{self.config.framework['context']['header_name']}>\n{header_content}\n</{self.config.framework['context']['header_name']}>"
        
        # Add IO Buffer
        # TODO: add estimated token count & verbose formatting from config
        io_buffer_str = ""
        buffer_messages = self.io.read_buffer()
        for i, msg in enumerate(buffer_messages):
            io_buffer_str += f'<{msg["type"]} cell="{i}" id="{msg["id"]}">\n{msg["content"]}\n</{msg["type"]}>\n'
        prompt += f"\n<io_buffer>\n{io_buffer_str}</io_buffer>"

        # Add Footer
        if footer_content:
            prompt += f"\n<{self.config.framework['context']['footer_name']}>\n{footer_content}\n</{self.config.framework['context']['footer_name']}>"

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
            
            # 1. GATHER CONTEXT - NOW USING CONTEXTMANAGER
            self.context_results.clear()
            self.context_gathering_complete.clear()

            # Let the manager determine what needs to run
            header_to_run, footer_to_run = self.context.get_snippets_to_execute()
            snippets_to_run = header_to_run + footer_to_run
            self.context_requests_pending = len(snippets_to_run)

            if self.context_requests_pending > 0:
                rospy.loginfo(f"Requesting {self.context_requests_pending} context snippets...")
                for snippet in snippets_to_run:
                    # Temporary workaround: embed name in code for result identification
                    code_with_meta = f"print('<snippet name=\"{snippet['name']}\" ttl=\"{snippet['ttl']}\">')\n{snippet['code']}\nprint('</snippet>')"
                    out_msg = CognitionOutput(type='context', content=f"<py>{code_with_meta}</py>")
                    self.output_pub.publish(out_msg)
                
                completed = self.context_gathering_complete.wait(timeout=15.0)
                if not completed:
                    rospy.logwarn("Timed out waiting for context snippets. Proceeding with what was received.")
            
            with self.state_lock:
                self.state = CognitionState.AWAITING_RESPONSE
                rospy.loginfo("State transition to AWAITING_RESPONSE. Assembling prompt.")
            
            # Assemble content from results and cache
            header_content = "".join(
                self.context_results.get(s['name'], self.context.cached_output.get(s['name'], ''))
                for s in self.context.header_snippets
            )
            footer_content = "".join(
                self.context_results.get(s['name'], self.context.cached_output.get(s['name'], ''))
                for s in self.context.footer_snippets
            )

            # 2. ASSEMBLE PROMPT AND IMAGES
            final_contents = self._construct_prompt_and_images(header_content, footer_content)
        
            # 3. CALL GEMINI API
            cfg = self.config.framework['main_model']
            safety_settings = {category: HarmBlockThreshold.BLOCK_NONE for category in HarmCategory}
            
            try:
                rospy.loginfo("Calling Gemini API...")
                stream = self.genai_client.generate_content(
                    contents=final_contents,
                    generation_config=genai.types.GenerationConfig(**cfg),
                    safety_settings=safety_settings,
                    stream=True
                )
                
                complete_response_text = ""
                for chunk in stream:
                    for part in chunk.parts:
                        if part.text:
                            # This logic for thoughts/chunks is a placeholder for actual API features
                            if hasattr(part, 'thought') and part.thought:
                                self.output_pub.publish(CognitionOutput(type='thoughts', content=part.text))
                            else:
                                self.output_pub.publish(CognitionOutput(type='chunk', content=part.text))
                                complete_response_text += part.text
            except Exception as e:
                rospy.logerr(f"Gemini API call failed: {e}")
                complete_response_text = f"<py>\n# API Error: {e}\nloop_cognition=False\n</py>"

            # 4. PROCESS RESPONSE
            rospy.loginfo("API stream finished.")
            self.io.append_message(msg_type='me', content=complete_response_text)
            final_output = CognitionOutput(type='llm', content=complete_response_text)
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