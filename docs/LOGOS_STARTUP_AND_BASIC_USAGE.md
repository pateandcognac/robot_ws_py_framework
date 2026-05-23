# Logos Startup and Basic Usage

This guide is for a person who has not used Linux or ROS before. It is meant
to get Logos from "powered off" to "ready to talk" without needing to know how
the robot software works internally.

## What Logos Is In This Setup

Logos is one computer doing several jobs:

- The ThinkPad strapped to Logos's back is the robot computer.
- The normal desktop monitor is the main computer screen. It is attached over
  wireless HDMI.
- The separate HDMI monitor used for Logos's face is a second screen.
- Logos's face is text art drawn inside a terminal. It is not a normal
  graphical face app window.
- The Kobuki mobile base has its own power. Turn it on when Logos should have
  the powered robot body available.

The ThinkPad laptop screen is broken. That is expected. Use the external
monitors.

## Physical Boot Checklist

1. Make sure the ThinkPad and the external displays have power.
2. Make sure the wireless HDMI receiver for the main desktop display is
   connected and powered.
3. Make sure the HDMI face monitor is connected and powered. Its power button
   is on the lower left of the face, on the green PCB next to the red/green
   LED.
4. Plug the Logitech K400 wireless keyboard USB receiver into the robot's USB
   hub if it is not already plugged in.
5. Turn on the Kobuki base if Logos should be able to use the robot body.
6. Open the ThinkPad lid far enough to reach the laptop power button.
7. Press the ThinkPad power button.
8. Close the ThinkPad lid again after it starts booting.
9. Wait for the Linux desktop to appear on the main monitor.

If the normal desktop appears but the face monitor is blank, keep going. The
face display is started later from a terminal on that monitor.

## Tiny Linux Survival Guide

### Open a terminal

A terminal is a text window where you run commands.

Press:

```
Ctrl+Alt+T
```

That opens a new terminal window.

### Run a command

Type or paste the command, then press `Enter`.

For the startup commands in this guide:

1. Open a new terminal.
2. Type the command shown.
3. Press `Enter`.
4. Leave that terminal open while Logos is using that part of the system.

Several parts of Logos run at the same time, so startup uses several terminal
windows.

### Repeat or fix the last command

If you just ran a command and need to run it again:

1. Press the `Up Arrow` key in that terminal.
2. The previous command appears again.
3. Press `Enter` to run it again.

You can also use `Up Arrow` to bring back the previous command, edit it with
the keyboard, and then press `Enter`.

### Stop a running command

Click the terminal that is running it, then press:

```
Ctrl+C
```

That means "stop the program in this terminal." It does not mean copy.

If a program stops and you want it again, run its startup command again in that
terminal.

### Copy and paste

In many Linux terminals:

- Copy is `Ctrl+Shift+C`
- Paste is `Ctrl+Shift+V`

Plain `Ctrl+C` is reserved for stopping a command.

## Normal Startup Order

The commands below use helper scripts already in the robot workspace. Start
them in this order.

These helper script names are already available in a normal terminal. You do
not need to change folders before running them.

### 1. Start the Chroma memory server

Open a terminal on the main monitor and run:

```
logos_chroma.sh
```

Leave that terminal open.

### 2. Start Logos's body and core robot services

Open another terminal on the main monitor and run:

```
logos_core.sh
```

Leave that terminal open.

This starts the core ROS robot stack, including the mobile base and other
hardware-facing services. If it complains that the Kobuki/base is not
available, check that the Kobuki power is on.

### 3. Start Logos's cognition and web dashboard

Open another terminal on the main monitor and run:

```
logos_cog.sh Logos
```

Leave that terminal open.

This starts Logos's cognition loop, Python worker, and browser web UI using the
`Logos` agent workspace.

### 4. Start speech input

Open another terminal on the main monitor and run:

```
logos_stt.sh
```

Leave that terminal open.

This starts Logos's microphone speech-to-text process.

### 5. Start the face and caption terminal

Use the separate face monitor for this step. The detailed setup is in
[Face Monitor Setup](#face-monitor-setup).

The face pane runs:

```
logos_face.sh
```

The caption pane runs:

```
logos_tts_caption.sh
```

### 6. Start the idle state indicator

After the face is running, open another terminal on the main monitor and run:

```
logos_idle.sh
```

Leave that terminal open.

## Use The Browser Dashboard

On the main desktop monitor, open a web browser and go to:

```
http://localhost:5000
```

`localhost` means "this same ThinkPad." It only works on the robot computer
unless the network setup is changed.

Use the web UI to:

- Type text input to Logos.
- Read Logos's inputs and outputs.
- See the cognition dashboard state.

If the page does not load, make sure the `logos_cog.sh Logos` terminal is still
running.

## Optional Robot Movement Modes

These are extra ROS modes for making Logos map or navigate. They are not
needed for basic conversation. Only start them after `logos_core.sh` is already
running.

Open a new terminal for each of these and leave it open while using that mode.

### Navigation

Navigation is for moving around with an existing map.

Run:

```
roslaunch logos_bringup logos_navigation.launch
```

If a specific map file is needed, the command may need a map path added later.
For example:

```
roslaunch logos_bringup logos_navigation.launch map_file:=/path/to/map.yaml
```

### SLAM / Mapping

SLAM is for building a map of a room or area.

Run:

```
roslaunch logos_bringup logos_slam.launch
```

Use either navigation or SLAM for normal operation, not both at the same time.
Stop the one you are finished with by clicking its terminal and pressing
`Ctrl+C`.

## Talk To Logos Out Loud

Speech input works like this:

1. Say "Hey Robot".
2. Wait for Logos to begin listening.
3. Say the message you want Logos to hear.
4. Say "End of Line" when the message is finished.

Logos will transcribe the message and send it into cognition.

There is a second stop phrase:

```
Edit Input
```

That asks for a chance to edit the transcript before it is sent. Use it only
when you are ready to interact with the speech terminal prompt. For everyday
conversation, "End of Line" is simpler.

## Face Monitor Setup

The face monitor is a secondary HDMI screen. The face is intentionally drawn as
ASCII terminal art, so set up a terminal there and keep it visible.

### Make the face terminal

1. Move the mouse pointer to the face monitor.
2. Open a terminal there with `Ctrl+Alt+T`.
3. Make that terminal full screen. On many Linux desktops, `F11` toggles full
   screen for a terminal window.
4. Right-click inside the terminal.
5. Select the terminal profile named `robot_face_03`.

The `robot_face_03` profile sets the colors and font sizing expected for the
face.

### Split the terminal into two panes with tmux

In the full-screen face terminal, run:

```
tmux
```

Then run:

```
tmux split-window -v -p 33
```

That creates:

- A larger top pane for Logos's face.
- A smaller bottom pane for text-to-speech captions and text output.

The split is approximately two-thirds top and one-third bottom.

### Start captions in the bottom pane

After the split command, tmux usually puts the cursor in the new bottom pane.
Run:

```
logos_tts_caption.sh
```

Leave it running.

### Start the face in the top pane

Move to the top tmux pane:

1. Hold `Ctrl` and press `B`.
2. Release both keys.
3. Press the `Up Arrow` key.

Then run:

```
logos_face.sh
```

The face should fill the top terminal pane.

### Useful tmux controls

tmux uses a two-part keyboard shortcut. First press `Ctrl+B`, release it, then
press the second key.

| What you want | Keys |
| --- | --- |
| Move to the top pane | `Ctrl+B`, then `Up Arrow` |
| Move to the bottom pane | `Ctrl+B`, then `Down Arrow` |
| Close tmux after its programs are stopped | type `exit` in each pane |

Do not worry about tmux if the face is already running. It is only the tool
that lets the face and captions share one full-screen terminal.

## Start Codex For Help

Codex can answer questions from the robot workspace, inspect files, and help
diagnose errors.

Open a new terminal on the main monitor and run:

```
cd ~/robot_ws
codex
```

Then ask questions in plain language, for example:

```
Logos did not hear "Hey Robot". What should I check?
```

or:

```
Explain the startup guide and help me restart the face.
```

If Codex asks you to sign in, follow the sign-in prompts on screen.

## Stop Logos

To stop a part of Logos:

1. Click its terminal.
2. Press `Ctrl+C`.

For a normal shutdown:

1. Stop the face and captions if they are running.
2. Stop the idle state indicator.
3. Stop speech input.
4. Stop cognition.
5. Stop the core robot stack.
6. Stop the Chroma memory server.
7. Shut down Linux from the desktop power menu.
8. Turn off Kobuki power when the robot body should be off.

## Quick Restart Cheatsheet

Open one terminal per command and leave it open:

```
logos_chroma.sh
logos_core.sh
logos_cog.sh Logos
logos_stt.sh
```

On the face monitor, in the face tmux layout:

```
logos_face.sh
logos_tts_caption.sh
```

After the face is running, start the idle state indicator in its own terminal:

```
logos_idle.sh
```

Open the dashboard on the main monitor:

```
http://localhost:5000
```

## When Something Looks Wrong

### The desktop is missing

- Check power to the main monitor.
- Check the wireless HDMI receiver.
- Remember that the ThinkPad's own laptop panel is broken.

### Typing does nothing

- Make sure the Logitech K400 wireless USB receiver is plugged into the USB
  hub.
- Make sure the keyboard has power/battery.
- Click the window where you want the typing to go.

### The face is blank

- Make sure `logos_core.sh` is running first.
- Check the face monitor power button. It is on the lower left of the face, on
  the green PCB next to the red/green LED.
- Make sure the face monitor terminal is open on the secondary monitor.
- Make sure the top face pane is running `logos_face.sh`.
- If the terminal text looks the wrong size or color, right-click and select
  the `robot_face_03` terminal profile.

### The browser dashboard does not open

- Check that the terminal running `logos_cog.sh Logos` is still open.
- Use the exact address `http://localhost:5000` on the ThinkPad's browser.

### Logos does not hear speech

- Check that the terminal running `logos_stt.sh` is still open.
- Say "Hey Robot" before the message.
- Say "End of Line" after the message.

### A terminal shows scary text

Do not close everything at once. Read the last few lines first. If help is
needed, start Codex in `~/robot_ws` and ask about the error text shown in that
terminal.

### Username and Password

The main username is `robot`. Password is `robot`.
