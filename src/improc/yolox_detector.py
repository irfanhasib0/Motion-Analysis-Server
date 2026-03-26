"""
YOLOX ONNX Detector

Wraps YOLOX-Nano / YOLOX-Tiny ONNX models for object detection.
Returns detection boxes in the same dict format as PersonDetector,
so they integrate directly into the optical flow tracker pipeline.

Requires: onnxruntime (optional dependency — disables gracefully if missing)

COCO class IDs (default targets):
    0 = person, 2 = car

Usage:
    detector = YOLOXDetector(model_path="yolox_nano.onnx")
    detections = detector.detect(frame_bgr)
"""
import os
import numpy as np
import cv2

try:
    import onnxruntime
    ONNXRUNTIME_AVAILABLE = True
except ImportError:
    ONNXRUNTIME_AVAILABLE = False

# --- Inlined YOLOX utilities (avoids heavy import chain from yolox repo) ---

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


class YOLOXDetector:
    # Default input shapes per model variant
    MODEL_CONFIGS = {
        "nano": (416, 416),
        "tiny": (416, 416),
    }

    def __init__(self,
                 model_size="nano",
                 input_shape=None,
                 score_thr=0.5,
                 nms_thr=0.45,
                 target_class_ids=None):
        """
        Args:
            model_size: 'nano' or 'tiny' — sets default input shape & model filename.
            input_shape: Override (H, W) input size. Defaults per model_size.
            score_thr: Confidence threshold for detections.
            nms_thr: IoU threshold for NMS.
            target_class_ids: Set of COCO class IDs to keep. Default {0, 2} (person, car).
        """
        self.enabled = False
        self.session = None
        self.model_size = model_size
        self.score_thr = score_thr
        self.nms_thr = nms_thr
        self.target_class_ids = target_class_ids if target_class_ids is not None else {0, 2}

        if input_shape is not None:
            self.input_shape = tuple(input_shape)
        else:
            self.input_shape = self.MODEL_CONFIGS.get(model_size, (416, 416))

        data_dir = os.path.join(os.path.dirname(__file__), "data")
        model_path = os.path.join(data_dir, f"yolox_{model_size}.onnx")

        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"Warning: YOLOX model not found at {model_path} — detector disabled.")

        self.session = onnxruntime.InferenceSession(model_path)
        self.input_name = self.session.get_inputs()[0].name
        self.enabled = True
        print(f"YOLOX-{model_size} loaded from {model_path} "
                f"(input={self.input_shape}, classes={self.target_class_ids})")
        
    def detect(self, frame_bgr):
        """
        Run YOLOX inference on a BGR frame.

        Returns:
            List of detection dicts compatible with optical flow tracker:
            [{'bbox', 'bbox_xywh', 'centroid', 'mask', 'type'}, ...]
        """
        if not self.enabled or self.session is None:
            return []

        img, ratio = _preproc(frame_bgr, self.input_shape)
        ort_inputs = {self.input_name: img[None, :, :, :]}
        output = self.session.run(None, ort_inputs)
        predictions = _demo_postprocess(output[0], self.input_shape)[0]

        boxes = predictions[:, :4]
        scores = predictions[:, 4:5] * predictions[:, 5:]

        # Convert cx,cy,w,h to x1,y1,x2,y2
        boxes_xyxy = np.empty_like(boxes)
        boxes_xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.0
        boxes_xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.0
        boxes_xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.0
        boxes_xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.0
        boxes_xyxy /= ratio

        dets = _multiclass_nms(boxes_xyxy, scores, nms_thr=self.nms_thr, score_thr=self.score_thr)
        if dets is None:
            return []

        final_boxes = dets[:, :4]
        final_scores = dets[:, 4]
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
                'bbox_xywh': [int(x1 + bw / 2), int(y1 + bh / 2), int(bw), int(bh)],
                'centroid': [y1 + bh / 2, x1 + bw / 2],
                'mask': mask,
                'type': f'{cls_name}',
                'score': round(float(final_scores[i]), 4),
            })

        return detections

    # --- Runtime configuration ---

    def set_enabled(self, enabled):
        self.enabled = enabled and (self.session is not None)

    def is_enabled(self):
        return self.enabled

    def set_target_classes(self, class_ids):
        self.target_class_ids = set(class_ids)

    def get_target_classes(self):
        return self.target_class_ids

    def set_score_threshold(self, thr):
        self.score_thr = thr

    def get_status(self):
        return {
            'enabled': self.enabled,
            'model_size': self.model_size,
            'input_shape': self.input_shape,
            'score_thr': self.score_thr,
            'target_class_ids': list(self.target_class_ids),
        }
