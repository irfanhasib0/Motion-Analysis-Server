
"""
AV Writer Module - Flexible audio/video recording

WRITER CONFIGURATION:
- AVWriter = AVWriterV2 (dual mode support)

MODES for AVWriterV2:
- mux_realtime=False: Record separate video + audio files (default, for testing)
- mux_realtime=True: Real-time FFmpeg muxing (single MP4 output)
"""

import os
import subprocess
import logging
import threading
import time
import struct
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class AVWriterV2:
    """Flexible AV writer - can do separate files or real-time muxing. Compatible interface with AVWriterV3."""
    
    def __init__(
        self,
        path: str,
        fps: float,
        width: int,
        height: int,
        camera_service=None,
        camera_id: str = "",
        camera_config: dict = None,
        mux_realtime: bool = False  # False = separate files, True = real-time ffmpeg muxing
    ) -> None:
        self.path = path
        self.fps = fps
        self.width = width
        self.height = height
        self.camera_service = camera_service
        self.camera_id = camera_id
        self.camera_config = camera_config or {}
        self.mux_realtime = mux_realtime
        self._closed = False
        
        # Audio recording setup
        self.audio_recording = False
        self.audio_file = None
        self.audio_file_path = None
        self.audio_bytes_written = 0
        
        # Frame rate control
        self.target_interval = 1.0 / float(fps)
        self.next_write_at = time.time()
        
        # Thread pool for async write operations
        self._write_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"write_{camera_id}")
        
        # Create directory if needed
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        
        if mux_realtime:
            # Use real-time FFmpeg muxing (like original AVFileWriterV2)
            self._init_realtime_muxing()
        else:
            # Use separate video/audio files (like AVWriterV3)
            self._init_separate_files()
    
    def _init_realtime_muxing(self):
        audio_enabled = self.camera_config.get('audio_enabled', False)

        cmd = [
            'ffmpeg', '-y',
            '-hide_banner',
            '-loglevel', 'warning',
            '-f', 'rawvideo',
            '-pix_fmt', 'bgr24',
            '-s', f'{self.width}x{self.height}',
            '-r', str(self.fps),
            '-i', 'pipe:0',
        ]

        self._audio_pipe = None
        audio_r_fd = None
        pass_fds = ()

        if audio_enabled:
            sample_rate = int(self.camera_config.get('audio_sample_rate', 16000))
            channels = int(self.camera_config.get('audio_channels', 1))

            audio_r_fd, audio_w_fd = os.pipe()
            os.set_inheritable(audio_r_fd, True)

            self._audio_pipe = os.fdopen(audio_w_fd, 'wb', buffering=0)

            cmd += [
                '-thread_queue_size', '512',
                '-f', 's16le',
                '-ar', str(sample_rate),
                '-ac', str(channels),
                '-i', f'pipe:{audio_r_fd}',
            ]

            pass_fds = (audio_r_fd,)

        cmd += [
            '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
            '-c:v', 'libx264',
            '-crf', '26',
            '-preset', 'ultrafast',
            '-pix_fmt', 'yuv420p',
        ]

        if audio_enabled:
            cmd += [
                '-c:a', 'aac',
                '-b:a', '128k',
                '-af', 'apad',
            ]

        cmd += ['-movflags', '+faststart', self.path]

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
        )

        if audio_r_fd is not None:
            os.close(audio_r_fd)

        time.sleep(0.1)
        if self._proc.poll() is not None:
            stderr_data = self._proc.stderr.read() if self._proc.stderr else b''
            raise RuntimeError(f'FFmpeg failed to start: {stderr_data.decode(errors="replace")[:1000]}')
        
    def _init_separate_files(self):
        """Initialize separate video/audio files mode."""
        # Initialize video writer
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        self.video_writer = cv2.VideoWriter(self.path, fourcc, self.fps, (self.width, self.height))
        
        if not self.video_writer.isOpened():
            raise RuntimeError(f"Failed to initialize video writer for {self.path}")
        
        # Initialize audio recording if enabled
        if self.camera_service and self.camera_id and self.camera_config.get('audio_enabled', False):
            recording_id = os.path.splitext(os.path.basename(self.path))[0]
            audio_dir = os.path.join(os.path.dirname(self.path))
            audio_filename = f"{recording_id}_audio.wav"
            self.audio_file_path = os.path.join(audio_dir, audio_filename)
            
            try:
                self.audio_file = open(self.audio_file_path, 'wb')
                self.audio_recording = True
                # Write WAV header
                self._write_wav_header()
                self.audio_bytes_written = 0
            except Exception as e:
                logger.error(f"Failed to initialize separate audio file: {e}")
                self.audio_file_path = None
                self.audio_recording = False
        
        # For real-time muxing compatibility
        self._proc = None
        self._audio_pipe = None
    
    def is_opened(self) -> bool:
        """Return True if writer is open."""
        if self.mux_realtime:
            return self._proc is not None and self._proc.poll() is None
        else:
            return not self._closed and self.video_writer.isOpened()
    
    def write(self, frame: np.ndarray) -> bool:
        """Write video frame (compatibility with cv2.VideoWriter interface)."""
        return self.write_video(frame)
        
    def write_video(self, frame: np.ndarray) -> bool:
        """Write one frame to the video."""
        if self._closed:
            return False
        
        if self.mux_realtime:
            # Real-time muxing mode
            if self._proc is None or self._proc.stdin is None:
                return False
            try:
                frame = np.ascontiguousarray(frame, dtype=np.uint8)
                expected = self.height * self.width * 3
                data = frame.tobytes()
                if len(data) != expected:
                    return False
                self._proc.stdin.write(data)
                return True
            except Exception as e:
                logger.error(f"Error writing video frame to pipe: {e}")
                return False
        else:
            # Separate files mode
            try:
                return self.video_writer.write(frame)
            except Exception as e:
                logger.error(f"Error writing video frame: {e}")
                return False
    
    def write_audio(self, audio_chunk: bytes = None) -> bool:
        """Write audio chunk."""
        if self._closed:
            return False
            
        if self.mux_realtime:
            # Real-time muxing mode
            if not self.audio_recording or self._audio_pipe is None or not audio_chunk:
                return False
            try:
                self._audio_pipe.write(audio_chunk)
                return True
            except Exception as e:
                logger.error(f"Error writing audio chunk to pipe: {e}")
                return False
        else:
            # Separate files mode
            if not self.audio_recording or not self.audio_file or not audio_chunk:
                return False
            try:
                self.audio_file.write(audio_chunk)
                self.audio_file.flush()
                self.audio_bytes_written += len(audio_chunk)
                return True
            except Exception as e:
                logger.error(f"Error writing audio chunk: {e}")
                return False
    
    def write_frame_with_timeout(self, frame: np.ndarray, write_timeout: float = 2.0) -> bool:
        """Write frame with frame rate control using ThreadPoolExecutor."""
        
        # Use ThreadPoolExecutor for async write
        future = self._write_executor.submit(self.write_video, frame)
        
        write_success = True
        try:
            # Wait for write operation to complete with timeout
            success = future.result(timeout=write_timeout)
            write_success = success
        except FutureTimeoutError:
            # Timeout occurred - cancel the future if it hasn't started yet
            cancelled = future.cancel()
            if cancelled:
                logger.warning(f"Frame write cancelled after {write_timeout}s timeout for camera {self.camera_id}")
            else:
                logger.warning(f"Frame write timeout after {write_timeout}s for camera {self.camera_id} - task already running")
            write_success = False
        except Exception as e:
            logger.error(f"Frame write error for camera {self.camera_id}: {e}")
            write_success = False
        
        # Handle frame rate timing
        self.next_write_at += self.target_interval
        sleep_for = self.next_write_at - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            self.next_write_at = time.time()
            
        return write_success
    
    def write_video_ffmpeg(self, frame: np.ndarray) -> bool:
        """Alternative FFmpeg-based video writing method.
        
        Creates a separate FFmpeg process for video encoding if not already initialized.
        Useful when OpenCV VideoWriter has compatibility issues or different codec requirements.
        """
        if self._closed:
            return False
            
        # Initialize FFmpeg video process if not already done
        if not hasattr(self, '_ffmpeg_video_proc') or self._ffmpeg_video_proc is None:
            self._init_ffmpeg_video_writer()
        
        if self._ffmpeg_video_proc is None or self._ffmpeg_video_proc.stdin is None:
            return False
            
        try:
            # Ensure frame is contiguous and correct format
            frame = np.ascontiguousarray(frame, dtype=np.uint8)
            expected_size = self.height * self.width * 3
            frame_data = frame.tobytes()
            
            if len(frame_data) != expected_size:
                logger.error(f"Frame size mismatch: expected {expected_size}, got {len(frame_data)}")
                return False
                
            self._ffmpeg_video_proc.stdin.write(frame_data)
            self._ffmpeg_video_proc.stdin.flush()
            return True
            
        except Exception as e:
            logger.error(f"Error writing video frame via FFmpeg: {e}")
            return False
    
    def write_audio_ffmpeg(self, audio_chunk: bytes) -> bool:
        """Alternative FFmpeg-based audio writing method.
        
        Creates a separate FFmpeg process for audio encoding if not already initialized.
        Useful for advanced audio processing or codec requirements.
        """
        if self._closed or not audio_chunk:
            return False
            
        # Initialize FFmpeg audio process if not already done
        if not hasattr(self, '_ffmpeg_audio_proc') or self._ffmpeg_audio_proc is None:
            self._init_ffmpeg_audio_writer()
        
        if self._ffmpeg_audio_proc is None or self._ffmpeg_audio_proc.stdin is None:
            return False
            
        try:
            self._ffmpeg_audio_proc.stdin.write(audio_chunk)
            self._ffmpeg_audio_proc.stdin.flush()
            return True
            
        except Exception as e:
            logger.error(f"Error writing audio chunk via FFmpeg: {e}")
            return False
    
    def get_ffmpeg_output_paths(self) -> dict:
        """Get the output file paths that would be used by FFmpeg methods.
        
        Returns:
            dict: Dictionary with 'ffmpeg_video' and 'ffmpeg_audio' paths
        """
        video_path = self.path.replace('.mp4', '_ffmpeg_video.mp4')
        audio_path = self.path.replace('.mp4', '_ffmpeg_audio.wav')
        
        return {
            'ffmpeg_video': video_path,
            'ffmpeg_audio': audio_path
        }
    
    def release(self) -> str:
        """Release writer and return final file path."""
        if self._closed:
            return self.path
        
        self._closed = True
        
        # Shutdown write thread pool
        if hasattr(self, '_write_executor'):
            try:
                self._write_executor.shutdown(wait=True)
                logger.debug(f"Thread pool shutdown complete for camera {self.camera_id}")
            except Exception as e:
                logger.warning(f"Error shutting down thread pool for camera {self.camera_id}: {e}")
        
        # Cleanup FFmpeg processes (both modes)
        self._cleanup_ffmpeg_processes()
        
        if self.mux_realtime:
            # Real-time muxing mode cleanup
            if self._proc is not None and self._proc.stdin is not None:
                self._proc.stdin.close()
                
            if self._audio_pipe is not None:
                self._audio_pipe.close()

            if self._proc is not None:
                try:
                    self._proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
            
            return self.path
        else:
            # Separate files mode cleanup
            final_path = self.path
            
            # Stop audio recording
            if self.audio_recording:
                self.audio_recording = False
            
            # Finalize WAV header before closing audio file
            if self.audio_file:
                self._finalize_wav_file()  # 🔧 FIX: Update WAV header with actual file sizes
                self.audio_file.close()
                
            # Release video writer
            if self.video_writer:
                self.video_writer.release()
                
            # NOTE: No muxing in this mode - return video file path
            # Audio file stays separate for testing
            return final_path
    
    def _cleanup_ffmpeg_processes(self):
        """Clean up FFmpeg video and audio processes."""
        # Clean up FFmpeg video process
        if hasattr(self, '_ffmpeg_video_proc') and self._ffmpeg_video_proc is not None:
            try:
                if self._ffmpeg_video_proc.stdin is not None:
                    self._ffmpeg_video_proc.stdin.close()
                
                try:
                    self._ffmpeg_video_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning("FFmpeg video process timeout, killing...")
                    self._ffmpeg_video_proc.kill()
                    self._ffmpeg_video_proc.wait()
                    
                if hasattr(self, '_ffmpeg_video_path'):
                    logger.info(f"FFmpeg video file written: {self._ffmpeg_video_path}")
                    
            except Exception as e:
                logger.error(f"Error cleaning up FFmpeg video process: {e}")
            finally:
                self._ffmpeg_video_proc = None
        
        # Clean up FFmpeg audio process  
        if hasattr(self, '_ffmpeg_audio_proc') and self._ffmpeg_audio_proc is not None:
            try:
                if self._ffmpeg_audio_proc.stdin is not None:
                    self._ffmpeg_audio_proc.stdin.close()
                
                try:
                    self._ffmpeg_audio_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning("FFmpeg audio process timeout, killing...")
                    self._ffmpeg_audio_proc.kill()
                    self._ffmpeg_audio_proc.wait()
                    
                if hasattr(self, '_ffmpeg_audio_path'):
                    logger.info(f"FFmpeg audio file written: {self._ffmpeg_audio_path}")
                    
            except Exception as e:
                logger.error(f"Error cleaning up FFmpeg audio process: {e}")
            finally:
                self._ffmpeg_audio_proc = None

    def _write_wav_header(self):
        """Write initial WAV header (will be updated in _finalize_wav_file)."""
        if not self.audio_file:
            return
        
        # Default audio parameters (16-bit, 16kHz, mono)
        sample_rate = int(self.camera_config.get('audio_sample_rate', 16000))
        num_channels = int(self.camera_config.get('audio_channels', 1))
        bits_per_sample = 16
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        
        # Write header with placeholder sizes (we'll update them later)
        header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', 0,            # RIFF chunk size (will be updated)
            b'WAVE',
            b'fmt ', 16,           # fmt sub-chunk size
            1,                     # PCM audio format
            num_channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b'data', 0,            # data sub-chunk size (will be updated)
        )
        self.audio_file.write(header)
        self.audio_file.flush()
    
    def _finalize_wav_file(self):
        """Update WAV header with actual file sizes."""
        if not self.audio_file or not hasattr(self, 'audio_bytes_written'):
            logger.warning("Cannot finalize WAV file: no audio file or bytes counter")
            return
        
        if self.audio_bytes_written == 0:
            logger.warning(f"No audio data written to {self.audio_file_path}")
            return
        
        # Calculate final sizes
        data_size = self.audio_bytes_written
        riff_size = data_size + 36  # 44 byte header - 8 byte RIFF header = 36
        
        
        # Update RIFF chunk size (bytes 4-7)
        self.audio_file.seek(4)
        self.audio_file.write(struct.pack('<I', riff_size))
        
        # Update data chunk size (bytes 40-43)
        self.audio_file.seek(40)
        self.audio_file.write(struct.pack('<I', data_size))
        
        # Return to end of file
        self.audio_file.seek(0, 2)
        self.audio_file.flush()
    
    def _init_ffmpeg_video_writer(self):
        """Initialize FFmpeg process for video writing."""
        try:
            # Generate video output path with _ffmpeg suffix
            video_path = self.path.replace('.mp4', '_ffmpeg_video.mp4')
            
            cmd = [
                'ffmpeg', '-y',
                '-hide_banner',
                '-loglevel', 'warning',
                '-f', 'rawvideo',
                '-pix_fmt', 'bgr24',
                '-s', f'{self.width}x{self.height}',
                '-r', str(self.fps),
                '-i', 'pipe:0',
                '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
                '-c:v', 'libx264',
                '-crf', '23',
                '-preset', 'medium',
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                video_path
            ]
            
            self._ffmpeg_video_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            self._ffmpeg_video_path = video_path
            
            # Small delay to ensure process starts properly
            time.sleep(0.1)
            if self._ffmpeg_video_proc.poll() is not None:
                stderr_data = self._ffmpeg_video_proc.stderr.read() if self._ffmpeg_video_proc.stderr else b''
                raise RuntimeError(f'FFmpeg video process failed to start: {stderr_data.decode(errors="replace")[:500]}')
                
            logger.info(f"Initialized FFmpeg video writer: {video_path}")
            
        except Exception as e:
            logger.error(f"Failed to initialize FFmpeg video writer: {e}")
            self._ffmpeg_video_proc = None
    
    def _init_ffmpeg_audio_writer(self):
        """Initialize FFmpeg process for audio writing."""
        try:
            # Get audio parameters from camera config
            sample_rate = int(self.camera_config.get('audio_sample_rate', 16000))
            channels = int(self.camera_config.get('audio_channels', 1))
            
            # Generate audio output path with _ffmpeg suffix
            audio_path = self.path.replace('.mp4', '_ffmpeg_audio.wav')
            
            cmd = [
                'ffmpeg', '-y',
                '-hide_banner',
                '-loglevel', 'warning',
                '-f', 's16le',
                '-ar', str(sample_rate),
                '-ac', str(channels),
                '-i', 'pipe:0',
                '-c:a', 'pcm_s16le',
                '-af', 'volume=1.0',
                audio_path
            ]
            
            self._ffmpeg_audio_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            self._ffmpeg_audio_path = audio_path
            
            # Small delay to ensure process starts properly
            time.sleep(0.1)
            if self._ffmpeg_audio_proc.poll() is not None:
                stderr_data = self._ffmpeg_audio_proc.stderr.read() if self._ffmpeg_audio_proc.stderr else b''
                raise RuntimeError(f'FFmpeg audio process failed to start: {stderr_data.decode(errors="replace")[:500]}')
                
            logger.info(f"Initialized FFmpeg audio writer: {audio_path}")
            
        except Exception as e:
            logger.error(f"Failed to initialize FFmpeg audio writer: {e}")
            self._ffmpeg_audio_proc = None
        
    def cancel(self) -> None:
        """Cancel recording and clean up files."""
        if self._closed:
            return
            
        self._closed = True
        
        # Shutdown write thread pool immediately
        if hasattr(self, '_write_executor'):
            try:
                self._write_executor.shutdown(wait=False)  # Don't wait for in-progress tasks
                logger.debug(f"Thread pool cancelled for camera {self.camera_id}")
            except Exception as e:
                logger.warning(f"Error cancelling thread pool for camera {self.camera_id}: {e}")
        
        # Cleanup FFmpeg processes (both modes) and remove their output files
        self._cancel_ffmpeg_processes()
        
        if self.mux_realtime:
            # Real-time muxing mode cleanup
            if self._proc is not None:
                self._proc.kill()
                self._proc.wait()
            if self._audio_pipe is not None:
                self._audio_pipe.close()
        else:
            # Separate files mode cleanup
            if self.audio_recording:
                self.audio_recording = False
            
            if self.audio_file:
                self.audio_file.close()
                    
            if self.audio_file_path and os.path.exists(self.audio_file_path):
                try:
                    os.remove(self.audio_file_path)
                except Exception:
                    pass
            
            if self.video_writer:
                try:
                    self.video_writer.release()
                except Exception:
                    pass
        
        # Remove output file
        if os.path.exists(self.path):
            try:
                os.remove(self.path)
            except Exception:
                pass
    
    def _cancel_ffmpeg_processes(self):
        """Clean up FFmpeg processes and remove their output files during cancellation."""
        # Kill and clean up FFmpeg video process
        if hasattr(self, '_ffmpeg_video_proc') and self._ffmpeg_video_proc is not None:
            try:
                self._ffmpeg_video_proc.kill()
                self._ffmpeg_video_proc.wait()
                
                # Remove video output file
                if hasattr(self, '_ffmpeg_video_path') and os.path.exists(self._ffmpeg_video_path):
                    try:
                        os.remove(self._ffmpeg_video_path)
                        logger.info(f"Removed cancelled FFmpeg video file: {self._ffmpeg_video_path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove FFmpeg video file: {e}")
                        
            except Exception as e:
                logger.error(f"Error cancelling FFmpeg video process: {e}")
            finally:
                self._ffmpeg_video_proc = None
        
        # Kill and clean up FFmpeg audio process
        if hasattr(self, '_ffmpeg_audio_proc') and self._ffmpeg_audio_proc is not None:
            try:
                self._ffmpeg_audio_proc.kill()
                self._ffmpeg_audio_proc.wait()
                
                # Remove audio output file
                if hasattr(self, '_ffmpeg_audio_path') and os.path.exists(self._ffmpeg_audio_path):
                    try:
                        os.remove(self._ffmpeg_audio_path)
                        logger.info(f"Removed cancelled FFmpeg audio file: {self._ffmpeg_audio_path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove FFmpeg audio file: {e}")
                        
            except Exception as e:
                logger.error(f"Error cancelling FFmpeg audio process: {e}")
            finally:
                self._ffmpeg_audio_proc = None


class AVFileWriterV1:
    """Write video and audio to proper files (e.g., .mp4, .wav), then mux into a final MP4 file."""
    def __init__(
        self,
        path: str,
        fps: float,
        width: int,
        height: int,
        audio_cap: Optional[Any] = None,
        video_codec: str = 'libx264',
        crf: int = 26,
    ) -> None:
        self.path = path
        self.fps = fps
        self.width = width
        self.height = height
        self._audio_cap = audio_cap
        self._closed = False
        self._video_path = path + '.video.mp4'
        self._audio_path = path + '.audio.wav' if audio_cap is not None else None
        # Open ffmpeg process for video
        self._video_proc = subprocess.Popen([
            'ffmpeg', '-y',
            '-hide_banner', '-loglevel', 'error',
            '-f', 'rawvideo', '-pix_fmt', 'bgr24',
            '-s', f'{width}x{height}',
            '-r', str(fps),
            '-i', 'pipe:0',
            '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
            '-c:v', video_codec,
            '-crf', str(crf),
            '-preset', 'ultrafast',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',  # Add faststart for better browser compatibility
            self._video_path
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        # Open ffmpeg process for audio (if needed)
        if audio_cap is not None:
            sample_rate = getattr(audio_cap, 'audio_sample_rate', 16000)
            channels = getattr(audio_cap, 'audio_channels', 1)
            self._audio_proc = subprocess.Popen([
                'ffmpeg', '-y',
                '-hide_banner', '-loglevel', 'error',
                '-f', 's16le',
                '-ar', str(sample_rate),
                '-ac', str(channels),
                '-i', 'pipe:0',
                self._audio_path
            ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        else:
            self._audio_proc = None
        self._audio_lock = threading.Lock()
        self._audio_stop = threading.Event()
        self._audio_thread: Optional[threading.Thread] = None

    def is_opened(self) -> bool:
        return not self._closed

    def write_audio(self, chunk: bytes) -> bool:
        if self._closed or self._audio_proc is None or self._audio_proc.stdin is None:
            return False
        if chunk is None or len(chunk) == 0:
            return False
        try:
            self._audio_proc.stdin.write(chunk)
            return True
        except Exception as e:
            logger.error(f"Error writing audio chunk: {e}")
            return False

    def write_video(self, frame: np.ndarray) -> bool:
        if self._closed or self._video_proc is None or self._video_proc.stdin is None:
            return False
        try:
            self._video_proc.stdin.write(
                np.ascontiguousarray(frame, dtype=np.uint8).tobytes()
            )
            return True
        except Exception as e:
            logger.error(f"Error writing video frame: {e}")
            return False

    def release(self) -> bool:
        if self._closed:
            return True
        self._closed = True
        # Close video and audio ffmpeg processes
        if self._video_proc and self._video_proc.stdin:
            self._video_proc.stdin.close()
            self._video_proc.wait()
        if self._audio_proc and self._audio_proc.stdin:
            self._audio_proc.stdin.close()
            self._audio_proc.wait()
        # Mux video and audio files into final MP4
        cmd = [
            'ffmpeg', '-y',
            '-hide_banner', '-loglevel', 'error',
            '-i', self._video_path,
        ]
        if self._audio_path is not None:
            cmd += ['-i', self._audio_path]
        cmd += [
            '-c:v', 'copy',
        ]
        if self._audio_path is not None:
            cmd += ['-c:a', 'aac', '-b:a', '128k']
        cmd += ['-movflags', '+faststart', self.path]
        subprocess.run(cmd, check=True)
        # Clean up temp files
        '''
        try:
            if os.path.exists(self._video_path):
                os.remove(self._video_path)
            if self._audio_path is not None and os.path.exists(self._audio_path):
                os.remove(self._audio_path)
        except Exception:
            pass
        '''
        ok = os.path.exists(self.path) and os.path.getsize(self.path) > 0
        if not ok:
            logger.warning(f'AVFileWriterSeparate: output file empty or missing: {self.path}')
        else:
            logger.info(f'AVFileWriterSeparate: output file created successfully: {self.path}')
        return ok

    def cancel(self) -> None:
        self._closed = True
        if self._video_proc and self._video_proc.stdin:
            try:
                self._video_proc.stdin.close()
            except Exception:
                pass
            self._video_proc.kill()
            self._video_proc.wait()
        if self._audio_proc and self._audio_proc.stdin:
            try:
                self._audio_proc.stdin.close()
            except Exception:
                pass
            self._audio_proc.kill()
            self._audio_proc.wait()
        try:
            if os.path.exists(self._video_path):
                os.remove(self._video_path)
            if self._audio_path is not None and os.path.exists(self._audio_path):
                os.remove(self._audio_path)
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class AVWriterV3:
    """Combined video writer with integrated audio recording from streaming service."""
    
    def __init__(
        self,
        path: str,
        fps: float,
        width: int,
        height: int,
        camera_service=None,
        camera_id: str = "",
        camera_config: dict = None,
        video_codec: str = 'mp4v'
    ) -> None:
        self.path = path
        self.fps = fps
        self.width = width
        self.height = height
        self.camera_service = camera_service
        self.camera_id = camera_id
        self.camera_config = camera_config or {}
        self._closed = False
        self.audio_file = None
        self.audio_file_path = None
        self.audio_recording_thread = None
        self.audio_recording = False
        
        # Frame rate control
        self.target_interval = 1.0 / float(fps)
        self.next_write_at = time.time()
        
        # Create directory if needed
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        
        # Initialize video writer
        fourcc = cv2.VideoWriter_fourcc(*video_codec)
        self.video_writer = cv2.VideoWriter(path, fourcc, fps, (width, height))
        
        if not self.video_writer.isOpened():
            logger.error(f"Failed to open video writer for {path}")
            raise RuntimeError(f"Failed to initialize video writer for {path}")
            
        # Initialize audio recording from stream if enabled
        if self.camera_service and camera_id and camera_config.get('audio_enabled', False):
            recording_id = os.path.splitext(os.path.basename(path))[0]
            
            # Create audio output directory
            audio_dir = os.path.join(os.path.dirname(path))
            os.makedirs(audio_dir, exist_ok=True)
            
            # Generate audio file path (raw PCM data)
            audio_filename = f"{recording_id}_audio.raw"
            self.audio_file_path = os.path.join(audio_dir, audio_filename)
            
            try:
                # Open raw binary file for writing
                self.audio_file = open(self.audio_file_path, 'wb')
                self.audio_recording = True
                
                logger.info(f"Initialized audio recording for {camera_id}: {self.audio_file_path}")
            except Exception as e:
                logger.error(f"Failed to initialize audio recording for {camera_id}: {e}")
                if self.audio_file:
                    self.audio_file.close()
                    self.audio_file = None
                self.audio_file_path = None
                self.audio_recording = False
        

        
    def is_opened(self) -> bool:
        """Return True if video writer is open."""
        return not self._closed and self.video_writer.isOpened()
    
    def write(self, frame: np.ndarray) -> bool:
        """Write video frame (compatibility with cv2.VideoWriter interface)."""
        return self.write_video(frame)
        
    def write_video(self, frame: np.ndarray) -> bool:
        """Write one frame to the video."""
        if self._closed or not self.video_writer.isOpened():
            return False
        
        try:
            success = self.video_writer.write(frame)
            return success
        except Exception as e:
            logger.error(f"Error writing video frame to {self.path}: {e}")
            return False
    
    def write_audio(self, audio_chunk: bytes = None) -> bool:
        """Write audio chunk to raw file."""
        if self._closed or not self.audio_recording or not self.audio_file:
            return False
            
        if audio_chunk and len(audio_chunk) > 0:
            try:
                self.audio_file.write(audio_chunk)
                self.audio_file.flush()  # Ensure data is written immediately
                return True
            except Exception as e:
                logger.error(f"Error writing audio chunk for {self.camera_id}: {e}")
                return False
        
        return True
    
    def write_frame_with_timing(self, frame: np.ndarray, audio_chunk: bytes = None) -> bool:
        """Write frame with frame rate control using ThreadPoolExecutor."""
        
        # Submit write task to thread pool
        write_timeout = 2.0  # 2 second timeout for frame write
        future = self._write_executor.submit(self.write_video, frame)
        
        write_success = True
        try:
            # Wait for write operation to complete with timeout
            success = future.result(timeout=write_timeout)
            write_success = success
        except FutureTimeoutError:
            # Timeout occurred - cancel the future if it hasn't started yet
            cancelled = future.cancel()
            if cancelled:
                logger.warning(f"Frame write cancelled after {write_timeout}s timeout for camera {self.camera_id}")
            else:
                logger.warning(f"Frame write timeout after {write_timeout}s for camera {self.camera_id} - task already running")
            write_success = False
        except Exception as e:
            logger.error(f"Frame write error for camera {self.camera_id}: {e}")
            write_success = False
        
        # Handle frame rate timing
        self.next_write_at += self.target_interval
        sleep_for = self.next_write_at - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            self.next_write_at = time.time()
            
        return write_success
    
    def release(self) -> str:
        """Release video writer immediately and start async audio muxing if needed."""
        if self._closed:
            return self.path
        
        self._closed = True
        
        # Stop audio recording
        if self.audio_recording:
            self.audio_recording = False
        
        # Close audio file
        if self.audio_file:
            try:
                self.audio_file.close()
                logger.info(f"Closed audio file: {self.audio_file_path}")
            except Exception as e:
                logger.warning(f"Error closing audio file: {e}")
        
        # Release video writer immediately - don't block here
        if self.video_writer:
            try:
                self.video_writer.release()
            except Exception as e:
                logger.warning(f"Error releasing video writer: {e}")
        
        # Return video file immediately to unblock recording loop
        video_only_path = self.path
        
        # Start async audio muxing if audio file exists
        if (self.audio_file_path and os.path.exists(self.audio_file_path) and 
            os.path.getsize(self.audio_file_path) > 0):
            
            # Start mux in background thread to avoid blocking
            mux_thread = threading.Thread(
                target=self._async_mux_audio,
                args=(video_only_path, self.audio_file_path, self.camera_config.get('audio_sample_rate', 16000)),
                daemon=True
            )
            mux_thread.start()
            logger.info(f"Started async audio mux for {video_only_path}")
        
        return video_only_path
    
    def _async_mux_audio(self, video_file_path: str, audio_file_path: str, sample_rate: int):
        """Background thread function to mux audio without blocking recording loop."""
        try:
            # Add delay to allow motion detection to complete before muxing
            time.sleep(0.5)
            
            # Check if files still exist before muxing (might be deleted for no-motion)
            if not os.path.exists(video_file_path):
                logger.info(f"Video file no longer exists (likely no-motion deletion): {video_file_path}")
                # Clean up audio file if video was deleted
                if os.path.exists(audio_file_path):
                    try:
                        os.remove(audio_file_path)
                        logger.info(f"Cleaned up audio file after video deletion: {audio_file_path}")
                    except Exception as e:
                        logger.warning(f"Could not clean up audio file: {e}")
                return
                
            if not os.path.exists(audio_file_path):
                logger.info(f"Audio file no longer exists: {audio_file_path}")
                return
            
            final_path = self.mux_audio_into_video(video_file_path, audio_file_path, sample_rate)
            logger.info(f"Async audio muxing completed: {final_path}")
        except Exception as e:
            logger.error(f"Async audio mux failed for {video_file_path}: {e}")
            # Clean up audio file if mux fails
            if os.path.exists(audio_file_path):
                try:
                    os.remove(audio_file_path)
                    logger.info(f"Cleaned up audio file after mux failure: {audio_file_path}")
                except Exception as cleanup_e:
                    logger.warning(f"Could not clean up audio file: {cleanup_e}")
    
    def mux_audio_into_video(self, video_file_path: str, audio_file_path: str, sample_rate: int = 16000) -> str:
        """
        Combine separate audio and video files into a single MP4.
        
        Args:
            video_file_path: Path to the video file
            audio_file_path: Path to the audio file
            sample_rate: Audio sample rate (default 16000)
            
        Returns:
            Path to the combined audio/video file
        """
        if not os.path.exists(video_file_path):
            logger.warning(f"Video file not found (may have been deleted): {video_file_path}")
            return video_file_path
            
        if not os.path.exists(audio_file_path):
            logger.warning(f"Audio file not found: {audio_file_path}")
            return video_file_path
            
        # Check audio file size
        audio_size = os.path.getsize(audio_file_path)
        logger.info(f"Muxing audio file: {audio_file_path} ({audio_size} bytes) at {sample_rate}Hz")
        
        if audio_size == 0:
            logger.warning(f"Audio file is empty, returning video only: {video_file_path}")
            return video_file_path
            
        try:
            # Generate unique output file path to avoid overwriting input
            base_name = os.path.splitext(video_file_path)[0]
            output_file_path = f"{base_name}_muxed.mp4"
            
            # Ensure output path is different from input
            if output_file_path == video_file_path:
                output_file_path = f"{base_name}_final.mp4"
            
            # Build FFmpeg mux command for raw PCM input
            cmd = [
                'ffmpeg',
                '-i', video_file_path,  # Video input
                '-f', 's16le',          # Raw PCM format (16-bit signed little endian)
                '-ar', str(sample_rate), # Audio sample rate from session
                '-ac', '1',             # Audio channels (mono)
                '-i', audio_file_path,  # Raw audio input
                '-c:v', 'copy',         # Copy video stream (no re-encoding)
                '-c:a', 'aac',          # Encode audio to AAC
                '-b:a', '128k',         # Audio bitrate
                '-af', 'apad,volume=3.0',  # Pad audio to video length + amplify volume
                '-map', '0:v:0',        # Map first video stream
                '-map', '1:a:0',        # Map first audio stream
                '-y',                   # Overwrite output
                output_file_path
            ]
            
            logger.info(f"Running mux command: {' '.join(cmd)}")
            
            # Execute mux command
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60  # 1 minute timeout
            )
            
            if result.returncode == 0 and os.path.exists(output_file_path):
                file_size = os.path.getsize(output_file_path)
                logger.info(f"Audio/video mux successful: {output_file_path} ({file_size} bytes)")
                
                # Clean up original video file
                try:
                    os.remove(video_file_path)
                    logger.info(f"Removed original video file: {video_file_path}")
                except Exception as e:
                    logger.warning(f"Could not remove original video file: {e}")
                    
                # Clean up audio file
                try:
                    os.remove(audio_file_path)
                    logger.info(f"Removed audio file: {audio_file_path}")
                except Exception as e:
                    logger.warning(f"Could not remove audio file: {e}")
                
                # Rename muxed file to original video path for consistency
                try:
                    os.rename(output_file_path, video_file_path)
                    logger.info(f"Renamed muxed file to original path: {video_file_path}")
                    return video_file_path
                except Exception as e:
                    logger.warning(f"Could not rename muxed file, keeping as: {output_file_path}. Error: {e}")
                    return output_file_path
            else:
                stderr_output = result.stderr[-500:] if result.stderr else "No error output"
                logger.error(f"FFmpeg mux failed (returncode={result.returncode}): {stderr_output}")
                logger.error(f"FFmpeg stdout: {result.stdout}")
                return video_file_path  # Return original path on failure
                
        except subprocess.TimeoutExpired:
            logger.error(f"Audio/video mux timeout: {video_file_path}")
            return video_file_path
        except Exception as e:
            logger.error(f"Error during audio/video mux: {e}")
            return video_file_path
    
    def cancel(self) -> None:
        """Cancel recording and clean up files."""
        if self._closed:
            return
            
        self._closed = True
        
        # Stop audio recording
        if self.audio_recording:
            self.audio_recording = False
            if self.audio_recording_thread:
                self.audio_recording_thread.join(timeout=1)
        
        # Close and remove audio file
        if self.audio_file:
            try:
                self.audio_file.close()
            except Exception:
                pass
                
        if self.audio_file_path and os.path.exists(self.audio_file_path):
            try:
                os.remove(self.audio_file_path)
            except Exception as e:
                logger.error(f"Error removing audio file {self.audio_file_path}: {e}")
        
        # Release video writer
        if self.video_writer:
            try:
                self.video_writer.release()
            except Exception:
                pass
        
        # Remove video file
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception as e:
            logger.error(f"Error removing video file {self.path}: {e}")
    
    def __enter__(self):
        return self
    
    def __exit__(self, *_):
        self.release()