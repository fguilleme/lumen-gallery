import errno
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app import create_app
from gallery.trash_worker import add_calendar_month, main, process_due_jobs, restore_job
from gallery.config import Settings
from gallery.db import Database


def _settings(tmp_path: Path) -> Settings:
    source = tmp_path / "source"
    source.mkdir()
    return Settings(
        project_dir=tmp_path,
        photo_root=source,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "uploads",
        access_password="secret",
        session_secret="secret",
        auth_required=False,
    )


def _photo(path: Path, *, rating: int = 0) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "root_path": str(path.parent.resolve()),
        "relative_path": path.name,
        "name": path.name,
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
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "exif": {},
        "media_type": "photo",
        "duration": None,
        "rating": rating,
    }


def test_due_move_requires_an_existing_zero_rated_catalogue_record(tmp_path: Path):
    settings = _settings(tmp_path)
    source = settings.photo_root / "orphan.jpg"
    source.write_bytes(b"keep-this-original")
    database = Database(settings.db_path)
    photo_id = database.upsert_photo(_photo(source))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    with database.connect() as connection:
        connection.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    result = process_due_jobs(
        settings, database, now=datetime(2024, 3, 1, tzinfo=timezone.utc), apply=True
    )
    assert result["moved"] == 0
    assert result["errors"]
    assert source.read_bytes() == b"keep-this-original"


def test_schedule_zero_persists_snapshot_and_calendar_due_date(tmp_path: Path):
    settings = _settings(tmp_path)
    source = settings.photo_root / "zero.jpg"
    source.write_bytes(b"zero-bytes")
    database = Database(settings.db_path)
    photo_id = database.upsert_photo(_photo(source))

    now = datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc)
    result = database.schedule_zero(now=now)

    assert result == {
        "scheduled": 1,
        "already_scheduled": 0,
        "total_due": 1,
        "due_at": "2024-02-29T12:00:00Z",
    }
    job = database.get_deletion_job_for_media(photo_id)
    assert job is not None
    assert job["media_id"] == photo_id
    assert job["path_key"] == str(source.resolve())
    assert job["status"] == "scheduled"
    assert job["original_name"] == "zero.jpg"
    assert job["scheduled_at"] == "2024-01-31T12:00:00Z"
    assert job["due_at"] == "2024-02-29T12:00:00Z"
    assert job["source_size"] == len(b"zero-bytes")
    assert job["source_mtime_ns"] == source.stat().st_mtime_ns
    assert job["trash_path"] is None
    assert job["error"] is None

    reopened = Database(settings.db_path)
    assert reopened.get_deletion_job_for_media(photo_id)["due_at"] == job["due_at"]  # type: ignore[index]


def test_schedule_zero_is_idempotent_and_rating_cancels_joined_annotation(tmp_path: Path):
    settings = _settings(tmp_path)
    first = settings.photo_root / "first.jpg"
    second = settings.photo_root / "second.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    database = Database(settings.db_path)
    first_id = database.upsert_photo(_photo(first))
    second_id = database.upsert_photo(_photo(second))
    database.update_rating(second_id, 1)

    now = datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc)
    assert database.schedule_zero(now=now)["scheduled"] == 1
    job = database.get_deletion_job_for_media(first_id)
    assert job is not None
    original_due = job["due_at"]

    repeated = database.schedule_zero(now=datetime(2024, 2, 29, 12, 0, tzinfo=timezone.utc))
    assert repeated["scheduled"] == 0
    assert repeated["already_scheduled"] == 1
    assert repeated["due_at"] == original_due
    assert database.get_deletion_job_for_media(first_id)["due_at"] == original_due  # type: ignore[index]
    assert database.get_deletion_job_for_media(second_id) is None

    scheduled = database.get_photo(first_id)
    assert scheduled is not None
    assert scheduled["deletion_scheduled_at"] == "2024-01-31T12:00:00Z"
    assert scheduled["delete_after"] == original_due
    assert scheduled["deletion_status"] == "scheduled"

    cancelled = database.update_rating(first_id, 1)
    assert cancelled is not None
    assert cancelled["deletion_scheduled_at"] is None
    assert cancelled["delete_after"] is None
    assert database.get_deletion_job_for_media(first_id)["status"] == "cancelled"  # type: ignore[index]

    database.update_rating(first_id, 0)
    assert database.get_deletion_job_for_media(first_id)["status"] == "cancelled"  # type: ignore[index]


def test_deletion_status_filter_composes_with_catalogue_filters(tmp_path: Path):
    settings = _settings(tmp_path)
    scheduled_path = settings.photo_root / "scheduled-cat.jpg"
    other_path = settings.photo_root / "other-cat.jpg"
    scheduled_path.write_bytes(b"scheduled")
    other_path.write_bytes(b"other")
    database = Database(settings.db_path)
    scheduled_id = database.upsert_photo(_photo(scheduled_path))
    database.upsert_photo(_photo(other_path))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))

    rows, total = database.list_photos(
        q="cat",
        deletion_status="scheduled",
        rating=0,
        sort="name_asc",
    )
    assert total == 2
    assert {row["id"] for row in rows} == {scheduled_id, database.get_photo_by_path(str(other_path.resolve()))["id"]}  # type: ignore[index]

    database.update_rating(scheduled_id, 1)
    rows, total = database.list_photos(deletion_status="scheduled")
    assert total == 1
    assert [row["name"] for row in rows] == ["other-cat.jpg"]


def test_deletion_api_schedule_status_filter_and_rating_undo(tmp_path: Path):
    settings = _settings(tmp_path)
    path = settings.photo_root / "api-zero.jpg"
    path.write_bytes(b"api-zero")
    database = Database(settings.db_path)
    photo_id = database.upsert_photo(_photo(path))
    client = TestClient(create_app(settings))

    assert client.get("/api/deletions/status").json() == {"scheduled": 0, "trashed": 0}
    scheduled = client.post("/api/deletions/schedule-zero", json={})
    assert scheduled.status_code == 200
    body = scheduled.json()
    assert body["scheduled"] == 1
    assert body["already_scheduled"] == 0
    assert body["total_due"] == 1
    assert body["due_at"].endswith("Z")

    repeated = client.post("/api/deletions/schedule-zero", json={})
    assert repeated.status_code == 200
    assert repeated.json()["scheduled"] == 0
    assert repeated.json()["already_scheduled"] == 1
    assert repeated.json()["due_at"] == body["due_at"]

    assert client.get("/api/deletions/status").json() == {"scheduled": 1, "trashed": 0}
    listing = client.get("/api/photos", params={"deletion_status": "scheduled"})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    item = listing.json()["items"][0]
    assert item["id"] == photo_id
    assert item["deletion_scheduled_at"]
    assert item["delete_after"] == body["due_at"]
    assert client.get(f"/api/photos/{photo_id}").json()["delete_after"] == body["due_at"]

    undone = client.patch(f"/api/photos/{photo_id}/rating", json={"rating": 1})
    assert undone.status_code == 200
    assert undone.json()["rating"] == 1
    assert undone.json()["deletion_scheduled_at"] is None
    assert undone.json()["delete_after"] is None
    assert client.get("/api/deletions/status").json() == {"scheduled": 0, "trashed": 0}


def test_explicit_cancel_endpoint_and_trash_recovery_listing(tmp_path: Path):
    settings = _settings(tmp_path)
    path = settings.photo_root / "cancel-me.jpg"
    path.write_bytes(b"cancel-me")
    database = Database(settings.db_path)
    photo_id = database.upsert_photo(_photo(path))
    client = TestClient(create_app(settings))

    assert client.post("/api/deletions/schedule-zero", json={}).status_code == 200
    cancelled = client.post(f"/api/photos/{photo_id}/deletion/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["deletion_scheduled_at"] is None
    assert database.get_deletion_job_for_media(photo_id)["status"] == "cancelled"  # type: ignore[index]

    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    job = database.get_deletion_job_for_media(photo_id)
    assert job is not None
    process_due_jobs(
        settings,
        database,
        now=datetime(2024, 2, 1, tzinfo=timezone.utc),
        apply=True,
    )
    trash = client.get("/api/deletions/trash")
    assert trash.status_code == 200
    assert trash.json()["total"] == 1
    listed = trash.json()["items"][0]
    assert listed["id"] == job["id"]
    assert listed["media_id"] == photo_id
    assert listed["status"] == "trashed"
    assert client.get("/api/photos").json()["total"] == 0
    assert client.get("/api/facets").json() == {"cameras": [], "lenses": [], "formats": [], "directories": []}
    assert client.patch(f"/api/photos/{photo_id}/rating", json={"rating": 1}).status_code == 404


def test_schedule_zero_requires_session_and_same_origin(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.auth_required = True
    client = TestClient(create_app(settings))

    assert client.post("/api/deletions/schedule-zero", json={}).status_code == 401
    assert client.post(
        "/api/deletions/schedule-zero",
        json={},
        headers={"Origin": "https://evil.example"},
    ).status_code == 403

    assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    assert client.post(
        "/api/deletions/schedule-zero",
        json={},
        headers={"Origin": "https://evil.example"},
    ).status_code == 403


def test_worker_dry_run_apply_manifest_and_restore_preserve_bytes(tmp_path: Path):
    settings = _settings(tmp_path)
    source = settings.photo_root / "recover.jpg"
    source.write_bytes(b"original-content")
    original_bytes = source.read_bytes()
    database = Database(settings.db_path)
    video = _photo(source)
    video.update(format="MP4", media_type="video", duration=2.5)
    photo_id = database.upsert_photo(video)
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    job = database.get_deletion_job_for_media(photo_id)
    assert job is not None
    due = datetime(2024, 2, 1, tzinfo=timezone.utc)

    dry_run = process_due_jobs(settings, database, now=due, apply=False)
    assert dry_run["checked"] == 1
    assert dry_run["moved"] == 0
    assert source.read_bytes() == original_bytes
    assert database.get_deletion_job_for_media(photo_id)["status"] == "scheduled"  # type: ignore[index]

    applied = process_due_jobs(settings, database, now=due, apply=True)
    assert applied["moved"] == 1
    assert not source.exists()
    trashed = database.get_deletion_job(job["id"])
    assert trashed is not None
    assert trashed["status"] == "trashed"
    trash_path = Path(str(trashed["trash_path"]))
    assert trash_path.is_file()
    assert trash_path.read_bytes() == original_bytes
    manifest = json.loads((trash_path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["job_id"] == job["id"]
    assert manifest["source"] == str(source.resolve())
    assert manifest["trash"] == str(trash_path)

    restored = restore_job(settings, database, job["id"])
    assert restored["restored"] is True
    assert source.read_bytes() == original_bytes
    assert trash_path.exists() is False
    restored_photo = database.get_photo(photo_id)
    assert restored_photo is not None
    assert restored_photo["rating"] == 1
    assert restored_photo["format"] == "MP4"
    assert restored_photo["media_type"] == "video"
    assert restored_photo["is_trashed"] == 0
    assert database.get_deletion_job(job["id"])["status"] == "cancelled"  # type: ignore[index]


def test_worker_marks_changed_source_error_without_moving_it(tmp_path: Path):
    settings = _settings(tmp_path)
    source = settings.photo_root / "changed.jpg"
    source.write_bytes(b"before")
    database = Database(settings.db_path)
    photo_id = database.upsert_photo(_photo(source))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    source.write_bytes(b"after-and-different")

    result = process_due_jobs(
        settings,
        database,
        now=datetime(2024, 2, 1, tzinfo=timezone.utc),
        apply=True,
    )

    assert result["moved"] == 0
    assert result["errors"]
    assert source.read_bytes() == b"after-and-different"
    assert database.get_deletion_job_for_media(photo_id)["status"] == "error"  # type: ignore[index]
    assert not (settings.data_dir / "trash").exists()


def test_worker_rejects_symlink_and_cross_root_catalogue_paths(tmp_path: Path):
    settings = _settings(tmp_path)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    link = settings.photo_root / "link.jpg"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    database = Database(settings.db_path)
    linked = _photo(outside)
    linked.update(
        path=str(link.absolute()),
        root_path=str(settings.photo_root),
        relative_path="link.jpg",
        name="link.jpg",
    )
    linked_id = database.upsert_photo(linked)
    escaped_id = database.upsert_photo(_photo(outside))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))

    result = process_due_jobs(
        settings,
        database,
        now=datetime(2024, 2, 1, tzinfo=timezone.utc),
        apply=True,
    )

    assert result["moved"] == 0
    assert database.get_deletion_job_for_media(linked_id)["status"] == "error"  # type: ignore[index]
    assert database.get_deletion_job_for_media(escaped_id)["status"] == "error"  # type: ignore[index]
    assert outside.read_bytes() == b"outside"
    assert link.is_symlink()


def test_restore_collision_is_blocked_without_overwrite(tmp_path: Path):
    settings = _settings(tmp_path)
    source = settings.photo_root / "collision.jpg"
    source.write_bytes(b"original")
    database = Database(settings.db_path)
    photo_id = database.upsert_photo(_photo(source))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    job = database.get_deletion_job_for_media(photo_id)
    assert job is not None
    process_due_jobs(
        settings,
        database,
        now=datetime(2024, 2, 1, tzinfo=timezone.utc),
        apply=True,
    )
    source.write_bytes(b"new-destination")

    restored = restore_job(settings, database, job["id"])
    assert restored["restored"] is False
    assert restored["error"] == "original destination exists"
    assert source.read_bytes() == b"new-destination"
    assert database.get_deletion_job(job["id"])["status"] == "trashed"  # type: ignore[index]


def test_worker_recovers_manifest_window_and_rejects_source_trash_both_present(tmp_path: Path):
    settings = _settings(tmp_path)
    source = settings.photo_root / "window.jpg"
    source.write_bytes(b"window")
    database = Database(settings.db_path)
    photo_id = database.upsert_photo(_photo(source))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    job = database.get_deletion_job_for_media(photo_id)
    assert job is not None
    trash = settings.data_dir / "trash" / "recover-window" / source.name
    trash.parent.mkdir(parents=True)
    source.rename(trash)
    manifest = {
        "job_id": job["id"],
        "media_id": photo_id,
        "source": str(source.resolve()),
        "trash": str(trash),
        "time": "2024-02-01T00:00:00Z",
        "source_size": job["source_size"],
        "source_mtime_ns": job["source_mtime_ns"],
    }
    (trash.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with database.connect() as connection:
        connection.execute(
            "UPDATE deletion_jobs SET trash_path = ? WHERE id = ?",
            (str(trash), job["id"]),
        )

    dry_recovery = process_due_jobs(
        settings,
        database,
        now=datetime(2024, 2, 1, tzinfo=timezone.utc),
        apply=False,
    )
    assert dry_recovery["errors"] == []
    assert database.get_deletion_job(job["id"])["status"] == "scheduled"  # type: ignore[index]

    recovered = process_due_jobs(
        settings,
        database,
        now=datetime(2024, 2, 1, tzinfo=timezone.utc),
        apply=True,
    )
    assert recovered["recovered"] == 1
    assert database.get_deletion_job(job["id"])["status"] == "trashed"  # type: ignore[index]

    both = settings.photo_root / "both.jpg"
    both.write_bytes(b"both")
    both_id = database.upsert_photo(_photo(both))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    both_job = database.get_deletion_job_for_media(both_id)
    assert both_job is not None
    both_trash = settings.data_dir / "trash" / "both-window" / both.name
    both_trash.parent.mkdir(parents=True)
    both_trash.write_bytes(b"copy")
    both_manifest = dict(manifest)
    both_manifest.update(
        job_id=both_job["id"],
        media_id=both_id,
        source=str(both.resolve()),
        trash=str(both_trash),
        source_size=both_job["source_size"],
        source_mtime_ns=both_job["source_mtime_ns"],
    )
    (both_trash.parent / "manifest.json").write_text(json.dumps(both_manifest), encoding="utf-8")
    with database.connect() as connection:
        connection.execute(
            "UPDATE deletion_jobs SET trash_path = ? WHERE id = ?",
            (str(both_trash), both_job["id"]),
        )

    blocked = process_due_jobs(
        settings,
        database,
        now=datetime(2024, 2, 1, tzinfo=timezone.utc),
        apply=True,
    )
    assert blocked["moved"] == 0
    assert database.get_deletion_job(both_job["id"])["status"] == "error"  # type: ignore[index]
    assert both.exists() and both_trash.exists()


def test_worker_cross_filesystem_move_fails_without_unlinking_source(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    source = settings.photo_root / "cross-device.jpg"
    source.write_bytes(b"cross-device")
    database = Database(settings.db_path)
    photo_id = database.upsert_photo(_photo(source))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))

    def fail_rename(_source, _destination):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr("gallery.trash_worker.os.rename", fail_rename)
    result = process_due_jobs(
        settings,
        database,
        now=datetime(2024, 2, 1, tzinfo=timezone.utc),
        apply=True,
    )

    assert result["moved"] == 0
    assert result["errors"]
    assert source.read_bytes() == b"cross-device"
    assert database.get_deletion_job_for_media(photo_id)["status"] == "error"  # type: ignore[index]


def test_reconcile_and_scan_upsert_preserve_scheduled_and_trashed_jobs(tmp_path: Path):
    settings = _settings(tmp_path)
    scheduled_source = settings.photo_root / "scheduled.jpg"
    trashed_source = settings.photo_root / "trashed.jpg"
    scheduled_source.write_bytes(b"scheduled")
    trashed_source.write_bytes(b"trashed")
    database = Database(settings.db_path)
    scheduled_id = database.upsert_photo(_photo(scheduled_source))
    trashed_id = database.upsert_photo(_photo(trashed_source))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    scheduled_job = database.get_deletion_job_for_media(scheduled_id)
    trashed_job = database.get_deletion_job_for_media(trashed_id)
    assert scheduled_job is not None and trashed_job is not None

    with database.connect() as connection:
        connection.execute(
            "UPDATE deletion_jobs SET due_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00Z", scheduled_job["id"]),
        )

    process_due_jobs(
        settings,
        database,
        now=datetime(2024, 2, 1, tzinfo=timezone.utc),
        apply=True,
    )
    assert database.get_deletion_job(trashed_job["id"])["status"] == "trashed"  # type: ignore[index]
    database.reconcile([])

    assert database.get_photo(scheduled_id) is not None
    assert database.get_photo(trashed_id) is not None
    assert database.list_photos(deletion_status="scheduled")[1] == 1
    assert database.list_photos()[1] == 1

    trashed_source.write_bytes(b"reappeared")
    database.upsert_photo(_photo(trashed_source))
    assert database.get_deletion_job(trashed_job["id"])["status"] == "trashed"  # type: ignore[index]
    assert database.list_photos()[1] == 1


def test_worker_holds_database_lock_through_final_move_check(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    source = settings.photo_root / "concurrent.jpg"
    source.write_bytes(b"concurrent")
    database = Database(settings.db_path)
    photo_id = database.upsert_photo(_photo(source))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    entered_rename = threading.Event()
    release_rename = threading.Event()
    update_finished = threading.Event()
    worker_result: list[dict[str, Any]] = []
    real_rename = os.rename

    def blocking_rename(source_path, destination_path):
        entered_rename.set()
        assert release_rename.wait(timeout=5)
        return real_rename(source_path, destination_path)

    monkeypatch.setattr("gallery.trash_worker.os.rename", blocking_rename)

    worker = threading.Thread(
        target=lambda: worker_result.append(
            process_due_jobs(
                settings,
                database,
                now=datetime(2024, 2, 1, tzinfo=timezone.utc),
                apply=True,
            )
        )
    )
    worker.start()
    assert entered_rename.wait(timeout=5)

    def update_rating():
        database.update_rating(photo_id, 1)
        update_finished.set()

    updater = threading.Thread(target=update_rating)
    updater.start()
    time.sleep(0.1)
    assert not update_finished.is_set()
    release_rename.set()
    worker.join(timeout=5)
    updater.join(timeout=5)
    assert not worker.is_alive() and not updater.is_alive()
    assert worker_result[0]["moved"] == 1
    assert update_finished.is_set()
    assert database.get_photo(photo_id)["rating"] == 0  # type: ignore[index]
    assert database.get_deletion_job_for_media(photo_id)["status"] == "trashed"  # type: ignore[index]


def test_worker_cli_defaults_to_dry_run_and_apply_is_explicit(tmp_path: Path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    source = settings.photo_root / "cli.jpg"
    source.write_bytes(b"cli")
    database = Database(settings.db_path)
    database.upsert_photo(_photo(source))
    database.schedule_zero(now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setenv("PHOTO_ROOT", str(settings.photo_root))
    monkeypatch.setenv("PHOTO_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("PHOTO_UPLOAD_DIR", str(settings.upload_dir))
    monkeypatch.setenv("PHOTO_DB", str(settings.db_path))

    assert main([]) == 0
    default_output = json.loads(capsys.readouterr().out)
    assert default_output["checked"] == 1
    assert default_output["moved"] == 0
    assert source.exists()

    assert main(["--dry-run", "--max-batch", "1000"]) == 0
    dry_output = json.loads(capsys.readouterr().out)
    assert dry_output["moved"] == 0
    assert source.exists()

    assert main(["--apply"]) == 0
    apply_output = json.loads(capsys.readouterr().out)
    assert apply_output["moved"] == 1
    assert not source.exists()


def test_calendar_month_due_clamps_end_of_month_and_leap_years():
    assert add_calendar_month(datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc)) == datetime(
        2024, 2, 29, 12, 0, tzinfo=timezone.utc
    )
    assert add_calendar_month(datetime(2023, 1, 31, 12, 0, tzinfo=timezone.utc)) == datetime(
        2023, 2, 28, 12, 0, tzinfo=timezone.utc
    )
    assert add_calendar_month(datetime(2024, 3, 31, 12, 0, tzinfo=timezone.utc)) == datetime(
        2024, 4, 30, 12, 0, tzinfo=timezone.utc
    )
