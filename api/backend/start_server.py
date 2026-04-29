from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import Dict
import argparse
import asyncio
import json
import os
import logging
import base64
import hashlib
import hmac
import secrets
import threading
import time

from services.streaming.camera_service import CameraService
from services.system.dashboard_service import SystemService


# =====================================================================
# REQUEST MODELS (remaining models moved to models/requests.py)
# =====================================================================

class LoginRequest(BaseModel):
    password: str

# =====================================================================
# CONFIGURATION AND INITIALIZATION
# =====================================================================

user = os.popen('uname -n').read().strip()
if user == 'irfan-linux':
    configs = 'pc'
elif user == 'raspberrypi' or 'pi' in user:
    configs = 'pi'
else:
    configs = 'default'

print("Running with config:", configs)

# Parse CLI flags early (parse_known_args ignores uvicorn's own argv)
_arg_parser = argparse.ArgumentParser(add_help=False)
_arg_parser.add_argument(
    '--prd', action='store_true',
    help='Production mode: auto-start stream and recording for all RTSP cameras at startup'
)
_arg_parser.add_argument(
    '--nginx', action='store_true',
    help='Nginx mode: bind to 127.0.0.1 and skip built-in static/frontend serving (nginx handles it)'
)
_cli_args, _ = _arg_parser.parse_known_args()
PRODUCTION_MODE: bool = _cli_args.prd
NGINX_MODE: bool = _cli_args.nginx

# Initialize services
camera_service = CameraService(configs=configs)
system_service = SystemService(camera_service=camera_service)

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
AUTH_PASSWORD = os.getenv("API_PASSWORD", "admin123")
AUTH_TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "86400"))
AUTH_SECRET = os.getenv("AUTH_SECRET", secrets.token_hex(32))

# Load LIVE_STREAM_MODE: env var takes priority, then system.yaml, then default
_sys_boot = camera_service.get_system_settings()
_live_stream_env = os.getenv("LIVE_STREAM_MODE")
LIVE_STREAM_MODE = (_live_stream_env if _live_stream_env in {"mjpeg", "hls", "ws"}
                    else str(_sys_boot['live_stream_mode']))
if LIVE_STREAM_MODE not in {"mjpeg", "hls", "ws"}:
    LIVE_STREAM_MODE = "mjpeg"

# Hardcoded to False - not configurable from frontend
UVICORN_RELOAD = False

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =====================================================================
# SHARED STATE FOR ROUTE MODULES
# =====================================================================
from routes import deps
deps.camera_service = camera_service
deps.system_service = system_service
deps.logger = logger
deps.set_live_stream_mode(LIVE_STREAM_MODE)

app = FastAPI(title="NVR Server", description="Network Video Recorder with RTSP and Camera Support")


@app.on_event("shutdown")
def _graceful_shutdown():
    """Stop all cameras, recordings, and streams on server shutdown."""
    logger.info("🛑 Shutdown signal received — cleaning up all cameras and streams...")
    for camera_id in list(camera_service._camera_streams.keys()):
        try:
            camera_service.stop_recording(camera_id)
        except Exception:
            pass
        try:
            camera_service.stop_av_stream(camera_id)
        except Exception:
            pass
    logger.info("🛑 Graceful shutdown complete.")

# Mount static files for React app only if build directory exists and not behind nginx
frontend_build_path = "frontend/build"
frontend_static_path = "frontend/build/static"
if NGINX_MODE:
    logger.info("Nginx mode: skipping static file mounting (nginx serves /static/ directly)")
elif os.path.exists(frontend_static_path):
    app.mount("/static", StaticFiles(directory=frontend_static_path), name="static")
    logger.info("Frontend static files mounted")
else:
    logger.warning("Frontend build directory not found. Run 'cd frontend && npm install && npm run build' to build the frontend.")

# WebSocket connections for real-time updates
active_connections: Dict[str, WebSocket] = {}
last_camera_status: Dict[str, str] = {}  # Track last known camera status

api_key_scheme = APIKeyHeader(name="x-api-password", auto_error=False)

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

# =====================================================================
# MIDDLEWARE AND AUTHENTICATION
# =====================================================================

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
                        "camera": camera.model_dump()
                    })
                    logger.info(f"Camera {camera.id} status changed to {current_status}")
                    
        except Exception as e:
            logger.error(f"Error checking camera status: {e}")
        
        # Check every 2 seconds
        await asyncio.sleep(2)

def _start_production_cameras():
    """Production mode: start stream and recording for every RTSP camera.
    Runs in a daemon thread after a short delay so the server is fully up."""
    time.sleep(3)  # Wait for server to be ready
    cameras = camera_service.get_cameras()
    rtsp_cameras = [c for c in cameras if c.source.startswith('rtsp://')]
    if not rtsp_cameras:
        logger.info('[prd] No RTSP cameras found — nothing to auto-start')
        return
    logger.info(f'[prd] Auto-starting {len(rtsp_cameras)} RTSP camera(s)')
    for camera in rtsp_cameras:
        logger.info(f'[prd] Starting camera {camera.id} ({camera.source})')
        stream_ok = False
        for attempt in range(1, 4):
            result = camera_service.start_av_stream(camera.id)
            if camera.id in camera_service._video_background_threads:
                stream_ok = True
                break
            if result is None:
                logger.error(f'[prd] Fatal error for {camera.id} (bad host/port/URL) — skipping retries')
                break
            logger.warning(f'[prd] Stream did not start for {camera.id}, attempt {attempt}/3')
            if attempt < 3:
                time.sleep(2)
        if not stream_ok:
            logger.error(f'[prd] Giving up on {camera.id} after 3 attempts, skipping recording')
            continue

        logger.info(f'[prd] Starting recording for {camera.id}')
        for attempt in range(1, 4):
            result = camera_service.start_recording(camera.id)
            if result is not None:
                break
            logger.warning(f'[prd] Recording did not start for {camera.id}, attempt {attempt}/3')
            if attempt < 3:
                time.sleep(2)
        else:
            logger.error(f'[prd] Giving up on recording for {camera.id} after 3 attempts')

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

    if PRODUCTION_MODE:
        logger.info('[prd] Production mode active — scheduling RTSP camera auto-start')
        threading.Thread(target=_start_production_cameras, daemon=True, name='prd-autostart').start()

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
        except Exception:
            pass

# =====================================================================
# AUTHENTICATION ENDPOINTS
# =====================================================================
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

# =====================================================================
# INCLUDE ROUTE MODULES
# =====================================================================
from routes.cameras import router as cameras_router
from routes.recordings import router as recordings_router
from routes.system import router as system_router
from routes.streaming import router as streaming_router

# ── Zone Control Plugin ───────────────────────────────────────────────────────
from services.zone_manager.zone_store import init_zone_store
from services.zone_manager.zone_routes import router as zones_router

_zones_dir = os.path.join(camera_service.root_dir, "configs", "zones")
init_zone_store(_zones_dir)
# Make ZoneStore available on the streaming service so the video thread
# can retrieve and cache zone configs without importing from routes.
camera_service._zone_store = camera_service.recording_manager._zone_store = \
    __import__('services.zone_manager.zone_store', fromlist=['get_zone_store']).get_zone_store()
# ─────────────────────────────────────────────────────────────────────────────

app.include_router(cameras_router)
app.include_router(recordings_router)
app.include_router(system_router)
app.include_router(streaming_router)
app.include_router(zones_router)

# ── Person Gallery / ReID ─────────────────────────────────────────────────────
from routes.reid import router as persons_router
from services.reid.reid_service import ReIDService

_recordings_dir = camera_service.recording_manager.recordings_dir
_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data')
deps.reid_service = ReIDService(
    recordings_dir=_recordings_dir,
    data_dir=_data_dir,
    model_variant='osnet_x0_25',
)
app.include_router(persons_router)
# ─────────────────────────────────────────────────────────────────────────────

# Make broadcast_message available to route modules
deps.broadcast_message = broadcast_message

# =====================================================================
# FRONTEND SERVING ENDPOINTS
# =====================================================================
if not NGINX_MODE:
    @app.get("/")
    async def serve_react_app():
        if os.path.exists("./api/frontend/build/index.html"):
            return FileResponse("./api/frontend/build/index.html")
        else:
            return {"message": "NVR Server API is running. Frontend not built. Access the API at /docs"}

    @app.get("/{path:path}")
    async def serve_react_routes(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found")

        file_path = f"./api/frontend/build/{path}"
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return FileResponse(file_path)
        elif os.path.exists("./api/frontend/build/index.html"):
            return FileResponse("./api/frontend/build/index.html")
        else:
            return {"message": "NVR Server API is running. Frontend not built. Access the API at /docs"}

# =====================================================================
# APPLICATION STARTUP
# =====================================================================
if __name__ == "__main__":
    import uvicorn
    system_service.start()
    _host = "127.0.0.1" if NGINX_MODE else "0.0.0.0"
    if NGINX_MODE:
        logger.info("Nginx mode: binding to 127.0.0.1:9001")
    uvicorn.run(
        app, 
        host=_host, 
        port=9001, 
        reload=bool(UVICORN_RELOAD),
        ws_ping_interval=None,  # Disable keepalive pings — prevents concurrent drain AssertionError
        ws_ping_timeout=None,
        ws_per_message_deflate=False,  # JPEGs are already compressed; deflating wastes CPU for ~0% size reduction
    )