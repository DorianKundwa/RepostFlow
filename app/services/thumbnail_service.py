"""
app/services/thumbnail_service.py - Thumbnail generation, formatting, and optimization.

Features:
  - Generate thumbnail from video file via FFmpeg frame capture.
  - Resize & crop/letterbox to YouTube's recommended 1280x720 aspect ratio (16:9).
  - Optimize file size using Pillow (JPEG, quality 88-92, max 2MB as per YouTube specs).
  - Process user-uploaded custom thumbnails safely.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

from app.services.media_service import extract_video_frame

logger = logging.getLogger(__name__)

# Standard YouTube thumbnail dimensions
YT_THUMB_WIDTH = 1280
YT_THUMB_HEIGHT = 720
MAX_THUMB_FILE_SIZE = 2 * 1024 * 1024  # 2MB YouTube limit


def optimize_thumbnail(source_image_path: str, target_image_path: str) -> Tuple[bool, Optional[str]]:
    """
    Load an image with Pillow, resize to 1280x720 (16:9) with high-quality resampling,
    and save as an optimized JPEG.
    """
    try:
        with Image.open(source_image_path) as img:
            # Convert RGBA / P modes to RGB for JPEG compatibility
            if img.mode in ("RGBA", "LA", "P"):
                rgb_img = Image.new("RGB", img.size, (0, 0, 0))
                if img.mode == "P":
                    img = img.convert("RGBA")
                rgb_img.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
                img = rgb_img
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # Fit into 1280x720 preserving aspect ratio with black letterbox if needed
            target_w, target_h = YT_THUMB_WIDTH, YT_THUMB_HEIGHT
            img_ratio = img.width / img.height
            target_ratio = target_w / target_h

            if abs(img_ratio - target_ratio) < 0.02:
                # Direct resize
                resized = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            else:
                # Resize and letterbox/pillarbox
                scale = min(target_w / img.width, target_h / img.height)
                new_w = int(img.width * scale)
                new_h = int(img.height * scale)
                scaled_img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                canvas = Image.new("RGB", (target_w, target_h), (15, 15, 15))
                offset_x = (target_w - new_w) // 2
                offset_y = (target_h - new_h) // 2
                canvas.paste(scaled_img, (offset_x, offset_y))
                resized = canvas

            out_path = Path(target_image_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)

            # Save as optimized JPEG
            resized.save(out_path, format="JPEG", quality=90, optimize=True)

            # Check file size limit
            if os.path.getsize(out_path) > MAX_THUMB_FILE_SIZE:
                resized.save(out_path, format="JPEG", quality=80, optimize=True)

        return True, None
    except Exception as exc:
        logger.error("Failed to optimize thumbnail: %s", exc)
        return False, str(exc)


def generate_video_thumbnail(
    video_path: str,
    output_dir: str,
    identifier: str,
    timestamp_seconds: float = 1.0,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract frame from video and generate an optimized 1280x720 JPEG thumbnail.

    Returns:
        (relative_or_absolute_file_path, None) on success
        (None, error_message) on failure
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    temp_raw = out_dir / f"raw_{identifier}_{uuid.uuid4().hex[:8]}.jpg"
    final_path = out_dir / f"thumb_{identifier}.jpg"

    ok, err = extract_video_frame(video_path, str(temp_raw), timestamp_seconds=timestamp_seconds)
    if not ok:
        if temp_raw.exists():
            temp_raw.unlink(missing_ok=True)
        return None, err or "Frame extraction failed."

    ok, opt_err = optimize_thumbnail(str(temp_raw), str(final_path))
    temp_raw.unlink(missing_ok=True)

    if not ok:
        return None, opt_err or "Thumbnail optimization failed."

    return str(final_path.resolve()), None


def process_uploaded_thumbnail(
    file_storage_or_path: Any,
    output_dir: str,
    identifier: str,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Process an uploaded thumbnail file (from Flask request.files or local path).

    Returns:
        (final_file_path, None) on success
        (None, error_message) on failure
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    temp_path = out_dir / f"upload_raw_{identifier}_{uuid.uuid4().hex[:8]}.tmp"
    final_path = out_dir / f"custom_{identifier}_{uuid.uuid4().hex[:8]}.jpg"

    try:
        if hasattr(file_storage_or_path, "save"):
            file_storage_or_path.save(str(temp_path))
        else:
            with open(file_storage_or_path, "rb") as sf, open(temp_path, "wb") as df:
                df.write(sf.read())

        ok, err = optimize_thumbnail(str(temp_path), str(final_path))
        temp_path.unlink(missing_ok=True)

        if not ok:
            return None, err or "Could not process image."

        return str(final_path.resolve()), None
    except Exception as exc:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return None, f"Image processing error: {exc}"
