"""
app/services/folder_monitor.py - Automatic local folder monitoring using Watchdog.

Features:
  - Multi-folder filesystem monitoring using Watchdog Observer.
  - Asynchronous file stabilization check (handles incomplete copies/renders).
  - Validation using FFprobe and thumbnail generation via media_service.
  - Idempotent: avoids duplicate library entries.
  - Rescan functionality for existing directories.
  - Windows path support.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.extensions import db
from app.models import (
    WatchedFolder,
    Video,
    SourceType,
    NotificationType,
)
from app.services.video_service import add_video_from_local_file
from app.services.notification_service import create_notification

logger = logging.getLogger(__name__)


def wait_for_file_stability(
    file_path: str,
    required_stable_checks: int = 3,
    check_interval: float = 1.0,
    timeout: float = 60.0,
) -> bool:
    """
    Wait until a file has finished copying or writing.

    A file is considered stable if:
      1. Its file size remains identical for `required_stable_checks` consecutive checks.
      2. Its file size is greater than 0 bytes.
      3. The file can be opened for reading in binary mode (verifying no exclusive write lock).

    Returns:
        True if stable, False if timeout reached or file removed.
    """
    path_obj = Path(file_path)
    start_time = time.time()
    last_size = -1
    stable_count = 0

    while time.time() - start_time < timeout:
        if not path_obj.is_file():
            return False

        try:
            current_size = path_obj.stat().st_size
        except OSError:
            # File may be temporarily inaccessible
            time.sleep(check_interval)
            continue

        if current_size > 0 and current_size == last_size:
            # Check if readable
            try:
                with open(file_path, "rb") as f:
                    _ = f.read(1024)
                stable_count += 1
            except (IOError, OSError, PermissionError):
                # File is locked by writing process
                stable_count = 0
        else:
            stable_count = 0
            last_size = current_size

        if stable_count >= required_stable_checks:
            return True

        time.sleep(check_interval)

    logger.warning("File stability timeout (%ss) reached for: %s", timeout, file_path)
    return False


class VideoFolderEventHandler(FileSystemEventHandler):
    """Watchdog event handler for monitored video folders."""

    def __init__(self, app, folder_id: int, allowed_extensions: Set[str]):
        super().__init__()
        self.app = app
        self.folder_id = folder_id
        self.allowed_extensions = {ext.lower() for ext in allowed_extensions}
        # In-flight files set to prevent duplicate worker threads
        self._processing_paths: Set[str] = set()
        self._lock = threading.Lock()

    def _should_handle(self, file_path: str) -> bool:
        ext = Path(file_path).suffix.lower()
        if ext not in self.allowed_extensions:
            return False
        # Ignore temporary / hidden files
        name = Path(file_path).name
        if name.startswith(".") or name.startswith("~") or name.endswith(".tmp") or name.endswith(".part"):
            return False
        return True

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._should_handle(event.src_path):
            self._queue_file_processing(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        dest_path = getattr(event, "dest_path", None)
        if dest_path and self._should_handle(dest_path):
            self._queue_file_processing(dest_path)

    def _queue_file_processing(self, file_path: str) -> None:
        resolved = str(Path(file_path).resolve())
        with self._lock:
            if resolved in self._processing_paths:
                return
            self._processing_paths.add(resolved)

        thread = threading.Thread(
            target=self._process_file_async,
            args=(resolved,),
            daemon=True,
            name=f"FolderProc-{Path(file_path).name}",
        )
        thread.start()

    def _process_file_async(self, resolved_path: str) -> None:
        try:
            logger.info("New video detected in watched folder %s: %s", self.folder_id, resolved_path)
            # Wait for file to finish copying
            is_stable = wait_for_file_stability(
                resolved_path,
                required_stable_checks=2,
                check_interval=1.0,
                timeout=60.0,
            )
            if not is_stable:
                logger.warning("Skipping unstable file: %s", resolved_path)
                return

            with self.app.app_context():
                folder = db.session.get(WatchedFolder, self.folder_id)
                if not folder or not folder.is_active:
                    return

                # Check for duplicate
                existing = Video.query.filter_by(local_file_path=resolved_path).first()
                if existing:
                    logger.debug("Video already registered: %s", resolved_path)
                    return

                video, err = add_video_from_local_file(
                    file_path=resolved_path,
                    source_type=SourceType.LOCAL_FOLDER,
                    watched_folder_id=self.folder_id,
                    user_id_for_notification=folder.user_id,
                )
                if video:
                    logger.info("Successfully ingested video from folder: %s", video.title)
                    try:
                        from app.services.automation_service import evaluate_rules_for_video
                        evaluate_rules_for_video(video)
                    except Exception as e_rule:
                        logger.warning("Error evaluating rules for ingested video %s: %s", video.id, e_rule)
                else:
                    logger.error("Failed to ingest video from folder: %s", err)
        except Exception as exc:
            logger.error("Error processing watched file %s: %s", resolved_path, exc, exc_info=True)
        finally:
            with self._lock:
                self._processing_paths.discard(resolved_path)


class FolderMonitorManager:
    """Manages active Watchdog observer threads and watches."""

    def __init__(self):
        self._observer: Optional[Observer] = None
        self._watches: Dict[int, Any] = {}  # folder_id -> watchdog watch object
        self._lock = threading.Lock()
        self.app = None

    def start(self, app) -> None:
        """Start the background filesystem observer for all active watched folders."""
        with self._lock:
            self.app = app
            if self._observer is not None and self._observer.is_alive():
                return

            self._observer = Observer()
            self._watches.clear()

            with app.app_context():
                folders = WatchedFolder.query.filter_by(is_active=True).all()
                allowed_exts = app.config.get("ALLOWED_VIDEO_EXTENSIONS", {".mp4", ".mov", ".mkv", ".avi", ".webm"})

                for folder in folders:
                    self._add_watch_unlocked(folder.id, folder.path, folder.recursive, allowed_exts)

            try:
                self._observer.start()
                logger.info("FolderMonitorManager started with %d active watches.", len(self._watches))
            except Exception as exc:
                logger.error("Failed to start FolderMonitorManager observer: %s", exc)

    def stop(self) -> None:
        """Stop observer and join thread."""
        with self._lock:
            if self._observer:
                try:
                    self._observer.stop()
                    self._observer.join(timeout=3.0)
                except Exception as exc:
                    logger.warning("Error stopping FolderMonitorManager: %s", exc)
                finally:
                    self._observer = None
                    self._watches.clear()

    def restart(self, app) -> None:
        """Restart observer with latest active folders."""
        self.stop()
        self.start(app)

    def add_watch(self, folder_id: int, folder_path: str, recursive: bool = False) -> bool:
        """Add or update a watch directory while running."""
        with self._lock:
            if not self._observer or not self._observer.is_alive():
                return False

            allowed_exts = self.app.config.get("ALLOWED_VIDEO_EXTENSIONS", {".mp4", ".mov", ".mkv", ".avi", ".webm"}) if self.app else {".mp4", ".mov", ".mkv", ".avi", ".webm"}
            return self._add_watch_unlocked(folder_id, folder_path, recursive, allowed_exts)

    def _add_watch_unlocked(self, folder_id: int, folder_path: str, recursive: bool, allowed_exts: Set[str]) -> bool:
        if folder_id in self._watches:
            try:
                self._observer.unschedule(self._watches[folder_id])
            except Exception:
                pass
            del self._watches[folder_id]

        p = Path(folder_path)
        try:
            if not p.is_dir():
                logger.warning("Watched folder directory does not exist: %s", folder_path)
                return False
        except (PermissionError, OSError) as exc:
            logger.warning("Watched folder directory inaccessible (%s): %s", exc, folder_path)
            return False

        handler = VideoFolderEventHandler(self.app, folder_id, allowed_exts)
        try:
            watch = self._observer.schedule(handler, str(p.resolve()), recursive=recursive)
            self._watches[folder_id] = watch
            logger.info("Scheduled watch on: %s (recursive=%s)", folder_path, recursive)
            return True
        except Exception as exc:
            logger.error("Failed to schedule watch on %s: %s", folder_path, exc)
            return False

    def remove_watch(self, folder_id: int) -> bool:
        """Unschedule a watch."""
        with self._lock:
            if folder_id in self._watches and self._observer:
                try:
                    self._observer.unschedule(self._watches[folder_id])
                except Exception:
                    pass
                del self._watches[folder_id]
                return True
            return False


# Singleton instance
folder_monitor = FolderMonitorManager()


def rescan_folder(folder_id: int, app=None) -> Tuple[int, Optional[str]]:
    """
    Manually scan a watched folder for existing video files that are not yet in the library.

    Returns:
        (new_videos_added_count, error_message_or_None)
    """
    from datetime import datetime, timezone

    folder = db.session.get(WatchedFolder, folder_id)
    if not folder:
        return 0, "Watched folder not found."

    p = Path(folder.path)
    if not p.is_dir():
        return 0, f"Folder path '{folder.path}' does not exist on disk."

    allowed_exts = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    if app:
        allowed_exts = app.config.get("ALLOWED_VIDEO_EXTENSIONS", allowed_exts)

    glob_pattern = "**/*" if folder.recursive else "*"
    files_to_check = []
    try:
        for item in p.glob(glob_pattern):
            if item.is_file() and item.suffix.lower() in allowed_exts:
                files_to_check.append(str(item.resolve()))
    except Exception as exc:
        return 0, f"Error scanning directory: {exc}"

    added_count = 0
    for file_path in files_to_check:
        existing = Video.query.filter_by(local_file_path=file_path).first()
        if existing:
            continue

        video, err = add_video_from_local_file(
            file_path=file_path,
            source_type=SourceType.LOCAL_FOLDER,
            watched_folder_id=folder.id,
            user_id_for_notification=folder.user_id,
        )
        if video:
            added_count += 1
            try:
                from app.services.automation_service import evaluate_rules_for_video
                evaluate_rules_for_video(video)
            except Exception as e_rule:
                logger.warning("Error evaluating rules during rescan for video %s: %s", video.id, e_rule)

    folder.last_scanned_at = datetime.now(timezone.utc)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to update last_scanned_at: %s", exc)

    return added_count, None
