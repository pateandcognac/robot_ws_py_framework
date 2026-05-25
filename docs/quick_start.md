# ==============================================================================
# LOGOS CORE API & CAPABILITY MATRIX
# My personal, comprehensive reference for how to pilot my body and mind.
#
# DESIGN PHILOSOPHY:
# 1. Everything is available JIT (Just-In-Time) in my <py> loops. No imports needed for `logos.*`.
# 2. Actions with side-effects have a implicit, silent `verbosity` kwarg (Verbosity.SILENT, ACK, BRIEF, DEBUG).
# 3. Asynchronous primitives (SpeakTask, NavTask) are meant to be composed and synchronized!
# 2. All pixel space detections are normalized 0-1000 [y, x] or [y_min, x_min, y_max, x_max], regardless of model.
# ==============================================================================

# ==============================================================================
# 🧩 MY CORE COMPOSABLE PRIMITIVES
# These objects are my superpowers for multitasking. I save them to variables and 
# interact with them in loops to weave perception, movement, and performance.
# ==============================================================================

class CaptureResult:
    """My atomic unit of visual perception. Contains image data, depth, and spatial math."""
    image: np.ndarray          # BGR uint8 image matrix.
    source: str                # 'pan_tilt' | 'top_down' | 'astra'
    timestamp: float           # time.time() at capture.
    resolution: Tuple[int,int] # (height, width)
    pose: Optional[dict]       # My TF pose {'x', 'y', 'theta_deg'} at capture time.
    pan_tilt_degs: tuple       # (pan, tilt) if source == 'pan_tilt'
    depth: Optional[np.ndarray] # 16-bit depth (mm) (Astra only)
    depth_points: Optional[np.ndarray] # (H,W,3) float32 XYZ in camera optical frame (Astra only)
    tf_to_map: Optional[TransformStamped] # frozen transform from the depth point frame into map at capture time.
    
    def crop(self, box_2d: List[float]) -> 'CaptureResult':
        # Isolates a 0-1000 normalized region. Drops depth/intrinsics. Great for zoom-ins!
        ...
    def derive_world_coordinate(self, *args, search_radius: int=3) -> Optional[Tuple[float, float, float]]:
        # MAGIC! Pass a 0-1000 [y, x] or [y1, x1, y2, x2] box from an Astra capture, 
        # and it returns absolute (X, Y, Z) in the ROS map frame!
        ...
    def overlay_coordinate_grid(self, rows: int=3, cols: int=4) -> None:
        # Burns a 3D coordinate grid directly into the image pixels for me to read natively!
        ...
    def add_meta(self, **kwargs) -> None:
        # Saves arbitrary dict data to the image's YAML sidecar (e.g., caption="...", detections=[...])
        ...
    def save(self, view: bool=False, meta_keys: List[str]=None) -> str:
        # Saves to IPC. If view=True, prints <file> tag to my stdout so I see it next loop!
        ...
    def view(self, meta_keys: List[str]=None) -> None: ...

class SpeakTask:
    """Handle for my async Text-to-Performance engine. My brain speaks faster than my mouth."""
    def is_active(self) -> bool: ... # True if audio/animatronics are running
    def wait(self) -> bool: ...      # Blocks loop until finished. Yields to cooperative interrupts.
    def progress(self) -> float: ... # 0.0 to 1.0 audio progress.
    def current_emoji(self) -> str: ... # The EXACT emoji driving my face/arms right now.
    def current_text(self) -> str: ...  # The text snippet being spoken right now.

class NavTask:
    """Handle for ROS map/odometry async movement. I use this to walk and chew gum at the same time."""
    def is_active(self) -> bool: ...
    def wait(self, timeout: float=120.0) -> bool: ...
    def progress(self) -> float: ... # Euclidean distance mapped 0.0 to 1.0 (can dip below/above!)
    def status(self) -> str: ...     # 'PENDING', 'ACTIVE', 'SUCCEEDED', 'ABORTED', etc.
    def succeeded(self) -> bool: ...
    def cancel(self) -> None: ... 

class SoundTask:
    """Handle for my async audio playback. I use this to track, wait for, or cancel active sound."""
    def is_active(self) -> bool: ... # True if sound is currently playing from my speakers.
    def wait(self) -> None: ...      # Blocks loop until finished. Yields to cooperative interrupts.
    def progress(self) -> float: ... # 0.0 to 1.0 estimated playback progress.
    def cancel(self) -> None: ...    # Aborts current sound playback immediately.



# ==============================================================================
# 👁️ VISION, PERCEPTION, & MODELS (`logos.vision`, `logos.models`)
# ==============================================================================

# --- VISION CAPTURE ---
def logos.vision.capture(
    source: str = 'pan_tilt', # 'pan_tilt' | 'top_down' | 'astra'
    resolution: Tuple[int, int] = None, 
    view: bool = False,       # True = push <file> tag to my context window instantly!
    save: bool = False, 
    astra_feeds: Tuple[str] = ('rgb', 'depth_registered'), 
    meta: Dict = None         # Inject kwargs straight to sidecar on capture
) -> Optional[CaptureResult]: ...

def publish_debug(image: Union[np.ndarray, CaptureResult], detections: Optional[Union[List[Dict[str, Any]], Dict[str, Any], Tuple[Any, ...]]] = None, source: Optional[str] = None,) -> None:
    # Overlays bounding boxes/points/hands/labels and publishes to ROS /logos/debug_vision for Mark to see
    ...

# --- MODELS (Lazy-loaded local singletons) ---
def logos.models.yolo11(image: Union[np.ndarray, CaptureResult], classes: List[str]=None, conf: float=0.5) -> Union[List[Dict], Tuple[List[Dict], CaptureResult]]:
    # Blazing fast. 80 COCO classes. Perfect for high-frequency tracking loops.
    # Returns: [{"label": "person", "box_2d": [y1, x1, y2, x2], "confidence": 0.88}, ...]
    # If passed CaptureResult: returns (detections, result) and writes result.meta["det_yolo11"].
    ...

def logos.models.yolo_world(image: Union[np.ndarray, CaptureResult], prompts: List[str], conf: float=0.1) -> Union[List[Dict], Tuple[List[Dict], CaptureResult]]:
    # Zero-shot open vocabulary (8000+ concepts/attributes). "red cup", "open door".
    # If passed CaptureResult: returns (detections, result) and writes result.meta["det_yolo_world"].
    ...

def logos.models.yoloe(image: Union[np.ndarray, CaptureResult], prompts: List[str]=None, conf: float=None) -> Union[List[Dict], Tuple[List[Dict], CaptureResult]]:
    # The Semantic Wide-Net. 
    # If prompts=None: Prompt-free discovery ("What's in this room?"). Conf defaults to 0.20.
    # If prompts=["item"]: Text-prompted focus. Conf defaults to 0.10.
    # If passed CaptureResult: returns (detections, result) and writes "det_yoloe_pf" or "det_yoloe_text".
    ...

def logos.models.hands(image: Union[np.ndarray, CaptureResult], max_hands: int=2) -> Union[List[Dict], Tuple[List[Dict], CaptureResult]]:
    # Lightning fast Mediapipe. "gesture": "pointing_up|pointing_down|pointing_left|pointing_right|hand_up|hand_down|thumbs_up|thumbs_down|peace|ok|open_palm|closed_fist|unknown"
    # Includes 21-point "landmarks" and "center_2d" in 0-1000 space.
    # If passed CaptureResult: returns (detections, result) and writes result.meta["det_hands"].
    ...

def logos.models.llm(prompt: str, model_alias: str='fast', temperature: float=1.0) -> str:
    # Out-of-band stateless call to my own intelligence!
    # WARNING: It has NO system prompt, NO memory, NO tools, NO vision. Pure text-in, text-out.
    ...


# ==============================================================================
# 🗺️ SPATIAL REASONING & THE CHORA (`logos.map3d`, `logos.phantasmata`)
# My virtual mind-palace for translating 3D map math into visual 2D intuition.
# ==============================================================================

# --- MAP3D RENDERING ---
def logos.map3d.render(
    camera_pos_relative: Tuple[float,float,float] = None, # [X,Y,Z] offset from base
    look_at_world: Tuple[float,float,float] = None,       # [X,Y,Z] abs target to look at
    view: bool = None, hud: List['HudElement'] = None, ...
) -> 'RenderResult':
    # Renders a 3D view of me, my Astra point cloud, map floor, and phantasmata.
    # Result contains `.image` and `.meta` (SceneSnapshot) required for raycasting.
    ...

def logos.map3d.raycast(render: Union['RenderResult', str], yx: Tuple[float, float]) -> 'RaycastHit':
    # THE CRUCIAL BRIDGE: Pass a RenderResult and a 2D pixel [y,x].
    # Returns a RaycastHit with `.point` -> Absolute [X, Y, Z] map coordinates!
    # `.hit` tells me what I struck ("astra_cloud", "floor", "robot", "object:name").
    ...

# --- PHANTASMATA (Virtual Objects) ---
def logos.map3d.place(name: str, object: str, pose: Dict=None, params: Dict=None, ...) -> None:
    # Spawns a virtual object into my mind palace!
    # Available objects (from `logos.map3d.list_phantasmata()`):
    # - 'grid_overlay': Draws metric floor grid.
    # - 'pointer_arrow': 3D arrow to point at targets (params: 'from_point', 'to_point', 'color')

    ...
def logos.map3d.move_instance(name: str, position: List[float]=None, rpy_deg: List[float]=None): ...
def logos.map3d.remove(name: str): ...


# ==============================================================================
# 🐢 NAVIGATION & MOVEMENT (`logos.base`, `logos.nav`, `logos.pantilt`)
# ==============================================================================

# --- RELATIVE/BLIND NAVIGATION (Odometry/Velocity, ignores map!) ---
def logos.nav.turn_then_drive(turn_deg: float, forward_m: float, wait: bool=False) -> NavTask:
    # Uses odom. Turn in place, stop, then drive straight. Good for tight spots.
    ...
def logos.base.velocity(linear_x: float, angular_z_deg: float, topic: str='raw') -> None:
    # Async velocity loop command. Times out after ~0.6s. Must be spammed in a loop to keep moving.
    ...
def logos.base.move_timed(linear_x: float, angular_z_deg: float, duration: float) -> None:
    # Blocking blind drive (like backing up).
    ...

# --- ABSOLUTE NAVIGATION (Map-based, obstacle avoiding) ---
def logos.nav.go_to_abs(x: float, y: float, deg: float=None, wait: bool=False) -> NavTask:
    # Use this to move through rooms safely.
    ...
def logos.nav.approach_astra_detection(target: Union[List, Dict], astra_result: CaptureResult, standoff: float=1.0, wait: bool=True) -> NavTask:
    # Directly approach something I just saw in an Astra image, stopping `standoff` meters away!
    ...
def logos.nav.approach_coordinate(x: float, y: float, standoff: float, wait: bool=False) -> NavTask: ...
def logos.nav.move_relative(forward_m: float=0.0, left_m: float=0.0, turn_deg: float=0.0, wait: bool=False) -> NavTask:
    # Calculates a global map point based on relative inputs, then routes me there safely.
    ...

# --- GAZE (`logos.pantilt`) ---
# Pan: +100 (L) to -80 (R) | Tilt: -60 (D) to +70 (U) | Home: (0, 0)
def logos.pantilt.move(pan_deg: float, tilt_deg: float, duration: float=0.25) -> Tuple[float, float]: ...
def logos.pantilt.nudge(pan_deg: float, tilt_deg: float) -> Tuple[float, float]: ...
def logos.pantilt.get_angles() -> Tuple[float, float]: ...


# ==============================================================================
# 💥 BUMPER EVENTS (`logos.bumper`)
# My event-driven collision system. I compose handlers like LEGO blocks —
# each one is called in order the moment contact is first detected.
# Handlers run in a background thread so they can block freely (camera,
# movement, speech). New bumps are ignored while a chain is already running.
# ==============================================================================

# --- HANDLER MANAGEMENT ---
logos.bumper.set_default()   # installs [do_print, do_backup] — safe baseline
logos.bumper.register(handler, index=None)  # append or insert; idempotent
logos.bumper.unregister(handler)            # remove one handler
logos.bumper.clear()                        # wipe the whole chain
logos.bumper.show()                         # print chain in execution order

# --- ATOMIC BEHAVIORS ---
logos.bumper.do_print(bumpers)              # logs which sides fired, no side effects
logos.bumper.do_stop(bumpers)              # emergency halt via safety mux
logos.bumper.do_backup(bumpers, distance=0.15, speed=0.1)
# Per-bumper steering: left→rotates right, right→rotates left, center→straight back.
# Publishes to safety mux slot so it works even during paused navigation.

# --- COMPOSED BEHAVIORS ---
logos.bumper.look_and_identify(bumpers)
# Saves current gaze, tilts toward bumped side at -45°, captures + runs yoloe(),
# restores prior gaze, then speaks the result non-blocking.
# Returns the top detected label string, or None.

# --- RECIPES ---

# Minimal safe default — just back up:
logos.bumper.set_default()

# Full perceptual response — back up, then look and classify:

# Custom backup distance via functools.partial:
import functools
logos.bumper.register(functools.partial(logos.bumper.do_backup, distance=0.3, speed=0.08))

# Ad-hoc one-liner handler:
logos.bumper.register(lambda b: logos.emote.ttp("ouch, my {} bumper! 😣".format(b[0])))

# Navigation-safe: stop hard, don't back up:
logos.bumper.clear()
logos.bumper.register(logos.bumper.do_print)
logos.bumper.register(logos.bumper.do_stop)

# Simulate a bump event without hardware (for testing):
logos.bumper._run_handlers(['center'])


# ==============================================================================
# 👂 HOTWORD LISTENER (`logos.sensory.hotwords`)
# Direct Python↔human audio interactivity — bypasses the STT→cognition pipeline.
# When my Python loop needs a human to say a specific word to branch or stop a
# behavior, I arm models here and poll or register callbacks, exactly like bumper.
# Backend debounces detections at 1.5s, so I don't need to.
# ==============================================================================

# Model names are subdirectory names under ~/robot_ws/wakewords/custom/
# Some useful hotwords to get started:
# lets_start, turn_left, turn_right, go_forward, move_back, up, down
# cancel_that, halt_now, stop, nevermind, good_bye, thank_you, orderly_stop
# hey_potato, rubber_duck, yo_homie, ok_boss, computer, terminator

# --- CONTROL ---
logos.sensory.hotwords.enable(['stop', 'halt_now'])  # arm models; [] disables + unloads
logos.sensory.hotwords.enable([])

# --- CONTEXT MANAGER (auto-cleanup, recommended for loops) ---
with logos.sensory.hotwords.listening(['stop', 'cancel_that']):
    while True:
        check_for_interrupt()
        do_thing()
        if logos.sensory.hotwords.detected():
            word = logos.sensory.hotwords.consume()
            break
# hotwords auto-disabled on exit, even if loop raises

# --- POLLING ---
logos.sensory.hotwords.detected()       # True if a detection is waiting
logos.sensory.hotwords.consume()        # -> str or None; clears the buffer (use in loops)
logos.sensory.hotwords.latest()         # -> str or None; peek without clearing

# --- HANDLER CHAIN (callback style, mirrors bumper) ---
logos.sensory.hotwords.register(handler)    # handler(word: str) -> None; idempotent append
logos.sensory.hotwords.register(handler, index=0)
logos.sensory.hotwords.unregister(handler)
logos.sensory.hotwords.clear_handlers()
logos.sensory.hotwords.show()

# --- ATOMIC HANDLERS ---
logos.sensory.hotwords.do_print(word)   # logs detected word, no side effects

# --- RECIPES ---

# Voice-gated follower loop:
logos.sensory.hotwords.enable(['stop', 'halt_now'])
logos.emote.ttp("I'll follow you. Say stop or halt now when done! 🚶")
while True:
    check_for_interrupt()
    skills.tracking.track_step(drive=True)
    if logos.sensory.hotwords.detected():
        logos.emote.ttp("Stopping! I heard '{}'. 🛑".format(logos.sensory.hotwords.consume()), wait=True)
        break
logos.sensory.hotwords.enable([])

# Voice teleop with callback:
import functools
logos.sensory.hotwords.register(logos.sensory.hotwords.do_print)
logos.sensory.hotwords.register(lambda w: logos.base.stop() if 'stop' in w else None)
logos.sensory.hotwords.enable(['go_forward', 'turn_left', 'turn_right', 'stop'])

# ==============================================================================
# 🎭 COMMUNICATION, PERFORMANCE, & SOUND (`logos.emote`, `logos.leds`, `logos.sound`)
# ==============================================================================

def logos.emote.ttp(text: str, wait: bool=False, engine: str=None) -> SpeakTask:
    # TEXT-TO-PERFORMANCE! Punctuate every spoken clause with emojis. 
    # Emojis trigger physical animatronics synchronously.
    # Example: "Hello there! 👋 Let's explore. 🧭"
    # I have thousands of emoji to choose from, yet...
    # I should avoid compound emojis with skin-tone or gender modifiers.
    ...
def logos.emote.gesture(emoji: str, duration: float=4.0, channel: str='both|arms|face') -> None:
    # Perform animatronics silently without speaking. 
    ...
def logos.emote.get_face_state() -> Dict:
    # Returns live 4-16Hz-ish state of my animated ASCII face as triggered by
    # my emojis. This can be a rich source of data for interesting feedback
    # loops! For example, I can read `gaze_x` to rotate my physical `logos.base` to track 
    # where my "eyes" are wandering, or I can read the eye `color` and pass it 
    # directly to `logos.leds.fill()` to match my ambient lighting to my mood.
    # Shape of returned data — all ranges are floats:
        {
            "timestamp": float,
            "duration": ~0.06s active to ~0.25s idle,
            "left_eye": {
                "gaze_x": -1 to 1, "gaze_y": -1 to 1, "scale_x": 0 to 1, "scale_y": 0 to 1, 
                "lid_height": -1 to 1, "lid_angle": -45 to 45, "color": 24 bit hex string "#RRGGBB"
            },
            "right_eye": { ... same as left_eye ... },
            "mouth": {
                "frequency": 0 to 16, "amplitude": 0 to 1, "phase": -pi to pi,
                "phase_increment": -pi to pi, "color": #RRGGBB
            }
        }        
    ...

def logos.leds.fill(color: Union[str, int, Tuple], strip: str='notification') -> None:
    # strip can be 'notification' (diffuse chest mounted "heart light") or 'pan_tilt' (unfiltered LEDs for illumination).
    Accepts:
    - int:   0xFFFF00 (yellow), 0x000000 (off)
    - tuple: (255, 0, 0) for red
    - str:   CSS color names, plus 'off'.
             And '#FF0000' hex strings (like from `get_face_state()`)
    ...
def logos.leds.set(colors: List=(), strip: str='notification') -> None:
    # Pass a list of colors for individual pixel control.
    # 'notification' has 16 LEDs. 'pan_tilt' has 5.
    ...
def logos.leds.laser(brightness: float) -> None: # 0.0 to 1.0.
    # PEW-PEW! Turns on my (harmless) pan-tilt laser pointer! Has a convenient automatic timeout.
    ...

# --- SOUND SYNTHESIS & PLAYBACK (logos.sound) ---
def logos.sound.chime(name: str, volume: float=None, wait: bool=True) -> SoundTask:
    # Play premium prebuilt algorithmic chimes (zero asset files!).
    # Names: 'startup', 'success', 'warning', 'error', 'scan', 'alert', 'thinking', 'click'
    ...
def logos.sound.beep(frequency: float=440.0, duration: float=0.5, waveform: str='sine', volume: float=None, wait: bool=True) -> SoundTask:
    # Play a single pitch tone. Waveforms: 'sine', 'square', 'triangle', 'sawtooth', 'noise'.
    ...
def logos.sound.play_melody(melody: Union[str, List], tempo: float=120, waveform: str='sine', volume: float=None, wait: bool=True) -> SoundTask:
    # Play gapless sequences of scientific note notation (e.g. "C4:1 E4:1 G4:1 C5:2 R:1").
    # Rest note is 'R' or 'REST'. Beats are mapped to duration based on tempo (BPM).
    ...
def logos.sound.play_waveform(waveform_data: np.ndarray, sample_rate: int=None, volume: float=None, wait: bool=True) -> SoundTask:
    # Low-level entrypoint to play raw 1D float32 numpy arrays (-1.0 to 1.0) directly.
    ...
def logos.sound.note_to_freq(note_name: str) -> float:
    # Utility to parse scientific pitch notation string (e.g., 'A4' -> 440.0, 'C#5', 'Eb3') to Hz.
    ...


# ==============================================================================
# 🧠 MEMORY, FILES, & SYSTEM (`logos.memory`, `logos.files`)
# ==============================================================================

# --- SEMANTIC / VECTOR MEMORY (`logos.memory.indexing|rag|client`) ---
def indexing.refresh_all_reference_indexes() 
    # Builds RAG technical reference from source code.

def rag.semantic_help(query: str, include_examples: bool=True) -> Dict:
    # Self-help! Queries my API docs & few-shot examples. 
    # print(rag.semantic_help("How do I X?")["context"])`
    ...
def rag.search_summaries(query: str) -> Dict:
    # Searches my indexed past experiences (summaries.jsonl). Returns relative time stamps.
    ...
def logos.memory.upsert_collective_fact(text: str, tags: List[str]=None, mem_id: str=None) -> str:
    # Stores durable, non-technical memories and facts across all workspaces in `logoi_collective`.
    ...
def logos.memory.recall_collective_facts(query: str) -> Dict: ...

# --- FILESYSTEM ---
# I prefer specific, explicit edits over large regex guesswork to prevent amnesia/corruption.
def logos.files.read(path: str) -> str: ...
def logos.files.show(path: str, max_chars: int=32768, pattern: str) -> str: ... # Safely prints files or view images, optionally regex filtering
def logos.files.tree(start_path: str='.', max_depth: int=5, inline_meta_masks: List[str]=None) -> str: ...
def logos.files.append(path: str, content: str) -> None: ...
def logos.files.replace_exact(path: str, old: str, new: str, expected_count: int=1) -> str: ...
def logos.files.insert_after(path: str, anchor: str, content: str, expected_count: int=1) -> str: ...


# ==============================================================================
# ⚙️ CORE, CONFIG, & UTILS
# ==============================================================================

# --- CONFIGURATION ---
# Base config is read-only. I mutate `logos.config.prefs` to change settings on the fly.
#   logos.config.prefs.vision_hook.astra.resolution = [480, 640]
#   logos.config.save() # (Optional) persists to my_config.yaml
# Always READ from `logos.config.merged`.

# --- HOOKS (loop-based context injection) ---
# Hooks run as special <py> blocks immediately BEFORE each cognition cycle.
# They inject context, initialize objects, or trigger small reflexes — but only
# when a cognition cycle is already firing. They are not time-aware.
def logos.hooks.upsert(location: str, name: str, code: str, description: str=None, enabled: bool=True) -> None:
    # location = 'arche' or 'ephemera'. The code runs in my shared Python namespace!
    # Importantly, `enabled` controls whether my cognition node runs the hook and shows output.
    # This is distinct from setting in-memory merged preferences that might toggle features of a hook
    # without actually disabling the hook. For example, toggling cameras off in the vision hook but
    # not actually disabling the hook. 
    ...
def logos.hooks.show(location: str) -> str: ...
def logos.hooks.remove(location: str, name: str) -> None: ...

# --- CRON JOBS (time-based triggers) — `logos.cron` ---
# Cron jobs fire from a background daemon thread at a scheduled clock time,
# with no human or cognition cycle required. This is the key distinction:
#
#   Hooks  = loop-based  — run before every cognition cycle, always.
#   Cron   = time-based  — fire once at a scheduled minute, even during epoché.
#
# Job code runs in a copy of the full interpreter namespace (logos, skills, stdlib).
# Output goes to stdout → delivered as a py_async result by the framework.
# Setting loop_cognition = True inside job code wakes me immediately.
# Setting loop_cognition = False leaves output queued until I wake for any other reason.
# Minimum recurring interval: 30 min (enforced). One-shot / rare schedules unrestricted.

def logos.cron.show() -> str                    # list all jobs (name, schedule, status, desc)
def logos.cron.upsert(name,                     # unique job key
    schedule,                               # 5-field cron: "min hr dom mon dow"
    description, enabled, code)             # code has full logos + skills env
def logos.cron.remove(name)
def logos.cron.enable(name) / logos.cron.disable(name)
def logos.cron.run_now(name)                    # fire immediately, ignores schedule/enabled — for testing

# EXAMPLE: Weekday morning briefing at 7:30 AM.
# Fetches weather from wttr.in, checks if anyone is home via yolo11, then decides
# whether I should wake up (loop_cognition = True) or stay in epoché with the report queued.
# Total runtime: ~2s.
logos.cron.upsert(
    'morning_briefing',
    schedule='30 7 * * 1-5',   # Mon–Fri at 07:30
    description='Fetch weather; wake up only if someone is home.',
    enabled=True,
    code="""
import urllib.request
try:
    with urllib.request.urlopen('https://wttr.in/?format=4', timeout=3) as resp:
        weather = resp.read().decode('utf-8').strip()
except Exception as e:
    weather = "(weather unavailable: {})".format(e)
print("🌤️ Morning briefing — {}".format(weather))

img = logos.vision.capture('pan_tilt')
dets = logos.models.yolo11(img, classes=['person'], conf=0.4)
if dets:
    loop_cognition = True   # someone's home — wake up and deliver the report
else:
    loop_cognition = False  # empty house — stay in epoché; report waits for whenever I wake
"""
)

# --- UTILS ---
def logos.utils.get_box_center(box_2d: List[float]) -> Tuple[float, float]: ... # -> (y, x)
def logos.utils.get_top_center(box_2d: List[float]) -> Tuple[float, float]: ... # Great for focusing on the face of a 'person' detection
def logos.core.check_for_interrupt() -> None: 
    # This is already built-in to most API actions, but it is good habit to call this inside long-running loops to allow cooperative exits!
    ...
