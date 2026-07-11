# Pico HUB75 hardware face

The standalone firmware lives in `/home/robot/pico/logos_face_hub75`. It drives
the proven salvaged 32x16, 1/8-scan panel mapping from a dedicated RP2040 scan
core and does not contain ROS code. The first PIO scanner was hardware-tested
blank, so the active implementation uses the known-good GPIO sequence.

USB CDC accepts newline-delimited JSON protocol version 1. `face` packets carry
two six-value eye arrays (`gaze_x`, `gaze_y`, `scale_x`, `scale_y`, `lid_height`,
`lid_angle`) and a four-value mouth array (`frequency`, `amplitude`, `phase`,
`phase_increment`). `audio` carries 16 unsigned levels. `config.brightness` is
0-255, but firmware clamps it to its hard safety cap of 96; default is 24.

For Logos, run after the Pico enumerates:

```sh
rosrun logos_face pico_face_bridge.py _device:=/dev/ttyACM0 _brightness:=24
```

The bridge consumes `/face/live_state/json` plus `/face/mouth/audio_wave`. It
reduces PCM to visual levels rather than transporting audio to the Pico.
The firmware uses those levels to perturb the bottom-row sine mouth; it does
not reserve the outer display columns for audio.

Eyes are clipped rectangles: `scale_x` and `scale_y` map to 4-12 pixels, and
each lid/brow spans only one pixel beyond the rendered eye width. The mouth
maps positive sine samples to the top row and negative samples to the bottom
row, with direct absolute-amplitude brightness. High-rate face/audio packets
intentionally have no acknowledgement to avoid filling USB CDC output when a
host only writes commands.

The scan core snapshots a completed framebuffer only at a full PWM-frame
boundary. GPIO data, clock, and latch transitions include explicit setup/hold
settling because the salvaged panel is being driven directly at 3.3 V. The
`status` command reports packet ages/counts, parser errors, and line overflows
to distinguish a live local animation from a stalled host data stream.
