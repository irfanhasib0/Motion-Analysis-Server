#https://github.com/microsoft/onnxruntime/releases/download/v1.25.0/onnxruntime-linux-aarch64-1.25.0.tgz
#https://github.com/microsoft/onnxruntime/releases/download/v1.25.0/onnxruntime-linux-x64-1.25.0.tgz
cmake_minimum_required(VERSION 3.16)
project(yolox VERSION 1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
set(CMAKE_AUTOUIC ON)
#set(CMAKE_PREFIX_PATH "/usr/lib/x86_64-linux-gnu/cmake/")

# Find Qt5
#find_package(Qt5 REQUIRED COMPONENTS Core Widgets)

# Find OpenCV
find_package(OpenCV REQUIRED)

# ONNX Runtime — defaults to the bundled aarch64 release next to this CMakeLists.
# Override with:  cmake .. -DONNXRUNTIME_ROOT=/path/to/other/onnxruntime
if(NOT DEFINED ONNXRUNTIME_ROOT)
    set(ONNXRUNTIME_ROOT
        "${CMAKE_CURRENT_SOURCE_DIR}/onnxruntime-linux-x64-1.25.0"
        CACHE PATH "Root directory of ONNX Runtime installation")
endif()
find_library(ONNXRUNTIME_LIB
    NAMES onnxruntime
    HINTS "${ONNXRUNTIME_ROOT}/lib"
    REQUIRED)
find_path(ONNXRUNTIME_INCLUDE
    NAMES onnxruntime_cxx_api.h
    HINTS "${ONNXRUNTIME_ROOT}/include"
    REQUIRED)


# ---------------------------------------------------------------------------
# yolox_detector shared library
# ---------------------------------------------------------------------------
add_library(yolox_detector SHARED
    yolox_detector.cpp
    yolox_detector.h
)

target_include_directories(yolox_detector PUBLIC
    ${OpenCV_INCLUDE_DIRS}
    ${ONNXRUNTIME_INCLUDE}
)

target_link_libraries(yolox_detector
    ${OpenCV_LIBS}
    ${ONNXRUNTIME_LIB}
)

set_target_properties(yolox_detector PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN ON
    POSITION_INDEPENDENT_CODE ON
)

# Compiler warnings
if(MSVC)
    target_compile_options(yolox_detector PRIVATE /W4)
else()
    target_compile_options(yolox_detector PRIVATE -Wall -Wextra)
endif()
