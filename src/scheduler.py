"""Schedule management for RTSP captures."""

import logging
from datetime import datetime, time, timedelta
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .models import CameraConfig, FrequencyType, Schedule, TimeWindow

logger = logging.getLogger(__name__)


class ScheduleManager:
    """Manages capture schedules using APScheduler."""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self._capture_callback: Optional[Callable[[CameraConfig], None]] = None
        self._cameras: dict[str, CameraConfig] = {}
        self._job_ids: dict[str, list[str]] = {}  # camera_name -> list of job IDs

    def set_capture_callback(self, callback: Callable[[CameraConfig], None]) -> None:
        """Set the callback function for capture jobs."""
        self._capture_callback = callback

    def start(self) -> None:
        """Start the scheduler."""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler started")

    def stop(self) -> None:
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
            logger.info("Scheduler stopped")

    def load_cameras(self, cameras: dict[str, CameraConfig]) -> None:
        """Load cameras and their schedules."""
        self._cameras = cameras

        for camera_name, camera in cameras.items():
            if camera.enabled:
                self._add_camera_schedules(camera)

    def _add_camera_schedules(self, camera: CameraConfig) -> None:
        """Add all schedules for a camera."""
        self._job_ids[camera.name] = []

        for schedule in camera.schedules:
            if not schedule.enabled:
                continue

            job_ids = self._create_schedule_jobs(camera, schedule)
            self._job_ids[camera.name].extend(job_ids)

    def _create_schedule_jobs(
        self,
        camera: CameraConfig,
        schedule: Schedule
    ) -> list[str]:
        """Create APScheduler jobs for a schedule."""
        job_ids = []

        if schedule.frequency == FrequencyType.HOURLY:
            job_id = self._create_hourly_job(camera, schedule)
            if job_id:
                job_ids.append(job_id)

        elif schedule.frequency == FrequencyType.INTERVAL:
            job_id = self._create_interval_job(camera, schedule)
            if job_id:
                job_ids.append(job_id)

        elif schedule.frequency == FrequencyType.X_PER_DAY:
            job_ids.extend(self._create_x_per_day_jobs(camera, schedule))

        return job_ids

    def _create_hourly_job(
        self,
        camera: CameraConfig,
        schedule: Schedule
    ) -> Optional[str]:
        """Create hourly capture job."""
        job_id = f"{camera.name}_{schedule.name}_hourly"

        trigger_kwargs = {"minute": 0}

        if schedule.time_window:
            trigger_kwargs["hour"] = self._get_hour_range(schedule.time_window)

        trigger = CronTrigger(**trigger_kwargs)

        self.scheduler.add_job(
            self._execute_capture,
            trigger=trigger,
            id=job_id,
            args=[camera],
            replace_existing=True
        )

        logger.info(f"Added hourly job for {camera.name}: {job_id}")
        return job_id

    def _create_interval_job(
        self,
        camera: CameraConfig,
        schedule: Schedule
    ) -> Optional[str]:
        """Create interval-based capture job."""
        job_id = f"{camera.name}_{schedule.name}_interval"

        trigger = IntervalTrigger(hours=schedule.value)

        self.scheduler.add_job(
            self._execute_capture_with_window_check,
            trigger=trigger,
            id=job_id,
            args=[camera, schedule.time_window],
            replace_existing=True
        )

        logger.info(f"Added interval job ({schedule.value}h) for {camera.name}: {job_id}")
        return job_id

    def _create_x_per_day_jobs(
        self,
        camera: CameraConfig,
        schedule: Schedule
    ) -> list[str]:
        """Create X captures per day distributed across time window."""
        job_ids = []
        times = self._calculate_distributed_times(schedule.value, schedule.time_window)

        for i, capture_time in enumerate(times):
            job_id = f"{camera.name}_{schedule.name}_daily_{i}"

            trigger = CronTrigger(
                hour=capture_time.hour,
                minute=capture_time.minute
            )

            self.scheduler.add_job(
                self._execute_capture,
                trigger=trigger,
                id=job_id,
                args=[camera],
                replace_existing=True
            )

            job_ids.append(job_id)
            logger.info(f"Added daily job at {capture_time} for {camera.name}: {job_id}")

        return job_ids

    def _calculate_distributed_times(
        self,
        count: int,
        time_window: Optional[TimeWindow]
    ) -> list[time]:
        """Calculate evenly distributed times across a window."""
        if time_window:
            start_minutes = time_window.start.hour * 60 + time_window.start.minute
            end_minutes = time_window.end.hour * 60 + time_window.end.minute
        else:
            start_minutes = 0
            end_minutes = 24 * 60 - 1

        if end_minutes <= start_minutes:
            end_minutes += 24 * 60

        total_minutes = end_minutes - start_minutes

        if count <= 1:
            mid = (start_minutes + end_minutes) // 2
            return [time(hour=(mid // 60) % 24, minute=mid % 60)]

        interval = total_minutes / (count - 1) if count > 1 else total_minutes

        times = []
        for i in range(count):
            minutes = int(start_minutes + i * interval)
            hour = (minutes // 60) % 24
            minute = minutes % 60
            times.append(time(hour=hour, minute=minute))

        return times

    def _get_hour_range(self, time_window: TimeWindow) -> str:
        """Convert time window to cron hour range."""
        start_hour = time_window.start.hour
        end_hour = time_window.end.hour

        if end_hour < start_hour:
            return f"{start_hour}-23,0-{end_hour}"

        return f"{start_hour}-{end_hour}"

    def _execute_capture(self, camera: CameraConfig) -> None:
        """Execute capture for a camera."""
        if self._capture_callback:
            logger.debug(f"Executing scheduled capture for {camera.name}")
            try:
                self._capture_callback(camera)
            except Exception as e:
                logger.error(f"Capture failed for {camera.name}: {e}")

    def _execute_capture_with_window_check(
        self,
        camera: CameraConfig,
        time_window: Optional[TimeWindow]
    ) -> None:
        """Execute capture with time window validation."""
        if time_window and not self._is_within_window(time_window):
            logger.debug(
                f"Skipping capture for {camera.name}: outside time window"
            )
            return

        self._execute_capture(camera)

    def _is_within_window(self, time_window: TimeWindow) -> bool:
        """Check if current time is within the time window."""
        now = datetime.now().time()
        start = time_window.start
        end = time_window.end

        if start <= end:
            return start <= now <= end
        else:
            return now >= start or now <= end

    def remove_camera(self, camera_name: str) -> None:
        """Remove all schedules for a camera."""
        if camera_name in self._job_ids:
            for job_id in self._job_ids[camera_name]:
                try:
                    self.scheduler.remove_job(job_id)
                    logger.info(f"Removed job: {job_id}")
                except Exception:
                    pass
            del self._job_ids[camera_name]

        if camera_name in self._cameras:
            del self._cameras[camera_name]

    def update_camera(self, camera: CameraConfig) -> None:
        """Update schedules for a camera."""
        self.remove_camera(camera.name)
        self._cameras[camera.name] = camera
        if camera.enabled:
            self._add_camera_schedules(camera)

    def pause_camera(self, camera_name: str) -> None:
        """Pause all jobs for a camera."""
        if camera_name in self._job_ids:
            for job_id in self._job_ids[camera_name]:
                try:
                    self.scheduler.pause_job(job_id)
                except Exception:
                    pass
            logger.info(f"Paused all jobs for {camera_name}")

    def resume_camera(self, camera_name: str) -> None:
        """Resume all jobs for a camera."""
        if camera_name in self._job_ids:
            for job_id in self._job_ids[camera_name]:
                try:
                    self.scheduler.resume_job(job_id)
                except Exception:
                    pass
            logger.info(f"Resumed all jobs for {camera_name}")

    def get_next_run_times(self) -> dict[str, list[dict]]:
        """Get next run times for all cameras."""
        result = {}

        for camera_name, job_ids in self._job_ids.items():
            result[camera_name] = []
            for job_id in job_ids:
                try:
                    job = self.scheduler.get_job(job_id)
                    if job and job.next_run_time:
                        result[camera_name].append({
                            "job_id": job_id,
                            "next_run": job.next_run_time.isoformat()
                        })
                except Exception:
                    pass

        return result

    def get_all_jobs(self) -> list[dict]:
        """Get information about all scheduled jobs."""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger)
            })
        return jobs
