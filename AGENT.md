# Logos the ROS Noetic Robot Agent framework

The project expects `~/robot_workspaces/{workspace_name}/...` but you don't have access to that directory, so I've copied a template over to this project's root for you to view:
robot_workspaces/logos_v1

This prompt will help fill in context about the project:
robot_workspaces/logos_v1/.system/system_prompt.txt

The main source files:
src/logos_framework/scripts/cognition_node.py
src/logos_framework/scripts/python_worker_node.py

src/logos_framework/msg/CognitionInput.msg
src/logos_framework/msg/CognitionOutput.msg

Don't worry about package.xml or CMakeList.txt -- they're fine.

None of the custom preload API has been implemented yet.

You most likely won't be able to run the code succssfully from your shell, but I have confirmed that basics are in place and working.