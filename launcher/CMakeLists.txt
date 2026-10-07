cmake_minimum_required(VERSION 3.25)
project(PyUpperLauncher LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(PY_UPPER_LAUNCHER_CONSOLE "Build a console-subsystem launcher for debugging the packaged application." OFF)

if(NOT PYSTAND_PYTHON_INCLUDE)
  message(FATAL_ERROR "PYSTAND_PYTHON_INCLUDE is required (PBS target SDK include directory)")
endif()

add_executable(PyUpper src/main.cpp src/PyUpper.cpp src/PyUpper.h)
target_include_directories(PyUpper PRIVATE "${PYSTAND_PYTHON_INCLUDE}")

if(PY_UPPER_APP_NAME)
  # The application name is compiled in at packaging time. The launcher cannot
  # read it from its own filename: renaming the executable is exactly the case
  # the static entry exists for.
  target_compile_definitions(PyUpper PRIVATE PY_UPPER_APP_NAME="${PY_UPPER_APP_NAME}")
endif()

if(WIN32)
  # A packaged application must not open a console window when it is started
  # from Explorer. The launcher attaches to the parent console instead when one
  # exists, so diagnostics still reach cmd.exe and CI logs. The console build is
  # the debugging escape hatch: it keeps stdout/stderr attached unconditionally.
  if(NOT PY_UPPER_LAUNCHER_CONSOLE)
    set_target_properties(PyUpper PROPERTIES WIN32_EXECUTABLE TRUE)
  else()
    target_compile_definitions(PyUpper PRIVATE PY_UPPER_CONSOLE)
  endif()
  # Static CRT: the launcher must start on a machine without the Visual C++
  # redistributable, and it must not add a second MSVCRT to a process that
  # already hosts the embedded CPython runtime.
  set_property(TARGET PyUpper PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
  # CommandLineToArgvW: the wide command line is the only lossless source of a
  # non-ASCII path.
  target_link_libraries(PyUpper PRIVATE shell32)
  if(PY_UPPER_RESOURCE_RC)
    enable_language(RC)
    target_sources(PyUpper PRIVATE "${PY_UPPER_RESOURCE_RC}")
  endif()
else()
  target_link_libraries(PyUpper PRIVATE ${CMAKE_DL_LIBS})
endif()

if(APPLE)
  # CFUserNotification: a fatal error in a bundle started from Finder has no
  # terminal to report to.
  target_link_libraries(PyUpper PRIVATE "-framework CoreFoundation")
endif()
