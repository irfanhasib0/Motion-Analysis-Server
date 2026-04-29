"""Recording management, storage, archive, playback, and motion data endpoints."""
import asyncio
from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from typing import Optional, List
import os

from models.recording import Recording
from models.requests import RecordingMetaUpdate, ArchiveExportRequest, ArchivePathRequest
from routes import deps

router = APIRouter(prefix="/api", tags=["recordings"])


@router.post("/cameras/{camera_id}/start-recording")
async def start_recording(camera_id: str, background_tasks: BackgroundTasks):
    """Start recording from a camera"""
    deps.logger.info(f"Start recording request for camera: {camera_id}")
    if camera_id not in deps.camera_service.cameras:
        raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")

    camera = deps.camera_service.cameras[camera_id]
    deps.logger.info(f"Camera status: {camera.status}, name: {camera.name}")

    recording_id = await asyncio.to_thread(deps.camera_service.start_recording, camera_id)
    if recording_id == 'fatal':
        raise HTTPException(status_code=503, detail="Camera stream failed to open (bad URL or credentials). Check camera settings.")
    if recording_id is None:
        raise HTTPException(status_code=503, detail="Camera stream unavailable. The camera may be offline or unreachable.")
    deps.logger.info(f"Recording started successfully: {recording_id}")
    return {"message": "Recording started", "recording_id": recording_id}


@router.post("/cameras/{camera_id}/stop-recording")
async def stop_recording(camera_id: str):
    """Stop recording from a camera"""
    await asyncio.to_thread(deps.camera_service.stop_recording, camera_id)
    return {"message": "Recording stopped"}


@router.get("/recordings", response_model=List[Recording])
async def get_recordings(camera_id: Optional[str] = None):
    """Get all recordings, optionally filtered by camera"""
    return deps.camera_service.get_recordings(camera_id)


@router.get("/recordings/storage")
async def get_recording_storage():
    """Get recording storage stats and enforce low-space cleanup policy."""
    try:
        return deps.camera_service.get_recording_storage_info(enforce_policy=True)
    except Exception as e:
        deps.logger.error(f"Failed to get recording storage info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/recordings/{recording_id}")
async def delete_recording(recording_id: str):
    """Delete a recording"""
    try:
        deps.camera_service.delete_recording(recording_id)
        return {"message": "Recording deleted successfully"}
    except Exception as e:
        deps.logger.error(f"Failed to delete recording: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/recordings/{recording_id}/meta")
async def update_recording_meta(recording_id: str, request: RecordingMetaUpdate):
    """Update alert label and/or note on a recording."""
    try:
        db_recording = deps.camera_service.db.get_recording(recording_id)
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
        deps.camera_service.db.update_recording(recording_id, {'metadata': meta})
        return {"success": True, "recording_id": recording_id, "metadata": meta}
    except HTTPException:
        raise
    except Exception as e:
        deps.logger.error(f"Failed to update recording meta: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/recordings/archive/export")
async def export_archive(request: ArchiveExportRequest):
    """Export filtered completed recordings to a timestamped archive folder with recordings.yaml."""
    try:
        result = deps.camera_service.recording_manager.export_archive(
            date_from=request.date_from,
            date_to=request.date_to,
            camera_ids=request.camera_ids,
            min_vel=request.min_vel,
            min_diff=request.min_diff,
            min_duration=request.min_duration,
            delete_after=request.delete_after,
            exclude_mode=request.exclude_mode,
            label_filter=request.label_filter,
            clean_up_extensions=request.clean_up_extensions,
        )
        return result
    except Exception as e:
        deps.logger.error(f"Failed to export archive: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/recordings/archive/list")
async def list_archives():
    """List archive folders inside the fixed archive directory that contain a recordings.yaml."""
    try:
        archives = deps.camera_service.recording_manager.list_archives()
        return {"archives": archives}
    except Exception as e:
        deps.logger.error(f"Failed to list archives: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/recordings/archive/load")
async def load_archive(request: ArchivePathRequest):
    """Load recordings from an archive directory into the database."""
    try:
        loaded_ids = deps.camera_service.recording_manager.load_archive(request.archive_path)
        recordings = deps.camera_service.get_recordings()
        loaded_recordings = [r for r in recordings if r.id in loaded_ids]
        return {
            "loaded_count": len(loaded_ids),
            "recordings": [r.model_dump() for r in loaded_recordings],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        deps.logger.error(f"Failed to load archive: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/recordings/archive/unload")
async def unload_archive(request: ArchivePathRequest):
    """Remove archive recordings from the database (files are NOT deleted)."""
    try:
        count = deps.camera_service.recording_manager.unload_archive(request.archive_path)
        return {"unloaded_count": count}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        deps.logger.error(f"Failed to unload archive: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/recordings/{recording_id}/stream")
async def get_recording_stream(recording_id: str):
    """Stream a recorded video"""
    return StreamingResponse(
        deps.camera_service.generate_recorded_video_stream(recording_id),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@router.get("/recordings/{recording_id}/play")
async def play_recording(recording_id: str, overlay: bool = False):
    """Serve a recorded video file for browser playback.
    Pass ?overlay=true to get a version with optical flow bounding boxes and tracks."""
    if overlay:
        try:
            file_path = deps.camera_service.recording_manager.generate_overlay_video(recording_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    else:
        file_path = deps.camera_service.get_browser_playable_recording_path(recording_id)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Recording file not found")
    
    response = FileResponse(file_path, media_type="video/mp4")
    response.headers["Content-Disposition"] = f'inline; filename="recording_{recording_id}.mp4"'
    return response


@router.get("/recordings/{recording_id}/thumbnail")
async def get_recording_thumbnail(recording_id: str):
    """Serve the peak-motion JPG thumbnail for a recording."""
    try:
        file_path = deps.camera_service.recording_manager.get_recording_path(recording_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # New per-clip format: …/rec_id/video.mp4 → …/rec_id/thumbnail.jpg
    # Legacy flat format: …/cam_id/recording.mp4 → …/cam_id/recording.jpg
    if os.path.basename(file_path) == 'video.mp4':
        thumb_path = os.path.join(os.path.dirname(file_path), 'thumbnail.jpg')
    else:
        thumb_path = os.path.splitext(file_path)[0] + '.jpg'
    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(thumb_path, media_type="image/jpeg")


@router.post("/recordings/{recording_id}/overlay/generate")
async def generate_overlay(recording_id: str):
    """Start overlay video generation in the background."""
    deps.camera_service.recording_manager.start_overlay_generation(recording_id)
    return deps.camera_service.recording_manager.get_overlay_status(recording_id)


@router.get("/recordings/{recording_id}/overlay/status")
async def overlay_status(recording_id: str):
    """Get overlay generation progress."""
    return deps.camera_service.recording_manager.get_overlay_status(recording_id)


@router.get("/cameras/{camera_id}/result_stream")
async def get_camera_results(camera_id: str):
    """Get JSON stream of processing results for a camera"""
    try:
        return deps.camera_service.generate_result_json_stream(camera_id)
            
    except Exception as e:
        deps.logger.error(f"Failed to get camera results: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/recordings/{recording_id}/download")
async def download_recording(recording_id: str):
    """Download a recorded video file"""
    try:
        file_path = deps.camera_service.get_recording_path(recording_id)
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Recording file not found")
        
        return FileResponse(
            file_path,
            media_type='video/mp4',
            filename=f"recording_{recording_id}.mp4"
        )
    except Exception as e:
        deps.logger.error(f"Failed to download recording: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/recordings/{recording_id}/motion-data")
async def get_motion_data(recording_id: str):
    """Get motion analysis data (velocity, bg_diff, loudness) for a recording"""
    try:
        # Get the recording file path and derive the metrics .txt path
        file_path = deps.camera_service.get_recording_path(recording_id)
        # New per-clip directory format: …/rec_id/video.mp4 → …/rec_id/metrics.txt
        # Legacy flat format: …/cam_id/recording.mp4 → …/cam_id/recording.txt
        if os.path.basename(file_path) == 'video.mp4':
            motion_file_path = os.path.join(os.path.dirname(file_path), 'metrics.txt')
        else:
            motion_file_path = file_path.rsplit('.', 1)[0] + '.txt'
        
        if not os.path.exists(motion_file_path):
            return {"data": []}
        
        motion_data = []
        with open(motion_file_path, 'r') as f:
            lines = f.readlines()
            if not lines:
                return {"data": []}
            
            # Skip header line and parse data
            for i, line in enumerate(lines[1:], start=0):
                line = line.strip()
                if line:
                    try:
                        parts = line.split(',')
                        if len(parts) >= 3:
                            motion_data.append({
                                "time": i,  # Frame index as time
                                "vel": float(parts[0]),
                                "bg_diff": int(parts[1]),
                                "loudness": float(parts[2])
                            })
                    except (ValueError, IndexError):
                        # Skip malformed lines
                        continue
        
        return {"data": motion_data}
    except Exception as e:
        deps.logger.error(f"Failed to get motion data: {e}")
        raise HTTPException(status_code=404, detail=str(e))
