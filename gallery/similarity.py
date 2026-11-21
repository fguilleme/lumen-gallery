from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
from PIL import Image


# These constants are deliberately local so constructing a manager does not load
# onnxruntime or the model. The parent embeddings module owns the same pinned
# model and exposes the complete version string at analysis time.
MODEL_SHA256 = "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"
CLIP_MODEL_VERSION = f"clip-vit-base-patch32-int8:{MODEL_SHA256}"
MODEL_VERSION = CLIP_MODEL_VERSION
PHASH_ALGORITHM = "phash-dct"
PHASH_VERSION = "32x32-low8-exclude-dc-median-v1"
COLOR_ALGORITHM = "rgb-histogram"
COLOR_VERSION = "rgb-32bins-per-channel-v1"
EMBEDDING_ALGORITHM = "clip-vit-base-patch32"
EMBEDDING_BATCH_SIZE = 8
PREP_WORKERS = 4
VECTOR_CHUNK_SIZE = 256

_VIDEO_SUFFIXES = {
    ".3gp",
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
    ".wmv",
}


@dataclass(frozen=True)
class _Feature:
    photo_id: int
    path: str
    size: int
    mtime_ns: int
    aspect_ratio: float
    phash: bytes
    color: np.ndarray
    embedding: np.ndarray


class SimilarityManager:
    """Persisted, local photo similarity analysis.

    The two clustering modes use deterministic leader clustering rather than
    connected components: a candidate joins a leader only when it directly
    matches that representative. This intentionally prevents an A~B~C bridge
    from turning a non-matching C into a member of A's group.
    """

    def __init__(self, settings: Any, database: Any, media: Any, embedder: Any = None):
        self.settings = settings
        self.database = database
        self.catalogue = database
        self.media = media
        self.embedder = embedder
        self.database_path = Path(settings.data_dir) / "similarity.sqlite3"
        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._embedding_version = CLIP_MODEL_VERSION
        self._snapshot: dict[str, Any] = {
            "running": False,
            "total": 0,
            "processed": 0,
            "cached": 0,
            "errors": [],
            "cancelled": False,
            "updated_at": None,
            "groups": {"near": 0, "content": 0},
            "analyzed": 0,
        }
        self.initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create only the manager's separate SQLite database and its tables."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS similarity_features (
                    photo_id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    phash_algorithm TEXT NOT NULL,
                    phash_version TEXT NOT NULL,
                    color_algorithm TEXT NOT NULL,
                    color_version TEXT NOT NULL,
                    embedding_algorithm TEXT NOT NULL,
                    embedding_version TEXT NOT NULL,
                    aspect_ratio REAL NOT NULL,
                    phash BLOB NOT NULL,
                    color BLOB NOT NULL,
                    embedding BLOB NOT NULL,
                    embedding_dim INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS similarity_groups (
                    mode TEXT NOT NULL CHECK (mode IN ('near', 'content')),
                    photo_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    group_token TEXT NOT NULL,
                    PRIMARY KEY (mode, photo_id)
                );
                CREATE INDEX IF NOT EXISTS idx_similarity_groups_token
                    ON similarity_groups(mode, group_token);
                """
            )
        self._restore_persisted_status()

    def _restore_persisted_status(self) -> None:
        grouped = {"near": 0, "content": 0}
        with self._connect() as connection:
            feature_summary = connection.execute(
                "SELECT COUNT(*) AS count, MAX(updated_at) AS updated_at FROM similarity_features"
            ).fetchone()
            analyzed = int(feature_summary["count"])
            updated_at = feature_summary["updated_at"]
            for row in connection.execute(
                """
                SELECT mode, group_token, COUNT(*) AS members
                FROM similarity_groups
                GROUP BY mode, group_token
                """
            ).fetchall():
                if int(row["members"]) >= 2 and str(row["mode"]) in grouped:
                    grouped[str(row["mode"])] += 1
        with self._lock:
            self._snapshot["analyzed"] = analyzed
            self._snapshot["groups"] = grouped
            if updated_at:
                self._snapshot["updated_at"] = str(updated_at)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _touch_locked(self) -> None:
        self._snapshot["updated_at"] = self._now()

    def _record_error(self, message: str) -> None:
        with self._lock:
            errors = self._snapshot["errors"]
            errors.append(message)
            self._touch_locked()

    @staticmethod
    def _exception_message(exc: BaseException) -> str:
        message = str(exc).strip()
        return message or exc.__class__.__name__

    @staticmethod
    def _row_basename(row: dict[str, Any]) -> str:
        name = row.get("name")
        if isinstance(name, str) and name:
            return Path(name).name
        path = row.get("path")
        if path:
            return Path(str(path)).name
        return str(row.get("id", "photo"))

    def _record_item_error(self, row: dict[str, Any], exc: BaseException) -> None:
        self._record_error(f"{self._row_basename(row)}: {self._exception_message(exc)}")
        self._mark_processed(success=False)

    def _mark_processed(self, *, success: bool, cached: bool = False) -> None:
        with self._lock:
            self._snapshot["processed"] += 1
            if cached:
                self._snapshot["cached"] += 1
            if success:
                self._snapshot["analyzed"] += 1
            self._touch_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": bool(self._snapshot["running"]),
                "total": int(self._snapshot["total"]),
                "processed": int(self._snapshot["processed"]),
                "cached": int(self._snapshot["cached"]),
                "errors": list(self._snapshot["errors"]),
                "cancelled": bool(self._snapshot["cancelled"]),
                "updated_at": self._snapshot["updated_at"],
                "groups": dict(self._snapshot["groups"]),
                "analyzed": int(self._snapshot["analyzed"]),
            }

    def start(self) -> bool:
        """Start one background analysis; return False while one is already running."""
        with self._lock:
            if self._snapshot["running"]:
                return False
            self._cancel_event.clear()
            self._snapshot.update(
                {
                    "running": True,
                    "total": 0,
                    "processed": 0,
                    "cached": 0,
                    "analyzed": 0,
                    "errors": [],
                    "cancelled": False,
                }
            )
            self._touch_locked()
            self._thread = threading.Thread(
                target=self._run,
                name="lumen-gallery-similarity",
                daemon=True,
            )
            self._thread.start()
            return True

    def cancel(self) -> None:
        with self._lock:
            if self._snapshot["running"]:
                self._cancel_event.set()
                self._snapshot["cancelled"] = True
                self._touch_locked()

    def _run(self) -> None:
        try:
            self._analyze()
        except Exception as exc:  # keep worker failures observable in status
            self._record_error(f"analysis: {self._exception_message(exc)}")
        finally:
            with self._lock:
                if self._cancel_event.is_set():
                    self._snapshot["cancelled"] = True
                self._snapshot["running"] = False
                self._touch_locked()

    @staticmethod
    def _is_trashed(row: dict[str, Any]) -> bool:
        for key in ("is_trashed", "trashed"):
            value = row.get(key)
            if isinstance(value, str):
                is_set = value.strip().lower() not in {"", "0", "false", "no", "none", "null"}
            else:
                is_set = bool(value)
            if is_set:
                return True
        for key in ("deletion_status", "status"):
            if str(row.get(key) or "").lower() == "trashed":
                return True
        return False

    @classmethod
    def _eligible(cls, row: dict[str, Any]) -> bool:
        if cls._is_trashed(row):
            return False
        if str(row.get("media_type") or "photo").lower() != "photo":
            return False
        path = str(row.get("path") or "")
        return Path(path).suffix.lower() not in _VIDEO_SUFFIXES

    def _catalogue_rows(self) -> list[dict[str, Any]]:
        result = self.database.list_photos(media_type="photo", limit=1_000_000_000)
        rows = result[0] if isinstance(result, tuple) else result
        return [dict(row) for row in rows if self._eligible(dict(row))]

    @staticmethod
    def _signature(row: dict[str, Any]) -> tuple[int, str, int, int]:
        photo_id = int(row["id"])
        path = str(row["path"])
        try:
            size = int(row["size"])
        except (KeyError, TypeError, ValueError):
            size = 0
        try:
            mtime_ns = int(row["mtime_ns"])
        except (KeyError, TypeError, ValueError):
            mtime_ns = 0
        return photo_id, path, size, mtime_ns

    @staticmethod
    def _dct_matrix(size: int = 32) -> np.ndarray:
        indices = np.arange(size, dtype=np.float64)
        frequencies = indices[:, None]
        matrix = np.cos(np.pi / size * (indices + 0.5) * frequencies)
        matrix[0, :] *= np.sqrt(1.0 / size)
        matrix[1:, :] *= np.sqrt(2.0 / size)
        return matrix

    @classmethod
    def _phash(cls, image: Image.Image) -> bytes:
        gray = np.asarray(image.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
        matrix = cls._dct_matrix()
        coefficients = matrix @ gray @ matrix.T
        low_frequency = coefficients[:8, :8].reshape(-1)[1:]
        median = float(np.median(low_frequency))
        bits = low_frequency > median
        return np.packbits(bits.astype(np.uint8), bitorder="big").tobytes()

    @staticmethod
    def _color_descriptor(image: Image.Image) -> np.ndarray:
        rgb = np.asarray(image.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR), dtype=np.uint8)
        histograms = [
            np.histogram(rgb[:, :, channel], bins=32, range=(0, 256))[0].astype(np.float32)
            for channel in range(3)
        ]
        descriptor = np.concatenate(histograms).astype(np.float32)
        norm = float(np.linalg.norm(descriptor))
        if norm < 1e-12:
            return np.zeros_like(descriptor)
        return descriptor / norm

    @staticmethod
    def _cosine(left: np.ndarray, right: np.ndarray) -> float:
        left_norm = float(np.linalg.norm(left))
        right_norm = float(np.linalg.norm(right))
        if left_norm < 1e-12 or right_norm < 1e-12:
            return 0.0
        return float(np.dot(left, right) / (left_norm * right_norm))

    @staticmethod
    def _hamming(left: bytes, right: bytes) -> int:
        left_bits = np.unpackbits(np.frombuffer(left, dtype=np.uint8), bitorder="big")[:63]
        right_bits = np.unpackbits(np.frombuffer(right, dtype=np.uint8), bitorder="big")[:63]
        if left_bits.shape != right_bits.shape:
            return 64
        return int(np.count_nonzero(left_bits != right_bits))

    def _image_features(self, row: dict[str, Any]) -> tuple[Path, bytes, np.ndarray, float]:
        preview = self.media.jpeg("thumb", row)
        image_path = Path(preview)
        with Image.open(image_path) as opened:
            opened.load()
            image = opened.convert("RGB")
            width, height = image.size
            if width <= 0 or height <= 0:
                raise ValueError("image has no dimensions")
            try:
                metadata_width = float(row.get("width") or 0)
                metadata_height = float(row.get("height") or 0)
            except (TypeError, ValueError):
                metadata_width = metadata_height = 0.0
            aspect_ratio = (
                metadata_width / metadata_height
                if metadata_width > 0 and metadata_height > 0
                else float(width) / float(height)
            )
            return image_path, self._phash(image), self._color_descriptor(image), aspect_ratio

    def _get_embedder(self) -> Any:
        if self.embedder is None:
            from .embeddings import ClipEmbedder, MODEL_VERSION as EMBEDDINGS_MODEL_VERSION

            self._embedding_version = str(EMBEDDINGS_MODEL_VERSION)
            model_path = Path(self.settings.data_dir) / "models" / "clip-vit-base-patch32" / "vision_model_quantized.onnx"
            self.embedder = ClipEmbedder(model_path)
        return self.embedder

    @staticmethod
    def _normalise_embedding_output(values: Any, expected: int) -> np.ndarray:
        array = np.asarray(values, dtype=np.float32)
        if expected == 1 and array.ndim == 1:
            array = array.reshape(1, -1)
        if array.ndim != 2 or array.shape[0] != expected or array.shape[1] < 1:
            raise ValueError(f"embedder returned shape {array.shape}, expected ({expected}, D)")
        if not np.isfinite(array).all():
            raise ValueError("embedder returned non-finite values")
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        if np.any(norms < 1e-12):
            raise ValueError("embedder returned a zero vector")
        return np.asarray(array / norms, dtype=np.float32)

    def _feature_from_parts(
        self,
        row: dict[str, Any],
        phash: bytes,
        color: np.ndarray,
        aspect_ratio: float,
        embedding: np.ndarray,
    ) -> _Feature:
        photo_id, path, size, mtime_ns = self._signature(row)
        return _Feature(
            photo_id=photo_id,
            path=path,
            size=size,
            mtime_ns=mtime_ns,
            aspect_ratio=aspect_ratio,
            phash=bytes(phash),
            color=np.asarray(color, dtype=np.float32).copy(),
            embedding=np.asarray(embedding, dtype=np.float32).copy(),
        )

    def _load_cached(self, row: dict[str, Any]) -> _Feature | None:
        photo_id, path, size, mtime_ns = self._signature(row)
        with self._connect() as connection:
            cached = connection.execute(
                "SELECT * FROM similarity_features WHERE photo_id = ?",
                (photo_id,),
            ).fetchone()
        if cached is None:
            return None
        if (
            str(cached["path"]) != path
            or int(cached["size"]) != size
            or int(cached["mtime_ns"]) != mtime_ns
            or str(cached["phash_algorithm"]) != PHASH_ALGORITHM
            or str(cached["phash_version"]) != PHASH_VERSION
            or str(cached["color_algorithm"]) != COLOR_ALGORITHM
            or str(cached["color_version"]) != COLOR_VERSION
            or str(cached["embedding_algorithm"]) != EMBEDDING_ALGORITHM
            or str(cached["embedding_version"]) != self._embedding_version
        ):
            return None
        try:
            phash = bytes(cached["phash"])
            color = np.frombuffer(bytes(cached["color"]), dtype=np.float32).copy()
            embedding = np.frombuffer(bytes(cached["embedding"]), dtype=np.float32).copy()
            embedding_dim = int(cached["embedding_dim"])
            if embedding_dim != embedding.size or embedding_dim < 1 or color.size != 96:
                return None
            if not np.isfinite(color).all() or not np.isfinite(embedding).all():
                return None
            if float(np.linalg.norm(embedding)) < 1e-12:
                return None
            embedding /= np.linalg.norm(embedding)
            return _Feature(
                photo_id=photo_id,
                path=path,
                size=size,
                mtime_ns=mtime_ns,
                aspect_ratio=float(cached["aspect_ratio"]),
                phash=phash,
                color=color,
                embedding=embedding,
            )
        except (TypeError, ValueError, OverflowError):
            return None

    def _save_feature(self, feature: _Feature) -> None:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO similarity_features(
                    photo_id, path, size, mtime_ns,
                    phash_algorithm, phash_version,
                    color_algorithm, color_version,
                    embedding_algorithm, embedding_version,
                    aspect_ratio, phash, color, embedding, embedding_dim, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(photo_id) DO UPDATE SET
                    path = excluded.path,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    phash_algorithm = excluded.phash_algorithm,
                    phash_version = excluded.phash_version,
                    color_algorithm = excluded.color_algorithm,
                    color_version = excluded.color_version,
                    embedding_algorithm = excluded.embedding_algorithm,
                    embedding_version = excluded.embedding_version,
                    aspect_ratio = excluded.aspect_ratio,
                    phash = excluded.phash,
                    color = excluded.color,
                    embedding = excluded.embedding,
                    embedding_dim = excluded.embedding_dim,
                    updated_at = excluded.updated_at
                """,
                (
                    feature.photo_id,
                    feature.path,
                    feature.size,
                    feature.mtime_ns,
                    PHASH_ALGORITHM,
                    PHASH_VERSION,
                    COLOR_ALGORITHM,
                    COLOR_VERSION,
                    EMBEDDING_ALGORITHM,
                    self._embedding_version,
                    feature.aspect_ratio,
                    sqlite3.Binary(feature.phash),
                    sqlite3.Binary(np.asarray(feature.color, dtype=np.float32).tobytes()),
                    sqlite3.Binary(np.asarray(feature.embedding, dtype=np.float32).tobytes()),
                    int(feature.embedding.size),
                    now,
                ),
            )

    def _add_embedded_feature(
        self,
        row: dict[str, Any],
        image_parts: tuple[Path, bytes, np.ndarray, float],
        embedding: np.ndarray,
        features: list[_Feature],
    ) -> None:
        _path, phash, color, aspect_ratio = image_parts
        feature = self._feature_from_parts(row, phash, color, aspect_ratio, embedding)
        self._save_feature(feature)
        features.append(feature)
        self._mark_processed(success=True)

    def _embed_batch(
        self,
        batch: list[tuple[dict[str, Any], tuple[Path, bytes, np.ndarray, float]]],
        features: list[_Feature],
    ) -> None:
        if not batch:
            return
        embedder = self._get_embedder()
        paths = [parts[0] for _row, parts in batch]
        try:
            values = self._normalise_embedding_output(embedder.embed(paths), len(batch))
        except Exception:
            # A single bad source should not discard a whole batch. Retry one
            # path at a time so valid photos still complete and are reported.
            for item in batch:
                if self._cancel_event.is_set():
                    return
                row, parts = item
                try:
                    single = self._normalise_embedding_output(embedder.embed([parts[0]]), 1)
                    self._add_embedded_feature(row, parts, single[0], features)
                except Exception as exc:
                    self._record_item_error(row, exc)
            return
        for (row, parts), embedding in zip(batch, values, strict=True):
            self._add_embedded_feature(row, parts, embedding, features)

    @staticmethod
    def _near_match(left: _Feature, right: _Feature) -> bool:
        if abs(left.aspect_ratio - right.aspect_ratio) / max(left.aspect_ratio, right.aspect_ratio, 1e-12) > 0.15:
            return False
        return (
            SimilarityManager._hamming(left.phash, right.phash) <= 10
            and SimilarityManager._cosine(left.color, right.color) >= 0.90
        )

    @staticmethod
    def _content_match(left: _Feature, right: _Feature) -> bool:
        if left.embedding.shape != right.embedding.shape:
            return False
        return float(np.dot(left.embedding, right.embedding)) >= 0.82

    @staticmethod
    def _cluster(
        features: Iterable[_Feature],
        mode: str,
        matches: Callable[[_Feature, _Feature], bool],
    ) -> tuple[dict[int, str], int]:
        ordered = sorted(features, key=lambda item: item.photo_id)
        if mode == "content" and ordered and len({item.embedding.shape for item in ordered}) == 1:
            embeddings = np.stack([item.embedding for item in ordered])
            return SimilarityManager._cluster_vectorized(
                ordered,
                mode,
                lambda leader_positions, candidate_index: np.concatenate(
                    [
                        (
                            embeddings[
                                np.asarray(
                                    leader_positions[start : start + VECTOR_CHUNK_SIZE],
                                    dtype=np.intp,
                                )
                            ]
                            @ embeddings[candidate_index]
                            >= 0.82
                        )
                        for start in range(0, len(leader_positions), VECTOR_CHUNK_SIZE)
                    ]
                ),
            )
        if mode == "near" and ordered and len({item.color.shape for item in ordered}) == 1 and len({len(item.phash) for item in ordered}) == 1:
            colors = np.stack([item.color for item in ordered])
            color_norms = np.linalg.norm(colors, axis=1)
            phashes = np.stack(
                [
                    np.unpackbits(
                        np.frombuffer(item.phash, dtype=np.uint8), bitorder="big"
                    )[:63]
                    for item in ordered
                ]
            )
            aspects = np.asarray([item.aspect_ratio for item in ordered], dtype=np.float64)

            def near_matches(leader_positions: list[int], candidate_index: int) -> np.ndarray:
                candidate_bits = phashes[candidate_index]
                candidate_color = colors[candidate_index]
                candidate_norm = color_norms[candidate_index]
                chunks: list[np.ndarray] = []
                for start in range(0, len(leader_positions), VECTOR_CHUNK_SIZE):
                    positions = np.asarray(
                        leader_positions[start : start + VECTOR_CHUNK_SIZE], dtype=np.intp
                    )
                    hamming = np.count_nonzero(phashes[positions] != candidate_bits, axis=1)
                    denominator = color_norms[positions] * candidate_norm
                    cosine = np.divide(
                        colors[positions] @ candidate_color,
                        denominator,
                        out=np.zeros_like(denominator, dtype=np.float64),
                        where=denominator >= 1e-12,
                    )
                    aspect_delta = np.abs(aspects[positions] - aspects[candidate_index]) / np.maximum(
                        aspects[positions], aspects[candidate_index]
                    )
                    chunks.append((hamming <= 10) & (cosine >= 0.90) & (aspect_delta <= 0.15))
                return np.concatenate(chunks) if chunks else np.empty(0, dtype=bool)

            return SimilarityManager._cluster_vectorized(ordered, mode, near_matches)

        return SimilarityManager._cluster_scalar(ordered, mode, matches)

    @staticmethod
    def _cluster_scalar(
        ordered: list[_Feature],
        mode: str,
        matches: Callable[[_Feature, _Feature], bool],
    ) -> tuple[dict[int, str], int]:
        assignments: dict[int, str] = {}
        leaders: list[tuple[_Feature, str, int]] = []
        for feature in ordered:
            for leader, token, count in leaders:
                if matches(leader, feature):
                    assignments[feature.photo_id] = token
                    index = next(index for index, item in enumerate(leaders) if item[1] == token)
                    leaders[index] = (leader, token, count + 1)
                    break
            else:
                token = f"{mode}:{feature.photo_id}"
                assignments[feature.photo_id] = token
                leaders.append((feature, token, 1))
        return assignments, sum(1 for _leader, _token, count in leaders if count >= 2)

    @staticmethod
    def _cluster_vectorized(
        ordered: list[_Feature],
        mode: str,
        match_chunks: Callable[[list[int], int], np.ndarray],
    ) -> tuple[dict[int, str], int]:
        assignments: dict[int, str] = {}
        leaders: list[tuple[_Feature, str, int]] = []
        leader_positions: list[int] = []
        for candidate_index, feature in enumerate(ordered):
            if leader_positions:
                matches = match_chunks(leader_positions, candidate_index)
                matching = np.flatnonzero(matches)
                if matching.size:
                    leader_index = int(matching[0])
                    leader, token, count = leaders[leader_index]
                    assignments[feature.photo_id] = token
                    leaders[leader_index] = (leader, token, count + 1)
                    continue
            token = f"{mode}:{feature.photo_id}"
            assignments[feature.photo_id] = token
            leaders.append((feature, token, 1))
            leader_positions.append(candidate_index)
        return assignments, sum(1 for _leader, _token, count in leaders if count >= 2)

    def _publish(
        self,
        features: list[_Feature],
        near_assignments: dict[int, str],
        content_assignments: dict[int, str],
    ) -> None:
        by_id = {feature.photo_id: feature for feature in features}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for mode, assignments in (
                ("near", near_assignments),
                ("content", content_assignments),
            ):
                connection.execute("DELETE FROM similarity_groups WHERE mode = ?", (mode,))
                connection.executemany(
                    """
                    INSERT INTO similarity_groups(
                        mode, photo_id, path, size, mtime_ns, group_token
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            mode,
                            photo_id,
                            by_id[photo_id].path,
                            by_id[photo_id].size,
                            by_id[photo_id].mtime_ns,
                            token,
                        )
                        for photo_id, token in assignments.items()
                    ],
                )

    def _analyze(self) -> None:
        rows = self._catalogue_rows()
        with self._lock:
            self._snapshot["total"] = len(rows)
            self._touch_locked()

        features: list[_Feature] = []
        pending_rows: list[dict[str, Any]] = []
        for row in rows:
            if self._cancel_event.is_set():
                return
            cached = self._load_cached(row)
            if cached is not None:
                features.append(cached)
                self._mark_processed(success=True, cached=True)
                continue
            pending_rows.append(row)

        # RAW preview extraction is usually the expensive part. Keep it
        # bounded, and finish each preparation batch before running the CPU
        # bounded CLIP batch so the two pools never oversubscribe the host.
        with ThreadPoolExecutor(max_workers=PREP_WORKERS, thread_name_prefix="lumen-similarity-prep") as executor:
            for start in range(0, len(pending_rows), EMBEDDING_BATCH_SIZE):
                if self._cancel_event.is_set():
                    return
                rows_batch = pending_rows[start : start + EMBEDDING_BATCH_SIZE]
                futures = [(row, executor.submit(self._image_features, row)) for row in rows_batch]
                pending: list[tuple[dict[str, Any], tuple[Path, bytes, np.ndarray, float]]] = []
                for row, future in futures:
                    try:
                        pending.append((row, future.result()))
                    except Exception as exc:
                        self._record_item_error(row, exc)
                if self._cancel_event.is_set():
                    return
                self._embed_batch(pending, features)
                if self._cancel_event.is_set():
                    return

        if self._cancel_event.is_set():
            return

        near_assignments, near_count = self._cluster(features, "near", self._near_match)
        content_assignments, content_count = self._cluster(features, "content", self._content_match)
        if self._cancel_event.is_set():
            return
        self._publish(features, near_assignments, content_assignments)
        with self._lock:
            self._snapshot["groups"] = {"near": near_count, "content": content_count}
            self._snapshot["analyzed"] = len(features)
            self._touch_locked()

    def _assignment_rows(self, mode: str, photo_ids: list[int]) -> dict[int, sqlite3.Row]:
        if not photo_ids:
            return {}
        output: dict[int, sqlite3.Row] = {}
        with self._connect() as connection:
            for start in range(0, len(photo_ids), 800):
                chunk = photo_ids[start : start + 800]
                placeholders = ", ".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT g.*, f.photo_id AS feature_photo_id
                    FROM similarity_groups AS g
                    JOIN similarity_features AS f
                      ON f.photo_id = g.photo_id
                     AND f.path = g.path
                     AND f.size = g.size
                     AND f.mtime_ns = g.mtime_ns
                    WHERE g.mode = ?
                      AND f.phash_algorithm = ?
                      AND f.phash_version = ?
                      AND f.color_algorithm = ?
                      AND f.color_version = ?
                      AND f.embedding_algorithm = ?
                      AND f.embedding_version = ?
                      AND g.photo_id IN ({placeholders})
                    """,
                    [
                        mode,
                        PHASH_ALGORITHM,
                        PHASH_VERSION,
                        COLOR_ALGORITHM,
                        COLOR_VERSION,
                        EMBEDDING_ALGORITHM,
                        self._embedding_version,
                        *chunk,
                    ],
                ).fetchall()
                output.update({int(row["photo_id"]): row for row in rows})
        return output

    def annotate_and_sort(self, rows: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
        if mode not in {"near", "content"}:
            raise ValueError("mode must be 'near' or 'content'")
        if not rows:
            return []
        copied = [dict(row) for row in rows]
        ids = []
        for row in copied:
            try:
                ids.append(int(row["id"]))
            except (KeyError, TypeError, ValueError):
                ids.append(-1)
        assignments = self._assignment_rows(mode, ids)
        tokens: list[str | None] = []
        for row in copied:
            try:
                row_id = int(row["id"])
            except (KeyError, TypeError, ValueError):
                row_id = -1
            assignment = assignments.get(row_id)
            current_signature = self._signature(row) if row_id != -1 and "path" in row else None
            if (
                assignment is None
                or not self._eligible(row)
                or current_signature is None
                or str(assignment["path"]) != current_signature[1]
                or int(assignment["size"]) != current_signature[2]
                or int(assignment["mtime_ns"]) != current_signature[3]
                or str(assignment["group_token"]).split(":", 1)[0] != mode
            ):
                tokens.append(None)
            else:
                tokens.append(str(assignment["group_token"]))

        visible_counts: dict[str, int] = {}
        for token in tokens:
            if token is not None:
                visible_counts[token] = visible_counts.get(token, 0) + 1

        grouped: dict[str, list[dict[str, Any]]] = {}
        singles: list[dict[str, Any]] = []
        for row, token in zip(copied, tokens, strict=True):
            if token is not None and visible_counts[token] >= 2:
                row["similarity_group"] = token
                row["similarity_size"] = visible_counts[token]
                grouped.setdefault(token, []).append(row)
            else:
                row["similarity_group"] = None
                row["similarity_size"] = 1
                singles.append(row)

        ordered: list[dict[str, Any]] = []
        for token in sorted(grouped, key=lambda value: (-len(grouped[value]), value)):
            ordered.extend(grouped[token])
        ordered.extend(singles)
        return ordered
