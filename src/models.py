"""Data models for RTSP Timelapse Generator."""

from dataclasses import dataclass, field
from datetime import time
from typing import Optional
from enum import Enum


class FrequencyType(Enum):
    """Schedule frequency types."""
    HOURLY = "hourly"
    INTERVAL = "interval"
    X_PER_DAY = "x_per_day"


@dataclass
class TimeWindow:
    """Time window for schedule execution."""
    start: time = field(default_factory=lambda: time(0, 0))
    end: time = field(default_factory=lambda: time(23, 59))

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["TimeWindow"]:
        if data is None:
            return None
        return cls(
            start=time.fromisoformat(data.get("start", "00:00")),
            end=time.fromisoformat(data.get("end", "23:59"))
        )


@dataclass
class Schedule:
    """Capture schedule configuration."""
    name: str
    frequency: FrequencyType
    enabled: bool = True
    value: int = 1  # For interval (hours) or x_per_day (count)
    time_window: Optional[TimeWindow] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Schedule":
        return cls(
            name=data.get("name", "default"),
            frequency=FrequencyType(data.get("frequency", "hourly")),
            enabled=data.get("enabled", True),
            value=data.get("value", 1),
            time_window=TimeWindow.from_dict(data.get("time_window"))
        )


@dataclass
class CaptureSettings:
    """Settings for image capture."""
    jpeg_quality: int = 90
    resolution_scale: Optional[float] = None  # None means original resolution
    timeout_seconds: int = 10
    retry_count: int = 3
    retry_delay_seconds: float = 1.0


@dataclass
class CameraConfig:
    """Camera configuration."""
    name: str
    url: str
    enabled: bool = True
    schedules: list[Schedule] = field(default_factory=list)
    capture_settings: CaptureSettings = field(default_factory=CaptureSettings)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "CameraConfig":
        schedules = [Schedule.from_dict(s) for s in data.get("schedules", [])]

        capture_data = data.get("capture_settings", {})
        capture_settings = CaptureSettings(
            jpeg_quality=capture_data.get("jpeg_quality", 90),
            resolution_scale=capture_data.get("resolution_scale"),
            timeout_seconds=capture_data.get("timeout_seconds", 10),
            retry_count=capture_data.get("retry_count", 3),
            retry_delay_seconds=capture_data.get("retry_delay_seconds", 1.0)
        )

        return cls(
            name=name,
            url=data.get("url", ""),
            enabled=data.get("enabled", True),
            schedules=schedules,
            capture_settings=capture_settings
        )


@dataclass
class ExportPreset:
    """Export preset configuration."""
    name: str
    fps: int = 30
    width: Optional[int] = None
    height: Optional[int] = None
    codec: str = "libx264"
    ffmpeg_preset: str = "medium"
    pixel_format: str = "yuv420p"

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "ExportPreset":
        return cls(
            name=name,
            fps=data.get("fps", 30),
            width=data.get("width"),
            height=data.get("height"),
            codec=data.get("codec", "libx264"),
            ffmpeg_preset=data.get("ffmpeg_preset", "medium"),
            pixel_format=data.get("pixel_format", "yuv420p")
        )


@dataclass
class WebUIConfig:
    """Web UI configuration."""
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 5000
    auth_enabled: bool = False
    username: str = "admin"
    password: str = "admin"


@dataclass
class StorageConfig:
    """Storage configuration."""
    captures_path: str = "captures"
    exports_path: str = "exports"
    logs_path: str = "logs"
    max_log_size_mb: int = 100


@dataclass
class AppConfig:
    """Application configuration."""
    web_ui: WebUIConfig = field(default_factory=WebUIConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        web_ui_data = data.get("web_ui", {})
        web_ui = WebUIConfig(
            enabled=web_ui_data.get("enabled", True),
            host=web_ui_data.get("host", "0.0.0.0"),
            port=web_ui_data.get("port", 5000),
            auth_enabled=web_ui_data.get("auth_enabled", False),
            username=web_ui_data.get("username", "admin"),
            password=web_ui_data.get("password", "admin")
        )

        storage_data = data.get("storage", {})
        storage = StorageConfig(
            captures_path=storage_data.get("captures_path", "captures"),
            exports_path=storage_data.get("exports_path", "exports"),
            logs_path=storage_data.get("logs_path", "logs"),
            max_log_size_mb=storage_data.get("max_log_size_mb", 100)
        )

        return cls(
            web_ui=web_ui,
            storage=storage,
            log_level=data.get("log_level", "INFO")
        )


@dataclass
class PendingExport:
    """Pending export job."""
    id: str
    camera: str
    start_date: str
    end_date: str
    preset: str = "standard"
    auto_generate: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "PendingExport":
        return cls(
            id=data.get("id", ""),
            camera=data.get("camera", ""),
            start_date=data.get("start_date", ""),
            end_date=data.get("end_date", ""),
            preset=data.get("preset", "standard"),
            auto_generate=data.get("auto_generate", False)
        )


@dataclass
class ExportHistory:
    """Export history entry."""
    id: str
    camera: str
    start_date: str
    end_date: str
    preset: str
    output_file: str
    created_at: str
    image_count: int
    duration_seconds: float
    file_size_bytes: int

    @classmethod
    def from_dict(cls, data: dict) -> "ExportHistory":
        return cls(
            id=data.get("id", ""),
            camera=data.get("camera", ""),
            start_date=data.get("start_date", ""),
            end_date=data.get("end_date", ""),
            preset=data.get("preset", ""),
            output_file=data.get("output_file", ""),
            created_at=data.get("created_at", ""),
            image_count=data.get("image_count", 0),
            duration_seconds=data.get("duration_seconds", 0.0),
            file_size_bytes=data.get("file_size_bytes", 0)
        )
