"""System configuration, presets, and health monitoring endpoints."""
from fastapi import APIRouter, HTTPException

from models.requests import (
    LiveStreamModeRequest,
    SystemSettingsUpdateRequest,
    PerformanceProfileUpdateRequest,
)
from routes import deps

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/system/info")
async def get_system_info():
    """Get overall system and start_server process metrics for dashboard."""
    try:
        info = deps.dashboard_service.get_system_info()
        mode = deps.get_live_stream_mode()
        info['settings'] = {
            'live_stream_mode': mode if mode in {"mjpeg", "hls", "ws"} else "mjpeg",
            **deps.camera_service.get_runtime_settings(),
        }
        return info
    except Exception as e:
        deps.logger.error(f"Failed to get system info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system/settings")
async def get_system_settings():
    mode = deps.get_live_stream_mode()
    normalized_mode = mode if mode in {"mjpeg", "hls", "ws"} else "mjpeg"
    runtime_settings = deps.camera_service.get_runtime_settings()
    return {
        'live_stream_mode': normalized_mode,
        **runtime_settings,
        'supported_live_stream_modes': ['mjpeg', 'hls', 'ws'],
        'sensitivity_range': {'min': 0, 'max': int(getattr(deps.camera_service, 'sensitivity_level', 5) or 5)},
        'jpeg_quality_range': {'min': 25, 'max': 95},
        'pipe_buffer_size_range': {'min': 65536, 'max': 268435456},
        'max_vel_range': {'min': 0.0, 'max': 5.0},
        'bg_diff_range': {'min': 1, 'max': 5000},
        'max_clip_length_range': {'min': 5, 'max': 600},
        'motion_check_interval_range': {'min': 1, 'max': 120},
    }


@router.put("/system/settings")
async def update_system_settings(payload: SystemSettingsUpdateRequest):
    if payload.live_stream_mode is not None:
        requested_mode = str(payload.live_stream_mode or '').strip().lower()
        if requested_mode not in {"mjpeg", "hls", "ws"}:
            raise HTTPException(status_code=400, detail="Invalid live_stream_mode. Supported: mjpeg, hls, ws")
        deps.set_live_stream_mode(requested_mode)

    # Collect non-None settings into a dict for custom preset save
    custom_updates = {}
    for field in ('sensitivity', 'jpeg_quality', 'pipe_buffer_size', 'max_vel',
                   'bg_diff', 'max_clip_length', 'motion_check_interval',
                   'min_free_storage_gb', 'rtsp_unified_demux_enabled',
                   'frame_rbf_len', 'audio_rbf_len', 'results_rbf_len', 'mux_realtime',
                   'auto_archive_days'):
        val = getattr(payload, field, None)
        if val is not None:
            custom_updates[field] = val

    if payload.live_stream_mode is not None:
        custom_updates['live_stream_mode'] = deps.get_live_stream_mode()

    if custom_updates:
        deps.camera_service.save_custom_settings(custom_updates)

    runtime_settings = deps.camera_service.get_runtime_settings()
    return {
        'message': 'System settings updated',
        'live_stream_mode': deps.get_live_stream_mode(),
        **runtime_settings,
        'supported_live_stream_modes': ['mjpeg', 'hls', 'ws'],
    }


@router.get("/system/live-stream-mode")
async def get_live_stream_mode():
    """Get server-configured default live stream mode."""
    mode = deps.get_live_stream_mode()
    normalized_mode = mode if mode in {"mjpeg", "hls", "ws"} else "mjpeg"
    return {
        "mode": normalized_mode,
        "live_stream_mode": normalized_mode,
        "supported_modes": ["mjpeg", "hls", "ws"],
    }


@router.post("/system/live-stream-mode")
async def set_live_stream_mode(payload: LiveStreamModeRequest):
    """Update server default live stream mode at runtime."""
    requested_mode = str(payload.mode or "").strip().lower()
    if requested_mode not in {"mjpeg", "hls", "ws"}:
        raise HTTPException(status_code=400, detail="Invalid mode. Supported: mjpeg, hls, ws")

    deps.set_live_stream_mode(requested_mode)
    mode = deps.get_live_stream_mode()
    return {
        "message": "Live stream mode updated",
        "mode": mode,
        "live_stream_mode": mode,
        "supported_modes": ["mjpeg", "hls", "ws"],
    }


@router.get("/system/presets")
async def get_system_presets():
    """Get available system presets from configuration."""
    try:
        presets = deps.camera_service.db.get_presets()
        sys_settings = deps.camera_service.get_system_settings()
        active_preset = sys_settings['active_preset']
        
        return {
            'presets': presets,
            'active_preset': active_preset,
            'available_presets': list(presets.keys())
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get presets: {str(e)}")


@router.put("/system/performance-profile")
async def update_performance_profile(payload: PerformanceProfileUpdateRequest):
    """Apply a performance preset or update custom settings."""
    try:
        preset_name = payload.preset_name
        
        if preset_name == 'custom':
            # Collect non-None custom setting values
            custom_settings = {}
            for field, value in payload.dict(exclude={'preset_name'}).items():
                if value is not None:
                    custom_settings[field] = value
            
            if custom_settings:
                updated_settings = deps.camera_service.save_custom_settings(custom_settings)
                # Sync live_stream_mode global if it was changed
                if 'live_stream_mode' in custom_settings:
                    mode = str(custom_settings['live_stream_mode']).lower()
                    if mode in ('mjpeg', 'hls'):
                        deps.set_live_stream_mode(mode)
                return {
                    'message': 'Custom performance profile updated',
                    'active_preset': 'custom',
                    'settings': updated_settings
                }
            else:
                # Just switch to custom without changing values
                updated_settings = deps.camera_service.apply_preset('custom')
                return {
                    'message': 'Switched to custom profile',
                    'active_preset': 'custom',
                    'settings': updated_settings
                }
        else:
            # Apply named preset (default or low_power) - read-only switch
            updated_settings = deps.camera_service.apply_preset(preset_name)
            # Sync live_stream_mode global from the preset
            deps.set_live_stream_mode(
                str(updated_settings.get('live_stream_mode', deps.get_live_stream_mode())).lower()
            )
            
            return {
                'message': f'Applied {preset_name} performance profile',
                'active_preset': preset_name,
                'settings': updated_settings
            }
            
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update performance profile: {str(e)}")


@router.get("/cameras/{camera_id}/stream-health")
async def get_camera_stream_health(camera_id: str):
    """Get stream health status for a specific camera."""
    try:
        camera = deps.camera_service.get_camera(camera_id)
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")
        
        health_status = deps.dashboard_service.stream_monitor.get_health_status(camera_id)
        return health_status
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stream health: {str(e)}")


@router.get("/system/stream-health")
async def get_all_cameras_stream_health():
    """Get stream health status for all cameras."""
    cameras = deps.camera_service.get_cameras()
    health_statuses = {}
    
    for camera in cameras:
        if camera.id in deps.camera_service._camera_streams:
            health_statuses[camera.id] = deps.dashboard_service.stream_monitor.get_health_status(camera.id)
    
    return {
        'cameras': health_statuses,
        'monitoring_config': {
            'lag_threshold': deps.dashboard_service.stream_monitor.lag_threshold,
            'slow_recovery_interval': deps.dashboard_service.stream_monitor.slow_recovery_interval,
            'enable_slow_recovery_threshold': deps.dashboard_service.stream_monitor.enable_slow_recovery_threshold,
        }
    }


@router.get("/system/lag-history")
async def get_all_lag_history():
    """Get 24h lag history for all cameras for dashboard plotting."""
    try:
        return deps.dashboard_service.stream_monitor.get_lag_history()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get lag history: {str(e)}")


@router.get("/system/resource-history")
async def get_resource_history():
    """Get 24h CPU and memory usage history for dashboard plotting."""
    try:
        return deps.dashboard_service.get_resource_history()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get resource history: {str(e)}")


@router.get("/cameras/{camera_id}/lag-history")
async def get_camera_lag_history(camera_id: str):
    """Get 24h lag history for a specific camera."""
    try:
        camera = deps.camera_service.get_camera(camera_id)
        if not camera:
            raise HTTPException(status_code=404, detail="Camera not found")
        return deps.dashboard_service.stream_monitor.get_lag_history(camera_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get lag history: {str(e)}")
