"""
app/services/video_service.py - Video library management and metadata services.

Handles:
  - Adding videos from local files (inspected with FFprobe + thumbnail generated).
  - Linking local files to YouTube-detected videos (resolving Awaiting Original File).
  - Updating video metadata (title, description, tags, privacy status).
  - Replacing or uploading custom thumbnails.
  - Video querying, search, and filtering.
  - Safe removal from library.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy import or_

from app.extensions import db
from app.models import (
    Video,
    VideoStatus,
    SourceType,
    PrivacyStatus,
    YouTubeChannel,
    WatchedFolder,
    NotificationType,
)
from app.services.media_service import validate_video_file
from app.services.thumbnail_service import (
    generate_video_thumbnail,
    process_uploaded_thumbnail,
)
from app.services.notification_service import create_notification

logger = logging.getLogger(__name__)


def parse_tags_to_json(tags_input: Any) -> Optional[str]:
    """Normalize tags input (list or comma-separated string) to a JSON string array."""
    if tags_input is None:
        return None
    if isinstance(tags_input, list):
        clean_tags = [str(t).strip() for t in tags_input if str(t).strip()]
        return json.dumps(clean_tags) if clean_tags else None
    if isinstance(tags_input, str):
        parts = [p.strip() for p in tags_input.split(",") if p.strip()]
        return json.dumps(parts) if parts else None
    return None


def parse_tags_to_list(tags_json: Optional[str]) -> List[str]:
    """Parse JSON string array back to a Python list of strings."""
    if not tags_json:
        return []
    try:
        data = json.loads(tags_json)
        if isinstance(data, list):
            return [str(t) for t in data]
        return [str(data)]
    except Exception:
        # Fallback if stored as comma-separated
        return [p.strip() for p in tags_json.split(",") if p.strip()]


def add_video_from_local_file(
    file_path: str,
    source_type: SourceType = SourceType.LOCAL_FOLDER,
    watched_folder_id: Optional[int] = None,
    source_channel_id: Optional[int] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Any] = None,
    privacy_status: PrivacyStatus = PrivacyStatus.PRIVATE,
    user_id_for_notification: Optional[int] = None,
) -> Tuple[Optional[Video], Optional[str]]:
    """
    Validate a local video file, inspect media details with FFprobe,
    generate an optimized thumbnail, and register in the database.

    Returns:
        (video_model, None) on success
        (None, error_message) on failure
    """
    allowed_exts = current_app.config.get("ALLOWED_VIDEO_EXTENSIONS", {".mp4", ".mov", ".mkv", ".avi", ".webm"})
    is_valid, err, meta = validate_video_file(file_path, allowed_extensions=allowed_exts)
    if not is_valid or not meta:
        return None, err or "Invalid video file."

    resolved_path = str(Path(file_path).resolve())
    orig_filename = Path(file_path).name

    # Check if this exact local file path is already registered
    existing = Video.query.filter_by(local_file_path=resolved_path).first()
    if existing:
        return existing, None

    # Derive default title from filename if not supplied
    video_title = title.strip() if title else Path(file_path).stem.replace("_", " ").title()

    # Generate thumbnail
    unique_id = uuid.uuid4().hex[:12]
    thumb_dir = current_app.config["THUMBNAILS_DIR"]
    thumb_path, _ = generate_video_thumbnail(
        video_path=resolved_path,
        output_dir=str(thumb_dir),
        identifier=unique_id,
        timestamp_seconds=min(1.0, (meta.get("duration_seconds") or 2.0) / 2.0),
    )

    video = Video(
        source_type=source_type,
        source_channel_id=source_channel_id,
        watched_folder_id=watched_folder_id,
        original_filename=orig_filename,
        local_file_path=resolved_path,
        file_size_bytes=meta["file_size_bytes"],
        duration_seconds=meta["duration_seconds"],
        width=meta["width"],
        height=meta["height"],
        fps=meta["fps"],
        codec=meta["codec"],
        title=video_title,
        description=description,
        tags=parse_tags_to_json(tags),
        privacy_status=privacy_status,
        local_thumbnail_path=thumb_path,
        status=VideoStatus.READY,
        detected_at=datetime.now(timezone.utc),
    )

    try:
        db.session.add(video)
        db.session.commit()

        if user_id_for_notification:
            create_notification(
                user_id=user_id_for_notification,
                title="New Video Added",
                message=f"'{video.title}' was added to the library ({meta.get('width')}x{meta.get('height')}).",
                type=NotificationType.SUCCESS,
            )

        logger.info("Added video %s: %s (%s)", video.id, video.title, resolved_path)
        return video, None
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to add video to database: %s", exc)
        return None, str(exc)


def attach_local_media_to_video(
    video_id: int,
    file_path: str,
) -> Tuple[bool, Optional[str]]:
    """
    Attach an original local media file to a Video record that is currently
    marked VideoStatus.AWAITING_FILE (e.g. detected from a YouTube source channel).

    Validates media with FFprobe, inspects technical specs, generates thumbnail,
    and transitions status to VideoStatus.READY.
    """
    video = db.session.get(Video, video_id)
    if not video:
        return False, "Video not found."

    allowed_exts = current_app.config.get("ALLOWED_VIDEO_EXTENSIONS", {".mp4", ".mov", ".mkv", ".avi", ".webm"})
    is_valid, err, meta = validate_video_file(file_path, allowed_extensions=allowed_exts)
    if not is_valid or not meta:
        return False, err or "Invalid media file."

    resolved_path = str(Path(file_path).resolve())
    video.local_file_path = resolved_path
    video.original_filename = Path(file_path).name
    video.file_size_bytes = meta["file_size_bytes"]
    video.duration_seconds = meta["duration_seconds"]
    video.width = meta["width"]
    video.height = meta["height"]
    video.fps = meta["fps"]
    video.codec = meta["codec"]

    # Generate thumbnail if none exists or update existing
    if not video.local_thumbnail_path or not os.path.isfile(video.local_thumbnail_path):
        unique_id = uuid.uuid4().hex[:12]
        thumb_dir = current_app.config["THUMBNAILS_DIR"]
        thumb_path, _ = generate_video_thumbnail(
            video_path=resolved_path,
            output_dir=str(thumb_dir),
            identifier=unique_id,
        )
        if thumb_path:
            video.local_thumbnail_path = thumb_path

    video.status = VideoStatus.READY
    video.updated_at = datetime.now(timezone.utc)

    try:
        db.session.commit()
        logger.info("Attached local file to video %s: %s", video.id, resolved_path)
        try:
            from app.services.automation_service import evaluate_rules_for_video
            evaluate_rules_for_video(video)
        except Exception as e_rule:
            logger.warning("Error evaluating rules after master file attached for video %s: %s", video.id, e_rule)
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def update_video_metadata(
    video_id: int,
    title: str,
    description: Optional[str] = None,
    tags: Optional[Any] = None,
    category_id: Optional[str] = None,
    privacy_status: Optional[PrivacyStatus] = None,
) -> Tuple[bool, Optional[str]]:
    """Update editable video metadata attributes."""
    video = db.session.get(Video, video_id)
    if not video:
        return False, "Video not found."

    if not title or not title.strip():
        return False, "Title cannot be empty."

    video.title = title.strip()
    video.description = description
    video.tags = parse_tags_to_json(tags)
    if category_id:
        video.category_id = category_id.strip()
    if privacy_status:
        video.privacy_status = privacy_status

    video.updated_at = datetime.now(timezone.utc)

    try:
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def set_custom_thumbnail(
    video_id: int,
    uploaded_file,
) -> Tuple[bool, Optional[str]]:
    """Upload and optimize a new custom thumbnail for a video."""
    video = db.session.get(Video, video_id)
    if not video:
        return False, "Video not found."

    thumb_dir = current_app.config["THUMBNAILS_DIR"]
    final_path, err = process_uploaded_thumbnail(
        file_storage_or_path=uploaded_file,
        output_dir=str(thumb_dir),
        identifier=str(video.id),
    )
    if err or not final_path:
        return False, err or "Failed to process thumbnail."

    video.local_thumbnail_path = final_path
    video.updated_at = datetime.now(timezone.utc)

    try:
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def delete_video(video_id: int, remove_local_file: bool = False) -> Tuple[bool, Optional[str]]:
    """
    Remove video from library. Optionally delete local video file if requested.
    Thumbnails are cleaned up automatically.
    """
    video = db.session.get(Video, video_id)
    if not video:
        return False, "Video not found."

    # Remove thumbnail if present
    if video.local_thumbnail_path and os.path.isfile(video.local_thumbnail_path):
        try:
            os.remove(video.local_thumbnail_path)
        except OSError:
            pass

    if remove_local_file and video.local_file_path and os.path.isfile(video.local_file_path):
        try:
            os.remove(video.local_file_path)
        except OSError as exc:
            logger.warning("Could not delete local file %s: %s", video.local_file_path, exc)

    try:
        db.session.delete(video)
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def get_video_stats() -> Dict[str, int]:
    """Return counts for video library statuses."""
    total = Video.query.count()
    ready = Video.query.filter_by(status=VideoStatus.READY).count()
    awaiting = Video.query.filter_by(status=VideoStatus.AWAITING_FILE).count()
    queued = Video.query.filter_by(status=VideoStatus.QUEUED).count()
    uploaded = Video.query.filter_by(status=VideoStatus.UPLOADED).count()
    failed = Video.query.filter_by(status=VideoStatus.FAILED).count()
    return {
        "total": total,
        "ready": ready,
        "awaiting_file": awaiting,
        "queued": queued,
        "uploaded": uploaded,
        "failed": failed,
    }


def query_videos(
    search_query: Optional[str] = None,
    status: Optional[str] = None,
    source_type: Optional[str] = None,
    channel_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Video], int]:
    """Search and filter videos with pagination."""
    q = Video.query

    if search_query:
        pattern = f"%{search_query.strip()}%"
        q = q.filter(
            or_(
                Video.title.ilike(pattern),
                Video.original_filename.ilike(pattern),
                Video.description.ilike(pattern),
            )
        )

    if status and status != "all":
        try:
            enum_status = VideoStatus(status)
            q = q.filter(Video.status == enum_status)
        except ValueError:
            pass

    if source_type and source_type != "all":
        try:
            enum_source = SourceType(source_type)
            q = q.filter(Video.source_type == enum_source)
        except ValueError:
            pass

    if channel_id:
        q = q.filter(Video.source_channel_id == channel_id)

    total_count = q.count()
    videos = q.order_by(Video.detected_at.desc()).offset(offset).limit(limit).all()
    return videos, total_count
