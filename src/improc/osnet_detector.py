"""
OSNet ONNX Re-ID Feature Extractor

Wraps OSNet ONNX models for person re-identification.
Given a BGR crop of a detected person, returns a 512-d L2-normalised
embedding that can be compared across frames/cameras for re-ID.

Requires: onnxruntime

Model variants (width multipliers):
    osnet_x1_0   — full size    (channels: 64/256/384/512)
    osnet_x0_75  — medium
    osnet_x0_5   — tiny         (default for edge devices)
    osnet_x0_25  — very tiny    (channels: 16/64/96/128)

Usage:
    extractor = OSNetExtractor(model_path="osnet_x0_25_imagenet.onnx")
    embedding = extractor.extract(crop_bgr)         # (512,) float32
    embeddings = extractor.extract_batch(crops)     # (N, 512) float32

    # From a frame + detection bboxes produced by YOLOXDetector:
    for det in detections:
        x1, y1, x2, y2 = det['bbox']
        crop = frame[y1:y2, x1:x2]
        det['reid_feat'] = extractor.extract(crop)
"""
import os
import numpy as np
import cv2

import onnxruntime

# ImageNet normalisation constants (RGB order)
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _preprocess(crop_bgr, input_hw):
    """Resize + normalise a single BGR crop to a model-ready float32 tensor.

    Args:
        crop_bgr: np.ndarray (H, W, 3) uint8, BGR.
        input_hw: (H, W) target spatial size, e.g. (256, 128).

    Returns:
        np.ndarray (1, 3, H, W) float32, RGB, ImageNet-normalised.
    """
    h, w = input_hw
    img = cv2.resize(crop_bgr, (w, h), interpolation=cv2.INTER_LINEAR)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    img = (img - _MEAN) / _STD                  # (H, W, 3)
    img = img.transpose(2, 0, 1)                 # (3, H, W)
    return np.ascontiguousarray(img[None], dtype=np.float32)  # (1, 3, H, W)


def _l2_norm(vecs):
    """Row-wise L2 normalisation. Input: (N, D). Returns: (N, D)."""
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return vecs / norms


class OSNetExtractor:
    """ONNX-based OSNet re-ID feature extractor.

    Mirrors the API of YOLOXDetector so it plugs into the same pipeline.
    """

    # Supported model filename stems → default input (H, W)
    MODEL_CONFIGS = {
        'osnet_x1_0':     (256, 128),
        'osnet_x0_75':    (256, 128),
        'osnet_x0_5':     (256, 128),
        'osnet_x0_25':    (256, 128),
        'osnet_ibn_x1_0': (256, 128),
    }

    def __init__(
        self,
        model_variant='osnet_x0_25',
        input_shape=None,
        normalize=True,
        min_crop_size=8,
        data_dir=None,
    ):
        """
        Args:
            model_variant: Key from MODEL_CONFIGS; also used to locate the .onnx
                file as ``<data_dir>/<model_variant>_imagenet.onnx``.
            input_shape: Override (H, W). Defaults to (256, 128).
            normalize: L2-normalise embeddings before returning (default True).
            min_crop_size: Crops smaller than this in either dimension are
                skipped and return a zero vector.
            data_dir: Directory containing the .onnx file.
                Defaults to ``../../data/`` relative to this file.
        """
        self.enabled = False
        self.session = None
        self.model_variant = model_variant
        self.normalize = normalize
        self.min_crop_size = min_crop_size

        self.input_shape = tuple(input_shape) if input_shape else \
            self.MODEL_CONFIGS.get(model_variant, (256, 128))

        if data_dir is None:
            data_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                '..', '..', 'data'
            )
        data_dir = os.path.abspath(data_dir)
        model_path = os.path.join(data_dir, f'{model_variant}_imagenet.onnx')

        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f'OSNet model not found at {model_path} — extractor disabled.'
            )

        providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        self.session = onnxruntime.InferenceSession(model_path, providers=providers)
        self.input_name  = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.feat_dim    = self.session.get_outputs()[0].shape[-1]  # typically 512
        self.enabled = True
        print(
            f'OSNet [{model_variant}] loaded from {model_path} '
            f'(input={self.input_shape}, feat_dim={self.feat_dim}, '
            f'normalize={normalize})'
        )

    # ------------------------------------------------------------------
    # Core inference
    # ------------------------------------------------------------------

    def extract(self, crop_bgr):
        """Extract a re-ID embedding from a single person crop.

        Args:
            crop_bgr: np.ndarray (H, W, 3) uint8, BGR.

        Returns:
            np.ndarray shape (feat_dim,) float32.
            Zero vector if extractor is disabled or crop is too small.
        """
        zero = np.zeros(self.feat_dim, dtype=np.float32)
        if not self.enabled or self.session is None:
            return zero
        if crop_bgr is None or crop_bgr.size == 0:
            return zero
        h, w = crop_bgr.shape[:2]
        if h < self.min_crop_size or w < self.min_crop_size:
            return zero

        tensor = _preprocess(crop_bgr, self.input_shape)
        feat = self.session.run([self.output_name], {self.input_name: tensor})[0]  # (1, D)
        feat = feat.reshape(1, -1)
        if self.normalize:
            feat = _l2_norm(feat)
        return feat[0]

    def extract_batch(self, crops_bgr):
        """Extract embeddings for a list of BGR crops in one forward pass.

        Args:
            crops_bgr: List of np.ndarray (H_i, W_i, 3) uint8, BGR.

        Returns:
            np.ndarray shape (N, feat_dim) float32.
            Rows for invalid/too-small crops are zero vectors.
        """
        n = len(crops_bgr)
        results = np.zeros((n, self.feat_dim), dtype=np.float32)
        if not self.enabled or self.session is None or n == 0:
            return results

        valid_idx, tensors = [], []
        for i, crop in enumerate(crops_bgr):
            if crop is None or crop.size == 0:
                continue
            h, w = crop.shape[:2]
            if h < self.min_crop_size or w < self.min_crop_size:
                continue
            tensors.append(_preprocess(crop, self.input_shape)[0])  # (3, H, W)
            valid_idx.append(i)

        if not tensors:
            return results

        batch = np.stack(tensors, axis=0)  # (M, 3, H, W)
        feats = self.session.run([self.output_name], {self.input_name: batch})[0]  # (M, D)
        if self.normalize:
            feats = _l2_norm(feats)
        for row, idx in enumerate(valid_idx):
            results[idx] = feats[row]
        return results

    # ------------------------------------------------------------------
    # Convenience helper
    # ------------------------------------------------------------------

    def extract_from_detections(self, frame_bgr, detections):
        """Attach 'reid_feat' to each detection dict in-place.

        Args:
            frame_bgr: Full camera frame (H, W, 3) uint8, BGR.
            detections: List of dicts with 'bbox' key as [x1, y1, x2, y2].
                        Compatible with YOLOXDetector output.

        Returns:
            The same list with 'reid_feat' (np.ndarray, shape (feat_dim,))
            added to each entry.
        """
        if not detections:
            return detections

        crops = []
        for det in detections:
            x1, y1, x2, y2 = det['bbox']
            crop = frame_bgr[max(0, y1):y2, max(0, x1):x2]
            crops.append(crop)

        embeddings = self.extract_batch(crops)
        for det, emb in zip(detections, embeddings):
            det['reid_feat'] = emb
        return detections

    # ------------------------------------------------------------------
    # Runtime configuration (mirrors YOLOXDetector API)
    # ------------------------------------------------------------------

    def set_enabled(self, enabled):
        self.enabled = enabled and (self.session is not None)

    def is_enabled(self):
        return self.enabled

    def get_status(self):
        return {
            'enabled':       self.enabled,
            'model_variant': self.model_variant,
            'input_shape':   self.input_shape,
            'feat_dim':      self.feat_dim,
            'normalize':     self.normalize,
            'min_crop_size': self.min_crop_size,
        }
