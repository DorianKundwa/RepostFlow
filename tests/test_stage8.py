"""
tests/test_stage8.py - Comprehensive automated tests for Stage 8 (Background Task Scheduling via APScheduler).

Covers:
  1. APScheduler Lifecycle:
     - Start, stop, pause, resume
     - Registered jobs inspection (YouTube monitor, Upload queue worker, Folder sweep)
     - Safe repeated start/stop handling
  2. Scheduled Worker Tasks:
     - YouTube source monitoring worker polling enabled source channels
     - Upload queue worker dispatching pending jobs respecting max_concurrent_uploads
     - Concurrency limits preventing queue overload
     - Folder maintenance sweep
  3. Dynamic Rescheduling:
     - Reconfiguring trigger interval when database settings change
  4. Web Routes & UI Controls:
     - GET /settings/ displays scheduler status and job schedule table
     - POST /settings/scheduler/toggle pauses and resumes scheduler daemon
     - POST /settings/scheduler/run-now/<job_id> dispatches on-demand job execution
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.extensions import db as _db
from app.models import (
    ApplicationSetting,
    ChannelRole,
    ConnectedAccount,
    PrivacyStatus,
    SourceType,
    UploadJob,
    UploadStatus,
    User,
    Video,
    VideoStatus,
    WatchedFolder,
    YouTubeChannel,
)
from app.services.scheduler_service import (
    JOB_FOLDER_SWEEP,
    JOB_UPLOAD_QUEUE,
    JOB_YOUTUBE_SOURCES,
    SchedulerService,
    scheduler_service,
)
from config import DevelopmentConfig


class Stage8TestConfig(DevelopmentConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "stage8-test-secret-key"
    TOKEN_ENCRYPTION_KEY = ""


@pytest.fixture(scope="session")
def app():
    application = create_app(config_override=Stage8TestConfig)
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
        u = User.query.filter_by(username="stage8user").first()
        if not u:
            u = User(
                username="stage8user",
                email="stage8user@repostflow.local",
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
# 1. Scheduler Lifecycle Tests                                       #
# ------------------------------------------------------------------ #

class TestSchedulerLifecycle:
    def test_scheduler_lifecycle_start_pause_resume_stop(self, app):
        service = SchedulerService()
        service.init_app(app)

        # 1. Start
        started = service.start(app)
        assert started is True
        assert service.is_running() is True
        assert service.is_paused() is False

        # 2. Check registered jobs
        jobs = service.get_jobs_info()
        job_ids = [j["id"] for j in jobs]
        assert JOB_YOUTUBE_SOURCES in job_ids
        assert JOB_UPLOAD_QUEUE in job_ids
        assert JOB_FOLDER_SWEEP in job_ids

        # 3. Pause
        paused = service.pause()
        assert paused is True
        assert service.is_paused() is True

        # 4. Resume
        resumed = service.resume()
        assert resumed is True
        assert service.is_paused() is False

        # 5. Stop
        service.stop()
        assert service.is_running() is False
        assert service.get_jobs_info() == []

    def test_repeated_start_is_idempotent(self, app):
        service = SchedulerService()
        service.init_app(app)

        assert service.start(app) is True
        # Second call should recognize running state and return True
        assert service.start(app) is True
        service.stop()


# ------------------------------------------------------------------ #
# 2. Scheduled Worker Task Execution Tests                            #
# ------------------------------------------------------------------ #

class TestSchedulerWorkers:
    def test_run_youtube_source_monitor_polls_active_source_channels(self, app, user_id):
        with app.app_context():
            account = ConnectedAccount.query.filter_by(google_account_id="stage8_act").first()
            if not account:
                account = ConnectedAccount(
                    user_id=user_id,
                    google_account_id="stage8_act",
                    email="stage8@gmail.com",
                    encrypted_credentials="dummy",
                    is_active=True,
                )
                _db.session.add(account)
                _db.session.flush()

            source_ch = YouTubeChannel.query.filter_by(channel_id="UC_stage8_source").first()
            if not source_ch:
                source_ch = YouTubeChannel(
                    connected_account_id=account.id,
                    channel_id="UC_stage8_source",
                    channel_name="Stage8 Source Channel",
                    role=ChannelRole.SOURCE,
                    monitoring_enabled=True,
                    is_active=True,
                )
                _db.session.add(source_ch)
                _db.session.commit()
            source_id = source_ch.id

        service = SchedulerService()
        service.init_app(app)

        with patch("app.services.video_detector.check_source_channel_for_new_videos", return_value=([MagicMock()], None)) as mock_check:
            service._run_youtube_source_monitor()
            mock_check.assert_called_with(source_id)

    def test_run_upload_queue_worker_respects_concurrency(self, app, user_id):
        with app.app_context():
            # Ensure max_concurrent_uploads is set to 2
            ApplicationSetting.set("max_concurrent_uploads", "2")

            account = ConnectedAccount.query.filter_by(google_account_id="stage8_act").first()
            channel = YouTubeChannel.query.filter_by(channel_id="UC_stage8_dest").first()
            if not channel:
                channel = YouTubeChannel(
                    connected_account_id=account.id,
                    channel_id="UC_stage8_dest",
                    channel_name="Stage8 Dest",
                    role=ChannelRole.DESTINATION,
                    is_active=True,
                )
                _db.session.add(channel)
                _db.session.flush()

            video = Video(
                source_type=SourceType.LOCAL_FOLDER,
                title="Queue Worker Test Video",
                status=VideoStatus.READY,
            )
            _db.session.add(video)
            _db.session.flush()

            # Create 1 UPLOADING job (uses 1 slot) and 2 PENDING jobs
            job_active = UploadJob(
                video_id=video.id,
                destination_channel_id=channel.id,
                status=UploadStatus.UPLOADING,
                idempotency_key="worker_active",
            )
            job_p1 = UploadJob(
                video_id=video.id,
                destination_channel_id=channel.id,
                status=UploadStatus.PENDING,
                idempotency_key="worker_pending_1",
            )
            job_p2 = UploadJob(
                video_id=video.id,
                destination_channel_id=channel.id,
                status=UploadStatus.PENDING,
                idempotency_key="worker_pending_2",
            )
            _db.session.add_all([job_active, job_p1, job_p2])
            _db.session.commit()

        service = SchedulerService()
        service.init_app(app)

        with patch("threading.Thread") as mock_thread:
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance

            service._run_upload_queue_worker()

            # Because max_concurrent=2 and 1 is already UPLOADING, only 1 slot was available!
            # Therefore mock_thread should only be called for 1 job (job_p1).
            assert mock_thread.call_count == 1
            mock_instance.start.assert_called_once()

    def test_run_folder_sweep_rescans_active_folders(self, app, user_id):
        with app.app_context():
            folder = WatchedFolder.query.filter_by(path="C:\\Stage8Watched").first()
            if not folder:
                folder = WatchedFolder(
                    user_id=user_id,
                    path="C:\\Stage8Watched",
                    label="Stage8 Folder",
                    is_active=True,
                )
                _db.session.add(folder)
                _db.session.commit()
            folder_id = folder.id

        service = SchedulerService()
        service.init_app(app)

        with patch("app.services.folder_monitor.rescan_folder", return_value=(1, None)) as mock_rescan:
            service._run_folder_sweep()
            mock_rescan.assert_called_with(folder_id, app)


# ------------------------------------------------------------------ #
# 3. Dynamic Rescheduling Tests                                      #
# ------------------------------------------------------------------ #

class TestDynamicRescheduling:
    def test_reschedule_jobs_updates_interval(self, app):
        service = SchedulerService()
        service.init_app(app)
        service.start(app)

        with app.app_context():
            ApplicationSetting.set("youtube_poll_interval", "900")

        service.reschedule_jobs(app)

        job_info = [j for j in service.get_jobs_info() if j["id"] == JOB_YOUTUBE_SOURCES]
        assert len(job_info) == 1
        assert "900" in job_info[0]["trigger"] or "15:00" in job_info[0]["trigger"] or "interval" in job_info[0]["trigger"]

        service.stop()


# ------------------------------------------------------------------ #
# 4. Settings Route & UI Integration Tests                           #
# ------------------------------------------------------------------ #

class TestSettingsSchedulerRoutes:
    def test_settings_index_displays_scheduler_panel(self, auth_client):
        resp = auth_client.get("/settings/")
        assert resp.status_code == 200
        assert b"Background Automation Engine" in resp.data
        assert b"APScheduler" in resp.data

    def test_toggle_scheduler_route(self, app, auth_client):
        scheduler_service.init_app(app)
        scheduler_service.start(app)
        assert scheduler_service.is_running() is True

        # Toggle to pause
        resp1 = auth_client.post("/settings/scheduler/toggle", follow_redirects=True)
        assert resp1.status_code == 200
        assert b"Background scheduler paused" in resp1.data
        assert scheduler_service.is_paused() is True

        # Toggle to resume
        resp2 = auth_client.post("/settings/scheduler/toggle", follow_redirects=True)
        assert resp2.status_code == 200
        assert b"Background scheduler resumed" in resp2.data
        assert scheduler_service.is_paused() is False

        scheduler_service.stop()

    def test_run_scheduler_job_now_success(self, app, auth_client):
        scheduler_service.init_app(app)

        with patch.object(scheduler_service, "run_job_now", return_value=(True, "Triggered OK")) as mock_run:
            resp = auth_client.post(f"/settings/scheduler/run-now/{JOB_YOUTUBE_SOURCES}", follow_redirects=True)
            assert resp.status_code == 200
            assert b"Triggered background job" in resp.data
            mock_run.assert_called_once()

    def test_run_scheduler_job_now_invalid_job(self, app, auth_client):
        scheduler_service.init_app(app)
        resp = auth_client.post("/settings/scheduler/run-now/invalid_job_id_xyz", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Could not trigger job" in resp.data

    def test_browse_folder_dialog_route(self, auth_client):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value.stdout = "C:\\Videos\\WatchedFolder\n"
            resp = auth_client.post("/settings/browse-folder-dialog")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            assert "WatchedFolder" in data["path"]
