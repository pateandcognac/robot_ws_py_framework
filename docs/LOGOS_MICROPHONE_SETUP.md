# Logos Microphone Setup

`stt_node.py` prefers an ALSA/PortAudio capture device named `logos_mic` and
falls back to the old webcam alias, `pan_tilt_mic`.

The existing webcam alias lives in `/home/robot/.asoundrc`:

```conf
pcm.pan_tilt_mic {
    type plug
    slave.pcm "hw:Webcam,0"
}

ctl.pan_tilt_mic {
    type hw
    card "Webcam"
}
```

## Find the New Mic ALSA Card Id

Plug in the mic and run these on the robot host:

```bash
lsusb | grep -i 'ff01:0009'
arecord -l
for f in /proc/asound/card*/usbid; do echo "$f: $(cat "$f")"; done
```

Find the `/proc/asound/cardN/usbid` entry that prints `ff01:0009`, then read
that card's ALSA id:

```bash
cat /proc/asound/cardN/id
```

Replace `cardN` with the matching card directory. The result is the stable card
name to use below.

## Add the `logos_mic` Alias

Add this block to `/home/robot/.asoundrc`, replacing `NEW_CARD_ID` with the
value from `/proc/asound/cardN/id`:

```conf
pcm.logos_mic {
    type plug
    slave.pcm "hw:NEW_CARD_ID,0"
}

ctl.logos_mic {
    type hw
    card "NEW_CARD_ID"
}
```

Reloading the STT node is usually enough. If ALSA keeps the old device table,
unplug/replug the mic or reboot the robot.

## Override Device Preference

The default preference is:

```text
logos_mic,pan_tilt_mic
```

To override without editing code:

```bash
LOGOS_STT_AUDIO_DEVICES=logos_mic,pan_tilt_mic logos_stt.sh
```

or launch the node with private ROS param `~audio_devices` as either a comma
separated string or a list.
