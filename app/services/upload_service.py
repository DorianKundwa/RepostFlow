"""
app/services/upload_service.py - YouTube resumable upload engine.

Handles:
  - Official chunked resumable upload protocol (MediaFileUpload, chunksize 5MB).
  - Real-time upload progress tracking (updating progress_percent in database).
  - Multi-channel support (uploading to distinct channels via separate OAuth credentials).
  - Metadata setting (title, description, tags, YouTube category, privacy status).
  - Custom thumbnail upload via thumbnails().set().
  - Storing destination video IDs and URLs (https://youtu.be/<id>).
  - Retry logic for transient failures, network interrupts, and token refreshes.
  - Quota error detection (HTTP 403 quotaExceeded).
  - Recording completed and permanently failed jobs in PublishingHistory.
  - No secrets, tokens, or credentials exposed in logs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from app.extensions import db
from app.models import (
    ApplicationSetting,
    NotificationType,
    PrivacyStatus,
    PublishingHistory,
    UploadJob,
    UploadStatus,
    Video,
    VideoStatus,
    YouTubeChannel,
)
from app.services.notification_service import create_notification
from app.services.video_service import parse_tags_to_list
from app.services.youtube_service import get_youtube_client

logger = logging.getLogger(__name__)

# Resumable chunk size: 5MB (must be a multiple of 256KB per YouTube Data API specs)
UPLOAD_CHUNK_SIZE = 5 * 1024 * 1024

# Concurrency & In-flight upload tracking to prevent duplicate uploads
_upload_execution_lock = threading.Lock()
_active_job_ids: Set[int] = set()
_active_video_destination_pairs: Set[Tuple[int, int]] = set()


def is_duplicate_prevention_enabled() -> bool:
    """Return whether duplicate upload prevention is globally active."""
    val = ApplicationSetting.get("prevent_duplicate_uploads", "true")
    if val is None:
        return True
    return str(val).strip().lower() in ("true", "1", "yes", "on")


def check_duplicate_upload(
    video: Video,
    destination_channel_id: int,
    target_title: Optional[str] = None,
    exclude_job_id: Optional[int] = None,
) -> Tuple[bool, Optional[str], Optional[PublishingHistory]]:
    """
    Check if a video has already been published or is currently uploading to a specific destination YouTube channel.

    Checks:
      1. PublishingHistory records for destination_channel_id with upload_status == 'completed':
         - Direct video ID match
         - Original YouTube video ID match (if present)
         - Title match (case-insensitive)
      2. UploadJob records for destination_channel_id with status == COMPLETED:
         - Direct video ID match
      3. Active in-flight UploadJob records for destination_channel_id (UPLOADING / PROCESSING)

    Returns:
        (is_duplicate, reason_message, matched_history_or_None)
    """
    # 1. Check PublishingHistory by video_id
    hist_by_video = PublishingHistory.query.filter(
        PublishingHistory.destination_channel_id == destination_channel_id,
        PublishingHistory.upload_status == "completed",
        PublishingHistory.video_id == video.id,
    )
    if exclude_job_id:
        hist_by_video = hist_by_video.filter(
            (PublishingHistory.upload_job_id != exclude_job_id) | (PublishingHistory.upload_job_id.is_(None))
        )
    hist_direct = hist_by_video.order_by(PublishingHistory.uploaded_at.desc()).first()
    if hist_direct:
        time_str = hist_direct.uploaded_at.strftime('%Y-%m-%d %H:%M UTC') if hist_direct.uploaded_at else "previously"
        yt_id_str = f" [YouTube ID: {hist_direct.destination_video_id}]" if hist_direct.destination_video_id else ""
        return True, f"Video #{video.id} was already published to this channel on {time_str}{yt_id_str}", hist_direct

    # 2. Check PublishingHistory by source YouTube video ID (if source was YouTube)
    if video.youtube_video_id:
        hist_by_yt = PublishingHistory.query.filter(
            PublishingHistory.destination_channel_id == destination_channel_id,
            PublishingHistory.upload_status == "completed",
            PublishingHistory.destination_video_id == video.youtube_video_id,
        )
        if exclude_job_id:
            hist_by_yt = hist_by_yt.filter(
                (PublishingHistory.upload_job_id != exclude_job_id) | (PublishingHistory.upload_job_id.is_(None))
            )
        hist_yt = hist_by_yt.order_by(PublishingHistory.uploaded_at.desc()).first()
        if hist_yt:
            return True, f"Video with source YouTube ID {video.youtube_video_id} was already published to this channel", hist_yt

    # 3. Check PublishingHistory by title
    clean_title = (target_title or video.title or "").strip()
    if clean_title and clean_title.lower() not in ("untitled video", "untitled"):
        hist_by_title = PublishingHistory.query.filter(
            PublishingHistory.destination_channel_id == destination_channel_id,
            PublishingHistory.upload_status == "completed",
            db.func.lower(PublishingHistory.video_title) == clean_title.lower(),
        )
        if exclude_job_id:
            hist_by_title = hist_by_title.filter(
                (PublishingHistory.upload_job_id != exclude_job_id) | (PublishingHistory.upload_job_id.is_(None))
            )
        hist_title = hist_by_title.order_by(PublishingHistory.uploaded_at.desc()).first()
        if hist_title:
            time_str = hist_title.uploaded_at.strftime('%Y-%m-%d %H:%M UTC') if hist_title.uploaded_at else "previously"
            yt_id_str = f" [YouTube ID: {hist_title.destination_video_id}]" if hist_title.destination_video_id else ""
            return True, f"Video with title '{hist_title.video_title}' was already published to this channel on {time_str}{yt_id_str}", hist_title

    # 4. Check completed UploadJob
    job_query = UploadJob.query.filter(
        UploadJob.destination_channel_id == destination_channel_id,
        UploadJob.video_id == video.id,
        UploadJob.status == UploadStatus.COMPLETED,
    )
    if exclude_job_id:
        job_query = job_query.filter(UploadJob.id != exclude_job_id)
    completed_job = job_query.first()
    if completed_job:
        yt_id_str = f" [YouTube ID: {completed_job.destination_video_id}]" if completed_job.destination_video_id else ""
        return True, f"UploadJob #{completed_job.id} already completed uploading this video to this channel{yt_id_str}", None

    # 5. Check active in-flight UploadJob for same video & destination
    active_job_query = UploadJob.query.filter(
        UploadJob.destination_channel_id == destination_channel_id,
        UploadJob.video_id == video.id,
        UploadJob.status.in_([UploadStatus.UPLOADING, UploadStatus.PROCESSING]),
    )
    if exclude_job_id:
        active_job_query = active_job_query.filter(UploadJob.id != exclude_job_id)
    active_job = active_job_query.first()
    if active_job:
        return True, f"UploadJob #{active_job.id} is currently uploading this video to this channel", None

    return False, None, None


def ensure_local_thumbnail(video: Video, target_dir: Path) -> Optional[str]:
    """
    Ensure a local thumbnail file is available on disk for uploading to YouTube.
    If only video.thumbnail_url is present, downloads and optimizes it.
    """
    if video.local_thumbnail_path:
        tp = Path(video.local_thumbnail_path)
        if not tp.is_absolute():
            try:
                from flask import current_app
                tp = Path(current_app.config["BASE_DIR"]) / tp
            except Exception:
                pass
        if tp.is_file():
            return str(tp.resolve())

    # Fallback to downloading remote thumbnail if present (e.g. from YouTube source)
    if video.thumbnail_url:
        try:
            import urllib.request
            target_dir.mkdir(parents=True, exist_ok=True)
            raw_path = target_dir / f"downloaded_raw_{video.id}.jpg"
            final_path = target_dir / f"custom_{video.id}_downloaded.jpg"

            req = urllib.request.Request(
                video.thumbnail_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp, open(raw_path, "wb") as out_f:
                out_f.write(resp.read())

            from app.services.thumbnail_service import optimize_thumbnail
            ok, _ = optimize_thumbnail(str(raw_path), str(final_path))
            raw_path.unlink(missing_ok=True)
            if ok and final_path.is_file():
                video.local_thumbnail_path = str(final_path.resolve())
                db.session.commit()
                return str(final_path.resolve())
        except Exception as e_dl:
            logger.warning("Could not download remote thumbnail for video %s (%s): %s", video.id, video.thumbnail_url, e_dl)

    return None


def upload_video_thumbnail_with_retry(
    service,
    video_id: str,
    thumbnail_path: str,
    max_retries: int = 4,
    initial_delay: float = 2.0,
) -> Tuple[bool, Optional[str]]:
    """
    Upload custom thumbnail to YouTube with retry logic and exponential backoff
    to handle YouTube's asynchronous video entity propagation delay (404 videoNotFound).
    """
    tp = Path(thumbnail_path)
    if not tp.is_file():
        return False, f"Thumbnail file not found on disk: {thumbnail_path}"

    delay = initial_delay
    last_err = None

    for attempt in range(max_retries):
        try:
            # YouTube propagation delay: brief wait on first attempt
            if attempt == 0 and initial_delay > 0:
                time.sleep(initial_delay)

            thumb_media = MediaFileUpload(str(tp), mimetype="image/jpeg", resumable=False)
            service.thumbnails().set(
                videoId=video_id,
                media_body=thumb_media,
            ).execute()
            logger.info("Custom thumbnail uploaded successfully for YouTube video %s on attempt %d", video_id, attempt + 1)
            return True, None
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", 0)
            content = str(getattr(exc, "content", b""))
            last_err = f"HTTP {status}: {content}"
            # 404 videoNotFound or transient server errors 500/502/503 -> wait and retry!
            if (status in (404, 500, 502, 503) or "videoNotFound" in content) and attempt < max_retries - 1:
                logger.info(
                    "YouTube video %s not yet ready for thumbnail (status %s). Retrying in %.1fs (attempt %d/%d)...",
                    video_id, status, delay, attempt + 1, max_retries
                )
                time.sleep(delay)
                delay *= 2
                continue
            if status == 403:
                # Custom thumbnails require phone-verified channel (Feature tier 2)
                err_msg = "Channel lacks custom thumbnail privileges (phone verification required on YouTube Studio)."
                logger.warning("Could not set thumbnail for video %s: %s (Details: %s)", video_id, err_msg, content)
                return False, err_msg
            logger.warning("Failed to upload custom thumbnail for video %s on attempt %d: %s", video_id, attempt + 1, exc)
            return False, str(exc)
        except Exception as exc:
            logger.warning("Unexpected error uploading thumbnail for video %s on attempt %d: %s", video_id, attempt + 1, exc)
            last_err = str(exc)
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return False, str(exc)

    return False, last_err or "Max retries exceeded uploading thumbnail."


def execute_upload_job(job_id: int, app=None) -> Tuple[bool, Optional[str]]:
    """
    Execute a single UploadJob using the official YouTube resumable upload mechanism.

    Guarantees:
      - Concurrency safe: prevents race conditions and double uploads of the same video.
      - Duplicate prevention: checks local history and live YouTube channel uploads.
      - Custom thumbnail: ensures local thumbnail is present (downloading from remote if needed)
        and uploads with exponential backoff retry to handle YouTube's 404 videoNotFound delay.
    """
    pair = None
    with _upload_execution_lock:
        if job_id in _active_job_ids:
            logger.warning("UploadJob #%s is already executing in an active thread. Aborting duplicate execution.", job_id)
            return False, f"UploadJob #{job_id} is already in progress."

        job = db.session.get(UploadJob, job_id)
        if not job:
            return False, f"UploadJob #{job_id} not found."

        if job.status == UploadStatus.COMPLETED:
            return False, f"Job #{job_id} is already in state 'completed'."
        if job.status == UploadStatus.PAUSED:
            return False, f"Job #{job_id} is paused."

        pair = (job.video_id, job.destination_channel_id)
        if pair in _active_video_destination_pairs:
            logger.warning(
                "Another upload job for video #%s to destination channel #%s is currently active.",
                job.video_id,
                job.destination_channel_id,
            )
            return False, f"Another upload for video #{job.video_id} to this channel is currently active."

        _active_job_ids.add(job_id)
        _active_video_destination_pairs.add(pair)
        job.status = UploadStatus.UPLOADING
        job.started_at = datetime.now(timezone.utc)
        job.last_attempt_at = datetime.now(timezone.utc)
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            _active_job_ids.discard(job_id)
            _active_video_destination_pairs.discard(pair)
            return False, f"Database error starting job: {exc}"

    try:
        video = job.video
        if not video:
            job.status = UploadStatus.FAILED
            job.error_message = "Associated video record missing."
            db.session.commit()
            return False, job.error_message

        # Check local video file
        if not video.local_file_path or not os.path.isfile(video.local_file_path):
            job.status = UploadStatus.AWAITING_FILE
            job.error_message = "Local original video file is missing on disk."
            db.session.commit()
            return False, job.error_message

        dest_channel = job.destination_channel
        if not dest_channel or not dest_channel.is_active:
            job.status = UploadStatus.FAILED
            job.error_message = "Destination channel is missing or inactive."
            db.session.commit()
            return False, job.error_message

        # Duplicate upload guard check
        rule_prevent = job.automation_rule.prevent_duplicates if job.automation_rule else True
        if rule_prevent and is_duplicate_prevention_enabled():
            is_dup, reason, matched_hist = check_duplicate_upload(
                video=video,
                destination_channel_id=dest_channel.id,
                target_title=job.upload_title or video.title,
                exclude_job_id=job.id,
            )
            if is_dup:
                job.status = UploadStatus.COMPLETED
                job.progress_percent = 100
                job.completed_at = datetime.now(timezone.utc)
                if matched_hist and matched_hist.destination_video_id:
                    job.destination_video_id = matched_hist.destination_video_id
                    job.destination_video_url = matched_hist.destination_video_url
                job.error_message = f"Skipped: Duplicate upload prevented ({reason})."

                # Check if all jobs for this video are completed
                remaining_jobs = UploadJob.query.filter(
                    UploadJob.video_id == video.id,
                    UploadJob.id != job.id,
                    UploadJob.status.in_([UploadStatus.PENDING, UploadStatus.UPLOADING, UploadStatus.PROCESSING]),
                ).count()
                if remaining_jobs == 0:
                    video.status = VideoStatus.UPLOADED

                db.session.commit()
                logger.info("Upload job #%s skipped: duplicate upload prevented (%s)", job.id, reason)
                return False, f"Duplicate upload prevented: {reason}"

        account = dest_channel.connected_account
        if not account or not account.is_active:
            job.status = UploadStatus.FAILED
            job.error_message = f"Google account for channel '{dest_channel.channel_name}' is disconnected."
            db.session.commit()
            return False, job.error_message

        service, err = get_youtube_client(account)
        if err or not service:
            job.error_message = err or "Failed to obtain valid YouTube API credentials."
            _handle_job_failure(job, job.error_message)
            db.session.commit()
            return False, job.error_message

        # Live YouTube duplicate check: verify if video is already on destination channel
        if rule_prevent and is_duplicate_prevention_enabled():
            target_title = (job.upload_title or video.title or "").strip()
            if target_title and target_title.lower() not in ("untitled video", "untitled"):
                try:
                    from app.services.youtube_service import get_uploads_playlist_id, fetch_recent_playlist_items
                    upl_pid, _ = get_uploads_playlist_id(service, dest_channel.channel_id)
                    if upl_pid:
                        recent_items, _ = fetch_recent_playlist_items(service, upl_pid, max_results=30)
                        for item in recent_items:
                            if item.get("title", "").strip().lower() == target_title.lower():
                                existing_yt_id = item.get("video_id")
                                existing_url = f"https://youtu.be/{existing_yt_id}"
                                logger.info(
                                    "Live YouTube duplicate detected for Job #%s: '%s' exists as %s",
                                    job.id, target_title, existing_yt_id
                                )
                                job.status = UploadStatus.COMPLETED
                                job.progress_percent = 100
                                job.completed_at = datetime.now(timezone.utc)
                                job.destination_video_id = existing_yt_id
                                job.destination_video_url = existing_url
                                job.error_message = f"Skipped: Video already exists on YouTube ({existing_yt_id})."

                                existing_hist = PublishingHistory.query.filter_by(
                                    destination_channel_id=dest_channel.id,
                                    destination_video_id=existing_yt_id,
                                ).first()
                                if not existing_hist:
                                    hist = PublishingHistory(
                                        upload_job_id=job.id,
                                        video_id=video.id,
                                        source_channel_id=video.source_channel_id,
                                        destination_channel_id=dest_channel.id,
                                        video_title=target_title,
                                        upload_status="completed",
                                        privacy_status=(job.upload_privacy or PrivacyStatus.PRIVATE).value,
                                        destination_video_id=existing_yt_id,
                                        destination_video_url=existing_url,
                                        uploaded_at=item.get("published_at") or datetime.now(timezone.utc),
                                    )
                                    db.session.add(hist)

                                remaining_jobs = UploadJob.query.filter(
                                    UploadJob.video_id == video.id,
                                    UploadJob.id != job.id,
                                    UploadJob.status.in_([UploadStatus.PENDING, UploadStatus.UPLOADING, UploadStatus.PROCESSING]),
                                ).count()
                                if remaining_jobs == 0:
                                    video.status = VideoStatus.UPLOADED

                                db.session.commit()
                                return False, f"Duplicate upload prevented: Video already exists on YouTube ({existing_yt_id})"
                except Exception as e_live:
                    logger.debug("Live YouTube duplicate check skipped due to error: %s", e_live)

        logger.info(
            "Starting resumable upload for Job #%s (Video #%s) -> '%s'",
            job.id,
            video.id,
            dest_channel.channel_name,
        )

        # Prepare upload metadata
        snippet = {
            "title": job.upload_title or video.title or "Untitled Video",
            "description": job.upload_description or video.description or "",
            "categoryId": video.category_id or "22",
        }

        # Parse tags
        tags_raw = job.upload_tags or video.tags
        tags_list = parse_tags_to_list(tags_raw)
        if tags_list:
            snippet["tags"] = tags_list[:50]  # YouTube limit: 50 tags

        privacy_val = (job.upload_privacy or PrivacyStatus.PRIVATE).value
        status_body = {
            "privacyStatus": privacy_val,
            "selfDeclaredMadeForKids": False,
        }

        body = {
            "snippet": snippet,
            "status": status_body,
        }

        media = MediaFileUpload(
            video.local_file_path,
            chunksize=UPLOAD_CHUNK_SIZE,
            resumable=True,
            mimetype="video/*",
        )

        insert_request = service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        # Resumable upload chunk streaming loop
        while response is None:
            upload_status, response = insert_request.next_chunk()
            if upload_status:
                progress = int(upload_status.progress() * 100)
                job.progress_percent = min(99, max(0, progress))
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

        # Upload complete!
        video_id = response.get("id")
        if not video_id:
            raise RuntimeError("YouTube API did not return an uploaded video ID.")

        video_url = f"https://youtu.be/{video_id}"
        job.destination_video_id = video_id
        job.destination_video_url = video_url
        job.progress_percent = 100
        job.status = UploadStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = None

        logger.info(
            "Upload successful for Job #%s! Destination Video ID: %s (%s)",
            job.id,
            video_id,
            video_url,
        )

        # Upload custom thumbnail if specified and exists (with retry for propagation delay)
        rule_copy_thumb = job.automation_rule.copy_thumbnail if job.automation_rule else True
        if rule_copy_thumb:
            try:
                from flask import current_app
                thumb_dir = Path(current_app.config["THUMBNAILS_DIR"])
            except Exception:
                thumb_dir = Path("storage/thumbnails")

            resolved_thumb = ensure_local_thumbnail(video, thumb_dir)
            if not resolved_thumb and job.upload_thumbnail_path:
                tp = Path(job.upload_thumbnail_path)
                if not tp.is_absolute():
                    try:
                        from flask import current_app
                        tp = Path(current_app.config["BASE_DIR"]) / tp
                    except Exception:
                        pass
                if tp.is_file():
                    resolved_thumb = str(tp.resolve())

            if resolved_thumb and os.path.isfile(resolved_thumb):
                is_testing = False
                try:
                    from flask import current_app
                    is_testing = current_app.config.get("TESTING", False)
                except Exception:
                    pass
                initial_wait = 0.0 if is_testing else 2.0

                from app.services.thumbnail_service import optimize_thumbnail
                opt_path = thumb_dir / f"upload_thumb_{video_id}_{uuid.uuid4().hex[:6]}.jpg"
                opt_ok, _ = optimize_thumbnail(resolved_thumb, str(opt_path))
                upload_thumb_file = str(opt_path.resolve()) if (opt_ok and opt_path.is_file()) else resolved_thumb

                ok_thumb, thumb_err = upload_video_thumbnail_with_retry(
                    service=service,
                    video_id=video_id,
                    thumbnail_path=upload_thumb_file,
                    max_retries=4,
                    initial_delay=initial_wait,
                )
                if opt_path.exists():
                    opt_path.unlink(missing_ok=True)

                if ok_thumb:
                    logger.info("Custom thumbnail successfully uploaded to YouTube for video %s", video_id)
                else:
                    logger.warning("Could not set custom thumbnail for YouTube video %s: %s", video_id, thumb_err)

        # Record in PublishingHistory
        history = PublishingHistory(
            upload_job_id=job.id,
            video_id=video.id,
            source_channel_id=video.source_channel_id,
            destination_channel_id=dest_channel.id,
            video_title=job.upload_title or video.title,
            upload_status="completed",
            privacy_status=privacy_val,
            destination_video_id=video_id,
            destination_video_url=video_url,
            uploaded_at=datetime.now(timezone.utc),
        )
        db.session.add(history)

        # Check if all jobs for this video are completed
        remaining_jobs = UploadJob.query.filter(
            UploadJob.video_id == video.id,
            UploadJob.id != job.id,
            UploadJob.status.in_([UploadStatus.PENDING, UploadStatus.UPLOADING, UploadStatus.PROCESSING]),
        ).count()
        if remaining_jobs == 0:
            video.status = VideoStatus.UPLOADED

        db.session.commit()

        # Send notification
        create_notification(
            user_id=account.user_id,
            title="Video Published",
            message=f"'{job.upload_title or video.title}' uploaded to {dest_channel.channel_name} ({privacy_val}).",
            type=NotificationType.SUCCESS,
            related_url=video_url,
        )

        return True, None

    except HttpError as exc:
        content_str = str(getattr(exc, "content", b""))
        reason_str = str(getattr(exc, "reason", ""))
        is_quota = exc.resp.status == 403 and (
            "quotaExceeded" in str(exc) or "quotaExceeded" in content_str or "quotaExceeded" in reason_str
        )
        err_msg = f"YouTube API error ({exc.resp.status}): {exc.reason}"
        if is_quota:
            err_msg = "YouTube API daily quota exceeded. Uploads paused until tomorrow."
            logger.error("Quota exceeded for account %s: %s", account.email, exc)

        _handle_job_failure(job, err_msg)
        db.session.commit()
        return False, err_msg

    except Exception as exc:
        err_msg = f"Upload error: {str(exc)}"
        logger.error("Exception during upload job #%s: %s", job.id, exc, exc_info=True)
        _handle_job_failure(job, err_msg)
        db.session.commit()
        return False, err_msg

    finally:
        with _upload_execution_lock:
            _active_job_ids.discard(job_id)
            if pair:
                _active_video_destination_pairs.discard(pair)


def _handle_job_failure(job: UploadJob, error_message: str) -> None:
    """Increment retry counter and transition to PENDING (retryable) or FAILED."""
    job.retry_count += 1
    job.last_attempt_at = datetime.now(timezone.utc)
    job.error_message = error_message

    account = job.destination_channel.connected_account if job.destination_channel else None
    user_id = account.user_id if account else None

    if job.retry_count < job.max_retries:
        job.status = UploadStatus.PENDING
        logger.warning(
            "Upload Job #%s failed (attempt %d/%d). Resetting to PENDING for retry.",
            job.id,
            job.retry_count,
            job.max_retries,
        )
    else:
        job.status = UploadStatus.FAILED
        logger.error(
            "Upload Job #%s permanently failed after %d attempts: %s",
            job.id,
            job.retry_count,
            error_message,
        )

        # Record in PublishingHistory
        history = PublishingHistory(
            upload_job_id=job.id,
            video_id=job.video_id,
            source_channel_id=job.video.source_channel_id if job.video else None,
            destination_channel_id=job.destination_channel_id,
            video_title=job.upload_title or (job.video.title if job.video else "Unknown"),
            upload_status="failed",
            privacy_status=(job.upload_privacy or PrivacyStatus.PRIVATE).value,
            error_message=error_message,
            uploaded_at=datetime.now(timezone.utc),
        )
        db.session.add(history)

        if user_id:
            create_notification(
                user_id=user_id,
                title="Upload Failed",
                message=f"Job #{job.id} ('{job.upload_title}') failed: {error_message}",
                type=NotificationType.ERROR,
                related_url="/uploads/",
            )


def retry_job(job_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
    """Reset a failed or paused job back to PENDING."""
    job = db.session.get(UploadJob, job_id)
    if not job:
        return False, "Job not found."

    # Validate ownership via destination channel connected account
    if job.destination_channel and job.destination_channel.connected_account.user_id != user_id:
        return False, "Access denied."

    job.status = UploadStatus.PENDING
    job.error_message = None
    job.progress_percent = 0
    # Allow fresh retries
    job.retry_count = 0
    try:
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def pause_job(job_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
    """Pause an active or pending job."""
    job = db.session.get(UploadJob, job_id)
    if not job:
        return False, "Job not found."

    if job.destination_channel and job.destination_channel.connected_account.user_id != user_id:
        return False, "Access denied."

    if job.status in (UploadStatus.COMPLETED, UploadStatus.FAILED):
        return False, f"Cannot pause job in status '{job.status.value}'."

    job.status = UploadStatus.PAUSED
    try:
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def resume_job(job_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
    """Resume a paused job."""
    job = db.session.get(UploadJob, job_id)
    if not job:
        return False, "Job not found."

    if job.destination_channel and job.destination_channel.connected_account.user_id != user_id:
        return False, "Access denied."

    if job.status != UploadStatus.PAUSED:
        return False, f"Job is not paused (status: '{job.status.value}')."

    job.status = UploadStatus.PENDING
    try:
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def cancel_job(job_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
    """Cancel and delete an upload job."""
    job = db.session.get(UploadJob, job_id)
    if not job:
        return False, "Job not found."

    if job.destination_channel and job.destination_channel.connected_account.user_id != user_id:
        return False, "Access denied."

    try:
        db.session.delete(job)
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def run_job_in_background(job_id: int, app) -> None:
    """Launch execute_upload_job in a detached background thread."""
    def _worker():
        with app.app_context():
            execute_upload_job(job_id, app)

    t = threading.Thread(target=_worker, daemon=True, name=f"UploadWorker-Job{job_id}")
    t.start()
