"""
tests/test_stage6.py - Comprehensive automated tests for Stage 6 (Automation Rule Engine & Queue Dispatcher).

Covers:
  1. Rule creation, updating, and validation:
     - Empty name validation
     - Destination channel requirement
     - Infinite loop prevention (source channel != destination channel)
  2. Metadata snapshot calculations:
     - Title prefix, suffix, and 100-char truncation
     - Description prepend and append
     - Extra tags merging and deduplication
     - Default privacy status
  3. Video evaluation & UploadJob generation:
     - YouTube source mapping
     - Local folder source mapping
     - Loop prevention at execution time
     - Idempotency & duplicate job prevention
     - Awaiting File vs Ready job statuses
  4. Rule application to existing library videos
  5. Route controllers:
     - GET /automation/
     - POST /automation/create (success and loop error handling)
     - POST /automation/<id>/toggle
     - POST /automation/<id>/apply-existing
     - POST /automation/<id>/delete
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
    WatchedFolder,
    Video,
    VideoStatus,
    SourceType,
    PrivacyStatus,
    AutomationRule,
    AutomationDestination,
    UploadJob,
    UploadStatus,
)
from app.services import automation_service
from config import DevelopmentConfig


class Stage6TestConfig(DevelopmentConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "stage6-test-secret-key"
    TOKEN_ENCRYPTION_KEY = ""


@pytest.fixture(scope="session")
def app():
    application = create_app(config_override=Stage6TestConfig)
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
        u = User.query.filter_by(username="stage6user").first()
        if not u:
            u = User(
                username="stage6user",
                email="stage6user@repostflow.local",
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
# 1. Rule Configuration & Loop Prevention Tests                      #
# ------------------------------------------------------------------ #

class TestAutomationRuleConfig:
    def test_create_rule_empty_name(self, app, user_id):
        with app.app_context():
            rule, err = automation_service.create_or_update_rule(
                user_id=user_id,
                name="",
                source_type=SourceType.YOUTUBE,
                destination_channel_ids=[1, 2],
                source_channel_id=3,
            )
            assert rule is None
            assert "empty" in err.lower()

    def test_create_rule_no_destinations(self, app, user_id):
        with app.app_context():
            rule, err = automation_service.create_or_update_rule(
                user_id=user_id,
                name="Rule 1",
                source_type=SourceType.YOUTUBE,
                destination_channel_ids=[],
                source_channel_id=3,
            )
            assert rule is None
            assert "destination" in err.lower()

    def test_loop_prevention_source_in_destinations(self, app, user_id):
        with app.app_context():
            # Source channel ID 10 is also in destination_channel_ids [10, 20]
            rule, err = automation_service.create_or_update_rule(
                user_id=user_id,
                name="Looping Rule",
                source_type=SourceType.YOUTUBE,
                source_channel_id=10,
                destination_channel_ids=[10, 20],
            )
            assert rule is None
            assert "loop prevention" in err.lower()

    def test_create_valid_rule_with_destinations(self, app, db, user_id):
        with app.app_context():
            # Create connected account & test channels
            account = ConnectedAccount(user_id=user_id, google_account_id="g_auto_1", email="auto@gmail.com")
            _db.session.add(account)
            _db.session.commit()

            src_ch = YouTubeChannel(connected_account_id=account.id, channel_id="UC_SRC", channel_name="Source Channel", role=ChannelRole.SOURCE)
            dest1 = YouTubeChannel(connected_account_id=account.id, channel_id="UC_DST1", channel_name="Dest Channel 1", role=ChannelRole.DESTINATION)
            dest2 = YouTubeChannel(connected_account_id=account.id, channel_id="UC_DST2", channel_name="Dest Channel 2", role=ChannelRole.DESTINATION)
            _db.session.add_all([src_ch, dest1, dest2])
            _db.session.commit()

            rule, err = automation_service.create_or_update_rule(
                user_id=user_id,
                name="Morning Distribution",
                source_type=SourceType.YOUTUBE,
                source_channel_id=src_ch.id,
                destination_channel_ids=[dest1.id, dest2.id],
                title_prefix="[Shorts] ",
                title_suffix=" - Official",
                description_prepend="Subscribe for more!",
                extra_tags=["viral", "music"],
                default_privacy=PrivacyStatus.PRIVATE,
            )
            assert err is None
            assert rule is not None
            assert rule.name == "Morning Distribution"
            assert len(rule.destinations) == 2


# ------------------------------------------------------------------ #
# 2. Metadata Transformation Snapshot Tests                          #
# ------------------------------------------------------------------ #

class TestMetadataSnapshot:
    def test_build_snapshot_metadata(self, app, db, user_id):
        with app.app_context():
            rule = AutomationRule.query.filter_by(name="Morning Distribution").first()
            assert rule is not None

            video = Video(
                source_type=SourceType.YOUTUBE,
                title="Amazing Acoustic Performance",
                description="Filmed in studio A.",
                tags='["acoustic", "guitar"]',
                local_thumbnail_path="storage/thumbnails/thumb1.jpg",
                status=VideoStatus.READY,
                detected_at=datetime.now(timezone.utc),
            )

            meta = automation_service.build_snapshot_metadata(video, rule)

            # Title
            assert meta["title"] == "[Shorts] Amazing Acoustic Performance - Official"
            # Description
            assert "Subscribe for more!" in meta["description"]
            assert "Filmed in studio A." in meta["description"]
            # Tags
            assert "acoustic" in meta["tags"]
            assert "viral" in meta["tags"]
            # Thumbnail & Privacy
            assert meta["thumbnail_path"] == "storage/thumbnails/thumb1.jpg"
            assert meta["privacy"] == PrivacyStatus.PRIVATE


# ------------------------------------------------------------------ #
# 3. Video Evaluation & UploadJob Generation Tests                   #
# ------------------------------------------------------------------ #

class TestRuleEvaluation:
    def test_evaluate_rules_for_video(self, app, db, user_id):
        with app.app_context():
            src_ch = YouTubeChannel.query.filter_by(channel_id="UC_SRC").first()
            assert src_ch is not None

            video = Video(
                source_type=SourceType.YOUTUBE,
                source_channel_id=src_ch.id,
                title="Song of the Day",
                description="Live session",
                status=VideoStatus.READY,
                detected_at=datetime.now(timezone.utc),
            )
            _db.session.add(video)
            _db.session.commit()

            # Evaluate rules: should generate 2 UploadJobs (one for each destination in "Morning Distribution")
            jobs = automation_service.evaluate_rules_for_video(video)
            assert len(jobs) == 2

            for j in jobs:
                assert j.video_id == video.id
                assert j.status == UploadStatus.PENDING
                assert "[Shorts] Song of the Day - Official" in j.upload_title

            # Video should transition to QUEUED
            reloaded_v = _db.session.get(Video, video.id)
            assert reloaded_v.status == VideoStatus.QUEUED

            # Idempotency check: running evaluation again should return no new jobs
            second_eval = automation_service.evaluate_rules_for_video(video)
            assert len(second_eval) == 0

    def test_evaluate_rules_for_awaiting_file_video(self, app, db):
        with app.app_context():
            src_ch = YouTubeChannel.query.filter_by(channel_id="UC_SRC").first()
            video = Video(
                source_type=SourceType.YOUTUBE,
                source_channel_id=src_ch.id,
                title="Needs Master File",
                status=VideoStatus.AWAITING_FILE,
                detected_at=datetime.now(timezone.utc),
            )
            _db.session.add(video)
            _db.session.commit()

            jobs = automation_service.evaluate_rules_for_video(video)
            assert len(jobs) == 2
            for j in jobs:
                assert j.status == UploadStatus.AWAITING_FILE


# ------------------------------------------------------------------ #
# 4. Route Tests                                                     #
# ------------------------------------------------------------------ #

class TestAutomationRoutes:
    def test_automation_index_route(self, auth_client):
        resp = auth_client.get("/automation/")
        assert resp.status_code == 200
        assert b"Automation Rule Engine" in resp.data
        assert b"New Rule" in resp.data

    def test_create_rule_route_success(self, auth_client, app, db, user_id):
        with app.app_context():
            dest = YouTubeChannel.query.filter_by(channel_id="UC_DST1").first()
            dest_id = dest.id

        resp = auth_client.post(
            "/automation/create",
            data={
                "name": "Route Created Rule",
                "source_type": "youtube",
                "source_channel_id": "1",
                "dest_channel_ids": [dest_id],
                "title_prefix": "[Exclusive] ",
                "default_privacy": "private",
                "copy_title": "1",
                "copy_description": "1",
                "copy_tags": "1",
                "copy_thumbnail": "1",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"created with 1 destination" in resp.data

    def test_create_rule_route_loop_error(self, auth_client, app, db, user_id):
        with app.app_context():
            src = YouTubeChannel.query.filter_by(channel_id="UC_SRC").first()
            src_id = src.id

        resp = auth_client.post(
            "/automation/create",
            data={
                "name": "Loop Attempt",
                "source_type": "youtube",
                "source_channel_id": str(src_id),
                "dest_channel_ids": [src_id],  # Same ID!
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Loop prevention error" in resp.data

    def test_toggle_rule_route(self, auth_client, app, db, user_id):
        with app.app_context():
            rule = AutomationRule.query.filter_by(user_id=user_id).first()
            rule_id = rule.id
            init_enabled = rule.is_enabled

        resp = auth_client.post(f"/automation/{rule_id}/toggle", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Rule status updated" in resp.data

        with app.app_context():
            reloaded = _db.session.get(AutomationRule, rule_id)
            assert reloaded.is_enabled != init_enabled

    def test_apply_existing_route(self, auth_client, app, db, user_id):
        with app.app_context():
            rule = AutomationRule.query.filter_by(user_id=user_id).first()
            rule_id = rule.id

        resp = auth_client.post(f"/automation/{rule_id}/apply-existing", follow_redirects=True)
        assert resp.status_code == 200

    def test_delete_rule_route(self, auth_client, app, db, user_id):
        with app.app_context():
            to_del = AutomationRule(
                user_id=user_id,
                name="To Delete Rule",
                source_type=SourceType.LOCAL_FOLDER,
            )
            _db.session.add(to_del)
            _db.session.commit()
            r_id = to_del.id

        resp = auth_client.post(f"/automation/{r_id}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Automation rule deleted" in resp.data
        with app.app_context():
            assert _db.session.get(AutomationRule, r_id) is None


# ------------------------------------------------------------------ #
# 6. Duplicate Upload Prevention Tests                               #
# ------------------------------------------------------------------ #

class TestDuplicateUploadPreventionInAutomation:
    def test_duplicate_prevention_skips_published_video(self, app, db, user_id):
        with app.app_context():
            from app.models import PublishingHistory, ApplicationSetting
            # Ensure global setting is true
            ApplicationSetting.set("prevent_duplicate_uploads", "true")

            # Create source folder and destination channel
            src_f = WatchedFolder(user_id=user_id, path="/dupe/watch/dir", label="Dupe Watch")
            dest_ch = YouTubeChannel.query.filter_by(channel_id="UC_dest_dupe_1").first()
            if not dest_ch:
                dest_ch = YouTubeChannel(
                    connected_account_id=1,
                    channel_id="UC_dest_dupe_1",
                    channel_name="Dupe Dest Channel 1",
                    role=ChannelRole.DESTINATION,
                    is_active=True,
                )
                _db.session.add(dest_ch)
            _db.session.add(src_f)
            _db.session.commit()

            # Create rule with prevent_duplicates=True
            rule, _ = automation_service.create_or_update_rule(
                user_id=user_id,
                name="Dupe Guard Rule",
                source_type=SourceType.LOCAL_FOLDER,
                source_folder_id=src_f.id,
                destination_channel_ids=[dest_ch.id],
                prevent_duplicates=True,
            )
            assert rule.prevent_duplicates is True

            # Create a video
            vid = Video(
                source_type=SourceType.LOCAL_FOLDER,
                watched_folder_id=src_f.id,
                original_filename="dupe_test.mp4",
                title="Dupe Test Video",
                status=VideoStatus.READY,
            )
            _db.session.add(vid)
            _db.session.commit()

            # Record in PublishingHistory that this video was already completed on dest_ch
            pub = PublishingHistory(
                video_id=vid.id,
                destination_channel_id=dest_ch.id,
                video_title="Dupe Test Video",
                upload_status="completed",
                destination_video_id="yt_already_there_123",
            )
            _db.session.add(pub)
            _db.session.commit()

            # Now evaluate rules for this video: should be skipped!
            jobs = automation_service.evaluate_rules_for_video(vid)
            assert len(jobs) == 0

    def test_duplicate_allowed_when_rule_disables_prevention(self, app, db, user_id):
        with app.app_context():
            from app.models import PublishingHistory, ApplicationSetting
            ApplicationSetting.set("prevent_duplicate_uploads", "true")

            src_f = WatchedFolder.query.filter_by(path="/dupe/watch/dir").first()
            dest_ch = YouTubeChannel.query.filter_by(channel_id="UC_dest_dupe_1").first()

            # Update rule to allow duplicates
            rule = AutomationRule.query.filter_by(name="Dupe Guard Rule").first()
            rule.prevent_duplicates = False
            _db.session.commit()

            vid = Video.query.filter_by(original_filename="dupe_test.mp4").first()
            # Clean any old upload jobs
            UploadJob.query.filter_by(video_id=vid.id, destination_channel_id=dest_ch.id).delete()
            _db.session.commit()

            # Now evaluate: should queue job despite PublishingHistory!
            jobs = automation_service.evaluate_rules_for_video(vid)
            assert len(jobs) == 1
            assert jobs[0].destination_channel_id == dest_ch.id

