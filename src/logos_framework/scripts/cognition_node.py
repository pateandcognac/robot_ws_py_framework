#!/usr/bin/env python3
import rospy
import os
from logos_framework.msg import CognitionInput, CognitionOutput

class CognitionNode:
    def __init__(self):
        rospy.init_node('cognition_node')
        rospy.loginfo("Cognition Node: Initializing...")

        # Get the workspace path from a ROS parameter, with a default
        default_workspace = os.path.expanduser('~/robot_workspaces/default_agent')
        self.workspace_path = rospy.get_param('~workspace_path', default_workspace)
        rospy.loginfo(f"Using workspace: {self.workspace_path}")

        # Ensure the workspace directory exists
        if not os.path.isdir(self.workspace_path):
            rospy.logwarn(f"Workspace path {self.workspace_path} not found! Creating it.")
            os.makedirs(self.workspace_path, exist_ok=True)

        # Publishers
        self.output_pub = rospy.Publisher('/cognition/output', CognitionOutput, queue_size=10)

        # Subscribers
        self.input_sub = rospy.Subscriber('/cognition/input', CognitionInput, self._input_callback, queue_size=10)

        self.api_in_progress = False
        rospy.loginfo("Cognition Node: Ready and waiting for input.")

    def _input_callback(self, msg: CognitionInput):
        """Handles all incoming data for the agent's cognition."""
        rospy.loginfo(f"Received CognitionInput of type '{msg.type}'")

        # --- TODO: Core Logic Implementation ---
        # 1. Check for self.api_in_progress lock. If locked, queue the message.
        # 2. Dequeue and process messages.
        # 3. Append message content to io_buffer.jsonl and io_history.jsonl
        # 4. If msg.loop_cognition is True, start the full thinking cycle:
        #    a. Set self.api_in_progress = True
        #    b. Publish CognitionOutput messages to get header/footer context.
        #    c. Wait for context results to arrive back on /cognition/input.
        #    d. Assemble the final prompt (system_prompt, header, io_buffer, footer).
        #    e. Pre-process for images.
        #    f. Call the Gemini API (streaming thoughts and chunks to /cognition/output).
        #    g. Process the final response, append to history.
        #    h. Publish the full response as a CognitionOutput with type 'llm'.
        #    i. Set self.api_in_progress = False
        # -----------------------------------------
        pass

    def run(self):
        rospy.spin()

if __name__ == '__main__':
    try:
        node = CognitionNode()
        node.run()
    except rospy.ROSInterruptException:
        pass
    