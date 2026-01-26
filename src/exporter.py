"""Timelapse video generation using FFmpeg."""

import logging
import os
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import ExportPreset, ExportHistory

logger = logging.getLogger(__name__)


class ExportError(Exception):
    """Error during export."""
    pass


class ExportProgress:
    """Tracks export progress."""

    def __init__(self, export_id: str, total_frames: int):
        self.export_id = export_id
        self.total_frames = total_frames
        self.current_frame = 0
        self.status = "pending"
        self.error: Optional[str] = None
        self.output_file: Optional[str] = None

    @property
    def progress_percent(self) -> float:
        if self.total_frames == 0:
            return 0.0
        return min(100.0, (self.current_frame / self.total_frames) * 100)

    def to_dict(self) -> dict:
        return {
            "export_id": self.export_id,
            "total_frames": self.total_frames,
            "current_frame": self.current_frame,
            "progress_percent": self.progress_percent,
            "status": self.status,
            "error": self.error,
            "output_file": self.output_file
        }


class Exporter:
    """Generates timelapse videos from captured images."""

    def __init__(self, captures_path: Path, exports_path: Path):
        self.captures_path = Path(captures_path)
        self.exports_path = Path(exports_path)
        self.exports_path.mkdir(parents=True, exist_ok=True)
        self._active_exports: dict[str, ExportProgress] = {}

    def generate_timelapse(
        self,
        camera: str,
        start_date: datetime,
        end_date: datetime,
        preset: ExportPreset,
        output_name: Optional[str] = None
    ) -> ExportHistory:
        """
        Generate a timelapse video from captures.

        Args:
            camera: Camera name
            start_date: Start date for captures
            end_date: End date for captures
            preset: Export preset configuration
            output_name: Optional custom output filename

        Returns:
            ExportHistory with details of the generated video
        """
        export_id = str(uuid.uuid4())[:8]
        images = self._get_images_in_range(camera, start_date, end_date)

        if not images:
            raise ExportError(f"No images found for {camera} in the specified date range")

        progress = ExportProgress(export_id, len(images))
        self._active_exports[export_id] = progress

        try:
            progress.status = "processing"

            if output_name is None:
                output_name = (
                    f"{camera}_{start_date.strftime('%Y%m%d')}_"
                    f"{end_date.strftime('%Y%m%d')}_{export_id}.mp4"
                )

            output_path = self.exports_path / output_name

            self._run_ffmpeg(images, output_path, preset, progress)

            progress.status = "completed"
            progress.output_file = str(output_path)

            file_size = output_path.stat().st_size
            duration = len(images) / preset.fps

            history = ExportHistory(
                id=export_id,
                camera=camera,
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                preset=preset.name,
                output_file=output_name,
                created_at=datetime.now().isoformat(),
                image_count=len(images),
                duration_seconds=duration,
                file_size_bytes=file_size
            )

            logger.info(
                f"Generated timelapse: {output_name} "
                f"({len(images)} images, {duration:.1f}s, {file_size / 1024 / 1024:.1f}MB)"
            )

            return history

        except Exception as e:
            progress.status = "failed"
            progress.error = str(e)
            logger.error(f"Export failed: {e}")
            raise ExportError(str(e))

        finally:
            if export_id in self._active_exports:
                del self._active_exports[export_id]

    def _get_images_in_range(
        self,
        camera: str,
        start_date: datetime,
        end_date: datetime
    ) -> list[Path]:
        """Get sorted list of images within date range."""
        camera_dir = self.captures_path / camera

        if not camera_dir.exists():
            return []

        images = []

        for img_path in camera_dir.rglob("*.jpg"):
            try:
                filename = img_path.stem
                date_str = "_".join(filename.split("_")[1:])
                capture_time = datetime.strptime(date_str, "%Y-%m-%d_%H-%M-%S")

                if start_date <= capture_time <= end_date:
                    images.append(img_path)
            except ValueError:
                continue

        images.sort(key=lambda p: p.name)
        return images

    def _run_ffmpeg(
        self,
        images: list[Path],
        output_path: Path,
        preset: ExportPreset,
        progress: ExportProgress
    ) -> None:
        """Run FFmpeg to generate the timelapse."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            for img in images:
                escaped_path = str(img).replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")
            file_list_path = f.name

        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", file_list_path,
                "-framerate", str(preset.fps),
                "-c:v", preset.codec,
                "-pix_fmt", preset.pixel_format,
                "-preset", preset.ffmpeg_preset,
            ]

            if preset.width and preset.height:
                cmd.extend(["-vf", f"scale={preset.width}:{preset.height}"])

            # Enable faststart for web streaming (moves moov atom to beginning)
            cmd.extend(["-movflags", "+faststart"])

            cmd.append(str(output_path))

            logger.debug(f"Running FFmpeg: {' '.join(cmd)}")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )

            _, stderr = process.communicate()

            if process.returncode != 0:
                raise ExportError(f"FFmpeg failed: {stderr}")

            progress.current_frame = len(images)

        finally:
            os.unlink(file_list_path)

    def get_export_progress(self, export_id: str) -> Optional[dict]:
        """Get progress of an active export."""
        if export_id in self._active_exports:
            return self._active_exports[export_id].to_dict()
        return None

    def calculate_export_info(
        self,
        camera: str,
        start_date: datetime,
        end_date: datetime,
        fps: int
    ) -> dict:
        """Calculate export statistics without generating."""
        images = self._get_images_in_range(camera, start_date, end_date)

        image_count = len(images)
        duration_seconds = image_count / fps if fps > 0 else 0
        estimated_size_mb = image_count * 0.05

        return {
            "image_count": image_count,
            "duration_seconds": duration_seconds,
            "duration_formatted": self._format_duration(duration_seconds),
            "estimated_size_mb": estimated_size_mb,
            "fps": fps
        }

    def _format_duration(self, seconds: float) -> str:
        """Format duration as human-readable string."""
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"

    def get_exports_storage_stats(self) -> dict:
        """Get storage statistics for exports."""
        total_size = 0
        total_files = 0

        for export_file in self.exports_path.glob("*.mp4"):
            total_size += export_file.stat().st_size
            total_files += 1

        return {
            "total_size_bytes": total_size,
            "total_files": total_files
        }

    def list_exports(self) -> list[dict]:
        """List all export files."""
        exports = []

        for export_file in self.exports_path.glob("*.mp4"):
            stat = export_file.stat()
            exports.append({
                "filename": export_file.name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat()
            })

        exports.sort(key=lambda x: x["created_at"], reverse=True)
        return exports

    def delete_export(self, filename: str) -> bool:
        """Delete an export file."""
        export_path = self.exports_path / filename

        if not export_path.exists():
            return False

        if not str(export_path.resolve()).startswith(str(self.exports_path.resolve())):
            raise ExportError("Invalid export path")

        export_path.unlink()
        logger.info(f"Deleted export: {filename}")
        return True
