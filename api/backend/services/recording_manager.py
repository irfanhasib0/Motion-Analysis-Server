import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import cv2
import psutil
import yaml

from models.camera import CameraStatus
from models.recording import Recording, RecordingStatus
from services.av_writer import AVWriterV2 as AVWriter  # Flexible writer - can switch between V2Flexible and V3
from services.audio_recording_utils import AudioRecordingUtils

logger = logging.getLogger(__name__)


class RecordingManager:
    def __init__(
        self,
        camera_service,
        db,
        recordings_dir: str,
        audio_utils: AudioRecordingUtils,
        min_free_storage_bytes: int,
        motion_check_interval: int,
        max_clip_length: int,
        max_velocity: float,
        max_bg_diff: int,
        motion_result_max_age_sec: float,
        archive_dir: str = '',
    ):
        self.camera_service = camera_service
        self.db = db
        self.recordings_dir = recordings_dir
        self.audio_utils = audio_utils
        self.archive_dir = archive_dir or os.path.join(os.path.dirname(recordings_dir), 'archive')
        self.min_free_storage_bytes = min_free_storage_bytes
        self.motion_check_interval = motion_check_interval
        self.max_clip_length = max_clip_length
        self.max_velocity = max_velocity
        self.max_bg_diff = max_bg_diff
        self.motion_result_max_age_sec = motion_result_max_age_sec
        self.active_recordings: Dict[str, dict] = {}

    def load_existing_recordings(self):
        if not os.path.exists(self.recordings_dir):
            return

        db_recordings = {rec['file_path']: rec for rec in self.db.get_all_recordings()}

        for filename in os.listdir(self.recordings_dir):
            if not filename.endswith('.mp4'):
                continue

            file_path = os.path.join(self.recordings_dir, filename)
            file_stat = os.stat(file_path)

            if file_path in db_recordings:
                continue

            base_name = filename[:-4]
            parts = base_name.split('_')
            if len(parts) < 2:
                continue

            camera_id = '_'.join(parts[:-1])
            timestamp_str = parts[-1]

            try:
                created_at = datetime.fromtimestamp(int(timestamp_str))
                recording_id = f"{camera_id}_{timestamp_str}"
                recording_data = {
                    'id': recording_id,
                    'camera_id': camera_id,
                    'file_path': file_path,
                    'start_time': created_at.isoformat(),
                    'duration': 0,
                    'file_size': file_stat.st_size,
                    'status': 'completed',
                }
                self.db.create_recording(recording_data)
                logger.info(f"Added existing recording to database: {filename}")
            except (ValueError, IndexError) as error:
                logger.warning(f"Could not parse recording filename {filename}: {error}")

    def init_recording(self, camera_id: str, db_camera: dict, cap) -> tuple[str, str, AVWriter]:
        self._ensure_min_free_storage()

        timestamp = int(time.time())
        filename = f"{camera_id}_{timestamp}.mp4"
        recording_id = f"{camera_id}_{timestamp}"
        file_path = os.path.join(self.recordings_dir, str(camera_id), filename)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        recording_data = {
            'id': recording_id,
            'camera_id': camera_id,
            'file_path': file_path,
            'start_time': datetime.now().isoformat(),
            'status': 'recording',
        }
        self.db.create_recording(recording_data)

        fps = db_camera['fps']
        width = cap.width
        height = cap.height

        # Create flexible AV writer with separate files mode (mux_realtime=False)
        writer = AVWriter(
            path=file_path,
            fps=fps,
            width=width,
            height=height,
            camera_service=self.camera_service,
            camera_id=camera_id,
            camera_config=db_camera,
            mux_realtime=False  # Start with separate files for testing
        )
        
        return recording_id, file_path, writer

    def _parse_recording_time(self, db_recording: dict) -> datetime:
        for key in ('start_time', 'created_at', 'end_time'):
            value = db_recording.get(key)
            if not value:
                continue
            try:
                value = value.replace('Z', '+00:00') if isinstance(value, str) else value
                return datetime.fromisoformat(value)
            except Exception:
                continue
        return datetime.min

    def _get_oldest_deletable_recording_id(self) -> Optional[str]:
        active_recording_ids = {
            info.get('recording_id')
            for info in self.active_recordings.values()
            if info.get('recording_id')
        }

        candidates = []
        for recording in self.db.get_all_recordings():
            recording_id = recording.get('id')
            if not recording_id:
                continue
            if recording.get('status') == 'recording':
                continue
            if recording_id in active_recording_ids:
                continue
            candidates.append(recording)

        if not candidates:
            return None

        oldest = min(candidates, key=self._parse_recording_time)
        return oldest.get('id')

    def _ensure_min_free_storage(self) -> int:
        deleted_count = 0
        while True:
            usage = psutil.disk_usage(self.recordings_dir)
            if usage.free >= self.min_free_storage_bytes:
                break

            oldest_recording_id = self._get_oldest_deletable_recording_id()
            if not oldest_recording_id:
                logger.warning(
                    "Low storage detected but no deletable recording found. "
                    f"Free bytes: {usage.free}"
                )
                break

            try:
                self.delete_recording(oldest_recording_id)
                deleted_count += 1
                logger.warning(
                    f"Deleted oldest recording {oldest_recording_id} due to low storage "
                    f"(free={usage.free} bytes)"
                )
            except Exception as error:
                logger.error(f"Failed to delete oldest recording {oldest_recording_id}: {error}")
                break

        return deleted_count

    def get_recording_storage_info(self, enforce_policy: bool = False) -> Dict:
        deleted_count = self._ensure_min_free_storage() if enforce_policy else 0
        disk = psutil.disk_usage(self.recordings_dir)
        return {
            'total_bytes': int(disk.total),
            'used_bytes': int(disk.used),
            'free_bytes': int(disk.free),
            'total_gb': round(disk.total / (1024 ** 3), 2),
            'used_gb': round(disk.used / (1024 ** 3), 2),
            'free_gb': round(disk.free / (1024 ** 3), 2),
            'percent_used': round(float(disk.percent), 2),
            'min_free_bytes': int(self.min_free_storage_bytes),
            'deleted_oldest_count': int(deleted_count),
        }

    def send_notification_to_app(self):
        pass

    def process_recorded_clip(
        self,
        recording_id: str,
        file_path: str,
        writer: AVWriter,
        clip_motion_detected: bool,
        clip_start_time: float,
        curr_time: float,
        vel: float = 0.0,
        bg_diff: int = 0,
        loudness: float = 0.0,
    ):
        # Check if audio was being recorded before releasing (compatible interface)
        had_audio = getattr(writer, 'audio_recording', False) and getattr(writer, 'audio_file_path', None) is not None
        
        # Release writer - returns immediately in separate files mode
        final_file_path = writer.release()
        
        if not clip_motion_detected:
            if os.path.exists(final_file_path):
                os.system(f'rm -f {final_file_path}')
                logger.info(f"Deleted no-motion recording: {final_file_path}")
                
            # Also clean up audio file if it exists
            audio_file_path = getattr(writer, 'audio_file_path', None)
            if audio_file_path and os.path.exists(audio_file_path):
                try:
                    os.remove(audio_file_path)
                except Exception as e:
                    logger.warning(f"Could not clean up audio file: {e}")
                    
            self.db.delete_recording(recording_id)
            return

        clip_duration = max(0, int(curr_time - clip_start_time))
        
        # Small delay to ensure file is fully written before getting size
        if os.path.exists(final_file_path):
            time.sleep(0.1)  # 100ms delay
            file_size = os.path.getsize(final_file_path)
        else:
            file_size = 0
        
        # Recording clip processed (reduced logging)
        
        self.db.update_recording(
            recording_id,
            {
                'status': 'completed',
                'end_time': datetime.now().isoformat(),
                'duration': clip_duration,
                'file_size': file_size,
                'file_path': final_file_path,
                'metadata': {
                    'motion_detected': True,
                    'vel': float(vel),
                    'diff': int(bg_diff),
                    'loudness': float(loudness),
                    'audio_recorded': had_audio,
                },
            },
        )
        logger.info(f"Saving recording with motion: {final_file_path}")
        threading.Thread(
            target=self._warm_browser_playback_cache,
            args=(recording_id,),
            daemon=True,
        ).start()
        self.send_notification_to_app()

    def _warm_browser_playback_cache(self, recording_id: str):
        try:
            self.get_browser_playable_recording_path(recording_id)
        except Exception as error:
            logger.warning(f"Failed to warm browser playback cache for {recording_id}: {error}")

    def record_worker(self, file_path, recording_id, camera_id, cap, writer):
        start_time = time.time()
        clip_start_time = start_time
        curr_time = start_time
        clip_motion_detected = False
        clip_vel = 0.0
        clip_bg_diff = 0
        clip_loudness = 0.0
        db_camera = self.db.get_camera(camera_id) or {}
        target_fps = max(1, int(db_camera.get('fps', 10) or 10))
        last_frame_seq = -1
        last_audio_chunk_seq = -1
        
        # Add small delay before starting to allow system to stabilize
        time.sleep(0.1)
        
        # Debug: Check if audio is enabled
        audio_enabled = db_camera.get('audio_enabled', False)
        logger.info(f"Recording worker started for camera {camera_id}, audio enabled: {audio_enabled}")

        while camera_id in self.active_recordings:
            lock = self.camera_service.stream_locks.get(camera_id)
            frame = None
            frame_seq = -1
            res = {'vel': 0.0, 'bg_diff': 0}
            audio_res = {}
            audio_chunk = None
            current_audio_chunk_seq = -1
            got_new_data = False

            # Minimize lock hold time by checking sequences inside lock
            if lock is not None:
                with lock:
                    # Get frame sequence first to check if we need the frame
                    current_frame_seq = int(getattr(self.camera_service, '_latest_frame_seq', {}).get(camera_id, -1))
                    current_audio_chunk_seq = getattr(self.camera_service, '_latest_audio_chunk_seq', {}).get(camera_id, -1)
                    
                    # Only get references if we have new data
                    if current_frame_seq > last_frame_seq:
                        frame_ref = getattr(self.camera_service, '_latest_frames', {}).get(camera_id)
                        frame_seq = current_frame_seq
                        last_frame_seq = current_frame_seq
                    else:
                        frame_ref = None
                        frame_seq = -1
                    
                    # Get audio chunk independently - don't tie it to video frames
                    if current_audio_chunk_seq > last_audio_chunk_seq:
                        audio_chunk_ref = getattr(self.camera_service, '_latest_audio_chunk', {}).get(camera_id)
                        if audio_chunk_ref and len(audio_chunk_ref) > 0:
                            last_audio_chunk_seq = current_audio_chunk_seq
                        else:
                            audio_chunk_ref = None
                    else:
                        audio_chunk_ref = None
                        
                    # Debug: Check if camera service has audio attributes
                    if not hasattr(self.camera_service, '_latest_audio_chunk'):
                        if not hasattr(self, '_audio_attr_warning_logged'):
                            logger.warning(f"Camera service missing _latest_audio_chunk attribute for camera {camera_id}")
                            self._audio_attr_warning_logged = True
                    
                    # Get other references quickly
                    res_ref = getattr(self.camera_service, '_latest_res_video', {}).get(camera_id)
                    audio_res_ref = getattr(self.camera_service, '_latest_res_audio', {}).get(camera_id)
                    
                # Do expensive copying operations outside the lock (only if we got new data)
                frame = frame_ref.copy() if frame_ref is not None else None
                res = res_ref.copy() if isinstance(res_ref, dict) else {'vel': 0.0, 'bg_diff': 0}
                audio_res = audio_res_ref.copy() if isinstance(audio_res_ref, dict) else {}
                
                # Process audio chunk outside lock (only if new and matched with frame)
                if audio_chunk_ref and len(audio_chunk_ref) > 0:
                    audio_chunk = audio_chunk_ref  # bytes are immutable, no need to copy
            else:
                # No lock available, check sequences quickly without blocking
                current_frame_seq = int(getattr(self.camera_service, '_latest_frame_seq', {}).get(camera_id, -1))
                current_audio_chunk_seq = getattr(self.camera_service, '_latest_audio_chunk_seq', {}).get(camera_id, -1)
                
                # Only get references if we have new data
                if current_frame_seq > last_frame_seq:
                    frame_ref = getattr(self.camera_service, '_latest_frames', {}).get(camera_id)
                    frame_seq = current_frame_seq
                    last_frame_seq = current_frame_seq
                else:
                    frame_ref = None
                    frame_seq = -1
                
                # Get audio chunk independently - don't tie it to video frames
                if current_audio_chunk_seq > last_audio_chunk_seq:
                    audio_chunk_ref = getattr(self.camera_service, '_latest_audio_chunk', {}).get(camera_id)
                    if audio_chunk_ref and len(audio_chunk_ref) > 0:
                        last_audio_chunk_seq = current_audio_chunk_seq
                    else:
                        audio_chunk_ref = None
                else:
                    audio_chunk_ref = None
                
                # Debug: Check if camera service has audio attributes
                if not hasattr(self.camera_service, '_latest_audio_chunk'):
                    if not hasattr(self, '_audio_attr_warning_logged'):
                        logger.warning(f"Camera service missing _latest_audio_chunk attribute for camera {camera_id}")
                        self._audio_attr_warning_logged = True
                
                res_ref = getattr(self.camera_service, '_latest_res_video', {}).get(camera_id)
                audio_res_ref = getattr(self.camera_service, '_latest_res_audio', {}).get(camera_id)
                
                # Do copying operations (only if we got new data)
                frame = frame_ref.copy() if frame_ref is not None else None
                res = res_ref.copy() if isinstance(res_ref, dict) else {'vel': 0.0, 'bg_diff': 0}
                audio_res = audio_res_ref.copy() if isinstance(audio_res_ref, dict) else {}
                
                # Process audio chunk (collect audio independently of video frames)
                if audio_chunk_ref and len(audio_chunk_ref) > 0:
                    audio_chunk = audio_chunk_ref

            # Frame will be None if no new frame available (sequence hasn't advanced)

            curr_time = time.time()
            if curr_time - start_time > self.motion_check_interval:
                recent_motion_detected = False

                res_timestamp = float(res.get('ts', 0.0) or 0.0)
                is_stale_motion_sample = (res_timestamp <= 0.0) or (
                    (curr_time - res_timestamp) > self.motion_result_max_age_sec
                )

                if is_stale_motion_sample:
                    vel = 0.0
                    bg_diff = 0
                else:
                    vel = float(res.get('vel', 0.0))
                    bg_diff = int(res.get('bg_diff', 0))

                if vel > self.max_velocity or bg_diff >= self.max_bg_diff:
                    recent_motion_detected = True
                    clip_motion_detected = True
                    clip_vel = max(clip_vel, vel)
                    clip_bg_diff = max(clip_bg_diff, bg_diff)
                clip_loudness = max(clip_loudness, float(audio_res.get('int', 0.0)))

                if not recent_motion_detected or (curr_time - clip_start_time) > self.max_clip_length:
                    self.process_recorded_clip(
                        recording_id,
                        file_path,
                        writer,
                        clip_motion_detected,
                        clip_start_time,
                        curr_time,
                        vel=clip_vel,
                        bg_diff=clip_bg_diff,
                        loudness=clip_loudness,
                    )
                    recording_id, file_path, writer = self.init_recording(
                        camera_id,
                        self.db.get_camera(camera_id),
                        cap,
                    )
                    clip_start_time = curr_time
                    clip_motion_detected = False
                    clip_vel = 0.0
                    clip_bg_diff = 0
                    clip_loudness = 0.0
                start_time = curr_time

            # Write frame if available, always try to write audio if available
            if frame is not None:
                writer.write_frame_with_timing(frame, None)  # Don't pass audio here
            
            # Write audio chunk separately if available - this prevents missing audio
            if audio_chunk and len(audio_chunk) > 0:
                success = writer.write_audio(audio_chunk)
                if not success:
                    logger.warning(f"Failed to write audio chunk for camera {camera_id}")
            elif audio_enabled and frame is not None:
                # Log when we have video but no audio (debugging)
                if hasattr(self, '_audio_debug_counter'):
                    self._audio_debug_counter += 1
                    if self._audio_debug_counter % 30 == 0:  # Log every 30 frames without audio
                        logger.debug(f"Camera {camera_id}: No audio chunk available (frame seq: {frame_seq})")
                else:
                    self._audio_debug_counter = 1
            
            # Small sleep to prevent busy waiting when no new data
            if frame is None and audio_chunk is None:
                time.sleep(0.01)

        self.process_recorded_clip(
            recording_id,
            file_path,
            writer,
            clip_motion_detected,
            clip_start_time,
            curr_time,
            vel=clip_vel,
            bg_diff=clip_bg_diff,
            loudness=clip_loudness,
        )

    def start_recording(self, camera_id: str) -> Optional[str]:
        db_camera = self.db.get_camera(camera_id)
        if camera_id in self.active_recordings:
            pass  # Camera already recording
            return self.active_recordings[camera_id]['recording_id']

        if camera_id not in self.camera_service._camera_streams:
            success = self.camera_service.start_camera(camera_id)
            if not success:
                return None
        cap = self.camera_service._camera_streams.get(camera_id)

        recording_id, file_path, writer = self.init_recording(camera_id, db_camera, cap)
        recording_thread = threading.Thread(
            target=self.record_worker,
            args=(file_path, recording_id, camera_id, cap, writer),
            daemon=True,
        )

        self.active_recordings[camera_id] = {
            'recording_id': recording_id,
            'thread': recording_thread,
            'start_time': datetime.now(),
        }

        recording_thread.start()
        self.db.update_camera(camera_id, {'status': CameraStatus.RECORDING.value})

        return recording_id

    def stop_recording(self, camera_id: str):
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            raise ValueError(f"Camera not found: {camera_id}")

        if camera_id not in self.active_recordings:
            raise ValueError(f"Camera {camera_id} is not recording")

        recording_info = self.active_recordings.pop(camera_id)
        recording_info['thread'].join(timeout=5)
        self.db.update_camera(camera_id, {'status': CameraStatus.ONLINE.value})
        pass  # Recording stopped
        

    def get_recordings(self, camera_id: Optional[str] = None) -> List[Recording]:
        db_recordings = self.db.get_recordings_by_camera(camera_id) if camera_id else self.db.get_all_recordings()
        recordings = []
        for db_recording in db_recordings:
            file_path = db_recording['file_path']
            filename = os.path.basename(file_path)
            created_at_str = db_recording.get('created_at')
            started_at_str = db_recording.get('start_time')
            ended_at_str = db_recording.get('end_time')
            metadata = db_recording.get('metadata')
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata) if metadata else None
                except Exception:
                    metadata = None

            recording = Recording(
                id=db_recording['id'],
                camera_id=db_recording['camera_id'],
                filename=filename,
                duration=db_recording.get('duration'),
                file_size=db_recording.get('file_size'),
                status=RecordingStatus(db_recording['status']),
                created_at=datetime.fromisoformat(created_at_str) if created_at_str else datetime.fromisoformat(started_at_str),
                started_at=datetime.fromisoformat(started_at_str) if started_at_str else None,
                ended_at=datetime.fromisoformat(ended_at_str) if ended_at_str else None,
                file_path=file_path,
                metadata=metadata,
            )
            recordings.append(recording)

        return recordings

    def get_recording_path(self, recording_id: str) -> str:
        db_recording = self.db.get_recording(recording_id)
        if not db_recording:
            raise ValueError(f"Recording not found: {recording_id}")

        file_path = db_recording.get('file_path')
        if not file_path:
            raise ValueError(f"Recording file path missing: {recording_id}")

        normalized_path = os.path.abspath(file_path)
        if os.path.exists(normalized_path):
            if db_recording.get('file_path') != normalized_path:
                self.db.update_recording(recording_id, {'file_path': normalized_path})
            return normalized_path

        candidates: List[str] = []
        filename = os.path.basename(file_path)
        camera_id = db_recording.get('camera_id')
        if filename and camera_id:
            candidates.append(os.path.join(self.recordings_dir, str(camera_id), filename))
        if filename:
            candidates.append(os.path.join(self.recordings_dir, filename))

        for candidate in candidates:
            candidate_abs = os.path.abspath(candidate)
            if os.path.exists(candidate_abs):
                if db_recording.get('file_path') != candidate_abs:
                    self.db.update_recording(recording_id, {'file_path': candidate_abs})
                return candidate_abs

        raise ValueError(f"Recording file not found: {recording_id}")

    def _get_media_codecs(self, file_path: str) -> Dict[str, Optional[str]]:
        codecs = {'video': None, 'audio': None}
        try:
            result = subprocess.run(
                [
                    'ffprobe',
                    '-v', 'error',
                    '-show_entries', 'stream=codec_type,codec_name',
                    '-of', 'json',
                    file_path,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return codecs

            payload = json.loads(result.stdout or '{}')
            for stream in payload.get('streams', []):
                stream_type = str(stream.get('codec_type') or '').strip().lower()
                codec_name = str(stream.get('codec_name') or '').strip().lower() or None
                if stream_type == 'video' and codecs['video'] is None:
                    codecs['video'] = codec_name
                if stream_type == 'audio' and codecs['audio'] is None:
                    codecs['audio'] = codec_name
                if codecs['video'] and codecs['audio']:
                    break
        except Exception:
            return codecs

        return codecs

    def get_browser_playable_recording_path(self, recording_id: str) -> str:
        source_path = self.get_recording_path(recording_id)
        codecs = self._get_media_codecs(source_path)
        video_codec = codecs.get('video')
        audio_codec = codecs.get('audio')

        browser_video_codecs = {'h264', 'avc1'}
        browser_audio_codecs = {'aac', 'mp3'}
        video_is_browser_compatible = video_codec in browser_video_codecs
        audio_is_browser_compatible = (audio_codec is None) or (audio_codec in browser_audio_codecs)

        if video_is_browser_compatible and audio_is_browser_compatible:
            return source_path

        root, ext = os.path.splitext(source_path)
        playable_path = f"{root}.browser{ext or '.mp4'}"

        source_mtime = os.path.getmtime(source_path)
        if os.path.exists(playable_path):
            playable_mtime = os.path.getmtime(playable_path)
            if playable_mtime >= source_mtime and os.path.getsize(playable_path) > 0:
                return playable_path

        cmd = [
            'ffmpeg',
            '-y',
            '-i', source_path,
            '-map', '0:v:0',
            '-map', '0:a:0?',
            '-c:v', 'copy' if video_is_browser_compatible else 'libx264',
        ]

        if not video_is_browser_compatible:
            cmd += [
                '-preset', 'ultrafast',
                '-crf', '28',
                '-pix_fmt', 'yuv420p',
            ]

        cmd += [
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            playable_path,
        ]

        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(playable_path) or os.path.getsize(playable_path) == 0:
            stderr_tail = (result.stderr or '')[-500:]
            logger.error(f"Failed to transcode recording {recording_id} for browser playback: {stderr_tail}")
            return source_path

        logger.info(f"Transcoded recording for browser playback: {playable_path}")
        return playable_path

    # -------------------------------------------------------------------------
    # Archive methods
    # -------------------------------------------------------------------------

    def export_archive(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        min_vel: Optional[float] = None,
        min_diff: Optional[float] = None,
        min_duration: Optional[float] = None,
        delete_after: bool = False,
        exclude_mode: bool = True,
        label_filter: Optional[List[str]] = None,
    ) -> dict:
        """Copy filtered completed recordings to a timestamped sub-directory under
        archive_dir and write a recordings.yaml manifest."""
        os.makedirs(self.archive_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        archive_name = f"archive_{timestamp}"
        archive_path = os.path.join(self.archive_dir, archive_name)
        os.makedirs(archive_path, exist_ok=True)

        # Parse date range filters (inclusive, cover the whole end day)
        dt_from = None
        dt_to = None
        if date_from:
            try:
                dt_from = datetime.fromisoformat(date_from)
            except Exception:
                pass
        if date_to:
            try:
                dt_to = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59, microsecond=999999)
            except Exception:
                pass

        recordings = self.get_recordings()
        completed = [r for r in recordings if r.status.value == 'completed']

        # Count totals per camera before filtering
        camera_total: Dict[str, int] = {}
        for rec in completed:
            camera_total[rec.camera_id] = camera_total.get(rec.camera_id, 0) + 1

        camera_archived: Dict[str, int] = {}
        exported = []
        for rec in completed:
            # Date filter
            if dt_from and rec.started_at and rec.started_at < dt_from:
                continue
            if dt_to and rec.started_at and rec.started_at > dt_to:
                continue
            # Metadata filters
            meta = rec.metadata or {}
            vel = float(meta.get('vel') or 0)
            diff = float(meta.get('diff') or 0)
            dur = float(rec.duration or 0)
            if min_vel is not None and (vel <= min_vel if exclude_mode else vel > min_vel):
                continue
            if min_diff is not None and (diff <= min_diff if exclude_mode else diff > min_diff):
                continue
            if min_duration is not None and (dur <= min_duration if exclude_mode else dur > min_duration):
                continue
            if label_filter:
                rec_label = str(meta.get('label') or '')
                if rec_label not in label_filter:
                    continue

            try:
                src = self.get_recording_path(rec.id)
            except ValueError as _e:
                logger.warning(f"Archive export: skipping {rec.id}, file not found: {_e}")
                continue
            try:
                rel_path = os.path.relpath(src, self.recordings_dir)
            except ValueError:
                rel_path = os.path.basename(src)
            dst = os.path.join(archive_path, rel_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            camera_archived[rec.camera_id] = camera_archived.get(rec.camera_id, 0) + 1
            exported.append({
                'id': rec.id,
                'camera_id': rec.camera_id,
                'filename': rec.filename,
                'file_path': rel_path,
                'duration': rec.duration,
                'file_size': rec.file_size,
                'status': rec.status.value,
                'started_at': rec.started_at.isoformat() if rec.started_at else None,
                'ended_at': rec.ended_at.isoformat() if rec.ended_at else None,
                'created_at': rec.created_at.isoformat() if rec.created_at else None,
                'metadata': rec.metadata,
            })

        yaml_path = os.path.join(archive_path, 'recordings.yaml')
        with open(yaml_path, 'w') as fh:
            yaml.dump({
                'archived_at': datetime.now().isoformat(),
                'recordings_count': len(exported),
                'recordings': exported,
            }, fh, default_flow_style=False, allow_unicode=True)

        per_camera = {
            cam: {'archived': camera_archived.get(cam, 0), 'total': total}
            for cam, total in camera_total.items()
        }

        deleted_count = 0
        if delete_after:
            for entry in exported:
                try:
                    self.delete_recording(entry['id'])
                    deleted_count += 1
                except Exception as _del_err:
                    logger.warning(f"Archive: could not remove {entry['id']} after archiving: {_del_err}")

        logger.info(f"Exported {len(exported)} recordings to archive: {archive_path}")
        return {
            'archive_name': archive_name,
            'archive_path': archive_path,
            'recordings_count': len(exported),
            'per_camera': per_camera,
            'deleted_count': deleted_count,
        }

    def list_archives(self) -> List[dict]:
        """List archive sub-directories that contain a recordings.yaml."""
        if not os.path.isdir(self.archive_dir):
            return []
        archives = []
        for entry in sorted(os.listdir(self.archive_dir), reverse=True):
            entry_path = os.path.join(self.archive_dir, entry)
            yaml_path = os.path.join(entry_path, 'recordings.yaml')
            if os.path.isdir(entry_path) and os.path.exists(yaml_path):
                try:
                    with open(yaml_path) as fh:
                        meta = yaml.safe_load(fh) or {}
                    archives.append({
                        'name': entry,
                        'path': entry_path,
                        'archived_at': meta.get('archived_at'),
                        'recordings_count': meta.get('recordings_count', 0),
                    })
                except Exception as err:
                    archives.append({'name': entry, 'path': entry_path, 'archived_at': None, 'recordings_count': 0})
        return archives

    def load_archive(self, archive_path: str) -> List[str]:
        """Register recordings from an archive's recordings.yaml into the DB.
        Returns a list of recording IDs that were loaded."""
        yaml_path = os.path.join(archive_path, 'recordings.yaml')
        if not os.path.exists(yaml_path):
            raise ValueError(f"No recordings.yaml found in: {archive_path}")

        with open(yaml_path) as fh:
            meta = yaml.safe_load(fh) or {}
        recordings_meta = meta.get('recordings', [])

        loaded_ids = []
        for rec in recordings_meta:
            rec_id = rec.get('id')
            if not rec_id:
                continue
            existing = self.db.get_recording(rec_id)
            if existing:
                loaded_ids.append(rec_id)
                continue
            rel_path = rec.get('file_path', '')
            abs_path = os.path.abspath(os.path.join(archive_path, rel_path))
            if not os.path.exists(abs_path):
                logger.warning(f"Archive recording file missing, skipping: {abs_path}")
                continue
            recording_data = {
                'id': rec_id,
                'camera_id': rec.get('camera_id', ''),
                'file_path': abs_path,
                'start_time': rec.get('started_at') or rec.get('created_at') or datetime.now().isoformat(),
                'end_time': rec.get('ended_at'),
                'duration': rec.get('duration'),
                'file_size': rec.get('file_size'),
                'status': 'completed',
            }
            self.db.create_recording(recording_data)
            loaded_ids.append(rec_id)

        logger.info(f"Loaded {len(loaded_ids)} recordings from archive: {archive_path}")
        return loaded_ids

    def unload_archive(self, archive_path: str) -> int:
        """Remove archive recordings from the DB (files are NOT deleted).
        Returns the number of DB entries removed."""
        yaml_path = os.path.join(archive_path, 'recordings.yaml')
        if not os.path.exists(yaml_path):
            raise ValueError(f"No recordings.yaml found in: {archive_path}")

        with open(yaml_path) as fh:
            meta = yaml.safe_load(fh) or {}
        recordings_meta = meta.get('recordings', [])

        abs_archive_path = os.path.abspath(archive_path)
        count = 0
        for rec in recordings_meta:
            rec_id = rec.get('id')
            if not rec_id:
                continue
            db_rec = self.db.get_recording(rec_id)
            if not db_rec:
                continue
            db_file = os.path.abspath(db_rec.get('file_path', ''))
            if db_file.startswith(abs_archive_path):
                self.db.delete_recording(rec_id)
                count += 1

        logger.info(f"Unloaded {count} archive recordings from DB: {archive_path}")
        return count

    def delete_recording(self, recording_id: str):
        db_recording = self.db.get_recording(recording_id)
        if not db_recording:
            raise ValueError(f"Recording not found: {recording_id}")

        file_path = db_recording['file_path']
        if os.path.exists(file_path):
            os.remove(file_path)

        root, ext = os.path.splitext(file_path)
        playable_path = f"{root}.browser{ext or '.mp4'}"
        if os.path.exists(playable_path):
            os.remove(playable_path)

        self.db.delete_recording(recording_id)
        logger.info(f"Deleted recording: {recording_id}")
