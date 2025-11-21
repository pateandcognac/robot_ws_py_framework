# EyeGazeX.msg is used 
# EyeGazeX.msg published *as goal* on /face/eye_gaze_x
# EyeGazeX.msg published *as frame information of interpolation to goal* on /face/live_state/eye_gaze_x
string eye_side # left, right, both (always both for live_state with left/right values being averaged)
float32 gaze_x # -1 to 1
float32 duration # seconds to spend animating/interpolating to goal. or, duration of frame (duration/rendering_fps)