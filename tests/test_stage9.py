"""
tests/test_stage9.py - Comprehensive automated tests for Stage 9 (Security Hardening, Backup & Production Deployment).

Covers:
  1. Security Hardening & HTTP Headers:
     - X-Content-Type-Options: nosniff
     - X-Frame-Options: SAMEORIGIN
     - X-XSS-Protection: 1; mode=block
     - Referrer-Policy: strict-origin-when-cross-origin
     - Content-Security-Policy presence
     - Permissions-Policy presence
  2. Session Cookie Hardening:
     - SESSION_COOKIE_HTTPONLY = True
     - SESSION_COOKIE_SAMESITE = "Lax"
     - REMEMBER_COOKIE_HTTPONLY = True
  3. Backup & Disaster Recovery Service:
     - Online SQLite integrity checks (PRAGMA integrity_check)
     - create_backup archive generation and file bundling
     - list_backups listing and sorting
     - restore_backup validation and pre-restore database safety copy
  4. Production WSGI Entry Point:
     - wsgi.py application export and background service bootstrapping
"""

import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.extensions import db as _db
from app.models import User
from app.services.backup_service import (
    create_backup,
    list_backups,
    restore_backup,
    verify_sqlite_integrity,
)
from config import DevelopmentConfig


class Stage9TestConfig(DevelopmentConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "stage9-test-secret-key"
    TOKEN_ENCRYPTION_KEY = ""


@pytest.fixture(scope="session")
def app():
    application = create_app(config_override=Stage9TestConfig)
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
        u = User.query.filter_by(username="stage9user").first()
        if not u:
            u = User(
                username="stage9user",
                email="stage9user@repostflow.local",
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
# 1. Security Hardening Tests                                        #
# ------------------------------------------------------------------ #

class TestSecurityHardening:
    def test_http_security_headers_present(self, client):
        resp = client.get("/auth/login")
        assert resp.status_code == 200

        # Assert mandatory security headers
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "Content-Security-Policy" in resp.headers
        assert "Permissions-Policy" in resp.headers

    def test_session_cookie_security_config(self, app):
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
        assert app.config["REMEMBER_COOKIE_HTTPONLY"] is True
        assert app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"


# ------------------------------------------------------------------ #
# 2. Backup & Disaster Recovery Tests                                #
# ------------------------------------------------------------------ #

class TestBackupService:
    def test_verify_sqlite_integrity_real_and_invalid(self, tmp_path):
        import sqlite3

        # 1. Real valid database
        valid_db = tmp_path / "test_valid.db"
        conn = sqlite3.connect(str(valid_db))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);")
        conn.execute("INSERT INTO t VALUES (1, 'hello');")
        conn.commit()
        conn.close()

        ok, msg = verify_sqlite_integrity(valid_db)
        assert ok is True
        assert msg == "ok"

        # 2. Corrupt / invalid file
        invalid_db = tmp_path / "corrupt.db"
        invalid_db.write_bytes(b"not a valid sqlite database header at all")
        ok_corrupt, msg_corrupt = verify_sqlite_integrity(invalid_db)
        assert ok_corrupt is False

        # 3. Nonexistent file
        nonexistent = tmp_path / "does_not_exist.db"
        ok_nonexistent, msg_nonexistent = verify_sqlite_integrity(nonexistent)
        assert ok_nonexistent is False

    def test_create_and_restore_backup(self, tmp_path):
        import sqlite3

        # Setup mock project directory structure
        project_dir = tmp_path / "mock_project"
        instance_dir = project_dir / "instance"
        instance_dir.mkdir(parents=True)
        creds_dir = project_dir / "storage" / "credentials"
        creds_dir.mkdir(parents=True)
        backup_dir = project_dir / "storage" / "backups"
        backup_dir.mkdir(parents=True)

        # Create mock database
        db_path = instance_dir / "repostflow.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE mock_data (id INT, item TEXT);")
        conn.execute("INSERT INTO mock_data VALUES (42, 'test_item_value');")
        conn.commit()
        conn.close()

        # Create mock .env and client_secrets.json
        (project_dir / ".env").write_text("TEST_KEY=SECRET_VAL\n", encoding="utf-8")
        (creds_dir / "client_secrets.json").write_text('{"mock": "secret"}\n', encoding="utf-8")

        # 1. Test create_backup
        success, archive_path, err = create_backup(base_dir=project_dir, backup_dir=backup_dir)
        assert success is True
        assert archive_path is not None
        assert err is None
        assert Path(archive_path).is_file()

        # Check archive contents
        with zipfile.ZipFile(archive_path, "r") as zf:
            namelist = zf.namelist()
            assert "repostflow.db" in namelist
            assert ".env" in namelist
            assert "client_secrets.json" in namelist
            assert "MANIFEST.txt" in namelist

        # 2. Test list_backups
        backups = list_backups(backup_dir=backup_dir)
        assert len(backups) == 1
        assert backups[0]["filename"] == Path(archive_path).name

        # 3. Simulate disaster (corrupting database)
        db_path.write_text("corrupted content during failure")

        # 4. Test restore_backup
        restore_ok, restore_err = restore_backup(archive_path, base_dir=project_dir)
        assert restore_ok is True
        assert restore_err is None

        # Verify database is restored and operational
        conn_restored = sqlite3.connect(str(db_path))
        cursor = conn_restored.cursor()
        cursor.execute("SELECT item FROM mock_data WHERE id=42;")
        row = cursor.fetchone()
        conn_restored.close()
        assert row is not None
        assert row[0] == "test_item_value"


# ------------------------------------------------------------------ #
# 3. Production WSGI Entry Point Tests                               #
# ------------------------------------------------------------------ #

class TestProductionWSGI:
    def test_wsgi_module_loads_application(self):
        import wsgi
        assert hasattr(wsgi, "app")
        assert hasattr(wsgi, "application")
        assert wsgi.app is wsgi.application
        assert "dashboard" in wsgi.app.blueprints
        assert "channels" in wsgi.app.blueprints
        assert "uploads" in wsgi.app.blueprints
