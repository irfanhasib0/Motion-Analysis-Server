#!/usr/bin/env bash
# Build the C++ shared library (yolox_detector) from src/cpp/CMakeLists.txt.
#
# Usage:
#   ./build_cpp.sh                   # auto-detect arch, release build
#   ./build_cpp.sh --debug           # debug build
#   ./build_cpp.sh --arch x64        # force x64 ORT bundle
#   ./build_cpp.sh --arch aarch64    # force aarch64 ORT bundle
#   ./build_cpp.sh --jobs 2          # limit parallel jobs

set -euo pipefail

# ---------------------------------------------------------------------------
# Dependency checks — install missing packages with apt if available
# ---------------------------------------------------------------------------
_ensure_tool() {
    local cmd="$1" pkg="${2:-$1}"
    if ! command -v "$cmd" &>/dev/null; then
        echo "INFO: '$cmd' not found — attempting install via apt..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y "$pkg"
        else
            echo "ERROR: '$cmd' is required but could not be installed automatically."
            echo "       Please install '$pkg' manually and re-run."
            exit 1
        fi
    fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CPP_DIR="$SCRIPT_DIR/src/cpp"
BUILD_DIR="$CPP_DIR/build"

BUILD_TYPE="Release"
JOBS="$(nproc 2>/dev/null || echo 4)"
ARCH=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --debug)   BUILD_TYPE="Debug"; shift ;;
        --arch)    ARCH="$2"; shift 2 ;;
        --jobs)    JOBS="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Auto-detect architecture if not specified
if [[ -z "$ARCH" ]]; then
    MACHINE="$(uname -m)"
    case "$MACHINE" in
        x86_64)          ARCH="x64" ;;
        aarch64|arm64)   ARCH="aarch64" ;;
        *)
            echo "WARNING: Unknown machine arch '$MACHINE', defaulting to x64"
            ARCH="x64"
            ;;
    esac
fi

ORT_DIR="$CPP_DIR/onnxruntime-linux-${ARCH}-1.25.0"
if [[ ! -d "$ORT_DIR" ]]; then
    ORT_TGZ="onnxruntime-linux-${ARCH}-1.25.0.tgz"
    ORT_URL="https://github.com/microsoft/onnxruntime/releases/download/v1.25.0/${ORT_TGZ}"
    echo "INFO: ONNX Runtime not found — downloading from $ORT_URL ..."
    _ensure_tool wget
    _ensure_tool tar
    wget -q --show-progress -O "/tmp/${ORT_TGZ}" "$ORT_URL"
    tar -xzf "/tmp/${ORT_TGZ}" -C "$CPP_DIR"
    rm "/tmp/${ORT_TGZ}"
    echo "INFO: Extracted to $ORT_DIR"
fi

echo "=== Building yolox_detector ==="
echo "  Arch:       $ARCH"
echo "  ORT dir:    $ORT_DIR"
echo "  Build type: $BUILD_TYPE"
echo "  Jobs:       $JOBS"
echo "  Build dir:  $BUILD_DIR"
echo ""

_ensure_tool cmake
mkdir -p "$BUILD_DIR"
cmake -S "$CPP_DIR" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
    -DONNXRUNTIME_ROOT="$ORT_DIR"

cmake --build "$BUILD_DIR" --target yolox_detector --parallel "$JOBS"

LIB="$BUILD_DIR/libyolox_detector.so"
if [[ -f "$LIB" ]]; then
    LIBS_DIR="$SCRIPT_DIR/src/libs"
    mkdir -p "$LIBS_DIR"
    cp "$LIB" "$LIBS_DIR/libyolox_detector.so"
    echo ""
    echo "Built:   $LIB"
    echo "Copied:  $LIBS_DIR/libyolox_detector.so"
else
    echo "ERROR: Expected output not found: $LIB"
    exit 1
fi
