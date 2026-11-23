"""
app/routes/settings.py - Application and watched folder settings routes (Stage 4 & Stage 8).

Endpoints:
  GET  /settings/                 — Settings dashboard (General settings + Watched folders + Scheduler)
  POST /settings/general          — Save general application preferences & reschedule jobs
  POST /settings/folders/add      — Register a new watched folder
  POST /settings/folders/<id>/toggle — Enable or disable a watched folder
  POST /settings/folders/<id>/rescan — Trigger manual rescan of a watched folder
  POST /settings/folders/<id>/delete — Remove a watched folder
  POST /settings/scheduler/toggle — Pause or resume APScheduler background worker
  POST /settings/scheduler/run-now/<job_id> — Manually trigger execution of a scheduled job
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    ApplicationSetting,
    WatchedFolder,
    Notification,
)
from app.services.folder_monitor import folder_monitor, rescan_folder
from app.services.scheduler_service import scheduler_service

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__)


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


@settings_bp.route("/")
@login_required
def index():
    settings = {s.key: s for s in ApplicationSetting.query.order_by(ApplicationSetting.key).all()}
    folders = WatchedFolder.query.filter_by(user_id=current_user.id).order_by(WatchedFolder.added_at.desc()).all()
    ctx = _get_base_ctx()

    scheduler_status = {
        "is_running": scheduler_service.is_running(),
        "is_paused": scheduler_service.is_paused(),
        "jobs": scheduler_service.get_jobs_info(),
    }

    return render_template(
        "settings.html",
        settings=settings,
        folders=folders,
        scheduler=scheduler_status,
        **ctx,
    )


@settings_bp.route("/general", methods=["POST"])
@login_required
def update_general():
    keys = [
        "app_name",
        "default_privacy",
        "max_concurrent_uploads",
        "youtube_poll_interval",
        "auto_copy_metadata",
        "auto_copy_thumbnail",
        "max_upload_retries",
    ]
    for k in keys:
        if k in request.form:
            ApplicationSetting.set(k, request.form[k].strip())

    # Reschedule background jobs with updated poll interval
    scheduler_service.reschedule_jobs(current_app._get_current_object())

    flash("Application settings saved successfully.", "success")
    return redirect(url_for("settings.index"))


@settings_bp.route("/scheduler/toggle", methods=["POST"])
@login_required
def toggle_scheduler():
    if scheduler_service.is_paused():
        scheduler_service.resume()
        flash("Background scheduler resumed.", "success")
    elif scheduler_service.is_running():
        scheduler_service.pause()
        flash("Background scheduler paused.", "info")
    else:
        # Scheduler not running at all, start it
        scheduler_service.start(current_app._get_current_object())
        flash("Background scheduler started.", "success")

    return redirect(url_for("settings.index"))


@settings_bp.route("/scheduler/run-now/<job_id>", methods=["POST"])
@login_required
def run_scheduler_job_now(job_id: str):
    success, msg = scheduler_service.run_job_now(job_id, current_app._get_current_object())
    if success:
        flash(f"Triggered background job '{job_id}'.", "success")
    else:
        flash(f"Could not trigger job '{job_id}': {msg}", "danger")

    return redirect(url_for("settings.index"))


@settings_bp.route("/folders/add", methods=["POST"])
@login_required
def add_folder():
    path_raw = request.form.get("path", "").strip()
    label = request.form.get("label", "").strip()
    recursive = request.form.get("recursive") == "1"

    if not path_raw:
        flash("Please provide a folder directory path.", "warning")
        return redirect(url_for("settings.index"))

    path_obj = Path(path_raw)
    if not path_obj.is_dir():
        flash(f"Directory path '{path_raw}' does not exist on this machine.", "danger")
        return redirect(url_for("settings.index"))

    resolved_path = str(path_obj.resolve())

    # Check for duplicate
    existing = WatchedFolder.query.filter_by(path=resolved_path).first()
    if existing:
        flash(f"Folder '{resolved_path}' is already registered.", "warning")
        return redirect(url_for("settings.index"))

    folder = WatchedFolder(
        user_id=current_user.id,
        path=resolved_path,
        label=label if label else path_obj.name,
        is_active=True,
        recursive=recursive,
    )

    try:
        db.session.add(folder)
        db.session.commit()

        # Add to background observer
        folder_monitor.add_watch(folder.id, folder.path, folder.recursive)

        # Trigger initial rescan of folder
        added_count, _ = rescan_folder(folder.id, current_app)
        msg = f"Watched folder '{folder.label}' added successfully."
        if added_count > 0:
            msg += f" Discovered {added_count} video(s)."

        flash(msg, "success")
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to add watched folder: %s", exc)
        flash(f"Error saving watched folder: {exc}", "danger")

    return redirect(url_for("settings.index"))


@settings_bp.route("/folders/<int:folder_id>/toggle", methods=["POST"])
@login_required
def toggle_folder(folder_id: int):
    folder = WatchedFolder.query.filter_by(id=folder_id, user_id=current_user.id).first_or_404()
    folder.is_active = not folder.is_active

    try:
        db.session.commit()
        if folder.is_active:
            folder_monitor.add_watch(folder.id, folder.path, folder.recursive)
            flash(f"Monitoring enabled for '{folder.label}'.", "success")
        else:
            folder_monitor.remove_watch(folder.id)
            flash(f"Monitoring disabled for '{folder.label}'.", "info")
    except Exception as exc:
        db.session.rollback()
        flash(f"Error toggling folder: {exc}", "danger")

    return redirect(url_for("settings.index"))


@settings_bp.route("/folders/<int:folder_id>/rescan", methods=["POST"])
@login_required
def rescan(folder_id: int):
    folder = WatchedFolder.query.filter_by(id=folder_id, user_id=current_user.id).first_or_404()
    added_count, err = rescan_folder(folder.id, current_app)

    if err:
        flash(f"Rescan error: {err}", "danger")
    elif added_count == 0:
        flash(f"Rescan complete for '{folder.label}'. No new video files detected.", "info")
    else:
        flash(f"Rescan complete! Found and registered {added_count} new video(s) from '{folder.label}'.", "success")

    return redirect(url_for("settings.index"))


@settings_bp.route("/folders/<int:folder_id>/delete", methods=["POST"])
@login_required
def delete_folder(folder_id: int):
    folder = WatchedFolder.query.filter_by(id=folder_id, user_id=current_user.id).first_or_404()
    label = folder.label
    folder_monitor.remove_watch(folder.id)

    try:
        db.session.delete(folder)
        db.session.commit()
        flash(f"Watched folder '{label}' removed.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Error deleting folder: {exc}", "danger")

    return redirect(url_for("settings.index"))
