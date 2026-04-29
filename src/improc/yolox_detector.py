"""
YOLOX Detector — ONNX backend

Wraps YOLOX-Nano / YOLOX-Tiny models for object detection.
Returns detection boxes in the same dict format as PersonDetector,
so they integrate directly into the optical flow tracker pipeline.

Class hierarchy:
    _YOLOXBase          — shared pre/postprocessing, detect(), runtime config
    YOLOXDetectorONNX   — onnxruntime backend  (requires: onnxruntime)
    YOLOXDetector(...)  — factory: returns YOLOXDetectorONNX

Model files expected in data_dir:
    ONNX:  yn.onnx / yt.onnx

COCO class IDs (default targets):
    0 = person, 2 = car

Usage:
    det = YOLOXDetector(model_size='nano')
    detections = det.detect(frame_bgr)
"""
import os
import time
from abc import ABC, abstractmethod
import numpy as np
import cv2

try:
    import onnxruntime as _ort
except ImportError:
    _ort = None

# ---------------------------------------------------------------------------
# Inlined YOLOX utilities (avoids heavy import chain from yolox repo)
# ---------------------------------------------------------------------------

COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)


def _preproc(img, input_size, swap=(2, 0, 1)):
    if len(img.shape) == 3:
        padded = np.ones((input_size[0], input_size[1], 3), dtype=np.uint8) * 114
    else:
        padded = np.ones(input_size, dtype=np.uint8) * 114
    r = min(input_size[0] / img.shape[0], input_size[1] / img.shape[1])
    resized = cv2.resize(img, (int(img.shape[1] * r), int(img.shape[0] * r)),
                         interpolation=cv2.INTER_LINEAR).astype(np.uint8)
    padded[:int(img.shape[0] * r), :int(img.shape[1] * r)] = resized
    padded = padded.transpose(swap)
    padded = np.ascontiguousarray(padded, dtype=np.float32)
    return padded, r


def _demo_postprocess(outputs, img_size, p6=False):
    grids, expanded_strides = [], []
    strides = [8, 16, 32, 64] if p6 else [8, 16, 32]
    hsizes = [img_size[0] // s for s in strides]
    wsizes = [img_size[1] // s for s in strides]
    for hsize, wsize, stride in zip(hsizes, wsizes, strides):
        xv, yv = np.meshgrid(np.arange(wsize), np.arange(hsize))
        grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
        grids.append(grid)
        expanded_strides.append(np.full((*grid.shape[:2], 1), stride))
    grids = np.concatenate(grids, 1)
    expanded_strides = np.concatenate(expanded_strides, 1)
    outputs[..., :2] = (outputs[..., :2] + grids) * expanded_strides
    outputs[..., 2:4] = np.exp(outputs[..., 2:4]) * expanded_strides
    return outputs


def _nms(boxes, scores, nms_thr):
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(ovr <= nms_thr)[0] + 1]
    return keep


def _multiclass_nms(boxes, scores, nms_thr, score_thr):
    cls_inds = scores.argmax(1)
    cls_scores = scores[np.arange(len(cls_inds)), cls_inds]
    valid = cls_scores > score_thr
    if valid.sum() == 0:
        return None
    keep = _nms(boxes[valid], cls_scores[valid], nms_thr)
    if not keep:
        return None
    v_boxes, v_scores, v_cls = boxes[valid], cls_scores[valid], cls_inds[valid]
    return np.concatenate([v_boxes[keep], v_scores[keep, None], v_cls[keep, None]], 1)


# ---------------------------------------------------------------------------
# Base class — shared state, detect(), and runtime configuration
# ---------------------------------------------------------------------------

class _YOLOXBase(ABC):
    """Abstract base for YOLOX detectors.

    Subclasses implement _run_inference(img_chw) → np.ndarray of shape (N, 85).
    All pre/postprocessing, NMS, and the public API live here.
    """

    # Default input shapes per model variant
    MODEL_CONFIGS = {
        "nano": (416, 416),
        "tiny": (416, 416),
    }

    def __init__(self,
                 model_size: str = "nano",
                 input_shape=None,
                 score_thr: float = 0.5,
                 nms_thr: float = 0.45,
                 target_class_ids=None):
        self.model_size = model_size
        self.score_thr = score_thr
        self.nms_thr = nms_thr
        self.target_class_ids = set(target_class_ids) if target_class_ids is not None else {0, 2}
        self.enabled = False
        self._last_latency_ms: float | None = None
        self._last_call_time:  float | None = None

        if input_shape is not None:
            self.input_shape = tuple(input_shape)
        else:
            self.input_shape = self.MODEL_CONFIGS.get(model_size, (416, 416))

    @property
    @abstractmethod
    def backend(self) -> str:
        """Return the backend name string, e.g. 'onnx' or 'ncnn'."""

    @abstractmethod
    def _run_inference(self, img_chw: np.ndarray) -> np.ndarray:
        """Run model inference.

        Args:
            img_chw: Preprocessed float32 CHW array, shape (3, H, W).

        Returns:
            np.ndarray of shape (N, 85) — raw YOLOX predictions before grid decode.
        """

    def detect(self, frame_bgr: np.ndarray) -> list:
        """Run YOLOX on a BGR frame.

        Returns:
            List of detection dicts: [{'bbox', 'centroid', 'mask', 'type', 'score'}, ...]
        """
        if not self.enabled:
            return []

        t0 = time.monotonic()

        img, ratio = _preproc(frame_bgr, self.input_shape)
        raw_preds = self._run_inference(img)                       # (N, 85)
        predictions = _demo_postprocess(raw_preds[None], self.input_shape)[0]

        boxes = predictions[:, :4]
        scores = predictions[:, 4:5] * predictions[:, 5:]

        boxes_xyxy = np.empty_like(boxes)
        boxes_xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.0
        boxes_xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.0
        boxes_xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.0
        boxes_xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.0
        boxes_xyxy /= ratio

        dets = _multiclass_nms(boxes_xyxy, scores, nms_thr=self.nms_thr, score_thr=self.score_thr)
        if dets is None:
            self._last_latency_ms = (time.monotonic() - t0) * 1000
            self._last_call_time  = time.time()
            return []

        final_boxes    = dets[:, :4]
        final_scores   = dets[:, 4]
        final_cls_inds = dets[:, 5].astype(int)

        h, w = frame_bgr.shape[:2]
        detections = []
        for i in range(len(final_boxes)):
            cls_id = int(final_cls_inds[i])
            if cls_id not in self.target_class_ids:
                continue
            x1 = max(0, int(final_boxes[i, 0]))
            y1 = max(0, int(final_boxes[i, 1]))
            x2 = min(w, int(final_boxes[i, 2]))
            y2 = min(h, int(final_boxes[i, 3]))
            bw, bh = x2 - x1, y2 - y1
            if bw <= 0 or bh <= 0:
                continue
            mask = np.zeros((h, w), dtype=np.uint8)
            mask[y1:y2, x1:x2] = 255
            cls_name = COCO_CLASSES[cls_id] if cls_id < len(COCO_CLASSES) else str(cls_id)
            detections.append({
                'bbox': [x1, y1, x2, y2],
                'centroid': [y1 + bh / 2, x1 + bw / 2],
                'mask': mask,
                'type': cls_name,
                'score': round(float(final_scores[i]), 4),
            })

        self._last_latency_ms = (time.monotonic() - t0) * 1000
        self._last_call_time  = time.time()
        return detections

    # --- Runtime configuration ---

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)

    def is_enabled(self) -> bool:
        return self.enabled

    def set_target_classes(self, class_ids):
        self.target_class_ids = set(class_ids)

    def get_target_classes(self):
        return self.target_class_ids

    def set_score_threshold(self, thr: float):
        self.score_thr = thr

    def get_status(self) -> dict:
        last_call_s = round(time.time() - self._last_call_time, 1) if self._last_call_time else None
        return {
            'enabled':          self.enabled,
            'backend':          self.backend,
            'model_size':       self.model_size,
            'input_shape':      self.input_shape,
            'score_thr':        self.score_thr,
            'target_class_ids': list(self.target_class_ids),
            'latency_ms':       self._last_latency_ms,
            'last_call_s':      last_call_s,
        }


# ---------------------------------------------------------------------------
# ONNX backend
# ---------------------------------------------------------------------------

class YOLOXDetectorONNX(_YOLOXBase):
    """YOLOX detector using onnxruntime. Requires: pip install onnxruntime."""

    @property
    def backend(self) -> str:
        return 'onnx'

    def __init__(self, model_size="nano", input_shape=None, score_thr=0.5,
                 nms_thr=0.45, target_class_ids=None, data_dir=None):
        super().__init__(model_size, input_shape, score_thr, nms_thr, target_class_ids)

        if _ort is None:
            raise ImportError("onnxruntime is not installed.")

        if data_dir is None:
            data_dir = os.path.join('.', 'data')

        model_path = os.path.join(data_dir, f"y{model_size[0]}.onnx")
        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"YOLOX ONNX model not found: {model_path}")

        available = _ort.get_available_providers()
        if 'XNNPACKExecutionProvider' in available:
            print("YOLOX: XNNPACKExecutionProvider detected — using XNNPACK")
        providers = (['XNNPACKExecutionProvider'] if 'XNNPACKExecutionProvider' in available else []) \
                    + ['CPUExecutionProvider']
        self._session = _ort.InferenceSession(model_path, providers=providers)
        self._input_name = self._session.get_inputs()[0].name
        self.enabled = True
        active_ep = self._session.get_providers()[0]
        print(f"YOLOX-{model_size} [onnx] loaded from {model_path} "
              f"(input={self.input_shape}, ep={active_ep}, classes={self.target_class_ids})")

    def _run_inference(self, img_chw: np.ndarray) -> np.ndarray:
        output = self._session.run(None, {self._input_name: img_chw[None]})
        return output[0][0]   # (N, 85)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def YOLOXDetector(model_size="nano", input_shape=None, score_thr=0.5,
                  nms_thr=0.45, target_class_ids=None, data_dir=None,
                  **kwargs) -> _YOLOXBase:
    """Return a YOLOXDetectorONNX instance."""
    return YOLOXDetectorONNX(
        model_size=model_size, input_shape=input_shape,
        score_thr=score_thr, nms_thr=nms_thr,
        target_class_ids=target_class_ids, data_dir=data_dir,
        **kwargs,
    )

