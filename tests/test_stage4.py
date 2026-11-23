"""
tests/test_stage4.py - Comprehensive automated tests for Stage 4 (Local Folder Monitoring & Watchdog).

Covers:
  1. File stability detector (wait_for_file_stability)
  2. Watchdog Event Handler (VideoFolderEventHandler: extension filtering, ignore temp files)
  3. FolderMonitorManager (start, stop, add_watch, remove_watch)
  4. Manual directory rescan (rescan_folder, duplicate prevention)
  5. Settings routes (general preferences update, add folder, toggle, rescan, delete)
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
    User,
    WatchedFolder,
    Video,
    ApplicationSetting,
    SourceType,
)
from app.services import folder_monitor as fm_service
from app.services.folder_monitor import (
    FolderMonitorManager,
    VideoFolderEventHandler,
    rescan_folder,
    wait_for_file_stability,
)
from config import DevelopmentConfig


class Stage4TestConfig(DevelopmentConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "stage4-test-secret-key"
    TOKEN_ENCRYPTION_KEY = ""


@pytest.fixture(scope="session")
def app():
    application = create_app(config_override=Stage4TestConfig)
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
        u = User.query.filter_by(username="stage4user").first()
        if not u:
            u = User(
                username="stage4user",
                email="stage4user@repostflow.local",
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
# 1. File Stability Tests                                            #
# ------------------------------------------------------------------ #

class TestFileStability:
    def test_nonexistent_file(self):
        assert wait_for_file_stability("C:/nonexistent_file_xyz.mp4", timeout=0.5) is False

    def test_empty_file_timeout(self, tmp_path):
        empty_f = tmp_path / "zero_bytes.mp4"
        empty_f.touch()
        # Empty file should fail stability check because size == 0
        assert wait_for_file_stability(str(empty_f), required_stable_checks=2, check_interval=0.1, timeout=0.5) is False

    def test_stable_file_success(self, tmp_path):
        stable_f = tmp_path / "valid_stable.mp4"
        stable_f.write_bytes(b"\x00" * 4096)
        # Should succeed in 2 consecutive checks
        assert wait_for_file_stability(str(stable_f), required_stable_checks=2, check_interval=0.05, timeout=2.0) is True


# ------------------------------------------------------------------ #
# 2. Watchdog Event Handler Tests                                    #
# ------------------------------------------------------------------ #

class TestEventHandler:
    def test_should_handle_filtering(self, app):
        handler = VideoFolderEventHandler(
            app=app,
            folder_id=1,
            allowed_extensions={".mp4", ".mov", ".mkv", ".avi", ".webm"},
        )
        # Valid video extensions
        assert handler._should_handle("C:/Videos/clip.mp4") is True
        assert handler._should_handle("C:/Videos/render.MOV") is True
        assert handler._should_handle("C:/Videos/movie.mkv") is True

        # Non-video files
        assert handler._should_handle("C:/Videos/notes.txt") is False
        assert handler._should_handle("C:/Videos/image.jpg") is False

        # Temporary and hidden files
        assert handler._should_handle("C:/Videos/.hidden.mp4") is False
        assert handler._should_handle("C:/Videos/~temp_render.mp4") is False
        assert handler._should_handle("C:/Videos/download.mp4.tmp") is False
        assert handler._should_handle("C:/Videos/video.mp4.part") is False


# ------------------------------------------------------------------ #
# 3. Rescan Folder Tests                                             #
# ------------------------------------------------------------------ #

class TestRescanFolder:
    def test_rescan_nonexistent_folder_id(self, app, db):
        with app.app_context():
            count, err = rescan_folder(folder_id=99999)
            assert count == 0
            assert "not found" in err.lower()

    def test_rescan_valid_folder(self, app, db, user_id, tmp_path):
        watch_dir = tmp_path / "my_renders"
        watch_dir.mkdir()

        # Place 2 dummy video files
        (watch_dir / "vid1.mp4").write_bytes(b"\x00" * 1024)
        (watch_dir / "vid2.mkv").write_bytes(b"\x00" * 1024)
        (watch_dir / "readme.txt").write_text("ignore me")

        fake_meta = {
            "duration_seconds": 45.0,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "codec": "h264",
            "file_size_bytes": 1024,
        }

        with app.app_context():
            folder = WatchedFolder(
                user_id=user_id,
                path=str(watch_dir.resolve()),
                label="Render Folder",
                is_active=True,
                recursive=False,
            )
            _db.session.add(folder)
            _db.session.commit()
            f_id = folder.id

            with patch("app.services.video_service.validate_video_file", return_value=(True, None, fake_meta)), \
                 patch("app.services.video_service.generate_video_thumbnail", return_value=("storage/thumbnails/t.jpg", None)):

                added, err = rescan_folder(f_id, app)
                assert err is None
                assert added == 2

                # Immediate second rescan: duplicate prevention should return 0 new additions
                added_again, err2 = rescan_folder(f_id, app)
                assert added_again == 0


# ------------------------------------------------------------------ #
# 4. Folder Monitor Manager Tests                                    #
# ------------------------------------------------------------------ #

class TestFolderMonitorManager:
    def test_manager_lifecycle(self, app, tmp_path):
        mgr = FolderMonitorManager()
        watch_dir = tmp_path / "watched_live"
        watch_dir.mkdir()

        mgr.start(app)
        assert mgr._observer is not None

        # Add watch
        ok = mgr.add_watch(101, str(watch_dir.resolve()), recursive=False)
        assert ok is True
        assert 101 in mgr._watches

        # Remove watch
        rem = mgr.remove_watch(101)
        assert rem is True
        assert 101 not in mgr._watches

        mgr.stop()
        assert mgr._observer is None


# ------------------------------------------------------------------ #
# 5. Settings & Watched Folder Routes Tests                          #
# ------------------------------------------------------------------ #

class TestSettingsRoutes:
    def test_settings_index(self, auth_client):
        resp = auth_client.get("/settings/")
        assert resp.status_code == 200
        assert b"Settings &amp; Automation Config" in resp.data
        assert b"Watched Folders" in resp.data
        assert b"Application Preferences" in resp.data

    def test_update_general_preferences(self, auth_client, app, db):
        resp = auth_client.post(
            "/settings/general",
            data={
                "app_name": "RepostFlow Pro",
                "default_privacy": "unlisted",
                "max_concurrent_uploads": "4",
                "youtube_poll_interval": "600",
                "auto_copy_metadata": "true",
                "auto_copy_thumbnail": "false",
                "max_upload_retries": "5",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Application settings saved successfully" in resp.data

        with app.app_context():
            assert ApplicationSetting.get("app_name") == "RepostFlow Pro"
            assert ApplicationSetting.get("default_privacy") == "unlisted"
            assert ApplicationSetting.get("max_concurrent_uploads") == "4"

    def test_add_folder_invalid_path(self, auth_client):
        resp = auth_client.post(
            "/settings/folders/add",
            data={"path": "C:/invalid_nonexistent_directory_abc123"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"does not exist on this machine" in resp.data

    def test_add_folder_valid(self, auth_client, app, db, user_id, tmp_path):
        target_dir = tmp_path / "user_auto_export"
        target_dir.mkdir()

        with patch("app.services.folder_monitor.rescan_folder", return_value=(0, None)), \
             patch.object(fm_service.folder_monitor, "add_watch", return_value=True):

            resp = auth_client.post(
                "/settings/folders/add",
                data={
                    "path": str(target_dir.resolve()),
                    "label": "Auto Export Direct",
                    "recursive": "1",
                },
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert b"added successfully" in resp.data

        with app.app_context():
            saved = WatchedFolder.query.filter_by(path=str(target_dir.resolve())).first()
            assert saved is not None
            assert saved.label == "Auto Export Direct"
            assert saved.recursive is True

    def test_toggle_folder_monitoring(self, auth_client, app, db, user_id):
        with app.app_context():
            f = WatchedFolder.query.filter_by(user_id=user_id).first()
            assert f is not None
            f_id = f.id
            init_state = f.is_active

        with patch.object(fm_service.folder_monitor, "remove_watch", return_value=True), \
             patch.object(fm_service.folder_monitor, "add_watch", return_value=True):

            resp = auth_client.post(f"/settings/folders/{f_id}/toggle", follow_redirects=True)
            assert resp.status_code == 200
            assert b"Monitoring" in resp.data

            with app.app_context():
                reloaded = _db.session.get(WatchedFolder, f_id)
                assert reloaded.is_active != init_state

    def test_rescan_route(self, auth_client, app, db, user_id):
        with app.app_context():
            f = WatchedFolder.query.filter_by(user_id=user_id).first()
            f_id = f.id

        with patch("app.routes.settings.rescan_folder", return_value=(3, None)):
            resp = auth_client.post(f"/settings/folders/{f_id}/rescan", follow_redirects=True)
            assert resp.status_code == 200
            assert b"Found and registered 3 new video(s)" in resp.data

    def test_delete_folder_route(self, auth_client, app, db, user_id):
        with app.app_context():
            f = WatchedFolder.query.filter_by(user_id=user_id).first()
            f_id = f.id

        with patch.object(fm_service.folder_monitor, "remove_watch", return_value=True):
            resp = auth_client.post(f"/settings/folders/{f_id}/delete", follow_redirects=True)
            assert resp.status_code == 200
            assert b"removed" in resp.data

            with app.app_context():
                assert _db.session.get(WatchedFolder, f_id) is None
