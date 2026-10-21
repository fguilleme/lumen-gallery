from __future__ import annotations

import os
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Iterator

from .config import Settings
from .db import Database
from .exif import SUPPORTED_EXTENSIONS, ExifTool, inspect_file


DEFAULT_SCAN_WORKERS = 4
MIN_SCAN_WORKERS = 1
MAX_SCAN_WORKERS = 8


def _scan_worker_count() -> int:
    raw_value = os.environ.get("PHOTO_SCAN_WORKERS")
    if raw_value is None:
        return DEFAULT_SCAN_WORKERS
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_SCAN_WORKERS
    return max(MIN_SCAN_WORKERS, min(MAX_SCAN_WORKERS, value))


class ScanProgress:
    def __init__(self, on_total: Callable[[int], None], on_processed: Callable[[], None], on_error: Callable[[str], None]):
        self.on_total = on_total
        self.on_processed = on_processed
        self.on_error = on_error


class PhotoScanner:
    def __init__(self, settings: Settings, database: Database, exiftool: ExifTool, progress: ScanProgress):
        self.settings = settings
        self.database = database
        self.exiftool = exiftool
        self.progress = progress

    @staticmethod
    def _inside(path: Path, root: Path) -> Path | None:
        try:
            resolved_root = root.resolve(strict=False)
            resolved_path = path.resolve(strict=True)
            resolved_path.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved_path

    def discover(self) -> Iterator[tuple[Path, Path]]:
        seen: set[str] = set()
        for configured_root in self.settings.roots:
            root = configured_root.resolve(strict=False)
            if not root.is_dir():
                continue
            for directory, directories, filenames in os.walk(root, followlinks=False):
                current = Path(directory)
                directories[:] = [
                    child
                    for child in directories
                    if self._inside(current / child, root) is not None
                ]
                for filename in filenames:
                    candidate = current / filename
                    if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    resolved = self._inside(candidate, root)
                    if resolved is None or not resolved.is_file():
                        continue
                    key = str(resolved)
                    if key in seen:
                        continue
                    seen.add(key)
                    yield resolved, root

    def _scan_one(self, path: Path, root: Path) -> str | None:
        key = str(path)
        try:
            stat = path.stat()
            existing = self.database.get_photo_by_path(key)
            if existing and int(existing["size"]) == stat.st_size and int(existing["mtime_ns"]) == stat.st_mtime_ns:
                return None
            display_name = self.database.original_name_for(key) or path.name
            inspected = inspect_file(path, self.exiftool, name=display_name)
            inspected.update(
                {
                    "path": key,
                    "root_path": str(root),
                    "relative_path": path.relative_to(root).as_posix(),
                }
            )
            self.database.upsert_photo(inspected)
            return None
        except Exception as exc:  # a broken photo must not abort the rest of the scan
            try:
                self.database.delete_path(key)
            except Exception:
                pass
            message = str(exc).strip() or exc.__class__.__name__
            return f"{path.name}: {message}"
        finally:
            self.progress.on_processed()

    def scan(self) -> None:
        candidates = list(self.discover())
        self.progress.on_total(len(candidates))
        current_paths = {str(path) for path, _root in candidates}
        candidate_iterator = iter(candidates)
        worker_count = _scan_worker_count()

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            pending = {}
            for _ in range(worker_count):
                try:
                    path, root = next(candidate_iterator)
                except StopIteration:
                    break
                pending[executor.submit(self._scan_one, path, root)] = path

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    path = pending.pop(future)
                    try:
                        error = future.result()
                    except Exception as exc:  # preserve isolation if a worker escapes its guard
                        message = str(exc).strip() or exc.__class__.__name__
                        error = f"{path.name}: {message}"
                    if error:
                        self.progress.on_error(error)
                    try:
                        next_path, next_root = next(candidate_iterator)
                    except StopIteration:
                        continue
                    pending[executor.submit(self._scan_one, next_path, next_root)] = next_path

        self.database.reconcile(current_paths)


class ScanManager:
    def __init__(self, settings: Settings, database: Database, exiftool: ExifTool):
        self.settings = settings
        self.database = database
        self.exiftool = exiftool
        self._lock = threading.RLock()
        self._status: dict[str, object] = {
            "running": False,
            "total": 0,
            "processed": 0,
            "errors": [],
            "photo_root": str(settings.photo_root),
        }

    def _set_total(self, total: int) -> None:
        with self._lock:
            self._status["total"] = total

    def _processed(self) -> None:
        with self._lock:
            self._status["processed"] = int(self._status["processed"]) + 1

    def _error(self, message: str) -> None:
        with self._lock:
            errors = self._status["errors"]
            assert isinstance(errors, list)
            errors.append(message)

    def _run(self) -> None:
        progress = ScanProgress(self._set_total, self._processed, self._error)
        try:
            PhotoScanner(self.settings, self.database, self.exiftool, progress).scan()
        except Exception as exc:  # status remains observable if a scan-wide failure occurs
            self._error(str(exc).strip() or exc.__class__.__name__)
        finally:
            with self._lock:
                self._status["running"] = False

    def start(self) -> bool:
        with self._lock:
            if bool(self._status["running"]):
                return True
            self._status.update({"running": True, "total": 0, "processed": 0, "errors": []})
            thread = threading.Thread(target=self._run, name="lumen-gallery-scan", daemon=True)
            thread.start()
        return True

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "running": bool(self._status["running"]),
                "total": int(self._status["total"]),
                "processed": int(self._status["processed"]),
                "errors": list(self._status["errors"]),
                "photo_root": str(self._status["photo_root"]),
            }
