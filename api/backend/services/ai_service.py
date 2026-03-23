"""
AI Service — Tracker process management for optical flow analysis.

Provides two modes:
  - multiprocess: Tracker runs in a dedicated process (bypasses GIL)
  - sequential:   Tracker runs inline in the video thread (current behavior)

Controlled by the 'tracker_multiprocess' flag on CameraService.
"""

import logging
import multiprocessing
import os
import queue
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import numpy as np

PROJECT_SRC_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))
if PROJECT_SRC_PATH not in sys.path:
    sys.path.append(PROJECT_SRC_PATH)

from services.colors import Colors

logger = logging.getLogger(__name__)


# ─── Data containers (pickle-safe) ──────────────────────────────────────────

@dataclass
class TrackerInput:
    """Sent from the video thread to the tracker process."""
    frame: np.ndarray
    frame_capture_time: float
    frame_index: int
    return_pts: bool = True


@dataclass
class TrackerResult:
    """Returned from the tracker process to the video thread."""
    points_dict: dict             # {traj_id: {vel, mean_vel, channel, bg_diff}}
    flow_pts: dict                # Optical flow keypoints payload
    frame_capture_time: float     # Preserved for ring-buffer timestamping
    frame_index: int


@dataclass
class TrackerConfigUpdate:
    """Config change message sent to the tracker process."""
    action: str                   # e.g. 'set_detection_method', 'restart', ...
    value: Any = None             # Action-specific payload


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _extract_dicts(detect_output) -> tuple:
    """Extract (points_dict, flow_pts) from tracker.detect() output,
    discarding the visualization frame to minimize pickle overhead."""
    if not isinstance(detect_output, tuple) or len(detect_output) == 0:
        return {}, {}

    points_dict = {}
    pts_payload = {}
    # detect_output[0] is the viz frame — skip it
    for item in detect_output[1:]:
        if isinstance(item, dict):
            if not points_dict and any(
                isinstance(v, dict) and ('vel' in v or 'mean_vel' in v)
                for v in item.values()
            ):
                points_dict = item
                continue
            if not pts_payload and any(
                isinstance(v, dict) and ('keypoints_1' in v or 'keypoints_2' in v)
                for v in item.values()
            ):
                pts_payload = item
    return points_dict, pts_payload


# ─── Worker function (runs in child process) ────────────────────────────────

def _tracker_worker(
    input_queue: multiprocessing.Queue,
    output_queue: multiprocessing.Queue,
    config_queue: multiprocessing.Queue,
    tracker_kwargs: dict,
    stop_event,  # multiprocessing.Event
):
    """
    Target for the tracker subprocess.

    Owns the OpticalFlowTracker instance entirely — no shared state with main
    process.  Communicates via three queues:

      input_queue   – TrackerInput (frames to process)
      output_queue  – TrackerResult (detection results)
      config_queue  – TrackerConfigUpdate (runtime config changes)
    """
    # Import here so the heavy OpenCV / model loading happens in the child
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))
    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    from improc.optical_flow import OpticalFlowTracker

    tracker = OpticalFlowTracker(**tracker_kwargs)
    logger.info(f"[TrackerProcess] Started with config: {list(tracker_kwargs.keys())}")

    while not stop_event.is_set():
        # 1) Apply pending config changes (non-blocking drain)
        try:
            while True:
                update: TrackerConfigUpdate = config_queue.get_nowait()
                _apply_config(tracker, update)
        except queue.Empty:
            pass

        # 2) Get next frame to process
        try:
            msg = input_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if msg is None:  # Poison pill
            break

        inp: TrackerInput = msg
        try:
            detect_output = tracker.detect(inp.frame, return_pts=inp.return_pts)
            # Strip the visualization frame to avoid pickling ~920KB per frame.
            # detect_output is (viz_frame, points_dict, flow_pts) — we only need dicts.
            points_dict, flow_pts = _extract_dicts(detect_output)
            result = TrackerResult(
                points_dict=points_dict,
                flow_pts=flow_pts,
                frame_capture_time=inp.frame_capture_time,
                frame_index=inp.frame_index,
            )
            # Non-blocking put — if output queue is full, drop oldest to make room
            try:
                output_queue.put_nowait(result)
            except queue.Full:
                try:
                    output_queue.get_nowait()  # discard oldest
                except queue.Empty:
                    pass
                try:
                    output_queue.put(result, timeout=0.1)
                except queue.Full:
                    pass  # drop this result if still full
        except Exception:
            logger.exception("[TrackerProcess] detect() failed")

    logger.info("[TrackerProcess] Exiting")


def _apply_config(tracker, update: TrackerConfigUpdate):
    """Apply a single config change to the tracker instance (runs in child process)."""
    action = update.action
    value = update.value
    try:
        if action == 'set_detection_method':
            tracker.set_detection_method(value)
        elif action == 'set_person_detection':
            tracker.set_person_detection_enabled(value)
        elif action == 'set_face_detection':
            tracker.set_face_detection_enabled(value)
        elif action == 'set_body_detection':
            tracker.set_body_detection_enabled(value)
        elif action == 'restart':
            tracker.restart()
        else:
            logger.warning(f"[TrackerProcess] Unknown config action: {action}")
        logger.info(f"[TrackerProcess] Applied config: {action}={value}")
    except Exception:
        logger.exception(f"[TrackerProcess] Failed to apply config: {action}={value}")


# ─── AIService — manages tracker processes ──────────────────────────────────

class AIService:
    """
    Manages per-camera tracker processes (or inline trackers for sequential mode).

    Attributes stored per camera_id:
        _tracker_processes      – multiprocessing.Process
        _tracker_input_queues   – frames → tracker
        _tracker_output_queues  – results ← tracker
        _tracker_config_queues  – config updates → tracker
        _tracker_stop_events    – clean shutdown signal
    """

    INPUT_QUEUE_SIZE = 2    # Small buffer — drop frames if tracker is behind
    OUTPUT_QUEUE_SIZE = 2
    CONFIG_QUEUE_SIZE = 8

    def __init__(self):
        # Per-camera multiprocessing resources
        self._tracker_processes: Dict[str, Any] = {}
        self._tracker_input_queues: Dict[str, Any] = {}
        self._tracker_output_queues: Dict[str, Any] = {}
        self._tracker_config_queues: Dict[str, Any] = {}
        self._tracker_stop_events: Dict[str, Any] = {}

        # Latest result cache (video thread reads this)
        self._latest_tracker_result: Dict[str, Optional[TrackerResult]] = {}

    # ── lifecycle ────────────────────────────────────────────────────────

    def start_tracker_process(self, camera_id: str, tracker_kwargs: dict) -> bool:
        """Spawn a tracker subprocess for *camera_id*."""
        if camera_id in self._tracker_processes:
            logger.warning(f"Tracker process already running for {camera_id}")
            return True

        stop_event = multiprocessing.Event()
        input_q = multiprocessing.Queue(maxsize=self.INPUT_QUEUE_SIZE)
        output_q = multiprocessing.Queue(maxsize=self.OUTPUT_QUEUE_SIZE)
        config_q = multiprocessing.Queue(maxsize=self.CONFIG_QUEUE_SIZE)

        proc = multiprocessing.Process(
            target=_tracker_worker,
            args=(input_q, output_q, config_q, tracker_kwargs, stop_event),
            daemon=True,
            name=f"tracker-{camera_id}",
        )
        proc.start()

        self._tracker_processes[camera_id] = proc
        self._tracker_input_queues[camera_id] = input_q
        self._tracker_output_queues[camera_id] = output_q
        self._tracker_config_queues[camera_id] = config_q
        self._tracker_stop_events[camera_id] = stop_event

        logger.info(f"{Colors.GREEN}Tracker process started for {camera_id} (pid={proc.pid}){Colors.RESET}")
        return True

    def stop_tracker_process(self, camera_id: str) -> None:
        """Stop the tracker subprocess for *camera_id* (if running)."""
        stop_event = self._tracker_stop_events.pop(camera_id, None)
        if stop_event:
            stop_event.set()

        input_q = self._tracker_input_queues.pop(camera_id, None)
        if input_q:
            try:
                input_q.put_nowait(None)  # Poison pill
            except Exception:
                pass

        proc = self._tracker_processes.pop(camera_id, None)
        if proc:
            proc.join(timeout=5)
            if proc.is_alive():
                logger.warning(f"{Colors.RED}Force-terminating tracker process for {camera_id}{Colors.RESET}")
                proc.terminate()
                proc.join(timeout=2)

        # Drain and close queues
        for q in (input_q, self._tracker_output_queues.pop(camera_id, None),
                  self._tracker_config_queues.pop(camera_id, None)):
            if q:
                try:
                    while not q.empty():
                        q.get_nowait()
                    q.close()
                    q.join_thread()
                except Exception:
                    pass

        self._latest_tracker_result.pop(camera_id, None)
        logger.info(f"{Colors.YELLOW}Tracker process stopped for {camera_id}{Colors.RESET}")

    def is_tracker_process_alive(self, camera_id: str) -> bool:
        proc = self._tracker_processes.get(camera_id)
        return proc is not None and proc.is_alive()

    # ── frame I/O (called from video thread) ─────────────────────────────

    def submit_frame(self, camera_id: str, frame: np.ndarray,
                     frame_capture_time: float, frame_index: int) -> bool:
        """
        Submit a frame to the tracker process (non-blocking).
        Returns True if frame was enqueued, False if dropped.
        """
        input_q = self._tracker_input_queues.get(camera_id)
        if not input_q:
            return False

        inp = TrackerInput(
            frame=frame,
            frame_capture_time=frame_capture_time,
            frame_index=frame_index,
        )
        try:
            input_q.put_nowait(inp)
            return True
        except queue.Full:
            # Tracker is behind — drop this frame (graceful degradation)
            return False

    def poll_result(self, camera_id: str) -> Optional[TrackerResult]:
        """
        Non-blocking poll for the latest tracker result.
        Drains the output queue and returns only the freshest result.
        Caches it in _latest_tracker_result for repeat reads.
        """
        output_q = self._tracker_output_queues.get(camera_id)
        if not output_q:
            return self._latest_tracker_result.get(camera_id)

        latest = None
        try:
            while True:
                latest = output_q.get_nowait()
        except queue.Empty:
            pass

        if latest is not None:
            self._latest_tracker_result[camera_id] = latest

        return self._latest_tracker_result.get(camera_id)

    def get_cached_result(self, camera_id: str) -> Optional[TrackerResult]:
        """Return the last polled result without touching the queue."""
        return self._latest_tracker_result.get(camera_id)

    # ── config updates (called from API route / camera_service) ──────────

    def send_config_update(self, camera_id: str, action: str, value: Any = None) -> bool:
        """
        Send a config change to the tracker process.
        Returns False if no process running or queue is full.
        """
        config_q = self._tracker_config_queues.get(camera_id)
        if not config_q:
            return False
        try:
            config_q.put_nowait(TrackerConfigUpdate(action=action, value=value))
            return True
        except queue.Full:
            logger.warning(f"Config queue full for {camera_id}, dropping: {action}={value}")
            return False

    # ── cleanup ──────────────────────────────────────────────────────────

    def stop_all(self) -> None:
        """Stop all tracker processes (e.g. on server shutdown)."""
        for camera_id in list(self._tracker_processes.keys()):
            self.stop_tracker_process(camera_id)
