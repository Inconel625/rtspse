"""Timelapse video generation using FFmpeg."""

import logging
import os
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .models import ExportPreset, ExportHistory, LocationConfig

logger = logging.getLogger(__name__)

# Target-bitrate model, shared by the encoder and the size estimator so that the
# estimate matches the actual output. Timelapses have low frame-to-frame
# correlation, so they need a higher bits-per-pixel than normal video.
# bitrate = clamp(BITRATE_BPP * width * height * fps, MIN, MAX). Tunable.
BITRATE_BPP = 0.12
BITRATE_MIN = 4_000_000
BITRATE_MAX = 20_000_000

# VAAPI render node used when hardware acceleration is enabled.
VAAPI_DEVICE = "/dev/dri/renderD128"

DEFAULT_DIMENSIONS = (1920, 1080)


def target_bitrate(width: int, height: int, fps: int, factor: float = 1.0) -> int:
    """Compute the capped target video bitrate (bits/s) for given output settings."""
    raw = BITRATE_BPP * width * height * max(1, fps) * factor
    return int(max(BITRATE_MIN, min(BITRATE_MAX, raw)))


class ExportError(Exception):
    """Error during export."""
    pass


class ExportProgress:
    """Tracks export progress."""

    def __init__(self, export_id: str, total_frames: int):
        import time as time_mod
        self.export_id = export_id
        self.total_frames = total_frames
        self.current_frame = 0
        self.status = "pending"
        self.error: Optional[str] = None
        self.output_file: Optional[str] = None
        self.cancelled = False
        self.started_at: float = time_mod.time()
        self.completed_at: Optional[float] = None

    @property
    def progress_percent(self) -> float:
        if self.total_frames == 0:
            return 0.0
        return min(100.0, (self.current_frame / self.total_frames) * 100)

    @property
    def eta_seconds(self) -> Optional[float]:
        """Estimated seconds remaining, based on the encode rate so far."""
        import time as time_mod
        if self.status != "processing" or self.current_frame <= 1:
            return None
        elapsed = time_mod.time() - self.started_at
        # Ignore the first couple of seconds: ffmpeg startup makes the early rate
        # wildly inaccurate (a single frame would project a huge ETA).
        if elapsed < 2.0:
            return None
        rate = self.current_frame / elapsed  # frames per second
        remaining = max(0, self.total_frames - self.current_frame)
        return remaining / rate if rate > 0 else None

    def to_dict(self) -> dict:
        return {
            "export_id": self.export_id,
            "total_frames": self.total_frames,
            "current_frame": self.current_frame,
            "progress_percent": self.progress_percent,
            "status": self.status,
            "error": self.error,
            "output_file": self.output_file,
            "cancelled": self.cancelled,
            "eta_seconds": self.eta_seconds
        }


class Exporter:
    """Generates timelapse videos from captured images."""

    def __init__(self, captures_path: Path, exports_path: Path, max_export_seconds: int = 7200,
                 hwaccel: str = "none"):
        self.captures_path = Path(captures_path)
        self.exports_path = Path(exports_path)
        self.exports_path.mkdir(parents=True, exist_ok=True)
        self._active_exports: dict[str, ExportProgress] = {}
        self._lock = threading.Lock()
        self.max_export_seconds = max_export_seconds
        self.hwaccel = hwaccel

    def _vaapi_available(self) -> bool:
        """True if VAAPI hardware encoding is enabled and the render node exists."""
        return self.hwaccel == "auto" and os.path.exists(VAAPI_DEVICE)

    def _probe_dimensions(self, image_path: Path) -> tuple[int, int]:
        """Return (width, height) of an image via ffprobe, with a safe fallback."""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
                 str(image_path)],
                capture_output=True, text=True, timeout=10
            )
            w, h = out.stdout.strip().split("x")
            return int(w), int(h)
        except Exception as e:
            logger.warning(f"Could not probe dimensions for {image_path}: {e}")
            return DEFAULT_DIMENSIONS

    def _output_dimensions(self, preset: ExportPreset, sample_image: Optional[Path]) -> tuple[int, int]:
        """Resolve the output resolution: explicit preset scaling, else source dims."""
        if preset.width and preset.height:
            return preset.width, preset.height
        if sample_image is not None:
            return self._probe_dimensions(sample_image)
        return DEFAULT_DIMENSIONS

    def _build_video_args(self, preset: ExportPreset, bitrate: int, smoothing: str = "none") -> list[str]:
        """Build the codec/rate-control/filter args, preferring hardware encode.

        Falls back to libx264 (software) when hardware acceleration is unavailable.
        """
        gop = str(max(1, round(preset.fps * 2)))
        maxrate = bitrate
        bufsize = bitrate * 2

        # Software video filters, applied in order before any GPU upload.
        filters = []
        if preset.width and preset.height:
            filters.append(f"scale={preset.width}:{preset.height}")
        if smoothing == "blend":
            # 2-frame rolling average: cross-dissolves adjacent frames to soften the
            # timelapse strobe. Preserves the output frame count (progress stays exact).
            filters.append("tmix=frames=2")

        if self._vaapi_available():
            # VAAPI path: software filters first, then upload frames to the GPU.
            vf = ",".join(filters + ["format=nv12", "hwupload"])
            return [
                "-vaapi_device", VAAPI_DEVICE,
                "-vf", vf,
                "-c:v", "h264_vaapi",
                "-b:v", str(bitrate), "-maxrate", str(maxrate), "-bufsize", str(bufsize),
                "-g", gop,
            ]

        # Software path (libx264) with capped bitrate.
        args = []
        if filters:
            args += ["-vf", ",".join(filters)]
        args += [
            "-c:v", preset.codec,
            "-pix_fmt", preset.pixel_format,
            "-preset", preset.ffmpeg_preset,
            "-b:v", str(bitrate), "-maxrate", str(maxrate), "-bufsize", str(bufsize),
            "-g", gop,
        ]
        return args

    def _cleanup_stale_exports(self) -> None:
        """Remove completed/failed exports older than 5 minutes. Must be called with self._lock held."""
        import time as time_mod
        now = time_mod.time()
        stale_ids = [
            eid for eid, p in self._active_exports.items()
            if p.completed_at is not None and (now - p.completed_at) > 300
        ]
        for eid in stale_ids:
            del self._active_exports[eid]

    def cancel_export(self, export_id: str) -> bool:
        """Cancel an active export by setting the cancelled flag."""
        with self._lock:
            progress = self._active_exports.get(export_id)
            if progress and progress.status == "processing":
                progress.cancelled = True
                return True
        return False

    def _get_dawn_dusk_for_date(self, location: LocationConfig, date) -> Optional[tuple[time, time]]:
        """Compute dawn and dusk times for a specific date and location."""
        try:
            from astral import LocationInfo
            from astral.sun import sun

            tz_str = location.timezone or "UTC"
            loc = LocationInfo(
                name="configured",
                region="",
                timezone=tz_str,
                latitude=location.latitude,
                longitude=location.longitude
            )
            s = sun(loc.observer, date=date, tzinfo=ZoneInfo(tz_str))
            return (s["dawn"].time().replace(tzinfo=None), s["dusk"].time().replace(tzinfo=None))
        except Exception as e:
            logger.warning(f"Failed to compute dawn/dusk for {date}: {e}")
            return None

    def generate_timelapse(
        self,
        camera: str,
        start_date: datetime,
        end_date: datetime,
        preset: ExportPreset,
        output_name: Optional[str] = None,
        start_time: Optional[time] = None,
        end_time: Optional[time] = None,
        use_dawn_dusk: bool = False,
        location: Optional[LocationConfig] = None,
        export_id: Optional[str] = None,
        smoothing: str = "none"
    ) -> ExportHistory:
        """
        Generate a timelapse video from captures.

        Args:
            camera: Camera name
            start_date: Start date for captures
            end_date: End date for captures
            preset: Export preset configuration
            output_name: Optional custom output filename
            start_time: Optional daily start time filter
            end_time: Optional daily end time filter

        Returns:
            ExportHistory with details of the generated video
        """
        import time as time_mod

        if export_id is None:
            export_id = str(uuid.uuid4())[:8]
        images = self._get_images_in_range(camera, start_date, end_date, start_time, end_time,
                                           use_dawn_dusk=use_dawn_dusk, location=location)

        if not images:
            raise ExportError(f"No images found for {camera} in the specified date range")

        progress = ExportProgress(export_id, len(images))
        with self._lock:
            self._active_exports[export_id] = progress

        try:
            progress.status = "processing"

            if output_name is None:
                output_name = (
                    f"{camera}_{start_date.strftime('%Y%m%d')}_"
                    f"{end_date.strftime('%Y%m%d')}_{export_id}.mp4"
                )

            output_path = self.exports_path / output_name

            self._run_ffmpeg(images, output_path, preset, progress, smoothing=smoothing)

            if progress.cancelled:
                progress.status = "cancelled"
                if output_path.exists():
                    output_path.unlink()
                raise ExportError("Export was cancelled")

            progress.status = "completed"
            progress.output_file = str(output_path)

            file_size = output_path.stat().st_size
            duration = len(images) / preset.fps

            history = ExportHistory(
                id=export_id,
                camera=camera,
                start_date=start_date.date(),
                end_date=end_date.date(),
                preset=preset.name,
                output_file=output_name,
                created_at=datetime.now(),
                image_count=len(images),
                duration_seconds=duration,
                file_size_bytes=file_size,
                start_time="dawn/dusk" if use_dawn_dusk else (start_time.strftime("%H:%M") if start_time else None),
                end_time="dawn/dusk" if use_dawn_dusk else (end_time.strftime("%H:%M") if end_time else None)
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
            progress.completed_at = time_mod.time()

    def _get_images_in_range(
        self,
        camera: str,
        start_date: datetime,
        end_date: datetime,
        start_time: Optional[time] = None,
        end_time: Optional[time] = None,
        use_dawn_dusk: bool = False,
        location: Optional[LocationConfig] = None
    ) -> list[Path]:
        """Get sorted list of images within date range and optional time-of-day filter."""
        camera_dir = self.captures_path / camera

        if not camera_dir.exists():
            return []

        # Pre-compute dawn/dusk cache per date if needed
        dawn_dusk_cache: dict = {}
        if use_dawn_dusk and location:
            from datetime import timedelta
            d = start_date.date()
            end_d = end_date.date()
            while d <= end_d:
                dawn_dusk_cache[d] = self._get_dawn_dusk_for_date(location, d)
                d += timedelta(days=1)

        images = []

        for img_path in camera_dir.rglob("*.jpg"):
            try:
                filename = img_path.stem
                date_str = "_".join(filename.split("_")[1:])
                capture_time = datetime.strptime(date_str, "%Y-%m-%d_%H-%M-%S")

                if start_date <= capture_time <= end_date:
                    t = capture_time.time()

                    if use_dawn_dusk and location:
                        dd = dawn_dusk_cache.get(capture_time.date())
                        if dd:
                            dawn, dusk = dd
                            if not (dawn <= t <= dusk):
                                continue
                    elif start_time and end_time:
                        if start_time <= end_time:
                            if not (start_time <= t <= end_time):
                                continue
                        else:
                            if not (t >= start_time or t <= end_time):
                                continue

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
        progress: ExportProgress,
        timeout: Optional[int] = None,
        smoothing: str = "none"
    ) -> None:
        """Run FFmpeg to generate the timelapse with real-time progress tracking."""
        import time as time_mod

        effective_timeout = timeout if timeout is not None else self.max_export_seconds

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            for img in images:
                escaped_path = str(img).replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")
            file_list_path = f.name

        # Output resolution drives the target bitrate (probe source when not scaling).
        out_w, out_h = self._output_dimensions(preset, images[0] if images else None)
        bitrate = target_bitrate(out_w, out_h, preset.fps, preset.bitrate_factor)

        process = None
        try:
            cmd = [
                "ffmpeg",
                "-y",
                # -r as an INPUT option (before -i) sets the read rate for the concat
                # demuxer, giving exactly one frame per image at the requested fps.
                # (The concat demuxer rejects -framerate, and -r placed after -i adds a
                # spurious trailing frame; it also previously defaulted the input to 25.)
                "-r", str(preset.fps),
                "-f", "concat",
                "-safe", "0",
                "-i", file_list_path,
            ]

            cmd.extend(self._build_video_args(preset, bitrate, smoothing))

            # Enable faststart for web streaming (moves moov atom to beginning)
            cmd.extend(["-movflags", "+faststart"])

            cmd.append(str(output_path))

            logger.debug(f"Running FFmpeg: {' '.join(cmd)}")

            frame_re = re.compile(r"frame=\s*(\d+)")
            start_time_ts = time_mod.time()

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )

            for line in process.stderr:
                m = frame_re.search(line)
                if m:
                    progress.current_frame = int(m.group(1))

                if progress.cancelled:
                    process.terminate()
                    process.wait(timeout=5)
                    return

                if effective_timeout is not None:
                    elapsed = time_mod.time() - start_time_ts
                    if elapsed > effective_timeout:
                        process.terminate()
                        process.wait(timeout=5)
                        raise ExportError(
                            f"FFmpeg timed out after {int(elapsed)} seconds "
                            f"(limit: {effective_timeout}s)"
                        )

            process.wait()

            if process.returncode != 0:
                raise ExportError(f"FFmpeg failed with return code {process.returncode}")

            progress.current_frame = len(images)

        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

            os.unlink(file_list_path)

    def get_export_progress(self, export_id: str) -> Optional[dict]:
        """Get progress of an active export."""
        with self._lock:
            self._cleanup_stale_exports()
            progress = self._active_exports.get(export_id)
            if progress is not None:
                return progress.to_dict()
        return None

    def calculate_export_info(
        self,
        camera: str,
        start_date: datetime,
        end_date: datetime,
        fps: int,
        start_time: Optional[time] = None,
        end_time: Optional[time] = None,
        use_dawn_dusk: bool = False,
        location: Optional[LocationConfig] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        bitrate_factor: float = 1.0
    ) -> dict:
        """Calculate export statistics without generating."""
        images = self._get_images_in_range(camera, start_date, end_date, start_time, end_time,
                                           use_dawn_dusk=use_dawn_dusk, location=location)

        image_count = len(images)
        duration_seconds = image_count / fps if fps > 0 else 0

        # Estimate size from the same capped-bitrate model the encoder uses, so the
        # estimate tracks the actual output: size = bitrate * duration / 8.
        if image_count and fps > 0:
            if width and height:
                out_w, out_h = width, height
            else:
                out_w, out_h = self._probe_dimensions(images[0])
            bitrate = target_bitrate(out_w, out_h, fps, bitrate_factor)
            estimated_size_mb = (bitrate * duration_seconds / 8) / (1024 * 1024)
        else:
            estimated_size_mb = 0.0

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
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z"
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
