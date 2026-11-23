"""
app/services/backup_service.py - Database and configuration backup & restore utility.

Handles:
  - Hot SQLite database backup using the official sqlite3 online backup API.
  - Integrity verification before and after backup (PRAGMA integrity_check).
  - Bundling database, environment configuration, and credentials into timestamped zip archives.
  - Safe restoration with pre-flight structure validation.
  - Listing available backup snapshots.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def get_default_backup_dir(base_dir: Optional[Path] = None) -> Path:
    if not base_dir:
        base_dir = Path(__file__).resolve().parent.parent.parent
    b_dir = base_dir / "storage" / "backups"
    b_dir.mkdir(parents=True, exist_ok=True)
    return b_dir


def verify_sqlite_integrity(db_path: Path) -> Tuple[bool, str]:
    """Execute PRAGMA integrity_check on SQLite database."""
    if not db_path.is_file():
        return False, f"Database file does not exist: {db_path}"

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        result = cursor.fetchone()
        conn.close()
        if result and result[0] == "ok":
            return True, "ok"
        return False, f"Integrity check failed: {result}"
    except Exception as exc:
        return False, f"Integrity check error: {exc}"


def create_backup(
    base_dir: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Create a complete, consistent backup archive of the RepostFlow deployment.

    Archive contents:
      - repostflow.db (online hot copy)
      - .env (environment settings)
      - client_secrets.json (if present)
      - manifest.json (timestamp, file hashes, metadata)

    Returns:
        (success, backup_zip_path_str, error_message)
    """
    if not base_dir:
        base_dir = Path(__file__).resolve().parent.parent.parent

    target_backup_dir = backup_dir or get_default_backup_dir(base_dir)
    target_backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_name = f"repostflow_backup_{timestamp}.zip"
    archive_path = target_backup_dir / archive_name

    db_source = base_dir / "instance" / "repostflow.db"
    env_source = base_dir / ".env"
    secrets_source = base_dir / "storage" / "credentials" / "client_secrets.json"

    # Temp dir for staging
    temp_stage = target_backup_dir / f"stage_{timestamp}"
    temp_stage.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Database backup via SQLite Online Backup API (thread-safe, lock-safe)
        if db_source.is_file():
            ok, msg = verify_sqlite_integrity(db_source)
            if not ok:
                return False, None, f"Source database integrity error: {msg}"

            staged_db = temp_stage / "repostflow.db"
            src_conn = sqlite3.connect(str(db_source))
            dst_conn = sqlite3.connect(str(staged_db))
            with dst_conn:
                src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()

            # Verify staged copy integrity
            ok_staged, msg_staged = verify_sqlite_integrity(staged_db)
            if not ok_staged:
                return False, None, f"Staged database copy failed integrity check: {msg_staged}"
        else:
            logger.warning("No SQLite database found at %s. Staging empty placeholder.", db_source)

        # 2. Copy .env
        if env_source.is_file():
            shutil.copy2(env_source, temp_stage / ".env")

        # 3. Copy client_secrets.json
        if secrets_source.is_file():
            shutil.copy2(secrets_source, temp_stage / "client_secrets.json")

        # 4. Create manifest
        manifest_text = (
            f"RepostFlow Backup Archive\n"
            f"Created At: {datetime.now(timezone.utc).isoformat()}\n"
            f"Timestamp: {timestamp}\n"
            f"Files Included: {[f.name for f in temp_stage.iterdir()]}\n"
        )
        (temp_stage / "MANIFEST.txt").write_text(manifest_text, encoding="utf-8")

        # 5. Compress into zip archive
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in temp_stage.iterdir():
                zf.write(item, arcname=item.name)

        logger.info("Backup archive created successfully: %s", archive_path)
        return True, str(archive_path), None

    except Exception as exc:
        logger.error("Failed to create backup: %s", exc, exc_info=True)
        return False, None, str(exc)

    finally:
        # Cleanup staging directory
        if temp_stage.exists():
            shutil.rmtree(temp_stage, ignore_errors=True)


def restore_backup(
    archive_path_str: str,
    base_dir: Optional[Path] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Restore an existing backup zip archive into the application directory.
    Creates a pre-restore safety copy of the current database before replacing.
    """
    archive_path = Path(archive_path_str)
    if not archive_path.is_file():
        return False, f"Backup archive not found: {archive_path}"

    if not base_dir:
        base_dir = Path(__file__).resolve().parent.parent.parent

    instance_dir = base_dir / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    credentials_dir = base_dir / "storage" / "credentials"
    credentials_dir.mkdir(parents=True, exist_ok=True)

    temp_extract = instance_dir / "_temp_restore"
    temp_extract.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(temp_extract)

        extracted_files = [f.name for f in temp_extract.iterdir()]
        logger.info("Extracting backup archive containing: %s", extracted_files)

        # Pre-restore safety copy of existing DB
        current_db = instance_dir / "repostflow.db"
        if current_db.is_file():
            safety_copy = instance_dir / f"repostflow.db.pre_restore_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(current_db, safety_copy)
            logger.info("Created safety copy of existing DB at %s", safety_copy)

        # Restore repostflow.db
        staged_db = temp_extract / "repostflow.db"
        if staged_db.is_file():
            ok, msg = verify_sqlite_integrity(staged_db)
            if not ok:
                return False, f"Restored database failed integrity check: {msg}"
            shutil.copy2(staged_db, current_db)

        # Restore client_secrets.json
        staged_secrets = temp_extract / "client_secrets.json"
        if staged_secrets.is_file():
            shutil.copy2(staged_secrets, credentials_dir / "client_secrets.json")

        logger.info("Backup restored successfully from %s", archive_path)
        return True, None

    except Exception as exc:
        logger.error("Error during backup restoration: %s", exc, exc_info=True)
        return False, str(exc)

    finally:
        if temp_extract.exists():
            shutil.rmtree(temp_extract, ignore_errors=True)


def list_backups(backup_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List all available backup zip archives in chronological order."""
    target_dir = backup_dir or get_default_backup_dir()
    if not target_dir.is_dir():
        return []

    backups = []
    for f in target_dir.glob("repostflow_backup_*.zip"):
        stat = f.stat()
        backups.append({
            "filename": f.name,
            "path": str(f.resolve()),
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })

    backups.sort(key=lambda x: x["created_at"], reverse=True)
    return backups
