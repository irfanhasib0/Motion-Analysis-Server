import gc
import os
import select
import subprocess
import sys
import cv2
import json
import numpy as np
import asyncio
import threading
import time
from typing import Generator, Dict, Optional, Any, Union
import logging
#from services.database_service import DatabaseService
from services.config_manager import ConfigManager
from services.hls_manager import HLSManager

PROJECT_SRC_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))
if PROJECT_SRC_PATH not in sys.path:
    sys.path.append(PROJECT_SRC_PATH)

from audioproc import FrequencyIntensityAnalyzer

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

    def draw_fps_overlay(self, frame: np.ndarray, fps_value: float, res: Optional[dict] = None) -> np.ndarray:
        if frame is None:
            return frame

        if not frame.flags.writeable or not frame.flags.c_contiguous:
            frame = np.ascontiguousarray(frame).copy()

        res = res or {}
        texts = [f"FPS: {fps_value:.1f}"]
        if len(res):
            texts += [f" | Vel: {res['vel']}" f" | Diff: {res['bg_diff']}"]

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.6
        thickness = 2
        margin = 10
        y_ofs = margin
        x = frame.shape[1] - 300
        y = margin
        for text in texts:
            (text_w, text_h), _ = cv2.getTextSize(text, font, font_scale, thickness)
            frame = cv2.putText(frame, text, (x, y + y_ofs), font, font_scale, (0, 255, 0), thickness, cv2.LINE_AA)
            y_ofs += text_h + margin

        return frame

    def plot_dynamic_stream_data(
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

        points_items = list(points_dict.items())
        bg_diff_int = 0
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

    def draw_box_tracking_overlay(
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

    def draw_pts_flow_for_stream(
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
                    cv2.rectangle(viz_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    cv2.putText(
                        viz_frame,
                        f'ID: {track_id}',
                        (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 255, 0),
                        2,
                    )
                except Exception:
                    pass

        try:
            viz_frame = cv2.add(viz_frame, draw_mask)
        except Exception:
            pass

        return viz_frame, draw_mask

class StreamingService:
    def __init__(self):
        #self.db = DatabaseService()  # Original database service for cameras and recordings
        self.stream_locks: Dict[str, threading.Lock] = {}
        self.active_streams: Dict[str, str] = {}  # camera_id -> active stream token
        self.active_audio_streams: Dict[str, str] = {}  # camera_id -> active audio stream token
        self.active_processing_streams: Dict[str, str] = {}  # camera_id -> active processing stream token
        self._latest_frames: Dict[str, np.ndarray] = {}
        self._latest_hls_frames: Dict[str, np.ndarray] = {}
        self._latest_frame_seq: Dict[str, int] = {}
        self._stream_frame_index: Dict[str, int] = {}
        self._latest_viz: Dict[str, np.ndarray] = {}
        self._latest_res_video: Dict[str, Dict[str, Any]] = {}
        self._latest_res_audio: Dict[str, Dict[str, Any]] = {}
        self._fps_stats: Dict[str, Dict[str, float]] = {}
        self._background_camera_threads: Dict[str, threading.Thread] = {}
        self._hls_manager = HLSManager(
            get_recordings_dir=lambda: getattr(self, 'recordings_dir', os.path.abspath('./recordings')),
            get_camera_config=lambda camera_id: self.db.get_camera(camera_id) if hasattr(self, 'db') else None,
            ensure_background_stream=self.ensure_background_camera_stream,
            get_latest_frame=self._get_latest_frame_for_hls,
        )
        self._hls_pipe_threads: Dict[str, threading.Thread] = {}
        self._hls_pipe_stop_events: Dict[str, threading.Event] = {}
        self._audio_chunk_analyzers: Dict[str, FrequencyIntensityAnalyzer] = {}
        self._latest_audio_chunk_analysis: Dict[str, Dict[str, Any]] = {}
        self._audio_streams: Dict[str, Any] = {}  # camera_id -> audio-only Capture
        self._drawing = StreamDrawingHelper()
        self.sensitivity_level = 5
        self.default_sensitivity = 2
        self._camera_sensitivity: Dict[str, int] = {}
        self._audio_chunk_index: Dict[str, int] = {}
        self._latest_pts_payload: Dict[str, Dict[Any, Dict[str, Any]]] = {}
        self._overlay_masks: Dict[str, np.ndarray] = {}

    def set_camera_sensitivity(self, camera_id: str, sensitivity: int) -> int:
        safe_sensitivity = max(0, min(int(self.sensitivity_level), int(sensitivity)))
        self._camera_sensitivity[camera_id] = safe_sensitivity
        return safe_sensitivity

    def get_camera_sensitivity(self, camera_id: str) -> int:
        try:
            value = int(self._camera_sensitivity.get(camera_id, self.default_sensitivity))
        except (TypeError, ValueError):
            value = self.default_sensitivity
        return max(0, min(int(self.sensitivity_level), value))

    def get_camera_effective_stride(self, camera_id: str) -> int:
        sensitivity = self.get_camera_sensitivity(camera_id)
        sensitivity_stride = max(1, int(self.sensitivity_level) - sensitivity)
        if sensitivity_stride >= int(self.sensitivity_level):
            return int(self.sensitivity_level)
        return max(1, sensitivity_stride)

    def _get_audio_chunk_analyzer(self, camera_id: str, db_camera: Dict[str, Any]) -> FrequencyIntensityAnalyzer:
        analyzer = self._audio_chunk_analyzers.get(camera_id)
        try:
            sample_rate = int(db_camera.get('audio_sample_rate'))
        except (TypeError, ValueError):
            sample_rate = int(os.getenv('AUDIO_SAMPLE_RATE', '16000'))
        channels = int(db_camera.get('audio_channels') or os.getenv('AUDIO_CHANNELS', '1'))

        if analyzer is not None:
            current_rate = getattr(analyzer.config, 'sample_rate', sample_rate)
            current_channels = getattr(analyzer.config, 'channels', channels)
            if current_rate == sample_rate and current_channels == channels:
                return analyzer

        analyzer = FrequencyIntensityAnalyzer(sample_rate=sample_rate, channels=channels)
        self._audio_chunk_analyzers[camera_id] = analyzer
        return analyzer

    def get_latest_audio_chunk_analysis(self, camera_id: str) -> Optional[Dict[str, Any]]:
        return self._latest_audio_chunk_analysis.get(camera_id)

    def ensure_background_camera_stream(self, camera_id: str):
        existing = self._background_camera_threads.get(camera_id)
        if existing and existing.is_alive():
            return

        def _worker():
            try:
                stream_iter = self.generate_live_video_stream(camera_id, emit_stream=False)
                next(stream_iter)
            except StopIteration:
                pass
            except Exception as error:
                logger.warning(f"Background camera stream worker stopped for {camera_id}: {error}")
            finally:
                self._background_camera_threads.pop(camera_id, None)

        thread = threading.Thread(target=_worker, daemon=True, name=f"bg-camera-stream-{camera_id}")
        self._background_camera_threads[camera_id] = thread
        thread.start()

    def _get_latest_frame_for_hls(self, camera_id: str) -> Optional[np.ndarray]:
        lock = self.stream_locks.get(camera_id)
        if lock is not None:
            with lock:
                frame = self._latest_hls_frames.get(camera_id)
                if frame is None:
                    frame = self._latest_frames.get(camera_id)
                return frame.copy() if frame is not None else None
        frame = self._latest_hls_frames.get(camera_id)
        if frame is None:
            frame = self._latest_frames.get(camera_id)
        return frame.copy() if frame is not None else None

    def start_hls_stream(self, camera_id: str) -> str:
        return self._hls_manager.start_stream(camera_id)

    def stop_hls_stream(self, camera_id: str, cleanup: bool = True):
        self._hls_manager.stop_stream(camera_id, cleanup=cleanup)

    def get_hls_stream_status(self, camera_id: str) -> Dict[str, Any]:
        return self._hls_manager.get_stream_status(camera_id)

    def get_hls_manifest_path(self, camera_id: str) -> str:
        return self._hls_manager.get_manifest_path(camera_id)

    def get_hls_segment_path(self, camera_id: str, segment_name: str) -> str:
        return self._hls_manager.get_segment_path(camera_id, segment_name)

    def _update_loop_fps(self, stream_key: str) -> float:
        now = time.time()
        stats = self._fps_stats.get(stream_key)
        if stats is None:
            self._fps_stats[stream_key] = {
                "window_start": now,
                "count": 1.0,
                "fps": 0.0,
            }
            return 0.0

        stats["count"] += 1.0
        elapsed = now - stats["window_start"]
        if elapsed >= 1.0:
            stats["fps"] = stats["count"] / elapsed
            stats["count"] = 0.0
            stats["window_start"] = now
        return stats["fps"]

    def _draw_fps_overlay(self, frame: np.ndarray, fps_value: float, res: dict = {}) -> np.ndarray:
        return self._drawing.draw_fps_overlay(frame, fps_value, res)

    def _format_audio_compact_text(self, analysis: Optional[Dict[str, Any]]) -> tuple[str, bool]:
        return self._drawing.format_audio_compact_text(analysis)

    def _truncate_overlay_text(self, text: str, font, font_scale: float, thickness: int, max_width: int) -> str:
        return self._drawing.truncate_overlay_text(text, font, font_scale, thickness, max_width)

    def _ensure_bgr_frame(self, frame: np.ndarray) -> np.ndarray:
        return self._drawing.ensure_bgr_frame(frame)

    @staticmethod
    def _aggregate_motion_result(raw_result: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        merged = {'vel': 0.0, 'bg_diff': 0, 'ts': time.time()}
        for item in raw_result.values():
            merged['vel'] = max(float(item.get('vel', 0.0) or 0.0), merged['vel'])
            merged['bg_diff'] = max(int(item.get('bg_diff', 0) or 0), merged['bg_diff'])
        return merged

    def _extract_detect_payload(
        self,
        detect_output: Any,
        fallback_frame: np.ndarray,
    ) -> tuple[np.ndarray, Dict[str, Any], Dict[Any, Dict[str, Any]]]:
        if isinstance(detect_output, np.ndarray):
            return detect_output, {}, {}

        if not isinstance(detect_output, tuple):
            return fallback_frame, {}, {}

        if len(detect_output) == 0:
            return fallback_frame, {}, {}

        frame_candidate = detect_output[0] if detect_output[0] is not None else fallback_frame
        points_dict: Dict[str, Any] = {}
        pts_payload: Dict[Any, Dict[str, Any]] = {}

        for item in detect_output[1:]:
            if isinstance(item, dict):
                if not points_dict and any(
                    isinstance(value, dict) and ('vel' in value or 'mean_vel' in value)
                    for value in item.values()
                ):
                    points_dict = item
                    continue
                if not pts_payload and any(
                    isinstance(value, dict) and ('keypoints_1' in value or 'keypoints_2' in value)
                    for value in item.values()
                ):
                    pts_payload = item

        return frame_candidate, points_dict, pts_payload

    def _plot_dynamic_stream_data(self, plot_array: np.ndarray, points_dict: Dict[str, Any], camera_id: str) -> tuple[np.ndarray, Dict[str, Dict[str, Any]]]:
        return self._drawing.plot_dynamic_stream_data(
            plot_array,
            points_dict,
            camera_id,
            self._latest_audio_chunk_analysis,
        )

    def _draw_box_tracking_overlay(self, frame: np.ndarray, camera_id: str, fps_value: float, res: Optional[Dict[str, Any]] = None) -> np.ndarray:
        return self._drawing.draw_box_tracking_overlay(
            frame,
            camera_id,
            fps_value,
            res,
            self._latest_audio_chunk_analysis,
        )

    def _draw_pts_flow_for_stream(self, frame: np.ndarray, camera_id: str, pts_payload: Optional[Dict[Any, Dict[str, Any]]]) -> np.ndarray:
        current_mask = self._overlay_masks.get(camera_id)
        frame_with_overlay, updated_mask = self._drawing.draw_pts_flow_for_stream(frame, pts_payload, current_mask)
        self._overlay_masks[camera_id] = updated_mask
        return frame_with_overlay

            
    def generate_failure_frame(self, msg: str = "Camera Unavailable"):
        failure_frame = np.zeros((480, 640, 3), dtype=np.uint8)  # Placeholder frame for errors
        w, h = cv2.getTextSize(msg, cv2.FONT_HERSHEY_COMPLEX, 0.7, 1)[0]
        x = (failure_frame.shape[1] - w) // 2
        y = (failure_frame.shape[0] + h) // 2
        failure_frame = cv2.putText(failure_frame, msg, (x, y), cv2.FONT_HERSHEY_COMPLEX, 0.7, (100, 100, 100), 1)
        failure_frame = self.frame_to_bytes(failure_frame)
        return failure_frame
    
    def generate_blank_image(self, msg:str = ''):
        blank_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        w, h = cv2.getTextSize(msg, cv2.FONT_HERSHEY_COMPLEX, 0.7, 1)[0]
        x = (blank_frame.shape[1] - w) // 2
        y = (blank_frame.shape[0] + h) // 2
        blank_frame = cv2.putText(blank_frame, msg, (x, y), cv2.FONT_HERSHEY_COMPLEX, 0.7, (100, 100, 100), 1)
        ret, buffer = cv2.imencode('.jpg', blank_frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buffer.tobytes()
    
    def frame_to_bytes(self, frame) -> bytes:
        """Convert a video frame to bytes for streaming"""
        quality = int(getattr(self, 'jpeg_quality', 70) or 70)
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        buffer = (b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        return buffer

    @staticmethod
    def _make_wav_header(sample_rate: int, num_channels: int, bits_per_sample: int = 16) -> bytes:
        """Build a 44-byte WAV header suitable for streaming.

        Both the RIFF and data chunk sizes are set to 0xFFFFFFFF (the standard
        sentinel for unknown/streaming length).  Most browsers, ffplay, and Web
        Audio API accept this without issue.
        """
        import struct
        byte_rate   = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        return struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', 0xFFFFFFFF,   # RIFF chunk size  — unknown/streaming
            b'WAVE',
            b'fmt ', 16,           # fmt  sub-chunk size
            1,                     # PCM audio format
            num_channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b'data', 0xFFFFFFFF,   # data sub-chunk size — unknown/streaming
        )

    def generate_live_audio_stream(self, camera_id: str, chunk_size: Optional[int] = None):
        db_camera = self.db.get_camera(camera_id)
        if not db_camera:
            raise ValueError(f"Camera not found: {camera_id}")

        if chunk_size is None:
            try:
                chunk_size = int(db_camera.get('audio_chunk_size'))
            except (TypeError, ValueError):
                chunk_size = int(os.getenv('AUDIO_CHUNK_SIZE', '512'))
        chunk_size = max(128, min(16384, int(chunk_size)))

        stream_token = f"{time.time_ns()}:{threading.get_ident()}"
        previous_token = self.active_audio_streams.get(camera_id)
        if previous_token:
            logger.info(f"Taking over existing audio stream for camera {camera_id}")
        self.active_audio_streams[camera_id] = stream_token

        # Start audio capture if not already running (mirrors generate_live_video_stream)
        cap = self._audio_streams.get(camera_id)
        if cap is None or not cap.is_audio_stream_opened():
            started = self.start_audio(camera_id)
            if not started:
                self.active_audio_streams.pop(camera_id, None)
                raise ValueError(f"Audio stream failed to start for camera: {camera_id}")
            cap = self._audio_streams.get(camera_id)

        if cap is None:
            self.active_audio_streams.pop(camera_id, None)
            raise ValueError(f"Audio capture unavailable for camera: {camera_id}")

        try:
            # Yield a streaming WAV header so the browser / Web Audio API gets a
            # properly typed audio stream without any extra encoding overhead.
            yield self._make_wav_header(cap.audio_sample_rate, cap.audio_channels)

            has_emitted_audio = False
            while cap.is_audio_stream_opened() and self.active_audio_streams.get(camera_id) == stream_token:
                ret, samples = cap.read_audio()
                if not ret or samples is None or samples.size == 0:
                    continue

                # Convert float32 samples → raw s16le bytes for streaming
                raw_chunk = (samples * 32768.0).clip(-32768, 32767).astype(np.int16).tobytes()

                self._audio_chunk_index[camera_id] = self._audio_chunk_index.get(camera_id, 0) + 1
                audio_chunk_index = self._audio_chunk_index[camera_id]
                effective_stride = self.get_camera_effective_stride(camera_id)
                if effective_stride == int(self.sensitivity_level):
                    should_run_audio_analysis = False
                else:
                    should_run_audio_analysis = (effective_stride == 1 or audio_chunk_index % effective_stride == 0)

                if should_run_audio_analysis:
                    analyzer = self._get_audio_chunk_analyzer(camera_id, db_camera)
                    analysis = analyzer.process_chunk(samples)
                    self._latest_audio_chunk_analysis[camera_id] = analysis
                    self._latest_res_audio[camera_id] = {
                        'int': float(analysis.get('overall_intensity', 0.0) or 0.0),
                        'freq': float(analysis.get('peak_frequency_mean', 0.0) or 0.0),
                    }

                has_emitted_audio = True
                yield raw_chunk

            if not has_emitted_audio:
                logger.warning(f"Live audio stream produced no data for camera {camera_id}")

        except Exception as error:
            logger.warning(f"Audio stream error for camera {camera_id}: {error}")

        finally:
            if self.active_audio_streams.get(camera_id) == stream_token:
                self.active_audio_streams.pop(camera_id, None)

    def stop_live_audio_stream(self, camera_id: str) -> bool:
        self.active_audio_streams.pop(camera_id, None)
        self._audio_chunk_analyzers.pop(camera_id, None)
        self._latest_audio_chunk_analysis.pop(camera_id, None)
        self._latest_res_audio.pop(camera_id, None)
        self._audio_chunk_index.pop(camera_id, None)
        cap = self._audio_streams.pop(camera_id, None)
        if not cap:
            return False
        try:
            cap.release_audio()
            return True
        except Exception:
            return True

    def generate_live_video_stream(self, camera_id: str, emit_stream: bool = True) -> Generator[bytes, None, None]:
        """Generate live video stream from camera."""
        # Get camera from database
        db_camera = self.db.get_camera(camera_id)
        
        if not db_camera:
            logger.warning(f"Camera not found: {camera_id}")
            return self.generate_failure_frame(f"Camera {camera_id} Not Found")
        
        logger.info(f"Generating stream for camera {camera_id}, status: {db_camera['status']}")

        # Create stream token; if an old stream exists for this camera, this takes over.
        # Old generator loop will exit when it sees token mismatch.
        stream_token = f"{time.time_ns()}:{threading.get_ident()}"
        previous_token = self.active_streams.get(camera_id)
        if previous_token:
            logger.info(f"Taking over existing stream for camera {camera_id}")
        self.active_streams[camera_id] = stream_token
        
        # Initialize camera capture if not already done
        if camera_id not in self._camera_streams:
            scc = self.start_camera(camera_id)
            if not scc:
                return self.generate_failure_frame("Camera Failed to Start")
        
        cap = self._camera_streams.get(camera_id)
        tracker = self._camera_trackers.get(camera_id)

        # Get or create lock for this camera
        if camera_id not in self.stream_locks:
            self.stream_locks[camera_id] = threading.Lock()
        
        lock = self.stream_locks[camera_id]

        while cap.is_video_stream_opened() and self.active_streams.get(camera_id) == stream_token:
            with lock:
                ret, frame = cap.read()

                if not ret:
                    if emit_stream:
                        yield self.generate_failure_frame("Failed to Read Frame")
                    continue
                # Store original frame for recording consumers
                self._latest_frames[camera_id] = frame
                self._latest_frame_seq[camera_id] = self._latest_frame_seq.get(camera_id, 0) + 1
                self._stream_frame_index[camera_id] = self._stream_frame_index.get(camera_id, 0) + 1
                frame_index = self._stream_frame_index[camera_id]

            # Resize frame if needed for better streaming performance
            frame  = self._resize_frame_for_streaming(frame)
            effective_stride = self.get_camera_effective_stride(camera_id)
            if effective_stride == int(self.sensitivity_level):
                should_run_tracker = False
            else:
                should_run_tracker = (effective_stride == 1) or (frame_index % effective_stride == 0)

            if should_run_tracker:
                detect_output = tracker.detect(frame, return_pts=True)
                frame, points_dict, pts_payload = self._extract_detect_payload(detect_output, frame)
                frame = self._draw_pts_flow_for_stream(frame, camera_id, pts_payload)
                viz1, _res = self._plot_dynamic_stream_data(frame, points_dict, camera_id)
                res = self._aggregate_motion_result(_res)
                with lock:
                    self._latest_viz[camera_id] = viz1
                    self._latest_res_video[camera_id] = res
                    self._latest_pts_payload[camera_id] = pts_payload
                    self._latest_hls_frames[camera_id] = frame
            else:
                with lock:
                    viz1 = self._latest_viz.get(camera_id)
                    res = self._latest_res_video.get(camera_id, {'vel': 0, 'bg_diff': 0, 'ts': time.time()})
                    if viz1 is None:
                        viz1 = frame
                        self._latest_viz[camera_id] = viz1
                    latest_pts = self._latest_pts_payload.get(camera_id)
                    frame = self._draw_pts_flow_for_stream(frame, camera_id, latest_pts)
                    self._latest_hls_frames[camera_id] = frame

            frame_fps = self._update_loop_fps(f"{camera_id}:primary")
            frame = self._draw_fps_overlay(frame, frame_fps)
            buffer = self.frame_to_bytes(frame)

            if emit_stream:
                yield buffer

        # Only clear if this stream is still the active owner
        if self.active_streams.get(camera_id) == stream_token:
            self.active_streams.pop(camera_id, None)
    
    def generate_processed_video_stream(self, camera_id: str) -> Generator[bytes, None, None]:
        """Generate processed video stream from camera."""
        # Get camera from database
        db_camera = self.db.get_camera(camera_id)

        if not db_camera:
            logger.warning(f"Camera not found: {camera_id}")
            return self.generate_failure_frame(f"Camera {camera_id} Not Found")

        stream_token = f"{time.time_ns()}:{threading.get_ident()}"
        previous_token = self.active_processing_streams.get(camera_id)
        if previous_token:
            logger.info(f"Taking over existing processing stream for camera {camera_id}")
        self.active_processing_streams[camera_id] = stream_token
        
        lock = self.stream_locks.get(camera_id)

        while camera_id in self._camera_trackers and self.active_processing_streams.get(camera_id) == stream_token:
            if lock is not None:
                with lock:
                    processed_frame = getattr(self, '_latest_viz', {}).get(camera_id, None)
            else:
                processed_frame = getattr(self, '_latest_viz', {}).get(camera_id, None)
            if processed_frame is None:
                yield self.generate_failure_frame("No Processed Frame Available")
                time.sleep(1.0 / 30)
                continue

            output_frame = processed_frame.copy()
            processing_fps = self._update_loop_fps(f"{camera_id}:processing")
            output_frame = self._draw_box_tracking_overlay(
                output_frame,
                camera_id,
                processing_fps,
                res=getattr(self, '_latest_res_video', {}).get(camera_id, {'vel': 0, 'bg_diff': 0}),
            )

            # Resize frame if needed for better streaming performance
            #processed_frame = self._resize_frame_for_streaming(frame)
            buffer = self.frame_to_bytes(output_frame)
            
            yield buffer
            
            # Small delay to control frame rate
            time.sleep(1.0 / 30)  # 30 FPS max

        if self.active_processing_streams.get(camera_id) == stream_token:
                self.active_processing_streams.pop(camera_id, None)
            
            
    def generate_recorded_video_stream(self, recording_id: str) -> Generator[bytes, None, None]:
        """Generate video stream from recorded file."""
        # Get recording from database
        db_recording = self.db.get_recording(recording_id)
        if not db_recording:
            raise ValueError(f"Recording not found: {recording_id}")
        
        # Resolve to absolute path to avoid CWD issues
        file_path = db_recording['file_path']
        abs_path = os.path.abspath(file_path)
        
        # Retry opening in case the writer is finalizing the file
        cap = None
        for _ in range(5):
            cap = cv2.VideoCapture(abs_path)
            if cap.isOpened():
                break
            time.sleep(0.2)
        
        if not cap or not cap.isOpened():
            raise ValueError(f"Failed to open recording file: {abs_path}")
        
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    # End of video, loop back to start
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                
                # Resize frame if needed
                frame = self._resize_frame_for_streaming(frame)
                
                # Encode frame as JPEG
                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not ret:
                    continue
                
                frame_bytes = buffer.tobytes()
                
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                
                # Control playback speed
                time.sleep(1.0 / 30)  # 30 FPS
                
        except Exception as e:
            logger.error(f"Error streaming recording {recording_id}: {e}")
        finally:
            cap.release()

    def _resize_frame_for_streaming(self, frame, max_width: int = 640):
        """Resize frame for optimal streaming performance"""
        height, width = frame.shape[:2]
        
        if width > max_width:
            # Calculate new height to maintain aspect ratio
            ratio = max_width / width
            new_width = max_width
            new_height = int(height * ratio)
            frame = cv2.resize(frame, (new_width, new_height))
        
        return frame
    
    def generate_result_json_stream(self, camera_id: str) -> Generator[Dict[str, Union[int, float]], None, None]:
        """Generate JSON stream of processing results for a camera"""
        while camera_id in self._camera_trackers:
            res = getattr(self, '_latest_res_video', {}).get(camera_id, None)
            if res is not None:
                yield json.dumps(res)
            time.sleep(1.0)  # Update every second