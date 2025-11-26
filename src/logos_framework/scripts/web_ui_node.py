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

# --- ROS Node Class ---
class WebUINode:
    def __init__(self):
        rospy.init_node('web_ui_node')
        rospy.loginfo("Web UI Node: Initializing...")

        # Publisher to send human input to the cognition node
        self.cognition_input_pub = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)
        # NEW: Publisher to send simulated AI output
        self.cognition_output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)

        # Subscribers to get data from the cognition node
        rospy.Subscriber('/cognition/ui_state', StringMsg, self.ui_state_callback)
        rospy.Subscriber('/cognition/input', CognitionInput, self.cognition_input_callback)
        rospy.Subscriber('/cognition/output', CognitionOutput, self.cognition_output_callback)

        rospy.loginfo("Web UI Node: Ready. UI available at http://localhost:5000")

    def ui_state_callback(self, msg):
        """
        Receives the full UI state (header, buffer, footer) and sends it to the browser.
        This triggers a full refresh of the UI.
        """
        rospy.loginfo("Received full UI state update.")
        try:
            data = json.loads(msg.data)
            socketio.emit('full_update', data)
        except json.JSONDecodeError as e:
            rospy.logerr(f"Failed to decode UI state JSON: {e}")

    def cognition_input_callback(self, msg: CognitionInput):
        """
        Receives ANY input message and appends it to the UI's io_buffer.
        """
        rospy.loginfo(f"Received cognition input of type '{msg.type}'.")
        data = {
            'type': msg.type,
            'content': msg.content,
            'filename': msg.filename,
            'system_hint': msg.system_hint
        }
        socketio.emit('append_io', data)

    def cognition_output_callback(self, msg: CognitionOutput):
        """
        Receives output from the cognition node.
        Handles streaming LLM chunks and complete, simulated messages.
        """
        if msg.type == 'feedback' or msg.type == 'me': # Ignore feedback messages for UI and 'me' messages (because we'll capture the streamed chunks instead)
            return
        if msg.type == 'chunk' and msg.content:
            # No log here, it would be too spammy.
            socketio.emit('stream_chunk', {'content': msg.content})
        # NEW: Handle non-chunk messages (like our simulated ones)
        # This allows the UI to display the message that was just published.
        else:
            rospy.loginfo(f"Received cognition output of type '{msg.type}' to append to UI.")
            data = {
                'type': msg.type,
                'content': msg.content,
                'filename': msg.filename,
                'system_hint': '' # CognitionOutput doesn't have this field
            }
            socketio.emit('append_io', data)

    def run_ros(self):
        rospy.spin()

# --- Flask Routes ---
@app.route('/')
def index():
    """Serves the main HTML file."""
    return render_template('index.html')

@app.route('/<path:path>')
def static_files(path):
    """Serves static files like CSS and JS."""
    return send_from_directory(web_dir, path)


# --- SocketIO Event Handlers ---
@socketio.on('connect')
def handle_connect():
    rospy.loginfo('Web client connected.')

@socketio.on('disconnect')
def handle_disconnect():
    rospy.loginfo('Web client disconnected.')

@socketio.on('human_input')
def handle_human_input(json_data):
    """
    Receives input from the browser and publishes it as a CognitionInput 
    or CognitionOutput message based on the selected mode.
    """
    rospy.loginfo(f"Received human input from web: {json_data}")
    try:
        # MODIFIED: Check the mode sent from the browser
        mode = json_data.get('mode', 'input')

        if mode == 'output':
            # Publish as a CognitionOutput message
            rospy.loginfo("Publishing as simulated AI output to /cognition/output")
            msg = CognitionOutput()
            msg.type = json_data.get('type', 'ai') # Default to 'ai' type
            msg.content = json_data.get('content', '')
            msg.filename = "webui" # As requested
            ros_node.cognition_output_pub.publish(msg)
        else:
            # Default behavior: publish as a CognitionInput message
            rospy.loginfo("Publishing as human input to /cognition/input")
            msg = CognitionInput()
            msg.type = json_data.get('type', 'human')
            msg.content = json_data.get('content', '')
            msg.system_hint = json_data.get('system_hint', '')
            msg.loop_cognition = bool(json_data.get('loop_cognition', False))
            msg.filename = "webui" # Add filename for consistency
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