import os
import cv2
import time
import threading
import subprocess
import logging
from typing import Any, Callable, List, Dict, Optional

logger = logging.getLogger(__name__)

class AudioRecordingUtils:
    """Minimal helper kept for hls_manager.py compatibility."""

    @staticmethod
    def _resolve_sample_rate(db_camera: dict) -> int:
        try:
            sample_rate = int(db_camera.get('audio_sample_rate'))
        except (TypeError, ValueError):
            sample_rate = 16000
        return max(8000, min(48000, sample_rate))

    @staticmethod
    def _resolve_audio_input_args(db_camera: dict) -> Optional[List[str]]:
        camera_source = str(db_camera.get('source') or '').strip()
        camera_type = str(db_camera.get('camera_type') or '').strip().lower()

        if camera_type in {'rtsp', 'ip_camera'}:
            if not camera_source:
                logger.warning('RTSP/IP camera source is empty; cannot start audio input.')
                return None

            if camera_source.startswith('rtsp://'):
                return [
                    '-fflags', 'nobuffer',
                    '-flags', 'low_delay',
                    '-analyzeduration', '0',
                    '-probesize', '32768',
                    '-reorder_queue_size', '0',
                    '-avioflags', 'direct',
                    '-max_delay', '500000',
                    '-rtsp_transport', 'tcp',
                    '-i', camera_source,
                ]

            if camera_source.startswith(('http://', 'https://')):
                return ['-i', camera_source]

            return ['-i', camera_source]

        input_format = str(
            db_camera.get('audio_input_format')
            or os.getenv('AUDIO_INPUT_FORMAT', 'pulse')
        ).strip().lower()
        input_source = str(
            db_camera.get('audio_source')
            or os.getenv('AUDIO_SOURCE', 'default')
        ).strip()

        if input_format not in {'pulse', 'alsa'}:
            logger.warning(f"Unsupported audio_input_format '{input_format}'. Use 'pulse' or 'alsa'.")
            return None

        if input_format == 'alsa' and input_source.lower() == 'default':
            input_source = os.getenv('AUDIO_SOURCE_ALSA', 'hw:1,0').strip() or 'hw:1,0'

        if not input_source:
            logger.warning('audio_source is empty; cannot start audio recording.')
            return None

        return ['-f', input_format, '-i', input_source]


class HLSManager:
    def __init__(
        self,
        get_recordings_dir: Callable[[], str],
        get_camera_config: Callable[[str], Optional[dict]],
        ensure_background_stream: Callable[[str], None],
        streaming_service: Any,  # Direct reference to streaming service for _get_spmc_data calls
        register_consumer: Callable[[str, str, list], bool] = None,  # (camera_id, consumer_id, data_types) -> success
    ):
        self._get_recordings_dir = get_recordings_dir
        self._get_camera_config = get_camera_config
        self._ensure_background_stream = ensure_background_stream
        self._streaming_service = streaming_service
        self._register_consumer = register_consumer

        self.active_streams: Dict[str, Dict[str, Any]] = {}
        self.last_errors: Dict[str, str] = {}
        self._pipe_threads: Dict[str, threading.Thread] = {}
        self._pipe_stop_events: Dict[str, threading.Event] = {}
        self._audio_pipes: Dict[str, Any] = {}  # Audio pipes for shared chunks
        
        # Consumer ID for SPMC access
        self._consumer_id_base = "hls_manager"

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

    def _build_hls_command(self, db_camera: dict, manifest_path: str, segment_pattern: str, use_shared_audio: bool = True) -> list[str]:
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
            if use_shared_audio and self._get_latest_audio_chunk_spmc:
                # Use shared audio chunks from background thread
                sample_rate = AudioRecordingUtils._resolve_sample_rate(db_camera)
                command += [
                    '-f', 's16le',
                    '-ar', str(sample_rate),
                    '-ac', '1',
                    '-i', 'pipe:3',  # Audio from shared chunks
                ]
                has_hls_audio = True
            else:
                # Fallback to separate audio capture
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
                '-af', 'volume=3.0',  # Volume amplification for consistency with recordings
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
        audio_enabled = bool(db_camera.get('audio_enabled', False))
        
        # Get audio pipe if audio is enabled
        audio_pipe = self._audio_pipes.get(camera_id) if audio_enabled else None

        def _writer():
            consumer_id = f"{self._consumer_id_base}_{camera_id}"
            next_tick = time.time()
            
            while not stop_event.is_set():
                if process.poll() is not None or process.stdin is None:
                    break

                # Write video frame using SPMC - direct call to _get_spmc_data
                frame = self._streaming_service._get_spmc_data(camera_id, f"{self._consumer_id_base}_{camera_id}", 'overlay')
                if frame is None:
                    time.sleep(min(0.02, frame_interval))
                    continue

                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(frame, (width, height))

                try:
                    process.stdin.write(frame.tobytes())
                except Exception:
                    break

                # Write audio chunk if available and enabled using SPMC
                if audio_enabled and audio_pipe and hasattr(self._streaming_service, '_get_spmc_data'):
                    try:
                        audio_chunk = self._streaming_service._get_spmc_data(camera_id, f"{self._consumer_id_base}_{camera_id}", 'audio')
                        if audio_chunk and len(audio_chunk) > 0:
                            audio_pipe.write(audio_chunk)
                    except Exception as e:
                                logger.debug(f"Audio pipe write failed for {camera_id}: {e}")

                next_tick += frame_interval
                sleep_for = next_tick - time.time()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_tick = time.time()
            
            # Cleanup pipes
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:
                pass
                
            try:
                if audio_pipe:
                    audio_pipe.close()
            except Exception:
                pass
                
            self._pipe_threads.pop(camera_id, None)
            self._audio_pipes.pop(camera_id, None)

        thread = threading.Thread(target=_writer, daemon=True, name=f'hls-pipe-writer-{camera_id}')
        self._pipe_threads[camera_id] = thread
        thread.start()
        return stop_event, thread

    def start_stream(self, camera_id: str) -> str:
        db_camera = self._get_camera_config(camera_id)
        if not db_camera:
            raise ValueError(f'Camera not found: {camera_id}')

        # Register as consumer for this camera
        consumer_id = f"{self._consumer_id_base}_{camera_id}"
        if self._register_consumer:
            data_types = ['overlay']  # Use overlay frames for HLS (includes FPS/overlays)
            if db_camera.get('audio_enabled', False) and hasattr(self._streaming_service, '_get_spmc_data'):
                data_types.append('audio')
            self._register_consumer(camera_id, consumer_id, data_types)

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

        # Set up audio pipe first if audio is enabled and shared audio is available
        audio_pipe = None
        pass_fds = ()
        preexec_fn = None
        use_shared_audio = False
        
        # Temporarily disable shared audio to debug the 404 issue
        # if bool(db_camera.get('audio_enabled', False)) and self._get_latest_audio_chunk_spmc:
        if False:  # Temporarily disabled for debugging
            try:
                audio_r_fd, audio_w_fd = os.pipe()
                audio_pipe = os.fdopen(audio_w_fd, 'wb', buffering=0)
                self._audio_pipes[camera_id] = audio_pipe
                
                def _audio_preexec():
                    os.dup2(audio_r_fd, 3)
                    if audio_r_fd != 3:
                        os.close(audio_r_fd)
                
                preexec_fn = _audio_preexec
                pass_fds = (audio_r_fd,)
                use_shared_audio = True
                logger.info(f"Set up shared audio pipe for HLS stream: {camera_id}")
            except Exception as e:
                logger.warning(f"Failed to setup audio pipe for {camera_id}, falling back to separate capture: {e}")
                use_shared_audio = False

        logger.info(f"Building HLS command for {camera_id} with audio_enabled={bool(db_camera.get('audio_enabled', False))}, use_shared_audio={use_shared_audio}")
        command = self._build_hls_command(db_camera, manifest_path, segment_pattern, use_shared_audio)
        self._ensure_background_stream(camera_id)
        
        logger.info(f"Starting FFmpeg process for {camera_id} with command: {' '.join(command)}")
        
        log_handle = open(ffmpeg_log_path, 'ab')
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_handle,
            bufsize=0,
            pass_fds=pass_fds,
            preexec_fn=preexec_fn,
        )
        
        logger.info(f"FFmpeg process started for {camera_id}, PID: {process.pid}")
        
        # Check if process died immediately
        time.sleep(0.1)
        if process.poll() is not None:
            logger.error(f"FFmpeg process died immediately for {camera_id}, return code: {process.poll()}")
        
        # Close the read end of audio pipe in parent process
        if pass_fds:
            try:
                os.close(pass_fds[0])  # Close audio_r_fd in parent
            except Exception:
                pass
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
                logger.info(f'Started HLS stream for camera {camera_id}: {manifest_path}')
                return manifest_path
            if process.poll() is not None:
                logger.error(f'FFmpeg process died early for camera {camera_id}')
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
            error_message = f'{error_message}. FFmpeg stderr: {stderr_tail}'
        
        # Add command for debugging
        command_str = ' '.join(command) if command else 'No command'
        logger.error(f'HLS start failed for {camera_id}. Command: {command_str}')
        error_message = f'{error_message}. Command: {command_str}'

        self.last_errors[camera_id] = error_message
        raise RuntimeError(error_message)

    def stop_stream(self, camera_id: str, cleanup: bool = True):
        state = self.active_streams.pop(camera_id, None)
        if not state:
            return

        stop_event = state.get('pipe_stop_event') or self._pipe_stop_events.pop(camera_id, None)
        if stop_event is not None:
            stop_event.set()

        # Clean up audio pipe
        audio_pipe = self._audio_pipes.pop(camera_id, None)
        if audio_pipe:
            try:
                audio_pipe.close()
            except Exception:
                pass

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
            
        # Clean up sequence tracking
        self._last_video_seq.pop(camera_id, None)
        self._last_audio_seq.pop(camera_id, None)

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
