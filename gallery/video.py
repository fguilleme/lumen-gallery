from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from .exif import ExifTool, _clean_exif, _date, _first, VIDEO_EXTENSIONS


VIDEO_PROBE_TIMEOUT_SECONDS = 30


class VideoProbeError(RuntimeError):
    pass


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _probe(path: Path, ffprobe_path: str | Path) -> dict[str, Any]:
    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=VIDEO_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoProbeError(f"ffprobe failed for {path.name}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise VideoProbeError(f"ffprobe metadata failed for {path.name}: {detail or 'unknown error'}")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise VideoProbeError(f"ffprobe returned invalid metadata for {path.name}") from exc
    if not isinstance(payload, dict):
        raise VideoProbeError(f"ffprobe returned invalid metadata for {path.name}")
    return payload


def _rotation_from_display_matrix(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    rows: list[list[float]] = []
    for line in value.splitlines():
        if ":" not in line:
            continue
        numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", line.split(":", 1)[1])
        if numbers:
            rows.append([float(number) for number in numbers])
    if len(rows) < 2 or len(rows[0]) < 2 or len(rows[1]) < 1:
        return None
    angle = math.degrees(math.atan2(-rows[0][1], rows[0][0]))
    if abs(angle) < 0.5 and abs(rows[1][0]) > 0.5:
        angle = math.degrees(math.atan2(rows[1][0], rows[0][0]))
    return angle


def _rotation(stream: dict[str, Any]) -> int:
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if not isinstance(item, dict):
                continue
            value = _number(item.get("rotation"))
            if value is None:
                value = _rotation_from_display_matrix(item.get("displaymatrix"))
            if value is not None:
                return int(round(value)) % 360
    tags = stream.get("tags")
    if isinstance(tags, dict):
        value = _number(tags.get("rotate"))
        if value is not None:
            return int(round(value)) % 360
    return 0


def _stream_tags(stream: dict[str, Any], format_data: dict[str, Any]) -> dict[str, Any]:
    tags: dict[str, Any] = {}
    format_tags = format_data.get("tags")
    if isinstance(format_tags, dict):
        tags.update(format_tags)
    stream_tags = stream.get("tags")
    if isinstance(stream_tags, dict):
        tags.update(stream_tags)
    return tags


def _compact_exif(
    exiftool_metadata: dict[str, Any],
    *,
    video_stream: dict[str, Any],
    audio_stream: dict[str, Any] | None,
    duration: float | None,
    rotation: int,
    creation_time: Any,
) -> dict[str, Any]:
    allowed = {
        "Make",
        "Model",
        "LensModel",
        "LensID",
        "CreateDate",
        "ModifyDate",
        "MediaCreateDate",
        "TrackCreateDate",
        "Rotation",
    }
    compact = {
        key: value
        for key, value in _clean_exif(exiftool_metadata).items()
        if key in allowed
    }
    compact.update(
        {
            "VideoCodec": video_stream.get("codec_name"),
            "VideoPixelFormat": video_stream.get("pix_fmt"),
            "Rotation": rotation,
        }
    )
    if audio_stream is not None:
        compact["AudioCodec"] = audio_stream.get("codec_name")
    if duration is not None:
        compact["Duration"] = duration
    if creation_time not in (None, ""):
        compact["CreationTime"] = creation_time
    return {key: value for key, value in compact.items() if value not in (None, "")}


def inspect_video(
    path: Path,
    tool: ExifTool,
    *,
    ffprobe_path: str | Path,
    name: str | None = None,
) -> dict[str, Any]:
    stat = path.stat()
    probe = _probe(path, ffprobe_path)
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise VideoProbeError(f"ffprobe found no streams for {path.name}")
    video_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise VideoProbeError(f"ffprobe found no video stream for {path.name}")
    audio_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    format_data = probe.get("format")
    if not isinstance(format_data, dict):
        format_data = {}
    duration = _number(video_stream.get("duration")) or _number(format_data.get("duration"))
    if duration is not None and duration < 0:
        duration = None
    rotation = _rotation(video_stream)
    width = int(video_stream.get("width"))
    height = int(video_stream.get("height"))
    if rotation % 180:
        width, height = height, width

    try:
        exiftool_metadata = tool.read_metadata(path)
    except Exception:
        exiftool_metadata = {}
    if not isinstance(exiftool_metadata, dict):
        exiftool_metadata = {}
    tags = _stream_tags(video_stream, format_data)
    date_value = _first(
        exiftool_metadata,
        "DateTimeOriginal",
        "CreateDate",
        "DateCreated",
        "MediaCreateDate",
        "TrackCreateDate",
    )
    if str(date_value or "").startswith(("0000:00:00", "0000-00-00")):
        date_value = None
    date_value = date_value or _first(tags, "creation_time", "CreationTime")
    camera = _first(
        exiftool_metadata,
        "Model",
        "CameraModelName",
        "Make",
    ) or _first(tags, "model", "com.apple.quicktime.model", "make", "com.apple.quicktime.make")
    lens = _first(
        exiftool_metadata,
        "LensModel",
        "Lens",
        "LensID",
    ) or _first(tags, "lens_model", "com.apple.quicktime.lens_model")
    return {
        "name": name or path.name,
        "format": path.suffix[1:].upper(),
        "date": _date(date_value, stat.st_mtime),
        "camera": str(camera or ""),
        "lens": str(lens or ""),
        "iso": None,
        "aperture": None,
        "shutter": "",
        "focal": None,
        "width": width,
        "height": height,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "media_type": "video",
        "duration": duration,
        "exif": _compact_exif(
            exiftool_metadata,
            video_stream=video_stream,
            audio_stream=audio_stream,
            duration=duration,
            rotation=rotation,
            creation_time=date_value,
        ),
    }
