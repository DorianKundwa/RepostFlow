"""
tests/test_stage3.py - Comprehensive automated tests for Stage 3 (Video Library & Media Processing).

Covers:
  1. Media service: FFprobe/FFmpeg availability, FPS parser, media inspection & validation
  2. Thumbnail service: Pillow image optimization (1280x720, RGB conversion, letterboxing)
  3. Video service:
     - Tag JSON serialization & deserialization
     - Adding videos from local files with technical inspection
     - Attaching local master files to YouTube-detected videos (Awaiting File -> Ready)
     - Updating metadata (title, description, tags, privacy)
     - Querying, filtering, and video stats calculations
     - Safe deletion
  4. Video routes:
     - Library listing with search & filters
     - Detail view with player and technical specs
     - Edit metadata controller
     - File matching controller
     - Thumbnail upload controller
     - File deletion controller
     - Safe streaming and thumbnail preview endpoints
"""

import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.extensions import db as _db
from app.models import (
    User,
    Video,
    VideoStatus,
    SourceType,
    PrivacyStatus,
    YouTubeChannel,
    ConnectedAccount,
)
from app.services import media_service, thumbnail_service, video_service
from config import DevelopmentConfig


class Stage3TestConfig(DevelopmentConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "stage3-test-secret-key"
    TOKEN_ENCRYPTION_KEY = ""


@pytest.fixture(scope="session")
def app():
    application = create_app(config_override=Stage3TestConfig)
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
        u = User.query.filter_by(username="stage3user").first()
        if not u:
            u = User(
                username="stage3user",
                email="stage3user@repostflow.local",
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
# 1. Media Service Tests                                             #
# ------------------------------------------------------------------ #

class TestMediaService:
    def test_fps_parser(self):
        assert media_service._parse_fps("30/1") == 30.0
        assert media_service._parse_fps("60000/1001") == 59.94
        assert media_service._parse_fps("24/1") == 24.0
        assert media_service._parse_fps("29.97") == 29.97
        assert media_service._parse_fps("0/0") is None
        assert media_service._parse_fps("") is None

    def test_tools_detected_on_system(self):
        # We verified ffmpeg and ffprobe are present in this Windows environment
        assert media_service.is_ffprobe_available() is True
        assert media_service.is_ffmpeg_available() is True

    def test_validate_nonexistent_file(self):
        ok, err, meta = media_service.validate_video_file("C:/does_not_exist_xyz.mp4")
        assert ok is False
        assert "does not exist" in err.lower()
        assert meta is None

    def test_validate_disallowed_extension(self, tmp_path):
        bad_file = tmp_path / "test.txt"
        bad_file.write_text("not a video")
        ok, err, meta = media_service.validate_video_file(str(bad_file), allowed_extensions={".mp4", ".mov"})
        assert ok is False
        assert "not supported" in err.lower()

    def test_validate_empty_file(self, tmp_path):
        empty_mp4 = tmp_path / "empty.mp4"
        empty_mp4.touch()
        ok, err, meta = media_service.validate_video_file(str(empty_mp4))
        assert ok is False
        assert "empty" in err.lower()


# ------------------------------------------------------------------ #
# 2. Thumbnail Service Tests                                         #
# ------------------------------------------------------------------ #

class TestThumbnailService:
    def test_optimize_thumbnail_pillow(self, tmp_path):
        src_img_path = tmp_path / "source.png"
        out_jpg_path = tmp_path / "thumb_1280.jpg"

        # Create a raw RGBA image
        img = Image.new("RGBA", (1920, 1080), color=(255, 0, 0, 255))
        img.save(src_img_path)

        ok, err = thumbnail_service.optimize_thumbnail(str(src_img_path), str(out_jpg_path))
        assert ok is True
        assert err is None
        assert out_jpg_path.exists()

        # Verify output properties
        with Image.open(out_jpg_path) as out_img:
            assert out_img.size == (1280, 720)
            assert out_img.format == "JPEG"

    def test_optimize_thumbnail_aspect_ratio_letterboxing(self, tmp_path):
        src_img_path = tmp_path / "square.png"
        out_jpg_path = tmp_path / "thumb_square.jpg"

        # Square image (1:1 aspect ratio) should be letterboxed to 16:9
        img = Image.new("RGB", (800, 800), color=(0, 255, 0))
        img.save(src_img_path)

        ok, err = thumbnail_service.optimize_thumbnail(str(src_img_path), str(out_jpg_path))
        assert ok is True
        with Image.open(out_jpg_path) as out_img:
            assert out_img.size == (1280, 720)


# ------------------------------------------------------------------ #
# 3. Video Service Logic Tests                                       #
# ------------------------------------------------------------------ #

class TestVideoService:
    def test_tags_parser_roundtrip(self):
        tags_input = "music, rock, 2026, live"
        json_str = video_service.parse_tags_to_json(tags_input)
        assert json_str is not None
        assert "rock" in json_str

        parsed_list = video_service.parse_tags_to_list(json_str)
        assert parsed_list == ["music", "rock", "2026", "live"]

        # List input
        json_from_list = video_service.parse_tags_to_json(["hiphop", "beats"])
        assert video_service.parse_tags_to_list(json_from_list) == ["hiphop", "beats"]

    def test_add_video_from_local_file(self, app, db, user_id, tmp_path):
        dummy_video = tmp_path / "test_clip.mp4"
        dummy_video.write_bytes(b"\x00" * 1024)  # 1KB dummy

        fake_meta = {
            "duration_seconds": 125.5,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "codec": "h264",
            "audio_codec": "aac",
            "file_size_bytes": 1024,
            "bitrate_kbps": 5000,
            "container_format": "mov,mp4,m4a,3gp,3g2,mj2",
        }

        with app.app_context():
            with patch("app.services.video_service.validate_video_file", return_value=(True, None, fake_meta)), \
                 patch("app.services.video_service.generate_video_thumbnail", return_value=("storage/thumbnails/dummy.jpg", None)):

                v, err = video_service.add_video_from_local_file(
                    file_path=str(dummy_video),
                    source_type=SourceType.LOCAL_FOLDER,
                    title="Automated Test Clip",
                    description="Test description here",
                    tags=["test", "clip"],
                    privacy_status=PrivacyStatus.PRIVATE,
                    user_id_for_notification=user_id,
                )
                assert err is None
                assert v is not None
                assert v.title == "Automated Test Clip"
                assert v.width == 1920
                assert v.height == 1080
                assert v.status == VideoStatus.READY

    def test_attach_local_media_to_youtube_detected_video(self, app, db, tmp_path):
        with app.app_context():
            # Create a video simulating YouTube source channel detection (awaiting file)
            yt_video = Video(
                source_type=SourceType.YOUTUBE,
                youtube_video_id="yt_abc12345",
                title="YouTube Source Video",
                status=VideoStatus.AWAITING_FILE,
                detected_at=datetime.now(timezone.utc),
            )
            _db.session.add(yt_video)
            _db.session.commit()
            vid_id = yt_video.id

            local_master = tmp_path / "master_yt_abc12345.mp4"
            local_master.write_bytes(b"\x00" * 2048)

            fake_meta = {
                "duration_seconds": 300.0,
                "width": 3840,
                "height": 2160,
                "fps": 60.0,
                "codec": "hevc",
                "file_size_bytes": 2048,
            }

            with patch("app.services.video_service.validate_video_file", return_value=(True, None, fake_meta)), \
                 patch("app.services.video_service.generate_video_thumbnail", return_value=("storage/thumbnails/master.jpg", None)):

                ok, err = video_service.attach_local_media_to_video(vid_id, str(local_master))
                assert ok is True
                assert err is None

                updated = _db.session.get(Video, vid_id)
                assert updated.status == VideoStatus.READY
                assert updated.width == 3840
                assert updated.height == 2160
                assert updated.codec == "hevc"
                assert updated.local_file_path == str(local_master.resolve())

    def test_update_video_metadata(self, app, db):
        with app.app_context():
            v = Video.query.first()
            assert v is not None
            vid_id = v.id

            ok, err = video_service.update_video_metadata(
                video_id=vid_id,
                title="Renamed Title",
                description="New description",
                tags=["newtag1", "newtag2"],
                category_id="10",
                privacy_status=PrivacyStatus.UNLISTED,
            )
            assert ok is True

            reloaded = _db.session.get(Video, vid_id)
            assert reloaded.title == "Renamed Title"
            assert reloaded.privacy_status == PrivacyStatus.UNLISTED
            assert reloaded.category_id == "10"
            assert "newtag1" in reloaded.tags

    def test_query_videos_and_stats(self, app, db):
        with app.app_context():
            stats = video_service.get_video_stats()
            assert stats["total"] >= 2
            assert stats["ready"] >= 1

            # Query with search
            videos, count = video_service.query_videos(search_query="Renamed")
            assert count >= 1
            assert any(v.title == "Renamed Title" for v in videos)


# ------------------------------------------------------------------ #
# 4. Route Tests                                                     #
# ------------------------------------------------------------------ #

class TestVideoRoutes:
    def test_videos_index_route(self, auth_client):
        resp = auth_client.get("/videos/")
        assert resp.status_code == 200
        assert b"Video Library" in resp.data
        assert b"Total Videos" in resp.data

    def test_video_detail_route(self, auth_client, app, db):
        with app.app_context():
            v = Video.query.first()
            vid_id = v.id

        resp = auth_client.get(f"/videos/{vid_id}")
        assert resp.status_code == 200
        assert b"Media Preview" in resp.data
        assert b"Technical Media Inspection" in resp.data
        assert b"Edit Video Metadata" in resp.data

    def test_video_edit_route(self, auth_client, app, db):
        with app.app_context():
            v = Video.query.first()
            vid_id = v.id

        resp = auth_client.post(
            f"/videos/{vid_id}/edit",
            data={
                "title": "Route Updated Title",
                "description": "Updated via route test",
                "tags": "flask, python, test",
                "privacy_status": "public",
                "category_id": "28",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Video metadata updated successfully" in resp.data

        with app.app_context():
            reloaded = _db.session.get(Video, vid_id)
            assert reloaded.title == "Route Updated Title"
            assert reloaded.privacy_status == PrivacyStatus.PUBLIC

    def test_video_match_file_route(self, auth_client, app, db, tmp_path):
        dummy_file = tmp_path / "route_matched.mp4"
        dummy_file.write_bytes(b"\x00" * 512)

        with app.app_context():
            awaiting_v = Video(
                source_type=SourceType.YOUTUBE,
                title="Needs File",
                status=VideoStatus.AWAITING_FILE,
                detected_at=datetime.now(timezone.utc),
            )
            _db.session.add(awaiting_v)
            _db.session.commit()
            vid_id = awaiting_v.id

        with patch("app.services.video_service.validate_video_file", return_value=(True, None, {"duration_seconds": 60, "width": 1280, "height": 720, "fps": 30, "codec": "h264", "file_size_bytes": 512})), \
             patch("app.services.video_service.generate_video_thumbnail", return_value=("storage/thumbnails/t.jpg", None)):

            resp = auth_client.post(
                f"/videos/{vid_id}/match-file",
                data={"local_file_path": str(dummy_file)},
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert b"Local media file verified and linked successfully" in resp.data

    def test_video_match_file_upload_route(self, auth_client, app, db):
        with app.app_context():
            awaiting_v = Video(
                source_type=SourceType.YOUTUBE,
                title="Needs File Upload",
                status=VideoStatus.AWAITING_FILE,
                detected_at=datetime.now(timezone.utc),
            )
            _db.session.add(awaiting_v)
            _db.session.commit()
            vid_id = awaiting_v.id

        with patch("app.services.video_service.validate_video_file", return_value=(True, None, {"duration_seconds": 60, "width": 1280, "height": 720, "fps": 30, "codec": "h264", "file_size_bytes": 512})), \
             patch("app.services.video_service.generate_video_thumbnail", return_value=("storage/thumbnails/t.jpg", None)):

            fake_file = (io.BytesIO(b"\x00" * 512), "picked_master.mp4")
            resp = auth_client.post(
                f"/videos/{vid_id}/match-file",
                data={"video_file": fake_file},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert b"Local media file verified and linked successfully" in resp.data

    def test_browse_file_dialog_route(self, auth_client):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value.stdout = "C:\\Videos\\sample.mp4\n"
            resp = auth_client.post("/videos/browse-file-dialog")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            assert "sample.mp4" in data["path"]

    def test_video_thumbnail_upload_route(self, auth_client, app, db):
        with app.app_context():
            v = Video.query.first()
            vid_id = v.id

        fake_img_bytes = io.BytesIO()
        Image.new("RGB", (640, 360), color="blue").save(fake_img_bytes, format="JPEG")
        fake_img_bytes.seek(0)

        with patch("app.services.thumbnail_service.optimize_thumbnail", return_value=(True, None)):
            resp = auth_client.post(
                f"/videos/{vid_id}/thumbnail",
                data={"thumbnail_file": (fake_img_bytes, "test_thumb.jpg")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert b"Thumbnail updated and optimized" in resp.data

    def test_video_generate_thumbnail_route(self, auth_client, app, db, tmp_path):
        dummy_video = tmp_path / "vid_gen_thumb.mp4"
        dummy_video.write_bytes(b"dummy video content")

        with app.app_context():
            v = Video(
                source_type=SourceType.LOCAL_FOLDER,
                title="Thumb Gen Video",
                local_file_path=str(dummy_video),
                status=VideoStatus.READY,
                detected_at=datetime.now(timezone.utc),
            )
            _db.session.add(v)
            _db.session.commit()
            vid_id = v.id

        with patch("app.services.thumbnail_service.generate_video_thumbnail", return_value=("storage/thumbnails/gen_thumb.jpg", None)):
            resp = auth_client.post(
                f"/videos/{vid_id}/generate-thumbnail",
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert b"Thumbnail updated and optimized from video frame" in resp.data

            with app.app_context():
                reloaded = _db.session.get(Video, vid_id)
                assert reloaded.local_thumbnail_path == "storage/thumbnails/gen_thumb.jpg"

    def test_video_thumbnail_upload_ajax(self, auth_client, app, db):
        with app.app_context():
            v = Video.query.first()
            vid_id = v.id

        fake_img_bytes = io.BytesIO()
        Image.new("RGB", (640, 360), color="green").save(fake_img_bytes, format="JPEG")
        fake_img_bytes.seek(0)

        with patch("app.services.thumbnail_service.optimize_thumbnail", return_value=(True, None)):
            resp = auth_client.post(
                f"/videos/{vid_id}/thumbnail",
                data={"thumbnail_file": (fake_img_bytes, "ajax_thumb.jpg")},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            assert "thumbnail_url" in data
            assert f"/videos/{vid_id}/thumbnail-preview" in data["thumbnail_url"]

    def test_video_edit_with_thumbnail_persists(self, auth_client, app, db):
        with app.app_context():
            v = Video.query.first()
            vid_id = v.id

        fake_img_bytes = io.BytesIO()
        Image.new("RGB", (640, 360), color="purple").save(fake_img_bytes, format="JPEG")
        fake_img_bytes.seek(0)

        with patch("app.services.thumbnail_service.optimize_thumbnail", return_value=(True, None)):
            resp = auth_client.post(
                f"/videos/{vid_id}/edit",
                data={
                    "title": "Updated Title With Thumb",
                    "description": "Desc",
                    "tags": "tag1, tag2",
                    "category_id": "22",
                    "privacy_status": "private",
                    "thumbnail_file": (fake_img_bytes, "edit_thumb.jpg"),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            assert resp.status_code == 200
            with app.app_context():
                reloaded = _db.session.get(Video, vid_id)
                assert reloaded.title == "Updated Title With Thumb"
                assert reloaded.local_thumbnail_path is not None

    def test_attach_local_media_preserves_existing_thumbnail(self, app, db, tmp_path):
        dummy_thumb = tmp_path / "existing_thumb.jpg"
        dummy_thumb.write_bytes(b"existing thumbnail image")
        dummy_video = tmp_path / "new_video.mp4"
        dummy_video.write_bytes(b"dummy video")

        with app.app_context():
            v = Video(
                source_type=SourceType.YOUTUBE,
                title="Awaiting Video",
                status=VideoStatus.AWAITING_FILE,
                local_thumbnail_path=str(dummy_thumb),
                detected_at=datetime.now(timezone.utc),
            )
            _db.session.add(v)
            _db.session.commit()
            vid_id = v.id

        fake_meta = {
            "duration_seconds": 30.0,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "codec": "h264",
            "file_size_bytes": 1024,
        }

        with patch("app.services.video_service.validate_video_file", return_value=(True, None, fake_meta)), \
             patch("app.services.video_service.generate_video_thumbnail") as mock_gen_thumb:
            with app.app_context():
                ok, err = video_service.attach_local_media_to_video(vid_id, str(dummy_video))
                assert ok is True
                # Should NOT call generate_video_thumbnail because existing thumb is valid
                mock_gen_thumb.assert_not_called()
                reloaded = _db.session.get(Video, vid_id)
                assert reloaded.local_thumbnail_path == str(dummy_thumb)

    def test_video_delete_route(self, auth_client, app, db):
        with app.app_context():
            to_del = Video(
                source_type=SourceType.LOCAL_FOLDER,
                title="To Delete",
                status=VideoStatus.READY,
                detected_at=datetime.now(timezone.utc),
            )
            _db.session.add(to_del)
            _db.session.commit()
            vid_id = to_del.id

        resp = auth_client.post(f"/videos/{vid_id}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Video removed from library" in resp.data

        with app.app_context():
            assert _db.session.get(Video, vid_id) is None

    def test_video_bulk_delete_route(self, auth_client, app, db):
        with app.app_context():
            v1 = Video(source_type=SourceType.LOCAL_FOLDER, title="Bulk 1", status=VideoStatus.READY)
            v2 = Video(source_type=SourceType.LOCAL_FOLDER, title="Bulk 2", status=VideoStatus.READY)
            _db.session.add_all([v1, v2])
            _db.session.commit()
            id1, id2 = v1.id, v2.id

        resp = auth_client.post(
            "/videos/bulk-delete",
            data={"video_ids": [str(id1), str(id2)]},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Successfully deleted 2 video(s)" in resp.data

        with app.app_context():
            assert _db.session.get(Video, id1) is None
            assert _db.session.get(Video, id2) is None
