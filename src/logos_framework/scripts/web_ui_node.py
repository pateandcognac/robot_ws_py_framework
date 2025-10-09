#!/usr/bin/env python3
import rospy
import threading
import json
import os # NEW: Import os for path handling
from flask import Flask, render_template, send_from_directory
from flask_socketio import SocketIO

from std_msgs.msg import String as StringMsg
from logos_framework.msg import CognitionInput, CognitionOutput

# --- Flask & SocketIO Setup ---

# This ensures that ROS can find your 'web' folder regardless of the working directory
script_dir = os.path.dirname(os.path.realpath(__file__))
web_dir = os.path.abspath(os.path.join(script_dir, '..', 'web'))

# When serving static files, Flask expects the folder path relative to the app's root.
# For templates, it's an absolute path. This setup is more reliable.
app = Flask(__name__, template_folder=web_dir, static_folder=web_dir)
app.config['SECRET_KEY'] = 'secret!'

# Flask-SocketIO will default to using the standard Flask development server, which is compatible with rospy.
socketio = SocketIO(app)

# --- ROS Node Class ---
class WebUINode:
    def __init__(self):
        # NOTE: anonymous=True is not needed when the node name is hardcoded
        rospy.init_node('web_ui_node')
        rospy.loginfo("Web UI Node: Initializing...")

        # Publisher to send human input to the cognition node
        self.cognition_input_pub = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)

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
        rospy.loginfo(f"Received cognition input of type '{msg.type}' to append to UI.")
        data = {
            'type': msg.type,
            'content': msg.content,
            'filename': msg.filename,
            'system_hint': msg.system_hint
        }
        socketio.emit('append_io', data)

    def cognition_output_callback(self, msg: CognitionOutput):
        """
        Receives output from the cognition node, specifically for streaming LLM chunks.
        """
        if msg.type == 'chunk' and msg.content:
            # No log here, it would be too spammy.
            socketio.emit('stream_chunk', {'content': msg.content})

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
    """Receives input from the browser and publishes it as a CognitionInput message."""
    rospy.loginfo(f"Received human input from web: {json_data}")
    try:
        msg = CognitionInput()
        msg.type = json_data.get('type', 'human')
        msg.content = json_data.get('content', '')
        msg.system_hint = json_data.get('system_hint', '')
        msg.loop_cognition = bool(json_data.get('loop_cognition', False))

        ros_node.cognition_input_pub.publish(msg)
    except Exception as e:
        rospy.logerr(f"Error processing human input from web: {e}")


if __name__ == '__main__':
    try:
        ros_node = WebUINode()

        # Run Flask in a separate thread
        # We add allow_unsafe_werkzeug=True for compatibility with newer versions
        # of Flask when running in this threaded mode.
        flask_thread = threading.Thread(target=lambda: socketio.run(
            app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True))
        flask_thread.daemon = True
        flask_thread.start()

        # Run ROS spin in the main thread
        ros_node.run_ros()

    except rospy.ROSInterruptException:
        pass