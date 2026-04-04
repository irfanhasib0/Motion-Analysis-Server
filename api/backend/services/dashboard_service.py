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
        self.recovery_cooldown = 300  # 5 minutes between attempts
        self.max_recovery_attempts = 3
        self.video_exhausted_cooldown = 900  # 15 minutes before retrying after max attempts
        
        self._last_check = 0
        self._check_interval = 30  # Check every 30 seconds
        
        # Lag history queue for 24-hour plotting
        self._lag_queue_maxlen = (24 * 3600) // self._check_interval  # 2880 samples at 30s interval
        self._lag_queues: Dict[str, deque] = {}  # camera_id -> deque of lag samples
        
        # Error log snapshot directory (project root)
        self._error_log_dir = os.path.join('.', 'error_logs')
        
    def monitor_streams(self):
        """Check stream health and trigger recovery if needed."""
        now = time.time()
        if now - self._last_check < self._check_interval:
            return
            
        self._last_check = now
        
        try:
            self._check_thread_health()
            for camera_id in list(self.camera_service._camera_streams.keys()):
                self._check_camera_health(camera_id)
        except Exception as e:
            logger.error(f"Error in stream health monitoring: {e}")
    
    def _check_camera_health(self, camera_id: str):
        """Check health of a specific camera and trigger recovery if needed."""
        logger.info(f"🔍 Running health check for camera {camera_id}")
        current_lag = self._get_current_lag_stats(camera_id)
        
        if camera_id not in self.lag_history:
            self.lag_history[camera_id] = {
                'producer_video_frozen': None,
                'producer_audio_frozen': None,
                'recorder_frozen': None,
                'last_video_recovery': 0,
                'last_audio_recovery': 0,
                'last_recorder_recovery': 0,
                'video_recovery_count': 0,
                'audio_recovery_count': 0,
                'recorder_recovery_count': 0,
                'video_exhausted_recording_stopped': False,
                'video_exhausted_time': None,
                'was_recording_before_failure': False,
                'notified_streaming_consumers': set(),  # Track which consumers we've already notified about
                'error_logged': False,
            }
        
        history = self.lag_history[camera_id]
        now = time.time()
        
        # Append lag sample for 24h history plot
        if camera_id not in self._lag_queues:
            self._lag_queues[camera_id] = deque(maxlen=self._lag_queue_maxlen)
        self._lag_queues[camera_id].append({
            'ts': now,
            'video': current_lag.get('producer_video_lag', {}).get(camera_id, 0),
            'audio': current_lag.get('producer_audio_lag', {}).get(camera_id, 0),
            'recorder': current_lag.get('recorder_lag', {}).get(camera_id, 0),
        })
        
        # Check streaming consumer lags and notify if threshold exceeded
        for lag_key, stream_type in [('video_stream_lag', 'video_stream'), ('audio_stream_lag', 'audio_stream')]:
            lag_val = current_lag.get(lag_key, {}).get(camera_id, 0)
            if lag_val > self.lag_threshold:
                if lag_key not in history['notified_streaming_consumers']:
                    self._notify_frozen_stream(camera_id, stream_type, lag_val)
                    history['notified_streaming_consumers'].add(lag_key)
            else:
                history['notified_streaming_consumers'].discard(lag_key)
        
        # Check producer video thread lag
        producer_video_lag = current_lag.get('producer_video_lag', {}).get(camera_id, 0)
        if producer_video_lag > self.lag_threshold:
            # Save error log snapshot on first detection
            if not history.get('error_logged'):
                self._save_error_log_snapshot(camera_id)
                history['error_logged'] = True
            if history['video_recovery_count'] >= self.max_recovery_attempts:
                # Max attempts exhausted — stop recording and wait for cooldown
                if not history.get('video_exhausted_recording_stopped'):
                    was_recording = camera_id in self.camera_service.active_recordings
                    history['was_recording_before_failure'] = was_recording
                    logger.error(f"🛑 Video recovery exhausted ({self.max_recovery_attempts} attempts) for {camera_id} — stopping recording, will retry in 15 min")
                    if was_recording:
                        self.camera_service.stop_recording(camera_id)
                    history['video_exhausted_recording_stopped'] = True
                    history['video_exhausted_time'] = now
                
                # After 15 min cooldown, reset and try again
                if now - history.get('video_exhausted_time', now) >= self.video_exhausted_cooldown:
                    logger.info(f"🔄 15-min cooldown elapsed for {camera_id} — resetting video recovery attempts")
                    history['video_recovery_count'] = 0
                    history['video_exhausted_recording_stopped'] = False
                    history['video_exhausted_time'] = None
            else:
                self._recover_producer_video(camera_id)
                history['last_video_recovery'] = now
                history['video_recovery_count'] += 1
            history['producer_video_frozen'] = producer_video_lag
        else:
            history['producer_video_frozen'] = None
            # If we were in exhausted state and stream recovered, restart recording if it was active before
            if history.get('video_exhausted_recording_stopped'):
                logger.info(f"✅ Video stream recovered for {camera_id} after exhaustion")
                self._recover_recording(camera_id)
                history['video_exhausted_recording_stopped'] = False
                history['video_exhausted_time'] = None
            history['video_recovery_count'] = 0
            history['error_logged'] = False
            
        # Check producer audio thread lag    
        producer_audio_lag = current_lag.get('producer_audio_lag', {}).get(camera_id, 0)
        if producer_audio_lag > self.lag_threshold:
            self._recover_producer_audio(camera_id)
            history['last_audio_recovery'] = now
            history['audio_recovery_count'] += 1
            history['producer_audio_frozen'] = producer_audio_lag
        else:
            history['producer_audio_frozen'] = None
            history['audio_recovery_count'] = 0
            
        # Check recording subscriber lag
        recorder_lag = current_lag.get('recorder_lag', {}).get(camera_id, 0)
        if recorder_lag > self.lag_threshold:
            self._recover_recording(camera_id)
            history['last_recorder_recovery'] = now
            history['recorder_recovery_count'] += 1
            history['recorder_frozen'] = recorder_lag
        else:
            history['recorder_frozen'] = None
            history['recorder_recovery_count'] = 0
    
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
            if not read_times or len(read_times) == 0:
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
        lag_summary = [f"{k}: {v[camera_id]:.3f}s" for k, v in lag_stats.items() if camera_id in v]
        if lag_summary:
            logger.info(f"📊 Lag summary for {camera_id}: {', '.join(lag_summary)}")
  
        return lag_stats
    
    def _recover_producer_video(self, camera_id: str):
        """Recover video producer thread by restarting it."""
        history = self.lag_history.get(camera_id, {})
        logger.warning(f"Recovering video producer thread for camera {camera_id} (attempt {history.get('video_recovery_count', 0) + 1}/{self.max_recovery_attempts})")
        # Track recording state BEFORE stopping (stop_video_stream stops recording too)
        was_recording = camera_id in self.camera_service.active_recordings
        history['was_recording_before_failure'] = was_recording or history.get('was_recording_before_failure', False)
        self.camera_service.stop_video_stream(camera_id, stop_recording=False)
        time.sleep(2)  # Allow clean shutdown
        success = self.camera_service.start_video_stream(camera_id)
        if success:
            logger.info(f"✅ Successfully recovered video producer thread for camera {camera_id}")
        else:
            logger.error(f"❌ Failed to recover video producer thread for camera {camera_id}")
        

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
        
    def _recover_recording(self, camera_id: str):
        """Recover recording subscriber by restarting recording if it was active."""
        #is_recording = camera_id in self.camera_service.active_recordings
        #history = self.lag_history.get(camera_id, {})
        #was_recording = is_recording or history.get('was_recording_before_failure', False)
        
        #if not was_recording:
        #    return
        
        logger.warning(f"Recovering recording for camera {camera_id}")
        self.camera_service.stop_recording(camera_id)
        time.sleep(1)  # Brief pause
        
        self.camera_service.start_recording(camera_id)
        logger.info(f"✅ Successfully recovered recording for camera {camera_id}")
        #history['was_recording_before_failure'] = False
    
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
            
    def _check_thread_health(self):
        """Check if background threads are alive and log alongside lag status."""
        thread_status = {}
        
        for camera_id in list(self.camera_service._camera_streams.keys()):
            rec_info = self.camera_service.recording_manager.active_recordings.get(camera_id)
            threads = {
                'video': self.camera_service._video_background_threads.get(camera_id),
                'audio': self.camera_service._audio_background_threads.get(camera_id),
                'rec': rec_info.get('thread') if rec_info else None,
            }
            
            alive = {name: t.is_alive() if t else None for name, t in threads.items()}
            thread_status[camera_id] = alive
            
            status_parts = [f"{name}={'✅' if ok else '❌'}" for name, ok in alive.items() if ok is not None]
            if status_parts:
                logger.info(f"🧵 Thread status for {camera_id}: {', '.join(status_parts)}")
            
            dead = [name for name, t in threads.items() if t and not alive[name]]
            if dead:
                logger.error(f"⚠️ Dead threads detected for {camera_id}: {', '.join(dead)} — recovery needed")
        
        return thread_status

    def get_health_status(self, camera_id: str) -> Dict:
        """Get current health status for a camera."""
        current_lag = self._get_current_lag_stats(camera_id)
        history = self.lag_history.get(camera_id, {})
        
        now = time.time()
        return {
            'camera_id': camera_id,
            'lag_stats': current_lag,
            'health_issues': {
                'video_producer_frozen': history.get('producer_video_frozen') is not None,
                'audio_producer_frozen': history.get('producer_audio_frozen') is not None,
                'recorder_frozen': history.get('recorder_frozen') is not None,
            },
            'recovery_counts': {
                'video_recovery_count': history.get('video_recovery_count', 0),
                'audio_recovery_count': history.get('audio_recovery_count', 0), 
                'recorder_recovery_count': history.get('recorder_recovery_count', 0),
            },
            'needs_manual_refresh': (
                history.get('video_recovery_count', 0) >= self.max_recovery_attempts or
                history.get('audio_recovery_count', 0) >= self.max_recovery_attempts
            )
        }

    def get_lag_history(self, camera_id: str = None) -> Dict:
        """Get lag history for 24-hour plotting."""
        if camera_id:
            return {camera_id: list(self._lag_queues.get(camera_id, []))}
        return {cid: list(q) for cid, q in self._lag_queues.items()}


class DashboardService:
    def __init__(self, camera_service, window_seconds: int = 300, sample_interval_seconds: int = 5):
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

        # Initialize stream health monitor
        self.stream_monitor = StreamHealthMonitor(camera_service)

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
            except Exception:
                logger.exception("⚠️ _sample_loop iteration failed — monitoring continues")
            time.sleep(self.sample_interval_seconds)

    def _collect_sample(self):
        now = time.time()
        memory = psutil.virtual_memory()
        disk_counters = psutil.disk_io_counters()

        process = self._find_start_server_process()
        process_cpu = process.cpu_percent(interval=0.05)
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
            process_io = None

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

        with self._lock:
            self._samples.append(sample)
            cutoff = now - self.window_seconds
            while self._samples and self._samples[0]['timestamp'] < cutoff:
                self._samples.popleft()

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
        recording_dir_size_bytes = self._get_directory_size_bytes(self.camera_service.recordings_dir)
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
            },
            'processing_active': processing_active,
            'active_recordings': len(self.camera_service.active_recordings),
            'total_cameras': len(self.camera_service.get_cameras()),
            'total_recordings': len(self.camera_service.get_recordings()),
        }
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