import json
import logging
import os
import glob
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import cv2
import numpy as np
import psutil
import yaml

from models.camera import CameraStatus
from models.recording import Recording, RecordingStatus
from services.av_writer import AVWriterV2 as AVWriter  # Flexible writer - can switch between V2Flexible and V3
from services.audio_recording_utils import AudioRecordingUtils
from services.drawing_utils import StreamDrawingHelper

PROJECT_SRC_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))
if PROJECT_SRC_PATH not in sys.path:
    sys.path.append(PROJECT_SRC_PATH)

from improc.optical_flow import OpticalFlowTracker
from services.colors import Colors

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
        auto_archive_days: int = 7,
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
        self.auto_archive_days = auto_archive_days

        # Overlay generation progress tracking
        self._overlay_progress: Dict[str, dict] = {}  # recording_id -> {status, progress, total_frames, error}
        self._overlay_threads: Dict[str, threading.Thread] = {}

        self.clip_start_time: Dict[str, float] = {}
        self.clip_motion_detected: Dict[str, bool] = {}
        self.clip_vel: Dict[str, float] = {}
        self.clip_max_vel: Dict[str, float] = {}
        self.clip_bg_diff: Dict[str, int] = {}
        self.clip_loudness: Dict[str, float] = {}
        self.clip_loudness_frame_count: Dict[str, int] = {}
        self.clip_motion_frame: Dict[str, Optional[np.ndarray]] = {}
        self.clip_motion_frame_count: Dict[str, int] = {}
        self.clip_no_motion_frame_count: Dict[str, int] = {}
        self.clean_up_extensions = ['.overlay.mp4']  # Extensions to clean up if no motion detected
        self._metrics_buffer: Dict[str, list] = {}  # camera_id -> buffered lines

    def _flush_metrics(self, camera_id: str, file_path: str) -> None:
        """Flush buffered metrics lines to the .txt file on disk."""
        lines = self._metrics_buffer.pop(camera_id, [])
        if not lines:
            return
        _ext = file_path.rsplit('.', 1)[-1]
        txt_path = file_path.replace(f'.{_ext}', '.txt')
        try:
            with open(txt_path, 'a') as f:
                f.writelines(lines)
        except Exception:
            logger.warning(f"{Colors.RED}Failed to flush metrics to {txt_path}{Colors.RESET}")

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

    def init_recording(self, camera_id: str) -> tuple[str, str, AVWriter]:
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
        cap = self.camera_service._camera_streams.get(camera_id)
        db_camera = self.db.get_camera(camera_id)
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
        
        self.clip_start_time[camera_id] = timestamp
        self.clip_motion_detected[camera_id] = False
        self.clip_vel[camera_id] = 0.0
        self.clip_max_vel[camera_id] = 0.0
        self.clip_bg_diff[camera_id] = 0
        self.clip_loudness[camera_id] = 0.0
        self.clip_loudness_frame_count[camera_id] = 0
        self.clip_motion_frame[camera_id] = None
        self.clip_motion_frame_count[camera_id] = 0
        self.clip_no_motion_frame_count[camera_id] = 0

        _ext = file_path.split('.')[-1]
        with open(file_path.replace(f'.{_ext}', '.txt'), 'w') as f:
            f.write('vel,bg_diff,loudness\n')
        self._metrics_buffer[camera_id] = []
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

    def auto_archive_oldest_dates(self) -> dict:
        """Archive recordings on the oldest date(s) when we have more than auto_archive_days
        distinct recording dates.  Returns the export_archive result dict or empty dict."""
        if self.auto_archive_days <= 0:
            return {}

        recordings = self.get_recordings()
        completed = [r for r in recordings if r.status.value == 'completed']
        if not completed:
            return {}

        # Collect distinct dates only from non-archive recordings
        # (loaded archive data is tagged source='archive' and excluded)
        date_set = set()
        for rec in completed:
            meta = rec.metadata or {}
            if meta.get('source') == 'archive':
                continue
            dt = rec.started_at or rec.created_at
            if dt:
                date_set.add(dt.date())

        if len(date_set) <= self.auto_archive_days:
            return {}

        # Find the oldest date to archive
        sorted_dates = sorted(date_set)
        oldest_date = sorted_dates[0]
        date_str = oldest_date.isoformat()  # YYYY-MM-DD

        logger.info(
            f"Auto-archive: {len(date_set)} distinct recording dates exceed limit of "
            f"{self.auto_archive_days}. Archiving oldest date: {date_str}"
        )

        return self.export_archive(
            date_from=date_str,
            date_to=date_str,
            delete_after=True,
            clean_up_extensions=self.clean_up_extensions,
        )

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
     
    def delete_no_motion_recording(self, camera_id: str, recording_id: str, final_file_path: str):
        # Release writer - returns immediately in separate files mode
        _ext = final_file_path.split('.')[1]
        for file in [final_file_path, final_file_path.replace(f'.{_ext}', '.wav'), final_file_path.replace(f'.{_ext}', '.txt')]:
            if os.path.exists(file):
                os.system(f'rm -f {file}')
                logger.info(f"{Colors.RED}🗑️ Deleted no-motion recording:{Colors.RESET} {file}")
            
        # Also clean up audio file if it exists - Note: 'writer' variable not available in this context
        # This would need to be passed as parameter or handled by the caller
        
        self.db.delete_recording(recording_id)
        return
    
    def save_recording(
        self,
        camera_id: str,
        recording_id: str,
        final_file_path: str,
        audio_enabled: bool,
        curr_time: float,
    ):
        
        clip_duration = max(0, int(curr_time - self.clip_start_time.get(camera_id, curr_time)))
        # Small delay to ensure file is fully written before getting size
        if os.path.exists(final_file_path):
            time.sleep(0.1)  # 100ms delay
            file_size = os.path.getsize(final_file_path)
        else:
            file_size = 0
        
        # Log successful recording save
        logger.info(f"{Colors.GREEN}💾 Saved recording:{Colors.RESET} {final_file_path} (duration: {clip_duration}s, size: {file_size} bytes, vel: {self.clip_vel.get(camera_id, 0.0):.2f}, bg_diff: {self.clip_bg_diff.get(camera_id, 0)})")
        motion_frame = self.clip_motion_frame.get(camera_id)
        if motion_frame is not None:
            cv2.imwrite(final_file_path.replace('.mp4', '.jpg'), motion_frame)

        self.clip_vel[camera_id] = round(self.clip_vel[camera_id] / self.clip_motion_frame_count[camera_id] if self.clip_motion_frame_count[camera_id] > 0 else 0.0, 2)
        self.clip_bg_diff[camera_id] = round(self.clip_bg_diff[camera_id] / self.clip_motion_frame_count[camera_id] if self.clip_motion_frame_count[camera_id] > 0 else 0, 2)
        self.clip_loudness[camera_id] = round(self.clip_loudness[camera_id] / self.clip_loudness_frame_count[camera_id] if self.clip_loudness_frame_count[camera_id] > 0 else 0.0, 2)
        clip_activity_percentage = round(self.clip_motion_frame_count[camera_id] / self.clip_no_motion_frame_count[camera_id] if self.clip_no_motion_frame_count[camera_id] > 0 else 1.0, 2)
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
                    'vel': float(self.clip_vel.get(camera_id, 0.0)),
                    'diff': int(self.clip_bg_diff.get(camera_id, 0)),
                    'loudness': float(self.clip_loudness.get(camera_id, 0.0)),
                    'activity_percentage': float(clip_activity_percentage),   
                    'audio_recorded': audio_enabled,
                },
            },
        )
        logger.info(f"{Colors.MAGENTA}✨ Saving recording with motion:{Colors.RESET} {final_file_path}")
        self.send_notification_to_app()

        # Auto-archive oldest dates if we exceed the configured day limit
        try:
            self.auto_archive_oldest_dates()
        except Exception as e:
            logger.warning(f"Auto-archive check failed: {e}")
        
    def record_worker(self, camera_id):
        writer = None
        recording_id = None
        file_path = None
        audio_enabled = False
        curr_time = time.time()
        try:
            start_time = time.time()
            
            # Get camera config first before initializing recording
            db_camera = self.db.get_camera(camera_id) or {}
            audio_enabled = db_camera.get('audio_enabled', False)
            
            recording_id, file_path, writer = self.init_recording(camera_id)
            curr_time = start_time
            recent_motion_detected = False
            prev_frame = None
            
            # Audio sync: track sample deficit to keep audio aligned with video
            if audio_enabled:
                audio_sample_rate = int(db_camera.get('audio_sample_rate', 16000) or 16000)
                audio_channels = int(db_camera.get('audio_channels', 1) or 1)
                audio_chunk_size = int(db_camera.get('audio_chunk_size', 512) or 512)
                camera_fps = int(db_camera.get('fps', 30))
                samples_per_frame = audio_sample_rate / camera_fps
                audio_sample_deficit = 0.0
                # Chunk size in bytes: samples * channels * 2 bytes (s16le)
                chunk_bytes = audio_chunk_size * audio_channels * 2
                silence_chunk = b'\x00' * chunk_bytes
            
            # Add small delay before starting to allow system to stabilize
            time.sleep(0.1)
            
            logger.info(f"{Colors.BLUE}🎬 Recording worker started{Colors.RESET} for camera {camera_id}, audio enabled: {audio_enabled}")
            # Track last motion check time
            last_motion_check = start_time
            _ext = file_path.split('.')[1]

            while camera_id in self.active_recordings:
        
                frame = None
                audio_chunks = []
                res = {'vel': 0.0, 'bg_diff': 0}
                audio_res = {}
                
                frame = self.streaming_service._get_spmc_data(camera_id, f"recorder_{camera_id}", 'frames')
                if frame is not None:
                    prev_frame = frame
                elif prev_frame is not None:
                    frame = prev_frame  # Use last frame if current is missing to keep recording alive
                else:
                    time.sleep(0.01)  # No frame available yet, wait briefly before retrying
                    continue

                if audio_enabled:
                    audio_sample_deficit += samples_per_frame
                    while audio_sample_deficit >= audio_chunk_size:
                        chunk = self.streaming_service._get_spmc_data(camera_id, f"recorder_{camera_id}", 'audio')
                        audio_chunks.append(chunk if chunk is not None else silence_chunk)
                        audio_sample_deficit -= audio_chunk_size
                results = self.streaming_service._get_spmc_data(camera_id, f"recorder_{camera_id}", 'results')
                res = results.get('video', {'vel': 0.0, 'bg_diff': 0}) if results else {'vel': 0.0, 'bg_diff': 0}
                audio_res = results.get('audio', {}) if results else {}
                curr_time = time.time()
                
                # Read detection flags directly from streaming service (staleness already handled)
                detected_vel = bool(res.get('detected_vel', False))
                detection_bg_diff = bool(res.get('detection_bg_diff', False))
                vel = float(res.get('vel', 0.0))
                bg_diff = int(res.get('bg_diff', 0))
                loudness = audio_res.get('int', 0.0)  # Default value

                if detected_vel or detection_bg_diff:
                    if self.clip_motion_frame[camera_id] is None or vel > self.clip_max_vel[camera_id]:
                        self.clip_motion_frame[camera_id] = frame
                    self.clip_max_vel[camera_id] = max(self.clip_max_vel.get(camera_id, 0.0), vel)
                    recent_motion_detected = True
                    self.clip_motion_detected[camera_id] = True
                    self.clip_vel[camera_id] = self.clip_vel.get(camera_id, 0.0) + vel
                    self.clip_bg_diff[camera_id] = self.clip_bg_diff.get(camera_id, 0) + bg_diff
                    self.clip_motion_frame_count[camera_id] += 1
                else:
                    self.clip_no_motion_frame_count[camera_id] += 1

                if audio_enabled and audio_res:
                    detected_loudness = bool(audio_res.get('detected_loudness', False))
                    loudness = float(audio_res.get('int', 0.0))
                    if detected_loudness:
                        self.clip_loudness[camera_id] = self.clip_loudness.get(camera_id, 0.0) + loudness
                        self.clip_loudness_frame_count[camera_id] += 1
                
                # Motion detection check every interval
                if curr_time - last_motion_check > self.motion_check_interval:
                    clip_duration = curr_time - self.clip_start_time.get(camera_id, curr_time)
                    logger.info(f"{Colors.YELLOW}⚠️ Motion check for camera {camera_id}: duration={clip_duration:.1f}s, max={self.max_clip_length}s, recent_motion={recent_motion_detected}, clip_has_motion={self.clip_motion_detected.get(camera_id, False)}{Colors.RESET}")
                    
                    if recent_motion_detected:
                        # Recent motion detected in this interval
                        if clip_duration >= self.max_clip_length:
                            # Clip exceeds max length - save and start new  
                            self._flush_metrics(camera_id, file_path)
                            final_file_path = writer.release()
                            self.save_recording(
                                camera_id,
                                recording_id,
                                final_file_path,
                                audio_enabled,
                                curr_time
                            )
                            # Start new recording after saving
                            recording_id, file_path, writer = self.init_recording(camera_id)
                            
                            if audio_enabled:
                                audio_sample_deficit = 0.0
                            logger.info(f"{Colors.CYAN}📹 Clip saved (max length reached):{Colors.RESET} duration={int(clip_duration)}s")
                        else:
                            # Continue recording - motion still happening
                            logger.info(f"{Colors.GREEN}⏱️ Continuing recording{Colors.RESET} - recent motion detected vel ={self.clip_vel.get(camera_id, 0.0)}, bg_diff ={self.clip_bg_diff.get(camera_id, 0)}, loudness ={self.clip_loudness.get(camera_id, 0.0)}, duration={int(clip_duration)}s")
                    else:
                        # No recent motion detected - end current clip
                        self._flush_metrics(camera_id, file_path)
                        final_file_path = writer.release()
                        if self.clip_motion_detected.get(camera_id, False):
                            # Save clip - it had motion earlier
                            self.save_recording(
                                camera_id,
                                recording_id,
                                final_file_path,
                                audio_enabled,
                                curr_time)                        
                            logger.info(f"{Colors.MAGENTA}💾 Clip saved (motion ended):{Colors.RESET} duration={int(clip_duration)}s")
                        else:
                            # Delete clip - no motion throughout entire clip
                            self.delete_no_motion_recording(camera_id, recording_id, final_file_path)
                            logger.info(f"{Colors.RED}🗑️ Clip deleted (no motion):{Colors.RESET} duration={int(clip_duration)}s")
                    
                        # Start new recording after ending previous clip
                        recording_id, file_path, writer = self.init_recording(camera_id)
                        if audio_enabled:
                            audio_sample_deficit = 0.0
                    
                    # Reset for next interval
                    recent_motion_detected = False
                    last_motion_check = curr_time
                    
                # Write video frame if available (but don't wait for it)
                if frame is not None:
                    writer.write_frame_with_timeout(frame)
                    # Write audio chunks to stay in sync with video
                    if audio_enabled and audio_chunks:
                        combined_audio = b''.join(audio_chunks)
                        writer.write_audio(combined_audio)
                    self._metrics_buffer.setdefault(camera_id, []).append(f"{vel},{bg_diff},{loudness}\n")            
                
            # Final processing when recording worker ends normally
            self._flush_metrics(camera_id, file_path)
            final_file_path = writer.release()
            writer = None  # Mark as released
            if self.clip_motion_detected.get(camera_id, False):
                self.save_recording(
                    camera_id,
                    recording_id,
                    final_file_path,
                    audio_enabled,
                    curr_time
                )
            else:
                self.delete_no_motion_recording(camera_id, recording_id, final_file_path)
        except Exception:
            logger.exception(f"{Colors.RED}💀 Recording worker crashed for camera {camera_id}{Colors.RESET}")
            # Emergency: release writer if not already released
            if writer is not None:
                try:
                    final_file_path = writer.release()
                    self.delete_no_motion_recording(camera_id, recording_id, final_file_path)
                except Exception:
                    logger.exception(f"Failed emergency writer release for {camera_id}")
        finally:
            # Ensure system knows recording is no longer active
            self.active_recordings.pop(camera_id, None)
            logger.info(f"{Colors.YELLOW}Recording worker exiting for {camera_id}{Colors.RESET}")

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

        # Register as consumer for this camera
        consumer_id = f"recorder_{camera_id}"
        data_types = ['frames', 'results']
        if db_camera and db_camera.get('audio_enabled', False):
            data_types.append('audio')
        self.streaming_service.register_consumer(camera_id, consumer_id, data_types)

        recording_thread = threading.Thread(
            target=self.record_worker,
            args=(camera_id,),
            daemon=True,
        )

        self.active_recordings[camera_id] = {
            'thread': recording_thread,
            'start_time': datetime.now(),
        }
        recording_thread.start()
        self.db.update_camera(camera_id, {'status': CameraStatus.RECORDING.value})

        return camera_id

    def stop_recording(self, camera_id: str):
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            logger.error(f"{Colors.RED}❌ Camera not found:{Colors.RESET} {camera_id}")
            return

        if camera_id not in self.active_recordings or camera_id not in self.clip_start_time:
            logger.error(f"{Colors.RED}❌ Camera is not recording:{Colors.RESET} {camera_id}")
            return
        
        recording_info = self.active_recordings.pop(camera_id)
        recording_info['thread'].join(timeout=5)
        self.db.update_camera(camera_id, {'status': CameraStatus.ONLINE.value})
        
        self.clip_start_time.pop(camera_id, None)
        self.clip_motion_detected.pop(camera_id, None)
        self.clip_vel.pop(camera_id, None)
        self.clip_bg_diff.pop(camera_id, None)
        self.clip_loudness.pop(camera_id, None)
        self.clip_loudness_frame_count.pop(camera_id, None)
        self.clip_motion_frame.pop(camera_id, None)

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
        
        root, ext = os.path.splitext(source_path)
        wav_path = f"{root}.wav"
        has_wav = os.path.exists(wav_path)
        
        # If already h264 and no wav to mux, nothing to do
        if codec in {"h264", "avc1"} and not has_wav:
            return source_path

        root, ext = os.path.splitext(source_path)
        playable_path = f"{root}.browser{ext or '.mp4'}"
        
        cmd = [
            "ffmpeg",
            "-y",
            "-i", source_path,
        ]
        
        # Add audio input if .wav file exists
        if has_wav:
            cmd.extend(["-i", wav_path])
            if codec in {"h264", "avc1"}:
                # Video already h264 — copy stream, just mux audio
                cmd.extend([
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-movflags", "+faststart",
                ])
            else:
                cmd.extend([
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "libx264",
                    "-c:a", "aac",
                    "-preset", "ultrafast",
                    "-crf", "28",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                ])
        else:
            cmd.extend([
                "-map", "0:v:0",  # Video only
                "-c:v", "libx264",
                "-preset", "ultrafast", 
                "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-an",            # No audio
            ])
        
        cmd.append(playable_path)

        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(playable_path) or os.path.getsize(playable_path) == 0:
            stderr_tail = (result.stderr or "")[-500:]
            logger.error(f"{Colors.RED}❌ Failed to transcode recording{Colors.RESET} {recording_id} for browser playback: {stderr_tail}")
            return source_path

        # Replace original with transcoded file and clean up wav
        try:
            os.replace(playable_path, source_path)
        except Exception as e:
            logger.error(f"{Colors.RED}Failed to replace original with transcoded file:{Colors.RESET} {e}")
            return playable_path
        if os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except Exception as e:
                logger.warning(f"Failed to remove wav file {wav_path}: {e}")

        logger.info(f"{Colors.GREEN}✅ Transcoded and replaced original:{Colors.RESET} {source_path}")
        return source_path


    # -------------------------------------------------------------------------
    # Overlay video generation
    # -------------------------------------------------------------------------

    def generate_overlay_video(self, recording_id: str) -> str:
        """Generate an overlay video with optical flow bounding boxes and tracks.
        Returns the path to the cached overlay MP4 (creates it on first call).
        Updates self._overlay_progress[recording_id] with frame-level progress."""
        source_path = self.get_recording_path(recording_id)
        root, ext = os.path.splitext(source_path)
        overlay_path = f"{root}.overlay.mp4"

        # Cache hit — return immediately if overlay is newer than source
        if os.path.exists(overlay_path):
            if os.path.getmtime(overlay_path) >= os.path.getmtime(source_path):
                self._overlay_progress[recording_id] = {'status': 'ready', 'progress': 100, 'total_frames': 0}
                return overlay_path

        cap = cv2.VideoCapture(source_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open recording file: {source_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')

        self._overlay_progress[recording_id] = {
            'status': 'processing', 'progress': 0, 'total_frames': total_frames,
            'processed_frames': 0,
        }

        tracker = OpticalFlowTracker()
        draw_mask = None

        tmp_path = f"{root}_overlay_tmp.mp4"
        writer = cv2.VideoWriter(tmp_path, fourcc, fps, (width, height))
        if not writer.isOpened():
            cap.release()
            raise ValueError(f"Cannot create overlay writer for: {tmp_path}")

        try:
            frame_idx = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                viz_frame, _pts = tracker.detect(frame)
                writer.write(viz_frame)
                frame_idx += 1
                if frame_idx % 10 == 0 or frame_idx == total_frames:
                    self._overlay_progress[recording_id].update({
                        'progress': int(90 * frame_idx / total_frames),  # 0-90% for frame processing
                        'processed_frames': frame_idx,
                    })
        finally:
            cap.release()
            writer.release()

        # 90% — encoding phase
        self._overlay_progress[recording_id].update({'progress': 90, 'status': 'encoding'})

        # Re-encode with ffmpeg for browser-compatible H.264 + faststart
        cmd = [
            'ffmpeg', '-y', '-i', tmp_path,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-an',
            overlay_path,
        ]
        # Mux audio from wav sidecar if available
        wav_path = f"{root}.wav"
        if os.path.exists(wav_path):
            cmd = [
                'ffmpeg', '-y', '-i', tmp_path, '-i', wav_path,
                '-map', '0:v:0', '-map', '1:a:0',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
                '-movflags', '+faststart',
                overlay_path,
            ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        # Clean up temp file
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if result.returncode != 0 or not os.path.exists(overlay_path):
            self._overlay_progress[recording_id] = {'status': 'error', 'progress': 0, 'error': 'Encoding failed'}
            logger.error(f"{Colors.RED}Failed to encode overlay video:{Colors.RESET} {result.stderr[-500:]}")
            raise ValueError(f"FFmpeg overlay encoding failed for {recording_id}")

        self._overlay_progress[recording_id] = {'status': 'ready', 'progress': 100, 'total_frames': total_frames}
        logger.info(f"{Colors.GREEN}Generated overlay video:{Colors.RESET} {overlay_path}")
        return overlay_path

    def start_overlay_generation(self, recording_id: str):
        """Start overlay generation in a background thread. Idempotent."""
        status = self._overlay_progress.get(recording_id, {}).get('status')
        if status in ('processing', 'encoding'):
            return  # Already running

        # Check cache first
        try:
            source_path = self.get_recording_path(recording_id)
            root, _ext = os.path.splitext(source_path)
            overlay_path = f"{root}_overlay.mp4"
            if os.path.exists(overlay_path) and os.path.getmtime(overlay_path) >= os.path.getmtime(source_path):
                self._overlay_progress[recording_id] = {'status': 'ready', 'progress': 100, 'total_frames': 0}
                return
        except ValueError:
            pass

        self._overlay_progress[recording_id] = {'status': 'processing', 'progress': 0, 'total_frames': 0}

        def _worker():
            try:
                self.generate_overlay_video(recording_id)
            except Exception as e:
                self._overlay_progress[recording_id] = {'status': 'error', 'progress': 0, 'error': str(e)}
                logger.error(f"{Colors.RED}Overlay generation failed for {recording_id}:{Colors.RESET} {e}")
            finally:
                self._overlay_threads.pop(recording_id, None)

        t = threading.Thread(target=_worker, daemon=True)
        self._overlay_threads[recording_id] = t
        t.start()

    def get_overlay_status(self, recording_id: str) -> dict:
        """Return current overlay generation status for a recording."""
        info = self._overlay_progress.get(recording_id)
        if info:
            return info
        # Check if cached file already exists
        try:
            source_path = self.get_recording_path(recording_id)
            root, _ext = os.path.splitext(source_path)
            overlay_path = f"{root}_overlay.mp4"
            if os.path.exists(overlay_path) and os.path.getmtime(overlay_path) >= os.path.getmtime(source_path):
                return {'status': 'ready', 'progress': 100}
        except ValueError:
            pass
        return {'status': 'not_started', 'progress': 0}

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
        clean_up_extensions: Optional[List[str]] = None,
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
            
            # Copy each related file (skip extensions marked for cleanup)
            _cleanup = clean_up_extensions or []
            for related_file in related_files:
                if any(related_file.endswith(ext) for ext in _cleanup):
                    # Delete from source instead of archiving
                    related_src = os.path.join(src_dir, related_file)
                    try:
                        os.remove(related_src)
                        logger.info(f"Cleaned up {related_file} (matched clean_up_extensions)")
                    except Exception as e:
                        logger.warning(f"Failed to clean up {related_file}: {e}")
                    continue
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

        # Build nested camera → date → recordings structure for readability
        cameras_nested: Dict[str, Dict[str, list]] = {}
        for entry in exported:
            cam = entry.get('camera_id', 'unknown')
            dt_str = (entry.get('started_at') or entry.get('created_at') or '')[:10]  # YYYY-MM-DD
            if not dt_str:
                dt_str = 'unknown_date'
            cameras_nested.setdefault(cam, {}).setdefault(dt_str, []).append(entry)

        yaml_path = os.path.join(archive_path, 'recordings.yaml')
        with open(yaml_path, 'w') as fh:
            yaml.dump({
                'archived_at': datetime.now().isoformat(),
                'recordings_count': len(exported),
                'cameras': cameras_nested,
                'recordings': exported,  # flat list kept for backward compatibility
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

        # Support both nested (cameras → date → recordings) and flat formats
        recordings_meta = meta.get('recordings', [])
        if not recordings_meta and 'cameras' in meta:
            # Extract flat list from nested camera → date → recordings structure
            for _cam_id, dates in meta['cameras'].items():
                if isinstance(dates, dict):
                    for _date, recs in dates.items():
                        if isinstance(recs, list):
                            recordings_meta.extend(recs)

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
                        cleaned = False
                        if not os.path.exists(related_dst):
                            for extension in self.clean_up_extensions:
                                if related_src.endswith(extension):
                                    os.remove(related_src)
                                    cleaned = True
                            if not cleaned:
                                shutil.copy2(related_src, related_dst)
                                logger.debug(f"Imported related file: {related_file}")
                    except Exception as e:
                        logger.warning(f"Failed to import related file {related_file}: {e}")
            # Tag with source='archive' so auto-archive excludes these recordings
            rec_metadata = rec.get('metadata') or {}
            if isinstance(rec_metadata, str):
                try:
                    rec_metadata = json.loads(rec_metadata)
                except Exception:
                    rec_metadata = {}
            rec_metadata['source'] = 'archive'

            recording_data = {
                'id': rec_id,
                'camera_id': rec.get('camera_id', ''),
                'file_path': abs_path,
                'start_time': rec.get('started_at') or rec.get('created_at') or datetime.now().isoformat(),
                'end_time': rec.get('ended_at'),
                'duration': rec.get('duration'),
                'file_size': rec.get('file_size'),
                'status': 'completed',
                'metadata': rec_metadata,
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
        main_basename = os.path.basename(file_path)
        
        # Match "stem.*" and "stem.something.*" (e.g. recording.browser.mp4)
        pattern = os.path.join(glob.escape(file_dir), glob.escape(file_stem) + '.*')
        return [
            os.path.basename(match)
            for match in glob.glob(pattern)
            if os.path.basename(match) != main_basename
        ]

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
