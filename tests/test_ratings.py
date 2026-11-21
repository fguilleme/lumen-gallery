from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import Settings, create_app
from gallery.db import Database
from gallery.scanner import PhotoScanner, ScanProgress


PROJECT = Path(__file__).resolve().parents[1]
EXIFTOOL = PROJECT / "vendor" / "exiftool" / "exiftool"


def make_settings(tmp_path: Path, *, auth_required: bool = False) -> Settings:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    return Settings(
        project_dir=PROJECT,
        photo_root=source,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "uploads",
        access_password="secret",
        session_secret="session-secret",
        exiftool_path=EXIFTOOL,
        auth_required=auth_required,
    )


def make_client(tmp_path: Path, *, auth_required: bool = False) -> TestClient:
    client = TestClient(create_app(make_settings(tmp_path, auth_required=auth_required)))
    if auth_required:
        assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    return client


def media_row(
    tmp_path: Path,
    name: str,
    *,
    date: str = "2024-01-01T00:00:00",
    camera: str = "Camera",
    lens: str = "Lens",
    media_type: str = "photo",
    format: str = "JPEG",
) -> dict[str, Any]:
    path = (tmp_path / name).resolve()
    return {
        "path": str(path),
        "root_path": str(tmp_path),
        "relative_path": name,
        "name": name,
        "format": format,
        "date": date,
        "camera": camera,
        "lens": lens,
        "iso": 100,
        "aperture": 2.8,
        "shutter": "1/125",
        "focal": 35,
        "width": 80,
        "height": 40,
        "size": 1234,
        "mtime_ns": 5678,
        "exif": {"Model": camera},
        "media_type": media_type,
        "duration": 1.25 if media_type == "video" else None,
    }


def insert_row(database: Database, tmp_path: Path, name: str, **kwargs: Any) -> dict[str, Any]:
    photo = media_row(tmp_path, name, **kwargs)
    photo["id"] = database.upsert_photo(photo)
    return photo


def test_legacy_schema_migration_adds_zero_rating_and_rating_index(tmp_path: Path):
    database_path = tmp_path / "gallery.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                root_path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                name TEXT NOT NULL,
                format TEXT NOT NULL,
                date TEXT NOT NULL,
                camera TEXT NOT NULL DEFAULT '',
                lens TEXT NOT NULL DEFAULT '',
                iso REAL,
                aperture REAL,
                shutter TEXT NOT NULL DEFAULT '',
                focal REAL,
                width INTEGER,
                height INTEGER,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                exif_json TEXT NOT NULL DEFAULT '{}',
                media_type TEXT NOT NULL DEFAULT 'photo',
                duration REAL
            );
            INSERT INTO photos(
                path, root_path, relative_path, name, format, date, camera, lens,
                iso, aperture, shutter, focal, width, height, size, mtime_ns,
                exif_json, media_type, duration
            ) VALUES (
                '/photos/original.jpg', '/photos', 'original.jpg', 'original.jpg', 'JPEG',
                '2024-01-02T03:04:05', 'Camera', 'Lens', 100, 2.8, '1/125', 35,
                80, 40, 1234, 5678, '{"Model":"Camera"}', 'photo', NULL
            );
            """
        )

    database = Database(database_path)
    row = database.get_photo(1)
    assert row is not None
    assert row["rating"] == 0
    assert row["path"] == "/photos/original.jpg"
    assert row["media_type"] == "photo"

    with database.connect() as connection:
        columns = {str(item[1]): item for item in connection.execute("PRAGMA table_info(photos)")}
        indexes = {str(item[1]) for item in connection.execute("PRAGMA index_list(photos)")}
        assert columns["rating"][3] == 1
        assert columns["rating"][4] == "0"
        assert "idx_photos_rating" in indexes
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE photos SET rating = 5 WHERE id = 1")

    reopened = Database(database_path)
    assert reopened.get_photo(1)["rating"] == 0  # type: ignore[index]


def test_rating_patch_rejects_bounds_and_non_strict_integer_payloads(tmp_path: Path):
    client = make_client(tmp_path)
    database = client.app.state.lumen_database
    row = insert_row(database, tmp_path, "photo.jpg")

    invalid_payloads = [
        {"rating": -1},
        {"rating": 5},
        {"rating": True},
        {"rating": "2"},
        {"rating": 2.0},
        {"rating": None},
        {},
    ]
    for payload in invalid_payloads:
        response = client.patch(f"/api/photos/{row['id']}/rating", json=payload)
        assert response.status_code == 422, payload

    assert database.get_photo(row["id"])["rating"] == 1  # type: ignore[index]
    assert client.patch("/api/photos/999999/rating", json={"rating": 2}).status_code == 404


def test_rating_patch_returns_full_item_and_persists_photo_and_video_across_reopen(tmp_path: Path):
    client = make_client(tmp_path)
    database = client.app.state.lumen_database
    photo = insert_row(database, tmp_path, "photo.jpg", camera="Photo Camera")
    video = insert_row(
        database,
        tmp_path,
        "video.mp4",
        format="MP4",
        media_type="video",
        camera="Video Camera",
    )

    for row, rating in ((photo, 4), (video, 2)):
        response = client.patch(f"/api/photos/{row['id']}/rating", json={"rating": rating})
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == row["id"]
        assert body["rating"] == rating
        assert body["media_type"] == row["media_type"]
        assert body["camera"] == row["camera"]
        assert body["thumb_url"] == f"/api/photos/{row['id']}/thumb"
        assert body["preview_url"] == f"/api/photos/{row['id']}/preview"
        assert body["download_url"] == f"/api/photos/{row['id']}/download"
        if row["media_type"] == "video":
            assert body["stream_url"] == f"/api/photos/{row['id']}/stream"
        else:
            assert body["stream_url"] is None

    listing = client.get("/api/photos", params={"sort": "name_asc"}).json()
    assert {item["id"]: item["rating"] for item in listing["items"]} == {
        photo["id"]: 4,
        video["id"]: 2,
    }
    assert client.get(f"/api/photos/{photo['id']}").json()["rating"] == 4

    reopened = Database(client.app.state.lumen_settings.db_path)
    assert reopened.get_photo(photo["id"])["rating"] == 4  # type: ignore[index]
    assert reopened.get_photo(video["id"])["rating"] == 2  # type: ignore[index]
    assert reopened.get_photo(video["id"])["media_type"] == "video"  # type: ignore[index]


def test_scan_same_and_changed_original_preserve_rating_without_touching_source(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    original = source / "photo.jpg"
    original.write_bytes(b"original")
    settings = Settings(
        project_dir=PROJECT,
        photo_root=source,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "uploads",
        access_password="secret",
        session_secret="session-secret",
    )
    database = Database(settings.db_path)
    scanner = PhotoScanner(
        settings,
        database,
        object(),
        ScanProgress(lambda _total: None, lambda: None, lambda _message: None),
    )
    inspected_dates = iter(("2024-01-01T00:00:00", "2025-02-02T00:00:00"))

    def fake_inspect(path: Path, _tool: object, *, name: str | None = None) -> dict[str, Any]:
        stat = path.stat()
        return {
            "name": name or path.name,
            "format": "JPEG",
            "date": next(inspected_dates),
            "camera": "Updated Camera" if stat.st_size != len(b"original") else "Original Camera",
            "lens": "Lens",
            "iso": 100,
            "aperture": 2.8,
            "shutter": "1/125",
            "focal": 35,
            "width": 80,
            "height": 40,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "exif": {},
        }

    monkeypatch.setattr("gallery.scanner.inspect_file", fake_inspect)
    scanner.scan()
    photo_id = database.get_photo_by_path(str(original.resolve()))["id"]  # type: ignore[index]
    database.update_rating(photo_id, 4)
    source_before_same = (
        hashlib.sha256(original.read_bytes()).hexdigest(),
        original.stat().st_size,
        original.stat().st_mtime_ns,
    )

    scanner.scan()
    unchanged = database.get_photo(photo_id)
    assert unchanged is not None
    assert unchanged["rating"] == 4
    assert unchanged["date"] == "2024-01-01T00:00:00"
    assert (
        hashlib.sha256(original.read_bytes()).hexdigest(),
        original.stat().st_size,
        original.stat().st_mtime_ns,
    ) == source_before_same

    original.write_bytes(b"changed-original")
    source_before_changed = (
        hashlib.sha256(original.read_bytes()).hexdigest(),
        original.stat().st_size,
        original.stat().st_mtime_ns,
    )
    scanner.scan()
    changed = database.get_photo(photo_id)
    assert changed is not None
    assert changed["rating"] == 4
    assert changed["date"] == "2025-02-02T00:00:00"
    assert changed["camera"] == "Updated Camera"
    assert (
        hashlib.sha256(original.read_bytes()).hexdigest(),
        original.stat().st_size,
        original.stat().st_mtime_ns,
    ) == source_before_changed


def test_rating_filters_compositions_pagination_total_and_stable_sorts(tmp_path: Path):
    client = make_client(tmp_path)
    database = client.app.state.lumen_database
    zero = insert_row(
        database,
        tmp_path,
        "zero-cat.jpg",
        date="2024-01-01T00:00:00",
        camera="Cat Camera",
    )
    zero_tie = insert_row(
        database,
        tmp_path,
        "zero-cat-tie.jpg",
        date="2024-01-01T00:00:00",
        camera="Cat Camera",
    )
    one = insert_row(
        database,
        tmp_path,
        "one-dog.jpg",
        date="2024-01-02T00:00:00",
        camera="Dog Camera",
    )
    three = insert_row(
        database,
        tmp_path,
        "three-video.mp4",
        date="2024-01-03T00:00:00",
        camera="Video Camera",
        format="MP4",
        media_type="video",
    )
    four = insert_row(
        database,
        tmp_path,
        "four-cat.jpg",
        date="2024-01-04T00:00:00",
        camera="Cat Camera",
    )
    for row, rating in ((zero, 0), (zero_tie, 0), (one, 1), (three, 3), (four, 4)):
        assert database.update_rating(row["id"], rating) is not None
    database.register_upload(zero["path"], zero["name"])
    database.register_upload(four["path"], four["name"])

    exact_zero = client.get("/api/photos", params={"rating": 0, "sort": "rating_asc"})
    assert exact_zero.status_code == 200
    assert exact_zero.json()["total"] == 2
    assert [item["name"] for item in exact_zero.json()["items"]] == [
        "zero-cat.jpg",
        "zero-cat-tie.jpg",
    ]
    minimum = client.get("/api/photos", params={"rating_min": 3, "sort": "rating_asc"})
    assert minimum.json()["total"] == 2
    assert [item["rating"] for item in minimum.json()["items"]] == [3, 4]

    descending = client.get("/api/photos", params={"sort": "rating_desc"}).json()
    assert [item["name"] for item in descending["items"]] == [
        "four-cat.jpg",
        "three-video.mp4",
        "one-dog.jpg",
        "zero-cat.jpg",
        "zero-cat-tie.jpg",
    ]
    ascending = client.get("/api/photos", params={"sort": "rating_asc"}).json()
    assert [item["name"] for item in ascending["items"]] == [
        "zero-cat.jpg",
        "zero-cat-tie.jpg",
        "one-dog.jpg",
        "three-video.mp4",
        "four-cat.jpg",
    ]

    composition = client.get(
        "/api/photos",
        params={
            "q": "cat",
            "media_type": "photo",
            "imported_only": "true",
            "date_from": "2024-01-01",
            "date_to": "2024-01-31",
            "rating_min": 0,
            "sort": "rating_desc",
            "offset": 0,
            "limit": 1,
        },
    )
    assert composition.status_code == 200
    assert composition.json()["total"] == 2
    assert [item["name"] for item in composition.json()["items"]] == ["four-cat.jpg"]

    next_page = client.get(
        "/api/photos",
        params={
            "q": "cat",
            "media_type": "photo",
            "imported_only": "true",
            "date_from": "2024-01-01",
            "date_to": "2024-01-31",
            "rating_min": 0,
            "sort": "rating_desc",
            "offset": 1,
            "limit": 1,
        },
    )
    assert next_page.json()["total"] == 2
    assert [item["name"] for item in next_page.json()["items"]] == ["zero-cat.jpg"]

    for parameter in ("rating", "rating_min"):
        assert client.get("/api/photos", params={parameter: -1}).status_code == 422
        assert client.get("/api/photos", params={parameter: 5}).status_code == 422


def test_rating_patch_requires_session_when_auth_enabled(tmp_path: Path):
    client = make_client(tmp_path, auth_required=True)
    database = client.app.state.lumen_database
    row = insert_row(database, tmp_path, "private.jpg")
    client.cookies.clear()

    response = client.patch(f"/api/photos/{row['id']}/rating", json={"rating": 3})
    assert response.status_code == 401
    assert database.get_photo(row["id"])["rating"] == 1  # type: ignore[index]


def test_passwordless_rating_patch_rejects_cross_origin_state_change(tmp_path: Path):
    client = make_client(tmp_path, auth_required=False)
    database = client.app.state.lumen_database
    row = insert_row(database, tmp_path, "csrf.jpg")

    response = client.patch(
        f"/api/photos/{row['id']}/rating",
        json={"rating": 3},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert database.get_photo(row["id"])["rating"] == 1  # type: ignore[index]
