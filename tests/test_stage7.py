"""
tests/test_stage7.py - Comprehensive automated tests for Stage 7 (Resumable Upload Engine & Upload Queue).

Covers:
  1. Resumable Chunked Upload Engine:
     - Multi-chunk streaming via MediaFileUpload
     - Progress percent tracking and database commits
     - Video ID and YouTube URL storage
     - Custom thumbnail upload via thumbnails().set()
     - State transitions (PENDING -> UPLOADING -> COMPLETED)
     - Associated video status update to UPLOADED
     - Immutable PublishingHistory recording
  2. Failure & Quota Handling:
     - Missing master video file -> AWAITING_FILE
     - Inactive destination channel or disconnected account -> FAILED
     - Google API Quota Exceeded (HTTP 403 quotaExceeded)
     - Transient failure retry increment and PENDING reset
     - Permanent FAILED status when max retries exceeded
     - Failed job recording in PublishingHistory
  3. Upload Job Control Functions:
     - retry_job (resets status and retry count)
     - pause_job & resume_job
     - cancel_job
     - Authorization checks (multi-tenant boundary protection)
  4. Web Routes & JSON Status API:
     - GET /uploads/ (queue table, stats badges, filter by status and channel)
     - GET /uploads/history (publishing history audit log)
     - POST /uploads/<id>/run (sync execution in test mode)
     - POST /uploads/<id>/retry
     - POST /uploads/<id>/pause
     - POST /uploads/<id>/resume
     - POST /uploads/<id>/cancel
     - POST /uploads/retry-all-failed
     - GET /uploads/api/status (JSON polling endpoint)
     - GET /uploads/api/<id> (JSON job detail)
"""

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.extensions import db as _db
from app.models import (
    ConnectedAccount,
    Notification,
    PrivacyStatus,
    PublishingHistory,
    SourceType,
    UploadJob,
    UploadStatus,
    User,
    Video,
    VideoStatus,
    YouTubeChannel,
)
from app.services import upload_service
from config import DevelopmentConfig


class Stage7TestConfig(DevelopmentConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "stage7-test-secret-key"
    TOKEN_ENCRYPTION_KEY = ""


@pytest.fixture(scope="session")
def app():
    application = create_app(config_override=Stage7TestConfig)
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
        u = User.query.filter_by(username="stage7user").first()
        if not u:
            u = User(
                username="stage7user",
                email="stage7user@repostflow.local",
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


@pytest.fixture()
def dummy_video_file(tmp_path):
    video_file = tmp_path / "test_video_upload.mp4"
    video_file.write_bytes(b"dummy mp4 video binary content for resumable testing")
    return str(video_file)


@pytest.fixture()
def dummy_thumb_file(tmp_path):
    thumb_file = tmp_path / "test_thumb.jpg"
    thumb_file.write_bytes(b"dummy jpeg binary content")
    return str(thumb_file)


@pytest.fixture()
def fixture_data(app, db, user_id, dummy_video_file, dummy_thumb_file):
    with app.app_context():
        # Connected account
        account = ConnectedAccount.query.filter_by(google_account_id="stage7_google_act").first()
        if not account:
            account = ConnectedAccount(
                user_id=user_id,
                google_account_id="stage7_google_act",
                email="stage7@gmail.com",
                display_name="Stage7 Tester",
                encrypted_credentials="dummy_enc_creds",
                token_expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
                is_active=True,
            )
            _db.session.add(account)
            _db.session.flush()

        # Destination YouTube channel
        channel = YouTubeChannel.query.filter_by(channel_id="UC_stage7_dest").first()
        if not channel:
            channel = YouTubeChannel(
                connected_account_id=account.id,
                channel_id="UC_stage7_dest",
                channel_name="Stage7 Destination Channel",
                channel_url="https://youtube.com/@stage7dest",
                is_active=True,
            )
            _db.session.add(channel)
            _db.session.flush()

        # Video
        video = Video(
            source_type=SourceType.LOCAL_FOLDER,
            original_filename="test_video_upload.mp4",
            local_file_path=dummy_video_file,
            local_thumbnail_path=dummy_thumb_file,
            title="Stage 7 Master Video",
            description="High quality master description",
            tags='["tag1", "tag2"]',
            privacy_status=PrivacyStatus.PRIVATE,
            status=VideoStatus.READY,
        )
        _db.session.add(video)
        _db.session.commit()

        return {
            "account_id": account.id,
            "channel_id": channel.id,
            "video_id": video.id,
        }


# ------------------------------------------------------------------ #
# 1. Resumable Upload Execution Tests                                 #
# ------------------------------------------------------------------ #

class TestResumableUploadExecution:
    def test_successful_resumable_upload_pipeline(self, app, fixture_data):
        with app.app_context():
            # Create UploadJob
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                upload_title="Published Title",
                upload_description="Published Description",
                upload_tags='["tagA", "tagB"]',
                upload_privacy=PrivacyStatus.PRIVATE,
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

        # Setup mock YouTube client
        mock_service = MagicMock()
        mock_insert = MagicMock()

        # Simulate chunk streaming: chunk 1 (50% progress), chunk 2 (100% complete)
        chunk1_status = MagicMock()
        chunk1_status.progress.return_value = 0.50

        mock_insert.next_chunk.side_effect = [
            (chunk1_status, None),
            (None, {"id": "yt_success_id_999"}),
        ]
        mock_service.videos().insert.return_value = mock_insert

        # Mock thumbnails set
        mock_thumb_set = MagicMock()
        mock_service.thumbnails().set.return_value = mock_thumb_set

        with patch("app.services.upload_service.get_youtube_client", return_value=(mock_service, None)), \
             patch("app.services.upload_service.MediaFileUpload") as mock_media:

            mock_media.return_value = MagicMock()

            with app.app_context():
                success, err = upload_service.execute_upload_job(job_id)
                assert success is True
                assert err is None

                updated_job = _db.session.get(UploadJob, job_id)
                assert updated_job.status == UploadStatus.COMPLETED
                assert updated_job.progress_percent == 100
                assert updated_job.destination_video_id == "yt_success_id_999"
                assert updated_job.destination_video_url == "https://youtu.be/yt_success_id_999"
                assert updated_job.completed_at is not None

                # Associated Video status should now be UPLOADED
                video = _db.session.get(Video, fixture_data["video_id"])
                assert video.status == VideoStatus.UPLOADED

                # Immutable PublishingHistory record must exist
                history = PublishingHistory.query.filter_by(upload_job_id=job_id).first()
                assert history is not None
                assert history.upload_status == "completed"
                assert history.destination_video_id == "yt_success_id_999"
                assert history.destination_video_url == "https://youtu.be/yt_success_id_999"
                assert history.video_title == "Published Title"

    def test_missing_local_file_marks_awaiting_file(self, app, fixture_data):
        with app.app_context():
            # Create video with non-existent path
            video_missing = Video(
                source_type=SourceType.LOCAL_FOLDER,
                original_filename="ghost.mp4",
                local_file_path="/invalid/nonexistent/ghost.mp4",
                title="Ghost Video",
                status=VideoStatus.AWAITING_FILE,
            )
            _db.session.add(video_missing)
            _db.session.flush()

            job = UploadJob(
                video_id=video_missing.id,
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                upload_title="Ghost Job",
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

            success, err = upload_service.execute_upload_job(job_id)
            assert success is False
            assert "missing on disk" in err

            updated_job = _db.session.get(UploadJob, job_id)
            assert updated_job.status == UploadStatus.AWAITING_FILE

    def test_inactive_channel_fails_gracefully(self, app, fixture_data):
        with app.app_context():
            # Mark channel inactive
            channel = _db.session.get(YouTubeChannel, fixture_data["channel_id"])
            channel.is_active = False

            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=channel.id,
                status=UploadStatus.PENDING,
                upload_title="Inactive Channel Test",
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

            success, err = upload_service.execute_upload_job(job_id)
            assert success is False
            assert "inactive" in err.lower()

            updated_job = _db.session.get(UploadJob, job_id)
            assert updated_job.status == UploadStatus.FAILED

            # Re-enable channel
            channel.is_active = True
            _db.session.commit()

    def test_quota_exceeded_handling(self, app, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                upload_title="Quota Test",
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

        mock_resp = MagicMock()
        mock_resp.status = 403
        mock_resp.reason = "quotaExceeded"
        mock_http_err = HttpError(resp=mock_resp, content=b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}')

        mock_service = MagicMock()
        mock_service.videos().insert.side_effect = mock_http_err

        with patch("app.services.upload_service.get_youtube_client", return_value=(mock_service, None)), \
             patch("app.services.upload_service.MediaFileUpload"):

            with app.app_context():
                success, err = upload_service.execute_upload_job(job_id)
                assert success is False
                assert "quota exceeded" in err.lower()

                updated_job = _db.session.get(UploadJob, job_id)
                assert updated_job.retry_count == 1
                assert "quota" in updated_job.error_message.lower()

    def test_transient_failure_and_permanent_exhaustion(self, app, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                upload_title="Retry Test",
                max_retries=2,
                retry_count=0,
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

        mock_service = MagicMock()
        mock_service.videos().insert.side_effect = RuntimeError("Temporary network socket timeout")

        with patch("app.services.upload_service.get_youtube_client", return_value=(mock_service, None)), \
             patch("app.services.upload_service.MediaFileUpload"):

            with app.app_context():
                # Attempt 1: Should increment retry to 1 and stay PENDING
                success1, err1 = upload_service.execute_upload_job(job_id)
                assert success1 is False
                job1 = _db.session.get(UploadJob, job_id)
                assert job1.status == UploadStatus.PENDING
                assert job1.retry_count == 1

                # Attempt 2: Should increment retry to 2 (max_retries reached) and transition to FAILED
                success2, err2 = upload_service.execute_upload_job(job_id)
                assert success2 is False
                job2 = _db.session.get(UploadJob, job_id)
                assert job2.status == UploadStatus.FAILED
                assert job2.retry_count == 2

                # Permanent failure must be recorded in PublishingHistory
                failed_hist = PublishingHistory.query.filter_by(
                    upload_job_id=job_id, upload_status="failed"
                ).first()
                assert failed_hist is not None


# ------------------------------------------------------------------ #
# 2. Upload Job Control Functions Tests                               #
# ------------------------------------------------------------------ #

class TestUploadJobActions:
    def test_retry_job_resets_counter_and_status(self, app, fixture_data, user_id):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.FAILED,
                retry_count=3,
                error_message="Fatal crash",
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

            success, err = upload_service.retry_job(job_id, user_id)
            assert success is True
            assert err is None

            updated = _db.session.get(UploadJob, job_id)
            assert updated.status == UploadStatus.PENDING
            assert updated.retry_count == 0
            assert updated.error_message is None

    def test_pause_and_resume_job(self, app, fixture_data, user_id):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

            # Pause
            success, err = upload_service.pause_job(job_id, user_id)
            assert success is True
            assert _db.session.get(UploadJob, job_id).status == UploadStatus.PAUSED

            # Resume
            success_res, err_res = upload_service.resume_job(job_id, user_id)
            assert success_res is True
            assert _db.session.get(UploadJob, job_id).status == UploadStatus.PENDING

    def test_cancel_job_deletes_record(self, app, fixture_data, user_id):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

            success, err = upload_service.cancel_job(job_id, user_id)
            assert success is True
            assert _db.session.get(UploadJob, job_id) is None

    def test_job_actions_security_unauthorized_user(self, app, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.FAILED,
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

            # User ID 999999 does not own this channel/job
            success, err = upload_service.retry_job(job_id, user_id=999999)
            assert success is False
            assert "access denied" in err.lower()


# ------------------------------------------------------------------ #
# 3. Web Route & JSON API Tests                                       #
# ------------------------------------------------------------------ #

class TestUploadRoutes:
    def test_get_uploads_index_authenticated(self, auth_client):
        resp = auth_client.get("/uploads/")
        assert resp.status_code == 200
        assert b"Upload Queue" in resp.data
        assert b"Jobs in Queue" in resp.data

    def test_get_uploads_filtered_by_status(self, auth_client):
        resp = auth_client.get("/uploads/?status=pending")
        assert resp.status_code == 200

    def test_get_publishing_history(self, auth_client):
        resp = auth_client.get("/uploads/history")
        assert resp.status_code == 200
        assert b"Publishing History" in resp.data
        assert b"Publishing Records" in resp.data

    def test_post_run_job_sync(self, app, auth_client, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                upload_title="Run Job Route Test",
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

        with patch("app.routes.uploads.execute_upload_job", return_value=(True, None)):
            resp = auth_client.post(f"/uploads/{job_id}/run", data={"sync": "true"}, follow_redirects=True)
            assert resp.status_code == 200
            assert b"uploaded successfully" in resp.data

    def test_post_retry_job_route(self, app, auth_client, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.FAILED,
                retry_count=3,
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

        resp = auth_client.post(f"/uploads/{job_id}/retry", follow_redirects=True)
        assert resp.status_code == 200
        assert b"reset to PENDING" in resp.data

    def test_post_pause_and_resume_routes(self, app, auth_client, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

        resp_pause = auth_client.post(f"/uploads/{job_id}/pause", follow_redirects=True)
        assert resp_pause.status_code == 200
        assert b"has been paused" in resp_pause.data

        resp_resume = auth_client.post(f"/uploads/{job_id}/resume", follow_redirects=True)
        assert resp_resume.status_code == 200
        assert b"resumed and set to PENDING" in resp_resume.data

    def test_post_cancel_job_route(self, app, auth_client, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

        resp = auth_client.post(f"/uploads/{job_id}/cancel", follow_redirects=True)
        assert resp.status_code == 200
        assert b"cancelled and removed" in resp.data

    def test_post_retry_all_failed(self, app, auth_client, fixture_data):
        with app.app_context():
            job1 = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.FAILED,
                error_message="err1",
                idempotency_key="retry_all_1",
            )
            job2 = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.FAILED,
                error_message="err2",
                idempotency_key="retry_all_2",
            )
            _db.session.add_all([job1, job2])
            _db.session.commit()

        resp = auth_client.post("/uploads/retry-all-failed", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Reset" in resp.data
        assert b"failed jobs back to PENDING" in resp.data

    def test_post_bulk_action_route(self, app, auth_client, fixture_data):
        with app.app_context():
            job1 = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                idempotency_key="bulk_act_1",
            )
            job2 = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                idempotency_key="bulk_act_2",
            )
            _db.session.add_all([job1, job2])
            _db.session.commit()
            j1_id, j2_id = job1.id, job2.id

        resp = auth_client.post(
            "/uploads/bulk-action",
            data={"action": "pause", "job_ids": [str(j1_id), str(j2_id)]},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Updated 2 job(s)" in resp.data

        with app.app_context():
            assert _db.session.get(UploadJob, j1_id).status == UploadStatus.PAUSED
            assert _db.session.get(UploadJob, j2_id).status == UploadStatus.PAUSED

    def test_bulk_action_delete_jobs(self, app, auth_client, fixture_data):
        with app.app_context():
            job1 = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                upload_title="To Bulk Delete 1",
                idempotency_key="del_job_1",
            )
            job2 = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.FAILED,
                upload_title="To Bulk Delete 2",
                idempotency_key="del_job_2",
            )
            _db.session.add_all([job1, job2])
            _db.session.commit()
            j1_id, j2_id = job1.id, job2.id

        resp = auth_client.post(
            "/uploads/bulk-action",
            data={"action": "delete", "job_ids": [str(j1_id), str(j2_id)]},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Permanently deleted 2 upload job(s)" in resp.data

        with app.app_context():
            assert _db.session.get(UploadJob, j1_id) is None
            assert _db.session.get(UploadJob, j2_id) is None

    def test_api_status_json(self, app, auth_client, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.UPLOADING,
                progress_percent=42,
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

        resp = auth_client.get("/uploads/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "active_jobs" in data
        matching = [j for j in data["active_jobs"] if j["id"] == job_id]
        assert len(matching) == 1
        assert matching[0]["progress_percent"] == 42
        assert matching[0]["status"] == "uploading"

    def test_api_job_detail_json(self, app, auth_client, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                upload_title="API Detail Video",
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

        resp = auth_client.get(f"/uploads/api/{job_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == job_id
        assert data["video_title"] == "API Detail Video"
        assert data["status"] == "pending"

    def test_execute_upload_job_prevents_duplicate_upload(self, app, fixture_data):
        with app.app_context():
            from app.models import PublishingHistory, ApplicationSetting
            ApplicationSetting.set("prevent_duplicate_uploads", "true")

            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                upload_title="Test Duplicate Prevention Execution",
                idempotency_key="dupe_test_key",
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

            # Add completed PublishingHistory for this video on this channel
            history = PublishingHistory(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                video_title="Test Duplicate Prevention Execution",
                upload_status="completed",
                destination_video_id="existing_yt_id_777",
                destination_video_url="https://youtu.be/existing_yt_id_777",
            )
            _db.session.add(history)
            _db.session.commit()

        # Mock YouTube client to verify it is NEVER called
        mock_service = MagicMock()

        with patch("app.services.upload_service.get_youtube_client", return_value=(mock_service, None)):
            with app.app_context():
                success, err = upload_service.execute_upload_job(job_id)
                # Should return False with duplicate prevented message
                assert success is False
                assert "Duplicate upload prevented" in err

                # YouTube insert MUST NOT have been called
                mock_service.videos().insert.assert_not_called()

                # Job status must be COMPLETED (marked as skipped)
                reloaded = _db.session.get(UploadJob, job_id)
                assert reloaded.status == UploadStatus.COMPLETED
                assert reloaded.progress_percent == 100
                assert "Skipped: Duplicate upload prevented" in reloaded.error_message
                assert reloaded.destination_video_id == "existing_yt_id_777"

    def test_execute_upload_job_concurrent_execution_blocked(self, app, fixture_data):
        with app.app_context():
            job = UploadJob(
                video_id=fixture_data["video_id"],
                destination_channel_id=fixture_data["channel_id"],
                status=UploadStatus.PENDING,
                upload_title="Concurrency Test Video",
                idempotency_key="concurrency_key_1",
            )
            _db.session.add(job)
            _db.session.commit()
            job_id = job.id

            from app.services.upload_service import _active_job_ids

            # Simulate another thread already executing this job
            _active_job_ids.add(job_id)
            try:
                success, err = upload_service.execute_upload_job(job_id)
                assert success is False
                assert "already in progress" in err.lower()
            finally:
                _active_job_ids.discard(job_id)

    def test_thumbnail_upload_with_retry_on_404(self, tmp_path):
        from app.services.upload_service import upload_video_thumbnail_with_retry
        from googleapiclient.errors import HttpError
        from unittest.mock import MagicMock

        dummy_thumb = tmp_path / "test_thumb.jpg"
        dummy_thumb.write_bytes(b"dummy image data")

        mock_service = MagicMock()
        mock_resp_404 = MagicMock()
        mock_resp_404.status = 404
        err_404 = HttpError(resp=mock_resp_404, content=b'{"error": {"message": "videoNotFound"}}')

        mock_set = MagicMock()
        # Fail on attempt 1 with 404, succeed on attempt 2
        mock_set.execute.side_effect = [err_404, {"url": "https://img.youtube.com/thumb_set.jpg"}]
        mock_service.thumbnails().set.return_value = mock_set

        ok, err = upload_video_thumbnail_with_retry(
            service=mock_service,
            video_id="test_yt_vid_123",
            thumbnail_path=str(dummy_thumb),
            max_retries=3,
            initial_delay=0.01,
        )
        assert ok is True
        assert err is None
        assert mock_set.execute.call_count == 2

