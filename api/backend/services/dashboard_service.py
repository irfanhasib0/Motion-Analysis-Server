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
        self.lag_threshold = 5.0  # 5 seconds max lag before considering frozen
        self.duration_threshold = 180  # 3 minutes
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
            for camera_id in list(self.camera_service._camera_streams.keys()):
                self._check_camera_health(camera_id)
        except Exception as e:
            logger.error(f"Error in stream health monitoring: {e}")
    
    def _check_camera_health(self, camera_id: str):
        """Check health of a specific camera and trigger recovery if needed."""
        current_lag = self._get_current_lag_stats(camera_id)
        
        if camera_id not in self.lag_history:
            self.lag_history[camera_id] = {
                'producer_video_frozen_since': None,
                'producer_audio_frozen_since': None,
                'recording_frozen_since': None,
                'last_video_recovery': 0,
                'last_audio_recovery': 0,
                'last_recording_recovery': 0,
                'video_recovery_count': 0,
                'audio_recovery_count': 0,
                'recording_recovery_count': 0,
                'notified_streaming_consumers': set()  # Track which consumers we've already notified about
            }
        
        history = self.lag_history[camera_id]
        now = time.time()
        
        # Check streaming consumer lags and notify if threshold exceeded
        if current_lag.get('video_stream_lag', 0) > self.lag_threshold:
            if 'video_stream_lag' not in history['notified_streaming_consumers']:
                self._notify_frozen_stream(camera_id, 'video_stream', current_lag['video_stream_lag'])
                history['notified_streaming_consumers'].add('video_stream_lag')
        elif current_lag.get('video_stream_lag', 0) <= self.lag_threshold:
            history['notified_streaming_consumers'].discard('video_stream_lag')
        
        if current_lag.get('audio_stream_lag', 0) > self.lag_threshold:
            if 'audio_stream_lag' not in history['notified_streaming_consumers']:
                self._notify_frozen_stream(camera_id, 'audio_stream', current_lag['audio_stream_lag'])
                history['notified_streaming_consumers'].add('audio_stream_lag')
        elif current_lag.get('audio_stream_lag', 0) <= self.lag_threshold:
            history['notified_streaming_consumers'].discard('audio_stream_lag')
        
        # Check producer video thread lag
        if current_lag['producer_video_lag'] > self.lag_threshold:
            if history['producer_video_frozen_since'] is None:
                history['producer_video_frozen_since'] = now
            elif (now - history['producer_video_frozen_since'] > self.duration_threshold and 
                  now - history['last_video_recovery'] > self.recovery_cooldown and
                  history['video_recovery_count'] < self.max_recovery_attempts):
                self._recover_producer_video(camera_id)
                history['last_video_recovery'] = now
                history['video_recovery_count'] += 1
                history['producer_video_frozen_since'] = None
        else:
            history['producer_video_frozen_since'] = None
            
        # Check producer audio thread lag    
        if current_lag['producer_audio_lag'] > self.lag_threshold:
            if history['producer_audio_frozen_since'] is None:
                history['producer_audio_frozen_since'] = now
            elif (now - history['producer_audio_frozen_since'] > self.duration_threshold and
                  now - history['last_audio_recovery'] > self.recovery_cooldown and
                  history['audio_recovery_count'] < self.max_recovery_attempts):
                self._recover_producer_audio(camera_id)
                history['last_audio_recovery'] = now
                history['audio_recovery_count'] += 1
                history['producer_audio_frozen_since'] = None
        else:
            history['producer_audio_frozen_since'] = None
            
        # Check recording subscriber lag
        if current_lag['recording_lag'] > self.lag_threshold:
            if history['recording_frozen_since'] is None:
                history['recording_frozen_since'] = now
            elif (now - history['recording_frozen_since'] > self.duration_threshold and
                  now - history['last_recording_recovery'] > self.recovery_cooldown and
                  history['recording_recovery_count'] < self.max_recovery_attempts):
                self._recover_recording(camera_id)
                history['last_recording_recovery'] = now
                history['recording_recovery_count'] += 1
                history['recording_frozen_since'] = None
        else:
            history['recording_frozen_since'] = None
    
    def _get_current_lag_stats(self, camera_id: str) -> Dict[str, float]:
        """Get current lag statistics for a camera."""
        lag_stats = {
            'producer_video_lag': 0.0,
            'producer_audio_lag': 0.0, 
            'recording_lag': 0.0,
            'video_stream_lag': 0.0,
            'audio_stream_lag': 0.0,
            'consumer_lags': {}  # consumer_id -> lag
        }
        
        now = time.time()
        
        # Get frame ring buffer for video lag
        frame_buffer = self.camera_service._frame_ring_buffers.get(camera_id)
        if frame_buffer and hasattr(frame_buffer, 'last_write_time'):
            # Get producer write time
            last_write = frame_buffer.last_write_time
            if last_write:
                lag_stats['producer_video_lag'] = now - last_write
            
            # Get consumer lag for each consumer
            if hasattr(frame_buffer, '_consumer_read_times'):
                for consumer_id, read_times in frame_buffer._consumer_read_times.items():
                    if read_times and len(read_times) > 0:
                        last_read = read_times[-1]
                        if last_read:  # Only calculate lag if we have a valid read time
                            consumer_lag = now - last_read
                            lag_stats['consumer_lags'][consumer_id] = consumer_lag
                            
                            # Special handling for recording consumer (only when lag is valid)
                            if consumer_id.startswith(f'recorder_{camera_id}'):
                                lag_stats['recording_lag'] = max(lag_stats['recording_lag'], consumer_lag)
                            if consumer_id == f'video_stream_{camera_id}_overlay':
                                lag_stats['video_stream_lag'] = consumer_lag
                            if consumer_id == f'audio_stream_{camera_id}_audio':
                                lag_stats['audio_stream_lag'] = consumer_lag
        
        # Get audio ring buffer for audio lag
        audio_buffer = self.camera_service._audio_ring_buffers.get(camera_id)
        if audio_buffer and hasattr(audio_buffer, 'last_write_time'):
            last_write = audio_buffer.last_write_time
            if last_write:
                lag_stats['producer_audio_lag'] = now - last_write
  
        return lag_stats
    
    def _recover_producer_video(self, camera_id: str):
        """Recover video producer thread by restarting it."""
        try:
            logger.warning(f"Recovering video producer thread for camera {camera_id}")
            self.camera_service.stop_video_stream(camera_id)
            time.sleep(2)  # Allow clean shutdown
            success = self.camera_service.start_video_stream(camera_id)
            if success:
                logger.info(f"Successfully recovered video producer thread for camera {camera_id}")
            else:
                logger.error(f"Failed to recover video producer thread for camera {camera_id}")
        except Exception as e:
            logger.error(f"Error recovering video producer thread for camera {camera_id}: {e}")
    
    def _recover_producer_audio(self, camera_id: str):
        """Recover audio producer thread by restarting it."""
        try:
            logger.warning(f"Recovering audio producer thread for camera {camera_id}")
            self.camera_service.stop_audio_stream(camera_id)
            time.sleep(2)  # Allow clean shutdown
            success = self.camera_service.start_audio_stream(camera_id)
            if success:
                logger.info(f"Successfully recovered audio producer thread for camera {camera_id}")
            else:
                logger.error(f"Failed to recover audio producer thread for camera {camera_id}")
        except Exception as e:
            logger.error(f"Error recovering audio producer thread for camera {camera_id}: {e}")
    
    def _recover_recording(self, camera_id: str):
        """Recover recording subscriber by restarting recording."""
        try:
            if camera_id in self.camera_service.active_recordings:
                logger.warning(f"Recovering recording for camera {camera_id}")
                self.camera_service.stop_recording(camera_id)
                time.sleep(1)  # Brief pause
                self.camera_service.start_recording(camera_id)
                logger.info(f"Successfully recovered recording for camera {camera_id}")
        except Exception as e:
            logger.error(f"Error recovering recording for camera {camera_id}: {e}")
    
    def _notify_frozen_stream(self, camera_id: str, stream_type: str, lag_seconds: float):
        """Notify when a stream appears frozen based on lag time."""
        try:
            logger.warning(f"Stream frozen detected: camera {camera_id}, {stream_type} stream, lag: {lag_seconds:.2f}s")
            # Could extend this to send alerts via webhook, email, etc.
            # For now, just log the frozen stream detection
        except Exception as e:
            logger.error(f"Error in frozen stream notification for {camera_id}: {e}")
            
    def get_health_status(self, camera_id: str) -> Dict:
        """Get current health status for a camera."""
        current_lag = self._get_current_lag_stats(camera_id)
        history = self.lag_history.get(camera_id, {})
        
        now = time.time()
        return {
            'camera_id': camera_id,
            'lag_stats': current_lag,
            'health_issues': {
                'video_producer_frozen': history.get('producer_video_frozen_since') is not None,
                'audio_producer_frozen': history.get('producer_audio_frozen_since') is not None,
                'recording_frozen': history.get('recording_frozen_since') is not None,
            },
            'recovery_counts': {
                'video_recovery_count': history.get('video_recovery_count', 0),
                'audio_recovery_count': history.get('audio_recovery_count', 0), 
                'recording_recovery_count': history.get('recording_recovery_count', 0),
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
            except Exception as error:
                logger.warning(f"Dashboard sampler failed: {error}")
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
                    self.camera_service.db.apply_preset('low_power')
                    # Apply the preset to runtime settings
                    presets = self.camera_service.db.get_presets()
                    if 'low_power' in presets:
                        preset_settings = presets['low_power']
                        self.camera_service.update_runtime_settings(**preset_settings)
                except Exception as e:
                    logger.error(f"Failed to switch to low_power preset: {e}")
                    
            elif available_ram >= ram_threshold * 1.2 and current_preset == 'low_power':
                # Add 20% hysteresis to prevent oscillation
                logger.info(f"RAM above threshold with hysteresis ({available_ram / (1024**3):.2f}GB >= {ram_threshold * 1.2 / (1024**3):.2f}GB), switching to default preset")
                try:
                    self.camera_service.db.apply_preset('default')
                    # Apply the preset to runtime settings
                    presets = self.camera_service.db.get_presets()
                    if 'default' in presets:
                        preset_settings = presets['default']
                        self.camera_service.update_runtime_settings(**preset_settings)
                except Exception as e:
                    logger.error(f"Failed to switch to default preset: {e}")
                    
        except Exception as e:
            logger.error(f"Error in RAM threshold check: {e}")