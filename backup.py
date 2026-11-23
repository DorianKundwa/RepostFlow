"""
backup.py - RepostFlow CLI Backup & Disaster Recovery Tool.

Usage:
    python backup.py create                   Create a fresh timestamped backup
    python backup.py list                     List all existing backups
    python backup.py restore <backup_file>    Restore a specific backup
"""

import argparse
import sys
from pathlib import Path

# Ensure root directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.backup_service import create_backup, list_backups, restore_backup


def main():
    parser = argparse.ArgumentParser(
        description="RepostFlow CLI Backup & Disaster Recovery Utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Backup action to perform")

    # Command: create
    subparsers.add_parser("create", help="Create a complete backup archive")

    # Command: list
    subparsers.add_parser("list", help="List all available backups")

    # Command: restore
    restore_parser = subparsers.add_parser("restore", help="Restore application data from backup")
    restore_parser.add_argument("archive", help="Path or filename of the backup zip archive to restore")

    args = parser.parse_args()

    if args.command == "create":
        print("\n[RepostFlow] Initializing database and configuration backup...")
        success, archive_path, err = create_backup()
        if success:
            print(f"[OK] Backup created successfully:\n     {archive_path}\n")
        else:
            print(f"[ERROR] Backup failed: {err}\n", file=sys.stderr)
            sys.exit(1)

    elif args.command == "list":
        backups = list_backups()
        if not backups:
            print("\n[RepostFlow] No backups found in storage/backups/\n")
            return

        print(f"\n[RepostFlow] Available Backups ({len(backups)}):")
        print("-" * 75)
        print(f"{'Filename':<35} {'Size (MB)':<12} {'Created At (UTC)':<25}")
        print("-" * 75)
        for b in backups:
            print(f"{b['filename']:<35} {b['size_mb']:<12} {b['created_at']:<25}")
        print("-" * 75 + "\n")

    elif args.command == "restore":
        archive_path = Path(args.archive)
        if not archive_path.is_file():
            # Check default backup directory if filename was given without path
            fallback = Path(__file__).resolve().parent / "storage" / "backups" / args.archive
            if fallback.is_file():
                archive_path = fallback
            else:
                print(f"[ERROR] Archive file not found: {args.archive}", file=sys.stderr)
                sys.exit(1)

        print(f"\n[RepostFlow] Restoring application state from {archive_path.name}...")
        success, err = restore_backup(str(archive_path))
        if success:
            print("[OK] Restore completed successfully! Database and credentials are ready.\n")
        else:
            print(f"[ERROR] Restore failed: {err}\n", file=sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
