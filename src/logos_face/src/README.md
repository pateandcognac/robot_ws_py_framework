# Logos Face Node

`logos_face` is a ROS Noetic C++ node that renders the animated terminal face for Logos.

It draws:

- Two expressive eyes
- Eye gaze, scale, lid height, lid angle, and color animation
- A sine-wave mouth/effect layer
- A live audio waveform mouth layer
- Terminal or libcaca display output
- A live JSON state topic for other tools to inspect the face state

The node is designed for a terminal-based robot face, using OpenCV for image rendering and `libcaca` for terminal/display dithering.

---

## Node Name

```bash
logos_face
````

---

## Main Source File

This README describes the C++ face node implementation in `FaceNodeCpp`.

The node subscribes to face-control topics, renders the current state into an OpenCV image, converts it to a terminal-friendly representation using `libcaca`, and continuously refreshes the display at the configured FPS.

For the newer split-pane HUD variant, see [`face_hud_README.md`](face_hud_README.md).

---

## Dependencies

This node uses:

* ROS Noetic
* OpenCV
* dynamic_reconfigure
* libcaca
* `logos_msgs`
* `logos_face` dynamic reconfigure config

System/package-level dependencies include:

```bash
sudo apt install libcaca-dev
```

ROS dependencies should be handled through the workspace package manifests, but the important ROS-side dependencies are:

```xml
<depend>roscpp</depend>
<depend>std_msgs</depend>
<depend>dynamic_reconfigure</depend>
<depend>logos_msgs</depend>
<depend>opencv</depend>
```

---

## Subscribed Topics

### Eye Control

#### `/face/eye_gaze_x`

Message type:

```text
logos_msgs/EyeGazeX
```

Controls horizontal eye gaze.

#### `/face/eye_gaze_y`

Message type:

```text
logos_msgs/EyeGazeY
```

Controls vertical eye gaze.

#### `/face/eye_scale_x`

Message type:

```text
logos_msgs/EyeScaleX
```

Controls horizontal eye size.

#### `/face/eye_scale_y`

Message type:

```text
logos_msgs/EyeScaleY
```

Controls vertical eye size.

#### `/face/eye_lid_height`

Message type:

```text
logos_msgs/EyeLidHeight
```

Controls eyelid height.

#### `/face/eye_lid_angle`

Message type:

```text
logos_msgs/EyeLidAngle
```

Controls eyelid angle.

The left and right eyes use mirrored lid-angle behavior so symmetrical expressions can be commanded more naturally.

#### `/face/eye_color`

Message type:

```text
logos_msgs/EyeColor
```

Controls eye color.

Expected color format:

```text
#RRGGBB
```

Invalid or missing colors fall back to green:

```text
#00FF00
```

---

### Mouth / Waveform Control

#### `/face/mouth/sine_wave`

Message type:

```text
logos_msgs/MouthSine
```

Controls the procedural sine-wave mouth/effect layer.

Fields used by the node include:

* `frequency`
* `amplitude`
* `phase`
* `phase_increment`
* `color`
* `duration`

This layer is useful as an idle mouth animation, decorative waveform, or expression effect.

#### `/face/mouth/audio_wave`

Message type:

```text
logos_msgs/AudioWave
```

Controls the live audio waveform mouth layer.

The audio data is expected as signed 16-bit-style sample values. The node normalizes incoming samples into floating point values from approximately `-1.0` to `1.0`.

Fields used by the node include:

* `data`
* `sample_rate`

The audio waveform is rendered while the received buffer is still “current.” Once playback time exceeds the known duration of the received audio buffer, the audio wave is cleared.

---

## Published Topics

### `/face/live_state/json`

Message type:

```text
std_msgs/String
```

Publishes a JSON snapshot of the current face state.

Example structure:

```json
{
  "timestamp": 1234567890.1234,
  "left_eye": {
    "gaze_x": 0.0,
    "gaze_y": 0.0,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "lid_height": 1.0,
    "lid_angle": 0.0,
    "color": "#00FF00"
  },
  "right_eye": {
    "gaze_x": 0.0,
    "gaze_y": 0.0,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "lid_height": 0.5,
    "lid_angle": 0.0,
    "color": "#00FF00"
  },
  "mouth": {
    "frequency": 1.0,
    "amplitude": 1.0,
    "phase": 0.0,
    "phase_increment": 0.1,
    "color": "#00FF00"
  },
  "duration": 0.125
}
```

This is mainly useful for letting another process use Logos’s face state for derived effect. For example, eye color -> LED color, or, eye gaze -> base twist. 

---

## Parameters

The node uses private ROS parameters.

### `fps`

Type:

```text
int
```

Default:

```text
8
```

Controls render refresh rate.

The node clamps keyboard-adjusted FPS between:

```text
1 and 24
```

---

### `output_mode`

Type:

```text
string
```

Default:

```text
display
```

Supported behavior:

* `display`: use a libcaca display
* any other value: fall back to ANSI terminal output

---

### `caca_driver`

Type:

```text
string
```

Default:

```text
ncurses
```

The libcaca display driver to use.

Common options may include:

```text
ncurses
x11
slang
raw
```

Available drivers depend on the local libcaca installation.

---

### `dither_antialias`

Type:

```text
string
```

Default:

```text
default
```

Passed to libcaca dithering.

---

### `dither_color`

Type:

```text
string
```

Default:

```text
full16
```

Passed to libcaca dithering.

This affects how much color survives the terminal/display conversion. If the face looks too flat or color-banded, this is one of the first settings to experiment with.

---

### `dither_charset`

Type:

```text
string
```

Default:

```text
ascii
```

Passed to libcaca dithering.

---

### `dither_algorithm`

Type:

```text
string
```

Default:

```text
ordered4
```

Passed to libcaca dithering.

---

### `render_px_per_char_x`

Type:

```text
double
```

Default:

```text
1.0
```

Controls horizontal render resolution relative to terminal/canvas width.

---

### `render_px_per_char_y`

Type:

```text
double
```

Default:

```text
1.0
```

Controls vertical render resolution relative to terminal/canvas height.

---

## Dynamic Reconfigure

The node supports dynamic reconfigure through:

```text
logos_face/FaceNodeConfig
```

Runtime-adjustable settings include:

* FPS
* output mode
* libcaca driver
* dither antialias
* dither color
* dither charset
* dither algorithm
* render pixel scaling

Changing output mode or libcaca driver may require restarting the node if keyboard behavior becomes weird.

---

## Keyboard Controls

When running in ANSI terminal mode, the node supports keyboard controls.

Some keyboard controls are also handled when using a libcaca display.

| Key | Action                  |
| --- | ----------------------- |
| `q` | Quit                    |
| `r` | Re-detect terminal size |
| `a` | Decrease columns        |
| `d` | Increase columns        |
| `s` | Decrease rows           |
| `w` | Increase rows           |
| `[` | Decrease FPS            |
| `]` | Increase FPS            |
| `\` | Clear screen            |

Terminal size controls only apply in ANSI mode.

---

## Rendering Overview

Each render frame does roughly this:

1. Check for quit events and keyboard input.
2. Update render geometry.
3. Update animated eye and mouth parameters.
4. Clear the OpenCV image.
5. Render both eyes.
6. Render the mouth waveform.
7. Convert the OpenCV image to RGBA.
8. Make pure black transparent.
9. Dither the image into a libcaca canvas.
10. Refresh the display or print ANSI output.
11. Publish live face state JSON.

---

## Eye Rendering

Each eye is rendered as an ellipse.

Eye position is affected by:

* `gaze_x`
* `gaze_y`

Eye shape is affected by:

* `scale_x`
* `scale_y`

The eyelid is rendered as a thick angled line, then the area above the lid is erased to black. This creates the appearance of expressive blinking, squinting, and attitude.

The eye outline uses the current mouth/effect color, which helps visually tie the eye expression and mouth animation together.

---

## Mouth Rendering

The mouth has two layers:

1. Audio waveform layer
2. Sine waveform layer

The audio waveform is drawn when fresh audio data is available.

The sine waveform is always drawn and acts as the base animated mouth/effect line.

Both are rendered near the lower part of the face, using this baseline:

```cpp
const int baseline = static_cast<int>(img.rows * 0.875);
```

The wave height uses approximately:

```cpp
img.rows * 0.125
```

So the mouth occupies the lower eighth of the rendered face area.

---

## Audio Waveform Color Behavior

The audio waveform can be colored by vertical distance from the mouth baseline.

The intended behavior is:

* Near the baseline: violet / cooler color
* Medium movement: blue, cyan, green, yellow, orange
* Peak distance from baseline: red

This means the color represents where the waveform is vertically on the face, not the raw audio sample value.

Both positive and negative peaks can be treated as “peaks” by using absolute distance from the baseline.

This produces a compressed full-spectrum look on quieter audio and a more spread-out rainbow effect on larger waveform movement.

---

## Color Utilities

Colors are generally handled as hex strings:

```text
#RRGGBB
```

Internally, OpenCV uses BGR channel order, so helper functions convert between hex RGB strings and OpenCV BGR values.

Invalid color strings fall back to:

```text
#00FF00
```

---

## Animation System

Most face parameters animate over time.

The node stores:

* current value
* start value
* target value
* duration
* start time
* active/inactive state

Animated values are linearly interpolated each frame.

This is used for:

* eye gaze
* eye scale
* eyelid height
* eyelid angle
* eye color
* sine wave frequency
* sine wave amplitude
* sine wave phase
* sine wave phase increment
* sine wave color

---

## Audio Handling

When an audio message arrives:

1. The node stores the audio sample buffer.
2. It converts sample values into normalized floats.
3. It records the sample rate.
4. It records the start time.
5. It computes the audio duration.

During rendering, the node calculates elapsed time and displays the matching segment of the audio buffer.

When the elapsed time exceeds the audio duration, the stored audio buffer is cleared.

---

## Running the Node

Example:

```bash
rosrun logos_face logos_face
```

Or from a launch file:

```xml
<node pkg="logos_face" type="logos_face" name="logos_face" output="screen">
    <param name="fps" value="8" />
    <param name="output_mode" value="display" />
    <param name="caca_driver" value="ncurses" />
    <param name="dither_color" value="full16" />
    <param name="dither_charset" value="ascii" />
    <param name="dither_algorithm" value="ordered4" />
</node>
```

---

## Testing With Example Messages

Set both eyes green:

```bash
rostopic pub /face/eye_color logos_msgs/EyeColor "eye_side: 'both'
color: '#00FF00'
duration: 0.5"
```

Look left:

```bash
rostopic pub /face/eye_gaze_x logos_msgs/EyeGazeX "eye_side: 'both'
gaze_x: -1.0
duration: 0.5"
```

Look right:

```bash
rostopic pub /face/eye_gaze_x logos_msgs/EyeGazeX "eye_side: 'both'
gaze_x: 1.0
duration: 0.5"
```

Change the sine mouth color:

```bash
rostopic pub /face/mouth/sine_wave logos_msgs/MouthSine "frequency: 2.0
amplitude: 1.0
phase: 0.0
phase_increment: 0.2
color: '#00FFFF'
duration: 0.5"
```

---

## Troubleshooting

### The face is too small, stretched, or squashed

Adjust:

```text
render_px_per_char_x
render_px_per_char_y
```

Terminal character cells are not square, so the face may need different X/Y scaling depending on the terminal and font.

---

### Colors look wrong or washed out

Experiment with:

```text
dither_color
dither_algorithm
dither_charset
```

Also check whether the terminal supports enough colors.

---

### The display does not start

Try a different libcaca driver:

```bash
rosrun logos_face logos_face _caca_driver:=ncurses
```

Or try ANSI mode by setting `output_mode` to something other than `display`.

---

### Keyboard input behaves strangely after changing output mode

Restart the node.

The code warns about this because switching between libcaca display mode and ANSI terminal mode at runtime can leave terminal input behavior in a weird state.

---

### The mouth waveform is mostly one color

Check the audio waveform color mapping.

For a full vertical-spectrum mouth effect, color should be based on rendered vertical distance from the mouth baseline, not directly on raw sample value.

---

## Notes For Future Development

Possible improvements:

* Add separate parameters for audio-mouth color mode.
* Add a configurable mouth baseline.
* Add a configurable mouth height.
* Add smoother gradient coloring by splitting long waveform segments into smaller colored pieces.
* Add ROS parameters for rainbow hue endpoints.
* Add a debug overlay showing FPS, terminal size, and render size.
* Add blink presets and emotional expression presets.
* Add a proper “speaking” mode that fades the sine wave behind the audio waveform.
* Add launch-file presets for different terminal types.

---

## Practical Mental Model

The node is basically:

```text
ROS messages in
    ↓
animated face state
    ↓
OpenCV image
    ↓
RGBA conversion with black transparency
    ↓
libcaca terminal/display rendering
    ↓
live JSON state out
```

It is not trying to be a photoreal face. It is a terminal puppet face, which is honestly much funnier and more Logos-appropriate anyway.
