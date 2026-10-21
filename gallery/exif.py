from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps


JPEG_EXTENSIONS = {".jpg", ".jpeg"}
RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".crw",
    ".dcr",
    ".dng",
    ".erf",
    ".iiq",
    ".k25",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".srw",
    ".x3f",
}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".webm"}
SUPPORTED_EXTENSIONS = JPEG_EXTENSIONS | RAW_EXTENSIONS | VIDEO_EXTENSIONS


class ExifToolError(RuntimeError):
    pass


class ExifTool:
    def __init__(self, executable: Path, perl: str = "perl"):
        self.executable = Path(executable)
        self.perl = perl

    def _run(self, *arguments: str, path: Path) -> subprocess.CompletedProcess[bytes]:
        command = [
            self.perl,
            str(self.executable),
            "-api",
            "LargeFileSupport=1",
            "-charset",
            "filename=utf8",
            *arguments,
            str(path),
        ]
        try:
            return subprocess.run(command, capture_output=True, check=False, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExifToolError(f"ExifTool failed for {path.name}: {exc}") from exc

    def read_metadata(self, path: Path) -> dict[str, Any]:
        result = self._run("-j", "-n", path=path)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ExifToolError(f"ExifTool metadata failed for {path.name}: {detail or 'unknown error'}")
        try:
            values = json.loads(result.stdout.decode("utf-8"))
            item = values[0]
        except (ValueError, IndexError, TypeError) as exc:
            raise ExifToolError(f"ExifTool returned invalid metadata for {path.name}") from exc
        if not isinstance(item, dict):
            raise ExifToolError(f"ExifTool returned invalid metadata for {path.name}")
        return item

    def extract_preview(self, path: Path) -> bytes:
        for tag in ("-JpgFromRaw", "-PreviewImage"):
            result = self._run("-b", tag, path=path)
            data = result.stdout
            if result.returncode == 0 and data.startswith(b"\xff\xd8"):
                return data
        raise ExifToolError(f"No embedded JPEG preview for {path.name}")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if "/" in text:
            return float(Fraction(text.replace(" ", "")))
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None
    except (ValueError, ZeroDivisionError):
        return None


def _nice_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value.is_integer() else value


def _shutter(value: Any) -> str:
    numeric = _number(value)
    if numeric is None:
        return str(value or "")
    if numeric > 0 and numeric < 1:
        fraction = Fraction(numeric).limit_denominator(10000)
        return f"{fraction.numerator}/{fraction.denominator}"
    if numeric.is_integer():
        return f"{int(numeric)}s"
    return f"{numeric:g}s"


def _date(value: Any, fallback_timestamp: float) -> str:
    text = str(value or "").strip()
    if text.startswith(("0000:00:00", "0000-00-00")):
        text = ""
    if text:
        match = re.match(r"^(\d{4}):(\d{2}):(\d{2})(.*)$", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}T{match.group(4).lstrip()}"
        try:
            return datetime.fromisoformat(text.replace(" ", "T")).isoformat()
        except ValueError:
            return text
    return datetime.fromtimestamp(fallback_timestamp, tz=timezone.utc).replace(microsecond=0).isoformat()


def _first(metadata: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = metadata.get(key)
        if value not in (None, "", "-"):
            return value
    return None


def _clean_exif(metadata: dict[str, Any]) -> dict[str, Any]:
    hidden = {
        "sourcefile",
        "directory",
        "filename",
        "filepath",
        "exiftoolversion",
    }
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if key.lower() in hidden:
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        cleaned[key] = value
    return cleaned


def orientation_value(image: Image.Image, fallback: Any = None) -> int | None:
    try:
        value = image.getexif().get(274)
    except (AttributeError, ValueError):
        value = None
    numeric = _number(value if value is not None else fallback)
    return int(numeric) if numeric is not None else None


def orient_image(image: Image.Image, fallback_orientation: Any = None) -> Image.Image:
    orientation = orientation_value(image, fallback_orientation)
    methods = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    if orientation in methods:
        return image.transpose(methods[orientation])
    return ImageOps.exif_transpose(image)


def preview_bytes(path: Path, tool: ExifTool) -> bytes:
    if path.suffix.lower() in JPEG_EXTENSIONS:
        return path.read_bytes()
    return tool.extract_preview(path)


def inspect_file(path: Path, tool: ExifTool, *, name: str | None = None) -> dict[str, Any]:
    stat = path.stat()
    metadata = tool.read_metadata(path)
    raw_orientation = _first(metadata, "Orientation", "CameraOrientation")
    data = preview_bytes(path, tool)
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if orientation_value(image, raw_orientation) in (5, 6, 7, 8):
            width, height = height, width

    camera = _first(metadata, "Model", "CameraModelName", "Make")
    lens = _first(metadata, "LensModel", "Lens", "LensID")
    iso = _number(_first(metadata, "ISO", "PhotographicSensitivity"))
    aperture = _number(_first(metadata, "FNumber", "Aperture"))
    focal = _number(_first(metadata, "FocalLength"))
    exposure = _first(metadata, "ExposureTime", "ShutterSpeed")
    return {
        "name": name or path.name,
        "format": "JPEG" if path.suffix.lower() in JPEG_EXTENSIONS else path.suffix[1:].upper(),
        "date": _date(_first(metadata, "DateTimeOriginal", "CreateDate", "DateCreated"), stat.st_mtime),
        "camera": str(camera or ""),
        "lens": str(lens or ""),
        "iso": _nice_number(iso),
        "aperture": _nice_number(aperture),
        "shutter": _shutter(exposure),
        "focal": _nice_number(focal),
        "width": width,
        "height": height,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "exif": _clean_exif(metadata),
    }
