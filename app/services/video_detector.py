"""
app/services/video_detector.py - YouTube source channel upload detection and local file matcher.

Handles:
  - Checking a source channel's uploads playlist for new videos.
  - Extracting video metadata (title, description, tags, category, duration, thumbnail).
  - Attempting to match detected YouTube uploads to local original files in watched folders or storage.
  - Marking videos as Awaiting Original File when no master file is found.
  - Idempotent duplicate prevention: never re-registers an existing YouTube video ID.
  - Tracking last processed video ID and checking timestamps.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.extensions import db
from app.models import (
    ChannelRole,
    NotificationType,
    PrivacyStatus,
    SourceType,
    Video,
    VideoStatus,
    WatchedFolder,
    YouTubeChannel,
)
from app.services.media_service import validate_video_file
from app.services.notification_service import create_notification
from app.services.video_service import parse_tags_to_json
from app.services.automation_service import evaluate_rules_for_video
from app.services.youtube_service import (
    fetch_recent_playlist_items,
    fetch_video_details_batch,
    get_uploads_playlist_id,
    get_youtube_client,
)

logger = logging.getLogger(__name__)


def _sanitize_for_matching(text: str) -> str:
    """Normalize string for fuzzy filename matching (lowercase, alphanumeric only)."""
    return re.sub(r"[^a-zA-Z0-9]", "", text or "").lower()


def find_matching_local_file(
    title: str,
    youtube_video_id: str,
    user_id: int,
    allowed_extensions: Optional[set] = None,
) -> Optional[str]:
    """
    Search watched folders and local storage for a master video file
    matching a YouTube video (by filename, title, or video ID).

    Returns:
        Absolute local file path if a valid match is found, else None.
    """
    allowed_exts = allowed_extensions or {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    norm_title = _sanitize_for_matching(title)
    norm_id = _sanitize_for_matching(youtube_video_id)

    folders = WatchedFolder.query.filter_by(user_id=user_id, is_active=True).all()
    search_dirs = [Path(f.path) for f in folders if Path(f.path).is_dir()]

    for folder_dir in search_dirs:
        try:
            for item in folder_dir.glob("*"):
                if not item.is_file() or item.suffix.lower() not in allowed_exts:
                    continue

                stem = item.stem
                norm_stem = _sanitize_for_matching(stem)

                # Match against video ID or title
                if (norm_id and norm_id in norm_stem) or (norm_title and (norm_title in norm_stem or norm_stem in norm_title)):
                    is_valid, _, _ = validate_video_file(str(item), allowed_extensions=allowed_exts)
                    if is_valid:
                        return str(item.resolve())
        except Exception as exc:
            logger.warning("Error searching folder %s for matches: %s", folder_dir, exc)

    return None


def check_source_channel_for_new_videos(
    channel_id: int,
) -> Tuple[List[Video], Optional[str]]:
    """
    Check a source YouTube channel's uploads feed for newly published videos.

    Workflow:
      1. Verify channel is eligible (active, role in [source, both], monitoring_enabled).
      2. Retrieve uploads playlist ID.
      3. Query recent playlist items.
      4. Filter out videos already registered in the Video library.
      5. Fetch detailed metadata (tags, category, duration) for new videos.
      6. Attempt to match with local master file:
         - Match found -> VideoStatus.READY
         - Match not found -> VideoStatus.AWAITING_FILE
      7. Save Video records, update channel.last_video_id and last_checked_at.
      8. Notify user.

    Returns:
        (list_of_new_video_models, error_message_or_None)
    """
    channel = db.session.get(YouTubeChannel, channel_id)
    if not channel:
        return [], "Channel not found."

    if not channel.is_active:
        return [], f"Channel '{channel.channel_name}' is inactive."

    if channel.role not in (ChannelRole.SOURCE, ChannelRole.BOTH):
        return [], f"Channel '{channel.channel_name}' is not configured as a Source channel."

    account = channel.connected_account
    if not account or not account.is_active:
        return [], "Connected Google account is inactive or missing."

    service, err = get_youtube_client(account)
    if err or not service:
        return [], err or "Could not initialize YouTube API client."

    # Ensure uploads playlist ID is present
    playlist_id = channel.uploads_playlist_id
    if not playlist_id:
        playlist_id, pl_err = get_uploads_playlist_id(service, channel.channel_id)
        if pl_err or not playlist_id:
            return [], pl_err or "Could not determine uploads playlist ID."
        channel.uploads_playlist_id = playlist_id
        db.session.commit()

    # Fetch recent uploads
    raw_items, fetch_err = fetch_recent_playlist_items(service, playlist_id, max_results=20)
    if fetch_err:
        return [], fetch_err

    if not raw_items:
        channel.last_checked_at = datetime.now(timezone.utc)
        db.session.commit()
        return [], None

    # Filter out already known video IDs
    new_items = []
    for item in raw_items:
        v_id = item["video_id"]
        # Stop if we reached the last processed video
        if channel.last_video_id and v_id == channel.last_video_id:
            break
        # Also check if already in database (idempotency check)
        existing = Video.query.filter_by(youtube_video_id=v_id).first()
        if not existing:
            new_items.append(item)

    if not new_items:
        channel.last_checked_at = datetime.now(timezone.utc)
        # Update last_video_id to top item if not set
        if not channel.last_video_id and raw_items:
            channel.last_video_id = raw_items[0]["video_id"]
        db.session.commit()
        return [], None

    # Fetch detailed metadata (tags, category, duration)
    video_ids = [it["video_id"] for it in new_items]
    details_map, _ = fetch_video_details_batch(service, video_ids)

    created_videos = []
    for item in new_items:
        v_id = item["video_id"]
        meta = details_map.get(v_id, {})

        title = meta.get("title") or item.get("title") or "Untitled YouTube Video"
        desc = meta.get("description") or item.get("description")
        tags = meta.get("tags", [])
        category_id = meta.get("category_id", "22")
        duration_sec = meta.get("duration_seconds")
        thumb_url = meta.get("thumbnail_url") or item.get("thumbnail_url")
        pub_dt = meta.get("published_at") or item.get("published_at")

        # Map YouTube privacy to PrivacyStatus
        yt_privacy = meta.get("privacy_status", "private").lower()
        privacy_enum = PrivacyStatus.PRIVATE
        if yt_privacy == "unlisted":
            privacy_enum = PrivacyStatus.UNLISTED
        elif yt_privacy == "public":
            privacy_enum = PrivacyStatus.PUBLIC

        # Try to match local master file
        matched_local_path = find_matching_local_file(
            title=title,
            youtube_video_id=v_id,
            user_id=account.user_id,
        )

        if matched_local_path:
            status = VideoStatus.READY
            note = "Automatically matched to local master file."
        else:
            status = VideoStatus.AWAITING_FILE
            note = "Detected from YouTube source channel. Original local master file required before reposting."

        video = Video(
            source_type=SourceType.YOUTUBE,
            source_channel_id=channel.id,
            youtube_video_id=v_id,
            youtube_published_at=pub_dt,
            local_file_path=matched_local_path,
            original_filename=Path(matched_local_path).name if matched_local_path else None,
            title=title,
            description=desc,
            tags=parse_tags_to_json(tags),
            category_id=category_id,
            duration_seconds=duration_sec,
            thumbnail_url=thumb_url,
            privacy_status=privacy_enum,
            status=status,
            detected_at=datetime.now(timezone.utc),
            notes=note,
        )

        db.session.add(video)
        created_videos.append(video)

    # Update channel tracking pointers
    channel.last_video_id = new_items[0]["video_id"]
    channel.last_checked_at = datetime.now(timezone.utc)

    try:
        db.session.commit()

        # Trigger automation rules for detected videos
        for v in created_videos:
            try:
                evaluate_rules_for_video(v)
            except Exception as e_rule:
                logger.warning("Error evaluating rules for video %s: %s", v.id, e_rule)

        # Send notifications
        awaiting_count = sum(1 for v in created_videos if v.status == VideoStatus.AWAITING_FILE)
        ready_count = len(created_videos) - awaiting_count

        msg = f"Detected {len(created_videos)} new video(s) on '{channel.channel_name}'."
        if awaiting_count > 0:
            msg += f" {awaiting_count} video(s) require an original master file."

        create_notification(
            user_id=account.user_id,
            title=f"Source Uploads: {channel.channel_name}",
            message=msg,
            type=NotificationType.WARNING if awaiting_count > 0 else NotificationType.SUCCESS,
            related_url="/videos/",
        )

        logger.info(
            "Checked channel %s: found %d new videos (%d ready, %d awaiting file)",
            channel.channel_name,
            len(created_videos),
            ready_count,
            awaiting_count,
        )
        return created_videos, None
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to commit detected videos: %s", exc)
        return [], str(exc)


def check_all_monitored_source_channels() -> Dict[str, Any]:
    """
    Check all active YouTube channels with monitoring enabled and role in [source, both].
    Returns summary dict.
    """
    channels = YouTubeChannel.query.filter(
        YouTubeChannel.is_active == True,
        YouTubeChannel.monitoring_enabled == True,
        YouTubeChannel.role.in_([ChannelRole.SOURCE, ChannelRole.BOTH]),
    ).all()

    total_detected = 0
    errors = []

    for ch in channels:
        videos, err = check_source_channel_for_new_videos(ch.id)
        if err:
            errors.append(f"{ch.channel_name}: {err}")
        else:
            total_detected += len(videos)

    return {
        "channels_checked": len(channels),
        "new_videos_detected": total_detected,
        "errors": errors,
    }
