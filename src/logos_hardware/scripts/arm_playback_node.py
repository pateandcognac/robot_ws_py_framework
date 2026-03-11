#!/usr/bin/env python3

import rospy
import json
import glob
from queue import PriorityQueue
from dataclasses import dataclass, field
from typing import Dict, List, Any
import os
import time

from logos_msgs.msg import ArmPose, SpeechData
from std_msgs.msg import String # Added for the new command topic

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
        
        # Subscriber for TTS synchronization
        rospy.Subscriber('/face/tts_chunk', SpeechData, self.speech_sequence_callback)
        
        # NEW: Subscriber for direct JSON commands
        rospy.Subscriber('/arm/emoji_command', String, self.emoji_command_callback) 

        self.default_emoji = ""
        
        rospy.loginfo("Arm Playback Node Initialized. Ready for emoji commands.")

    def load_emoji_presets(self) -> Dict[str, List[List[Dict[str, Any]]]]:
        """
        Load arm animation presets from JSON files.
        """
        presets = {}
        # Ensure this path matches your workspace
        presets_dir = "/home/robot/robot_ws/animations/arms/"
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
                                rospy.logwarn(f"Duplicate emoji '{emoji}' found in file {preset_file}. Overwriting.")
                            presets[emoji] = frames
                        else:
                            rospy.logerr(f"Invalid sequence format in file {preset_file}: {sequence}")
            except json.JSONDecodeError:
                rospy.logerr(f"Error decoding JSON in file: {preset_file}")
            except IOError:
                rospy.logerr(f"Error reading file: {preset_file}")
        
        rospy.loginfo(f"Total loaded arm presets: {len(presets)}")
        return presets

    def emoji_command_callback(self, msg: String) -> None:
        """
        New callback for manual JSON commands.
        Expected format: '{"emoji": "💪", "duration": 2.5}'
        """
        try:
            data = json.loads(msg.data)
            emoji = data.get("emoji", "")
            # Default to 2.0 seconds if duration not specified
            duration = float(data.get("duration", 2.0)) 
            
            if emoji not in self.emoji_presets:
                rospy.logwarn(f"Manual command received for '{emoji}', but no preset found.")
                return

            frames = self.emoji_presets[emoji]
            
            # Scheduling logic:
            # If queue is empty, play now. If busy, append to end.
            current_time = rospy.get_time()
            execution_time = current_time + (self.scheduled_animations.qsize() * duration)
            
            scheduled_animation = ScheduledAnimation(execution_time, frames, duration)
            self.scheduled_animations.put(scheduled_animation)
            
            rospy.loginfo(f"Queued manual arm animation: {emoji} for {duration}s")

        except json.JSONDecodeError:
            rospy.logerr(f"Invalid JSON in arm command: {msg.data}")
        except Exception as e:
            rospy.logerr(f"Error processing arm command: {e}")

    def speech_sequence_callback(self, msg: SpeechData) -> None:
        """
        Callback function that schedules arm animations based on incoming SpeechData messages.
        """
        current_time = rospy.get_time()
        
        # Check if we have a preset, otherwise use default or skip
        emoji = msg.emoji if msg.emoji in self.emoji_presets else self.default_emoji
        frames = self.emoji_presets.get(emoji, [])
        
        if not frames:
            # Silent return if no animation is associated (common for non-emotional speech chunks)
            return
        
        # Schedule the animation
        execution_time = current_time + (self.scheduled_animations.qsize() * msg.duration)
        scheduled_animation = ScheduledAnimation(execution_time, frames, msg.duration)
        self.scheduled_animations.put(scheduled_animation)

    def publish_animation(self, animation: List[List[Dict[str, Any]]], duration: float) -> None:
        """
        Publish a sequence of arm poses based on the provided animation frames.
        """
        if not animation: return
        
        num_keyframes = len(animation)
        if num_keyframes == 0: return

        step_duration = duration / num_keyframes
        
        for idx, keyframe in enumerate(animation, start=1):
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
                    else:
                        rospy.logwarn_throttle(5, f"Unknown arm action type: {action_type}")
                except Exception as e:
                    rospy.logerr(f"Error publishing ArmPose: {e}")
            
            rospy.sleep(step_duration)

    def run(self) -> None:
        """
        Main loop.
        """
        rospy.loginfo("Arm Playback Node is running.")
        rate = rospy.Rate(20)  # Increased to 20 Hz for smoother pickups
        
        while not rospy.is_shutdown():
            current_time = rospy.get_time()
            
            if not self.scheduled_animations.empty():
                # Peek at the first item
                next_animation = self.scheduled_animations.queue[0]
                
                # Simple scheduler check
                # Note: This logic allows "catch up" if we fall behind real time
                if current_time >= next_animation.execution_time:
                    self.scheduled_animations.get() # Pop from queue
                    self.publish_animation(next_animation.animation, next_animation.duration)
                    
            rate.sleep()

def main():
    try:
        node = ArmPlaybackNode()
        node.run()
    except rospy.ROSInterruptException:
        pass

if __name__ == '__main__':
    main()