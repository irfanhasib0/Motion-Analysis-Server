import gc
import os
import select
import subprocess
import sys
import cv2
import json
import numpy as np
import asyncio
import threading
import time
from typing import Generator, Dict, Optional, Any, Union, List
import logging
from services.hls_manager import HLSManager  
from services.ws_streaming_manager import WSStreamingManager
from services.drawing_utils import StreamDrawingHelper
from services.frame_buffer import FrameRingBuffer, AudioRingBuffer, ResultsRingBuffer, FrameBufferManager
from services.ai_service import AIService

PROJECT_SRC_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))
if PROJECT_SRC_PATH not in sys.path:
    sys.path.append(PROJECT_SRC_PATH)

from audioproc import FrequencyIntensityAnalyzer
from services.colors import Colors

logger = logging.getLogger(__name__)

class StreamingService:
    def __init__(self, frame_rbf_len: int = 10, audio_rbf_len: int = 10, results_rbf_len: int = 10):
        #self.db = DatabaseService()  # Original database service for cameras and recordings
        self.stream_locks: Dict[str, threading.Lock] = {}
        self.active_streams: Dict[str, str] = {}  # camera_id -> active stream token
        self.active_audio_streams: Dict[str, str] = {}  # camera_id -> active audio stream token
        self.active_processing_streams: Dict[str, str] = {}  # camera_id -> active processing stream token
        self._latest_frames: Dict[str, np.ndarray] = {}
        self._latest_hls_frames: Dict[str, np.ndarray] = {}
        self._latest_frame_seq: Dict[str, int] = {}
        self._latest_viz: Dict[str, np.ndarray] = {}
        self._latest_res_video: Dict[str, Dict[str, Any]] = {}
        self._latest_res_audio: Dict[str, Dict[str, Any]] = {}
        self._fps_stats: Dict[str, Dict[str, float]] = {}
        # Initialize HLS manager with direct streaming service reference
        self._hls_manager = HLSManager(
            get_recordings_dir=lambda: getattr(self, 'recordings_dir', os.path.abspath('./recordings')),
            get_camera_config=lambda camera_id: self.db.get_camera(camera_id) if hasattr(self, 'db') else None,
            ensure_background_stream=self.start_av_stream,
            streaming_service=self,  # Direct reference for _get_spmc_data calls
            register_consumer=self.register_consumer,  # Consumer registration
        )
        self._hls_pipe_threads: Dict[str, threading.Thread] = {}
        self._hls_pipe_stop_events: Dict[str, threading.Event] = {}
        # Initialize WebSocket streaming manager (mirrors HLS manager pattern)
        self._ws_manager = WSStreamingManager(
            streaming_service=self,
            register_consumer=self.register_consumer,
        )
        self._latest_audio_chunk_analysis: Dict[str, Dict[str, Any]] = {}
        self._audio_streams: Dict[str, Any] = {}  # camera_id -> audio-only Capture
        self._drawing = StreamDrawingHelper()
        self.ai_service = AIService()
    # =====================================================================
    # INITIALIZATION AND CONFIGURATION
    # =====================================================================
    
        self.sensitivity_level = 5
        self.default_sensitivity = 2
        self._camera_sensitivity: Dict[str, int] = {}
        self._audio_chunk_index: Dict[str, int] = {}
        self._latest_pts_payload: Dict[str, Dict[Any, Dict[str, Any]]] = {}
        self._overlay_masks: Dict[str, np.ndarray] = {}
        self._latest_person_stats: Dict[str, Dict[str, float]] = {}
        
        # Simple audio streaming with background threads
        self._audio_background_threads: Dict[str, threading.Thread] = {}
        self._audio_stop_events: Dict[str, threading.Event] = {}
        self._latest_audio_chunk: Dict[str, bytes] = {}
        self._latest_audio_chunk_seq: Dict[str, int] = {}
        self._audio_thread_locks: Dict[str, threading.Lock] = {}
        
        # Simple video streaming with background threads
        self._video_background_threads: Dict[str, threading.Thread] = {}
        self._video_stop_events: Dict[str, threading.Event] = {}
        self._latest_video_frame: Dict[str, bytes] = {}
        
        # SPMC Ring Buffers for efficient multi-consumer data distribution
        self._frame_ring_buffers: Dict[str, FrameRingBuffer] = {}  # camera_id -> frame buffer
        self._audio_ring_buffers: Dict[str, AudioRingBuffer] = {}  # camera_id -> audio buffer  
        self._results_ring_buffers: Dict[str, ResultsRingBuffer] = {}  # camera_id -> results buffer
        self._viz_ring_buffers: Dict[str, FrameRingBuffer] = {}  # camera_id -> visualization buffer (reuse FrameRingBuffer)
        self._overlay_frame_ring_buffers: Dict[str, FrameRingBuffer] = {}  # camera_id -> overlay frame buffer (with FPS/optical flow overlays)
        self._ring_buffer_lock = threading.RLock()  # For buffer management
        self._video_thread_locks: Dict[str, threading.Lock] = {}

        self.max_sequence_number = 2**32 -1  # Prevent unbounded growth of sequence numbers
        self.frame_rbf_len = frame_rbf_len  # Ring buffer length for frames (from config)
        self.audio_rbf_len = audio_rbf_len  # Ring buffer length for audio chunks (from config)
        self.results_rbf_len = results_rbf_len  # Ring buffer length for results (from config)
        self.no_motion_slow_down_thr_sec = 300
        self.no_motion_slow_down_delay_sec = 0.3

        # Initialize StreamingService - ready for camera management
    
    # =====================================================================
    # UTILITY AND HELPER METHODS
    # =====================================================================

    def _sequence_has_new_data(self, current_seq, last_seq, max_val=2**32):
        """Check if current sequence indicates new data, handling wrap-around"""
        if last_seq == -1:  # Initial state
            return current_seq != -1
        
        # Handle wrap-around by computing signed difference
        diff = (current_seq - last_seq) % max_val
        if diff > max_val // 2:  # Wrapped backwards (shouldn't happen normally)
            diff -= max_val
        
        return diff > 0

    def set_camera_sensitivity(self, camera_id: str, sensitivity: int) -> int:
        safe_sensitivity = max(0, min(int(self.sensitivity_level), int(sensitivity)))
        self._camera_sensitivity[camera_id] = safe_sensitivity
        return safe_sensitivity

    def get_camera_sensitivity(self, camera_id: str) -> int:
        try:
            value = int(self._camera_sensitivity.get(camera_id, self.default_sensitivity))
        except (TypeError, ValueError):
            value = self.default_sensitivity
        return max(0, min(int(self.sensitivity_level), value))

    def get_camera_effective_stride(self, camera_id: str) -> int:
        sensitivity = self.get_camera_sensitivity(camera_id)
        sensitivity_stride = max(1, int(self.sensitivity_level) - sensitivity)
        if sensitivity_stride >= int(self.sensitivity_level):
            return int(self.sensitivity_level)
        return max(1, sensitivity_stride)

    def _get_audio_chunk_analyzer(self, camera_id: str) -> Optional[FrequencyIntensityAnalyzer]:
        """Get audio analyzer for camera (managed by CameraService now)"""
        return self._audio_chunk_analyzers.get(camera_id)

    def get_latest_audio_chunk_analysis(self, camera_id: str) -> Optional[Dict[str, Any]]:
        """Get latest audio analysis from background thread."""
        return self._latest_audio_chunk_analysis.get(camera_id)
    
    # =====================================================================
    # RING BUFFER MANAGEMENT
    # =====================================================================
    
    def _ensure_ring_buffers(self, camera_id: str) -> None:
        """Ensure ring buffers exist for camera"""
        with self._ring_buffer_lock:
            if camera_id not in self._frame_ring_buffers:
                self._frame_ring_buffers[camera_id] = FrameRingBuffer(capacity=self.frame_rbf_len, enable_stats=True)
            
            if camera_id not in self._overlay_frame_ring_buffers:
                self._overlay_frame_ring_buffers[camera_id] = FrameRingBuffer(capacity=self.frame_rbf_len, enable_stats=True)

            if camera_id not in self._audio_ring_buffers:
                self._audio_ring_buffers[camera_id] = AudioRingBuffer(capacity=self.audio_rbf_len, enable_stats=True)
            
            if camera_id not in self._results_ring_buffers:
                self._results_ring_buffers[camera_id] = ResultsRingBuffer(capacity=self.results_rbf_len, enable_stats=True)
            
            if camera_id not in self._viz_ring_buffers:
                self._viz_ring_buffers[camera_id] = FrameRingBuffer(capacity=self.frame_rbf_len, enable_stats=True)
            

    def register_consumer(self, camera_id: str, consumer_id: str, 
                         data_types: List[str] = ['frames', 'audio', 'results']) -> bool:
        """Register consumer for specific data types from camera
        
        Args:
            camera_id: Camera to consume from
            consumer_id: Unique consumer identifier (e.g., 'recorder_cam1', 'streamer_cam1')
            data_types: List of data types to consume ['frames', 'audio', 'results', 'viz', 'overlay']
        """
        self._ensure_ring_buffers(camera_id)
        
        success = True
        
        if 'frames' in data_types:
            success &= self._frame_ring_buffers[camera_id].register_consumer(f"{consumer_id}_frames")
        
        if 'audio' in data_types:
            success &= self._audio_ring_buffers[camera_id].register_consumer(f"{consumer_id}_audio")
        
        if 'results' in data_types:
            success &= self._results_ring_buffers[camera_id].register_consumer(f"{consumer_id}_results")
        
        if 'viz' in data_types:
            success &= self._viz_ring_buffers[camera_id].register_consumer(f"{consumer_id}_viz")
        
        if 'overlay' in data_types:
            success &= self._overlay_frame_ring_buffers[camera_id].register_consumer(f"{consumer_id}_overlay")
        
        logger.info(f"Registered consumer {consumer_id} for camera {camera_id} ({data_types})")
        return success
    
    def unregister_consumer(self, camera_id: str, consumer_id: str, 
                           data_types: List[str] = ['frames', 'audio', 'results', 'viz', 'overlay']) -> bool:
        """Unregister consumer from specific data types"""
        if camera_id not in self._frame_ring_buffers:
            return False
        
        success = True
        
        if 'frames' in data_types:
            success &= self._frame_ring_buffers[camera_id].unregister_consumer(f"{consumer_id}_frames")
        
        if 'audio' in data_types:
            success &= self._audio_ring_buffers[camera_id].unregister_consumer(f"{consumer_id}_audio")
        
        if 'results' in data_types:
            success &= self._results_ring_buffers[camera_id].unregister_consumer(f"{consumer_id}_results")
        
        if 'viz' in data_types:
            success &= self._viz_ring_buffers[camera_id].unregister_consumer(f"{consumer_id}_viz")
        
        if 'overlay' in data_types:
            success &= self._overlay_frame_ring_buffers[camera_id].unregister_consumer(f"{consumer_id}_overlay")
        
        logger.info(f"Unregistered consumer {consumer_id} from {camera_id}")
        return success
    
    # =====================================================================
    # SPMC CONSUMER ACCESS METHODS
    # =====================================================================
    
    def _get_spmc_data(self, camera_id: str, consumer_id: str, data_type: str, peek: bool = False) -> Optional[Union[np.ndarray, bytes, Dict[str, Any]]]:
        """Generic SPMC data access method"""
        if data_type == 'frames':
            buffer_dict = self._frame_ring_buffers
        elif data_type == 'audio':
            buffer_dict = self._audio_ring_buffers
        elif data_type == 'results':
            buffer_dict = self._results_ring_buffers
        elif data_type == 'viz':
            buffer_dict = self._viz_ring_buffers
        elif data_type == 'overlay':
            buffer_dict = self._overlay_frame_ring_buffers
        else:
            return None # Invalid data type     
            
        if not buffer_dict or camera_id not in buffer_dict:
            return None
        
        consumer_key = f"{consumer_id}_{data_type}"
        if peek:
            return buffer_dict[camera_id].peek(consumer_key)
        return buffer_dict[camera_id].get(consumer_key)
    
    def get_ring_buffer_stats(self, camera_id: str) -> Dict[str, Any]:
        """Get ring buffer statistics for debugging and monitoring"""
        if camera_id not in self._frame_ring_buffers:
            return {}
        
        return {
            'frames': self._frame_ring_buffers[camera_id].get_stats(),
            'audio': self._audio_ring_buffers[camera_id].get_stats(),
            'results': self._results_ring_buffers[camera_id].get_stats(),
            'viz': self._viz_ring_buffers[camera_id].get_stats(),
            'overlay': self._overlay_frame_ring_buffers[camera_id].get_stats()
        }
    
    # =====================================================================
    # STREAM PROCESSING AND BACKGROUND THREADS
    # =====================================================================
    
    def start_av_stream(self, camera_id: str):
        """Start background video and audio streams directly (checks audio_enabled setting).
        Returns True=success, False=retryable failure, None=fatal (don't retry)."""
        try:
            # Start video background thread if not already running
            # (start_video_stream calls _ensure_ring_buffers internally)
            if camera_id not in self._video_background_threads:
                result = self.start_video_stream(camera_id)
                if result is not True:
                    return result  # propagate None (fatal) or False (retryable)

            # Start audio background thread only if audio is enabled for this camera
            db_camera = self.db.get_camera(camera_id)
            audio_enabled = bool(db_camera.get('audio_enabled', False)) if db_camera else False

            if audio_enabled and camera_id not in self._audio_background_threads:
                self.start_audio_stream(camera_id)

            return True
        except Exception as error:
            logger.warning(f"{Colors.RED}Failed to start AV streams for {camera_id}: {error}{Colors.RESET}")
            return False



    # =====================================================================
    # HLS INTEGRATION (Direct access to _hls_manager for simplicity)
    # =====================================================================

    # =====================================================================
    # INTERNAL PROCESSING HELPERS
    # =====================================================================
    
    def _update_loop_fps(self, stream_key: str) -> float:
        now = time.time()
        stats = self._fps_stats.get(stream_key)
        if stats is None:
            self._fps_stats[stream_key] = {
                "window_start": now,
                "count": 1.0,
                "fps": 0.0,
            }
            return 0.0

        stats["count"] += 1.0
        elapsed = now - stats["window_start"]
        if elapsed >= 1.0:
            stats["fps"] = stats["count"] / elapsed
            stats["count"] = 0.0
            stats["window_start"] = now
        return stats["fps"]

    def _aggregate_motion_result(self, raw_result: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        merged = {'vel': 0.0, 'bg_diff': 0, 'ts': time.time(), 'det_ts': None, 'detected_vel': False, 'detection_bg_diff': False}
        for item in raw_result.values():
            merged['vel'] = max(float(item.get('vel', 0.0) or 0.0), merged['vel'])
            merged['bg_diff'] = max(int(item.get('bg_diff', 0) or 0), merged['bg_diff'])
        
        # Check for stale motion data
        curr_time = time.time()
        res_timestamp = float(merged.get('ts', 0.0) or 0.0)
        is_stale_motion_sample = (res_timestamp <= 0.0) or ((curr_time - res_timestamp) > self.motion_result_max_age_sec)
        
        # Add motion detection threshold checking with staleness check
        if not is_stale_motion_sample:
            merged['detected_vel'] = merged['vel'] > self.max_velocity
            merged['detection_bg_diff'] = merged['bg_diff'] >= self.max_bg_diff

        if merged['detected_vel'] or merged['detection_bg_diff']:
            merged['det_ts'] = time.time()  # Update timestamp to now if motion is detected
            
        return merged

    def _extract_detect_payload(
        self,
        detect_output: Any,
    ) -> tuple[Dict[str, Any], Dict[Any, Dict[str, Any]]]:
        """Extract points_dict and pts_payload from tracker.detect() output.
        The visualized frame from detect() is discarded — stream_frame is used directly."""
        if not isinstance(detect_output, tuple) or len(detect_output) == 0:
            return {}, {}

        points_dict: Dict[str, Any] = {}
        pts_payload: Dict[Any, Dict[str, Any]] = {}

        # Skip detect_output[0] (the visualized frame — unused)
        for item in detect_output[1:]:
            if isinstance(item, dict):
                if not points_dict and any(
                    isinstance(value, dict) and ('vel' in value or 'mean_vel' in value)
                    for value in item.values()
                ):
                    points_dict = item
                    continue
                if not pts_payload and any(
                    isinstance(value, dict) and ('keypoints_1' in value or 'keypoints_2' in value)
                    for value in item.values()
                ):
                    pts_payload = item

        return points_dict, pts_payload

    def _draw_motion_analysis_chart(self, plot_array: np.ndarray, points_dict: Dict[str, Any], camera_id: str) -> tuple[np.ndarray, Dict[str, Dict[str, Any]]]:
        return self._drawing.draw_motion_analysis_chart(
            plot_array,
            points_dict,
            camera_id,
            self._latest_audio_chunk_analysis,
        )

    def _draw_stream_status_overlay(self, frame: np.ndarray, camera_id: str, fps_value: float, res: Optional[Dict[str, Any]] = None) -> np.ndarray:
        return self._drawing.draw_stream_status_overlay(
            frame,
            camera_id,
            fps_value,
            res,
            self._latest_audio_chunk_analysis,
        )

    def _draw_optical_flow_overlay(self, frame: np.ndarray, camera_id: str, pts_payload: Optional[Dict[Any, Dict[str, Any]]]) -> np.ndarray:
        current_mask = self._overlay_masks.get(camera_id)
        frame_with_overlay, updated_mask = self._drawing.draw_optical_flow_overlay(frame, pts_payload, current_mask)
        self._overlay_masks[camera_id] = updated_mask
        return frame_with_overlay

            
    # =====================================================================
    # FRAME UTILITY METHODS
    # =====================================================================
    
    def generate_message_frame(self, msg: str = "Camera Unavailable", for_streaming: bool = True) -> bytes:
        """Generate frame with text message (consolidated from generate_failure_frame and generate_blank_image)"""
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        w, h = cv2.getTextSize(msg, cv2.FONT_HERSHEY_COMPLEX, 0.7, 1)[0]
        x = (frame.shape[1] - w) // 2
        y = (frame.shape[0] + h) // 2
        frame = cv2.putText(frame, msg, (x, y), cv2.FONT_HERSHEY_COMPLEX, 0.7, (100, 100, 100), 1)
        
        if for_streaming:
            return self.frame_to_bytes(frame)
        else:
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            return buffer.tobytes()
    
    def generate_failure_frame(self, msg: str = "Camera Unavailable") -> bytes:
        """Generate failure frame for streaming (backwards compatibility)"""
        return self.generate_message_frame(msg, for_streaming=True)
    
    def generate_blank_image(self, msg: str = '') -> bytes:
        """Generate blank image with message (backwards compatibility)"""
        return self.generate_message_frame(msg, for_streaming=False)
    
    def frame_to_bytes(self, frame) -> bytes:
        """Convert a video frame to bytes for streaming"""
        quality = int(getattr(self, 'jpeg_quality', 70) or 70)
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        buffer = (b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        return buffer

    @staticmethod
    def _make_wav_header(sample_rate: int, num_channels: int, bits_per_sample: int = 16) -> bytes:
        """Build a 44-byte WAV header suitable for streaming.

        Both the RIFF and data chunk sizes are set to 0xFFFFFFFF (the standard
        sentinel for unknown/streaming length).  Most browsers, ffplay, and Web
        Audio API accept this without issue.
        """
        import struct
        byte_rate   = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        return struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', 0xFFFFFFFF,   # RIFF chunk size  — unknown/streaming
            b'WAVE',
            b'fmt ', 16,           # fmt  sub-chunk size
            1,                     # PCM audio format
            num_channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b'data', 0xFFFFFFFF,   # data sub-chunk size — unknown/streaming
        )

    def start_audio_stream(self, camera_id: str) -> bool:
        """Start background audio stream thread for camera."""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            logger.warning(f"Camera not found: {camera_id}")
            return False

        if not bool(db_camera.get('audio_enabled', False)):
            logger.warning(f"{Colors.RED}Audio not enabled for {camera_id}{Colors.RESET}")
            return False

        # Check if thread is already running and alive
        existing_thread = self._audio_background_threads.get(camera_id)
        if existing_thread and existing_thread.is_alive():
            logger.warning(f"{Colors.RED}Audio thread already running for {camera_id}{Colors.RESET}")
            return True

        # Stop existing thread if running
        self.stop_audio_stream(camera_id)
        
        # Ensure ring buffers and register consumer
        self._ensure_ring_buffers(camera_id)
        self.register_consumer(camera_id, f"audio_stream_{camera_id}", ['audio'])
        
        # Create thread-safe resources
        stop_event = threading.Event()
        audio_lock = threading.Lock()
        self._audio_stop_events[camera_id] = stop_event
        self._audio_thread_locks[camera_id] = audio_lock
        
        def _audio_thread():
            """Background thread that continuously captures audio chunks."""
            cap = None
            try:
                # Start audio capture
                success = self.start_audio(camera_id)
                if not success:
                    logger.warning(f"{Colors.RED}Failed to start audio for {camera_id}{Colors.RESET}")
                    return
                
                cap = self._audio_streams.get(camera_id)
                if not cap or not cap.is_audio_stream_opened():
                    logger.warning(f"{Colors.RED}Audio capture unavailable for {camera_id}{Colors.RESET}")
                    return
                
                analyzer = self._get_audio_chunk_analyzer(camera_id)
                consecutive_failures = 0
                max_failures = 10
                audio_chunk_seq = 0
                
                logger.info(f"{Colors.GREEN}Audio stream started for {camera_id}{Colors.RESET}")
                while not stop_event.is_set() and cap.is_audio_stream_opened():
                    ret, chunk = cap.read_audio()
                    audio_capture_time = time.time()  # Capture timestamp immediately after audio read
                    
                    if not ret or chunk is None or len(chunk) == 0:
                        consecutive_failures += 1
                        if consecutive_failures >= max_failures:
                            logger.warning(f"{Colors.RED}Too many audio failures for {camera_id}, stopping{Colors.RESET}")
                            break
                        time.sleep(0.01)
                        continue
                    
                    consecutive_failures = 0
                    audio_chunk_seq += 1
                    
                    # Process audio analysis
                    samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
                    samples /= (32768.0/25.0)  # Normalize to -10.0 to +10.0 range for 16-bit audio
                    
                    self._audio_chunk_index[camera_id] = (self._audio_chunk_index.get(camera_id, 0) + 1) % self.max_sequence_number
                    audio_chunk_index = self._audio_chunk_index[camera_id]
                    effective_stride = self.get_camera_effective_stride(camera_id)
                    
                    should_run_analysis = (effective_stride == 1 or audio_chunk_index % effective_stride == 0) if effective_stride != int(self.sensitivity_level) else False
                    
                    if should_run_analysis and analyzer is not None:
                        analysis = analyzer.process_chunk(samples)
                        self._latest_audio_chunk_analysis[camera_id] = analysis
                        self._latest_res_audio[camera_id] = {
                            'int': float(analysis.get('overall_intensity', 0.0) or 0.0),
                            'freq': float(analysis.get('peak_frequency_mean', 0.0) or 0.0),
                            'detected_loudness': float(analysis.get('overall_intensity', 0.0) or 0.0) > 0.0  # Threshold set to 0.0 for now
                        }
                        
                        # Update current audio results and publish only audio to ring buffer with capture timestamp
                        if camera_id in self._results_ring_buffers:
                            self._results_ring_buffers[camera_id].put_audio_only_with_timestamp(self._latest_res_audio[camera_id], audio_capture_time)
                    
                    _ = self._update_loop_fps(f"{camera_id}:audio")

                    # Store latest chunk with sequence number (thread-safe)
                    with audio_lock:
                        self._latest_audio_chunk[camera_id] = chunk
                        self._latest_audio_chunk_seq[camera_id] = audio_chunk_seq
                        
                        # Publish to ring buffer for SPMC consumers with capture timestamp
                        if camera_id in self._audio_ring_buffers:
                            self._audio_ring_buffers[camera_id].put_with_timestamp(chunk, audio_capture_time)
                            

                    camera_res = self._latest_res_video.get(camera_id, {})
                    det_ts = camera_res.get('det_ts')
                    if det_ts and (time.time() - det_ts) > self.no_motion_slow_down_thr_sec:
                        time.sleep(self.no_motion_slow_down_delay_sec)

                logger.info(f"{Colors.YELLOW}Audio stream finished for {camera_id}{Colors.RESET}")
            except Exception:
                logger.exception(f"{Colors.RED} Audio thread crashed for {camera_id}{Colors.RESET}")
            finally:
                # Cleanup audio resources regardless of how we exit
                if cap is not None:
                    try:
                        cap.release_audio()
                    except Exception:
                        pass
                self._audio_streams.pop(camera_id, None)
                logger.info(f"{Colors.YELLOW}Audio thread exiting for {camera_id}{Colors.RESET}")
        
        # Start thread
        thread = threading.Thread(target=_audio_thread, daemon=True, name=f'audio-bg-{camera_id}')
        thread.start()
        self._audio_background_threads[camera_id] = thread
        
        logger.info(f"{Colors.GREEN}Audio stream started for {camera_id}{Colors.RESET}")
        return True

    def stop_audio_stream(self, camera_id: str) -> bool:
        """Stop background audio stream thread for camera."""
        # Check if there's actually a thread to stop
        thread = self._audio_background_threads.get(camera_id)
        thread_was_running = thread and thread.is_alive()
        
        # Signal stop
        stop_event = self._audio_stop_events.get(camera_id)
        if stop_event:
            stop_event.set()
        
        # Wait for thread to finish
        if thread and thread.is_alive():
            thread.join(timeout=2)
        
        # Unregister consumer
        self.unregister_consumer(camera_id, f"audio_stream_{camera_id}", ['audio'])
        
        # Cleanup
        self._audio_background_threads.pop(camera_id, None)
        self._audio_stop_events.pop(camera_id, None)
        self._audio_thread_locks.pop(camera_id, None)
        self._latest_audio_chunk.pop(camera_id, None)
        self._latest_audio_chunk_seq.pop(camera_id, None)
        self.active_audio_streams.pop(camera_id, None)
        
        # Legacy cleanup  
        self._latest_audio_chunk_analysis.pop(camera_id, None)
        self._latest_res_audio.pop(camera_id, None)
        self._audio_chunk_index.pop(camera_id, None)
        
        if thread_was_running:
            logger.info(f"{Colors.YELLOW}Audio stream stopped for {camera_id}{Colors.RESET}")
        
        # Stop audio capture
        self.stop_audio(camera_id)
        
        return True

    def generate_audio_stream_endpoint(self, camera_id: str) -> Generator[bytes, None, None]:
        """Generate audio stream by reading from background thread data."""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            logger.warning(f"Camera not found: {camera_id}")
            return

        if not bool(db_camera.get('audio_enabled', False)):
            logger.info(f"{Colors.GREEN}Audio stream started for {camera_id}{Colors.RESET}")
            return
        
        audio_sample_rate = int(db_camera.get('audio_sample_rate') or os.getenv('AUDIO_SAMPLE_RATE', '16000'))
        audio_channels = int(db_camera.get('audio_channels') or os.getenv('AUDIO_CHANNELS', '1'))
        
        # Create stream token and use pre-registered consumer
        stream_token = f"{time.time_ns()}:{threading.get_ident()}"
        consumer_id = f"audio_stream_{camera_id}"  # Use camera-specific consumer ID
        previous_token = self.active_audio_streams.get(camera_id)
        if previous_token:
            logger.info(f"{Colors.GREEN}Audio stream connected for {camera_id}{Colors.RESET}")
        else:
            logger.info(f"{Colors.GREEN}Audio stream started for {camera_id}{Colors.RESET}")
        self.active_audio_streams[camera_id] = stream_token
        
        # Consumer already registered in start_av_stream - just use it
        
        # Yield WAV header first
        yield self._make_wav_header(audio_sample_rate, audio_channels)
        
        # Background thread should already be running from /start endpoint
        if camera_id not in self._audio_background_threads:
            logger.warning(f"{Colors.RED}No audio thread for {camera_id} - start audio ...{Colors.RESET}")
            self.start_audio_stream(camera_id)
            return
        
        consecutive_empty = 0
        # Stream loop
        while (self.active_audio_streams.get(camera_id) == stream_token and 
               camera_id in self._audio_background_threads):
            
            # Use SPMC ring buffer access
            current_chunk = self._get_spmc_data(camera_id, consumer_id, 'audio')
            
            if current_chunk and len(current_chunk) > 0:
                yield current_chunk
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                time.sleep(0.1)  # 100ms wait between checks
                # Keep audio stream alive indefinitely - don't terminate on silence
                # Audio streams naturally have gaps during silence
        
        # Cleanup token (consumer stays registered for other streams)
        if self.active_audio_streams.get(camera_id) == stream_token:
            self.active_audio_streams.pop(camera_id, None)
        
        logger.info(f"{Colors.YELLOW}Audio stream finished for {camera_id}{Colors.RESET}")

    def start_video_stream(self, camera_id: str) -> bool:
        """Start background video stream thread for camera."""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            logger.warning(f"Camera not found: {camera_id}")
            return False

        # Stop existing thread only if one is actually running
        if camera_id in self._video_background_threads:
            self.stop_video_stream(camera_id)
        
        # Ensure ring buffers and register consumers
        self._ensure_ring_buffers(camera_id)
        self.register_consumer(camera_id, f"video_stream_{camera_id}", ['overlay'])
        self.register_consumer(camera_id, f"proc_stream_{camera_id}", ['viz', 'results'])
        
        # Initialize camera capture if needed
        if camera_id not in self._camera_streams:
            success = self.start_video(camera_id)
            if success is not True:
                logger.warning(f"{Colors.RED}Failed to start video for {camera_id}{Colors.RESET}")
                return success  # None (fatal/don't retry) or False (retryable)
        
        cap = self._camera_streams.get(camera_id)
        
        if not cap:
            logger.warning(f"{Colors.RED}Camera capture unavailable for {camera_id}{Colors.RESET}")
            return False
        
        # Ensure tracker is running (AIService handles multiprocess vs sequential)
        tracker_kwargs = {
            'enable_person_detection': getattr(self, 'enable_person_detection', False),
            'enable_yolox': getattr(self, 'enable_yolox', False),
            'yolox_model_size': getattr(self, 'yolox_model_size', 'nano'),
            'yolox_score_thr': getattr(self, 'yolox_score_thr', 0.5),
        }
        if not self.ai_service.ensure_tracker(camera_id, tracker_kwargs):
            logger.warning(f"{Colors.RED}Failed to start tracker for {camera_id}{Colors.RESET}")
            return False
        
        # Create thread-safe resources
        stop_event = threading.Event()
        video_lock = threading.Lock()
        self._video_stop_events[camera_id] = stop_event
        self._video_thread_locks[camera_id] = video_lock
        
        # Get or create stream lock for this camera
        if camera_id not in self.stream_locks:
            self.stream_locks[camera_id] = threading.Lock()
        stream_lock = self.stream_locks[camera_id]
        
        def _video_thread():
            """Background thread that continuously captures and processes video frames."""
            try:
                logger.info(f"{Colors.GREEN}Video stream started for {camera_id}{Colors.RESET}")
                
                consecutive_failures = 0
                max_failures = 10
                frame_index = 0
                _last_tracker_result_ts = None  # Track last seen tracker result to avoid double-counting in FPS
                
                while not stop_event.is_set() and cap.is_video_stream_opened():
                    with stream_lock:
                        frame_capture_time = time.time()  # Capture timestamp before read to avoid burst-skew on Pi
                        ret, frame = cap.read_video()
                        
                        if not ret:
                            consecutive_failures += 1
                            if consecutive_failures >= max_failures:
                                logger.warning(f"{Colors.RED}Too many video failures for {camera_id}, stopping{Colors.RESET}")
                                break
                            else:
                                time.sleep(0.1)
                                continue
                        
                        consecutive_failures = 0
                        
                        # Store original frame for recording consumers
                        self._latest_frames[camera_id] = frame
                        self._latest_frame_seq[camera_id] = (self._latest_frame_seq.get(camera_id, 0) + 1) % self.max_sequence_number
                        frame_index += 1
                        
                        # Publish to ring buffer for SPMC consumers with capture timestamp
                        if camera_id in self._frame_ring_buffers:
                            self._frame_ring_buffers[camera_id].put_with_timestamp(frame, frame_capture_time)
                    
                    # Resize frame for streaming performance
                    stream_frame = self._resize_frame_for_streaming(frame)
                    effective_stride = self.get_camera_effective_stride(camera_id)
                    
                    # Determine if we should run motion tracking on this frame
                    should_run_tracker = (effective_stride == 1 or frame_index % effective_stride == 0) if effective_stride != int(self.sensitivity_level) else False
                    
                    # ── Obtain detection output ──
                    detect_output = None
                    result_ts = frame_capture_time
                    
                    if should_run_tracker:
                        self.ai_service.submit_frame(camera_id, stream_frame, frame_capture_time, frame_index)
                    tracker_result = self.ai_service.poll_result(camera_id)
                    if tracker_result is not None:
                        detect_output = (tracker_result.points_dict, tracker_result.flow_pts)
                        result_ts = tracker_result.frame_capture_time
                        if result_ts != _last_tracker_result_ts:
                            self._update_loop_fps(f"{camera_id}:tracker")
                            _last_tracker_result_ts = result_ts
                    
                    # ── Process detection output (shared path) ──
                    if detect_output is not None:
                        points_dict, flow_pts = detect_output
                        self._latest_person_stats[camera_id] = points_dict.get('_stats', {})
                        
                        has_proc_viewer = bool(self.active_processing_streams.get(camera_id))
                        if has_proc_viewer:
                            viz_frame, _res = self._draw_motion_analysis_chart(stream_frame, points_dict, camera_id)
                        else:
                            viz_frame = stream_frame
                            bg_diff_int = 0
                            for p in points_dict.values():
                                if isinstance(p, dict) and 'bg_diff' in p:
                                    bg_diff_int = int(p.get('bg_diff', 0))
                                    break
                            _res = {
                                _id: {'vel': round(float(p.get('mean_vel', 0.0)), 2), 'bg_diff': bg_diff_int}
                                for _id, p in points_dict.items() if isinstance(p, dict) and _id != '_stats'
                            }
                        res = self._aggregate_motion_result(_res)
                        res['person_count'] = self._latest_person_stats.get(camera_id, {}).get('person_count', 0)
                        res['person_density'] = self._latest_person_stats.get(camera_id, {}).get('person_density', 0.0)
                        res['avg_person_conf'] = self._latest_person_stats.get(camera_id, {}).get('avg_person_conf', 0.0)
                        
                        with stream_lock:
                            self._latest_viz[camera_id] = viz_frame
                            self._latest_res_video[camera_id] = res
                            if camera_id in self._viz_ring_buffers:
                                self._viz_ring_buffers[camera_id].put_with_timestamp(viz_frame, result_ts)
                            if camera_id in self._results_ring_buffers:
                                self._results_ring_buffers[camera_id].put_video_only_with_timestamp(res, result_ts)
                            self._latest_pts_payload[camera_id] = flow_pts
                    else:
                        # No new detection — use cached results
                        with stream_lock:
                            viz_frame = self._latest_viz.get(camera_id)
                            res = self._latest_res_video.get(camera_id, {'vel': 0, 'bg_diff': 0, 'ts': time.time(), 'detected_vel': False, 'detection_bg_diff': False})
                            if viz_frame is None:
                                viz_frame = stream_frame
                                self._latest_viz[camera_id] = viz_frame
                            flow_pts = self._latest_pts_payload.get(camera_id)
                            
                    _det_ts = res.get('det_ts')
                    if _det_ts and (time.time() - _det_ts) > self.no_motion_slow_down_thr_sec:
                        time.sleep(self.no_motion_slow_down_delay_sec)
                    # Primary video stream: clean frame with FPS and optical flow overlays
                    frame_fps = self._update_loop_fps(f"{camera_id}:primary")
                    _tracker_fps = self._fps_stats.get(f"{camera_id}:tracker", {}).get("fps", 0.0)
                    _person_stats = self._latest_person_stats.get(camera_id, {})
                    primary_frame = self._drawing.draw_fps_overlay(stream_frame, frame_fps, tracker_fps=_tracker_fps, person_stats=_person_stats)
                    primary_frame = self._draw_optical_flow_overlay(primary_frame, camera_id, flow_pts)
                    # Encode once — all clients receive pre-encoded JPEG bytes (Q3)
                    frame_bytes = self.frame_to_bytes(primary_frame)
                    self._overlay_frame_ring_buffers[camera_id].put_with_timestamp(frame_bytes, frame_capture_time)
                    # Store for fallback path (thread-safe)
                    with video_lock:
                        self._latest_video_frame[camera_id] = frame_bytes
            except Exception:
                logger.exception(f"{Colors.RED} Video thread crashed for {camera_id}{Colors.RESET}")
            finally:
                logger.info(f"{Colors.YELLOW}Video thread exiting for {camera_id}{Colors.RESET}")
                    
        # Start thread
        thread = threading.Thread(target=_video_thread, daemon=True, name=f'video-bg-{camera_id}')
        thread.start()
        self._video_background_threads[camera_id] = thread
        
        logger.info(f"{Colors.GREEN}Video stream started for {camera_id}{Colors.RESET}")
        return True

    def stop_video_stream(self, camera_id: str, stop_recording: bool = True) -> bool:
        """Stop background video stream thread for camera."""

        # Stop any active recording
        if stop_recording and camera_id in self.active_recordings:
            self.stop_recording(camera_id)
            
        # Signal stop
        stop_event = self._video_stop_events.get(camera_id)
        if stop_event:
            stop_event.set()
        
        # Wait for thread to finish
        thread = self._video_background_threads.get(camera_id)
        if thread and thread.is_alive():
            thread.join(timeout=5)
        
        # Unregister consumers
        self.unregister_consumer(camera_id, f"video_stream_{camera_id}", ['overlay'])
        self.unregister_consumer(camera_id, f"proc_stream_{camera_id}", ['viz', 'results'])
        
        # Cleanup
        self._video_background_threads.pop(camera_id, None)
        self._video_stop_events.pop(camera_id, None)
        self._video_thread_locks.pop(camera_id, None)
        self._latest_video_frame.pop(camera_id, None)
        self.active_streams.pop(camera_id, None)
        
        logger.info(f"{Colors.YELLOW}Video stream stopped for {camera_id}{Colors.RESET}")
        
        # Stop video capture
        self.stop_video(camera_id)
        
        return True

    def stop_av_stream(self, camera_id: str):
        """Stop background video and audio streams."""
        try:
            # Stop background threads (they handle their own consumer cleanup)
            self.stop_video_stream(camera_id)
            self.stop_audio_stream(camera_id)
            
            logger.info(f"{Colors.YELLOW}Stopped AV streams for {camera_id}{Colors.RESET}")
                
        except Exception as error:
            logger.warning(f"{Colors.RED}Failed to stop AV streams for {camera_id}: {error}{Colors.RESET}")

    # =====================================================================
    # STREAM ENDPOINT GENERATORS
    # =====================================================================
    
    def generate_video_stream_endpoint(self, camera_id: str) -> Generator[bytes, None, None]:
        """Generate video stream by reading from background thread data."""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            logger.warning(f"{Colors.RED}Camera not found: {camera_id}{Colors.RESET}")
            return self.generate_failure_frame(f"Camera {camera_id} Not Found")
        
        # Create stream token and use pre-registered consumer
        stream_token = f"{time.time_ns()}:{threading.get_ident()}"
        consumer_id = f"video_stream_{camera_id}"  # Use camera-specific consumer ID
        previous_token = self.active_streams.get(camera_id)
        if previous_token:
            logger.info(f"{Colors.GREEN}Video stream connected for {camera_id}{Colors.RESET}")
        else:
            logger.info(f"{Colors.GREEN}Video stream started for {camera_id}{Colors.RESET}")
        self.active_streams[camera_id] = stream_token
        
        # Consumer already registered in start_av_stream - just use it
        
        # Background thread should already be running from /start endpoint
        if camera_id not in self._video_background_threads:
            logger.warning(f"{Colors.RED}No video thread for {camera_id} - starting one...{Colors.RESET}")
            success = self.start_video_stream(camera_id)
            if not success:
                logger.error(f"{Colors.RED}Failed to start video thread for {camera_id}{Colors.RESET}")
                yield self.generate_failure_frame("Failed to start video processing")
                return
            # Give the thread a moment to start producing frames
            time.sleep(0.5)
        
        consecutive_empty = 0
        max_empty = 300  # 30 seconds timeout (300 * 0.1s)
        last_yielded_frame = None
        
        # Stream loop
        while (self.active_streams.get(camera_id) == stream_token and 
               camera_id in self._video_background_threads):
            
            # Get pre-encoded JPEG bytes from SPMC ring buffer (Q3: single encode path)
            current_frame_bytes = self._get_spmc_data(camera_id, consumer_id, 'overlay')
            
            if current_frame_bytes is not None:
                yield current_frame_bytes
                last_yielded_frame = current_frame_bytes
                consecutive_empty = 0
            
            elif last_yielded_frame is not None:
                # Re-yield last frame to keep stream alive
                yield last_yielded_frame
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= max_empty:
                    logger.warning(f"{Colors.RED}No video data for {camera_id}, ending stream{Colors.RESET}")
                    break
                # Yield placeholder to keep connection alive
                time.sleep(0.1)  # Short wait before checking for next frame
                
        
        # Cleanup token (consumer stays registered for other streams)
        if self.active_streams.get(camera_id) == stream_token:
            self.active_streams.pop(camera_id, None)
        
        logger.info(f"{Colors.YELLOW}Video stream finished for {camera_id}{Colors.RESET}")
    
    def generate_processing_stream_endpoint(self, camera_id: str) -> Generator[bytes, None, None]:
        """Generate processed video stream from camera (visualization overlay)."""
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            logger.warning(f"{Colors.RED}Camera not found: {camera_id}{Colors.RESET}")
            yield self.generate_failure_frame(f"Camera {camera_id} Not Found")
            return

        # Create stream token and use pre-registered consumer
        stream_token = f"{time.time_ns()}:{threading.get_ident()}"
        consumer_id = f"proc_stream_{camera_id}"  # Use camera-specific consumer ID
        previous_token = self.active_processing_streams.get(camera_id)
        if previous_token:
            logger.info(f"{Colors.GREEN}Processing stream connected for {camera_id}{Colors.RESET}")
        else:
            logger.info(f"{Colors.GREEN}Processing stream started for {camera_id}{Colors.RESET}")
        self.active_processing_streams[camera_id] = stream_token
        
        # Ensure video background thread is running
        if camera_id not in self._video_background_threads:
            logger.warning(f"{Colors.RED}No processing thread for {camera_id} - start camera first{Colors.RESET}")
            yield self.generate_failure_frame("Camera not started - click start button first")
            return

        consecutive_empty = 0
        max_empty = 300  # 30 seconds timeout (300 * 0.1s)
        last_processed_frame = None
        
        while self.active_processing_streams.get(camera_id) == stream_token and camera_id in self._video_background_threads:
            # Use SPMC ring buffer access
            processed_frame = self._get_spmc_data(camera_id, consumer_id, 'viz')
            
            if processed_frame is not None:
                results = self._get_spmc_data(camera_id, consumer_id, 'results')
                res = results.get('video', {'vel': 0, 'bg_diff': 0, 'detected_vel': False, 'detection_bg_diff': False}) if results else {'vel': 0, 'bg_diff': 0, 'detected_vel': False, 'detection_bg_diff': False}
                consecutive_empty = 0
                output_frame = processed_frame.copy()
                processing_fps = self._update_loop_fps(f"{camera_id}:processing")
                output_frame = self._draw_stream_status_overlay(output_frame, camera_id, processing_fps, res=res)
                
                buffer = self.frame_to_bytes(output_frame)
                yield buffer
                last_processed_frame = buffer
                
            elif last_processed_frame is not None:
                # Re-yield last frame to keep stream alive
                yield last_processed_frame
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= max_empty:
                    logger.warning(f"{Colors.RED}No processed frames for {camera_id}, ending stream{Colors.RESET}")
                    break
                # Keep connection alive with small delay
                time.sleep(0.1)

        # Cleanup token (consumer stays registered for other streams)
        if self.active_processing_streams.get(camera_id) == stream_token:
            self.active_processing_streams.pop(camera_id, None)
        
        logger.info(f"{Colors.YELLOW}Processing stream finished for {camera_id}{Colors.RESET}")
            
            
    def generate_recorded_video_stream(self, recording_id: str) -> Generator[bytes, None, None]:
        """Generate video stream from recorded file."""
        # Get recording from database
        db_recording = self.db.get_recording(recording_id)
        if not db_recording:
            raise ValueError(f"Recording not found: {recording_id}")
        
        # Resolve to absolute path to avoid CWD issues
        file_path = db_recording['file_path']
        abs_path = os.path.abspath(file_path)
        
        # Retry opening in case the writer is finalizing the file
        cap = None
        for _ in range(5):
            cap = cv2.VideoCapture(abs_path)
            if cap.isOpened():
                break
            time.sleep(0.2)
        
        if not cap or not cap.isOpened():
            raise ValueError(f"Failed to open recording file: {abs_path}")
        
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    # End of video, loop back to start
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                
                # Resize frame if needed
                frame = self._resize_frame_for_streaming(frame)
                
                # Encode frame as JPEG
                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not ret:
                    continue
                
                frame_bytes = buffer.tobytes()
                
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                
                # Control playback speed
                time.sleep(1.0 / 30)  # 30 FPS
                
        except Exception as e:
            logger.error(f"Error streaming recording {recording_id}: {e}")
        finally:
            cap.release()

    def _resize_frame_for_streaming(self, frame, max_width: int = 640):
        """Resize frame for optimal streaming performance"""
        height, width = frame.shape[:2]
        
        if width > max_width:
            # Calculate new height to maintain aspect ratio
            ratio = max_width / width
            new_width = max_width
            new_height = int(height * ratio)
            frame = cv2.resize(frame, (new_width, new_height))
        
        return frame
    
    def generate_result_json_stream(self, camera_id: str) -> Generator[Dict[str, Union[int, float]], None, None]:
        """Generate JSON stream of processing results for a camera"""
        while self.ai_service.is_tracker_alive(camera_id):
            res = getattr(self, '_latest_res_video', {}).get(camera_id, None)
            if res is not None:
                yield json.dumps(res)
            time.sleep(1.0)  # Update every second