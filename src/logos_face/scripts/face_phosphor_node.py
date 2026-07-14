#!/usr/bin/env python3
"""
face_phosphor_node.py — PHOSPHOR: a vector-oscilloscope face for Logos.

Logos's mouth has always been an oscilloscope trace, and his head is an oval
bezel — very nearly a round CRT tube. This node leans all the way in: the face
is rendered as glowing vector strokes on a phosphor screen. Strokes leave
decaying persistence trails (blinks and saccades streak light, for free), a
soft bloom halo hugs every line, a faint graticule sits behind the glass, and
an oval vignette melts the image into the physical bezel. On boot the tube
"warms up": dot, then a horizontal sweep, then the face fades in.

It is a drop-in alternative to face_hud_node:

  Subscribes (identical topics & semantics):
    /face/eye_gaze_x, /face/eye_gaze_y      logos_msgs/EyeGazeX|Y
    /face/eye_scale_x, /face/eye_scale_y    logos_msgs/EyeScaleX|Y
    /face/eye_lid_height                    logos_msgs/EyeLidHeight
    /face/eye_lid_angle                     logos_msgs/EyeLidAngle
    /face/eye_color                         logos_msgs/EyeColor
    /face/mouth/sine_wave                   logos_msgs/MouthSine
    /face/mouth/audio_wave                  logos_msgs/AudioWave
    /face/hud/event                         std_msgs/String (JSON)
    /face/layer0/image, /face/layer2/image  sensor_msgs/Image

  Publishes (identical schema):
    /face/live_state/json                   std_msgs/String

Captions (pane=status, kind=caption) reveal word-by-word across the event's
duration, so text lands roughly in sync with the TTS audio.

Efficiency: the stroke/trail/bloom pipeline runs at a reduced internal
resolution (~render_scale) and the node drops from ~fps (active ceiling,
default 24) to ~idle_fps (default 10) whenever nothing is animating. The
`fps` param is therefore a ceiling, not a literal rate. Measured on the
robot's i7-8550U (offscreen): ~12% of one core idle, ~40% while speaking.

Keys (window focus): q/ESC quit, [ ] fps, -/+ pane split, \\ clear, f window.

Zero new dependencies: pygame, numpy, pyfiglet — all already on the robot.
"""

import json
import math
import os
import time
import colorsys
import threading

# Keep pygame's hello out of Logos's logs.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np
import pygame

import rospy
from std_msgs.msg import String
from sensor_msgs.msg import Image

from logos_msgs.msg import (
    EyeGazeX, EyeGazeY, EyeScaleX, EyeScaleY,
    EyeLidHeight, EyeLidAngle, EyeColor, MouthSine, AudioWave,
)

try:
    import pyfiglet
except ImportError:  # figlet degrades to plain text, matching face_hud
    pyfiglet = None


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

PHOSPHOR_BG = (2, 5, 3)          # deep green-black: the unlit tube
STATUS_BG = (1, 3, 2)
GRID_DOT = (0, 34, 20)
GRID_AXIS = (0, 24, 14)
DIVIDER = (0, 46, 27)

# caca ANSI color names -> phosphor-friendly RGB
CACA_RGB = {
    "black": (10, 14, 12),
    "blue": (40, 90, 220),
    "green": (0, 165, 88),
    "cyan": (0, 160, 160),
    "red": (200, 45, 45),
    "magenta": (170, 40, 170),
    "brown": (170, 120, 0),
    "yellow": (170, 120, 0),
    "lightgray": (168, 180, 174),
    "lightgrey": (168, 180, 174),
    "gray": (168, 180, 174),
    "grey": (168, 180, 174),
    "darkgray": (90, 100, 95),
    "darkgrey": (90, 100, 95),
    "bright_black": (90, 100, 95),
    "bright_blue": (96, 148, 255),
    "lightblue": (96, 148, 255),
    "bright_green": (57, 255, 106),
    "lightgreen": (57, 255, 106),
    "bright_cyan": (80, 255, 238),
    "lightcyan": (80, 255, 238),
    "bright_red": (255, 84, 92),
    "lightred": (255, 84, 92),
    "bright_magenta": (255, 92, 240),
    "lightmagenta": (255, 92, 240),
    "bright_yellow": (255, 232, 92),
    "lightyellow": (255, 232, 92),
    "bright_white": (232, 248, 240),
    "white": (232, 248, 240),
    "default": (168, 180, 174),
}

PLAIN_FIGLET_FONTS = {"term", "terminal", "plain", "text", "ascii", "none"}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def hex_to_rgb(value, default=(0, 255, 0)):
    if isinstance(value, str) and len(value) >= 7 and value[0] == "#":
        try:
            return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
        except ValueError:
            pass
    return default


def rgb_to_hex(rgb):
    return "#%02X%02X%02X" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def resolve_color(value, default_rgb):
    """Accept '#RRGGBB' or a caca color name; fall back to default."""
    if not isinstance(value, str) or not value:
        return default_rgb
    if value.startswith("#"):
        return hex_to_rgb(value, default_rgb)
    return CACA_RGB.get(value.lower(), default_rgb)


def build_amplitude_lut():
    """Violet (idx 0) -> red (idx 255), matching face_hud's OpenCV LUT."""
    lut = []
    for i in range(256):
        t = i / 255.0
        hue_deg = 270.0 * (1.0 - t)  # cv hue 135 == 270 degrees
        r, g, b = colorsys.hsv_to_rgb(hue_deg / 360.0, 1.0, 1.0)
        lut.append((int(r * 255), int(g * 255), int(b * 255)))
    return lut


def scale_rgb(rgb, k):
    return (int(rgb[0] * k), int(rgb[1] * k), int(rgb[2] * k))


# ---------------------------------------------------------------------------
# Animation primitives (same linear semantics as face_hud.cpp)
# ---------------------------------------------------------------------------

class Anim(object):
    __slots__ = ("start", "end", "duration", "t0", "active")

    def __init__(self):
        self.start = 0.0
        self.end = 0.0
        self.duration = 0.001
        self.t0 = 0.0
        self.active = False

    def set(self, start, end, duration, now):
        self.start = float(start)
        self.end = float(end)
        self.duration = max(float(duration), 0.001)
        self.t0 = now
        self.active = True

    def value(self, now, target):
        if not self.active:
            return target
        t = (now - self.t0) / self.duration
        if t >= 1.0:
            t = 1.0
            self.active = False
        return self.start + (self.end - self.start) * t


class ColorAnim(object):
    __slots__ = ("start", "end", "duration", "t0", "active")

    def __init__(self):
        self.start = (0, 255, 0)
        self.end = (0, 255, 0)
        self.duration = 0.001
        self.t0 = 0.0
        self.active = False

    def set(self, start_rgb, end_rgb, duration, now):
        self.start = start_rgb
        self.end = end_rgb
        self.duration = max(float(duration), 0.001)
        self.t0 = now
        self.active = True

    def value(self, now, target_rgb):
        if not self.active:
            return target_rgb
        t = (now - self.t0) / self.duration
        if t >= 1.0:
            t = 1.0
            self.active = False
        return tuple(
            int(self.start[i] + (self.end[i] - self.start[i]) * t) for i in range(3)
        )

class EyeState(object):
    """One eye: animated gaze/scale/lid/color, mirroring face_hud defaults."""

    PARAMS = ("gaze_x", "gaze_y", "scale_x", "scale_y", "lid_height", "lid_angle")

    def __init__(self, lid_height):
        self.target = {
            "gaze_x": 0.0, "gaze_y": 0.0,
            "scale_x": 1.0, "scale_y": 1.0,
            "lid_height": lid_height, "lid_angle": 0.0,
        }
        self.anims = {p: Anim() for p in self.PARAMS}
        self.color_target = (0, 255, 0)
        self.color_anim = ColorAnim()

    def set_param(self, param, value, duration, now):
        anim = self.anims[param]
        current = anim.value(now, self.target[param])
        self.target[param] = float(value)
        anim.set(current, value, duration, now)

    def set_color(self, rgb, duration, now):
        current = self.color_anim.value(now, self.color_target)
        self.color_target = rgb
        self.color_anim.set(current, rgb, duration, now)

    def current(self, now):
        vals = {p: self.anims[p].value(now, self.target[p]) for p in self.PARAMS}
        vals["color"] = self.color_anim.value(now, self.color_target)
        return vals


class MouthState(object):
    """Sine-wave mouth effect params, mirroring face_hud's effect params."""

    def __init__(self):
        self.target = {"frequency": 1.0, "amplitude": 1.0, "phase_increment": 0.1}
        self.anims = {k: Anim() for k in self.target}
        self.phase = 0.0
        self.phase_anim = Anim()
        self.phase_target = 0.0
        self.color_target = (0, 255, 0)
        self.color_anim = ColorAnim()

    def apply(self, msg, now):
        duration = max(float(msg.duration), 0.001)
        for key, value in (
            ("frequency", msg.frequency),
            ("amplitude", msg.amplitude),
            ("phase_increment", msg.phase_increment),
        ):
            current = self.anims[key].value(now, self.target[key])
            self.target[key] = float(value)
            self.anims[key].set(current, value, duration, now)
        self.phase_target = float(msg.phase)
        self.phase_anim.set(self.phase, msg.phase, duration, now)
        rgb = hex_to_rgb(msg.color, self.color_target)
        current_rgb = self.color_anim.value(now, self.color_target)
        self.color_target = rgb
        self.color_anim.set(current_rgb, rgb, duration, now)

    def current(self, now):
        vals = {k: self.anims[k].value(now, self.target[k]) for k in self.target}
        if self.phase_anim.active:
            self.phase = self.phase_anim.value(now, self.phase_target)
        vals["phase"] = self.phase
        vals["color"] = self.color_anim.value(now, self.color_target)
        return vals

    def any_active(self):
        return self.phase_anim.active or any(a.active for a in self.anims.values())


# ---------------------------------------------------------------------------
# HUD structures
# ---------------------------------------------------------------------------

class CrawlState(object):
    def __init__(self):
        self.active = False
        self.text = ""
        self.color = (57, 255, 106)
        self.t0 = 0.0
        self.speed = 8.0
        self.duration = 0.0
        self.location_x = 0.0
        self.location_y = 600.0
        self.direction = (-1.0, 0.0)
        self.density = 1.0
        self.tile_x = True
        self.tile_y = False
        self.surface = None  # built lazily on the render thread


class LayerImage(object):
    def __init__(self):
        self.rgb = None          # numpy HxWx3, set on callback thread
        self.surface = None      # built lazily on render thread
        self.scaled = None
        self.t0 = 0.0
        self.active = False


class StatusJob(object):
    """A queued status-pane print; captions reveal word-by-word."""

    def __init__(self, kind, color, duration=0.0, words=None, lines=None):
        self.kind = kind                  # 'caption' | 'status' | 'figlet'
        self.color = color
        self.duration = max(0.0, duration)
        self.words = words or []          # caption reveal source
        self.lines = lines or []          # pre-split lines for instant jobs
        self.revealed = 0                 # words revealed so far
        self.built_lines = []             # wrapped lines built during reveal
        self.started = False
        self.t0 = 0.0

    @property
    def caption(self):
        return self.kind == "caption" and self.duration > 0.0 and len(self.words) > 1


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------

class PhosphorFace(object):
    def __init__(self):
        rospy.init_node("logos_face_phosphor")

        # --- params ------------------------------------------------------
        gp = rospy.get_param
        self.win_w = int(gp("~window_width", 800))
        self.win_h = int(gp("~window_height", 1280))
        self.windowed = bool(gp("~windowed", False))
        self.fps_max = int(clamp(gp("~fps", 24), 1, 60))
        self.fps_idle = int(clamp(gp("~idle_fps", 10), 1, 60))
        self.render_scale = clamp(float(gp("~render_scale", 0.5)), 0.2, 1.0)
        self.status_ratio = clamp(float(gp("~status_region_ratio", 0.33)), 0.05, 0.95)
        self.trail_tau = max(0.02, float(gp("~trail_tau", 0.22)))
        self.trail_gain = clamp(float(gp("~trail_gain", 0.5)), 0.0, 1.0)
        self.bloom_strength = clamp(float(gp("~bloom_strength", 0.65)), 0.0, 1.0)
        self.phase_rate = float(gp("~phase_rate_hz", 8.0))  # matches 8fps heritage
        self.face_max_lines = int(gp("~face_max_lines", 240))
        self.status_max_lines = int(gp("~status_max_lines", 60))
        self.default_figlet_font = str(gp("~default_figlet_font", "standard"))
        self.fade_in = max(0.0, float(gp("~layer_image_fade_in_sec", 0.6)))
        self.hold = max(0.0, float(gp("~layer_image_hold_sec", 4.0)))
        self.fade_out = max(0.0, float(gp("~layer_image_fade_out_sec", 0.8)))
        self.image_max_alpha = clamp(float(gp("~layer_image_max_alpha", 1.0)), 0.0, 1.0)
        self.frame_dump_dir = str(gp("~frame_dump_dir", ""))
        self.frame_dump_period = max(0.2, float(gp("~frame_dump_period", 2.0)))

        self.face_default_rgb = resolve_color(gp("~face_canvas_color", "bright_green"), CACA_RGB["bright_green"])
        self.status_default_rgb = resolve_color(gp("~status_color", "bright_white"), CACA_RGB["bright_white"])
        self.caption_default_rgb = resolve_color(gp("~caption_color", "bright_magenta"), CACA_RGB["bright_magenta"])

        # --- pygame ------------------------------------------------------
        if not self.windowed:
            os.environ.setdefault("SDL_VIDEO_WINDOW_POS", "0,0")
        pygame.display.init()
        pygame.font.init()
        flags = 0 if self.windowed else pygame.NOFRAME
        self.window = pygame.display.set_mode((self.win_w, self.win_h), flags)
        pygame.display.set_caption("LOGOS // PHOSPHOR")
        pygame.mouse.set_visible(False)

        mono = pygame.font.match_font("dejavusansmono", bold=True) or \
            pygame.font.match_font("liberationmono", bold=True)
        mono_reg = pygame.font.match_font("dejavusansmono") or mono
        s = self.win_w / 800.0  # layout scale for non-default window sizes
        self.caption_font = pygame.font.Font(mono, max(12, int(44 * s)))
        self.status_font = pygame.font.Font(mono_reg, max(9, int(24 * s)))
        self.face_font = pygame.font.Font(mono, max(9, int(22 * s)))
        self.margin = int(26 * s)
        self._text_cache = {}  # (font_id, text, rgb) -> (crisp, glow)

        # --- state -------------------------------------------------------
        self.lock = threading.Lock()
        now = time.monotonic()
        self.boot_t0 = now
        self.boot_seconds = 1.6
        self.last_activity = now
        self.eyes = {"left": EyeState(1.0), "right": EyeState(0.5)}
        self.mouth = MouthState()
        self.audio = None            # numpy float array in [-1, 1]
        self.audio_sr = 22050.0
        self.audio_t0 = 0.0
        self.audio_duration = 0.0
        self.amplitude_lut = build_amplitude_lut()

        # face-pane text layers: index 0 -> layer 0, index 1 -> layer 2
        self.face_lines = [[], []]   # list of (text, rgb, expires_at)
        self.crawls = [CrawlState(), CrawlState()]
        self.layer_images = [LayerImage(), LayerImage()]

        self.status_history = []     # list of (kind, text, rgb)
        self.status_jobs = []        # list of StatusJob
        self.status_dirty = True
        self._status_surface = None
        self._cursor_on = True
        self._last_blink = now
        self._last_dump = 0.0

        self.rebuild_geometry()

        # --- ROS ---------------------------------------------------------
        self.pub_live = rospy.Publisher("/face/live_state/json", String, queue_size=10)
        sub = rospy.Subscriber
        sub("/face/eye_gaze_x", EyeGazeX, self._cb_simple("gaze_x", "gaze_x"), queue_size=10)
        sub("/face/eye_gaze_y", EyeGazeY, self._cb_simple("gaze_y", "gaze_y"), queue_size=10)
        sub("/face/eye_scale_x", EyeScaleX, self._cb_simple("scale_x", "scale_x"), queue_size=10)
        sub("/face/eye_scale_y", EyeScaleY, self._cb_simple("scale_y", "scale_y"), queue_size=10)
        sub("/face/eye_lid_height", EyeLidHeight, self._cb_simple("lid_height", "lid_height"), queue_size=10)
        sub("/face/eye_lid_angle", EyeLidAngle, self.cb_lid_angle, queue_size=10)
        sub("/face/eye_color", EyeColor, self.cb_color, queue_size=10)
        sub("/face/mouth/sine_wave", MouthSine, self.cb_sine, queue_size=10)
        sub("/face/mouth/audio_wave", AudioWave, self.cb_audio, queue_size=10)
        sub("/face/hud/event", String, self.cb_hud_event, queue_size=50)
        sub("/face/layer0/image", Image, lambda m: self.cb_layer_image(m, 0), queue_size=1)
        sub("/face/layer2/image", Image, lambda m: self.cb_layer_image(m, 2), queue_size=1)

        rospy.loginfo("PHOSPHOR face online: %dx%d, fps %d (idle %d), scale %.2f",
                      self.win_w, self.win_h, self.fps_max, self.fps_idle, self.render_scale)
        self._push_status("status", "· phosphor face online", scale_rgb(self.face_default_rgb, 0.75))

    # ------------------------------------------------------------------
    # Geometry / static surfaces
    # ------------------------------------------------------------------

    def rebuild_geometry(self):
        self.status_h = int(round(self.win_h * self.status_ratio))
        self.face_h = self.win_h - self.status_h
        self.face_w = self.win_w
        self.lw = max(64, int(self.face_w * self.render_scale))
        self.lh = max(64, int(self.face_h * self.render_scale))

        self.trail = pygame.Surface((self.lw, self.lh)).convert()
        self.trail.fill((0, 0, 0))
        self.trail_scratch = self.trail.copy()
        self.strokes = pygame.Surface((self.lw, self.lh)).convert()
        self.strokes_dim = self.strokes.copy()
        self.face_pane = pygame.Surface((self.face_w, self.face_h)).convert()
        self._status_surface = pygame.Surface((self.win_w, self.status_h)).convert()
        self.status_dirty = True
        for img in self.layer_images:
            img.scaled = None

        # Vignette + scanlines are static, so bake them into the pane
        # backgrounds once instead of alpha-blitting a full-screen overlay
        # every frame (that blit alone cost ~1.5 ms).
        overlay = self._build_overlay()
        self.face_bg = self._build_graticule()
        self.face_bg.blit(overlay, (0, 0),
                          pygame.Rect(0, 0, self.win_w, self.face_h))
        self.status_bg = pygame.Surface((self.win_w, self.status_h)).convert()
        self.status_bg.fill(STATUS_BG)
        pygame.draw.line(self.status_bg, DIVIDER, (0, 0), (self.win_w, 0))
        self.status_bg.blit(overlay, (0, 0),
                            pygame.Rect(0, self.face_h, self.win_w, self.status_h))

    def _build_graticule(self):
        """The instrument glass: dot grid, center axes, mouth baseline."""
        bg = pygame.Surface((self.face_w, self.face_h)).convert()
        bg.fill(PHOSPHOR_BG)
        step = max(24, int(self.face_w / 16))
        cx, cy = self.face_w // 2, int(self.face_h * 0.5)
        for y in range(step // 2, self.face_h, step):
            for x in range(step // 2, self.face_w, step):
                bg.set_at((x, y), GRID_DOT)
        pygame.draw.line(bg, GRID_AXIS, (cx, 0), (cx, self.face_h))
        baseline = int(self.face_h * 0.875)
        pygame.draw.line(bg, GRID_AXIS, (0, baseline), (self.face_w, baseline))
        # short ticks along the axes, like a scope reticle
        tick = max(3, step // 8)
        for y in range(step // 2, self.face_h, step):
            pygame.draw.line(bg, GRID_DOT, (cx - tick, y), (cx + tick, y))
        for x in range(step // 2, self.face_w, step):
            pygame.draw.line(bg, GRID_DOT, (x, baseline - tick), (x, baseline + tick))
        return bg

    def _build_overlay(self):
        """Oval vignette (melts into the bezel) + faint scanlines, one RGBA blit."""
        w, h = self.win_w, self.win_h
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        nx = (xx - w / 2.0) / (w * 0.62)
        ny = (yy - h / 2.0) / (h * 0.60)
        r = np.sqrt(nx * nx + ny * ny)
        alpha = np.clip((r - 0.78) / 0.34, 0.0, 1.0) * 215.0
        alpha += ((yy.astype(np.int32) % 3) == 0) * 13.0
        alpha = np.clip(alpha, 0, 255).astype(np.uint8)
        overlay = pygame.Surface((w, h), pygame.SRCALPHA)
        pixels = pygame.surfarray.pixels_alpha(overlay)
        pixels[:, :] = alpha.T
        del pixels
        return overlay

    # ------------------------------------------------------------------
    # ROS callbacks (thin: store state under lock)
    # ------------------------------------------------------------------

    def _touch(self):
        self.last_activity = time.monotonic()

    def _cb_simple(self, param, field):
        def handler(msg):
            now = time.monotonic()
            with self.lock:
                for side in self._sides(msg.eye_side):
                    self.eyes[side].set_param(param, getattr(msg, field), msg.duration, now)
                self._touch()
        return handler

    @staticmethod
    def _sides(eye_side):
        if eye_side == "both":
            return ("left", "right")
        if eye_side in ("left", "right"):
            return (eye_side,)
        return ()

    def cb_lid_angle(self, msg):
        now = time.monotonic()
        with self.lock:
            # face_hud negates the left lid angle so brows mirror.
            if msg.eye_side in ("left", "both"):
                self.eyes["left"].set_param("lid_angle", -msg.lid_angle, msg.duration, now)
            if msg.eye_side in ("right", "both"):
                self.eyes["right"].set_param("lid_angle", msg.lid_angle, msg.duration, now)
            self._touch()

    def cb_color(self, msg):
        now = time.monotonic()
        rgb = hex_to_rgb(msg.color)
        with self.lock:
            for side in self._sides(msg.eye_side):
                self.eyes[side].set_color(rgb, msg.duration, now)
            self._touch()

    def cb_sine(self, msg):
        now = time.monotonic()
        with self.lock:
            self.mouth.apply(msg, now)
            self._touch()

    def cb_audio(self, msg):
        wave = np.asarray(msg.data, dtype=np.float32) / 32767.0
        with self.lock:
            self.audio = wave
            self.audio_sr = float(msg.sample_rate) or 22050.0
            self.audio_t0 = time.monotonic()
            self.audio_duration = len(wave) / self.audio_sr if self.audio_sr > 0 else 0.0
            self._touch()

    def cb_layer_image(self, msg, layer):
        rgb = self._image_to_rgb(msg)
        if rgb is None:
            return
        now = time.monotonic()
        idx = 0 if layer == 0 else 1
        with self.lock:
            img = self.layer_images[idx]
            was_active = img.active and (now - img.t0) <= (self.fade_in + self.hold + self.fade_out)
            img.rgb = rgb
            img.surface = None
            img.scaled = None
            img.active = True
            img.t0 = (now - self.fade_in) if was_active else now
            self._touch()

    @staticmethod
    def _image_to_rgb(msg):
        try:
            buf = np.frombuffer(msg.data, dtype=np.uint8)
            if msg.encoding in ("bgr8", "rgb8"):
                arr = buf.reshape(msg.height, msg.step // 3, 3)[:, : msg.width, :]
                return arr[:, :, ::-1].copy() if msg.encoding == "bgr8" else arr.copy()
            if msg.encoding in ("bgra8", "rgba8"):
                arr = buf.reshape(msg.height, msg.step // 4, 4)[:, : msg.width, :3]
                return arr[:, :, ::-1].copy() if msg.encoding == "bgra8" else arr.copy()
            if msg.encoding == "mono8":
                arr = buf.reshape(msg.height, msg.step)[:, : msg.width]
                return np.repeat(arr[:, :, None], 3, axis=2).copy()
        except Exception as exc:
            rospy.logwarn("phosphor: image convert failed: %s", exc)
        rospy.logwarn_once("phosphor: unsupported image encoding '%s'", msg.encoding)
        return None

    # ------------------------------------------------------------------
    # HUD events
    # ------------------------------------------------------------------

    def cb_hud_event(self, msg):
        try:
            root = json.loads(msg.data)
        except (ValueError, TypeError) as exc:
            rospy.logwarn("phosphor: bad HUD event JSON: %s", exc)
            return
        if not isinstance(root, dict):
            return

        pane = str(root.get("pane", "face")).lower()
        kind = str(root.get("kind", "text")).lower()
        if pane not in ("face", "status", "all"):
            rospy.logwarn("phosphor: unknown HUD pane '%s'", pane)
            return
        if pane == "all" and kind != "clear" and not root.get("clear", False):
            return

        with self.lock:
            self._touch()
            if kind == "clear" or root.get("clear", False):
                self._clear_pane(pane, root.get("layer"))
                if kind == "clear":
                    return
            text = str(root.get("text", ""))
            if not text:
                return

            if pane == "face":
                self._face_event(root, kind, text)
            else:
                self._status_event(root, kind, text)

    def _clear_pane(self, pane, layer):
        if pane in ("status", "all"):
            self.status_history = []
            self.status_jobs = []
            self.status_dirty = True
        if pane in ("face", "all"):
            layers = (0, 1)
            if pane == "face" and layer in (0, 2):
                layers = (0 if layer == 0 else 1,)
            for i in layers:
                self.face_lines[i] = []
                self.crawls[i] = CrawlState()
                self.layer_images[i] = LayerImage()

    def _figlet(self, text, font):
        if font.lower() in PLAIN_FIGLET_FONTS:
            return text
        if pyfiglet is None:
            return text
        safe = "".join(c for c in font if c.isalnum() or c in "_-") or "standard"
        cols = max(20, (self.win_w - 2 * self.margin) // max(6, self.status_font.size("M")[0]))
        try:
            rendered = pyfiglet.figlet_format(text, font=safe, width=cols)
            return rendered.rstrip("\n") or text
        except Exception:
            rospy.logwarn("phosphor: pyfiglet failed for font '%s'", safe)
            return text

    def _face_event(self, root, kind, text):
        if kind not in ("text", "figlet"):
            rospy.logwarn("phosphor: unsupported face HUD kind '%s'", kind)
            return
        layer = root.get("layer", 0)
        if layer not in (0, 2):
            rospy.logwarn("phosphor: invalid face layer '%s'", layer)
            return
        idx = 0 if layer == 0 else 1
        rgb = resolve_color(root.get("color"), self.face_default_rgb)
        if kind == "figlet":
            text = self._figlet(text, str(root.get("font", self.default_figlet_font)))

        effect = str(root.get("effect", "terminal")).lower()
        now = time.monotonic()
        if effect in ("crawl", "scroll", "marquee", "move", "motion"):
            crawl = CrawlState()
            crawl.text = text
            crawl.color = rgb
            crawl.t0 = now
            crawl.speed = max(0.1, float(root.get("speed", 8.0)))
            crawl.duration = max(0.0, float(root.get("duration", 0.0)))
            crawl.location_x = clamp(float(root.get("location_x", 0.0)), 0.0, 1000.0)
            crawl.location_y = clamp(float(root.get("location_y", 600.0)), 0.0, 1000.0)
            dx = float(root.get("direction_x", -1000.0))
            dy = float(root.get("direction_y", 0.0))
            mag = math.hypot(dx, dy)
            crawl.direction = (dx / mag, dy / mag) if mag > 1e-6 else (0.0, 0.0)
            crawl.density = clamp(float(root.get("density", 1000.0)) / 1000.0, 0.0, 1.0)
            crawl.tile_x = bool(root.get("tile_x", True))
            crawl.tile_y = bool(root.get("tile_y", False))
            crawl.active = True
            self.crawls[idx] = crawl
            return

        duration = float(root.get("duration", 0.0))
        expires = now + duration if duration > 0.0 else 0.0
        maxw = self.face_w - 2 * self.margin
        for line in self._wrap(text, self.face_font, maxw):
            self.face_lines[idx].append((line, rgb, expires))
        if len(self.face_lines[idx]) > self.face_max_lines:
            self.face_lines[idx] = self.face_lines[idx][-self.face_max_lines:]

    def _status_event(self, root, kind, text):
        if kind == "caption":
            rgb = resolve_color(root.get("color"), self.caption_default_rgb)
            job = StatusJob("caption", rgb,
                            duration=float(root.get("duration", 0.0)),
                            words=text.split())
        elif kind == "figlet":
            rgb = resolve_color(root.get("color"), self.status_default_rgb)
            rendered = self._figlet(text, str(root.get("font", self.default_figlet_font)))
            job = StatusJob("figlet", rgb, lines=rendered.split("\n"))
        else:
            rgb = resolve_color(root.get("color"), self.status_default_rgb)
            maxw = self.win_w - 2 * self.margin
            job = StatusJob("status", rgb, lines=self._wrap(text, self.status_font, maxw))

        # Match face_hud: non-captions queue behind a currently revealing
        # caption but ahead of the next caption.
        if not job.caption and self.status_jobs and self.status_jobs[0].caption:
            i = 1
            while i < len(self.status_jobs) and not self.status_jobs[i].caption:
                i += 1
            self.status_jobs.insert(i, job)
        else:
            self.status_jobs.append(job)

    def _push_status(self, kind, text, rgb):
        with self.lock:
            self.status_history.append((kind, text, rgb))
            self._trim_status()
            self.status_dirty = True

    def _trim_status(self):
        if len(self.status_history) > self.status_max_lines:
            self.status_history = self.status_history[-self.status_max_lines:]

    @staticmethod
    def _wrap(text, font, maxw):
        lines = []
        for raw in text.replace("\r", "").split("\n"):
            if not raw:
                lines.append("")
                continue
            current = ""
            for word in raw.split(" "):
                candidate = (current + " " + word) if current else word
                if font.size(candidate)[0] <= maxw or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            # hard-break anything still too wide (URLs, figlet junk)
            while font.size(current)[0] > maxw and len(current) > 1:
                cut = max(1, int(len(current) * maxw / max(1, font.size(current)[0])))
                lines.append(current[:cut])
                current = current[cut:]
            lines.append(current)
        return lines

    # ------------------------------------------------------------------
    # Status jobs: word-synced caption reveal
    # ------------------------------------------------------------------

    def _update_status_jobs(self, now):
        """Advance the front job; returns True while anything is revealing."""
        changed = False
        while self.status_jobs:
            job = self.status_jobs[0]
            if not job.started:
                job.started = True
                job.t0 = now
                changed = True

            if job.kind == "caption":
                n = max(1, len(job.words))
                if job.caption:
                    elapsed = now - job.t0
                    target = min(n, int(elapsed / job.duration * n) + 1)
                else:
                    target = n
                if target > job.revealed:
                    maxw = self.win_w - 2 * self.margin
                    for word in job.words[job.revealed:target]:
                        self._reveal_word(job, word, maxw)
                    job.revealed = target
                    changed = True
                if job.revealed < n:
                    break  # still revealing; later jobs wait
                for line in job.built_lines:
                    self.status_history.append(("caption", line, job.color))
            else:
                for line in job.lines:
                    self.status_history.append((job.kind, line, job.color))
                changed = True

            self._trim_status()
            self.status_jobs.pop(0)

        if changed:
            self.status_dirty = True
        job = self.status_jobs[0] if self.status_jobs else None
        return bool(job and job.started and job.revealed < len(job.words))

    def _reveal_word(self, job, word, maxw):
        if not job.built_lines:
            job.built_lines = [word]
            return
        candidate = job.built_lines[-1] + " " + word
        if self.caption_font.size(candidate)[0] <= maxw:
            job.built_lines[-1] = candidate
        else:
            job.built_lines.append(word)

    # ------------------------------------------------------------------
    # Text rendering cache (crisp + glow pairs on opaque black)
    # ------------------------------------------------------------------

    def _text_surfaces(self, font, text, rgb):
        key = (id(font), text, rgb)
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached
        mask = font.render(text if text else " ", True, rgb)
        crisp = pygame.Surface(mask.get_size()).convert()
        crisp.fill((0, 0, 0))
        crisp.blit(mask, (0, 0))
        w, h = crisp.get_size()
        gw, gh = max(1, w // 3), max(1, h // 3)
        glow = pygame.transform.smoothscale(
            pygame.transform.smoothscale(crisp, (gw, gh)), (w, h))
        glow.fill((72, 72, 72), special_flags=pygame.BLEND_MULT)
        if len(self._text_cache) > 160:
            self._text_cache.pop(next(iter(self._text_cache)))
        self._text_cache[key] = (crisp, glow)
        return crisp, glow

    def _blit_text(self, target, font, text, rgb, pos):
        crisp, glow = self._text_surfaces(font, text, rgb)
        target.blit(glow, pos, special_flags=pygame.BLEND_ADD)
        target.blit(crisp, pos, special_flags=pygame.BLEND_ADD)
        return crisp.get_size()

    # ------------------------------------------------------------------
    # Face rendering
    # ------------------------------------------------------------------

    def _draw_eye(self, surf, x0, x1, eye, mouth_rgb, boot_gain):
        w = x1 - x0
        h = surf.get_height()
        cx = x0 + w * 0.5 + eye["gaze_x"] * w * 0.25
        cy = h * 0.375 - eye["gaze_y"] * h * 0.125
        rx = max(1.0, max(0.01, eye["scale_x"]) * w * 0.20)
        ry = max(1.0, max(0.01, eye["scale_y"]) * h * 0.20)
        lid_px = eye["lid_height"] * h * 0.25
        # Fill runs cooler than the outline so the additive bloom brings the
        # core back to true color instead of blowing out to white.
        color = scale_rgb(eye["color"], 0.55 * boot_gain)
        outline = scale_rgb(mouth_rgb, boot_gain)

        rect = pygame.Rect(int(cx - rx), int(cy - ry), int(rx * 2), int(ry * 2))
        pygame.draw.ellipse(surf, color, rect)
        width = int(clamp(2, 1, min(rx, ry)))
        pygame.draw.ellipse(surf, outline, rect, width)

        # Lid: erase everything above the (possibly tilted) lid line, then
        # draw the lid/brow stroke — same construction as face_hud.
        angle = math.radians(eye["lid_angle"])
        half = max((ry + rx + 10.0) / 2.0, rx, 10.0)
        lx1, lx2 = cx - half, cx + half
        ly1 = cy - half * math.sin(angle) - lid_px * math.cos(angle)
        ly2 = cy + half * math.sin(angle) - lid_px * math.cos(angle)
        pad = max(2, int(w * 0.05))
        ex1 = max(x0, int(lx1) - pad)
        ex2 = min(x1 - 1, int(lx2) + pad)
        pygame.draw.polygon(surf, (0, 0, 0),
                            [(ex1, int(ly1)), (ex2, int(ly2)), (ex2, 0), (ex1, 0)])
        lid_rgb = scale_rgb(tuple((a + b) // 2 for a, b in zip(eye["color"], mouth_rgb)),
                            0.85 * boot_gain)
        thick = max(1, int(round(h * 0.02)))
        pygame.draw.line(surf, lid_rgb, (int(lx1), int(ly1)), (int(lx2), int(ly2)), thick)

    def _draw_mouth(self, surf, mouth, now, boot_gain):
        w = surf.get_width()
        h = surf.get_height()
        baseline = h * 0.875
        amp_px = h * 0.125
        n = max(48, w // 2)
        xs = np.linspace(0, w - 1, n)

        # Audio trace (rainbow by |amplitude|), drawn under the sine.
        if self.audio is not None:
            elapsed = now - self.audio_t0
            if elapsed <= self.audio_duration and self.audio_sr > 0:
                needed = n
                passed = int(elapsed * self.audio_sr)
                start = max(0, passed - needed)
                end = min(len(self.audio), start + needed)
                window = np.zeros(needed, dtype=np.float32)
                if end > start:
                    window[: end - start] = self.audio[start:end]
                lo, hi = float(window.min()), float(window.max())
                if hi - lo > 1e-9:
                    window = 2.0 * (window - lo) / (hi - lo) - 1.0
                else:
                    window[:] = 0.0
                ys = baseline + window * amp_px
                thick = max(1, int(round(h / 130.0)))
                for i in range(1, n):
                    yy = ys[i] if abs(ys[i] - baseline) > abs(ys[i - 1] - baseline) else ys[i - 1]
                    t = clamp(abs(yy - baseline) / max(amp_px, 1e-6), 0.0, 1.0)
                    rgb = scale_rgb(self.amplitude_lut[int(t * 255)], boot_gain)
                    pygame.draw.line(surf, rgb, (int(xs[i - 1]), int(ys[i - 1])),
                                     (int(xs[i]), int(ys[i])), thick)
            else:
                self.audio = None

        # Idle sine (mouth color), always on top — face_hud does the same.
        t = np.linspace(0.0, 2.0 * math.pi, n)
        ys = baseline + np.sin(mouth["frequency"] * t + mouth["phase"]) * \
            mouth["amplitude"] * amp_px
        rgb = scale_rgb(mouth["color"], boot_gain)
        pts = list(zip(xs.astype(int), ys.astype(int)))
        thick = max(1, int(round(4 * self.render_scale * self.win_w / 800.0)))
        pygame.draw.lines(surf, rgb, False, pts, thick)

    def _draw_boot(self, surf, now, gain=1.0):
        """CRT warm-up: dot -> horizontal sweep -> fade as the face wakes."""
        p = (now - self.boot_t0) / self.boot_seconds
        w, h = surf.get_width(), surf.get_height()
        cx, cy = w // 2, int(h * 0.5)
        white = scale_rgb((200, 255, 220), gain)
        if p < 0.2:
            r = 1 + int(6 * (p / 0.2))
            pygame.draw.circle(surf, white, (cx, cy), r)
        elif p < 0.65:
            q = (p - 0.2) / 0.45
            half = int(q * w / 2)
            pygame.draw.line(surf, white, (cx - half, cy), (cx + half, cy), 3)
            pygame.draw.circle(surf, white, (cx, cy), 4)
        else:
            q = (1.0 - (p - 0.65) / 0.35) * gain
            g = scale_rgb((200, 255, 220), q)
            pygame.draw.line(surf, g, (0, cy), (w, cy), 2)

    def _boot_gain(self, now):
        p = (now - self.boot_t0) / self.boot_seconds
        if p >= 1.0:
            return 1.0
        if p < 0.65:
            return 0.0
        return (p - 0.65) / 0.35

    def _layer_image_alpha(self, img, now):
        if not img.active or img.rgb is None:
            return 0.0
        elapsed = now - img.t0
        total = self.fade_in + self.hold + self.fade_out
        if elapsed > total:
            img.active = False
            img.rgb = None
            img.surface = None
            img.scaled = None
            return 0.0
        if self.fade_in > 0 and elapsed < self.fade_in:
            return self.image_max_alpha * (elapsed / self.fade_in)
        if elapsed < self.fade_in + self.hold or self.fade_out <= 0:
            return self.image_max_alpha
        return self.image_max_alpha * (1.0 - (elapsed - self.fade_in - self.hold) / self.fade_out)

    def _blit_layer_image(self, pane, idx, now):
        img = self.layer_images[idx]
        alpha = self._layer_image_alpha(img, now)
        if alpha <= 0.0:
            return
        if img.surface is None and img.rgb is not None:
            arr = np.ascontiguousarray(img.rgb)
            img.surface = pygame.image.frombuffer(
                arr.tobytes(), (arr.shape[1], arr.shape[0]), "RGB").convert()
        if img.surface is None:
            return
        if img.scaled is None or img.scaled.get_size() != pane.get_size():
            img.scaled = pygame.transform.smoothscale(img.surface, pane.get_size())
        img.scaled.set_alpha(int(alpha * 255))
        pane.blit(img.scaled, (0, 0))

    def _draw_face_lines(self, pane, idx):
        lines = self.face_lines[idx]
        if not lines:
            return
        now = time.monotonic()
        keep = [ln for ln in lines if ln[2] == 0.0 or ln[2] > now]
        if len(keep) != len(lines):
            self.face_lines[idx] = keep
        lh = self.face_font.get_linesize()
        max_visible = max(1, pane.get_height() // lh)
        visible = keep[-max_visible:]
        y = pane.get_height() - lh * len(visible)
        for text, rgb, _ in visible:
            if text:
                self._blit_text(pane, self.face_font, text, rgb, (self.margin, y))
            y += lh

    def _draw_crawl(self, pane, idx, now):
        crawl = self.crawls[idx]
        if not crawl.active:
            return
        if crawl.duration > 0.0 and now - crawl.t0 > crawl.duration:
            crawl.active = False
            return
        if crawl.surface is None:
            lines = crawl.text.split("\n")
            lh = self.face_font.get_linesize()
            wmax = max(1, max(self.face_font.size(ln)[0] for ln in lines))
            block = pygame.Surface((wmax, lh * len(lines))).convert()
            block.fill((0, 0, 0))
            for i, ln in enumerate(lines):
                if ln:
                    crisp, _ = self._text_surfaces(self.face_font, ln, crawl.color)
                    block.blit(crisp, (0, i * lh), special_flags=pygame.BLEND_ADD)
            crawl.surface = block

        w, h = pane.get_width(), pane.get_height()
        bw, bh = crawl.surface.get_size()
        char_w = max(6, self.face_font.size("M")[0])
        elapsed = now - crawl.t0
        base_x = (w - 1) * crawl.location_x / 1000.0 + elapsed * crawl.speed * char_w * crawl.direction[0]
        base_y = (h - 1) * crawl.location_y / 1000.0 + elapsed * crawl.speed * char_w * crawl.direction[1]
        gap = max(char_w, int((1.0 - crawl.density) * min(w, h)))
        period_x = bw + gap
        period_y = bh + gap

        if crawl.tile_x:
            first_x = (base_x % period_x) - period_x
            xs = np.arange(first_x, w + period_x, period_x)
        else:
            xs = [base_x]
        if crawl.tile_y:
            first_y = (base_y % period_y) - period_y
            ys = np.arange(first_y, h + period_y, period_y)
        else:
            ys = [base_y]
        for ty in ys:
            for tx in xs:
                pane.blit(crawl.surface, (int(tx), int(ty)), special_flags=pygame.BLEND_ADD)

    def render_face(self, now, dt):
        with self.lock:
            left = self.eyes["left"].current(now)
            right = self.eyes["right"].current(now)
            mouth = self.mouth.current(now)
            if not self.mouth.phase_anim.active:
                self.mouth.phase += mouth["phase_increment"] * dt * self.phase_rate
                self.mouth.phase = math.fmod(self.mouth.phase, 2.0 * math.pi * 1e6)
            booting = (now - self.boot_t0) < self.boot_seconds

        # --- strokes (low-res, additive) ---------------------------------
        # Drawn twice: full brightness for the live frame, dimmed for the
        # persistence buffer. Two draw passes beat a copy + BLEND_MULT fill
        # (~3 ms) by an order of magnitude at idle.
        K = self.strokes
        K.fill((0, 0, 0))
        K2 = self.strokes_dim
        K2.fill((0, 0, 0))
        gain = self._boot_gain(now) if booting else 1.0
        for surf, g in ((K, gain), (K2, gain * self.trail_gain)):
            if g > 0.0:
                half = surf.get_width() // 2
                self._draw_eye(surf, 0, half, left, mouth["color"], g)
                self._draw_eye(surf, half, surf.get_width(), right, mouth["color"], g)
                self._draw_mouth(surf, mouth, now, g)
        if booting:
            self._draw_boot(K, now, 1.0)
            self._draw_boot(K2, now, self.trail_gain)

        # --- phosphor persistence + bloom --------------------------------
        # Decay = alpha-blit onto black (SIMD path; BLEND_MULT fill is slow).
        decay = int(clamp(255.0 * math.exp(-dt / self.trail_tau), 0, 250))
        self.trail_scratch.fill((0, 0, 0))
        self.trail.set_alpha(decay)
        self.trail_scratch.blit(self.trail, (0, 0))
        self.trail.set_alpha(None)
        self.trail, self.trail_scratch = self.trail_scratch, self.trail
        self.trail.blit(K2, (0, 0), special_flags=pygame.BLEND_ADD)

        lw, lh = self.lw, self.lh
        combined = self.trail.copy()
        combined.blit(K, (0, 0), special_flags=pygame.BLEND_ADD)
        if self.bloom_strength > 0.0:
            small = pygame.transform.smoothscale(combined, (max(1, lw // 6), max(1, lh // 6)))
            g = int(255 * self.bloom_strength)
            small.fill((g, g, g), special_flags=pygame.BLEND_MULT)  # tiny -> cheap
            halo = pygame.transform.smoothscale(small, (lw, lh))
            combined.blit(halo, (0, 0), special_flags=pygame.BLEND_ADD)

        # --- compose the pane at full res ---------------------------------
        pane = self.face_pane
        pane.blit(self.face_bg, (0, 0))
        self._blit_layer_image(pane, 0, now)
        self._draw_crawl(pane, 0, now)
        self._draw_face_lines(pane, 0)
        up = pygame.transform.smoothscale(combined, (self.face_w, self.face_h))
        pane.blit(up, (0, 0), special_flags=pygame.BLEND_ADD)
        self._draw_crawl(pane, 1, now)
        self._draw_face_lines(pane, 1)
        self._blit_layer_image(pane, 1, now)
        self.window.blit(pane, (0, 0))
        return left, right, mouth

    # ------------------------------------------------------------------
    # Status pane
    # ------------------------------------------------------------------

    def render_status(self, now):
        with self.lock:
            revealing = self._update_status_jobs(now)
            blink = (now - self._last_blink) > 0.45
            if blink:
                self._cursor_on = not self._cursor_on
                self._last_blink = now
            if not (self.status_dirty or blink):
                self.window.blit(self._status_surface, (0, self.face_h))
                return revealing

            entries = list(self.status_history)
            job = self.status_jobs[0] if self.status_jobs else None
            if job is not None and job.kind == "caption" and job.started:
                entries += [("caption", ln, job.color) for ln in job.built_lines]
                cursor_rgb = job.color
            else:
                cursor_rgb = scale_rgb(self.face_default_rgb, 0.8)
            self.status_dirty = False

        surf = self._status_surface
        surf.blit(self.status_bg, (0, 0))

        # lay out bottom-up: measure heights, then draw what fits
        line_h = {
            "caption": self.caption_font.get_linesize(),
            "status": self.status_font.get_linesize(),
            "figlet": self.status_font.get_linesize(),
        }
        pad = int(self.margin * 0.5)
        budget = self.status_h - 2 * pad
        chosen = []
        for entry in reversed(entries):
            h = line_h[entry[0]]
            if budget - h < 0:
                break
            budget -= h
            chosen.append(entry)
        chosen.reverse()

        y = self.status_h - pad - sum(line_h[k] for k, _, _ in chosen)
        end_x, end_y = self.margin, y
        last_kind = "status"
        for kind, text, rgb in chosen:
            font = self.caption_font if kind == "caption" else self.status_font
            if text:
                w, _ = self._blit_text(surf, font, text, rgb, (self.margin, y))
            else:
                w = 0
            end_x, end_y, last_kind = self.margin + w, y, kind
            y += line_h[kind]

        if self._cursor_on:
            font = self.caption_font if last_kind == "caption" else self.status_font
            ch_w = max(4, font.size("M")[0] - 2)
            ch_h = font.get_linesize()
            cursor = pygame.Rect(end_x + 4, end_y + int(ch_h * 0.15), ch_w, int(ch_h * 0.75))
            if cursor.right < self.win_w:
                pygame.draw.rect(surf, scale_rgb(cursor_rgb, 0.85), cursor)

        self.window.blit(surf, (0, self.face_h))
        return revealing

    # ------------------------------------------------------------------
    # Live state
    # ------------------------------------------------------------------

    def publish_live_state(self, left, right, mouth, frame_duration):
        def eye_dict(e):
            return {
                "gaze_x": round(e["gaze_x"], 4), "gaze_y": round(e["gaze_y"], 4),
                "scale_x": round(e["scale_x"], 4), "scale_y": round(e["scale_y"], 4),
                "lid_height": round(e["lid_height"], 4),
                "lid_angle": round(e["lid_angle"], 4),
                "color": rgb_to_hex(e["color"]),
            }
        payload = {
            "timestamp": round(rospy.get_time(), 4),
            "left_eye": eye_dict(left),
            "right_eye": eye_dict(right),
            "mouth": {
                "frequency": round(mouth["frequency"], 4),
                "amplitude": round(mouth["amplitude"], 4),
                "phase": round(mouth["phase"], 4),
                "phase_increment": round(mouth["phase_increment"], 4),
                "color": rgb_to_hex(mouth["color"]),
            },
            "duration": round(frame_duration, 4),
        }
        self.pub_live.publish(String(data=json.dumps(payload)))

    # ------------------------------------------------------------------
    # Activity / main loop
    # ------------------------------------------------------------------

    def _is_active(self, now, revealing):
        if now - self.boot_t0 < self.boot_seconds + self.trail_tau * 4:
            return True
        if now - self.last_activity < 0.6:
            return True
        if revealing or self.status_jobs:
            return True
        if self.audio is not None:
            return True
        if any(c.active for c in self.crawls):
            return True
        if any(i.active for i in self.layer_images):
            return True
        if self.mouth.any_active():
            return True
        for eye in self.eyes.values():
            if eye.color_anim.active or any(a.active for a in eye.anims.values()):
                return True
        return False

    def _handle_keys(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type != pygame.KEYDOWN:
                continue
            key = event.key
            if key in (pygame.K_q, pygame.K_ESCAPE):
                return False
            if key == pygame.K_RIGHTBRACKET:
                self.fps_max = int(clamp(self.fps_max + 2, 4, 60))
            elif key == pygame.K_LEFTBRACKET:
                self.fps_max = int(clamp(self.fps_max - 2, 4, 60))
            elif key in (pygame.K_PLUS, pygame.K_EQUALS):
                self.status_ratio = clamp(self.status_ratio - 0.01, 0.05, 0.95)
                self.rebuild_geometry()
            elif key == pygame.K_MINUS:
                self.status_ratio = clamp(self.status_ratio + 0.01, 0.05, 0.95)
                self.rebuild_geometry()
            elif key == pygame.K_BACKSLASH:
                with self.lock:
                    self._clear_pane("all", None)
            elif key == pygame.K_f:
                self.windowed = not self.windowed
                flags = 0 if self.windowed else pygame.NOFRAME
                self.window = pygame.display.set_mode((self.win_w, self.win_h), flags)
                self.rebuild_geometry()
            with self.lock:
                self._touch()
        return True

    def _maybe_dump_frame(self, now):
        if not self.frame_dump_dir:
            return
        if now - self._last_dump < self.frame_dump_period:
            return
        self._last_dump = now
        try:
            os.makedirs(self.frame_dump_dir, exist_ok=True)
            pygame.image.save(self.window,
                              os.path.join(self.frame_dump_dir, "phosphor_%.1f.png" % now))
        except Exception as exc:
            rospy.logwarn_throttle(30, "phosphor: frame dump failed: %s" % exc)

    def run(self):
        clock = pygame.time.Clock()
        prev = time.monotonic()
        while not rospy.is_shutdown():
            if not self._handle_keys():
                break
            now = time.monotonic()
            dt = clamp(now - prev, 1e-3, 0.25)
            prev = now

            left, right, mouth = self.render_face(now, dt)
            revealing = self.render_status(now)
            pygame.display.flip()
            self._maybe_dump_frame(now)

            with self.lock:
                active = self._is_active(now, revealing)
            target = self.fps_max if active else min(self.fps_idle, self.fps_max)
            self.publish_live_state(left, right, mouth, 1.0 / target)
            clock.tick(target)

        pygame.quit()


def main():
    node = PhosphorFace()
    try:
        node.run()
    except KeyboardInterrupt:
        pass
    finally:
        rospy.signal_shutdown("phosphor face exit")


if __name__ == "__main__":
    main()
