"""
app/services/notification_service.py - In-dashboard notification helper.

Creates Notification rows for the current user. All notifications
are stored in the database and displayed in the topbar bell menu.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.extensions import db
from app.models import Notification, NotificationType

logger = logging.getLogger(__name__)


def create_notification(
    user_id: int,
    title: str,
    message: str = None,
    type: NotificationType = NotificationType.INFO,
    related_url: str = None,
) -> None:
    """
    Persist a notification for a user.

    Args:
        user_id:     ID of the User to notify.
        title:       Short title shown in the notification centre.
        message:     Optional longer message.
        type:        NotificationType (info/success/warning/error).
        related_url: Optional URL the user can click to investigate.
    """
    try:
        note = Notification(
            user_id=user_id,
            type=type,
            title=title,
            message=message,
            is_read=False,
            created_at=datetime.now(timezone.utc),
            related_url=related_url,
        )
        db.session.add(note)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to create notification: %s", exc)


def mark_all_read(user_id: int) -> None:
    """Mark all notifications for a user as read."""
    try:
        Notification.query.filter_by(user_id=user_id, is_read=False).update(
            {"is_read": True}
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to mark notifications read: %s", exc)


def mark_read(notification_id: int, user_id: int) -> None:
    """Mark a single notification as read (validates ownership)."""
    try:
        note = Notification.query.filter_by(
            id=notification_id, user_id=user_id
        ).first()
        if note:
            note.is_read = True
            db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to mark notification %s read: %s", notification_id, exc)
