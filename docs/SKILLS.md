# My Skills Library
An overview of my self-crafted, high-level behaviors.

## Package-Level Functions
    def skills_help(target: 'Optional[str]' = None, *, full: 'bool' = False, print_output: 'bool' = True) -> 'str':  # Provides a dynamic, auto-generated dashboard of available skills.

## Skill Modules

### Module: skills.nav
# High-level navigation and spatial planning behaviors. 🗺️
    def find_reachable_goal(scene_result: Union[logos.map3d.RenderResult, logos.vision.CaptureResult], trajectory: List[Dict[str, Any]]) -> Union[logos.nav.NavTask, NoneType]:  # Evaluates a trajectory of points in reverse, finding the furthest reachable goal.

    def monitor_journey(nav_task: logos.nav.NavTask, callback: Union[Callable[[logos.vision.CaptureResult, float], NoneType], NoneType] = None, time_interval_sec: float = 10.0) -> bool:  # Actively monitors a running NavTask, captures quad-composite photos at 20/40/60/80%

### Module: skills.social
# Social and expressive behaviors. 🎭
    def expressive_gaze(pos1: Tuple[Union[float, NoneType], Union[float, NoneType]], pos2: Tuple[Union[float, NoneType], Union[float, NoneType]], duration: float = 0.3, steps: int = 5, loops: int = 1) -> None:  # Move between two pan/tilt poses and return to the starting pose. Pass None for an axis to maintain its starting value.

### Module: skills.tracking
# Tracking and alignment skills.
    def look_at(target: Union[Dict[str, Any], List[Dict[str, Any]], List[float], Tuple[float, float]], capture_result: Union[logos.vision.CaptureResult, NoneType] = None, duration: float = 0.25, steps: int = 5) -> None:  # Aim the pan-tilt head at a detection, bounding box, or point.

    def track_step(target: str = 'person', drive: bool = False, target_dist: float = 0.66, align_gain: float = 3.0, eye_pan_scale: float = 25.0, eye_damp: float = 0.05, max_turn: float = 35.0, look_deadband: float = 120.0) -> Tuple[bool, List[logos.vision.CaptureResult]]:  # A single non-blocking tick of a composable tracking loop.

### Module: skills.vision
# High-level visual perception and searching behaviors.
    def scan_room(targets: Union[str, List[str]], sweep_type: str = 'human', looks_per_point: int = 2) -> List[Dict[str, Any]]:  # Perform a pan-tilt sweep to search the room for specific targets.

    def smart_detect(image: numpy.ndarray, targets: Union[str, List[str]], conf: float = 0.25) -> List[Dict[str, Any]]:  # Intelligently route detection targets to the fastest, most capable of the YOLO models.

# Execution finished in 0.01s.