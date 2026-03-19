"""Live streaming, HLS, WebSocket, audio, and processing stream endpoints."""
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse, FileResponse
from typing import Optional
import asyncio
from urllib.parse import quote_plus

from routes import deps

router = APIRouter(prefix="/api", tags=["streaming"])


@router.get("/cameras/{camera_id}/stream")
async def get_camera_stream(camera_id: str, mode: Optional[str] = None):
    """Get live stream entrypoint; supports MJPEG (default) and HLS descriptor mode."""
    selected_mode = (mode or deps.get_live_stream_mode()).strip().lower()
    if selected_mode == "hls":
        try:
            deps.camera_service.start_av_stream(camera_id)
        except Exception as worker_error:
            deps.logger.warning(f"Failed to ensure background camera stream for {camera_id}: {worker_error}")
        deps.camera_service._hls_manager.start_stream(camera_id)
        return {
            "mode": "hls",
            "manifest_url": f"/api/cameras/{camera_id}/hls/index.m3u8",
        }
    elif selected_mode == "mjpeg":
        # MJPEG mode requested: stop any HLS process for this camera first
        deps.camera_service._hls_manager.stop_stream(camera_id)
        return StreamingResponse(
            deps.camera_service.generate_video_stream_endpoint(camera_id),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "X-Accel-Buffering": "no",
            },
        )
    elif selected_mode == "ws":
        # WebSocket mode: return connection info (actual stream via ws endpoint)
        deps.camera_service._hls_manager.stop_stream(camera_id)
        return {
            "mode": "ws",
            "ws_url": f"/api/cameras/{camera_id}/ws_stream",
        }
    else:
        raise HTTPException(status_code=400, detail="Invalid mode. Supported: mjpeg, hls, ws")

    
@router.get("/cameras/{camera_id}/hls/index.m3u8")
async def get_camera_hls_manifest(camera_id: str, request: Request):
    """Serve HLS manifest for a live camera stream."""
    try:
        manifest_path = deps.camera_service._hls_manager.get_manifest_path(camera_id)
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
        deps.logger.error(f"Failed to get camera HLS manifest: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/cameras/{camera_id}/hls/{segment_name}")
async def get_camera_hls_segment(camera_id: str, segment_name: str):
    """Serve HLS segment for a live camera stream."""
    try:
        segment_path = deps.camera_service._hls_manager.get_segment_path(camera_id, segment_name)
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
        deps.logger.error(f"Failed to get camera HLS segment: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/cameras/{camera_id}/hls/stop")
async def stop_camera_hls_stream(camera_id: str):
    """Stop HLS process for a camera."""
    deps.camera_service._hls_manager.stop_stream(camera_id)
    return {"message": "Camera HLS stream stopped successfully"}


# ----- WebSocket video stream -----

@router.websocket("/cameras/{camera_id}/ws_stream")
async def ws_camera_stream(websocket: WebSocket, camera_id: str):
    """WebSocket binary stream of JPEG frames for a camera.
    
    Each message is a raw JPEG image (binary). The client renders
    frames onto a <canvas> or <img> element via createObjectURL.
    """
    await websocket.accept()

    # Ensure camera AV stream is running
    try:
        deps.camera_service.start_av_stream(camera_id)
    except Exception as e:
        deps.logger.warning(f"Failed to start stream for WS {camera_id}: {e}")
        await websocket.close(code=1011, reason="Camera unavailable")
        return

    ws_manager = deps.camera_service._ws_manager
    q = ws_manager.subscribe(camera_id)
    try:
        while True:
            jpeg_bytes = await q.get()
            await websocket.send_bytes(jpeg_bytes)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        deps.logger.error(f"WS stream error for {camera_id}: {e}")
    finally:
        ws_manager.unsubscribe(camera_id, q)


@router.get("/cameras/{camera_id}/processing_stream")
async def get_processing_stream(camera_id: str):
    """Get processed video stream from camera"""
    return StreamingResponse(
        deps.camera_service.generate_processing_stream_endpoint(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/cameras/{camera_id}/audio_stream")
async def get_camera_audio_stream(camera_id: str, request: Request, fmt: Optional[str] = 'wav'):
    """Get separate live audio stream for a camera (for use alongside MJPEG)."""
    
    if str(deps.get_live_stream_mode()).strip().lower() == 'hls':
        raise HTTPException(status_code=409, detail="Separate audio stream is disabled in HLS mode (audio is muxed into HLS)")

    media_type = 'audio/wav'

    return StreamingResponse(
        deps.camera_service.generate_audio_stream_endpoint(camera_id),
        media_type=media_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cameras/{camera_id}/audio_stream/start")
async def start_camera_audio_stream(camera_id: str, fmt: Optional[str] = 'wav'):
    """Start/prewarm live audio stream process for a camera.
    
    NOTE: This endpoint is redundant - the main /cameras/{id}/start endpoint 
    now starts both video and audio streams. Maintained for backward compatibility.
    """
    if str(deps.get_live_stream_mode()).strip().lower() == 'hls':
        raise HTTPException(status_code=409, detail="Separate audio stream is disabled in HLS mode (audio is muxed into HLS)")

    output_format = (fmt or 'wav').strip().lower()
    started = await asyncio.to_thread(deps.camera_service.start_audio_stream, camera_id)
    if not started:
        deps.logger.warning(f"Audio stream for camera {camera_id} is already running or failed to start")
    return {"started": True, "camera_id": camera_id, "format": output_format}

    
@router.post("/cameras/{camera_id}/audio_stream/stop")
async def stop_camera_audio_stream(camera_id: str):
    """Stop active live audio stream process for a camera."""
    if str(deps.get_live_stream_mode()).strip().lower() == 'hls':
        return {"stopped": True, "camera_id": camera_id, "message": "No separate audio stream in HLS mode"}

    stopped = await asyncio.to_thread(deps.camera_service.stop_audio_stream, camera_id)
    return {"stopped": bool(stopped), "camera_id": camera_id}


@router.get("/cameras/{camera_id}/audio_stream/analysis")
async def get_camera_audio_stream_analysis(camera_id: str):
    """Get latest per-chunk audio analysis for a camera."""
    try:
        db_camera = deps.camera_service.db.get_camera(camera_id)
        if not db_camera:
            raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")

        analysis = deps.camera_service.get_latest_audio_chunk_analysis(camera_id)
        return {
            "camera_id": camera_id,
            "has_analysis": bool(analysis),
            "analysis": analysis,
        }
    except HTTPException:
        raise
    except Exception as e:
        deps.logger.error(f"Failed to get camera audio stream analysis: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cameras/{camera_id}/stream/close")
async def close_camera_stream(camera_id: str):
    """Close camera stream to free resources"""
    deps.camera_service.close_camera_stream(camera_id)
    return {"message": "Camera stream closed successfully"}


@router.get("/cameras/{camera_id}/stream/blank")
async def get_blank_stream(camera_id: str):
    """Stream a blank video"""
    return Response(
        deps.camera_service.generate_blank_image(str(camera_id)),
        media_type="image/jpeg"
    )
