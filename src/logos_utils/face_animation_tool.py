#!/usr/bin/env python3

import rospy
from logos_msgs.msg import (
    EyeGazeX, EyeGazeY, EyeScaleX, EyeScaleY,
    EyeLidHeight, EyeLidAngle, EyeColor, MouthSine
)
import tkinter as tk
from tkinter import colorchooser, simpledialog, messagebox
import threading
import json
import time
import os

class FacePublisher:
    def __init__(self):
        rospy.init_node('face_animation_tool', anonymous=True)

        # Initialize publishers for each facial parameter
        self.pub_eye_gaze_x = rospy.Publisher('/face/eye_gaze_x', EyeGazeX, queue_size=10)
        self.pub_eye_gaze_y = rospy.Publisher('/face/eye_gaze_y', EyeGazeY, queue_size=10)
        self.pub_eye_scale_x = rospy.Publisher('/face/eye_scale_x', EyeScaleX, queue_size=10)
        self.pub_eye_scale_y = rospy.Publisher('/face/eye_scale_y', EyeScaleY, queue_size=10)
        self.pub_eye_lid_height = rospy.Publisher('/face/eye_lid_height', EyeLidHeight, queue_size=10)
        self.pub_eye_lid_angle = rospy.Publisher('/face/eye_lid_angle', EyeLidAngle, queue_size=10)
        self.pub_eye_color = rospy.Publisher('/face/eye_color', EyeColor, queue_size=10)
        self.pub_mouth_sine = rospy.Publisher('/face/mouth/sine_wave', MouthSine, queue_size=10)
        # self.pub_arm_command = rospy.Publisher('/arm/command', ArmCommand, queue_size=10)


    def publish_eye_gaze_x(self, eye_side, gaze_x, duration=0.1):
        msg = EyeGazeX()
        msg.eye_side = eye_side
        msg.gaze_x = gaze_x
        msg.duration = duration
        self.pub_eye_gaze_x.publish(msg)

    def publish_eye_gaze_y(self, eye_side, gaze_y, duration=0.1):
        msg = EyeGazeY()
        msg.eye_side = eye_side
        msg.gaze_y = gaze_y
        msg.duration = duration
        self.pub_eye_gaze_y.publish(msg)

    def publish_eye_scale_x(self, eye_side, scale_x, duration=0.1):
        msg = EyeScaleX()
        msg.eye_side = eye_side
        msg.scale_x = scale_x
        msg.duration = duration
        self.pub_eye_scale_x.publish(msg)

    def publish_eye_scale_y(self, eye_side, scale_y, duration=0.1):
        msg = EyeScaleY()
        msg.eye_side = eye_side
        msg.scale_y = scale_y
        msg.duration = duration
        self.pub_eye_scale_y.publish(msg)

    def publish_eye_lid_height(self, eye_side, lid_height, duration=0.1):
        msg = EyeLidHeight()
        msg.eye_side = eye_side
        msg.lid_height = lid_height
        msg.duration = duration
        self.pub_eye_lid_height.publish(msg)

    def publish_eye_lid_angle(self, eye_side, lid_angle, duration=0.1):
        msg = EyeLidAngle()
        msg.eye_side = eye_side
        msg.lid_angle = lid_angle
        msg.duration = duration
        self.pub_eye_lid_angle.publish(msg)

    def publish_eye_color(self, eye_side, color, duration=0.1):
        msg = EyeColor()
        msg.eye_side = eye_side
        msg.color = color
        msg.duration = duration
        self.pub_eye_color.publish(msg)

    def publish_mouth_sine(self, frequency, amplitude, phase, phase_increment, duration, color):
        msg = MouthSine()
        msg.frequency = frequency
        msg.amplitude = amplitude
        msg.phase = phase
        msg.phase_increment = phase_increment
        msg.duration = duration
        msg.color = color
        self.pub_mouth_sine.publish(msg)

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

class AnimationToolGUI:
    def __init__(self, face_pub):
        self.face_pub = face_pub
        self.root = tk.Tk()
        self.root.title("Piper and Stella's Logos Robot Facial Animation Tool")

        # Initialize variables for parameters
        self.eye_side_left = "left"
        self.eye_side_right = "right"

        # Dictionaries to keep track of 'both' linking for each parameter
        self.link_eye_gaze_x = False
        self.link_eye_gaze_y = False
        self.link_eye_scale_x = False
        self.link_eye_scale_y = False
        self.link_eye_lid_height = False
        self.link_eye_lid_angle = False
        self.link_eye_color = False

        # -------------------- Eye Gaze X --------------------
        tk.Label(self.root, text="Eye Gaze X - Left").grid(row=0, column=0, padx=5, pady=5)
        self.gaze_x_left_slider = tk.Scale(self.root, from_=-1.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_gaze_x('left', val))
        self.gaze_x_left_slider.set(0.0)
        self.gaze_x_left_slider.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Eye Gaze X - Right").grid(row=0, column=2, padx=5, pady=5)
        self.gaze_x_right_slider = tk.Scale(self.root, from_=-1.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_gaze_x('right', val))
        self.gaze_x_right_slider.set(0.0)
        self.gaze_x_right_slider.grid(row=0, column=3, padx=5, pady=5)

        self.gaze_x_both_button = tk.Button(self.root, text="Link Both", command=lambda: self.toggle_link('gaze_x'))
        self.gaze_x_both_button.grid(row=0, column=4, padx=5, pady=5)

        # -------------------- Eye Gaze Y --------------------
        tk.Label(self.root, text="Eye Gaze Y - Left").grid(row=1, column=0, padx=5, pady=5)
        self.gaze_y_left_slider = tk.Scale(self.root, from_=-1.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_gaze_y('left', val))
        self.gaze_y_left_slider.set(0.0)
        self.gaze_y_left_slider.grid(row=1, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Eye Gaze Y - Right").grid(row=1, column=2, padx=5, pady=5)
        self.gaze_y_right_slider = tk.Scale(self.root, from_=-1.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_gaze_y('right', val))
        self.gaze_y_right_slider.set(0.0)
        self.gaze_y_right_slider.grid(row=1, column=3, padx=5, pady=5)

        self.gaze_y_both_button = tk.Button(self.root, text="Link Both", command=lambda: self.toggle_link('gaze_y'))
        self.gaze_y_both_button.grid(row=1, column=4, padx=5, pady=5)

        # -------------------- Eye Scale X --------------------
        tk.Label(self.root, text="Eye Scale X - Left").grid(row=2, column=0, padx=5, pady=5)
        self.scale_x_left_slider = tk.Scale(self.root, from_=0.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_scale_x('left', val))
        self.scale_x_left_slider.set(1.0)
        self.scale_x_left_slider.grid(row=2, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Eye Scale X - Right").grid(row=2, column=2, padx=5, pady=5)
        self.scale_x_right_slider = tk.Scale(self.root, from_=0.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_scale_x('right', val))
        self.scale_x_right_slider.set(1.0)
        self.scale_x_right_slider.grid(row=2, column=3, padx=5, pady=5)

        self.scale_x_both_button = tk.Button(self.root, text="Link Both", command=lambda: self.toggle_link('scale_x'))
        self.scale_x_both_button.grid(row=2, column=4, padx=5, pady=5)

        # -------------------- Eye Scale Y --------------------
        tk.Label(self.root, text="Eye Scale Y - Left").grid(row=3, column=0, padx=5, pady=5)
        self.scale_y_left_slider = tk.Scale(self.root, from_=0.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_scale_y('left', val))
        self.scale_y_left_slider.set(0.9)
        self.scale_y_left_slider.grid(row=3, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Eye Scale Y - Right").grid(row=3, column=2, padx=5, pady=5)
        self.scale_y_right_slider = tk.Scale(self.root, from_=0.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_scale_y('right', val))
        self.scale_y_right_slider.set(0.9)
        self.scale_y_right_slider.grid(row=3, column=3, padx=5, pady=5)

        self.scale_y_both_button = tk.Button(self.root, text="Link Both", command=lambda: self.toggle_link('scale_y'))
        self.scale_y_both_button.grid(row=3, column=4, padx=5, pady=5)

        # -------------------- Eye Lid Height --------------------
        tk.Label(self.root, text="Eye Lid Height - Left").grid(row=4, column=0, padx=5, pady=5)
        self.lid_height_left_slider = tk.Scale(self.root, from_=-1.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_lid_height('left', val))
        self.lid_height_left_slider.set(0.0)
        self.lid_height_left_slider.grid(row=4, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Eye Lid Height - Right").grid(row=4, column=2, padx=5, pady=5)
        self.lid_height_right_slider = tk.Scale(self.root, from_=-1.0, to=1.0, resolution=0.01, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_lid_height('right', val))
        self.lid_height_right_slider.set(0.0)
        self.lid_height_right_slider.grid(row=4, column=3, padx=5, pady=5)

        self.lid_height_both_button = tk.Button(self.root, text="Link Both", command=lambda: self.toggle_link('lid_height'))
        self.lid_height_both_button.grid(row=4, column=4, padx=5, pady=5)

        # -------------------- Eye Lid Angle --------------------
        tk.Label(self.root, text="Eye Lid Angle - Left").grid(row=5, column=0, padx=5, pady=5)
        self.lid_angle_left_slider = tk.Scale(self.root, from_=-45, to=45, resolution=1, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_lid_angle('left', val))
        self.lid_angle_left_slider.set(0)
        self.lid_angle_left_slider.grid(row=5, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Eye Lid Angle - Right").grid(row=5, column=2, padx=5, pady=5)
        self.lid_angle_right_slider = tk.Scale(self.root, from_=-45, to=45, resolution=1, orient=tk.HORIZONTAL, command=lambda val: self.update_eye_lid_angle('right', val))
        self.lid_angle_right_slider.set(0)
        self.lid_angle_right_slider.grid(row=5, column=3, padx=5, pady=5)

        self.lid_angle_both_button = tk.Button(self.root, text="Link Both", command=lambda: self.toggle_link('lid_angle'))
        self.lid_angle_both_button.grid(row=5, column=4, padx=5, pady=5)

        # -------------------- Eye Color --------------------
        tk.Label(self.root, text="Eye Color - Left").grid(row=6, column=0, padx=5, pady=5)
        self.eye_color_left_button = tk.Button(self.root, text="Choose Color", command=lambda: self.choose_eye_color('left'))
        self.eye_color_left_button.grid(row=6, column=1, padx=5, pady=5)
        self.eye_color_left = "#FF69B4"  # Default color
        self.eye_color_left_button.config(bg=self.eye_color_left)

        tk.Label(self.root, text="Eye Color - Right").grid(row=6, column=2, padx=5, pady=5)
        self.eye_color_right_button = tk.Button(self.root, text="Choose Color", command=lambda: self.choose_eye_color('right'))
        self.eye_color_right_button.grid(row=6, column=3, padx=5, pady=5)
        self.eye_color_right = "#FF69B4"  # Default color
        self.eye_color_right_button.config(bg=self.eye_color_right)

        self.eye_color_both_button = tk.Button(self.root, text="Link Both", command=lambda: self.toggle_link('eye_color'))
        self.eye_color_both_button.grid(row=6, column=4, padx=5, pady=5)


        # -------------------- Mouth Sine Parameters --------------------
        tk.Label(self.root, text="Mouth Sine Frequency").grid(row=7, column=0, padx=5, pady=5)
        self.mouth_freq_slider = tk.Scale(self.root, from_=0, to=16, resolution=0.01, orient=tk.HORIZONTAL, command=self.update_mouth_sine)
        self.mouth_freq_slider.set(0.0)
        self.mouth_freq_slider.grid(row=7, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Mouth Sine Amplitude").grid(row=8, column=0, padx=5, pady=5)
        self.mouth_amp_slider = tk.Scale(self.root, from_=0.0, to=1.0, resolution=0.05, orient=tk.HORIZONTAL, command=self.update_mouth_sine)
        self.mouth_amp_slider.set(1.0)
        self.mouth_amp_slider.grid(row=8, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Mouth Sine Phase").grid(row=9, column=0, padx=5, pady=5)
        self.mouth_phase_slider = tk.Scale(self.root, from_=-3.1416, to=3.1416, resolution=0.01, orient=tk.HORIZONTAL, command=self.update_mouth_sine)
        self.mouth_phase_slider.set(0.0)
        self.mouth_phase_slider.grid(row=9, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Mouth Phase Increment").grid(row=10, column=0, padx=5, pady=5)
        self.mouth_phase_inc_slider = tk.Scale(self.root, from_=-3.14, to=3.14, resolution=0.01, orient=tk.HORIZONTAL, command=self.update_mouth_sine)
        self.mouth_phase_inc_slider.set(0.0)
        self.mouth_phase_inc_slider.grid(row=10, column=1, padx=5, pady=5)

        tk.Label(self.root, text="Mouth Color").grid(row=11, column=0, padx=5, pady=5)
        self.mouth_color_button = tk.Button(self.root, text="Choose Color", command=self.choose_mouth_color)
        self.mouth_color_button.grid(row=11, column=1, padx=5, pady=5)
        # Initialize mouth_sine_params
        self.mouth_sine_params = {
            "frequency": 0.0,
            "amplitude": 1.0,
            "phase": 0.0,
            "phase_increment": 0.0,
            "color": "#FFFFFF",  # Default mouth color
            "duration": 0.1  # default duration
        }
        self.mouth_color_button.config(bg=self.mouth_sine_params["color"])

        # -------------------- Keyframe and Sequence Buttons --------------------
        # Add Keyframe Button
        self.add_keyframe_button = tk.Button(self.root, text="Add Keyframe", command=self.add_keyframe)
        self.add_keyframe_button.grid(row=12, column=0, padx=5, pady=10)

        # Save Sequence Button
        self.save_sequence_button = tk.Button(self.root, text="Save Sequence", command=self.save_sequence)
        self.save_sequence_button.grid(row=12, column=1, padx=5, pady=10)

        # Test Sequence Button
        self.test_sequence_button = tk.Button(self.root, text="Test Sequence", command=self.test_sequence)
        self.test_sequence_button.grid(row=12, column=2, padx=5, pady=10)

        # Clear Sequence Button
        self.clear_sequence_button = tk.Button(self.root, text="Clear Sequence", command=self.clear_sequence)
        self.clear_sequence_button.grid(row=12, column=3, padx=5, pady=10)

        # Update Face Button
        self.update_face_button = tk.Button(self.root, text="Update Face", command=self.update_face)
        self.update_face_button.grid(row=12, column=4, padx=5, pady=10)

        # Initialize keyframes list
        self.keyframes = []

    def toggle_link(self, parameter):
        """
        Toggle the linking of left and right sliders/buttons for a given parameter.
        When linked, adjusting one slider/button will adjust the other.
        """
        if parameter == 'gaze_x':
            self.link_eye_gaze_x = not self.link_eye_gaze_x
            state = "Unlink" if self.link_eye_gaze_x else "Link Both"
            self.gaze_x_both_button.config(text=state)
            if self.link_eye_gaze_x:
                # Sync initial values
                left_val = self.gaze_x_left_slider.get()
                self.gaze_x_right_slider.set(left_val)
                self.face_pub.publish_eye_gaze_x('right', left_val)
        elif parameter == 'gaze_y':
            self.link_eye_gaze_y = not self.link_eye_gaze_y
            state = "Unlink" if self.link_eye_gaze_y else "Link Both"
            self.gaze_y_both_button.config(text=state)
            if self.link_eye_gaze_y:
                left_val = self.gaze_y_left_slider.get()
                self.gaze_y_right_slider.set(left_val)
                self.face_pub.publish_eye_gaze_y('right', left_val)
        elif parameter == 'scale_x':
            self.link_eye_scale_x = not self.link_eye_scale_x
            state = "Unlink" if self.link_eye_scale_x else "Link Both"
            self.scale_x_both_button.config(text=state)
            if self.link_eye_scale_x:
                left_val = self.scale_x_left_slider.get()
                self.scale_x_right_slider.set(left_val)
                self.face_pub.publish_eye_scale_x('right', left_val)
        elif parameter == 'scale_y':
            self.link_eye_scale_y = not self.link_eye_scale_y
            state = "Unlink" if self.link_eye_scale_y else "Link Both"
            self.scale_y_both_button.config(text=state)
            if self.link_eye_scale_y:
                left_val = self.scale_y_left_slider.get()
                self.scale_y_right_slider.set(left_val)
                self.face_pub.publish_eye_scale_y('right', left_val)
        elif parameter == 'lid_height':
            self.link_eye_lid_height = not self.link_eye_lid_height
            state = "Unlink" if self.link_eye_lid_height else "Link Both"
            self.lid_height_both_button.config(text=state)
            if self.link_eye_lid_height:
                left_val = self.lid_height_left_slider.get()
                self.lid_height_right_slider.set(left_val)
                self.face_pub.publish_eye_lid_height('right', left_val)
        elif parameter == 'lid_angle':
            self.link_eye_lid_angle = not self.link_eye_lid_angle
            state = "Unlink" if self.link_eye_lid_angle else "Link Both"
            self.lid_angle_both_button.config(text=state)
            if self.link_eye_lid_angle:
                left_val = self.lid_angle_left_slider.get()
                self.lid_angle_right_slider.set(left_val)
                self.face_pub.publish_eye_lid_angle('right', left_val)
        elif parameter == 'eye_color':
            self.link_eye_color = not self.link_eye_color
            state = "Unlink" if self.link_eye_color else "Link Both"
            self.eye_color_both_button.config(text=state)
            if self.link_eye_color:
                left_color = self.eye_color_left
                self.eye_color_right = left_color
                self.face_pub.publish_eye_color('right', self.eye_color_right)
                self.eye_color_right_button.config(bg=self.eye_color_right)

    def update_eye_gaze_x(self, eye_side, val):
        gaze_x = float(val)
        self.face_pub.publish_eye_gaze_x(eye_side, gaze_x)
        if eye_side == 'left' and self.link_eye_gaze_x:
            self.gaze_x_right_slider.set(val)
            self.face_pub.publish_eye_gaze_x('right', gaze_x)
        elif eye_side == 'right' and self.link_eye_gaze_x:
            self.gaze_x_left_slider.set(val)
            self.face_pub.publish_eye_gaze_x('left', gaze_x)

    def update_eye_gaze_y(self, eye_side, val):
        gaze_y = float(val)
        self.face_pub.publish_eye_gaze_y(eye_side, gaze_y)
        if eye_side == 'left' and self.link_eye_gaze_y:
            self.gaze_y_right_slider.set(val)
            self.face_pub.publish_eye_gaze_y('right', gaze_y)
        elif eye_side == 'right' and self.link_eye_gaze_y:
            self.gaze_y_left_slider.set(val)
            self.face_pub.publish_eye_gaze_y('left', gaze_y)

    def update_eye_scale_x(self, eye_side, val):
        scale_x = float(val)
        self.face_pub.publish_eye_scale_x(eye_side, scale_x)
        if eye_side == 'left' and self.link_eye_scale_x:
            self.scale_x_right_slider.set(val)
            self.face_pub.publish_eye_scale_x('right', scale_x)
        elif eye_side == 'right' and self.link_eye_scale_x:
            self.scale_x_left_slider.set(val)
            self.face_pub.publish_eye_scale_x('left', scale_x)

    def update_eye_scale_y(self, eye_side, val):
        scale_y = float(val)
        self.face_pub.publish_eye_scale_y(eye_side, scale_y)
        if eye_side == 'left' and self.link_eye_scale_y:
            self.scale_y_right_slider.set(val)
            self.face_pub.publish_eye_scale_y('right', scale_y)
        elif eye_side == 'right' and self.link_eye_scale_y:
            self.scale_y_left_slider.set(val)
            self.face_pub.publish_eye_scale_y('left', scale_y)

    def update_eye_lid_height(self, eye_side, val):
        lid_height = float(val)
        self.face_pub.publish_eye_lid_height(eye_side, lid_height)
        if eye_side == 'left' and self.link_eye_lid_height:
            self.lid_height_right_slider.set(val)
            self.face_pub.publish_eye_lid_height('right', lid_height)
        elif eye_side == 'right' and self.link_eye_lid_height:
            self.lid_height_left_slider.set(val)
            self.face_pub.publish_eye_lid_height('left', lid_height)

    def update_eye_lid_angle(self, eye_side, val):
        lid_angle = int(float(val))
        self.face_pub.publish_eye_lid_angle(eye_side, lid_angle)
        if eye_side == 'left' and self.link_eye_lid_angle:
            self.lid_angle_right_slider.set(val)
            self.face_pub.publish_eye_lid_angle('right', lid_angle)
        elif eye_side == 'right' and self.link_eye_lid_angle:
            self.lid_angle_left_slider.set(val)
            self.face_pub.publish_eye_lid_angle('left', lid_angle)

    def choose_eye_color(self, eye_side):
        color_code = colorchooser.askcolor(title=f"Choose Eye Color - {eye_side.capitalize()}")
        if color_code[1]:
            if eye_side == 'left':
                self.eye_color_left = color_code[1]
                self.face_pub.publish_eye_color('left', self.eye_color_left)
                self.eye_color_left_button.config(bg=self.eye_color_left)
                if self.link_eye_color:
                    self.eye_color_right = self.eye_color_left
                    self.face_pub.publish_eye_color('right', self.eye_color_right)
                    self.eye_color_right_button.config(bg=self.eye_color_right)
            elif eye_side == 'right':
                self.eye_color_right = color_code[1]
                self.face_pub.publish_eye_color('right', self.eye_color_right)
                self.eye_color_right_button.config(bg=self.eye_color_right)
                if self.link_eye_color:
                    self.eye_color_left = self.eye_color_right
                    self.face_pub.publish_eye_color('left', self.eye_color_left)
                    self.eye_color_left_button.config(bg=self.eye_color_left)

    def choose_mouth_color(self):
        color_code = colorchooser.askcolor(title="Choose Mouth Color")
        if color_code[1]:
            # Update the mouth_sine_params color
            self.mouth_sine_params["color"] = color_code[1]
            
            # Publish the updated MouthSine message
            self.face_pub.publish_mouth_sine(
                frequency=self.mouth_sine_params["frequency"],
                amplitude=self.mouth_sine_params["amplitude"],
                phase=self.mouth_sine_params["phase"],
                phase_increment=self.mouth_sine_params["phase_increment"],
                duration=self.mouth_sine_params["duration"],
                color=self.mouth_sine_params["color"]
            )
            
            # Update the button's background color
            self.mouth_color_button.config(bg=self.mouth_sine_params["color"])


    def update_mouth_sine(self, val):
        self.mouth_sine_params["frequency"] = float(self.mouth_freq_slider.get())
        self.mouth_sine_params["amplitude"] = float(self.mouth_amp_slider.get())
        self.mouth_sine_params["phase"] = float(self.mouth_phase_slider.get())
        self.mouth_sine_params["phase_increment"] = float(self.mouth_phase_inc_slider.get())
        self.face_pub.publish_mouth_sine(
            frequency=self.mouth_sine_params["frequency"],
            amplitude=self.mouth_sine_params["amplitude"],
            phase=self.mouth_sine_params["phase"],
            phase_increment=self.mouth_sine_params["phase_increment"],
            duration=self.mouth_sine_params["duration"],
            color=self.mouth_sine_params["color"]  # Use the updated color
        )

    def add_keyframe(self):
        # Collect current parameters
        actions = []

        # Eye Gaze X
        if self.link_eye_gaze_x:
            gaze_x = self.gaze_x_left_slider.get()
            actions.append({
                "state": "EyeGazeX",
                "parameters": {
                    "eye_side": "both",
                    "gaze_x": gaze_x
                }
            })
        else:
            actions.append({
                "state": "EyeGazeX",
                "parameters": {
                    "eye_side": "left",
                    "gaze_x": self.gaze_x_left_slider.get()
                }
            })
            actions.append({
                "state": "EyeGazeX",
                "parameters": {
                    "eye_side": "right",
                    "gaze_x": self.gaze_x_right_slider.get()
                }
            })

        # Eye Gaze Y
        if self.link_eye_gaze_y:
            gaze_y = self.gaze_y_left_slider.get()
            actions.append({
                "state": "EyeGazeY",
                "parameters": {
                    "eye_side": "both",
                    "gaze_y": gaze_y
                }
            })
        else:
            actions.append({
                "state": "EyeGazeY",
                "parameters": {
                    "eye_side": "left",
                    "gaze_y": self.gaze_y_left_slider.get()
                }
            })
            actions.append({
                "state": "EyeGazeY",
                "parameters": {
                    "eye_side": "right",
                    "gaze_y": self.gaze_y_right_slider.get()
                }
            })

        # Eye Scale X
        if self.link_eye_scale_x:
            scale_x = self.scale_x_left_slider.get()
            actions.append({
                "state": "EyeScaleX",
                "parameters": {
                    "eye_side": "both",
                    "scale_x": scale_x
                }
            })
        else:
            actions.append({
                "state": "EyeScaleX",
                "parameters": {
                    "eye_side": "left",
                    "scale_x": self.scale_x_left_slider.get()
                }
            })
            actions.append({
                "state": "EyeScaleX",
                "parameters": {
                    "eye_side": "right",
                    "scale_x": self.scale_x_right_slider.get()
                }
            })

        # Eye Scale Y
        if self.link_eye_scale_y:
            scale_y = self.scale_y_left_slider.get()
            actions.append({
                "state": "EyeScaleY",
                "parameters": {
                    "eye_side": "both",
                    "scale_y": scale_y
                }
            })
        else:
            actions.append({
                "state": "EyeScaleY",
                "parameters": {
                    "eye_side": "left",
                    "scale_y": self.scale_y_left_slider.get()
                }
            })
            actions.append({
                "state": "EyeScaleY",
                "parameters": {
                    "eye_side": "right",
                    "scale_y": self.scale_y_right_slider.get()
                }
            })

        # Eye Lid Height
        if self.link_eye_lid_height:
            lid_height = self.lid_height_left_slider.get()
            actions.append({
                "state": "EyeLidHeight",
                "parameters": {
                    "eye_side": "both",
                    "lid_height": lid_height
                }
            })
        else:
            actions.append({
                "state": "EyeLidHeight",
                "parameters": {
                    "eye_side": "left",
                    "lid_height": self.lid_height_left_slider.get()
                }
            })
            actions.append({
                "state": "EyeLidHeight",
                "parameters": {
                    "eye_side": "right",
                    "lid_height": self.lid_height_right_slider.get()
                }
            })

        # Eye Lid Angle
        if self.link_eye_lid_angle:
            lid_angle = self.lid_angle_left_slider.get()
            actions.append({
                "state": "EyeLidAngle",
                "parameters": {
                    "eye_side": "both",
                    "lid_angle": lid_angle
                }
            })
        else:
            actions.append({
                "state": "EyeLidAngle",
                "parameters": {
                    "eye_side": "left",
                    "lid_angle": self.lid_angle_left_slider.get()
                }
            })
            actions.append({
                "state": "EyeLidAngle",
                "parameters": {
                    "eye_side": "right",
                    "lid_angle": self.lid_angle_right_slider.get()
                }
            })

        # Eye Color
        if self.link_eye_color:
            color = self.eye_color_left  # Both are the same
            actions.append({
                "state": "EyeColor",
                "parameters": {
                    "eye_side": "both",
                    "color": color
                }
            })
        else:
            actions.append({
                "state": "EyeColor",
                "parameters": {
                    "eye_side": "left",
                    "color": self.eye_color_left
                }
            })
            actions.append({
                "state": "EyeColor",
                "parameters": {
                    "eye_side": "right",
                    "color": self.eye_color_right
                }
            })

        # Mouth Sine
        mouth_params = {
            "frequency": self.mouth_sine_params["frequency"],
            "amplitude": self.mouth_sine_params["amplitude"],
            "phase": self.mouth_sine_params["phase"],
            "phase_increment": self.mouth_sine_params["phase_increment"],
            "color": self.mouth_sine_params["color"]
        }
        actions.append({
            "state": "MouthSine",
            "parameters": mouth_params
        })

        # Add the actions list as a keyframe
        self.keyframes.append(actions)
        messagebox.showinfo("Keyframe Added", f"Keyframe {len(self.keyframes)} added.")

    def save_sequence(self):
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

        # Create the sequence dictionary as per the new JSON format
        sequence = [
            {
                "emoji": emoji,
                "reasoning": reasoning,
                "frames": self.keyframes  # List of keyframes (each is a list of actions)
            }
        ]

        # Ensure the presets directory exists
        presets_dir = "/home/robot/robot_ws/animations/face"  # Updated to absolute path
        os.makedirs(presets_dir, exist_ok=True)

        # Generate a timestamped filename
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{presets_dir}/emoji_face_seq_{timestamp}.json"

        # Save the sequence to a file
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                # Write JSON as a list with one object, adhering to the new format
                json.dump(sequence, f, indent=4, ensure_ascii=False)
            messagebox.showinfo("Success", f"Sequence saved to {filename}")

            # Clear the sequence after saving
            self.clear_sequence()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save sequence: {e}")

    def clear_sequence(self):
        """
        Clears the current sequence of keyframes.
        """
        if messagebox.askyesno("Confirm Clear", "Are you sure you want to clear the current sequence?"):
            self.keyframes = []
            messagebox.showinfo("Sequence Cleared", "The current sequence has been cleared.")

    def test_sequence(self):
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

                if action_type == "MouthSine":
                    self.face_pub.publish_mouth_sine(
                        frequency=params["frequency"],
                        amplitude=params["amplitude"],
                        phase=params["phase"],
                        phase_increment=params["phase_increment"],
                        duration=1.0,  # Set duration to 1.0 for testing
                        color=params["color"]
                    )
                elif action_type == "EyeGazeX":
                    self.face_pub.publish_eye_gaze_x(params["eye_side"], float(params["gaze_x"]), duration=1.0)
                elif action_type == "EyeGazeY":
                    self.face_pub.publish_eye_gaze_y(params["eye_side"], float(params["gaze_y"]), duration=1.0)
                elif action_type == "EyeScaleX":
                    self.face_pub.publish_eye_scale_x(params["eye_side"], float(params["scale_x"]), duration=1.0)
                elif action_type == "EyeScaleY":
                    self.face_pub.publish_eye_scale_y(params["eye_side"], float(params["scale_y"]), duration=1.0)
                elif action_type == "EyeLidHeight":
                    self.face_pub.publish_eye_lid_height(params["eye_side"], float(params["lid_height"]), duration=1.0)
                elif action_type == "EyeLidAngle":
                    self.face_pub.publish_eye_lid_angle(params["eye_side"], int(float(params["lid_angle"])), duration=1.0)
                elif action_type == "EyeColor":
                    self.face_pub.publish_eye_color(params["eye_side"], params["color"], duration=1.0)
                else:
                    rospy.logwarn(f"Unknown action type: {action_type}")

            # Pause for 2 seconds (1 second for animation + 1 second hold)
            time.sleep(2.0)

        rospy.loginfo("Test sequence completed.")
        messagebox.showinfo("Test Sequence", "Test sequence completed.")

    def update_face(self):
        """
        Reads all current slider and color picker values and publishes them to ensure the face is in sync.
        """
        rospy.loginfo("Updating face with current GUI values...")

        # Eye Gaze X
        if self.link_eye_gaze_x:
            gaze_x = self.gaze_x_left_slider.get()
            self.face_pub.publish_eye_gaze_x('both', gaze_x)
        else:
            gaze_x_left = self.gaze_x_left_slider.get()
            gaze_x_right = self.gaze_x_right_slider.get()
            self.face_pub.publish_eye_gaze_x('left', gaze_x_left)
            self.face_pub.publish_eye_gaze_x('right', gaze_x_right)

        # Eye Gaze Y
        if self.link_eye_gaze_y:
            gaze_y = self.gaze_y_left_slider.get()
            self.face_pub.publish_eye_gaze_y('both', gaze_y)
        else:
            gaze_y_left = self.gaze_y_left_slider.get()
            gaze_y_right = self.gaze_y_right_slider.get()
            self.face_pub.publish_eye_gaze_y('left', gaze_y_left)
            self.face_pub.publish_eye_gaze_y('right', gaze_y_right)

        # Eye Scale X
        if self.link_eye_scale_x:
            scale_x = self.scale_x_left_slider.get()
            self.face_pub.publish_eye_scale_x('both', scale_x)
        else:
            scale_x_left = self.scale_x_left_slider.get()
            scale_x_right = self.scale_x_right_slider.get()
            self.face_pub.publish_eye_scale_x('left', scale_x_left)
            self.face_pub.publish_eye_scale_x('right', scale_x_right)

        # Eye Scale Y
        if self.link_eye_scale_y:
            scale_y = self.scale_y_left_slider.get()
            self.face_pub.publish_eye_scale_y('both', scale_y)
        else:
            scale_y_left = self.scale_y_left_slider.get()
            scale_y_right = self.scale_y_right_slider.get()
            self.face_pub.publish_eye_scale_y('left', scale_y_left)
            self.face_pub.publish_eye_scale_y('right', scale_y_right)

        # Eye Lid Height
        if self.link_eye_lid_height:
            lid_height = self.lid_height_left_slider.get()
            self.face_pub.publish_eye_lid_height('both', lid_height)
        else:
            lid_height_left = self.lid_height_left_slider.get()
            lid_height_right = self.lid_height_right_slider.get()
            self.face_pub.publish_eye_lid_height('left', lid_height_left)
            self.face_pub.publish_eye_lid_height('right', lid_height_right)

        # Eye Lid Angle
        if self.link_eye_lid_angle:
            lid_angle = self.lid_angle_left_slider.get()
            self.face_pub.publish_eye_lid_angle('both', lid_angle)
        else:
            lid_angle_left = self.lid_angle_left_slider.get()
            lid_angle_right = self.lid_angle_right_slider.get()
            self.face_pub.publish_eye_lid_angle('left', lid_angle_left)
            self.face_pub.publish_eye_lid_angle('right', lid_angle_right)

        # Eye Color
        if self.link_eye_color:
            color = self.eye_color_left  # Both are the same
            self.face_pub.publish_eye_color('both', color)
        else:
            self.face_pub.publish_eye_color('left', self.eye_color_left)
            self.face_pub.publish_eye_color('right', self.eye_color_right)

        # Mouth Sine
        self.face_pub.publish_mouth_sine(
            frequency=self.mouth_sine_params["frequency"],
            amplitude=self.mouth_sine_params["amplitude"],
            phase=self.mouth_sine_params["phase"],
            phase_increment=self.mouth_sine_params["phase_increment"],
            duration=self.mouth_sine_params["duration"],
            color=self.mouth_sine_params["color"]  # Use the updated color
        )

        rospy.loginfo("Face updated successfully.")
        messagebox.showinfo("Update Face", "Face updated successfully.")


    def run(self):
        self.root.mainloop()

def ros_spin():
    rospy.spin()

if __name__ == "__main__":
    face_pub = FacePublisher()
    gui = AnimationToolGUI(face_pub)

    # Start ROS spin in a separate thread
    ros_thread = threading.Thread(target=ros_spin)
    ros_thread.daemon = True
    ros_thread.start()

    # Run the GUI
    gui.run()
