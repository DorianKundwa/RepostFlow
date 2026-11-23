"""
app/services/channel_service.py - YouTube channel discovery and management.

Uses the authenticated Google credentials to:
  - Retrieve the user's own YouTube channels (including Brand Accounts
    if the OAuth grant includes them).
  - Fetch channel metadata: name, ID, thumbnail, subscriber count,
    uploads playlist ID.
  - Register channels in the database.
  - Test channel connectivity.
  - Update channel metadata on demand.

YouTube Data API quota cost:
  - channels.list: 1 unit per call.
  - Calls are batched where possible.

Important limitations:
  - The YouTube Data API does not expose Brand Account channels in the
    same way as the primary account. After OAuth the user may only see
    their primary channel. Additional Brand Account channels can be
    accessed by logging in with the Brand Account's own Google account.
  - Subscriber counts may be hidden for channels that have privacy
    enabled on that field.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.extensions import db
from app.models import (
    ConnectedAccount,
    YouTubeChannel,
    ChannelRole,
    Notification,
    NotificationType,
)
from app.services.google_auth import get_valid_credentials

logger = logging.getLogger(__name__)

# YouTube API service name and version
_YT_SERVICE = "youtube"
_YT_VERSION = "v3"


# ------------------------------------------------------------------ #
# Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _build_youtube_service(connected_account: ConnectedAccount):
    """
    Build an authenticated YouTube API service for the given account.
    Returns (service, error_message).
    """
    creds = get_valid_credentials(connected_account)
    if creds is None:
        return None, "Could not obtain valid credentials. Please reconnect this account."

    # Commit any token refresh that happened inside get_valid_credentials.
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        service = build(_YT_SERVICE, _YT_VERSION, credentials=creds)
        return service, None
    except Exception as exc:
        logger.error("Failed to build YouTube service: %s", exc)
        return None, str(exc)


def _channel_row_from_api_item(item: dict) -> dict:
    """Extract a flat dict of channel fields from a channels.list API item."""
    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})
    content_details = item.get("contentDetails", {})

    # Highest-resolution thumbnail available
    thumbnails = snippet.get("thumbnails", {})
    thumb_url = (
        thumbnails.get("high", {}).get("url")
        or thumbnails.get("medium", {}).get("url")
        or thumbnails.get("default", {}).get("url")
    )

    # Uploads playlist: the channel's UC... ID becomes UU...
    uploads_playlist = (
        content_details.get("relatedPlaylists", {}).get("uploads")
    )

    sub_count = statistics.get("subscriberCount")
    sub_count = int(sub_count) if sub_count and not statistics.get("hiddenSubscriberCount") else None

    return {
        "channel_id": item["id"],
        "channel_name": snippet.get("title", "Unknown Channel"),
        "channel_url": f"https://www.youtube.com/channel/{item['id']}",
        "thumbnail_url": thumb_url,
        "subscriber_count": sub_count,
        "uploads_playlist_id": uploads_playlist,
    }


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def fetch_channels_for_account(
    connected_account: ConnectedAccount,
) -> Tuple[List[dict], Optional[str]]:
    """
    Fetch all YouTube channels accessible to this ConnectedAccount.

    Returns:
        (list_of_channel_dicts, error_message_or_None)
        Each dict has the keys from _channel_row_from_api_item().
    """
    service, err = _build_youtube_service(connected_account)
    if err:
        return [], err

    channels = []
    try:
        # "mine=True" returns the channels owned by the authenticated user.
        request = service.channels().list(
            part="snippet,contentDetails,statistics",
            mine=True,
            maxResults=50,
        )
        while request is not None:
            response = request.execute()
            for item in response.get("items", []):
                channels.append(_channel_row_from_api_item(item))
            request = service.channels().list_next(request, response)
    except HttpError as exc:
        error_msg = f"YouTube API error: {exc.resp.status} {exc.error_details}"
        logger.error("fetch_channels_for_account: %s", error_msg)
        return [], error_msg
    except Exception as exc:
        logger.error("fetch_channels_for_account unexpected error: %s", exc)
        return [], str(exc)

    return channels, None


def register_channel(
    connected_account: ConnectedAccount,
    channel_data: dict,
    role: ChannelRole = ChannelRole.DESTINATION,
) -> Tuple[Optional[YouTubeChannel], Optional[str]]:
    """
    Register (or update) a YouTube channel in the database.

    If the channel_id already exists, the existing row is updated with
    fresh metadata and the function returns the existing row.

    Args:
        connected_account: The ConnectedAccount that owns this channel.
        channel_data: Dict from _channel_row_from_api_item().
        role: The role to assign (source/destination/both).

    Returns:
        (YouTubeChannel, None) on success, (None, error_message) on failure.
    """
    try:
        existing = YouTubeChannel.query.filter_by(
            channel_id=channel_data["channel_id"]
        ).first()

        if existing:
            # Update metadata
            existing.channel_name = channel_data["channel_name"]
            existing.channel_url = channel_data["channel_url"]
            existing.thumbnail_url = channel_data["thumbnail_url"]
            existing.subscriber_count = channel_data["subscriber_count"]
            existing.uploads_playlist_id = channel_data["uploads_playlist_id"]
            existing.updated_at = datetime.now(timezone.utc)
            existing.is_active = True
            channel = existing
            logger.info("Updated existing channel: %s", channel.channel_name)
        else:
            channel = YouTubeChannel(
                connected_account_id=connected_account.id,
                channel_id=channel_data["channel_id"],
                channel_name=channel_data["channel_name"],
                channel_url=channel_data["channel_url"],
                thumbnail_url=channel_data["thumbnail_url"],
                subscriber_count=channel_data["subscriber_count"],
                uploads_playlist_id=channel_data["uploads_playlist_id"],
                role=role,
                is_active=True,
            )
            db.session.add(channel)
            logger.info("Registered new channel: %s", channel.channel_name)

        db.session.commit()
        return channel, None

    except Exception as exc:
        db.session.rollback()
        logger.error("register_channel error: %s", exc)
        return None, str(exc)


def test_channel_connectivity(
    connected_account: ConnectedAccount,
    channel_id: str,
) -> Tuple[bool, str]:
    """
    Test that we can make a successful authenticated call for this channel.

    Returns:
        (True, success_message) or (False, error_message)
    """
    service, err = _build_youtube_service(connected_account)
    if err:
        return False, err

    try:
        resp = service.channels().list(
            part="snippet",
            id=channel_id,
            maxResults=1,
        ).execute()

        if resp.get("items"):
            name = resp["items"][0]["snippet"].get("title", channel_id)
            return True, f"Connected successfully to: {name}"
        return False, "Channel not found or not accessible with these credentials."
    except HttpError as exc:
        return False, f"YouTube API error: {exc.resp.status} {exc.error_details}"
    except Exception as exc:
        return False, str(exc)


def refresh_channel_metadata(channel: YouTubeChannel) -> Tuple[bool, str]:
    """
    Re-fetch and update metadata for a single channel.

    Returns:
        (True, message) or (False, error_message)
    """
    account = channel.connected_account
    service, err = _build_youtube_service(account)
    if err:
        return False, err

    try:
        resp = service.channels().list(
            part="snippet,contentDetails,statistics",
            id=channel.channel_id,
            maxResults=1,
        ).execute()

        items = resp.get("items", [])
        if not items:
            return False, "Channel not found in YouTube API response."

        data = _channel_row_from_api_item(items[0])
        channel.channel_name = data["channel_name"]
        channel.channel_url = data["channel_url"]
        channel.thumbnail_url = data["thumbnail_url"]
        channel.subscriber_count = data["subscriber_count"]
        channel.uploads_playlist_id = data["uploads_playlist_id"]
        channel.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        return True, f"Metadata refreshed for {channel.channel_name}"

    except HttpError as exc:
        db.session.rollback()
        return False, f"YouTube API error: {exc.resp.status} {exc.error_details}"
    except Exception as exc:
        db.session.rollback()
        return False, str(exc)


def disconnect_channel(
    channel: YouTubeChannel,
    revoke_token: bool = False,
) -> Tuple[bool, str]:
    """
    Mark a channel as inactive (soft-delete) and optionally revoke its token.

    Does NOT hard-delete the channel row so that history records remain intact.

    Args:
        channel: The YouTubeChannel to disconnect.
        revoke_token: If True, attempt to revoke the OAuth token with Google.
                      Only revoke if this is the last channel using that account.

    Returns:
        (True, message) or (False, error_message)
    """
    from app.services.google_auth import revoke_credentials

    try:
        channel.is_active = False
        channel.monitoring_enabled = False
        channel.updated_at = datetime.now(timezone.utc)

        account = channel.connected_account
        # Revoke only if this is the last active channel on the account
        if revoke_token and account:
            other_active = YouTubeChannel.query.filter(
                YouTubeChannel.connected_account_id == account.id,
                YouTubeChannel.channel_id != channel.channel_id,
                YouTubeChannel.is_active == True,
            ).count()
            if other_active == 0:
                revoke_credentials(account)
                account.is_active = False

        db.session.commit()
        return True, f"{channel.channel_name} disconnected."
    except Exception as exc:
        db.session.rollback()
        logger.error("disconnect_channel error: %s", exc)
        return False, str(exc)


def get_user_info_from_credentials(creds) -> Optional[dict]:
    """
    Fetch the authenticated user's basic profile (name, email, picture)
    using the OAuth2 userinfo endpoint.

    Returns:
        dict with keys: id, email, name, picture  — or None on failure.
    """
    import requests as req_lib

    if not creds or not creds.token:
        return None
    try:
        resp = req_lib.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "id": data.get("sub"),
                "email": data.get("email"),
                "name": data.get("name"),
                "picture": data.get("picture"),
            }
        logger.warning("userinfo returned %s", resp.status_code)
        return None
    except Exception as exc:
        logger.error("get_user_info_from_credentials: %s", exc)
        return None
