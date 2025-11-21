#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver();

#define SERVOMIN  150 // This is the 'minimum' pulse length count (out of 4096)
#define SERVOMAX  600 // This is the 'maximum' pulse length count (out of 4096)

void setup() {
  Serial.begin(9600);
  pwm.begin();
  pwm.setPWMFreq(60);  // Analog servos run at ~50 Hz updates

  Serial.println("Servo Calibration");
  Serial.println("Enter channel number (0-15) and position (0-180) separated by a comma");
  Serial.println("Example: 3,90");
}

void loop() {
  if (Serial.available() > 0) {
    String input = Serial.readStringUntil('\n');
    int commaIndex = input.indexOf(',');
    
    if (commaIndex != -1) {
      int channel = input.substring(0, commaIndex).toInt();
      int position = input.substring(commaIndex + 1).toInt();
      
      if (channel >= 0 && channel <= 15 && position >= 0 && position <= 180) {
        int pulse = map(position, 0, 180, SERVOMIN, SERVOMAX);
        pwm.setPWM(channel, 0, pulse);
        
        Serial.print("Set channel ");
        Serial.print(channel);
        Serial.print(" to position ");
        Serial.println(position);
      } else {
        Serial.println("Invalid input. Channel should be 0-15, position should be 0-180.");
      }
    } else {
      Serial.println("Invalid input format. Use 'channel,position'");
    }
  }
}