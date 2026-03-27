#!/bin/bash
# Export YOLOX nano and tiny .pth models to ONNX format
# Outputs are placed in src/improc/data/ for use by YOLOXDetector
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

YOLOX_DIR="$PROJECT_ROOT/src/improc/yolox"
WEIGHTS_DIR="$PROJECT_ROOT/src/models/yolox"
OUTPUT_DIR="$PROJECT_ROOT/data"
EXPORT_SCRIPT="$YOLOX_DIR/tools/export_onnx.py"

mkdir -p "$OUTPUT_DIR"

# Models to export: name, checkpoint, experiment file, output filename
declare -A MODELS
MODELS=(
    ["nano"]="$WEIGHTS_DIR/yolox_nano.pth|$YOLOX_DIR/exps/default/yolox_nano.py|$OUTPUT_DIR/yolox_nano.onnx"
    ["tiny"]="$WEIGHTS_DIR/yolox_tiny.pth|$YOLOX_DIR/exps/default/yolox_tiny.py|$OUTPUT_DIR/yolox_tiny.onnx"
)

for model_name in "${!MODELS[@]}"; do
    IFS='|' read -r CKPT EXP_FILE OUTPUT <<< "${MODELS[$model_name]}"

    if [ ! -f "$CKPT" ]; then
        echo "ERROR: Checkpoint not found: $CKPT"
        echo "  Download from https://github.com/Megvii-BaseDetection/YOLOX/releases"
        continue
    fi

    if [ -f "$OUTPUT" ]; then
        echo "SKIP: $OUTPUT already exists (delete to re-export)"
        continue
    fi

    echo "Exporting yolox_${model_name} -> $OUTPUT"
    pushd "$YOLOX_DIR" > /dev/null
    python3 tools/export_onnx.py \
        -f "$EXP_FILE" \
        -c "$CKPT" \
        --output-name "$OUTPUT" \
        --no-onnxsim
    popd > /dev/null
    echo "Done: $OUTPUT"
done

echo "All exports complete."
