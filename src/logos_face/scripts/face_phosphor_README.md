# PHOSPHOR — a vector-oscilloscope face for Logos

`face_phosphor_node.py` is a graphical, drop-in alternative to the ASCII
`face_hud_node`. Same topics in, same `/face/live_state/json` out — different
soul. Where `face_hud` is Logos's terminal heritage, PHOSPHOR is the analog
instrument sibling: the face rendered as glowing vector strokes on a
green-black CRT, complete with phosphor persistence.

The concept comes from Logos himself: his mouth has always been an
oscilloscope trace, and his head is an oval bezel — very nearly a round tube.
This node just finishes the thought.

## What it looks like

- **Vector strokes with bloom.** Eyes, brows, and mouth are additive glowing
  strokes. Fills run cooler than outlines so colors stay saturated instead of
  blowing out to white.
- **Phosphor persistence.** Strokes leave decaying afterglow. Blinks and
  saccades streak light across the tube for free; the speaking waveform
  builds Lissajous-like ghost curtains.
- **Instrument glass.** A faint dot graticule with center axes and a mouth
  baseline sits behind everything. An oval vignette melts the image into the
  physical bezel; subtle scanlines finish the tube.
- **CRT warm-up.** On boot: dot, horizontal sweep, then the face fades in
  through the persistence buffer.
- **Word-synced captions.** `kind: caption` HUD events reveal word-by-word
  across the event duration, so text lands roughly with the TTS audio.
  A block cursor blinks at the end of the stream, as is right and proper.

## Launch

```bash
bin/logos_face_phosphor.sh
# or
rosrun logos_face face_phosphor_node.py
```

Runs frameless at 800x1280 (portrait; the OS handles rotation) on the head
display. `q`/`ESC` quits, `[`/`]` adjusts the FPS ceiling, `-`/`+` moves the
pane split, `\` clears HUD panes, `f` toggles windowed mode.

The `face_hud_bridge_node.py` (from `logos_core.launch`) feeds it exactly as
it feeds the ASCII HUD — no bridge changes needed.

## Topics

Identical to `face_hud_node`:
subscribes `/face/eye_{gaze,scale}_{x,y}`, `/face/eye_lid_{height,angle}`,
`/face/eye_color`, `/face/mouth/{sine_wave,audio_wave}`, `/face/hud/event`,
`/face/layer{0,2}/image`; publishes `/face/live_state/json` with the same
schema (the Pico face bridge doesn't know the difference).

HUD events support the same JSON grammar: panes `face`/`status`/`all`, kinds
`text`/`figlet`/`caption`/`clear`, `effect: crawl` with
speed/location/direction/density/tiling, colors as caca names or `#RRGGBB`.
Figlet renders via `pyfiglet` in-process and falls back to plain text.

## Efficiency

The stroke/trail/bloom pipeline runs at `~render_scale` (default 0.5)
internal resolution and upscales once. The node renders at `~fps`
(default 24) only while something is happening — animations, audio, caption
reveal, crawls, images — and drops to `~idle_fps` (default 10) otherwise.
Treat `fps` as a ceiling, not a rate.

Measured offscreen on the robot's i7-8550U: **~12% of one core idle, ~40%
while speaking.** Static content (vignette, scanlines, graticule) is baked
into pane backgrounds at startup; per-frame BLEND_MULT fills were profiled
out (they cost ~3.5 ms each on this box — alpha-blit decay and dual-gain
stroke passes replace them).

## Tuning params (private, `_param:=value`)

| Param | Default | Meaning |
|---|---|---|
| `fps` / `idle_fps` | 24 / 10 | active ceiling / idle rate |
| `render_scale` | 0.5 | internal stroke-buffer resolution |
| `trail_tau` | 0.22 | persistence decay time constant (s) |
| `trail_gain` | 0.5 | ghost injection brightness |
| `bloom_strength` | 0.65 | halo intensity (0 disables) |
| `status_region_ratio` | 0.33 | caption pane height fraction |
| `phase_rate_hz` | 8.0 | idle-sine phase speed (matches 8 fps heritage) |
| `window_width/height` | 800/1280 | display size |
| `windowed` | false | frameless fullscreen vs. window |
| `frame_dump_dir` | "" | dump PNGs periodically (debug/remote eyes) |
| `layer_image_*` | as face_hud | image fade envelope |

## Testing without the robot display

```bash
# isolated master + offscreen render + frame dumps
export ROS_MASTER_URI=http://localhost:11322 SDL_VIDEODRIVER=dummy
roscore -p 11322 &
rosrun logos_face face_phosphor_node.py _frame_dump_dir:=/tmp/phosphor_frames
```

Zero new dependencies: pygame 2.6, numpy, and pyfiglet were already aboard.
