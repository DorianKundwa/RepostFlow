"""
app/routes/automation.py - Automation rule management routes (Stage 6).

Endpoints:
  GET  /automation/                   — List automation rules, source/destination dropdowns
  POST /automation/create             — Create new automation rule
  POST /automation/<id>/edit          — Edit existing automation rule
  POST /automation/<id>/toggle        — Enable or disable rule
  POST /automation/<id>/delete        — Delete rule
  POST /automation/<id>/apply-existing— Queue uploads for existing matching videos in library
"""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    AutomationRule,
    ChannelRole,
    ConnectedAccount,
    Notification,
    PrivacyStatus,
    SourceType,
    WatchedFolder,
    YouTubeChannel,
)
from app.services.automation_service import (
    apply_rule_to_existing_videos,
    create_or_update_rule,
    delete_rule,
    toggle_rule,
)

logger = logging.getLogger(__name__)

automation_bp = Blueprint("automation", __name__)


def _get_base_ctx():
    """Retrieve common context variables required by base.html."""
    unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    notifs = (
        Notification.query
        .filter_by(user_id=current_user.id, is_read=False)
        .order_by(Notification.created_at.desc())
        .limit(20)
        .all()
    )
    return {"unread_count": unread, "notifications": notifs}


@automation_bp.route("/")
@login_required
def index():
    rules = AutomationRule.query.filter_by(user_id=current_user.id).order_by(AutomationRule.created_at.desc()).all()

    # User's available channels
    source_channels = (
        YouTubeChannel.query
        .join(ConnectedAccount)
        .filter(
            ConnectedAccount.user_id == current_user.id,
            YouTubeChannel.is_active == True,
            YouTubeChannel.role.in_([ChannelRole.SOURCE, ChannelRole.BOTH]),
        )
        .order_by(YouTubeChannel.channel_name)
        .all()
    )

    dest_channels = (
        YouTubeChannel.query
        .join(ConnectedAccount)
        .filter(
            ConnectedAccount.user_id == current_user.id,
            YouTubeChannel.is_active == True,
            YouTubeChannel.role.in_([ChannelRole.DESTINATION, ChannelRole.BOTH]),
        )
        .order_by(YouTubeChannel.channel_name)
        .all()
    )

    watched_folders = (
        WatchedFolder.query
        .filter_by(user_id=current_user.id, is_active=True)
        .order_by(WatchedFolder.label)
        .all()
    )

    ctx = _get_base_ctx()

    return render_template(
        "automation.html",
        rules=rules,
        source_channels=source_channels,
        dest_channels=dest_channels,
        watched_folders=watched_folders,
        **ctx,
    )


@automation_bp.route("/create", methods=["POST"])
@login_required
def create():
    name = request.form.get("name", "").strip()
    source_type_str = request.form.get("source_type", "youtube").strip()
    source_channel_id = request.form.get("source_channel_id", type=int)
    source_folder_id = request.form.get("source_folder_id", type=int)
    dest_channel_ids = request.form.getlist("dest_channel_ids", type=int)

    title_prefix = request.form.get("title_prefix", "")
    title_suffix = request.form.get("title_suffix", "")
    description_prepend = request.form.get("description_prepend", "")
    description_append = request.form.get("description_append", "")
    extra_tags = request.form.get("extra_tags", "")

    copy_title = request.form.get("copy_title") == "1"
    copy_description = request.form.get("copy_description") == "1"
    copy_tags = request.form.get("copy_tags") == "1"
    copy_thumbnail = request.form.get("copy_thumbnail") == "1"
    prevent_duplicates = request.form.get("prevent_duplicates", "1") in ("1", "true", "on")

    privacy_str = request.form.get("default_privacy", "private").strip().lower()
    try:
        privacy = PrivacyStatus(privacy_str)
    except ValueError:
        privacy = PrivacyStatus.PRIVATE

    source_type = SourceType.YOUTUBE if source_type_str == "youtube" else SourceType.LOCAL_FOLDER

    rule, err = create_or_update_rule(
        user_id=current_user.id,
        name=name,
        source_type=source_type,
        destination_channel_ids=dest_channel_ids,
        source_channel_id=source_channel_id if source_type == SourceType.YOUTUBE else None,
        source_folder_id=source_folder_id if source_type == SourceType.LOCAL_FOLDER else None,
        copy_title=copy_title,
        copy_description=copy_description,
        copy_tags=copy_tags,
        copy_thumbnail=copy_thumbnail,
        title_prefix=title_prefix,
        title_suffix=title_suffix,
        description_prepend=description_prepend,
        description_append=description_append,
        extra_tags=extra_tags,
        default_privacy=privacy,
        prevent_duplicates=prevent_duplicates,
        is_enabled=True,
    )

    if rule:
        flash(f"Automation rule '{rule.name}' created with {len(dest_channel_ids)} destination(s)!", "success")
        # Check if user opted to apply to existing videos immediately
        if request.form.get("apply_existing") == "1":
            queued, _ = apply_rule_to_existing_videos(rule.id, current_user.id)
            if queued > 0:
                flash(f"Automatically queued {queued} upload job(s) for existing videos in library.", "info")
    else:
        flash(f"Could not create automation rule: {err}", "danger")

    return redirect(url_for("automation.index"))


@automation_bp.route("/<int:rule_id>/data")
@login_required
def rule_data(rule_id: int):
    rule = AutomationRule.query.filter_by(id=rule_id, user_id=current_user.id).first()
    if not rule:
        return jsonify({"success": False, "error": "Automation rule not found."}), 404

    from app.services.video_service import parse_tags_to_list
    extra_tags_list = parse_tags_to_list(rule.extra_tags) if rule.extra_tags else []
    dest_ids = [d.channel_id for d in rule.destinations]

    return jsonify({
        "success": True,
        "rule": {
            "id": rule.id,
            "name": rule.name,
            "source_type": rule.source_type.value,
            "source_channel_id": rule.source_channel_id,
            "source_folder_id": rule.source_folder_id,
            "destination_channel_ids": dest_ids,
            "copy_title": rule.copy_title,
            "copy_description": rule.copy_description,
            "copy_tags": rule.copy_tags,
            "copy_thumbnail": rule.copy_thumbnail,
            "title_prefix": rule.title_prefix or "",
            "title_suffix": rule.title_suffix or "",
            "description_prepend": rule.description_prepend or "",
            "description_append": rule.description_append or "",
            "extra_tags": ", ".join(extra_tags_list),
            "default_privacy": rule.default_privacy.value,
            "prevent_duplicates": getattr(rule, "prevent_duplicates", True),
            "is_enabled": rule.is_enabled,
        }
    })


@automation_bp.route("/<int:rule_id>/edit", methods=["POST"])
@login_required
def edit(rule_id: int):
    name = request.form.get("name", "").strip()
    source_type_str = request.form.get("source_type", "youtube").strip()
    source_channel_id = request.form.get("source_channel_id", type=int)
    source_folder_id = request.form.get("source_folder_id", type=int)
    dest_channel_ids = request.form.getlist("dest_channel_ids", type=int)

    title_prefix = request.form.get("title_prefix", "")
    title_suffix = request.form.get("title_suffix", "")
    description_prepend = request.form.get("description_prepend", "")
    description_append = request.form.get("description_append", "")
    extra_tags = request.form.get("extra_tags", "")

    copy_title = request.form.get("copy_title") == "1"
    copy_description = request.form.get("copy_description") == "1"
    copy_tags = request.form.get("copy_tags") == "1"
    copy_thumbnail = request.form.get("copy_thumbnail") == "1"
    prevent_duplicates = request.form.get("prevent_duplicates", "1") in ("1", "true", "on")

    rule = AutomationRule.query.filter_by(id=rule_id, user_id=current_user.id).first()
    if not rule:
        flash("Automation rule not found.", "danger")
        return redirect(url_for("automation.index"))

    is_enabled_form = request.form.get("is_enabled")
    is_enabled = is_enabled_form in ("1", "true", "on") if is_enabled_form is not None else rule.is_enabled

    privacy_str = request.form.get("default_privacy", "private").strip().lower()
    try:
        privacy = PrivacyStatus(privacy_str)
    except ValueError:
        privacy = PrivacyStatus.PRIVATE

    source_type = SourceType.YOUTUBE if source_type_str == "youtube" else SourceType.LOCAL_FOLDER

    updated_rule, err = create_or_update_rule(
        user_id=current_user.id,
        name=name,
        source_type=source_type,
        destination_channel_ids=dest_channel_ids,
        source_channel_id=source_channel_id if source_type == SourceType.YOUTUBE else None,
        source_folder_id=source_folder_id if source_type == SourceType.LOCAL_FOLDER else None,
        copy_title=copy_title,
        copy_description=copy_description,
        copy_tags=copy_tags,
        copy_thumbnail=copy_thumbnail,
        title_prefix=title_prefix,
        title_suffix=title_suffix,
        description_prepend=description_prepend,
        description_append=description_append,
        extra_tags=extra_tags,
        default_privacy=privacy,
        prevent_duplicates=prevent_duplicates,
        is_enabled=is_enabled,
        rule_id=rule_id,
    )

    if updated_rule:
        flash(f"Automation rule '{updated_rule.name}' updated successfully!", "success")
        if request.form.get("apply_existing") == "1":
            queued, _ = apply_rule_to_existing_videos(updated_rule.id, current_user.id)
            if queued > 0:
                flash(f"Automatically queued {queued} upload job(s) for existing videos in library.", "info")
    else:
        flash(f"Could not update automation rule: {err}", "danger")

    return redirect(url_for("automation.index"))


@automation_bp.route("/<int:rule_id>/toggle", methods=["POST"])
@login_required
def toggle(rule_id: int):
    ok, err = toggle_rule(rule_id, current_user.id)
    if ok:
        flash("Rule status updated.", "success")
    else:
        flash(f"Error: {err}", "danger")
    return redirect(url_for("automation.index"))


@automation_bp.route("/<int:rule_id>/delete", methods=["POST"])
@login_required
def delete(rule_id: int):
    ok, err = delete_rule(rule_id, current_user.id)
    if ok:
        flash("Automation rule deleted.", "success")
    else:
        flash(f"Error deleting rule: {err}", "danger")
    return redirect(url_for("automation.index"))


@automation_bp.route("/<int:rule_id>/apply-existing", methods=["POST"])
@login_required
def apply_existing(rule_id: int):
    queued, err = apply_rule_to_existing_videos(rule_id, current_user.id)
    if err:
        flash(f"Error: {err}", "danger")
    elif queued == 0:
        flash("No new videos needed to be queued for this rule.", "info")
    else:
        flash(f"Successfully queued {queued} upload job(s) for existing videos in library!", "success")
    return redirect(url_for("automation.index"))
