"""Project files (plan section 5, task T33): save / save-as / load / list / delete / CSV.

Module-level functions taking the :class:`~services.workspace.Workspace` first;
``Workspace`` delegates to them (``ws.save()`` -> ``files.save(ws)`` ...).

Conventions:

* A saved project's ``id`` is its persistence primary key (an ``int``); ``load`` and
  ``delete_project`` also accept a decimal string.
* The workspace remembers the saved project it was last saved to / loaded from in
  ``projects.based_on_pk``. ``save`` overwrites that copy (keeping its saved name);
  without one it saves under the project's name (``Conflict`` on a name clash).
* Every replacing operation is one SQLite transaction, and CSV text is parsed completely
  before the workspace is touched, so a failure leaves the workspace unchanged.
* A CSV import leaves the workspace *dirty* (it is not saved anywhere): the revision is
  bumped and ``based_on`` is cleared.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from project_planner.engine import csv_io
from project_planner.engine.errors import Conflict, Issue, NotFound, ObjectType
from project_planner.persistence import repositories as repo
from project_planner.persistence import row_ops
from project_planner.persistence.db import transaction
from project_planner.services.workspace import Workspace

__all__ = [
    "ImportSummary",
    "ProjectInfo",
    "delete_project",
    "export_csv",
    "import_csv",
    "list_projects",
    "load",
    "save",
    "save_as",
]


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    """A saved project: ``id`` (persistence pk), library ``name``, ``saved_at`` (ISO text)."""

    id: int
    name: str
    saved_at: str | None


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """What a CSV import created (counts) and what the importer noted."""

    nodes: int
    resources: int
    assignments: int
    dependencies: int
    holidays: int
    exceptions: int
    notes: tuple[Issue, ...]
    defaults_applied: tuple[str, ...]


def _pk(project_id: int | str) -> int:
    if isinstance(project_id, bool):
        raise NotFound(ObjectType.PROJECT.value, str(project_id))
    if isinstance(project_id, int):
        return project_id
    try:
        return int(str(project_id).strip())
    except ValueError:
        raise NotFound(ObjectType.PROJECT.value, str(project_id)) from None


def _saved_info(ws: Workspace, pk: int) -> ProjectInfo:
    record = repo.get_project_record(ws.connection, pk)
    return ProjectInfo(record.pk, record.name, record.saved_at)


def _saved_record(ws: Workspace, project_id: int | str) -> int:
    """pk of an existing *saved* project, else ``NotFound``."""
    pk = _pk(project_id)
    try:
        record = repo.get_project_record(ws.connection, pk)
    except NotFound:
        raise NotFound(ObjectType.PROJECT.value, str(project_id)) from None
    if record.kind != "saved":
        raise NotFound(ObjectType.PROJECT.value, str(project_id))
    return pk


def list_projects(ws: Workspace) -> list[ProjectInfo]:
    """Saved projects, newest first."""
    return [ProjectInfo(pk, name, at) for pk, name, at in repo.list_saved_projects(ws.connection)]


def save(ws: Workspace) -> ProjectInfo:
    """Save the workspace (definition and all results) and mark it clean.

    Overwrites the saved project the workspace was loaded from / saved to; a workspace
    that was never saved is saved under the project's name.

    Raises:
        Conflict: first save and a different saved project already has that name
            (use :func:`save_as`).
    """
    conn = ws.connection
    record = repo.get_project_record(conn, ws.project_pk)
    target = record.based_on_pk
    existing: str | None = None
    if target is not None:
        try:
            target_record = repo.get_project_record(conn, target)
            if target_record.kind == "saved":
                existing = target_record.name
        except NotFound:
            pass
    if existing is not None and target is not None:
        pk = repo.copy_project(conn, ws.project_pk, target, kind="saved", name=existing)
    else:
        name = ws.project().name
        if any(n == name for _, n, _ in repo.list_saved_projects(conn)):
            raise Conflict(
                f"a saved project named {name!r} already exists; use save_as with another name"
            )
        pk = repo.copy_project(conn, ws.project_pk, None, kind="saved", name=name)
    ws.mark_clean(pk)
    return _saved_info(ws, pk)


def save_as(ws: Workspace, name: str) -> ProjectInfo:
    """Save the workspace as a new saved project ``name`` and track it.

    Raises:
        Conflict: a saved project with that name exists.
    """
    if not isinstance(name, str):
        raise TypeError(f"name must be a str, got {type(name).__name__}")
    if not name.strip():
        raise Conflict("a saved project needs a non-empty name")
    pk = repo.copy_project(ws.connection, ws.project_pk, None, kind="saved", name=name)
    ws.mark_clean(pk)
    return _saved_info(ws, pk)


def load(ws: Workspace, project_id: int | str, *, discard_unsaved: bool = False) -> None:
    """Replace the workspace by a saved project, results included, without recalculating.

    Raises:
        UnsavedChanges: the workspace is dirty and ``discard_unsaved`` is false.
        NotFound: unknown saved project (workspace untouched).
    """
    ws.require_clean(discard_unsaved)
    pk = _saved_record(ws, project_id)
    conn = ws.connection
    with transaction(conn):
        name = repo.load_project(conn, pk).name
        repo.copy_project(conn, pk, ws.project_pk, kind="workspace", name=name)
    ws.reload(operation="load")
    ws.mark_clean(pk)


def delete_project(ws: Workspace, project_id: int | str) -> None:
    """Delete a saved project (never the workspace). Raises ``NotFound``."""
    pk = _saved_record(ws, project_id)
    record = repo.get_project_record(ws.connection, ws.project_pk)
    with transaction(ws.connection):
        repo.delete_saved_project(ws.connection, pk)
        if record.based_on_pk == pk:
            # The workspace no longer tracks a saved copy; keep its dirty state.
            row_ops.set_based_on(ws.connection, ws.project_pk, None, record.based_on_revision)


def _read_source(source: str | Path | TextIO) -> str | bytes:
    """Whole CSV input.

    A ``Path`` is read as a file. A ``str`` containing a line break is CSV text itself;
    any other ``str`` is a file path. A stream is read fully.
    """
    if isinstance(source, Path):
        return source.read_bytes()
    if isinstance(source, str):
        if "\n" in source or "\r" in source:
            return source
        return Path(source).read_bytes()
    data = source.read()
    return data


def import_csv(
    ws: Workspace, source: str | Path | TextIO, *, discard_unsaved: bool = False
) -> ImportSummary:
    """Replace the workspace project by the contents of a CSV file.

    The input is parsed completely first; on ``ImportFailed`` nothing changes. On
    success all results are discarded and the workspace is dirty (not saved anywhere).

    Raises:
        UnsavedChanges: the workspace is dirty and ``discard_unsaved`` is false.
        ImportFailed: the file has errors (workspace byte-identical).
    """
    ws.require_clean(discard_unsaved)
    outcome = csv_io.parse(_read_source(source))
    project = outcome.project
    conn = ws.connection
    with transaction(conn):
        repo.save_project(conn, ws.project_pk, project)
        repo.delete_runs(conn, ws.project_pk)
        row_ops.set_name(conn, ws.project_pk, project.name)
        repo.bump_revision(conn, ws.project_pk)  # revision >= 2, so never equal to 1
        row_ops.set_based_on(conn, ws.project_pk, None, 1)
    ws.reload(operation="import_csv")
    cal = project.calendar
    return ImportSummary(
        nodes=len(project.nodes),
        resources=len(project.resources),
        assignments=len(project.assignments),
        dependencies=len(project.dependencies),
        holidays=len(cal.holidays),
        exceptions=len(cal.exceptions),
        notes=outcome.notes,
        defaults_applied=tuple(n.message for n in outcome.notes if n.code == "CSV_DEFAULT_APPLIED"),
    )


def export_csv(ws: Workspace, destination: str | Path | None = None) -> str:
    """CSV text of the project (with RESULT rows when a result exists); optionally written.

    The file is UTF-8 without BOM. Results that no longer match the definition are
    marked stale in the export.
    """
    state = ws.state()
    text = csv_io.export(ws.project(), ws.result(), stale=state.stale_dates or state.stale_costs)
    if destination is not None:
        with open(destination, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return text
