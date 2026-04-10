import cv2
import numpy as np
import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Create a simple mock stream that generates frames programmatically
class MockCapture:
    def __init__(self, width=640, height=480, camera_name="Mock Camera"):
        self.width = width
        self.height = height
        self.camera_name = camera_name
        self.frame_count = 0
        self.start_time = time.time()
        
    def isOpened(self):
        return True
        
    def read(self):
        # Generate a test pattern frame
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        
        # Create a gradient background
        for i in range(self.height):
            color_intensity = int((i / self.height) * 255)
            frame[i, :] = [color_intensity // 3, color_intensity // 2, color_intensity]
        
        # Add moving circle
        current_time = time.time() - self.start_time
        circle_x = int((self.width / 2) + 100 * np.sin(current_time))
        circle_y = int((self.height / 2) + 50 * np.cos(current_time))
        cv2.circle(frame, (circle_x, circle_y), 30, (0, 255, 255), -1)
        
        # Add text
        cv2.putText(frame, f"Mock Camera: {self.camera_name}", (10, 30), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(frame, f"Frame: {self.frame_count}", (10, self.height - 20), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        
        self.frame_count += 1
        return True, frame
        
    def release(self):
        pass
        
    def set(self, prop, value):
        pass


class StreamDrawingHelper:
    NUM_TRAJ_VIZ = 5
    TRAJECTORY_COLORS = (
        (90, 220, 255),
        (180, 255, 100),
        (255, 170, 90),
        (255, 120, 200),
        (120, 180, 255),
        (140, 255, 210),
        (255, 230, 120),
        (200, 170, 255),
    )
    AUDIO_LOUDNESS_COLOR = (100, 220, 255)
    AUDIO_PEAKFREQ_COLOR = (180, 255, 100)
    AUDIO_SERIES_GAIN = 1.0 #0.82
    VELOCITY_ROW_GAIN = 0.1
    # --- Detection-type color map (BGR) for bounding boxes ---
    BBOX_TYPE_COLORS = {
        'motion': (0, 255, 0),        # Green — MOG2 motion contours
        'face':   (255, 0, 255),       # Magenta — Haar face
        'body':   (255, 165, 0),       # Orange — Haar body
    }
    BBOX_YOLOX_COLORS = {
        'person': (0, 255, 255),       # Yellow — YOLOX person
        'car':    (255, 0, 0),         # Blue — YOLOX car
        'truck':  (200, 50, 0),        # Dark blue — YOLOX truck
        'bus':    (200, 100, 0),       # Navy — YOLOX bus
    }
    BBOX_YOLOX_DEFAULT_COLOR = (0, 200, 200)  # Teal — other YOLOX classes
    BBOX_DEFAULT_COLOR = (0, 255, 0)           # Green fallback
    
    
    @classmethod
    def bbox_color_for_type(cls, det_type: str) -> tuple:
        """Return BGR color tuple for a detection type string."""
        if det_type in cls.BBOX_TYPE_COLORS:
            return cls.BBOX_TYPE_COLORS[det_type]
        if det_type in cls.BBOX_YOLOX_COLORS:
            return cls.BBOX_YOLOX_COLORS.get(det_type, cls.BBOX_YOLOX_DEFAULT_COLOR)
        return cls.BBOX_DEFAULT_COLOR

    OVERLAY_COLORS = (
        (0, 255, 0),
        (255, 0, 0),
        (0, 0, 255),
        (255, 255, 0),
        (255, 0, 255),
        (0, 255, 255),
        (128, 255, 0),
        (255, 128, 0),
    )

    @staticmethod
    def ensure_bgr_frame(frame: np.ndarray) -> np.ndarray:
        if len(frame.shape) == 2 or (len(frame.shape) == 3 and frame.shape[2] == 1):
            return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        return frame

    @staticmethod
    def truncate_overlay_text(text: str, font, font_scale: float, thickness: int, max_width: int) -> str:
        candidate = text
        while candidate:
            (text_w, _), _ = cv2.getTextSize(candidate, font, font_scale, thickness)
            if text_w <= max_width:
                return candidate
            if len(candidate) <= 4:
                return candidate[:1]
            candidate = candidate[:-2].rstrip() + '…'
        return text

    @staticmethod
    def format_audio_compact_text(analysis: Optional[Dict[str, Any]]) -> tuple[str, bool]:
        if not analysis:
            return "Amp: 0.00 Fq: 0000", False

        overall = float(analysis.get('overall_intensity', 0.0) or 0.0)
        anomaly = analysis.get('anomaly') or {}
        peak_frequency_mean = float(analysis.get('peak_frequency_mean', 0.0) or 0.0)

        has_alert = bool(anomaly.get('intensity')) or bool(anomaly.get('frequency'))
        overall_text = f"{overall:.2f}".rjust(5)
        peak_freq_text = str(max(0, int(round(peak_frequency_mean)))).zfill(4)
        return f"Amp: {overall_text} Fq: {peak_freq_text}", has_alert

    def draw_fps_overlay(self, frame: np.ndarray, fps_value: float, res: Optional[dict] = None, tracker_fps: float = 0.0, person_stats: Optional[dict] = None) -> np.ndarray:
        if frame is None:
            return frame

        if not frame.flags.writeable or not frame.flags.c_contiguous:
            frame = np.ascontiguousarray(frame).copy()

        res = res or {}
        texts = [f"FPS: {fps_value:.1f} | Tracker: {tracker_fps:.1f}"]
        if len(res):
            texts += [f" | Vel: {res['vel']}" f" | Diff: {res['bg_diff']}"]

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.5
        thickness = 1
        margin = 5
        x = frame.shape[1]
        y = margin
        for text in texts:
            (text_w, text_h), _ = cv2.getTextSize(text, font, font_scale, thickness)
            frame = cv2.putText(frame, text, (x - (text_w + margin), y + text_h), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
            y += text_h

        # Person count & density (top-left)
        p_count = person_stats.get('person_count', 0) if person_stats else 0
        p_density = person_stats.get('person_density', 0.0) if person_stats else 0.0
        stats_text = f"Persons: {p_count} | Density: {p_density:.2f}"
        (tw, th), _ = cv2.getTextSize(stats_text, font, font_scale, thickness)
        frame = cv2.putText(frame, stats_text, (margin, margin + th), font, font_scale, (0, 255, 255), thickness, cv2.LINE_AA)

        return frame

    def draw_motion_analysis_chart(
        self,
        plot_array: np.ndarray,
        points_dict: Dict[str, Any],
        camera_id: str,
        latest_audio_chunk_analysis: Dict[str, Dict[str, Any]],
    ) -> tuple[np.ndarray, Dict[str, Dict[str, Any]]]:
        plot_array = np.zeros_like(plot_array)

        h, w = plot_array.shape[:2]
        top_margin = max(10, int(h * 0.25))
        bottom_margin = max(4, int(h * 0.05))
        usable_h = max(1, h - top_margin - bottom_margin)

        num_traj_viz = self.NUM_TRAJ_VIZ

        # Fixed layout: 2 audio rows (0-1) + num_traj_viz velocity rows (2-6) = 7 total, always
        total_rows = 2 + num_traj_viz
        row_h = max(8, usable_h // total_rows)

        scale = max(0.65, min(1.6, h / 720.0))
        baseline_thickness = max(1, int(round(1.2 * scale)))
        curve_thickness = max(1, int(round(2.0 * scale)))
        left_pad = max(8, int(round(10 * scale)))
        graph_x0 = max(left_pad + 4, int(round(w * 0.18)))
        max_pts = max(1, w - graph_x0 - left_pad)

        points_items = list((k, v) for k, v in points_dict.items() if k != '_stats')
        
        # Extract background difference from any trajectory (they should all have the same bg_diff)
        bg_diff_int = 0
        if points_items:
            # Get bg_diff from the first trajectory that has it
            for _id, payload in points_items:
                if isinstance(payload, dict) and 'bg_diff' in payload:
                    bg_diff_int = int(payload.get('bg_diff', 0))
                    logger.debug(f"Extracted bg_diff={bg_diff_int} for camera {camera_id} from trajectory {_id}")
                    break
            if bg_diff_int == 0:
                logger.debug(f"No bg_diff found in motion data for camera {camera_id}, using default 0")
        colors = self.TRAJECTORY_COLORS
        n_colors = max(1, len(colors))

        analysis = latest_audio_chunk_analysis.get(camera_id) or {}

        # Extract just the y-values from audio series (drop timestamps — same as velocity)
        def _extract_series_values(series) -> np.ndarray:
            if not series:
                return np.array([], dtype=np.float32)
            arr = np.asarray(series, dtype=np.float32)
            if arr.ndim == 2 and arr.shape[1] >= 2:
                return arr[:, 1]
            if arr.ndim == 1:
                return arr
            return np.array([], dtype=np.float32)

        loudness_vals = _extract_series_values(analysis.get('loudness_series') or [])
        peak_freq_vals = _extract_series_values(analysis.get('peak_frequency_mean_series') or [])
        
        # Fixed audio row defs — always at rows 0 and 1 (empty axes shown if no data)
        audio_row_defs = [
            ("A-Amp", loudness_vals,  self.AUDIO_LOUDNESS_COLOR, self.AUDIO_SERIES_GAIN),
            ("A-Fq",  peak_freq_vals/20000, self.AUDIO_PEAKFREQ_COLOR, self.AUDIO_SERIES_GAIN),
        ]

        def _draw_row(row_idx: int, label: str, vals: np.ndarray, color, gain: float):
            center_y = top_margin + row_idx * row_h + (row_h // 2)
            cv2.line(plot_array, (0, center_y), (w, center_y), (150, 150, 150), baseline_thickness, cv2.LINE_AA)
            if label:
                row_label_scale = max(0.32, 0.38 * scale)
                row_label_thickness = max(1, int(round(1.2 * scale)))
                label_y = max(8, min(h - 4, center_y - 3))
                cv2.putText(plot_array, label, (4, label_y), cv2.FONT_HERSHEY_SIMPLEX,
                            row_label_scale, color, row_label_thickness, cv2.LINE_AA)
            if vals.size < 2:
                return
            clipped = vals[-max_pts:]
            amp = 1.0 #max(float(np.max(np.abs(clipped))), 1e-6)
            y_amp = max(3.0, row_h * gain)
            xs = (5 * np.arange(clipped.size, dtype=np.float32)) + graph_x0
            ys = center_y - (clipped / amp) * y_amp
            pts = np.stack([xs, ys], axis=1).astype(np.int32).reshape((-1, 1, 2))
            cv2.polylines(plot_array, [pts], False, color, curve_thickness, cv2.LINE_AA)

        # Draw audio rows — always rows 0 and 1
        for row_idx, (label, vals, color, gain) in enumerate(audio_row_defs):
            _draw_row(row_idx, label, vals, color, gain)

        # Draw velocity rows — always rows 2 .. 2+num_traj_viz-1 (empty axis if no camera data)
        empty_vel = np.array([], dtype=np.float32)
        res = {}
        for idx in range(num_traj_viz):
            row_idx = 2 + idx
            if idx < len(points_items):
                _id, payload = points_items[idx]
                color = colors[idx % n_colors]
                vel = np.asarray(payload.get('vel', []), dtype=np.float32).reshape(-1)
                mean_vel = float(payload.get('mean_vel', 0.0))
                _draw_row(row_idx, f"V-{_id}", vel, color, self.VELOCITY_ROW_GAIN)
                res[_id] = {'vel': round(mean_vel, 2), 'bg_diff': bg_diff_int}
            else:
                _draw_row(row_idx, "", empty_vel, colors[idx % n_colors], self.VELOCITY_ROW_GAIN)

        return plot_array, res

    def draw_stream_status_overlay(
        self,
        frame: np.ndarray,
        camera_id: str,
        fps_value: float,
        res: Optional[Dict[str, Any]],
        latest_audio_chunk_analysis: Dict[str, Dict[str, Any]],
    ) -> np.ndarray:
        if frame is None:
            return frame

        frame = self.ensure_bgr_frame(frame)

        res = res or {}
        vel = float(res.get('vel', 0.0) or 0.0)
        bg_diff = int(res.get('bg_diff', 0) or 0)
        audio_text, has_alert = self.format_audio_compact_text(latest_audio_chunk_analysis.get(camera_id))

        font = cv2.FONT_HERSHEY_SIMPLEX
        h, w = frame.shape[:2]
        pad_x = max(6, int(w * 0.012))
        pad_y = max(4, int(h * 0.01))
        font_scale = max(0.35, min(0.72, h / 1400.0))
        thickness = max(1, int(round(h / 520.0)))

        fps_text = f"{fps_value:.1f}".rjust(5)
        vel_text = f"{vel:.1f}".rjust(6)
        diff_text = str(max(0, bg_diff)).zfill(4)
        text = f"F{fps_text} V{vel_text} D{diff_text} {audio_text}"
        text = self.truncate_overlay_text(text, font, font_scale, thickness, max(20, w - 2 * pad_x - 2))
        (_, text_h), _ = cv2.getTextSize(text, font, font_scale, thickness)

        box_bottom = h - max(2, pad_y // 2)
        box_top = max(0, box_bottom - (text_h + 2 * pad_y))
        cv2.rectangle(
            frame,
            (pad_x // 2, box_top),
            (w - (pad_x // 2), box_bottom),
            (0, 0, 0),
            -1,
        )

        color = (80, 235, 140) if not has_alert else (0, 200, 255)
        text_y = min(box_bottom - pad_y, box_top + text_h + pad_y)
        frame = cv2.putText(frame, text, (pad_x, text_y), font, font_scale, color, thickness, cv2.LINE_AA)
        return frame

    def draw_optical_flow_overlay(
        self,
        frame: np.ndarray,
        pts_payload: Optional[Dict[Any, Dict[str, Any]]],
        draw_mask: Optional[np.ndarray] = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        viz_frame = frame.copy()
        if draw_mask is None or draw_mask.shape[:2] != viz_frame.shape[:2]:
            draw_mask = np.zeros_like(viz_frame)
        else:
            draw_mask = (0.99 * draw_mask).astype(np.uint8)

        if not pts_payload:
            return viz_frame, draw_mask

        colors = self.OVERLAY_COLORS
        n_colors = max(1, len(colors))

        for track_id, track_data in pts_payload.items():
            good_new = np.asarray(track_data.get('keypoints_2', []), dtype=np.float32).reshape(-1, 2)
            good_old = np.asarray(track_data.get('keypoints_1', []), dtype=np.float32).reshape(-1, 2)

            for idx, (new, old) in enumerate(zip(good_new, good_old)):
                a, b = new.ravel().astype(int)
                c, d = old.ravel().astype(int)
                if min(a, b, c, d) < 0:
                    continue
                color = colors[idx % n_colors]
                try:
                    draw_mask = cv2.line(draw_mask, (a, b), (c, d), color, 2)
                    viz_frame = cv2.circle(viz_frame, (a, b), 3, color, -1)
                except Exception:
                    continue

            bbox = track_data.get('bbox')
            if bbox is not None and len(bbox) >= 4:
                try:
                    x1, y1, x2, y2 = [int(v) for v in bbox[:4]]
                    det_type = track_data.get('type', 'motion')
                    bbox_color = self.bbox_color_for_type(det_type)
                    cv2.rectangle(viz_frame, (x1, y1), (x2, y2), bbox_color, 2)
                    label = f'ID:{track_id} {det_type}'
                    cv2.putText(
                        viz_frame,
                        label,
                        (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        bbox_color,
                        2,
                    )
                except Exception:
                    pass

        try:
            viz_frame = cv2.add(viz_frame, draw_mask)
        except Exception:
            pass

        return viz_frame, draw_mask
