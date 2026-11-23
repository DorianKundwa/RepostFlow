"""
app/routes/uploads.py - Upload queue and publishing history routes.
"""

from __future__ import annotations

import logging
from flask import (
    Blueprint,
    current_app,
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
    ConnectedAccount,
    PublishingHistory,
    UploadJob,
    UploadStatus,
    Video,
    YouTubeChannel,
)
from app.services.upload_service import (
    cancel_job,
    execute_upload_job,
    pause_job,
    resume_job,
    retry_job,
    run_job_in_background,
)

logger = logging.getLogger(__name__)

uploads_bp = Blueprint("uploads", __name__)


# ------------------------------------------------------------------ #
# Upload Queue                                                        #
# ------------------------------------------------------------------ #

@uploads_bp.route("/")
@login_required
def index():
    status_filter = request.args.get("status", "").strip().lower()
    channel_filter = request.args.get("channel_id", type=int)
    search_q = request.args.get("q", "").strip()

    # Base query joined with user's channels
    query = (
        UploadJob.query.join(
            YouTubeChannel, UploadJob.destination_channel_id == YouTubeChannel.id
        )
        .join(ConnectedAccount, YouTubeChannel.connected_account_id == ConnectedAccount.id)
        .filter(ConnectedAccount.user_id == current_user.id)
    )

    if status_filter:
        try:
            status_enum = UploadStatus(status_filter)
            query = query.filter(UploadJob.status == status_enum)
        except ValueError:
            pass

    if channel_filter:
        query = query.filter(UploadJob.destination_channel_id == channel_filter)

    if search_q:
        query = query.join(Video, UploadJob.video_id == Video.id).filter(
            UploadJob.upload_title.ilike(f"%{search_q}%")
            | Video.title.ilike(f"%{search_q}%")
            | Video.original_filename.ilike(f"%{search_q}%")
        )

    jobs = query.order_by(UploadJob.created_at.desc()).limit(150).all()

    # User's destination channels for filtering
    channels = (
        YouTubeChannel.query.join(
            ConnectedAccount, YouTubeChannel.connected_account_id == ConnectedAccount.id
        )
        .filter(ConnectedAccount.user_id == current_user.id, YouTubeChannel.is_active == True)
        .order_by(YouTubeChannel.channel_name)
        .all()
    )

    # Queue statistics for badges
    user_job_base = (
        UploadJob.query.join(
            YouTubeChannel, UploadJob.destination_channel_id == YouTubeChannel.id
        )
        .join(ConnectedAccount, YouTubeChannel.connected_account_id == ConnectedAccount.id)
        .filter(ConnectedAccount.user_id == current_user.id)
    )

    stats = {
        "all": user_job_base.count(),
        "pending": user_job_base.filter(UploadJob.status == UploadStatus.PENDING).count(),
        "uploading": user_job_base.filter(
            UploadJob.status.in_([UploadStatus.UPLOADING, UploadStatus.PROCESSING])
        ).count(),
        "completed": user_job_base.filter(UploadJob.status == UploadStatus.COMPLETED).count(),
        "failed": user_job_base.filter(UploadJob.status == UploadStatus.FAILED).count(),
        "paused": user_job_base.filter(UploadJob.status == UploadStatus.PAUSED).count(),
        "awaiting_file": user_job_base.filter(
            UploadJob.status == UploadStatus.AWAITING_FILE
        ).count(),
    }

    return render_template(
        "upload_queue.html",
        jobs=jobs,
        channels=channels,
        stats=stats,
        current_status=status_filter,
        current_channel=channel_filter,
        search_q=search_q,
    )


# ------------------------------------------------------------------ #
# Publishing History                                                  #
# ------------------------------------------------------------------ #

@uploads_bp.route("/history")
@login_required
def history():
    status_filter = request.args.get("status", "").strip().lower()
    channel_filter = request.args.get("channel_id", type=int)
    search_q = request.args.get("q", "").strip()

    query = (
        PublishingHistory.query.join(
            YouTubeChannel, PublishingHistory.destination_channel_id == YouTubeChannel.id
        )
        .join(ConnectedAccount, YouTubeChannel.connected_account_id == ConnectedAccount.id)
        .filter(ConnectedAccount.user_id == current_user.id)
    )

    if status_filter:
        query = query.filter(PublishingHistory.upload_status == status_filter)

    if channel_filter:
        query = query.filter(PublishingHistory.destination_channel_id == channel_filter)

    if search_q:
        query = query.filter(PublishingHistory.video_title.ilike(f"%{search_q}%"))

    history_records = query.order_by(PublishingHistory.uploaded_at.desc()).limit(200).all()

    channels = (
        YouTubeChannel.query.join(
            ConnectedAccount, YouTubeChannel.connected_account_id == ConnectedAccount.id
        )
        .filter(ConnectedAccount.user_id == current_user.id)
        .order_by(YouTubeChannel.channel_name)
        .all()
    )

    total_published = (
        PublishingHistory.query.join(
            YouTubeChannel, PublishingHistory.destination_channel_id == YouTubeChannel.id
        )
        .join(ConnectedAccount, YouTubeChannel.connected_account_id == ConnectedAccount.id)
        .filter(
            ConnectedAccount.user_id == current_user.id,
            PublishingHistory.upload_status == "completed",
        )
        .count()
    )

    return render_template(
        "history.html",
        records=history_records,
        channels=channels,
        total_published=total_published,
        current_status=status_filter,
        current_channel=channel_filter,
        search_q=search_q,
    )


# ------------------------------------------------------------------ #
# Job Actions                                                         #
# ------------------------------------------------------------------ #

@uploads_bp.route("/<int:job_id>/run", methods=["POST"])
@login_required
def run_job(job_id: int):
    job = db.session.get(UploadJob, job_id)
    if not job or job.destination_channel.connected_account.user_id != current_user.id:
        flash("Upload job not found.", "danger")
        return redirect(url_for("uploads.index"))

    # In testing/synchronous mode, execute directly; else launch in background
    sync_mode = request.form.get("sync", "false").lower() == "true" or current_app.config.get("TESTING", False)

    if sync_mode:
        success, err = execute_upload_job(job_id)
        if success:
            flash(f"Job #{job_id} uploaded successfully!", "success")
        else:
            flash(f"Job #{job_id} failed: {err}", "danger")
    else:
        run_job_in_background(job_id, current_app._get_current_object())
        flash(f"Job #{job_id} started in background.", "info")

    return redirect(url_for("uploads.index"))


@uploads_bp.route("/<int:job_id>/retry", methods=["POST"])
@login_required
def retry_job_endpoint(job_id: int):
    success, err = retry_job(job_id, current_user.id)
    if success:
        flash(f"Job #{job_id} reset to PENDING and queued for retry.", "success")
    else:
        flash(f"Could not retry job #{job_id}: {err}", "danger")
    return redirect(url_for("uploads.index"))


@uploads_bp.route("/<int:job_id>/pause", methods=["POST"])
@login_required
def pause_job_endpoint(job_id: int):
    success, err = pause_job(job_id, current_user.id)
    if success:
        flash(f"Job #{job_id} has been paused.", "info")
    else:
        flash(f"Could not pause job #{job_id}: {err}", "danger")
    return redirect(url_for("uploads.index"))


@uploads_bp.route("/<int:job_id>/resume", methods=["POST"])
@login_required
def resume_job_endpoint(job_id: int):
    success, err = resume_job(job_id, current_user.id)
    if success:
        flash(f"Job #{job_id} resumed and set to PENDING.", "success")
    else:
        flash(f"Could not resume job #{job_id}: {err}", "danger")
    return redirect(url_for("uploads.index"))


@uploads_bp.route("/<int:job_id>/cancel", methods=["POST"])
@login_required
def cancel_job_endpoint(job_id: int):
    success, err = cancel_job(job_id, current_user.id)
    if success:
        flash(f"Job #{job_id} cancelled and removed from queue.", "info")
    else:
        flash(f"Could not cancel job #{job_id}: {err}", "danger")
    return redirect(url_for("uploads.index"))


@uploads_bp.route("/retry-all-failed", methods=["POST"])
@login_required
def retry_all_failed():
    failed_jobs = (
        UploadJob.query.join(
            YouTubeChannel, UploadJob.destination_channel_id == YouTubeChannel.id
        )
        .join(ConnectedAccount, YouTubeChannel.connected_account_id == ConnectedAccount.id)
        .filter(
            ConnectedAccount.user_id == current_user.id,
            UploadJob.status == UploadStatus.FAILED,
        )
        .all()
    )

    count = 0
    for job in failed_jobs:
        job.status = UploadStatus.PENDING
        job.retry_count = 0
        job.error_message = None
        count += 1

    try:
        db.session.commit()
        flash(f"Reset {count} failed jobs back to PENDING.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Failed to reset jobs: {exc}", "danger")

    return redirect(url_for("uploads.index"))


# ------------------------------------------------------------------ #
# JSON Status API for Dynamic Live Queue Updates                      #
# ------------------------------------------------------------------ #

@uploads_bp.route("/api/status")
@login_required
def api_status():
    jobs = (
        UploadJob.query.join(
            YouTubeChannel, UploadJob.destination_channel_id == YouTubeChannel.id
        )
        .join(ConnectedAccount, YouTubeChannel.connected_account_id == ConnectedAccount.id)
        .filter(
            ConnectedAccount.user_id == current_user.id,
            UploadJob.status.in_([
                UploadStatus.PENDING,
                UploadStatus.UPLOADING,
                UploadStatus.PROCESSING,
                UploadStatus.PAUSED,
            ]),
        )
        .all()
    )

    data = [
        {
            "id": j.id,
            "status": j.status.value,
            "progress_percent": j.progress_percent,
            "destination_video_id": j.destination_video_id,
            "destination_video_url": j.destination_video_url,
            "error_message": j.error_message,
        }
        for j in jobs
    ]
    return jsonify({"active_jobs": data})


@uploads_bp.route("/api/<int:job_id>")
@login_required
def api_job_detail(job_id: int):
    job = db.session.get(UploadJob, job_id)
    if not job or job.destination_channel.connected_account.user_id != current_user.id:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "id": job.id,
        "video_id": job.video_id,
        "video_title": job.upload_title or (job.video.title if job.video else ""),
        "channel_name": job.destination_channel.channel_name if job.destination_channel else "",
        "status": job.status.value,
        "progress_percent": job.progress_percent,
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
        "destination_video_id": job.destination_video_id,
        "destination_video_url": job.destination_video_url,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    })
