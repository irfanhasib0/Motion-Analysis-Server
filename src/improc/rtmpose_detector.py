"""
RTMPose ONNX Detector

Runs RTMPose-tiny / RTMPose-s keypoint estimation via ONNX Runtime.
Accepts person bounding boxes (x1,y1,x2,y2) and returns COCO-17 keypoints
in image coordinates.

No mmpose / mmengine dependency — all pre/post-processing is inlined.

Model file names in data/ (produced by scripts/export_rtmpose_onnx.py):
    rtmpose-tiny.onnx
    rtmpose-s.onnx

Usage:
    detector = RTMPoseDetector(model_size="tiny")
    results  = detector.detect(frame_bgr, bboxes_xyxy)
    # results: list of dicts per person, each with 'keypoints' and 'scores'

Keypoint order (COCO-17):
    0  nose          5  left_shoulder   10 right_wrist
    1  left_eye      6  right_shoulder  11 left_hip
    2  right_eye     7  left_elbow      12 right_hip
    3  left_ear      8  right_elbow     13 left_knee
    4  right_ear     9  left_wrist      14 right_knee
                                        15 left_ankle
                                        16 right_ankle
"""

import os
import math
import time
import numpy as np
import cv2
import onnxruntime


# ── COCO-17 metadata ─────────────────────────────────────────────────────────

KEYPOINT_NAMES = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]

# Pairs for skeleton drawing: (kp_a_idx, kp_b_idx)
SKELETON = [
    (0, 1), (0, 2), (1, 3), (2, 4),           # face
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),  # arms
    (5, 11), (6, 12), (11, 12),                # torso
    (11, 13), (13, 15), (12, 14), (14, 16),    # legs
]

# ImageNet normalisation (matches RTMPose data preprocessor)
_MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32)
_STD  = np.array([58.395,  57.12,  57.375], dtype=np.float32)


# ── Geometry helpers (inlined from mmpose/structures/bbox/transforms.py) ─────

def _rotate_point(pt: np.ndarray, angle_rad: float) -> np.ndarray:
    sn, cs = math.sin(angle_rad), math.cos(angle_rad)
    return np.array([cs * pt[0] - sn * pt[1],
                     sn * pt[0] + cs * pt[1]], dtype=np.float32)


def _get_3rd_point(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    direction = a - b
    return b + np.array([-direction[1], direction[0]], dtype=np.float32)


def _get_warp_matrix(center: np.ndarray,
                     scale: np.ndarray,
                     rot_deg: float,
                     output_size) -> np.ndarray:
    """2×3 affine matrix: original image → cropped-and-resized patch."""
    src_w, _  = scale
    dst_w, dst_h = output_size

    rot_rad = math.radians(rot_deg)
    src_dir = _rotate_point(np.array([-src_w * 0.5, 0.], dtype=np.float32), rot_rad)
    dst_dir = np.array([-dst_w * 0.5, 0.], dtype=np.float32)

    src = np.zeros((3, 2), dtype=np.float32)
    src[0] = center
    src[1] = center + src_dir
    src[2] = _get_3rd_point(src[0], src[1])

    dst = np.zeros((3, 2), dtype=np.float32)
    dst[0] = [dst_w * 0.5, dst_h * 0.5]
    dst[1] = dst[0] + dst_dir
    dst[2] = _get_3rd_point(dst[0], dst[1])

    return cv2.getAffineTransform(src, dst)


def _bbox_xyxy_to_cs(x1: float, y1: float, x2: float, y2: float,
                     padding: float = 1.25):
    """Bounding box → (center, scale) with aspect-ratio padding."""
    cx = (x1 + x2) * 0.5
    cy = (y1 + y2) * 0.5
    w  = (x2 - x1) * padding
    h  = (y2 - y1) * padding
    # fix aspect ratio to match model input (W=192, H=256  → ratio = 192/256 = 0.75)
    aspect = INPUT_W / INPUT_H
    if w > h * aspect:
        h = w / aspect
    else:
        w = h * aspect
    return np.array([cx, cy], dtype=np.float32), np.array([w, h], dtype=np.float32)


# ── SimCC post-processing (inlined from mmpose/codecs/utils/post_processing.py) ─

def _simcc_argmax(simcc_x: np.ndarray,
                  simcc_y: np.ndarray) -> tuple:
    """
    Decode SimCC 1-D distributions to keypoint locations and scores.

    Args:
        simcc_x: (K, Wx) or (N, K, Wx)  — x-axis distribution
        simcc_y: (K, Wy) or (N, K, Wy)  — y-axis distribution

    Returns:
        locs  : (..., K, 2)  locations as (x, y) in SimCC index space
        scores: (..., K)     confidence scores (raw logits; visible joints
                             are typically >> 0, invisible near/below 0)
    """
    batched = simcc_x.ndim == 3
    if not batched:
        simcc_x = simcc_x[None]
        simcc_y = simcc_y[None]

    N, K, _ = simcc_x.shape
    # argmax along the last axis: x index in [0, Wx), y index in [0, Wy)
    x_idx = simcc_x.argmax(-1)   # (N, K)
    y_idx = simcc_y.argmax(-1)   # (N, K)

    # Score = max of the two axis peaks (matches C++ RTMPose reference).
    # Using max (not mean) prevents a single low-confidence axis from
    # suppressing a clearly visible joint.
    x_val = simcc_x[np.arange(N)[:, None], np.arange(K)[None, :], x_idx]
    y_val = simcc_y[np.arange(N)[:, None], np.arange(K)[None, :], y_idx]
    scores = np.maximum(x_val, y_val)   # (N, K)

    # locs[:, :, 0] = x (column in SimCC space)
    # locs[:, :, 1] = y (row    in SimCC space)
    locs = np.stack([x_idx, y_idx], axis=-1).astype(np.float32)  # (N, K, 2)

    if not batched:
        locs   = locs[0]
        scores = scores[0]

    return locs, scores


# ── RTMPose model constants ───────────────────────────────────────────────────

INPUT_H = 256
INPUT_W = 192
SIMCC_SPLIT_RATIO = 2.0
NUM_KEYPOINTS = 17

MODEL_FILES = {
    "tiny": "rtmpose-tiny.onnx",
    "s":    "rtmpose-s.onnx",
}


# ── Main class ────────────────────────────────────────────────────────────────

class RTMPoseDetector:
    """RTMPose ONNX keypoint estimator (top-down, COCO-17).

    Args:
        model_size:  ``'tiny'`` or ``'s'``.
        score_thr:   Keypoint confidence threshold (0–1). Keypoints below this
                     are still returned but flagged with low ``score``.
        data_dir:    Directory that contains the .onnx file.
                     Defaults to ``./data``.
        device:      ``'cpu'`` or ``'cuda'``.
    """

    def __init__(self,
                 model_size: str = "tiny",
                 score_thr: float = 0.3,
                 data_dir: str | None = None,
                 device: str = "cpu"):
        self.model_size = model_size
        self.score_thr  = score_thr
        self.enabled    = False
        self.session    = None

        if data_dir is None:
            data_dir = os.path.join(".", "data")

        filename = MODEL_FILES.get(model_size)
        if filename is None:
            raise ValueError(f"Unknown model_size '{model_size}'. "
                             f"Choose from {list(MODEL_FILES)}")

        model_path = os.path.join(data_dir, filename)
        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f"RTMPose model not found at {model_path}. "
                "Run scripts/export_rtmpose_onnx.py first."
            )

        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if device == "cuda"
            else ["CPUExecutionProvider"]
        )
        self.session    = onnxruntime.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.enabled    = True
        self._last_latency_ms: float | None = None
        self._last_call_time:  float | None = None
        print(f"RTMPose-{model_size} loaded from {model_path}")

    # ── public API ────────────────────────────────────────────────────────────

    def detect(self, frame_bgr: np.ndarray,
               bboxes_xyxy: np.ndarray | list) -> list:
        """Run pose estimation on a set of person bounding boxes.

        Args:
            frame_bgr:   BGR image (H, W, 3) uint8.
            bboxes_xyxy: Person bounding boxes as (N, 4) array or list of
                         [x1, y1, x2, y2].

        Returns:
            List of N dicts, one per input bbox::

                {
                    'keypoints': np.ndarray (17, 2),  # (x, y) in image pixels
                    'scores':    np.ndarray (17,),    # per-keypoint confidence
                    'bbox':      [x1, y1, x2, y2],
                }
        """
        if not self.enabled or self.session is None:
            return []

        t0 = time.monotonic()
        
        bboxes = np.asarray(bboxes_xyxy, dtype=np.float32)
        if bboxes.ndim == 1:
            bboxes = bboxes[None]
        if bboxes.shape[0] == 0:
            return []

        results = []
        for bbox in bboxes:
            kps, scores = self._run_single(frame_bgr, bbox)
            results.append({
                "keypoints": kps,
                "scores":    scores,
                "bbox":      bbox.tolist(),
            })
        self._last_latency_ms = (time.monotonic() - t0) * 1000
        self._last_call_time  = time.time()
        return results

    def detect_batch(self, frame_bgr: np.ndarray,
                     bboxes_xyxy: np.ndarray | list) -> list:
        """Same as :meth:`detect` but processes all bboxes in one ONNX call.

        More efficient when many persons are present in a single frame.
        """
        if not self.enabled or self.session is None:
            return []

        bboxes = np.asarray(bboxes_xyxy, dtype=np.float32)
        if bboxes.ndim == 1:
            bboxes = bboxes[None]
        if bboxes.shape[0] == 0:
            return []

        batch_imgs, warp_mats = [], []
        for bbox in bboxes:
            crop, warp_inv = self._preprocess(frame_bgr, bbox)
            batch_imgs.append(crop)
            warp_mats.append(warp_inv)

        inp = np.stack(batch_imgs, axis=0)          # (N, 3, H, W)
        simcc_x, simcc_y = self.session.run(
            None, {self.input_name: inp}
        )                                             # (N, 17, Wx/Wy)

        results = []
        for i, bbox in enumerate(bboxes):
            kps, scores = self._decode(simcc_x[i], simcc_y[i], warp_mats[i])
            results.append({
                "keypoints": kps,
                "scores":    scores,
                "bbox":      bbox.tolist(),
            })
        return results

    # ── drawing helper ────────────────────────────────────────────────────────

    def draw(self, frame_bgr: np.ndarray, results: list,
             kp_radius: int = 4,
             kp_color=(0, 255, 0),
             sk_color=(255, 128, 0),
             score_thr: float | None = None) -> np.ndarray:
        """Draw keypoints and skeleton on a copy of *frame_bgr*."""
        vis   = frame_bgr.copy()
        thr   = score_thr if score_thr is not None else self.score_thr

        for res in results:
            kps    = res["keypoints"]
            scores = res["scores"]
            for a, b in SKELETON:
                if scores[a] >= thr and scores[b] >= thr:
                    pt1 = (int(kps[a, 0]), int(kps[a, 1]))
                    pt2 = (int(kps[b, 0]), int(kps[b, 1]))
                    cv2.line(vis, pt1, pt2, sk_color, 2, cv2.LINE_AA)
            for k in range(NUM_KEYPOINTS):
                if scores[k] >= thr:
                    pt = (int(kps[k, 0]), int(kps[k, 1]))
                    cv2.circle(vis, pt, kp_radius, kp_color, -1, cv2.LINE_AA)
        return vis

    # ── runtime config ────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled and (self.session is not None)

    def is_enabled(self) -> bool:
        return self.enabled

    def get_status(self) -> dict:
        last_call_s = round(time.time() - self._last_call_time, 1) if self._last_call_time else None
        return {
            "enabled":    self.enabled,
            "model_size": self.model_size,
            "input_size": (INPUT_W, INPUT_H),
            "score_thr":  self.score_thr,
            "latency_ms": self._last_latency_ms,
            "last_call_s": last_call_s,
        }

    # ── internals ─────────────────────────────────────────────────────────────

    def _run_single(self, frame_bgr: np.ndarray,
                    bbox: np.ndarray) -> tuple:
        crop, warp_inv = self._preprocess(frame_bgr, bbox)
        inp = crop[None]   # (1, 3, H, W)
        simcc_x, simcc_y = self.session.run(
            None, {self.input_name: inp}
        )
        return self._decode(simcc_x[0], simcc_y[0], warp_inv)

    def _preprocess(self, frame_bgr: np.ndarray,
                    bbox: np.ndarray) -> tuple:
        """Crop & normalise a single person region.

        Returns:
            crop     : float32 (3, H, W) ready for ONNX
            warp_inv : (2, 3) inverse affine matrix (crop → image)
        """
        x1, y1, x2, y2 = bbox[:4]
        center, scale = _bbox_xyxy_to_cs(x1, y1, x2, y2)

        warp = _get_warp_matrix(center, scale, 0., (INPUT_W, INPUT_H))
        patch = cv2.warpAffine(
            frame_bgr, warp, (INPUT_W, INPUT_H), flags=cv2.INTER_LINEAR
        )

        # BGR → RGB, float32, normalise
        patch = patch[:, :, ::-1].astype(np.float32)
        patch = (patch - _MEAN) / _STD
        patch = patch.transpose(2, 0, 1)   # HWC → CHW

        # Inverse affine for keypoint back-projection
        warp_inv = cv2.invertAffineTransform(warp)

        return patch, warp_inv

    def _decode(self, simcc_x: np.ndarray, simcc_y: np.ndarray,
                warp_inv: np.ndarray) -> tuple:
        """Decode SimCC outputs → image-space keypoints.

        Coordinate conventions
        ----------------------
        SimCC indices after argmax:
            simcc_x → x_simcc in [0, INPUT_W * split_ratio)  (horizontal)
            simcc_y → y_simcc in [0, INPUT_H * split_ratio)  (vertical)
        After /= split_ratio:
            x_crop in [0, INPUT_W=192)  — column in the warped crop
            y_crop in [0, INPUT_H=256)  — row    in the warped crop
        After warp_inv:
            x_img, y_img — column / row in the original BGR frame  (x, y)

        Args:
            simcc_x  : (17, Wx)  x-axis SimCC logits
            simcc_y  : (17, Wy)  y-axis SimCC logits
            warp_inv : (2, 3)    inverse affine  crop (x,y) → image (x,y)

        Returns:
            keypoints: (17, 2)  float32, each row is (x, y) in image pixels
            scores   : (17,)    float32, raw SimCC logit scores
        """
        # SimCC argmax → (x, y) locations in SimCC index space
        locs, scores = _simcc_argmax(simcc_x, simcc_y)   # (17, 2)

        # SimCC index → crop pixel (x = col, y = row)
        locs /= SIMCC_SPLIT_RATIO   # (17, 2)

        # Homogeneous coords: [x_crop, y_crop, 1]  per keypoint
        ones  = np.ones((NUM_KEYPOINTS, 1), dtype=np.float32)
        locs_h = np.concatenate([locs, ones], axis=1)   # (17, 3)

        # warp_inv (2,3) maps crop (x,y) → image (x,y)
        # (warp_inv @ locs_h.T) is (2,17); .T gives (17,2) with [x_img, y_img]
        kps = (warp_inv @ locs_h.T).T                   # (17, 2)

        return kps.astype(np.float32), scores.astype(np.float32)
