'''
minotor_streams
|-- check_camera_health
    |-- _get_current_lag_stats
    |-- _ensure_camera_history
    |-- _get_lag_values **
    |-- _recover_producer_video
    |-- _recover_producer_audio
    |-- _recover_recording
    |-- _recover_ai_tracker
    |-- _save_error_log_snapshot
    |-- _notify_frozen_stream
|-- _ai_health_check
    |-- get_tracker_status **

|-- _check_thread_health
    |-- is_alive (thread) **
    |-- is_tracker_alive (AI tracker process) **
'''
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Optional

import psutil


logger = logging.getLogger(__name__)


class StreamHealthMonitor:
    """Monitors stream lag and health of camera streams with automatic recovery."""
    
    def __init__(self, camera_service):
        self.camera_service = camera_service
        self.lag_history = {}  # camera_id -> lag_data
        
        # Recovery policies
        self.lag_threshold = 120.0  # 2 minutes max lag before considering frozen
        
        self._last_check = 0
        self._check_interval = 30  # Check every 30 seconds
        self.enable_slow_recovery_threshold = 3  # After 3 recoveries, enable slow recovery mode
        self.slow_recovery_interval = 5  # In slow recovery mode, recover every 5th failure

        # Lag history queue for 24-hour plotting
        self._lag_queue_maxlen = (24 * 3600) // self._check_interval  # 2880 samples at 30s interval
        self._lag_queues: Dict[str, deque] = {}  # camera_id -> deque of lag samples
        
        # Error log snapshot directory (project root)
        self._error_log_dir = os.path.join('.', 'error_logs')

        # Recovery callbacks — defined once, keyed by stream name
        self._recover_fn = {
            'video':     self._recover_producer_video,
            'audio':     self._recover_producer_audio,
            'recording': self._recover_recording,
            'ai':        self._recover_ai_tracker,
        }

        self._last_ai_check = 0
        
    def monitor_streams(self):
        """Check stream health and trigger recovery if needed."""
        now = time.time()
        if now - self._last_check < self._check_interval:
            return
            
        self._last_check = now
        
        try:
            cameras_cache = getattr(self.camera_service, '_cameras_cache', {})
            monitored = [
                camera_id for camera_id, info in cameras_cache.items()
                if info.get('keep_online', True)
            ]
            for camera_id in monitored:
                self._check_thread_health(camera_id)
                self._check_camera_health(camera_id)
                self._ai_health_check(camera_id)
        except Exception as e:
            logger.error(f"Error in stream health monitoring: {e}")
    
    def _check_camera_health(self, camera_id: str):
        """Check health of a specific camera and trigger recovery if needed."""
        logger.debug(f"🔍 Running health check for camera {camera_id}")
        current_lag = self._get_current_lag_stats(camera_id)
        history = self._ensure_camera_history(camera_id, current_lag)

        # Check streaming consumer lags and notify if threshold exceeded
        for lag_key, stream_type in [('video_stream_lag', 'video_stream'), ('audio_stream_lag', 'audio_stream')]:
            lag_val = current_lag.get(lag_key, {}).get(camera_id, 0)
            if lag_val > self.lag_threshold:
                if lag_key not in history['notified_streaming_consumers']:
                    self._notify_frozen_stream(camera_id, stream_type, lag_val)
                    history['notified_streaming_consumers'].add(lag_key)
            else:
                history['notified_streaming_consumers'].discard(lag_key)

        lag_values = self._get_lag_values(camera_id, current_lag)
        streams = history['streams']
        first_recovery_triggered = False
        for name, lag in lag_values.items():
            s = streams[name]
            if lag > self.lag_threshold:
                if not s['slow_recovery'] or s['count'] % self.slow_recovery_interval == 0:
                    self._recover_fn[name](camera_id)
                s['count'] += 1
                s['frozen'] = lag if lag != float('inf') else True
                if s['count'] >= self.enable_slow_recovery_threshold and not s['slow_recovery']:
                    logger.warning(f"Enabling slow recovery for {name} of camera {camera_id} after {s['count']} recoveries")
                    s['slow_recovery'] = True
                if s['count'] == 1:
                    first_recovery_triggered = True
            else:
                s['frozen'] = None if name != 'ai' else False
                s['count'] = 0
                s['slow_recovery'] = False

        if first_recovery_triggered:
            self._save_error_log_snapshot(camera_id)
    
    def _ensure_camera_history(self, camera_id: str, current_lag: dict) -> dict:
        """Initialise lag_history and lag_queue for camera_id if not already present,
        then append the current lag sample to the 24h queue."""
        if camera_id not in self.lag_history:
            self.lag_history[camera_id] = {
                'streams': {
                    'video':     {'frozen': None,  'count': 0, 'slow_recovery': False},
                    'audio':     {'frozen': None,  'count': 0, 'slow_recovery': False},
                    'recording': {'frozen': None,  'count': 0, 'slow_recovery': False},
                    'ai':        {'frozen': False, 'count': 0, 'slow_recovery': False},
                },
                'notified_streaming_consumers': set(),
            }
        if camera_id not in self._lag_queues:
            self._lag_queues[camera_id] = deque(maxlen=self._lag_queue_maxlen)
        now = time.time()
        self._lag_queues[camera_id].append({
            'ts':       now,
            'video':    current_lag.get('producer_video_lag', {}).get(camera_id, 0),
            'audio':    current_lag.get('producer_audio_lag', {}).get(camera_id, 0),
            'recorder': current_lag.get('recorder_lag', {}).get(camera_id, 0),
        })
        return self.lag_history[camera_id]

    def _get_lag_values(self, camera_id: str, current_lag: dict) -> dict:
        """Return per-stream lag values for the recovery loop.
        AI liveness is encoded as 0 (alive) or inf (dead)."""
        return {
            'video':     current_lag.get('producer_video_lag', {}).get(camera_id, float('inf')),
            'audio':     current_lag.get('producer_audio_lag', {}).get(camera_id, float('inf')),
            'recording': current_lag.get('recorder_lag', {}).get(camera_id, float('inf')),
            'ai':        0.0 if self.camera_service.ai_service.is_tracker_alive(camera_id) else float('inf'),
        }

    def _extract_buffer_lags(self, buffer, camera_id: str, now: float,
                              producer_key: str, stream_consumer_prefix: str,
                              stream_lag_key: str, lag_stats: dict):
        """Extract producer and consumer lag from a single ring buffer."""
        if not buffer or not hasattr(buffer, 'last_write_time'):
            return
        last_write = buffer.last_write_time
        if last_write:
            lag_stats[producer_key][camera_id] = now - last_write
        if not hasattr(buffer, '_consumer_read_times'):
            return
        for consumer_id, read_times in buffer._consumer_read_times.items():
            if not read_times:
                continue
            last_read = read_times[-1]
            if not last_read:
                continue
            consumer_lag = now - last_read
            if f'recorder_{camera_id}' in consumer_id:
                lag_stats['recorder_lag'][camera_id] = max(
                    lag_stats['recorder_lag'].get(camera_id, 0), consumer_lag)
            if f'{stream_consumer_prefix}_{camera_id}' in consumer_id:
                lag_stats[stream_lag_key][camera_id] = consumer_lag

    def _get_current_lag_stats(self, camera_id: str) -> Dict[str, float]:
        """Get current lag statistics for a camera."""
        lag_stats = {
            'producer_video_lag': {},
            'producer_audio_lag': {}, 
            'recorder_lag': {},
            'video_stream_lag': {},
            'audio_stream_lag': {}
        }
        
        now = time.time()
        
        self._extract_buffer_lags(
            self.camera_service._frame_ring_buffers.get(camera_id), camera_id, now,
            'producer_video_lag', 'video_stream', 'video_stream_lag', lag_stats)
        self._extract_buffer_lags(
            self.camera_service._audio_ring_buffers.get(camera_id), camera_id, now,
            'producer_audio_lag', 'audio_stream', 'audio_stream_lag', lag_stats)
        
        # Summary log for high-level lag overview
        lag_parts = []
        label_map = {
            'producer_video_lag': 'vid',
            'producer_audio_lag': 'aud',
            'recorder_lag':       'rec',
            'video_stream_lag':   'vid-stream',
            'audio_stream_lag':   'aud-stream',
        }
        for k, v in lag_stats.items():
            if camera_id in v:
                ms = v[camera_id] * 1000
                flag = ' ⚠' if ms > 500 else ''
                lag_parts.append(f"{label_map.get(k, k)}={ms:.0f}ms{flag}")
        if lag_parts:
            logger.info(f"📊 [{camera_id}] lag  | {' | '.join(lag_parts)}")
  
        return lag_stats
    
    def _recover_producer_video(self, camera_id: str):
        """Recover video producer thread by restarting it."""
        logger.warning(f"Recovering video producer thread for camera {camera_id}")
        self.camera_service.stop_video_stream(camera_id, stop_recording=False)
        time.sleep(2)  # Allow clean shutdown
        success = self.camera_service.start_video_stream(camera_id)
        if success:
            logger.info(f"✅ Successfully recovered video producer thread for camera {camera_id}")
        else:
            logger.error(f"❌ Failed to recover video producer thread for camera {camera_id}")
            time.sleep(15)  # Wait before retrying


    def _recover_producer_audio(self, camera_id: str):
        """Recover audio producer thread by restarting it."""
        logger.warning(f"Recovering audio producer thread for camera {camera_id}")
        self.camera_service.stop_audio_stream(camera_id)
        time.sleep(2)  # Allow clean shutdown
        success = self.camera_service.start_audio_stream(camera_id)
        if success:
            logger.info(f"✅ Successfully recovered audio producer thread for camera {camera_id}")
        else:
            logger.error(f"❌ Failed to recover audio producer thread for camera {camera_id}")
            time.sleep(15)  # Wait before retrying

    def _recover_recording(self, camera_id: str):
        """Recover recording subscriber by restarting recording if it was active."""
        logger.warning(f"Recovering recording for camera {camera_id}")
        #self.stop_recording(camera_id)
        #Start recording already stops it first if necessary.
        recording_id = self.camera_service.start_recording(camera_id)
        if recording_id:
            logger.info(f"✅ Successfully recovered recording for camera {camera_id}")
        else:
            logger.error(f"❌ Failed to recover recording for camera {camera_id}")
            time.sleep(15)  # Wait before retrying

    def _recover_ai_tracker(self, camera_id: str):
        """Recover AI tracker by stopping and restarting it."""
        logger.warning(f"Recovering AI tracker for camera {camera_id}")
        cs = self.camera_service
        tracker_kwargs = {
            'enable_person_detection': getattr(cs, 'enable_person_detection', False),
            'enable_yolox': getattr(cs, 'enable_yolox', False),
            'yolox_model_size': getattr(cs, 'yolox_model_size', 'nano'),
            'yolox_score_thr': getattr(cs, 'yolox_score_thr', 0.5),
            'enable_pose': getattr(cs, 'enable_pose', False),
            'pose_model_size': getattr(cs, 'pose_model_size', 'tiny'),
            'pose_score_thr': getattr(cs, 'pose_score_thr', 0.3),
        }
        cs.ai_service.stop_tracker(camera_id)
        success = cs.ai_service.start_ai_tracker(camera_id, tracker_kwargs)
        if success:
            logger.info(f"✅ Successfully recovered AI tracker for camera {camera_id}")
        else:
            logger.error(f"❌ Failed to recover AI tracker for camera {camera_id}")
            time.sleep(15)  # Wait before retrying

    def _save_error_log_snapshot(self, camera_id: str):
        """Save last 500 lines of nvr.log when lag exceeds threshold for the first time."""
        try:
            log_path = os.path.join(os.path.dirname(self._error_log_dir), 'nvr.log')
            if not os.path.exists(log_path):
                logger.warning(f"Cannot save error log snapshot: {log_path} not found")
                return
            os.makedirs(self._error_log_dir, exist_ok=True)
            with open(log_path, 'r') as f:
                tail = deque(f, maxlen=500)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            snapshot_path = os.path.join(self._error_log_dir, f'error_{camera_id}_{timestamp}.txt')
            with open(snapshot_path, 'w') as f:
                f.writelines(tail)
            logger.info(f"📋 Saved error log snapshot ({len(tail)} lines) to {snapshot_path}")
        except Exception as e:
            logger.error(f"Failed to save error log snapshot for {camera_id}: {e}")

    def _notify_frozen_stream(self, camera_id: str, stream_type: str, lag_seconds: float):
        """Notify when a stream appears frozen based on lag time."""
        logger.warning(f"Stream frozen detected: camera {camera_id}, {stream_type} stream, lag: {lag_seconds:.2f}s")
        # Could extend this to send alerts via webhook, email, etc.
        # For now, just log the frozen stream detection

    def _ai_health_check(self, camera_id: str):
        """Log AI processing health — proc alive, detector enabled/running/latency."""
        
        status = self.camera_service.ai_service.get_tracker_status(camera_id)

        alive      = status.get('alive', False)
        mode       = status.get('mode', '?')
        latency_ms = status.get('latency_ms') or 0.0
        age_s      = status.get('result_age_s')
        detectors  = status.get('detectors', {})

        proc_age = f" age={age_s:.1f}s" if age_s is not None else ''
        proc_str = f"proc={'OK' if alive else 'DEAD'} mode={mode} {latency_ms:.0f}ms{proc_age}"

        det_parts = []
        det_icons = {'yolox': '🔍', 'person': '🧍', 'pose': '🦴'}

        for name in ('yolox', 'person', 'pose'):
            d = detectors.get(name, {})
            enabled    = d.get('enabled')
            running    = d.get('running')
            lat        = d.get('latency_ms')
            call_s     = d.get('last_call_s')
            face_en    = d.get('face_enabled')  # person only
            body_en    = d.get('body_enabled')  # person only

            if enabled is None:
                det_parts.append(f"{det_icons.get(name)}:{name}=?")
                continue

            if not enabled:
                det_parts.append(f"{det_icons.get(name)}:{name}=OFF")
                continue

            # Per-detector latency available in inline mode; None in multiprocess.
            if lat is not None:
                status_str = f"{lat:.0f}ms"
            elif running:
                # Multiprocess — show overall proc latency as proxy
                status_str = f"{latency_ms:.0f}ms"
            else:
                status_str = 'STALL'

            if name == 'person':
                sub = []
                if face_en:
                    sub.append('face')
                if body_en:
                    sub.append('body')
                sub_str = f"[{','.join(sub)}]" if sub else ''
                det_parts.append(f"{det_icons.get(name)}:{name}{sub_str}={status_str}")
            else:
                det_parts.append(f"{det_icons.get(name)}:{name}={status_str}")

        logger.info(f"🤖 [{camera_id}] ai | {proc_str} | {' | '.join(det_parts)}")
            
    def _check_thread_health(self, camera_id: str) -> Dict[str, Optional[bool]]:
        """Check if background threads are alive and log alongside lag status."""
        thread_status = {}
        
        rec_info = self.camera_service.recording_manager.active_recordings.get(camera_id)
        threads = {
            'video': self.camera_service._video_background_threads.get(camera_id),
            'audio': self.camera_service._audio_background_threads.get(camera_id),
            'rec': rec_info.get('thread') if rec_info else None,
        }
        
        alive = {name: t.is_alive() if t else None for name, t in threads.items()}
        thread_status[camera_id] = alive
        
        ai_alive = self.camera_service.ai_service.is_tracker_alive(camera_id)
        alive['ai'] = ai_alive

        icons = {'video': '🎥', 'audio': '🔊', 'rec': '⏺', 'ai': '🤖'}
        parts = []
        for name, ok in alive.items():
            if ok is None:
                parts.append(f"{icons.get(name, name)}:{name}=--")
            else:
                parts.append(f"{icons.get(name, name)}:{name}={'OK' if ok else 'DEAD'}")
        logger.info(f"🧵 [{camera_id}] threads | {' | '.join(parts)}")

        dead = [name for name, ok in alive.items() if ok is False]
        if dead:
            logger.error(f"⚠️  [{camera_id}] DEAD threads: {', '.join(dead)} — recovery needed")
        
        return thread_status

    def get_health_status(self, camera_id: str) -> Dict:
        """Get current health status for a camera."""
        current_lag = self._get_current_lag_stats(camera_id)
        history = self.lag_history.get(camera_id, {})
        
        streams = history.get('streams', {})
        return {
            'camera_id': camera_id,
            'lag_stats': current_lag,
            'health_issues': {name: bool(s['frozen']) for name, s in streams.items()},
            'recovery_counts': {name: s['count'] for name, s in streams.items()},
            'ai_tracker_alive': self.camera_service.ai_service.is_tracker_alive(camera_id),
        }

    def get_lag_history(self, camera_id: str = None) -> Dict:
        """Get lag history for 24-hour plotting."""
        if camera_id:
            return {camera_id: list(self._lag_queues.get(camera_id, []))}
        return {cid: list(q) for cid, q in self._lag_queues.items()}

    # ---------------------------------------------------------------
    # Dashboard public API
    # ---------------------------------------------------------------

    def get_thread_status(self) -> Dict:
        """Return thread alive-status for every active camera.

        Returns::

            {
                'cam-1': {
                    'video': True,   # video producer thread alive
                    'audio': True,   # audio producer thread alive
                    'rec':   False,  # recording thread not started / stopped
                    'ai':    True,   # AI tracker alive
                },
                ...
            }

        Values are ``True`` (alive), ``False`` (dead/stopped), or ``None``
        (thread was never started).
        """
        result = {}
        for camera_id in list(self.camera_service._camera_streams.keys()):
            rec_info = self.camera_service.recording_manager.active_recordings.get(camera_id)
            rec_thread = rec_info.get('thread') if rec_info else None

            video_t = self.camera_service._video_background_threads.get(camera_id)
            audio_t = self.camera_service._audio_background_threads.get(camera_id)

            result[camera_id] = {
                'video': video_t.is_alive() if video_t else None,
                'audio': audio_t.is_alive() if audio_t else None,
                'rec':   rec_thread.is_alive() if rec_thread else None,
                'ai':    self.camera_service.ai_service.is_tracker_alive(camera_id),
            }
        return result

    def get_ai_proc_status(self) -> Dict:
        """Return AI processing status per camera.

        Returns::

            {
                'cam-1': {
                    'alive':        True,
                    'mode':         'inline',
                    'latency_ms':   12.4,
                    'result_age_s': 0.3,
                    'detectors': {
                        'yolox':  {'enabled': True,  'running': True,  'model_size': 'tiny', 'score_thr': 0.3},
                        'person': {'enabled': False, 'running': False, 'face_enabled': False, 'body_enabled': False},
                        'pose':   {'enabled': False, 'running': False, 'model_size': 'tiny', 'score_thr': 0.3},
                    },
                },
                ...
            }
        """
        result = {}
        for camera_id in list(self.camera_service._camera_streams.keys()):
            result[camera_id] = self.camera_service.ai_service.get_tracker_status(camera_id)
        return result

    def get_lag_summary(self) -> Dict:
        """Return video / audio / AI / recording lags per camera.

        Returns::

            {
                'cam-1': {
                    'video_lag_s': 0.12,
                    'audio_lag_s': 0.08,
                    'ai_lag_s':    0.0,   # 0.0 alive, inf = dead
                    'rec_lag_s':   0.25,
                },
                ...
            }
        """
        result = {}
        for camera_id in list(self.camera_service._camera_streams.keys()):
            lag = self._get_current_lag_stats(camera_id)
            ai_alive = self.camera_service.ai_service.is_tracker_alive(camera_id)
            result[camera_id] = {
                'video_lag_s': round(lag.get('producer_video_lag', {}).get(camera_id, 0.0), 3),
                'audio_lag_s': round(lag.get('producer_audio_lag', {}).get(camera_id, 0.0), 3),
                'ai_lag_s':    0.0 if ai_alive else float('inf'),
                'rec_lag_s':   round(lag.get('recorder_lag', {}).get(camera_id, 0.0), 3),
            }
        return result

    def get_camera_dashboard(self, camera_id: str = None) -> Dict:
        """Aggregate thread, AI, and lag status into a single dict.

        If *camera_id* is given, returns the sub-dict for that camera only.
        Otherwise returns a dict keyed by camera_id with all three sections.
        """
        threads = self.get_thread_status()
        ai_proc = self.get_ai_proc_status()
        lags    = self.get_lag_summary()

        all_ids = set(threads) | set(ai_proc) | set(lags)
        combined = {
            cid: {
                'threads': threads.get(cid, {}),
                'ai_proc': ai_proc.get(cid, {}),
                'lags':    lags.get(cid, {}),
            }
            for cid in all_ids
        }

        if camera_id is not None:
            return combined.get(camera_id, {})
        return combined


class SystemService:
    def __init__(self, camera_service, window_seconds: int = 30, sample_interval_seconds: int = 5):
        self.camera_service = camera_service
        self.window_seconds = window_seconds
        self.sample_interval_seconds = sample_interval_seconds

        self._samples: Deque[Dict] = deque()
        self._lock = threading.Lock()

        self._last_disk_counters = None
        self._last_process_io = None
        self._last_sample_time = None
        self._last_ram_check = 0
        self._ram_check_interval = 60  # Check RAM every 60 seconds

        # 24h resource usage history (cpu/memory sampled every 30s)
        _resource_interval = 30
        _resource_maxlen = (24 * 3600) // _resource_interval  # 2880 samples
        self._resource_queue: deque = deque(maxlen=_resource_maxlen)
        self._resource_interval = _resource_interval
        self._last_resource_sample = 0.0

        # Initialize stream health monitor
        self.stream_monitor = StreamHealthMonitor(camera_service)

        # Cached recordings directory size — refreshed every 60s in _sample_loop.
        self._recording_dir_size_bytes: int = 0
        self._last_dir_size_refresh: float = 0.0
        self._dir_size_refresh_interval: int = 60

        # psutil.Process cache for AI tracker subprocesses (multiprocess mode).
        # Keyed by OS pid; evicted when the process is no longer alive.
        self._ai_proc_cache: Dict[int, psutil.Process] = {}
    
    def start(self):
        self._sampler_thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._sampler_thread.start()

    def _find_start_server_process(self) -> psutil.Process:
        try:
            candidates = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
                cmdline = proc.info.get('cmdline') or []
                if any('start_server.py' in str(part) for part in cmdline):
                    candidates.append(proc)

            if candidates:
                return sorted(candidates, key=lambda proc: proc.info.get('create_time') or 0)[0]
        except Exception as error:
            logger.warning(f"Unable to resolve start_server process: {error}")

        return psutil.Process(os.getpid())

    def _sample_loop(self):
        while True:
            try:
                self._collect_sample()
                self._check_ram_threshold()
                self.stream_monitor.monitor_streams()  # Add stream health monitoring
                now = time.time()
                if now - self._last_dir_size_refresh >= self._dir_size_refresh_interval:
                    self._recording_dir_size_bytes = self._get_directory_size_bytes(
                        self.camera_service.recordings_dir
                    )
                    self._last_dir_size_refresh = now
            except Exception:
                logger.exception("⚠️ _sample_loop iteration failed — monitoring continues")
            time.sleep(self.sample_interval_seconds)

    def _collect_sample(self):
        now = time.time()
        memory = psutil.virtual_memory()
        disk_counters = psutil.disk_io_counters()

        process = self._find_start_server_process()
        _cpu_count = psutil.cpu_count() or 1
        process_cpu = process.cpu_percent(interval=0.05) / _cpu_count
        process_memory_percent = process.memory_percent()
        process_memory_mb = process.memory_info().rss / (1024 ** 2)

        process_io = None
        process_read_mb_total = 0.0
        process_write_mb_total = 0.0
        try:
            process_io = process.io_counters()
            process_read_mb_total = process_io.read_bytes / (1024 ** 2)
            process_write_mb_total = process_io.write_bytes / (1024 ** 2)
        except Exception:
            pass

        disk_read_mb_total = (disk_counters.read_bytes / (1024 ** 2)) if disk_counters else 0.0
        disk_write_mb_total = (disk_counters.write_bytes / (1024 ** 2)) if disk_counters else 0.0

        disk_read_mb_s = 0.0
        disk_write_mb_s = 0.0
        process_read_mb_s = 0.0
        process_write_mb_s = 0.0

        if self._last_sample_time is not None:
            elapsed = max(1e-6, now - self._last_sample_time)

            if self._last_disk_counters is not None and disk_counters is not None:
                delta_disk_read = max(0.0, disk_counters.read_bytes - self._last_disk_counters.read_bytes)
                delta_disk_write = max(0.0, disk_counters.write_bytes - self._last_disk_counters.write_bytes)
                disk_read_mb_s = (delta_disk_read / (1024 ** 2)) / elapsed
                disk_write_mb_s = (delta_disk_write / (1024 ** 2)) / elapsed

            if self._last_process_io is not None and process_io is not None:
                delta_proc_read = max(0.0, process_io.read_bytes - self._last_process_io.read_bytes)
                delta_proc_write = max(0.0, process_io.write_bytes - self._last_process_io.write_bytes)
                process_read_mb_s = (delta_proc_read / (1024 ** 2)) / elapsed
                process_write_mb_s = (delta_proc_write / (1024 ** 2)) / elapsed

        sample = {
            'timestamp': now,
            'cpu_usage': psutil.cpu_percent(interval=0.05),
            'memory_usage': memory.percent,
            'disk_io_read_mb_s': disk_read_mb_s,
            'disk_io_write_mb_s': disk_write_mb_s,
            'disk_io_read_mb_total': disk_read_mb_total,
            'disk_io_write_mb_total': disk_write_mb_total,
            'process_name': process.name() or 'start_server',
            'process_pid': process.pid,
            'process_cpu_percent': process_cpu,
            'process_memory_percent': process_memory_percent,
            'process_memory_mb': process_memory_mb,
            'process_disk_io_read_mb_s': process_read_mb_s,
            'process_disk_io_write_mb_s': process_write_mb_s,
            'process_disk_io_read_mb_total': process_read_mb_total,
            'process_disk_io_write_mb_total': process_write_mb_total,
        }

        # Aggregate CPU + memory across all AI tracker subprocesses.
        ai_cpu_total = 0.0
        ai_memory_mb_total = 0.0
        ai_memory_pct_total = 0.0
        ai_tracker_pids: list = []
        try:
            ai_service = getattr(self.camera_service, 'ai_service', None)
            if ai_service and self.camera_service.ai_service.use_multiprocess:
                ai_tracker_pids = ai_service.get_tracker_pids()
                live_pids = set(ai_tracker_pids)
                # Evict stale cached Process objects
                for dead in [p for p in self._ai_proc_cache if p not in live_pids]:
                    self._ai_proc_cache.pop(dead, None)
                for pid in ai_tracker_pids:
                    new_entry = pid not in self._ai_proc_cache
                    if new_entry:
                        self._ai_proc_cache[pid] = psutil.Process(pid)
                        # Prime the cpu_percent baseline; first call always returns 0.0
                        self._ai_proc_cache[pid].cpu_percent(interval=None)
                    try:
                        p = self._ai_proc_cache[pid]
                        if not new_entry:
                            # Normalize to 0–100% regardless of core count
                            ai_cpu_total += p.cpu_percent(interval=None) / _cpu_count
                        ai_memory_mb_total += p.memory_info().rss / (1024 ** 2)
                        ai_memory_pct_total += p.memory_percent()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        self._ai_proc_cache.pop(pid, None)
        except Exception:
            pass

        sample['ai_tracker_pids'] = ai_tracker_pids
        sample['ai_cpu_percent'] = ai_cpu_total
        sample['ai_memory_mb'] = ai_memory_mb_total
        sample['ai_memory_percent'] = ai_memory_pct_total

        with self._lock:
            self._samples.append(sample)
            cutoff = now - self.window_seconds
            while self._samples and self._samples[0]['timestamp'] < cutoff:
                self._samples.popleft()

        # Append to 24h resource history at 30s granularity
        if now - self._last_resource_sample >= self._resource_interval:
            self._resource_queue.append({
                'ts': now,
                'cpu': sample.get('cpu_usage', 0.0),
                'mem': sample.get('memory_usage', 0.0),
                'proc_cpu': sample.get('process_cpu_percent', 0.0),
                'proc_mem': sample.get('process_memory_percent', 0.0),
                'ai_cpu': sample.get('ai_cpu_percent', 0.0),
                'ai_mem': sample.get('ai_memory_percent', 0.0),
            })
            self._last_resource_sample = now

        self._last_sample_time = now
        self._last_disk_counters = disk_counters
        self._last_process_io = process_io

    @staticmethod
    def _average(samples, key: str) -> float:
        if not samples:
            return 0.0
        values = [float(sample.get(key, 0.0) or 0.0) for sample in samples]
        return sum(values) / len(values)

    @staticmethod
    def _get_directory_size_bytes(directory_path: str) -> int:
        total_size = 0
        for root, _, files in os.walk(directory_path):
            for filename in files:
                file_path = os.path.join(root, filename)
                try:
                    if os.path.isfile(file_path):
                        total_size += os.path.getsize(file_path)
                except OSError:
                    continue
        return total_size

    def get_system_info(self) -> Dict:
        with self._lock:
            if not self._samples:
                self._collect_sample()
            samples = list(self._samples)

        latest = samples[-1] if samples else {}

        recording_disk = psutil.disk_usage(self.camera_service.recordings_dir)
        overall_disk = psutil.disk_usage(os.path.abspath(os.sep))
        recording_dir_size_bytes = self._recording_dir_size_bytes
        uptime_delta = datetime.now() - self.camera_service.start_time
        total_uptime_seconds = max(0, int(uptime_delta.total_seconds()))

        processing_active = {
            camera.id: camera.processing_type
            for camera in self.camera_service.get_cameras()
            if camera.processing_active
        }

        return {
            'uptime': {
                'text': str(uptime_delta).split('.')[0],
                'days': total_uptime_seconds // 86400,
                'hours': (total_uptime_seconds % 86400) // 3600,
                'minutes': (total_uptime_seconds % 3600) // 60,
                'seconds': total_uptime_seconds % 60,
            },
            'cpu_usage': latest.get('cpu_usage', 0.0),
            'memory_usage': latest.get('memory_usage', 0.0),
            'disk_usage': {
                'percent_used': recording_disk.percent,
                'used_gb': recording_disk.used / (1024 ** 3),
                'total_gb': recording_disk.total / (1024 ** 3),
                'free_gb': recording_disk.free / (1024 ** 3),
                'io_read_mb': latest.get('disk_io_read_mb_total', 0.0),
                'io_write_mb': latest.get('disk_io_write_mb_total', 0.0),
                'io_read_mb_s': latest.get('disk_io_read_mb_s', 0.0),
                'io_write_mb_s': latest.get('disk_io_write_mb_s', 0.0),
            },
            'disk_size': {
                'overall_total_gb': overall_disk.total / (1024 ** 3),
                'overall_used_gb': overall_disk.used / (1024 ** 3),
                'overall_free_gb': overall_disk.free / (1024 ** 3),
                'recording_dir_size_gb': recording_dir_size_bytes / (1024 ** 3),
            },
            'process_usage': {
                'name': latest.get('process_name', 'start_server'),
                'pid': latest.get('process_pid'),
                'cpu_percent': latest.get('process_cpu_percent', 0.0),
                'memory_percent': latest.get('process_memory_percent', 0.0),
                'memory_mb': latest.get('process_memory_mb', 0.0),
                'disk_io_read_mb': latest.get('process_disk_io_read_mb_total', 0.0),
                'disk_io_write_mb': latest.get('process_disk_io_write_mb_total', 0.0),
                'disk_io_read_mb_s': latest.get('process_disk_io_read_mb_s', 0.0),
                'disk_io_write_mb_s': latest.get('process_disk_io_write_mb_s', 0.0),
            },
            'ai_process_usage': {
                'mode': 'multiprocess' if getattr(getattr(self.camera_service, 'ai_service', None), 'use_multiprocess', False) else 'inline',
                'tracker_pids': latest.get('ai_tracker_pids', []),
                'tracker_count': len(latest.get('ai_tracker_pids', [])),
                'cpu_percent': latest.get('ai_cpu_percent', 0.0),
                'memory_mb': latest.get('ai_memory_mb', 0.0),
                'memory_percent': latest.get('ai_memory_percent', 0.0),
            },
            'averages_5m': {
                'window_seconds': self.window_seconds,
                'sample_count': len(samples),
                'cpu_usage': self._average(samples, 'cpu_usage'),
                'memory_usage': self._average(samples, 'memory_usage'),
                'disk_io_read_mb_s': self._average(samples, 'disk_io_read_mb_s'),
                'disk_io_write_mb_s': self._average(samples, 'disk_io_write_mb_s'),
                'process_cpu_percent': self._average(samples, 'process_cpu_percent'),
                'process_memory_percent': self._average(samples, 'process_memory_percent'),
                'process_disk_io_read_mb_s': self._average(samples, 'process_disk_io_read_mb_s'),
                'process_disk_io_write_mb_s': self._average(samples, 'process_disk_io_write_mb_s'),
                'ai_cpu_percent': self._average(samples, 'ai_cpu_percent'),
                'ai_memory_percent': self._average(samples, 'ai_memory_percent'),
            },
            'processing_active': processing_active,
            'active_recordings': len(self.camera_service.active_recordings),
            'total_cameras': len(self.camera_service.get_cameras()),
            'total_recordings': len(self.camera_service.get_recordings()),
        }

    def get_resource_history(self) -> list:
        """Get 24h CPU and memory usage history for dashboard plotting."""
        return list(self._resource_queue)

    def _check_ram_threshold(self):
        """Check RAM usage and auto-switch to low power mode if below threshold."""
        now = time.time()
        if now - self._last_ram_check < self._ram_check_interval:
            return
            
        self._last_ram_check = now
        
        try:
            sys_settings = self.camera_service.db.get_system_settings()
            if not sys_settings.get('ram_auto_switch_enabled', True):
                return
                
            current_preset = sys_settings.get('active_preset', 'default')
            ram_threshold = sys_settings.get('ram_threshold_bytes', 1073741824)  # 1GB default
            
            memory = psutil.virtual_memory()
            available_ram = memory.available
            
            if available_ram < ram_threshold and current_preset != 'low_power':
                logger.info(f"RAM below threshold ({available_ram / (1024**3):.2f}GB < {ram_threshold / (1024**3):.2f}GB), switching to low_power preset")
                try:
                    self.camera_service.apply_preset('low_power')
                except Exception as e:
                    logger.error(f"Failed to switch to low_power preset: {e}")
                    
            elif available_ram >= ram_threshold * 1.2 and current_preset == 'low_power':
                # Add 20% hysteresis to prevent oscillation
                logger.info(f"RAM above threshold with hysteresis ({available_ram / (1024**3):.2f}GB >= {ram_threshold * 1.2 / (1024**3):.2f}GB), switching to default preset")
                try:
                    self.camera_service.apply_preset('default')
                except Exception as e:
                    logger.error(f"Failed to switch to default preset: {e}")
                    
        except Exception as e:
            logger.error(f"Error in RAM threshold check: {e}")