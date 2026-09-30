cmake_minimum_required(VERSION 3.25)
project(PyStandLauncher LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT PYSTAND_PYTHON_INCLUDE)
  message(FATAL_ERROR "PYSTAND_PYTHON_INCLUDE is required (PBS target SDK include directory)")
endif()

add_executable(PyStand src/main.cpp src/PyStand.cpp src/PyStand.h)
target_include_directories(PyStand PRIVATE "${PYSTAND_PYTHON_INCLUDE}")

if(NOT WIN32)
  target_link_libraries(PyStand PRIVATE ${CMAKE_DL_LIBS})
endif()
