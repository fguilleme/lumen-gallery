from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt

from gallery.config import Settings
from gallery.db import (
    SORT_SQL,
    Database,
    directory_label,
    directory_parent,
    directory_token,
)
from gallery.exif import SUPPORTED_EXTENSIONS, ExifTool
from gallery.media import MediaCache, MediaConversionError, MediaNotFound, resolve_photo_path
from gallery.scanner import ScanManager
from gallery.similarity import SimilarityManager


SESSION_COOKIE = "lumen_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


class LoginPayload(BaseModel):
    password: str


class RatingPayload(BaseModel):
    rating: StrictInt = Field(ge=0, le=4)


def _sign_session(secret: str, expiry: int, nonce: str) -> str:
    body = f"{expiry}.{nonce}"
    signature = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{body}.{encoded}"


def _valid_session(secret: str, value: str | None) -> bool:
    if not value:
        return False
    try:
        expiry_text, nonce, signature = value.split(".", 2)
        expiry = int(expiry_text)
    except (TypeError, ValueError):
        return False
    if expiry < int(time.time()) or not nonce or not signature:
        return False
    expected = _sign_session(secret, expiry, nonce).rsplit(".", 1)[1]
    return hmac.compare_digest(signature, expected)


def _host_without_port(host: str) -> str:
    try:
        return (urlsplit(f"//{host}").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _host_is_local(host: str) -> bool:
    if not host:
        return False
    if host in {"localhost", "testserver"} or host.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address in ipaddress.ip_network("100.64.0.0/10")
    )


def _normal_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname.lower().rstrip(".")
    default_port = (parsed.scheme == "http" and port in {None, 80}) or (
        parsed.scheme == "https" and port in {None, 443}
    )
    return f"{parsed.scheme}://{host}" if default_port else f"{parsed.scheme}://{host}:{port}"


def _check_state_change(request: Request) -> None:
    host = _host_without_port(request.headers.get("host", ""))
    configured = {
        _host_without_port(item.strip())
        for item in os.environ.get("PHOTO_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    if host not in configured and not _host_is_local(host):
        raise HTTPException(status_code=403, detail="Host is not allowed")
    origin = request.headers.get("origin")
    if origin is not None:
        expected = _normal_origin(f"{request.url.scheme}://{request.headers.get('host', '')}")
        if not expected or _normal_origin(origin) != expected:
            raise HTTPException(status_code=403, detail="Cross-origin state change denied")


def _public_number(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _item(row: dict[str, Any]) -> dict[str, Any]:
    photo_id = int(row["id"])
    media_type = str(row.get("media_type") or "photo")
    parent = directory_parent(str(row["relative_path"]))
    root = str(row["root_path"])
    return {
        "id": photo_id,
        "name": str(row["name"]),
        "directory": directory_token(root, parent),
        "directory_label": directory_label(root, parent),
        "format": str(row["format"]),
        "media_type": media_type,
        "duration": row.get("duration"),
        "rating": int(row["rating"]),
        "similarity_group": row.get("similarity_group"),
        "similarity_size": int(row.get("similarity_size") or 0),
        "deletion_scheduled_at": row.get("deletion_scheduled_at"),
        "delete_after": row.get("delete_after"),
        "date": str(row["date"]),
        "camera": str(row["camera"]),
        "lens": str(row["lens"]),
        "iso": _public_number(row["iso"]),
        "aperture": _public_number(row["aperture"]),
        "shutter": str(row["shutter"]),
        "focal": _public_number(row["focal"]),
        "width": row["width"],
        "height": row["height"],
        "size": int(row["size"]),
        "thumb_url": f"/api/photos/{photo_id}/thumb",
        "preview_url": f"/api/photos/{photo_id}/preview",
        "download_url": f"/api/photos/{photo_id}/download",
        "stream_url": f"/api/photos/{photo_id}/stream" if media_type == "video" else None,
    }


def _safe_original_name(filename: str | None) -> str:
    candidate = Path(filename or "").name
    cleaned = "".join(char if char.isprintable() and char not in {"\x00", "\r", "\n"} else "_" for char in candidate)
    return cleaned or "upload"


def _file_range_response(request: Request, path: Path, media_type: str) -> Response:
    size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
    if not range_header.startswith("bytes=") or "," in range_header:
        raise HTTPException(
            status_code=416,
            detail="Invalid byte range",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    value = range_header[6:].strip()
    try:
        start_text, end_text = value.split("-", 1)
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError
            start = max(size - suffix_length, 0)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start < 0 or start >= size or end < start:
                raise ValueError
            end = min(end, size - 1)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=416,
            detail="Invalid byte range",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )

    length = end - start + 1

    def content():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        content(),
        status_code=206,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{size}",
        },
    )


async def _write_upload(path: Path, upload: UploadFile, max_bytes: int) -> int:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    total = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"file exceeds {max_bytes} byte limit")
                handle.write(chunk)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return total


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env(Path(__file__).resolve().parent)
    database = Database(settings.db_path)
    exiftool = ExifTool(settings.exiftool_path, settings.perl_path)
    scanner = ScanManager(settings, database, exiftool)
    media = MediaCache(settings, exiftool)
    app = FastAPI(title="Lumen Gallery")
    app.state.lumen_settings = settings
    app.state.lumen_database = database
    app.state.lumen_scanner = scanner
    app.state.lumen_media = media
    similarity_manager = SimilarityManager(settings, database, media)
    app.state.lumen_similarity = similarity_manager

    def require_session(request: Request) -> None:
        if not settings.auth_required:
            return
        if not _valid_session(settings.session_secret or "", request.cookies.get(SESSION_COOKIE)):
            raise HTTPException(status_code=401, detail="Authentication required")

    @app.get("/")
    def index() -> FileResponse:
        path = settings.project_dir / "static" / "index.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Gallery frontend is not installed")
        return FileResponse(path, media_type="text/html")

    app.mount("/static", StaticFiles(directory=settings.project_dir / "static", check_dir=False), name="static")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request) -> dict[str, bool]:
        if not settings.auth_required:
            return {"authenticated": True, "auth_required": False}
        return {"authenticated": _valid_session(settings.session_secret or "", request.cookies.get(SESSION_COOKIE))}

    @app.post("/api/login", dependencies=[Depends(_check_state_change)])
    def login(payload: LoginPayload, response: Response, request: Request) -> dict[str, bool]:
        if not hmac.compare_digest(payload.password, settings.access_password or ""):
            raise HTTPException(status_code=401, detail="Invalid password")
        expiry = int(time.time()) + SESSION_TTL_SECONDS
        response.set_cookie(
            SESSION_COOKIE,
            _sign_session(settings.session_secret or "", expiry, secrets.token_urlsafe(12)),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return {"authenticated": True}

    @app.post("/api/logout", dependencies=[Depends(_check_state_change)])
    def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"authenticated": False}

    @app.post("/api/scan", dependencies=[Depends(_check_state_change)])
    def start_scan(_: None = Depends(require_session)) -> dict[str, bool]:
        scanner.start()
        return {"running": True}

    @app.get("/api/status")
    def status(_: None = Depends(require_session)) -> dict[str, object]:
        return scanner.snapshot()

    @app.post("/api/upload", dependencies=[Depends(_check_state_change)])
    async def upload(
        files: list[UploadFile] = File(...),
        _: None = Depends(require_session),
    ) -> dict[str, Any]:
        accepted = 0
        errors: list[str] = []
        for upload_file in files:
            display_name = _safe_original_name(upload_file.filename)
            suffix = Path(display_name).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                errors.append(f"{display_name}: unsupported file type")
                await upload_file.close()
                continue
            stored = settings.upload_dir / f"{secrets.token_hex(16)}{suffix}"
            try:
                await _write_upload(stored, upload_file, settings.max_upload_bytes)
                database.register_upload(str(stored.resolve()), display_name)
                accepted += 1
            except (OSError, ValueError) as exc:
                errors.append(f"{display_name}: {exc}")
                stored.unlink(missing_ok=True)
            finally:
                await upload_file.close()
        if accepted:
            scanner.start()
        return {"accepted": accepted, "errors": errors}

    @app.post("/api/deletions/schedule-zero", dependencies=[Depends(_check_state_change)])
    def schedule_zero(_: None = Depends(require_session)) -> dict[str, Any]:
        return database.schedule_zero(now=datetime.now(timezone.utc))

    @app.get("/api/deletions/status")
    def deletion_status(_: None = Depends(require_session)) -> dict[str, int]:
        return database.deletion_status()

    @app.get("/api/deletions/trash")
    def trashed_deletions(_: None = Depends(require_session)) -> dict[str, Any]:
        items = database.list_trash_jobs()
        return {"items": items, "total": len(items)}

    @app.get("/api/similarity/status")
    def similarity_status(_: None = Depends(require_session)) -> dict[str, Any]:
        return similarity_manager.snapshot()

    @app.post("/api/similarity/analyze", dependencies=[Depends(_check_state_change)])
    def similarity_analyze(_: None = Depends(require_session)) -> dict[str, Any]:
        started = similarity_manager.start()
        return {"started": started, **similarity_manager.snapshot()}

    @app.post("/api/similarity/cancel", dependencies=[Depends(_check_state_change)])
    def similarity_cancel(_: None = Depends(require_session)) -> dict[str, Any]:
        similarity_manager.cancel()
        return similarity_manager.snapshot()

    @app.get("/api/photos")
    def photos(
        q: str | None = None,
        camera: str | None = None,
        lens: str | None = None,
        format: str | None = None,
        media_type: Literal["photo", "video"] | None = Query(None),
        imported_only: bool = Query(False),
        directory: str | None = Query(None),
        similarity: Literal["near", "content"] | None = Query(None),
        date_from: str | None = None,
        date_to: str | None = None,
        iso_min: float | None = None,
        iso_max: float | None = None,
        rating: int | None = Query(None, ge=0, le=4),
        rating_min: int | None = Query(None, ge=0, le=4),
        deletion_status: Literal["scheduled"] | None = Query(None),
        sort: str = Query("date_desc"),
        offset: int = Query(0, ge=0),
        limit: int = Query(60, ge=1, le=200),
        _: None = Depends(require_session),
    ) -> dict[str, Any]:
        if sort not in SORT_SQL:
            raise HTTPException(status_code=422, detail="Invalid sort")
        rows, total = database.list_photos(
            q=q,
            camera=camera,
            lens=lens,
            format=format,
            media_type=media_type,
            imported_only=imported_only,
            directory=directory,
            date_from=date_from,
            date_to=date_to,
            iso_min=iso_min,
            iso_max=iso_max,
            rating=rating,
            rating_min=rating_min,
            deletion_status=deletion_status,
            sort=sort,
            offset=0 if similarity else offset,
            limit=1000000000 if similarity else limit,
        )
        if similarity:
            rows = similarity_manager.annotate_and_sort(rows, similarity)[offset:offset + limit]
        return {"items": [_item(row) for row in rows], "total": total}

    @app.get("/api/facets")
    def facets(_: None = Depends(require_session)) -> dict[str, Any]:
        return database.facets()

    def get_photo(photo_id: int) -> dict[str, Any]:
        row = database.get_photo(photo_id)
        if row is None or bool(row.get("is_trashed")):
            raise HTTPException(status_code=404, detail="Photo not found")
        return row

    @app.get("/api/photos/{photo_id}")
    def photo_detail(photo_id: int, _: None = Depends(require_session)) -> dict[str, Any]:
        row = get_photo(photo_id)
        try:
            exif = json.loads(str(row["exif_json"]))
        except (TypeError, ValueError):
            exif = {}
        result = _item(row)
        result["exif"] = exif
        return result

    @app.patch("/api/photos/{photo_id}/rating", dependencies=[Depends(_check_state_change)])
    def update_photo_rating(
        photo_id: int,
        payload: RatingPayload,
        _: None = Depends(require_session),
    ) -> dict[str, Any]:
        row = database.update_rating(photo_id, payload.rating)
        if row is None or bool(row.get("is_trashed")):
            raise HTTPException(status_code=404, detail="Photo not found")
        return _item(row)

    @app.post(
        "/api/photos/{photo_id}/deletion/cancel",
        dependencies=[Depends(_check_state_change)],
    )
    def cancel_photo_deletion(
        photo_id: int,
        _: None = Depends(require_session),
    ) -> dict[str, Any]:
        row = database.cancel_deletion(photo_id)
        if row is None or bool(row.get("is_trashed")):
            raise HTTPException(status_code=404, detail="Photo not found")
        return _item(row)

    @app.get("/api/photos/{photo_id}/thumb")
    def thumbnail(photo_id: int, _: None = Depends(require_session)) -> FileResponse:
        row = get_photo(photo_id)
        try:
            path = media.jpeg("thumb", row)
        except MediaConversionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (MediaNotFound, OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Thumbnail unavailable") from exc
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/photos/{photo_id}/preview")
    def preview(photo_id: int, _: None = Depends(require_session)) -> FileResponse:
        row = get_photo(photo_id)
        try:
            path = media.jpeg("preview", row)
        except MediaConversionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (MediaNotFound, OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Preview unavailable") from exc
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/photos/{photo_id}/stream")
    def stream(photo_id: int, request: Request, _: None = Depends(require_session)) -> Response:
        row = get_photo(photo_id)
        if str(row.get("media_type") or "photo") != "video":
            raise HTTPException(status_code=404, detail="Stream unavailable")
        try:
            path = media.stream_path(row)
        except MediaNotFound as exc:
            raise HTTPException(status_code=404, detail="Video unavailable") from exc
        except (MediaConversionError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _file_range_response(request, path, "video/mp4")

    @app.get("/api/photos/{photo_id}/download")
    def download(photo_id: int, _: None = Depends(require_session)) -> FileResponse:
        row = get_photo(photo_id)
        try:
            path = resolve_photo_path(settings, row)
        except MediaNotFound as exc:
            raise HTTPException(status_code=404, detail="Photo unavailable") from exc
        if str(row.get("media_type") or "photo") == "video":
            media_type = {
                "MOV": "video/quicktime",
                "WEBM": "video/webm",
            }.get(str(row["format"]).upper(), "video/mp4")
        else:
            media_type = "image/jpeg" if str(row["format"]) == "JPEG" else "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=str(row["name"]))

    return app


app = create_app()

__all__ = ["Settings", "app", "create_app"]
