#!/usr/bin/env python3

import rospy
from logos_msgs.msg import ArmPose
import tkinter as tk
from tkinter import simpledialog, messagebox
import threading
import json
import time
import os

class ArmPublisher:
    def __init__(self):
        rospy.init_node('arm_animation_tool', anonymous=True)
        self.pub_arm_command = rospy.Publisher('/arm/command', ArmPose, queue_size=10)

    def publish_arm_pose(self, side, joint1, joint2, wrist, duration=0.1):
        msg = ArmPose()
        msg.side = side
        msg.joint1 = joint1
        msg.joint2 = joint2
        msg.wrist = wrist
        # Assuming duration is part of the message or handled elsewhere
        self.pub_arm_command.publish(msg)

class MultiLineDialog(simpledialog.Dialog):
    """Custom dialog for multi-line text input."""
    def __init__(self, parent, title=None):
        self.result = None
        super().__init__(parent, title=title)

    def body(self, master):
        tk.Label(master, text="Enter reasoning for the sequence:").pack()
        self.text = tk.Text(master, width=60, height=10)
        self.text.pack()
        return self.text

    def apply(self):
        self.result = self.text.get("1.0", tk.END).strip()

class ArmAnimationToolGUI:
    def __init__(self, arm_pub):
        self.arm_pub = arm_pub
        self.root = tk.Tk()
        self.root.title("Piper and Stella's Logos Robot Arm Key Frame Generation Tool")

        # Initialize variables for parameters
        self.side_left = "left"
        self.side_right = "right"

        # Linking flags
        self.link_arm = False

        # Arm Parameters
        # Each arm has joint1, joint2, wrist
        self.arm_params = {
            "left": {"joint1": 0.0, "joint2": 0.0, "wrist": 90.0},
            "right": {"joint1": 0.0, "joint2": 0.0, "wrist": 90.0}
        }

        # Create GUI components
        self.create_widgets()

        # Initialize keyframes list
        self.keyframes = []

    def create_widgets(self):
        # -------------------- Joint1 --------------------
        tk.Label(self.root, text="Joint1 - Left").grid(row=0, column=0, padx=5, pady=5)
        self.joint1_left_slider = tk.Scale(self.root, from_=-90, to=90, resolution=1, orient=tk.HORIZONTAL, command=lambda val: self.update_joint('left', 'joint1', val))
        self.joint1_left_slider.set(0)
        self.joint1_left_slider.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Joint1 - Right").grid(row=0, column=2, padx=5, pady=5)
        self.joint1_right_slider = tk.Scale(self.root, from_=-90, to=90, resolution=1, orient=tk.HORIZONTAL, command=lambda val: self.update_joint('right', 'joint1', val))
        self.joint1_right_slider.set(0)
        self.joint1_right_slider.grid(row=0, column=3, padx=5, pady=5)

        # -------------------- Joint2 --------------------
        tk.Label(self.root, text="Joint2 - Left").grid(row=1, column=0, padx=5, pady=5)
        self.joint2_left_slider = tk.Scale(self.root, from_=-90, to=90, resolution=1, orient=tk.HORIZONTAL, command=lambda val: self.update_joint('left', 'joint2', val))
        self.joint2_left_slider.set(0)
        self.joint2_left_slider.grid(row=1, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Joint2 - Right").grid(row=1, column=2, padx=5, pady=5)
        self.joint2_right_slider = tk.Scale(self.root, from_=-90, to=90, resolution=1, orient=tk.HORIZONTAL, command=lambda val: self.update_joint('right', 'joint2', val))
        self.joint2_right_slider.set(0)
        self.joint2_right_slider.grid(row=1, column=3, padx=5, pady=5)

        # -------------------- Wrist --------------------
        tk.Label(self.root, text="Wrist - Left").grid(row=2, column=0, padx=5, pady=5)
        self.wrist_left_slider = tk.Scale(self.root, from_=-90, to=90, resolution=1, orient=tk.HORIZONTAL, command=lambda val: self.update_joint('left', 'wrist', val))
        self.wrist_left_slider.set(0)
        self.wrist_left_slider.grid(row=2, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Wrist - Right").grid(row=2, column=2, padx=5, pady=5)
        self.wrist_right_slider = tk.Scale(self.root, from_=-90, to=90, resolution=1, orient=tk.HORIZONTAL, command=lambda val: self.update_joint('right', 'wrist', val))
        self.wrist_right_slider.set(0)
        self.wrist_right_slider.grid(row=2, column=3, padx=5, pady=5)

        # -------------------- Link Both Arms --------------------
        self.link_var = tk.IntVar()
        self.link_checkbox = tk.Checkbutton(self.root, text="Link Both Arms", variable=self.link_var, command=self.toggle_link)
        self.link_checkbox.grid(row=3, column=0, padx=5, pady=5)

        # -------------------- Keyframe and Sequence Buttons --------------------
        # Add Keyframe Button
        self.add_keyframe_button = tk.Button(self.root, text="Add Keyframe", command=self.add_keyframe)
        self.add_keyframe_button.grid(row=4, column=0, padx=5, pady=10)

        # Save Sequence Button
        self.save_sequence_button = tk.Button(self.root, text="Save Sequence", command=self.save_sequence)
        self.save_sequence_button.grid(row=4, column=1, padx=5, pady=10)

        # Test Sequence Button
        self.test_sequence_button = tk.Button(self.root, text="Test Sequence", command=self.test_sequence)
        self.test_sequence_button.grid(row=4, column=2, padx=5, pady=10)

        # Clear Sequence Button
        self.clear_sequence_button = tk.Button(self.root, text="Clear Sequence", command=self.clear_sequence)
        self.clear_sequence_button.grid(row=4, column=3, padx=5, pady=10)

        # Update Arms Button
        self.update_arms_button = tk.Button(self.root, text="Update Arms", command=self.update_arms)
        self.update_arms_button.grid(row=5, column=1, padx=5, pady=10)

    def toggle_link(self):
        """Toggle linking of both arms."""
        if self.link_var.get():
            # Link both arms by setting right sliders to left sliders
            self.joint1_right_slider.set(self.joint1_left_slider.get())
            self.joint2_right_slider.set(self.joint2_left_slider.get())
            self.wrist_right_slider.set(self.wrist_left_slider.get())

            # Disable right sliders
            self.joint1_right_slider.config(state='disabled')
            self.joint2_right_slider.config(state='disabled')
            self.wrist_right_slider.config(state='disabled')

            # Publish linked arms
            self.arm_pub.publish_arm_pose(
                side='both',
                joint1=float(self.joint1_left_slider.get()),
                joint2=float(self.joint2_left_slider.get()),
                wrist=float(self.wrist_left_slider.get())
            )
        else:
            # Enable right sliders
            self.joint1_right_slider.config(state='normal')
            self.joint2_right_slider.config(state='normal')
            self.wrist_right_slider.config(state='normal')

    def update_joint(self, side, joint, value):
        """Update joint values and publish ArmPose messages."""
        value = float(value)
        self.arm_params[side][joint] = value

        if self.link_var.get() and side == 'left':
            # When linked, update right arm to match left arm
            if joint == 'joint1':
                self.arm_params['right']['joint1'] = value
                self.joint1_right_slider.set(value)
            elif joint == 'joint2':
                self.arm_params['right']['joint2'] = value
                self.joint2_right_slider.set(value)
            elif joint == 'wrist':
                self.arm_params['right']['wrist'] = value
                self.wrist_right_slider.set(value)

            # Publish for both arms
            self.arm_pub.publish_arm_pose(
                side='both',
                joint1=self.arm_params['left']['joint1'],
                joint2=self.arm_params['left']['joint2'],
                wrist=self.arm_params['left']['wrist']
            )
        else:
            # Publish individually
            self.arm_pub.publish_arm_pose(
                side=side,
                joint1=self.arm_params[side]['joint1'],
                joint2=self.arm_params[side]['joint2'],
                wrist=self.arm_params[side]['wrist']
            )

    def add_keyframe(self):
        """Add the current arm positions as a keyframe."""
        actions = []

        # Left Arm
        if self.link_var.get():
            actions.append({
                "state": "ArmPose",  # Changed from "Action" to "state"
                "parameters": {
                    "side": "both",
                    "joint1": self.arm_params["left"]["joint1"],
                    "joint2": self.arm_params["left"]["joint2"],
                    "wrist": self.arm_params["left"]["wrist"]
                }
            })
        else:
            actions.append({
                "state": "ArmPose",  # Changed from "Action" to "state"
                "parameters": {
                    "side": "left",
                    "joint1": self.arm_params["left"]["joint1"],
                    "joint2": self.arm_params["left"]["joint2"],
                    "wrist": self.arm_params["left"]["wrist"]
                }
            })
            actions.append({
                "state": "ArmPose",  # Changed from "Action" to "state"
                "parameters": {
                    "side": "right",
                    "joint1": self.arm_params["right"]["joint1"],
                    "joint2": self.arm_params["right"]["joint2"],
                    "wrist": self.arm_params["right"]["wrist"]
                }
            })

        self.keyframes.append(actions)
        messagebox.showinfo("Keyframe Added", f"Keyframe {len(self.keyframes)} added.")

    def save_sequence(self):
        """Save the sequence of keyframes to a JSON file."""
        if not self.keyframes:
            messagebox.showwarning("No Keyframes", "No keyframes to save.")
            return

        # Prompt for emoji via terminal
        emoji = input("Enter emoji identifier for the sequence: ").strip()
        if not emoji:
            messagebox.showwarning("Input Needed", "Emoji identifier is required.")
            return

        # Prompt for reasoning using a custom multi-line dialog
        dialog = MultiLineDialog(self.root, title="Sequence Reasoning")
        reasoning = dialog.result
        if not reasoning:
            reasoning = "No reasoning provided."

        # Create the sequence dictionary with the new format
        sequence = {
            "emoji": emoji,
            "reasoning": reasoning,
            "frames": self.keyframes
        }

        # Wrap the sequence in a list to match the new JSON format
        sequences = [sequence]

        # Ensure the presets directory exists
        presets_dir = "./presets/arms"
        os.makedirs(presets_dir, exist_ok=True)

        # Generate a timestamped filename
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{presets_dir}/emoji_arm_seq_{timestamp}.json"

        # Save the sequence to a file
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                # Write JSON with ensure_ascii=False to prevent escape sequences
                json.dump(sequences, f, indent=0, ensure_ascii=False)
            messagebox.showinfo("Success", f"Sequence saved to {filename}")

            # Clear the sequence after saving
            self.clear_sequence()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save sequence: {e}")

    def clear_sequence(self):
        """Clears the current sequence of keyframes."""
        if messagebox.askyesno("Confirm Clear", "Are you sure you want to clear the current sequence?"):
            self.keyframes = []
            messagebox.showinfo("Sequence Cleared", "The current sequence has been cleared.")

    def test_sequence(self):
        """Test the saved sequence by publishing the keyframes with delays."""
        if not self.keyframes:
            messagebox.showwarning("No Keyframes", "No keyframes to test.")
            return

        # Start the test sequence in a separate thread to avoid blocking the GUI
        test_thread = threading.Thread(target=self.run_test_sequence)
        test_thread.start()

    def run_test_sequence(self):
        rospy.loginfo("Starting test sequence...")
        for idx, keyframe in enumerate(self.keyframes, start=1):
            rospy.loginfo(f"Publishing Keyframe {idx}/{len(self.keyframes)}")

            # Publish each action in the keyframe
            for action in keyframe:
                action_type = action["state"]  # Changed from "Action" to "state"
                params = action["parameters"]

                if action_type == "ArmPose":
                    self.arm_pub.publish_arm_pose(
                        side=params["side"],
                        joint1=params["joint1"],
                        joint2=params["joint2"],
                        wrist=params["wrist"],
                        duration=1.0  # Set duration to 1.0 for testing
                    )

            # Pause for 2 seconds (1 second for animation + 1 second hold)
            time.sleep(2.0)

        rospy.loginfo("Test sequence completed.")
        messagebox.showinfo("Test Sequence", "Test sequence completed.")

    def update_arms(self):
        """Publish the current arm positions to ensure the arms are in sync."""
        rospy.loginfo("Updating arms with current GUI values...")

        if self.link_var.get():
            # Publish for both arms
            self.arm_pub.publish_arm_pose(
                side='both',
                joint1=self.arm_params['left']['joint1'],
                joint2=self.arm_params['left']['joint2'],
                wrist=self.arm_params['left']['wrist']
            )
        else:
            # Publish individually
            self.arm_pub.publish_arm_pose(
                side='left',
                joint1=self.arm_params['left']['joint1'],
                joint2=self.arm_params['left']['joint2'],
                wrist=self.arm_params['left']['wrist']
            )
            self.arm_pub.publish_arm_pose(
                side='right',
                joint1=self.arm_params['right']['joint1'],
                joint2=self.arm_params['right']['joint2'],
                wrist=self.arm_params['right']['wrist']
            )

        rospy.loginfo("Arms updated successfully.")
        messagebox.showinfo("Update Arms", "Arms updated successfully.")

    def run(self):
        self.root.mainloop()

def ros_spin():
    rospy.spin()

if __name__ == "__main__":
    arm_pub = ArmPublisher()
    gui = ArmAnimationToolGUI(arm_pub)

    # Start ROS spin in a separate thread
    ros_thread = threading.Thread(target=ros_spin)
    ros_thread.daemon = True
    ros_thread.start()

    # Run the GUI
    gui.run()
