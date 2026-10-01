"""Schema migrations driven by ``PRAGMA user_version``."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from project_planner.engine.errors import Conflict

__all__ = ["LATEST_VERSION", "migrate", "schema_version"]

_SCHEMA_V1 = Path(__file__).with_name("schema.sql")


def _v1() -> str:
    return _SCHEMA_V1.read_text(encoding="utf-8")


# version -> function returning the SQL script that upgrades from version - 1.
_MIGRATIONS: dict[int, Callable[[], str]] = {1: _v1}
LATEST_VERSION = max(_MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    """The database's ``user_version``."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database to :data:`LATEST_VERSION`; idempotent. Returns the version.

    Each step runs in its own transaction together with its ``user_version`` bump.

    Raises:
        Conflict: if the database is newer than this code.
    """
    current = schema_version(conn)
    if current > LATEST_VERSION:
        raise Conflict(f"database schema v{current} is newer than supported v{LATEST_VERSION}")
    for version in range(current + 1, LATEST_VERSION + 1):
        script = _MIGRATIONS[version]()
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)
