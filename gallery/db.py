from __future__ import annotations

import base64
import calendar
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, Iterable, Iterator


def _normalise_root(root: PurePath | str) -> str:
    value = str(Path(root))
    return value.rstrip("/") or "/"


def _normalise_parent(parent: PurePath | str) -> str:
    value = PurePosixPath(str(parent)).as_posix()
    return value if value not in {"", "."} else "."


def directory_token(root: PurePath | str, parent: PurePath | str) -> str:
    """Encode a directory as URL-safe base64 JSON ``[root_path, parent]``.

    The token is opaque to the public API, not a secrecy boundary. ``root_path``
    is normalized without a trailing slash and a root-level directory uses
    ``parent == '.'``.
    """
    payload = json.dumps(
        [_normalise_root(root), _normalise_parent(parent)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_directory_token(value: str | None) -> tuple[str, str] | None:
    """Decode and validate a public directory token, returning ``(root, parent)``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(
            base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        )
    except (UnicodeEncodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list) or len(payload) != 2:
        return None
    root, parent = payload
    if not isinstance(root, str) or not isinstance(parent, str) or not root or not parent:
        return None
    normalized_root = _normalise_root(root)
    normalized_parent = _normalise_parent(parent)
    parent_path = PurePosixPath(normalized_parent)
    if (
        root != normalized_root
        or parent != normalized_parent
        or parent_path.is_absolute()
    ):
        return None
    if ".." in parent_path.parts:
        return None
    return normalized_root, normalized_parent


def directory_parent(relative_path: PurePath | str) -> str:
    """Return the POSIX parent directory, using ``'.'`` for root-level files."""
    return _normalise_parent(PurePosixPath(str(relative_path)).parent)


def directory_label(root: PurePath | str, parent: PurePath | str) -> str:
    """Return the public root-basename/relative-parent directory label."""
    root_name = PurePosixPath(_normalise_root(root)).name or _normalise_root(root)
    normalized_parent = _normalise_parent(parent)
    return root_name if normalized_parent == "." else f"{root_name}/{normalized_parent}"


SORT_SQL = {
    "date_desc": "date DESC, name COLLATE NOCASE ASC, id ASC",
    "date_asc": "date ASC, name COLLATE NOCASE ASC, id ASC",
    "rating_desc": "rating DESC, date DESC, id ASC",
    "rating_asc": "rating ASC, date ASC, id ASC",
    "name_asc": "name COLLATE NOCASE ASC, id ASC",
    "directory_asc": (
        "root_path COLLATE NOCASE ASC, "
        "directory_parent(relative_path) COLLATE NOCASE ASC, "
        "name COLLATE NOCASE ASC, id ASC"
    ),
    "directory_desc": (
        "root_path COLLATE NOCASE DESC, "
        "directory_parent(relative_path) COLLATE NOCASE DESC, "
        "name COLLATE NOCASE ASC, id ASC"
    ),
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
            connection.create_function(
                "directory_parent", 1, directory_parent, deterministic=True
            )
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
                CREATE TABLE IF NOT EXISTS deletion_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_id INTEGER NOT NULL,
                    path_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('scheduled', 'cancelled', 'trashed', 'error')),
                    original_name TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    source_size INTEGER NOT NULL,
                    source_mtime_ns INTEGER NOT NULL,
                    trash_path TEXT,
                    error TEXT
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
            deletion_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(deletion_jobs)").fetchall()
            }
            if "path_key" not in deletion_columns:
                connection.execute("ALTER TABLE deletion_jobs ADD COLUMN path_key TEXT")
            connection.execute(
                """
                UPDATE deletion_jobs
                SET path_key = (
                    SELECT path FROM photos WHERE photos.id = deletion_jobs.media_id
                )
                WHERE path_key IS NULL
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_photos_media_type ON photos(media_type)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_photos_rating ON photos(rating)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deletion_jobs_status_due "
                "ON deletion_jobs(status, due_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deletion_jobs_media_status "
                "ON deletion_jobs(media_id, status)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_deletion_jobs_one_scheduled "
                "ON deletion_jobs(media_id) WHERE status = 'scheduled'"
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _photo_select() -> str:
        return """
            SELECT p.*,
                   (
                       SELECT scheduled_at FROM deletion_jobs
                       WHERE media_id = p.id AND status = 'scheduled'
                       ORDER BY id DESC LIMIT 1
                   ) AS deletion_scheduled_at,
                   (
                       SELECT due_at FROM deletion_jobs
                       WHERE media_id = p.id AND status = 'scheduled'
                       ORDER BY id DESC LIMIT 1
                   ) AS delete_after,
                   (
                       SELECT status FROM deletion_jobs
                       WHERE media_id = p.id AND status = 'scheduled'
                       ORDER BY id DESC LIMIT 1
                   ) AS deletion_status,
                   EXISTS(
                       SELECT 1 FROM deletion_jobs
                       WHERE media_id = p.id AND status = 'trashed'
                   ) AS is_trashed
            FROM photos AS p
        """

    @staticmethod
    def _utc_now(value: datetime | None = None) -> datetime:
        current = value or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return current.astimezone(timezone.utc)

    @staticmethod
    def _utc_iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _calendar_month(value: datetime) -> datetime:
        year = value.year + (1 if value.month == 12 else 0)
        month = 1 if value.month == 12 else value.month + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return value.replace(year=year, month=month, day=day)

    def get_photo(self, photo_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._row(
                connection.execute(
                    f"{self._photo_select()} WHERE p.id = ?", (photo_id,)
                ).fetchone()
            )

    def get_photo_by_path(self, path: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._row(
                connection.execute(
                    f"{self._photo_select()} WHERE p.path = ?", (path,)
                ).fetchone()
            )

    def update_rating(self, photo_id: int, rating: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE photos
                SET rating = ?
                WHERE id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM deletion_jobs
                      WHERE media_id = photos.id AND status = 'trashed'
                  )
                """,
                (rating, photo_id),
            )
            if cursor.rowcount != 1:
                return None
            if rating >= 1:
                connection.execute(
                    """
                    UPDATE deletion_jobs
                    SET status = 'cancelled', error = NULL
                    WHERE media_id = ? AND status = 'scheduled'
                    """,
                    (photo_id,),
                )
            return self._row(
                connection.execute(
                    f"{self._photo_select()} WHERE p.id = ?", (photo_id,)
                ).fetchone()
            )

    def schedule_zero(self, *, now: datetime | None = None) -> dict[str, Any]:
        scheduled_at = self._utc_now(now)
        due_at = self._calendar_month(scheduled_at)
        scheduled_iso = self._utc_iso(scheduled_at)
        due_iso = self._utc_iso(due_at)
        scheduled_count = 0
        already_scheduled = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT p.id, p.path, p.name, p.size, p.mtime_ns,
                       EXISTS (
                           SELECT 1 FROM deletion_jobs
                           WHERE media_id = p.id AND status = 'scheduled'
                       ) AS already_scheduled
                FROM photos AS p
                WHERE p.rating = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM deletion_jobs
                      WHERE media_id = p.id AND status = 'trashed'
                  )
                ORDER BY p.id
                """
            ).fetchall()
            for row in rows:
                if bool(row["already_scheduled"]):
                    already_scheduled += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO deletion_jobs(
                        media_id, path_key, status, original_name, scheduled_at, due_at,
                        source_size, source_mtime_ns, trash_path, error
                    ) VALUES (?, ?, 'scheduled', ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        int(row["id"]),
                        str(row["path"]),
                        str(row["name"]),
                        scheduled_iso,
                        due_iso,
                        int(row["size"]),
                        int(row["mtime_ns"]),
                    ),
                )
                scheduled_count += 1
            total_due = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM deletion_jobs AS j
                    JOIN photos AS p ON p.id = j.media_id
                    WHERE j.status = 'scheduled' AND p.rating = 0
                    """
                ).fetchone()[0]
            )
            response_due = due_iso
            if scheduled_count == 0:
                existing_due = connection.execute(
                    """
                    SELECT MIN(j.due_at)
                    FROM deletion_jobs AS j
                    JOIN photos AS p ON p.id = j.media_id
                    WHERE j.status = 'scheduled' AND p.rating = 0
                    """
                ).fetchone()[0]
                if existing_due:
                    response_due = str(existing_due)
        return {
            "scheduled": scheduled_count,
            "already_scheduled": already_scheduled,
            "total_due": total_due,
            "due_at": response_due,
        }

    def get_deletion_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM deletion_jobs WHERE id = ?", (job_id,)
                ).fetchone()
            )

    def get_deletion_job_for_media(self, media_id: int, *, status: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM deletion_jobs WHERE media_id = ?"
        params: list[Any] = [media_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT 1"
        with self.connect() as connection:
            return self._row(connection.execute(query, params).fetchone())

    def cancel_deletion(self, media_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM photos WHERE id = ?", (media_id,)).fetchone() is None:
                return None
            connection.execute(
                """
                UPDATE deletion_jobs
                SET status = 'cancelled', error = NULL
                WHERE media_id = ? AND status = 'scheduled'
                """,
                (media_id,),
            )
            return self._row(
                connection.execute(
                    f"{self._photo_select()} WHERE p.id = ?", (media_id,)
                ).fetchone()
            )

    def deletion_status(self) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(status = 'scheduled'), 0),
                    COALESCE(SUM(status = 'trashed'), 0)
                FROM deletion_jobs
                """
            ).fetchone()
        return {"scheduled": int(row[0]), "trashed": int(row[1])}

    def list_trash_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, media_id, status, original_name, scheduled_at, due_at,
                       trash_path, error
                FROM deletion_jobs
                WHERE status = 'trashed'
                ORDER BY id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def due_deletion_jobs(self, *, now: datetime | None = None, limit: int = 100) -> list[dict[str, Any]]:
        current_iso = self._utc_iso(self._utc_now(now))
        bounded_limit = max(1, min(100, int(limit)))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.*, COALESCE(p.path, j.path_key) AS source_path, p.rating AS current_rating
                FROM deletion_jobs AS j
                LEFT JOIN photos AS p ON p.id = j.media_id
                WHERE j.status = 'scheduled' AND j.due_at <= ?
                ORDER BY j.due_at ASC, j.id ASC
                LIMIT ?
                """,
                (current_iso, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]

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
            _normalise_root(photo["root_path"]),
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
                    media_type, duration, rating
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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
            connection.execute(
                """
                DELETE FROM photos
                WHERE path = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM deletion_jobs
                      WHERE media_id = photos.id AND status IN ('scheduled', 'trashed')
                  )
                """,
                (path,),
            )

    def reconcile(self, current_paths: Iterable[str]) -> None:
        paths = set(current_paths)
        with self.connect() as connection:
            if paths:
                placeholders = ", ".join("?" for _ in paths)
                connection.execute(
                    f"""
                    DELETE FROM photos
                    WHERE path NOT IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM deletion_jobs
                          WHERE media_id = photos.id AND status IN ('scheduled', 'trashed')
                      )
                    """,
                    list(paths),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM photos
                    WHERE NOT EXISTS (
                        SELECT 1 FROM deletion_jobs
                        WHERE media_id = photos.id AND status IN ('scheduled', 'trashed')
                    )
                    """
                )
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
        directory: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        iso_min: float | None = None,
        iso_max: float | None = None,
        rating: int | None = None,
        rating_min: int | None = None,
        deletion_status: str | None = None,
    ) -> tuple[list[str], list[Any]]:
        clauses = [
            "1 = 1",
            "NOT EXISTS (SELECT 1 FROM deletion_jobs WHERE media_id = p.id AND status = 'trashed')",
        ]
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
                "EXISTS (SELECT 1 FROM upload_names WHERE upload_names.stored_path = p.path)"
            )
        if directory is not None:
            decoded = decode_directory_token(directory)
            if decoded is None:
                clauses.append("0 = 1")
            else:
                root, parent = decoded
                parent_expression = "directory_parent(p.relative_path)"
                clauses.append(
                    "(p.root_path = ? AND "
                    f"(? = '.' OR {parent_expression} = ? OR "
                    f"substr({parent_expression}, 1, length(?) + 1) = ? || '/'))"
                )
                params.extend([root, parent, parent, parent, parent])
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
        if deletion_status is not None:
            if deletion_status != "scheduled":
                raise ValueError("invalid deletion status")
            clauses.append(
                "EXISTS (SELECT 1 FROM deletion_jobs WHERE media_id = p.id AND status = 'scheduled')"
            )
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
        directory: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        iso_min: float | None = None,
        iso_max: float | None = None,
        rating: int | None = None,
        rating_min: int | None = None,
        deletion_status: str | None = None,
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
            directory=directory,
            date_from=date_from,
            date_to=date_to,
            iso_min=iso_min,
            iso_max=iso_max,
            rating=rating,
            rating_min=rating_min,
            deletion_status=deletion_status,
        )
        where = " AND ".join(clauses)
        order = SORT_SQL[sort]
        with self.connect() as connection:
            total = int(
                connection.execute(f"SELECT COUNT(*) FROM photos AS p WHERE {where}", params).fetchone()[0]
            )
            rows = connection.execute(
                f"{self._photo_select()} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows], total

    def facets(self) -> dict[str, Any]:
        with self.connect() as connection:
            result: dict[str, Any] = {}
            output_keys = {"camera": "cameras", "lens": "lenses", "format": "formats"}
            for key in ("camera", "lens", "format"):
                result[output_keys[key]] = [
                    str(row[0])
                    for row in connection.execute(
                        f"""
                        SELECT DISTINCT p.{key}
                        FROM photos AS p
                        WHERE p.{key} IS NOT NULL AND p.{key} != ''
                          AND NOT EXISTS (
                              SELECT 1 FROM deletion_jobs
                              WHERE media_id = p.id AND status = 'trashed'
                          )
                        ORDER BY p.{key} COLLATE NOCASE
                        """
                    )
                ]

            directory_counts: dict[tuple[str, str], int] = {}
            rows = connection.execute(
                """
                SELECT p.root_path, directory_parent(p.relative_path) AS parent
                FROM photos AS p
                WHERE NOT EXISTS (
                    SELECT 1 FROM deletion_jobs
                    WHERE media_id = p.id AND status = 'trashed'
                )
                """
            ).fetchall()
            for row in rows:
                root = _normalise_root(str(row["root_path"]))
                parent = _normalise_parent(str(row["parent"]))
                parts = [] if parent == "." else parent.split("/")
                ancestors = ["."] + [
                    "/".join(parts[:index]) for index in range(1, len(parts) + 1)
                ]
                for ancestor in ancestors:
                    key = (root, ancestor)
                    directory_counts[key] = directory_counts.get(key, 0) + 1

            result["directories"] = [
                {
                    "value": directory_token(root, parent),
                    "label": directory_label(root, parent),
                    "count": count,
                }
                for (root, parent), count in sorted(
                    directory_counts.items(),
                    key=lambda item: (
                        item[0][0].casefold(),
                        item[0][0],
                        item[0][1].casefold(),
                        item[0][1],
                    ),
                )
            ]
        return result
