"""
app/routes/videos.py - Central video library routes (Stage 3).

Endpoints:
  GET  /videos/                      — Video library list, filter, search, stats
  GET  /videos/<id>                  — Video details, technical specs, metadata editor
  POST /videos/<id>/edit             — Update title, description, tags, privacy
  POST /videos/<id>/thumbnail        — Upload custom thumbnail
  POST /videos/<id>/match-file       — Link local file to Awaiting Original File video
  POST /videos/<id>/delete           — Delete video from library
  POST /videos/import-file           — Manual import of a local video file
  GET  /videos/<id>/preview-stream   — Safe video file streaming for HTML5 preview
  GET  /videos/<id>/thumbnail-preview— Safe thumbnail image serving
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from werkzeug.utils import secure_filename

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    Video,
    VideoStatus,
    SourceType,
    PrivacyStatus,
    YouTubeChannel,
    Notification,
)
from app.services.video_service import (
    add_video_from_local_file,
    attach_local_media_to_video,
    delete_video,
    get_video_stats,
    parse_tags_to_list,
    query_videos,
    set_custom_thumbnail,
    update_video_metadata,
)

logger = logging.getLogger(__name__)

videos_bp = Blueprint("videos", __name__)


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


@videos_bp.route("/")
@login_required
def index():
    search = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "all").strip()
    source_filter = request.args.get("source", "all").strip()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 24
    offset = (page - 1) * per_page

    videos, total = query_videos(
        search_query=search if search else None,
        status=status_filter,
        source_type=source_filter,
        limit=per_page,
        offset=offset,
    )

    stats = get_video_stats()
    channels = YouTubeChannel.query.filter_by(is_active=True).all()
    total_pages = max(1, (total + per_page - 1) // per_page)
    ctx = _get_base_ctx()

    return render_template(
        "videos.html",
        videos=videos,
        total=total,
        page=page,
        total_pages=total_pages,
        stats=stats,
        channels=channels,
        search=search,
        status_filter=status_filter,
        source_filter=source_filter,
        **ctx,
    )


@videos_bp.route("/<int:video_id>")
@login_required
def detail(video_id: int):
    video = db.session.get(Video, video_id)
    if not video:
        flash("Video not found.", "danger")
        return redirect(url_for("videos.index"))

    tags_list = parse_tags_to_list(video.tags)
    tags_string = ", ".join(tags_list)
    ctx = _get_base_ctx()

    return render_template(
        "video_detail.html",
        video=video,
        tags_list=tags_list,
        tags_string=tags_string,
        **ctx,
    )


@videos_bp.route("/<int:video_id>/edit", methods=["POST"])
@login_required
def edit(video_id: int):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    tags_raw = request.form.get("tags", "").strip()
    category_id = request.form.get("category_id", "22").strip()
    privacy_str = request.form.get("privacy_status", "private").strip().lower()

    try:
        privacy = PrivacyStatus(privacy_str)
    except ValueError:
        privacy = PrivacyStatus.PRIVATE

    ok, err = update_video_metadata(
        video_id=video_id,
        title=title,
        description=description,
        tags=tags_raw,
        category_id=category_id,
        privacy_status=privacy,
    )

    if ok:
        flash("Video metadata updated successfully.", "success")
    else:
        flash(f"Failed to update metadata: {err}", "danger")

    return redirect(url_for("videos.detail", video_id=video_id))


@videos_bp.route("/<int:video_id>/thumbnail", methods=["POST"])
@login_required
def upload_thumbnail(video_id: int):
    file = request.files.get("thumbnail_file")
    if not file or not file.filename:
        flash("No image file was selected.", "warning")
        return redirect(url_for("videos.detail", video_id=video_id))

    ok, err = set_custom_thumbnail(video_id, file)
    if ok:
        flash("Thumbnail updated and optimized.", "success")
    else:
        flash(f"Could not update thumbnail: {err}", "danger")

    return redirect(url_for("videos.detail", video_id=video_id))


@videos_bp.route("/<int:video_id>/match-file", methods=["POST"])
@login_required
def match_file(video_id: int):
    uploaded_file = request.files.get("video_file")
    file_path = request.form.get("local_file_path", "").strip()

    target_path = None

    if uploaded_file and uploaded_file.filename:
        safe_name = secure_filename(uploaded_file.filename)
        dest_dir = Path(current_app.config["VIDEOS_DIR"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        unique_name = f"matched_{video_id}_{safe_name}"
        save_target = dest_dir / unique_name
        uploaded_file.save(str(save_target))
        target_path = str(save_target)
    elif file_path:
        target_path = file_path
    else:
        flash("Please select a video file or enter a valid local file path.", "warning")
        return redirect(url_for("videos.detail", video_id=video_id))

    ok, err = attach_local_media_to_video(video_id, target_path)
    if ok:
        flash("Local media file verified and linked successfully! Video is now Ready for reposting.", "success")
    else:
        flash(f"Failed to link media file: {err}", "danger")

    return redirect(url_for("videos.detail", video_id=video_id))


@videos_bp.route("/browse-file-dialog", methods=["POST"])
@login_required
def browse_file_dialog():
    """
    Launch native Windows file chooser dialog on the host system
    and return the chosen absolute file path as JSON.
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        return jsonify({"success": False, "error": "Native desktop dialog only available on Windows."}), 400

    cmd = (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
        '[System.Reflection.Assembly]::LoadWithPartialName(\'System.Windows.Forms\') | Out-Null; '
        '$d = New-Object System.Windows.Forms.OpenFileDialog; '
        '$d.Filter = \'Video Files (*.mp4;*.mov;*.mkv;*.webm;*.avi)|*.mp4;*.mov;*.mkv;*.webm;*.avi|All Files (*.*)|*.*\'; '
        '$d.Title = \'Select Original Master Video File\'; '
        'if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.FileName }"'
    )
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        selected_path = proc.stdout.strip()
        if selected_path:
            return jsonify({"success": True, "path": selected_path})
        return jsonify({"success": False, "error": "No file selected."})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "File dialog timed out."})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)})


@videos_bp.route("/<int:video_id>/delete", methods=["POST"])
@login_required
def delete(video_id: int):
    delete_file = request.form.get("delete_local_file") == "1"
    ok, err = delete_video(video_id, remove_local_file=delete_file)
    if ok:
        flash("Video removed from library.", "success")
    else:
        flash(f"Error deleting video: {err}", "danger")
    return redirect(url_for("videos.index"))


@videos_bp.route("/bulk-delete", methods=["POST"])
@login_required
def bulk_delete():
    video_ids = request.form.getlist("video_ids")
    if not video_ids:
        flash("No videos were selected for deletion.", "warning")
        return redirect(url_for("videos.index"))

    delete_local = request.form.get("delete_local_file") == "1"
    count = 0
    for vid_str in video_ids:
        try:
            vid = int(vid_str)
            ok, _ = delete_video(vid, remove_local_file=delete_local)
            if ok:
                count += 1
        except Exception:
            pass

    flash(f"Successfully deleted {count} video(s) from your library.", "success")
    return redirect(url_for("videos.index"))


@videos_bp.route("/import-file", methods=["POST"])
@login_required
def import_file():
    """Manual import either by local file path or by direct file upload."""
    uploaded_file = request.files.get("video_file")
    local_path = request.form.get("local_path", "").strip()
    title = request.form.get("title", "").strip()

    target_path = None

    if uploaded_file and uploaded_file.filename:
        safe_name = secure_filename(uploaded_file.filename)
        dest_dir = Path(current_app.config["VIDEOS_DIR"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        target_path = str(dest_dir / safe_name)
        uploaded_file.save(target_path)
    elif local_path:
        target_path = local_path
    else:
        flash("Please provide a local file path or upload a video file.", "warning")
        return redirect(url_for("videos.index"))

    video, err = add_video_from_local_file(
        file_path=target_path,
        source_type=SourceType.LOCAL_FOLDER,
        title=title if title else None,
        user_id_for_notification=current_user.id,
    )

    if video:
        flash(f"Video '{video.title}' successfully imported and inspected!", "success")
        return redirect(url_for("videos.detail", video_id=video.id))
    else:
        flash(f"Failed to import video: {err}", "danger")
        return redirect(url_for("videos.index"))


@videos_bp.route("/<int:video_id>/preview-stream")
@login_required
def preview_stream(video_id: int):
    """Safely stream the video file for HTML5 in-browser preview."""
    video = db.session.get(Video, video_id)
    if not video or not video.local_file_path:
        abort(404, "Media not found.")

    file_path = Path(video.local_file_path).resolve()
    if not file_path.is_file():
        abort(404, "File missing on disk.")

    # Determine mimetype
    ext = file_path.suffix.lower()
    mimetypes = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
    }
    mimetype = mimetypes.get(ext, "application/octet-stream")
    return send_file(file_path, mimetype=mimetype, conditional=True)


@videos_bp.route("/<int:video_id>/thumbnail-preview")
@login_required
def thumbnail_preview(video_id: int):
    """Safely serve the generated thumbnail image."""
    video = db.session.get(Video, video_id)
    if not video:
        abort(404)

    if video.local_thumbnail_path:
        t_path = Path(video.local_thumbnail_path).resolve()
        if t_path.is_file():
            return send_file(t_path, mimetype="image/jpeg")

    # If local thumbnail is missing, redirect to remote YouTube thumbnail or return 404
    if video.thumbnail_url:
        return redirect(video.thumbnail_url)

    abort(404, "Thumbnail not found.")
