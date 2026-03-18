"""Request/response models for API endpoints."""
from pydantic import BaseModel
from typing import Optional, List


class SystemSettingsFields(BaseModel):
    """Shared settings fields used by both system settings and performance profile endpoints."""
    live_stream_mode: Optional[str] = None
    sensitivity: Optional[int] = None
    jpeg_quality: Optional[int] = None
    pipe_buffer_size: Optional[int] = None
    max_vel: Optional[float] = None
    bg_diff: Optional[int] = None
    max_clip_length: Optional[int] = None
    motion_check_interval: Optional[int] = None
    min_free_storage_bytes: Optional[int] = None
    rtsp_unified_demux_enabled: Optional[bool] = None
    frame_rbf_len: Optional[int] = None
    audio_rbf_len: Optional[int] = None
    results_rbf_len: Optional[int] = None
    mux_realtime: Optional[bool] = None


class PerformanceProfileUpdateRequest(SystemSettingsFields):
    preset_name: str


class LiveStreamModeRequest(BaseModel):
    mode: str


class CameraSensitivityRequest(BaseModel):
    sensitivity: int


class SystemSettingsUpdateRequest(SystemSettingsFields):
    auto_archive_days: Optional[int] = None


class RecordingMetaUpdate(BaseModel):
    label: Optional[str] = None
    note: Optional[str] = None


class ArchiveExportRequest(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    camera_ids: Optional[List[str]] = None
    min_vel: Optional[float] = None
    min_diff: Optional[float] = None
    min_duration: Optional[float] = None
    delete_after: bool = False
    exclude_mode: bool = True
    label_filter: Optional[List[str]] = None
    clean_up_extensions: Optional[List[str]] = None


class ArchivePathRequest(BaseModel):
    archive_path: str
