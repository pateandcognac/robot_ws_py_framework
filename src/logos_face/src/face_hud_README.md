# Logos Face HUD

`face_hud_node` is the split-pane terminal face for Logos. The upper pane is a
three-layer face renderer: layer 0 is a playful canvas behind the animated face,
layer 1 is the eyes/mouth face animation, and layer 2 is a front overlay canvas.
The lower pane is the functional `status` area for TTS captions and
human-facing feedback.

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

- Upper pane: layer 0 face effects, layer 1 eyes/mouth waveform, and layer 2
  front overlays.
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
{"pane":"face","layer":0,"kind":"text","text":"ambient face canvas text","color":"bright_white"}
```

```json
{"pane":"face","layer":0,"kind":"figlet","text":"spark","font":"small","effect":"crawl","color":"bright_blue","speed":8.0}
```

```json
{"pane":"face","layer":2,"kind":"text","text":"overlay","effect":"terminal","color":"bright_yellow","duration":2.0}
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
status events; the old public `caption` pane has been removed. Face events
accept `layer:0` or `layer:2`, defaulting to layer 0. Supported face text
effects are `terminal`, `crawl`, and `rain`.

## Face Images

The HUD subscribes to two image topics:

- `/face/layer0/image`: fades an image behind the animated face.
- `/face/layer2/image`: fades an image in front of the animated face.

Both use `sensor_msgs/Image` and the same fade/hold behavior. The HUD no longer
subscribes to `/logos/debug_vision/face`; that topic is only a debug/web mirror
published by the Logos API.

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

The transitional `/face/text_backdrop` topic is not supported by this HUD.
Existing eye, mouth, audio waveform, and `/face/live_state/json` topics are
preserved from the older face renderer.
