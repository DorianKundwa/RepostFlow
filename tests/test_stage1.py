"""
tests/test_stage1.py - Stage 1 smoke tests.

Tests:
1. App factory creates a valid Flask app.
2. All database tables are created.
3. Default ApplicationSettings are seeded.
4. Login page returns 200.
5. Register page returns 200 (when no users exist).
6. Protected routes redirect to login when unauthenticated.
7. User creation and login flow.
8. All registered blueprints are present.
"""

import pytest
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.extensions import db as _db
from app.models import (
    User, ApplicationSetting, YouTubeChannel,
    Video, UploadJob, PublishingHistory, WatchedFolder,
    AutomationRule, ConnectedAccount, Notification
)
from config import DevelopmentConfig
from werkzeug.security import generate_password_hash


# ---- Fixtures ----

class TestConfig(DevelopmentConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret-key"
    TOKEN_ENCRYPTION_KEY = ""


@pytest.fixture(scope="session")
def app():
    """Create application configured for testing."""
    application = create_app(config_override=TestConfig)
    with application.app_context():
        yield application


@pytest.fixture(scope="session")
def db(app):
    """Provide the test database."""
    with app.app_context():
        yield _db


@pytest.fixture()
def client(app):
    """Test client."""
    return app.test_client()


@pytest.fixture()
def app_ctx(app):
    """Push an application context for model tests."""
    with app.app_context():
        yield


# ---- Tests ----

class TestAppFactory:
    def test_app_created(self, app):
        assert app is not None

    def test_app_is_testing(self, app):
        assert app.config["TESTING"] is True

    def test_blueprints_registered(self, app):
        blueprint_names = set(app.blueprints.keys())
        expected = {"dashboard", "channels", "videos", "automation", "uploads", "settings", "auth"}
        assert expected.issubset(blueprint_names), \
            f"Missing blueprints: {expected - blueprint_names}"


class TestDatabase:
    def test_tables_exist(self, app, db):
        with app.app_context():
            inspector = _db.inspect(_db.engine)
            tables = set(inspector.get_table_names())
            expected_tables = {
                "user", "connected_account", "youtube_channel",
                "watched_folder", "video", "automation_rule",
                "automation_destination", "upload_job",
                "publishing_history", "application_setting", "notification"
            }
            assert expected_tables.issubset(tables), \
                f"Missing tables: {expected_tables - tables}"

    def test_default_settings_seeded(self, app, db):
        with app.app_context():
            count = ApplicationSetting.query.count()
            assert count >= 5, f"Expected at least 5 default settings, got {count}"

    def test_default_privacy_setting(self, app, db):
        with app.app_context():
            val = ApplicationSetting.get("default_privacy")
            assert val == "private"


class TestModels:
    def test_create_user(self, app, db):
        with app.app_context():
            u = User(
                username="testuser_model",
                email="testmodel@example.com",
                password_hash=generate_password_hash("Password123"),
            )
            _db.session.add(u)
            _db.session.commit()
            found = User.query.filter_by(username="testuser_model").first()
            assert found is not None
            assert found.email == "testmodel@example.com"

    def test_application_setting_set_get(self, app, db):
        with app.app_context():
            ApplicationSetting.set("test_key", "test_value", "A test setting")
            assert ApplicationSetting.get("test_key") == "test_value"

    def test_application_setting_default(self, app, db):
        with app.app_context():
            result = ApplicationSetting.get("nonexistent_key_xyz", default="fallback")
            assert result == "fallback"


class TestRoutes:
    def test_login_page_get(self, client):
        resp = client.get("/auth/login")
        assert resp.status_code == 200
        assert b"RepostFlow" in resp.data

    def test_register_page_accessible_when_no_users(self, client, app):
        with app.app_context():
            # Clear all users first
            User.query.delete()
            _db.session.commit()
        resp = client.get("/auth/register")
        assert resp.status_code == 200

    def test_dashboard_redirects_when_not_logged_in(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 401)

    def test_channels_redirects_when_not_logged_in(self, client):
        resp = client.get("/channels/", follow_redirects=False)
        assert resp.status_code in (302, 401)

    def test_videos_redirects_when_not_logged_in(self, client):
        resp = client.get("/videos/", follow_redirects=False)
        assert resp.status_code in (302, 401)

    def test_uploads_redirects_when_not_logged_in(self, client):
        resp = client.get("/uploads/", follow_redirects=False)
        assert resp.status_code in (302, 401)

    def test_settings_redirects_when_not_logged_in(self, client):
        resp = client.get("/settings/", follow_redirects=False)
        assert resp.status_code in (302, 401)


class TestUserAuthFlow:
    def test_register_and_login(self, client, app):
        with app.app_context():
            User.query.delete()
            _db.session.commit()

        # Register
        resp = client.post("/auth/register", data={
            "username": "admin",
            "email": "admin@repostflow.local",
            "password": "SecurePass1",
            "confirm_password": "SecurePass1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Overview" in resp.data or b"RepostFlow" in resp.data

    def test_dashboard_accessible_after_login(self, client, app):
        with app.app_context():
            User.query.delete()
            _db.session.commit()
            u = User(
                username="dashtest",
                email="dashtest@repostflow.local",
                password_hash=generate_password_hash("Password123"),
            )
            _db.session.add(u)
            _db.session.commit()

        resp = client.post("/auth/login", data={
            "username": "dashtest",
            "password": "Password123",
        }, follow_redirects=True)
        assert resp.status_code == 200
        # Dashboard content
        assert b"Overview" in resp.data or b"Connected Channels" in resp.data

    def test_logout(self, client, app):
        with app.app_context():
            User.query.delete()
            _db.session.commit()
            u = User(
                username="logouttest",
                email="logouttest@repostflow.local",
                password_hash=generate_password_hash("Password123"),
            )
            _db.session.add(u)
            _db.session.commit()

        client.post("/auth/login", data={
            "username": "logouttest",
            "password": "Password123",
        })
        resp = client.get("/auth/logout", follow_redirects=True)
        assert resp.status_code == 200
        assert b"logged out" in resp.data or b"Sign In" in resp.data
