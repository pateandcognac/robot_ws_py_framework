#include <ros.h>
#include <std_msgs/Float32.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

// Initialize ROS node handle
ros::NodeHandle nh;

// Initialize PWM driver
Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver();

// Define servo frequency
#define SERVO_FREQ 60 // These servos seem to run best ~60 Hz updates???
#define TIMEOUT 1000  // 1 second timeout for powering down servos

// Variables to track the last command time for each servo
unsigned long lastCommandTime[6] = {0, 0, 0, 0, 0, 0};

// Function to move joints (existing functionality)
void moveServo(uint8_t channel, float angle) {
  // Map angle from -90 to 90 degrees to pulse width from 150 to 600
  uint16_t pulseWidth = map(angle, -90, 90, 150, 600);
  pwm.setPWM(channel, 0, pulseWidth);
  lastCommandTime[channel] = millis();  // Update the last command time for this servo
}

// Function to power down servo
void powerDownServo(uint8_t channel) {
  pwm.setPWM(channel, 0, 0);  // Stop sending PWM signal to the servo
}

// New function to move wrists
void moveWrist(uint8_t channel, float angle) {
  // Map angle from 0 to 180 degrees to pulse width from 150 to 600
  uint16_t pulseWidth = map(angle, -90, 90, 150, 600);
  pwm.setPWM(channel, 0, pulseWidth);
  lastCommandTime[channel] = millis();  // Update the last command time for this servo
}

// Callback for left joint1
void leftJoint1Callback(const std_msgs::Float32& msg) {
  moveServo(0, msg.data);
}

// Callback for left joint2
void leftJoint2Callback(const std_msgs::Float32& msg) {
  moveServo(1, -msg.data);  // Invert for left arm
}

// Callback for right joint1
void rightJoint1Callback(const std_msgs::Float32& msg) {
  moveServo(2, -msg.data);  // Invert for right arm
}

// Callback for right joint2
void rightJoint2Callback(const std_msgs::Float32& msg) {
  moveServo(3, msg.data);
}

// Callback for both joint1
void bothJoint1Callback(const std_msgs::Float32& msg) {
  moveServo(0, msg.data);     // Left joint1
  moveServo(2, -msg.data);    // Right joint1 (inverted)
}

// Callback for both joint2
void bothJoint2Callback(const std_msgs::Float32& msg) {
  moveServo(1, -msg.data);    // Left joint2 (inverted)
  moveServo(3, msg.data);     // Right joint2
}

// New Callback for left wrist
void leftWristCallback(const std_msgs::Float32& msg) {
  moveWrist(4, msg.data);  // Channel 4 for left wrist
}

// New Callback for right wrist
void rightWristCallback(const std_msgs::Float32& msg) {
  moveWrist(5, -msg.data);  // Channel 5 for right wrist (inverted)
}

// New Callback for both wrists
void bothWristCallback(const std_msgs::Float32& msg) {
  moveWrist(4, msg.data);  // Left wrist
  moveWrist(5, -msg.data);  // Right wrist (inverted)
}

// Subscribers for individual joints
ros::Subscriber<std_msgs::Float32> sub1("/arm/left/joint1", leftJoint1Callback);
ros::Subscriber<std_msgs::Float32> sub2("/arm/left/joint2", leftJoint2Callback);
ros::Subscriber<std_msgs::Float32> sub3("/arm/right/joint1", rightJoint1Callback);
ros::Subscriber<std_msgs::Float32> sub4("/arm/right/joint2", rightJoint2Callback);

// Subscribers for controlling both joints
ros::Subscriber<std_msgs::Float32> subBoth1("/arm/both/joint1", bothJoint1Callback);
ros::Subscriber<std_msgs::Float32> subBoth2("/arm/both/joint2", bothJoint2Callback);

// New Subscribers for wrists
ros::Subscriber<std_msgs::Float32> sub5("/arm/left/wrist", leftWristCallback);
ros::Subscriber<std_msgs::Float32> sub6("/arm/right/wrist", rightWristCallback);
ros::Subscriber<std_msgs::Float32> subBothWrist("/arm/both/wrist", bothWristCallback);

void setup() {
  // Initialize ROS node
  nh.initNode();

  // Subscribe to joint topics
  nh.subscribe(sub1);
  nh.subscribe(sub2);
  nh.subscribe(sub3);
  nh.subscribe(sub4);
  nh.subscribe(subBoth1);
  nh.subscribe(subBoth2);

  // Subscribe to wrist topics
  nh.subscribe(sub5);
  nh.subscribe(sub6);
  nh.subscribe(subBothWrist);

  // Initialize PWM driver
  pwm.begin();
  pwm.setOscillatorFrequency(27000000);
  pwm.setPWMFreq(SERVO_FREQ);  // Analog servos run at ~60 Hz
}

void loop() {
  nh.spinOnce();
  
  // Check if any servos have timed out (1 second without command)
  unsigned long currentTime = millis();
  for (uint8_t i = 0; i < 6; i++) {
    if (currentTime - lastCommandTime[i] > TIMEOUT) {
      powerDownServo(i);  // Power down servo if timeout reached
    }
  }

  delay(1);
}
