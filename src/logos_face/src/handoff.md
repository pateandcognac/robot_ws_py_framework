We are working on Logos’s ROS Noetic C++ face node.

Goal:
Add a simple “sine mouth trail” effect to the existing mouth sine wave drawing.

Important design constraints:
- Do NOT implement this as “last N sine waves.”
- Use a persistent OpenCV image/buffer/layer for the sine mouth trail.
- Each render frame should fade the existing trail layer, then draw the newest sine wave into that trail layer at full intensity.
- The audio waveform must be drawn over the sine trails.
- Keep this simple. Do not add a pile of tunables.

New tunables:
1. mouth_trail_enabled
   - bool
   - default: true

2. mouth_trail_half_life
   - double
   - default suggestion: 0.5 seconds
   - meaning: how long it takes the trail brightness to decay by half
   - must be time-based, not frame-count-based, because FPS can change.

Current relevant behavior:
- renderCallback() clears frame_bgr_ each frame.
- renderCallback() calls renderEyes(frame_bgr_), then renderWaveform(frame_bgr_), then dithers frame_bgr_ to libcaca.
- renderWaveform() currently:
  - creates/generates sine_wave_buffer_
  - maybe draws the multicolor audio waveform
  - then draws the sine wave on top
- generateSineWaveInPlace() advances effect_params_.phase once per rendered frame.

Desired new render order:
1. Clear main frame.
2. Render eyes.
3. Fade/update persistent sine trail layer.
4. Draw the latest sine wave into the trail layer at full intensity.
5. Composite/add the sine trail layer onto the main frame.
6. If audio is active, draw the multicolor audio waveform over the sine trails.
7. Dither/output as before.

Implementation notes:
- Add a persistent cv::Mat member for the mouth sine trail layer, probably CV_8UC3.
- It can be full-frame size for simplicity. *A mouth-only ROI is preferable if it stays simple and clean, but do not over-engineer it.*
- Ensure the trail buffer is created/resized/cleared when render geometry changes.
- Use black as “empty” in the trail layer so the existing pure-black transparency behavior remains compatible with libcaca.
- If mouth_trail_enabled is false, clear the trail layer and draw only the current sine wave normally, preserving old behavior except for the requested audio-over-sine order if practical.
- If mouth_trail_half_life <= 0, treat it as “no trail” or clamp to a tiny safe positive value. Avoid divide-by-zero.
- Compute fading using elapsed wall/ROS time:
    decay = pow(0.5, dt / mouth_trail_half_life)
  Then multiply/dim the trail layer by that decay each frame.
- Track the previous trail update time with a ros::Time member.
- The newest sine wave should always be drawn at full intensity using effect_params_.color.
- Audio waveform should render after the sine trail, so it visually sits on top.
- Do not create extra allocations every frame if easily avoidable.
- Keep C++ style consistent with the existing file.

Likely places to inspect/edit:
- The main C++ face node file containing FaceNodeCpp.
- The dynamic_reconfigure cfg file for logos_face::FaceNodeConfig, if tunables are managed there.
- Any launch/config YAML that sets defaults, if applicable.

Suggested structure:
- Add members:
    bool mouth_trail_enabled_;
    double mouth_trail_half_life_;
    cv::Mat mouth_trail_bgr_;
    ros::Time mouth_trail_last_update_;
- Initialize the params in the constructor using nh_.param.
- Add them to dynamic_reconfigure if the project’s cfg file supports the other face/render params.
- In recreateRenderBuffersLocked(), create/clear mouth_trail_bgr_ to match render dimensions.
- Split renderWaveform() slightly if needed:
    - generate/normalize sine buffer once
    - update/draw sine trail
    - composite trail onto img
    - draw audio waveform over it
- Avoid changing message semantics or ROS topic behavior.

Acceptance criteria:
- With mouth_trail_enabled=true, the sine mouth leaves smooth fading trails.
- Trail persistence is consistent across FPS changes because decay is based on elapsed time.
- The newest sine wave appears at full brightness.
- The audio waveform is drawn over the sine trails while speaking.
- With mouth_trail_enabled=false, the mouth still draws a normal sine wave and audio still works.
- No per-frame vector history of old waves is added.
- No big architecture rewrite.
- Build succeeds in ROS Noetic.