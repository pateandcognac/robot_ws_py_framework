# file: ~/robot_ws/src/logos_framework/scripts/cognition_lib/io_manager.py

import rospy
import os
import json
import time
import threading
import string
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
PY_ASYNC_MERGE_WINDOW_SEC = 10 * 60

# Epoch for photo IDs: Feb 8, 1977
EPOCH = datetime(1977, 2, 8, tzinfo=timezone.utc)

# Module-level state for per-second sequencing
_last_second: Optional[int] = None
_seq_in_second: int = 0


class IOManager:
    """Handles thread-safe reading and writing to the agent's I/O files."""
    def __init__(self, workspace_path: Path, framework_config: dict):
        state_path = workspace_path / "state"
        state_path.mkdir(exist_ok=True)

         # = self.framework_config.get('context', {})

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


    def base36_encode(number: int, min_length: int = 4) -> str:
        """
        Encode a non-negative integer as a zero-padded base36 string.

        Args:
            number: The integer to encode. Must be >= 0.
            min_length: Minimum length of the returned string, left-padded
                with '0' characters.

        Returns:
            A lowercase base36 string, at least `min_length` characters long.
            Lexicographic sort == numeric sort when strings are same length.
        """
        if number < 0:
            raise ValueError("Cannot encode negative numbers")
        if number == 0:
            return "0" * min_length

        chars = []
        while number:
            chars.append(ALPHABET[number % 36])
            number //= 36
        result = "".join(reversed(chars))
        return result.rjust(min_length, "0")


    def make_time_id(prefix: str = "", now: Optional[datetime] = None) -> str:
        """
        Generate a time-based, 7-character, base36 ID with optional prefix, e.g. "sum-", "msg-"

        Format: 6 chars of seconds-since-epoch (base36) + 1 char burst sequence.
        Lexicographic sort == chronological sort, including bursts within the
        same second.

        Args:
            now: Optional datetime for testing. Defaults to UTC now.

        Returns:
            A 7-character string like "0a3f2x0".

        Note to self:
            6 base36 chars covers ~69 years from the 2025-01-01 epoch.
            The burst digit supports up to 36 captures per second before
            wrapping. More than enough for our camera cadence.
        """
        global _last_second, _seq_in_second

        if now is None:
            now = datetime.now(timezone.utc)

        delta = now - EPOCH
        second = int(delta.total_seconds())

        # Update per-second sequence counter
        if _last_second is None or second != _last_second:
            _last_second = second
            _seq_in_second = 0
        else:
            _seq_in_second = (_seq_in_second + 1) % len(ALPHABET)

        ts_part = IOManager.base36_encode(second, min_length=6)
        seq_part = ALPHABET[_seq_in_second]

        return f"{prefix}{ts_part}{seq_part}"



    def _truncate_for_buffer(self, msg_id: str, content: str) -> str:
        if len(content) <= self.max_chars:
            return content

        chars_removed = len(content) - (self.keep_head + self.keep_tail)
        rospy.logwarn(f"Message {msg_id} content is too long ({len(content)} chars). Truncating by {chars_removed} chars for io_buffer.")

        head = content[:self.keep_head]
        tail = content[-self.keep_tail:]
        truncation_notice = f"\n\n... [content truncated - {chars_removed} chars removed. Full content of message {msg_id} can be found in io_history.jsonl] ...\n\n"

        return head + truncation_notice + tail

    def _try_merge_py_async_buffer_message(self, message_data: dict) -> Optional[str]:
        if message_data.get("type") != "py_async" or not self.buffer_file.exists():
            return None

        with open(self.buffer_file, 'r') as f:
            lines = f.readlines()

        if not lines:
            return None

        last_index = None
        last_message = None
        for i in range(len(lines) - 1, -1, -1):
            if not lines[i].strip():
                continue
            last_index = i
            try:
                last_message = json.loads(lines[i])
            except json.JSONDecodeError:
                return None
            break

        if last_message is None or last_message.get("type") != "py_async":
            return None

        last_timestamp = last_message.get("timestamp", 0)
        current_timestamp = message_data["timestamp"]
        if current_timestamp - last_timestamp > PY_ASYNC_MERGE_WINDOW_SEC:
            return None

        merged_content = "\n\n".join(
            part for part in [
                last_message.get("content", "").rstrip(),
                message_data.get("content", "").lstrip(),
            ]
            if part
        )
        last_message["content"] = self._truncate_for_buffer(last_message.get("id", message_data["id"]), merged_content)
        last_message["timestamp"] = current_timestamp
        last_message["token_count"] = last_message.get("token_count", 0) + message_data.get("token_count", 0)
        if message_data.get("filename"):
            last_message["filename"] = message_data["filename"]

        lines[last_index] = json.dumps(last_message) + '\n'
        with open(self.buffer_file, 'w') as f:
            f.writelines(lines)

        return last_message.get("id")

    def append_message(self, msg_type: str, content: str, filename: str = None):
        with self._lock:
            msg_id = f"msg-{IOManager.make_time_id()}"
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

                merged_msg_id = self._try_merge_py_async_buffer_message(message_data)
                if merged_msg_id:
                    rospy.loginfo(f"IOManager: Merged py_async message {msg_id} into {merged_msg_id}.")
                    return merged_msg_id

                # --- BUFFER file gets potentially truncated content ---
                buffer_content = self._truncate_for_buffer(msg_id, content)
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
