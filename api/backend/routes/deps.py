"""Shared dependencies for API route modules.

Attributes are set by start_server.py during application initialization,
before any request is served.
"""

# Service instances — set by start_server.py at startup
camera_service = None
system_service = None
reid_service = None
logger = None

# Mutable shared state
_live_stream_mode = "mjpeg"


def get_live_stream_mode():
    return _live_stream_mode


def set_live_stream_mode(mode):
    global _live_stream_mode
    _live_stream_mode = mode


# Async broadcast function — set by start_server.py after app creation
broadcast_message = None
