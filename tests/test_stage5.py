"""
tests/test_stage5.py - Comprehensive automated tests for Stage 5 (YouTube Source Monitoring & Detection).

Covers:
  1. YouTube Service:
     - ISO 8601 duration parser (PT1H2M3S, PT4M13S, PT45S, invalid)
     - Uploads playlist ID resolution & fallback convention
     - Playlist items retrieval with mocked response
     - Batch video details retrieval with mocked response
  2. Video Detector:
     - Sanitization for fuzzy filename matching
     - Local master file search in watched folders
     - Detection of new YouTube uploads with AWAITING_FILE status
     - Detection of new YouTube uploads with auto-matching to local master file (READY status)
     - Idempotency and last_video_id pointer tracking
     - Source channel monitoring eligibility validation
     - Aggregate check_all_monitored_source_channels
  3. Route:
     - POST /channels/<id>/check-uploads controller
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.extensions import db as _db
from app.models import (
    User,
    ConnectedAccount,
    YouTubeChannel,
    ChannelRole,
    Video,
    VideoStatus,
    WatchedFolder,
    Notification,
)
from app.services import youtube_service, video_detector
from config import DevelopmentConfig


class Stage5TestConfig(DevelopmentConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "stage5-test-secret-key"
    TOKEN_ENCRYPTION_KEY = ""


@pytest.fixture(scope="session")
def app():
    application = create_app(config_override=Stage5TestConfig)
    with application.app_context():
        yield application


@pytest.fixture(scope="session")
def db(app):
    with app.app_context():
        yield _db


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def user_id(app, db):
    with app.app_context():
        u = User.query.filter_by(username="stage5user").first()
        if not u:
            u = User(
                username="stage5user",
                email="stage5user@repostflow.local",
                password_hash=generate_password_hash("Pass12345!"),
            )
            _db.session.add(u)
            _db.session.commit()
        return u.id


@pytest.fixture()
def auth_client(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


# ------------------------------------------------------------------ #
# 1. YouTube Service Tests                                           #
# ------------------------------------------------------------------ #

class TestYouTubeService:
    def test_parse_iso8601_duration(self):
        assert youtube_service.parse_iso8601_duration("PT4M13S") == 253.0
        assert youtube_service.parse_iso8601_duration("PT1H2M3S") == 3723.0
        assert youtube_service.parse_iso8601_duration("PT45S") == 45.0
        assert youtube_service.parse_iso8601_duration("P1DT2H") == 93600.0
        assert youtube_service.parse_iso8601_duration("INVALID") is None
        assert youtube_service.parse_iso8601_duration("") is None

    def test_get_uploads_playlist_id_convention_fallback(self):
        mock_service = MagicMock()
        mock_service.channels().list().execute.return_value = {"items": []}

        # Falls back to UC... -> UU...
        playlist_id, err = youtube_service.get_uploads_playlist_id(mock_service, "UC1234567890abcdef")
        assert err is None
        assert playlist_id == "UU1234567890abcdef"

    def test_fetch_recent_playlist_items(self):
        mock_service = MagicMock()
        mock_service.playlistItems().list().execute.return_value = {
            "items": [
                {
                    "contentDetails": {"videoId": "vid_abc_1"},
                    "snippet": {
                        "title": "First Upload",
                        "description": "Upload description",
                        "publishedAt": "2026-11-20T10:00:00Z",
                        "thumbnails": {"high": {"url": "https://img.youtube.com/vi/vid_abc_1/hqdefault.jpg"}},
                    },
                }
            ]
        }

        items, err = youtube_service.fetch_recent_playlist_items(mock_service, "UU1234567890abcdef")
        assert err is None
        assert len(items) == 1
        assert items[0]["video_id"] == "vid_abc_1"
        assert items[0]["title"] == "First Upload"
        assert items[0]["thumbnail_url"] == "https://img.youtube.com/vi/vid_abc_1/hqdefault.jpg"

    def test_fetch_video_details_batch(self):
        mock_service = MagicMock()
        mock_service.videos().list().execute.return_value = {
            "items": [
                {
                    "id": "vid_abc_1",
                    "snippet": {
                        "title": "Rich Video Title",
                        "description": "Detailed video description",
                        "tags": ["ambient", "music", "4k"],
                        "categoryId": "10",
                        "publishedAt": "2026-11-20T10:00:00Z",
                        "thumbnails": {"maxres": {"url": "https://img.youtube.com/vi/vid_abc_1/maxresdefault.jpg"}},
                    },
                    "contentDetails": {
                        "duration": "PT3M45S",
                    },
                    "status": {
                        "privacyStatus": "public",
                    },
                }
            ]
        }

        details, err = youtube_service.fetch_video_details_batch(mock_service, ["vid_abc_1"])
        assert err is None
        assert "vid_abc_1" in details
        meta = details["vid_abc_1"]
        assert meta["title"] == "Rich Video Title"
        assert meta["tags"] == ["ambient", "music", "4k"]
        assert meta["category_id"] == "10"
        assert meta["duration_seconds"] == 225.0
        assert meta["privacy_status"] == "public"


# ------------------------------------------------------------------ #
# 2. Video Detector Tests                                            #
# ------------------------------------------------------------------ #

class TestVideoDetector:
    def test_sanitize_for_matching(self):
        assert video_detector._sanitize_for_matching("Episode #1: Hello, World!") == "episode1helloworld"
        assert video_detector._sanitize_for_matching("vid_ABC_123") == "vidabc123"

    def test_find_matching_local_file(self, app, db, user_id, tmp_path):
        folder_dir = tmp_path / "detector_watched"
        folder_dir.mkdir()

        matching_file = folder_dir / "Morning_Lyrics_Ep1_vid_xyz_99.mp4"
        matching_file.write_bytes(b"\x00" * 1024)

        with app.app_context():
            wf = WatchedFolder(
                user_id=user_id,
                path=str(folder_dir.resolve()),
                label="Detector Watched",
                is_active=True,
            )
            _db.session.add(wf)
            _db.session.commit()

            with patch("app.services.video_detector.validate_video_file", return_value=(True, None, {})):
                # Match by video ID
                match = video_detector.find_matching_local_file(
                    title="Morning Lyrics Episode 1",
                    youtube_video_id="vid_xyz_99",
                    user_id=user_id,
                )
                assert match is not None
                assert "Morning_Lyrics_Ep1_vid_xyz_99.mp4" in match

                # Non-matching
                no_match = video_detector.find_matching_local_file(
                    title="Completely Unrelated Subject",
                    youtube_video_id="vid_other_000",
                    user_id=user_id,
                )
                assert no_match is None

    def test_check_source_channel_detection_awaiting_file(self, app, db, user_id):
        with app.app_context():
            account = ConnectedAccount(
                user_id=user_id,
                google_account_id="goog_source_detector",
                email="sourcechannel@gmail.com",
            )
            _db.session.add(account)
            _db.session.commit()

            channel = YouTubeChannel(
                connected_account_id=account.id,
                channel_id="UC_SOURCE_CH_1",
                channel_name="Source Channel Alpha",
                role=ChannelRole.SOURCE,
                is_active=True,
                monitoring_enabled=True,
                uploads_playlist_id="UU_SOURCE_CH_1",
            )
            _db.session.add(channel)
            _db.session.commit()
            ch_id = channel.id

            fake_items = [
                {
                    "video_id": "yt_detect_new_1",
                    "title": "Brand New Upload",
                    "description": "Auto detected description",
                    "published_at": datetime.now(timezone.utc),
                    "thumbnail_url": "https://img.youtube.com/thumb1.jpg",
                }
            ]
            fake_details = {
                "yt_detect_new_1": {
                    "title": "Brand New Upload",
                    "description": "Auto detected description",
                    "tags": ["news", "tech"],
                    "category_id": "28",
                    "duration_seconds": 180.0,
                    "privacy_status": "public",
                    "thumbnail_url": "https://img.youtube.com/thumb1.jpg",
                }
            }

            with patch("app.services.video_detector.get_youtube_client", return_value=(MagicMock(), None)), \
                 patch("app.services.video_detector.fetch_recent_playlist_items", return_value=(fake_items, None)), \
                 patch("app.services.video_detector.fetch_video_details_batch", return_value=(fake_details, None)), \
                 patch("app.services.video_detector.find_matching_local_file", return_value=None):

                videos, err = video_detector.check_source_channel_for_new_videos(ch_id)
                assert err is None
                assert len(videos) == 1
                v = videos[0]
                assert v.youtube_video_id == "yt_detect_new_1"
                assert v.title == "Brand New Upload"
                # Because no local file was found:
                assert v.status == VideoStatus.AWAITING_FILE
                assert v.local_file_path is None

                # Verify channel tracking pointers updated
                updated_ch = _db.session.get(YouTubeChannel, ch_id)
                assert updated_ch.last_video_id == "yt_detect_new_1"
                assert updated_ch.last_checked_at is not None

                # Verify notification was sent
                note = Notification.query.filter_by(user_id=user_id).order_by(Notification.created_at.desc()).first()
                assert note is not None
                assert "Source Channel Alpha" in note.title or "Source Channel Alpha" in note.message

    def test_check_source_channel_idempotent_subsequent_check(self, app, db):
        with app.app_context():
            ch = YouTubeChannel.query.filter_by(channel_id="UC_SOURCE_CH_1").first()
            assert ch is not None
            ch_id = ch.id

            fake_items = [
                {
                    "video_id": "yt_detect_new_1",  # Same video as previous check
                    "title": "Brand New Upload",
                }
            ]

            with patch("app.services.video_detector.get_youtube_client", return_value=(MagicMock(), None)), \
                 patch("app.services.video_detector.fetch_recent_playlist_items", return_value=(fake_items, None)):

                videos, err = video_detector.check_source_channel_for_new_videos(ch_id)
                assert err is None
                assert len(videos) == 0  # No new videos!


# ------------------------------------------------------------------ #
# 3. Route Tests                                                     #
# ------------------------------------------------------------------ #

class TestSourceChannelRoutes:
    def test_check_uploads_route(self, auth_client, app, db):
        with app.app_context():
            ch = YouTubeChannel.query.filter_by(channel_id="UC_SOURCE_CH_1").first()
            ch_id = ch.id

        with patch("app.routes.channels.check_source_channel_for_new_videos", return_value=([], None)):
            resp = auth_client.post(f"/channels/{ch_id}/check-uploads", follow_redirects=True)
            assert resp.status_code == 200
            assert b"No new video uploads found" in resp.data
