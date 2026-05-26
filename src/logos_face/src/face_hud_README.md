# Logos Face HUD

`face_hud_node` is the split-pane terminal face for Logos. It keeps the animated
face in the upper pane, prints status text behind the face as a stable-color
terminal backdrop, and reserves the lower pane for TTS figlet captions.

## Launch

```bash
bin/logos_face.sh
```

The helper script starts:

- `rosrun logos_ui face_hud_bridge_node.py`
- `rosrun logos_face face_hud_node`

The Python bridge parses cognition/TTS events and handles sound effects. The C++
HUD owns all terminal rendering, pane layout, color persistence, and figlet
generation.

## Layout

- Upper pane: eyes, mouth waveform, debug image overlay, and status backdrop.
- Lower pane: TTS captions only.
- Default split: about 2/3 face and 1/3 captions.
- Caption figlet lines are printed gradually over the TTS chunk duration, so
  they scroll upward like terminal output.

The split can be tuned at startup with the private ROS param:

```bash
rosrun logos_face face_hud_node _caption_region_ratio:=0.33
```

## Keyboard Controls

These controls work while the HUD has keyboard focus:

- `q`: quit
- `[` / `]`: decrease / increase FPS
- `-`: give more height to the TTS caption pane
- `+` or `=`: give more height to the face/status pane
- `\`: clear the current display
- `r`: re-detect terminal size in ANSI mode
- `a` / `d`: decrease / increase ANSI canvas columns
- `s` / `w`: decrease / increase ANSI canvas rows

## HUD Events

The C++ HUD subscribes to `/face/hud/event` as `std_msgs/String` JSON.

Examples:

```json
{"pane":"status","kind":"text","text":"<human>\nhello\n</human>","color":"bright_white"}
```

```json
{"pane":"status","kind":"figlet","text":"thinking","font":"small","color":"bright_blue"}
```

```json
{"pane":"caption","kind":"caption","text":"spoken words","font":"thick","color":"bright_magenta","duration":1.2}
```

```json
{"pane":"caption","kind":"clear"}
```

Supported panes are `status`, `caption`, and `all` for clear events.

## Font Behavior

Figlet output is generated in C++ with:

```bash
python3 -m pyfiglet -w <current_canvas_width> -f <font>
```

Plain-text font names skip pyfiglet and print directly:

- `term`
- `terminal`
- `plain`
- `text`
- `ascii`
- `none`

If pyfiglet fails, stderr is suppressed and the HUD falls back to plain text so
tracebacks do not print into Logos's face.

## Compatibility

The HUD still listens to the legacy `/face/text_backdrop` topic and maps that
content into the status backdrop. Existing eye, mouth, audio waveform, debug
image, and `/face/live_state/json` topics are preserved from the older face
renderer.
