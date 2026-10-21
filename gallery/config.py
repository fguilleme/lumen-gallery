from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_UPLOAD_BYTES = 250 * 1024 * 1024
AUTH_DISABLED_VALUES = {"false", "0", "no", "off"}


def _resolve_path(value: Path | str | None, default: Path, project_dir: Path) -> Path:
    path = Path(value if value is not None else default).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return path.resolve()


def _read_or_create_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    if not value:
        value = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    os.chmod(path, 0o600)
    return value


@dataclass
class Settings:
    project_dir: Path
    photo_root: Path | None = None
    data_dir: Path | None = None
    upload_dir: Path | None = None
    db_path: Path | None = None
    cache_dir: Path | None = None
    access_password: str | None = None
    session_secret: str | None = None
    exiftool_path: Path | None = None
    perl_path: str | None = None
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    auth_required: bool = True
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None

    def __post_init__(self) -> None:
        self.project_dir = _resolve_path(self.project_dir, Path.cwd(), Path.cwd())
        self.data_dir = _resolve_path(self.data_dir, self.project_dir / "data", self.project_dir)
        self.upload_dir = _resolve_path(self.upload_dir, self.data_dir / "uploads", self.project_dir)
        self.photo_root = _resolve_path(self.photo_root, self.upload_dir, self.project_dir)
        self.db_path = _resolve_path(self.db_path, self.data_dir / "gallery.sqlite3", self.project_dir)
        self.cache_dir = _resolve_path(self.cache_dir, self.data_dir / "cache", self.project_dir)
        self.exiftool_path = _resolve_path(
            self.exiftool_path,
            self.project_dir / "vendor" / "exiftool" / "exiftool",
            self.project_dir,
        )
        self.perl_path = self.perl_path or shutil.which("perl") or "perl"
        self.ffmpeg_path = self.ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_path = self.ffprobe_path or shutil.which("ffprobe") or "ffprobe"
        self.max_upload_bytes = int(self.max_upload_bytes)
        if self.max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not self.access_password:
            self.access_password = os.environ.get("PHOTO_PASSWORD") or _read_or_create_secret(
                self.data_dir / "access-password"
            )
        if not self.session_secret:
            self.session_secret = os.environ.get("PHOTO_SESSION_SECRET") or _read_or_create_secret(
                self.data_dir / "session-secret"
            )

    @classmethod
    def from_env(cls, project_dir: Path) -> "Settings":
        project_dir = Path(project_dir).resolve()

        def env_path(name: str, default: Path) -> Path:
            value = os.environ.get(name)
            if not value:
                return default
            path = Path(value).expanduser()
            return path if path.is_absolute() else project_dir / path

        data_dir = env_path("PHOTO_DATA_DIR", project_dir / "data")
        max_upload = int(os.environ.get("PHOTO_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES))
        return cls(
            project_dir=project_dir,
            photo_root=env_path("PHOTO_ROOT", data_dir / "uploads"),
            data_dir=data_dir,
            upload_dir=env_path("PHOTO_UPLOAD_DIR", data_dir / "uploads"),
            db_path=env_path("PHOTO_DB", data_dir / "gallery.sqlite3"),
            cache_dir=env_path("PHOTO_CACHE_DIR", data_dir / "cache"),
            access_password=os.environ.get("PHOTO_PASSWORD") or None,
            session_secret=os.environ.get("PHOTO_SESSION_SECRET") or None,
            ffmpeg_path=os.environ.get("PHOTO_FFMPEG") or None,
            ffprobe_path=os.environ.get("PHOTO_FFPROBE") or None,
            auth_required=(
                os.environ.get("PHOTO_AUTH_REQUIRED", "").strip().lower() not in AUTH_DISABLED_VALUES
            ),
            max_upload_bytes=max_upload,
        )

    @property
    def roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        for root in (self.photo_root, self.upload_dir):
            if root not in roots:
                roots.append(root)
        return tuple(roots)
