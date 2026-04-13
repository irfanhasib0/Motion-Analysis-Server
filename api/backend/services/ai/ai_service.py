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
from multiprocessing.shared_memory import SharedMemory
from typing import Any, Dict, Optional, Tuple

import numpy as np

PROJECT_SRC_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'src'))
if PROJECT_SRC_PATH not in sys.path:
    sys.path.append(PROJECT_SRC_PATH)

from services.streaming.colors import Colors
from improc.optical_flow import OpticalFlowTracker

logger = logging.getLogger(__name__)


# ─── Data containers (pickle-safe) ──────────────────────────────────────────

# Number of pre-allocated SHM slots per camera.
# = INPUT_QUEUE_SIZE(2) + 1 being processed + 1 being written = 4 total.
SHM_RING_SIZE = 4


@dataclass
class TrackerSetup:
    """Sent once (before the first TrackerInput) when the SHM ring is
    (re-)allocated.  The worker opens all N slots at startup so no
    shm_open/mmap syscalls happen on the hot path."""
    shm_names: list     # length == SHM_RING_SIZE
    slot_shape: tuple
    slot_dtype_str: str


@dataclass
class TrackerInput:
    """Metadata envelope sent from the video thread to the tracker subprocess.

    The raw frame lives in a *pre-allocated* fixed SHM ring slot instead of a
    freshly-created segment.  The worker already has every slot mapped; no
    shm_open/ftruncate/mmap/unlink occurs on the hot path — only a memcpy and
    an integer index crossing the queue socket.

    Lifecycle:
      • On first submit (or frame-size change): produce a TrackerSetup message
        first, then start sending TrackerInput with slot_index.
      • Producer writes frame into slots[slot_index % SHM_RING_SIZE] before
        enqueuing; advances counter only on successful put_nowait.
      • Worker reads directly from its pre-mapped slot handle; never unlinks.
      • Slots are unlinked in bulk when the tracker process is stopped.
    """
    slot_index: int         # index into the pre-allocated SHM ring
    shape: tuple            # frame shape e.g. (480, 640, 3)
    dtype_str: str          # numpy dtype string e.g. '|u1'
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

    tracker = OpticalFlowTracker(**tracker_kwargs)
    logger.info(f"[TrackerProcess] Started with config: {list(tracker_kwargs.keys())}")

    # Pre-mapped SHM ring slots — populated on first TrackerSetup message.
    # We keep them open for the full process lifetime; no per-frame mmap.
    shm_slots: list = []          # List[SharedMemory]
    slot_shape = None
    slot_dtype = None

    while not stop_event.is_set():
        # 1) Apply pending config changes (non-blocking drain)
        try:
            while True:
                update: TrackerConfigUpdate = config_queue.get_nowait()
                _apply_config(tracker, update)
        except queue.Empty:
            pass

        # 2) Get next message
        try:
            msg = input_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if msg is None:  # Poison pill
            break

        # ── One-time setup: open all ring slots ──
        if isinstance(msg, TrackerSetup):
            for s in shm_slots:
                try:
                    s.close()
                except Exception:
                    pass
            shm_slots = []
            try:
                shm_slots = [SharedMemory(name=n) for n in msg.shm_names]
                slot_shape = msg.slot_shape
                slot_dtype = np.dtype(msg.slot_dtype_str)
                logger.info(f"[TrackerProcess] SHM ring ready: {len(shm_slots)} slots {slot_shape} {slot_dtype}")
            except Exception:
                logger.exception("[TrackerProcess] Failed to open SHM ring slots")
            continue

        # ── Normal frame: read from pre-mapped slot (zero syscalls) ──
        inp: TrackerInput = msg
        if not shm_slots:
            logger.warning("[TrackerProcess] Received TrackerInput before TrackerSetup — dropping")
            continue
        try:
            shm = shm_slots[inp.slot_index]
            frame = np.ndarray(inp.shape, dtype=np.dtype(inp.dtype_str), buffer=shm.buf)
            detect_output = tracker.detect(frame, return_pts=inp.return_pts)
            # Strip the visualization frame — we only need the two dict payloads.
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
        # Note: no close/unlink — slots are reused until the process stops.

    # Clean up pre-mapped slots on exit
    for s in shm_slots:
        try:
            s.close()
        except Exception:
            pass
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
    Unified tracker management — handles both multiprocess and sequential modes.

    Callers use the same interface regardless of mode:
        ensure_tracker(camera_id, tracker_kwargs)  – start tracker
        submit_frame(camera_id, frame, ...)        – feed a frame
        poll_result(camera_id)                     – get latest result
        stop_tracker(camera_id)                    – cleanup

    In *multiprocess* mode, detect() runs in a child process via queues.
    In *sequential* mode, detect() runs inline inside submit_frame().
    """

    INPUT_QUEUE_SIZE = 2    # Small buffer — drop frames if tracker is behind
    OUTPUT_QUEUE_SIZE = 2
    CONFIG_QUEUE_SIZE = 8

    def __init__(self, shm_ring_size: int = SHM_RING_SIZE):
        # Number of pre-allocated SHM slots per camera.  Defaults to the
        # module constant but callers should pass frame_rbf_len from system.yaml
        # so the ring matches the video ring buffer depth exactly.
        self._shm_ring_size: int = max(2, shm_ring_size)
        self._use_multiprocess: bool = False

        # Per-camera multiprocessing resources
        self._tracker_processes: Dict[str, Any] = {}
        self._tracker_input_queues: Dict[str, Any] = {}
        self._tracker_output_queues: Dict[str, Any] = {}
        self._tracker_config_queues: Dict[str, Any] = {}
        self._tracker_stop_events: Dict[str, Any] = {}

        # Pre-allocated SHM ring slots (fixed, reused across frames)
        self._shm_slots: Dict[str, list] = {}           # camera_id -> List[SharedMemory]
        self._shm_slot_nbytes: Dict[str, int] = {}      # camera_id -> slot size in bytes
        self._shm_slot_counter: Dict[str, int] = {}     # camera_id -> next write slot

        # Per-camera sequential-mode trackers
        self._inline_trackers: Dict[str, Any] = {}

        # Tracker kwargs cached for multiprocess mode introspection
        self._tracker_kwargs: Dict[str, dict] = {}

        # Latest result cache (video thread reads this)
        self._latest_tracker_result: Dict[str, Optional[TrackerResult]] = {}

        # Latency tracking: wall-clock timestamps of last submit and last result
        self._last_submit_time: Dict[str, float] = {}   # camera_id -> time.time()
        self._last_result_time: Dict[str, float] = {}   # camera_id -> time.time()
        self._last_ai_latency:  Dict[str, float] = {}   # camera_id -> seconds

    # ── configuration ────────────────────────────────────────────────────

    def set_multiprocess(self, enabled: bool) -> None:
        """Set whether new trackers should use multiprocess mode."""
        self._use_multiprocess = enabled

    @property
    def use_multiprocess(self) -> bool:
        return self._use_multiprocess

    # ── unified lifecycle ────────────────────────────────────────────────

    def start_ai_tracker(self, camera_id: str, tracker_kwargs: dict) -> bool:
        """Ensure a tracker is running for *camera_id* (multiprocess or sequential)."""
        if self.is_tracker_alive(camera_id):
            return True
        self._tracker_kwargs[camera_id] = tracker_kwargs
        if self._use_multiprocess:
            return self._start_tracker_process(camera_id, tracker_kwargs)
        return self._start_inline_tracker(camera_id, tracker_kwargs)

    def stop_tracker(self, camera_id: str) -> None:
        """Stop the tracker for *camera_id* (either mode)."""
        self._stop_tracker_process(camera_id)
        self._inline_trackers.pop(camera_id, None)
        self._latest_tracker_result.pop(camera_id, None)
        self._tracker_kwargs.pop(camera_id, None)

    def is_tracker_alive(self, camera_id: str) -> bool:
        """Check if a tracker is active for *camera_id* (either mode)."""
        if camera_id in self._inline_trackers:
            # Inline trackers are plain objects — they never crash.  The real
            # liveness signal is whether the video thread is still calling
            # submit_frame().  _last_result_time is stamped on every successful
            # detect() call; if it's stale (> 60 s) the video thread has stopped.
            last = self._last_result_time.get(camera_id)
            if last is None:
                return True  # just started, no frame submitted yet
            return (time.time() - last) < 60.0
        proc = self._tracker_processes.get(camera_id)
        return proc is not None and proc.is_alive()

    # ── sequential (inline) lifecycle ────────────────────────────────────

    def _start_inline_tracker(self, camera_id: str, tracker_kwargs: dict) -> bool:
        """Create an inline OpticalFlowTracker for sequential mode."""
        from improc.optical_flow import OpticalFlowTracker

        tracker = OpticalFlowTracker(**tracker_kwargs)
        self._inline_trackers[camera_id] = tracker
        logger.info(f"{Colors.GREEN}Inline tracker started for {camera_id}{Colors.RESET}")
        return True

    # ── multiprocess lifecycle ───────────────────────────────────────────

    def _start_tracker_process(self, camera_id: str, tracker_kwargs: dict) -> bool:
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

    def _stop_tracker_process(self, camera_id: str) -> None:
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

        # Drain and close queues.  The input queue may hold TrackerInput items
        # whose SHM segments must be unlinked — otherwise they leak in /dev/shm.
        if input_q:
            try:
                # Drain queued messages — no per-item SHM cleanup needed since
                # slots are managed in bulk below.
                while not input_q.empty():
                    try:
                        input_q.get_nowait()
                    except Exception:
                        break
                input_q.close()
                input_q.join_thread()
            except Exception:
                pass

        # Unlink all pre-allocated SHM ring slots for this camera.
        for _s in self._shm_slots.pop(camera_id, []):
            try:
                _s.close()
                _s.unlink()
            except Exception:
                pass
        self._shm_slot_nbytes.pop(camera_id, None)
        self._shm_slot_counter.pop(camera_id, None)

        for q in (self._tracker_output_queues.pop(camera_id, None),
                  self._tracker_config_queues.pop(camera_id, None)):
            if q:
                try:
                    while not q.empty():
                        q.get_nowait()
                    q.close()
                    q.join_thread()
                except Exception:
                    pass

        logger.info(f"{Colors.YELLOW}Tracker stopped for {camera_id}{Colors.RESET}")

    # ── frame I/O (called from video thread) ─────────────────────────────

    def submit_frame(self, camera_id: str, frame: np.ndarray,
                     frame_capture_time: float, frame_index: int) -> bool:
        """
        Submit a frame to the tracker (unified interface).

        - Sequential mode: runs detect() inline and caches the result.
        - Multiprocess mode: enqueues the frame (non-blocking, may drop).
        """
        # ── Sequential (inline) path ──
        inline_tracker = self._inline_trackers.get(camera_id)
        if inline_tracker is not None:
            try:
                t0 = time.monotonic()
                raw = inline_tracker.detect(frame, return_pts=True)
                self._last_ai_latency[camera_id] = time.monotonic() - t0
                self._last_result_time[camera_id] = time.time()
                points_dict, flow_pts = _extract_dicts(raw)
                self._latest_tracker_result[camera_id] = TrackerResult(
                    points_dict=points_dict,
                    flow_pts=flow_pts,
                    frame_capture_time=frame_capture_time,
                    frame_index=frame_index,
                )
                return True
            except Exception:
                logger.exception(f"Inline tracker detect() failed for {camera_id}")
                return False

        # ── Multiprocess path ──
        input_q = self._tracker_input_queues.get(camera_id)
        if not input_q:
            return False

        # (Re-)allocate the fixed SHM ring when first called or if the frame
        # size changes (e.g. resolution switch).  Only SHM_RING_SIZE segments
        # are ever created per camera; they are reused across all frames so
        # shm_open/ftruncate/mmap/unlink are never called on the hot path.
        nbytes = max(frame.nbytes, 1)
        if self._shm_slot_nbytes.get(camera_id) != nbytes:
            # Close and unlink any previous slots.
            for _s in self._shm_slots.pop(camera_id, []):
                try:
                    _s.close()
                    _s.unlink()
                except Exception:
                    pass
            try:
                new_slots = [SharedMemory(create=True, size=nbytes) for _ in range(self._shm_ring_size)]
            except Exception:
                logger.exception(f"submit_frame: failed to allocate SHM ring for {camera_id}")
                return False
            self._shm_slots[camera_id] = new_slots
            self._shm_slot_nbytes[camera_id] = nbytes
            self._shm_slot_counter[camera_id] = 0
            # Inform the worker of the new slot names so it can pre-map them.
            setup = TrackerSetup(
                shm_names=[s.name for s in new_slots],
                slot_shape=frame.shape,
                slot_dtype_str=frame.dtype.str,
            )
            try:
                input_q.put(setup, timeout=1.0)
            except Exception:
                logger.error(f"submit_frame: could not send TrackerSetup for {camera_id}")
                return False

        # Write the frame into the next ring slot and enqueue only the index.
        slots = self._shm_slots[camera_id]
        counter = self._shm_slot_counter[camera_id]
        slot_idx = counter % self._shm_ring_size
        try:
            np.ndarray(frame.shape, dtype=frame.dtype, buffer=slots[slot_idx].buf)[:] = frame
            inp = TrackerInput(
                slot_index=slot_idx,
                shape=frame.shape,
                dtype_str=frame.dtype.str,
                frame_capture_time=frame_capture_time,
                frame_index=frame_index,
            )
            input_q.put_nowait(inp)
            # Advance counter only on successful enqueue so the same slot is
            # retried next frame if the queue was temporarily full.
            self._shm_slot_counter[camera_id] = counter + 1
            self._last_submit_time[camera_id] = time.time()
            return True
        except queue.Full:
            # Tracker is behind — drop this frame.  Do NOT advance the counter;
            # the slot can be overwritten on the next submit attempt.
            return False
        except Exception:
            logger.exception(f"submit_frame error for {camera_id}")
            return False

    def poll_result(self, camera_id: str) -> Optional[TrackerResult]:
        """
        Non-blocking poll for the latest tracker result (unified interface).

        - Sequential mode: returns the result cached by submit_frame().
        - Multiprocess mode: drains the output queue for the freshest result.
        """
        # Sequential mode — result already cached by submit_frame()
        if camera_id in self._inline_trackers:
            return self._latest_tracker_result.get(camera_id)

        # Multiprocess mode — drain queue
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
            now = time.time()
            self._last_result_time[camera_id] = now
            submit_t = self._last_submit_time.get(camera_id)
            if submit_t:
                self._last_ai_latency[camera_id] = now - submit_t
            return latest

        # Queue was empty — no new result this poll cycle.
        # Return None so the video thread does not re-use a stale result_ts,
        # which would write duplicate timestamps into the viz/results ring buffers.
        return None

    def get_cached_result(self, camera_id: str) -> Optional[TrackerResult]:
        """Return the last polled result without touching the queue."""
        return self._latest_tracker_result.get(camera_id)

    def get_tracker_status(self, camera_id: str) -> Dict[str, Any]:
        """Return AI proc status for *camera_id* suitable for the dashboard.

        Works in both sequential and multiprocess modes.  Detector-level
        enable/latency info is only available in inline (sequential) mode;
        multiprocess mode returns config-level enable flags.
        """
        now = time.time()
        alive = self.is_tracker_alive(camera_id)
        last_result = self._last_result_time.get(camera_id)
        ai_latency  = round(self._last_ai_latency.get(camera_id, 0.0) * 1000, 1)  # ms
        # How long ago did we get a result?  None if never.
        result_age  = round(now - last_result, 2) if last_result else None

        detectors: Dict[str, Any] = {}
        tracker = self._inline_trackers.get(camera_id)
        # A detector is considered "running" if it's enabled and was called
        # recently (within the last 30 s — more than one monitoring cycle).
        _RUNNING_THRESHOLD_S = 30.0

        def _is_running(enabled: bool, last_call_s) -> bool:
            return bool(enabled and last_call_s is not None and last_call_s < _RUNNING_THRESHOLD_S)

        if tracker is not None:
            # ── YOLOX ──
            ys = tracker.get_yolox_status() if hasattr(tracker, 'get_yolox_status') else {}
            detectors['yolox'] = {
                'enabled':    ys.get('enabled', False),
                'running':    _is_running(ys.get('enabled', False), ys.get('last_call_s')),
                'model_size': ys.get('model_size', ''),
                'score_thr':  ys.get('score_thr', 0.0),
                'latency_ms': ys.get('latency_ms'),
                'last_call_s': ys.get('last_call_s'),
            }
            # ── Person / Face detector ──
            ps = tracker.get_person_detection_status() if hasattr(tracker, 'get_person_detection_status') else {}
            detectors['person'] = {
                'enabled':      ps.get('enabled', False),
                'running':      _is_running(ps.get('enabled', False), ps.get('last_call_s')),
                'face_enabled': ps.get('face', False),
                'body_enabled': ps.get('body', False),
                'latency_ms':   ps.get('latency_ms'),
                'last_call_s':  ps.get('last_call_s'),
            }
            # ── RTMPose ──
            pose_s = tracker.get_pose_status() if hasattr(tracker, 'get_pose_status') else {}
            detectors['pose'] = {
                'enabled':    pose_s.get('enabled', False),
                'running':    _is_running(pose_s.get('enabled', False), pose_s.get('last_call_s')),
                'model_size': pose_s.get('model_size', ''),
                'score_thr':  pose_s.get('score_thr', 0.0),
                'latency_ms': pose_s.get('latency_ms'),
                'last_call_s': pose_s.get('last_call_s'),
            }
        else:
            # Multiprocess mode — subprocess is opaque; derive enabled from the
            # kwargs used to start the tracker, and running from process liveness
            # + whether a result arrived recently.
            kw = self._tracker_kwargs.get(camera_id, {})
            _RUNNING_THRESHOLD_S = 30.0
            recently_ran = (last_result is not None and
                            (now - last_result) < _RUNNING_THRESHOLD_S)

            yolox_en  = bool(kw.get('enable_yolox', False))
            person_en = bool(kw.get('enable_person_detection', False))
            pose_en   = bool(kw.get('enable_pose', False))

            detectors['yolox'] = {
                'enabled':    yolox_en,
                'running':    yolox_en and alive and recently_ran,
                'model_size': kw.get('yolox_model_size', ''),
                'score_thr':  kw.get('yolox_score_thr', 0.0),
                'latency_ms': None,
                'last_call_s': None,
            }
            detectors['person'] = {
                'enabled':      person_en,
                'running':      person_en and alive and recently_ran,
                'face_enabled': None,   # not determinable from multiprocess
                'body_enabled': None,
                'latency_ms':   None,
                'last_call_s':  None,
            }
            detectors['pose'] = {
                'enabled':    pose_en,
                'running':    pose_en and alive and recently_ran,
                'model_size': kw.get('pose_model_size', ''),
                'score_thr':  kw.get('pose_score_thr', 0.0),
                'latency_ms': None,
                'last_call_s': None,
            }

        return {
            'alive':       alive,
            'mode':        'multiprocess' if self._use_multiprocess else 'inline',
            'latency_ms':  ai_latency,
            'result_age_s': result_age,
            'detectors':   detectors,
        }

    # ── config updates (called from API route / camera_service) ──────────

    def send_config_update(self, camera_id: str, action: str, value: Any = None) -> bool:
        """
        Send a config change to the tracker (unified interface).

        - Sequential mode: applies directly to the inline tracker.
        - Multiprocess mode: enqueues to the config queue.
        """
        # Sequential mode — apply directly
        inline_tracker = self._inline_trackers.get(camera_id)
        if inline_tracker is not None:
            _apply_config(inline_tracker, TrackerConfigUpdate(action=action, value=value))
            return True

        # Multiprocess mode — enqueue
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
        """Stop all trackers (e.g. on server shutdown)."""
        for camera_id in list(self._tracker_processes.keys()) + list(self._inline_trackers.keys()):
            self.stop_tracker(camera_id)
