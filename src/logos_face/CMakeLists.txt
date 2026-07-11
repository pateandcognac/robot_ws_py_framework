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

catkin_install_python(PROGRAMS
  scripts/pico_face_bridge.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

include_directories(
  ${catkin_INCLUDE_DIRS}
  ${CACA_INCLUDE_DIRS}
  ${OpenCV_INCLUDE_DIRS}
)

function(add_logos_face_node target source)
  add_executable(${target} ${source})

  target_link_libraries(${target}
    ${catkin_LIBRARIES}
    ${CACA_LIBRARIES}
    ${OpenCV_LIBRARIES}
    caca
  )

  # Wait for generated messages and dynamic reconfigure headers.
  add_dependencies(${target}
    ${${PROJECT_NAME}_EXPORTED_TARGETS}
    ${catkin_EXPORTED_TARGETS}
    ${PROJECT_NAME}_gencfg
  )
endfunction()

add_logos_face_node(face_node src/face_node.cpp)
add_logos_face_node(face_node_mouth_trails src/face_node_mouth_trails.cpp)
add_logos_face_node(face_node_text_backdrop src/face_node_text_backdrop.cpp)
add_logos_face_node(face_hud_node src/face_hud.cpp)
