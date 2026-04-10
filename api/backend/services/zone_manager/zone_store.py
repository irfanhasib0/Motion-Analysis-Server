"""
Zone Store — thread-safe file-backed persistence for per-camera zone configs.

One JSON file per camera: {zones_dir}/{safe_camera_id}.json
An in-memory cache avoids redundant disk reads during high-frequency
video-thread calls (invalidated by the API on every write).
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional

from .zone_model import CameraZoneConfig, ZoneDefinition, ZoneType

logger = logging.getLogger(__name__)


class ZoneStore:
    """Thread-safe file-based persistence for per-camera zone configurations."""

    def __init__(self, zones_dir: str) -> None:
        self.zones_dir = zones_dir
        os.makedirs(zones_dir, exist_ok=True)
        self._cache: Dict[str, CameraZoneConfig] = {}
        self._lock = threading.RLock()

    # ── public read API ───────────────────────────────────────────────────────

    def get_camera_zones(self, camera_id: str) -> CameraZoneConfig:
        with self._lock:
            if camera_id not in self._cache:
                self._cache[camera_id] = self._load(camera_id)
            return self._cache[camera_id]

    def get_all_zones(self) -> Dict[str, CameraZoneConfig]:
        """Return configs for every camera that has a zones file on disk."""
        result: Dict[str, CameraZoneConfig] = {}
        try:
            for fname in os.listdir(self.zones_dir):
                if fname.endswith(".json"):
                    camera_id = fname[:-5]
                    result[camera_id] = self.get_camera_zones(camera_id)
        except OSError:
            pass
        return result

    def get_active_mask_zones(self, camera_id: str) -> List[ZoneDefinition]:
        """Return enabled ACTIVE_MASK zones for a camera (used by streaming service)."""
        config = self.get_camera_zones(camera_id)
        return [z for z in config.zones
                if z.zone_type == ZoneType.ACTIVE_MASK and z.enabled]

    def get_active_zones(self, camera_id: str) -> List[ZoneDefinition]:
        """Return enabled ACTIVE_ZONE zones for a camera (used by streaming service)."""
        config = self.get_camera_zones(camera_id)
        return [z for z in config.zones
                if z.zone_type == ZoneType.ACTIVE_ZONE and z.enabled]

    # ── public write API ──────────────────────────────────────────────────────

    def save_camera_zones(self, config: CameraZoneConfig) -> None:
        with self._lock:
            config.updated_at = datetime.utcnow()
            self._cache[config.camera_id] = config
            self._persist(config)

    def invalidate_cache(self, camera_id: str) -> None:
        with self._lock:
            self._cache.pop(camera_id, None)

    # ── private helpers ───────────────────────────────────────────────────────

    def _load(self, camera_id: str) -> CameraZoneConfig:
        path = self._path(camera_id)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                return CameraZoneConfig(**data)
            except Exception as exc:
                logger.warning(f"[ZoneStore] Failed to load zones for {camera_id}: {exc}")
        return CameraZoneConfig(camera_id=camera_id)

    def _persist(self, config: CameraZoneConfig) -> None:
        path = self._path(config.camera_id)
        try:
            with open(path, "w") as f:
                json.dump(config.model_dump(), f, indent=2, default=str)
        except Exception as exc:
            logger.error(f"[ZoneStore] Failed to save zones for {config.camera_id}: {exc}")

    def _path(self, camera_id: str) -> str:
        # Sanitise camera_id so it's safe as a filename component.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in camera_id)
        return os.path.join(self.zones_dir, f"{safe}.json")


# ── Module-level singleton ────────────────────────────────────────────────────

_store: Optional[ZoneStore] = None


def init_zone_store(zones_dir: str) -> ZoneStore:
    """Initialise the module-level singleton — call once from start_server.py."""
    global _store
    _store = ZoneStore(zones_dir)
    logger.info(f"[ZoneStore] Initialised at {zones_dir}")
    return _store


def get_zone_store() -> Optional[ZoneStore]:
    """Return the singleton created by init_zone_store(), or None if not yet initialised."""
    return _store
