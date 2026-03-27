#!/bin/bash
# =============================================================================
# Nuitka Standalone Build Script for Motion Analysis Server
# 
# Strategy: Compile YOUR code to C, bundle heavy 3rd-party wheels as-is.
# This avoids recompiling numpy/cv2/scipy/onnxruntime from source.
# =============================================================================

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJECT_ROOT/api/backend"
SRC_DIR="$PROJECT_ROOT/src"
FRONTEND_BUILD="$PROJECT_ROOT/api/frontend/build"
CONFIGS_DIR="$PROJECT_ROOT/configs"
OUTPUT_DIR="$PROJECT_ROOT/dist"

echo "=== Motion Analysis Server - Nuitka Build ==="
echo "Project root: $PROJECT_ROOT"
echo "Backend dir:  $BACKEND_DIR"
echo "Src dir:      $SRC_DIR"

# ---- Prerequisites check ----
command -v python3 >/dev/null 2>&1 || { echo "python3 not found"; exit 1; }
python3 -c "import nuitka" 2>/dev/null || {
    echo "Installing nuitka and ordered-set..."
    pip install nuitka ordered-set
}

# ---- Prepare: copy src packages into backend so Nuitka sees them ----
# Nuitka needs all packages reachable from the entry point directory.
# We symlink src/{improc,audioproc,trackers} into the backend dir temporarily.
echo ""
echo "=== Setting up package symlinks ==="

SYMLINKS=()
for pkg in improc audioproc trackers; do
    target="$SRC_DIR/$pkg"
    link="$BACKEND_DIR/$pkg"
    if [ -e "$link" ]; then
        echo "  $pkg already exists in backend dir, skipping"
    else
        ln -sf "$target" "$link"
        SYMLINKS+=("$link")
        echo "  Linked $pkg -> $target"
    fi
done

cleanup() {
    echo ""
    echo "=== Cleaning up symlinks ==="
    for link in "${SYMLINKS[@]}"; do
        rm -f "$link"
        echo "  Removed $link"
    done
}
trap cleanup EXIT

# ---- Build ----
echo ""
echo "=== Running Nuitka ==="

DATA_DIR="$PROJECT_ROOT/data"
mkdir -p "$OUTPUT_DIR/data"
mkdir -p "$OUTPUT_DIR/configs"

cp "$DATA_DIR/yn.onnx" "$OUTPUT_DIR/data/"
cp "$DATA_DIR/yt.onnx" "$OUTPUT_DIR/data/"
cp "$DATA_DIR/hfd.xml" "$OUTPUT_DIR/data/"
cp "$DATA_DIR/hfb.xml" "$OUTPUT_DIR/data/"
cp "$CONFIGS_DIR/"*.yaml "$OUTPUT_DIR/configs/"

cd "$BACKEND_DIR"

python3 -m nuitka \
    --nofollow-imports \
    --follow-import-to=services \
    --follow-import-to=models \
    --follow-import-to=routes \
    --follow-import-to=improc \
    --follow-import-to=audioproc \
    --follow-import-to=trackers \
    \
    --include-package=services \
    --include-package=models \
    --include-package=routes \
    --include-package=improc \
    --include-package=audioproc \
    --include-package=trackers \
    --include-data-dir="$DATA_DIR=data" \
    --include-data-dir="$CONFIGS_DIR=configs" \
    \
    --nofollow-import-to=trackers.pose_tracker \
    \
    --output-dir="$OUTPUT_DIR" \
    --output-filename=motion-analysis-server \
    \
    --jobs=1 \
    --lto=no \
    --low-memory \
    \
    --plugin-enable=anti-bloat \
    --show-progress \
    --show-memory \
    \
    start_server.py

echo ""
echo "=== Build Complete ==="
echo "Output: $OUTPUT_DIR/start_server.dist/"

# ---- Bundle frontend build if available ----
if [ -d "$FRONTEND_BUILD" ]; then
    echo "Copying frontend build..."
    cp -r "$FRONTEND_BUILD" "$OUTPUT_DIR/start_server.dist/frontend/build"
    echo "Frontend bundled."
fi

echo ""
echo "=== Done ==="
echo "To run: cd $OUTPUT_DIR/start_server.dist && ./motion-analysis-server"
