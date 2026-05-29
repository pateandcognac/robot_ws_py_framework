# Logos Embodied AI: ROS Architecture & Integration

**Version:** 1.0 "Spinal Cord"
**Maintainers:** Mark & Logos
**Last Updated:** 2026-05-29

## 1. Philosophy: The Layered Self
Unlike standard ROS robots that bundle hardware drivers with specific applications (e.g., "turning on mapping turns on the camera"), Logos employs a **Persistent Body** philosophy.

*   **Layer 0 (The Body):** Hardware drivers, safety systems, and sensor streams run continuously. They do not restart when switching tasks.
*   **Layer 1 (The Mind):** The Python-based cognitive loop (LLM, API, Worker Node). *API WIP*
*   **Layer 2 (The Skills):** Transient ROS nodes that provide capabilities (Navigation, SLAM, docking) on demand.

## 2. Workspace Hierarchy
The system uses a chained Catkin workspace setup.

1.  **`/opt/ros/noetic`**: Standard ROS system packages.
2.  **`~/tb2_ws`** (The Overlay): Contains standard TurtleBot2 packages (`turtlebot_bringup`, `kobuki_node`, `yocs_*`). We treat this as a read-only library of working defaults.
3.  **`~/robot_ws`** (The Logos Workspace): Contains custom Logos packages. This is where our development happens.

**Source Order:**
```bash
source /opt/ros/noetic/setup.bash
source ~/tb2_ws/devel/setup.bash
source ~/robot_ws/devel/setup.bash
```

## 3. The `logos_bringup` Package
**Location:** `~/robot_ws/src/logos_bringup/`

This package owns the robot's physical definition and startup routines.

### A. Control Flow (Velocity Pipeline)
We use a mostly standard TurtleBot2-style command pipeline: commands that need smoothing flow through `yocs_velocity_smoother` first, then the final `yocs_cmd_vel_mux` arbitrates what reaches the Kobuki base.

```text
[Kobuki safety controller] --(prio 10)----\
[Logos safety override] -----(prio  8)-----\
[Joystick / teleop] ---------(prio  7)------[ CMD_VEL_MUX ]---->[ KOBUKI BASE ]
[Move Base + Logos normal] -->[ VELOCITY_SMOOTHER ]--(prio 6)-/
[Logos raw low priority] ----(prio  4)----/
```

*   **Velocity smoother (`yocs_velocity_smoother`):** Shared by normal Logos base commands and `move_base`/`turtlebot_move` output. It publishes into the mux's `input/smooth` slot.
*   **Multiplexer (`yocs_cmd_vel_mux`):** The final arbiter before the base. Safety and teleop can still preempt smoothed autonomy.
*   **Kobuki safety controller (`kobuki_safety_controller`):** Reacts to bumper, cliff, and wheel-drop events and publishes to the highest-priority mux slot.

### B. Key Launch Files

#### Layer 0: The Core (Run Once, Run Forever)
*   **`launch/logos_core.launch`**
    *   **Function:** Wakes up the robot.
    *   **Includes:**
        *   `robot_description`: URDF/TF generation.
        *   `logos_mobile_base.launch.xml`: Custom base config. Starts Nodelets, Kobuki Driver, Smoother, Mux, Kobuki safety controller, bumper pointcloud, and turtlebot_move.
        *   `3dsensor.launch`: Starts the Orbbec Astra camera (Lazy publishing enabled).
    *   **Key Remapping:** `velocity_smoother/raw_cmd_vel` -> `cmd_vel_mux/input/smooth`; mux output -> `mobile_base/commands/velocity`.
    *   **Bumper Costmap Path:** `kobuki_bumper2pc` publishes `mobile_base/sensors/bumper_pointcloud`; the TurtleBot2 navigation costmap params loaded by `logos_move_base.launch.xml` include it as the `bump` observation source.

#### Layer 2: The Skills (Transient)
*   **`launch/logos_navigation.launch`**
    *   **Function:** Pathfinding in a known environment.
    *   **Requires:** A map file (default: `env TURTLEBOT_MAP_FILE`).
    *   **Nodes:** `map_server`, `amcl`, `move_base`.
*   **`launch/logos_slam.launch`**
    *   **Function:** Mapping an unknown environment.
    *   **Nodes:** `gmapping`, `move_base`.

#### Shared Utilities
*   **`launch/includes/logos_move_base.launch.xml`**
    *   **Function:** Pure path planning configuration.
    *   **Optimization:** Stripped of hardware nodes found in standard TB2 config (since Layer 0 handles them).
    *   **Key Remapping:** `cmd_vel` -> `velocity_smoother/raw_cmd_vel`.

## 4. Configuration Reference

### Mux Priorities (`param/mux.yaml`)
The nervous system listens to inputs in this order of importance:
1.  **Safety Controller (10):** Cliff sensors, bumper hits.
2.  **Logos Safety Override (8):** Logos emergency stop/back-up commands (`input/logos_safety`), below the Kobuki controller.
3.  **Teleoperation (7):** Joystick/Keyboard override.
4.  **Smoothed Autonomy (6):** Shared `move_base`, `turtlebot_move`, and normal Logos API path (`input/smooth`).
5.  **Logos Raw (4):** Low-priority unsmoothed Logos commands (`input/logos_raw`).

## 5. Quick Start Guide

**1. Wake up the Body:**
```bash
roslaunch logos_bringup logos_core.launch
```

**2. Start a Skill (Choose one):**

*   *To Map a Room:*
    ```bash
    roslaunch logos_bringup logos_slam.launch
    ```
*   *To Navigate a Room:*
    ```bash
    roslaunch logos_bringup logos_navigation.launch map_file:=/path/to/map.yaml
    ```

**3. Visualize:**
```bash
roslaunch turtlebot_rviz_launchers view_navigation.launch
```
