"""
Frame Buffer Management for High-Performance Video Streaming

This module provides frame buffering and ring buffer capabilities for
decoupling capture from processing and efficient multi-consumer data distribution.
"""

import threading
import time
from collections import deque
from typing import Optional, Dict, Any, Tuple
import numpy as np
import cv2
import logging

logger = logging.getLogger(__name__)


class SPMCRingBuffer:
    """
    Simple Single-Producer Multiple-Consumer ring buffer.
    Lock-free design for high-performance data distribution.
    """
    
    def __init__(self, capacity: int, enable_stats: bool = False):
        self.capacity = capacity
        self._buffer = [None] * capacity
        self._write_idx = 0
        self._consumer_cursors: Dict[str, int] = {}
        self._lock = threading.RLock()
        
        # Sequence number for detecting missed data (optional enhancement)
        self._sequence = 0
        
        # FPS Statistics
        self._stats_enabled = enable_stats
        self._producer_write_times = deque(maxlen=100)  # Last 100 writes
        self._consumer_read_times = {}  # consumer_id -> deque of read times
        self._last_producer_fps = 0.0
        self._last_consumer_fps = {}  # consumer_id -> fps
        self._last_stats_update = time.time()
        self._stats_update_interval = 1.0  # Update stats every second
    
    def put(self, item: Any) -> bool:
        """Put item in buffer (producer side)"""
        current_time = time.time()
        
        with self._lock:
            current_write_pos = self._write_idx
            
            # Handle slow consumers: if cursor == write_pos, they're too slow (standard practice)
            for consumer_id, cursor in list(self._consumer_cursors.items()):
                if cursor == current_write_pos:
                    # Consumer is too slow, skip them forward (standard overrun handling)
                    self._consumer_cursors[consumer_id] = (current_write_pos + 1) % self.capacity
                    
            # Write item and advance (simple and fast)
            self._buffer[current_write_pos] = item
            self._write_idx = (self._write_idx + 1) % self.capacity
            self._sequence += 1
            
            # Track producer write times for FPS calculation
            if self._stats_enabled:
                self._producer_write_times.append(current_time)
                self._update_stats_if_needed(current_time)
            
            return True
    
    def get(self, consumer_id: str) -> Optional[Any]:
        """Get next item for consumer (standard SPMC pattern)"""
        current_time = time.time()
        
        with self._lock:
            if consumer_id not in self._consumer_cursors:
                # New consumer starts at current write position (standard)
                self._consumer_cursors[consumer_id] = self._write_idx
                if self._stats_enabled:
                    self._consumer_read_times[consumer_id] = deque(maxlen=100)
                return None
            
            cursor = self._consumer_cursors[consumer_id]
            
            # No new data if cursor caught up to write position
            if cursor == self._write_idx:
                return None
            
            # Read item and advance cursor (simple!)
            item = self._buffer[cursor]
            self._consumer_cursors[consumer_id] = (cursor + 1) % self.capacity
            
            # Track stats
            if self._stats_enabled:
                if consumer_id not in self._consumer_read_times:
                    self._consumer_read_times[consumer_id] = deque(maxlen=100)
                self._consumer_read_times[consumer_id].append(current_time)
                self._update_stats_if_needed(current_time)
            
            return item
    
    def get_last_read_time(self, consumer_id: str) -> Optional[float]:
        """Get last read time for consumer"""
        with self._lock:
            if self._stats_enabled and consumer_id in self._consumer_read_times and len(self._consumer_read_times[consumer_id]) > 0:
                return self._consumer_read_times[consumer_id][-1]
        return None
    
    @property
    def last_write_time(self) -> Optional[float]:
        """Get last write time for producer"""
        with self._lock:
            if self._stats_enabled and len(self._producer_write_times) > 0:
                return self._producer_write_times[-1]
        return None
    
    def register_consumer(self, consumer_id: str) -> bool:
        """Register a new consumer (standard SPMC pattern)"""
        with self._lock:
            self._consumer_cursors[consumer_id] = self._write_idx  # Start at current position
            if self._stats_enabled:
                self._consumer_read_times[consumer_id] = deque(maxlen=100)
            return True
    
    def unregister_consumer(self, consumer_id: str) -> bool:
        """Remove consumer (standard cleanup)"""
        with self._lock:
            self._consumer_cursors.pop(consumer_id, None)
            if self._stats_enabled:
                self._consumer_read_times.pop(consumer_id, None)
                self._last_consumer_fps.pop(consumer_id, None)
            return True
    
    def enable_stats(self, enabled: bool = True):
        """Enable or disable FPS statistics tracking"""
        with self._lock:
            self._stats_enabled = enabled
            if enabled:
                # Initialize stats tracking
                for consumer_id in self._consumer_cursors.keys():
                    if consumer_id not in self._consumer_read_times:
                        self._consumer_read_times[consumer_id] = deque(maxlen=100)
            else:
                # Clear stats when disabled
                self._producer_write_times.clear()
                self._consumer_read_times.clear()
                self._last_consumer_fps.clear()
                self._last_producer_fps = 0.0
    
    def _update_stats_if_needed(self, current_time: float):
        """Update FPS statistics if enough time has passed"""
        if current_time - self._last_stats_update >= self._stats_update_interval:
            self._calculate_fps(current_time)
            self._last_stats_update = current_time
    
    def _calculate_fps(self, current_time: float):
        """Calculate FPS for producer and consumers"""
        # Calculate producer FPS
        if len(self._producer_write_times) >= 2:
            time_span = self._producer_write_times[-1] - self._producer_write_times[0]
            if time_span > 0:
                self._last_producer_fps = (len(self._producer_write_times) - 1) / time_span
        
        # Calculate consumer FPS for each consumer
        for consumer_id, read_times in self._consumer_read_times.items():
            if len(read_times) >= 2:
                time_span = read_times[-1] - read_times[0]
                if time_span > 0:
                    self._last_consumer_fps[consumer_id] = (len(read_times) - 1) / time_span
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current FPS statistics"""
        with self._lock:
            if not self._stats_enabled:
                return {'stats_enabled': False}
            
            # Force update stats
            self._calculate_fps(time.time())
            
            return {
                'stats_enabled': True,
                'capacity': self.capacity,
                'producer_fps': round(self._last_producer_fps, 2),
                'consumer_fps': {cid: round(fps, 2) for cid, fps in self._last_consumer_fps.items()},
                'active_consumers': list(self._consumer_cursors.keys()),
                'producer_write_count': len(self._producer_write_times),
                'consumer_read_counts': {cid: len(times) for cid, times in self._consumer_read_times.items()}
            }


class FrameRingBuffer(SPMCRingBuffer):
    """Ring buffer for video frames"""
    
    def __init__(self, capacity: int = 50, enable_stats: bool = False):
        super().__init__(capacity, enable_stats)
    
    def put(self, frame: np.ndarray) -> bool:
        """Store video frame"""
        return super().put(frame)


class AudioRingBuffer(SPMCRingBuffer):
    """Ring buffer for audio chunks"""
    
    def __init__(self, capacity: int = 200, enable_stats: bool = False):
        super().__init__(capacity, enable_stats)
    
    def put(self, audio_chunk: bytes) -> bool:
        """Store audio chunk"""
        return super().put(audio_chunk)


class ResultsRingBuffer(SPMCRingBuffer):
    """Ring buffer for processing results"""
    
    def __init__(self, capacity: int = 100, enable_stats: bool = False):
        super().__init__(capacity, enable_stats)
        self._last_combined = {'video': {}, 'audio': {}, 'timestamp': time.time()}
        self._lock = threading.RLock()
    
    def put(self, video_result: Dict[str, Any], audio_result: Dict[str, Any]) -> bool:
        """Store combined results"""
        with self._lock:
            combined = {
                'video': video_result,
                'audio': audio_result,
                'timestamp': time.time()
            }
            self._last_combined = combined
            return super().put(combined)
    
    def put_video_only(self, video_result: Dict[str, Any]) -> bool:
        """Update only video results, preserving existing audio results"""
        with self._lock:
            combined = {
                'video': video_result,
                'audio': self._last_combined.get('audio', {}),
                'timestamp': time.time()
            }
            self._last_combined = combined
            return super().put(combined)
    
    def put_audio_only(self, audio_result: Dict[str, Any]) -> bool:
        """Update only audio results, preserving existing video results"""
        with self._lock:
            combined = {
                'video': self._last_combined.get('video', {}),
                'audio': audio_result,
                'timestamp': time.time()
            }
            self._last_combined = combined
            return super().put(combined)


class FrameBuffer:
    """Simple frame buffer for smooth streaming"""
    
    def __init__(self, camera_id: str, max_size: int = 10):
        self.camera_id = camera_id
        self.max_size = max_size
        self._buffer = deque(maxlen=max_size)
        self._lock = threading.RLock()
        self._current_frame = None
        self._current_frame_bytes = None
    
    def add_frame(self, frame: np.ndarray) -> bool:
        """Add frame to buffer"""
        with self._lock:
            self._current_frame = frame.copy()
            self._current_frame_bytes = None  # Clear cached bytes
            if len(self._buffer) >= self.max_size:
                self._buffer.popleft()
            self._buffer.append(frame.copy())
        return True
    
    def get_latest_frame(self) -> Optional[np.ndarray]:
        """Get most recent frame"""
        with self._lock:
            if self._current_frame is not None:
                return self._current_frame.copy()
        return None
    
    def get_latest_frame_bytes(self, jpeg_quality: int = 70) -> Optional[bytes]:
        """Get most recent frame as JPEG bytes"""
        with self._lock:
            if self._current_frame is None:
                return None
            
            # Use cached bytes if available
            if self._current_frame_bytes is not None:
                return self._current_frame_bytes
            
            # Convert to JPEG
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
            success, buffer = cv2.imencode('.jpg', self._current_frame, encode_params)
            
            if success:
                self._current_frame_bytes = buffer.tobytes()
                return self._current_frame_bytes
        return None
    
    def clear(self):
        """Clear buffer"""
        with self._lock:
            self._buffer.clear()
            self._current_frame = None
            self._current_frame_bytes = None


class FrameBufferManager:
    """Manager for camera frame buffers"""
    
    def __init__(self):
        self._buffers: Dict[str, FrameBuffer] = {}
        self._lock = threading.Lock()
    
    def create_buffer(self, camera_id: str, max_size: int = 10, target_fps: int = 30) -> FrameBuffer:
        """Create frame buffer for camera"""
        with self._lock:
            if camera_id not in self._buffers:
                self._buffers[camera_id] = FrameBuffer(camera_id, max_size)
        return self._buffers[camera_id]
    
    def get_buffer(self, camera_id: str) -> Optional[FrameBuffer]:
        """Get buffer for camera"""
        return self._buffers.get(camera_id)
    
    def remove_buffer(self, camera_id: str) -> bool:
        """Remove buffer for camera"""
        with self._lock:
            if camera_id in self._buffers:
                self._buffers[camera_id].clear()
                del self._buffers[camera_id]
                return True
        return False