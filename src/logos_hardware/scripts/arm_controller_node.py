#!/usr/bin/env python3

import math
import random

import rospy
from std_msgs.msg import Float32
from logos_msgs.msg import ArmPose


def lerp(start: float, end: float, t: float) -> float:
    """Linear interpolation between start and end."""
    return start + t * (end - start)


def clamp(value: float, low: float, high: float) -> float:
    """Clamp value to a range."""
    return max(low, min(value, high))


def ease_linear(t: float) -> float:
    return t


def ease_in_out_sine(t: float) -> float:
    return -(math.cos(math.pi * t) - 1.0) / 2.0


def ease_in_out_quad(t: float) -> float:
    if t < 0.5:
        return 2.0 * t * t
    return 1.0 - pow(-2.0 * t + 2.0, 2.0) / 2.0


def ease_out_cubic(t: float) -> float:
    return 1.0 - pow(1.0 - t, 3.0)


def ease_out_back(t: float) -> float:
    """
    Slight overshoot for goofy expressive arm motion.
    Good for non-functional decorative arms.
    """
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * pow(t - 1.0, 3.0) + c1 * pow(t - 1.0, 2.0)


class ArmController:
    def __init__(self):
        rospy.init_node("arm_controller")

        self.current_position = {
            "left": {"joint1": 0.0, "joint2": 0.0, "wrist": 0.0},
            "right": {"joint1": 0.0, "joint2": 0.0, "wrist": 0.0},
        }

        self.target_position = {
            "left": {"joint1": 0.0, "joint2": 0.0, "wrist": 0.0},
            "right": {"joint1": 0.0, "joint2": 0.0, "wrist": 0.0},
        }

        self.last_published_position = {
            "left": {"joint1": None, "joint2": None, "wrist": None},
            "right": {"joint1": None, "joint2": None, "wrist": None},
            "both": {"joint1": None, "joint2": None, "wrist": None},
        }

        self.publishers = {
            "left": {
                "joint1": rospy.Publisher("/arm/left/joint1", Float32, queue_size=10),
                "joint2": rospy.Publisher("/arm/left/joint2", Float32, queue_size=10),
                "wrist": rospy.Publisher("/arm/left/wrist", Float32, queue_size=10),
            },
            "right": {
                "joint1": rospy.Publisher("/arm/right/joint1", Float32, queue_size=10),
                "joint2": rospy.Publisher("/arm/right/joint2", Float32, queue_size=10),
                "wrist": rospy.Publisher("/arm/right/wrist", Float32, queue_size=10),
            },
            "both": {
                "joint1": rospy.Publisher("/arm/both/joint1", Float32, queue_size=10),
                "joint2": rospy.Publisher("/arm/both/joint2", Float32, queue_size=10),
                "wrist": rospy.Publisher("/arm/both/wrist", Float32, queue_size=10),
            },
        }

        # Motion state for expressive arm joints only.
        self.arm_motion_state = {
            "left": {
                "joint1": self.make_motion_state(0.0),
                "joint2": self.make_motion_state(0.0),
            },
            "right": {
                "joint1": self.make_motion_state(0.0),
                "joint2": self.make_motion_state(0.0),
            },
        }

        self.arm_easing_functions = [
            ease_in_out_sine,
            ease_in_out_quad,
            ease_out_cubic,
            ease_out_back,
        ]

        rospy.Subscriber("/arm/command", ArmPose, self.command_callback)
        rospy.Timer(rospy.Duration(0.05), self.update_position)

        self.movement_threshold = 0.5
        self.wrist_lerp_t = 0.2

    def make_motion_state(self, position: float) -> dict:
        """Create an animation state for one arm joint."""
        return {
            "start": position,
            "target": position,
            "start_time": rospy.get_time(),
            "duration": 0.4,
            "easing": ease_in_out_sine,
            "active": False,
        }

    def command_callback(self, msg: ArmPose):
        """Handle incoming ArmPose messages and set target positions."""
        if msg.side == "both":
            sides = ["left", "right"]
        elif msg.side in ["left", "right"]:
            sides = [msg.side]
        else:
            rospy.logwarn(f"Invalid side: {msg.side}")
            return

        for side in sides:
            if self.is_valid_position(msg.joint1, joint=True):
                self.set_arm_joint_target(side, "joint1", msg.joint1)

            else:
                rospy.logwarn(f"Invalid joint1 position for {side}: {msg.joint1}")

            if self.is_valid_position(msg.joint2, joint=True):
                self.set_arm_joint_target(side, "joint2", msg.joint2)
            else:
                rospy.logwarn(f"Invalid joint2 position for {side}: {msg.joint2}")

            if self.is_valid_position(msg.wrist, wrist=True):
                self.target_position[side]["wrist"] = msg.wrist
            else:
                rospy.logwarn(f"Invalid wrist position for {side}: {msg.wrist}")

    def set_arm_joint_target(self, side: str, joint_name: str, target: float):
        """
        Start a new expressive movement for an arm joint.
        Randomizes easing profile and movement duration per command.
        """
        current = self.current_position[side][joint_name]
        easing = random.choice(self.arm_easing_functions)

        # Slight duration variation makes repeated gestures feel less robotic.
        distance = abs(target - current)
        base_duration = 0.25 + (distance / 90.0) * 0.35
        duration = clamp(base_duration + random.uniform(-0.08, 0.5), 0.18, 2.0)

        self.target_position[side][joint_name] = target
        self.arm_motion_state[side][joint_name] = {
            "start": current,
            "target": target,
            "start_time": rospy.get_time(),
            "duration": duration,
            "easing": easing,
            "active": True,
        }

    def is_valid_position(
        self,
        value: float,
        joint: bool = False,
        wrist: bool = False,
    ) -> bool:
        """Check if the value is within the valid range."""
        if joint or wrist:
            return -90.0 <= value <= 90.0
        return False

    def has_significant_movement(self, current: float, last: float) -> bool:
        """Check if movement is above threshold."""
        if last is None:
            return True
        return abs(current - last) > self.movement_threshold

    def update_arm_joint(self, side: str, joint_name: str):
        """Update one expressive arm joint using its chosen easing curve."""
        state = self.arm_motion_state[side][joint_name]

        if not state["active"]:
            return

        elapsed = rospy.get_time() - state["start_time"]
        progress = clamp(elapsed / state["duration"], 0.0, 1.0)
        eased = state["easing"](progress)

        new_position = clamp(lerp(state["start"], state["target"], eased), -90.0, 90.0)
        self.current_position[side][joint_name] = new_position

        if progress >= 1.0:
            self.current_position[side][joint_name] = state["target"]
            state["active"] = False

    def update_wrist(self, side: str):
        """Update wrist using plain smoothing."""
        current = self.current_position[side]["wrist"]
        target = self.target_position[side]["wrist"]
        self.current_position[side]["wrist"] = lerp(current, target, self.wrist_lerp_t)

    def publish_if_needed(self, side: str, component: str):
        """Publish a component position if it changed significantly."""
        value = self.current_position[side][component]
        last = self.last_published_position[side][component]

        if self.has_significant_movement(value, last):
            self.publishers[side][component].publish(Float32(value))
            self.last_published_position[side][component] = value

    def positions_match(self, component: str, tolerance: float = 0.01) -> bool:
        """Check if left and right positions are effectively identical."""
        left = self.current_position["left"][component]
        right = self.current_position["right"][component]
        return abs(left - right) <= tolerance

    def update_position(self, _event):
        """Update all joints and publish changed values."""
        for side in ["left", "right"]:
            self.update_arm_joint(side, "joint1")
            self.update_arm_joint(side, "joint2")
            self.update_wrist(side)

            for component in ["joint1", "joint2", "wrist"]:
                self.publish_if_needed(side, component)

        if (
            self.positions_match("joint1")
            and self.positions_match("joint2")
            and self.positions_match("wrist")
        ):
            for component in ["joint1", "joint2", "wrist"]:
                value = self.current_position["left"][component]
                last = self.last_published_position["both"][component]

                if self.has_significant_movement(value, last):
                    self.publishers["both"][component].publish(Float32(value))
                    self.last_published_position["both"][component] = value

    def run(self):
        rospy.spin()


if __name__ == "__main__":
    try:
        controller = ArmController()
        controller.run()
    except rospy.ROSInterruptException:
        pass