"""Process-scoped singleton factory for shared ML detector instances.

Each process gets at most one YOLOXDetector and one PersonDetector.
ONNX InferenceSession is thread-safe for inference, and Haar cascades
are read-only after load, so sharing across threads is safe.

Usage:
    from improc.shared_detectors import get_shared_yolox, get_shared_person_detector

    tracker = OpticalFlowTracker(
        yolox_detector=get_shared_yolox(),
        person_detector=get_shared_person_detector(enable_face=True),
    )
"""

import threading

_lock = threading.Lock()
_yolox_instance = None
_person_instance = None


def get_shared_yolox(**kwargs):
    """Return the process-scoped YOLOXDetector singleton, creating it on first call."""
    global _yolox_instance
    if _yolox_instance is None:
        with _lock:
            if _yolox_instance is None:
                from improc.yolox_detector import YOLOXDetector
                _yolox_instance = YOLOXDetector(**kwargs)
    return _yolox_instance


def get_shared_person_detector(**kwargs):
    """Return the process-scoped PersonDetector singleton, creating it on first call."""
    global _person_instance
    if _person_instance is None:
        with _lock:
            if _person_instance is None:
                from improc.person_detection import PersonDetector
                _person_instance = PersonDetector(**kwargs)
    return _person_instance
