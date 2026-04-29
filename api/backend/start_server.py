from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict
import os

from login_utils import auth_middleware, login
from routes import deps
from routes.cameras import router as cameras_router
from routes.recordings import router as recordings_router
from routes.reid import router as persons_router
from routes.streaming import router as streaming_router
from routes.system import router as system_router
from services.reid.reid_service import ReIDService
from services.streaming.camera_service import CameraService
from services.system.dashboard_service import SystemService
from services.zone_manager.zone_routes import router as zones_router
from services.zone_manager.zone_store import get_zone_store, init_zone_store
from utils import (
    configure_logging,
    configure_static_mount,
    graceful_shutdown,
    parse_cli_args,
    resolve_configs,
    resolve_live_stream_mode,
    serve_frontend_index,
    serve_frontend_path,
)


# =====================================================================
# CONFIGURATION AND INITIALIZATION
# =====================================================================

CONFIGS = resolve_configs()
FRONTEND_STATIC_PATH = 'frontend/build/static'
UVICORN_RELOAD = False

cli_args = parse_cli_args()
NGINX_MODE: bool = cli_args.nginx
active_connections: Dict[str, WebSocket] = {}

logger = configure_logging()


def configure_shared_deps(camera_service: CameraService, system_service: SystemService, live_stream_mode: str) -> None:
    deps.camera_service = camera_service
    deps.system_service = system_service
    deps.logger = logger
    deps.set_live_stream_mode(live_stream_mode)


def configure_zone_store(camera_service: CameraService) -> None:
    zones_dir = os.path.join(camera_service.root_dir, 'configs', 'zones')
    init_zone_store(zones_dir)
    zone_store = get_zone_store()
    camera_service._zone_store = zone_store
    camera_service.recording_manager._zone_store = zone_store


def configure_reid_service(camera_service: CameraService) -> None:
    recordings_dir = camera_service.recording_manager.recordings_dir
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data')
    deps.reid_service = ReIDService(
        recordings_dir=recordings_dir,
        data_dir=data_dir,
        model_variant='osnet_x0_25',
    )


def include_routers(app: FastAPI) -> None:
    app.include_router(cameras_router)
    app.include_router(recordings_router)
    app.include_router(system_router)
    app.include_router(streaming_router)
    app.include_router(zones_router)
    app.include_router(persons_router)


def configure_middleware(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )
    app.middleware('http')(auth_middleware)


def register_frontend_routes(app: FastAPI, nginx_mode: bool) -> None:
    if nginx_mode:
        return

    @app.get('/')
    async def serve_react_app():
        return serve_frontend_index()

    @app.get('/{path:path}')
    async def serve_react_routes(path: str):
        return serve_frontend_path(path)


def create_app_lifespan(camera_service: CameraService):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        graceful_shutdown(camera_service, logger)

    return lifespan


print('Running with config:', CONFIGS)

# =====================================================================
# OBJECT CREATION
# =====================================================================

camera_service = CameraService(configs=CONFIGS)
system_service = SystemService(camera_service=camera_service)
live_stream_mode = resolve_live_stream_mode(camera_service.get_system_settings())

configure_shared_deps(camera_service, system_service, live_stream_mode)
configure_zone_store(camera_service)
configure_reid_service(camera_service)

app = FastAPI(
    title='NVR Server',
    description='Network Video Recorder with RTSP and Camera Support',
    lifespan=create_app_lifespan(camera_service),
)
configure_middleware(app)
configure_static_mount(app, NGINX_MODE, FRONTEND_STATIC_PATH, logger)
include_routers(app)
register_frontend_routes(app, NGINX_MODE)

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


app.post('/api/auth/login')(login)

# =====================================================================
# APPLICATION STARTUP
# =====================================================================
if __name__ == "__main__":
    system_service.start()
    import uvicorn
    host = '127.0.0.1' if NGINX_MODE else '0.0.0.0'
    if NGINX_MODE:
        logger.info('Nginx mode: binding to 127.0.0.1:9001')
    else:
        logger.info('Binding to all interfaces to 0.0.0.0:9001')
    uvicorn.run(
        app,
        host=host,
        port=9001,
        access_log=False,
        reload=bool(UVICORN_RELOAD),
        ws_ping_interval=None,
        ws_ping_timeout=None,
        ws_per_message_deflate=False,
    )