"""RTSP capture module for capturing frames from camera streams."""

import logging
import shutil
import threading
import time
from datetime import datetime, timedelta
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

    def _capture_with_timeout(self, target, timeout_seconds: float):
        """Run target() in a daemon thread with a hard timeout.

        Returns the result of target(). Raises CaptureError on timeout or if
        target raises an exception.
        """
        outcome = []  # [result] on success, [None, exc] on failure

        def runner():
            try:
                outcome.append(target())
            except Exception as e:
                outcome.append(None)
                outcome.append(e)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout=timeout_seconds)

        if t.is_alive():
            raise CaptureError("Capture timed out")

        if len(outcome) == 2:
            raise outcome[1]

        return outcome[0]

    def _do_capture(
        self,
        camera: CameraConfig,
        settings: CaptureSettings
    ) -> Path:
        """Execute a single capture attempt."""
        free = shutil.disk_usage(self.captures_path).free
        if free < 100 * 1024 * 1024:  # 100MB minimum
            raise CaptureError(f"Insufficient disk space: {free / 1024 / 1024:.0f}MB free")

        def _capture():
            cap = cv2.VideoCapture(camera.url)
            try:
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, settings.timeout_seconds * 1000)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, settings.timeout_seconds * 1000)

                if not cap.isOpened():
                    raise CaptureError(f"Failed to open stream: {camera.url}")

                ret, frame = cap.read()
                if not ret or frame is None:
                    raise CaptureError("Failed to read frame from stream")

                output_path = self._get_output_path(camera.name)
                encode_params = [cv2.IMWRITE_JPEG_QUALITY, settings.jpeg_quality]

                success = cv2.imwrite(str(output_path), frame, encode_params)
                if not success:
                    raise CaptureError(f"Failed to write image to {output_path}")

                logger.info(f"Captured frame from '{camera.name}' -> {output_path}")
                return output_path
            finally:
                cap.release()

        return self._capture_with_timeout(_capture, settings.timeout_seconds + 5)

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

        def _test():
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

        try:
            return self._capture_with_timeout(_test, timeout_seconds + 5)
        except CaptureError as e:
            result["error"] = str(e)
            return result

    def cleanup_old_captures(self, max_age_days: int = 30) -> dict:
        """Delete capture files older than max_age_days and remove empty month dirs."""
        cutoff = datetime.now() - timedelta(days=max_age_days)
        deleted_files = 0
        freed_bytes = 0

        for camera_dir in self.captures_path.iterdir():
            if not camera_dir.is_dir():
                continue

            for month_dir in sorted(camera_dir.iterdir()):
                if not month_dir.is_dir():
                    continue

                for img_path in list(month_dir.iterdir()):
                    if img_path.suffix != ".jpg":
                        continue
                    try:
                        filename = img_path.stem
                        date_str = "_".join(filename.split("_")[1:])
                        capture_time = datetime.strptime(date_str, "%Y-%m-%d_%H-%M-%S")
                    except ValueError:
                        continue

                    if capture_time < cutoff:
                        size = img_path.stat().st_size
                        logger.info(f"Deleting old capture: {img_path}")
                        img_path.unlink()
                        deleted_files += 1
                        freed_bytes += size

                # Remove empty month directory
                try:
                    month_dir.rmdir()
                    logger.debug(f"Removed empty directory: {month_dir}")
                except OSError:
                    pass  # Not empty, leave it

        logger.info(
            f"Cleanup complete: deleted {deleted_files} files, "
            f"freed {freed_bytes / 1024 / 1024:.1f}MB"
        )
        return {"deleted_files": deleted_files, "freed_bytes": freed_bytes}

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

    def get_capture_date_counts(self, camera_name: str) -> dict[str, int]:
        """Return a map of 'YYYY-MM-DD' -> number of captures for a camera.

        Dates are parsed from filenames (no files are opened), so this stays
        cheap even for large libraries. Used by the export date picker to show
        which days have photos available.
        """
        camera_dir = self.captures_path / camera_name

        if not camera_dir.exists():
            return {}

        counts: dict[str, int] = {}
        for img_path in camera_dir.rglob("*.jpg"):
            filename = img_path.stem
            # Filenames look like "<camera>_YYYY-MM-DD_HH-MM-SS"; the date is the
            # first dash-delimited token after the camera-name prefix.
            parts = filename.split("_")
            if len(parts) < 3:
                continue
            day = parts[-2]
            if len(day) == 10 and day[4] == "-" and day[7] == "-":
                counts[day] = counts.get(day, 0) + 1

        return counts

    def get_recent_captures(self, limit: int = 20) -> list[dict]:
        """Get most recent captures across all cameras."""
        # Collect up to `limit` most-recent files per camera by scanning month
        # directories newest-first, stopping early once we have enough.
        per_camera: list[dict] = []

        for camera_dir in self.captures_path.iterdir():
            if not camera_dir.is_dir():
                continue

            camera_name = camera_dir.name
            camera_captures: list[dict] = []

            try:
                month_dirs = sorted(
                    (d for d in camera_dir.iterdir() if d.is_dir()),
                    reverse=True
                )
            except OSError:
                continue

            for month_dir in month_dirs:
                try:
                    jpg_files = sorted(
                        (f for f in month_dir.iterdir() if f.suffix == ".jpg"),
                        key=lambda p: p.name,
                        reverse=True
                    )
                except OSError:
                    continue

                for img_path in jpg_files:
                    try:
                        filename = img_path.stem
                        date_str = "_".join(filename.split("_")[1:])
                        capture_time = datetime.strptime(date_str, "%Y-%m-%d_%H-%M-%S")
                    except ValueError:
                        continue

                    camera_captures.append({
                        "camera": camera_name,
                        "path": str(img_path.relative_to(self.captures_path)),
                        "timestamp": capture_time.isoformat() + 'Z',
                        "filename": img_path.name
                    })

                    if len(camera_captures) >= limit:
                        break

                if len(camera_captures) >= limit:
                    break

            per_camera.extend(camera_captures)

        per_camera.sort(key=lambda x: x["timestamp"], reverse=True)
        return per_camera[:limit]

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
