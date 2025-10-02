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

# Gemini API and Image Handling
import google.genai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import PIL.Image

from logos_framework.msg import CognitionInput, CognitionOutput

yaml = YAML(typ='safe')

class ConfigManager:
    """Handles loading and accessing all agent configuration files."""
    def __init__(self, workspace_path: Path):
        self.workspace_path = workspace_path
        self.framework = {}
        self.system_prompt = ""
        self.my_config = {}
        self.header_snippets = []
        self.footer_snippets = []

    def load_configs(self):
        rospy.loginfo("ConfigManager: Loading configurations...")
        try:
            framework_path = self.workspace_path / ".system" / "framework_config.json"
            prompt_path = self.workspace_path / ".system" / "system_prompt.txt"
            with open(framework_path, 'r') as f: self.framework = json.load(f)
            with open(prompt_path, 'r') as f: self.system_prompt = f.read()
            # TODO: replace in system prompt: {{header_name}}, {{footer_name}}, {{workspace_path}}, {{global_max_io_buffer_media}}... what else?

            my_config_path = self.workspace_path / "state" / "my_config.yaml"
            header_cfg_path = self.workspace_path / "state" / f"{self.framework['context']['header_name']}_config.yaml"
            footer_cfg_path = self.workspace_path / "state" / f"{self.framework['context']['footer_name']}_config.yaml"
            with open(my_config_path, 'r') as f: self.my_config = yaml.load(f)

            if header_cfg_path.exists():
                with open(header_cfg_path, 'r') as f: self.header_snippets = yaml.load(f) or []
            if footer_cfg_path.exists():
                with open(footer_cfg_path, 'r') as f: self.footer_snippets = yaml.load(f) or []

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
        self.api_in_progress = False
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
        # If this is a result from a context request, handle it specially
        if msg.type == 'context' and self.api_in_progress:
            # The name of the snippet is embedded in the content for now
            # A better approach would be adding a 'request_id' to the messages
            match = re.search(r'<snippet name="([^"]+)"', msg.content)
            if match:
                snippet_name = match.group(1)
                self.context_results[snippet_name] = msg.content
                self.context_requests_pending -= 1
                if self.context_requests_pending <= 0:
                    self.context_gathering_complete.set()
            return

        with self.queue_lock:
            self.incoming_queue.append(msg)

    def _process_queue(self, event=None):
        if self.api_in_progress or not self.incoming_queue:
            return
        with self.queue_lock:
            batch = list(self.incoming_queue); self.incoming_queue.clear()
        if not batch: return

        rospy.loginfo(f"Processing batch of {len(batch)} messages.")
        should_start_cognition = False
        for msg in batch:
            self.io.append_message(msg_type=msg.type, content=msg.content)
            if msg.loop_cognition: should_start_cognition = True
            default_system_hint = "<!-- <system>: Ready for Logos's reply. -->\n\n<me>"
            if msg.system_hint:
                self.last_received_system_hint = msg.system_hint + "\n" + default_system_hint
            else:
                self.last_received_system_hint = default_system_hint


        if should_start_cognition:
            # Run the cycle in a new thread to avoid blocking the ROS callbacks
            threading.Thread(target=self._initiate_cognition_cycle).start()

    def _construct_prompt_and_images(self, header_content, footer_content):
        """Builds the final prompt string and separates image parts."""
        prompt = self.config.system_prompt

        # Add Header
        if header_content:
            prompt += f"\n<{self.config.framework['context']['header_name']}>\n{header_content}\n</{self.config.framework['context']['header_name']}>"
        
        # Add IO Buffer
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
            # Add text part before the image
            final_contents.append(prompt[last_index:match.start()])
            
            # Add the image part
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
        
        # Add the remaining text part
        final_contents.append(prompt[last_index:])
        return final_contents

    def _initiate_cognition_cycle(self):
        self.api_in_progress = True
        rospy.loginfo("--- Starting Cognition Cycle ---")
        
        # 1. GATHER CONTEXT
        self.context_results.clear()
        self.context_gathering_complete.clear()
        all_snippets = self.config.header_snippets + self.config.footer_snippets
        self.context_requests_pending = len(all_snippets)

        if self.context_requests_pending > 0:
            rospy.loginfo(f"Requesting {self.context_requests_pending} context snippets...")
            for snippet in all_snippets:
                # We embed the name in the code so we can identify the result
                # This is a temporary workaround. A proper request_id would be better.
                code_with_meta = f"print('<snippet name=\"{snippet['name']}\" ttl=\"{snippet['ttl']}\">')\n{snippet['code']}\nprint('</snippet>')"
                out_msg = CognitionOutput(type='context', content=f"<py>{code_with_meta}</py>")
                self.output_pub.publish(out_msg)
            
            # Wait for all context results to return, with a timeout
            completed = self.context_gathering_complete.wait(timeout=15.0)
            if not completed:
                rospy.logwarn("Timed out waiting for context snippets. Proceeding with what was received.")
        
        header_content = "".join(self.context_results.get(s['name'], '') for s in self.config.header_snippets)
        footer_content = "".join(self.context_results.get(s['name'], '') for s in self.config.footer_snippets)

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
        response_msg_id = self.io.append_message(msg_type='me', content=complete_response_text)
        final_output = CognitionOutput(type='llm', content=complete_response_text)
        # TODO: Add msg_id/meta to CognitionOutput message
        self.output_pub.publish(final_output)
        
        rospy.loginfo("--- Cognition Cycle Finished ---")
        self.api_in_progress = False
        self.last_received_system_hint = ""

    def run(self):
        rospy.spin()

if __name__ == '__main__':
    try:
        node = CognitionNode()
        node.run()
    except rospy.ROSInterruptException:
        pass