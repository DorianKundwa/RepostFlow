"""
app/services/automation_service.py - Automation rule engine and distribution queue dispatcher.

Handles:
  - Automation rule configuration (Source -> Multiple Destinations).
  - Loop prevention (ensuring source channels are never their own destination).
  - Metadata modification rules (prefixes, suffixes, prepend/append descriptions, tag merging).
  - Snapshotting metadata for individual UploadJobs.
  - Automatic evaluation when new videos are detected.
  - Manual execution of rules against existing video library entries.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.extensions import db
from app.models import (
    AutomationDestination,
    AutomationRule,
    NotificationType,
    PrivacyStatus,
    SourceType,
    UploadJob,
    UploadStatus,
    Video,
    VideoStatus,
    YouTubeChannel,
)
from app.services.notification_service import create_notification
from app.services.upload_service import check_duplicate_upload, is_duplicate_prevention_enabled
from app.services.video_service import parse_tags_to_json, parse_tags_to_list

logger = logging.getLogger(__name__)


def create_or_update_rule(
    user_id: int,
    name: str,
    source_type: SourceType,
    destination_channel_ids: List[int],
    source_channel_id: Optional[int] = None,
    source_folder_id: Optional[int] = None,
    copy_title: bool = True,
    copy_description: bool = True,
    copy_tags: bool = True,
    copy_thumbnail: bool = True,
    title_prefix: Optional[str] = None,
    title_suffix: Optional[str] = None,
    description_prepend: Optional[str] = None,
    description_append: Optional[str] = None,
    extra_tags: Optional[Any] = None,
    default_privacy: PrivacyStatus = PrivacyStatus.PRIVATE,
    prevent_duplicates: bool = True,
    is_enabled: bool = True,
    rule_id: Optional[int] = None,
) -> Tuple[Optional[AutomationRule], Optional[str]]:
    """
    Create or update an AutomationRule with destination mappings.

    Enforces loop prevention: destination channels cannot contain source channel.
    """
    if not name or not name.strip():
        return None, "Rule name cannot be empty."

    if not destination_channel_ids:
        return None, "At least one destination channel must be selected."

    # Validate source
    if source_type == SourceType.YOUTUBE and not source_channel_id:
        return None, "A source YouTube channel must be selected."
    if source_type == SourceType.LOCAL_FOLDER and not source_folder_id:
        return None, "A source watched folder must be selected."

    # Loop prevention check
    if source_type == SourceType.YOUTUBE and source_channel_id in destination_channel_ids:
        return None, "Loop prevention error: A source YouTube channel cannot also be its own destination."

    tags_json = parse_tags_to_json(extra_tags) if extra_tags else None

    try:
        if rule_id:
            rule = AutomationRule.query.filter_by(id=rule_id, user_id=user_id).first()
            if not rule:
                return None, "Automation rule not found."
            rule.name = name.strip()
            rule.source_type = source_type
            rule.source_channel_id = source_channel_id
            rule.source_folder_id = source_folder_id
            rule.copy_title = copy_title
            rule.copy_description = copy_description
            rule.copy_tags = copy_tags
            rule.copy_thumbnail = copy_thumbnail
            rule.title_prefix = title_prefix if title_prefix else None
            rule.title_suffix = title_suffix if title_suffix else None
            rule.description_prepend = description_prepend
            rule.description_append = description_append
            rule.extra_tags = tags_json
            rule.default_privacy = default_privacy
            rule.prevent_duplicates = prevent_duplicates
            rule.is_enabled = is_enabled
            rule.updated_at = datetime.now(timezone.utc)
            # Clear old destinations
            AutomationDestination.query.filter_by(rule_id=rule.id).delete()
        else:
            rule = AutomationRule(
                user_id=user_id,
                name=name.strip(),
                source_type=source_type,
                source_channel_id=source_channel_id,
                source_folder_id=source_folder_id,
                copy_title=copy_title,
                copy_description=copy_description,
                copy_tags=copy_tags,
                copy_thumbnail=copy_thumbnail,
                title_prefix=title_prefix if title_prefix else None,
                title_suffix=title_suffix if title_suffix else None,
                description_prepend=description_prepend,
                description_append=description_append,
                extra_tags=tags_json,
                default_privacy=default_privacy,
                prevent_duplicates=prevent_duplicates,
                is_enabled=is_enabled,
            )
            db.session.add(rule)
            db.session.flush()

        # Insert destinations
        for dest_id in set(destination_channel_ids):
            # Verify channel exists
            dest_channel = db.session.get(YouTubeChannel, dest_id)
            if dest_channel and dest_channel.is_active:
                ad = AutomationDestination(rule_id=rule.id, channel_id=dest_id)
                db.session.add(ad)

        db.session.commit()
        logger.info("Saved automation rule %s: '%s' with %d destinations", rule.id, rule.name, len(destination_channel_ids))
        return rule, None
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to save automation rule: %s", exc)
        return None, str(exc)


def toggle_rule(rule_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
    """Enable or disable an automation rule."""
    rule = AutomationRule.query.filter_by(id=rule_id, user_id=user_id).first()
    if not rule:
        return False, "Rule not found."
    rule.is_enabled = not rule.is_enabled
    rule.updated_at = datetime.now(timezone.utc)
    try:
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def delete_rule(rule_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
    """Delete an automation rule."""
    rule = AutomationRule.query.filter_by(id=rule_id, user_id=user_id).first()
    if not rule:
        return False, "Rule not found."
    try:
        db.session.delete(rule)
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def build_snapshot_metadata(
    video: Video,
    rule: AutomationRule,
) -> Dict[str, Any]:
    """Calculate the final title, description, tags, and privacy for a destination upload."""
    # 1. Title
    base_title = (video.title or video.original_filename or "").strip() if rule.copy_title else ""
    prefix = rule.title_prefix or ""
    suffix = rule.title_suffix or ""
    final_title = f"{prefix}{base_title}{suffix}".strip() or "Untitled Video"
    if len(final_title) > 100:
        final_title = final_title[:100]

    # 2. Description
    parts = []
    if rule.description_prepend and rule.description_prepend.strip():
        parts.append(rule.description_prepend.strip())
    if rule.copy_description and video.description and video.description.strip():
        parts.append(video.description.strip())
    if rule.description_append and rule.description_append.strip():
        parts.append(rule.description_append.strip())
    final_desc = "\n\n".join(parts)
    if len(final_desc) > 5000:
        final_desc = final_desc[:5000]

    # 3. Tags
    tags_list = parse_tags_to_list(video.tags) if rule.copy_tags else []
    if rule.extra_tags:
        tags_list.extend(parse_tags_to_list(rule.extra_tags))
    # Deduplicate while preserving order
    seen = set()
    deduped_tags = [t for t in tags_list if not (t.lower() in seen or seen.add(t.lower()))]
    final_tags_json = json.dumps(deduped_tags) if deduped_tags else None

    # 4. Thumbnail
    thumb_path = video.local_thumbnail_path if rule.copy_thumbnail else None

    # 5. Privacy
    privacy = rule.default_privacy or PrivacyStatus.PRIVATE

    return {
        "title": final_title,
        "description": final_desc,
        "tags": final_tags_json,
        "thumbnail_path": thumb_path,
        "privacy": privacy,
    }


def evaluate_rules_for_video(video: Video) -> List[UploadJob]:
    """
    Find matching active automation rules for a newly detected or updated video,
    and generate UploadJob entries for all configured destination channels.

    Guarantees:
      - Loop prevention: never queues an upload to the video's original source channel.
      - Idempotency: skips destination channels if an UploadJob for this video already exists.
      - Handles Awaiting File state: job is created with UploadStatus.AWAITING_FILE.
    """
    # Find matching enabled rules
    query = AutomationRule.query.filter_by(is_enabled=True, source_type=video.source_type)

    if video.source_type == SourceType.YOUTUBE and video.source_channel_id:
        query = query.filter_by(source_channel_id=video.source_channel_id)
    elif video.source_type == SourceType.LOCAL_FOLDER and video.watched_folder_id:
        query = query.filter_by(source_folder_id=video.watched_folder_id)
    else:
        return []

    rules = query.all()
    if not rules:
        return []

    created_jobs = []

    for rule in rules:
        meta_snapshot = build_snapshot_metadata(video, rule)

        for dest in rule.destinations:
            dest_channel = dest.channel
            if not dest_channel or not dest_channel.is_active:
                continue

            # Loop prevention: never upload back to source
            if video.source_channel_id and dest.channel_id == video.source_channel_id:
                logger.warning(
                    "Skipping destination channel %s for video %s: matches source channel (loop prevented).",
                    dest.channel_id,
                    video.id,
                )
                continue

            # Duplicate prevention check: skip if video was already published to this destination channel
            rule_prevent = getattr(rule, "prevent_duplicates", True)
            if rule_prevent and is_duplicate_prevention_enabled():
                is_dup, dup_reason, _ = check_duplicate_upload(
                    video=video,
                    destination_channel_id=dest.channel_id,
                    target_title=meta_snapshot["title"],
                )
                if is_dup:
                    logger.info(
                        "Skipping destination channel %s for video %s: duplicate upload prevented (%s).",
                        dest.channel_id,
                        video.id,
                        dup_reason,
                    )
                    continue

            # Idempotency check: does an upload job already exist for this video & destination?
            existing_job = UploadJob.query.filter_by(
                video_id=video.id,
                destination_channel_id=dest.channel_id,
                idempotency_key="default",
            ).first()

            if existing_job:
                # If existing job was awaiting file and video is now ready, upgrade it!
                if existing_job.status == UploadStatus.AWAITING_FILE and video.status == VideoStatus.READY:
                    existing_job.status = UploadStatus.PENDING
                    existing_job.upload_title = meta_snapshot["title"]
                    existing_job.upload_description = meta_snapshot["description"]
                    existing_job.upload_tags = meta_snapshot["tags"]
                    existing_job.upload_thumbnail_path = meta_snapshot["thumbnail_path"]
                    created_jobs.append(existing_job)
                continue

            # Determine initial job status
            if video.status == VideoStatus.AWAITING_FILE:
                initial_job_status = UploadStatus.AWAITING_FILE
            else:
                initial_job_status = UploadStatus.PENDING

            job = UploadJob(
                video_id=video.id,
                destination_channel_id=dest.channel_id,
                automation_rule_id=rule.id,
                idempotency_key="default",
                status=initial_job_status,
                upload_title=meta_snapshot["title"],
                upload_description=meta_snapshot["description"],
                upload_tags=meta_snapshot["tags"],
                upload_privacy=meta_snapshot["privacy"],
                upload_thumbnail_path=meta_snapshot["thumbnail_path"],
                created_at=datetime.now(timezone.utc),
            )
            db.session.add(job)
            created_jobs.append(job)

    if created_jobs:
        # If video was READY and jobs are queued, mark video as QUEUED
        if video.status == VideoStatus.READY:
            video.status = VideoStatus.QUEUED

        try:
            db.session.commit()
            logger.info("Queued %d upload job(s) for video #%s ('%s')", len(created_jobs), video.id, video.title)
        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to commit upload jobs for video %s: %s", video.id, exc)
            return []

    return created_jobs


def apply_rule_to_existing_videos(rule_id: int, user_id: int) -> Tuple[int, Optional[str]]:
    """
    Apply an automation rule to all existing videos in the library matching its source.

    Returns:
        (newly_queued_jobs_count, error_message_or_None)
    """
    rule = AutomationRule.query.filter_by(id=rule_id, user_id=user_id).first()
    if not rule:
        return 0, "Automation rule not found."

    q = Video.query.filter_by(source_type=rule.source_type)
    if rule.source_type == SourceType.YOUTUBE and rule.source_channel_id:
        q = q.filter_by(source_channel_id=rule.source_channel_id)
    elif rule.source_type == SourceType.LOCAL_FOLDER and rule.source_folder_id:
        q = q.filter_by(watched_folder_id=rule.source_folder_id)
    else:
        return 0, "Rule has no valid source configured."

    videos = q.all()
    total_queued = 0

    for v in videos:
        jobs = evaluate_rules_for_video(v)
        total_queued += len(jobs)

    return total_queued, None
