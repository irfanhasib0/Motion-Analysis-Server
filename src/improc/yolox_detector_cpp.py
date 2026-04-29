"""
YOLOX Detector — C++ shared library backend

Wraps libyolox_detector.so via ctypes, exposing the same public interface as
YOLOXDetectorONNX so it can be used as a drop-in replacement.

Build the shared library first:
    cd src/cpp/build
    cmake .. && make yolox_detector

Usage:
    from improc.yolox_detector_cpp import CppYOLOXDetector

    det = CppYOLOXDetector(model_path='data/yn.onnx')
    det.set_enabled(True)
    detections = det.detect(frame_bgr)
"""
import ctypes
import os

# Default shared library path (deployed to src/libs/ by build_cpp.sh)
_DEFAULT_LIB_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'libs', 'libyolox_detector.so'
)

# COCO class names — must match order in yolox_detector.cpp
_COCO_CLASSES = (
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


# ---------------------------------------------------------------------------
# ctypes layout for the C struct Detection (yolox_detector.h)
#
#   struct Detection {
#       int   x1, y1, x2, y2;   // 4 × 4 = 16 bytes
#       float cx, cy;            // 2 × 4 =  8 bytes
#       float score;             //     4 =  4 bytes
#       int   class_id;          //     4 =  4 bytes
#       std::string class_name;  //        = 32 bytes on 64-bit libstdc++
#   };  — total 64 bytes
#
# class_name is a C++ std::string and cannot be read portably via ctypes.
# We skip it and derive the name from class_id in Python.
# ---------------------------------------------------------------------------
class _CDetection(ctypes.Structure):
    _fields_ = [
        ('x1',       ctypes.c_int),
        ('y1',       ctypes.c_int),
        ('x2',       ctypes.c_int),
        ('y2',       ctypes.c_int),
        ('cx',       ctypes.c_float),
        ('cy',       ctypes.c_float),
        ('score',    ctypes.c_float),
        ('class_id', ctypes.c_int),
        ('_padding', ctypes.c_byte * 32),   # std::string on 64-bit libstdc++
    ]


class CppYOLOXDetector:
    """YOLOX detector backed by libyolox_detector.so via ctypes.

    Exposes the same public interface as YOLOXDetectorONNX:
        detect(), set_enabled(), is_enabled(), set_score_threshold(), get_status()
    """

    _MAX_DETS = 300

    def __init__(self,
                 model_path: str,
                 lib_path: str = _DEFAULT_LIB_PATH,
                 input_h: int = 416,
                 input_w: int = 416,
                 score_thr: float = 0.5,
                 nms_thr: float = 0.45,
                 target_class_ids=None):
        self._lib = ctypes.CDLL(os.path.abspath(lib_path))
        self._setup_signatures()

        self._handle = self._lib.yolox_create(
            model_path.encode(),
            ctypes.c_int(input_h),
            ctypes.c_int(input_w),
            ctypes.c_float(score_thr),
            ctypes.c_float(nms_thr),
        )
        if not self._handle:
            raise RuntimeError(f"yolox_create failed for model: {model_path}")

        self._out_buf = (_CDetection * self._MAX_DETS)()
        self._input_shape = (input_h, input_w)
        self._score_thr = score_thr
        self.target_class_ids = set(target_class_ids) if target_class_ids is not None else {0, 2}
        self.enabled = True
        print(f"YOLOX [cpp] loaded from {model_path} "
              f"(input={self._input_shape}, score_thr={score_thr})")

    def _setup_signatures(self):
        lib = self._lib

        lib.yolox_create.restype  = ctypes.c_void_p
        lib.yolox_create.argtypes = [
            ctypes.c_char_p,   # model_path
            ctypes.c_int,      # input_h
            ctypes.c_int,      # input_w
            ctypes.c_float,    # score_thr
            ctypes.c_float,    # nms_thr
        ]

        lib.yolox_destroy.restype  = None
        lib.yolox_destroy.argtypes = [ctypes.c_void_p]

        lib.yolox_set_enabled.restype  = None
        lib.yolox_set_enabled.argtypes = [ctypes.c_void_p, ctypes.c_int]

        lib.yolox_is_enabled.restype  = ctypes.c_int
        lib.yolox_is_enabled.argtypes = [ctypes.c_void_p]

        lib.yolox_detect.restype  = ctypes.c_int
        lib.yolox_detect.argtypes = [
            ctypes.c_void_p,                 # handle
            ctypes.POINTER(ctypes.c_uint8),  # bgr_data
            ctypes.c_int, ctypes.c_int,      # frame_h, frame_w
            ctypes.POINTER(_CDetection),     # out
            ctypes.c_int,                    # max_dets
        ]

        lib.yolox_last_latency_ms.restype  = ctypes.c_float
        lib.yolox_last_latency_ms.argtypes = [ctypes.c_void_p]

    def __del__(self):
        if getattr(self, '_handle', None):
            self._lib.yolox_destroy(self._handle)
            self._handle = None

    # --- Public interface (mirrors YOLOXDetectorONNX) ---

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)
        self._lib.yolox_set_enabled(self._handle, int(enabled))

    def is_enabled(self) -> bool:
        return bool(self._lib.yolox_is_enabled(self._handle))

    def set_score_threshold(self, thr: float):
        self._score_thr = thr

    def detect(self, frame_bgr) -> list:
        """Run detection on a BGR numpy array. Returns list of detection dicts."""
        import numpy as np

        if not self.is_enabled():
            return []

        frame = np.ascontiguousarray(frame_bgr, dtype=np.uint8)
        h, w = frame.shape[:2]
        ptr = frame.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))

        n = self._lib.yolox_detect(
            self._handle, ptr,
            ctypes.c_int(h), ctypes.c_int(w),
            self._out_buf, ctypes.c_int(self._MAX_DETS),
        )
        if n < 0:
            return []

        results = []
        for i in range(n):
            d = self._out_buf[i]
            if d.class_id not in self.target_class_ids:
                continue
            bh = d.y2 - d.y1
            bw = d.x2 - d.x1
            mask = np.zeros((h, w), dtype=np.uint8)
            mask[d.y1:d.y2, d.x1:d.x2] = 255
            cls_name = (_COCO_CLASSES[d.class_id]
                        if 0 <= d.class_id < len(_COCO_CLASSES)
                        else str(d.class_id))
            results.append({
                'bbox':     [d.x1, d.y1, d.x2, d.y2],
                'centroid': [float(d.cy), float(d.cx)],
                'mask':     mask,
                'type':     cls_name,
                'score':    round(float(d.score), 4),
            })
        return results

    @property
    def last_latency_ms(self) -> float:
        return float(self._lib.yolox_last_latency_ms(self._handle))

    def get_status(self) -> dict:
        return {
            'enabled':     self.is_enabled(),
            'backend':     'cpp',
            'input_shape': self._input_shape,
            'score_thr':   self._score_thr,
            'latency_ms':  self.last_latency_ms,
        }
