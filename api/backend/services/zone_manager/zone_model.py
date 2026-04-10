"""
Zone Control — Pydantic models.

Two zone types:
  ACTIVE_MASK  — one composite mask (union of polygons) applied to the
                 grayscale frame before MOG2, so background subtraction
                 only processes the region of interest.  Motion outside
                 the mask is completely suppressed at the pixel level.

  ACTIVE_ZONE  — named regions for analytics tagging.  Each clip's
                 metadata includes which zones had tracked objects and
                 the per-zone motion frame counts.

Coordinates are stored normalised to [0.0, 1.0] so stored configs
survive camera resolution changes without needing migration.

Hit modes (ACTIVE_ZONE only):
  CENTROID  — object must have its centroid inside the polygon  (default)
  BBOX_ANY  — any corner of the bounding box touches the polygon
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ─── Enumerations ────────────────────────────────────────────────────────────

class ZoneType(str, Enum):
    ACTIVE_MASK = "active_mask"
    ACTIVE_ZONE = "active_zone"


class ZoneHitMode(str, Enum):
    CENTROID = "centroid"
    BBOX_ANY  = "bbox_any"


# ─── Core zone definition ─────────────────────────────────────────────────────

class ZoneDefinition(BaseModel):
    """A single named zone that belongs to a camera's config.

    `polygons` is a list of polygons; each polygon is a list of [x, y]
    normalised points.  Supporting multiple polygons per zone lets users
    model non-contiguous areas (e.g. two doorways as one "entry" zone).
    """
    zone_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    color: str = "#00ff00"          # Hex colour used both in UI and frame overlay
    zone_type: ZoneType
    polygons: List[List[List[float]]]  # [ [[x,y], ...], [[x,y], ...], ... ]
    hit_mode: ZoneHitMode = ZoneHitMode.CENTROID
    min_dwell_frames: int = Field(default=1, ge=1,
        description="Frames an object must remain in the zone before a hit is counted "
                    "(reduces transient false-positives)")
    enabled: bool = True
    include_background: bool = Field(default=False,
        description="ACTIVE_ZONE only — also tag motion that falls outside *all* zones "
                    "as the implicit 'background' zone")


class CameraZoneConfig(BaseModel):
    """All zone definitions for one camera."""
    camera_id: str
    zones: List[ZoneDefinition] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ─── Request / response helpers ───────────────────────────────────────────────

class ZoneCreateRequest(BaseModel):
    name: str
    color: str = "#00ff00"
    zone_type: ZoneType
    polygons: List[List[List[float]]]
    hit_mode: ZoneHitMode = ZoneHitMode.CENTROID
    min_dwell_frames: int = Field(default=1, ge=1)
    enabled: bool = True
    include_background: bool = False


class ZoneUpdateRequest(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    polygons: Optional[List[List[List[float]]]] = None
    hit_mode: Optional[ZoneHitMode] = None
    min_dwell_frames: Optional[int] = Field(default=None, ge=1)
    enabled: Optional[bool] = None
    include_background: Optional[bool] = None


class ZoneToggleRequest(BaseModel):
    enabled: bool


# ─── Analytics summary (embedded in recording metadata) ──────────────────────

class ZoneMotionSummary(BaseModel):
    """Stored inside Recording.metadata['zone_summary']."""
    zones_active: List[str] = Field(default_factory=list,
        description="zone_ids with at least one detected object during the clip")
    zone_motion_counts: Dict[str, int] = Field(default_factory=dict,
        description="Per-zone accumulated frame-hit counts throughout the clip")
    zone_vel_max: Dict[str, float] = Field(default_factory=dict,
        description="Max observed velocity per zone during the clip")
