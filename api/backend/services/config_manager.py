import os
import yaml
import psutil
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

from services.colors import Colors

logger = logging.getLogger(__name__)


# System-level metadata keys (not configuration values)
_SYSTEM_META_KEYS = {'active_preset', 'ram_auto_switch_enabled', 'ram_threshold_bytes', 'presets'}

# All required configuration keys that MUST exist in every preset in system.yaml
_REQUIRED_CONFIG_KEYS = {
    'live_stream_mode',
    'sensitivity',
    'jpeg_quality',
    'pipe_buffer_size',
    'max_vel',
    'bg_diff',
    'max_clip_length',
    'motion_check_interval',
    'min_free_storage_gb',
    'rtsp_unified_demux_enabled',
    'frame_rbf_len',
    'audio_rbf_len',
    'results_rbf_len',
    'mux_realtime',
    'auto_archive_days',
    'enable_person_detection',
}

# Presets that cannot be modified from the frontend
_PROTECTED_PRESETS = {'default', 'low_power'}

_GB = 1_073_741_824  # bytes per gigabyte


class ConfigManager:
    """YAML-based dynamic text database for cameras, recordings, and system settings.

    Mirrors the public API of DatabaseService using YAML files stored under configs/.
    """

    def __init__(self, configs_dir: Optional[str] = './configs'):
        
        os.makedirs(configs_dir, exist_ok=True)

        self.cameras_path = os.path.join(configs_dir, "cameras.yaml")
        self.recordings_path = os.path.join(configs_dir, "recordings.yaml")
        self.system_path = os.path.join(os.path.dirname(configs_dir), "system.yaml")

        # Ensure files exist with base structure
        if not os.path.exists(self.cameras_path):
            self._write_yaml(self.cameras_path, {"cameras": []})
        if not os.path.exists(self.recordings_path):
            self._write_yaml(self.recordings_path, {"cameras": {}})
        if not os.path.exists(self.system_path):
            raise FileNotFoundError(
                f"system.yaml not found at {self.system_path}. "
                "This file is required and must contain all preset configurations."
            )
        
        # Auto-detect low RAM and switch to low_power preset if needed
        self._auto_detect_ram_preset()
        
        # Load into memory
        self.cameras: List[Dict[str, Any]] = self._read_yaml(self.cameras_path).get("cameras", [])
        self.recordings: List[Dict[str, Any]] = self._load_recordings()

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
        # Nest flat list into camera → date → recordings structure
        nested: Dict[str, Dict[str, list]] = {}
        for rec in self.recordings:
            cam_id = rec.get('camera_id', 'unknown')
            dt_str = (rec.get('start_time') or rec.get('created_at') or '')[:10]
            if not dt_str:
                dt_str = 'unknown'
            nested.setdefault(cam_id, {}).setdefault(dt_str, []).append(rec)
        self._write_yaml(self.recordings_path, {"cameras": nested})

    def _load_recordings(self) -> List[Dict[str, Any]]:
        """Load recordings from YAML, supporting both nested and flat formats."""
        data = self._read_yaml(self.recordings_path)
        # New nested format: cameras → date → recordings
        if 'cameras' in data and isinstance(data['cameras'], dict):
            flat: List[Dict[str, Any]] = []
            for _cam_id, dates in data['cameras'].items():
                if isinstance(dates, dict):
                    for _date, recs in dates.items():
                        if isinstance(recs, list):
                            flat.extend(recs)
            return flat
        # Old flat format: recordings → list
        return data.get('recordings', [])

    def _auto_detect_ram_preset(self) -> None:
        """On startup, if RAM < threshold and ram_auto_switch is enabled, switch to low_power preset."""
        try:
            data = self._read_yaml(self.system_path)
            sys_meta = data.get('system', {})
            ram_auto = sys_meta.get('ram_auto_switch_enabled', True)
            ram_threshold = int(sys_meta.get('ram_threshold_bytes', 1073741824))
            
            if not ram_auto:
                return
            
            total_memory = psutil.virtual_memory().total
            if 0 < total_memory <= ram_threshold:
                current_preset = sys_meta.get('active_preset', 'default')
                if current_preset != 'low_power':
                    logger.warning(
                        f"{Colors.YELLOW}RAM ({total_memory / (1024**3):.1f} GB) below threshold "
                        f"({ram_threshold / (1024**3):.1f} GB). Auto-switching to low_power preset.{Colors.RESET}"
                    )
                    sys_meta['active_preset'] = 'low_power'
                    data['system'] = sys_meta
                    self._write_yaml(self.system_path, data)
        except Exception as e:
            logger.error(f"{Colors.RED}Failed to auto-detect RAM preset: {e}{Colors.RESET}")

    def _validate_preset_keys(self, preset_name: str, preset_values: Dict[str, Any]) -> None:
        """Raise an error if any required config key is missing from a preset."""
        missing = _REQUIRED_CONFIG_KEYS - set(preset_values.keys())
        if missing:
            raise KeyError(
                f"Missing required config key(s) {sorted(missing)} in preset '{preset_name}' "
                f"in {self.system_path}. Add them to system.yaml under system.presets.{preset_name}"
            )

    # System settings operations
    def get_system_settings(self) -> Dict[str, Any]:
        """Return system settings by reading from active preset + system metadata.
        
        Raises KeyError if any required config key is missing from the active preset.
        """
        data = self._read_yaml(self.system_path)
        sys_section = data.get('system', {})
        
        # Build result with system metadata
        result = {
            'active_preset': sys_section.get('active_preset', 'default'),
            'ram_auto_switch_enabled': sys_section.get('ram_auto_switch_enabled', True),
            'ram_threshold_bytes': sys_section.get('ram_threshold_bytes', 1073741824),
        }
        
        # Get configuration values from active preset - no fallbacks
        active_preset = result['active_preset']
        presets = data.get('system', {}).get('presets', {})
        
        if active_preset not in presets:
            raise KeyError(
                f"Active preset '{active_preset}' not found in system.yaml. "
                f"Available presets: {list(presets.keys())}"
            )
        
        preset_values = presets[active_preset]
        self._validate_preset_keys(active_preset, preset_values)
        result.update(self._preset_gb_to_bytes(preset_values))
        
        return result

    def save_custom_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Save configuration values to the 'custom' preset only.
        
        Protected presets (default, low_power) cannot be modified.
        Automatically sets active_preset to 'custom'.
        """
        data = self._read_yaml(self.system_path)
        
        if 'system' not in data:
            data['system'] = {}
        if 'presets' not in data.get('system', {}):
            data['system']['presets'] = {}
        
        # Ensure custom preset exists (copy from default if not)
        if 'custom' not in data['system']['presets']:
            default_preset = data['system']['presets'].get('default', {})
            data['system']['presets']['custom'] = dict(default_preset)
        
        # Write only config values to custom preset (convert bytes → GB for storage)
        yaml_updates = self._preset_bytes_to_gb(updates)
        for key, value in yaml_updates.items():
            if key not in _SYSTEM_META_KEYS:
                data['system']['presets']['custom'][key] = value
        
        # Switch active preset to custom
        data['system']['active_preset'] = 'custom'
        
        self._write_yaml(self.system_path, data)
        return self.get_system_settings()

    def get_presets(self) -> Dict[str, Dict[str, Any]]:
        """Get available presets from system.yaml (units converted to runtime values)."""
        data = self._read_yaml(self.system_path)
        raw = data.get('system', {}).get('presets', {})
        return {name: self._preset_gb_to_bytes(values) for name, values in raw.items()}

    @staticmethod
    def _preset_gb_to_bytes(preset: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of preset with min_free_storage_gb converted from GB to bytes."""
        result = dict(preset)
        if 'min_free_storage_gb' in result:
            result['min_free_storage_gb'] = int(float(result['min_free_storage_gb']) * _GB)
        return result

    @staticmethod
    def _preset_bytes_to_gb(updates: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of updates with min_free_storage_gb converted from bytes to GB."""
        result = dict(updates)
        if 'min_free_storage_gb' in result:
            result['min_free_storage_gb'] = float(result['min_free_storage_gb']) / _GB
        return result

    def apply_preset(self, preset_name: str) -> Dict[str, Any]:
        """Apply a preset by updating active_preset metadata.
        
        Validates that the preset exists and has all required keys.
        """
        presets = self.get_presets()
        if preset_name not in presets:
            raise ValueError(f"Preset '{preset_name}' not found. Available: {list(presets.keys())}")
        
        # Validate the target preset has all required keys
        self._validate_preset_keys(preset_name, presets[preset_name])
        
        data = self._read_yaml(self.system_path)
        data['system']['active_preset'] = preset_name
        self._write_yaml(self.system_path, data)
        
        return self.get_system_settings()

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
