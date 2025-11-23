#!/usr/bin/env python3

import rospy
import actionlib
import json
from logos_msgs.msg import SpeakAction, SpeakGoal

def feedback_cb(feedback):
    print(f"[Feedback] Chunk {feedback.current_chunk_index + 1}/{feedback.total_chunks}: "
          f"'{feedback.text_snippet}' (Emoji: {feedback.emoji_snippet})")

def test_client():
    rospy.init_node('test_speak_client')

    client_name = "speak" # This must match the name in SpeakActionServer("speak")
    print(f"Waiting for '{client_name}' action server...")
    
    client = actionlib.SimpleActionClient(client_name, SpeakAction)
    
    # This will block until the server is found. 
    # If it hangs here, the topic names are definitely mismatched.
    if not client.wait_for_server(rospy.Duration(5.0)):
        print(f"ERROR: Action server '{client_name}' not found within 5 seconds.")
        print("Run 'rostopic list | grep goal' to see available action topics.")
        return

    print("Server found! Sending goal...")

    # Construct the goal
    goal = SpeakGoal()
    goal.utterance_text = """Hello there! 👋 I am testing my new voice server. 🤖 Thank you for helping me out! 🐝  If you ever plug an HDMI monitor into the robot, sounddevice might try to default to the HDMI audio out. 🐕"""
    goal.engine = "piper"

    # params = {"voice": "kal_diphone"}#  "speed": 1.0, "volume": 1.0}
    # params = {"voice": "en-us+croak", "speed": 1.0, "volume": 1.0}

    # Voice options: 
    # 'af_bella', 'af_sarah', 'am_adam', 'am_michael', 
    # 'bf_emma', 'bf_isabella', 'bm_george', 'bm_lewis', etc
    # or a mix: '0.5*af_bella + 0.5*am_adam'
    # params = {
        # "voice": "1.0*am_onyx",
        # "voice": "0.35*im_nicola + 0.40*am_onyx + 0.25*bm_fable", 
        # "voice": "0.30*im_nicola + 0.35*bf_emma + 0.35*hm_omega", 
        # "voice": "0.30*im_nicola + 0.40*am_onyx + 0.30*bm_fable", 
        # "speed": 1.25 
    #}
    params = {"voice": "en_US-joe-medium", "speed": 1.0, "volume": 0.5}
    goal.engine_params = json.dumps(params)

    # Send goal with feedback callback
    client.send_goal(goal, feedback_cb=feedback_cb)

    print("Goal sent. Waiting for result...")
    client.wait_for_result()
    
    result = client.get_result()
    print("--- Result ---")
    print(f"Success: {result.success}")
    print(f"Message: {result.final_message}")
    print(f"Total Duration: {result.total_duration:.2f}s")

if __name__ == '__main__':
    try:
        test_client()
    except rospy.ROSInterruptException:
        pass