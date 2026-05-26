# Logos Face HUD

`face_hud_node` is the split-pane terminal face for Logos. The upper pane is the
animated face plus a playful `face` canvas drawn underneath it. The lower pane is
the functional `status` area for TTS captions and human-facing feedback.

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

- Upper pane: eyes, mouth waveform, debug image overlay, and the `face` canvas.
- Lower pane: `status` stream with TTS captions, figlet feedback, and plain text.
- Default split: about 2/3 face and 1/3 status.
- Caption figlet lines are printed gradually over the TTS chunk duration, so
  they scroll upward like terminal output.
- Other status events are queued behind any currently scrolling caption block so
  they do not split figlet glyphs.

The split can be tuned at startup with the private ROS param:

```bash
rosrun logos_face face_hud_node _status_region_ratio:=0.33
```

## Keyboard Controls

These controls work while the HUD has keyboard focus:

- `q`: quit
- `[` / `]`: decrease / increase FPS
- `-`: give more height to the status pane
- `+` or `=`: give more height to the face pane
- `\`: clear the current display
- `r`: re-detect terminal size in ANSI mode
- `a` / `d`: decrease / increase ANSI canvas columns
- `s` / `w`: decrease / increase ANSI canvas rows

## HUD Events

The C++ HUD subscribes to `/face/hud/event` as `std_msgs/String` JSON.

Examples:

```json
{"pane":"face","kind":"text","text":"ambient face canvas text","color":"bright_white"}
```

```json
{"pane":"face","kind":"figlet","text":"spark","font":"small","color":"bright_blue"}
```

```json
{"pane":"status","kind":"caption","text":"spoken words","font":"thick","color":"bright_magenta","duration":1.2}
```

```json
{"pane":"status","kind":"figlet","text":"thinking","font":"small","color":"bright_blue"}
```

```json
{"pane":"status","kind":"clear"}
```

Supported panes are `face`, `status`, and `all` for clear events. Captions are
status events; the old public `caption` pane has been removed.


ALSO: subscribes to topic /logos_vision/debug/face Image msg type and displays the image *over* the face for a set duration


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
content into the `face` canvas. Existing eye, mouth, audio waveform, debug
image, and `/face/live_state/json` topics are preserved from the older face
renderer.
