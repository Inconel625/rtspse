"""RTSP capture module for capturing frames from camera streams."""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2

from .models import CameraConfig, CaptureSettings

logger = logging.getLogger(__name__)


class CaptureError(Exception):
    """Error during frame capture."""
    pass


class CaptureManager:
    """Manages RTSP frame capture."""

    def __init__(self, captures_path: Path):
        self.captures_path = Path(captures_path)
        self.captures_path.mkdir(parents=True, exist_ok=True)

    def capture_frame(
        self,
        camera: CameraConfig,
        settings: Optional[CaptureSettings] = None
    ) -> Optional[Path]:
        """
        Capture a single frame from an RTSP stream.

        Args:
            camera: Camera configuration
            settings: Optional capture settings override

        Returns:
            Path to saved image or None on failure
        """
        if not camera.enabled:
            logger.debug(f"Camera '{camera.name}' is disabled, skipping capture")
            return None

        settings = settings or camera.capture_settings
        last_error: Optional[Exception] = None

        for attempt in range(settings.retry_count):
            try:
                return self._do_capture(camera, settings)
            except CaptureError as e:
                last_error = e
                logger.warning(
                    f"Capture attempt {attempt + 1}/{settings.retry_count} "
                    f"failed for '{camera.name}': {e}"
                )
                if attempt < settings.retry_count - 1:
                    delay = settings.retry_delay_seconds * (2 ** attempt)
                    time.sleep(delay)

        logger.error(f"All capture attempts failed for '{camera.name}': {last_error}")
        return None

    def _do_capture(
        self,
        camera: CameraConfig,
        settings: CaptureSettings
    ) -> Path:
        """Execute a single capture attempt."""
        cap = cv2.VideoCapture(camera.url)

        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, settings.timeout_seconds * 1000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, settings.timeout_seconds * 1000)

            if not cap.isOpened():
                raise CaptureError(f"Failed to open stream: {camera.url}")

            ret, frame = cap.read()
            if not ret or frame is None:
                raise CaptureError("Failed to read frame from stream")

            if settings.resolution_scale and settings.resolution_scale != 1.0:
                width = int(frame.shape[1] * settings.resolution_scale)
                height = int(frame.shape[0] * settings.resolution_scale)
                frame = cv2.resize(frame, (width, height))

            output_path = self._get_output_path(camera.name)
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, settings.jpeg_quality]

            success = cv2.imwrite(str(output_path), frame, encode_params)
            if not success:
                raise CaptureError(f"Failed to write image to {output_path}")

            logger.info(f"Captured frame from '{camera.name}' -> {output_path}")
            return output_path

        finally:
            cap.release()

    def _get_output_path(self, camera_name: str) -> Path:
        """Generate output path for a capture."""
        now = datetime.now()

        camera_dir = self.captures_path / camera_name / now.strftime("%Y-%m")
        camera_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{camera_name}_{now.strftime('%Y-%m-%d_%H-%M-%S')}.jpg"
        return camera_dir / filename

    def test_connection(self, url: str, timeout_seconds: int = 10) -> dict:
        """
        Test connection to an RTSP stream.

        Returns:
            Dict with success status, resolution, fps, and error message
        """
        result = {
            "success": False,
            "width": None,
            "height": None,
            "fps": None,
            "error": None
        }

        cap = cv2.VideoCapture(url)

        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_seconds * 1000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_seconds * 1000)

            if not cap.isOpened():
                result["error"] = "Failed to open stream"
                return result

            ret, frame = cap.read()
            if not ret or frame is None:
                result["error"] = "Failed to read frame"
                return result

            result["success"] = True
            result["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            result["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            result["fps"] = cap.get(cv2.CAP_PROP_FPS)

            return result

        except Exception as e:
            result["error"] = str(e)
            return result

        finally:
            cap.release()

    def get_captures_for_camera(
        self,
        camera_name: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> list[Path]:
        """Get list of capture files for a camera within date range."""
        camera_dir = self.captures_path / camera_name

        if not camera_dir.exists():
            return []

        captures = []
        for img_path in camera_dir.rglob("*.jpg"):
            try:
                filename = img_path.stem
                date_str = "_".join(filename.split("_")[1:])
                capture_time = datetime.strptime(date_str, "%Y-%m-%d_%H-%M-%S")

                if start_date and capture_time < start_date:
                    continue
                if end_date and capture_time > end_date:
                    continue

                captures.append(img_path)
            except ValueError:
                continue

        captures.sort(key=lambda p: p.name)
        return captures

    def get_recent_captures(self, limit: int = 20) -> list[dict]:
        """Get most recent captures across all cameras."""
        all_captures = []

        for camera_dir in self.captures_path.iterdir():
            if not camera_dir.is_dir():
                continue

            camera_name = camera_dir.name

            for img_path in camera_dir.rglob("*.jpg"):
                try:
                    filename = img_path.stem
                    date_str = "_".join(filename.split("_")[1:])
                    capture_time = datetime.strptime(date_str, "%Y-%m-%d_%H-%M-%S")

                    all_captures.append({
                        "camera": camera_name,
                        "path": str(img_path.relative_to(self.captures_path)),
                        "timestamp": capture_time.isoformat(),
                        "filename": img_path.name
                    })
                except ValueError:
                    continue

        all_captures.sort(key=lambda x: x["timestamp"], reverse=True)
        return all_captures[:limit]

    def get_storage_stats(self) -> dict:
        """Get storage statistics for captures."""
        total_size = 0
        total_files = 0
        cameras = {}

        for camera_dir in self.captures_path.iterdir():
            if not camera_dir.is_dir():
                continue

            camera_name = camera_dir.name
            camera_size = 0
            camera_files = 0

            for img_path in camera_dir.rglob("*.jpg"):
                camera_size += img_path.stat().st_size
                camera_files += 1

            cameras[camera_name] = {
                "size_bytes": camera_size,
                "file_count": camera_files
            }
            total_size += camera_size
            total_files += camera_files

        return {
            "total_size_bytes": total_size,
            "total_files": total_files,
            "cameras": cameras
        }
