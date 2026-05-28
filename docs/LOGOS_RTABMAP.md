# Logos RTAB-Map

# Created by Codex. To resume session, run: `codex resume 019e6f73-d47c-7db1-9700-04f33bd1e08c`

This is the practical guide for using RTAB-Map on Logos when your mental model
is still "GMapping makes a 2D map, AMCL localizes in it." That model is still
useful. RTAB-Map just adds a richer database and loop-closure graph behind the
scenes.

## The Short Version

For the first pass, use RTAB-Map like this:

```bash
# Terminal 1: already running, but listed here for completeness
roslaunch logos_bringup logos_core.launch

# Terminal 2: build/update an RTAB-Map database and publish /map
bin/logos_rtabmap.sh map

# Drive Logos around slowly with teleop.

# Terminal 3: when /map looks good, save it as a classic AMCL map
bin/logos_rtabmap.sh save2d kitchen

# Later, use the saved 2D map with the existing AMCL navigation stack
bin/logos_rtabmap.sh amcl ~/maps/kitchen.yaml
```

That is a perfectly valid workflow: RTAB-Map for mapping, `map_server` plus
AMCL for normal navigation.

## What RTAB-Map Is Saving

GMapping mostly gives you an occupancy grid: a `.pgm` image plus a `.yaml`
metadata file.

RTAB-Map saves a database, by default:

```text
~/.ros/logos_rtabmap.db
```

That database can contain RGB images, depth, scans, odometry, the pose graph,
loop closures, local occupancy grids, and cached map data. The live ROS node
uses that database to publish a 2D occupancy grid on:

```text
/map
```

Internally RTAB-Map calls this `/rtabmap/grid_map`; the Logos launch remaps it
to `/map` so classic ROS tools like `map_saver`, RViz, and navigation are happy.

## Helper Commands

The helper script is:

```bash
bin/logos_rtabmap.sh
```

Useful commands:

```bash
bin/logos_rtabmap.sh map
bin/logos_rtabmap.sh fresh
bin/logos_rtabmap.sh localize
bin/logos_rtabmap.sh nav
bin/logos_rtabmap.sh save2d kitchen
bin/logos_rtabmap.sh amcl ~/maps/kitchen.yaml
bin/logos_rtabmap.sh info
bin/logos_rtabmap.sh view
bin/logos_rtabmap.sh export-cloud
```

`fresh` deletes the selected RTAB-Map database before mapping. Use it when you
want a clean mapping run. `map` keeps appending/updating the existing database.

## Mapping Technique

RTAB-Map likes recognizable places and loop closures.

Drive slower than you think you need to. Pause at corners and doorways. Turn in
place gently so the camera sees overlapping views. Revisit the start area before
you finish, so RTAB-Map can close the loop and correct drift.

Good first route:

```text
start area -> wall loop -> doorway -> return through same doorway -> start area
```

Bad first route:

```text
sprint down a hall -> fast spin -> new room -> stop forever
```

After a mapping run, watch the 2D `/map` in RViz. Save it only after the walls
look stable and the robot pose is not obviously offset from the map.

## Saving A Classic 2D Map

While RTAB-Map is running and publishing `/map`:

```bash
bin/logos_rtabmap.sh save2d kitchen
```

This writes:

```text
~/maps/kitchen.yaml
~/maps/kitchen.pgm
```

To choose an explicit output path:

```bash
bin/logos_rtabmap.sh save2d-to ~/maps/downstairs_2026_05_28
```

The saved `.yaml/.pgm` pair is the same kind of artifact you would get from
GMapping and can be used by `map_server` and AMCL.

## Using AMCL After RTAB-Map

Yes: once you have a good 2D occupancy map, using AMCL nav is the conservative
and very reasonable path.

```bash
bin/logos_rtabmap.sh amcl ~/maps/kitchen.yaml
```

This starts:

```text
map_server -> AMCL -> move_base
```

Use this mode for ordinary navigation when you trust the 2D map and the
environment has not changed much.

## Using RTAB-Map For Localization

RTAB-Map localization is useful when you want visual/depth loop-closure help in
addition to wheel odom and scan data:

```bash
bin/logos_rtabmap.sh localize
```

With navigation:

```bash
bin/logos_rtabmap.sh nav
```

This does not use a saved `.yaml` map. It localizes against the RTAB-Map
database and publishes `/map` live from that database.

Good use cases:

- You want to relocalize from visual place recognition.
- The 2D map from AMCL is okay, but the robot often starts with a poor initial
  pose.
- You want to keep RTAB-Map's 3D/map database as the source of truth.

Reasons to prefer AMCL:

- It is simpler and more predictable.
- It uses the saved 2D map directly.
- It is cheaper to run than RGB-D mapping/localization.
- It matches the TurtleBot2 navigation stack Logos already has.

## Post-Processing: What Matters First

For a noob-friendly workflow, "post-processing" mostly means:

1. Get a clean RTAB-Map database by driving well.
2. Let loop closures happen by returning to known areas.
3. Save a 2D `/map` only after the live map looks coherent.
4. Test that saved map with AMCL.
5. Redo the mapping route if the 2D map has doubled walls, warped rooms, or
   broken doorways.

RTAB-Map also has offline tools:

```bash
bin/logos_rtabmap.sh info
bin/logos_rtabmap.sh view
bin/logos_rtabmap.sh export-cloud
```

The database viewer is the main manual inspection/post-processing UI. It can
show constraints, loop closures, graph errors, images, depth, and generated maps.
It needs a desktop display.

The point cloud export is useful for inspection or external 3D processing, but
it is not required for AMCL navigation.

## Common Problems

### `/map` is not publishing

Make sure RTAB-Map is running and has received synchronized inputs:

```bash
rostopic hz /map
rostopic hz /camera/rgb/image_rect_color
rostopic hz /camera/depth_registered/image_raw
rostopic hz /scan
rostopic hz /odom
```

### The 2D map has doubled walls

That usually means odometry drift was not corrected by loop closure. Drive back
through previously seen areas, pause, and give RTAB-Map enough visual overlap.

### AMCL works badly on the saved map

Check that the saved map has clear walls and door openings. If the RTAB-Map
2D grid was built from the depth-derived scan, shiny/low/transparent obstacles
may still be unreliable. For AMCL, a boring clean wall map is better than a
beautiful but noisy 3D reconstruction.

### RTAB-Map starts but the GUI does not

Use:

```bash
roslaunch logos_bringup logos_rtabmap.launch rtabmap_viz:=false
```

or:

```bash
bin/logos_rtabmap.sh map
```

from a terminal with a working desktop session if you want the GUI.

## Current Logos Defaults

The launch file is:

```text
src/logos_bringup/launch/logos_rtabmap.launch
```

It assumes `logos_core.launch` already provides:

```text
/odom
/scan
/camera/rgb/image_rect_color
/camera/depth_registered/image_raw
/camera/rgb/camera_info
```

RTAB-Map uses Kobuki wheel odometry instead of starting its own visual odometry.
It publishes `map -> odom`, while the base and robot description publish the
rest of the TF tree.

Do not run `logos_rtabmap.launch`, `logos_slam.launch`, and
`logos_navigation.launch` together unless you know exactly which node owns
`map -> odom` and `/map`.
