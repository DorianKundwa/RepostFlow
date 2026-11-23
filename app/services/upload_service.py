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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from app.extensions import db
from app.models import (
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


def execute_upload_job(job_id: int, app=None) -> Tuple[bool, Optional[str]]:
    """
    Execute a single UploadJob using the official YouTube resumable upload mechanism.

    Workflow:
      1. Fetch and validate UploadJob and its Video.
      2. Verify local video master file exists and is readable.
      3. Obtain authenticated YouTube API client for destination channel.
      4. Initialize MediaFileUpload with resumable=True and 5MB chunking.
      5. Call videos().insert(part='snippet,status', media_body=media) and stream chunks.
      6. Update progress_percent during streaming.
      7. On success, store destination video ID and URL (https://youtu.be/<id>).
      8. Upload custom thumbnail if specified and account is eligible.
      9. Record in PublishingHistory and update job status to COMPLETED.
      10. If an error occurs, increment retry_count; if retries exhausted, mark FAILED.

    Returns:
        (success_boolean, error_message_or_None)
    """
    job = db.session.get(UploadJob, job_id)
    if not job:
        return False, f"UploadJob #{job_id} not found."

    if job.status in (UploadStatus.COMPLETED, UploadStatus.PAUSED):
        return False, f"Job #{job_id} is already in state '{job.status.value}'."

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

    # Transition to UPLOADING
    job.status = UploadStatus.UPLOADING
    job.started_at = datetime.now(timezone.utc)
    job.last_attempt_at = datetime.now(timezone.utc)
    job.progress_percent = 0
    job.error_message = None
    db.session.commit()

    logger.info(
        "Starting resumable upload for Job #%s (Video #%s) -> '%s'",
        job.id,
        video.id,
        dest_channel.channel_name,
    )

    try:
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

        # Upload custom thumbnail if specified and exists
        thumb_path = job.upload_thumbnail_path or video.local_thumbnail_path
        if thumb_path and os.path.isfile(thumb_path):
            try:
                thumb_media = MediaFileUpload(thumb_path, mimetype="image/jpeg")
                service.thumbnails().set(
                    videoId=video_id,
                    media_body=thumb_media,
                ).execute()
                logger.info("Custom thumbnail uploaded for video %s", video_id)
            except Exception as e_thumb:
                # Custom thumbnail failure does not invalidate the video upload
                logger.warning(
                    "Could not upload custom thumbnail for video %s (account may lack verification): %s",
                    video_id,
                    e_thumb,
                )

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
