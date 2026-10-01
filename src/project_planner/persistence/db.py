"""SQLite connection and transaction helpers.

Connections run in autocommit mode (``isolation_level=None``); every write goes
through :func:`transaction`, which is re-entrant (inner uses become savepoints) and
turns :class:`sqlite3.IntegrityError` into :class:`~project_planner.engine.errors.Conflict`.
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from project_planner.engine.errors import Conflict

__all__ = ["FileLock", "connect", "transaction"]

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


class FileLock:
    """Exclusive, non-blocking OS lock on a sidecar ``<database>.lock`` file (D12).

    One open ``Workspace`` per database file: the lock is taken on a separate file (not
    the database itself, so SQLite's own locking and WAL are untouched) with
    ``msvcrt.locking(LK_NBLCK)`` on Windows and ``fcntl.flock(LOCK_EX | LOCK_NB)``
    elsewhere. Both locks belong to the open file handle, so a second acquisition fails
    at once both from another process *and* from the same process, and the operating
    system drops the lock when the handle is closed or the process ends (also on a
    crash), so a stale ``.lock`` file never blocks anything. The file itself is left in
    place on release (deleting it would race with a concurrent opener).
    """

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self.path = Path(self.database + ".lock")
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock. Raises ``Conflict`` if another workspace holds it."""
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            raise Conflict(
                f"the project database {self.database!r} is already open in "
                f"another workspace (in this or another program); close it there first"
            ) from None
        self._fd = fd

    def release(self) -> None:
        """Release the lock (idempotent)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_fd(fd)
        except OSError:
            pass  # closing the handle releases it anyway
        finally:
            os.close(fd)


if sys.platform == "win32":
    import msvcrt

    def _lock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _reraise(exc: BaseException) -> None:
    if isinstance(exc, sqlite3.IntegrityError):
        raise Conflict(f"database constraint violated: {exc}") from exc
    raise exc
