"""
app/services/youtube_service.py - YouTube Data API v3 operations.

Handles:
  - Retrieving uploads playlist ID from channel ID (or relatedPlaylists).
  - Fetching playlist items (uploads feed) via PlaylistItems API.
  - Fetching detailed video metadata via Videos API.
  - ISO 8601 duration parsing (PT1H2M3S -> seconds).
  - Quota error and API error handling.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.extensions import db
from app.models import ConnectedAccount, YouTubeChannel
from app.services.google_auth import get_valid_credentials

logger = logging.getLogger(__name__)

_YT_SERVICE = "youtube"
_YT_VERSION = "v3"


def parse_iso8601_duration(duration_str: str) -> Optional[float]:
    """
    Parse an ISO 8601 duration string like 'PT1H2M10S', 'PT4M33S', or 'PT45S' to seconds.
    """
    if not duration_str or not duration_str.startswith("P"):
        return None

    pattern = re.compile(
        r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
    )
    match = pattern.match(duration_str)
    if not match:
        return None

    parts = match.groupdict()
    total_seconds = 0.0
    if parts.get("days"):
        total_seconds += float(parts["days"]) * 86400
    if parts.get("hours"):
        total_seconds += float(parts["hours"]) * 3600
    if parts.get("minutes"):
        total_seconds += float(parts["minutes"]) * 60
    if parts.get("seconds"):
        total_seconds += float(parts["seconds"])

    return round(total_seconds, 2)


def get_youtube_client(connected_account: ConnectedAccount):
    """
    Build an authenticated YouTube API client for a ConnectedAccount.
    Returns (service, error_message).
    """
    creds = get_valid_credentials(connected_account)
    if creds is None:
        return None, "Invalid or expired credentials. Please reconnect your Google account."

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        service = build(_YT_SERVICE, _YT_VERSION, credentials=creds)
        return service, None
    except Exception as exc:
        logger.error("Failed to initialize YouTube API client: %s", exc)
        return None, str(exc)


def get_uploads_playlist_id(service, channel_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Retrieve the uploads playlist ID ('UU...') for a channel.

    Returns:
        (uploads_playlist_id, None) on success
        (None, error_message) on failure
    """
    try:
        resp = service.channels().list(
            part="contentDetails",
            id=channel_id,
            maxResults=1,
        ).execute()

        items = resp.get("items", [])
        if not items:
            # Fallback convention: UC... -> UU...
            if channel_id.startswith("UC") and len(channel_id) > 2:
                fallback_id = "UU" + channel_id[2:]
                return fallback_id, None
            return None, f"Channel '{channel_id}' not found."

        content_details = items[0].get("contentDetails", {})
        uploads_id = content_details.get("relatedPlaylists", {}).get("uploads")
        if not uploads_id:
            # Fallback convention
            if channel_id.startswith("UC") and len(channel_id) > 2:
                uploads_id = "UU" + channel_id[2:]
        return uploads_id, None
    except HttpError as exc:
        return None, f"YouTube API error: {exc.resp.status} {exc.error_details}"
    except Exception as exc:
        return None, str(exc)


def fetch_recent_playlist_items(
    service,
    playlist_id: str,
    max_results: int = 50,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Retrieve recent items from a playlist.

    Returns:
        (list_of_items, error_message_or_None)
        Each item dict has keys: video_id, published_at, title, description, thumbnail_url
    """
    try:
        resp = service.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=min(max_results, 50),
        ).execute()

        results = []
        for item in resp.get("items", []):
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            video_id = content.get("videoId") or snippet.get("resourceId", {}).get("videoId")
            if not video_id:
                continue

            thumbs = snippet.get("thumbnails", {})
            thumb_url = (
                thumbs.get("maxres", {}).get("url")
                or thumbs.get("high", {}).get("url")
                or thumbs.get("medium", {}).get("url")
                or thumbs.get("default", {}).get("url")
            )

            raw_pub = snippet.get("publishedAt")
            published_dt = None
            if raw_pub:
                try:
                    published_dt = datetime.fromisoformat(raw_pub.replace("Z", "+00:00"))
                except ValueError:
                    pass

            results.append({
                "video_id": video_id,
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "published_at": published_dt,
                "thumbnail_url": thumb_url,
            })

        return results, None
    except HttpError as exc:
        return [], f"YouTube API error: {exc.resp.status} {exc.error_details}"
    except Exception as exc:
        return [], str(exc)


def fetch_video_details_batch(
    service,
    video_ids: List[str],
) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """
    Fetch comprehensive metadata for up to 50 videos in a single batch API call.

    Returns:
        ({video_id: metadata_dict}, error_message_or_None)
    """
    if not video_ids:
        return {}, None

    try:
        resp = service.videos().list(
            part="snippet,contentDetails,status",
            id=",".join(video_ids[:50]),
        ).execute()

        details = {}
        for item in resp.get("items", []):
            v_id = item["id"]
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            status = item.get("status", {})

            thumbs = snippet.get("thumbnails", {})
            thumb_url = (
                thumbs.get("maxres", {}).get("url")
                or thumbs.get("high", {}).get("url")
                or thumbs.get("medium", {}).get("url")
                or thumbs.get("default", {}).get("url")
            )

            raw_pub = snippet.get("publishedAt")
            published_dt = None
            if raw_pub:
                try:
                    published_dt = datetime.fromisoformat(raw_pub.replace("Z", "+00:00"))
                except ValueError:
                    pass

            duration_sec = parse_iso8601_duration(content.get("duration", ""))

            details[v_id] = {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "tags": snippet.get("tags", []),
                "category_id": snippet.get("categoryId", "22"),
                "published_at": published_dt,
                "thumbnail_url": thumb_url,
                "duration_seconds": duration_sec,
                "privacy_status": status.get("privacyStatus", "private"),
                "upload_status": status.get("uploadStatus", "processed"),
            }

        return details, None
    except HttpError as exc:
        return {}, f"YouTube API error: {exc.resp.status} {exc.error_details}"
    except Exception as exc:
        return {}, str(exc)
