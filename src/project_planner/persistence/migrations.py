"""Schema migrations driven by ``PRAGMA user_version``.

* v1 - ``schema.sql`` (kept verbatim forever: it is the v1 DDL that older databases
  were created with, and new databases are built by applying v1 then every later step).
* v2 - rebuilds ``projects`` with ``INTEGER PRIMARY KEY AUTOINCREMENT`` so a deleted
  saved project's id is never reused, and adds the results generation columns
  ``results_gen`` / ``based_on_results_gen`` (unsaved-changes tracking for stored
  results). Data, primary keys and every foreign key referring to ``projects`` are
  preserved (SQLite's documented 12-step table rebuild, see :func:`migrate`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from project_planner.engine.errors import Conflict

__all__ = ["LATEST_VERSION", "migrate", "schema_version"]

_SCHEMA_V1 = Path(__file__).with_name("schema.sql")


def _v1() -> str:
    return _SCHEMA_V1.read_text(encoding="utf-8")


_PROJECT_COLUMNS = (
    "pk, kind, name, project_id, project_name, start, currency, cost_report_unit, "
    "revision, based_on_pk, based_on_revision, saved_at"
)

_V2 = f"""
CREATE TABLE projects_v2 (
    pk                    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                  TEXT NOT NULL CHECK (kind IN ('saved', 'workspace')),
    name                  TEXT NOT NULL,
    project_id            TEXT NOT NULL,
    project_name          TEXT NOT NULL,
    start                 TEXT NOT NULL,
    currency              TEXT NOT NULL,
    cost_report_unit      TEXT NOT NULL,
    revision              INTEGER NOT NULL DEFAULT 1,
    based_on_pk           INTEGER NULL,
    based_on_revision     INTEGER NULL,
    saved_at              TEXT NULL,
    results_gen           INTEGER NOT NULL DEFAULT 0,
    based_on_results_gen  INTEGER NOT NULL DEFAULT 0
);
INSERT INTO projects_v2({_PROJECT_COLUMNS}) SELECT {_PROJECT_COLUMNS} FROM projects ORDER BY pk;
DROP TABLE projects;
ALTER TABLE projects_v2 RENAME TO projects;
CREATE UNIQUE INDEX ux_projects_saved_name ON projects(name) WHERE kind = 'saved';
CREATE UNIQUE INDEX ux_projects_one_workspace ON projects(kind) WHERE kind = 'workspace';
"""


def _v2() -> str:
    return _V2


@dataclass(frozen=True)
class _Step:
    script: str
    rebuilds_tables: bool = False  # needs foreign keys off + foreign_key_check


# version -> step that upgrades from version - 1.
_MIGRATIONS: dict[int, _Step] = {
    1: _Step(_v1()),
    2: _Step(_v2(), rebuilds_tables=True),
}
LATEST_VERSION = max(_MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    """The database's ``user_version``."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _run_step(conn: sqlite3.Connection, version: int, step: _Step) -> None:
    """One step in one transaction together with its ``user_version`` bump.

    For table rebuilds foreign key enforcement is switched off *outside* the
    transaction (``PRAGMA foreign_keys`` is a no-op inside one), so dropping the old
    ``projects`` table does not cascade into its children; ``PRAGMA foreign_key_check``
    must come back empty before the commit, and enforcement is restored afterwards.
    """
    fk_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if step.rebuilds_tables and fk_on:
        conn.execute("PRAGMA foreign_keys = OFF")
    try:
        # executescript leaves the transaction open (no COMMIT in the script).
        conn.executescript(f"BEGIN IMMEDIATE;\n{step.script}\nPRAGMA user_version = {version};")
        if step.rebuilds_tables:
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise Conflict(
                    f"schema migration to v{version} would break {len(broken)} foreign key(s)"
                )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        if step.rebuilds_tables and fk_on:
            conn.execute("PRAGMA foreign_keys = ON")


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database to :data:`LATEST_VERSION`; idempotent. Returns the version.

    Each step runs in its own transaction together with its ``user_version`` bump, so
    a failed step leaves the database at the previous version.

    Raises:
        Conflict: if the database is newer than this code.
    """
    current = schema_version(conn)
    if current > LATEST_VERSION:
        raise Conflict(f"database schema v{current} is newer than supported v{LATEST_VERSION}")
    for version in range(current + 1, LATEST_VERSION + 1):
        _run_step(conn, version, _MIGRATIONS[version])
    return schema_version(conn)
