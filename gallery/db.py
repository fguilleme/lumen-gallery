from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator


SORT_SQL = {
    "date_desc": "date DESC, name COLLATE NOCASE ASC, id ASC",
    "date_asc": "date ASC, name COLLATE NOCASE ASC, id ASC",
    "rating_desc": "rating DESC, date DESC, id ASC",
    "rating_asc": "rating ASC, date ASC, id ASC",
    "name_asc": "name COLLATE NOCASE ASC, id ASC",
    "iso_asc": "(iso IS NULL) ASC, iso ASC, name COLLATE NOCASE ASC, id ASC",
    "iso_desc": "(iso IS NULL) ASC, iso DESC, name COLLATE NOCASE ASC, id ASC",
    "aperture_asc": "(aperture IS NULL) ASC, aperture ASC, name COLLATE NOCASE ASC, id ASC",
    "focal_asc": "(focal IS NULL) ASC, focal ASC, name COLLATE NOCASE ASC, id ASC",
}


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS photos (
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
                    duration REAL,
                    rating INTEGER NOT NULL DEFAULT 0 CHECK (rating BETWEEN 0 AND 4)
                );
                CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(date);
                CREATE INDEX IF NOT EXISTS idx_photos_camera ON photos(camera);
                CREATE INDEX IF NOT EXISTS idx_photos_lens ON photos(lens);
                CREATE INDEX IF NOT EXISTS idx_photos_format ON photos(format);
                CREATE TABLE IF NOT EXISTS upload_names (
                    stored_path TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(photos)").fetchall()
            }
            if "media_type" not in columns:
                connection.execute(
                    "ALTER TABLE photos ADD COLUMN media_type TEXT NOT NULL DEFAULT 'photo'"
                )
            if "duration" not in columns:
                connection.execute("ALTER TABLE photos ADD COLUMN duration REAL")
            if "rating" not in columns:
                connection.execute(
                    "ALTER TABLE photos ADD COLUMN rating INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (rating BETWEEN 0 AND 4)"
                )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_photos_media_type ON photos(media_type)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_photos_rating ON photos(rating)")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_photo(self, photo_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._row(connection.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone())

    def get_photo_by_path(self, path: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._row(connection.execute("SELECT * FROM photos WHERE path = ?", (path,)).fetchone())

    def update_rating(self, photo_id: int, rating: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE photos SET rating = ? WHERE id = ?",
                (rating, photo_id),
            )
            if cursor.rowcount != 1:
                return None
            return self._row(
                connection.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
            )

    def register_upload(self, stored_path: str, original_name: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO upload_names(stored_path, original_name) VALUES (?, ?)
                ON CONFLICT(stored_path) DO UPDATE SET original_name = excluded.original_name
                """,
                (stored_path, original_name),
            )

    def original_name_for(self, stored_path: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT original_name FROM upload_names WHERE stored_path = ?", (stored_path,)
            ).fetchone()
        return str(row[0]) if row else None

    def upsert_photo(self, photo: dict[str, Any]) -> int:
        fields = (
            photo["path"],
            photo["root_path"],
            photo["relative_path"],
            photo["name"],
            photo["format"],
            photo["date"],
            photo["camera"],
            photo["lens"],
            photo["iso"],
            photo["aperture"],
            photo["shutter"],
            photo["focal"],
            photo["width"],
            photo["height"],
            photo["size"],
            photo["mtime_ns"],
            json.dumps(photo["exif"], ensure_ascii=False, separators=(",", ":")),
            photo.get("media_type", "photo"),
            photo.get("duration"),
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO photos(
                    path, root_path, relative_path, name, format, date, camera, lens,
                    iso, aperture, shutter, focal, width, height, size, mtime_ns, exif_json,
                    media_type, duration
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    root_path = excluded.root_path,
                    relative_path = excluded.relative_path,
                    name = excluded.name,
                    format = excluded.format,
                    date = excluded.date,
                    camera = excluded.camera,
                    lens = excluded.lens,
                    iso = excluded.iso,
                    aperture = excluded.aperture,
                    shutter = excluded.shutter,
                    focal = excluded.focal,
                    width = excluded.width,
                    height = excluded.height,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    exif_json = excluded.exif_json,
                    media_type = excluded.media_type,
                    duration = excluded.duration
                """,
                fields,
            )
            row = connection.execute("SELECT id FROM photos WHERE path = ?", (photo["path"],)).fetchone()
        assert row is not None
        return int(row[0])

    def delete_path(self, path: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM photos WHERE path = ?", (path,))

    def reconcile(self, current_paths: Iterable[str]) -> None:
        paths = set(current_paths)
        with self.connect() as connection:
            existing = [str(row[0]) for row in connection.execute("SELECT path FROM photos")]
            stale = [path for path in existing if path not in paths]
            if stale:
                connection.executemany("DELETE FROM photos WHERE path = ?", ((path,) for path in stale))
            connection.execute(
                "DELETE FROM upload_names WHERE stored_path NOT IN (SELECT path FROM photos)"
            )

    @staticmethod
    def _where(
        *,
        q: str | None = None,
        camera: str | None = None,
        lens: str | None = None,
        format: str | None = None,
        media_type: str | None = None,
        imported_only: bool = False,
        date_from: str | None = None,
        date_to: str | None = None,
        iso_min: float | None = None,
        iso_max: float | None = None,
        rating: int | None = None,
        rating_min: int | None = None,
    ) -> tuple[list[str], list[Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if q:
            query = f"%{q.lower()}%"
            clauses.append(
                "(LOWER(name) LIKE ? OR LOWER(relative_path) LIKE ? OR LOWER(camera) LIKE ? OR LOWER(lens) LIKE ?)"
            )
            params.extend([query, query, query, query])
        if camera:
            clauses.append("camera = ?")
            params.append(camera)
        if lens:
            clauses.append("lens = ?")
            params.append(lens)
        if format:
            clauses.append("format = ?")
            params.append(format.upper())
        if media_type:
            clauses.append("media_type = ?")
            params.append(media_type)
        if imported_only:
            clauses.append(
                "EXISTS (SELECT 1 FROM upload_names WHERE upload_names.stored_path = photos.path)"
            )
        if date_from:
            clauses.append("substr(date, 1, 10) >= ?")
            params.append(date_from[:10])
        if date_to:
            clauses.append("substr(date, 1, 10) <= ?")
            params.append(date_to[:10])
        if iso_min is not None:
            clauses.append("iso >= ?")
            params.append(iso_min)
        if iso_max is not None:
            clauses.append("iso <= ?")
            params.append(iso_max)
        if rating is not None:
            clauses.append("rating = ?")
            params.append(rating)
        if rating_min is not None:
            clauses.append("rating >= ?")
            params.append(rating_min)
        return clauses, params

    def list_photos(
        self,
        *,
        q: str | None = None,
        camera: str | None = None,
        lens: str | None = None,
        format: str | None = None,
        media_type: str | None = None,
        imported_only: bool = False,
        date_from: str | None = None,
        date_to: str | None = None,
        iso_min: float | None = None,
        iso_max: float | None = None,
        rating: int | None = None,
        rating_min: int | None = None,
        sort: str = "date_desc",
        offset: int = 0,
        limit: int = 60,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses, params = self._where(
            q=q,
            camera=camera,
            lens=lens,
            format=format,
            media_type=media_type,
            imported_only=imported_only,
            date_from=date_from,
            date_to=date_to,
            iso_min=iso_min,
            iso_max=iso_max,
            rating=rating,
            rating_min=rating_min,
        )
        where = " AND ".join(clauses)
        order = SORT_SQL[sort]
        with self.connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM photos WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows], total

    def facets(self) -> dict[str, list[str]]:
        with self.connect() as connection:
            result: dict[str, list[str]] = {}
            output_keys = {"camera": "cameras", "lens": "lenses", "format": "formats"}
            for key in ("camera", "lens", "format"):
                result[output_keys[key]] = [
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT DISTINCT {key} FROM photos WHERE {key} IS NOT NULL AND {key} != '' ORDER BY {key} COLLATE NOCASE"
                    )
                ]
        return result
