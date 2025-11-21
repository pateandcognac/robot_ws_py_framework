#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include <ros.h>
#include <std_msgs/String.h>
#include <std_msgs/Int32.h>
#include <Adafruit_NeoPixel.h>
#include <std_msgs/Int32MultiArray.h>
#include <std_msgs/UInt8.h>

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver();

#define PAN_CHANNEL 0
#define TILT_CHANNEL 1
#define PAN_MIN 150
#define PAN_MAX 600
#define TILT_MIN 225
#define TILT_MAX 550
#define HOME_PAN 400
#define HOME_TILT 400
#define DELAY_TIME 5
#define STEP_SIZE 5
#define POWER_DOWN_TIMEOUT 1000 // 1 second

#define LASER_PIN 7
#define NOTIFICATION_PIN 4
#define FACE_LED_PIN 8
#define PANTILT_LED_PIN 6
#define NOTIFICATION_LED_COUNT 16
#define FACE_LED_COUNT 12
#define PANTILT_LED_COUNT 5
#define LED_TIMEOUT 30000 // milliseconds
#define LASER_TIMEOUT 60000 // 1 minute in milliseconds
#define TILT_WIDE_MIN 150
#define TILT_WIDE_MAX 600

ros::NodeHandle nh;

uint16_t current_pan = HOME_PAN;
uint16_t current_tilt = HOME_TILT;
int last_published_pan = HOME_PAN;
int last_published_tilt = HOME_TILT;
unsigned long last_command_time = 0;
bool servos_powered = true;
unsigned long last_laser_time = 0;

Adafruit_NeoPixel notification_strip(NOTIFICATION_LED_COUNT, NOTIFICATION_PIN, NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel pantilt_strip(PANTILT_LED_COUNT, PANTILT_LED_PIN, NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel face_strip(FACE_LED_COUNT, FACE_LED_PIN, NEO_GRB + NEO_KHZ800);

unsigned long led_last_update[FACE_LED_COUNT + PANTILT_LED_COUNT] = {0};  // Updated to handle both strips

void panTiltCallback(const std_msgs::String& cmd_msg) {
  String cmd = cmd_msg.data;

  int pan_step = 0;
  int tilt_step = 0;

  if (cmd == "home") {
    moveServo(PAN_CHANNEL, HOME_PAN);
    moveServo(TILT_CHANNEL, HOME_TILT);
    current_pan = HOME_PAN;
    current_tilt = HOME_TILT;
    last_command_time = millis();
    powerUpServos();
    return;
  }

  if (cmd.indexOf("right") != -1) {
    pan_step = -STEP_SIZE;
  } else if (cmd.indexOf("left") != -1) {
    pan_step = STEP_SIZE;
  }
  
  if (cmd.indexOf("up") != -1) {
    tilt_step = -STEP_SIZE;
  } else if (cmd.indexOf("down") != -1) {
    tilt_step = STEP_SIZE;
  }
  
  movePanTilt(pan_step, tilt_step);
  last_command_time = millis();
  powerUpServos();
}

void panPosCallback(const std_msgs::Int32& pos_msg) {
  int pan_pulse = constrain(pos_msg.data, PAN_MIN, PAN_MAX);
  moveServo(PAN_CHANNEL, pan_pulse);
  current_pan = pan_pulse;
  last_command_time = millis();
  powerUpServos();
}

void tiltPosCallback(const std_msgs::Int32& pos_msg) {
  int tilt_pulse = constrain(pos_msg.data, TILT_MIN, TILT_MAX);
  moveServo(TILT_CHANNEL, tilt_pulse);
  current_tilt = tilt_pulse;
  last_command_time = millis();
  powerUpServos();
}

void laserCallback(const std_msgs::UInt8& msg) {
  uint8_t laser_value = constrain(msg.data, 0, 255);
  analogWrite(LASER_PIN, laser_value);
  if (laser_value > 0) {
    last_laser_time = millis();
  }
}

void notificationLedCallback(const std_msgs::Int32MultiArray& msg) {
  updateLEDs(msg, notification_strip, 0);
}

void pantiltLedCallback(const std_msgs::Int32MultiArray& msg) {
  updateLEDs(msg, pantilt_strip, FACE_LED_COUNT);  // Offset adjusted for the face strip
}

void faceLedCallback(const std_msgs::Int32MultiArray& msg) {
  updateLEDs(msg, face_strip, 0);  // No offset for the face LED strip
}

void updateLEDs(const std_msgs::Int32MultiArray& msg, Adafruit_NeoPixel& strip, int offset) {
  for (int i = 0; i < msg.data_length; i++) {
    uint32_t data = msg.data[i];
    uint8_t led_number = data >> 24;
    uint32_t color = data & 0xFFFFFF;
    
    if (led_number < strip.numPixels()) {
      strip.setPixelColor(led_number, color);
      led_last_update[offset + led_number] = millis();
    }
  }
  strip.show();
}

ros::Subscriber<std_msgs::String> move_sub("/pan_tilt/move", &panTiltCallback);
ros::Subscriber<std_msgs::Int32> pan_pos_sub("/pan_tilt/move/pan", &panPosCallback);
ros::Subscriber<std_msgs::Int32> tilt_pos_sub("/pan_tilt/move/tilt", &tiltPosCallback);
ros::Subscriber<std_msgs::UInt8> laser_sub("/pan_tilt/laser", &laserCallback);
ros::Subscriber<std_msgs::Int32MultiArray> notification_led_sub("/notification/rgbled", &notificationLedCallback);
ros::Subscriber<std_msgs::Int32MultiArray> pantilt_led_sub("/pan_tilt/rgbled", &pantiltLedCallback);
ros::Subscriber<std_msgs::Int32MultiArray> face_led_sub("/face/rgbled", &faceLedCallback);  // New subscriber for face LEDs

std_msgs::Int32 current_pan_pos;
std_msgs::Int32 current_tilt_pos;
ros::Publisher pan_pos_pub("/pan_tilt/pan_pos", &current_pan_pos);
ros::Publisher tilt_pos_pub("/pan_tilt/tilt_pos", &current_tilt_pos);

void setup() {
  pwm.begin();
  pwm.setPWMFreq(50);
  moveServo(PAN_CHANNEL, HOME_PAN);
  moveServo(TILT_CHANNEL, HOME_TILT);
  
  pinMode(LASER_PIN, OUTPUT);
  notification_strip.begin();
  pantilt_strip.begin();
  face_strip.begin();  // Initialize the face LED strip
  
  nh.initNode();
  nh.getHardware()->setBaud(57600);
  nh.subscribe(move_sub);
  nh.subscribe(pan_pos_sub);
  nh.subscribe(tilt_pos_sub);
  nh.subscribe(laser_sub);
  nh.subscribe(notification_led_sub);
  nh.subscribe(pantilt_led_sub);
  nh.subscribe(face_led_sub);  // Subscribe to the face LED topic
  nh.advertise(pan_pos_pub);
  nh.advertise(tilt_pos_pub);
  // Serial.println("Arduino initialized");

}

void loop() {
  // Update and publish current positions
if (current_pan != last_published_pan) {
  current_pan_pos.data = current_pan;
  pan_pos_pub.publish(&current_pan_pos);
  last_published_pan = current_pan;
}

if (current_tilt != last_published_tilt) {
  current_tilt_pos.data = current_tilt;
  tilt_pos_pub.publish(&current_tilt_pos);
  last_published_tilt = current_tilt;
}

  
  // Power down servos if no commands received for POWER_DOWN_TIMEOUT
  if (millis() - last_command_time > POWER_DOWN_TIMEOUT && servos_powered) {
    powerDownServos();
  }
  
  // Turn off laser if timeout reached
  if (millis() - last_laser_time > LASER_TIMEOUT) {
    analogWrite(LASER_PIN, 0);
  }
  
  checkLEDTimeout();
  
  nh.spinOnce();
  delay(6);
}

void moveServo(uint8_t channel, uint16_t pulse) {
  pwm.setPWM(channel, 0, pulse);
  delay(DELAY_TIME);
}

void movePanTilt(int pan_step, int tilt_step) {
  current_pan = constrain(current_pan + pan_step, PAN_MIN, PAN_MAX);
  current_tilt = constrain(current_tilt + tilt_step, TILT_MIN, TILT_MAX);
  
  moveServo(PAN_CHANNEL, current_pan);
  moveServo(TILT_CHANNEL, current_tilt);
}

void powerUpServos() {
  if (!servos_powered) {
    pwm.setPWM(PAN_CHANNEL, 0, current_pan);
    pwm.setPWM(TILT_CHANNEL, 0, current_tilt);
    servos_powered = true;
  }
}

void powerDownServos() {
  pwm.setPWM(PAN_CHANNEL, 0, 0);
  pwm.setPWM(TILT_CHANNEL, 0, 0);
  servos_powered = false;
}

void checkLEDTimeout() {
  unsigned long current_time = millis();
  
  for (int i = 0; i < FACE_LED_COUNT; i++) {
    if (current_time - led_last_update[i] > LED_TIMEOUT) {
      face_strip.setPixelColor(i, 0);  // Turn off face LED if timeout
    }
  }
  
  for (int i = 0; i < PANTILT_LED_COUNT; i++) {
    if (current_time - led_last_update[FACE_LED_COUNT + i] > LED_TIMEOUT) {
      pantilt_strip.setPixelColor(i, 0);  // Turn off pantilt LED if timeout
    }
  }
  
  face_strip.show();
  pantilt_strip.show();
}
