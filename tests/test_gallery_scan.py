from __future__ import annotations

import hashlib
import shutil
import time
from io import BytesIO
from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from PIL import Image

from app import Settings, create_app
from gallery.exif import ExifTool

PROJECT = Path(__file__).resolve().parents[1]
EXIFTOOL = PROJECT / "vendor" / "exiftool" / "exiftool"


def make_jpeg(path: Path, *, date: str, model: str, lens: str, iso: int, focal: int) -> bytes:
    image = Image.new("RGB", (80, 40), (80, 120, 160))
    exif = Image.Exif()
    exif[0x010F] = "Test Make"
    exif[0x0110] = model
    exif[0x9003] = date
    exif[0x0132] = date
    exif[0xA434] = lens
    exif[0x8827] = iso
    exif[0x829D] = (focal, 2)
    exif[0x829A] = (1, 125)
    exif[0x920A] = (focal, 1)
    image.save(path, format="JPEG", exif=exif)
    return path.read_bytes()


def make_client(
    tmp_path: Path,
    source: Path | None = None,
    *,
    max_upload_bytes: int = 250 * 1024 * 1024,
) -> TestClient:
    source = source or (tmp_path / "source")
    source.mkdir(exist_ok=True)
    settings = Settings(
        project_dir=PROJECT,
        photo_root=source,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "uploads",
        access_password="secret",
        session_secret="session-secret",
        exiftool_path=EXIFTOOL,
        max_upload_bytes=max_upload_bytes,
    )
    client = TestClient(create_app(settings))
    assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    return client


def wait_for_scan(client: TestClient) -> dict:
    for _ in range(100):
        status = client.get("/api/status").json()
        if not status["running"]:
            return status
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


def test_scan_search_sort_filter_and_download(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    alpha = source / "alpha.jpg"
    zeta = source / "nested-zeta.jpeg"
    alpha_bytes = make_jpeg(
        alpha,
        date="2024:01:02 03:04:05",
        model="Camera A",
        lens="Lens Wide",
        iso=100,
        focal=24,
    )
    make_jpeg(
        zeta,
        date="2025:05:06 07:08:09",
        model="Camera B",
        lens="Lens Tele",
        iso=800,
        focal=85,
    )

    client = make_client(tmp_path, source)
    started = client.post("/api/scan")
    assert started.status_code == 200
    assert started.json() == {"running": True}

    status = wait_for_scan(client)
    assert status["total"] == 2
    assert status["processed"] == 2
    assert status["errors"] == []

    listing = client.get("/api/photos", params={"sort": "name_asc"})
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2
    assert [item["name"] for item in body["items"]] == ["alpha.jpg", "nested-zeta.jpeg"]
    first = body["items"][0]
    assert first["format"] == "JPEG"
    assert first["date"].startswith("2024-01-02T03:04:05")
    assert first["camera"] == "Camera A"
    assert first["lens"] == "Lens Wide"
    assert first["iso"] == 100
    assert first["focal"] == 24
    assert first["width"] == 80
    assert first["height"] == 40
    assert first["size"] == len(alpha_bytes)
    assert first["thumb_url"].startswith("/api/photos/")

    filtered = client.get(
        "/api/photos",
        params={"camera": "Camera B", "iso_min": 500, "q": "zeta"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["name"] == "nested-zeta.jpeg"

    date_filtered = client.get(
        "/api/photos",
        params={"date_from": "2025-01-01", "date_to": "2025-12-31", "sort": "date_asc"},
    )
    assert date_filtered.status_code == 200
    assert [item["name"] for item in date_filtered.json()["items"]] == ["nested-zeta.jpeg"]
    assert client.get("/api/photos", params={"sort": "iso_desc"}).json()["items"][0]["iso"] == 800
    assert client.get("/api/facets").json() == {
        "cameras": ["Camera A", "Camera B"],
        "lenses": ["Lens Tele", "Lens Wide"],
        "formats": ["JPEG"],
    }

    detail = client.get(f"/api/photos/{first['id']}")
    assert detail.status_code == 200
    assert detail.json()["exif"]["Model"] == "Camera A"
    thumbnail = client.get(first["thumb_url"])
    assert thumbnail.status_code == 200
    with Image.open(BytesIO(thumbnail.content)) as image:
        assert image.format == "JPEG"
        assert image.width <= 480
        assert image.height <= 480

    download = client.get(first["download_url"])
    assert download.status_code == 200
    assert download.content == alpha_bytes
    assert 'attachment; filename="alpha.jpg"' in download.headers["content-disposition"]


def test_jpeg_orientation_is_transposed_for_metadata_and_preview(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    image = Image.new("RGB", (40, 80), (10, 20, 30))
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x9003] = "2024:01:02 03:04:05"
    exif[0x0110] = "Rotated Camera"
    exif[0xA434] = "Rotated Lens"
    exif[0x8827] = 100
    image.save(source / "rotated.jpg", format="JPEG", exif=exif)

    client = make_client(tmp_path, source)
    client.post("/api/scan")
    wait_for_scan(client)
    item = client.get("/api/photos").json()["items"][0]

    assert (item["width"], item["height"]) == (80, 40)
    with Image.open(BytesIO(client.get(item["preview_url"]).content)) as preview:
        assert preview.size == (80, 40)


def test_cr2_embedded_preview_fallback_and_original_download(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    fixture = PROJECT / "tests" / "fixtures" / "RAW_CANON_40D_SRAW_V103.CR2"
    shutil.copyfile(fixture, source / fixture.name)

    client = make_client(tmp_path, source)
    assert client.post("/api/scan").json() == {"running": True}
    status = wait_for_scan(client)
    assert status["total"] == 1
    assert status["processed"] == 1
    assert status["errors"] == []

    listing = client.get("/api/photos").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["format"] == "CR2"
    assert item["date"].startswith("2007-09-14T11:32:02")
    assert item["camera"] == "Canon EOS 40D"
    assert item["lens"] == "EF-S17-85mm f/4-5.6 IS USM"
    assert item["iso"] == 100
    assert item["focal"] == 17
    assert item["width"] == 1936
    assert item["height"] == 1288

    preview = client.get(item["preview_url"])
    assert preview.status_code == 200
    assert preview.content.startswith(b"\xff\xd8")
    with Image.open(BytesIO(preview.content)) as image:
        assert image.format == "JPEG"
        assert image.size == (1936, 1288)

    download = client.get(item["download_url"])
    assert download.status_code == 200
    assert hashlib.sha256(download.content).hexdigest() == (
        "152382ce4dbf644899d12b41b4c577f07638aa3ec5745ac344c36bad93826125"
    )

    detail = client.get(f"/api/photos/{item['id']}")
    assert detail.status_code == 200
    assert "SourceFile" not in detail.json()["exif"]


def test_rescan_skips_unchanged_file_by_mtime_and_size(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    make_jpeg(
        source / "stable.jpg",
        date="2024:01:02 03:04:05",
        model="Camera",
        lens="Lens",
        iso=100,
        focal=24,
    )
    client = make_client(tmp_path, source)
    client.post("/api/scan")
    wait_for_scan(client)

    def should_not_reprocess(*args, **kwargs):
        raise AssertionError("unchanged file was reprocessed")

    monkeypatch.setattr("gallery.scanner.inspect_file", should_not_reprocess)
    client.post("/api/scan")
    status = wait_for_scan(client)

    assert status["total"] == 1
    assert status["processed"] == 1
    assert status["errors"] == []
    assert client.get("/api/photos").json()["total"] == 1


def test_cr2_source_orientation_is_applied_to_real_preview(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    fixture = PROJECT / "tests" / "fixtures" / "M0054341_01_00005.cr2"
    shutil.copyfile(fixture, source / fixture.name)

    client = make_client(tmp_path, source)
    client.post("/api/scan")
    status = wait_for_scan(client)
    assert status["errors"] == []
    item = client.get("/api/photos").json()["items"][0]
    assert client.get(f"/api/photos/{item['id']}").json()["exif"]["Orientation"] == 3

    raw_preview = ExifTool(EXIFTOOL).extract_preview(fixture)
    with Image.open(BytesIO(raw_preview)) as raw:
        raw.load()
        expected_corner = raw.getpixel((raw.width - 1, raw.height - 1))
    with Image.open(BytesIO(client.get(item["preview_url"]).content)) as preview:
        actual_corner = preview.getpixel((0, 0))

    assert max(abs(actual - expected) for actual, expected in zip(actual_corner, expected_corner)) < 35


def test_deleted_source_is_removed_on_rescan(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    make_jpeg(
        source / "gone.jpg",
        date="2024:01:02 03:04:05",
        model="Camera A",
        lens="Lens Wide",
        iso=100,
        focal=24,
    )
    client = make_client(tmp_path, source)
    client.post("/api/scan")
    wait_for_scan(client)
    assert client.get("/api/photos").json()["total"] == 1

    (source / "gone.jpg").unlink()
    client.post("/api/scan")
    wait_for_scan(client)

    assert client.get("/api/photos").json() == {"items": [], "total": 0}


def test_upload_is_indexed_when_configured_photo_root_is_distinct(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    incoming = tmp_path / "incoming.jpg"
    incoming_bytes = make_jpeg(
        incoming,
        date="2026:06:07 08:09:10",
        model="Uploaded Camera",
        lens="Uploaded Lens",
        iso=400,
        focal=50,
    )
    client = make_client(tmp_path, source)

    uploaded = client.post(
        "/api/upload",
        files=[("files", ("original-name.jpg", incoming_bytes, "image/jpeg"))],
    )

    assert uploaded.status_code == 200
    assert uploaded.json() == {"accepted": 1, "errors": []}
    status = wait_for_scan(client)
    assert status["errors"] == []
    listing = client.get("/api/photos").json()
    assert listing["total"] == 1
    assert listing["items"][0]["name"] == "original-name.jpg"
    assert client.get(listing["items"][0]["download_url"]).content == incoming_bytes


def test_scan_reports_broken_photo_without_aborting(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "broken.jpg").write_bytes(b"not a jpeg")
    make_jpeg(
        source / "valid.jpg",
        date="2024:01:02 03:04:05",
        model="Valid Camera",
        lens="Valid Lens",
        iso=100,
        focal=24,
    )
    client = make_client(tmp_path, source)

    client.post("/api/scan")
    status = wait_for_scan(client)

    assert status["total"] == 2
    assert status["processed"] == 2
    assert any("broken.jpg" in error for error in status["errors"])
    assert client.get("/api/photos").json()["total"] == 1


def test_upload_rejects_unsupported_and_oversized_files(tmp_path: Path):
    client = make_client(tmp_path, max_upload_bytes=10)

    response = client.post(
        "/api/upload",
        files=[
            ("files", ("notes.txt", b"not a photo", "text/plain")),
            ("files", ("too-large.jpg", b"01234567890", "image/jpeg")),
        ],
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert len(response.json()["errors"]) == 2
    assert client.get("/api/photos").json() == {"items": [], "total": 0}


def test_media_rechecks_database_path_containment(tmp_path: Path):
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    make_jpeg(
        source / "inside.jpg",
        date="2024:01:02 03:04:05",
        model="Camera",
        lens="Lens",
        iso=100,
        focal=24,
    )
    outside_photo = outside / "outside.jpg"
    outside_photo.write_bytes((source / "inside.jpg").read_bytes())
    client = make_client(tmp_path, source)
    client.post("/api/scan")
    wait_for_scan(client)
    row = client.app.state.lumen_database.get_photo(1)
    assert row is not None
    with client.app.state.lumen_database.connect() as connection:
        connection.execute("UPDATE photos SET path = ? WHERE id = ?", (str(outside_photo), 1))

    assert client.get("/api/photos/1/download").status_code == 404
    assert client.get("/api/photos/1/preview").status_code == 404


def test_symlink_outside_photo_root_is_not_indexed(tmp_path: Path):
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    outside_photo = outside / "outside.jpg"
    make_jpeg(
        outside_photo,
        date="2024:01:02 03:04:05",
        model="Outside Camera",
        lens="Outside Lens",
        iso=100,
        focal=24,
    )
    try:
        (source / "escape.jpg").symlink_to(outside_photo)
    except OSError:
        pytest.skip("symlinks are not available")

    client = make_client(tmp_path, source)
    client.post("/api/scan")
    status = wait_for_scan(client)

    assert status["total"] == 0
    assert client.get("/api/photos").json() == {"items": [], "total": 0}


def test_invalid_sort_is_rejected(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.get("/api/photos", params={"sort": "not-a-sort"})

    assert response.status_code == 422
