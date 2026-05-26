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

## Rendering Tweak Params

Useful private ROS params for visual tuning:

- `_eye_lid_thickness_ratio:=0.05`: lid/brow stroke thickness as a fraction of
  face-pane render height. Try `0.035` for a lighter stroke.
- `_eye_lid_min_thickness_px:=1`: minimum lid/brow stroke thickness.
- `_eye_outline_thickness_px:=2`: eye ellipse outline thickness.
- `_eye_center_y_ratio:=0.375`: eye center height within the face pane.
- `_eye_gaze_x_ratio:=0.25` and `_eye_gaze_y_ratio:=0.125`: gaze travel range.
- `_eye_radius_x_ratio:=0.20` and `_eye_radius_y_ratio:=0.20`: base eye size.
- `_eye_lid_height_ratio:=0.25`: lid height travel range.
- `_eye_lid_erase_padding_x_ratio:=0.025`: horizontal padding for the restored
  area above the lid line.
- `_waveform_baseline_y_ratio:=0.875`: mouth waveform baseline height.
- `_waveform_amplitude_y_ratio:=0.125`: mouth/audio waveform travel range.
- `_audio_wave_thickness_ratio:=0.0142857`: audio waveform stroke thickness.
- `_mouth_sine_thickness:=4`: idle sine mouth stroke thickness in pixels.
- `_render_px_per_char_x:=1.0` and `_render_px_per_char_y:=1.0`: OpenCV render
  resolution per terminal cell before libcaca dithering.
- `_layer_image_fade_in_sec:=0.6`, `_layer_image_hold_sec:=4.0`,
  `_layer_image_fade_out_sec:=0.8`, and `_layer_image_max_alpha:=1.0`: image
  layer fade envelope.

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
