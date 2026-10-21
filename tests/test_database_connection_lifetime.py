from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from gallery.db import Database


def _live_sqlite_fds(database_path: Path) -> int:
    root = str(database_path.resolve())
    count = 0
    for entry in os.scandir("/proc/self/fd"):
        try:
            target = os.readlink(entry.path)
        except OSError:
            continue
        if target == root or target.startswith(f"{root}-"):
            count += 1
    return count


def test_connect_closes_connection_after_context(tmp_path: Path):
    database = Database(tmp_path / "gallery.sqlite3")

    with database.connect() as connection:
        handle = connection
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        handle.execute("SELECT 1")


def test_connect_rolls_back_and_closes_connection_on_exception(tmp_path: Path):
    database = Database(tmp_path / "gallery.sqlite3")
    with database.connect() as connection:
        connection.execute("CREATE TABLE transaction_probe (value INTEGER NOT NULL)")

    with pytest.raises(RuntimeError, match="rollback probe"):
        with database.connect() as connection:
            handle = connection
            connection.execute("INSERT INTO transaction_probe(value) VALUES (1)")
            raise RuntimeError("rollback probe")

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM transaction_probe").fetchone()[0] == 0

    with pytest.raises(sqlite3.ProgrammingError):
        handle.execute("SELECT 1")


@pytest.mark.skipif(sys.platform != "linux", reason="/proc/self/fd is Linux-specific")
def test_repeated_read_write_does_not_grow_live_sqlite_fds(tmp_path: Path):
    database_path = tmp_path / "gallery.sqlite3"
    database = Database(database_path)
    with database.connect() as connection:
        connection.execute("CREATE TABLE fd_probe (value INTEGER NOT NULL)")

    baseline = _live_sqlite_fds(database_path)
    for value in range(32):
        with database.connect() as connection:
            connection.execute("INSERT INTO fd_probe(value) VALUES (?)", (value,))
            assert connection.execute("SELECT COUNT(*) FROM fd_probe").fetchone()[0] == value + 1

    assert _live_sqlite_fds(database_path) == baseline
