# logos_face (ROS Noetic) — Logos ASCII Face Node

`logos_face` renders Logos’s face by drawing into an OpenCV bitmap (`cv::Mat`), then converting that bitmap into colored ASCII using **libcaca dithering**.  
The rendering style intentionally comes from **bitmap → ASCII conversion** (we do *not* use libcaca drawing primitives).

This node supports two output paths:

- **Display mode (recommended):** libcaca **display** drivers (default: `ncurses`, but can use `x11` or `gl` for experiments)
- **ANSI fallback:** export libcaca canvas to ANSI and brute-force print to `stdout`

FPS is controlled by the node’s ROS timer and is intended to remain compatible with external “idle / activity FPS controller” logic.

---

## Features

- OpenCV bitmap renderer for:
  - eyes (gaze, scale, lid height, lid angle, color)
  - mouth waveform (sine + optional audio waveform blend)
- libcaca bitmap dithering for ASCII output
- Output backends:
  - libcaca display drivers (`ncurses` default; `x11`, `gl` experimental)
  - ANSI stdout fallback mode
- Dynamic reconfigure for rendering tweaks (FPS + libcaca dither settings + output mode/driver)

---

## Topics

### Subscribed
- `/face/eye_gaze_x` (`logos_msgs/EyeGazeX`)
- `/face/eye_gaze_y` (`logos_msgs/EyeGazeY`)
- `/face/eye_scale_x` (`logos_msgs/EyeScaleX`)
- `/face/eye_scale_y` (`logos_msgs/EyeScaleY`)
- `/face/eye_lid_height` (`logos_msgs/EyeLidHeight`)
- `/face/eye_lid_angle` (`logos_msgs/EyeLidAngle`)
- `/face/eye_color` (`logos_msgs/EyeColor`)
- `/face/mouth/sine_wave` (`logos_msgs/MouthSine`)
- `/face/mouth/audio_wave` (`logos_msgs/AudioWave`)

### Published
- `/notification/rgbled` (`std_msgs/Int32MultiArray`)
- `/face/live_state/eye_gaze_x` (`logos_msgs/EyeGazeX`)
- `/face/live_state/eye_gaze_y` (`logos_msgs/EyeGazeY`)
- `/face/live_state/eye_scale_x` (`logos_msgs/EyeScaleX`)
- `/face/live_state/eye_scale_y` (`logos_msgs/EyeScaleY`)
- `/face/live_state/eye_lid_height` (`logos_msgs/EyeLidHeight`)
- `/face/live_state/eye_lid_angle` (`logos_msgs/EyeLidAngle`)
- `/face/live_state/eye_color` (`logos_msgs/EyeColor`)
- `/face/live_state/mouth_sine_wave` (`logos_msgs/MouthSine`)

> Note: the live-state publishers are intended to expose the *current interpolated state* (useful for debugging / introspection).

---

## Parameters (ROS params)

These can be set in a launch file or via `rosparam`.

- `~fps` (int, default `8`)  
  Render FPS (ROS timer period). External FPS controllers can update this via dynamic reconfigure.

- `~output_mode` (string, default `display`)  
  - `display`: use libcaca display drivers
  - `ansi`: export canvas to ANSI and print to stdout

- `~caca_driver` (string, default `ncurses`)  
  libcaca driver name when `output_mode=display`. Common options:
  - `ncurses` (terminal UI)
  - `x11` (X11 window)
  - `gl` (OpenGL)
  - `slang`, etc.

- Dither tuning (strings; defaults match current code):
  - `~dither_antialias` (default `default`)
  - `~dither_color` (default `full16`)
  - `~dither_charset` (default `ascii`)
  - `~dither_algorithm` (default `ordered4`)

---

## Dynamic Reconfigure

Dynamic reconfigure exposes:
- `fps`
- `output_mode`, `caca_driver`
- `dither_antialias`, `dither_color`, `dither_charset`, `dither_algorithm`

### Important note on switching output modes at runtime
Switching `output_mode` / `caca_driver` on the fly will reinitialize libcaca objects.  
If keyboard input behaves oddly after switching, restart the node.

(We can make hot-switching fully clean later, but the current implementation keeps things simple and stable.)

---

## Keyboard Controls

### In **display mode** (`output_mode=display`)
Keyboard events come from libcaca’s event system.

- `q` : quit
- `[` / `]` : decrease / increase FPS
- `\` : clear screen

> Resize handling is managed by libcaca; manual cols/rows controls are not used.

### In **ANSI mode** (`output_mode=ansi`)
Keyboard handling uses a raw terminal input thread (termios).

- `q` : quit
- `r` : re-detect terminal size (and resize canvas)
- `a` / `d` : decrease / increase columns
- `w` / `s` : decrease / increase rows
- `[` / `]` : decrease / increase FPS
- `\` : clear screen

---

## Environment Variables (Optional)

libcaca also supports environment variables that can influence output (handy for quick experiments):

- `CACA_DRIVER` — choose display driver (e.g. `ncurses`, `x11`, `gl`)
- `CACA_GEOMETRY` — force display geometry (e.g. `98x75`)
- `CACA_FONT` — font selection (mostly relevant to some windowed drivers)

You can use these instead of (or alongside) ROS params depending on your setup.

---

## Example Launch Patterns

### Terminal display (recommended default)
```xml
<node pkg="logos_face" type="logos_face" name="logos_face" output="screen">
  <param name="output_mode" value="display"/>
  <param name="caca_driver" value="ncurses"/>
  <param name="fps" value="8"/>
</node>
```

### ANSI stdout fallback

```xml
<node pkg="logos_face" type="logos_face" name="logos_face" output="screen">
  <param name="output_mode" value="ansi"/>
  <param name="fps" value="8"/>
</node>
```

### Experimental X11 window

```xml
<node pkg="logos_face" type="logos_face" name="logos_face" output="screen">
  <param name="output_mode" value="display"/>
  <param name="caca_driver" value="x11"/>
</node>
```

### Experimental OpenGL output

```xml
<node pkg="logos_face" type="logos_face" name="logos_face" output="screen">
  <param name="output_mode" value="display"/>
  <param name="caca_driver" value="gl"/>
</node>
```

---

## Rendering Notes / Design Constraints

* The node intentionally renders via:

  1. OpenCV bitmap drawing (eyes/waveform)
  2. libcaca dither of the bitmap into a character canvas

* Transparency rule:

  * **Pure black (0,0,0)** pixels are treated as transparent via the alpha channel.
  * This is intentional; do not change unless you want to alter the look.

* Performance notes:

  * libcaca canvas/dither/display are kept **persistent** to reduce per-frame allocation churn.
  * FPS is controlled by ROS timer; do not replace with libcaca frame pacing if you rely on external FPS management.

---

## Troubleshooting

### “Display driver failed, fell back to ANSI”

* The requested driver may not be available on the system.
* Try `ncurses` first.
* Ensure you have an X session running before trying `x11` or `gl`.

### “Keyboard controls don’t work”

* In `display` mode: events are handled by libcaca; focus must be on the display window/terminal.
* In `ansi` mode: the node uses raw terminal input; ensure it is run in the terminal you expect.

### Looks stretched / wrong aspect ratio

* libcaca drivers differ in how they map pixels to character cells.
* You can experiment with:

  * terminal font
  * `CACA_GEOMETRY`
  * dither settings (charset/algorithm)

---

## License / Credits

* Uses **libcaca** for ASCII art dithering and optional display backends.
* Uses **OpenCV** for bitmap rendering.