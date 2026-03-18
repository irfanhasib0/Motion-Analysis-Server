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
        self.lag_threshold = 120.0  # 5 seconds max lag before considering frozen
        self.recovery_cooldown = 300  # 5 minutes between attempts
        self.max_recovery_attempts = 3
        
        self._last_check = 0
        self._check_interval = 30  # Check every 30 seconds
        
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
                'notified_streaming_consumers': set()  # Track which consumers we've already notified about
            }
        
        history = self.lag_history[camera_id]
        now = time.time()
        
        # Check streaming consumer lags and notify if threshold exceeded
        video_stream_lag = current_lag.get('video_stream_lag', {}).get(camera_id, 0)
        if video_stream_lag > self.lag_threshold:
            if 'video_stream_lag' not in history['notified_streaming_consumers']:
                self._notify_frozen_stream(camera_id, 'video_stream', video_stream_lag)
                history['notified_streaming_consumers'].add('video_stream_lag')
        elif video_stream_lag <= self.lag_threshold:
            history['notified_streaming_consumers'].discard('video_stream_lag')
        
        audio_stream_lag = current_lag.get('audio_stream_lag', {}).get(camera_id, 0)
        if audio_stream_lag > self.lag_threshold:
            if 'audio_stream_lag' not in history['notified_streaming_consumers']:
                self._notify_frozen_stream(camera_id, 'audio_stream', audio_stream_lag)
                history['notified_streaming_consumers'].add('audio_stream_lag')
        elif audio_stream_lag <= self.lag_threshold:
            history['notified_streaming_consumers'].discard('audio_stream_lag')
        
        # Check producer video thread lag
        producer_video_lag = current_lag.get('producer_video_lag', {}).get(camera_id, 0)
        if producer_video_lag > self.lag_threshold:
            self._recover_producer_video(camera_id)
            history['last_video_recovery'] = now
            history['video_recovery_count'] += 1
            history['producer_video_frozen'] = producer_video_lag
        else:
            history['producer_video_frozen'] = None
            history['video_recovery_count'] = 0
            
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
        
        # Get frame ring buffer for video lag
        frame_buffer = self.camera_service._frame_ring_buffers.get(camera_id)
        if frame_buffer and hasattr(frame_buffer, 'last_write_time'):
            # Get producer write time
            last_write = frame_buffer.last_write_time
            if last_write:
                video_producer_lag = now - last_write
                lag_stats['producer_video_lag'][camera_id] = video_producer_lag
            
            # Get consumer lag for each consumer
            if hasattr(frame_buffer, '_consumer_read_times'):
                for consumer_id, read_times in frame_buffer._consumer_read_times.items():
                    if read_times and len(read_times) > 0:
                        last_read = read_times[-1]
                        if last_read:  # Only calculate lag if we have a valid read time
                            consumer_lag = now - last_read
                            
                            # Special handling for recording consumer (only when lag is valid)
                            if f'recorder_{camera_id}' in consumer_id:
                                lag_stats['recorder_lag'][camera_id] = consumer_lag
                            elif f'video_stream_{camera_id}' in consumer_id:
                                lag_stats['video_stream_lag'][camera_id] = consumer_lag
        
        # Get audio ring buffer for audio lag
        audio_buffer = self.camera_service._audio_ring_buffers.get(camera_id)
        if audio_buffer and hasattr(audio_buffer, 'last_write_time'):
            last_write = audio_buffer.last_write_time
            if last_write:
                audio_producer_lag = now - last_write
                lag_stats['producer_audio_lag'][camera_id] = audio_producer_lag
            
            # Get audio consumer lag
            if hasattr(audio_buffer, '_consumer_read_times'):
                for consumer_id, read_times in audio_buffer._consumer_read_times.items():
                    if read_times and len(read_times) > 0:
                        last_read = read_times[-1]
                        if last_read:  # Only calculate lag if we have a valid read time
                            consumer_lag = now - last_read
                            # Special handling for recording consumer (only when lag is valid)
                            if f'recorder_{camera_id}' in consumer_id:
                                lag_stats['recorder_lag'][camera_id] = max(lag_stats['recorder_lag'].get(camera_id, 0), consumer_lag)
                            if f'audio_stream_{camera_id}' in consumer_id:
                                lag_stats['audio_stream_lag'][camera_id] = consumer_lag
        
        # Summary log for high-level lag overview
        lag_summary = []
        for lag_type, lag_data in lag_stats.items():
            if lag_data and camera_id in lag_data:
                lag_summary.append(f"{lag_type}: {lag_data[camera_id]:.3f}s")
        
        if lag_summary:
            logger.info(f"📊 Lag summary for {camera_id}: {', '.join(lag_summary)}")
  
        return lag_stats
    
    def _recover_producer_video(self, camera_id: str):
        """Recover video producer thread by restarting it."""
        is_recording = camera_id in self.camera_service.active_recordings
        logger.warning(f"Recovering video producer thread for camera {camera_id}")
        self.camera_service.stop_video_stream(camera_id)
        time.sleep(2)  # Allow clean shutdown
        success = self.camera_service.start_video_stream(camera_id)
        if success:
            logger.info(f"Successfully recovered video producer thread for camera {camera_id}")
        else:
            logger.error(f"Failed to recover video producer thread for camera {camera_id}")
        if is_recording:
            self._recover_recording(camera_id)

    def _recover_producer_audio(self, camera_id: str):
        """Recover audio producer thread by restarting it."""
        logger.warning(f"Recovering audio producer thread for camera {camera_id}")
        self.camera_service.stop_audio_stream(camera_id)
        time.sleep(2)  # Allow clean shutdown
        success = self.camera_service.start_audio_stream(camera_id)
        if success:
            logger.info(f"Successfully recovered audio producer thread for camera {camera_id}")
        else:
            logger.error(f"Failed to recover audio producer thread for camera {camera_id}")
        
    def _recover_recording(self, camera_id: str):
        """Recover recording subscriber by restarting recording."""
        logger.warning(f"Recovering recording for camera {camera_id}")
        self.camera_service.stop_recording(camera_id)
        time.sleep(1)  # Brief pause
        self.camera_service.start_recording(camera_id)
        logger.info(f"Successfully recovered recording for camera {camera_id}")
    
    def _notify_frozen_stream(self, camera_id: str, stream_type: str, lag_seconds: float):
        """Notify when a stream appears frozen based on lag time."""
        logger.warning(f"Stream frozen detected: camera {camera_id}, {stream_type} stream, lag: {lag_seconds:.2f}s")
        # Could extend this to send alerts via webhook, email, etc.
        # For now, just log the frozen stream detection
            
    def _check_thread_health(self):
        """Check if background threads are alive and log alongside lag status."""
        thread_status = {}
        
        for camera_id in list(self.camera_service._camera_streams.keys()):
            video_thread = self.camera_service._video_background_threads.get(camera_id)
            audio_thread = self.camera_service._audio_background_threads.get(camera_id)
            rec_info = self.camera_service.recording_manager.active_recordings.get(camera_id)
            rec_thread = rec_info.get('thread') if rec_info else None
            
            video_alive = video_thread.is_alive() if video_thread else None
            audio_alive = audio_thread.is_alive() if audio_thread else None
            rec_alive = rec_thread.is_alive() if rec_thread else None
            
            thread_status[camera_id] = {
                'video': video_alive,
                'audio': audio_alive,
                'recording': rec_alive,
            }
            
            # Warn on dead threads that should be alive
            dead = []
            if video_thread and not video_alive:
                dead.append('video')
            if audio_thread and not audio_alive:
                dead.append('audio')
            if rec_thread and not rec_alive:
                dead.append('recording')
            
            status_parts = []
            if video_thread is not None:
                status_parts.append(f"video={'✅' if video_alive else '💀'}")
            if audio_thread is not None:
                status_parts.append(f"audio={'✅' if audio_alive else '💀'}")
            if rec_thread is not None:
                status_parts.append(f"rec={'✅' if rec_alive else '💀'}")
            
            if status_parts:
                logger.info(f"🧵 Thread status for {camera_id}: {', '.join(status_parts)}")
            
            if dead:
                logger.error(f"💀 Dead threads detected for {camera_id}: {', '.join(dead)} — recovery needed")
        
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
                logger.exception("💀 _sample_loop iteration failed — monitoring continues")
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
                'recording_dir_size_gb': recording_dir_size_bytes / (1024 ** 3),
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