import numpy as np
import os
import select
import subprocess
import time
from datetime import datetime
from typing import Dict, List, Optional, Union
import psutil
import logging

from models.camera import Camera, CameraCreate, CameraUpdate, CameraType, CameraStatus
from models.recording import Recording
#from services.database_service import DatabaseService
from services.config_manager import ConfigManager
from services.streaming_service import StreamingService
from services.recording_manager import RecordingManager
from services.audio_recording_utils import AudioRecordingUtils

import sys
sys.path.append('../../src')
from improc.optical_flow import OpticalFlowTracker
from audioproc import FrequencyIntensityAnalyzer

logger = logging.getLogger(__name__)

class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    GRAY = '\033[90m'
    BOLD = '\033[1m'
    RESET = '\033[0m'  # Reset to default

class Capture:
    def __init__(
        self,
        source: Union[str, int],
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        low_power_mode: bool = False,
        audio_sample_rate: Optional[int] = None,
        audio_channels: Optional[int] = None,
        audio_chunk_size: Optional[int] = None,
        audio_input_format: Optional[str] = None,
        audio_source: Optional[str] = None,
        rtsp_unified_demux_enabled: Optional[bool] = None,
        pipe_buffer_size: Optional[int] = None,
        audio_only: bool = False,
        audio_enabled: bool = True,
    ):
        self.source = source
        self.cam_type = None
        self.width = width
        self.height = height
        self.fps = fps
        self.low_power_mode = low_power_mode
        self.cap = None
        self._consecutive_read_failures = 0
        self._max_video_read_failures_before_reconnect = 30
        self._max_audio_read_failures_before_reconnect = 30
        self._reconnect_cooldown_sec = 2.0
        self._last_reconnect_at = 0.0
        self.audio_enabled = audio_enabled
        self.audio_cap = None
        self.audio_sample_rate = int(audio_sample_rate or 16000)
        self.audio_channels = int(audio_channels or os.getenv('AUDIO_CHANNELS', '1'))
        # Store as byte count: samples × channels × 2 bytes/sample (s16le)
        self._audio_chunk_bytes = max(256, int(audio_chunk_size or 512)) * self.audio_channels * 2
        self.audio_input_format = str(audio_input_format or os.getenv('AUDIO_INPUT_FORMAT', 'pulse')).strip().lower()
        self.audio_source = str(audio_source or os.getenv('AUDIO_SOURCE', 'default')).strip()
        self._audio_reconnect_cooldown_sec = 3.0
        self._last_audio_reconnect_at = 0.0
        self.pipe_buffer_size = int(pipe_buffer_size) if pipe_buffer_size is not None else None
        self._rtsp_unified_demux_enabled = bool(rtsp_unified_demux_enabled) and self.audio_enabled
        self._rtsp_unified_demux_active = False
        self._audio_pipe_read_fd: Optional[int] = None
        self._audio_pipe_write_fd: Optional[int] = None
        self._audio_pipe_reader = None
        self._audio_chunk_leftover = b""

        try:
            source = int(source)
        except:
            source = str(source)

        if isinstance(source, str) and source.startswith(('rtsp://', 'rtmp://')):
            self.cam_type = 'rtsp'
        elif isinstance(source, str) and source.startswith(('http://', 'https://')):
            self.cam_type = 'http'
        elif type(source) == int or (isinstance(source, str) and source.split('.')[-1] in ['mp4', 'avi', 'mkv', 'mov']):
            self.cam_type = 'webcam'
        else:
            raise ValueError(f"Unsupported camera source: {source}")

        if audio_only:
            self.open_audio()
        else:
            self.open_video()

    def _resolve_pipe_buffer_size(self) -> int:
        if self.pipe_buffer_size is not None:
            return max(65536, int(self.pipe_buffer_size))
        return 10**6 if self.low_power_mode else 10**8

    def _close_unified_audio_pipe(self):
        if self._audio_pipe_reader is not None:
            try:
                self._audio_pipe_reader.close()
            except Exception:
                pass
            self._audio_pipe_reader = None

        if self._audio_pipe_read_fd is not None:
            try:
                os.close(self._audio_pipe_read_fd)
            except Exception:
                pass
            self._audio_pipe_read_fd = None

        if self._audio_pipe_write_fd is not None:
            try:
                os.close(self._audio_pipe_write_fd)
            except Exception:
                pass
            self._audio_pipe_write_fd = None

    def _open_rtsp_av_stream_unified(self) -> bool:
        self._close_unified_audio_pipe()
        self._rtsp_unified_demux_active = False

        
        read_fd, write_fd = os.pipe()
        os.set_inheritable(write_fd, True)
        self._audio_pipe_read_fd = read_fd
        self._audio_pipe_write_fd = write_fd
    
        cmd = [
            "ffmpeg",
            "-hide_banner",                         # suppress version/config noise on startup
            "-loglevel", "error",                   # surface only actual ffmpeg errors
            "-rtsp_transport", "tcp",               # use TCP for RTSP — more reliable than UDP on lossy networks
            "-threads", "1",                        # limit decoder threads to avoid CPU contention
            "-fflags", "nobuffer+discardcorrupt",   # disable input buffering for low latency; drop corrupt packets
            "-flags", "low_delay",                  # hint decoder to prefer low-latency over quality
            "-avioflags", "direct",                 # bypass protocol-layer read-ahead buffering
            "-analyzeduration", "1000000",          # limit stream analysis to 1 s (faster startup)
            "-probesize", "1000000",                # cap probe read to 1 MB (faster startup)
            "-i", self.source,                      # RTSP/HTTP input URL
            # --- video output → pipe:1 (stdout) ---
            "-map", "0:v:0",                        # select first video stream from input
            "-vf", f"fps={self.fps},scale={self.width}:{self.height}",  # normalize fps and resolution
            "-pix_fmt", "bgr24",                    # raw BGR24 — matches numpy/OpenCV layout
            "-f", "rawvideo",                       # emit uncompressed raw frames (no container)
            "pipe:1",                               # write video frames to stdout
            # --- audio output → pipe:3 (inherited fd) ---
            "-map", "0:a:0?",                       # select first audio stream if present (? = optional)
            "-ac", str(self.audio_channels),        # downmix/upmix to target channel count
            "-ar", str(self.audio_sample_rate),     # resample to target rate (Hz)
            "-af", "volume=3.0",                    # amplify audio 3x for better volume
            "-f", "s16le",                          # raw signed 16-bit little-endian PCM
            "pipe:3",                               # write audio PCM to the inherited pipe fd
        ]

        pipe_buffer_size = self._resolve_pipe_buffer_size()
        self.cap = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=pipe_buffer_size,
            pass_fds=(write_fd,),
        )

        os.close(write_fd)
        
        self._audio_pipe_write_fd = None

        self._audio_pipe_reader = os.fdopen(read_fd, 'rb', buffering=0)
        self._audio_pipe_read_fd = None
        self._rtsp_unified_demux_active = True
        logger.info(f"RTSP unified demux enabled for source: {self.source}")
        ret = self.is_video_stream_opened()
        if ret:
            logger.info(f"RTSP unified demux stream opened successfully: {self.source}")
        return ret
        
    def open_video_stream_webcam(self):
        if self.cap:
            self.release_video_stream_webcam()

        webcam_input_format = os.getenv('WEBCAM_INPUT_FORMAT', 'v4l2').strip().lower()
        webcam_source = self.source
        is_webcam_device = isinstance(webcam_source, int)
        if isinstance(webcam_source, int):
            webcam_source = f"/dev/video{webcam_source}"

        cmd = [
            "ffmpeg",
            "-hide_banner",            # reduce non-critical startup logs
            "-loglevel", "error",      # surface only ffmpeg errors
            "-fflags", "nobuffer",     # minimize input buffering/latency
            "-flags", "low_delay",     # request low-latency decoding path
        ]

        if is_webcam_device:
            cmd += [
                "-f", webcam_input_format,   # webcam input backend (default: v4l2)
                "-framerate", str(self.fps), # capture fps from device when supported
                "-video_size", f"{self.width}x{self.height}",  # requested capture resolution
                "-i", str(webcam_source),    # webcam device path
            ]
        else:
            cmd += [
                "-i", str(webcam_source),    # file/video source path
            ]

        cmd += [
            "-an",                       # disable audio in video pipeline
            "-vf", f"fps={self.fps},scale={self.width}:{self.height}",  # normalize output fps/size
            "-pix_fmt", "bgr24",        # raw BGR frames for OpenCV/Numpy
            "-f", "rawvideo",           # emit raw byte frames
            "pipe:1",
        ]

        pipe_buffer_size = self._resolve_pipe_buffer_size()
        self.cap = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=pipe_buffer_size,
        )
        
        return self

    def open_video_stream_rtsp(self):
        if self.cap:
            self.release_video_stream_rtsp()

        if self._rtsp_unified_demux_enabled and self.cam_type in {'rtsp', 'http'}:
            if self._open_rtsp_av_stream_unified():
                return self
            logger.warning(f"{Colors.RED}OpenVideo:: Falling back to split RTSP capture for source: {self.source}{Colors.RESET}")

        cmd = [
            "ffmpeg",
            "-hide_banner",                     # reduce non-critical startup logs
            "-loglevel", "error",               # surface only ffmpeg errors
            "-rtsp_transport", "tcp",           # prefer TCP for RTSP reliability
            "-threads", "1",                    # bounded decoder thread usage
            #"-rw_timeout", "5000000",
            "-fflags", "nobuffer+discardcorrupt",  # low-latency read; drop corrupt packets
            "-flags", "low_delay",              # low-latency decode behavior
            "-avioflags", "direct",             # reduce protocol-layer buffering
            "-analyzeduration", "1000000",      # faster startup probing window
            "-probesize", "1000000",            # bounded probe size for startup latency
            "-i", self.source,
            "-an",                             # disable audio in video pipeline
            "-vf", f"fps={self.fps},scale={self.width}:{self.height}",  # normalize output fps/size
            "-pix_fmt", "bgr24",             # raw BGR frames for OpenCV/Numpy
            "-f", "rawvideo",
            "pipe:1"
        ]
        
        pipe_buffer_size = self._resolve_pipe_buffer_size()
        self.cap = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=pipe_buffer_size)
        self._rtsp_unified_demux_active = False
        return self

    @staticmethod
    def _resolve_pulse_source(source: str) -> str:
        """Return the real PulseAudio source name for 'default', or *source* unchanged.

        Runs ``pactl get-default-source`` (PulseAudio / PipeWire-pulse).
        Falls back to the original value on any error so FFmpeg can still try.
        """
        try:
            result = subprocess.run(
                ['pactl', 'get-default-source'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            name = result.stdout.decode().strip()
            if name:
                return name
        except Exception:
            pass
        return source

    def open_audio_stream_webcam(self):
        if self.audio_cap:
            self.release_audio_stream_webcam()

        input_format = self.audio_input_format
        input_source = self.audio_source
        if input_format == 'alsa' and input_source.lower() == 'default':
            input_source = os.getenv('AUDIO_SOURCE_ALSA', 'hw:1,0').strip() or 'hw:1,0'
        elif input_format == 'pulse' and input_source.lower() == 'default':
            input_source = self._resolve_pulse_source(input_source)
            
        cmd = [
            'ffmpeg',
            '-hide_banner',             # reduce non-critical startup logs
            '-loglevel', 'error',       # surface only ffmpeg errors
            '-f', input_format,         # input audio backend (pulse/alsa/etc.)
            '-i', input_source,         # input device/source name
            '-ac', str(self.audio_channels),      # output channel count
            '-ar', str(self.audio_sample_rate),   # output sampling rate
            '-af', 'volume=3.0',        # amplify audio 3x for better volume
            '-f', 's16le',              # raw PCM 16-bit little-endian
            'pipe:1',
        ]
        
        
        self.audio_cap = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=max(10**6, self._audio_chunk_bytes * 16),
        )
        return self

    def open_audio_stream_rtsp(self):
        if self._rtsp_unified_demux_active and self.cap and self.cap.poll() is None and self._audio_pipe_reader is not None:
            return self

        if self.audio_cap:
            self.release_audio_stream_rtsp()

        cmd = [
            'ffmpeg',
            '-hide_banner',             # reduce non-critical startup logs
            '-loglevel', 'error',       # surface only ffmpeg errors
            '-rtsp_transport', 'tcp',   # prefer TCP for RTSP reliability
            '-i', self.source,
            '-c:a', 'pcm_s16le',         # decode audio to raw PCM in ffmpeg for simplicity and reliability
            '-map', '0:a:0',             # select first audio stream if present (? = optional)
            '-vn',                      # disable video in audio pipeline
            '-ac', str(self.audio_channels),      # output channel count
            '-ar', str(self.audio_sample_rate),   # output sampling rate
            '-af', 'volume=3.0',        # amplify audio 3x for better volume
            '-acodec', 'pcm_s16le',          # raw PCM 16-bit little-endian
            '-f', 's16le',              # raw PCM 16-bit little-endian
            'pipe:1',
        ]
        
        self.audio_cap = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
                bufsize=max(10**6, self._audio_chunk_bytes * 16),
            )
        return self
    
    def open_video(self):
        if self.cam_type in ['rtsp', 'http']:
            return self.open_video_stream_rtsp()
        elif self.cam_type == 'webcam':
            return self.open_video_stream_webcam()

    def open_audio(self):
        if self.cam_type in ['rtsp', 'http']:
            return self.open_audio_stream_rtsp()
        elif self.cam_type == 'webcam':
            return self.open_audio_stream_webcam()
        
    def is_video_stream_opened(self):
        return bool(self.cap and self.cap.poll() is None and self.cap.stdout is not None)

    def is_audio_stream_opened(self):
        if self._rtsp_unified_demux_active:
            return bool(self.cap and self.cap.poll() is None and self._audio_pipe_reader is not None)
        ret = self.audio_cap and self.audio_cap.poll() is None and self.audio_cap.stdout is not None
        return ret

    def reconnect_video_stream(self) -> bool:
        now = time.time()
        if now - self._last_reconnect_at < self._reconnect_cooldown_sec:
            return self.is_video_stream_opened()

        self._last_reconnect_at = now
        logger.warning(f"{Colors.RED}Reconnecting video stream source: {self.source}{Colors.RESET}")
        
        self.release_video()
        self.open_video()
        return self.is_video_stream_opened()
    
    def reconnect_audio_stream(self):
        logger.warning(f"{Colors.RED}Reconnecting audio stream for source: {self.source}{Colors.RESET}")
        now = time.time()
        if now - self._last_audio_reconnect_at < self._audio_reconnect_cooldown_sec:
            return self.is_audio_stream_opened()
        
        self._last_audio_reconnect_at = now
        
        # Add delay before reconnecting to avoid aggressive reconnections
        time.sleep(0.5)
        
        self.release_audio()
        self.open_audio()
        if self._rtsp_unified_demux_active:
            self.open_video_stream_rtsp()
        return self.is_audio_stream_opened()
        
    def read_video(self):
        if not self.cap or not self.is_video_stream_opened():
            if not self.reconnect_video_stream():
                return False, None

        frame_size = self.width * self.height * 3
        raw = self.cap.stdout.read(frame_size)
        
        if len(raw) == frame_size:
            self._consecutive_read_failures = 0
            frame = np.frombuffer(raw, np.uint8).reshape((self.height, self.width, 3))
            return True, frame

        # Partial/empty read — track failures and trigger reconnect when threshold exceeded
        self._consecutive_read_failures += 1
        if self._consecutive_read_failures >= self._max_video_read_failures_before_reconnect:
            self._consecutive_read_failures = 0
            logger.warning(f"{Colors.RED}ReadVideo:: Video read failure threshold exceeded, reconnecting stream: {self.source}{Colors.RESET }")
            self.reconnect_video_stream()
        return False, None

    def read_audio(self):
        # Unified demux: audio arrives on the dedicated pipe reader, not audio_cap
        if not self.is_audio_stream_opened():
            if not self.reconnect_audio_stream():
                return False, b'a'
            
        read_success = False
        if self._rtsp_unified_demux_active and self._audio_pipe_reader is not None:
            pipe = self._audio_pipe_reader
        elif self.audio_cap is not None and self.audio_cap.stdout is not None:
            pipe = self.audio_cap.stdout
        else:
            read_success = False

        # Increased timeout to handle network latency and audio device delays
        try:
            ready, _, _ = select.select([pipe], [], [], 0.5)  # Increased timeout for stability
            if not ready:
                return False, b''
            
            # Read available data without forcing exact chunk size
            available_data = pipe.read(self._audio_chunk_bytes)
            if not available_data:
                return False, b''
                
            chunk = self._audio_chunk_leftover + available_data
            usable = len(chunk) - (len(chunk) % 2)
            chunk = chunk[:usable]
            self._audio_chunk_leftover = chunk[usable:]
            read_success = True
        except Exception as e:
            logger.error(f"{Colors.RED}ReadAudio:: Error reading audio chunk: {e}{Colors.RESET}")
            read_success = False

        if read_success:
            self.consecutive_audio_read_failures = 0
            return True, chunk
        else:
            self.consecutive_audio_read_failures += 1
            if self.consecutive_audio_read_failures >= self._max_audio_read_failures_before_reconnect:
                self.consecutive_audio_read_failures = 0
                self.reconnect_audio_stream()
        return False, b'a'

    def release_video(self):
        if self.cap:
            try:
                if self.cap.stdout:
                    self.cap.stdout.close()
            except Exception:
                pass

            try:
                self.cap.terminate()
                self.cap.wait(timeout=1.0)
            except Exception:
                try:
                    self.cap.kill()
                except Exception:
                    pass
            self.cap = None
        self._rtsp_unified_demux_active = False
        self._close_unified_audio_pipe()

    def release_audio_stream_webcam(self):
        if self.audio_cap:
            try:
                if self.audio_cap.stdout:
                    self.audio_cap.stdout.close()
            except Exception:
                pass
            try:
                if self.audio_cap.stderr:
                    self.audio_cap.stderr.close()
            except Exception:
                pass

            try:
                self.audio_cap.terminate()
                self.audio_cap.wait(timeout=1.0)
            except Exception:
                try:
                    self.audio_cap.kill()
                except Exception:
                    pass
            self.audio_cap = None

    def release_audio_stream_rtsp(self):
        if self._rtsp_unified_demux_active:
            self._close_unified_audio_pipe()
            return
        self.release_audio_stream_webcam()
            
    def release_audio(self):
        """Release only the audio pipeline (mirrors release() for video)."""
        if self.cam_type in ['rtsp', 'http']:
            self.release_audio_stream_rtsp()
        elif self.cam_type == 'webcam':
            self.release_audio_stream_webcam()


class CameraService(StreamingService):
    def __init__(self, configs: Optional[str] = 'default'):
        self.ram_auto_low_power_enabled = True
        self.low_power_ram_threshold_bytes = 1 * 1024 * 1024 * 1024
        self.rtsp_unified_demux_enabled = str(os.getenv('RTSP_UNIFIED_DEMUX', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
        self.low_power_mode = self._should_auto_enable_low_power()
        self.root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
        self.db = ConfigManager(configs_dir=os.path.join(self.root_dir, 'configs', configs))  # Use same YAML-backed DB as recording service
        
        # Load system settings (ConfigManager now reads from presets automatically)
        _sys = self.db.get_system_settings()
        
        # Initialize streaming service with ring buffer settings
        frame_rbf_len = int(_sys.get('frame_rbf_len', 10))
        audio_rbf_len = int(_sys.get('audio_rbf_len', 10))
        results_rbf_len = int(_sys.get('results_rbf_len', 10))
        super().__init__(frame_rbf_len=frame_rbf_len, audio_rbf_len=audio_rbf_len, results_rbf_len=results_rbf_len)
        
        self.recordings_dir = os.path.join(self.root_dir, "recordings")
        self.archive_dir = os.path.join(self.root_dir, "archive")
        self.start_time = datetime.now()
        
        # Create recordings and archive directories
        os.makedirs(self.recordings_dir, exist_ok=True)
        os.makedirs(self.archive_dir, exist_ok=True)
        
        self._camera_streams = {}
        self._camera_trackers = {}
        self._audio_chunk_analyzers = {}
        
        self.motion_check_interval = int(_sys.get('motion_check_interval', 10))
        self.max_clip_length = int(_sys.get('max_clip_length', 60))
        self.max_velocity = float(_sys.get('max_vel', 0.1))
        self.max_bg_diff = int(_sys.get('bg_diff', 50))
        self.motion_result_max_age_sec = 3.0  # not persisted
        self.min_free_storage_bytes = int(_sys.get('min_free_storage_bytes', 1 * 1024 * 1024 * 1024))
        if 'low_power_mode' in _sys:
            self.low_power_mode = bool(_sys['low_power_mode'])
        self.sensitivity = int(_sys.get('sensitivity', 2 if self.low_power_mode else 4))
        self.jpeg_quality = int(_sys.get('jpeg_quality', 55 if self.low_power_mode else 70))
        self.pipe_buffer_size = int(_sys.get('pipe_buffer_size', 10**6 if self.low_power_mode else 10**8))
        self.rtsp_unified_demux_enabled = bool(_sys.get('rtsp_unified_demux_enabled', self.rtsp_unified_demux_enabled))
        self.live_stream_mode = str(_sys.get('live_stream_mode', 'mjpeg')).lower()
        
        # Store ring buffer settings for runtime updates
        self.frame_rbf_len = frame_rbf_len
        self.audio_rbf_len = audio_rbf_len
        self.results_rbf_len = results_rbf_len
        self.mux_realtime = bool(_sys.get('mux_realtime', False))

        self.default_sensitivity = int(self.sensitivity)

        # Initialize audio recording utils
        audio_utils = AudioRecordingUtils(self.recordings_dir)
        audio_utils.set_camera_service(self)  # Set reference to camera service

        self.recording_manager = RecordingManager(
            camera_service=self,
            streaming_service=self,
            db=self.db,
            recordings_dir=self.recordings_dir,
            audio_utils=audio_utils,
            min_free_storage_bytes=self.min_free_storage_bytes,
            motion_check_interval=self.motion_check_interval,
            max_clip_length=self.max_clip_length,
            max_velocity=self.max_velocity,
            max_bg_diff=self.max_bg_diff,
            motion_result_max_age_sec=self.motion_result_max_age_sec,
            archive_dir=self.archive_dir,
            mux_realtime=self.mux_realtime,
        )
        self.active_recordings = self.recording_manager.active_recordings
        self._load_existing_recordings()

    def _should_auto_enable_low_power(self) -> bool:
        try:
            total_memory_bytes = int(psutil.virtual_memory().total)
        except Exception:
            total_memory_bytes = 0
        return 0 < total_memory_bytes <= int(self.low_power_ram_threshold_bytes)

    def get_runtime_settings(self) -> Dict[str, Union[bool, int, float]]:
        try:
            total_memory_bytes = int(psutil.virtual_memory().total)
        except Exception:
            total_memory_bytes = 0

        # Get current preset info
        sys_settings = self.db.get_system_settings()
        active_preset = sys_settings.get('active_preset', 'default')
        ram_auto_switch = sys_settings.get('ram_auto_switch_enabled', True)
        ram_threshold = sys_settings.get('ram_threshold_bytes', 1073741824)

        return {
            'low_power_mode': bool(self.low_power_mode),
            'sensitivity': int(self.sensitivity),
            'jpeg_quality': int(self.jpeg_quality),
            'pipe_buffer_size': int(self.pipe_buffer_size),
            'max_vel': float(self.max_velocity),
            'bg_diff': int(self.max_bg_diff),
            'max_clip_length': int(self.max_clip_length),
            'motion_check_interval': int(self.motion_check_interval),
            'min_free_storage_bytes': int(self.min_free_storage_bytes),
            'rtsp_unified_demux_enabled': bool(self.rtsp_unified_demux_enabled),
            'live_stream_mode': str(getattr(self, 'live_stream_mode', 'mjpeg')),
            'ram_auto_low_power_enabled': bool(self.ram_auto_low_power_enabled),
            'low_power_ram_threshold_bytes': int(self.low_power_ram_threshold_bytes),
            'total_memory_bytes': total_memory_bytes,
            # Advanced Performance Settings
            'frame_rbf_len': int(self.frame_rbf_len),
            'audio_rbf_len': int(self.audio_rbf_len),
            'results_rbf_len': int(self.results_rbf_len),
            # Recording Settings
            'mux_realtime': bool(self.mux_realtime),
            # Preset Settings
            'active_preset': active_preset,
            'ram_auto_switch_enabled': ram_auto_switch,
            'ram_threshold_bytes': ram_threshold,
        }

    def update_runtime_settings(
        self,
        low_power_mode: Optional[bool] = None,
        sensitivity: Optional[int] = None,
        jpeg_quality: Optional[int] = None,
        pipe_buffer_size: Optional[int] = None,
        max_vel: Optional[float] = None,
        bg_diff: Optional[int] = None,
        max_clip_length: Optional[int] = None,
        motion_check_interval: Optional[int] = None,
        min_free_storage_bytes: Optional[int] = None,
        rtsp_unified_demux_enabled: Optional[bool] = None,
        # Advanced Performance Settings
        frame_rbf_len: Optional[int] = None,
        audio_rbf_len: Optional[int] = None,
        results_rbf_len: Optional[int] = None,
        # Recording Settings
        mux_realtime: Optional[bool] = None,
        # Preset-specific settings
        fps: Optional[int] = None,
        max_buffer_frames: Optional[int] = None,
        max_recording_duration_minutes: Optional[int] = None,
        quality: Optional[str] = None,
        recording_enabled: Optional[bool] = None,
        live_stream_mode: Optional[str] = None,
    ) -> Dict[str, Union[bool, int, float]]:
        low_power_changed = False

        if low_power_mode is not None:
            self.low_power_mode = bool(low_power_mode)
            low_power_changed = True

        if sensitivity is not None:
            safe_sensitivity = max(0, min(int(self.sensitivity_level), int(sensitivity)))
            self.sensitivity = safe_sensitivity
            try:
                for camera in self.get_cameras():
                    self.set_camera_sensitivity(camera.id, safe_sensitivity)
            except Exception:
                pass
        elif low_power_changed:
            self.sensitivity = 2 if self.low_power_mode else 4

        if jpeg_quality is not None:
            safe_quality = max(25, min(95, int(jpeg_quality)))
            self.jpeg_quality = safe_quality
        elif low_power_changed:
            self.jpeg_quality = 55 if self.low_power_mode else 70

        if pipe_buffer_size is not None:
            self.pipe_buffer_size = max(65536, min(268435456, int(pipe_buffer_size)))
        elif low_power_changed:
            self.pipe_buffer_size = 10**6 if self.low_power_mode else 10**8

        if max_vel is not None:
            self.max_velocity = max(0.0, min(5.0, float(max_vel)))

        if bg_diff is not None:
            self.max_bg_diff = max(1, min(5000, int(bg_diff)))

        if max_clip_length is not None:
            self.max_clip_length = max(5, min(600, int(max_clip_length)))

        if motion_check_interval is not None:
            self.motion_check_interval = max(1, min(120, int(motion_check_interval)))

        if min_free_storage_bytes is not None:
            self.min_free_storage_bytes = max(0, int(min_free_storage_bytes))
            try:
                self.recording_manager.min_free_storage_bytes = self.min_free_storage_bytes
            except Exception:
                pass

        if rtsp_unified_demux_enabled is not None:
            self.rtsp_unified_demux_enabled = bool(rtsp_unified_demux_enabled)

        # Handle advanced performance settings
        if frame_rbf_len is not None:
            self.frame_rbf_len = max(1, min(100, int(frame_rbf_len)))

        if audio_rbf_len is not None:
            self.audio_rbf_len = max(1, min(100, int(audio_rbf_len)))

        if results_rbf_len is not None:
            self.results_rbf_len = max(1, min(100, int(results_rbf_len)))

        # Handle recording settings
        if mux_realtime is not None:
            self.mux_realtime = bool(mux_realtime)

        # Handle preset-specific settings
        if fps is not None:
            # Update FPS for all active camera streams
            fps = max(1, min(60, int(fps)))
            for cap in self._camera_streams.values():
                try:
                    cap.fps = fps
                except Exception:
                    continue

        if max_buffer_frames is not None:
            # Map max_buffer_frames to frame ring buffer length
            buffer_frames = max(1, min(100, int(max_buffer_frames)))
            self.frame_rbf_len = buffer_frames

        if max_recording_duration_minutes is not None:
            # Convert minutes to seconds and update max_clip_length
            duration_seconds = max(5, min(3600, int(max_recording_duration_minutes * 60)))
            self.max_clip_length = duration_seconds

        if quality is not None:
            # Map quality string to JPEG quality value
            quality_map = {
                'low': 30,
                'medium': 55, 
                'high': 70,
                'ultra': 90
            }
            if quality.lower() in quality_map:
                self.jpeg_quality = quality_map[quality.lower()]

        if recording_enabled is not None:
            # Store recording enabled setting for future use
            self.recording_enabled = bool(recording_enabled)

        if live_stream_mode is not None:
            # Validate and store live stream mode setting
            valid_modes = ['mjpeg', 'hls']
            if str(live_stream_mode).lower() in valid_modes:
                self.live_stream_mode = str(live_stream_mode).lower()
            else:
                logger.warning(f"Invalid live_stream_mode '{live_stream_mode}', keeping current mode")

        try:
            self.default_sensitivity = int(self.sensitivity)
        except Exception:
            pass

        for cap in self._camera_streams.values():
            try:
                cap.low_power_mode = bool(self.low_power_mode)
                cap.pipe_buffer_size = int(self.pipe_buffer_size)
                if hasattr(cap, '_rtsp_unified_demux_enabled'):
                    cap._rtsp_unified_demux_enabled = bool(self.rtsp_unified_demux_enabled)
            except Exception:
                continue

        try:
            self.recording_manager.max_velocity = float(self.max_velocity)
            self.recording_manager.max_bg_diff = int(self.max_bg_diff)
            self.recording_manager.max_clip_length = int(self.max_clip_length)
            self.recording_manager.motion_check_interval = int(self.motion_check_interval)
            self.recording_manager.min_free_storage_bytes = int(self.min_free_storage_bytes)
            self.recording_manager.mux_realtime = bool(self.mux_realtime)
        except Exception:
            pass

        # Persist all runtime settings to appropriate preset
        try:
            current_settings = self.db.get_system_settings()
            current_preset = current_settings.get('active_preset', 'default')
            
            settings_to_save = {
                'low_power_mode': bool(self.low_power_mode),
                'sensitivity': int(self.sensitivity),
                'jpeg_quality': int(self.jpeg_quality),
                'pipe_buffer_size': int(self.pipe_buffer_size),
                'max_vel': float(self.max_velocity),
                'bg_diff': int(self.max_bg_diff),
                'max_clip_length': int(self.max_clip_length),
                'motion_check_interval': int(self.motion_check_interval),
                'min_free_storage_bytes': int(self.min_free_storage_bytes),
                'rtsp_unified_demux_enabled': bool(self.rtsp_unified_demux_enabled),
                'live_stream_mode': str(self.live_stream_mode),
                # Advanced Performance Settings
                'frame_rbf_len': int(self.frame_rbf_len),
                'audio_rbf_len': int(self.audio_rbf_len),
                'results_rbf_len': int(self.results_rbf_len),
                # Recording Settings
                'mux_realtime': bool(self.mux_realtime),
            }
            
            # Add preset-specific settings if they were set
            if hasattr(self, 'recording_enabled'):
                settings_to_save['recording_enabled'] = bool(self.recording_enabled)
            
            # If individual settings are being modified (not from preset switch),
            # automatically switch to custom preset to store modifications
            if current_preset != 'custom':
                # Check if any setting differs from current preset template
                presets = self.db.get_presets()
                current_preset_values = presets.get(current_preset, {})
                
                settings_differ = False
                for key, value in settings_to_save.items():
                    if key in current_preset_values and current_preset_values[key] != value:
                        settings_differ = True
                        break
                
                # If settings differ, switch to custom preset
                if settings_differ:
                    settings_to_save['active_preset'] = 'custom'
                    
            self.db.save_system_settings(settings_to_save)
        except Exception:
            pass

        return self.get_runtime_settings()

    def __del__(self):
        # Clean up any active recordings on shutdown
        try:
            if hasattr(self, 'active_recordings'):
                for camera_id in list(self.active_recordings.keys()):
                    self.stop_recording(camera_id)
            if hasattr(self, '_camera_streams'):
                for camera_id in list(self._camera_streams.keys()):
                    self.stop_video(camera_id)
            if hasattr(self, '_audio_streams'):
                for camera_id in list(self._audio_streams.keys()):
                    self.stop_audio(camera_id)
        except Exception:
            pass  # Ignore cleanup errors during shutdown
        
    def _load_existing_recordings(self):
        self.recording_manager.load_existing_recordings()

    def get_cameras(self) -> List[Camera]:
        """Get all cameras"""
        db_cameras = self.db.get_all_cameras()
        cameras = []
        
        for db_camera in db_cameras:
            camera = Camera(
                id=db_camera['id'],
                name=db_camera['name'],
                source=db_camera['source'],
                camera_type=CameraType(db_camera.get('camera_type', 'webcam')),
                fps=db_camera['fps'],
                resolution=db_camera['resolution'],
                status=CameraStatus(db_camera['status']),
                created_at=datetime.fromisoformat(db_camera['created_at']),
                processing_active=db_camera['processing_active'],
                processing_type=db_camera.get('processing_type'),
                audio_enabled=bool(db_camera.get('audio_enabled', False)),
                audio_source=db_camera.get('audio_source'),
                audio_input_format=db_camera.get('audio_input_format'),
                audio_sample_rate=int(db_camera.get('audio_sample_rate', 16000) or 16000),
                audio_chunk_size=int(db_camera.get('audio_chunk_size', 512) or 512),
            )
            cameras.append(camera)
        
        return cameras

    def add_camera(self, camera_data: CameraCreate) -> Camera:
        """Add a new camera"""
        camera_id = f"{camera_data.name}_{camera_data.camera_type.value}_{int(time.time())}"
        
        camera_dict = {
            'id': camera_id,
            'name': camera_data.name,
            'source': camera_data.source,
            'camera_type': camera_data.camera_type.value,
            'fps': camera_data.fps,
            'resolution': camera_data.resolution,
            'status': CameraStatus.OFFLINE.value,
            'audio_enabled': bool(camera_data.audio_enabled),
            'audio_source': camera_data.audio_source,
            'audio_input_format': camera_data.audio_input_format,
            'audio_sample_rate': int(camera_data.audio_sample_rate or 16000),
            'audio_chunk_size': int(camera_data.audio_chunk_size or 512),
        }
        
        # Store in database
        db_camera = self.db.create_camera(camera_dict)
        
        # Convert to Camera model
        camera = Camera(
            id=db_camera['id'],
            name=db_camera['name'],
            source=db_camera['source'],
            camera_type=camera_data.camera_type,
            fps=db_camera['fps'],
            resolution=db_camera['resolution'],
            status=CameraStatus(db_camera['status']),
            created_at=datetime.fromisoformat(db_camera['created_at']),
            audio_enabled=bool(db_camera.get('audio_enabled', False)),
            audio_source=db_camera.get('audio_source'),
            audio_input_format=db_camera.get('audio_input_format'),
            audio_sample_rate=int(db_camera.get('audio_sample_rate', 16000) or 16000),
            audio_chunk_size=int(db_camera.get('audio_chunk_size', 512) or 512),
        )
        
        logger.info(f"Added camera: {camera.name} ({camera_id})")
        return camera

    def get_camera(self, camera_id: str) -> Optional[Camera]:
        """Get a camera by ID"""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            return None
            
        return Camera(
            id=db_camera['id'],
            name=db_camera['name'],
            source=db_camera['source'],
            camera_type=CameraType(db_camera.get('camera_type', 'webcam')),
            fps=db_camera['fps'],
            resolution=db_camera['resolution'],
            status=CameraStatus(db_camera['status']),
            created_at=datetime.fromisoformat(db_camera['created_at']),
            processing_active=db_camera['processing_active'],
            processing_type=db_camera.get('processing_type'),
            audio_enabled=bool(db_camera.get('audio_enabled', False)),
            audio_source=db_camera.get('audio_source'),
            audio_input_format=db_camera.get('audio_input_format'),
            audio_sample_rate=int(db_camera.get('audio_sample_rate', 16000) or 16000),
            audio_chunk_size=int(db_camera.get('audio_chunk_size', 512) or 512),
        )

    def update_camera(self, camera_id: str, camera_update: CameraUpdate) -> Camera:
        """Update camera settings"""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            raise ValueError(f"Camera not found: {camera_id}")
        
        # Update fields if provided
        update_data = camera_update.dict(exclude_unset=True)
        update_dict = {}
        
        if 'name' in update_data:
            update_dict['name'] = update_data['name']
        if 'source' in update_data:
            update_dict['source'] = update_data['source']
        if 'camera_type' in update_data and update_data['camera_type'] is not None:
            update_dict['camera_type'] = update_data['camera_type'].value if hasattr(update_data['camera_type'], 'value') else str(update_data['camera_type'])
        if 'fps' in update_data:
            update_dict['fps'] = update_data['fps']
        if 'resolution' in update_data:
            update_dict['resolution'] = update_data['resolution']
        if 'audio_enabled' in update_data:
            update_dict['audio_enabled'] = bool(update_data['audio_enabled'])
        if 'audio_source' in update_data:
            update_dict['audio_source'] = update_data['audio_source']
        if 'audio_input_format' in update_data:
            update_dict['audio_input_format'] = update_data['audio_input_format']
        if 'audio_sample_rate' in update_data and update_data['audio_sample_rate'] is not None:
            update_dict['audio_sample_rate'] = int(update_data['audio_sample_rate'])
        if 'audio_chunk_size' in update_data and update_data['audio_chunk_size'] is not None:
            update_dict['audio_chunk_size'] = int(update_data['audio_chunk_size'])
        
        ## Validate source if updated
        #if 'source' in update_dict:
        #    if not self._validate_camera_source(update_dict['source'], camera_update.camera_type or CameraType.WEBCAM):
        #        raise ValueError(f"Invalid camera source: {update_dict['source']}")
        
        # Update in database
        if update_dict:
            updated_camera = self.db.update_camera(camera_id, update_dict)
            if updated_camera:
                camera = Camera(
                    id=updated_camera['id'],
                    name=updated_camera['name'],
                    source=updated_camera['source'],
                    camera_type=CameraType(updated_camera.get('camera_type', 'webcam')),
                    fps=updated_camera['fps'],
                    resolution=updated_camera['resolution'],
                    status=CameraStatus(updated_camera['status']),
                    created_at=datetime.fromisoformat(updated_camera['created_at']),
                    processing_active=updated_camera['processing_active'],
                    processing_type=updated_camera.get('processing_type'),
                    processing_params=updated_camera.get('processing_params', {}),
                    audio_enabled=bool(updated_camera.get('audio_enabled', False)),
                    audio_source=updated_camera.get('audio_source'),
                    audio_input_format=updated_camera.get('audio_input_format'),
                    audio_sample_rate=int(updated_camera.get('audio_sample_rate', 16000) or 16000),
                    audio_chunk_size=int(updated_camera.get('audio_chunk_size', 512) or 512),
                )
                
                logger.info(f"Updated camera: {camera.name} ({camera_id})")
                return camera
        
        raise ValueError("No valid updates provided")

    def remove_camera(self, camera_id: str):
        """Remove a camera"""
        db_camera = self.db.get_camera(camera_id)
        
        # Stop any active recording
        if camera_id in self.active_recordings:
            self.stop_recording(camera_id)
        
        camera_name = db_camera['name']
        self.db.delete_camera(camera_id)
        logger.info(f"Removed camera: {camera_name} ({camera_id})")
    
    def video_capture(self, camera_id: str):
        db_camera = self.db.get_camera(camera_id)
        source = db_camera['source']
        
        try:
            source = int(source)
        except:
            source = str(source)
        
        resolution = [int(res) for res in db_camera['resolution'].split('x')]
        fps = db_camera['fps']
        audio_enabled = bool(db_camera.get('audio_enabled', False))
        audio_sample_rate = int(db_camera.get('audio_sample_rate') or 16000)
        audio_chunk_size = int(db_camera.get('audio_chunk_size') or 512)
        audio_input_format = db_camera.get('audio_input_format')
        audio_source = db_camera.get('audio_source')
        
        cap = Capture(
            source,
            width=resolution[0],
            height=resolution[1],
            fps=fps,
            low_power_mode=self.low_power_mode,
            audio_enabled=audio_enabled,
            audio_sample_rate=audio_sample_rate,
            audio_chunk_size=audio_chunk_size,
            audio_input_format=audio_input_format,
            audio_source=audio_source,
            rtsp_unified_demux_enabled=self.rtsp_unified_demux_enabled,
            pipe_buffer_size=self.pipe_buffer_size,
        )
        return cap
    
    def start_video(self, camera_id: str) -> bool:
        """Start video capture for a camera"""
        camera_started = False
        cap = self.video_capture(camera_id)
        
        if cap is None:
            logger.warning(f"{Colors.RED}Failed to start video for {camera_id}{Colors.RESET}")
            self.db.update_camera(camera_id, {'status': CameraStatus.OFFLINE.value})
            return False
            
        tracker = OpticalFlowTracker()
        ret, _ = cap.read_video()
            
        if ret:
            self.db.update_camera(camera_id, {'status': CameraStatus.ONLINE.value})
            logger.info(f"{Colors.GREEN}Video started for {camera_id}{Colors.RESET}")
            camera_started = True
            self._camera_streams[camera_id] = cap
            self._camera_trackers[camera_id] = tracker
            
            # Initialize ring buffers for SPMC data distribution
            self._ensure_ring_buffers(camera_id)
        else:
            cap.release_video()
            self.db.update_camera(camera_id, {'status': CameraStatus.OFFLINE.value})
            logger.warning(f"{Colors.RED}Failed to read frames for {camera_id}{Colors.RESET}")

        return camera_started
        
    def stop_video(self, camera_id: str):
        """Stop video capture for a camera"""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            logger.warning(f"{Colors.RED}Failed to stop video - camera not found: {camera_id}{Colors.RESET}")
            return

        try:
            self._hls_manager.stop_stream(camera_id)
        except Exception:
            pass

        self.active_streams.pop(camera_id, None)
        self.active_processing_streams.pop(camera_id, None)

        cap = self._camera_streams.pop(camera_id, None)
        if cap is not None:
            try:
                cap.release_video()
            except Exception:
                pass

        tracker = self._camera_trackers.pop(camera_id, None)
        if tracker is not None:
            try:
                del tracker
            except Exception:
                pass

        self.stream_locks.pop(camera_id, None)
        self._latest_frames.pop(camera_id, None)
        self._latest_hls_frames.pop(camera_id, None)
        self._latest_frame_seq.pop(camera_id, None)
        self._latest_viz.pop(camera_id, None)
        self._latest_res_video.pop(camera_id, None)
        self._latest_pts_payload.pop(camera_id, None)
        self._overlay_masks.pop(camera_id, None)

        self._fps_stats.pop(f"{camera_id}:primary", None)
        self._fps_stats.pop(f"{camera_id}:processing", None)

        self.db.update_camera(camera_id, {'status': CameraStatus.OFFLINE.value})

        if hasattr(self, "active_streams"):
            self.active_streams.pop(camera_id, None)

        logger.info(f"{Colors.YELLOW}Video stopped for {camera_id}{Colors.RESET}")

    def restart_camera(self, camera_id: str) -> bool:
        """Restart camera by stopping recording, stopping camera, and starting again."""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            logger.error(f"{Colors.RED}❌ Camera not found:{Colors.RESET} {camera_id}")
            return False

        logger.info(f"{Colors.BLUE}🔄 Restarting camera:{Colors.RESET} {camera_id}")
        
        try:
            # Step 1: Stop recording if currently recording
            if camera_id in self.recording_manager.active_recordings:
                logger.info(f"{Colors.YELLOW}⏹️ Stopping recording for restart:{Colors.RESET} {camera_id}")
                self.stop_recording(camera_id)
            
            # Step 2: Stop camera stream (like stop button)
            self.stop_video(camera_id)
            logger.info(f"{Colors.YELLOW}⏹️ Stopped camera stream:{Colors.RESET} {camera_id}")
            
            # Step 3: Start camera stream again (like start button)  
            success = self.start_video(camera_id)
            if success:
                logger.info(f"{Colors.GREEN}▶️ Restarted camera successfully:{Colors.RESET} {camera_id}")
                return True
            else:
                logger.error(f"{Colors.RED}❌ Failed to restart camera:{Colors.RESET} {camera_id}")
                return False
                
        except Exception as error:
            logger.error(f"{Colors.RED}❌ Error restarting camera {camera_id}:{Colors.RESET} {error}")
            return False

    def audio_capture(self, camera_id: str):
        """Create an audio-only Capture for this camera (mirrors video_capture)."""
        db_camera = self.db.get_camera(camera_id)
        source = db_camera['source']

        try:
            source = int(source)
        except Exception:
            source = str(source)

        audio_sample_rate = int(db_camera.get('audio_sample_rate') or 16000)
        audio_chunk_size = int(db_camera.get('audio_chunk_size') or 512)
        audio_input_format = db_camera.get('audio_input_format')
        audio_source = db_camera.get('audio_source')

        
        cap = Capture(
            source,
            audio_sample_rate=audio_sample_rate,
            audio_chunk_size=audio_chunk_size,
            audio_input_format=audio_input_format,
            audio_source=audio_source,
            audio_only=True,
        )
        return cap
        
    def start_audio(self, camera_id: str) -> bool:
        """Start audio capture and verify it works (mirrors start_video)."""
        
        cap = self.audio_capture(camera_id)  # Initialize audio_cap for the camera

        if cap is None:
            return False

        # Retry probe to cover FFmpeg/PulseAudio startup latency (can be 3-5 s).
        # Each read_audio() call waits up to 2 s internally via select.select,
        # so 6 attempts = up to 12 s total probe window.
        _MAX_PROBE_ATTEMPTS = 6
        ret = False
        for attempt in range(_MAX_PROBE_ATTEMPTS):
            if not cap.is_audio_stream_opened():
                stderr_msg = ''
                if cap.audio_cap and cap.audio_cap.stderr:
                    try:
                        stderr_msg = cap.audio_cap.stderr.read().decode(errors='replace').strip()
                    except Exception:
                        pass
                exit_code = cap.audio_cap.poll() if cap.audio_cap else None
                logger.warning(
                    f"{Colors.RED}Failed to start audio for {camera_id} - attempt {attempt + 1}/{_MAX_PROBE_ATTEMPTS}"
                    + (f" (exit {exit_code})" if exit_code is not None else "")
                    + (f": {stderr_msg}" if stderr_msg else "")
                    + f"{Colors.RESET}"
                )
            else:   
                _, _ = cap.read_audio()
                self._audio_streams[camera_id] = cap
                ret = True
                db_camera = self.db.get_camera(camera_id)
                logger.info(f"{Colors.GREEN}Audio started for {camera_id}{Colors.RESET}")
                break

        if not ret:
            logger.warning(f"{Colors.RED}Failed to read audio data for {camera_id}{Colors.RESET}")
            cap.release_audio()
            return ret

        # Initialize audio analyzer if audio is enabled
        db_camera = self.db.get_camera(camera_id)
        if db_camera and bool(db_camera.get('audio_enabled', False)):
            sample_rate = int(db_camera.get('audio_sample_rate'))
            channels = int(db_camera.get('audio_channels', 1))
            audio_analyzer = FrequencyIntensityAnalyzer(sample_rate=sample_rate, channels=channels)
            self._audio_chunk_analyzers[camera_id] = audio_analyzer
            
        return ret
    
    def stop_audio(self, camera_id: str):
        """Stop audio capture for a camera"""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            logger.warning(f"{Colors.RED}Failed to stop audio - camera not found: {camera_id}{Colors.RESET}")
            return

        # Clean up audio-related resources
        audio_analyzer = self._audio_chunk_analyzers.pop(camera_id, None)
        if audio_analyzer is not None:
            try:
                del audio_analyzer
            except Exception:
                pass

        # Release audio capture
        audio_cap = self._audio_streams.pop(camera_id, None)
        if audio_cap is not None:
            try:
                audio_cap.release_audio()
            except Exception:
                pass

        logger.info(f"{Colors.YELLOW}Audio stopped for {camera_id}{Colors.RESET}")

        
    
    def get_recording_storage_info(self, enforce_policy: bool = False) -> Dict:
        return self.recording_manager.get_recording_storage_info(enforce_policy=enforce_policy)

    def start_recording(self, camera_id: str):
        return self.recording_manager.start_recording(camera_id)

    def stop_recording(self, camera_id: str):
        self.recording_manager.stop_recording(camera_id)

    def get_recordings(self, camera_id: Optional[str] = None) -> List[Recording]:
        return self.recording_manager.get_recordings(camera_id=camera_id)

    def get_recording_path(self, recording_id: str) -> str:
        return self.recording_manager.get_recording_path(recording_id)

    def get_browser_playable_recording_path(self, recording_id: str) -> str:
        return self.recording_manager.get_browser_playable_recording_path(recording_id)

    def delete_recording(self, recording_id: str):
        self.recording_manager.delete_recording(recording_id)

    def get_camera_status(self) -> Dict[str, dict]:
        """Get status of all cameras"""
        cameras = self.get_cameras()
        status = {}
        
        for camera in cameras:
            recording_info = self.active_recordings.get(camera.id)
            status[camera.id] = {
                'name': camera.name,
                'status': camera.status.value,
                'is_recording': camera.id in self.active_recordings,
                'recording_start': recording_info['start_time'].isoformat() if recording_info else None,
                'processing_active': camera.processing_active,
                'processing_type': camera.processing_type
            }
        
        return status

    def get_disk_usage(self) -> float:
        """Return disk usage percent for recordings directory."""
        return psutil.disk_usage(self.recordings_dir).percent

    def get_uptime(self) -> str:
        """Return human-readable uptime string (HH:MM:SS)."""
        uptime = datetime.now() - self.start_time
        return str(uptime).split('.')[0]
    
    # Properties to maintain compatibility
    @property
    def cameras(self) -> Dict[str, Camera]:
        """Get cameras as dict for backward compatibility"""
        cameras = self.get_cameras()
        return {camera.id: camera for camera in cameras}
    
    @property
    def recordings(self) -> Dict[str, Recording]:
        """Get recordings as dict for backward compatibility"""
        recordings = self.get_recordings()
        return {recording.id: recording for recording in recordings}
    