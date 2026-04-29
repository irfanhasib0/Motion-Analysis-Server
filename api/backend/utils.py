import argparse
import logging
import os

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def resolve_configs() -> str:
    user = os.popen('uname -n').read().strip()
    if user == 'irfan-linux':
        return 'pc'
    if user == 'raspberrypi' or 'pi' in user:
        return 'pi'
    return 'default'


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '--nginx', action='store_true',
        help='Nginx mode: bind to 127.0.0.1 and skip built-in static/frontend serving (nginx handles it)'
    )
    cli_args, _ = parser.parse_known_args()
    return cli_args


def resolve_live_stream_mode(system_settings: dict) -> str:
    live_stream_env = os.getenv('LIVE_STREAM_MODE')
    live_stream_mode = (
        live_stream_env
        if live_stream_env in {'mjpeg', 'hls', 'ws'}
        else str(system_settings['live_stream_mode'])
    )
    if live_stream_mode not in {'mjpeg', 'hls', 'ws'}:
        return 'mjpeg'
    return live_stream_mode


def configure_logging() -> logging.Logger:
    logging.basicConfig(level=logging.INFO)
    return logging.getLogger(__name__)


def graceful_shutdown(camera_service, logger: logging.Logger) -> None:
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


def configure_static_mount(app, nginx_mode: bool, frontend_static_path: str, logger: logging.Logger) -> None:
    if nginx_mode:
        logger.info('Nginx mode: skipping static file mounting (nginx serves /static/ directly)')
        return

    if os.path.exists(frontend_static_path):
        app.mount('/static', StaticFiles(directory=frontend_static_path), name='static')
        logger.info('Frontend static files mounted')
        return

    logger.warning("Frontend build directory not found. Run 'cd frontend && npm install && npm run build' to build the frontend.")


def serve_frontend_index() -> FileResponse | dict:
    if os.path.exists('./api/frontend/build/index.html'):
        return FileResponse('./api/frontend/build/index.html')
    return {'message': 'NVR Server API is running. Frontend not built. Access the API at /docs'}


def serve_frontend_path(path: str) -> FileResponse | dict:
    if path.startswith('api/'):
        raise HTTPException(status_code=404, detail='API endpoint not found')

    file_path = f'./api/frontend/build/{path}'
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)
    if os.path.exists('./api/frontend/build/index.html'):
        return FileResponse('./api/frontend/build/index.html')
    return {'message': 'NVR Server API is running. Frontend not built. Access the API at /docs'}    