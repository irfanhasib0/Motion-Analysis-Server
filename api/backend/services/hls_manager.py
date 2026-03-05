import os
import cv2
import time
import threading
import subprocess
import logging
from typing import Any, Callable, Dict, Optional
from services.audio_utils import AudioRecordingUtils

logger = logging.getLogger(__name__)


class HLSManager:
    def __init__(
        self,
        get_recordings_dir: Callable[[], str],
        get_camera_config: Callable[[str], Optional[dict]],
        ensure_background_stream: Callable[[str], None],
        get_latest_frame: Callable[[str], Optional[Any]],
    ):
        self._get_recordings_dir = get_recordings_dir
        self._get_camera_config = get_camera_config
        self._ensure_background_stream = ensure_background_stream
        self._get_latest_frame = get_latest_frame

        self.active_streams: Dict[str, Dict[str, Any]] = {}
        self.last_errors: Dict[str, str] = {}
        self._pipe_threads: Dict[str, threading.Thread] = {}
        self._pipe_stop_events: Dict[str, threading.Event] = {}

    def _hls_root_dir(self) -> str:
        path = os.path.join(self._get_recordings_dir(), 'hls')
        os.makedirs(path, exist_ok=True)
        return path

    def _hls_output_dir(self, camera_id: str) -> str:
        safe_camera_id = str(camera_id).replace('/', '_').replace('\\', '_')
        path = os.path.join(self._hls_root_dir(), safe_camera_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _clean_hls_output_dir(self, output_dir: str):
        if not os.path.isdir(output_dir):
            return
        for filename in os.listdir(output_dir):
            if filename.endswith(('.m3u8', '.ts', '.m4s', '.tmp')):
                try:
                    os.remove(os.path.join(output_dir, filename))
                except Exception:
                    pass

    @staticmethod
    def _parse_resolution(resolution_value: str) -> tuple[int, int]:
        try:
            width, height = [int(v) for v in str(resolution_value).split('x', 1)]
            return max(2, width), max(2, height)
        except Exception:
            return 640, 480

    def _build_hls_command(self, db_camera: dict, manifest_path: str, segment_pattern: str) -> list[str]:
        fps = max(1, int(db_camera.get('fps', 15) or 15))
        width, height = self._parse_resolution(db_camera.get('resolution', '640x480'))

        command = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'error',
            '-y',
            '-f', 'rawvideo',
            '-pix_fmt', 'bgr24',
            '-s', f'{width}x{height}',
            '-r', str(fps),
            '-i', 'pipe:0',
        ]

        has_hls_audio = False
        if bool(db_camera.get('audio_enabled', False)):
            audio_input_args = AudioRecordingUtils._resolve_audio_input_args(db_camera)
            if audio_input_args:
                command += [
                    '-thread_queue_size', '1024',
                    *audio_input_args,
                ]
                has_hls_audio = True
            else:
                logger.warning('HLS audio requested but audio input could not be resolved; serving video-only HLS')

        command += [
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-profile:v', 'baseline',
            '-level', '3.1',
            '-pix_fmt', 'yuv420p',
            '-g', str(max(2, fps * 2)),
            '-keyint_min', str(max(1, fps)),
            '-sc_threshold', '0',
            '-map', '0:v:0',
        ]

        if has_hls_audio:
            sample_rate = AudioRecordingUtils._resolve_sample_rate(db_camera)
            command += [
                '-map', '1:a:0?',
                '-c:a', 'aac',
                '-b:a', '64k',
                '-ac', '1',
                '-ar', str(sample_rate),
            ]
        else:
            command += ['-an']

        command += [
            '-f', 'hls',
            '-hls_time', '1',
            '-hls_list_size', '6',
            '-hls_flags', 'delete_segments+append_list+independent_segments+omit_endlist',
            '-hls_segment_filename', segment_pattern,
            manifest_path,
        ]

        return command

    def _start_pipe_writer(self, camera_id: str, db_camera: dict, process: subprocess.Popen) -> tuple[threading.Event, threading.Thread]:
        stop_event = threading.Event()
        self._pipe_stop_events[camera_id] = stop_event

        fps = max(1, int(db_camera.get('fps', 15) or 15))
        width, height = self._parse_resolution(db_camera.get('resolution', '640x480'))
        frame_interval = 1.0 / float(fps)

        def _writer():
            next_tick = time.time()
            while not stop_event.is_set():
                if process.poll() is not None or process.stdin is None:
                    break

                frame = self._get_latest_frame(camera_id)
                if frame is None:
                    time.sleep(min(0.02, frame_interval))
                    continue

                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(frame, (width, height))

                try:
                    process.stdin.write(frame.tobytes())
                except Exception:
                    break

                next_tick += frame_interval
                sleep_for = next_tick - time.time()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_tick = time.time()

            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:
                pass
            self._pipe_threads.pop(camera_id, None)

        thread = threading.Thread(target=_writer, daemon=True, name=f'hls-pipe-writer-{camera_id}')
        self._pipe_threads[camera_id] = thread
        thread.start()
        return stop_event, thread

    def start_stream(self, camera_id: str) -> str:
        db_camera = self._get_camera_config(camera_id)
        if not db_camera:
            raise ValueError(f'Camera not found: {camera_id}')

        existing = self.active_streams.get(camera_id)
        if existing:
            process = existing.get('process')
            manifest_path = existing.get('manifest_path')
            if process and process.poll() is None and manifest_path and os.path.exists(manifest_path):
                return manifest_path
            self.stop_stream(camera_id, cleanup=False)

        output_dir = self._hls_output_dir(camera_id)
        self._clean_hls_output_dir(output_dir)
        manifest_path = os.path.join(output_dir, 'index.m3u8')
        segment_pattern = os.path.join(output_dir, 'segment_%05d.ts')
        ffmpeg_log_path = os.path.join(output_dir, 'ffmpeg.log')

        try:
            if os.path.exists(ffmpeg_log_path):
                os.remove(ffmpeg_log_path)
        except Exception:
            pass

        command = self._build_hls_command(db_camera, manifest_path, segment_pattern)
        self._ensure_background_stream(camera_id)

        log_handle = open(ffmpeg_log_path, 'ab')
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_handle,
            bufsize=0,
        )
        stop_event, pipe_thread = self._start_pipe_writer(camera_id, db_camera, process)

        timeout_at = time.time() + 12.0
        while time.time() < timeout_at:
            if os.path.exists(manifest_path) and os.path.getsize(manifest_path) > 0:
                self.active_streams[camera_id] = {
                    'process': process,
                    'manifest_path': manifest_path,
                    'output_dir': output_dir,
                    'ffmpeg_log_path': ffmpeg_log_path,
                    'stderr_handle': log_handle,
                    'pipe_stop_event': stop_event,
                    'pipe_thread': pipe_thread,
                    'command': command,
                    'started_at': time.time(),
                }
                self.last_errors.pop(camera_id, None)
                logger.info(f'Started HLS stream for camera {camera_id}')
                return manifest_path
            if process.poll() is not None:
                break
            time.sleep(0.15)

        try:
            stop_event.set()
            if pipe_thread.is_alive():
                pipe_thread.join(timeout=1.0)
        except Exception:
            pass

        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass

        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

        try:
            log_handle.close()
        except Exception:
            pass

        stderr_tail = ''
        try:
            if os.path.exists(ffmpeg_log_path):
                with open(ffmpeg_log_path, 'rb') as f:
                    stderr_tail = f.read()[-1500:].decode('utf-8', errors='ignore')
        except Exception:
            stderr_tail = ''

        error_message = f'Failed to start HLS stream for camera {camera_id}'
        if stderr_tail:
            error_message = f'{error_message}. ffmpeg tail: {stderr_tail}'

        self.last_errors[camera_id] = error_message
        raise RuntimeError(error_message)

    def stop_stream(self, camera_id: str, cleanup: bool = True):
        state = self.active_streams.pop(camera_id, None)
        if not state:
            return

        stop_event = state.get('pipe_stop_event') or self._pipe_stop_events.pop(camera_id, None)
        if stop_event is not None:
            stop_event.set()

        pipe_thread = state.get('pipe_thread') or self._pipe_threads.pop(camera_id, None)
        if pipe_thread and pipe_thread.is_alive():
            try:
                pipe_thread.join(timeout=1.0)
            except Exception:
                pass

        process = state.get('process')
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:
                pass
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        stderr_handle = state.get('stderr_handle')
        if stderr_handle:
            try:
                stderr_handle.close()
            except Exception:
                pass

        if cleanup:
            self._clean_hls_output_dir(state.get('output_dir', ''))

    def get_stream_status(self, camera_id: str) -> Dict[str, Any]:
        state = self.active_streams.get(camera_id)
        output_dir = self._hls_output_dir(camera_id)
        manifest_path = os.path.join(output_dir, 'index.m3u8')
        ffmpeg_log_path = os.path.join(output_dir, 'ffmpeg.log')

        running = False
        pid = None
        if state and state.get('process') is not None:
            process = state.get('process')
            running = process.poll() is None
            pid = process.pid if running else None

        segment_count = 0
        try:
            segment_count = len([f for f in os.listdir(output_dir) if f.endswith('.ts')])
        except Exception:
            segment_count = 0

        return {
            'camera_id': camera_id,
            'running': running,
            'pid': pid,
            'output_dir': output_dir,
            'manifest_path': manifest_path,
            'manifest_exists': os.path.exists(manifest_path),
            'segment_count': segment_count,
            'pipe_writer_alive': bool(state and state.get('pipe_thread') and state.get('pipe_thread').is_alive()),
            'ffmpeg_log_path': ffmpeg_log_path,
            'last_error': self.last_errors.get(camera_id),
        }

    def get_manifest_path(self, camera_id: str) -> str:
        return self.start_stream(camera_id)

    def get_segment_path(self, camera_id: str, segment_name: str) -> str:
        if '/' in segment_name or '\\' in segment_name:
            raise ValueError('Invalid segment name')
        if not segment_name.endswith(('.ts', '.m4s', '.mp4')):
            raise ValueError('Invalid HLS segment extension')

        manifest_path = self.get_manifest_path(camera_id)
        output_dir = os.path.dirname(manifest_path)
        segment_path = os.path.join(output_dir, segment_name)
        if not os.path.exists(segment_path):
            raise ValueError(f'HLS segment not found: {segment_name}')
        return segment_path
