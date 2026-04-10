import logging
import os
import subprocess
import tempfile
import time
import threading
from typing import Dict, Optional


logger = logging.getLogger(__name__)


class AudioRecordingUtils:
    """Utility class for handling audio recording, capture, and muxing operations."""
    
    def __init__(self, recordings_dir: str, camera_service=None):
        self.recordings_dir = recordings_dir
        self.camera_service = camera_service
        self.active_sessions: Dict[str, dict] = {}
        
    def set_camera_service(self, camera_service):
        """Set the camera service reference (for dependency injection)."""
        self.camera_service = camera_service
        
    def start_audio_capture(self, camera_id: str, recording_id: str, camera_config: dict) -> Optional[dict]:
        """
        Start audio capture for a camera recording session using existing camera service.
        
        Args:
            camera_id: Camera identifier
            recording_id: Recording session identifier  
            camera_config: Camera configuration containing audio settings
            
        Returns:
            Audio session dictionary or None if audio not enabled
        """
        # Check if audio is enabled for this camera
        if not camera_config.get('audio_enabled', False):
            logger.debug(f"Audio not enabled for camera {camera_id}")
            return None
            
        if not self.camera_service:
            logger.warning(f"No camera service available for audio capture: {camera_id}")
            return None
            
        try:
            # Check if audio stream exists and is open
            audio_cap = self.camera_service._audio_streams.get(camera_id)
            
            # Always try to start/restart audio if not available
            if audio_cap is None or not audio_cap.is_audio_stream_opened():
                logger.info(f"Audio stream not available, starting audio for recording: {camera_id}")
                success = self.camera_service.start_audio(camera_id)
                if not success:
                    logger.error(f"Failed to start audio stream for camera {camera_id}")
                    return None
                audio_cap = self.camera_service._audio_streams.get(camera_id)
            
            # Double-check audio stream is now available
            if not audio_cap or not audio_cap.is_audio_stream_opened():
                logger.error(f"Audio capture still not available for camera {camera_id}")
                return None
                
            logger.info(f"Using audio stream for recording: {camera_id}")
                
            # Create audio output directory
            audio_dir = os.path.join(self.recordings_dir, camera_id)
            os.makedirs(audio_dir, exist_ok=True)
            
            # Generate audio file path (raw PCM data)
            audio_filename = f"{recording_id}_audio.raw"
            audio_file_path = os.path.join(audio_dir, audio_filename)
            
            # Get audio parameters
            sample_rate = int(camera_config.get('audio_sample_rate', 16000))
            channels = 1  # Mono audio
            
            # Open raw binary file for writing
            audio_file = open(audio_file_path, 'wb')
            
            # Create session info
            session = {
                'camera_id': camera_id,
                'recording_id': recording_id,
                'audio_cap': audio_cap,
                'audio_file': audio_file,
                'audio_file_path': audio_file_path,
                'start_time': time.time(),
                'sample_rate': sample_rate,
                'channels': channels,
                'recording': True,
                'retry_on_failure': True  # Flag to enable restart attempts
            }
            
            # Start audio recording thread
            recording_thread = threading.Thread(
                target=self._audio_recording_worker,
                args=(session,),
                daemon=True
            )
            session['recording_thread'] = recording_thread
            recording_thread.start()
            
            # Store session
            session_key = f"{camera_id}_{recording_id}"
            self.active_sessions[session_key] = session
            
            logger.info(f"Started audio capture for {camera_id}: {audio_file_path}")
            return session
            
        except Exception as e:
            logger.error(f"Failed to start audio capture for {camera_id}: {e}")
            return None
    
    def _audio_recording_worker(self, session: dict):
        """Worker thread that continuously reads audio chunks and writes to raw file."""
        audio_cap = session['audio_cap']
        audio_file = session['audio_file']
        camera_id = session['camera_id']
        
        logger.info(f"Audio recording worker started for {camera_id}")
        
        total_bytes_written = 0
        consecutive_failures = 0
        max_failures = 10
        restart_attempts = 0
        max_restart_attempts = 3
        
        try:
            while session['recording']:
                # Check if audio stream is still open
                if not audio_cap.is_audio_stream_opened():
                    logger.warning(f"Audio stream closed for {camera_id}")
                    
                    # Attempt to restart if configured to do so
                    if session.get('retry_on_failure', False) and restart_attempts < max_restart_attempts:
                        restart_attempts += 1
                        logger.info(f"Attempting audio stream restart {restart_attempts}/{max_restart_attempts} for {camera_id}")
                        
                        time.sleep(0.5)  # Brief delay before restart
                        success = self.camera_service.start_audio(camera_id)
                        
                        if success:
                            new_audio_cap = self.camera_service._audio_streams.get(camera_id)
                            if new_audio_cap and new_audio_cap.is_audio_stream_opened():
                                audio_cap = new_audio_cap
                                session['audio_cap'] = new_audio_cap
                                logger.info(f"Audio stream restarted successfully for {camera_id}")
                                consecutive_failures = 0
                                continue
                        
                        logger.warning(f"Failed to restart audio stream for {camera_id}")
                    
                    logger.warning(f"Audio stream unavailable, stopping recording for {camera_id}")
                    break
                    
                ret, audio_chunk = audio_cap.read_audio()
                
                if ret and audio_chunk and len(audio_chunk) > 0:
                    # Write raw audio chunk directly to file
                    audio_file.write(audio_chunk)
                    audio_file.flush()  # Ensure data is written
                    total_bytes_written += len(audio_chunk)
                    consecutive_failures = 0
                    restart_attempts = 0  # Reset restart counter on successful read
                    
                    # Debug log every ~1 second of audio (16000 samples = 1 sec at 16kHz)
                    if total_bytes_written % (16000 * 2) < len(audio_chunk):  # 2 bytes per sample
                        logger.debug(f"Audio recording {camera_id}: {total_bytes_written} bytes written")
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= max_failures:
                        logger.warning(f"Too many audio read failures for {camera_id}, stopping recording")
                        break
                    # Small sleep to prevent busy loop if no audio data
                    time.sleep(0.01)
                    
        except Exception as e:
            logger.error(f"Error in audio recording worker for {camera_id}: {e}")
        finally:
            logger.info(f"Audio recording worker finished for {camera_id}, total bytes: {total_bytes_written}")

    def stop_audio_capture(self, audio_session: Optional[dict]) -> Optional[str]:
        """
        Stop audio capture and finalize the audio file.
        
        Args:
            audio_session: Audio session dictionary from start_audio_capture
            
        Returns:
            Path to the captured audio file, or None if no valid session
        """
        if not audio_session:
            return None
            
        try:
            camera_id = audio_session.get('camera_id')
            recording_id = audio_session.get('recording_id')
            audio_file = audio_session.get('audio_file')
            audio_cap = audio_session.get('audio_cap')
            audio_file_path = audio_session.get('audio_file_path')
            recording_thread = audio_session.get('recording_thread')
            
            # Stop the recording
            audio_session['recording'] = False
            
            # Wait for recording thread to finish
            if recording_thread:
                recording_thread.join(timeout=2)
                
            # Close audio file
            if audio_file:
                try:
                    audio_file.close()
                except Exception as e:
                    logger.warning(f"Error closing audio file: {e}")
                    
            # Don't close the audio capture - it's shared with live streaming
            # Just log that we're done with it
            logger.debug(f"Released audio recording session for {camera_id}")
                    
            # Remove from active sessions
            session_key = f"{camera_id}_{recording_id}"
            if session_key in self.active_sessions:
                del self.active_sessions[session_key]
                
            # Check if audio file was created and has content
            if audio_file_path and os.path.exists(audio_file_path):
                file_size = os.path.getsize(audio_file_path)
                if file_size > 0:  # Any audio data is good
                    logger.info(f"Audio capture completed: {audio_file_path} ({file_size} bytes)")
                    return audio_file_path
                else:
                    logger.warning(f"Audio file is empty, removing: {audio_file_path}")
                    try:
                        os.remove(audio_file_path)
                    except OSError:
                        pass
                        
            return None
            
        except Exception as e:
            logger.error(f"Error stopping audio capture: {e}")
            return None
    
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
            logger.error(f"Video file not found: {video_file_path}")
            return video_file_path
            
        if not os.path.exists(audio_file_path):
            logger.error(f"Audio file not found: {audio_file_path}")
            return video_file_path
            
        # Check audio file size
        audio_size = os.path.getsize(audio_file_path)
        logger.info(f"Muxing audio file: {audio_file_path} ({audio_size} bytes) at {sample_rate}Hz")
        
        if audio_size == 0:
            logger.warning(f"Audio file is empty, returning video only: {video_file_path}")
            return video_file_path
            
        try:
            # Generate output file path
            base_name = os.path.splitext(video_file_path)[0]
            output_file_path = f"{base_name}_with_audio.mp4"
            
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
                '-map', '0:v:0',        # Map first video stream
                '-map', '1:a:0',        # Map first audio stream  
                '-shortest',            # Match shortest stream duration
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
                except OSError:
                    logger.warning(f"Could not remove original video file: {video_file_path}")
                    
                return output_file_path
            else:
                stderr_output = result.stderr[-500:] if result.stderr else "No error output"
                logger.error(f"FFmpeg mux failed (returncode={result.returncode}): {stderr_output}")
                logger.error(f"FFmpeg stdout: {result.stdout}")
                return video_file_path
                
        except subprocess.TimeoutExpired:
            logger.error(f"Audio/video mux timeout: {video_file_path}")
            return video_file_path
        except Exception as e:
            logger.error(f"Error during audio/video mux: {e}")
            return output_file_path
    
    def cleanup_orphaned_sessions(self):
        """Clean up any orphaned audio capture sessions."""
        orphaned_keys = []
        
        for session_key, session in self.active_sessions.items():
            recording_thread = session.get('recording_thread')
            if recording_thread and not recording_thread.is_alive():
                # Thread has terminated
                orphaned_keys.append(session_key)
                
        for key in orphaned_keys:
            session = self.active_sessions.pop(key, {})
            camera_id = session.get('camera_id', 'unknown')
            logger.info(f"Cleaned up orphaned audio session: {camera_id}")
    
    def stop_all_sessions(self):
        """Stop all active audio capture sessions."""
        session_keys = list(self.active_sessions.keys())
        
        for session_key in session_keys:
            session = self.active_sessions.get(session_key)
            if session:
                self.stop_audio_capture(session)
        
        logger.info(f"Stopped {len(session_keys)} audio capture sessions")
    
    def get_session_info(self, camera_id: str) -> Optional[dict]:
        """Get information about active audio session for a camera."""
        for session_key, session in self.active_sessions.items():
            if session.get('camera_id') == camera_id:
                recording_thread = session.get('recording_thread')
                return {
                    'camera_id': camera_id,
                    'recording_id': session.get('recording_id'),
                    'start_time': session.get('start_time'),
                    'audio_file_path': session.get('audio_file_path'),
                    'is_recording': recording_thread and recording_thread.is_alive()
                }
        return None