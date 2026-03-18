from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import Dict
import asyncio
import json
import os
import logging
import base64
import hashlib
import hmac
import secrets
import time

from services.camera_service import CameraService
from services.dashboard_service import DashboardService


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

# Initialize services
camera_service = CameraService(configs=configs)
dashboard_service = DashboardService(camera_service=camera_service)
    
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
AUTH_PASSWORD = os.getenv("API_PASSWORD", "admin123")
AUTH_TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "86400"))
AUTH_SECRET = os.getenv("AUTH_SECRET", secrets.token_hex(32))

# Load LIVE_STREAM_MODE: env var takes priority, then system.yaml, then default
_sys_boot = camera_service.get_system_settings()
_live_stream_env = os.getenv("LIVE_STREAM_MODE")
LIVE_STREAM_MODE = (_live_stream_env if _live_stream_env in {"mjpeg", "hls"}
                    else str(_sys_boot['live_stream_mode']))
if LIVE_STREAM_MODE not in {"mjpeg", "hls"}:
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
deps.dashboard_service = dashboard_service
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

app.include_router(cameras_router)
app.include_router(recordings_router)
app.include_router(system_router)
app.include_router(streaming_router)

# Make broadcast_message available to route modules
deps.broadcast_message = broadcast_message

# =====================================================================
# FRONTEND SERVING ENDPOINTS
# =====================================================================
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

# =====================================================================
# APPLICATION STARTUP
# =====================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "start_server:app", 
        host="0.0.0.0", 
        port=9001, 
        reload=bool(UVICORN_RELOAD)
    )