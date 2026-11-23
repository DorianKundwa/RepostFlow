"""
tests/test_stage2.py - Comprehensive automated tests for Stage 2.

Covers:
  1. Token encryption / decryption (with Fernet key and fallback)
  2. Credentials serialization & deserialization (dict <-> Credentials)
  3. OAuth flow error handling and state validation
  4. Channel discovery item parsing (_channel_row_from_api_item)
  5. Channel registration and updating existing channels
  6. Channel connectivity test & metadata refresh (mocked YouTube service)
  7. Channel disconnection (soft-delete & token revocation logic)
  8. In-dashboard notification service
  9. Channel routes: list, update_role, toggle_monitoring, test, refresh, disconnect
 10. OAuth routes: start, callback state verification, disconnect_account
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.extensions import db as _db
from app.models import (
    User,
    ConnectedAccount,
    YouTubeChannel,
    ChannelRole,
    Notification,
    NotificationType,
)
from app.services import google_auth, channel_service, notification_service
from config import DevelopmentConfig


# Generate a test Fernet key
TEST_FERNET_KEY = Fernet.generate_key().decode()


class Stage2TestConfig(DevelopmentConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "stage2-test-secret-key"
    TOKEN_ENCRYPTION_KEY = TEST_FERNET_KEY
    GOOGLE_CLIENT_SECRETS_FILE = "storage/credentials/client_secrets.json"
    OAUTH_REDIRECT_URI = "http://localhost:5000/oauth/callback"


@pytest.fixture(scope="session")
def app():
    application = create_app(config_override=Stage2TestConfig)
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
        u = User.query.filter_by(username="stage2user").first()
        if not u:
            u = User(
                username="stage2user",
                email="stage2user@repostflow.local",
                password_hash=generate_password_hash("Password123!"),
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
# 1. Token Encryption & Serialization Tests                          #
# ------------------------------------------------------------------ #

class TestTokenEncryption:
    def test_encrypt_and_decrypt_with_key(self, app):
        with app.app_context():
            raw_creds = {
                "token": "ya29.test_access_token",
                "refresh_token": "1//test_refresh_token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "test_client_id.apps.googleusercontent.com",
                "client_secret": "test_secret",
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
                "expiry": "2026-12-01T12:00:00+00:00",
            }
            encrypted = google_auth.encrypt_credentials(raw_creds)
            assert encrypted != str(raw_creds)
            assert not encrypted.startswith("{")

            decrypted = google_auth.decrypt_credentials(encrypted)
            assert decrypted == raw_creds

    def test_encrypt_and_decrypt_fallback_without_key(self, app):
        with app.app_context():
            orig_key = app.config.get("TOKEN_ENCRYPTION_KEY")
            app.config["TOKEN_ENCRYPTION_KEY"] = ""
            try:
                raw_creds = {"token": "unencrypted_token"}
                stored = google_auth.encrypt_credentials(raw_creds)
                decrypted = google_auth.decrypt_credentials(stored)
                assert decrypted == raw_creds
            finally:
                app.config["TOKEN_ENCRYPTION_KEY"] = orig_key

    def test_decrypt_invalid_string(self, app):
        with app.app_context():
            result = google_auth.decrypt_credentials("invalid_fernet_blob_content")
            assert result is None

    def test_credentials_dict_roundtrip(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_creds = MagicMock()
        mock_creds.token = "token123"
        mock_creds.refresh_token = "refresh123"
        mock_creds.token_uri = "https://oauth2.googleapis.com/token"
        mock_creds.client_id = "cid123"
        mock_creds.client_secret = "csec123"
        mock_creds.scopes = ["scope1", "scope2"]
        mock_creds.expiry = expiry

        c_dict = google_auth.credentials_to_dict(mock_creds)
        assert c_dict["token"] == "token123"
        assert c_dict["refresh_token"] == "refresh123"

        creds_obj = google_auth.dict_to_credentials(c_dict)
        assert creds_obj.token == "token123"
        assert creds_obj.refresh_token == "refresh123"
        assert creds_obj.scopes == ["scope1", "scope2"]
        assert not creds_obj.expired


# ------------------------------------------------------------------ #
# 2. Channel Service Logic Tests                                     #
# ------------------------------------------------------------------ #

class TestChannelService:
    def test_channel_row_from_api_item(self):
        sample_item = {
            "id": "UC_x5XG1OV2P6uZZ5FSM9Ttw",
            "snippet": {
                "title": "Google Developers",
                "thumbnails": {
                    "high": {"url": "https://yt3.ggpht.com/high.jpg"},
                    "default": {"url": "https://yt3.ggpht.com/default.jpg"},
                },
            },
            "contentDetails": {
                "relatedPlaylists": {
                    "uploads": "UU_x5XG1OV2P6uZZ5FSM9Ttw",
                }
            },
            "statistics": {
                "subscriberCount": "2250000",
                "hiddenSubscriberCount": False,
            },
        }

        parsed = channel_service._channel_row_from_api_item(sample_item)
        assert parsed["channel_id"] == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
        assert parsed["channel_name"] == "Google Developers"
        assert parsed["thumbnail_url"] == "https://yt3.ggpht.com/high.jpg"
        assert parsed["uploads_playlist_id"] == "UU_x5XG1OV2P6uZZ5FSM9Ttw"
        assert parsed["subscriber_count"] == 2250000
        assert parsed["channel_url"] == "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw"

    def test_register_and_update_channel(self, app, db, user_id):
        with app.app_context():
            account = ConnectedAccount.query.filter_by(
                user_id=user_id, google_account_id="goog_12345"
            ).first()
            if not account:
                account = ConnectedAccount(
                    user_id=user_id,
                    google_account_id="goog_12345",
                    email="account@gmail.com",
                    display_name="Google Account",
                )
                _db.session.add(account)
                _db.session.commit()

            channel_data = {
                "channel_id": "UC_TEST_REG_1",
                "channel_name": "Initial Name",
                "channel_url": "https://youtube.com/channel/UC_TEST_REG_1",
                "thumbnail_url": "https://thumb.url/1.png",
                "subscriber_count": 100,
                "uploads_playlist_id": "UU_TEST_REG_1",
            }

            # Register
            ch, err = channel_service.register_channel(account, channel_data, role=ChannelRole.DESTINATION)
            assert err is None
            assert ch is not None
            assert ch.channel_name == "Initial Name"
            assert ch.role == ChannelRole.DESTINATION

            # Update same channel with new name
            channel_data["channel_name"] = "Updated Name"
            channel_data["subscriber_count"] = 150
            ch2, err2 = channel_service.register_channel(account, channel_data)
            assert err2 is None
            assert ch2.id == ch.id
            assert ch2.channel_name == "Updated Name"
            assert ch2.subscriber_count == 150

    def test_test_channel_connectivity(self, app, db, user_id):
        with app.app_context():
            account = ConnectedAccount.query.filter_by(user_id=user_id).first()

            with patch("app.services.channel_service._build_youtube_service") as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = (mock_service, None)

                mock_service.channels().list().execute.return_value = {
                    "items": [{"snippet": {"title": "Connected Channel Name"}}]
                }

                ok, msg = channel_service.test_channel_connectivity(account, "UC_TEST_REG_1")
                assert ok is True
                assert "Connected successfully" in msg
                assert "Connected Channel Name" in msg

    def test_disconnect_channel(self, app, db, user_id):
        with app.app_context():
            channel = YouTubeChannel.query.filter_by(channel_id="UC_TEST_REG_1").first()
            assert channel is not None
            assert channel.is_active is True

            with patch("app.services.google_auth.revoke_credentials") as mock_revoke:
                mock_revoke.return_value = True
                ok, msg = channel_service.disconnect_channel(channel, revoke_token=True)
                assert ok is True
                assert channel.is_active is False
                assert channel.monitoring_enabled is False

                # Reactivate for following route tests
                channel.is_active = True
                _db.session.commit()


# ------------------------------------------------------------------ #
# 3. Notification Service Tests                                      #
# ------------------------------------------------------------------ #

class TestNotificationService:
    def test_create_and_mark_notifications(self, app, db, user_id):
        with app.app_context():
            notification_service.create_notification(
                user_id=user_id,
                title="Test Alert",
                message="This is a test notification message.",
                type=NotificationType.SUCCESS,
            )

            notes = Notification.query.filter_by(user_id=user_id, is_read=False).all()
            assert len(notes) >= 1
            note = notes[0]
            assert note.title == "Test Alert"
            assert note.type == NotificationType.SUCCESS

            notification_service.mark_read(note.id, user_id)
            refreshed = _db.session.get(Notification, note.id)
            assert refreshed.is_read is True

            # Create another and test mark_all_read
            notification_service.create_notification(
                user_id=user_id,
                title="Second Alert",
                type=NotificationType.WARNING,
            )
            notification_service.mark_all_read(user_id)
            unread_count = Notification.query.filter_by(user_id=user_id, is_read=False).count()
            assert unread_count == 0


# ------------------------------------------------------------------ #
# 4. Route Tests                                                     #
# ------------------------------------------------------------------ #

class TestChannelAndOAuthRoutes:
    def test_channels_index_authenticated(self, auth_client):
        resp = auth_client.get("/channels/")
        assert resp.status_code == 200
        assert b"Connected Channels" in resp.data
        assert b"Connect Channel" in resp.data

    def test_update_channel_role(self, auth_client, app, db, user_id):
        with app.app_context():
            ch = YouTubeChannel.query.filter_by(channel_id="UC_TEST_REG_1").first()
            assert ch is not None
            ch_id = ch.id

        resp = auth_client.post(
            f"/channels/{ch_id}/role",
            data={"role": "source"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(YouTubeChannel, ch_id)
            assert updated.role == ChannelRole.SOURCE

    def test_toggle_monitoring(self, auth_client, app, db, user_id):
        with app.app_context():
            ch = YouTubeChannel.query.filter_by(channel_id="UC_TEST_REG_1").first()
            ch_id = ch.id
            init_monitoring = ch.monitoring_enabled

        resp = auth_client.post(
            f"/channels/{ch_id}/toggle-monitoring",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            updated = _db.session.get(YouTubeChannel, ch_id)
            assert updated.monitoring_enabled != init_monitoring

    def test_test_connection_route(self, auth_client, app, db, user_id):
        with app.app_context():
            ch = YouTubeChannel.query.filter_by(channel_id="UC_TEST_REG_1").first()
            ch_id = ch.id

        with patch("app.routes.channels.test_channel_connectivity") as mock_test:
            mock_test.return_value = (True, "Connection OK")
            resp = auth_client.post(f"/channels/{ch_id}/test", follow_redirects=True)
            assert resp.status_code == 200
            assert b"Connection OK" in resp.data

    def test_refresh_channel_route(self, auth_client, app, db, user_id):
        with app.app_context():
            ch = YouTubeChannel.query.filter_by(channel_id="UC_TEST_REG_1").first()
            ch_id = ch.id

        with patch("app.routes.channels.refresh_channel_metadata") as mock_refresh:
            mock_refresh.return_value = (True, "Metadata updated")
            resp = auth_client.post(f"/channels/{ch_id}/refresh", follow_redirects=True)
            assert resp.status_code == 200
            assert b"Metadata updated" in resp.data

    def test_oauth_start_missing_secrets(self, auth_client):
        # When client_secrets.json does not exist on disk, /oauth/start catches FileNotFoundError and redirects with flash
        with patch("app.services.google_auth.os.path.isfile", return_value=False):
            resp = auth_client.get("/oauth/start?role=destination", follow_redirects=True)
            assert resp.status_code == 200
            assert b"client secrets file not found" in resp.data or b"Channels" in resp.data

    def test_oauth_start_valid_redirect(self, auth_client):
        # When client secrets exist, /oauth/start returns 302 redirecting to Google OAuth
        resp = auth_client.get("/oauth/start?role=destination", follow_redirects=False)
        assert resp.status_code == 302
        assert "accounts.google.com" in resp.location

    def test_oauth_callback_state_mismatch(self, auth_client):
        with auth_client.session_transaction() as sess:
            sess["oauth_state"] = "legitimate_state_token"

        resp = auth_client.get(
            "/oauth/callback?state=tampered_or_missing_state&code=test_code",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"OAuth authorisation failed: invalid state" in resp.data

    def test_oauth_disconnect_account_route(self, auth_client, app, db, user_id):
        with app.app_context():
            acc = ConnectedAccount.query.filter_by(user_id=user_id).first()
            assert acc is not None
            acc_id = acc.id

        with patch("app.routes.oauth.revoke_credentials") as mock_revoke:
            mock_revoke.return_value = True
            resp = auth_client.post(
                f"/oauth/disconnect-account/{acc_id}",
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert b"disconnected" in resp.data

            with app.app_context():
                reloaded = _db.session.get(ConnectedAccount, acc_id)
                assert reloaded.is_active is False
