"""
Zone Processor — pure geometry utilities.  No I/O, no threading.

All coordinates are normalised [0.0, 1.0].
All functions convert to pixel space on demand using the supplied frame dims.

Public API consumed by streaming_service.py:
    build_active_mask()   → uint8 numpy mask for MOG2 gating
    classify_detections() → zone_id sets per tracked object
    draw_zones_overlay()  → in-place annotated frame copy
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

import cv2
import numpy as np

from .zone_model import ZoneDefinition, ZoneHitMode, ZoneType


# ─── Coordinate helpers ───────────────────────────────────────────────────────

def _norm_to_px(polygon: List[List[float]], w: int, h: int) -> np.ndarray:
    """Convert a normalised polygon [[x,y], ...] to integer pixel coordinates."""
    pts = np.array([[p[0] * w, p[1] * h] for p in polygon], dtype=np.float32)
    return pts.reshape((-1, 1, 2)).astype(np.int32)


def _hex_to_bgr(color: str) -> Tuple[int, int, int]:
    """#RRGGBB → (B, G, R) BGR tuple for OpenCV."""
    try:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        return (b, g, r)
    except Exception:
        return (0, 255, 0)


# ─── Active-mask helpers ──────────────────────────────────────────────────────

def build_active_mask(
    zones: List[ZoneDefinition],
    frame_h: int,
    frame_w: int,
) -> Optional[np.ndarray]:
    """Build a binary uint8 mask from all enabled ACTIVE_MASK zones.

    The resulting mask is the *union* of all mask polygons (multiple zones
    are OR-ed together).  Returns None when no mask zones are configured,
    which signals the caller to pass the raw frame without modification.

    255 = process this pixel, 0 = ignore.
    """
    mask_zones = [z for z in zones if z.zone_type == ZoneType.ACTIVE_MASK and z.enabled]
    if not mask_zones:
        return None

    mask = np.zeros((frame_h, frame_w), dtype=np.uint8)
    for zone in mask_zones:
        for polygon in zone.polygons:
            if len(polygon) >= 3:
                pts = _norm_to_px(polygon, frame_w, frame_h)
                cv2.fillPoly(mask, [pts], 255)
    return mask


def apply_active_mask(
    frame: np.ndarray,
    mask: Optional[np.ndarray],
) -> np.ndarray:
    """Apply a pre-built active mask to a BGR or grayscale frame.

    Returns the original frame unchanged when mask is None (no mask zones).
    """
    if mask is None:
        return frame
    if frame.ndim == 3:
        # BGR: broadcast mask across all channels
        return cv2.bitwise_and(frame, frame, mask=mask)
    return cv2.bitwise_and(frame, mask)


# ─── Zone-hit classification ──────────────────────────────────────────────────

def _point_in_zone(px: float, py: float, zone: ZoneDefinition, w: int, h: int) -> bool:
    """Return True if pixel point (px, py) is inside any polygon of zone."""
    for polygon in zone.polygons:
        if len(polygon) < 3:
            continue
        pts = _norm_to_px(polygon, w, h)
        if cv2.pointPolygonTest(pts, (float(px), float(py)), measureDist=False) >= 0:
            return True
    return False


def classify_detections(
    flow_pts: Dict,
    zones: List[ZoneDefinition],
    frame_w: int,
    frame_h: int,
) -> Dict[str, List[str]]:
    """Classify every tracked object against active zones.

    Args:
        flow_pts:  The `flow_pts` payload from TrackerResult — a dict keyed
                   by pid/traj_id where each value contains at least 'bbox'
                   ([x1,y1,x2,y2]) or 'centroid' ([row, col]).
        zones:     List of enabled ACTIVE_ZONE definitions.
        frame_w, frame_h: Pixel dimensions of the current frame.

    Returns:
        Mapping  {traj_id: [zone_id, ...]}  — empty list means no zone hit.
        An implicit 'background' entry is added for objects outside all zones
        when any zone has include_background=True.
    """
    active_zones = [z for z in zones if z.zone_type == ZoneType.ACTIVE_ZONE and z.enabled]
    if not active_zones or not flow_pts:
        return {}

    has_background_zone = any(z.include_background for z in active_zones)
    result: Dict[str, List[str]] = {}

    for traj_id, pt_data in flow_pts.items():
        if not isinstance(pt_data, dict):
            continue

        # Determine test points based on hit mode
        bbox = pt_data.get("bbox")          # [x1, y1, x2, y2]
        centroid = pt_data.get("centroid")  # [row, col] (optical-flow convention)

        hit_zones: Set[str] = set()

        for zone in active_zones:
            hit_mode = zone.hit_mode

            if hit_mode == ZoneHitMode.CENTROID:
                if centroid is not None and len(centroid) >= 2:
                    # centroid is [row, col]
                    px, py = float(centroid[1]), float(centroid[0])
                elif bbox is not None and len(bbox) == 4:
                    px = (float(bbox[0]) + float(bbox[2])) / 2
                    py = (float(bbox[1]) + float(bbox[3])) / 2
                else:
                    continue
                if _point_in_zone(px, py, zone, frame_w, frame_h):
                    hit_zones.add(zone.zone_id)

            else:  # BBOX_ANY
                if bbox is None or len(bbox) != 4:
                    continue
                x1, y1, x2, y2 = (float(bbox[0]), float(bbox[1]),
                                   float(bbox[2]), float(bbox[3]))
                test_pts = [
                    (x1, y1), (x2, y1), (x1, y2), (x2, y2),
                    ((x1 + x2) / 2, (y1 + y2) / 2),
                ]
                for px, py in test_pts:
                    if _point_in_zone(px, py, zone, frame_w, frame_h):
                        hit_zones.add(zone.zone_id)
                        break  # one corner is enough

        if not hit_zones and has_background_zone:
            hit_zones.add("background")

        result[traj_id] = list(hit_zones)

    return result


def aggregate_zone_hits(per_object_hits: Dict[str, List[str]]) -> List[str]:
    """Flatten per-object zone hits into a deduplicated list of active zone_ids."""
    ids: Set[str] = set()
    for zones in per_object_hits.values():
        ids.update(zones)
    return sorted(ids)


# ─── Visual overlay ───────────────────────────────────────────────────────────

def draw_zones_overlay(
    frame: np.ndarray,
    zones: List[ZoneDefinition],
    alpha: float = 0.07,
) -> np.ndarray:
    """Draw zone polygons on *a copy* of frame and return the annotated copy.

    ACTIVE_MASK zones:  drawn with a hatched/stripy tint in their colour.
    ACTIVE_ZONE zones:  drawn as a semi-transparent filled polygon + border.
    """
    if not zones:
        return frame

    result = frame.copy()
    overlay = frame.copy()
    h, w = frame.shape[:2]

    # Pass 1 — fills only (blended at low alpha for transparency)
    for zone in zones:
        if not zone.enabled:
            continue
        color = _hex_to_bgr(zone.color)

        for polygon in zone.polygons:
            if len(polygon) < 3:
                continue
            pts = _norm_to_px(polygon, w, h)
            cv2.fillPoly(overlay, [pts], color)

    cv2.addWeighted(overlay, alpha, result, 1 - alpha, 0, result)

    # Pass 2 — dark shadow border first, then colored border on top so the
    # boundary stands out against both bright and dark backgrounds.
    for zone in zones:
        if not zone.enabled:
            continue
        color = _hex_to_bgr(zone.color)

        for polygon in zone.polygons:
            if len(polygon) < 3:
                continue
            pts = _norm_to_px(polygon, w, h)
            # Dark halo (wider, drawn first)
            cv2.polylines(result, [pts], isClosed=True, color=(0, 0, 0), thickness=1, lineType=cv2.LINE_AA)
            # Colored border on top
            cv2.polylines(result, [pts], isClosed=True, color=color, thickness=1, lineType=cv2.LINE_AA)

    # Draw zone name labels (second pass so labels are on top of fills)
    for zone in zones:
        if not zone.enabled:
            continue
        color = _hex_to_bgr(zone.color)
        type_tag = "[M]" if zone.zone_type == ZoneType.ACTIVE_MASK else "[Z]"
        label = f"{type_tag} {zone.name}"

        for polygon in zone.polygons:
            if len(polygon) >= 3:
                pts_arr = np.array(polygon)
                cx = int(np.mean(pts_arr[:, 0]) * w)
                cy = int(np.mean(pts_arr[:, 1]) * h)
                # Shadow for readability on bright backgrounds
                cv2.putText(result, label, (cx - 20, cy),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(result, label, (cx - 20, cy),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
                break  # label once per zone using first polygon

    return result
