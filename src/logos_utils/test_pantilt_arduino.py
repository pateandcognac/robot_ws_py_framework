import rospy
import readchar
from std_msgs.msg import String, Int32, Int32MultiArray, UInt8

class PanTiltController:
    def __init__(self):
        self.pan_tilt_move_pub = rospy.Publisher('/pan_tilt/move', String, queue_size=10) # home, up, down, left, right. moves by 5 servo ticks from current position.
        self.pan_pos_pub = rospy.Publisher('/pan_tilt/move/pan', Int32, queue_size=10) # literal servo values
        self.tilt_pos_pub = rospy.Publisher('/pan_tilt/move/tilt', Int32, queue_size=10)
        self.laser_pub = rospy.Publisher('/pan_tilt/laser', UInt8, queue_size=10) # laser brightness 0=off to 255=full on
        self.rgb_led_pub = rospy.Publisher('/pan_tilt/rgbled', Int32MultiArray, queue_size=10)

        self.HOME_PAN_POS = 400  # HOME_PAN position
        self.HOME_TILT_POS = 400  # HOME_TILT position
        self.PAN_MIN = 150
        self.PAN_MAX = 600
        self.TILT_MIN = 225
        self.TILT_MAX = 550
        self.STEP_SIZE = 1

# Let's have logic that controls the pan tilt mech using degrees around the home position

        self.current_pan_pos = self.HOME_PAN_POS
        self.current_tilt_pos = self.HOME_TILT_POS
        self.desired_pan_pos = self.current_pan_pos
        self.desired_tilt_pos = self.current_tilt_pos
        self.laser_on = False
        self.leds_on = False

    def pan_pos_callback(self, msg):
        self.current_pan_pos = msg.data
        print(f"{self.current_pan_pos=}")

    def tilt_pos_callback(self, msg):
        self.current_tilt_pos = msg.data
        print(f"{self.current_tilt_pos=}")


    def publish_move_command(self, command):
        msg = String()
        msg.data = command
        self.pan_tilt_move_pub.publish(msg)

    def publish_pan_pos(self, pos):
        msg = Int32()
        msg.data = pos
        self.pan_pos_pub.publish(msg)

    def publish_tilt_pos(self, pos):
        msg = Int32()
        msg.data = pos
        self.tilt_pos_pub.publish(msg)

    def toggle_rgb_leds(self):
        self.leds_on = not self.leds_on
        msg = Int32MultiArray()
        data = []

        color = 0xFFFFFF if self.leds_on else 0x000000
        for led in range(5):  # Assume 5 LEDs in the pan_tilt strip
            led_data = (led << 24) | color
            data.append(led_data)

        msg.data = data
        self.rgb_led_pub.publish(msg)
        print(f"RGB LEDs {'ON' if self.leds_on else 'OFF'}")

    def toggle_laser(self):
        self.laser_on = not self.laser_on
        msg = UInt8()
        msg.data = 255 if self.laser_on else 0
        self.laser_pub.publish(msg)
        print(f"Laser {'ON' if self.laser_on else 'OFF'}")

    def process_input(self, key):
        if key == ' ': 
            self.desired_pan_pos = self.HOME_PAN_POS
            self.publish_pan_pos(self.desired_pan_pos)
            self.desired_tilt_pos = self.HOME_TILT_POS
            self.publish_tilt_pos(self.desired_tilt_pos)
        elif key == 'j':
            self.desired_pan_pos = max(self.PAN_MIN, self.desired_pan_pos - self.STEP_SIZE)
            self.publish_pan_pos(self.desired_pan_pos)
        elif key == 'l':
            self.desired_pan_pos = min(self.PAN_MAX, self.desired_pan_pos + self.STEP_SIZE)
            self.publish_pan_pos(self.desired_pan_pos)
        elif key == 'k':
            self.desired_tilt_pos = max(self.TILT_MIN, self.desired_tilt_pos - self.STEP_SIZE)
            self.publish_tilt_pos(self.desired_tilt_pos)
        elif key == 'i':
            self.desired_tilt_pos = min(self.TILT_MAX, self.desired_tilt_pos + self.STEP_SIZE)
            self.publish_tilt_pos(self.desired_tilt_pos)
        elif key == 'p':
            self.toggle_laser()
        elif key == 'f':
            self.toggle_rgb_leds()

        # print(f"{self.current_pan_pos=}")
        # print(f"{self.current_tilt_pos=}")


def main():
    rospy.init_node('pan_tilt_controller', anonymous=True)
    controller = PanTiltController()

    print("Pan-Tilt Controller Node Started.")
    print("Use the following keys to control the pan-tilt mechanism:")
    print("  space: home")
    print("  j: Decrease pan position by STEP_SIZE")
    print("  l: Increase pan position by STEP_SIZE")
    print("  k: Decrease tilt position by STEP_SIZE")
    print("  i: Increase tilt position by STEP_SIZE")
    print("  f: Toggle flash (RGB LEDs full white)")
    print("  q: Quit")

    while not rospy.is_shutdown():
        key = readchar.readchar().lower()
        if key == 'q':
            break
        controller.process_input(key)


if __name__ == '__main__':
    main()
