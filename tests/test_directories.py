from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app import Settings, create_app
from gallery.db import Database, directory_token


PROJECT = Path(__file__).resolve().parents[1]


def _photo(root: Path, relative_path: str, *, name: str | None = None) -> dict[str, Any]:
    return {
        "path": str((root / relative_path).resolve()),
        "root_path": str(root),
        "relative_path": relative_path,
        "name": name or Path(relative_path).name,
        "format": "JPEG",
        "date": "2024-01-01T00:00:00",
        "camera": "Camera",
        "lens": "Lens",
        "iso": 100,
        "aperture": 2.8,
        "shutter": "1/125",
        "focal": 35,
        "width": 80,
        "height": 40,
        "size": 1234,
        "mtime_ns": 5678,
        "exif": {},
        "media_type": "photo",
        "duration": None,
    }


def _insert(database: Database, root: Path, relative_path: str, *, name: str | None = None) -> int:
    return database.upsert_photo(_photo(root, relative_path, name=name))


def _insert_variant(
    database: Database,
    root: Path,
    relative_path: str,
    *,
    media_type: str,
    format: str,
) -> int:
    photo = _photo(root, relative_path)
    photo["media_type"] = media_type
    photo["format"] = format
    photo["duration"] = 2.5 if media_type == "video" else None
    return database.upsert_photo(photo)


def _client(tmp_path: Path) -> TestClient:
    root = tmp_path / "photos"
    root.mkdir()
    settings = Settings(
        project_dir=PROJECT,
        photo_root=root,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "uploads",
        access_password="secret",
        session_secret="session-secret",
        auth_required=False,
    )
    return TestClient(create_app(settings))


def decode_token(token: str) -> list[str]:
    padded = token + "=" * (-len(token) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_directory_token_encodes_normalized_root_and_parent_opaquely(tmp_path: Path):
    token = directory_token(f"{tmp_path}/", "nested")

    assert token
    assert "/" not in token
    assert "\\" not in token
    assert decode_token(token) == [str(tmp_path), "nested"]


def test_api_directory_filter_exposes_opaque_directory_fields_based_on_relative_path(tmp_path: Path):
    client = _client(tmp_path)
    database = client.app.state.lumen_database
    root = client.app.state.lumen_settings.photo_root
    photo_id = _insert(database, root, "album/actual-file.jpg", name="renamed-display.jpg")
    directory = directory_token(root, "album")

    response = client.get("/api/photos", params={"directory": directory})

    assert response.status_code == 200
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["id"] == photo_id
    assert item["name"] == "renamed-display.jpg"
    assert item["directory"] == directory
    assert item["directory_label"] == "photos/album"


def test_api_unknown_or_malformed_directory_token_returns_empty_result(tmp_path: Path):
    client = _client(tmp_path)
    database = client.app.state.lumen_database
    root = client.app.state.lumen_settings.photo_root
    _insert(database, root, "album/photo.jpg")

    for token in ("not-a-token", "", directory_token(tmp_path / "other", ".")):
        response = client.get("/api/photos", params={"directory": token})
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0}


def test_facets_include_root_and_ancestor_directories_with_subtree_counts_without_paths(tmp_path: Path):
    client = _client(tmp_path)
    database = client.app.state.lumen_database
    root_one = tmp_path / "one" / "photos"
    root_two = tmp_path / "two" / "photos"
    root_one.mkdir(parents=True)
    root_two.mkdir(parents=True)

    rows = [
        (root_one, "root.jpg"),
        (root_one, "nested/one.jpg"),
        (root_one, "nested/deeper/two.jpg"),
        (root_one, "nestedish/three.jpg"),
        (root_one, "trash/hidden.jpg"),
        (root_two, "root.jpg"),
        (root_two, "nested/other.jpg"),
    ]
    ids = [_insert(database, root, relative_path) for root, relative_path in rows]
    database.schedule_zero()
    with database.connect() as connection:
        connection.execute(
            "UPDATE deletion_jobs SET status = 'trashed' WHERE media_id = ?",
            (ids[4],),
        )

    response = client.get("/api/facets")

    assert response.status_code == 200
    assert str(root_one) not in response.text
    assert str(root_two) not in response.text
    directories = response.json()["directories"]
    by_key = {
        (entry["label"], tuple(decode_token(entry["value"]))): entry["count"]
        for entry in directories
    }
    assert by_key == {
        ("photos", (str(root_one), ".")): 4,
        ("photos/nested", (str(root_one), "nested")): 2,
        ("photos/nested/deeper", (str(root_one), "nested/deeper")): 1,
        ("photos/nestedish", (str(root_one), "nestedish")): 1,
        ("photos", (str(root_two), ".")): 2,
        ("photos/nested", (str(root_two), "nested")): 1,
    }


def test_directory_sort_is_stable_and_paginates_after_directory_filter(tmp_path: Path):
    client = _client(tmp_path)
    database = client.app.state.lumen_database
    root_one = tmp_path / "one" / "photos"
    root_two = tmp_path / "two" / "photos"
    root_one.mkdir(parents=True)
    root_two.mkdir(parents=True)
    _insert(database, root_one, "b/file.jpg", name="b-file.jpg")
    _insert(database, root_one, "a/z.jpg", name="z.jpg")
    _insert(database, root_one, "a/y.jpg", name="y.jpg")
    _insert(database, root_two, "a/file.jpg", name="other-root.jpg")

    ascending = client.get("/api/photos", params={"sort": "directory_asc", "limit": 10}).json()
    descending = client.get("/api/photos", params={"sort": "directory_desc", "limit": 10}).json()
    page = client.get(
        "/api/photos",
        params={
            "directory": directory_token(root_one, "."),
            "sort": "directory_asc",
            "offset": 1,
            "limit": 2,
        },
    ).json()

    assert [item["directory_label"] for item in ascending["items"]] == [
        "photos/a",
        "photos/a",
        "photos/b",
        "photos/a",
    ]
    assert [item["name"] for item in ascending["items"]] == [
        "y.jpg",
        "z.jpg",
        "b-file.jpg",
        "other-root.jpg",
    ]
    assert [item["name"] for item in descending["items"]] == [
        "other-root.jpg",
        "b-file.jpg",
        "y.jpg",
        "z.jpg",
    ]
    assert page["total"] == 3
    assert [item["name"] for item in page["items"]] == ["z.jpg", "b-file.jpg"]


def test_directory_filter_treats_percent_and_underscore_as_literal_and_rejects_bad_tokens(tmp_path: Path):
    root = tmp_path / "photos"
    root.mkdir()
    database = Database(tmp_path / "gallery.sqlite3")
    _insert(database, root, "literal%/percent.jpg")
    _insert(database, root, "literal_/underscore.jpg")
    _insert(database, root, "literalX/other.jpg")

    percent_rows, percent_total = database.list_photos(
        directory=directory_token(root, "literal%"),
        sort="name_asc",
    )
    underscore_rows, underscore_total = database.list_photos(
        directory=directory_token(root, "literal_"),
        sort="name_asc",
    )

    assert percent_total == 1
    assert [row["relative_path"] for row in percent_rows] == ["literal%/percent.jpg"]
    assert underscore_total == 1
    assert [row["relative_path"] for row in underscore_rows] == ["literal_/underscore.jpg"]
    for token in ("not-a-token", "", directory_token(tmp_path / "other", ".")):
        assert database.list_photos(directory=token)[1] == 0


def test_directory_filter_matches_exact_directory_and_descendants_only(tmp_path: Path):
    root = tmp_path / "photos"
    root.mkdir()
    database = Database(tmp_path / "gallery.sqlite3")
    _insert(database, root, "album/one.jpg")
    _insert(database, root, "album/nested/two.jpg")
    _insert(database, root, "albumatic/three.jpg")
    _insert(database, root, "root.jpg")

    rows, total = database.list_photos(directory=directory_token(root, "album"), sort="name_asc")

    assert total == 2
    assert [row["relative_path"] for row in rows] == ["album/one.jpg", "album/nested/two.jpg"]


def test_directory_filter_composes_with_rating_media_type_imported_and_pagination(tmp_path: Path):
    client = _client(tmp_path)
    database = client.app.state.lumen_database
    root = client.app.state.lumen_settings.photo_root
    photo_id = _insert(database, root, "album/photo.jpg")
    video_id = _insert_variant(database, root, "album/nested/clip.mp4", media_type="video", format="MP4")
    other_video_id = _insert_variant(
        database,
        root,
        "album/nested/old-clip.mp4",
        media_type="video",
        format="MP4",
    )
    database.update_rating(photo_id, 4)
    database.update_rating(video_id, 3)
    database.update_rating(other_video_id, 2)
    video = database.get_photo(video_id)
    assert video is not None
    database.register_upload(str(video["path"]), "clip.mp4")

    response = client.get(
        "/api/photos",
        params={
            "directory": directory_token(root, "album"),
            "media_type": "video",
            "rating_min": 3,
            "imported_only": "true",
            "sort": "directory_asc",
            "offset": 0,
            "limit": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["items"]] == [video_id]
    assert response.json()["items"][0]["media_type"] == "video"


