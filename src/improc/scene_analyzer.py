"""Stateful per-camera scene analyzer for fight and burglary detection.

Consumes trajectory data from FlowMemory and tracked results from ByteTracker
each frame, computes composite threat scores, and emits alerts with explainable
breakdowns.

Usage:
    analyzer = SceneAnalyzer(config, zone_polygons)
    result = analyzer.update(tracked_results, flow_memory, current_pids, detections)
    # result['fight_score'], result['burglary_score'], result['fight_alert'], ...
"""

import numpy as np
from collections import deque

from improc.scene_features import (
    loiter_score,
    trajectory_entropy,
    velocity_oscillation,
    bbox_area_variance,
    approach_rate,
    proximity_score,
    person_object_overlap,
    zone_dwell_frames,
)


# Default configuration — overridden by system.yaml values
_DEFAULT_CONFIG = {
    'enabled': True,
    'fight_threshold': 0.6,
    'burglary_threshold': 0.5,
    'cooldown_frames': 150,
    'min_persons_for_fight': 2,
    'proximity_distance_ratio': 2.0,
    'proximity_min_frames': 15,
    'trajectory_window': 30,
    'fight_weights': {
        'oscillation': 0.25,
        'entropy': 0.20,
        'approach_rate': 0.25,
        'proximity': 0.20,
        'bbox_variance': 0.10,
    },
    'burglary_weights': {
        'loiter': 0.30,
        'zone_dwell': 0.30,
        'carried_object': 0.25,
        'threat_object': 0.15,
    },
    'threat_classes': ['knife', 'scissors', 'baseball bat'],
    'carry_classes': ['backpack', 'handbag', 'suitcase', 'bottle'],
}


class SceneAnalyzer:
    """Per-camera scene analysis — called once per frame after tracker update.

    Computes two composite threat scores (fight, burglary) from trajectory and
    detection features.  Each score is a weighted linear combination of named
    sub-features, making the output fully explainable.
    """

    def __init__(self, config: dict = None, zone_polygons: list = None):
        """
        Args:
            config:        scene_analysis section from system.yaml (merged with defaults).
            zone_polygons: List of zone dicts from configs/zones/{camera_id}.json.
                           Each dict has 'zone_id', 'polygons' (list of [K,2] normalized),
                           'zone_type', 'enabled'.
        """
        cfg = dict(_DEFAULT_CONFIG)
        if config:
            # Shallow merge, then deep merge weight dicts
            for k, v in config.items():
                if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                    cfg[k] = {**cfg[k], **v}
                else:
                    cfg[k] = v
        self.cfg = cfg

        self.fight_threshold = float(cfg['fight_threshold'])
        self.burglary_threshold = float(cfg['burglary_threshold'])
        self.cooldown_frames = int(cfg['cooldown_frames'])
        self.min_persons = int(cfg['min_persons_for_fight'])
        self.proximity_ratio = float(cfg['proximity_distance_ratio'])
        self.proximity_min_frames = int(cfg['proximity_min_frames'])
        self.traj_window = int(cfg['trajectory_window'])
        self.fight_weights = cfg['fight_weights']
        self.burglary_weights = cfg['burglary_weights']
        self.threat_classes = set(cfg['threat_classes'])
        self.carry_classes = set(cfg['carry_classes'])

        # Zone polygons as numpy arrays for point-in-polygon tests
        self._zones = []
        if zone_polygons:
            for z in zone_polygons:
                if not z.get('enabled', True):
                    continue
                for poly in z.get('polygons', []):
                    self._zones.append({
                        'zone_id': z['zone_id'],
                        'polygon': np.array(poly, dtype=np.float32),
                    })

        # Series history for plotting (same pattern as FrequencyIntensityAnalyzer)
        series_maxlen = int(cfg.get('series_max_points', 300))
        self._fight_series:    deque = deque(maxlen=series_maxlen)
        self._burglary_series: deque = deque(maxlen=series_maxlen)

        # Rolling state (minimal — only what pure functions can't derive)
        self._bbox_history: dict[int, deque] = {}      # pid → deque of area values
        self._proximity_counters: dict[tuple, int] = {} # (pid_a, pid_b) → consecutive frames close
        self._fight_cooldown = 0
        self._burglary_cooldown = 0
        self._frame_w = 0
        self._frame_h = 0

    def set_frame_size(self, w: int, h: int):
        """Set frame dimensions for zone coordinate normalization."""
        self._frame_w = w
        self._frame_h = h

    def update(self, tracked_results: dict, flow_memory, current_pids: list,
               detections: list = None) -> dict:
        """Main per-frame call.

        Args:
            tracked_results: dict[pid, {bbox, type, score, ...}] from ByteTracker.update().
            flow_memory: FlowMemory instance (read-only trajectory access).
            current_pids: list of currently visible PIDs.
            detections: raw YOLOX detection dicts (for object-class filtering).

        Returns:
            dict with fight_score, burglary_score, fight_alert, burglary_alert,
            and an explainable 'details' sub-dict.
        """
        if detections is None:
            detections = []

        # Identify person PIDs
        person_pids = []
        person_bboxes = {}   # pid → (x1, y1, x2, y2)
        person_centroids = {}  # pid → (cx, cy)

        for pid in current_pids:
            wpid = pid % flow_memory.maxpid
            data = tracked_results.get(pid)
            if data is None:
                continue
            bbox = data.get('bbox')
            if bbox is None:
                continue
            x1, y1, x2, y2 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
            # Check if this PID is a person
            type_counts = flow_memory._type_counts.get(wpid, {})
            if any(type_counts.get(t, 0) > 0 for t in flow_memory.PERSON_TYPES):
                person_pids.append(pid)
                person_bboxes[pid] = (x1, y1, x2, y2)
                person_centroids[pid] = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

        # ── Step 1: Per-PID features ──────────────────────────────────────
        pid_features = {}
        for pid in person_pids:
            wpid = pid % flow_memory.maxpid
            features = {}

            # Get trajectory positions (primary keypoint slot 0)
            buf = flow_memory._buffers.get(f'{wpid}|0')
            if buf is not None and buf.count >= 3:
                positions = buf.get_last_n(min(self.traj_window, buf.count))
                diffs = np.diff(positions, axis=0)
                speeds = np.sqrt((diffs ** 2).sum(axis=1))

                features['loiter'] = loiter_score(positions)
                features['entropy'] = trajectory_entropy(diffs)
                features['oscillation'] = velocity_oscillation(speeds)
            else:
                features['loiter'] = 0.0
                features['entropy'] = 0.0
                features['oscillation'] = 0.0

            # Bbox area variance
            bx1, by1, bx2, by2 = person_bboxes[pid]
            area = (bx2 - bx1) * (by2 - by1)
            if pid not in self._bbox_history:
                self._bbox_history[pid] = deque(maxlen=self.traj_window)
            self._bbox_history[pid].append(area)
            if len(self._bbox_history[pid]) >= 3:
                features['bbox_variance'] = bbox_area_variance(
                    np.array(self._bbox_history[pid]))
            else:
                features['bbox_variance'] = 0.0

            # Zone dwell
            features['zone_dwells'] = {}
            if self._zones and buf is not None and buf.count >= 1:
                positions = buf.get_last_n(min(self.traj_window, buf.count))
                for zone in self._zones:
                    dwell = zone_dwell_frames(
                        positions, zone['polygon'], self._frame_w, self._frame_h)
                    if dwell > 0:
                        features['zone_dwells'][zone['zone_id']] = dwell

            pid_features[pid] = features

        # ── Step 2: Pairwise features ─────────────────────────────────────
        max_approach = 0.0
        max_proximity = 0.0
        max_proximity_duration = 0
        new_prox_counters = {}

        n_persons = len(person_pids)
        for i in range(n_persons):
            for j in range(i + 1, n_persons):
                pid_a, pid_b = person_pids[i], person_pids[j]
                wpid_a = pid_a % flow_memory.maxpid
                wpid_b = pid_b % flow_memory.maxpid

                buf_a = flow_memory._buffers.get(f'{wpid_a}|0')
                buf_b = flow_memory._buffers.get(f'{wpid_b}|0')
                if buf_a is None or buf_b is None or buf_a.count < 3 or buf_b.count < 3:
                    continue

                n = min(self.traj_window, buf_a.count, buf_b.count)
                pos_a = buf_a.get_last_n(n)
                pos_b = buf_b.get_last_n(n)

                # Approach rate
                ar = approach_rate(pos_a, pos_b)
                max_approach = min(max_approach, ar)  # most negative = fastest closing

                # Proximity
                cx_a, cy_a = person_centroids[pid_a]
                cx_b, cy_b = person_centroids[pid_b]
                dist = np.sqrt((cx_a - cx_b) ** 2 + (cy_a - cy_b) ** 2)
                h_a = person_bboxes[pid_a][3] - person_bboxes[pid_a][1]
                h_b = person_bboxes[pid_b][3] - person_bboxes[pid_b][1]
                prox = proximity_score(dist, h_a, h_b)
                max_proximity = max(max_proximity, prox)

                # Proximity duration counter
                pair_key = (min(pid_a, pid_b), max(pid_a, pid_b))
                avg_h = max((h_a + h_b) / 2.0, 1.0)
                if dist < self.proximity_ratio * avg_h:
                    prev_count = self._proximity_counters.get(pair_key, 0)
                    new_prox_counters[pair_key] = prev_count + 1
                    max_proximity_duration = max(max_proximity_duration,
                                                 new_prox_counters[pair_key])

        self._proximity_counters = new_prox_counters

        # ── Step 3: Object-person interaction ─────────────────────────────
        threat_objects = []
        carried_objects = []
        max_threat_overlap = 0.0
        max_carry_overlap = 0.0

        for det in detections:
            det_type = det.get('type', '')
            det_bbox = det.get('bbox')
            if det_bbox is None:
                continue
            ob = (float(det_bbox[0]), float(det_bbox[1]),
                  float(det_bbox[2]), float(det_bbox[3]))

            is_threat = det_type in self.threat_classes
            is_carry = det_type in self.carry_classes
            if not is_threat and not is_carry:
                continue

            for pid in person_pids:
                overlap = person_object_overlap(person_bboxes[pid], ob)
                if overlap > 0.05:  # Minimum overlap threshold
                    entry = {'class': det_type, 'near_pid': pid % flow_memory.maxpid,
                             'overlap': round(overlap, 3)}
                    if is_threat:
                        threat_objects.append(entry)
                        max_threat_overlap = max(max_threat_overlap, overlap)
                    if is_carry:
                        carried_objects.append(entry)
                        max_carry_overlap = max(max_carry_overlap, overlap)

        # ── Step 4: Composite scoring ─────────────────────────────────────

        # --- Fight score ---
        fw = self.fight_weights
        if n_persons >= self.min_persons:
            # Worst-case per-PID features (max across all persons)
            max_osc = max((f['oscillation'] for f in pid_features.values()), default=0.0)
            max_ent = max((f['entropy'] for f in pid_features.values()), default=0.0)
            max_bv = max((f['bbox_variance'] for f in pid_features.values()), default=0.0)
            # Clamp bbox_variance to [0, 1] (CV can exceed 1)
            max_bv = min(max_bv, 1.0)

            # Normalize approach rate: map [-20, 0] → [1, 0]
            approach_norm = float(np.clip(-max_approach / 20.0, 0.0, 1.0))

            # Normalize proximity duration
            prox_dur_norm = float(np.clip(
                max_proximity_duration / max(self.proximity_min_frames, 1), 0.0, 1.0))

            fight_score = (
                fw.get('oscillation', 0) * max_osc
                + fw.get('entropy', 0) * max_ent
                + fw.get('approach_rate', 0) * approach_norm
                + fw.get('proximity', 0) * prox_dur_norm
                + fw.get('bbox_variance', 0) * max_bv
            )
        else:
            fight_score = 0.0
            max_osc = max_ent = max_bv = approach_norm = prox_dur_norm = 0.0

        # --- Burglary score ---
        bw = self.burglary_weights
        max_loiter = max((f['loiter'] for f in pid_features.values()), default=0.0)

        # Zone dwell: max normalized dwell across all PIDs and zones
        max_zone_dwell = 0.0
        zone_dwells_detail = {}
        for pid, feats in pid_features.items():
            for zone_id, dwell in feats.get('zone_dwells', {}).items():
                norm_dwell = float(np.clip(dwell / max(self.traj_window, 1), 0.0, 1.0))
                if norm_dwell > max_zone_dwell:
                    max_zone_dwell = norm_dwell
                wpid = pid % flow_memory.maxpid
                zone_dwells_detail.setdefault(wpid, {})[zone_id] = dwell

        burglary_score = (
            bw.get('loiter', 0) * max_loiter
            + bw.get('zone_dwell', 0) * max_zone_dwell
            + bw.get('carried_object', 0) * min(max_carry_overlap * 2.0, 1.0)
            + bw.get('threat_object', 0) * min(max_threat_overlap * 2.0, 1.0)
        )

        # ── Step 5: Alert with cooldown ───────────────────────────────────
        fight_score = float(np.clip(fight_score, 0.0, 1.0))
        burglary_score = float(np.clip(burglary_score, 0.0, 1.0))

        fight_alert = False
        burglary_alert = False

        if self._fight_cooldown > 0:
            self._fight_cooldown -= 1
        if self._burglary_cooldown > 0:
            self._burglary_cooldown -= 1

        if fight_score >= self.fight_threshold and self._fight_cooldown <= 0:
            fight_alert = True
            self._fight_cooldown = self.cooldown_frames

        if burglary_score >= self.burglary_threshold and self._burglary_cooldown <= 0:
            burglary_alert = True
            self._burglary_cooldown = self.cooldown_frames

        # Cleanup stale bbox history for disappeared PIDs
        active_set = set(current_pids)
        stale = [p for p in self._bbox_history if p not in active_set]
        for p in stale:
            del self._bbox_history[p]

        self._fight_series.append(fight_score)
        self._burglary_series.append(burglary_score)

        return {
            'fight_score': round(fight_score, 4),
            'burglary_score': round(burglary_score, 4),
            'fight_series': list(self._fight_series),
            'burglary_series': list(self._burglary_series),
            'fight_alert': fight_alert,
            'burglary_alert': burglary_alert,
            'person_count': n_persons,
            'details': {
                'max_approach_rate': round(max_approach, 3),
                'max_proximity': round(max_proximity, 3),
                'max_proximity_duration': max_proximity_duration,
                'max_oscillation': round(max_osc, 3) if n_persons >= self.min_persons else 0.0,
                'max_entropy': round(max_ent, 3) if n_persons >= self.min_persons else 0.0,
                'max_bbox_variance': round(max_bv, 3) if n_persons >= self.min_persons else 0.0,
                'max_loiter': round(max_loiter, 3),
                'max_zone_dwell': round(max_zone_dwell, 3),
                'zone_dwells': zone_dwells_detail,
                'threat_objects': threat_objects,
                'carried_objects': carried_objects,
                'loiter_scores': {
                    pid % flow_memory.maxpid: round(f['loiter'], 3)
                    for pid, f in pid_features.items()
                },
            },
        }
