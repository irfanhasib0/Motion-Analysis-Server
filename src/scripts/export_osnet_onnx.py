"""
Convert OSNet (.pth) weights to ONNX format.

Source:  src/deep-person-reid/torchreid
Weights: src/models/osnet/*.pth
Output:  data/

Usage:
    python export_osnet_onnx.py [--model MODEL] [--weights PATH] [--out DIR]
                                [--num-classes N] [--input-size H W]
                                [--opset OPSET] [--dynamic]

Examples:
    # Export both bundled weights with auto-detected architecture
    python export_osnet_onnx.py

    # Export a specific weight file
    python export_osnet_onnx.py --model osnet_x0_25 \
        --weights ../models/osnet/osnet_x0_25_imagenet.pth \
        --out ../../data/
"""

import argparse
import os
import sys

import torch

# Add torchreid to path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TORCHREID_ROOT = os.path.join(_SCRIPT_DIR, '..', 'deep-person-reid')
sys.path.insert(0, _TORCHREID_ROOT)

from torchreid.models.osnet import (  # noqa: E402
    osnet_x0_25,
    osnet_x0_5,
    osnet_x0_75,
    osnet_x1_0,
    osnet_ibn_x1_0,
)

# Map model name → factory function and expected filename fragment
_MODEL_REGISTRY = {
    'osnet_x1_0':     (osnet_x1_0,     'osnet_x1_0'),
    'osnet_x0_75':    (osnet_x0_75,    'osnet_x0_75'),
    'osnet_x0_5':     (osnet_x0_5,     'osnet_x0_5'),
    'osnet_x0_25':    (osnet_x0_25,    'osnet_x0_25'),
    'osnet_ibn_x1_0': (osnet_ibn_x1_0, 'osnet_ibn_x1_0'),
}


def guess_model_from_path(weights_path: str) -> str:
    """Infer model name from the weight filename."""
    name = os.path.basename(weights_path)
    for key in sorted(_MODEL_REGISTRY.keys(), key=len, reverse=True):
        if key in name:
            return key
    raise ValueError(
        f"Cannot infer model architecture from '{name}'. "
        f"Pass --model explicitly. Available: {list(_MODEL_REGISTRY.keys())}"
    )


def load_model(model_name: str, weights_path: str, num_classes: int, device: torch.device):
    factory, _ = _MODEL_REGISTRY[model_name]

    # Build model without downloading pretrained weights
    model = factory(num_classes=num_classes, pretrained=False, loss='softmax')

    state_dict = torch.load(weights_path, map_location=device)

    # Unwrap DataParallel / Lightning checkpoints
    if isinstance(state_dict, dict) and 'state_dict' in state_dict:
        state_dict = state_dict['state_dict']
    if isinstance(state_dict, dict):
        from collections import OrderedDict
        cleaned = OrderedDict()
        for k, v in state_dict.items():
            cleaned[k.replace('module.', '', 1)] = v
        state_dict = cleaned

    missing, unexpected = model.load_state_dict(state_dict, strict=False)
    if missing:
        print(f"  [warn] Missing keys ({len(missing)}): {missing[:5]}{'...' if len(missing) > 5 else ''}")
    if unexpected:
        print(f"  [warn] Unexpected keys ({len(unexpected)}): {unexpected[:5]}{'...' if len(unexpected) > 5 else ''}")

    model.to(device)
    model.eval()
    return model


def export_onnx(
    model: torch.nn.Module,
    out_path: str,
    input_size: tuple,
    opset: int,
    dynamic_batch: bool,
    device: torch.device,
):
    h, w = input_size
    dummy = torch.zeros(1, 3, h, w, device=device)

    dynamic_axes = None
    if dynamic_batch:
        dynamic_axes = {'input': {0: 'batch'}, 'output': {0: 'batch'}}

    torch.onnx.export(
        model,
        dummy,
        out_path,
        opset_version=opset,
        input_names=['input'],
        output_names=['output'],
        dynamic_axes=dynamic_axes,
        do_constant_folding=True,
    )
    size_mb = os.path.getsize(out_path) / 1e6
    print(f"  Saved → {out_path}  ({size_mb:.1f} MB)")


def find_default_weights():
    """Return list of .pth files in src/models/osnet/."""
    osnet_dir = os.path.join(_SCRIPT_DIR, '..', 'models', 'osnet')
    if not os.path.isdir(osnet_dir):
        return []
    return [
        os.path.join(osnet_dir, f)
        for f in os.listdir(osnet_dir)
        if f.endswith('.pth')
    ]


def parse_args():
    p = argparse.ArgumentParser(description='Export OSNet weights to ONNX')
    p.add_argument('--model', default=None,
                   choices=list(_MODEL_REGISTRY.keys()),
                   help='Model architecture. Auto-detected from filename if omitted.')
    p.add_argument('--weights', default=None,
                   help='Path to .pth checkpoint. Defaults to all files in src/models/osnet/.')
    p.add_argument('--out', default=None,
                   help='Output directory. Defaults to <repo_root>/data/.')
    p.add_argument('--num-classes', type=int, default=1000,
                   help='Number of identity classes the checkpoint was trained with (default: 1000).')
    p.add_argument('--input-size', type=int, nargs=2, default=[256, 128],
                   metavar=('H', 'W'),
                   help='Model input resolution H W (default: 256 128).')
    p.add_argument('--opset', type=int, default=12,
                   help='ONNX opset version (default: 12).')
    p.add_argument('--dynamic', action='store_true',
                   help='Export with dynamic batch dimension.')
    p.add_argument('--cpu', action='store_true',
                   help='Force CPU export even if CUDA is available.')
    return p.parse_args()


def main():
    args = parse_args()

    device = torch.device('cpu' if args.cpu or not torch.cuda.is_available() else 'cuda')
    print(f"Device: {device}")

    # Resolve output directory
    if args.out:
        out_dir = os.path.abspath(args.out)
    else:
        # <repo_root>/data/
        out_dir = os.path.abspath(os.path.join(_SCRIPT_DIR, '..', '..', 'data'))
    os.makedirs(out_dir, exist_ok=True)

    # Collect weight files to convert
    if args.weights:
        weight_files = [args.weights]
    else:
        weight_files = find_default_weights()
        if not weight_files:
            print("No .pth files found in src/models/osnet/. Pass --weights explicitly.")
            sys.exit(1)
        print(f"Found {len(weight_files)} weight file(s): {[os.path.basename(f) for f in weight_files]}")

    for weights_path in weight_files:
        weights_path = os.path.abspath(weights_path)
        if not os.path.isfile(weights_path):
            print(f"[skip] File not found: {weights_path}")
            continue

        model_name = args.model or guess_model_from_path(weights_path)
        stem = os.path.splitext(os.path.basename(weights_path))[0]
        out_path = os.path.join(out_dir, stem + '.onnx')

        print(f"\n[{model_name}]  {os.path.basename(weights_path)}")
        print(f"  num_classes={args.num_classes}  input={args.input_size}  opset={args.opset}")

        model = load_model(model_name, weights_path, args.num_classes, device)
        export_onnx(model, out_path, args.input_size, args.opset, args.dynamic, device)


if __name__ == '__main__':
    main()
