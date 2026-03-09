from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import Optional, List, Dict
import asyncio
import json
import os
from datetime import datetime
import logging
import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote_plus

from services.camera_service import CameraService
from services.dashboard_service import DashboardService
from models.camera import Camera, CameraCreate, CameraUpdate
from models.recording import Recording, RecordingCreate

user = os.popen('uname -n').read().strip()
if user == 'irfan-linux':
    configs = 'pc'
elif user == 'raspberrypi':
    configs = 'pi'
else:
    configs = 'default'

print("Running with config:", configs)

# Initialize services
camera_service = CameraService(configs=configs)
dashboard_service = DashboardService(camera_service=camera_service)
    
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
AUTH_PASSWORD = os.getenv("API_PASSWORD", "admin123")
AUTH_TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "86400"))
AUTH_SECRET = os.getenv("AUTH_SECRET", secrets.token_hex(32))

# Load LIVE_STREAM_MODE: env var takes priority, then system.yaml, then default
_sys_boot = camera_service.db.get_system_settings()
_live_stream_env = os.getenv("LIVE_STREAM_MODE")
LIVE_STREAM_MODE = (_live_stream_env if _live_stream_env in {"mjpeg", "hls"}
                    else str(_sys_boot.get('live_stream_mode', 'mjpeg')))
if LIVE_STREAM_MODE not in {"mjpeg", "hls"}:
    LIVE_STREAM_MODE = "mjpeg"

# Load UVICORN_RELOAD: env var takes priority, then system.yaml, then default
_uvicorn_env = os.getenv("UVICORN_RELOAD")
if _uvicorn_env is not None:
    UVICORN_RELOAD = _uvicorn_env.lower() in {"1", "true", "yes", "on"}
else:
    UVICORN_RELOAD = bool(_sys_boot.get('uvicorn_reload', True))

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="NVR Server", description="Network Video Recorder with RTSP and Camera Support")

# Mount static files for React app only if build directory exists
frontend_build_path = "frontend/build"
frontend_static_path = "frontend/build/static"
if os.path.exists(frontend_static_path):
    app.mount("/static", StaticFiles(directory=frontend_static_path), name="static")
    logger.info("Frontend static files mounted")
else:
    logger.warning("Frontend build directory not found. Run 'cd frontend && npm install && npm run build' to build the frontend.")

# WebSocket connections for real-time updates
active_connections: Dict[str, WebSocket] = {}
last_camera_status: Dict[str, str] = {}  # Track last known camera status

api_key_scheme = APIKeyHeader(name="x-api-password", auto_error=False)

class LoginRequest(BaseModel):
    password: str


class LiveStreamModeRequest(BaseModel):
    mode: str


class CameraSensitivityRequest(BaseModel):
    sensitivity: int


class SystemSettingsUpdateRequest(BaseModel):
    live_stream_mode: Optional[str] = None
    low_power_mode: Optional[bool] = None
    sensitivity: Optional[int] = None
    jpeg_quality: Optional[int] = None
    pipe_buffer_size: Optional[int] = None
    max_vel: Optional[float] = None
    bg_diff: Optional[int] = None
    max_clip_length: Optional[int] = None
    motion_check_interval: Optional[int] = None
    min_free_storage_bytes: Optional[int] = None
    rtsp_unified_demux_enabled: Optional[bool] = None
    uvicorn_reload: Optional[bool] = None


class RecordingMetaUpdate(BaseModel):
    label: Optional[str] = None
    note: Optional[str] = None


class ArchiveExportRequest(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    min_vel: Optional[float] = None
    min_diff: Optional[float] = None
    min_duration: Optional[float] = None
    delete_after: bool = False
    exclude_mode: bool = True
    label_filter: Optional[List[str]] = None


class ArchivePathRequest(BaseModel):
    archive_path: str

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_access_token() -> str:
    payload = {
        "exp": int(time.time()) + AUTH_TOKEN_TTL_SECONDS,
        "scope": "api",
    }
    payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_part = _b64url(payload_str.encode("utf-8"))
    signature = hmac.new(AUTH_SECRET.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256).digest()
    signature_part = _b64url(signature)
    return f"{payload_part}.{signature_part}"


def verify_access_token(token: str) -> bool:
    try:
        payload_part, signature_part = token.split(".", 1)
        expected_signature = hmac.new(
            AUTH_SECRET.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256
        ).digest()
        provided_signature = _b64url_decode(signature_part)
        if not hmac.compare_digest(expected_signature, provided_signature):
            return False

        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return False
        return payload.get("scope") == "api"
    except Exception:
        return False


async def legacy_auth_header_valid(request: Request) -> bool:
    password_header = await api_key_scheme(request)
    return bool(password_header and password_header == AUTH_PASSWORD)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not AUTH_ENABLED:
        return await call_next(request)

    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)

    public_paths = {"/api/health", "/api/auth/login"}
    if path in public_paths:
        return await call_next(request)
    
    # Optional: Do not remove this commented block
    #auth_header = request.headers.get("Authorization", "")
    #if auth_header.startswith("Bearer "):
    #    token = auth_header.replace("Bearer ", "", 1).strip()
    #    if verify_access_token(token):
    #        return await call_next(request)
    
    auth_header = request.headers.get("Authorization", "")
    bearer_token = ""
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header.replace("Bearer ", "", 1).strip()
    if bearer_token and verify_access_token(bearer_token):
        return await call_next(request)

    query_token = request.query_params.get("access_token")
    if query_token:
        query_token = query_token.split("?", 1)[0]
    if query_token and verify_access_token(query_token):
        return await call_next(request)
    
    # Optional: Do not remove this commented block
    #password_header = request.headers.get("x-api-password")
    #if password_header and password_header == AUTH_PASSWORD:
    #    return await call_next(request)
    if await legacy_auth_header_valid(request):
        return await call_next(request)

    return Response(
        content=json.dumps({"detail": "Unauthorized"}),
        status_code=401,
        media_type="application/json",
    )

async def check_camera_status_changes():
    """Background task to check for camera status changes and broadcast updates"""
    global last_camera_status
    while True:
        try:
            cameras = camera_service.get_cameras()
            for camera in cameras:
                current_status = camera.status.value
                last_status = last_camera_status.get(camera.id)
                
                if last_status != current_status:
                    last_camera_status[camera.id] = current_status
                    await broadcast_message({
                        "type": "camera_status_updated",
                        "camera_id": camera.id,
                        "status": current_status,
                        "camera": camera.dict()
                    })
                    logger.info(f"Camera {camera.id} status changed to {current_status}")
                    
        except Exception as e:
            logger.error(f"Error checking camera status: {e}")
        
        # Check every 2 seconds
        await asyncio.sleep(2)

# Start the background task
@app.on_event("startup")
async def startup_event():
    """Initialize background tasks"""
    # Initialize camera status tracking
    cameras = camera_service.get_cameras()
    for camera in cameras:
        last_camera_status[camera.id] = camera.status.value
    
    # Start status checking task
    asyncio.create_task(check_camera_status_changes())
    logger.info("Started camera status monitoring")

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    active_connections[client_id] = websocket
    try:
        while True:
            data = await websocket.receive_text()
            # Handle WebSocket messages if needed
    except WebSocketDisconnect:
        del active_connections[client_id]

async def broadcast_message(message: dict):
    """Broadcast message to all connected WebSocket clients"""
    for connection in active_connections.values():
        try:
            await connection.send_text(json.dumps(message))
        except:
            pass

# Camera management endpoints
@app.post("/api/auth/login")
async def login(payload: LoginRequest):
    if not AUTH_ENABLED:
        return {
            "access_token": "",
            "token_type": "bearer",
            "expires_in": 0,
            "auth_enabled": False,
        }

    if payload.password != AUTH_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")

    return {
        "access_token": create_access_token(),
        "token_type": "bearer",
        "expires_in": AUTH_TOKEN_TTL_SECONDS,
        "auth_enabled": True,
    }


@app.get("/api/cameras", response_model=List[Camera])
async def get_cameras():
    """Get all cameras"""
    return camera_service.get_cameras()

@app.post("/api/cameras", response_model=Camera)
async def create_camera(camera: CameraCreate):
    """Add a new camera"""
    try:
        new_camera = camera_service.add_camera(camera)
        await broadcast_message({"type": "camera_added", "camera": new_camera.dict()})
        return new_camera
    except Exception as e:
        logger.error(f"Failed to add camera: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.put("/api/cameras/{camera_id}", response_model=Camera)
async def update_camera(camera_id: str, camera_update: CameraUpdate):
    """Update camera settings"""
    try:
        updated_camera = camera_service.update_camera(camera_id, camera_update)
        await broadcast_message({"type": "camera_updated", "camera": updated_camera.dict()})
        return updated_camera
    except Exception as e:
        logger.error(f"Failed to update camera: {e}")
        raise HTTPException(status_code=404, detail=str(e))

@app.delete("/api/cameras/{camera_id}")
async def delete_camera(camera_id: str):
    """Delete a camera"""
    try:
        camera_service.remove_camera(camera_id)
        await broadcast_message({"type": "camera_deleted", "camera_id": camera_id})
        return {"message": "Camera deleted successfully"}
    except Exception as e:
        logger.error(f"Failed to delete camera: {e}")
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/api/cameras/{camera_id}/start")
async def start_camera(camera_id: str):
    """Start camera with both video and audio streams (unified approach)"""
    success = camera_service.start_camera(camera_id)
    if success:
        # Also start background audio/video streaming threads if camera started successfully
        await asyncio.to_thread(camera_service.start_av_stream, camera_id)
        #await broadcast_message({"type": "camera_started", "camera_id": camera_id})
        return {"message": "Camera and streaming started successfully"}
    else:
        camera_service.close_camera_stream(camera_id)
        success = camera_service.start_camera(camera_id)
    if success:
        # Also start background streaming on retry
        await asyncio.to_thread(camera_service.start_av_stream, camera_id)
        #await broadcast_message({"type": "camera_started", "camera_id": camera_id})
        return {"message": "Camera and streaming started successfully on retry"}
    else:
        raise HTTPException(status_code=400, detail="Failed to start camera - camera may be unavailable or in use")
    

@app.post("/api/cameras/{camera_id}/stop")
async def stop_camera(camera_id: str):
    """Stop camera and all associated streams (video, audio, HLS, recordings)"""
    camera_service.close_camera_stream(camera_id)
    #await broadcast_message({"type": "camera_stopped", "camera_id": camera_id})
    return {"message": "Camera and all streams stopped successfully"}

# Recording management endpoints
@app.post("/api/cameras/{camera_id}/start-recording")
async def start_recording(camera_id: str, background_tasks: BackgroundTasks):
    """Start recording from a camera"""
    logger.info(f"Start recording request for camera: {camera_id}")
    if camera_id not in camera_service.cameras:
        raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")

    camera = camera_service.cameras[camera_id]
    logger.info(f"Camera status: {camera.status}, name: {camera.name}")

    recording_id = camera_service.start_recording(camera_id)
    logger.info(f"Recording started successfully: {recording_id}")

    #await broadcast_message({
    #    "type": "recording_started",
    #    "camera_id": camera_id,
    #    "recording_id": recording_id
    #})
    return {"message": "Recording started", "recording_id": recording_id}

@app.post("/api/cameras/{camera_id}/stop-recording")
async def stop_recording(camera_id: str):
    """Stop recording from a camera"""
    camera_service.stop_recording(camera_id)
    #await broadcast_message({"type": "recording_stopped", "camera_id": camera_id})
    return {"message": "Recording stopped"}

@app.get("/api/recordings", response_model=List[Recording])
async def get_recordings(camera_id: Optional[str] = None):
    """Get all recordings, optionally filtered by camera"""
    return camera_service.get_recordings(camera_id)

@app.get("/api/system/info")
async def get_system_info():
    """Get overall system and start_server process metrics for dashboard."""
    try:
        info = dashboard_service.get_system_info()
        info['settings'] = {
            'live_stream_mode': LIVE_STREAM_MODE if LIVE_STREAM_MODE in {"mjpeg", "hls"} else "mjpeg",
            **camera_service.get_runtime_settings(),
        }
        return info
    except Exception as e:
        logger.error(f"Failed to get system info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system/settings")
async def get_system_settings():
    normalized_mode = LIVE_STREAM_MODE if LIVE_STREAM_MODE in {"mjpeg", "hls"} else "mjpeg"
    runtime_settings = camera_service.get_runtime_settings()
    return {
        'live_stream_mode': normalized_mode,
        'uvicorn_reload': bool(UVICORN_RELOAD),
        **runtime_settings,
        'supported_live_stream_modes': ['mjpeg', 'hls'],
        'sensitivity_range': {'min': 0, 'max': int(getattr(camera_service, 'sensitivity_level', 5) or 5)},
        'jpeg_quality_range': {'min': 25, 'max': 95},
        'pipe_buffer_size_range': {'min': 65536, 'max': 268435456},
        'max_vel_range': {'min': 0.0, 'max': 5.0},
        'bg_diff_range': {'min': 1, 'max': 5000},
        'max_clip_length_range': {'min': 5, 'max': 600},
        'motion_check_interval_range': {'min': 1, 'max': 120},
    }


@app.put("/api/system/settings")
async def update_system_settings(payload: SystemSettingsUpdateRequest):
    global LIVE_STREAM_MODE, UVICORN_RELOAD
    restart_required = False

    if payload.live_stream_mode is not None:
        requested_mode = str(payload.live_stream_mode or '').strip().lower()
        if requested_mode not in {"mjpeg", "hls"}:
            raise HTTPException(status_code=400, detail="Invalid live_stream_mode. Supported: mjpeg, hls")
        LIVE_STREAM_MODE = requested_mode

    runtime_settings = camera_service.update_runtime_settings(
        low_power_mode=payload.low_power_mode,
        sensitivity=payload.sensitivity,
        jpeg_quality=payload.jpeg_quality,
        pipe_buffer_size=payload.pipe_buffer_size,
        max_vel=payload.max_vel,
        bg_diff=payload.bg_diff,
        max_clip_length=payload.max_clip_length,
        motion_check_interval=payload.motion_check_interval,
        min_free_storage_bytes=payload.min_free_storage_bytes,
        rtsp_unified_demux_enabled=payload.rtsp_unified_demux_enabled,
    )

    if payload.uvicorn_reload is not None:
        next_reload = bool(payload.uvicorn_reload)
        if next_reload != bool(UVICORN_RELOAD):
            restart_required = True
        UVICORN_RELOAD = next_reload
        os.environ["UVICORN_RELOAD"] = "1" if UVICORN_RELOAD else "0"

    # Persist live_stream_mode and uvicorn_reload to system.yaml
    try:
        camera_service.db.save_system_settings({
            'live_stream_mode': LIVE_STREAM_MODE,
            'uvicorn_reload': bool(UVICORN_RELOAD),
        })
    except Exception:
        pass

    return {
        'message': 'System settings updated',
        'live_stream_mode': LIVE_STREAM_MODE,
        'uvicorn_reload': bool(UVICORN_RELOAD),
        'restart_required': restart_required,
        **runtime_settings,
        'supported_live_stream_modes': ['mjpeg', 'hls'],
    }

@app.get("/api/system/live-stream-mode")
async def get_live_stream_mode():
    """Get server-configured default live stream mode."""
    normalized_mode = LIVE_STREAM_MODE if LIVE_STREAM_MODE in {"mjpeg", "hls"} else "mjpeg"
    return {
        "mode": normalized_mode,
        "live_stream_mode": normalized_mode,
        "supported_modes": ["mjpeg", "hls"],
    }


@app.post("/api/system/live-stream-mode")
async def set_live_stream_mode(payload: LiveStreamModeRequest):
    """Update server default live stream mode at runtime."""
    global LIVE_STREAM_MODE

    requested_mode = str(payload.mode or "").strip().lower()
    if requested_mode not in {"mjpeg", "hls"}:
        raise HTTPException(status_code=400, detail="Invalid mode. Supported: mjpeg, hls")

    LIVE_STREAM_MODE = requested_mode
    return {
        "message": "Live stream mode updated",
        "mode": LIVE_STREAM_MODE,
        "live_stream_mode": LIVE_STREAM_MODE,
        "supported_modes": ["mjpeg", "hls"],
    }

@app.get("/api/recordings/storage")
async def get_recording_storage():
    """Get recording storage stats and enforce low-space cleanup policy."""
    try:
        return camera_service.get_recording_storage_info(enforce_policy=True)
    except Exception as e:
        logger.error(f"Failed to get recording storage info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/recordings/{recording_id}")
async def delete_recording(recording_id: str):
    """Delete a recording"""
    try:
        camera_service.delete_recording(recording_id)
        return {"message": "Recording deleted successfully"}
    except Exception as e:
        logger.error(f"Failed to delete recording: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/api/recordings/{recording_id}/meta")
async def update_recording_meta(recording_id: str, request: RecordingMetaUpdate):
    """Update alert label and/or note on a recording."""
    try:
        db_recording = camera_service.db.get_recording(recording_id)
        if not db_recording:
            raise HTTPException(status_code=404, detail=f"Recording not found: {recording_id}")
        meta = db_recording.get('metadata') or {}
        if isinstance(meta, str):
            try:
                import json as _json
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        if request.label is not None:
            if request.label == '':
                meta.pop('label', None)
            else:
                meta['label'] = request.label
        if request.note is not None:
            meta['note'] = request.note
        camera_service.db.update_recording(recording_id, {'metadata': meta})
        return {"success": True, "recording_id": recording_id, "metadata": meta}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update recording meta: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Archive endpoints
@app.post("/api/recordings/archive/export")
async def export_archive(request: ArchiveExportRequest):
    """Export filtered completed recordings to a timestamped archive folder with recordings.yaml."""
    try:
        result = camera_service.recording_manager.export_archive(
            date_from=request.date_from,
            date_to=request.date_to,
            min_vel=request.min_vel,
            min_diff=request.min_diff,
            min_duration=request.min_duration,
            delete_after=request.delete_after,
            exclude_mode=request.exclude_mode,
            label_filter=request.label_filter,
        )
        return result
    except Exception as e:
        logger.error(f"Failed to export archive: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/recordings/archive/list")
async def list_archives():
    """List archive folders inside the fixed archive directory that contain a recordings.yaml."""
    try:
        archives = camera_service.recording_manager.list_archives()
        return {"archives": archives}
    except Exception as e:
        logger.error(f"Failed to list archives: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/recordings/archive/load")
async def load_archive(request: ArchivePathRequest):
    """Load recordings from an archive directory into the database."""
    try:
        loaded_ids = camera_service.recording_manager.load_archive(request.archive_path)
        recordings = camera_service.get_recordings()
        loaded_recordings = [r for r in recordings if r.id in loaded_ids]
        return {
            "loaded_count": len(loaded_ids),
            "recordings": [r.dict() for r in loaded_recordings],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to load archive: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/recordings/archive/unload")
async def unload_archive(request: ArchivePathRequest):
    """Remove archive recordings from the database (files are NOT deleted)."""
    try:
        count = camera_service.recording_manager.unload_archive(request.archive_path)
        return {"unloaded_count": count}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to unload archive: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Video streaming endpoints
@app.get("/api/cameras/{camera_id}/stream")
async def get_camera_stream(camera_id: str, mode: Optional[str] = None):
    """Get live stream entrypoint; supports MJPEG (default) and HLS descriptor mode."""
    selected_mode = (mode or LIVE_STREAM_MODE).strip().lower()
    if selected_mode == "hls":
        try:
            camera_service.start_av_stream(camera_id)
        except Exception as worker_error:
            logger.warning(f"Failed to ensure background camera stream for {camera_id}: {worker_error}")
        camera_service.start_hls_stream(camera_id)
        return {
            "mode": "hls",
            "manifest_url": f"/api/cameras/{camera_id}/hls/index.m3u8",
        }
    elif selected_mode == "mjpeg":
        # MJPEG mode requested: stop any HLS process for this camera first
        camera_service.stop_hls_stream(camera_id)
        return StreamingResponse(
            camera_service.generate_video_stream_endpoint(camera_id),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        raise HTTPException(status_code=400, detail="Invalid mode. Supported: mjpeg, hls")

    
@app.get("/api/cameras/{camera_id}/hls/index.m3u8")
async def get_camera_hls_manifest(camera_id: str, request: Request):
    """Serve HLS manifest for a live camera stream."""
    try:
        manifest_path = camera_service.get_hls_manifest_path(camera_id)
        token = request.query_params.get("access_token")

        if not token:
            return FileResponse(
                manifest_path,
                media_type="application/vnd.apple.mpegurl",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

        with open(manifest_path, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()

        safe_token = quote_plus(token)
        rewritten_lines = []
        for raw_line in lines:
            line = raw_line.strip()
            if (not line) or line.startswith("#"):
                rewritten_lines.append(raw_line)
                continue

            separator = "&" if "?" in line else "?"
            rewritten_lines.append(f"{line}{separator}access_token={safe_token}\n")

        return Response(
            content="".join(rewritten_lines),
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as e:
        logger.error(f"Failed to get camera HLS manifest: {e}")
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/cameras/{camera_id}/hls/{segment_name}")
async def get_camera_hls_segment(camera_id: str, segment_name: str):
    """Serve HLS segment for a live camera stream."""
    try:
        segment_path = camera_service.get_hls_segment_path(camera_id, segment_name)
        media_type = "video/mp2t" if segment_name.endswith(".ts") else "application/octet-stream"
        return FileResponse(
            segment_path,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as e:
        logger.error(f"Failed to get camera HLS segment: {e}")
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/api/cameras/{camera_id}/hls/stop")
async def stop_camera_hls_stream(camera_id: str):
    """Stop HLS process for a camera."""
    camera_service.stop_hls_stream(camera_id)
    return {"message": "Camera HLS stream stopped successfully"}

@app.get("/api/cameras/{camera_id}/processing_stream")
async def get_processing_stream(camera_id: str):
    """Get processed video stream from camera"""
    return StreamingResponse(
        camera_service.generate_processed_video_stream(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )

@app.get("/api/cameras/{camera_id}/audio_stream")
async def get_camera_audio_stream(camera_id: str, request: Request, fmt: Optional[str] = 'wav'):
    """Get separate live audio stream for a camera (for use alongside MJPEG)."""
    
    if str(LIVE_STREAM_MODE).strip().lower() == 'hls':
        raise HTTPException(status_code=409, detail="Separate audio stream is disabled in HLS mode (audio is muxed into HLS)")

    media_type = 'audio/wav'

    return StreamingResponse(
        camera_service.generate_audio_stream_endpoint(camera_id),
        media_type=media_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )

@app.post("/api/cameras/{camera_id}/video_stream/start")
async def start_camera_video_stream(camera_id: str):
    """Start video background processing thread for a camera.
    
    NOTE: This endpoint is redundant - the main /cameras/{id}/start endpoint 
    now starts both video and audio streams. Maintained for backward compatibility.
    """
    started = await asyncio.to_thread(camera_service.start_video_stream, camera_id)
    if not started:
        logger.warning(f"Video stream for camera {camera_id} is already running or failed to start")
    return {"started": True, "camera_id": camera_id}

@app.post("/api/cameras/{camera_id}/video_stream/stop")
async def stop_camera_video_stream(camera_id: str):
    """Stop video background processing thread for a camera."""
    stopped = await asyncio.to_thread(camera_service.stop_video_stream, camera_id)
    return {"stopped": stopped, "camera_id": camera_id}

@app.post("/api/cameras/{camera_id}/audio_stream/start")
async def start_camera_audio_stream(camera_id: str, fmt: Optional[str] = 'wav'):
    """Start/prewarm live audio stream process for a camera.
    
    NOTE: This endpoint is redundant - the main /cameras/{id}/start endpoint 
    now starts both video and audio streams. Maintained for backward compatibility.
    """
    if str(LIVE_STREAM_MODE).strip().lower() == 'hls':
        raise HTTPException(status_code=409, detail="Separate audio stream is disabled in HLS mode (audio is muxed into HLS)")

    output_format = (fmt or 'wav').strip().lower()
    started = await asyncio.to_thread(camera_service.start_audio_stream, camera_id)
    if not started:
        logger.warning(f"Audio stream for camera {camera_id} is already running or failed to start")
    return {"started": True, "camera_id": camera_id, "format": output_format}
    
@app.post("/api/cameras/{camera_id}/audio_stream/stop")
async def stop_camera_audio_stream(camera_id: str):
    """Stop active live audio stream process for a camera."""
    if str(LIVE_STREAM_MODE).strip().lower() == 'hls':
        return {"stopped": True, "camera_id": camera_id, "message": "No separate audio stream in HLS mode"}

    stopped = await asyncio.to_thread(camera_service.stop_audio_stream, camera_id)
    return {"stopped": bool(stopped), "camera_id": camera_id}


@app.get("/api/cameras/{camera_id}/audio_stream/analysis")
async def get_camera_audio_stream_analysis(camera_id: str):
    """Get latest per-chunk audio analysis for a camera."""
    try:
        db_camera = camera_service.db.get_camera(camera_id)
        if not db_camera:
            raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")

        analysis = camera_service.get_latest_audio_chunk_analysis(camera_id)
        return {
            "camera_id": camera_id,
            "has_analysis": bool(analysis),
            "analysis": analysis,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get camera audio stream analysis: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/cameras/{camera_id}/sensitivity")
async def get_camera_sensitivity(camera_id: str):
    try:
        db_camera = camera_service.db.get_camera(camera_id)
        if not db_camera:
            raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")

        sensitivity = camera_service.get_camera_sensitivity(camera_id)
        sensitivity_level = int(getattr(camera_service, 'sensitivity_level', 5) or 5)
        effective_stride = camera_service.get_camera_effective_stride(camera_id)
        return {
            "camera_id": camera_id,
            "sensitivity": sensitivity,
            "sensitivity_level": sensitivity_level,
            "effective_stride": effective_stride,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get camera sensitivity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/cameras/{camera_id}/sensitivity")
@app.post("/api/cameras/{camera_id}/sensitivity")
async def set_camera_sensitivity(camera_id: str, payload: CameraSensitivityRequest):
    try:
        db_camera = camera_service.db.get_camera(camera_id)
        if not db_camera:
            raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")

        sensitivity_level = int(getattr(camera_service, 'sensitivity_level', 5) or 5)
        sensitivity = int(payload.sensitivity)
        if sensitivity < 0 or sensitivity > sensitivity_level:
            raise HTTPException(
                status_code=400,
                detail=f"sensitivity must be between 0 and {sensitivity_level}",
            )

        updated = camera_service.set_camera_sensitivity(camera_id, sensitivity)
        effective_stride = camera_service.get_camera_effective_stride(camera_id)
        return {
            "camera_id": camera_id,
            "sensitivity": updated,
            "sensitivity_level": sensitivity_level,
            "effective_stride": effective_stride,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to set camera sensitivity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cameras/{camera_id}/stream/close")
async def close_camera_stream(camera_id: str):
    """Close camera stream to free resources"""
    camera_service.close_camera_stream(camera_id)
    return {"message": "Camera stream closed successfully"}

@app.get("/api/cameras/{camera_id}/stream/blank")
async def get_blank_stream(camera_id: str):
    """Stream a blank video"""
    return Response(
        camera_service.generate_blank_image(str(camera_id)),
        media_type="image/jpeg"
    )
    
@app.get("/api/recordings/{recording_id}/stream")
async def get_recording_stream(recording_id: str):
    """Stream a recorded video"""
    return StreamingResponse(
        camera_service.generate_recorded_video_stream(recording_id),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.get("/api/recordings/{recording_id}/play")
async def play_recording(recording_id: str):
    """Serve a recorded video file for browser playback"""
    try:
        file_path = camera_service.get_browser_playable_recording_path(recording_id)
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Recording file not found")
        
        response = FileResponse(file_path, media_type="video/mp4")
        response.headers["Content-Disposition"] = f'inline; filename="recording_{recording_id}.mp4"'
        return response
    except Exception as e:
        logger.error(f"Failed to play recording: {e}")
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/cameras/{camera_id}/result_stream")
async def get_camera_results(camera_id: str):
    """Get JSON stream of processing results for a camera"""
    try:
        return camera_service.generate_result_json_stream(camera_id)
            
    except Exception as e:
        logger.error(f"Failed to get camera results: {e}")
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/recordings/{recording_id}/download")
async def download_recording(recording_id: str):
    """Download a recorded video file"""
    try:
        file_path = camera_service.get_recording_path(recording_id)
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Recording file not found")
        
        return FileResponse(
            file_path,
            media_type='video/mp4',
            filename=f"recording_{recording_id}.mp4"
        )
    except Exception as e:
        logger.error(f"Failed to download recording: {e}")
        raise HTTPException(status_code=404, detail=str(e))

# Serve React app
@app.get("/")
async def serve_react_app():
    if os.path.exists("../frontend/build/index.html"):
        return FileResponse("../frontend/build/index.html")
    else:
        return {"message": "NVR Server API is running. Frontend not built. Access the API at /docs"}

@app.get("/{path:path}")
async def serve_react_routes(path: str):
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")

    file_path = f"../frontend/build/{path}"
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)
    elif os.path.exists("../frontend/build/index.html"):
        return FileResponse("../frontend/build/index.html")
    else:
        return {"message": "NVR Server API is running. Frontend not built. Access the API at /docs"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("start_server:app", host="0.0.0.0", port=9001, reload=bool(UVICORN_RELOAD))