"""Camera management endpoints: CRUD, start/stop/restart, sensitivity."""
from fastapi import APIRouter, HTTPException
from typing import List
import asyncio

from models.camera import Camera, CameraCreate, CameraUpdate
from models.requests import CameraSensitivityRequest
from routes import deps

router = APIRouter(prefix="/api", tags=["cameras"])


@router.get("/cameras", response_model=List[Camera])
async def get_cameras():
    """Get all cameras"""
    return deps.camera_service.get_cameras()


@router.post("/cameras", response_model=Camera)
async def create_camera(camera: CameraCreate):
    """Add a new camera"""
    try:
        new_camera = deps.camera_service.add_camera(camera)
        await deps.broadcast_message({"type": "camera_added", "camera": new_camera.dict()})
        return new_camera
    except Exception as e:
        deps.logger.error(f"Failed to add camera: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/cameras/{camera_id}", response_model=Camera)
async def update_camera(camera_id: str, camera_update: CameraUpdate):
    """Update camera settings"""
    try:
        updated_camera = deps.camera_service.update_camera(camera_id, camera_update)
        await deps.broadcast_message({"type": "camera_updated", "camera": updated_camera.dict()})
        return updated_camera
    except Exception as e:
        deps.logger.error(f"Failed to update camera: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/cameras/{camera_id}")
async def delete_camera(camera_id: str):
    """Delete a camera"""
    try:
        deps.camera_service.remove_camera(camera_id)
        await deps.broadcast_message({"type": "camera_deleted", "camera_id": camera_id})
        return {"message": "Camera deleted successfully"}
    except Exception as e:
        deps.logger.error(f"Failed to delete camera: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/cameras/{camera_id}/start")
async def start_camera(camera_id: str):
    """Start camera with both video and audio streams (unified approach)"""
    success = deps.camera_service.start_video(camera_id)
    if success:
        # Also start background audio/video streaming threads if camera started successfully
        await asyncio.to_thread(deps.camera_service.start_av_stream, camera_id)
        #await deps.broadcast_message({"type": "camera_started", "camera_id": camera_id})
        return {"message": "Camera and streaming started successfully"}
    else:
        deps.camera_service.stop_video(camera_id)
        success = deps.camera_service.start_video(camera_id)
    if success:
        # Also start background streaming on retry
        await asyncio.to_thread(deps.camera_service.start_av_stream, camera_id)
        #await deps.broadcast_message({"type": "camera_started", "camera_id": camera_id})
        return {"message": "Camera and streaming started successfully on retry"}
    else:
        raise HTTPException(status_code=400, detail="Failed to start camera - camera may be unavailable or in use")


@router.post("/cameras/{camera_id}/stop")
async def stop_camera(camera_id: str):
    """Stop camera and all associated streams (video, audio, HLS, recordings)"""
    deps.camera_service.stop_video_stream(camera_id)
    deps.camera_service.stop_audio_stream(camera_id)
    #await deps.broadcast_message({"type": "camera_stopped", "camera_id": camera_id})
    return {"message": "Camera and all streams stopped successfully"}


@router.post("/cameras/{camera_id}/restart")
async def restart_camera(camera_id: str):
    """Restart camera (stop recording, stop camera, start camera)"""
    try:
        success = deps.camera_service.restart_camera(camera_id)
        if success:
            #await deps.broadcast_message({"type": "camera_restarted", "camera_id": camera_id})
            return {"message": "Camera restarted successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to restart camera")
    except Exception as e:
        deps.logger.error(f"Error restarting camera {camera_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to restart camera: {str(e)}")


@router.get("/cameras/{camera_id}/sensitivity")
async def get_camera_sensitivity(camera_id: str):
    try:
        db_camera = deps.camera_service.db.get_camera(camera_id)
        if not db_camera:
            raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")

        sensitivity = deps.camera_service.get_camera_sensitivity(camera_id)
        sensitivity_level = int(getattr(deps.camera_service, 'sensitivity_level', 5) or 5)
        effective_stride = deps.camera_service.get_camera_effective_stride(camera_id)
        return {
            "camera_id": camera_id,
            "sensitivity": sensitivity,
            "sensitivity_level": sensitivity_level,
            "effective_stride": effective_stride,
        }
    except HTTPException:
        raise
    except Exception as e:
        deps.logger.error(f"Failed to get camera sensitivity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/cameras/{camera_id}/sensitivity")
async def set_camera_sensitivity(camera_id: str, payload: CameraSensitivityRequest):
    try:
        db_camera = deps.camera_service.db.get_camera(camera_id)
        if not db_camera:
            raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")

        sensitivity_level = int(getattr(deps.camera_service, 'sensitivity_level', 5) or 5)
        sensitivity = int(payload.sensitivity)
        if sensitivity < 0 or sensitivity > sensitivity_level:
            raise HTTPException(
                status_code=400,
                detail=f"sensitivity must be between 0 and {sensitivity_level}",
            )

        updated = deps.camera_service.set_camera_sensitivity(camera_id, sensitivity)
        effective_stride = deps.camera_service.get_camera_effective_stride(camera_id)
        return {
            "camera_id": camera_id,
            "sensitivity": updated,
            "sensitivity_level": sensitivity_level,
            "effective_stride": effective_stride,
        }
    except HTTPException:
        raise
    except Exception as e:
        deps.logger.error(f"Failed to set camera sensitivity: {e}")
        raise HTTPException(status_code=500, detail=str(e))
