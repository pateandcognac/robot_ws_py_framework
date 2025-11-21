#!/usr/bin/env python3

import rospy
import json
import glob
from queue import PriorityQueue
from dataclasses import dataclass, field
from typing import Dict, List, Any
from logos_msgs.msg import ArmPose, SpeechData
import os
import time

@dataclass(order=True)
class ScheduledAnimation:
    execution_time: float
    animation: List[List[Dict[str, Any]]] = field(compare=False)
    duration: float = field(compare=False)

class ArmPlaybackNode:
    def __init__(self):
        rospy.init_node('arm_playback_node', anonymous=False)
        
        self.scheduled_animations = PriorityQueue()
        self.emoji_presets = self.load_emoji_presets()
        
        self.arm_pub = rospy.Publisher('/arm/command', ArmPose, queue_size=10)
        rospy.Subscriber('/face/tts_chunk', SpeechData, self.speech_sequence_callback)
        rospy.Subscriber('/face/state_mon', SpeechData, self.speech_sequence_callback)

        self.default_emoji = ""
        
        rospy.loginfo("Arm Playback Node Initialized.")

    def load_emoji_presets(self) -> Dict[str, List[List[Dict[str, Any]]]]:
        """
        Load arm animation presets from JSON files.
        The new format expects each JSON file to contain a list of sequences,
        each with 'emoji', 'reasoning', and 'frames'.
        """
        presets = {}
        presets_dir = "/home/robot/logos_ws/presets/arms/"
        preset_files = glob.glob(os.path.join(presets_dir, "emoji_arm_seq_*.json"))
        rospy.loginfo(f"Loading arm presets from directory: {presets_dir}")
        
        for preset_file in preset_files:
            try:
                with open(preset_file, 'r', encoding='utf-8') as f:
                    preset_data = json.load(f)
                    
                    if not isinstance(preset_data, list):
                        rospy.logerr(f"Expected a list of sequences in file: {preset_file}")
                        continue
                    
                    for sequence in preset_data:
                        emoji = sequence.get("emoji")
                        frames = sequence.get("frames")
                        
                        if emoji and frames:
                            if emoji in presets:
                                rospy.logwarn(f"Duplicate emoji '{emoji}' found in file {preset_file}. Overwriting previous entry.")
                            presets[emoji] = frames
                            # rospy.loginfo(f"Loaded sequence for emoji: {emoji}")
                        else:
                            rospy.logerr(f"Invalid sequence format in file {preset_file}: {sequence}")
            except json.JSONDecodeError:
                rospy.logerr(f"Error decoding JSON in file: {preset_file}")
            except IOError:
                rospy.logerr(f"Error reading file: {preset_file}")
        
        rospy.loginfo(f"Total loaded arm presets: {len(presets)}")
        return presets

    def speech_sequence_callback(self, msg: SpeechData) -> None:
        """
        Callback function that schedules arm animations based on incoming SpeechData messages.
        """
        current_time = rospy.get_time()
        # Schedule the animation after the last one in the queue
        execution_time = current_time + (self.scheduled_animations.qsize() * msg.duration)
        
        emoji = msg.emoji if msg.emoji in self.emoji_presets else self.default_emoji
        
        if emoji != msg.emoji:
            rospy.logwarn(f"No animation preset found for emoji: {msg.emoji}. Using default '{self.default_emoji}'.")
        
        frames = self.emoji_presets.get(emoji, [])
        
        if not frames:
            rospy.logwarn(f"No frames found for emoji '{emoji}'. Skipping animation.")
            return
        
        scheduled_animation = ScheduledAnimation(execution_time, frames, msg.duration)
        self.scheduled_animations.put(scheduled_animation)
        
        # rospy.loginfo(f"Scheduled animation for emoji '{emoji}' at time {execution_time}")

    def publish_animation(self, animation: List[List[Dict[str, Any]]], duration: float) -> None:
        """
        Publish a sequence of arm poses based on the provided animation frames.
        """
        if not animation:
            rospy.logwarn("Received empty animation sequence.")
            return
        
        num_keyframes = len(animation)
        if num_keyframes == 0:
            rospy.logwarn("Animation sequence contains no keyframes.")
            return

        step_duration = duration / num_keyframes
        rospy.loginfo(f"Starting arm animation with {num_keyframes} keyframes. Step duration: {step_duration:.2f} seconds.")
        
        for idx, keyframe in enumerate(animation, start=1):
            # rospy.loginfo(f"Publishing Keyframe {idx}/{num_keyframes}")
            
            for action in keyframe:
                try:
                    action_type = action['state']
                    params = action['parameters']
                    
                    if action_type == "ArmPose":
                        arm_pose = ArmPose()
                        arm_pose.side = params["side"]
                        arm_pose.joint1 = params["joint1"]
                        arm_pose.joint2 = params["joint2"]
                        arm_pose.wrist = params["wrist"]
                        
                        self.arm_pub.publish(arm_pose)
                        rospy.loginfo(f"Published ArmPose: side={arm_pose.side}, joint1={arm_pose.joint1}, joint2={arm_pose.joint2}, wrist={arm_pose.wrist}")
                    else:
                        rospy.logwarn(f"Unknown action type: {action_type}")
                except KeyError as e:
                    rospy.logerr(f"Missing key in action parameters: {e}")
                except Exception as e:
                    rospy.logerr(f"Error publishing ArmPose: {e}")
            
            rospy.sleep(step_duration)
        
        rospy.loginfo("Completed arm animation sequence.")

    def run(self) -> None:
        """
        Main loop that checks for scheduled animations and publishes them at the correct time.
        """
        rospy.loginfo("Arm Playback Node is running.")
        rate = rospy.Rate(10)  # 10 Hz
        
        while not rospy.is_shutdown():
            current_time = rospy.get_time()
            
            if not self.scheduled_animations.empty():
                next_animation = self.scheduled_animations.queue[0]
                
                if current_time >= next_animation.execution_time:
                    self.scheduled_animations.get()
                    self.publish_animation(next_animation.animation, next_animation.duration)
                    
            rate.sleep()

def main():
    try:
        node = ArmPlaybackNode()
        node.run()
    except rospy.ROSInterruptException:
        rospy.loginfo("Arm Playback Node terminated.")

if __name__ == '__main__':
    main()
