from pydantic import BaseModel
from typing import Optional, Dict, Any, Literal
from datetime import datetime
from enum import Enum

ResolutionOption = Literal[
    "320x240",
    "480x360",
    "640x480",
    "1280x720",
    "1920x1080"
]

class CameraType(str, Enum):
    RTSP = "rtsp"
    WEBCAM = "webcam"
    IP_CAMERA = "ip_camera"
    RECORDED = "recorded"

class CameraStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    RECORDING = "recording"
    ERROR = "error"

class CameraBase(BaseModel):
    name: str
    camera_type: CameraType
    source: str  # RTSP URL, device index, or IP camera URL
    resolution: Optional[ResolutionOption] = "640x480"
    fps: Optional[int] = 30
    enabled: bool = True
    description: Optional[str] = None
    location: Optional[str] = None
    audio_enabled: bool = False
    audio_source: Optional[str] = None
    audio_input_format: Optional[str] = None
    audio_sample_rate: Optional[int] = 16000
    audio_chunk_size: Optional[int] = 512
    keep_online: bool = True

class CameraCreate(CameraBase):
    pass

class CameraUpdate(CameraBase):
    name: Optional[str] = None
    camera_type: Optional[CameraType] = None
    source: Optional[str] = None
    resolution: Optional[ResolutionOption] = None
    fps: Optional[int] = None
    enabled: Optional[bool] = None
    description: Optional[str] = None
    location: Optional[str] = None
    audio_enabled: Optional[bool] = None
    audio_source: Optional[str] = None
    audio_input_format: Optional[str] = None
    audio_sample_rate: Optional[int] = None
    audio_chunk_size: Optional[int] = None
    keep_online: Optional[bool] = None

class Camera(CameraBase):
    id: str
    status: CameraStatus
    created_at: datetime
    last_seen: Optional[datetime] = None
    recording_id: Optional[str] = None
    processing_active: bool = False
    processing_type: Optional[str] = None
    
    class Config:
        from_attributes = True