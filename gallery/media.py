from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from .config import Settings
from .exif import ExifTool, VIDEO_EXTENSIONS, orient_image, preview_bytes


FFMPEG_TIMEOUT_SECONDS = 120
MAX_FFMPEG_JOBS = 2


class MediaNotFound(FileNotFoundError):
    pass


class MediaConversionError(RuntimeError):
    pass


def resolve_photo_path(settings: Settings, row: dict[str, Any]) -> Path:
    candidate = Path(str(row["path"]))
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise MediaNotFound("Photo file is unavailable")
    for root in settings.roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    raise MediaNotFound("Photo file is unavailable")


class MediaCache:
    def __init__(self, settings: Settings, exiftool: ExifTool):
        self.settings = settings
        self.exiftool = exiftool
        self._locks_guard = threading.Lock()
        self._target_locks: dict[str, threading.Lock] = {}
        self._ffmpeg_slots = threading.BoundedSemaphore(MAX_FFMPEG_JOBS)

    def _target_lock(self, target: Path) -> threading.Lock:
        key = str(target)
        with self._locks_guard:
            return self._target_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _usable(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _cache_path(self, kind: str, row: dict[str, Any]) -> Path:
        directory = self.settings.cache_dir / kind
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{int(row['id'])}-{int(row['mtime_ns'])}-{int(row['size'])}.jpg"

    def _stream_cache_path(self, row: dict[str, Any]) -> Path:
        directory = self.settings.cache_dir / "stream"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{int(row['id'])}-{int(row['mtime_ns'])}-{int(row['size'])}.mp4"

    @staticmethod
    def _video_timestamp(row: dict[str, Any]) -> float:
        try:
            duration = float(row.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        return min(1.0, max(0.0, duration * 0.1)) if duration > 0 else 1.0

    def _run_ffmpeg(self, arguments: list[str], *, path: Path) -> None:
        command = [self.settings.ffmpeg_path or "ffmpeg", *arguments]
        try:
            with self._ffmpeg_slots:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    timeout=FFMPEG_TIMEOUT_SECONDS,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaConversionError(f"FFmpeg failed for {path.name}: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 500:
                detail = detail[-500:]
            raise MediaConversionError(
                f"FFmpeg could not prepare {path.name}: {detail or 'unknown error'}"
            )

    @staticmethod
    def _temporary_path(directory: Path, suffix: str) -> Path:
        fd, name = tempfile.mkstemp(prefix=".lumen-", suffix=suffix, dir=directory)
        os.close(fd)
        return Path(name)

    def _video_jpeg(self, kind: str, row: dict[str, Any]) -> Path:
        output = self._cache_path(kind, row)
        if self._usable(output):
            return output
        source = resolve_photo_path(self.settings, row)
        max_size = 480 if kind == "thumb" else 2048
        target_lock = self._target_lock(output)
        with target_lock:
            if self._usable(output):
                return output
            temporary = self._temporary_path(output.parent, ".jpg")
            try:
                self._run_ffmpeg(
                    [
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-ss",
                        f"{self._video_timestamp(row):.6f}",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale=w='min({max_size},iw)':h='min({max_size},ih)':force_original_aspect_ratio=decrease",
                        "-q:v",
                        "3",
                        "-an",
                        "-f",
                        "image2",
                        str(temporary),
                    ],
                    path=source,
                )
                if not self._usable(temporary):
                    raise MediaConversionError(f"FFmpeg produced no preview for {source.name}")
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
        return output

    def jpeg(self, kind: str, row: dict[str, Any]) -> Path:
        if kind not in {"thumb", "preview"}:
            raise ValueError("invalid media kind")
        if str(row.get("media_type") or "photo") == "video" or Path(str(row["path"])).suffix.lower() in VIDEO_EXTENSIONS:
            return self._video_jpeg(kind, row)

        output = self._cache_path(kind, row)
        if self._usable(output):
            return output
        source = resolve_photo_path(self.settings, row)
        try:
            metadata = json.loads(str(row["exif_json"]))
        except (TypeError, ValueError):
            metadata = {}
        data = preview_bytes(source, self.exiftool)
        target_lock = self._target_lock(output)
        with target_lock:
            if self._usable(output):
                return output
            with Image.open(BytesIO(data)) as opened:
                image = orient_image(opened, metadata.get("Orientation"))
                image.load()
                if kind == "thumb":
                    image.thumbnail((480, 480), Image.Resampling.LANCZOS)
                else:
                    image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                if image.mode != "RGB":
                    image = image.convert("RGB")
                temporary = self._temporary_path(output.parent, ".jpg")
                try:
                    image.save(temporary, format="JPEG", quality=88, optimize=True)
                    os.replace(temporary, output)
                finally:
                    temporary.unlink(missing_ok=True)
        return output

    @staticmethod
    def _video_codecs(row: dict[str, Any]) -> tuple[str, str]:
        try:
            metadata = json.loads(str(row.get("exif_json") or "{}"))
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return str(metadata.get("VideoCodec") or "").lower(), str(metadata.get("AudioCodec") or "").lower()

    def _direct_stream(self, row: dict[str, Any]) -> bool:
        suffix = Path(str(row["path"])).suffix.lower()
        video_codec, audio_codec = self._video_codecs(row)
        return suffix in {".mp4", ".m4v"} and video_codec in {"h264", "avc1"} and audio_codec in {"", "aac"}

    def _remux_or_transcode(self, source: Path, output: Path, row: dict[str, Any]) -> None:
        video_codec, audio_codec = self._video_codecs(row)
        arguments = [
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            # Browser cache keeps the primary audio track only. Modern MOVs
            # may also contain Apple spatial/APAC tracks unsupported in MP4.
            "0:a:0?",
        ]
        if video_codec in {"h264", "avc1"}:
            arguments.extend(["-c:v", "copy"])
        else:
            arguments.extend(["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"])
        if audio_codec == "aac":
            arguments.extend(["-c:a", "copy"])
        else:
            arguments.extend(["-c:a", "aac"])
        arguments.extend(["-map_metadata", "0", "-movflags", "+faststart", str(output)])
        self._run_ffmpeg(arguments, path=source)

    def stream_path(self, row: dict[str, Any]) -> Path:
        if str(row.get("media_type") or "photo") != "video":
            raise MediaNotFound("Stream unavailable for photos")
        source = resolve_photo_path(self.settings, row)
        if self._direct_stream(row):
            return source
        output = self._stream_cache_path(row)
        if self._usable(output):
            return output
        target_lock = self._target_lock(output)
        with target_lock:
            if self._usable(output):
                return output
            temporary = self._temporary_path(output.parent, ".mp4")
            try:
                self._remux_or_transcode(source, temporary, row)
                if not self._usable(temporary):
                    raise MediaConversionError(f"FFmpeg produced no stream for {source.name}")
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
        return output
