#!/usr/bin/env python3

import rospy
from std_msgs.msg import Float32
from logos_msgs.msg import ArmPose

def lerp(start: float, end: float, t: float) -> float:
    """Linear interpolation between start and end with factor t (0.0 - 1.0)."""
    return start + t * (end - start)

class ArmController:
    def __init__(self):
        rospy.init_node('arm_controller')
        
        # Initialize current and target positions within the range of -90 to 90 for joints and wrist
        self.current_position = {
            'left': {'joint1': 0.0, 'joint2': 0.0, 'wrist': 0.0},
            'right': {'joint1': 0.0, 'joint2': 0.0, 'wrist': 0.0}
        }
        self.target_position = {
            'left': {'joint1': 0.0, 'joint2': 0.0, 'wrist': 0.0},
            'right': {'joint1': 0.0, 'joint2': 0.0, 'wrist': 0.0}
        }

        # Last published positions to compare and stop publishing when stable
        self.last_published_position = {
            'left': {'joint1': None, 'joint2': None, 'wrist': None},
            'right': {'joint1': None, 'joint2': None, 'wrist': None}
        }

        # Publishers for individual arms and wrist
        self.publishers = {
            'left': {
                'joint1': rospy.Publisher('/arm/left/joint1', Float32, queue_size=10),
                'joint2': rospy.Publisher('/arm/left/joint2', Float32, queue_size=10),
                'wrist': rospy.Publisher('/arm/left/wrist', Float32, queue_size=10)
            },
            'right': {
                'joint1': rospy.Publisher('/arm/right/joint1', Float32, queue_size=10),
                'joint2': rospy.Publisher('/arm/right/joint2', Float32, queue_size=10),
                'wrist': rospy.Publisher('/arm/right/wrist', Float32, queue_size=10)
            },
            'both': {
                'joint1': rospy.Publisher('/arm/both/joint1', Float32, queue_size=10),
                'joint2': rospy.Publisher('/arm/both/joint2', Float32, queue_size=10),
                'wrist': rospy.Publisher('/arm/both/wrist', Float32, queue_size=10)
            }
        }
        
        # Subscriber to listen for ArmPose messages
        rospy.Subscriber('/arm/command', ArmPose, self.command_callback)
        
        # Timer to call update_position at 20Hz (0.05 seconds)
        rospy.Timer(rospy.Duration(0.05), self.update_position)

        # Threshold to determine if a position has changed significantly
        self.movement_threshold = 0.5  # Adjust as necessary

    def command_callback(self, msg: ArmPose):
        """Handles incoming ArmPose messages and sets the target positions."""
        sides = []
        if msg.side == 'both':
            sides = ['left', 'right']
        elif msg.side in ['left', 'right']:
            sides = [msg.side]
        else:
            rospy.logwarn(f"Invalid side: {msg.side}")
            return

        for side in sides:
            # Set target positions for joints
            if self.is_valid_position(msg.joint1, joint=True):
                self.target_position[side]['joint1'] = msg.joint1
            else:
                rospy.logwarn(f"Invalid joint1 position for {side}: {msg.joint1}")

            if self.is_valid_position(msg.joint2, joint=True):
                self.target_position[side]['joint2'] = msg.joint2
            else:
                rospy.logwarn(f"Invalid joint2 position for {side}: {msg.joint2}")

            # Set target position for wrist
            if self.is_valid_position(msg.wrist, wrist=True):
                self.target_position[side]['wrist'] = msg.wrist
            else:
                rospy.logwarn(f"Invalid wrist position for {side}: {msg.wrist}")

    def is_valid_position(self, value: float, joint: bool = False, wrist: bool = False) -> bool:
        """Checks if the value is within the valid range."""
        if joint:
            return -90.0 <= value <= 90.0
        if wrist:
            return -90.0 <= value <= 90.0
        return False

    def has_significant_movement(self, current: float, last: float) -> bool:
        """Check if movement is above a certain threshold."""
        if last is None:
            return True  # First time publishing
        return abs(current - last) > self.movement_threshold

    def update_position(self, event):
        """Smoothly moves current position towards the target and publishes the new positions if they change significantly."""
        for side in ['left', 'right']:
            for component in ['joint1', 'joint2', 'wrist']:
                current = self.current_position[side][component]
                target = self.target_position[side][component]
                
                # Adjust the interpolation factor as needed for smoothing speed
                # Here, t is set to 0.333 for gradual movement
                t = 0.2
                new_position = lerp(current, target, t)
                self.current_position[side][component] = new_position

                # Only publish if there has been significant movement
                if self.has_significant_movement(new_position, self.last_published_position[side][component]):
                    self.publishers[side][component].publish(Float32(new_position))
                    self.last_published_position[side][component] = new_position

        # Publish to the "both" topics if both arms are moving identically
        if (self.current_position['left']['joint1'] == self.current_position['right']['joint1'] and
            self.current_position['left']['joint2'] == self.current_position['right']['joint2'] and
            self.current_position['left']['wrist'] == self.current_position['right']['wrist']):
            
            for component in ['joint1', 'joint2', 'wrist']:
                value = self.current_position['left'][component]
                if self.has_significant_movement(value, self.last_published_position['left'][component]):
                    self.publishers['both'][component].publish(Float32(value))
                    self.last_published_position['left'][component] = value

    def run(self):
        """Starts the ROS event loop."""
        rospy.spin()

if __name__ == '__main__':
    try:
        controller = ArmController()
        controller.run()
    except rospy.ROSInterruptException:
        pass
