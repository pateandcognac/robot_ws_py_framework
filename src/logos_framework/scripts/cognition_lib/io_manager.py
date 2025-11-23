# file: ~/robot_ws/src/logos_framework/scripts/cognition_lib/io_manager.py

import rospy
import os
import json
import time
import threading
import string
from pathlib import Path

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

        # Load safety limits from config with sane defaults
        limits = self.framework_config.get('io_safety_limits', {})
        self.max_chars = limits.get('max_content_chars', 24000)
        self.keep_head = limits.get('truncate_keep_head', 4000)
        self.keep_tail = limits.get('truncate_keep_tail', 4000)

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
            # Token count should be based on the original, full content
            token_count = len(content) // divisor

            message_data = {
                "id": msg_id,
                "type": msg_type,
                "timestamp": time.time(),
                "token_count": token_count,
                "content": content # Start with the original, full content
            }
            if filename:
                message_data['filename'] = filename

            try:
                # --- HISTORY file always gets the FULL, untruncated content ---
                history_line = json.dumps(message_data) + '\n'
                with open(self.history_file, 'a') as f:
                    f.write(history_line)

                # --- BUFFER file gets potentially truncated content ---
                buffer_content = content
                if len(buffer_content) > self.max_chars:
                    chars_removed = len(buffer_content) - (self.keep_head + self.keep_tail)
                    rospy.logwarn(f"Message {msg_id} content is too long ({len(buffer_content)} chars). Truncating by {chars_removed} chars for io_buffer.")
                    
                    head = buffer_content[:self.keep_head]
                    tail = buffer_content[-self.keep_tail:]
                    truncation_notice = f"\n\n... [content truncated - {chars_removed} chars removed. Full content of message {msg_id} can be found in io_history.jsonl] ...\n\n"
                    
                    buffer_content = head + truncation_notice + tail
                    
                    # Update the message_data dictionary for the buffer file only
                    message_data['content'] = buffer_content

                buffer_line = json.dumps(message_data) + '\n'
                with open(self.buffer_file, 'a') as f:
                    f.write(buffer_line)

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