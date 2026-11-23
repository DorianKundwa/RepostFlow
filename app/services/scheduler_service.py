"""
app/services/scheduler_service.py - Background task scheduling via APScheduler.

Handles:
  - Background scheduler initialization and lifecycle management (start, stop, pause, resume).
  - Periodic YouTube source channel polling (fetches new uploads and invokes rule engine).
  - Background upload queue worker (picks pending jobs respecting concurrency limits).
  - Watched folder stability and rescan sweep.
  - Dynamic rescheduling based on live ApplicationSetting values.
  - Manual on-demand job triggering.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.extensions import db
from app.models import (
    ApplicationSetting,
    ChannelRole,
    UploadJob,
    UploadStatus,
    WatchedFolder,
    YouTubeChannel,
)

logger = logging.getLogger(__name__)

JOB_YOUTUBE_SOURCES = "youtube_source_monitor"
JOB_UPLOAD_QUEUE = "upload_queue_worker"
JOB_FOLDER_SWEEP = "folder_monitor_sweep"

DEFAULT_YOUTUBE_POLL_INTERVAL = 300   # 5 minutes
DEFAULT_UPLOAD_QUEUE_INTERVAL = 30    # 30 seconds
DEFAULT_FOLDER_SWEEP_INTERVAL = 600   # 10 minutes


class SchedulerService:
    def __init__(self):
        self._scheduler: Optional[BackgroundScheduler] = None
        self._lock = threading.Lock()
        self._app = None
        self._is_paused = False

    def init_app(self, app) -> None:
        """Store Flask app reference for context in workers."""
        self._app = app

    def start(self, app=None) -> bool:
        """Start the background scheduler if not already running."""
        with self._lock:
            if app:
                self._app = app

            if self._scheduler and self._scheduler.running:
                logger.info("APScheduler is already running.")
                return True

            try:
                self._scheduler = BackgroundScheduler(daemon=True)

                poll_interval = self._get_setting_int("youtube_poll_interval", DEFAULT_YOUTUBE_POLL_INTERVAL)
                queue_interval = DEFAULT_UPLOAD_QUEUE_INTERVAL
                folder_interval = DEFAULT_FOLDER_SWEEP_INTERVAL

                # 1. Source channel monitor job
                self._scheduler.add_job(
                    func=self._run_youtube_source_monitor,
                    trigger=IntervalTrigger(seconds=poll_interval),
                    id=JOB_YOUTUBE_SOURCES,
                    name="YouTube Source Channel Monitor",
                    replace_existing=True,
                )

                # 2. Upload queue worker job
                self._scheduler.add_job(
                    func=self._run_upload_queue_worker,
                    trigger=IntervalTrigger(seconds=queue_interval),
                    id=JOB_UPLOAD_QUEUE,
                    name="Resumable Upload Queue Worker",
                    replace_existing=True,
                )

                # 3. Watched folders maintenance sweep
                self._scheduler.add_job(
                    func=self._run_folder_sweep,
                    trigger=IntervalTrigger(seconds=folder_interval),
                    id=JOB_FOLDER_SWEEP,
                    name="Watched Folder Maintenance Sweep",
                    replace_existing=True,
                )

                self._scheduler.start()
                self._is_paused = False
                logger.info(
                    "APScheduler started successfully. Jobs: YouTube Monitor (%ds), Upload Worker (%ds), Folder Sweep (%ds).",
                    poll_interval,
                    queue_interval,
                    folder_interval,
                )
                return True
            except Exception as exc:
                logger.error("Failed to start APScheduler: %s", exc, exc_info=True)
                return False

    def stop(self) -> None:
        """Stop the background scheduler."""
        with self._lock:
            if self._scheduler and self._scheduler.running:
                logger.info("Stopping APScheduler...")
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception as exc:
                    logger.warning("Error shutting down APScheduler: %s", exc)
                finally:
                    self._scheduler = None
                    self._is_paused = False
                    logger.info("APScheduler stopped.")

    def pause(self) -> bool:
        """Pause all scheduled job execution."""
        with self._lock:
            if self._scheduler and self._scheduler.running:
                self._scheduler.pause()
                self._is_paused = True
                logger.info("APScheduler paused.")
                return True
            return False

    def resume(self) -> bool:
        """Resume scheduled job execution."""
        with self._lock:
            if self._scheduler and self._scheduler.running:
                self._scheduler.resume()
                self._is_paused = False
                logger.info("APScheduler resumed.")
                return True
            return False

    def is_running(self) -> bool:
        """Check if scheduler is currently running and active."""
        return bool(self._scheduler and self._scheduler.running and not self._is_paused)

    def is_paused(self) -> bool:
        """Check if scheduler exists but is paused."""
        return self._is_paused

    def get_jobs_info(self) -> List[Dict[str, Any]]:
        """Return information about currently registered jobs."""
        if not self._scheduler:
            return []

        info = []
        for job in self._scheduler.get_jobs():
            next_run = job.next_run_time
            info.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": next_run.strftime("%Y-%m-%d %H:%M:%S UTC") if next_run else "Paused",
                "trigger": str(job.trigger),
                "is_active": not self._is_paused,
            })
        return info

    def run_job_now(self, job_id: str, app=None) -> Tuple[bool, Optional[str]]:
        """Trigger an immediate execution of a specific scheduled job."""
        target_app = app or self._app
        if not target_app:
            return False, "Application context not available."

        if job_id == JOB_YOUTUBE_SOURCES:
            threading.Thread(target=self._run_youtube_source_monitor, daemon=True).start()
            return True, "YouTube source check triggered."
        elif job_id == JOB_UPLOAD_QUEUE:
            threading.Thread(target=self._run_upload_queue_worker, daemon=True).start()
            return True, "Upload queue worker triggered."
        elif job_id == JOB_FOLDER_SWEEP:
            threading.Thread(target=self._run_folder_sweep, daemon=True).start()
            return True, "Folder maintenance sweep triggered."
        else:
            return False, f"Unknown job ID '{job_id}'."

    def reschedule_jobs(self, app=None) -> None:
        """Update job intervals based on updated database settings."""
        with self._lock:
            if not self._scheduler or not self._scheduler.running:
                return

            poll_sec = self._get_setting_int("youtube_poll_interval", DEFAULT_YOUTUBE_POLL_INTERVAL)
            try:
                self._scheduler.reschedule_job(
                    job_id=JOB_YOUTUBE_SOURCES,
                    trigger=IntervalTrigger(seconds=poll_sec),
                )
                logger.info("Rescheduled %s with interval %ds", JOB_YOUTUBE_SOURCES, poll_sec)
            except Exception as exc:
                logger.warning("Could not reschedule job %s: %s", JOB_YOUTUBE_SOURCES, exc)

    # ------------------------------------------------------------------ #
    # Internal Worker Tasks                                               #
    # ------------------------------------------------------------------ #

    def _get_app(self):
        return self._app

    def _get_setting_int(self, key: str, default: int) -> int:
        if not self._app:
            return default
        try:
            with self._app.app_context():
                setting = ApplicationSetting.query.filter_by(key=key).first()
                if setting and setting.value:
                    return int(setting.value)
        except Exception:
            pass
        return default

    def _run_youtube_source_monitor(self) -> None:
        """Scheduled task: check all enabled source channels for new uploads."""
        app = self._get_app()
        if not app:
            return

        with app.app_context():
            logger.info("Executing scheduled YouTube source monitor task...")
            channels = YouTubeChannel.query.filter(
                YouTubeChannel.is_active == True,
                YouTubeChannel.monitoring_enabled == True,
                YouTubeChannel.role.in_([ChannelRole.SOURCE, ChannelRole.BOTH]),
            ).all()

            if not channels:
                logger.debug("No active source channels with monitoring enabled.")
                return

            from app.services.video_detector import check_source_channel_for_new_videos

            for ch in channels:
                try:
                    logger.debug("Checking source channel '%s' (ID %s)...", ch.channel_name, ch.id)
                    videos, err = check_source_channel_for_new_videos(ch.id)
                    if err:
                        logger.warning("Error checking channel '%s': %s", ch.channel_name, err)
                    elif videos:
                        logger.info("Detected %d new video(s) on channel '%s'.", len(videos), ch.channel_name)
                except Exception as exc:
                    logger.error("Unhandled error polling channel '%s': %s", ch.channel_name, exc, exc_info=True)

    def _run_upload_queue_worker(self) -> None:
        """Scheduled task: pick up pending upload jobs respecting concurrency limits."""
        app = self._get_app()
        if not app:
            return

        with app.app_context():
            # Check maximum concurrent uploads allowed
            max_concurrent = self._get_setting_int("max_concurrent_uploads", 2)

            active_count = UploadJob.query.filter(
                UploadJob.status.in_([UploadStatus.UPLOADING, UploadStatus.PROCESSING])
            ).count()

            available_slots = max(0, max_concurrent - active_count)
            if available_slots <= 0:
                logger.debug("Upload queue: maximum concurrent uploads reached (%d active).", active_count)
                return

            # Fetch next pending jobs
            pending_jobs = (
                UploadJob.query.filter(UploadJob.status == UploadStatus.PENDING)
                .order_by(UploadJob.created_at.asc())
                .limit(available_slots)
                .all()
            )

            if not pending_jobs:
                return

            logger.info(
                "Upload queue worker found %d pending job(s) for %d available slot(s).",
                len(pending_jobs),
                available_slots,
            )

            from app.services.upload_service import execute_upload_job

            for job in pending_jobs:
                job_id = job.id
                def _worker(jid=job_id):
                    with app.app_context():
                        execute_upload_job(jid, app)

                threading.Thread(target=_worker, daemon=True, name=f"UploadWorker-{job_id}").start()

    def _run_folder_sweep(self) -> None:
        """Scheduled task: maintenance scan of active watched folders."""
        app = self._get_app()
        if not app:
            return

        with app.app_context():
            folders = WatchedFolder.query.filter_by(is_active=True).all()
            if not folders:
                return

            from app.services.folder_monitor import rescan_folder

            for folder in folders:
                try:
                    rescan_folder(folder.id, app)
                except Exception as exc:
                    logger.warning("Error during folder sweep for '%s': %s", folder.path, exc)


# Global singleton instance
scheduler_service = SchedulerService()
