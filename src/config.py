"""Configuration loading and validation for RTSP Timelapse Generator."""

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

from .models import (
    AppConfig,
    CameraConfig,
    ExportPreset,
    PendingExport,
    ExportHistory,
)

logger = logging.getLogger(__name__)

# Default config directory relative to project root
DEFAULT_CONFIG_DIR = Path(__file__).parent.parent / "config"


class ConfigError(Exception):
    """Configuration error."""
    pass


class ConfigManager:
    """Manages application configuration."""

    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
        self.config_dir = Path(self.config_dir)

        self.app_config: AppConfig = AppConfig()
        self.cameras: dict[str, CameraConfig] = {}
        self.export_presets: dict[str, ExportPreset] = {}
        self.pending_exports: list[PendingExport] = []
        self.export_history: list[ExportHistory] = []

        self._ensure_config_files()

    def _ensure_config_files(self) -> None:
        """Create default config files if they don't exist."""
        self.config_dir.mkdir(parents=True, exist_ok=True)

        app_yaml = self.config_dir / "app.yaml"
        if not app_yaml.exists():
            self._write_default_app_config(app_yaml)

        cameras_yaml = self.config_dir / "cameras.yaml"
        if not cameras_yaml.exists():
            self._write_default_cameras_config(cameras_yaml)

        exports_yaml = self.config_dir / "exports.yaml"
        if not exports_yaml.exists():
            self._write_default_exports_config(exports_yaml)

    def _write_default_app_config(self, path: Path) -> None:
        """Write default app configuration."""
        default = {
            "web_ui": {
                "enabled": True,
                "host": "0.0.0.0",
                "port": 5000,
                "auth_enabled": False,
                "username": "admin",
                "password": "admin"
            },
            "storage": {
                "captures_path": "captures",
                "exports_path": "exports",
                "logs_path": "logs",
                "max_log_size_mb": 100
            },
            "log_level": "INFO"
        }
        with open(path, "w") as f:
            yaml.dump(default, f, default_flow_style=False, sort_keys=False)
        logger.info(f"Created default app config at {path}")

    def _write_default_cameras_config(self, path: Path) -> None:
        """Write default cameras configuration."""
        default = {
            "cameras": {
                "example_camera": {
                    "url": "rtsp://example.com/stream",
                    "enabled": False,
                    "schedules": [
                        {
                            "name": "hourly_daytime",
                            "frequency": "hourly",
                            "enabled": True,
                            "time_window": {
                                "start": "06:00",
                                "end": "20:00"
                            }
                        }
                    ],
                    "capture_settings": {
                        "jpeg_quality": 90,
                        "timeout_seconds": 10,
                        "retry_count": 3,
                        "retry_delay_seconds": 1.0
                    }
                }
            }
        }
        with open(path, "w") as f:
            yaml.dump(default, f, default_flow_style=False, sort_keys=False)
        logger.info(f"Created default cameras config at {path}")

    def _write_default_exports_config(self, path: Path) -> None:
        """Write default exports configuration."""
        default = {
            "presets": {
                "standard": {
                    "fps": 30,
                    "codec": "libx264",
                    "ffmpeg_preset": "medium",
                    "pixel_format": "yuv420p"
                },
                "fast_preview": {
                    "fps": 15,
                    "width": 854,
                    "height": 480,
                    "codec": "libx264",
                    "ffmpeg_preset": "ultrafast",
                    "pixel_format": "yuv420p"
                },
                "high_quality": {
                    "fps": 60,
                    "codec": "libx264",
                    "ffmpeg_preset": "slow",
                    "pixel_format": "yuv420p"
                }
            },
            "pending_exports": [],
            "export_history": []
        }
        with open(path, "w") as f:
            yaml.dump(default, f, default_flow_style=False, sort_keys=False)
        logger.info(f"Created default exports config at {path}")

    def load_all(self) -> None:
        """Load all configuration files."""
        self.load_app_config()
        self.load_cameras_config()
        self.load_exports_config()

    def load_app_config(self) -> AppConfig:
        """Load app.yaml configuration."""
        path = self.config_dir / "app.yaml"
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            self.app_config = AppConfig.from_dict(data)
            logger.debug(f"Loaded app config from {path}")
            return self.app_config
        except Exception as e:
            raise ConfigError(f"Failed to load app config: {e}")

    def load_cameras_config(self) -> dict[str, CameraConfig]:
        """Load cameras.yaml configuration."""
        path = self.config_dir / "cameras.yaml"
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}

            cameras_data = data.get("cameras", {})
            self.cameras = {}

            for name, cam_data in cameras_data.items():
                if not self._validate_camera_url(cam_data.get("url", "")):
                    logger.warning(f"Camera '{name}' has invalid URL, skipping")
                    continue
                self.cameras[name] = CameraConfig.from_dict(name, cam_data)

            logger.debug(f"Loaded {len(self.cameras)} cameras from {path}")
            return self.cameras
        except Exception as e:
            raise ConfigError(f"Failed to load cameras config: {e}")

    def load_exports_config(self) -> None:
        """Load exports.yaml configuration."""
        path = self.config_dir / "exports.yaml"
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}

            # Load presets
            presets_data = data.get("presets", {})
            self.export_presets = {
                name: ExportPreset.from_dict(name, preset_data)
                for name, preset_data in presets_data.items()
            }

            # Load pending exports
            pending_data = data.get("pending_exports", [])
            self.pending_exports = [
                PendingExport.from_dict(p) for p in pending_data
            ]

            # Load export history
            history_data = data.get("export_history", [])
            self.export_history = [
                ExportHistory.from_dict(h) for h in history_data
            ]

            logger.debug(f"Loaded {len(self.export_presets)} export presets")
        except Exception as e:
            raise ConfigError(f"Failed to load exports config: {e}")

    def _validate_camera_url(self, url: str) -> bool:
        """Validate camera URL format."""
        if not url:
            return False
        return url.startswith(("rtsp://", "rtsps://", "http://", "https://"))

    def save_cameras_config(self) -> None:
        """Save cameras configuration to file."""
        path = self.config_dir / "cameras.yaml"
        data = {"cameras": {}}

        for name, camera in self.cameras.items():
            cam_data = {
                "url": camera.url,
                "enabled": camera.enabled,
                "schedules": [],
                "capture_settings": {
                    "jpeg_quality": camera.capture_settings.jpeg_quality,
                    "timeout_seconds": camera.capture_settings.timeout_seconds,
                    "retry_count": camera.capture_settings.retry_count,
                    "retry_delay_seconds": camera.capture_settings.retry_delay_seconds,
                }
            }

            if camera.capture_settings.resolution_scale:
                cam_data["capture_settings"]["resolution_scale"] = camera.capture_settings.resolution_scale

            for schedule in camera.schedules:
                sched_data = {
                    "name": schedule.name,
                    "frequency": schedule.frequency.value,
                    "enabled": schedule.enabled,
                    "value": schedule.value,
                }
                if schedule.time_window:
                    sched_data["time_window"] = {
                        "start": schedule.time_window.start.strftime("%H:%M"),
                        "end": schedule.time_window.end.strftime("%H:%M"),
                    }
                cam_data["schedules"].append(sched_data)

            data["cameras"][name] = cam_data

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        logger.info(f"Saved cameras config to {path}")

    def save_exports_config(self) -> None:
        """Save exports configuration to file."""
        path = self.config_dir / "exports.yaml"
        data = {
            "presets": {},
            "pending_exports": [],
            "export_history": []
        }

        for name, preset in self.export_presets.items():
            preset_data = {
                "fps": preset.fps,
                "codec": preset.codec,
                "ffmpeg_preset": preset.ffmpeg_preset,
                "pixel_format": preset.pixel_format,
            }
            if preset.width:
                preset_data["width"] = preset.width
            if preset.height:
                preset_data["height"] = preset.height
            data["presets"][name] = preset_data

        for pending in self.pending_exports:
            data["pending_exports"].append({
                "id": pending.id,
                "camera": pending.camera,
                "start_date": pending.start_date,
                "end_date": pending.end_date,
                "preset": pending.preset,
                "auto_generate": pending.auto_generate,
            })

        for history in self.export_history:
            data["export_history"].append({
                "id": history.id,
                "camera": history.camera,
                "start_date": history.start_date,
                "end_date": history.end_date,
                "preset": history.preset,
                "output_file": history.output_file,
                "created_at": history.created_at,
                "image_count": history.image_count,
                "duration_seconds": history.duration_seconds,
                "file_size_bytes": history.file_size_bytes,
            })

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        logger.info(f"Saved exports config to {path}")

    def get_captures_path(self) -> Path:
        """Get the captures directory path."""
        base = Path(__file__).parent.parent
        return base / self.app_config.storage.captures_path

    def get_exports_path(self) -> Path:
        """Get the exports directory path."""
        base = Path(__file__).parent.parent
        return base / self.app_config.storage.exports_path

    def get_logs_path(self) -> Path:
        """Get the logs directory path."""
        base = Path(__file__).parent.parent
        return base / self.app_config.storage.logs_path


# Global config instance
_config_manager: Optional[ConfigManager] = None


def get_config() -> ConfigManager:
    """Get the global config manager instance."""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
        _config_manager.load_all()
    return _config_manager


def reload_config() -> ConfigManager:
    """Reload all configuration files."""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    _config_manager.load_all()
    return _config_manager
