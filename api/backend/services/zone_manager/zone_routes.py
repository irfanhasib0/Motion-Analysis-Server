"""
Zone Control API routes — isolated FastAPI plugin.

All zone endpoints live under  /api/cameras/{camera_id}/zones
plus a cross-camera summary at  /api/zones/summary

Integration contract:
    start_server.py must call:
        1.  zones.zone_store.init_zone_store(zones_dir)
        2.  app.include_router(zones.zone_routes.router)
"""

import uuid
from datetime import datetime
from typing import Dict, List

from fastapi import APIRouter, HTTPException

from .zone_model import (
    CameraZoneConfig,
    ZoneCreateRequest,
    ZoneDefinition,
    ZoneToggleRequest,
    ZoneUpdateRequest,
)
from .zone_store import get_zone_store

router = APIRouter(tags=["zones"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _store():
    s = get_zone_store()
    if s is None:
        raise HTTPException(status_code=503, detail="Zone store not initialised")
    return s


def _notify_stream_cache(camera_id: str) -> None:
    """Tell the streaming service to rebuild its zone cache for this camera."""
    try:
        from routes import deps
        svc = getattr(deps, "camera_service", None)
        if svc and hasattr(svc, "invalidate_zone_cache"):
            svc.invalidate_zone_cache(camera_id)
    except Exception:
        pass  # Not fatal — cache will self-heal on next frame


# ── per-camera zone CRUD ──────────────────────────────────────────────────────

@router.get(
    "/api/cameras/{camera_id}/zones",
    response_model=CameraZoneConfig,
    summary="List all zones for a camera",
)
async def list_zones(camera_id: str) -> CameraZoneConfig:
    return _store().get_camera_zones(camera_id)


@router.post(
    "/api/cameras/{camera_id}/zones",
    response_model=ZoneDefinition,
    status_code=201,
    summary="Create a zone for a camera",
)
async def create_zone(camera_id: str, request: ZoneCreateRequest) -> ZoneDefinition:
    store = _store()
    config = store.get_camera_zones(camera_id)
    new_zone = ZoneDefinition(
        zone_id=str(uuid.uuid4()),
        name=request.name,
        color=request.color,
        zone_type=request.zone_type,
        polygons=request.polygons,
        hit_mode=request.hit_mode,
        min_dwell_frames=request.min_dwell_frames,
        enabled=request.enabled,
        include_background=request.include_background,
    )
    config.zones.append(new_zone)
    store.save_camera_zones(config)
    _notify_stream_cache(camera_id)
    return new_zone


@router.put(
    "/api/cameras/{camera_id}/zones/{zone_id}",
    response_model=ZoneDefinition,
    summary="Update a zone",
)
async def update_zone(
    camera_id: str, zone_id: str, request: ZoneUpdateRequest
) -> ZoneDefinition:
    store = _store()
    config = store.get_camera_zones(camera_id)
    zone = next((z for z in config.zones if z.zone_id == zone_id), None)
    if zone is None:
        raise HTTPException(status_code=404, detail=f"Zone {zone_id} not found")
    for field, value in request.dict(exclude_unset=True).items():
        setattr(zone, field, value)
    store.save_camera_zones(config)
    _notify_stream_cache(camera_id)
    return zone


@router.delete(
    "/api/cameras/{camera_id}/zones/{zone_id}",
    summary="Delete a zone",
)
async def delete_zone(camera_id: str, zone_id: str) -> Dict[str, str]:
    store = _store()
    config = store.get_camera_zones(camera_id)
    before = len(config.zones)
    config.zones = [z for z in config.zones if z.zone_id != zone_id]
    if len(config.zones) == before:
        raise HTTPException(status_code=404, detail=f"Zone {zone_id} not found")
    store.save_camera_zones(config)
    _notify_stream_cache(camera_id)
    return {"deleted": zone_id}


@router.patch(
    "/api/cameras/{camera_id}/zones/{zone_id}/enabled",
    response_model=ZoneDefinition,
    summary="Enable or disable a zone",
)
async def toggle_zone(
    camera_id: str, zone_id: str, request: ZoneToggleRequest
) -> ZoneDefinition:
    store = _store()
    config = store.get_camera_zones(camera_id)
    zone = next((z for z in config.zones if z.zone_id == zone_id), None)
    if zone is None:
        raise HTTPException(status_code=404, detail=f"Zone {zone_id} not found")
    zone.enabled = request.enabled
    store.save_camera_zones(config)
    _notify_stream_cache(camera_id)
    return zone


# ── cross-camera summary (used by filter dropdowns) ───────────────────────────

@router.get(
    "/api/zones/summary",
    summary="All zones across all cameras — for filter dropdowns",
)
async def zones_summary() -> Dict[str, CameraZoneConfig]:
    return _store().get_all_zones()
