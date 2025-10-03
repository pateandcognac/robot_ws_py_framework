cmake_minimum_required(VERSION 3.0.2)
project(logos_framework)

find_package(catkin REQUIRED COMPONENTS
  message_generation
  rospy
  std_msgs
)

## Generate messages in the 'msg' folder
add_message_files(
  FILES
  CognitionInput.msg
  CognitionOutput.msg
)

## Generate added messages and services with any dependencies listed here
generate_messages(
  DEPENDENCIES
  std_msgs
)

catkin_package(
#  INCLUDE_DIRS include
#  LIBRARIES logos_framework
  CATKIN_DEPENDS message_generation rospy std_msgs message_runtime
#  DEPENDS system_lib
)

###########
## Build ##
###########

## Specify additional locations of header files
## Your package locations should be listed before other locations
include_directories(
# include
  ${catkin_INCLUDE_DIRS}
)

## Mark executable scripts (Python etc.) for installation
## in contrast to setup.py, you can choose the destination
catkin_install_python(PROGRAMS
  scripts/cognition_node.py
  scripts/python_worker_node.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)
