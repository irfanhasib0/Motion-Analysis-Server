"""WebSocket broadcast manager for live video streaming.

Manages per-camera broadcast loops that read pre-encoded JPEG bytes from
the SPMC overlay ring buffer and push them to all connected WebSocket clients.
Each camera has at most one broadcast thread; N clients share that single
producer via asyncio.Queue fan-out.

Architecture (designed to parallel hls_manager.py and allow future
webrtc_manager.py to follow the same pattern):

    _video_thread  ──encode──▶  overlay ring buffer (raw JPEG bytes)
                                        │
                                        ▼
                            WSStreamingManager._broadcast_loop  (1 thread/camera)
                                        │
                    ┌───────────────────┼───────────────────────┐
                    ▼                   ▼                       ▼
              client queue 1      client queue 2         client queue N
              (asyncio.Queue)     (asyncio.Queue)        (asyncio.Queue)
                    │                   │                       │
                    ▼                   ▼                       ▼
             WebSocket send       WebSocket send         WebSocket send
"""

import asyncio
import threading
import time
import logging
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)


class WSStreamingManager:
    """Manages WebSocket broadcast for live camera video streams.

    Parameters
    ----------
    streaming_service : Any
        Reference to the parent StreamingService (used for _get_spmc_data,
        register_consumer, unregister_consumer, start_av_stream).
    register_consumer : Callable
        Callback to register an SPMC ring buffer consumer.
    """

    def __init__(
        self,
        streaming_service: Any,
        register_consumer: Callable,
    ):
        self._ss = streaming_service
        self._register_consumer = register_consumer

        # Per-camera state
        self._rooms: Dict[str, Set[asyncio.Queue]] = {}       # camera_id -> set of client queues
        self._room_locks: Dict[str, threading.Lock] = {}      # protects _rooms[camera_id]
        self._broadcast_threads: Dict[str, threading.Thread] = {}
        self._broadcast_stop: Dict[str, threading.Event] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, camera_id: str) -> asyncio.Queue:
        """Add a WebSocket client to a camera's broadcast room.

        Returns an asyncio.Queue that will receive raw JPEG bytes for each
        new frame. The queue has ``maxsize=2`` — if the client is too slow,
        the oldest frame is silently dropped.
        """
        lock = self._room_locks.setdefault(camera_id, threading.Lock())
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        with lock:
            if camera_id not in self._rooms:
                self._rooms[camera_id] = set()
            self._rooms[camera_id].add(q)

        # Start broadcast loop if not already running
        if camera_id not in self._broadcast_threads or not self._broadcast_threads[camera_id].is_alive():
            self._start_broadcast(camera_id)

        logger.info(f"WS client subscribed to {camera_id} "
                     f"(viewers: {len(self._rooms.get(camera_id, set()))})")
        return q

    def unsubscribe(self, camera_id: str, q: asyncio.Queue) -> None:
        """Remove a WebSocket client from a camera's broadcast room."""
        lock = self._room_locks.get(camera_id)
        if lock is None:
            return
        with lock:
            room = self._rooms.get(camera_id)
            if room:
                room.discard(q)
                remaining = len(room)
            else:
                remaining = 0

        logger.info(f"WS client unsubscribed from {camera_id} "
                     f"(viewers: {remaining})")

        # Stop broadcast loop if last subscriber left
        if remaining == 0:
            self._stop_broadcast(camera_id)

    def stop_all(self) -> None:
        """Stop all broadcast threads (called during shutdown)."""
        for camera_id in list(self._broadcast_threads):
            self._stop_broadcast(camera_id)

    # ------------------------------------------------------------------
    # Broadcast loop
    # ------------------------------------------------------------------

    def _start_broadcast(self, camera_id: str) -> None:
        """Start the single broadcast thread for *camera_id*."""
        # Clean up dead thread if any
        self._stop_broadcast(camera_id)

        stop_evt = threading.Event()
        self._broadcast_stop[camera_id] = stop_evt

        consumer_id = f"ws_broadcast_{camera_id}"
        self._register_consumer(camera_id, consumer_id, ['overlay'])

        def _broadcast_loop() -> None:
            try:
                logger.info(f"WS broadcast loop started for {camera_id}")
                while not stop_evt.is_set():
                    frame_bytes = self._ss._get_spmc_data(camera_id, consumer_id, 'overlay')
                    if frame_bytes is None:
                        # No new frame yet — short sleep to avoid busy-wait
                        time.sleep(0.03)
                        continue

                    # Strip MJPEG multipart headers if present so WS clients
                    # receive pure JPEG bytes.
                    jpeg = self._strip_mjpeg_headers(frame_bytes)

                    lock = self._room_locks.get(camera_id)
                    if lock is None:
                        break
                    with lock:
                        room = self._rooms.get(camera_id)
                        if not room:
                            break
                        dead = []
                        for q in room:
                            try:
                                q.put_nowait(jpeg)
                            except asyncio.QueueFull:
                                # Drop oldest frame for slow client
                                try:
                                    q.get_nowait()
                                    q.put_nowait(jpeg)
                                except Exception:
                                    dead.append(q)
                        for q in dead:
                            room.discard(q)
            except Exception:
                logger.exception(f"WS broadcast loop crashed for {camera_id}")
            finally:
                # Unregister consumer
                try:
                    self._ss.unregister_consumer(camera_id, consumer_id, ['overlay'])
                except Exception:
                    pass
                logger.info(f"WS broadcast loop stopped for {camera_id}")

        t = threading.Thread(target=_broadcast_loop, daemon=True,
                             name=f'ws-broadcast-{camera_id}')
        t.start()
        self._broadcast_threads[camera_id] = t

    def _stop_broadcast(self, camera_id: str) -> None:
        """Signal and join the broadcast thread for *camera_id*."""
        evt = self._broadcast_stop.pop(camera_id, None)
        if evt:
            evt.set()
        t = self._broadcast_threads.pop(camera_id, None)
        if t and t.is_alive():
            t.join(timeout=3)
        # Don't clear the room — clients may reconnect

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_mjpeg_headers(data: bytes) -> bytes:
        """Return raw JPEG bytes, stripping MJPEG multipart framing if present.

        The overlay ring buffer may contain either:
        - Raw JPEG bytes (starts with 0xFF 0xD8)
        - MJPEG multipart frame: ``--frame\\r\\nContent-Type: image/jpeg\\r\\n\\r\\n<jpeg>\\r\\n``

        This method handles both transparently.
        """
        if data[:2] == b'\xff\xd8':
            return data
        # Find JPEG SOI after the multipart headers
        idx = data.find(b'\xff\xd8')
        if idx < 0:
            return data  # can't parse — return as-is
        # Strip trailing \r\n added by multipart framing
        jpeg = data[idx:]
        if jpeg.endswith(b'\r\n'):
            jpeg = jpeg[:-2]
        return jpeg
