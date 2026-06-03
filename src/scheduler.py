"""Schedule management for RTSP captures."""

import logging
import threading
from datetime import datetime, time, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .models import CameraConfig, FrequencyType, LocationConfig, Schedule, TimeWindow

logger = logging.getLogger(__name__)


class ScheduleManager:
    """Manages capture schedules using APScheduler."""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self._capture_callback: Optional[Callable[[CameraConfig], None]] = None
        self._cameras: dict[str, CameraConfig] = {}
        self._job_ids: dict[str, list[str]] = {}  # camera_name -> list of job IDs
        self._location: Optional[LocationConfig] = None
        self._lock = threading.Lock()

    def set_location(self, location: Optional[LocationConfig]) -> None:
        """Set the location for dawn/dusk calculations."""
        self._location = location

    def set_capture_callback(self, callback: Callable[[CameraConfig], None]) -> None:
        """Set the callback function for capture jobs."""
        self._capture_callback = callback

    def start(self) -> None:
        """Start the scheduler."""
        if not self.scheduler.running:
            try:
                self.scheduler.start()
                logger.info("Scheduler started")
            except Exception as e:
                logger.error(f"Failed to start scheduler: {e}")
                raise

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
        """Add all schedules for a camera. Caller must hold self._lock."""
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

        # Always fire at :00 of every hour and let _execute_capture_with_window_check
        # enforce the exact window boundaries at runtime.  Using _get_hour_range in the
        # cron trigger would discard sub-hour minute boundaries (e.g. 06:30 treated as
        # 06:00), and mixing trigger-level and runtime filtering is confusing.
        trigger = CronTrigger(minute=0)
        self.scheduler.add_job(
            self._execute_capture_with_window_check,
            trigger=trigger,
            id=job_id,
            args=[camera, schedule.time_window],
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

        unit = schedule.interval_unit
        trigger_kwargs = {unit: schedule.value}
        trigger = IntervalTrigger(**trigger_kwargs)

        self.scheduler.add_job(
            self._execute_capture_with_window_check,
            trigger=trigger,
            id=job_id,
            args=[camera, schedule.time_window],
            replace_existing=True
        )

        logger.info(f"Added interval job ({schedule.value} {unit}) for {camera.name}: {job_id}")
        return job_id

    def _create_x_per_day_jobs(
        self,
        camera: CameraConfig,
        schedule: Schedule
    ) -> list[str]:
        """Create X captures per day distributed across time window."""
        job_ids = []

        if schedule.time_window and schedule.time_window.use_dawn_dusk:
            # Use a daily recalculation job that schedules captures dynamically
            recalc_job_id = f"{camera.name}_{schedule.name}_dawn_dusk_recalc"
            trigger = CronTrigger(hour=0, minute=1)
            self.scheduler.add_job(
                self._recalculate_dawn_dusk_jobs,
                trigger=trigger,
                id=recalc_job_id,
                args=[camera, schedule],
                replace_existing=True
            )
            job_ids.append(recalc_job_id)
            # Also run immediately to set up today's jobs
            self._recalculate_dawn_dusk_jobs(camera, schedule)
            logger.info(f"Added dawn/dusk recalc job for {camera.name}: {recalc_job_id}")
        else:
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

    def _now_local_time(self) -> time:
        """Get current time in the configured timezone."""
        if self._location and self._location.timezone:
            return datetime.now(ZoneInfo(self._location.timezone)).time()
        return datetime.now().time()

    def _is_within_window(self, time_window: TimeWindow) -> bool:
        """Check if current time is within the time window."""
        now = self._now_local_time()

        if time_window.use_dawn_dusk:
            dawn_dusk = self._get_dawn_dusk_window()
            if dawn_dusk:
                start, end = dawn_dusk
            else:
                start = time_window.start
                end = time_window.end
        else:
            start = time_window.start
            end = time_window.end

        if start <= end:
            return start <= now <= end
        else:
            return now >= start or now <= end

    def _get_dawn_dusk_window(self) -> Optional[tuple[time, time]]:
        """Compute today's dawn and dusk times using astral."""
        if not self._location:
            logger.warning("Dawn/dusk requested but no location configured")
            return None
        try:
            from astral import LocationInfo
            from astral.sun import sun

            tz_str = self._location.timezone or "UTC"
            loc = LocationInfo(
                name="configured",
                region="",
                timezone=tz_str,
                latitude=self._location.latitude,
                longitude=self._location.longitude
            )
            s = sun(loc.observer, date=datetime.now().date(), tzinfo=ZoneInfo(tz_str))
            dawn = s["dawn"].time().replace(tzinfo=None)
            dusk = s["dusk"].time().replace(tzinfo=None)
            logger.debug(f"Computed dawn={dawn}, dusk={dusk}")
            return (dawn, dusk)
        except Exception as e:
            logger.error(f"Failed to compute dawn/dusk: {e}")
            return None

    def _recalculate_dawn_dusk_jobs(self, camera: CameraConfig, schedule: Schedule) -> None:
        """Recalculate distributed capture times based on today's dawn/dusk."""
        prefix = f"{camera.name}_{schedule.name}_dd_"

        # Remove old dynamic jobs for this schedule from both the scheduler and
        # _job_ids so that remove_camera / update_camera stay consistent.
        for job in self.scheduler.get_jobs():
            if job.id.startswith(prefix):
                try:
                    self.scheduler.remove_job(job.id)
                except Exception as e:
                    logger.warning(f"Failed to remove job {job.id}: {e}")
        with self._lock:
            if camera.name in self._job_ids:
                self._job_ids[camera.name] = [
                    jid for jid in self._job_ids[camera.name]
                    if not jid.startswith(prefix)
                ]

        dawn_dusk = self._get_dawn_dusk_window()
        if dawn_dusk:
            tw = TimeWindow(start=dawn_dusk[0], end=dawn_dusk[1])
        elif schedule.time_window:
            tw = schedule.time_window
        else:
            tw = None

        times = self._calculate_distributed_times(schedule.value, tw)
        now = self._now_local_time()

        for i, capture_time in enumerate(times):
            if capture_time < now:
                continue  # Skip times that already passed today
            job_id = f"{prefix}{i}"
            trigger = CronTrigger(hour=capture_time.hour, minute=capture_time.minute)
            self.scheduler.add_job(
                self._execute_capture,
                trigger=trigger,
                id=job_id,
                args=[camera],
                replace_existing=True
            )
            # Track the _dd_ job ID so remove_camera/update_camera can clean it up
            with self._lock:
                if camera.name in self._job_ids:
                    self._job_ids[camera.name].append(job_id)
            logger.info(f"Dawn/dusk daily job at {capture_time} for {camera.name}: {job_id}")

    def get_dawn_dusk_times(self) -> Optional[dict]:
        """Get today's dawn and dusk times for the API."""
        result = self._get_dawn_dusk_window()
        if result:
            return {"dawn": result[0].strftime("%H:%M"), "dusk": result[1].strftime("%H:%M")}
        return None

    def remove_camera(self, camera_name: str) -> None:
        """Remove all schedules for a camera."""
        with self._lock:
            if camera_name in self._job_ids:
                for job_id in self._job_ids[camera_name]:
                    try:
                        self.scheduler.remove_job(job_id)
                        logger.info(f"Removed job: {job_id}")
                    except Exception as e:
                        logger.warning(f"Failed to remove job {job_id}: {e}")
                del self._job_ids[camera_name]

            if camera_name in self._cameras:
                del self._cameras[camera_name]

    def update_camera(self, camera: CameraConfig) -> None:
        """Update schedules for a camera."""
        self.remove_camera(camera.name)
        with self._lock:
            self._cameras[camera.name] = camera
            if camera.enabled:
                self._add_camera_schedules(camera)

    def get_next_run_times(self) -> dict[str, list[dict]]:
        """Get next run times for all cameras."""
        result = {}

        with self._lock:
            job_ids_snapshot = {name: list(ids) for name, ids in self._job_ids.items()}

        for camera_name, job_ids in job_ids_snapshot.items():
            result[camera_name] = []
            for job_id in job_ids:
                try:
                    job = self.scheduler.get_job(job_id)
                    if job and job.next_run_time:
                        result[camera_name].append({
                            "job_id": job_id,
                            "next_run": job.next_run_time.isoformat()
                        })
                except Exception as e:
                    logger.warning(f"Failed to get next run time for job {job_id}: {e}")

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
