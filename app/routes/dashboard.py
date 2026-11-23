"""
app/routes/dashboard.py - Overview dashboard.
"""

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    YouTubeChannel, Video, UploadJob, PublishingHistory,
    Notification, ChannelRole, UploadStatus, VideoStatus
)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    # --- Stats cards (live from database) ---
    total_channels = YouTubeChannel.query.filter_by(is_active=True).count()
    source_channels = YouTubeChannel.query.filter(
        YouTubeChannel.role.in_([ChannelRole.SOURCE, ChannelRole.BOTH]),
        YouTubeChannel.is_active == True
    ).count()
    dest_channels = YouTubeChannel.query.filter(
        YouTubeChannel.role.in_([ChannelRole.DESTINATION, ChannelRole.BOTH]),
        YouTubeChannel.is_active == True
    ).count()
    videos_detected = Video.query.count()
    videos_published = UploadJob.query.filter_by(status=UploadStatus.COMPLETED).count()
    pending_uploads = UploadJob.query.filter(
        UploadJob.status.in_([UploadStatus.PENDING, UploadStatus.UPLOADING, UploadStatus.PROCESSING])
    ).count()
    failed_uploads = UploadJob.query.filter_by(status=UploadStatus.FAILED).count()

    # --- Recent activity (last 10 history entries) ---
    recent_history = PublishingHistory.query.order_by(
        PublishingHistory.uploaded_at.desc()
    ).limit(10).all()

    # --- Upload queue (first 5 pending) ---
    queue_preview = UploadJob.query.filter(
        UploadJob.status.in_([UploadStatus.PENDING, UploadStatus.UPLOADING, UploadStatus.PROCESSING])
    ).order_by(UploadJob.created_at.asc()).limit(5).all()

    # --- Connected channels ---
    channels = YouTubeChannel.query.filter_by(is_active=True).order_by(
        YouTubeChannel.channel_name
    ).limit(8).all()

    # --- Recent videos ---
    recent_videos = Video.query.order_by(Video.detected_at.desc()).limit(6).all()

    # --- Unread notifications ---
    notifications = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).order_by(Notification.created_at.desc()).limit(20).all()
    unread_count = len(notifications)

    stats = {
        "total_channels": total_channels,
        "source_channels": source_channels,
        "dest_channels": dest_channels,
        "videos_detected": videos_detected,
        "videos_published": videos_published,
        "pending_uploads": pending_uploads,
        "failed_uploads": failed_uploads,
    }

    return render_template(
        "dashboard.html",
        stats=stats,
        recent_history=recent_history,
        queue_preview=queue_preview,
        channels=channels,
        recent_videos=recent_videos,
        notifications=notifications,
        unread_count=unread_count,
    )
