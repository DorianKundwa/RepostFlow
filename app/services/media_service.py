"""
app/services/media_service.py - Media inspection and validation using FFprobe and FFmpeg.

Features:
  - Inspect media files using ffprobe (duration, resolution, fps, codecs, bitrate).
  - Validate that video files are complete, non-corrupted, and have valid video streams.
  - Extract preview thumbnail frames using ffmpeg.
  - Safe error handling if FFmpeg / FFprobe is missing or file is corrupted.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def is_ffprobe_available() -> bool:
    """Check if ffprobe executable is found in PATH."""
    return shutil.which("ffprobe") is not None


def is_ffmpeg_available() -> bool:
    """Check if ffmpeg executable is found in PATH."""
    return shutil.which("ffmpeg") is not None


def _parse_fps(fps_str: str) -> Optional[float]:
    """Parse FPS string like '30/1', '60000/1001', or '29.97'."""
    if not fps_str or fps_str == "0/0":
        return None
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/", 1)
            num_f = float(num)
            den_f = float(den)
            return round(num_f / den_f, 2) if den_f != 0 else None
        return round(float(fps_str), 2)
    except (ValueError, ZeroDivisionError):
        return None


def inspect_media(file_path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Run ffprobe on a video file and return structured technical metadata.

    Returns:
        (metadata_dict, None) on success
        (None, error_message) on failure
    """
    path_obj = Path(file_path)
    if not path_obj.is_file():
        return None, f"File not found: {file_path}"

    if not is_ffprobe_available():
        return None, "ffprobe executable not found on system PATH."

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration,size,bit_rate,format_name",
        "-show_streams",
        "-of", "json",
        str(path_obj.resolve()),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or "Unknown ffprobe error"
            logger.error("ffprobe failed on %s: %s", file_path, err)
            return None, f"FFprobe error: {err}"

        data = json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        return None, "FFprobe execution timed out (30s)."
    except json.JSONDecodeError as exc:
        return None, f"Failed to parse FFprobe JSON output: {exc}"
    except Exception as exc:
        return None, f"Unexpected error running FFprobe: {exc}"

    streams = data.get("streams", [])
    format_info = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video_stream:
        return None, "No video stream detected in file."

    # Parse duration (prefer format duration, fallback to stream duration)
    raw_duration = format_info.get("duration") or video_stream.get("duration")
    duration_sec = None
    if raw_duration:
        try:
            duration_sec = round(float(raw_duration), 2)
        except ValueError:
            pass

    # Resolution
    width = video_stream.get("width")
    height = video_stream.get("height")
    if width is not None:
        width = int(width)
    if height is not None:
        height = int(height)

    # Frame rate
    fps = _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))

    # Codecs
    video_codec = video_stream.get("codec_name")
    audio_codec = audio_stream.get("codec_name") if audio_stream else None

    # Bitrate & size
    file_size = int(format_info.get("size", os.path.getsize(path_obj)))
    bitrate = format_info.get("bit_rate")
    bitrate_kbps = int(bitrate) // 1000 if bitrate and bitrate.isdigit() else None

    metadata = {
        "duration_seconds": duration_sec,
        "width": width,
        "height": height,
        "fps": fps,
        "codec": video_codec,
        "audio_codec": audio_codec,
        "file_size_bytes": file_size,
        "bitrate_kbps": bitrate_kbps,
        "container_format": format_info.get("format_name"),
    }
    return metadata, None


def validate_video_file(file_path: str, allowed_extensions: Optional[set] = None) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """
    Validate that the file is an accessible, valid video container with a playable video stream.

    Returns:
        (is_valid, error_message_or_None, metadata_or_None)
    """
    path_obj = Path(file_path)
    if not path_obj.is_file():
        return False, "File does not exist.", None

    if allowed_extensions:
        if path_obj.suffix.lower() not in allowed_extensions:
            return False, f"Extension {path_obj.suffix} not supported. Allowed: {', '.join(sorted(allowed_extensions))}", None

    file_size = os.path.getsize(path_obj)
    if file_size == 0:
        return False, "File is empty (0 bytes).", None

    metadata, err = inspect_media(file_path)
    if err or not metadata:
        return False, err or "Failed to inspect media.", None

    if not metadata.get("width") or not metadata.get("height"):
        return False, "Invalid video resolution detected.", None

    if metadata.get("duration_seconds") is not None and metadata["duration_seconds"] <= 0:
        return False, "Video duration must be greater than zero.", None

    return True, None, metadata


def extract_video_frame(
    video_path: str,
    output_image_path: str,
    timestamp_seconds: float = 1.0,
) -> Tuple[bool, Optional[str]]:
    """
    Use ffmpeg to capture a single frame at the given timestamp and save as an image.

    Returns:
        (success, error_message_or_None)
    """
    if not is_ffmpeg_available():
        return False, "ffmpeg executable not found on system PATH."

    out_p = Path(output_image_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(max(0.0, timestamp_seconds)),
        "-i", str(Path(video_path).resolve()),
        "-vframes", "1",
        "-q:v", "2",
        str(out_p.resolve()),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or "ffmpeg frame extraction failed"
            logger.error("ffmpeg error extracting frame from %s: %s", video_path, err)
            return False, err
        if not out_p.is_file() or os.path.getsize(out_p) == 0:
            return False, "Extracted frame file is empty or missing."
        return True, None
    except Exception as exc:
        logger.error("extract_video_frame exception: %s", exc)
        return False, str(exc)
