from __future__ import annotations

import json
import os
import tempfile
import threading
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from .config import Settings
from .exif import ExifTool, orient_image, preview_bytes


class MediaNotFound(FileNotFoundError):
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
        self._lock = threading.Lock()

    def _cache_path(self, kind: str, row: dict[str, Any]) -> Path:
        directory = self.settings.cache_dir / kind
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{int(row['id'])}-{int(row['mtime_ns'])}-{int(row['size'])}.jpg"

    def jpeg(self, kind: str, row: dict[str, Any]) -> Path:
        if kind not in {"thumb", "preview"}:
            raise ValueError("invalid media kind")
        output = self._cache_path(kind, row)
        if output.is_file() and output.stat().st_size > 0:
            return output
        source = resolve_photo_path(self.settings, row)
        try:
            metadata = json.loads(str(row["exif_json"]))
        except (TypeError, ValueError):
            metadata = {}
        data = preview_bytes(source, self.exiftool)
        with self._lock:
            if output.is_file() and output.stat().st_size > 0:
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
                with tempfile.NamedTemporaryFile(
                    mode="wb", suffix=".tmp", dir=output.parent, delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    image.save(handle, format="JPEG", quality=88, optimize=True)
            os.replace(temporary, output)
        return output
