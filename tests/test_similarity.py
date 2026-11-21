from __future__ import annotations

import time
import threading
import shutil
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from gallery.db import Database
from gallery.similarity import CLIP_MODEL_VERSION, PHASH_VERSION, SimilarityManager


class FakeSettings:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir


class FakeDatabase:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls: list[dict] = []

    def list_photos(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows), len(self.rows)


class FakeMedia:
    def __init__(self, failures: set[str] | None = None):
        self.failures = failures or set()
        self.calls: list[str] = []

    def jpeg(self, kind: str, row: dict) -> Path:
        self.calls.append(row["name"])
        if row["name"] in self.failures:
            raise OSError("broken preview")
        return Path(row["path"])


class MappingEmbedder:
    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = {name: np.asarray(value, dtype=np.float32) for name, value in vectors.items()}
        self.calls: list[list[Path]] = []

    def embed(self, paths: list[Path]) -> np.ndarray:
        self.calls.append(list(paths))
        return np.stack([self.vectors[path.name] for path in paths])


class BlockingEmbedder(MappingEmbedder):
    def __init__(self, vectors: dict[str, list[float]]):
        super().__init__(vectors)
        self.started = threading.Event()
        self.release = threading.Event()

    def embed(self, paths: list[Path]) -> np.ndarray:
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test embedder was not released")
        return super().embed(paths)


def make_image(path: Path, *, color: tuple[int, int, int], quality: int = 95) -> Path:
    image = Image.new("RGB", (96, 64), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, 40, 52), fill=(255 - color[0], 255 - color[1], 255 - color[2]))
    draw.line((0, 0, 95, 63), fill=(20, 30, 40), width=3)
    image.save(path, format="JPEG", quality=quality)
    return path


def row_for(path: Path, photo_id: int, *, media_type: str = "photo") -> dict:
    stat = path.stat()
    return {
        "id": photo_id,
        "name": path.name,
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "media_type": media_type,
    }


def wait_for_manager(manager: SimilarityManager, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.snapshot()
        if not status["running"]:
            return status
        time.sleep(0.01)
    raise AssertionError("similarity analysis did not finish")


def test_reanalysis_progress_counts_current_pass_once(tmp_path: Path, monkeypatch):
    path = make_image(tmp_path / 'cached.jpg', color=(50, 100, 160))
    manager = SimilarityManager(FakeSettings(tmp_path / 'data'), FakeDatabase([row_for(path, 1)]), FakeMedia(), MappingEmbedder({'cached.jpg': [1, 0]}))
    assert manager.start()
    assert wait_for_manager(manager)['analyzed'] == 1
    started, release = threading.Event(), threading.Event()
    cluster = manager._cluster

    def pause_before_publish(features, mode, matches):
        started.set()
        assert release.wait(5)
        return cluster(features, mode, matches)

    monkeypatch.setattr(manager, '_cluster', pause_before_publish)
    assert manager.start()
    try:
        assert started.wait(5)
        status = manager.snapshot()
        assert status['cached'] == status['processed'] == status['analyzed'] == status['total'] == 1
    finally:
        release.set()
        wait_for_manager(manager)


def test_similarity_manager_initializes_only_its_database(tmp_path: Path):
    class Settings:
        data_dir = tmp_path / "data"

    manager = SimilarityManager(Settings(), object(), object())

    assert manager.database_path == tmp_path / "data" / "similarity.sqlite3"
    assert manager.snapshot()["running"] is False
    assert manager.embedder is None
    assert CLIP_MODEL_VERSION.endswith("9299")
    assert PHASH_VERSION


def test_initialization_does_not_add_similarity_tables_to_catalogue(tmp_path: Path):
    catalogue = Database(tmp_path / "catalogue.sqlite3")
    with catalogue.connect() as connection:
        before = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]

    SimilarityManager(FakeSettings(tmp_path / "data"), catalogue, object(), embedder=object())

    with catalogue.connect() as connection:
        after = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
    assert after == before


def test_annotations_add_singleton_fields_without_mutating_rows(tmp_path: Path):
    class Settings:
        data_dir = tmp_path / "data"

    rows = [
        {"id": 2, "path": "/photos/two.jpg", "size": 2, "mtime_ns": 20, "media_type": "photo"},
        {"id": 1, "path": "/photos/one.jpg", "size": 1, "mtime_ns": 10, "media_type": "photo"},
    ]
    manager = SimilarityManager(Settings(), object(), object(), embedder=object())

    annotated = manager.annotate_and_sort(rows, "near")

    assert [row["id"] for row in annotated] == [2, 1]
    assert all(row["similarity_group"] is None for row in annotated)
    assert all(row["similarity_size"] == 1 for row in annotated)
    assert all("similarity_group" not in row for row in rows)


def test_analysis_groups_real_near_duplicates_and_excludes_video(tmp_path: Path):
    first = make_image(tmp_path / "first.jpg", color=(50, 100, 160))
    original_bytes = first.read_bytes()
    exact = tmp_path / "exact.jpg"
    shutil.copyfile(first, exact)
    recompressed = make_image(tmp_path / "recompressed.jpg", color=(50, 100, 160), quality=65)
    distinct = make_image(tmp_path / "distinct.jpg", color=(180, 40, 20))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    rows = [
        row_for(first, 1),
        row_for(exact, 2),
        row_for(recompressed, 3),
        row_for(distinct, 4),
        row_for(video, 5, media_type="video"),
    ]
    database = FakeDatabase(rows)
    embedder = MappingEmbedder(
        {
            "first.jpg": [1, 0],
            "exact.jpg": [1, 0],
            "recompressed.jpg": [1, 0],
            "distinct.jpg": [0, 1],
        }
    )
    manager = SimilarityManager(FakeSettings(tmp_path / "data"), database, FakeMedia(), embedder)

    assert manager.start() is True
    status = wait_for_manager(manager)

    assert status["total"] == 4
    assert status["processed"] == 4
    assert status["analyzed"] == 4
    assert status["errors"] == []
    assert status["groups"]["near"] == 1
    assert database.calls == [{"media_type": "photo", "limit": 1_000_000_000}]
    assert len(embedder.calls) == 1
    assert len(embedder.calls[0]) == 4

    annotated = manager.annotate_and_sort([rows[3], rows[0], rows[1], rows[2], rows[4]], "near")
    assert [row["id"] for row in annotated] == [1, 2, 3, 4, 5]
    assert [row["similarity_group"] for row in annotated] == ["near:1", "near:1", "near:1", None, None]
    assert [row["similarity_size"] for row in annotated] == [3, 3, 3, 1, 1]
    assert first.read_bytes() == original_bytes
    assert first.is_file() and recompressed.is_file() and distinct.is_file()


def test_embeddings_are_batched_in_bounded_chunks(tmp_path: Path):
    seed = make_image(tmp_path / "seed.jpg", color=(50, 100, 160))
    paths = [seed]
    for index in range(1, 10):
        copy = tmp_path / f"copy-{index}.jpg"
        shutil.copyfile(seed, copy)
        paths.append(copy)
    rows = [row_for(path, index) for index, path in enumerate(paths, start=1)]
    embedder = MappingEmbedder({path.name: [1, 0] for path in paths})
    manager = SimilarityManager(
        FakeSettings(tmp_path / "data"), FakeDatabase(rows), FakeMedia(), embedder
    )

    assert manager.start() is True
    assert wait_for_manager(manager)["errors"] == []

    assert [len(batch) for batch in embedder.calls] == [8, 2]


def test_content_groups_use_representative_direct_matches_and_visible_subset_sizes(tmp_path: Path):
    paths = [
        make_image(tmp_path / "a.jpg", color=(40, 80, 120)),
        make_image(tmp_path / "b.jpg", color=(45, 85, 125)),
        make_image(tmp_path / "c.jpg", color=(50, 90, 130)),
        make_image(tmp_path / "d.jpg", color=(200, 100, 30)),
    ]
    rows = [row_for(path, index) for index, path in enumerate(paths, start=1)]
    embedder = MappingEmbedder(
        {
            "a.jpg": [1.0, 0.0],
            "b.jpg": [0.85, 0.5267827],
            "c.jpg": [0.50, 0.8660254],
            "d.jpg": [0.0, -1.0],
        }
    )
    manager = SimilarityManager(
        FakeSettings(tmp_path / "data"), FakeDatabase(rows), FakeMedia(), embedder
    )

    assert manager.start() is True
    status = wait_for_manager(manager)

    assert status["groups"]["content"] == 1
    annotated = manager.annotate_and_sort([rows[2], rows[3], rows[1], rows[0]], "content")
    assert [row["id"] for row in annotated] == [2, 1, 3, 4]
    assert [row["similarity_group"] for row in annotated] == ["content:1", "content:1", None, None]
    assert [row["similarity_size"] for row in annotated] == [2, 2, 1, 1]

    subset = manager.annotate_and_sort([rows[2], rows[0]], "content")
    assert [row["id"] for row in subset] == [3, 1]
    assert [row["similarity_group"] for row in subset] == [None, None]
    assert [row["similarity_size"] for row in subset] == [1, 1]
    visible_group = manager.annotate_and_sort([rows[1], rows[0]], "content")
    assert [row["similarity_group"] for row in visible_group] == ["content:1", "content:1"]
    assert [row["similarity_size"] for row in visible_group] == [2, 2]


def test_cache_reuse_and_persistence_reload_avoid_reembedding(tmp_path: Path):
    first = make_image(tmp_path / "first.jpg", color=(50, 100, 160))
    second = make_image(tmp_path / "second.jpg", color=(52, 102, 162))
    rows = [row_for(first, 1), row_for(second, 2)]
    data_dir = tmp_path / "data"

    first_embedder = MappingEmbedder({"first.jpg": [1, 0], "second.jpg": [1, 0]})
    manager = SimilarityManager(FakeSettings(data_dir), FakeDatabase(rows), FakeMedia(), first_embedder)
    assert manager.start() is True
    assert wait_for_manager(manager)["errors"] == []
    with sqlite3.connect(manager.database_path) as connection:
        cached = connection.execute(
            "SELECT path, size, mtime_ns, phash_version, embedding_version, color, embedding "
            "FROM similarity_features WHERE photo_id = 1"
        ).fetchone()
    assert cached is not None
    assert cached[0] == str(first)
    assert cached[1] == first.stat().st_size
    assert cached[2] == first.stat().st_mtime_ns
    assert cached[3] == PHASH_VERSION
    assert cached[4].endswith("9299")
    assert len(cached[5]) == 96 * 4
    assert len(cached[6]) == 2 * 4

    class MustNotEmbed:
        def embed(self, paths: list[Path]):
            raise AssertionError(f"unexpected embedding call: {paths}")

    reloaded = SimilarityManager(FakeSettings(data_dir), FakeDatabase(rows), FakeMedia(), MustNotEmbed())
    assert reloaded.snapshot()["analyzed"] == 2
    assert reloaded.snapshot()["groups"]["content"] == 1
    assert reloaded.start() is True
    status = wait_for_manager(reloaded)

    assert status["errors"] == []
    assert status["cached"] == 2
    assert status["analyzed"] == 2
    assert reloaded.annotate_and_sort(rows, "content")[0]["similarity_group"] == "content:1"


def test_errors_are_isolated_and_stale_signatures_become_singletons(tmp_path: Path):
    first = make_image(tmp_path / "first.jpg", color=(50, 100, 160))
    second = make_image(tmp_path / "second.jpg", color=(50, 100, 160))
    broken = make_image(tmp_path / "broken.jpg", color=(200, 20, 20))
    rows = [row_for(first, 1), row_for(second, 2), row_for(broken, 3)]
    manager = SimilarityManager(
        FakeSettings(tmp_path / "data"),
        FakeDatabase(rows),
        FakeMedia({"broken.jpg"}),
        MappingEmbedder({"first.jpg": [1, 0], "second.jpg": [1, 0]}),
    )

    assert manager.start() is True
    status = wait_for_manager(manager)

    assert status["total"] == 3
    assert status["processed"] == 3
    assert status["analyzed"] == 2
    assert len(status["errors"]) == 1
    assert status["errors"][0].startswith("broken.jpg: ")
    assert manager.annotate_and_sort(rows, "content")[0]["similarity_group"] == "content:1"

    stale = dict(rows[0])
    stale["mtime_ns"] += 1
    visible = manager.annotate_and_sort([stale, rows[1]], "content")
    assert [row["id"] for row in visible] == [1, 2]
    assert [row["similarity_group"] for row in visible] == [None, None]
    assert [row["similarity_size"] for row in visible] == [1, 1]


def test_cancelled_analysis_is_single_flight_and_preserves_previous_assignments(tmp_path: Path):
    first = make_image(tmp_path / "first.jpg", color=(50, 100, 160))
    second = make_image(tmp_path / "second.jpg", color=(50, 100, 160))
    third = make_image(tmp_path / "third.jpg", color=(180, 40, 20))
    rows = [row_for(first, 1), row_for(second, 2)]
    data_dir = tmp_path / "data"

    baseline = SimilarityManager(
        FakeSettings(data_dir),
        FakeDatabase(rows),
        FakeMedia(),
        MappingEmbedder({"first.jpg": [1, 0], "second.jpg": [1, 0]}),
    )
    assert baseline.start() is True
    assert wait_for_manager(baseline)["errors"] == []

    rows.append(row_for(third, 3))
    blocker = BlockingEmbedder({"third.jpg": [0, 1]})
    database = FakeDatabase(rows)
    manager = SimilarityManager(FakeSettings(data_dir), database, FakeMedia(), blocker)

    assert manager.start() is True
    assert manager.start() is False
    assert blocker.started.wait(timeout=5)
    manager.cancel()
    blocker.release.set()
    status = wait_for_manager(manager)

    assert status["cancelled"] is True
    assert status["running"] is False
    old = manager.annotate_and_sort(rows[:2], "content")
    assert [row["similarity_group"] for row in old] == ["content:1", "content:1"]
    assert manager.annotate_and_sort(rows, "content")[-1]["similarity_group"] is None


def test_top_level_failure_is_reported_and_worker_always_stops(tmp_path: Path):
    class FailingDatabase:
        def list_photos(self, **kwargs):
            raise RuntimeError("catalogue unavailable")

    manager = SimilarityManager(
        FakeSettings(tmp_path / "data"), FailingDatabase(), FakeMedia(), MappingEmbedder({})
    )

    assert manager.start() is True
    status = wait_for_manager(manager)

    assert status["running"] is False
    assert status["errors"] == ["analysis: catalogue unavailable"]
