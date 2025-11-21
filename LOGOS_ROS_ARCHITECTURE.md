# Logos Embodied AI: ROS Architecture & Integration

**Version:** 1.0 "Spinal Cord"
**Maintainers:** Mark & Logos
**Last Updated:** Current Date

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
We use a custom pipeline to ensure the AI (Logos) has priority over automated navigation, while keeping the hardware safe.

```text
[Joystick] ------(prio 7)-----\
                               \
[Logos API] -----(prio 6)-------[ CMD_VEL_MUX ]---->[ VELOCITY_SMOOTHER ]---->[ KOBUKI BASE ]
                               /
[Move Base] -----(prio 5)-----/
```

*   **Multiplexer (`yocs_cmd_vel_mux`):** Arbitrates who gets to drive.
*   **Smoother (`yocs_velocity_smoother`):** A global filter. It sits *after* the mux, ensuring that even jerky commands from the AI or Joystick are smoothed before hitting the motors.

### B. Key Launch Files

#### Layer 0: The Core (Run Once, Run Forever)
*   **`launch/logos_core.launch`**
    *   **Function:** Wakes up the robot.
    *   **Includes:**
        *   `robot_description`: URDF/TF generation.
        *   `logos_mobile_base.launch.xml`: Custom base config. Starts Nodelets, Kobuki Driver, Mux, and Smoother.
        *   `3dsensor.launch`: Starts the Orbbec Astra camera (Lazy publishing enabled).
    *   **Key Remapping:** Mux output `raw_cmd_vel` -> Smoother -> `mobile_base/commands/velocity`.

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
    *   **Optimization:** Stripped of all hardware/safety includes found in standard TB2 config (since Layer 0 handles them).
    *   **Key Remapping:** `cmd_vel` -> `cmd_vel_mux/input/navi`.

## 4. Configuration Reference

### Mux Priorities (`param/mux.yaml`)
The nervous system listens to inputs in this order of importance:
1.  **Safety Controller (10):** Cliff sensors, bumper hits.
2.  **Teleoperation (7):** Joystick/Keyboard override.
3.  **Logos Agent (6):** The LLM/Python API (`input/logos`). **<-- ME**
4.  **Navigation (5):** Move Base automated pathing (`input/navi`).

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
```
roslaunch turtlebot_rviz_launchers view_navigation.launch