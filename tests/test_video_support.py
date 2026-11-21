from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app import Settings, create_app
from gallery.db import Database


PROJECT = Path(__file__).resolve().parents[1]
EXIFTOOL = PROJECT / "vendor" / "exiftool" / "exiftool"
FFMPEG = shutil.which("ffmpeg") or "/sbin/ffmpeg"


def make_video(path: Path, *, creation_time: str | None, rotate90: bool = False) -> bytes:
    audio_codec = "pcm_s16le" if path.suffix.lower() == ".mov" else "aac"
    command = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if rotate90:
        command.extend(["-display_rotation", "90", "-noautorotate"])
    command.extend(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x40:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=500:sample_rate=8000",
            "-t",
            "1.2",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            audio_codec,
        ]
    )
    if creation_time is not None:
        command.extend(["-metadata", f"creation_time={creation_time}"])
    if path.suffix.lower() == ".mp4":
        command.extend(["-movflags", "+faststart"])
    command.extend(["-y", str(path)])
    subprocess.run(command, check=True, capture_output=True)
    return path.read_bytes()


def make_client(tmp_path: Path, source: Path) -> TestClient:
    settings = Settings(
        project_dir=PROJECT,
        photo_root=source,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "uploads",
        access_password="secret",
        session_secret="session-secret",
        exiftool_path=EXIFTOOL,
    )
    client = TestClient(create_app(settings))
    assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    return client


def wait_for_scan(client: TestClient) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client.get("/api/status").json()
        if not status["running"]:
            return status
        time.sleep(0.01)
    raise AssertionError("scan did not finish within 10 seconds")


def fake_photo_metadata(path: Path, *, name: str | None = None) -> dict:
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


def test_existing_photo_rows_migrate_to_media_columns_without_changing_values(tmp_path: Path):
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
                exif_json TEXT NOT NULL DEFAULT '{}'
            );
            INSERT INTO photos(
                path, root_path, relative_path, name, format, date, camera, lens,
                iso, aperture, shutter, focal, width, height, size, mtime_ns, exif_json
            ) VALUES (
                '/photos/original.jpg', '/photos', 'original.jpg', 'original.jpg', 'JPEG',
                '2024-01-02T03:04:05', 'Camera', 'Lens', 100, 2.8, '1/125', 35,
                80, 40, 1234, 5678, '{"Model":"Camera"}'
            );
            """
        )

    database = Database(database_path)
    row = database.get_photo(1)

    assert row is not None
    assert row["path"] == "/photos/original.jpg"
    assert row["name"] == "original.jpg"
    assert row["format"] == "JPEG"
    assert row["date"] == "2024-01-02T03:04:05"
    assert row["camera"] == "Camera"
    assert row["lens"] == "Lens"
    assert row["width"] == 80
    assert row["height"] == 40
    assert row["size"] == 1234
    assert row["mtime_ns"] == 5678
    assert row["exif_json"] == '{"Model":"Camera"}'
    assert row["media_type"] == "photo"
    assert row["duration"] is None


def test_real_mp4_and_mov_scan_with_video_metadata_and_filters(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    make_video(source / "portrait.mp4", creation_time="2024-01-02T03:04:05Z", rotate90=True)
    make_video(source / "portrait.mov", creation_time="2025-05-06T07:08:09Z", rotate90=True)

    client = make_client(tmp_path, source)
    assert client.post("/api/scan").json() == {"running": True}
    status = wait_for_scan(client)

    assert status["total"] == 2
    assert status["processed"] == 2
    assert status["errors"] == []

    listing = client.get("/api/photos", params={"media_type": "video", "sort": "date_desc"})
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2
    assert [item["name"] for item in body["items"]] == ["portrait.mov", "portrait.mp4"]

    for item in body["items"]:
        assert item["media_type"] == "video"
        assert item["duration"] == pytest.approx(1.2, abs=0.1)
        assert (item["width"], item["height"]) == (40, 64)
        assert isinstance(item["camera"], str)
        assert isinstance(item["lens"], str)
        assert item["stream_url"] == f"/api/photos/{item['id']}/stream"
        assert item["thumb_url"] == f"/api/photos/{item['id']}/thumb"
        assert item["preview_url"] == f"/api/photos/{item['id']}/preview"
        assert item["download_url"] == f"/api/photos/{item['id']}/download"

    assert client.get("/api/photos", params={"media_type": "photo"}).json() == {
        "items": [],
        "total": 0,
    }
    assert client.get("/api/photos", params={"format": "MP4"}).json()["total"] == 1
    assert client.get("/api/photos", params={"format": "MOV"}).json()["total"] == 1
    assert client.get("/api/photos", params={"media_type": "audio"}).status_code == 422


def test_mov_with_secondary_incompatible_audio_uses_only_primary_audio(tmp_path: Path):
    source=tmp_path/'source'
    source.mkdir()
    path=source/'multi-audio.mov'
    subprocess.run([
        FFMPEG,'-hide_banner','-loglevel','error','-y',
        '-f','lavfi','-i','testsrc2=size=64x40:rate=10',
        '-f','lavfi','-i','sine=frequency=500:sample_rate=48000',
        '-f','lavfi','-i','sine=frequency=800:sample_rate=48000',
        '-t','0.5','-map','0:v:0','-map','1:a:0','-map','2:a:0',
        '-c:v','libx264','-pix_fmt','yuv420p','-c:a:0','aac','-c:a:1','pcm_s16le',str(path)
    ],check=True,capture_output=True)
    original=hashlib.sha256(path.read_bytes()).hexdigest()
    client=make_client(tmp_path,source)
    assert client.post('/api/scan').status_code==200
    assert wait_for_scan(client)['errors']==[]
    item=client.get('/api/photos').json()['items'][0]
    response=client.get(item['stream_url'])
    assert response.status_code==200,response.text[:500]
    prepared=tmp_path/'prepared.mp4'
    prepared.write_bytes(response.content)
    info=json.loads(subprocess.run(['ffprobe','-v','error','-show_streams','-of','json',str(prepared)],check=True,capture_output=True,text=True).stdout)
    audio=[stream for stream in info['streams'] if stream['codec_type']=='audio']
    assert len(audio)==1
    assert audio[0]['codec_name']=='aac'
    assert next(stream for stream in info['streams'] if stream['codec_type']=='video')['codec_name']=='h264'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==original


def test_video_preview_stream_remux_range_download_and_source_integrity(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    mp4 = source / "portrait.mp4"
    mov = source / "portrait.mov"
    mp4_bytes = make_video(mp4, creation_time="2024-01-02T03:04:05Z", rotate90=True)
    mov_bytes = make_video(mov, creation_time="2025-05-06T07:08:09Z", rotate90=True)
    os.chmod(mp4, 0o444)
    os.chmod(mov, 0o444)
    source_before = {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode)
        for path in (mp4, mov)
    }

    client = make_client(tmp_path, source)
    client.post("/api/scan")
    status = wait_for_scan(client)
    assert status["errors"] == []
    items = client.get("/api/photos", params={"media_type": "video", "sort": "name_asc"}).json()["items"]
    by_format = {item["format"]: item for item in items}

    for item in items:
        thumbnail = client.get(item["thumb_url"])
        preview = client.get(item["preview_url"])
        assert thumbnail.status_code == 200
        assert preview.status_code == 200
        with Image.open(BytesIO(thumbnail.content)) as image:
            assert image.format == "JPEG"
            assert image.width <= 480
            assert image.height <= 480
        with Image.open(BytesIO(preview.content)) as image:
            assert image.format == "JPEG"
            assert image.size == (40, 64)

    mp4_stream = client.get(by_format["MP4"]["stream_url"])
    assert mp4_stream.status_code == 200
    assert mp4_stream.headers["content-type"].startswith("video/mp4")
    assert mp4_stream.headers["accept-ranges"] == "bytes"
    assert mp4_stream.content == mp4_bytes

    ranged = client.get(by_format["MP4"]["stream_url"], headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206
    assert ranged.headers["accept-ranges"] == "bytes"
    assert ranged.headers["content-range"] == f"bytes 0-99/{len(mp4_bytes)}"
    assert ranged.content == mp4_bytes[:100]

    mov_stream = client.get(by_format["MOV"]["stream_url"])
    assert mov_stream.status_code == 200
    assert mov_stream.headers["content-type"].startswith("video/mp4")
    assert mov_stream.content != mov_bytes
    remuxed = tmp_path / "remuxed.mp4"
    remuxed.write_bytes(mov_stream.content)
    probe = subprocess.run(
        [
            shutil.which("ffprobe") or "/sbin/ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(remuxed),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    assert next(stream["codec_name"] for stream in streams if stream["codec_type"] == "video") == "h264"
    assert next(stream["codec_name"] for stream in streams if stream["codec_type"] == "audio") == "aac"
    video_stream = next(stream for stream in streams if stream["codec_type"] == "video")
    assert video_stream["side_data_list"][0]["rotation"] == 90

    mp4_download = client.get(by_format["MP4"]["download_url"])
    mov_download = client.get(by_format["MOV"]["download_url"])
    assert mp4_download.status_code == 200
    assert mov_download.status_code == 200
    assert mp4_download.headers["content-type"].startswith("video/mp4")
    assert mov_download.headers["content-type"].startswith("video/quicktime")
    assert mp4_download.content == mp4_bytes
    assert mov_download.content == mov_bytes
    assert 'attachment; filename="portrait.mp4"' in mp4_download.headers["content-disposition"]
    assert 'attachment; filename="portrait.mov"' in mov_download.headers["content-disposition"]

    for path in (mp4, mov):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source_before[path.name][0]
        assert path.stat().st_mode == source_before[path.name][1]


def test_upload_during_scan_queues_one_bounded_rescan(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    initial = source / "initial.jpg"
    initial.write_bytes(b"initial")
    started = threading.Event()
    release = threading.Event()
    scan_runs: list[int] = []

    def fake_inspect(path: Path, _tool: object, *, name: str | None = None) -> dict:
        if path.name == "initial.jpg" and not started.is_set():
            started.set()
            assert release.wait(timeout=5)
        return fake_photo_metadata(path, name=name)

    monkeypatch.setattr("gallery.scanner.inspect_file", fake_inspect)
    real_scan = __import__("gallery.scanner", fromlist=["PhotoScanner"]).PhotoScanner.scan

    def counted_scan(scanner):
        scan_runs.append(1)
        return real_scan(scanner)

    monkeypatch.setattr("gallery.scanner.PhotoScanner.scan", counted_scan)
    client = make_client(tmp_path, source)
    assert client.post("/api/scan").json() == {"running": True}
    assert started.wait(timeout=5)

    uploaded = client.post(
        "/api/upload",
        files=[("files", ("queued.jpg", b"queued", "image/jpeg"))],
    )
    assert uploaded.status_code == 200
    assert uploaded.json() == {"accepted": 1, "errors": []}
    for _ in range(5):
        assert client.post("/api/scan").json() == {"running": True}

    release.set()
    for _ in range(200):
        if len(scan_runs) == 2 and not client.get("/api/status").json()["running"]:
            break
        time.sleep(0.01)

    assert scan_runs == [1, 1]
    assert client.get("/api/photos").json()["total"] == 2


def test_video_upload_is_indexed_from_a_distinct_configured_upload_root(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    uploaded_bytes = make_video(tmp_path / "fixture.mp4", creation_time="2026-06-07T08:09:10Z")
    client = make_client(tmp_path, source)

    uploaded = client.post(
        "/api/upload",
        files=[("files", ("uploaded-video.mp4", uploaded_bytes, "video/mp4"))],
    )
    assert uploaded.status_code == 200
    assert uploaded.json() == {"accepted": 1, "errors": []}
    status = wait_for_scan(client)
    assert status["errors"] == []

    listing = client.get("/api/photos", params={"media_type": "video"}).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["name"] == "uploaded-video.mp4"
    assert item["media_type"] == "video"
    download = client.get(item["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("video/mp4")
    assert download.content == uploaded_bytes


def test_photo_items_keep_backward_compatible_media_fields(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    image = Image.new("RGB", (40, 20), (10, 20, 30))
    image.save(source / "photo.jpg", format="JPEG")
    client = make_client(tmp_path, source)
    client.post("/api/scan")
    assert wait_for_scan(client)["errors"] == []

    item = client.get("/api/photos", params={"media_type": "photo"}).json()["items"][0]
    assert item["media_type"] == "photo"
    assert item["duration"] is None
    assert item["stream_url"] is None
    assert client.get(f"/api/photos/{item['id']}/stream").status_code == 404


def test_video_date_falls_back_to_source_mtime_without_creation_metadata(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    video = source / "mtime.mp4"
    make_video(video, creation_time=None)
    fallback_time = datetime(2022, 2, 3, 4, 5, 6, tzinfo=timezone.utc).timestamp()
    os.utime(video, (fallback_time, fallback_time))

    client = make_client(tmp_path, source)
    client.post("/api/scan")
    assert wait_for_scan(client)["errors"] == []
    item = client.get("/api/photos").json()["items"][0]

    assert item["date"].startswith("2022-02-03T04:05:06")
    detail = client.get(f"/api/photos/{item['id']}").json()
    assert detail["exif"]["VideoCodec"] == "h264"
    assert detail["exif"]["AudioCodec"] == "aac"
    assert "SourceFile" not in detail["exif"]
