"""
app/routes/channels.py - Full channel management routes (Stage 2).

Endpoints:
  GET  /channels/              — List all channels
  POST /channels/<id>/role     — Update channel role
  POST /channels/<id>/toggle-monitoring — Enable/disable monitoring
  POST /channels/<id>/test     — Test connectivity
  POST /channels/<id>/refresh  — Refresh metadata from YouTube
  POST /channels/<id>/disconnect — Soft-delete a channel
  POST /channels/notifications/mark-all-read — Mark notifications read
"""

from __future__ import annotations

import logging

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    ChannelRole,
    ConnectedAccount,
    YouTubeChannel,
    Notification,
)
from app.services.channel_service import (
    disconnect_channel,
    refresh_channel_metadata,
    test_channel_connectivity,
)
from app.services.video_detector import check_source_channel_for_new_videos
from app.services.notification_service import mark_all_read

logger = logging.getLogger(__name__)

channels_bp = Blueprint("channels", __name__)


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _get_user_channels():
    return (
        YouTubeChannel.query
        .join(ConnectedAccount)
        .filter(ConnectedAccount.user_id == current_user.id)
        .order_by(YouTubeChannel.channel_name)
        .all()
    )


def _get_user_accounts():
    return (
        ConnectedAccount.query
        .filter_by(user_id=current_user.id)
        .order_by(ConnectedAccount.email)
        .all()
    )


def _get_channel_or_404(channel_id: int) -> YouTubeChannel:
    return (
        YouTubeChannel.query
        .join(ConnectedAccount)
        .filter(
            YouTubeChannel.id == channel_id,
            ConnectedAccount.user_id == current_user.id,
        )
        .first_or_404()
    )


def _get_base_ctx():
    """Context vars needed by base.html."""
    unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    notifs = (
        Notification.query
        .filter_by(user_id=current_user.id, is_read=False)
        .order_by(Notification.created_at.desc())
        .limit(20).all()
    )
    return {"unread_count": unread, "notifications": notifs}


# ------------------------------------------------------------------ #
# Channel list                                                        #
# ------------------------------------------------------------------ #

@channels_bp.route("/")
@login_required
def index():
    channels = _get_user_channels()
    accounts = _get_user_accounts()
    ctx = _get_base_ctx()
    return render_template(
        "channels.html",
        channels=channels,
        accounts=accounts,
        **ctx,
    )


# ------------------------------------------------------------------ #
# Role update                                                         #
# ------------------------------------------------------------------ #

@channels_bp.route("/<int:channel_id>/role", methods=["POST"])
@login_required
def update_role(channel_id: int):
    channel = _get_channel_or_404(channel_id)
    role_str = request.form.get("role", "destination")
    role_map = {
        "source": ChannelRole.SOURCE,
        "destination": ChannelRole.DESTINATION,
        "both": ChannelRole.BOTH,
    }
    new_role = role_map.get(role_str)
    if not new_role:
        flash("Invalid channel role.", "danger")
        return redirect(url_for("channels.index"))
    channel.role = new_role
    try:
        db.session.commit()
        flash(f"Role updated to '{role_str}' for {channel.channel_name}.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Error updating role: {exc}", "danger")
    return redirect(url_for("channels.index"))


# ------------------------------------------------------------------ #
# Monitoring toggle                                                   #
# ------------------------------------------------------------------ #

@channels_bp.route("/<int:channel_id>/toggle-monitoring", methods=["POST"])
@login_required
def toggle_monitoring(channel_id: int):
    channel = _get_channel_or_404(channel_id)
    channel.monitoring_enabled = not channel.monitoring_enabled
    try:
        db.session.commit()
        state = "enabled" if channel.monitoring_enabled else "disabled"
        flash(f"Monitoring {state} for {channel.channel_name}.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Error: {exc}", "danger")
    return redirect(url_for("channels.index"))


# ------------------------------------------------------------------ #
# Connectivity test                                                   #
# ------------------------------------------------------------------ #

@channels_bp.route("/<int:channel_id>/test", methods=["POST"])
@login_required
def test_connection(channel_id: int):
    channel = _get_channel_or_404(channel_id)
    ok, message = test_channel_connectivity(
        channel.connected_account, channel.channel_id
    )
    flash(message, "success" if ok else "danger")
    return redirect(url_for("channels.index"))


# ------------------------------------------------------------------ #
# Metadata refresh                                                    #
# ------------------------------------------------------------------ #

@channels_bp.route("/<int:channel_id>/refresh", methods=["POST"])
@login_required
def refresh_channel(channel_id: int):
    channel = _get_channel_or_404(channel_id)
    ok, message = refresh_channel_metadata(channel)
    flash(message, "success" if ok else "danger")
    return redirect(url_for("channels.index"))


# ------------------------------------------------------------------ #
# Check source uploads                                                #
# ------------------------------------------------------------------ #

@channels_bp.route("/<int:channel_id>/check-uploads", methods=["POST"])
@login_required
def check_uploads(channel_id: int):
    channel = _get_channel_or_404(channel_id)
    videos, err = check_source_channel_for_new_videos(channel.id)
    if err:
        flash(f"Error checking uploads: {err}", "danger")
    elif not videos:
        flash(f"Checked '{channel.channel_name}'. No new video uploads found.", "info")
    else:
        awaiting = sum(1 for v in videos if v.status.value == "awaiting_file")
        msg = f"Found {len(videos)} new upload(s) on '{channel.channel_name}'!"
        if awaiting:
            msg += f" ({awaiting} awaiting local master file)"
        flash(msg, "success")
    return redirect(url_for("channels.index"))


# ------------------------------------------------------------------ #
# Disconnect channel                                                  #
# ------------------------------------------------------------------ #

@channels_bp.route("/<int:channel_id>/disconnect", methods=["POST"])
@login_required
def disconnect(channel_id: int):
    channel = _get_channel_or_404(channel_id)
    name = channel.channel_name
    ok, message = disconnect_channel(channel)
    flash(message, "success" if ok else "danger")
    logger.info("User %s disconnected channel %s (%s)", current_user.id, channel_id, name)
    return redirect(url_for("channels.index"))


# ------------------------------------------------------------------ #
# Notifications                                                       #
# ------------------------------------------------------------------ #

@channels_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def notifications_mark_read():
    mark_all_read(current_user.id)
    return redirect(request.referrer or url_for("dashboard.index"))
