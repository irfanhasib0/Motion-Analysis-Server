"""Process-scoped singleton factory for shared ML detector instances.

Each process gets at most one YOLOXDetector, one PersonDetector, and one
RTMPoseDetector.  ONNX InferenceSession is thread-safe for inference, and
Haar cascades are read-only after load, so sharing across threads is safe.

Usage:
    from improc.shared_detectors import (
        get_shared_yolox,
        get_shared_cpp_yolox,
        get_shared_person_detector,
        get_shared_rtmpose,
    )

    tracker = OpticalFlowTracker(
        yolox_detector=get_shared_yolox(),
        person_detector=get_shared_person_detector(enable_face=True),
    )
"""

import os
import threading

_lock = threading.Lock()
_yolox_instance = None
_cpp_yolox_instance = None
_person_instance = None
_rtmpose_instance = None

# data/ lives at project root (CWD-relative for portability)
_DATA_DIR = os.path.join('.', 'data')

# Default shared library path (deployed to src/libs/ by build_cpp.sh)
_CPP_LIB_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'libs', 'libyolox_detector.so'
)


def get_shared_yolox(**kwargs):
    """Return the process-scoped YOLOXDetector singleton, creating it on first call."""
    global _yolox_instance
    if _yolox_instance is None:
        with _lock:
            if _yolox_instance is None:
                from improc.yolox_detector import YOLOXDetector
                kwargs.setdefault('data_dir', _DATA_DIR)
                _yolox_instance = YOLOXDetector(**kwargs)
    return _yolox_instance


def get_shared_yolox_cpp(model_path=None, lib_path=_CPP_LIB_PATH, **kwargs):
    """Return the process-scoped CppYOLOXDetector singleton, creating it on first call.

    Args:
        model_path:  Path to the ONNX model file.  If not given, derived from
                     model_size ('nano' → yn.onnx, 'tiny' → yt.onnx).
        lib_path:    Path to libyolox_detector.so.  Defaults to src/cpp/build/.
        model_size:  'nano' (default) or 'tiny' — ignored if model_path is set.
        backend:     Accepted but ignored (the cpp backend is always used here).
        **kwargs:    Forwarded to CppYOLOXDetector (input_h, input_w, score_thr, nms_thr).
    """
    global _cpp_yolox_instance
    if _cpp_yolox_instance is None:
        with _lock:
            if _cpp_yolox_instance is None:
                from improc.yolox_detector_cpp import CppYOLOXDetector
                # Strip kwargs not accepted by CppYOLOXDetector
                kwargs.pop('backend', None)
                model_size = kwargs.pop('model_size', 'nano')
                if model_path is None:
                    stem = 'yn' if model_size == 'nano' else 'yt'
                    model_path = os.path.abspath(
                        os.path.join(_DATA_DIR, f'{stem}.onnx')
                    )
                _cpp_yolox_instance = CppYOLOXDetector(
                    model_path=model_path,
                    lib_path=lib_path,
                    **kwargs,
                )
    return _cpp_yolox_instance


def get_shared_person_detector(**kwargs):
    """Return the process-scoped PersonDetector singleton, creating it on first call."""
    global _person_instance
    if _person_instance is None:
        with _lock:
            if _person_instance is None:
                from improc.person_detection import PersonDetector
                kwargs.setdefault('data_dir', _DATA_DIR)
                _person_instance = PersonDetector(**kwargs)
    return _person_instance


def get_shared_rtmpose(**kwargs):
    """Return the process-scoped RTMPoseDetector singleton, creating it on first call.

    Keyword args are forwarded to RTMPoseDetector on first construction only.
    Subsequent calls ignore kwargs and return the existing instance.
    """
    global _rtmpose_instance
    if _rtmpose_instance is None:
        with _lock:
            if _rtmpose_instance is None:
                from improc.rtmpose_detector import RTMPoseDetector
                kwargs.setdefault('data_dir', _DATA_DIR)
                _rtmpose_instance = RTMPoseDetector(**kwargs)
    return _rtmpose_instance
