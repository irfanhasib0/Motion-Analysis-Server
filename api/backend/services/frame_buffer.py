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
    Timestamped Single-Producer Multiple-Consumer ring buffer.
    Uses write timestamps to ensure consumers only get fresh data,
    automatically handling speed discrepancies.
    """
    
    def __init__(self, capacity: int, enable_stats: bool = False):
        self.capacity = capacity
        self._buffer = [(None, 0.0)] * capacity  # (item, write_timestamp)
        self._write_idx = 0
        self._consumer_cursors: Dict[str, int] = {}
        self._consumer_last_timestamps: Dict[str, float] = {}  # Track last read timestamp
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
        """Put item in buffer with write timestamp (producer side)"""
        current_time = time.time()
        
        with self._lock:
            # Store item with timestamp for freshness checking
            self._buffer[self._write_idx] = (item, current_time)
            self._write_idx = (self._write_idx + 1) % self.capacity
            self._sequence += 1
            
            # Track producer write times for FPS calculation
            if self._stats_enabled:
                self._producer_write_times.append(current_time)
                self._update_stats_if_needed(current_time)
            
            return True
    
    def put_with_timestamp(self, item: Any, capture_timestamp: float) -> bool:
        """Put item in buffer with explicit capture timestamp for chronological ordering"""
        with self._lock:
            # Store item with provided capture timestamp instead of write time
            self._buffer[self._write_idx] = (item, capture_timestamp)
            self._write_idx = (self._write_idx + 1) % self.capacity
            self._sequence += 1
            
            # Track producer write times for FPS calculation (use current time for stats)
            if self._stats_enabled:
                self._producer_write_times.append(time.time())
                self._update_stats_if_needed(time.time())
            
            return True
    
    def get_latest(self, consumer_id: str) -> Optional[Any]:
        """Get most recent item immediately (for video streaming)"""
        with self._lock:
            if self._sequence == 0:
                return None  # Buffer empty
            
            # Get most recent item with its timestamp
            recent_pos = (self._write_idx - 1) % self.capacity
            item, write_timestamp = self._buffer[recent_pos]
            
            # Update consumer's last seen timestamp to most recent
            self._consumer_cursors[consumer_id] = self._write_idx
            self._consumer_last_timestamps[consumer_id] = write_timestamp
            
            if self._stats_enabled:
                if consumer_id not in self._consumer_read_times:
                    self._consumer_read_times[consumer_id] = deque(maxlen=100)
                self._consumer_read_times[consumer_id].append(time.time())
            
            return item
    
    def get(self, consumer_id: str) -> Optional[Any]:
        """Get next fresh item for consumer using timestamp-based freshness checking"""
        current_time = time.time()
        
        with self._lock:
            if consumer_id not in self._consumer_cursors:
                # New consumer: start at current write position for fresh data
                self._consumer_cursors[consumer_id] = self._write_idx
                self._consumer_last_timestamps[consumer_id] = -1.0  # Initialize to get first item
                if self._stats_enabled:
                    self._consumer_read_times[consumer_id] = deque(maxlen=100)
                return None  # Wait for next fresh data
            
            cursor = self._consumer_cursors[consumer_id]
            last_timestamp = self._consumer_last_timestamps[consumer_id]
            
            # Check current cursor position for fresh data
            if cursor != self._write_idx:  # There's data at cursor position
                item, write_timestamp = self._buffer[cursor]
                
                # Timestamp freshness check - only return if newer than last read
                if item is not None and write_timestamp > last_timestamp:
                    # Fresh data found!
                    self._consumer_cursors[consumer_id] = (cursor + 1) % self.capacity
                    self._consumer_last_timestamps[consumer_id] = write_timestamp
                    
                    # Track stats
                    if self._stats_enabled:
                        if consumer_id not in self._consumer_read_times:
                            self._consumer_read_times[consumer_id] = deque(maxlen=100)
                        self._consumer_read_times[consumer_id].append(current_time)
                        self._update_stats_if_needed(current_time)
                    
                    return item
                else:
                    # Data at cursor is stale (timestamp <= last_timestamp)
                    # Advance cursor to look for fresher data (help slow consumers catch up)
                    self._consumer_cursors[consumer_id] = (cursor + 1) % self.capacity
                    return None  # Return None, but cursor advanced for next attempt
            
            # No new data available (cursor caught up to write_idx)
            return None
    
    def peek(self, consumer_id: str) -> Optional[Any]:
        """Peek at next fresh item for consumer without advancing cursor"""
        with self._lock:
            if consumer_id not in self._consumer_cursors:
                # New consumer: register and wait for fresh data
                self._consumer_cursors[consumer_id] = self._write_idx
                self._consumer_last_timestamps[consumer_id] = -1.0
                if self._stats_enabled:
                    self._consumer_read_times[consumer_id] = deque(maxlen=100)
                return None
            
            cursor = self._consumer_cursors[consumer_id]
            last_timestamp = self._consumer_last_timestamps[consumer_id]
            
            # Check current cursor position for fresh data (no advance)
            if cursor != self._write_idx:
                item, write_timestamp = self._buffer[cursor]
                
                # Return item only if timestamp indicates freshness
                if item is not None and write_timestamp > last_timestamp:
                    return item
            
            return None
    
    def get_last_read_time(self, consumer_id: str) -> Optional[float]:
        """Get last read time for consumer (stats-based)"""
        with self._lock:
            if self._stats_enabled and consumer_id in self._consumer_read_times and len(self._consumer_read_times[consumer_id]) > 0:
                return self._consumer_read_times[consumer_id][-1]
        return None
    
    def get_last_read_timestamp(self, consumer_id: str) -> Optional[float]:
        """Get last read data timestamp for consumer (freshness-based)"""
        with self._lock:
            return self._consumer_last_timestamps.get(consumer_id)
    
    @property
    def last_write_time(self) -> Optional[float]:
        """Get last write time for producer (stats-based)"""
        with self._lock:
            if self._stats_enabled and len(self._producer_write_times) > 0:
                return self._producer_write_times[-1]
        return None
    
    @property  
    def last_write_timestamp(self) -> Optional[float]:
        """Get timestamp of most recent data written"""
        with self._lock:
            if self._sequence > 0:
                recent_pos = (self._write_idx - 1) % self.capacity
                _, timestamp = self._buffer[recent_pos]
                return timestamp
        return None
    
    def register_consumer(self, consumer_id: str) -> bool:
        """Register a new consumer for timestamped buffer"""
        with self._lock:
            # New consumers wait at write position for fresh data
            self._consumer_cursors[consumer_id] = self._write_idx
            self._consumer_last_timestamps[consumer_id] = -1.0  # Initialize to get first item
            if self._stats_enabled:
                self._consumer_read_times[consumer_id] = deque(maxlen=100)
            return True
    
    def unregister_consumer(self, consumer_id: str) -> bool:
        """Remove consumer and cleanup timestamp tracking"""
        with self._lock:
            self._consumer_cursors.pop(consumer_id, None)
            self._consumer_last_timestamps.pop(consumer_id, None)  # Cleanup timestamp tracking
            if self._stats_enabled:
                self._consumer_read_times.pop(consumer_id, None)
                self._last_consumer_fps.pop(consumer_id, None)
            return True
    
    def enable_stats(self, enabled: bool = True):
        """Enable or disable FPS statistics tracking"""
        with self._lock:
            self._stats_enabled = enabled
            if enabled:
                # Initialize stats tracking for existing consumers
                for consumer_id in self._consumer_cursors.keys():
                    if consumer_id not in self._consumer_read_times:
                        self._consumer_read_times[consumer_id] = deque(maxlen=100)
            else:
                # Clear stats when disabled (but keep timestamp tracking for functionality)
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
        """Get current FPS statistics and timestamp info"""
        with self._lock:
            if not self._stats_enabled:
                return {
                    'stats_enabled': False,
                    'consumer_timestamps': {cid: round(ts, 3) for cid, ts in self._consumer_last_timestamps.items()}
                }
            
            # Force update stats
            self._calculate_fps(time.time())
            
            return {
                'stats_enabled': True,
                'capacity': self.capacity,
                'producer_fps': round(self._last_producer_fps, 2),
                'consumer_fps': {cid: round(fps, 2) for cid, fps in self._last_consumer_fps.items()},
                'active_consumers': list(self._consumer_cursors.keys()),
                'consumer_timestamps': {cid: round(ts, 3) for cid, ts in self._consumer_last_timestamps.items()},
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
    
    def put_with_timestamp(self, video_result: Dict[str, Any], audio_result: Dict[str, Any], capture_timestamp: float) -> bool:
        """Store combined results with explicit capture timestamp"""
        with self._lock:
            combined = {
                'video': video_result,
                'audio': audio_result,
                'timestamp': capture_timestamp
            }
            self._last_combined = combined
            return super().put_with_timestamp(combined, capture_timestamp)
    
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
    
    def put_video_only_with_timestamp(self, video_result: Dict[str, Any], capture_timestamp: float) -> bool:
        """Update only video results with explicit capture timestamp"""
        with self._lock:
            combined = {
                'video': video_result,
                'audio': self._last_combined.get('audio', {}),
                'timestamp': capture_timestamp
            }
            self._last_combined = combined
            return super().put_with_timestamp(combined, capture_timestamp)
    
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
    
    def put_audio_only_with_timestamp(self, audio_result: Dict[str, Any], capture_timestamp: float) -> bool:
        """Update only audio results with explicit capture timestamp"""
        with self._lock:
            combined = {
                'video': self._last_combined.get('video', {}),
                'audio': audio_result,
                'timestamp': capture_timestamp
            }
            self._last_combined = combined
            return super().put_with_timestamp(combined, capture_timestamp)


class SimpleMutexBuffer:
    """
    Ultra-simple mutex-based buffer (Option 3).
    Bulletproof implementation that always works regardless of producer/consumer speeds.
    Supports both streaming (latest frame) and recording (bounded sequential) modes.
    """
    
    def __init__(self, recording_buffer_size: int = 100, enable_stats: bool = False):
        self._latest_item = (None, 0)  # (item, sequence_number)
        self._recording_buffer = deque(maxlen=recording_buffer_size)  # For sequential access
        self._consumer_last_seen = {}  # consumer_id -> last_sequence_seen
        self._sequence = 0
        self._lock = threading.RLock()
        
        # Statistics (optional)
        self._stats_enabled = enable_stats
        self._put_count = 0
        self._get_count = 0
        self._recording_drops = 0
        self._last_put_time = 0.0
        
    def put(self, item: Any) -> bool:
        """Put item in buffer (producer side) - always succeeds"""
        current_time = time.time()
        
        with self._lock:
            self._sequence += 1
            self._latest_item = (item, self._sequence)
            
            # Also store in recording buffer (bounded, drops old data if full)
            if len(self._recording_buffer) >= self._recording_buffer.maxlen:
                self._recording_drops += 1
            self._recording_buffer.append((item, self._sequence))
            
            # Update stats
            if self._stats_enabled:
                self._put_count += 1
                self._last_put_time = current_time
                
        return True
    
    def get(self, consumer_id: str, mode: str = 'streaming') -> Optional[Any]:
        """
        Get item for consumer.
        
        Args:
            consumer_id: Unique identifier for consumer
            mode: 'streaming' for latest frame, 'recording' for sequential frames
            
        Returns:
            Item or None if no data available
        """
        with self._lock:
            if mode == 'streaming':
                # Always return latest item - perfect for video streaming
                item, sequence = self._latest_item
                if item is not None:
                    # Track what this consumer has seen
                    self._consumer_last_seen[consumer_id] = sequence
                    if self._stats_enabled:
                        self._get_count += 1
                return item
                
            elif mode == 'recording':
                # Sequential access for recording - get oldest unread
                if not self._recording_buffer:
                    return None
                    
                # Find first unread item for this consumer
                last_seen = self._consumer_last_seen.get(consumer_id, 0)
                
                # Look for next sequence number
                for i, (item, sequence) in enumerate(self._recording_buffer):
                    if sequence > last_seen:
                        # Found next unread item
                        self._consumer_last_seen[consumer_id] = sequence
                        if self._stats_enabled:
                            self._get_count += 1
                        return item
                
                # All items in buffer are old/already read
                return None
                
            else:
                raise ValueError(f"Invalid mode '{mode}'. Use 'streaming' or 'recording'")
    
    def get_latest(self, consumer_id: str) -> Optional[Any]:
        """Get latest item immediately (alias for get with streaming mode)"""
        return self.get(consumer_id, mode='streaming')
    
    def peek(self, consumer_id: str, mode: str = 'streaming') -> Optional[Any]:
        """Peek at next item without advancing consumer position"""
        with self._lock:
            if mode == 'streaming':
                return self._latest_item[0]
            elif mode == 'recording':
                if not self._recording_buffer:
                    return None
                last_seen = self._consumer_last_seen.get(consumer_id, 0)
                for item, sequence in self._recording_buffer:
                    if sequence > last_seen:
                        return item
                return None
            else:
                raise ValueError(f"Invalid mode '{mode}'. Use 'streaming' or 'recording'")
    
    def register_consumer(self, consumer_id: str, mode: str = 'streaming') -> bool:
        """Register a new consumer"""
        with self._lock:
            if mode == 'streaming':
                # For streaming, start with latest sequence to get fresh data immediately
                self._consumer_last_seen[consumer_id] = self._sequence
            elif mode == 'recording':
                # For recording, start from current position to not miss new data
                self._consumer_last_seen[consumer_id] = self._sequence
            return True
    
    def unregister_consumer(self, consumer_id: str) -> bool:
        """Remove consumer"""
        with self._lock:
            self._consumer_last_seen.pop(consumer_id, None)
            return True
    
    def clear(self):
        """Clear all data"""
        with self._lock:
            self._latest_item = (None, 0)
            self._recording_buffer.clear()
            self._consumer_last_seen.clear()
            self._sequence = 0
            if self._stats_enabled:
                self._put_count = 0
                self._get_count = 0
                self._recording_drops = 0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get buffer statistics"""
        with self._lock:
            if not self._stats_enabled:
                return {'stats_enabled': False}
            
            fps = 0.0
            if self._last_put_time > 0:
                elapsed = time.time() - self._last_put_time + 0.001  # Avoid division by zero
                if elapsed < 60:  # Only calculate FPS for recent data
                    fps = min(self._put_count, 100) / min(elapsed, 60)  # Rough FPS estimate
            
            return {
                'stats_enabled': True,
                'latest_sequence': self._sequence,
                'recording_buffer_size': len(self._recording_buffer),
                'recording_buffer_capacity': self._recording_buffer.maxlen,
                'recording_drops': self._recording_drops,
                'active_consumers': list(self._consumer_last_seen.keys()),
                'put_count': self._put_count,
                'get_count': self._get_count,
                'estimated_fps': round(fps, 2)
            }


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