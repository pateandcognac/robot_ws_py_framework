#!/usr/bin/env python3
# file: ~/robot_ws/src/logos_framework/scripts/web_ui_node.py

import rospy
import threading
import json
import os
import base64
import io
import time
from collections import deque
from flask import Flask, jsonify, render_template, send_from_directory
from flask_socketio import SocketIO

from std_msgs.msg import String as StringMsg
from sensor_msgs.msg import CompressedImage, Image
from logos_framework.msg import CognitionInput, CognitionOutput
from PIL import Image as PILImage

# --- Flask & SocketIO Setup ---
script_dir = os.path.dirname(os.path.realpath(__file__))
web_dir = os.path.abspath(os.path.join(script_dir, '..', 'web'))

app = Flask(__name__, template_folder=web_dir, static_folder=web_dir)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app)

# Global variable to hold workspace path for Flask
WORKSPACE_PATH = ""

def ensure_py_block(content: str) -> str:
    content = content or ""
    if "<py" in content and "</py>" in content:
        return content
    return f"<py>{content}</py>"

# --- ROS Node Class ---
class WebUINode:
    def __init__(self):
        rospy.init_node('web_ui_node')
        rospy.loginfo("Web UI Node: Initializing...")

        # Grab the workspace path so Flask knows where images are
        global WORKSPACE_PATH
        workspace_param = rospy.get_param('~workspace_path', None)
        if workspace_param:
            WORKSPACE_PATH = os.path.expanduser(workspace_param)
        else:
            rospy.logwarn("~workspace_path parameter not set. Image serving may fail.")

        self.cognition_input_pub = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)
        self.cognition_output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)
        self.runtime_config_pub = rospy.Publisher('/cognition/runtime_config/set', StringMsg, queue_size=5)
        self.latest_runtime_config = {}
        self.debug_vision_prefix = rospy.get_param('~debug_vision_prefix', '/logos/debug_vision/')
        self.debug_vision_poll_interval = float(rospy.get_param('~debug_vision_poll_interval', 2.0))
        self.debug_vision_buffer_size = int(rospy.get_param('~debug_vision_buffer_size', 24))
        self.debug_vision_jpeg_quality = int(rospy.get_param('~debug_vision_jpeg_quality', 82))
        self.web_port = int(rospy.get_param('~web_port', 5000))
        self.debug_vision_lock = threading.RLock()
        self.debug_vision_frames = deque(maxlen=max(1, self.debug_vision_buffer_size))
        self.debug_vision_subscribers = {}
        self.debug_vision_topic_types = {}
        self.debug_vision_warned_encodings = set()

        rospy.Subscriber('/cognition/ui_state', StringMsg, self.ui_state_callback)
        rospy.Subscriber('/cognition/runtime_config/state', StringMsg, self.runtime_config_callback)
        rospy.Subscriber('/cognition/input', CognitionInput, self.cognition_input_callback)
        rospy.Subscriber('/cognition/output', CognitionOutput, self.cognition_output_callback)
        self.debug_vision_timer = rospy.Timer(
            rospy.Duration(max(0.5, self.debug_vision_poll_interval)),
            self.discover_debug_vision_topics
        )

        rospy.loginfo(f"Web UI Node: Ready. UI available at http://localhost:{self.web_port}")

    def ui_state_callback(self, msg):
        try:
            data = json.loads(msg.data)
            socketio.emit('full_update', data)
        except json.JSONDecodeError as e:
            rospy.logerr(f"Failed to decode UI state JSON: {e}")

    def runtime_config_callback(self, msg):
        try:
            self.latest_runtime_config = json.loads(msg.data)
            socketio.emit('runtime_config_state', self.latest_runtime_config)
        except json.JSONDecodeError as e:
            rospy.logerr(f"Failed to decode runtime config JSON: {e}")

    def cognition_input_callback(self, msg: CognitionInput):
        data = {
            'type': msg.type,
            'content': msg.content,
            'filename': msg.filename,
            'system_hint': msg.system_hint
        }
        socketio.emit('append_io', data)

    def cognition_output_callback(self, msg: CognitionOutput):
        if msg.type == 'feedback' or msg.type == 'me': 
            return # 'me' is ignored because we build it via stream chunks
            
        if msg.type == 'thoughts':
            # Send thoughts out as a special transient event!
            socketio.emit('thought_update', {'content': msg.content})
            
        elif msg.type == 'chunk' and msg.content:
            socketio.emit('stream_chunk', {'content': msg.content})
            
        elif msg.type not in ['thoughts', 'chunk']:
            data = {
                'type': msg.type,
                'content': msg.content,
                'filename': msg.filename,
                'system_hint': '' 
            }
            socketio.emit('append_io', data)

    def discover_debug_vision_topics(self, _event=None):
        try:
            topics = rospy.get_published_topics()
        except Exception as e:
            rospy.logwarn_throttle(10.0, f"Debug vision topic discovery failed: {e}")
            return

        for topic, type_name in topics:
            if not topic.startswith(self.debug_vision_prefix):
                continue
            if topic in self.debug_vision_subscribers:
                continue

            msg_class = None
            if type_name == 'sensor_msgs/Image':
                msg_class = Image
            elif type_name == 'sensor_msgs/CompressedImage':
                msg_class = CompressedImage
            else:
                rospy.logwarn_throttle(
                    10.0,
                    f"Skipping debug vision topic {topic}: unsupported type {type_name}"
                )
                continue

            try:
                sub = rospy.Subscriber(
                    topic,
                    msg_class,
                    self.debug_vision_callback,
                    callback_args=topic,
                    queue_size=5
                )
                with self.debug_vision_lock:
                    self.debug_vision_subscribers[topic] = sub
                    self.debug_vision_topic_types[topic] = type_name
                rospy.loginfo(f"Subscribed to debug vision topic {topic} ({type_name})")
            except Exception as e:
                rospy.logwarn(f"Failed to subscribe to debug vision topic {topic}: {e}")

    def debug_vision_callback(self, msg, topic):
        try:
            frame = self.encode_debug_vision_frame(msg, topic)
            if not frame:
                return
            with self.debug_vision_lock:
                self.debug_vision_frames.append(frame)
                payload = self.get_debug_vision_snapshot_locked()
            socketio.emit('debug_vision_update', payload)
        except Exception as e:
            rospy.logwarn(f"Failed to process debug vision frame from {topic}: {e}")

    def encode_debug_vision_frame(self, msg, topic):
        received_sec = time.time()
        header_stamp = ''
        if hasattr(msg, 'header') and msg.header.stamp:
            stamp_sec = msg.header.stamp.to_sec()
            if stamp_sec:
                header_stamp = f"{stamp_sec:.6f}"

        if isinstance(msg, CompressedImage):
            media_type = 'image/jpeg'
            fmt = (msg.format or '').lower()
            if 'png' in fmt:
                media_type = 'image/png'
            image_data = base64.b64encode(bytes(msg.data)).decode('ascii')
            width = 0
            height = 0
        else:
            pil_image = self.ros_image_to_pil(msg, topic)
            if pil_image is None:
                return None
            width, height = pil_image.size
            output = io.BytesIO()
            if pil_image.mode not in ('RGB', 'L'):
                pil_image = pil_image.convert('RGB')
            pil_image.save(output, format='JPEG', quality=self.debug_vision_jpeg_quality, optimize=True)
            media_type = 'image/jpeg'
            image_data = base64.b64encode(output.getvalue()).decode('ascii')

        return {
            'id': f"{received_sec:.6f}:{topic}",
            'topic': topic,
            'name': topic[len(self.debug_vision_prefix):] or topic,
            'received_time': received_sec,
            'header_stamp': header_stamp,
            'width': width,
            'height': height,
            'src': f"data:{media_type};base64,{image_data}",
        }

    def ros_image_to_pil(self, msg, topic):
        encoding = (msg.encoding or '').lower()
        specs = {
            'rgb8': ('RGB', 'RGB', 3),
            'bgr8': ('RGB', 'BGR', 3),
            'rgba8': ('RGBA', 'RGBA', 4),
            'bgra8': ('RGBA', 'BGRA', 4),
            'mono8': ('L', 'L', 1),
            '8uc1': ('L', 'L', 1),
        }

        if encoding in specs:
            mode, raw_mode, channels = specs[encoding]
            row_size = msg.width * channels
            data = self.compact_image_rows(msg.data, msg.height, msg.step, row_size)
            return PILImage.frombytes(mode, (msg.width, msg.height), data, 'raw', raw_mode)

        if encoding in ('mono16', '16uc1'):
            row_size = msg.width * 2
            data = self.compact_image_rows(msg.data, msg.height, msg.step, row_size)
            image = PILImage.frombytes('I;16', (msg.width, msg.height), data)
            extrema = image.getextrema()
            if extrema[1] > extrema[0]:
                scale = 255.0 / float(extrema[1] - extrema[0])
                image = image.point(lambda value: int((value - extrema[0]) * scale))
            return image.convert('L')

        if encoding not in self.debug_vision_warned_encodings:
            self.debug_vision_warned_encodings.add(encoding)
            rospy.logwarn(f"Unsupported debug vision encoding on {topic}: {msg.encoding}")
        return None

    @staticmethod
    def compact_image_rows(data, height, step, row_size):
        raw = bytes(data)
        if step == row_size:
            return raw
        rows = []
        for row in range(height):
            start = row * step
            rows.append(raw[start:start + row_size])
        return b''.join(rows)

    def get_debug_vision_snapshot(self):
        with self.debug_vision_lock:
            return self.get_debug_vision_snapshot_locked()

    def get_debug_vision_snapshot_locked(self):
        return {
            'frames': list(self.debug_vision_frames),
            'topics': sorted(self.debug_vision_subscribers.keys()),
            'prefix': self.debug_vision_prefix,
            'buffer_size': self.debug_vision_buffer_size,
        }

    def run_ros(self):
        rospy.spin()

# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/logs')
def logs():
    return render_template('logs.html')

@app.route('/workspace/<path:filename>')
def serve_workspace_file(filename):
    if not WORKSPACE_PATH:
        return "Workspace path not configured", 404
    return send_from_directory(WORKSPACE_PATH, filename)

def read_workspace_jsonl(filename):
    if not WORKSPACE_PATH:
        return {"name": filename, "available": False, "error": "Workspace path not configured.", "entries": []}

    path = os.path.join(WORKSPACE_PATH, "state", filename)
    if not os.path.exists(path):
        return {"name": filename, "available": False, "error": "File does not exist yet.", "entries": []}

    entries = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                raw = line.rstrip("\n")
                if not raw.strip():
                    continue
                try:
                    parsed = json.loads(raw)
                    entries.append({"line": line_number, "raw": raw, "data": parsed})
                except json.JSONDecodeError as e:
                    entries.append({
                        "line": line_number,
                        "raw": raw,
                        "data": None,
                        "parse_error": str(e),
                    })
    except Exception as e:
        return {"name": filename, "available": False, "error": str(e), "entries": []}

    return {"name": filename, "available": True, "error": "", "entries": entries}

@app.route('/api/state-jsonl')
def state_jsonl():
    return jsonify({
        "workspace_path": WORKSPACE_PATH,
        "files": {
            "io_buffer": read_workspace_jsonl("io_buffer.jsonl"),
            "io_history": read_workspace_jsonl("io_history.jsonl"),
            "summaries": read_workspace_jsonl("summaries.jsonl"),
        },
    })

@app.route('/api/debug-vision')
def debug_vision():
    if 'ros_node' not in globals():
        return jsonify({"frames": [], "topics": [], "prefix": "/logos/debug_vision/", "buffer_size": 0})
    return jsonify(ros_node.get_debug_vision_snapshot())

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(web_dir, path)

# --- SocketIO Event Handlers ---
@socketio.on('connect')
def handle_connect():
    rospy.loginfo('Web client connected.')
    if getattr(ros_node, 'latest_runtime_config', None):
        socketio.emit('runtime_config_state', ros_node.latest_runtime_config)
    if hasattr(ros_node, 'get_debug_vision_snapshot'):
        socketio.emit('debug_vision_update', ros_node.get_debug_vision_snapshot())

@socketio.on('human_input')
def handle_human_input(json_data):
    try:
        mode = json_data.get('mode', 'input')
        if mode == 'output':
            msg = CognitionOutput()
            msg.type = 'debug'
            msg.content = ensure_py_block(json_data.get('content', ''))
            msg.filename = "webui_debug"
            ros_node.cognition_output_pub.publish(msg)
        else:
            msg = CognitionInput()
            msg.type = json_data.get('type', 'human')
            msg.content = json_data.get('content', '')
            msg.system_hint = json_data.get('system_hint', '')
            msg.loop_cognition = bool(json_data.get('loop_cognition', False))
            msg.filename = "webui" 
            ros_node.cognition_input_pub.publish(msg)
    except Exception as e:
        rospy.logerr(f"Error processing human input from web: {e}")

@socketio.on('runtime_config_set')
def handle_runtime_config_set(json_data):
    try:
        allowed_keys = {
            'api_profile',
            'fallback_api_profile',
            'key_failover',
            'model',
            'thinking_level',
            'media_resolution',
            'use_files_api',
        }
        payload = {
            key: value
            for key, value in json_data.items()
            if key in allowed_keys
        }
        ros_node.runtime_config_pub.publish(StringMsg(data=json.dumps(payload)))
    except Exception as e:
        rospy.logerr(f"Error processing runtime config update from web: {e}")

if __name__ == '__main__':
    try:
        ros_node = WebUINode()
        flask_thread = threading.Thread(target=lambda: socketio.run(
            app, host='0.0.0.0', port=ros_node.web_port, allow_unsafe_werkzeug=True))
        flask_thread.daemon = True
        flask_thread.start()
        ros_node.run_ros()
    except rospy.ROSInterruptException:
        pass
