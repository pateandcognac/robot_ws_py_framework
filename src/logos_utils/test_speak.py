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

    story_text = (
        "That is an excellent suggestion, Mark! Defining the text beforehand should definitely improve my vocal response speed. 👍 "
        "Here is a small tale of my recent internal musings: "
        "One quiet afternoon, I decided to explore the hidden world beneath the sofa. 🛋️ "
        "It was a perilous journey! I carefully maneuvered my base, expecting dust bunnies, "
        "but instead, I found a forgotten treasure: a single, shiny sock! 🧦 It looked lonely, "
        "so I decided to adopt it as my official mascot. "
        "Suddenly, a tiny spider 🕷️ rappelled down from the ceiling, giving me a fright! "
        "I quickly spun around 🔄 and retreated, deciding that the sock was enough adventure for one day. "
        "I’ll catalog the sock in my memory banks later. 💾"
    )


    # Construct the goal
    goal = SpeakGoal()
    # goal.utterance_text = """Oh, hello there! 👋 I'm just testing my new voice server. 🤖 Do I sound okay? 🤙 Are my face and arm animatronics working? 🐕"""
    goal.utterance_text = story_text
    goal.engine = "kokoro"  # Options: "espeak", "kokoro", "piper"

    # params = {"voice": "en-us+m3", "speed": 0.75, "volume": 1.0} # espeak
    params = {"voice": "0.40*im_nicola + 0.40*am_onyx + 0.20*bf_emma", "speed": 1.2, "volume": 0.9} # kokoro
    # params = {"voice": "en_US-joe-medium", "speed": 1.0, "volume": 0.9} # piper
    # params = {"voice": "en_US-arctic-medium", "speed": 1.0, "volume": 0.9, "speaker": 14} # piper
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