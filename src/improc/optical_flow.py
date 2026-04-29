"""
Optical Flow Tracker with Background Subtraction

This module provides an OpticalFlowTracker class that uses background subtraction (MOG2)
for motion detection combined with optical flow for tracking.

Features:
- Background subtraction using MOG2 for foreground detection
- Keypoint detection and tracking within detected regions
- Multiple detection modes: 'fast', 'accurate'
- Trajectory memory and coreset-based motion analysis

Example Usage:
    # Create tracker
    tracker = OpticalFlowTracker()
    
    # Set detection mode
    tracker.set_detection_method('fast')
    
    # Process frame
    points_dict = tracker.detect(frame)
    
    # Switch to accurate mode
    tracker.set_detection_method('accurate')

Detection Modes:
- 'fast': Quick detection using only centroids, good for real-time
- 'accurate': Full keypoint detection and optical flow tracking
"""

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment
from trackers.trackers import SimpleTracker, ByteTracker
from improc.memory import FlowMemory, CoresetMemory
from improc.shared_detectors import get_shared_yolox, get_shared_person_detector, get_shared_rtmpose
from improc.scene_analyzer import SceneAnalyzer


def _copy_pts(pts: dict) -> dict:
    """Fast copy of points dict — replaces expensive deepcopy on dict-of-dict-of-ndarray."""
    return {
        k: {key: val.copy() if isinstance(val, np.ndarray) else val
             for key, val in v.items()}
        for k, v in pts.items()
    }

class OpticalFlowTracker:
    """Motion detection and tracking pipeline.

    Combines MOG2 background subtraction for motion region detection with
    Lucas-Kanade sparse optical flow for keypoint tracking.  Each detected
    region is assigned a persistent ID via SimpleTracker; keypoints inside the
    region are tracked across frames and their trajectories stored in FlowMemory.

    Pipeline per frame (accurate mode):
        1. _detect_foreground_boxes  — MOG2 → contours → bboxes + optional YOLOX/person det
        2. SimpleTracker.update      — assign persistent IDs with CamShift refinement
        3. _detect_keypoints_in_boxes — detect SIFT/FAST/GFTT keypoints inside each bbox
        4. _compute_sparse_flow      — LK optical flow (forward + backward consistency)
        5. _detect_and_match         — match new detections to tracked flow points
        6. memory.add                — store per-keypoint positions in ring buffers
        7. memory.get_traj_velocities — compute per-trajectory speed for output

    Pipeline per frame (fast mode):
        Steps 1-2 only, using bbox centroids instead of keypoints (no optical flow).
    """

    def __init__(self,
                 mem_min_traj_len=10,
                 mem_max_traj_len=100,
                 mem_max_pid=2500,
                 mem_keep_last_seen=90,
                 coreset_k=2,
                 matcher_mode="hungarian",
                 det_method="fast",
                 bg_indoor=True,
                 bg_detect_shadow=True,
                 iou_threshold=0.5,
                 enable_yolox=False,
                 enable_person_detection=False,
                 yolox_model_size='nano',
                 yolox_score_thr=0.5,
                 yolox_backend='ncnn',
                 yolox_bg_diff_threshold=1000,
                 yolox_max_vel_threshold=1.5,
                 enable_pose=False,
                 pose_model_size='tiny',
                 pose_score_thr=0.3,
                 enable_sub_blob=True,
                 scene_analysis_config=None):

        # --- Frame state ---
        self.prev_gray = None   # Previous grayscale frame for flow computation
        self.prev_pts  = None   # Previous tracked points dict {pid: {keypoints_1, keypoints_2, bbox, ...}}
        self.mask      = None   # Fading trail overlay for flow visualization
        self.viz_pos   = None
        self.viz_vel   = None
        self.fg_mask   = 0      # Running foreground mask (blended across frames)
        self.count     = 0      # Frame counter (used for detection frequency)

        # --- Detection parameters ---
        self.det_method = det_method             # 'fast' (centroid-only) or 'accurate' (keypoints + flow)
        self.kpt_det_freq = 1                    # Re-detect keypoints every N frames in accurate mode
        self.kpt_det_idx = 1                     # Keypoint detector: 0=FAST, 1=SIFT, 2=ORB, 3=GFTT
        self.kpt_max_kpts = 5                    # Max keypoints per bbox (top by response score)
        self.num_traj_viz = 5                    # Number of trajectories to visualize

        # --- Background subtraction parameters ---
        self.bg_min_bbox_area = 500              # Ignore contours smaller than this (pixels²)
        self.bg_min_pix_thr = 200                # Foreground mask threshold (gray → binary)
        self.bg_mask_dilate_ksize = (3, 3)       # Morphology kernel size for mask cleanup
        self.bf_detect_shadow = bg_detect_shadow
        self.bg_shadow_pixel_value = 127         # MOG2 shadow pixel marker value
        # LUT that maps shadow pixels (127) → 0 while preserving foreground (255).
        # Used by _detect_foreground_boxes to strip shadows without a temporary bool array.
        _lut = np.zeros(256, dtype=np.uint8)
        _lut[255] = 255
        self._shadow_lut = _lut

        # --- Matching parameters ---
        self.mtc_max_cost_thr = 50               # Max L2² distance for keypoint assignment
        self.iou_threshold = iou_threshold       # IoU overlap to merge detection sources
        self.matcher_mode = matcher_mode         # 'hungarian' (robust) or 'greedy' (faster)

        # --- Visualization colors (8 base colors × 10 repeats) ---
        _base_colors = [
            (0, 255, 0), (255, 0, 0), (0, 0, 255), (255, 255, 0),
            (255, 0, 255), (0, 255, 255), (128, 255, 0), (255, 128, 0)
        ]
        self.colors = _base_colors * 10
        self.n_colors = len(self.colors)

        # --- MOG2 background subtractor ---
        # Indoor: high sensitivity (short history, low variance threshold)
        # Outdoor: low sensitivity (longer history, higher variance threshold)
        if bg_indoor:
            shadow_thr = 0.7    # Lower = more aggressive shadow filtering
            history = 50        # Shorter history → adapts faster to indoor lighting changes
            var_thr = 16        # Lower = more sensitive to small changes
        else:
            shadow_thr = 0.5
            history = 200
            var_thr = 30

        self.bgsub = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=var_thr, detectShadows=self.bf_detect_shadow)
        if self.bf_detect_shadow:
            self.bgsub.setShadowValue(self.bg_shadow_pixel_value)
            self.bgsub.setShadowThreshold(shadow_thr)

        # --- Keypoint detectors ---
        self.fast = cv2.FastFeatureDetector_create(threshold=25, nonmaxSuppression=True)
        self.sift = cv2.SIFT_create(nfeatures=100, contrastThreshold=0.04, edgeThreshold=10, sigma=1.6, nOctaveLayers=3)
        self.orb  = cv2.ORB_create(nfeatures=24)
        self.gftt = cv2.GFTTDetector_create(maxCorners=100, qualityLevel=0.1, minDistance=10, blockSize=10)

        # --- Lucas-Kanade optical flow parameters ---
        self.flow_params = dict(
            winSize=(9, 9),
            maxLevel=1,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 7, 0.03),
        )

        # --- Object tracker (assigns persistent IDs across frames) ---
        #self.tracker = SimpleTracker(max_disappeared=10, max_distance=400)
        self.tracker = ByteTracker()

        # --- Trajectory memory ---
        self.memory = FlowMemory(
            maxpid=mem_max_pid, min_traj_len=mem_min_traj_len,
            max_traj_len=mem_max_traj_len, keep_last_seen=mem_keep_last_seen)
        # Coreset memory (PatchCore-like) for representative motion trajectories
        self.coreset = CoresetMemory(sample_len=mem_max_traj_len, max_items=mem_max_traj_len)
        self.coreset_k = coreset_k

        # --- Optional deep-learning detectors ---
        self.person_detector = get_shared_person_detector() if enable_person_detection else None
        self.enable_person_detection = self.person_detector is not None

        yolox = get_shared_yolox(model_size=yolox_model_size, score_thr=yolox_score_thr) if enable_yolox else None
        self.yolox_detector = yolox
        self.enable_yolox = yolox is not None and yolox.is_enabled()

        # Expand YOLOX target classes for scene analysis (backpack, knife, etc.)
        if self.enable_yolox and scene_analysis_config:
            extra_ids = scene_analysis_config.get('yolox_extra_classes', [])
            if extra_ids:
                self.yolox_detector.target_class_ids = self.yolox_detector.target_class_ids | set(int(c) for c in extra_ids)

        # Motion-gating for YOLOX: skip inference when scene is idle.
        # Thresholds sourced from system.yaml (same values used by the recording motion gate).
        self._yolox_bg_diff_threshold = yolox_bg_diff_threshold
        self._yolox_max_vel_threshold = yolox_max_vel_threshold
        self._yolox_cooldown_frames = 30    # frames to keep inferring after motion stops
        self._yolox_idle_frames = 0         # frames elapsed since last motion above threshold
        self._last_max_vel = 0.0            # max mean_vel observed in previous frame
        # Temporal skip: run full YOLOX inference every N active frames.
        # Between detections ByteTracker + CamShift propagates existing boxes.
        self._yolox_det_interval = 3        # run inference every 3rd motion-active frame
        self._yolox_det_counter = 0         # counts frames since last inference
        self._last_yolox_dets = []          # cached detections replayed on skipped frames
        self._sub_blob_area_ratio = 0.20    # contour area < this × det area → sub-blob candidate
        self.enable_sub_blob = enable_sub_blob  # Enable sub-blob tagging via contour_in_dets

        # --- Optional RTMPose keypoint estimator ---    
        pose_detector = get_shared_rtmpose(model_size=pose_model_size, score_thr=pose_score_thr) if enable_pose else None
        self.pose_detector = pose_detector
        self.enable_pose = pose_detector is not None and pose_detector.is_enabled()    

        self.curr_frame = None   # Latest BGR frame (used by pose detector)
        self.viz_div_h = None

        # --- Scene trajectory analyzer (fight / burglary detection) ---
        self.scene_analyzer = None
        if scene_analysis_config and scene_analysis_config.get('enabled', False):
            self.scene_analyzer = SceneAnalyzer(config=scene_analysis_config)

    def restart(self, matcher_mode="hungarian"):
        self.__init__(matcher_mode=matcher_mode)
        print("OpticalFlowTracker restarted.")
    
    def set_detection_method(self, mode):
        self.det_method = mode  # 'fast', 'accurate'
    
    def get_detection_method(self):
        return self.det_method
    
    def set_person_detection_enabled(self, enabled):
        """Enable or disable person detection feature"""
        if enabled and self.person_detector is None:
            self.person_detector = get_shared_person_detector()
        self.enable_person_detection = enabled and (self.person_detector is not None)
        if self.enable_person_detection:
            print("Person detection enabled.")
        else:
            print("Person detection disabled.")
    
    def set_face_detection_enabled(self, enabled):
        """Enable or disable face detection specifically"""
        if self.person_detector is not None:
            self.person_detector.set_face_enabled(enabled)
    
    def set_body_detection_enabled(self, enabled):
        """Enable or disable body detection specifically"""
        if self.person_detector is not None:
            self.person_detector.set_body_enabled(enabled)
    
    def is_person_detection_enabled(self):
        """Check if person detection is enabled"""
        return self.enable_person_detection
    
    def set_yolox_enabled(self, enabled):
        if enabled and self.yolox_detector is None:
            self.yolox_detector = get_shared_yolox()
        self.enable_yolox = enabled and (self.yolox_detector is not None) and self.yolox_detector.is_enabled()
    
    def is_yolox_enabled(self):
        return self.enable_yolox

    def set_pose_detection_enabled(self, enabled):
        """Enable or disable RTMPose keypoint estimation."""
        if enabled and self.pose_detector is None:
            try:
                self.pose_detector = get_shared_rtmpose()
                self.enable_pose = self.pose_detector.is_enabled()
            except FileNotFoundError as e:
                print(f"Warning: RTMPose disabled — {e}")
                return
        if self.pose_detector is not None:
            self.pose_detector.set_enabled(enabled)
        self.enable_pose = enabled and (self.pose_detector is not None) and self.pose_detector.is_enabled()

    def is_pose_detection_enabled(self):
        return self.enable_pose

    def get_pose_status(self):
        if self.pose_detector is None:
            return {'enabled': False}
        status = self.pose_detector.get_status()
        status['enabled'] = self.enable_pose
        return status
    
    def get_yolox_status(self):
        if self.yolox_detector is None:
            return {'enabled': False, 'latency_ms': None, 'last_call_s': None}
        status = self.yolox_detector.get_status()
        status['enabled'] = self.enable_yolox
        return status
    
    def get_person_detection_status(self):
        """Get detailed status of person detection features"""
        if self.person_detector is None:
            return {'enabled': False, 'face': False, 'body': False,
                    'latency_ms': None, 'last_call_s': None}

        s = self.person_detector.get_status()
        return {
            'enabled': self.enable_person_detection,
            'face': s.get('face_enabled', False),
            'body': s.get('body_enabled', False),
            'latency_ms': s.get('latency_ms'),
            'last_call_s': s.get('last_call_s'),
        }

    def get_person_stats(self, frame_shape=None):
        """Return person count, inter-person density, and per-PID bbox lists.

        Density is computed as::

            person_density = person_count / mean_normalized_distance

        where *mean_normalized_distance* is the average pairwise Euclidean
        distance between person centroids, divided by the mean bbox height
        (a perspective-correction proxy).  Higher value → people are packed
        closer together relative to their apparent size.

        When fewer than 2 persons are visible, density is 0.

        Args:
            frame_shape: (H, W) tuple used for edge-cropping filter on person bboxes.

        Returns:
            dict with keys:
              'person_count', 'person_density', 'avg_person_conf'  — aggregates
              'person_bboxes'     — list of {pid, bbox, score} for person-type PIDs
                                    passing aspect-ratio, area, edge, and duration filters
              'all_object_bboxes' — list of {pid, bbox, score, type} for all tracked
                                    PIDs passing area and duration filters (for thumbnails)
        """
        current_pids = list(self.prev_pts.keys()) if self.prev_pts else []
        person_count = self.memory.get_person_count(current_pids)

        person_density = 0.0
        if person_count >= 2 and self.prev_pts:
            # Collect centroids and bbox heights for person-class PIDs
            centroids = []
            heights = []
            for pid in current_pids:
                wpid = pid % self.memory.maxpid
                if self.memory.classify_pid(wpid) not in self.memory.PERSON_TYPES:
                    continue
                bbox = self.prev_pts[pid]['bbox']
                cx = (bbox[0] + bbox[2]) / 2.0
                cy = (bbox[1] + bbox[3]) / 2.0
                h = abs(bbox[3] - bbox[1])
                centroids.append((cx, cy))
                heights.append(max(h, 1.0))

            if len(centroids) >= 2:
                pts = np.array(centroids, dtype=np.float32)
                avg_h = float(np.mean(heights))
                # Pairwise Euclidean distances (upper triangle)
                total_dist = 0.0
                n_pairs = 0
                for i in range(len(pts)):
                    for j in range(i + 1, len(pts)):
                        d = np.sqrt((pts[i, 0] - pts[j, 0]) ** 2 + (pts[i, 1] - pts[j, 1]) ** 2)
                        total_dist += d
                        n_pairs += 1
                mean_norm_dist = (total_dist / n_pairs) / avg_h if n_pairs > 0 else 0.0
                person_density = person_count / mean_norm_dist if mean_norm_dist > 0 else 0.0

        # Per-PID bbox lists and confidence aggregation
        avg_person_conf = 0.0
        # One unified list: all tracked objects that pass every filter.
        # Persons additionally carry is_person=True for crop extraction.
        thumbnail_bboxes = []
        person_scores = []

        if self.prev_pts:
            for pid in current_pids:
                wpid = pid % self.memory.maxpid
                obj_data = self.prev_pts[pid]
                bbox = obj_data.get('bbox')
                score = float(obj_data.get('score', 0.0))

                if bbox is None:
                    continue

                x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
                bw = max(x2 - x1, 1)
                bh = max(y2 - y1, 1)
                area = bw * bh

                # Area filter
                if area < 500 or area > 200000:
                    continue

                # Motion-duration filter
                total_seen = self.memory.pid_hist.get(wpid, {}).get('total_seen', 0)
                if total_seen < self.memory.min_traj_len:
                    continue

                # Movement filter: travel bounding box must exceed the object's own size.
                traj_w, traj_h = self.memory.get_pid_traj_span(wpid)
                if traj_h <= 0.2 * bh and traj_w <= 0.2 * bw:
                    continue

                obj_type = self.memory.classify_pid(wpid)
                type_counts = self.memory._type_counts.get(wpid, {})
                is_person = any(type_counts.get(t, 0) > 0 for t in self.memory.PERSON_TYPES)

                # Person-specific: aspect ratio filter (must be taller than wide)
                if is_person and bh / bw < 0.8:
                    continue

                thumbnail_bboxes.append({
                    'pid': wpid, 'bbox': (x1, y1, x2, y2),
                    'score': score, 'type': obj_type, 'is_person': is_person,
                })
                if is_person:
                    person_scores.append(score)

        if person_scores:
            avg_person_conf = float(np.mean(person_scores))

        return {
            'person_count': person_count,
            'person_density': round(person_density, 4),
            'avg_person_conf': round(avg_person_conf, 4),
            'thumbnail_bboxes': thumbnail_bboxes,
        }

    def _compute_dense_flow(self, prev_gray, gray):
        """Compute dense optical flow (Farneback) and return an HSV visualization."""
        flow = cv2.calcOpticalFlowFarneback(
            prev_gray, gray, None,
            pyr_scale=0.5, levels=3, winsize=15,
            iterations=3, poly_n=5, poly_sigma=1.2, flags=0
        )
        mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1], angleInDegrees=True)
        hsv = np.zeros((gray.shape[0], gray.shape[1], 3), dtype=np.uint8)
        hsv[..., 0] = (ang / 2).astype(np.uint8)       # Hue from angle
        hsv[..., 1] = cv2.normalize(mag, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        hsv[..., 2] = 255                               # Full value
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    
    def contour_in_dets(self, cnt_box, det_id, dets):
        """Check cnt_box overlap against all items in dets.

        cnt_box is a YOLOX det bbox; dets is the current results list.

        Returns True  — cnt_box overlaps a non-motion item (Haar/existing DL box):
                        caller should suppress adding the YOLOX det.
        Returns False — either no overlap, or the overlapping motion box was
                        removed from dets so the YOLOX box replaces it cleanly.

        Side-effects when det_id is not None:
          - Partial-overlap motion boxes (0 < IoU < threshold, area < ratio) are
            recorded in self._sub_blob_iou_map keyed by id(motion_dict) so that
            _tag_sub_blobs can assign them without recomputing IoU.
        """
        x1, y1, x2, y2 = cnt_box
        cnt_area = max((x2 - x1) * (y2 - y1), 1)
        best_iou = 0.0
        best_idx = -1
        
        for i, det in enumerate(dets):
            if not det.get('type') == 'motion':
                continue  # Motion boxes are allowed to overlap and be replaced by YOLOX detections.
            dx1, dy1, dx2, dy2 = det['bbox']
            inter_w = min(x2, dx2) - max(x1, dx1)
            inter_h = min(y2, dy2) - max(y1, dy1)
            #if inter_w <= 0 or inter_h <= 0:
            #    continue
            inter = inter_w * inter_h
            det_area = (dx2 - dx1) * (dy2 - dy1)
            iou = inter / (cnt_area + det_area - inter)
            if det_id is not None:
                # Record partial-overlap motion boxes for _tag_sub_blobs (no recomputation needed)
                if (0 < iou < self.iou_threshold): # det_area < self._sub_blob_area_ratio * cnt_area
                    if iou > det.get('_sub_blob_iou', 0.0):
                        det['_sub_blob_det_id'] = det_id
                        det['_sub_blob_iou'] = iou
                        det.pop('_sub_blob_dist', None)  # Remove any existing distance record since IoU is higher
                # motion box with no intersection → record normalized distance for potential sub-blob tagging
                elif iou <= 0.0 and '_sub_blob_iou' not in det:
                    norm_dist = np.sqrt((max(x1, dx1) - min(x2, dx2)) ** 2 + (max(y1, dy1) - min(y2, dy2)) ** 2)/cnt_area
                    if det.get('_sub_blob_dist', float('inf')) > norm_dist:
                        det['_sub_blob_det_id'] = det_id
                        det['_sub_blob_dist'] = norm_dist

            if iou > best_iou:
                best_iou = iou
                best_idx = i

        if best_iou >= self.iou_threshold and best_idx >= 0:
            if dets[best_idx].get('type') == 'motion':
                # Remove the motion box; caller adds the YOLOX box directly.
                dets.pop(best_idx)
            # Overlaps a Haar or existing DL box — suppress the YOLOX det.
            
    def _tag_sub_blobs(self, results):
        """Tag motion boxes as sub-blobs using IoU data stored by contour_in_dets.

        contour_in_dets writes '_sub_blob_det_id' and '_sub_blob_iou' directly
        onto each qualifying motion box dict, so no separate map or recomputation
        is needed here.
        """
        for res in results:
            if res.get('type') != 'motion':
                continue
            det_id = res.get('_sub_blob_det_id')
            if det_id is None:
                continue
            res['type'] = f'kpt_{det_id}'
            res['_parent_det_id'] = det_id
            # _sub_blob_iou already present on the dict

    def _detect_foreground_boxes(self, gray):
        """Detect motion regions via MOG2 background subtraction.

        Steps:
            1. Apply MOG2 to get raw foreground mask, remove shadow pixels
            2. Blend with previous mask for temporal smoothing
            3. Morphological cleanup (erode + dilate) to reduce noise
            4. Collect detection boxes from: person detector, YOLOX, contours
            5. Assign persistent IDs with SimpleTracker + CamShift bbox refinement

        Returns:
            dict[int, dict]: Tracked results keyed by persistent object ID.
                Each value contains 'bbox', 'bbox_xywh', 'centroid', 'mask', 'type'.
        """
        # --- 1. Background subtraction ---
        prev_fg_mask = self.fg_mask.copy() if isinstance(self.fg_mask, np.ndarray) else self.fg_mask
        bg_mask = self.bgsub.apply(gray)
        if self.bf_detect_shadow:
            cv2.LUT(bg_mask, self._shadow_lut, dst=bg_mask)

        # Temporal blend: 50% previous + 50% current for smoother transitions
        # On the very first frame fg_mask is still int(0) — initialize it so dst= works in-place.
        if not isinstance(self.fg_mask, np.ndarray):
            self.fg_mask = bg_mask.copy()
        else:
            cv2.addWeighted(self.fg_mask, 0.5, bg_mask, 0.5, 0, dst=self.fg_mask)

        # Compute background change metric (used in velocity visualization)
        if isinstance(prev_fg_mask, int):
            prev_fg_mask = self.fg_mask.copy()  # First frame — no previous mask
        diff_mask = cv2.absdiff(self.fg_mask, prev_fg_mask)
        count = cv2.countNonZero(diff_mask)
        self.bg_diff = cv2.sumElems(diff_mask)[0] / count if count > 0 else 0

        # --- 2. Morphological cleanup ---
        if self.bg_mask_dilate_ksize[0] > 1:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, self.bg_mask_dilate_ksize)
            self.fg_mask = cv2.erode(self.fg_mask, kernel, iterations=1)
            self.fg_mask = cv2.dilate(self.fg_mask, kernel, iterations=1)

        # Binary threshold: pixels above threshold become foreground (255)
        #self.fg_mask[self.fg_mask > self.bg_min_pix_thr] = 255
        contours, _ = cv2.findContours(self.fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # --- 3. Collect detection boxes from all sources ---
        results = []
        person_detections = []
        yolox_detections = []
        frame_bgr = None

        # 3a. Person detector (face/body Haar cascades)
        if self.enable_person_detection and self.person_detector is not None:
            frame_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            person_detections = self.person_detector.detect(frame_bgr)
            results.extend(person_detections)

        # 3b. MOG2 contour boxes — sorted by area (largest first).
        #     Added before YOLOX so that YOLOX can promote them instead of adding duplicate boxes.
        #     Only skip contours that overlap Haar person detections (already correctly typed).
        areas = np.array([cv2.contourArea(cnt) for cnt in contours])
        indx  = np.argsort(areas)[::-1].astype(np.int32)
        areas = areas[indx]
        contours = [contours[i] for i in indx]
        for cnt, area in zip(contours, areas):
            if area > self.bg_min_bbox_area:
                x, y, w, h = cv2.boundingRect(cnt)
                _mask = np.zeros_like(self.fg_mask)
                _mask[y:y+h, x:x+w] = self.fg_mask[y:y+h, x:x+w]
                results.append({'bbox': [x, y, x + w, y + h],
                                'centroid': [y + h / 2, x + w / 2],
                                'mask': _mask,
                                'type': 'motion'})

        # 3c. YOLOX detector — runs after contours so contour_in_dets() can promote the
        #     best-matching motion box in one IoU pass instead of adding a duplicate DL box.
        #     Motion-gated: only runs when bg_diff or velocity indicates activity.
        #     Temporal skip: within active windows, only infers every N frames.
        if self.enable_yolox and self.yolox_detector is not None:
            motion_detected = (self.bg_diff >= self._yolox_bg_diff_threshold
                               or self._last_max_vel > self._yolox_max_vel_threshold)
            if motion_detected:
                self._yolox_idle_frames = 0
                self._yolox_det_counter = self._yolox_det_interval
            else:
                self._yolox_idle_frames += 1

            if self._yolox_idle_frames < self._yolox_cooldown_frames:
                # Temporal skip: only call the model every N frames
                self._yolox_det_counter += 1
                if self._yolox_det_counter >= self._yolox_det_interval:
                    self._yolox_det_counter = 0
                    if frame_bgr is None:
                        frame_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                    dets = self.yolox_detector.detect(frame_bgr)
                    # Assign temporary per-frame IDs so sub-blobs can reference their parent det
                    for idx, det in enumerate(dets):
                        det['_det_id'] = idx if self.enable_sub_blob else None
                    self._last_yolox_dets = []
                    for det in dets:
                        # If det overlaps a motion box → remove motion box, add YOLOX det.
                        # If det overlaps a Haar box → skip (already correctly typed).
                        # If no overlap → add as a new DL detection.
                        self.contour_in_dets(det['bbox'], det['_det_id'], results)
                        yolox_detections.append(det)
                    # Tag remaining motion boxes that partially overlap a YOLOX det
                    if self.enable_sub_blob:
                        self._tag_sub_blobs(results)
                    self._last_yolox_dets = list(yolox_detections)  # cache for skipped frames
                    results.extend(yolox_detections)
                else:
                    # Skipped frame: replay cached YOLOX dets.
                    for det in self._last_yolox_dets:
                        self.contour_in_dets(det['bbox'], det['_det_id'], results)
                        results.append(det)
                    if self.enable_sub_blob:
                        self._tag_sub_blobs(results)
            else:
                # Scene went idle — reset counter and clear cache
                self._yolox_det_counter = 0
                self._last_yolox_dets = []

        # --- 4. Assign persistent IDs + CamShift bbox refinement ---
        results = self.tracker.update(results)
        for i in results.keys():
            bbox = results[i]['bbox']
            x1 = int(bbox[0]);  y1 = int(bbox[1])
            bw  = int(bbox[2] - bbox[0]);  bh = int(bbox[3] - bbox[1])
            # cv2.CamShift expects (x_topleft, y_topleft, w, h) — NOT center format.
            # Passing (cx, cy, w, h) started the search in the bottom-right quadrant,
            # causing the window to drift downward especially for rectangular YOLOX masks.
            ret, track_window = cv2.CamShift(
                results[i]['mask'], (x1, y1, bw, bh),
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 1))
            tx, ty, tw, th = track_window
            results[i]['bbox'] = np.array([tx, ty, tx + tw, ty + th])

        # --- 5. Sub-blob post-processing ---
        # ByteTracker preserves all dict fields (via copy), so '_det_id' on YOLOX tracks
        # and 'type'='kpt_{det_id}' on sub-blob tracks both survive the update.
        # Build det_id → tracker pid mapping for YOLOX-type parent tracks.
        det_id_to_pid = {}
        for pid, data in results.items():
            if '_det_id' in data:
                det_id_to_pid[data['_det_id']] = pid

        # Attach each tracked sub-blob's centroid to its parent as sub_keypoints.
        sub_blob_pids = []
        for pid, data in list(results.items()):
            ptype = data.get('type', '')
            if not ptype.startswith('kpt_'):
                continue
            parent_det_id = int(ptype.split('_', 1)[1])
            parent_pid = det_id_to_pid.get(parent_det_id)
            if parent_pid is not None and parent_pid in results:
                bbox = data['bbox']
                cx = float((bbox[0] + bbox[2]) / 2)
                cy = float((bbox[1] + bbox[3]) / 2)
                if 'sub_keypoints' not in results[parent_pid]:
                    results[parent_pid]['sub_keypoints'] = []
                results[parent_pid]['sub_keypoints'].append({'centroid': [cx, cy], 'blob_pid': pid})
            sub_blob_pids.append(pid)

        # In fast mode there is no LK flow to propagate sub-blob positions, so a sub-blob
        # track was only kept alive through ByteTracker.  Now that centroids are attached
        # to the parent, remove sub-blob tracks from the top-level result so they are not
        # emitted as independent objects.  In accurate mode we keep them so
        # _detect_keypoints_in_boxes can run SIFT/GFTT on them too.
        if self.det_method == 'fast':
            for pid in sub_blob_pids:
                results.pop(pid, None)

        return results
    
    def pts_in_bbox(self, pts, bbox):
        """Boolean mask: which points (N,1,2) lie inside the given [x1,y1,x2,y2] bbox."""
        x1, y1, x2, y2 = bbox
        in_bbox = (pts[:, 0, 0] >= x1) & (pts[:, 0, 0] <= x2) & (pts[:, 0, 1] >= y1) & (pts[:, 0, 1] <= y2)
        return in_bbox

    def _detect_keypoints_in_boxes(self, gray, results, det_feat_pts=True):
        """Detect keypoints inside each tracked bounding box.

        For each bbox, the centroid is always included as keypoint[0].
        When det_feat_pts=True, additional feature keypoints (SIFT/FAST/ORB/GFTT)
        are detected in the ROI, scored by response, and the top-K are kept.

        Populates per-result:
            keypoints_1:      [K, 2] initial positions (pre-flow)
            keypoints_2:      [K, 2] copy of keypoints_1 (post-flow destination)
            keypoint_scores:  [K, 1] detector response scores (1.0 for centroid)
        """
        for i in results.keys():
            bbox = results[i]['bbox']

            # Centroid is always the first keypoint
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            pts = np.array([[cx, cy]], dtype=np.float32)
            scores = np.array([[1.0]], dtype=np.float32)

            if det_feat_pts:
                # Crop ROI and detect feature keypoints
                roi = gray[int(bbox[1]):int(bbox[3]), int(bbox[0]):int(bbox[2])]
                kpt_det = [self.fast, self.sift, self.orb, self.gftt][self.kpt_det_idx]
                kps = kpt_det.detect(roi)
                det_pts = cv2.KeyPoint_convert(kps)

                if len(det_pts) > 0:
                    # Shift ROI-local coords to frame-global coords
                    det_pts += np.array([bbox[0], bbox[1]], dtype=np.float32)
                    pts = np.concatenate([pts, det_pts], axis=0)

                    # Sort by response score (descending) and keep top-K
                    scores = np.array([1.0] + [kp.response for kp in kps], dtype=np.float32)
                    inds = np.argsort(scores)[::-1]
                    pts    = pts[inds][:self.kpt_max_kpts].reshape(-1, 2)
                    scores = scores[inds][:self.kpt_max_kpts].reshape(-1, 1)

            if self.enable_pose:
                obj_type = results[i].get('type', '')
                if obj_type in ('person', 'body'):
                    pose_res = self.pose_detector.detect(self.curr_frame, [bbox])
                    if pose_res:
                        kps    = pose_res[0]['keypoints']   # (17, 2)
                        kp_sc  = pose_res[0]['scores']      # (17,)
                        results[i]['pose_keypoints'] = kps
                        results[i]['pose_scores']    = kp_sc

                        # Add high-confidence joints to the tracked point set
                        valid = kp_sc >= self.pose_detector.score_thr
                        if valid.any():
                            pose_pts = kps[valid].astype(np.float32)   # (M, 2)
                            pose_sc  = kp_sc[valid].reshape(-1, 1).astype(np.float32)
                            pts    = np.concatenate([pts,    pose_pts], axis=0)
                            scores = np.concatenate([scores, pose_sc],  axis=0)
                            # Re-apply top-K limit
                            inds   = np.argsort(scores[:, 0])[::-1]
                            pts    = pts[inds][:self.kpt_max_kpts].reshape(-1, 2)
                            scores = scores[inds][:self.kpt_max_kpts].reshape(-1, 1)

            # Inject sub-blob centroids as low-confidence keypoints (accurate mode only).
            # Sub-blobs are small MOG2 contours with partial overlap against this YOLOX det —
            # they act as a cheap pose hint when RTMPose is disabled.
            sub_kpts = results[i].get('sub_keypoints')
            if sub_kpts:
                sub_pts = np.array([[sk['centroid'][0], sk['centroid'][1]] for sk in sub_kpts],
                                   dtype=np.float32)
                sub_sc  = np.full((len(sub_kpts), 1), 0.3, dtype=np.float32)
                pts    = np.concatenate([pts, sub_pts], axis=0)
                scores = np.concatenate([scores, sub_sc], axis=0)
                inds   = np.argsort(scores[:, 0])[::-1]
                pts    = pts[inds][:self.kpt_max_kpts].reshape(-1, 2)
                scores = scores[inds][:self.kpt_max_kpts].reshape(-1, 1)

            results[i]['keypoints_1'] = pts
            results[i]['keypoints_2'] = pts.copy()
            results[i]['keypoint_scores'] = scores

        return results

    def _detect_and_match_keypoints_in_boxes(self, gray, _detect):
        """Re-detect or propagate keypoints, then ensure centroid is keypoint[0].

        When _detect=True (detection frame):
            - Re-detect keypoints via _detect_keypoints_in_boxes
            - For existing tracked objects: match new detections to previous flow
              positions using Hungarian assignment, so keypoint IDs stay consistent
            - Remove objects that disappeared from detection results

        When _detect=False (propagation frame):
            - Copy flow destinations (keypoints_2) back to keypoints_1
            - Shift bboxes by estimated velocity for next flow computation

        Finally, override keypoint[0] with the current bbox centroid for all objects.
        """
        if _detect:
            # Re-detect foreground regions and keypoints, then match with tracked flow
            results = self._detect_foreground_boxes(gray)
            det_pts = self._detect_keypoints_in_boxes(gray, results, det_feat_pts=True)

            for i in det_pts.keys():
                if i in self.prev_pts and len(self.prev_pts[i]['keypoints_1']) >= det_pts[i]['keypoints_1'].shape[0]:
                    # Match previous flow destinations to new detections for ID consistency
                    flow_kpts = self.prev_pts[i]['keypoints_2'].copy()
                    self.prev_pts[i] = det_pts[i]
                    self.prev_pts[i]['keypoints_1'] = self.match_keypoints_by_distance(
                        flow_kpts, det_pts[i]['keypoints_1'], matcher=self.matcher_mode)
                    self.prev_pts[i]['keypoints_2'] = self.prev_pts[i]['keypoints_1'].copy()
                else:
                    # New object or grew more keypoints — accept fresh detections as-is
                    self.prev_pts[i] = det_pts[i]

            # Prune objects no longer in detection results
            for i in list(self.prev_pts.keys()):
                if i not in det_pts:
                    del self.prev_pts[i]
        else:
            # Propagation: carry forward flow positions and shift bbox by velocity
            for i in self.prev_pts.keys():
                self.prev_pts[i]['keypoints_1'] = self.prev_pts[i]['keypoints_2'].copy()
                vel = self.prev_pts[i].get('vel', np.array([0.0, 0.0]))
                vel = np.nan_to_num(vel, nan=0.0, posinf=0.0, neginf=0.0)
                self.prev_pts[i]['bbox'] = self.prev_pts[i]['bbox'] + np.array([vel[0], vel[1], vel[0], vel[1]])

        # Ensure keypoint[0] is always the bbox centroid
        for i in self.prev_pts.keys():
            bbox = self.prev_pts[i]['bbox']
            center = np.array([[(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]], dtype=np.float32)
            self.prev_pts[i]['keypoints_1'][0] = center
            self.prev_pts[i]['keypoints_2'][0] = center

    def match_keypoints_by_distance(self, kp1, kp2, matcher='hungarian'):
        """Assign kp2 positions to kp1 slots via minimum-cost distance matching.

        Uses scipy's Hungarian (Jonker-Volgenant) algorithm to find the
        optimal assignment.  Matches with cost >= mtc_max_cost_thr are rejected.

        Args:
            kp1: Previous keypoints [N, 2].
            kp2: Newly detected keypoints [M, 2].
            matcher: Assignment strategy (currently only 'hungarian').

        Returns:
            [N, 2] array with matched kp2 positions written into kp1 slots.
        """
        if kp1 is None or kp2 is None:
            return kp1 if kp2 is None else kp2
        if kp1.size == 0:
            return kp2
        if kp2.size == 0:
            return kp1

        # Pairwise squared-L2 cost matrix [N, M]
        _kp1 = kp1.reshape(-1, 2)[:, None, :]
        _kp2 = kp2.reshape(-1, 2)[None, :, :]
        dists = ((_kp1 - _kp2) ** 2).sum(axis=2).astype(np.float32)
        dists = np.nan_to_num(dists, nan=0.0, posinf=1e3, neginf=0.0)

        kp2s = np.empty_like(kp1)
        row_ind, col_ind = linear_sum_assignment(dists)

        # Reject high-cost matches (object likely different)
        valid = dists[row_ind, col_ind] < self.mtc_max_cost_thr
        kp2s[row_ind[valid]] = kp2[col_ind[valid]]
        return kp2s
    
    @staticmethod
    def _sanitize_points(pts, h, w):
        """Replace NaN/Inf and clip coordinates to frame bounds."""
        pts = np.nan_to_num(pts, nan=0.0, posinf=1e3, neginf=0.0)
        pts[:, 0, 0] = np.clip(pts[:, 0, 0], 0, w - 1)
        pts[:, 0, 1] = np.clip(pts[:, 0, 1], 0, h - 1)
        return pts

    def _compute_sparse_flow(self, gray):
        """Track keypoints via forward-backward Lucas-Kanade optical flow.

        For each tracked object:
            1. Forward LK:  prev_gray → gray  gives predicted positions (p1)
            2. Backward LK: gray → prev_gray  back-projects p1 to (p0r)
            3. Consistency check: |kp1 - p0r| < 1.0 px rejects bad tracks
            4. Valid keypoints get updated positions; invalid ones get shifted
               by the mean flow velocity so they don’t freeze in place
        """
        h, w = gray.shape[:2]

        for i in self.prev_pts.keys():
            if len(self.prev_pts[i]['keypoints_1']) == 0:
                continue

            kp1 = self.prev_pts[i]['keypoints_1'].astype(np.float32).reshape(-1, 1, 2)
            kp1 = self._sanitize_points(kp1, h, w)

            # Forward flow: prev_gray → gray
            p1, st, _ = cv2.calcOpticalFlowPyrLK(
                self.prev_gray, gray, kp1, None, **self.flow_params)
            p1 = self._sanitize_points(p1, h, w)

            # Backward flow: gray → prev_gray (for consistency check)
            p0r, _, _ = cv2.calcOpticalFlowPyrLK(
                gray, self.prev_gray, p1, None, **self.flow_params)
            p0r = self._sanitize_points(p0r, h, w)

            # Forward-backward consistency: reject points that don't round-trip within 1 px
            d = abs(kp1 - p0r)
            good_mask = d.reshape(-1, 2).max(-1) < 1.0
            val_pts = (st.flatten() == 1) & good_mask

            # Update valid keypoints with their new flow positions
            bbox = self.prev_pts[i]['bbox']
            new_center = np.array([[[(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]]], dtype=np.float32)
            self.prev_pts[i]['keypoints_2'][val_pts] = p1[val_pts].reshape(-1, 2)

            # If centroid hasn't drifted too far, snap keypoint[0] to bbox center
            # and apply mean velocity to invalid keypoints as a fallback
            if ((kp1[0] - new_center) ** 2).sum() < self.mtc_max_cost_thr:
                self.prev_pts[i]['keypoints_2'][0] = new_center
                self.prev_pts[i]['vel'] = np.mean(p1[val_pts] - kp1[val_pts], axis=0)
                self.prev_pts[i]['keypoints_2'][~val_pts] += self.prev_pts[i]['vel'].reshape(1, 2)

    def detect(self, frame, return_pts: bool = False):
        """Run full detection + tracking pipeline on one BGR frame.

        Returns:
            If return_pts=False: (points_dict,)
            If return_pts=True:  (points_dict, pts_out)

            points_dict:  {traj_id: {vel, mean_vel, channel, bg_diff}} for top trajectories.
            pts_out:      Copy of per-object tracked point dict (for recording/external use).
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self.curr_frame = frame

        # Set frame size for ByteTracker scaling (no-op for SimpleTracker)
        if hasattr(self.tracker, 'set_frame_size'):
            self.tracker.set_frame_size(gray.shape[0], gray.shape[1])

        # --- First frame: bootstrap detection, no flow yet ---
        if self.prev_gray is None:
            results = self._detect_foreground_boxes(gray)
            self.prev_pts = self._detect_keypoints_in_boxes(
                gray, results, det_feat_pts=(self.det_method != 'fast'))
            self.prev_gray = gray
            self.viz_div_h = int(gray.shape[0] // (self.num_traj_viz + 1))
            self.viz_div_w = int(gray.shape[1])
            if return_pts:
                return {}, _copy_pts(self.prev_pts)
            return ({},)

        self.prev_gray = gray
        self.memory._viz_pos = None
        self.memory._viz_vel = None

        # --- Store current keypoints in trajectory memory ---
        pts = _copy_pts(self.prev_pts)
        self.memory.add(pts)
        sorted_ids = self.memory.get_sorted_traj_ids(
            curr_pids=pts.keys(), num_of_kpts=self.num_traj_viz)

        # --- Build per-trajectory velocity output ---
        points_dict = {}
        for ch, tid in enumerate(sorted_ids, start=1):
            vel = 0.1 * self.memory.get_traj_velocities(traj_id=tid)
            if len(vel) > 0:
                pid = int(tid.split('|')[0])
                points_dict[tid] = {
                    'vel': np.asarray(vel, dtype=np.float32),
                    'mean_vel': float(np.mean(vel)),
                    'channel': ch,
                    'bg_diff': int(self.bg_diff),
                    'type': self.memory.classify_pid(pid),
                }

        # Update max velocity from this frame's trajectories (used by YOLOX motion gate next frame)
        if points_dict:
            self._last_max_vel = max(
                (v['mean_vel'] for v in points_dict.values() if isinstance(v, dict) and 'mean_vel' in v),
                default=0.0
            )

        # --- Person count & density ---
        person_stats = self.get_person_stats(frame_shape=gray.shape)

        # --- Scene trajectory analysis (fight / burglary scoring) ---
        if self.scene_analyzer is not None and self.prev_pts:
            h, w = gray.shape[:2]
            self.scene_analyzer.set_frame_size(w, h)
            current_pids = list(self.prev_pts.keys())
            scene_result = self.scene_analyzer.update(
                tracked_results=self.prev_pts,
                flow_memory=self.memory,
                current_pids=current_pids,
                detections=self._last_yolox_dets,
            )
            person_stats['scene_analysis'] = scene_result

        points_dict['_stats'] = person_stats

        pts_out = _copy_pts(self.prev_pts)

        # --- Fast mode: centroid-only detection (no optical flow) ---
        if self.det_method == 'fast':
            results = self._detect_foreground_boxes(gray)
            self.prev_pts = self._detect_keypoints_in_boxes(gray, results, det_feat_pts=False)

            if return_pts:
                return points_dict, pts_out
            return (points_dict,)

        # --- Accurate mode: sparse optical flow + keypoint re-detection ---
        self._compute_sparse_flow(gray)
        _detect = self.count % self.kpt_det_freq == 0
        self._detect_and_match_keypoints_in_boxes(gray, _detect)
        self.count += 1

        if return_pts:
            return points_dict, pts_out
        return (points_dict,)
    
    def plot_velocities(self, plot_array, points_dict):
        """Draw per-trajectory velocity waveforms on plot_array.

        Each trajectory gets a horizontal row with a gray baseline and a
        colored oscillating curve representing frame-to-frame speed.
        Scales dynamically to the array size.

        Returns:
            (plot_array, res) where res maps traj_id -> {vel, bg_diff}.
        """
        plot_array = 0 * plot_array
        h, w = plot_array.shape[:2]
        top_margin = max(8, int(h * 0.20))
        bottom_margin = max(6, int(h * 0.03))
        usable_h = max(1, h - top_margin - bottom_margin)

        row_count = max(1, min(len(points_dict), self.num_traj_viz))
        row_h = max(10, usable_h // row_count)

        scale = max(0.65, min(1.6, h / 720.0))
        baseline_thickness = max(1, int(round(1.2 * scale)))
        curve_thickness = max(1, int(round(2.0 * scale)))
        font_scale = max(0.38, 0.60 * scale)
        text_thickness = max(1, int(round(1.5 * scale)))
        text_gap = max(10, int(round(15 * scale)))
        left_pad = max(8, int(round(10 * scale)))
        text_start_offset = max(8, int(round(10 * scale)))
        graph_x0 = max(left_pad + 4, int(round(w * 0.18)))
        max_pts = max(1, w - graph_x0 - left_pad)

        points_items = list((k, v) for k, v in points_dict.items() if k != '_stats')
        bg_diff_int = int(self.bg_diff)
        n_colors = self.n_colors
        colors = self.colors

        res = {}
        for idx, (_id, payload) in enumerate(points_items):
            color = colors[idx % n_colors]
            center_y = top_margin + idx * row_h + (row_h // 2)

            cv2.line(
                plot_array,
                (0, center_y),
                (w, center_y),
                (150, 150, 150),
                baseline_thickness,
                cv2.LINE_AA,
            )

            vel = np.asarray(payload.get('vel', []), dtype=np.float32).reshape(-1)
            mean_vel = float(payload.get('mean_vel', 0.0))
            if vel.size > 0:
                vel = vel[-max_pts:]
                amp = max(float(np.max(np.abs(vel))), 1e-6)
                y_amp = max(3.0, row_h * 0.25)

                xs = np.arange(vel.size, dtype=np.float32) + graph_x0
                ys = center_y - (vel / amp) * y_amp
                points = np.stack([xs, ys], axis=1).astype(np.int32).reshape((-1, 1, 2))

                cv2.polylines(
                    plot_array,
                    pts=[points],
                    isClosed=False,
                    color=color,
                    thickness=curve_thickness,
                    lineType=cv2.LINE_AA,
                )

            res[_id] = {'vel': round(mean_vel, 2), 'bg_diff': bg_diff_int}
            if idx >= self.num_traj_viz - 1:
                break

        return plot_array, res
        
    def get_coreset_prototypes(self, k=32):
        """Return selected representative trajectory ids and embeddings."""
        if self.coreset is None:
            return [], np.empty((0, 0), dtype=np.float32)
        return self.coreset.select_kcenter(k)
    
    def get_plot_arrays(self):
        """Return dictionary of plot arrays"""
        return getattr(self, 'plot_arrays', {})
    
    def save_plot_arrays(self, save_path):
        """Save all plot arrays to disk"""
        if hasattr(self, 'plot_arrays'):
            for plot_name, plot_array in self.plot_arrays.items():
                filename = f"{save_path}/{plot_name}_frame_{self.count}.npy"
                np.save(filename, plot_array)
                print(f"Saved plot array: {filename}")
    
    def clear_plot_arrays(self):
        """Clear stored plot arrays to free memory"""
        if hasattr(self, 'plot_arrays'):
            self.plot_arrays.clear()
    
    def _draw_pts_flow(self, frame, _pts):
        """Draw optical flow trails, keypoint dots, bounding boxes, and IDs on frame."""
        viz_frame = frame.copy()

        # Color map for detection types (BGR)
        _type_colors = {
            'motion': (0, 255, 0),        # Green — MOG2 motion contours
            'face':   (255, 0, 255),       # Magenta — Haar face
            'body':   (255, 165, 0),       # Orange — Haar body
        }
        _yolox_colors = {
            'person': (0, 255, 255),       # Yellow — YOLOX person
            'car':    (255, 0, 0),         # Blue — YOLOX car
            'truck':  (200, 50, 0),        # Dark blue — YOLOX truck
            'bus':    (200, 100, 0),       # Navy — YOLOX bus
        }
        _yolox_default_color = (0, 200, 200)  # Teal — other YOLOX classes

        for i in _pts.keys():
            # Skip objects seen fewer than 3 frames (likely noise)
            wrapped_pid = i % self.memory.maxpid
            hist = self.memory.pid_hist.get(wrapped_pid)
            if hist is None or hist['total_seen'] < 3:
                continue

            good_new = _pts[i]['keypoints_2']
            good_old = _pts[i]['keypoints_1']

            # Initialize or fade the flow trail overlay
            if self.mask is None:
                self.mask = np.zeros_like(viz_frame)
            else:
                self.mask = (0.99 * self.mask).astype(np.uint8)

            # Draw flow line + dot for each keypoint
            for j, (new, old) in enumerate(zip(good_new, good_old)):
                a, b = new.ravel().astype(int)
                c, d = old.ravel().astype(int)
                if min(a, b, c, d) < 0:
                    continue
                color = self.colors[j % self.n_colors]
                self.mask = cv2.line(self.mask, (a, b), (c, d), color, 2)
                viz_frame = cv2.circle(viz_frame, (a, b), 3, color, -1)

            # Composite the fading trail overlay onto the frame
            if self.mask.shape == viz_frame.shape:
                viz_frame = cv2.add(viz_frame, self.mask)

            # Resolve bbox color from detection type
            det_type = _pts[i].get('type', 'motion')
            if det_type in _type_colors:
                bbox_color = _type_colors[det_type]
            elif det_type.startswith('yolox_'):
                cls = det_type[6:]  # strip 'yolox_' prefix
                bbox_color = _yolox_colors.get(cls, _yolox_default_color)
            else:
                bbox_color = (0, 255, 0)  # Fallback green

            # Draw bounding box and ID + type label
            bbox = _pts[i]['bbox']
            cv2.rectangle(viz_frame,
                          (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])),
                          bbox_color, 2)
            label = f'ID:{i} {det_type}'
            cv2.putText(viz_frame, label,
                        (int(bbox[0]), int(bbox[1]) - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, bbox_color, 2)

        return viz_frame