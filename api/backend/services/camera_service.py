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
from services.audio_utils import AudioRecordingUtils
from services.recording_manager import RecordingManager

import sys
sys.path.append('../../src')
from improc.optical_flow import OpticalFlowTracker

logger = logging.getLogger(__name__)

class Capture:
    def __init__(self, source: Union[str, int], width:int =640, height:int =480, fps:int =30, low_power_mode: bool = False):
        self.source = source
        self.cam_type = None
        self.width = width
        self.height = height
        self.fps = fps
        self.low_power_mode = low_power_mode
        self.cap = None
        self._consecutive_read_failures = 0
        self._max_read_failures_before_reconnect = 3
        self._reconnect_cooldown_sec = 2.0
        self._last_reconnect_at = 0.0
        self.audio_cap = None
        self.audio_sample_rate = int(os.getenv('AUDIO_SAMPLE_RATE', '16000'))
        self.audio_channels = int(os.getenv('AUDIO_CHANNELS', '1'))
        self.audio_chunk_seconds = float(os.getenv('AUDIO_CHUNK_SECONDS', '0.1'))
        self._audio_chunk_bytes = max(
            320,
            int(self.audio_sample_rate * self.audio_channels * 2 * self.audio_chunk_seconds),
        )
        
        try:
            source = int(source)
        except:
            source = str(source)

        if isinstance(source, str) and source.startswith(('rtsp://', 'rtmp://')):
            self.cam_type = 'rtsp'
            self.open_video_stream_rtsp()
        elif isinstance(source, str) and source.startswith(('http://', 'https://')):
            self.cam_type = 'http'
            self.open_video_stream_rtsp()  # For simplicity, treat HTTP sources as RTSP for now
        elif type(source) == int or (isinstance(source, str) and source.split('.')[-1] in ['mp4', 'avi', 'mkv', 'mov']):
            self.cam_type = 'webcam'
            self.open_video_stream_webcam()
        else:
            raise ValueError(f"Unsupported camera source: {source}")

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

        try:
            pipe_buffer_size = 10**6 if self.low_power_mode else 10**8
            self.cap = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=pipe_buffer_size,
            )
        except Exception as error:
            logger.error(f"Failed to open webcam video stream ({webcam_input_format}:{webcam_source}): {error}")
            self.cap = None
        return self

    def open_video_stream_rtsp(self):
        if self.cap:
            self.release_video_stream_rtsp()

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
        try:
            pipe_buffer_size = 10**6 if self.low_power_mode else 10**8
            self.cap = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=pipe_buffer_size)
        except Exception as e:
            logger.error(f"Failed to open RTSP stream: {e}")
            self.cap = None
        return self

    def open_audio_stream_webcam(self):
        if self.audio_cap:
            self.release_audio_stream_webcam()

        input_format = os.getenv('AUDIO_INPUT_FORMAT', 'pulse').strip().lower()
        input_source = os.getenv('AUDIO_SOURCE', 'default').strip()

        cmd = [
            'ffmpeg',
            '-hide_banner',             # reduce non-critical startup logs
            '-loglevel', 'error',       # surface only ffmpeg errors
            '-f', input_format,         # input audio backend (pulse/alsa/etc.)
            '-i', input_source,         # input device/source name
            '-ac', str(self.audio_channels),      # output channel count
            '-ar', str(self.audio_sample_rate),   # output sampling rate
            '-f', 's16le',              # raw PCM 16-bit little-endian
            'pipe:1',
        ]
        try:
            self.audio_cap = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize= 10**4 #max(10**6, self._audio_chunk_bytes * 16),
            )
        except Exception as e:
            logger.error(f"Failed to open webcam audio stream ({input_format}:{input_source}): {e}")
            self.audio_cap = None
        return self

    def open_audio_stream_rtsp(self):
        if self.audio_cap:
            self.release_audio_stream_rtsp()

        cmd = [
            'ffmpeg',
            '-hide_banner',             # reduce non-critical startup logs
            '-loglevel', 'error',       # surface only ffmpeg errors
            '-rtsp_transport', 'tcp',   # prefer TCP for RTSP reliability
            '-i', self.source,
            '-vn',                      # disable video in audio pipeline
            '-ac', str(self.audio_channels),      # output channel count
            '-ar', str(self.audio_sample_rate),   # output sampling rate
            '-f', 's16le',              # raw PCM 16-bit little-endian
            'pipe:1',
        ]
        try:
            self.audio_cap = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=max(10**6, self._audio_chunk_bytes * 16),
            )
        except Exception as e:
            logger.error(f"Failed to open RTSP audio stream: {e}")
            self.audio_cap = None
        return self
    
    def is_video_stream_opened(self):
        return bool(self.cap and self.cap.poll() is None and self.cap.stdout is not None)

    def is_audio_stream_opened(self):
        return self.audio_cap and self.audio_cap.poll() is None and self.audio_cap.stdout is not None

    def reconnect_video_stream_rtsp(self) -> bool:
        now = time.time()
        if now - self._last_reconnect_at < self._reconnect_cooldown_sec:
            return self.is_video_stream_opened()

        self._last_reconnect_at = now
        logger.warning(f"Reconnecting stream source: {self.source}")
        self.release_video_stream_rtsp()
        self.open_video_stream_rtsp()
        self._consecutive_read_failures = 0
        return self.is_video_stream_opened()
    
    def read_video_stream_webcam(self):
        if not self.cap or not self.is_video_stream_opened():
            self.open_video_stream_webcam()
            if not self.is_video_stream_opened():
                return False, None

        frame_size = self.width * self.height * 3
        raw = b""
        try:
            raw = self.cap.stdout.read(frame_size)
        except Exception as error:
            logger.warning(f"Webcam video read failed for source {self.source}: {error}")

        if len(raw) == frame_size:
            frame = np.frombuffer(raw, np.uint8).reshape((self.height, self.width, 3))
            return True, frame
        return False, None
    
    def read_video_stream_rtsp(self):
        if not self.cap:
            return False, None

        if not self.is_video_stream_opened():
            self.reconnect_video_stream_rtsp()
            if not self.is_video_stream_opened():
                return False, None

        frame_size = self.width * self.height * 3
        raw = b""
        try:
            raw = self.cap.stdout.read(frame_size)
        except Exception as e:
            logger.warning(f"RTSP read failed for source {self.source}: {e}")

        if len(raw) == frame_size:
            self._consecutive_read_failures = 0
            frame = np.frombuffer(raw, np.uint8).reshape((self.height, self.width, 3))
            return True, frame

        self._consecutive_read_failures += 1
        if self._consecutive_read_failures >= self._max_read_failures_before_reconnect:
            logger.warning(
                f"Short/empty RTSP frame read ({len(raw)}/{frame_size}) from {self.source}; attempting reconnect"
            )
            if self.reconnect_video_stream_rtsp() and self.cap and self.cap.stdout:
                try:
                    raw = self.cap.stdout.read(frame_size)
                except Exception:
                    raw = b""

                if len(raw) == frame_size:
                    self._consecutive_read_failures = 0
                    frame = np.frombuffer(raw, np.uint8).reshape((self.height, self.width, 3))
                    return True, frame

        return False, None

    def read_audio_stream_webcam(self):
        if not self.audio_cap or not self.is_audio_stream_opened():
            self.open_audio_stream_webcam()
            if not self.is_audio_stream_opened():
                return False, None

        try:
            raw = self.audio_cap.stdout.read(self._audio_chunk_bytes)
        except Exception as e:
            logger.warning(f"Webcam audio read failed: {e}")
            return False, None

        if raw and len(raw) > 0:
            return True, raw
        return False, None

    def read_audio_stream_rtsp(self):
        if not self.audio_cap or not self.is_audio_stream_opened():
            self.open_audio_stream_rtsp()
            if not self.is_audio_stream_opened():
                return False, None

        try:
            raw = self.audio_cap.stdout.read(self._audio_chunk_bytes)
        except Exception as e:
            logger.warning(f"RTSP audio read failed for source {self.source}: {e}")
            return False, None

        if raw and len(raw) > 0:
            return True, raw
        return False, None

    def release_video_stream_webcam(self):
        self.release_video_stream_rtsp()

    def release_video_stream_rtsp(self):
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

    def release_audio_stream_webcam(self):
        if self.audio_cap:
            try:
                if self.audio_cap.stdout:
                    self.audio_cap.stdout.close()
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
        self.release_audio_stream_webcam()
            
    def open(self):
        if self.cam_type in ['rtsp', 'http']:
            return self.open_video_stream_rtsp()
        elif self.cam_type == 'webcam':
            return self.open_video_stream_webcam()

    def open_audio(self):
        if self.cam_type in ['rtsp', 'http']:
            return self.open_audio_stream_rtsp()
        elif self.cam_type == 'webcam':
            return self.open_audio_stream_webcam()
    
    def read(self):
        if self.cam_type in ['rtsp', 'http']:
            return self.read_video_stream_rtsp()
        elif self.cam_type == 'webcam':
            return self.read_video_stream_webcam()
        else:
            raise ValueError(f"Unsupported camera type: {self.cam_type}")

    def read_audio(self):
        if self.cam_type in ['rtsp', 'http']:
            return self.read_audio_stream_rtsp()
        elif self.cam_type == 'webcam':
            return self.read_audio_stream_webcam()
        else:
            raise ValueError(f"Unsupported camera type: {self.cam_type}")
        
    def release(self):
        if self.cam_type in ['rtsp', 'http']:
            self.release_video_stream_rtsp()
            self.release_audio_stream_rtsp()
        elif self.cam_type == 'webcam':
            self.release_video_stream_webcam()
            self.release_audio_stream_webcam()
        else:
            raise ValueError(f"Unsupported camera type: {self.cam_type}")
        

class CameraService(StreamingService):
    def __init__(self, configs: Optional[str] = 'default'):
        super().__init__()
        self.low_power_mode = True
        self.root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
        self.db = ConfigManager(configs_dir=os.path.join(self.root_dir, 'configs', configs))  # Use same YAML-backed DB as recording service
        self.recordings_dir = os.path.join(self.root_dir, "recordings")
        self.start_time = datetime.now()
        
        # Create recordings directory
        os.makedirs(self.recordings_dir, exist_ok=True)
        self.audio_utils = AudioRecordingUtils(self.recordings_dir)
        
        self._camera_streams = {}
        self._camera_trackers = {}
        self._active_audio_stream_processes: Dict[str, subprocess.Popen] = {}
        self._active_audio_stream_formats: Dict[str, str] = {}

        self.motion_check_interval = 10  # seconds
        self.max_clip_length = 60  # seconds
        self.max_velocity = 0.1#0.4  # velocity threshold for motion detection
        self.max_bg_diff = 50#200  # background difference threshold for motion detection
        self.motion_result_max_age_sec = 3.0  # ignore stale vel/bg_diff samples
        self.min_free_storage_bytes = 1 * 1024 * 1024 * 1024  # 1 GB
        self.processing_stride = 3 if self.low_power_mode else 1
        self.jpeg_quality = 55 if self.low_power_mode else 70

        self.recording_manager = RecordingManager(
            camera_service=self,
            db=self.db,
            recordings_dir=self.recordings_dir,
            audio_utils=self.audio_utils,
            min_free_storage_bytes=self.min_free_storage_bytes,
            motion_check_interval=self.motion_check_interval,
            max_clip_length=self.max_clip_length,
            max_velocity=self.max_velocity,
            max_bg_diff=self.max_bg_diff,
            motion_result_max_age_sec=self.motion_result_max_age_sec,
        )
        self.active_recordings = self.recording_manager.active_recordings
        self._load_existing_recordings()

    def __del__(self):
        # Clean up any active recordings on shutdown
        for camera_id in list(self.active_recordings.keys()):
            self.stop_recording(camera_id)
        for camera_id in list(self._camera_streams.keys()):
            self.stop_camera(camera_id)
        for camera_id in list(self._active_audio_stream_processes.keys()):
            self.stop_live_audio_stream(camera_id)
        
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
        
        cap = None
        try:
            cap = Capture(source, width=resolution[0], height=resolution[1], fps=fps, low_power_mode=self.low_power_mode)
        except Exception as e:
            logger.error(f"Failed to open video source: {source} with error: {e}")
            logger.error(f"Check if the source is correct and accessible: {source}")
        return cap
    
    def start_camera(self, camera_id: str) -> bool:
        """Start a camera"""
        camera_started = False
        cap = self.video_capture(camera_id)
        
        if cap is None:
            logger.warning(f"Camera: {self.db.get_camera(camera_id)['name']} Id: {camera_id} failed to open")
            self.db.update_camera(camera_id, {'status': CameraStatus.OFFLINE.value})
            return False
            
        tracker = OpticalFlowTracker()
        
        ret, _ = cap.read()
            
        if ret:
            self.db.update_camera(camera_id, {'status': CameraStatus.ONLINE.value})
            logger.info(f"Started camera: {self.db.get_camera(camera_id)['name']} ({camera_id})")
            camera_started = True
            self._camera_streams[camera_id] = cap
            self._camera_trackers[camera_id] = tracker
        else:
            logger.warning(f"Camera {camera_id} opened but failed to read frames")
            cap.release()
        
        if not camera_started:
            logger.warning(f"Camera: {self.db.get_camera(camera_id)['name']} Id: {camera_id} failed to open")
            self.db.update_camera(camera_id, {'status': CameraStatus.OFFLINE.value})

        return camera_started
        
    def stop_camera(self, camera_id: str):
        """Stop a camera"""
        db_camera = self.db.get_camera(camera_id)
        
        # Stop any active recording
        if camera_id in self.active_recordings:
            self.stop_recording(camera_id)

        if camera_id in self._camera_streams:
            cap = self._camera_streams.pop(camera_id)
            tracker = self._camera_trackers.pop(camera_id)
            cap.release()
            self.db.update_camera(camera_id, {'status': CameraStatus.OFFLINE.value})
            logger.info(f"Stopped camera: {db_camera['name']} ({camera_id})")
            
            del tracker
        else:
            logger.warning(f"No active camera object found for id: {camera_id}")
            return

    def close_camera_stream(self, camera_id: str):
        """Close an active camera stream and release related resources."""
        try:
            self.stop_hls_stream(camera_id)
        except Exception:
            pass

        if camera_id not in self._camera_streams:
            logger.info(f"No active stream to close for camera: {camera_id}")
            return

        self.stop_camera(camera_id)

        self.stream_locks.pop(camera_id, None)
        self._latest_frames.pop(camera_id, None)
        self._latest_frame_seq.pop(camera_id, None)
        self._latest_viz.pop(camera_id, None)
        self._latest_res.pop(camera_id, None)

        self._fps_stats.pop(f"{camera_id}:primary", None)
        self._fps_stats.pop(f"{camera_id}:processing", None)

        if hasattr(self, "active_streams"):
            self.active_streams.pop(camera_id, None)
    
    def get_recording_storage_info(self, enforce_policy: bool = False) -> Dict:
        return self.recording_manager.get_recording_storage_info(enforce_policy=enforce_policy)

    def audio_capture(self, camera_id: str, output_format: str = 'wav'):
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            raise ValueError(f"Camera not found: {camera_id}")

        command = self.audio_utils.build_live_audio_stream_command(db_camera, output_format=output_format)
        if not command:
            raise ValueError(f"Audio stream is not configured for camera: {camera_id}")

        process = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as error:
            logger.error(f"Failed to open audio source for camera {camera_id}: {error}")
            process = None
        return process

    def start_audio(self, camera_id: str, output_format: str = 'wav') -> bool:
        self.stop_live_audio_stream(camera_id)

        process = self.audio_capture(camera_id, output_format=output_format)
        if process is None:
            return False

        # Probe quickly for initial bytes to validate process readiness.
        ready = False
        try:
            if process.stdout:
                probe_deadline = time.time() + 0.6
                while time.time() < probe_deadline and process.poll() is None:
                    r, _, _ = select.select([process.stdout], [], [], 0.05)
                    if not r:
                        continue
                    probe_chunk = os.read(process.stdout.fileno(), 256)
                    if probe_chunk:
                        ready = True
                        break
        except Exception:
            ready = False

        if not ready and process.poll() is not None:
            try:
                stderr_text = (process.stderr.read() if process.stderr else b'').decode('utf-8', errors='ignore')
            except Exception:
                stderr_text = ''
            logger.warning(f"Audio process exited early for camera {camera_id}: {(stderr_text or '')[-400:]}")
            try:
                process.kill()
            except Exception:
                pass
            return False

        self._active_audio_stream_processes[camera_id] = process
        self._active_audio_stream_formats[camera_id] = str(output_format or 'wav').strip().lower()
        return True

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