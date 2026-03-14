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

# ANSI Color codes for log formatting
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

logger = logging.getLogger(__name__)


class RecordingManager:
    def __init__(
        self,
        camera_service,
        streaming_service,
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
        mux_realtime: bool = False,
    ):
        self.camera_service = camera_service
        self.streaming_service = streaming_service
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
        self.mux_realtime = mux_realtime

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
            mux_realtime=self.mux_realtime  # Start with separate files for testing
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
     
    def delete_recording(self, recording_id: str, final_file_path: str):
        # Release writer - returns immediately in separate files mode
        
        if os.path.exists(final_file_path):
            os.system(f'rm -f {final_file_path}')
            logger.info(f"{Colors.RED}🗑️ Deleted no-motion recording:{Colors.RESET} {final_file_path}")
            
        # Also clean up audio file if it exists
        audio_file_path = getattr(writer, 'audio_file_path', None)
        if audio_file_path and os.path.exists(audio_file_path):
            try:
                os.remove(audio_file_path)
            except Exception as e:
                logger.warning(f"{Colors.YELLOW}⚠️ Could not clean up audio file:{Colors.RESET} {e}")
                
        self.db.delete_recording(recording_id)
        return
    
    def save_recording(
        self,
        recording_id: str,
        final_file_path: str,
        audio_enabled: bool,
        clip_start_time: float,
        curr_time: float,
        vel: float = 0.0,
        bg_diff: int = 0,
        loudness: float = 0.0,
    ):
        
        clip_duration = max(0, int(curr_time - clip_start_time))
        # Small delay to ensure file is fully written before getting size
        if os.path.exists(final_file_path):
            time.sleep(0.1)  # 100ms delay
            file_size = os.path.getsize(final_file_path)
        else:
            file_size = 0
        
        # Log successful recording save
        logger.info(f"{Colors.GREEN}💾 Saved recording:{Colors.RESET} {final_file_path} (duration: {clip_duration}s, size: {file_size} bytes, vel: {vel:.2f}, bg_diff: {bg_diff})")
        
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
                    'audio_recorded': audio_enabled,
                },
            },
        )
        logger.info(f"{Colors.MAGENTA}✨ Saving recording with motion:{Colors.RESET} {final_file_path}")
        self.send_notification_to_app()
        
    def record_worker(self, file_path, recording_id, camera_id, cap, writer):
        start_time = time.time()
        clip_start_time = start_time
        curr_time = start_time
        recent_motion_detected = False
        clip_motion_detected = False
        clip_vel = 0.0
        clip_bg_diff = 0
        clip_loudness = 0.0
        db_camera = self.db.get_camera(camera_id) or {}
        
        # Add small delay before starting to allow system to stabilize
        time.sleep(0.1)
        
        # Debug: Check if audio is enabled
        audio_enabled = db_camera.get('audio_enabled', False)
        logger.info(f"{Colors.BLUE}🎬 Recording worker started{Colors.RESET} for camera {camera_id}, audio enabled: {audio_enabled}")
        # Track last motion check time
        last_motion_check = start_time
        _ext = file_path.split('.')[1]
        with open(file_path.replace(f'.{_ext}', '.txt'), 'w') as f:
            f.write('vel,bg_diff,loudness\n')

        while camera_id in self.active_recordings:
            loop_start = time.time()
            
            # Get video frame and results (less frequent)
            lock = self.camera_service.stream_locks.get(camera_id)
            frame = None
            res = {'vel': 0.0, 'bg_diff': 0}
            audio_res = {}
            
            frame = self.streaming_service._get_spmc_data(camera_id, f"recorder_{camera_id}", 'frames')
            audio_chunk = self.streaming_service._get_spmc_data(camera_id, f"recorder_{camera_id}", 'audio') if audio_enabled else None
            results = self.streaming_service._get_spmc_data(camera_id, f"recorder_{camera_id}", 'results')
            res = results.get('video', {'vel': 0.0, 'bg_diff': 0}) if results else {'vel': 0.0, 'bg_diff': 0}
            audio_res = results.get('audio', {}) if results else {}
            curr_time = time.time()
            
            # Read detection flags directly from streaming service (staleness already handled)
            detected_vel = bool(res.get('detected_vel', False))
            detection_bg_diff = bool(res.get('detection_bg_diff', False))
            vel = float(res.get('vel', 0.0))
            bg_diff = int(res.get('bg_diff', 0))

            if detected_vel or detection_bg_diff:
                recent_motion_detected = True
                clip_motion_detected   = True
                clip_vel = max(clip_vel, vel)
                clip_bg_diff = max(clip_bg_diff, bg_diff)

            if audio_enabled and audio_res:
                # Read detected loudness flag from audio results
                detected_loudness = bool(audio_res.get('detected_loudness', False))
                loudness = float(audio_res.get('int', 0.0))
                clip_loudness = max(clip_loudness, loudness)
            
            # Motion detection check (less frequent to avoid blocking audio) 
            # [i] > interval, no motion -> end clip
            # [ii] > interval, motion, < max length -> continue clip
            # [iii] > interval, motion, > max length -> end clip
            if curr_time - last_motion_check > self.motion_check_interval:
                logger.info(f"{Colors.YELLOW}⚠️ Motion check for camera {(curr_time - clip_start_time)},{self.max_clip_length}, {clip_motion_detected} - vel: {vel:.2f}, bg_diff: {bg_diff}, loudness: {clip_loudness:.2f}{Colors.RESET}")
                start_new_recording = False
                
                if not recent_motion_detected:
                    recorded_file_path = writer.release()
                    self.delete_recording(recording_id, recorded_file_path)
                    start_new_recording = True
                else:
                    if (curr_time - clip_start_time) < self.max_clip_length:
                        logger.info(f"{Colors.GREEN}⏱️ Continuing recording{Colors.RESET} for camera {camera_id} - motion: {clip_motion_detected}, duration: {int(curr_time - clip_start_time)}s, vel: {clip_vel:.2f}, bg_diff: {clip_bg_diff}, loudness: {clip_loudness:.2f}")
                    else:
                        logger.info(f"{Colors.CYAN}🎬 Clip ended{Colors.RESET} for camera {camera_id} - motion: {clip_motion_detected}, duration: {int(curr_time - clip_start_time)}s, vel: {clip_vel:.2f}, bg_diff: {clip_bg_diff}, loudness: {clip_loudness:.2f}")
                        
                        self.save_recording(
                            recording_id,
                            writer,
                            clip_motion_detected,
                            clip_start_time,
                            curr_time,
                            vel=clip_vel,
                            bg_diff=clip_bg_diff,
                            loudness=clip_loudness,
                        )
                        start_new_recording = True
                
                if start_new_recording:
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
                last_motion_check = curr_time
                recent_motion_detected = False
            
            # Write video frame if available (but don't wait for it)
            if frame is not None:
                writer.write_frame_with_timing(frame, None)  # Audio already written above
            if audio_enabled and audio_chunk is not None:
                writer.write_audio(audio_chunk)  # Write audio chunk immediately if enabled
            with open(file_path.replace(f'.{_ext}', '.txt'), 'a') as f:
                f.write(f"{vel},{bg_diff},{clip_loudness}\n")            
            
        self.process_recorded_clip(
            recording_id,
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
            self.stop_recording(camera_id)
            #return self.active_recordings[camera_id]['recording_id']
        
        if camera_id not in self.camera_service._camera_streams:
            success = self.camera_service.start_video(camera_id)
            if not success:
                logger.error(f"{Colors.RED} Failed to start camera stream for recording:{Colors.RESET} {camera_id}")
                return None
        cap = self.camera_service._camera_streams.get(camera_id)

        # Register as consumer for this camera
        consumer_id = f"recorder_{camera_id}"
        data_types = ['frames', 'results']
        if db_camera and db_camera.get('audio_enabled', False):
            data_types.append('audio')
        self.streaming_service.register_consumer(camera_id, consumer_id, data_types)

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
            logger.error(f"{Colors.RED}❌ Camera is not recording:{Colors.RESET} {camera_id}")

        try:
            recording_info = self.active_recordings.pop(camera_id)
            recording_info['thread'].join(timeout=5)
            self.db.update_camera(camera_id, {'status': CameraStatus.ONLINE.value})
        except Exception as error:
            logger.warning(f"{Colors.YELLOW}⚠️ Error stopping recording{Colors.RESET} for camera {camera_id}: {error}")
        

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

    def _get_video_codec(self, file_path: str) -> str:
        """Get the video codec of a video file using ffprobe."""
        try:
            cmd = [
                'ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name', '-of', 'csv=p=0',
                file_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                codec = result.stdout.strip().lower()
                logger.debug(f"Detected video codec for {file_path}: {codec}")
                return codec
            else:
                logger.warning(f"Could not detect codec for {file_path}: {result.stderr}")
                return "unknown"
        except subprocess.TimeoutExpired:
            logger.error(f"ffprobe timeout while detecting codec for: {file_path}")
            return "unknown"
        except FileNotFoundError:
            logger.error("ffprobe not found - install ffmpeg package")
            return "unknown"
        except Exception as e:
            logger.error(f"Error detecting video codec for {file_path}: {e}")
            return "unknown"

    def get_browser_playable_recording_path(self, recording_id: str) -> str:
        """Return recording path optimized for HTML5 video playback.

        If source codec is not broadly browser-compatible, create and reuse a
        transcoded H.264 version alongside the source file.
        """
        source_path = self.get_recording_path(recording_id)
        codec = self._get_video_codec(source_path)

        # Most reliable baseline for browser playback in MP4 containers
        if codec in {"h264", "avc1"}:
            return source_path

        root, ext = os.path.splitext(source_path)
        playable_path = f"{root}.browser{ext or '.mp4'}"

        source_mtime = os.path.getmtime(source_path)
        if os.path.exists(playable_path):
            playable_mtime = os.path.getmtime(playable_path)
            if playable_mtime >= source_mtime and os.path.getsize(playable_path) > 0:
                return playable_path

        cmd = [
            "ffmpeg",
            "-y",
            "-i", source_path,
            "-map", "0:v:0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-an",
            playable_path,
        ]

        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(playable_path) or os.path.getsize(playable_path) == 0:
            stderr_tail = (result.stderr or "")[-500:]
            logger.error(f"{Colors.RED}❌ Failed to transcode recording{Colors.RESET} {recording_id} for browser playback: {stderr_tail}")
            return source_path

        logger.info(f"{Colors.GREEN}✅ Transcoded recording for browser playback:{Colors.RESET} {playable_path}")
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
            
            # Copy main file
            shutil.copy2(src, dst)
            
            # Copy all related files with the same stem but different extensions
            related_files = self._find_related_files(src)
            dst_dir = os.path.dirname(dst)
            src_dir = os.path.dirname(src)
            
            # Copy each related file
            for related_file in related_files:
                related_src = os.path.join(src_dir, related_file)
                related_dst = os.path.join(dst_dir, related_file)
                try:
                    shutil.copy2(related_src, related_dst)
                except Exception as e:
                    logger.warning(f"Failed to copy related file {related_file}: {e}")
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
                
            # Copy main file back to recordings directory
            src = abs_path
            dst_rel_path = rel_path if rel_path else os.path.basename(abs_path)
            dst = os.path.join(self.recordings_dir, dst_rel_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
                
                # Copy all related files with the same stem but different extensions
                related_files = self._find_related_files(src)
                dst_dir = os.path.dirname(dst)
                src_dir = os.path.dirname(src)
                
                # Copy each related file
                for related_file in related_files:
                    related_src = os.path.join(src_dir, related_file)
                    related_dst = os.path.join(dst_dir, related_file)
                    try:
                        if not os.path.exists(related_dst):
                            shutil.copy2(related_src, related_dst)
                            logger.debug(f"Imported related file: {related_file}")
                    except Exception as e:
                        logger.warning(f"Failed to import related file {related_file}: {e}")
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

    def _find_related_files(self, file_path: str) -> List[str]:
        """Find all files with the same file stem but different extensions.
        
        Args:
            file_path: Path to the main file
            
        Returns:
            List of filenames (not full paths) that share the same stem
        """
        file_dir = os.path.dirname(file_path)
        file_stem = os.path.splitext(os.path.basename(file_path))[0]
        related_files = []
        
        if os.path.exists(file_dir):
            for filename in os.listdir(file_dir):
                if filename == os.path.basename(file_path):
                    continue  # Skip the main file itself
                    
                file_base_stem = os.path.splitext(filename)[0]
                # Handle files like recording.browser.mp4 -> stem is "recording"
                if '.' in file_base_stem:
                    base_stem = file_base_stem.split('.')[0]
                else:
                    base_stem = file_base_stem
                
                if base_stem == file_stem:
                    related_files.append(filename)
        
        return related_files

    def delete_recording(self, recording_id: str):
        db_recording = self.db.get_recording(recording_id)
        if not db_recording:
            raise ValueError(f"Recording not found: {recording_id}")

        file_path = db_recording['file_path']
        
        # Delete main file
        if os.path.exists(file_path):
            os.remove(file_path)

        # Delete all related files with the same stem but different extensions
        related_files = self._find_related_files(file_path)
        file_dir = os.path.dirname(file_path)
        deleted_files = [os.path.basename(file_path)]
        
        for related_file in related_files:
            related_file_path = os.path.join(file_dir, related_file)
            try:
                if os.path.exists(related_file_path):
                    os.remove(related_file_path)
                    deleted_files.append(related_file)
                    logger.debug(f"Deleted related file: {related_file}")
            except Exception as e:
                logger.warning(f"Failed to delete related file {related_file}: {e}")

        self.db.delete_recording(recording_id)
        logger.info(f"Deleted recording {recording_id} and {len(deleted_files)} related files: {deleted_files}")
