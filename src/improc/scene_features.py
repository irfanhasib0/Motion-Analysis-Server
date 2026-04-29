"""Pure stateless feature-extraction functions for scene trajectory analysis.

All functions take numpy arrays and return scalar floats or small arrays.
No side effects, no class state — easy to unit test with synthetic data.

Used by SceneAnalyzer to compute per-PID, pairwise, and object-interaction
features for fight / burglary scoring.
"""

import numpy as np


# ---------------------------------------------------------------------------
# Per-PID trajectory features
# ---------------------------------------------------------------------------

def loiter_score(positions: np.ndarray) -> float:
    """Ratio of net displacement to total path length.

    Returns a value in [0, 1].
    - Close to 0 → loitering / pacing (lots of distance traveled, little net displacement)
    - Close to 1 → straight-line transit

    Args:
        positions: [N, 2] chronological (x, y) positions.
    """
    if len(positions) < 2:
        return 1.0
    diffs = np.diff(positions, axis=0)
    step_lengths = np.sqrt((diffs ** 2).sum(axis=1))
    total_path = step_lengths.sum()
    if total_path < 1e-6:
        return 1.0  # stationary — not loitering
    net_disp = np.linalg.norm(positions[-1] - positions[0])
    return float(np.clip(1.0 - (net_disp / total_path), 0.0, 1.0))


def trajectory_entropy(velocities: np.ndarray, n_bins: int = 8) -> float:
    """Shannon entropy of velocity direction histogram, normalized to [0, 1].

    High → erratic / unpredictable direction changes (fighting).
    Low  → smooth / directional motion.

    Args:
        velocities: [N, 2] per-frame (dx, dy) displacement vectors.
        n_bins: Number of angular bins (default 8 → 45° each).
    """
    if len(velocities) < 2:
        return 0.0
    # Filter out near-zero vectors (stationary noise)
    speeds = np.sqrt((velocities ** 2).sum(axis=1))
    mask = speeds > 1e-3
    if mask.sum() < 2:
        return 0.0
    angles = np.arctan2(velocities[mask, 1], velocities[mask, 0])  # [-π, π]
    angles = (angles + np.pi) / (2 * np.pi)  # [0, 1]
    hist, _ = np.histogram(angles, bins=n_bins, range=(0, 1))
    hist = hist / hist.sum()
    hist = hist[hist > 0]
    entropy = -np.sum(hist * np.log2(hist))
    max_entropy = np.log2(n_bins)
    return float(entropy / max_entropy) if max_entropy > 0 else 0.0


def velocity_oscillation(speeds: np.ndarray, min_period: int = 2, max_period: int = 10) -> float:
    """Autocorrelation peak in [min_period, max_period] lag range.

    Returns a value in [0, 1].
    High → repetitive rhythmic motion (punching, kicking).
    Low  → no periodic pattern.

    Args:
        speeds: [N] per-frame scalar speeds.
        min_period: Minimum lag (frames) for oscillation detection.
        max_period: Maximum lag (frames).
    """
    n = len(speeds)
    if n < max_period + 2:
        return 0.0
    # Mean-center
    centered = speeds - speeds.mean()
    var = np.dot(centered, centered)
    if var < 1e-8:
        return 0.0
    # Compute normalized autocorrelation at each lag
    best = 0.0
    for lag in range(min_period, min(max_period + 1, n)):
        corr = np.dot(centered[:n - lag], centered[lag:]) / var
        best = max(best, corr)
    return float(np.clip(best, 0.0, 1.0))


def bbox_area_variance(areas: np.ndarray) -> float:
    """Coefficient of variation of bbox areas over a window.

    High → rapid posture changes (fighting causes bbox to grow/shrink).
    Low  → stable posture.

    Args:
        areas: [N] per-frame bbox areas (w × h).
    """
    if len(areas) < 3:
        return 0.0
    mean_area = areas.mean()
    if mean_area < 1e-6:
        return 0.0
    return float(areas.std() / mean_area)


# ---------------------------------------------------------------------------
# Pairwise trajectory features
# ---------------------------------------------------------------------------

def approach_rate(positions_a: np.ndarray, positions_b: np.ndarray) -> float:
    """Rate of change of inter-person distance (pixels/frame).

    Negative → closing fast (approaching each other).
    Positive → separating.

    Uses linear regression of last N distance samples for robustness.

    Args:
        positions_a, positions_b: [N, 2] chronological positions of two persons.
    """
    n = min(len(positions_a), len(positions_b))
    if n < 3:
        return 0.0
    a = positions_a[-n:]
    b = positions_b[-n:]
    dists = np.sqrt(((a - b) ** 2).sum(axis=1))
    # Simple linear fit: slope of distance over time
    t = np.arange(n, dtype=np.float64)
    t_mean = t.mean()
    d_mean = dists.mean()
    slope = np.dot(t - t_mean, dists - d_mean) / max(np.dot(t - t_mean, t - t_mean), 1e-8)
    return float(slope)


def proximity_score(dist: float, bbox_h_a: float, bbox_h_b: float) -> float:
    """Proximity normalized by average bbox height (perspective correction).

    Returns a value in [0, 1].
    High → very close (within ~1 body height).
    Low  → far apart.

    Args:
        dist: Euclidean distance between centroids.
        bbox_h_a, bbox_h_b: Bbox heights of the two persons.
    """
    avg_h = max((bbox_h_a + bbox_h_b) / 2.0, 1.0)
    normalized = dist / avg_h
    # Sigmoid-like mapping: ratio of 1 → score ~0.73, ratio of 2 → score ~0.37
    return float(np.exp(-0.5 * normalized))


# ---------------------------------------------------------------------------
# Object–person interaction features
# ---------------------------------------------------------------------------

def person_object_overlap(person_bbox: tuple, object_bbox: tuple) -> float:
    """IoU of object bbox with the lower half of person bbox.

    Detects carried / held items near hands.

    Args:
        person_bbox: (x1, y1, x2, y2) of person.
        object_bbox: (x1, y1, x2, y2) of object.

    Returns:
        IoU in [0, 1].
    """
    px1, py1, px2, py2 = person_bbox
    # Lower half of person
    mid_y = (py1 + py2) / 2.0
    lx1, ly1, lx2, ly2 = px1, mid_y, px2, py2

    ox1, oy1, ox2, oy2 = object_bbox
    inter_w = max(0.0, min(lx2, ox2) - max(lx1, ox1))
    inter_h = max(0.0, min(ly2, oy2) - max(ly1, oy1))
    inter = inter_w * inter_h
    area_lower = max((lx2 - lx1) * (ly2 - ly1), 1e-6)
    area_obj = max((ox2 - ox1) * (oy2 - oy1), 1e-6)
    union = area_lower + area_obj - inter
    return float(inter / union) if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Zone features
# ---------------------------------------------------------------------------

def point_in_polygon(point: tuple, polygon: np.ndarray) -> bool:
    """Ray-casting point-in-polygon test.

    Args:
        point: (x, y) in normalized [0, 1] coordinates.
        polygon: [K, 2] array of (x, y) vertices.
    """
    x, y = point
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def zone_dwell_frames(centroid_history: np.ndarray, zone_polygon: np.ndarray,
                      frame_w: int, frame_h: int) -> int:
    """Count recent frames where centroid was inside polygon.

    Args:
        centroid_history: [N, 2] pixel coordinates (x, y).
        zone_polygon: [K, 2] normalized [0, 1] polygon vertices.
        frame_w, frame_h: Frame dimensions for normalization.
    """
    if len(centroid_history) == 0 or len(zone_polygon) == 0:
        return 0
    count = 0
    for x, y in centroid_history:
        nx = x / max(frame_w, 1)
        ny = y / max(frame_h, 1)
        if point_in_polygon((nx, ny), zone_polygon):
            count += 1
    return count
