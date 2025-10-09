#!/usr/bin/env python3
import urwid
import rospy
from logos_framework.msg import CognitionInput, CognitionOutput
from datetime import datetime

# Global reference to the UI class
ui = None

def get_timestamp():
    return datetime.now().strftime("[%H:%M:%S]")

class ChatInterface:
    def __init__(self):
        # --- Chat output widgets ---
        self.output_walker = urwid.SimpleListWalker([urwid.Text(f"{get_timestamp()} Logos TUI Initialized. Ready for input.")])
        self.listbox = urwid.ListBox(self.output_walker)
        self.listbox_widget = urwid.LineBox(self.listbox, title="Logos Framework I/O")

        # --- Multiline edit for user input ---
        self.edit_widget = urwid.Edit("", multiline=True, allow_tab=True)
        self.input_box = urwid.LineBox(self.edit_widget, title="<human> Input (Alt+Enter to send)")

        # --- Main frame ---
        self.frame = urwid.Frame(
            body=urwid.AttrWrap(self.listbox_widget, 'body'),
            footer=urwid.AttrWrap(self.input_box, 'footer')
        )

        # --- Color palette ---
        palette = [
            ('body', 'light gray', 'dark blue'),
            ('footer', 'light gray', 'black'),
            ('title', 'white', 'dark blue', 'bold'),
            ('human', 'white', 'dark blue'),
            ('llm_chunk', 'light green', 'dark blue'),
            ('llm_final', 'white', 'dark blue', 'bold'),
            ('thoughts', 'dark cyan', 'dark blue'),
            ('py_result', 'yellow', 'dark blue'),
            ('context', 'light magenta', 'dark blue'),
            ('system', 'light red', 'dark blue'),
        ]

        # --- ROS Publisher ---
        self.input_pub = rospy.Publisher('/cognition/input', CognitionInput, queue_size=10)

        # --- Main loop ---
        self.loop = urwid.MainLoop(self.frame, palette, unhandled_input=self.handle_input)
        self.is_streaming = False

    def handle_input(self, key):
        if key == 'meta enter': # Alt+Enter
            self.submit_input()
        elif key == 'tab':
            # Switch focus between chat output and input box
            if self.frame.focus_position == 'body':
                self.frame.focus_position = 'footer'
            else:
                self.frame.focus_position = 'body'
        else:
            return key # Propagate unhandled keys

    def submit_input(self):
        raw_text = self.edit_widget.get_edit_text()
        trimmed_text = raw_text.strip()

        if trimmed_text.lower() in ('exit', 'quit'):
            raise urwid.ExitMainLoop()

        if trimmed_text:
            # For our framework, human input is simple and direct.
            # The cognition node will handle appending it to the buffer.
            msg = CognitionInput(
                type='human',
                content=trimmed_text,
                system_hint="",
                loop_cognition=True # Human input should always trigger a cognition cycle
            )
            self.input_pub.publish(msg)
            self.add_to_output(f"Human:\n{trimmed_text}", 'human')

        self.edit_widget.set_edit_text('')

    def add_to_output(self, message, style, is_chunk=False):
        timestamp = get_timestamp()

        if is_chunk and self.is_streaming:
            # Append to the last message if it was also a chunk
            last_widget = self.output_walker[-1]
            last_text = last_widget.get_text()[0] # Urwid text is a tuple (style, text)
            last_widget.set_text(last_text + message)
        else:
            # Add a new line for a new message
            text_widget = urwid.Text((style, f"{timestamp} {message}"))
            self.output_walker.append(text_widget)

        self.is_streaming = is_chunk

        # Auto-scroll to the bottom
        self.listbox.set_focus(len(self.output_walker) - 1)
        self.loop.draw_screen()

    def ros_output_callback(self, msg: CognitionOutput):
        """Handles messages from the cognition node and displays them."""
        if msg.type == 'chunk':
            # msg.content contains the streamed text token
            self.add_to_output(msg.content, 'llm_chunk', is_chunk=True)
        elif msg.type == 'thoughts':
            self.add_to_output(f"Logos (thinking):\n{msg.content}", 'thoughts')
        elif msg.type == 'llm':
             # This is the final, complete message from the LLM
            self.add_to_output(f"Logos:\n{msg.content}", 'llm_final')
        # We can add more handlers here if we want to see other message types
        # For now, we'll ignore 'context', 'state', etc. for a cleaner UI.

    def ros_input_callback(self, msg: CognitionInput):
        """
        Optional: Echoes back non-human inputs to the TUI for visibility.
        Useful for seeing py_result, py_async, etc.
        """
        if msg.type != 'human': # We already display human input on submit
            self.add_to_output(f"{msg.type.upper()}:\n{msg.content}", msg.type)


    def run(self):
        # Set up a timer to process ROS events within the urwid loop
        def refresh_ros(loop, user_data):
            rospy.sleep(0.1) # Prevents busy-waiting
            loop.set_alarm_in(0.1, refresh_ros)

        self.loop.set_alarm_in(0.1, refresh_ros)
        self.loop.run()

def main():
    global ui
    rospy.init_node('logos_tui_node')
    ui = ChatInterface()

    # Subscribe to the output of the cognition node to display it
    rospy.Subscriber('/cognition/output', CognitionOutput, ui.ros_output_callback)
    # Subscribe to the input to see system-generated messages, py_result, etc
    rospy.Subscriber('/cognition/input', CognitionInput, ui.ros_input_callback)

    try:
        ui.run()
    except KeyboardInterrupt:
        print("Exiting TUI...")
    except Exception as e:
        # This will catch urwid errors on exit and prevent a messy traceback
        rospy.logerr(f"TUI crashed: {e}")

if __name__ == "__main__":
    main()