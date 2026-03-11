#!/usr/bin/env python3
# file: ~/robot_ws/src/logos_framework/scripts/web_ui_node.py

import rospy
import threading
import json
import os
from flask import Flask, render_template, send_from_directory
from flask_socketio import SocketIO

from std_msgs.msg import String as StringMsg
from logos_framework.msg import CognitionInput, CognitionOutput

# --- Flask & SocketIO Setup ---
script_dir = os.path.dirname(os.path.realpath(__file__))
web_dir = os.path.abspath(os.path.join(script_dir, '..', 'web'))

app = Flask(__name__, template_folder=web_dir, static_folder=web_dir)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app)

# Global variable to hold workspace path for Flask
WORKSPACE_PATH = ""

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

        rospy.Subscriber('/cognition/ui_state', StringMsg, self.ui_state_callback)
        rospy.Subscriber('/cognition/input', CognitionInput, self.cognition_input_callback)
        rospy.Subscriber('/cognition/output', CognitionOutput, self.cognition_output_callback)

        rospy.loginfo("Web UI Node: Ready. UI available at http://localhost:5000")

    def ui_state_callback(self, msg):
        try:
            data = json.loads(msg.data)
            socketio.emit('full_update', data)
        except json.JSONDecodeError as e:
            rospy.logerr(f"Failed to decode UI state JSON: {e}")

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

    def run_ros(self):
        rospy.spin()

# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(web_dir, path)

# NEW: Serve workspace files directly so we don't need Base64!
@app.route('/workspace/<path:filename>')
def serve_workspace_file(filename):
    if not WORKSPACE_PATH:
        return "Workspace path not configured", 404
    return send_from_directory(WORKSPACE_PATH, filename)

# --- SocketIO Event Handlers ---
@socketio.on('connect')
def handle_connect():
    rospy.loginfo('Web client connected.')

@socketio.on('human_input')
def handle_human_input(json_data):
    try:
        mode = json_data.get('mode', 'input')
        if mode == 'output':
            msg = CognitionOutput()
            msg.type = json_data.get('type', 'ai') 
            msg.content = json_data.get('content', '')
            msg.filename = "webui" 
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

if __name__ == '__main__':
    try:
        ros_node = WebUINode()
        flask_thread = threading.Thread(target=lambda: socketio.run(
            app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True))
        flask_thread.daemon = True
        flask_thread.start()
        ros_node.run_ros()
    except rospy.ROSInterruptException:
        pass