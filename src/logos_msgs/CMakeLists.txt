cmake_minimum_required(VERSION 3.0.2)
project(logos_msgs)

## Compile as C++11
add_compile_options(-std=c++11)

find_package(catkin REQUIRED COMPONENTS
  actionlib_msgs
  message_generation
  std_msgs
)

################################################
## Declare ROS messages, services and actions ##
################################################

## 1. MESSAGES
## Added the 'FILES' keyword here!
add_message_files(
  FILES
  EyeGazeX.msg
  EyeScaleX.msg
  EyeGazeY.msg
  EyeScaleY.msg
  EyeLidHeight.msg
  EyeLidAngle.msg
  EyeColor.msg
  SpeechData.msg
  ArmPose.msg
  AudioWave.msg
  MouthSine.msg
)

## 2. ACTIONS
## Added the 'FILES' keyword here too!
add_action_files(
  FILES
  Speak.action
)

## 3. GENERATE
## This MUST be uncommented to actually create the code!
generate_messages(
  DEPENDENCIES
  actionlib_msgs
  std_msgs
)

###################################
## catkin specific configuration ##
###################################

## We need to export message_runtime so other packages can link against this
catkin_package(
  CATKIN_DEPENDS actionlib_msgs message_runtime std_msgs
)

include_directories(
  ${catkin_INCLUDE_DIRS}
)