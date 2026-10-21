from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database


MAX_BATCH = 100


class WorkerFailure(RuntimeError):
    """A recoverable safety check or filesystem failure."""


def add_calendar_month(value: datetime) -> datetime:
    """Return *value* one calendar month later, clamped to month end."""
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return current.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return _utc_now(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _lexical(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _roots(settings: Settings) -> tuple[Path, ...]:
    return tuple(_lexical(root.resolve(strict=False)) for root in settings.roots)


def _inside_without_symlink(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    cursor = root
    if cursor.is_symlink():
        return False
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            return False
    return True


def _source_path(settings: Settings, raw_path: str, *, require_exists: bool) -> Path:
    candidate = _lexical(raw_path)
    if not any(_inside_without_symlink(candidate, root) for root in _roots(settings)):
        raise WorkerFailure("source is outside configured roots")

    if os.path.lexists(candidate):
        if candidate.is_symlink():
            raise WorkerFailure("source is a symlink")
        try:
            resolved = candidate.resolve(strict=True)
            if resolved != candidate or not candidate.is_file():
                raise WorkerFailure("source is not a regular file")
        except (OSError, RuntimeError) as exc:
            raise WorkerFailure("source cannot be resolved safely") from exc
    elif require_exists:
        raise WorkerFailure("source is missing")
    return candidate


def _trash_root(settings: Settings) -> Path:
    configured = getattr(settings, "trash_dir", None)
    root = _lexical(configured or (settings.data_dir / "trash"))
    if root.is_symlink():
        raise WorkerFailure("trash root is a symlink")
    return root


def _trash_path(settings: Settings, raw_path: str) -> Path:
    root = _trash_root(settings)
    candidate = _lexical(raw_path)
    if candidate == root or not _inside_without_symlink(candidate, root):
        raise WorkerFailure("trash path is outside the trash root")
    return candidate


def _safe_basename(name: str, fallback: str) -> str:
    value = Path(name or fallback).name
    value = "".join(char if char not in {"\x00", "\r", "\n"} else "_" for char in value)
    if value in {"", ".", ".."}:
        return "unnamed"
    return value


def _allocate_trash_path(settings: Settings, original_name: str, source: Path) -> Path:
    root = _trash_root(settings)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkerFailure("trash root cannot be created") from exc
    if root.is_symlink():
        raise WorkerFailure("trash root is a symlink")
    for _ in range(10):
        directory = root / str(uuid.uuid4())
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise WorkerFailure("unique trash directory cannot be created") from exc
        destination = directory / _safe_basename(original_name, source.name)
        if os.path.lexists(destination):
            raise WorkerFailure("trash destination already exists")
        return destination
    raise WorkerFailure("could not allocate a unique trash directory")


def _manifest_path(trash_path: Path) -> Path:
    return trash_path.parent / "manifest.json"


def _safe_manifest_path(settings: Settings, trash_path: Path) -> Path:
    candidate = _lexical(_manifest_path(trash_path))
    if not _inside_without_symlink(candidate, _trash_root(settings)) or candidate.is_symlink():
        raise WorkerFailure("recovery manifest path is unsafe")
    return candidate


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise WorkerFailure("recovery manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise WorkerFailure("recovery manifest is not an object")
    return value


def _manifest_matches(manifest: dict[str, Any], job: dict[str, Any], source: Path, trash: Path) -> bool:
    try:
        return (
            int(manifest["job_id"]) == int(job["id"])
            and int(manifest["media_id"]) == int(job["media_id"])
            and str(manifest["source"]) == str(source)
            and str(manifest["trash"]) == str(trash)
            and int(manifest["source_size"]) == int(job["source_size"])
            and int(manifest["source_mtime_ns"]) == int(job["source_mtime_ns"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _write_manifest(
    settings: Settings,
    job: dict[str, Any],
    source: Path,
    trash: Path,
    now: datetime,
) -> None:
    manifest_path = _safe_manifest_path(settings, trash)
    payload = {
        "job_id": int(job["id"]),
        "media_id": int(job["media_id"]),
        "source": str(source),
        "trash": str(trash),
        "time": _utc_iso(now),
        "source_size": int(job["source_size"]),
        "source_mtime_ns": int(job["source_mtime_ns"]),
    }
    if manifest_path.exists():
        if not _manifest_matches(_read_manifest(manifest_path), job, source, trash):
            raise WorkerFailure("recovery manifest does not match job")
        return
    temporary = manifest_path.parent / f".manifest-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
    except (OSError, TypeError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        raise WorkerFailure("could not write recovery manifest") from exc


def _find_manifest_for_job(settings: Settings, job: dict[str, Any], source: Path) -> Path | None:
    root = _trash_root(settings)
    if not root.is_dir() or root.is_symlink():
        return None
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise WorkerFailure("trash root cannot be read") from exc
    for directory in entries:
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            manifest_path = _safe_manifest_path(settings, directory / "placeholder")
        except WorkerFailure:
            continue
        if not manifest_path.is_file():
            continue
        try:
            manifest = _read_manifest(manifest_path)
        except WorkerFailure:
            continue
        try:
            if int(manifest.get("job_id")) != int(job["id"]):
                continue
            trash = _trash_path(settings, str(manifest["trash"]))
        except (KeyError, TypeError, ValueError, WorkerFailure):
            continue
        if _manifest_matches(manifest, job, source, trash):
            return trash
    return None


def _mark_error(connection: Any, job_id: int, message: str) -> None:
    connection.execute(
        "UPDATE deletion_jobs SET status = 'error', error = ? WHERE id = ? AND status = 'scheduled'",
        (message[:1000], job_id),
    )


def _mark_cancelled(connection: Any, job_id: int) -> None:
    connection.execute(
        "UPDATE deletion_jobs SET status = 'cancelled', error = NULL WHERE id = ? AND status = 'scheduled'",
        (job_id,),
    )


def _process_locked(
    settings: Settings,
    connection: Any,
    job: dict[str, Any],
    now: datetime,
) -> str:
    current_rating = job.get("current_rating")
    if current_rating is not None and int(current_rating) != 0:
        _mark_cancelled(connection, int(job["id"]))
        return "cancelled"

    source = _source_path(
        settings,
        str(job.get("path_key") or job["source_path"]),
        require_exists=False,
    )
    source_present = os.path.lexists(source)
    if source_present and current_rating is None:
        raise WorkerFailure("catalogue record is missing; zero rating cannot be verified")
    raw_trash = job.get("trash_path")
    if source_present and not raw_trash:
        try:
            stat = source.stat()
        except OSError as exc:
            raise WorkerFailure("source cannot be stat'ed") from exc
        if stat.st_size != int(job["source_size"]):
            raise WorkerFailure("source size changed")
        if stat.st_mtime_ns != int(job["source_mtime_ns"]):
            raise WorkerFailure("source mtime changed")
    if raw_trash:
        trash = _trash_path(settings, str(raw_trash))
    elif source_present:
        trash = _allocate_trash_path(settings, str(job["original_name"]), source)
    else:
        recovered = _find_manifest_for_job(settings, job, source)
        if recovered is None:
            raise WorkerFailure("source and recovery trash are missing")
        trash = recovered

    destination_present = os.path.lexists(trash)
    if source_present and destination_present:
        raise WorkerFailure("source and trash both exist; refusing overwrite")

    if not source_present:
        if not destination_present or not trash.is_file():
            raise WorkerFailure("source and recovery trash are missing")
        manifest_path = _safe_manifest_path(settings, trash)
        if not manifest_path.is_file():
            raise WorkerFailure("recovery manifest is missing")
        if not _manifest_matches(_read_manifest(manifest_path), job, source, trash):
            raise WorkerFailure("recovery manifest does not match job")
        connection.execute(
            """
            UPDATE deletion_jobs
            SET status = 'trashed', trash_path = ?, error = NULL
            WHERE id = ? AND status = 'scheduled'
            """,
            (str(trash), int(job["id"])),
        )
        return "recovered"

    try:
        stat = source.stat()
    except OSError as exc:
        raise WorkerFailure("source cannot be stat'ed") from exc
    if stat.st_size != int(job["source_size"]):
        raise WorkerFailure("source size changed")
    if stat.st_mtime_ns != int(job["source_mtime_ns"]):
        raise WorkerFailure("source mtime changed")

    _write_manifest(settings, job, source, trash, now)
    connection.execute(
        "UPDATE deletion_jobs SET trash_path = ? WHERE id = ? AND status = 'scheduled'",
        (str(trash), int(job["id"])),
    )
    try:
        os.rename(source, trash)
    except OSError as exc:
        raise WorkerFailure(f"source move failed: {exc.__class__.__name__}") from exc
    connection.execute(
        """
        UPDATE deletion_jobs
        SET status = 'trashed', error = NULL
        WHERE id = ? AND status = 'scheduled'
        """,
        (int(job["id"]),),
    )
    return "moved"


def _dry_check(settings: Settings, job: dict[str, Any]) -> None:
    source = _source_path(settings, str(job.get("path_key") or job["source_path"]), require_exists=False)
    source_present = os.path.lexists(source)
    if job.get("trash_path"):
        trash = _trash_path(settings, str(job["trash_path"]))
    elif source_present:
        trash = None
    else:
        trash = _find_manifest_for_job(settings, job, source)
        if trash is None:
            raise WorkerFailure("source and recovery trash are missing")
    if source_present and trash is not None and os.path.lexists(trash):
        raise WorkerFailure("source and trash both exist; refusing overwrite")
    if not source_present:
        if trash is None or not trash.is_file():
            raise WorkerFailure("source and recovery trash are missing")
        manifest_path = _safe_manifest_path(settings, trash)
        if not manifest_path.is_file():
            raise WorkerFailure("recovery manifest is missing")
        if not _manifest_matches(_read_manifest(manifest_path), job, source, trash):
            raise WorkerFailure("recovery manifest does not match job")
        return
    stat = source.stat()
    if stat.st_size != int(job["source_size"]):
        raise WorkerFailure("source size changed")
    if stat.st_mtime_ns != int(job["source_mtime_ns"]):
        raise WorkerFailure("source mtime changed")


def _process_one(settings: Settings, database: Database, job_id: int, now: datetime) -> tuple[str, str | None]:
    try:
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT j.*, COALESCE(p.path, j.path_key) AS source_path, p.rating AS current_rating
                FROM deletion_jobs AS j
                LEFT JOIN photos AS p ON p.id = j.media_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None or str(row["status"]) != "scheduled":
                return "skipped", None
            job = dict(row)
            if str(job["due_at"]) > _utc_iso(now):
                return "skipped", None
            try:
                outcome = _process_locked(settings, connection, job, now)
            except WorkerFailure as exc:
                _mark_error(connection, int(job["id"]), str(exc))
                return "error", str(exc)
            except OSError as exc:
                message = f"filesystem error: {exc.__class__.__name__}"
                _mark_error(connection, int(job["id"]), message)
                return "error", message
            return outcome, None
    except OSError as exc:
        return "error", f"filesystem error: {exc.__class__.__name__}"


def process_due_jobs(
    settings: Settings,
    database: Database,
    *,
    now: datetime | None = None,
    apply: bool = False,
    max_batch: int = MAX_BATCH,
) -> dict[str, Any]:
    current = _utc_now(now)
    jobs = database.due_deletion_jobs(now=current, limit=min(MAX_BATCH, max(1, int(max_batch))))
    result: dict[str, Any] = {
        "checked": len(jobs),
        "moved": 0,
        "recovered": 0,
        "cancelled": 0,
        "skipped": 0,
        "errors": [],
    }
    errors = result["errors"]
    assert isinstance(errors, list)
    for job in jobs:
        job_id = int(job["id"])
        if not apply:
            try:
                _dry_check(settings, job)
                result["skipped"] += 1
            except WorkerFailure as exc:
                errors.append(f"{job_id}: {exc}")
            continue
        outcome, error = _process_one(settings, database, job_id, current)
        if outcome == "moved":
            result["moved"] += 1
        elif outcome == "recovered":
            result["recovered"] += 1
        elif outcome == "cancelled":
            result["cancelled"] += 1
        elif outcome == "skipped":
            result["skipped"] += 1
        elif outcome == "error":
            errors.append(f"{job_id}: {error or 'worker error'}")
    return result


def restore_job(settings: Settings, database: Database, job_id: int) -> dict[str, Any]:
    try:
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT j.*, COALESCE(p.path, j.path_key) AS source_path
                FROM deletion_jobs AS j
                LEFT JOIN photos AS p ON p.id = j.media_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return {"restored": False, "job_id": job_id, "error": "job not found"}
            job = dict(row)
            if str(job["status"]) != "trashed":
                return {"restored": False, "job_id": job_id, "error": "job is not trashed"}
            source = _source_path(
                settings,
                str(job.get("path_key") or job["source_path"]),
                require_exists=False,
            )
            if os.path.lexists(source):
                return {
                    "restored": False,
                    "job_id": job_id,
                    "error": "original destination exists",
                }
            raw_trash = job.get("trash_path")
            if not raw_trash:
                return {"restored": False, "job_id": job_id, "error": "trash path is missing"}
            trash = _trash_path(settings, str(raw_trash))
            if not trash.is_file():
                return {"restored": False, "job_id": job_id, "error": "trash file is missing"}
            manifest_path = _safe_manifest_path(settings, trash)
            if not manifest_path.is_file():
                return {"restored": False, "job_id": job_id, "error": "recovery manifest is missing"}
            if not _manifest_matches(_read_manifest(manifest_path), job, source, trash):
                return {"restored": False, "job_id": job_id, "error": "recovery manifest does not match job"}
            if not source.parent.is_dir():
                return {"restored": False, "job_id": job_id, "error": "original parent is missing"}
            try:
                os.rename(trash, source)
            except OSError as exc:
                return {
                    "restored": False,
                    "job_id": job_id,
                    "error": f"restore failed: {exc.__class__.__name__}",
                }
            connection.execute("UPDATE photos SET rating = 1 WHERE id = ?", (int(job["media_id"]),))
            connection.execute(
                "UPDATE deletion_jobs SET status = 'cancelled', error = NULL WHERE id = ? AND status = 'trashed'",
                (job_id,),
            )
            try:
                manifest_path.unlink(missing_ok=True)
                manifest_path.parent.rmdir()
            except OSError:
                pass
            return {"restored": True, "job_id": job_id}
    except WorkerFailure as exc:
        return {"restored": False, "job_id": job_id, "error": str(exc)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Move due zero-rated gallery media to recoverable trash")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply due moves")
    mode.add_argument("--dry-run", action="store_true", help="check due jobs without moving files (default)")
    parser.add_argument("--restore", type=int, metavar="JOBID", help="restore one trashed job")
    parser.add_argument("--max-batch", type=int, default=MAX_BATCH, help="maximum due jobs to inspect (max 100)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.restore is not None and (args.apply or args.dry_run):
        print("--restore cannot be combined with --apply or --dry-run", file=sys.stderr)
        return 2
    settings = Settings.from_env(Path(__file__).resolve().parents[1])
    database = Database(settings.db_path)
    if args.restore is not None:
        result = restore_job(settings, database, args.restore)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("restored") else 1
    result = process_due_jobs(
        settings,
        database,
        apply=bool(args.apply),
        max_batch=args.max_batch,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MAX_BATCH", "add_calendar_month", "main", "process_due_jobs", "restore_job"]
