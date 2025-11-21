cmake_minimum_required(VERSION 3.0.2)
project(logos_face)

add_compile_options(-std=c++11)

find_package(catkin REQUIRED COMPONENTS
  roscpp
  std_msgs
  logos_msgs
  dynamic_reconfigure
  cv_bridge
  image_transport
  sensor_msgs
)

find_package(PkgConfig REQUIRED)
pkg_check_modules(CACA REQUIRED caca)
find_package(OpenCV REQUIRED)

# Dynamic Reconfigure
generate_dynamic_reconfigure_options(
  cfg/FaceNodeConfig.cfg
)

catkin_package(
  CATKIN_DEPENDS roscpp std_msgs logos_msgs
)

include_directories(
  ${catkin_INCLUDE_DIRS}
  ${CACA_INCLUDE_DIRS}
  ${OpenCV_INCLUDE_DIRS}
)

add_executable(face_node src/face_node.cpp)

target_link_libraries(face_node
  ${catkin_LIBRARIES}
  ${CACA_LIBRARIES}
  ${OpenCV_LIBRARIES}
  caca
)

# Make sure face_node waits for:
# 1. Exported targets (msgs)
# 2. Catkin targets
# 3. The Dynamic Reconfigure Generation (_gencfg) <--- THE KEY
add_dependencies(face_node 
  ${${PROJECT_NAME}_EXPORTED_TARGETS} 
  ${catkin_EXPORTED_TARGETS}
  ${PROJECT_NAME}_gencfg
)