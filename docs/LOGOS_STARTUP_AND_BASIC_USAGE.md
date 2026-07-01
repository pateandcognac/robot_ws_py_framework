# Logos Startup and Basic Usage

This guide is for a third-party who has not used Linux or ROS before. It is meant
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

For the full tmux-based bringup dashboard, run:

```
logos_launch.sh
```

This opens a main-monitor `gnome-terminal`, creates a `tmux` session named
`logos`, starts the usual stack in separate panes, and leaves the final pane
waiting for the cognition workspace name. The main terminal opens maximized by
default, and the cognition pane uses 50% of the tmux window width. Press Enter
there to use the displayed default, or type an existing/new `Logos_*` workspace
name. For boot/autostart use, the same helper can be called with explicit
display and automatic cognition startup:

```
/home/robot/robot_ws/bin/logos_launch.sh --display :0 --auto-cog --workspace Logos_001
```

To resume the most recently touched cloned workspace at boot, use
`--last-workspace`. It selects the newest existing
`~/robot_workspaces/Logos_*` directory as the default. Without `--auto-cog`, the
cognition pane shows a 60 second countdown so an operator can type a different
workspace before it launches the default:

```
/home/robot/robot_ws/bin/logos_launch.sh --display :0 --last-workspace --boot-voice
```

Set `LOGOS_MAIN_TERMINAL_MAXIMIZE=0` to use the geometry hint instead of a
maximized window. Set `LOGOS_COG_PANE_PERCENT=60` or another value from 1 to 99
to change the cognition pane width. The older `LOGOS_COG_PANE_WIDTH` fixed
column setting still works and overrides the percentage setting when present.

The launcher loads the exported environment from `~/.bashrc` before creating
the tmux session. This gives Startup Applications the same Gemini keys,
TurtleBot sensor settings, ROS overlays, paths, and library settings available
in a normal terminal. Set `LOGOS_LOAD_BASHRC=0` only when intentionally testing
without that environment.

At startup, the launcher also shows a desktop notification with the robot's
Ubuntu login keyring reminder. The default reminder tells the operator to enter
password `robot` when Ubuntu asks for login keyring authentication. Use
`--no-login-notification` or set `LOGOS_LOGIN_NOTIFICATION=0` to suppress it.

For an optional narrated startup sequence, add `--boot-voice`:

```
/home/robot/robot_ws/bin/logos_launch.sh --display :0 --auto-cog \
  --workspace Logos_001 --boot-voice
```

Ubuntu Startup Applications does not evaluate shell expansions such as
`$EPOCHSECONDS`, process substitution, or multiline shell commands. To create
a fresh time-derived clone at every boot, use the launcher's built-in
`--time-workspace` option instead:

```
/home/robot/robot_ws/bin/logos_launch.sh --display :0 --auto-cog --time-workspace --boot-voice
```

This computes the CRC32 of the current Unix epoch seconds and launches a
workspace named `Logos_<eight-hex-digit-crc>`, for example
`Logos_2e8f5c38`. It overrides any value passed through `--workspace`.

The narration sets the system output volume to 100%, begins with the `espeak`
binary for Linux and ROS startup, uses Festival around core hardware bringup,
then switches to Logos text-to-performance with Piper and finally Kokoro. Before
core bringup it briefly checks `/dev/kobuki` for incoming serial data. If the
base is missing or silent, it speaks and displays a reminder to use the power
switch next to the charging cord.

After STT becomes ready, narrated startup runs:

```
bin/logos_ambient.sh 1 1 '[]'
```

It then explains the `hey robot` wake phrase, the explicit `end of line`
terminator, and the spring-shaped capacitive microphone mute switch on the
right side of Logos's head. Later Kokoro reminders call attention to login
keyring authentication, browser interface launch, and the LOOK HERE workspace
prompt in the main terminal. If `docs/SPEAKME.txt` exists and is nonempty, its
emoji-punctuated contents are performed last using the final Kokoro voice.
Set `LOGOS_BOOT_VOICE=1` instead of using the command-line flag when preferred.

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

### 3. Start speech input

Open another terminal on the main monitor and run:

```
logos_stt.sh
# Optional streaming Nemotron backend:
logos_stt.sh nemotron
```

Leave that terminal open.

This starts Logos's microphone speech-to-text process.

### 3. Start Logos's cognition and web dashboard

Open another terminal on the main monitor and run:

```
logos_cog.sh Logos_workspace
```
Where `Logos_workspace` is the name of an existing workspace, or a new workspace to `git clone` from the core Logos workspace.

Leave that terminal open.

This starts Logos's cognition loop, Python worker, and browser web UI using the
`Logos` agent workspace.


### 4. Start the face

`logos_core.sh` automatically opens the face HUD in a fullscreen
`gnome-terminal` on the secondary face monitor, using the `robot_face_03`
terminal profile. The helper skips launching a duplicate terminal if
`face_hud_node` is already running.

The `robot_face_03` profile sets the colors and font sizing expected for the
face.

To launch only the face terminal helper manually, run:

```
logos_face_term.sh
```

To run core bringup without opening the face terminal, run:

```
LOGOS_FACE_TERM=0 logos_core.sh
```

If the automatic helper cannot place the terminal correctly, use the separate
face monitor manually. (Face monitor power button is adjacent to the red/green
LED on the green circuit board on Logos's face.) Open a terminal, right-click
inside the terminal, select the `robot_face_03` profile, then maximize the
window or enter fullscreen with F11. In that manual terminal, run:

```
logos_face.sh
```

### 5. Start the idle state indicator

`logos_core.sh` starts the idle state indicator through
`logos_bringup/logos_core.launch`. For isolated debugging, it can still be run
manually with `logos_idle.sh`.

### 6. Mapping / Localization
These are ROS modes for making Logos map or navigate. Run one or the other, not both.
Open a new terminal for each of these and leave it open while using that mode.

### Navigation

Navigation is for moving around with an existing map. This is usually what you want to run, once a stable map of an environment has been built.

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

For RGB-D mapping with RTAB-Map instead of GMapping, see
`docs/LOGOS_RTABMAP.md`. The quick-start command is:

```
roslaunch logos_bringup logos_rtabmap.launch
```

(RTAB-Map database can also be used to created assets for Logos's `map3d` aka Chora.)

Use either navigation or SLAM for normal operation, not both at the same time.
Stop the one you are finished with by clicking its terminal and pressing
`Ctrl+C`.

### 7. Start Logos's body and core robot services

Open another terminal on the main monitor and run:

```
logos_core.sh
```

Leave that terminal open.

This starts the core ROS robot stack, including the mobile base and other
hardware-facing services. If it complains that the Kobuki/base is not
available, check that the Kobuki power is on.


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

If the page does not load, make sure the `logos_cog.sh Logos_workspace` terminal is still
running.


## Talk To Logos Out Loud

Speech input works like this:

1. Say "Hey Robot".
2. Wait for Logos to begin listening.
3. Say the message you want Logos to hear.
4. Say "End of Line" when the message is finished. Or "Cancel That" to discard the message.

Logos will transcribe the message and send it into cognition.
(Note: The microphone has a hardware mute. The metal spring on the robot's head is capacitive touch switch. Tap it to change LED red to green to unmute.)

## Start Codex or Claude Code For Help

Codex or Claude can answer questions from the robot harness or API workspace, inspect files, and help
diagnose errors.

Open a new terminal on the main monitor and run:

```
cd ~/robot_ws         # or other directory, like ~/robot_workspaces/Logos
codex                 # or other AI agent, like claude
```

Then ask questions in plain language, for example:
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
logos_stt.sh
logos_cog.sh Logos
```

On the face monitor, in the face tmux layout:

```
logos_face.sh
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
- Make sure the face monitor terminal is open on the secondary monitor. You can
  reopen it with `logos_face_term.sh`.
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
