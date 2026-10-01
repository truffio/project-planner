"""SQLite connection and transaction helpers.

Connections run in autocommit mode (``isolation_level=None``); every write goes
through :func:`transaction`, which is re-entrant (inner uses become savepoints) and
turns :class:`sqlite3.IntegrityError` into :class:`~project_planner.engine.errors.Conflict`.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from project_planner.engine.errors import Conflict

__all__ = ["connect", "transaction"]

_savepoint_ids = itertools.count(1)


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection (``":memory:"`` allowed) with foreign keys on and WAL for files.

    No type detection is used: values are stored as TEXT / INTEGER and converted by
    the repositories. The schema is *not* created here; call
    :func:`project_planner.persistence.migrations.migrate`.
    """
    text = str(path)
    conn = sqlite3.connect(
        text, isolation_level=None, detect_types=0, check_same_thread=check_same_thread
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if text != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically: ``BEGIN IMMEDIATE`` ... ``COMMIT``, ``ROLLBACK`` on error.

    Nested use creates a savepoint, so an inner failure undoes only the inner block
    (and propagates). ``sqlite3.IntegrityError`` is re-raised as ``Conflict``.
    """
    if conn.in_transaction:
        name = f"sp{next(_savepoint_ids)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException as exc:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            _reraise(exc)
        else:
            conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException as exc:
        conn.execute("ROLLBACK")
        _reraise(exc)
    else:
        conn.execute("COMMIT")


def _reraise(exc: BaseException) -> None:
    if isinstance(exc, sqlite3.IntegrityError):
        raise Conflict(f"database constraint violated: {exc}") from exc
    raise exc
