cmake_minimum_required(VERSION 3.25)
project(PyUpperLauncher LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT PYSTAND_PYTHON_INCLUDE)
  message(FATAL_ERROR "PYSTAND_PYTHON_INCLUDE is required (PBS target SDK include directory)")
endif()

add_executable(PyUpper src/main.cpp src/PyUpper.cpp src/PyUpper.h)
target_include_directories(PyUpper PRIVATE "${PYSTAND_PYTHON_INCLUDE}")

if(NOT WIN32)
  target_link_libraries(PyUpper PRIVATE ${CMAKE_DL_LIBS})
endif()
