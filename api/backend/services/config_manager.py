import os
import yaml
from typing import Dict, List, Optional, Any
from datetime import datetime


_SYSTEM_DEFAULTS: Dict[str, Any] = {
    'live_stream_mode': 'mjpeg',
    'uvicorn_reload': True,
    'low_power_mode': False,
    'sensitivity': 4,
    'jpeg_quality': 70,
    'pipe_buffer_size': 100000000,
    'max_vel': 0.1,
    'bg_diff': 50,
    'max_clip_length': 60,
    'motion_check_interval': 10,
    'min_free_storage_bytes': 1073741824,
    'rtsp_unified_demux_enabled': False,
    # Advanced Performance Settings
    'frame_rbf_len': 10,
    'audio_rbf_len': 10,
    'results_rbf_len': 10,
    # Recording Settings
    'mux_realtime': False,
}


class ConfigManager:
    """YAML-based dynamic text database for cameras, recordings, and system settings.

    Mirrors the public API of DatabaseService using YAML files stored under configs/.
    """

    def __init__(self, configs_dir: Optional[str] = './configs'):
        
        os.makedirs(configs_dir, exist_ok=True)

        self.cameras_path = os.path.join(configs_dir, "cameras.yaml")
        self.recordings_path = os.path.join(configs_dir, "recordings.yaml")
        self.system_path = os.path.join(configs_dir, "system.yaml")

        # Ensure files exist with base structure
        if not os.path.exists(self.cameras_path):
            self._write_yaml(self.cameras_path, {"cameras": []})
        if not os.path.exists(self.recordings_path):
            self._write_yaml(self.recordings_path, {"recordings": []})
        if not os.path.exists(self.system_path):
            self._write_yaml(self.system_path, {"system": dict(_SYSTEM_DEFAULTS)})
        
        # Load into memory
        self.cameras: List[Dict[str, Any]] = self._read_yaml(self.cameras_path).get("cameras", [])
        self.recordings: List[Dict[str, Any]] = self._read_yaml(self.recordings_path).get("recordings", [])

    # Helpers
    def _now(self) -> str:
        return datetime.utcnow().isoformat()

    def _read_yaml(self, path: str) -> Dict[str, Any]:
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        return data

    def _write_yaml(self, path: str, data: Dict[str, Any]) -> None:
        with open(path, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)

    def _save_cameras(self) -> None:
        self._write_yaml(self.cameras_path, {"cameras": self.cameras})

    def _save_recordings(self) -> None:
        self._write_yaml(self.recordings_path, {"recordings": self.recordings})

    # System settings operations
    def get_system_settings(self) -> Dict[str, Any]:
        """Return system settings, merging defaults with what is stored on disk."""
        result = dict(_SYSTEM_DEFAULTS)
        if os.path.exists(self.system_path):
            stored = self._read_yaml(self.system_path).get("system", {}) or {}
            result.update(stored)
        return result

    def save_system_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Merge *updates* into the stored system settings and persist to disk."""
        current = self.get_system_settings()
        current.update({k: v for k, v in updates.items() if k in _SYSTEM_DEFAULTS})
        self._write_yaml(self.system_path, {"system": current})
        return current

    def _ensure_camera_defaults(self, cam: Dict[str, Any]) -> Dict[str, Any]:
        cam.setdefault("camera_type", "webcam")
        cam.setdefault("fps", 30)
        cam.setdefault("resolution", "640x480")
        cam.setdefault("status", "offline")
        cam.setdefault("processing_active", False)
        cam.setdefault("processing_type", None)
        cam.setdefault("processing_params", {})
        cam.setdefault("created_at", self._now())
        cam.setdefault("updated_at", self._now())
        cam.setdefault("audio_enabled", False)
        cam.setdefault("audio_source", None)
        cam.setdefault("audio_input_format", None)
        cam.setdefault("audio_sample_rate", 16000)
        cam.setdefault("audio_chunk_size", 512)
        return cam

    def _ensure_recording_defaults(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        rec.setdefault("status", "recording")
        rec.setdefault("end_time", None)
        rec.setdefault("duration", None)
        rec.setdefault("file_size", None)
        rec.setdefault("created_at", self._now())
        return rec

    # Camera operations
    def create_camera(self, camera_data: Dict[str, Any]) -> Dict[str, Any]:
        required = ["id", "name", "source"]
        if any(k not in camera_data for k in required):
            raise ValueError(f"Missing required camera fields: {required}")

        if self.get_camera(camera_data["id"]) is not None:
            raise ValueError(f"Camera already exists: {camera_data['id']}")

        cam = {**camera_data}
        cam = self._ensure_camera_defaults(cam)
        self.cameras.append(cam)
        self._save_cameras()
        return self.get_camera(camera_data["id"])  # type: ignore

    def get_camera(self, camera_id: str) -> Optional[Dict[str, Any]]:
        for cam in self.cameras:
            if cam.get("id") == camera_id:
                # Normalize boolean
                cam["processing_active"] = bool(cam.get("processing_active", False))
                return {**cam}
        return None

    def get_all_cameras(self) -> List[Dict[str, Any]]:
        return [{**c, "processing_active": bool(c.get("processing_active", False))} for c in self.cameras]

    def update_camera(self, camera_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for idx, cam in enumerate(self.cameras):
            if cam.get("id") == camera_id:
                updated = {**cam, **updates}
                if "processing_active" in updates:
                    updated["processing_active"] = bool(updates["processing_active"])
                if "processing_params" in updates and updated["processing_params"] is None:
                    updated["processing_params"] = {}
                updated["updated_at"] = self._now()
                self.cameras[idx] = updated
                self._save_cameras()
                return self.get_camera(camera_id)
        return None

    def delete_camera(self, camera_id: str) -> bool:
        before = len(self.cameras)
        self.cameras = [c for c in self.cameras if c.get("id") != camera_id]
        deleted = len(self.cameras) < before
        if deleted:
            self._save_cameras()
        return deleted

    # Recording operations
    def create_recording(self, recording_data: Dict[str, Any]) -> Dict[str, Any]:
        required = ["id", "camera_id", "file_path", "start_time"]
        if any(k not in recording_data for k in required):
            raise ValueError(f"Missing required recording fields: {required}")

        if self.get_recording(recording_data["id"]) is not None:
            raise ValueError(f"Recording already exists: {recording_data['id']}")

        rec = {**recording_data}
        rec = self._ensure_recording_defaults(rec)
        self.recordings.append(rec)
        self._save_recordings()
        return self.get_recording(recording_data["id"])  # type: ignore

    def get_recording(self, recording_id: str) -> Optional[Dict[str, Any]]:
        for rec in self.recordings:
            if rec.get("id") == recording_id:
                return {**rec}
        return None

    def get_recordings_by_camera(self, camera_id: str) -> List[Dict[str, Any]]:
        return [r.copy() for r in self.recordings if r.get("camera_id") == camera_id]

    def get_all_recordings(self) -> List[Dict[str, Any]]:
        # Include camera_name similar to DatabaseService
        cameras_by_id = {c.get("id"): c.get("name") for c in self.cameras}
        out: List[Dict[str, Any]] = []
        for r in self.recordings:
            item = r.copy()
            item["camera_name"] = cameras_by_id.get(r.get("camera_id"))
            out.append(item)
        # Sort by start_time descending if present
        return sorted(out, key=lambda x: x.get("start_time", ""), reverse=True)

    def update_recording(self, recording_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for idx, rec in enumerate(self.recordings):
            if rec.get("id") == recording_id:
                updated = {**rec, **updates}
                self.recordings[idx] = updated
                self._save_recordings()
                return self.get_recording(recording_id)
        return None

    def delete_recording(self, recording_id: str) -> bool:
        before = len(self.recordings)
        self.recordings = [r for r in self.recordings if r.get("id") != recording_id]
        deleted = len(self.recordings) < before
        if deleted:
            self._save_recordings()
        return deleted

    def get_active_recordings(self) -> List[Dict[str, Any]]:
        return [r.copy() for r in self.recordings if r.get("status") == "recording"]
