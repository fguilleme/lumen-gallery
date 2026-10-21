from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from gallery.config import Settings
from gallery.db import Database
from gallery.exif import inspect_file
from gallery.scanner import PhotoScanner, ScanProgress, _scan_worker_count


def make_scanner(tmp_path: Path) -> tuple[PhotoScanner, Database, Path]:
    source = tmp_path / "photos"
    source.mkdir()
    settings = Settings(
        project_dir=tmp_path,
        photo_root=source,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "uploads",
        access_password="secret",
        session_secret="secret",
    )
    database = Database(settings.db_path)
    scanner = PhotoScanner(
        settings,
        database,
        object(),
        ScanProgress(lambda _total: None, lambda: None, lambda _message: None),
    )
    return scanner, database, source


def inspected_photo(path: Path, *, name: str | None = None) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": name or path.name,
        "format": "JPEG",
        "date": "2024-01-01T00:00:00",
        "camera": "Test Camera",
        "lens": "Test Lens",
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


def test_scan_uses_bounded_concurrent_workers_and_isolates_errors(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PHOTO_SCAN_WORKERS", raising=False)
    scanner, database, source = make_scanner(tmp_path)
    names = ["one.jpg", "two.jpg", "broken.jpg", "four.jpg"]
    for name in names:
        (source / name).write_bytes(name.encode())

    barrier = threading.Barrier(len(names))
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    inspected_names: list[str] = []
    totals: list[int] = []
    processed: list[bool] = []
    errors: list[str] = []
    progress = ScanProgress(totals.append, lambda: processed.append(True), errors.append)
    scanner.progress = progress

    real_reconcile = database.reconcile
    reconcile_processed: list[int] = []

    def reconcile(current_paths):
        reconcile_processed.append(len(processed))
        return real_reconcile(current_paths)

    monkeypatch.setattr(database, "reconcile", reconcile)

    def fake_inspect(path: Path, _tool: object, *, name: str | None = None) -> dict[str, Any]:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            inspected_names.append(path.name)
        try:
            barrier.wait(timeout=2)
            if path.name == "broken.jpg":
                raise ValueError("broken photo")
            return inspected_photo(path, name=name)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr("gallery.scanner.inspect_file", fake_inspect)

    scanner.scan()

    assert max_active == 4
    assert sorted(inspected_names) == sorted(names)
    assert totals == [4]
    assert len(processed) == 4
    assert errors == ["broken.jpg: broken photo"]
    assert reconcile_processed == [4]
    rows, total = database.list_photos(sort="name_asc")
    assert total == 3
    assert [row["name"] for row in rows] == ["four.jpg", "one.jpg", "two.jpg"]


def test_scan_skips_unchanged_files_on_incremental_scan(tmp_path: Path, monkeypatch):
    scanner, database, source = make_scanner(tmp_path)
    names = ["one.jpg", "two.jpg"]
    for name in names:
        (source / name).write_bytes(name.encode())

    inspected_names: list[str] = []

    def fake_inspect(path: Path, _tool: object, *, name: str | None = None) -> dict[str, Any]:
        inspected_names.append(path.name)
        return inspected_photo(path, name=name)

    monkeypatch.setattr("gallery.scanner.inspect_file", fake_inspect)
    first_processed: list[bool] = []
    first_errors: list[str] = []
    scanner.progress = ScanProgress(lambda _total: None, lambda: first_processed.append(True), first_errors.append)

    scanner.scan()
    assert sorted(inspected_names) == sorted(names)
    assert len(first_processed) == 2
    assert first_errors == []

    scanner.scan()

    assert sorted(inspected_names) == sorted(names)
    assert len(first_processed) == 4
    assert first_errors == []
    assert database.list_photos()[1] == 2


def test_inspect_file_swaps_oriented_dimensions_without_transposing_pixels(tmp_path: Path, monkeypatch):
    path = tmp_path / "oriented.jpg"
    image = Image.new("RGB", (40, 80), (10, 20, 30))
    exif = Image.Exif()
    exif[0x0112] = 6
    image.save(path, format="JPEG", exif=exif)

    class FakeExifTool:
        def read_metadata(self, _path: Path) -> dict[str, int]:
            return {"Orientation": 6}

    transpose_calls: list[object] = []

    def fail_transpose(self, *args: object, **kwargs: object):
        transpose_calls.append(self)
        raise AssertionError("metadata inspection transposed image pixels")

    monkeypatch.setattr(Image.Image, "transpose", fail_transpose)

    inspected = inspect_file(path, FakeExifTool())

    assert (inspected["width"], inspected["height"]) == (80, 40)
    assert transpose_calls == []


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, 4),
        ("1", 1),
        ("8", 8),
        ("0", 1),
        ("-2", 1),
        ("99", 8),
        ("not-a-number", 4),
    ],
)
def test_photo_scan_worker_count_is_bounded(monkeypatch, raw_value: str | None, expected: int):
    if raw_value is None:
        monkeypatch.delenv("PHOTO_SCAN_WORKERS", raising=False)
    else:
        monkeypatch.setenv("PHOTO_SCAN_WORKERS", raw_value)

    assert _scan_worker_count() == expected
