"""The workspace: edit service, state and reads (plan sections 1.2 and 3, task T31).

A :class:`Workspace` owns one SQLite connection and the single *workspace* project
stored in it. SQLite is authoritative; an immutable :class:`~engine.model.Project`
snapshot is cached next to it and replaced after every committed edit (nothing is
reloaded). Every edit

1. validates against the cached project and builds the new project value (raising
   ``ValidationFailed`` / ``NotFound`` / ``TypeError`` before anything is written),
2. writes only the rows it changes (:mod:`persistence.row_ops`) plus the revision bump
   in one transaction,
3. swaps the cache, forgets delete previews, and emits an ``edited`` event.

Staleness is never tracked with flags: ``state()`` compares the fingerprints stored
with each result to the current project's. Rate / cost-view edits recost results
whose dates are still current, in the same transaction.

Extension points for the other service tasks (separate modules that ``Workspace``
delegates to; marked ``DELEGATION POINT`` below). This plumbing is internal
(underscore-prefixed) and not part of the public API:

* ``services/compute.py`` + ``jobs.py``: ``schedule``, ``level_preview``,
  ``apply_leveling``, ``submit_*`` ... They read ``ws.project()``, call the engine and
  hand results to :meth:`Workspace._store_result` / :meth:`Workspace._discard_results`
  (both make the workspace dirty: stored results are part of what Save persists).
* ``services/files.py``: ``save``, ``save_as``, ``load``, ``list_projects``,
  ``import_csv`` ... They use ``ws._connection``, ``ws._project_pk``,
  :meth:`Workspace._require_clean`, :meth:`Workspace._check_revision` and
  :meth:`Workspace._write_clean` inside their single transaction, then
  :meth:`Workspace._reload` / :meth:`Workspace._set_clean` after the commit.

Concurrency (D12): one open workspace per database file (an OS lock on a sidecar
``<database>.lock`` file), and every write checks the revision it expects
(``UPDATE ... WHERE revision = ?``), raising ``Conflict`` on a mismatch.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import sqlite3
import uuid
import weakref
from collections import deque
from collections.abc import Callable, Collection, Iterable, Sequence
from concurrent.futures import Executor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol, TextIO, TypeVar

from project_planner.engine import config as _cfg
from project_planner.engine.config import DEFAULT_CONFIG, Config
from project_planner.engine.errors import (
    Conflict,
    Issue,
    NotFound,
    ObjectType,
    UnsavedChanges,
    ValidationFailed,
)
from project_planner.engine.fingerprint import cost_fp, schedule_fp
from project_planner.engine.model import (
    Assignment,
    Calendar,
    CalendarException,
    CalendarSettings,
    Dependency,
    DependencyType,
    ExceptionKind,
    Holiday,
    NodeKind,
    Project,
    Resource,
    SizingMode,
    TimeQty,
    WbsNode,
    WorkUnit,
    as_time_qty,
)
from project_planner.engine.network import cycle_issue
from project_planner.engine.results import LevelingResult, ScheduleResult
from project_planner.persistence import repositories as repo
from project_planner.persistence import row_ops
from project_planner.persistence.db import FileLock, connect, transaction
from project_planner.persistence.migrations import migrate
from project_planner.persistence.records import RunKind
from project_planner.services.dto import DeletePreview, WorkspaceState
from project_planner.services.events import Callback, Event, EventBus, EventKind
from project_planner.services.result_store import ResultStore

if TYPE_CHECKING:
    from project_planner.services.files import ImportSummary, ProjectInfo
    from project_planner.services.jobs import Job
    from project_planner.services.read_models import (
        CostReportView,
        GanttRow,
        Link,
        LoadingView,
        ProjectSummary,
        TaskDetails,
        WbsPage,
    )

__all__ = ["CalendarApi", "Workspace", "open_workspace"]

_ID_RE = re.compile(r"^([A-Za-z_]+)(\d+)$")

_NODE_PREFIX = {NodeKind.GROUP: "g", NodeKind.TASK: "t", NodeKind.MILESTONE: "m"}


_R = TypeVar("_R", ScheduleResult, ScheduleResult | None)


class _HasId(Protocol):
    @property
    def id(self) -> str: ...


# =============================================================================
# Small helpers
# =============================================================================


def _fail(
    code: str,
    message: str,
    *,
    object_type: ObjectType | str,
    object_id: str | None = None,
    field: str | None = None,
) -> NoReturn:
    raise ValidationFailed(
        [Issue.error(code, message, object_type=object_type, object_id=object_id, field=field)]
    )


def _id_of(value: str | _HasId, what: str = "id") -> str:
    """Accept an ID or any object with an ``.id``."""
    if isinstance(value, str):
        return value
    ident = getattr(value, "id", None)
    if isinstance(ident, str):
        return ident
    raise TypeError(
        f"{what} must be an ID string or an object with an .id, got {type(value).__name__}"
    )


def _project_id(value: object) -> int | str:
    """A saved project's id from a ``ProjectInfo`` (or anything with an ``.id``) or the id."""
    ident = getattr(value, "id", value)
    if isinstance(ident, int | str):
        return ident
    raise TypeError(
        f"project_id must be a ProjectInfo, an int or a str, got {type(value).__name__}"
    )


def _release(conn: sqlite3.Connection, lock: FileLock | None) -> None:
    """Close the connection, then drop the file lock (``weakref.finalize`` target)."""
    try:
        conn.close()
    except sqlite3.ProgrammingError:
        pass  # collected on another thread: SQLite closes the handle on deallocation
    finally:
        if lock is not None:
            lock.release()


def _name(value: object, object_type: ObjectType, object_id: str | None) -> str:
    if not isinstance(value, str):
        raise TypeError(f"name must be a str, got {type(value).__name__}")
    if not value.strip():
        _fail(
            "NAME_EMPTY",
            "name must not be empty",
            object_type=object_type,
            object_id=object_id,
            field="name",
        )
    return value


def _number(
    value: object, what: str, *, object_type: ObjectType, object_id: str | None, field: str
) -> Decimal:
    """A ``Decimal`` from int / str / Decimal; ``float`` and ``bool`` are refused."""
    if isinstance(value, bool | float) or not isinstance(value, int | str | Decimal):
        raise TypeError(
            f"{what} must be a Decimal, int or str (never float), got {type(value).__name__}"
        )
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation:
        _fail(
            "NUMBER_INVALID",
            f"{what} {value!r} is not a number",
            object_type=object_type,
            object_id=object_id,
            field=field,
        )
    if not number.is_finite():
        _fail(
            "NUMBER_INVALID",
            f"{what} {value!r} must be finite",
            object_type=object_type,
            object_id=object_id,
            field=field,
        )
    return number


def _find_path(project: Project, start: str, goal: str) -> list[str] | None:
    """Shortest dependency path ``start -> ... -> goal`` (node IDs), or ``None``."""
    previous: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == goal:
            path: list[str] = []
            walk: str | None = current
            while walk is not None:
                path.append(walk)
                walk = previous[walk]
            return path[::-1]
        for dep in project.dependencies_from(current):
            if dep.succ_id not in previous:
                previous[dep.succ_id] = current
                queue.append(dep.succ_id)
    return None


class _IdAllocator:
    """Next unused ``<prefix><number>`` per prefix (``t1``, ``t2`` ... ``r1`` ... ``d1``)."""

    def __init__(self, project: Project) -> None:
        self._max: dict[str, int] = {}
        for ident in (
            *(n.id for n in project.nodes),
            *(r.id for r in project.resources),
            *(d.id for d in project.dependencies),
        ):
            self.note(ident)

    def note(self, ident: str) -> None:
        match = _ID_RE.match(ident)
        if match is not None:
            prefix, number = match.group(1), int(match.group(2))
            if number > self._max.get(prefix, 0):
                self._max[prefix] = number

    def peek(self, prefix: str, exists: Callable[[str], bool]) -> str:
        number = self._max.get(prefix, 0) + 1
        while exists(f"{prefix}{number}"):
            number += 1
        return f"{prefix}{number}"


# =============================================================================
# Calendar namespace
# =============================================================================


class CalendarApi:
    """``ws.calendar``: initialisation and edits of the project calendar (plan 2.3).

    Every method is one edit: all values are validated together through
    :meth:`CalendarSettings.from_values` (``ValidationFailed`` lists every bad field)
    and applied all-or-nothing. Changes to ``working_days_per_year`` do not stale the
    schedule (only person-year views are recomputed).
    """

    def __init__(self, workspace: Workspace) -> None:
        self._ws = workspace

    def settings(self) -> CalendarSettings:
        """The current calendar as :class:`CalendarSettings`."""
        return self._ws.project().calendar.settings()

    def initialize(
        self,
        *,
        working_weekdays: Any = _cfg.DEFAULT_WORKING_WEEKDAYS,
        hours_per_day: Any = _cfg.DEFAULT_HOURS_PER_DAY,
        working_days_per_year: Any = _cfg.DEFAULT_WORKING_DAYS_PER_YEAR,
        workday_start: Any = _cfg.DEFAULT_WORKDAY_START,
        holidays: Iterable[Any] = (),
        exceptions: Iterable[Any] = (),
    ) -> None:
        """Set the whole calendar; omitted arguments take their plan 2.3 defaults.

        Raises:
            ValidationFailed: one issue per invalid field; nothing is applied.
        """
        settings = CalendarSettings.from_values(
            working_weekdays=working_weekdays,
            hours_per_day=hours_per_day,
            working_days_per_year=working_days_per_year,
            workday_start=workday_start,
            holidays=holidays,
            exceptions=exceptions,
        )
        self._ws._set_calendar(settings.to_calendar(), "calendar.initialize")

    def _change(self, operation: str, **changes: Any) -> None:
        cal = self._ws.project().calendar
        values: dict[str, Any] = {
            "working_weekdays": cal.working_weekdays,
            "hours_per_day": cal.hours_per_day,
            "working_days_per_year": cal.working_days_per_year,
            "workday_start": cal.workday_start,
            "holidays": cal.holidays,
            "exceptions": cal.exceptions,
        }
        values.update(changes)
        new = CalendarSettings.from_values(**values).to_calendar()
        self._ws._set_calendar(new, operation, ids=tuple(changes))

    def set_hours_per_day(self, hours: int | str | Decimal) -> None:
        """Set working hours per day (``> 0`` and ``<= 24``, whole minutes)."""
        self._change("calendar.set_hours_per_day", hours_per_day=hours)

    def set_working_days_per_year(self, days: int | str | Decimal) -> None:
        """Set working days per year (person-year reporting only; nothing becomes stale)."""
        self._change("calendar.set_working_days_per_year", working_days_per_year=days)

    def set_working_weekdays(self, weekdays: Any) -> None:
        """Set the recurring working weekdays, e.g. ``"Mon-Fri"``."""
        self._change("calendar.set_working_weekdays", working_weekdays=weekdays)

    def set_workday_start(self, start: dt.time | str) -> None:
        """Set the clock time the working day starts, e.g. ``"09:00"``."""
        self._change("calendar.set_workday_start", workday_start=start)

    def add_holiday(self, date: dt.date, name: str = "") -> None:
        """Add a nonworking date. Raises ``ValidationFailed`` for a duplicate date."""
        cal = self._ws.project().calendar
        self._change("calendar.add_holiday", holidays=(*cal.holidays, Holiday(date, name)))

    def remove_holiday(self, date: dt.date) -> None:
        """Remove a holiday. Raises ``NotFound`` if the date is not a holiday."""
        cal = self._ws.project().calendar
        kept = tuple(h for h in cal.holidays if h.date != date)
        if len(kept) == len(cal.holidays):
            raise NotFound("holiday", date.isoformat())
        self._change("calendar.remove_holiday", holidays=kept)

    def add_exception(self, date: dt.date, kind: ExceptionKind | str, name: str = "") -> None:
        """Add a working / nonworking override for a date."""
        cal = self._ws.project().calendar
        try:
            exc_kind = ExceptionKind(kind)
        except ValueError:
            _fail(
                "INVALID_VALUE",
                f"exception kind {kind!r} must be 'working' or 'nonworking'",
                object_type=ObjectType.CALENDAR,
                field="exceptions",
            )
        exception = CalendarException(date, exc_kind, name)
        self._change("calendar.add_exception", exceptions=(*cal.exceptions, exception))

    def remove_exception(self, date: dt.date) -> None:
        """Remove a date exception. Raises ``NotFound`` if there is none for the date."""
        cal = self._ws.project().calendar
        kept = tuple(e for e in cal.exceptions if e.date != date)
        if len(kept) == len(cal.exceptions):
            raise NotFound("exception", date.isoformat())
        self._change("calendar.remove_exception", exceptions=kept)

    def __repr__(self) -> str:
        return f"<calendar {self.settings()!r}>"


# =============================================================================
# Workspace
# =============================================================================

_WriteFn = Callable[[sqlite3.Connection, int], None]


class Workspace:
    """Working project of one SQLite database. Create with :func:`open_workspace`.

    A file database can be open in only one workspace at a time (D12): the workspace
    holds an OS lock on ``<database>.lock`` until :meth:`close` (or garbage collection,
    or process exit). Every write additionally checks that the stored revision is the
    one this workspace last saw and raises ``Conflict`` otherwise.

    Members starting with ``_`` (``_store_result``, ``_discard_results``,
    ``_mark_clean``, ``_reload``, ``_require_clean``, ``_connection``, ``_results``,
    ``_project_pk``) are internal plumbing for the service modules.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        project_pk: int,
        config: Config = DEFAULT_CONFIG,
        *,
        lock: FileLock | None = None,
    ) -> None:
        self._connection = conn
        self._project_pk = project_pk
        self._config = config
        self._events = EventBus()
        self.calendar = CalendarApi(self)
        self._closed = False
        self._finalizer = weakref.finalize(self, _release, conn, lock)
        self._previews: dict[str, DeletePreview] = {}
        self._fp_cache: tuple[int, str, str] | None = None
        self._project: Project
        self._revision = 0
        self._based_on_revision = 1
        self._results_gen = 0
        self._based_on_results_gen = 0
        self._ids: _IdAllocator
        self._results = ResultStore(conn, project_pk, lambda: self._project)
        self._load_cache()

    # --------------------------------------------------------------- lifecycle

    def _load_cache(self) -> None:
        record = repo.get_project_record(self._connection, self._project_pk)
        self._project = repo.load_project(self._connection, self._project_pk)
        self._revision = record.revision
        self._based_on_revision = (
            record.based_on_revision if record.based_on_revision is not None else 1
        )
        self._results_gen = record.results_gen
        self._based_on_results_gen = record.based_on_results_gen
        self._ids = _IdAllocator(self._project)
        self._previews.clear()
        self._fp_cache = None
        self._results.reset(self._project_pk)

    def close(self) -> None:
        """Close the database connection and release the file lock (idempotent)."""
        if not self._closed:
            self._closed = True
            self._finalizer()

    def __enter__(self) -> Workspace:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("workspace is closed")

    @property
    def config(self) -> Config:
        """The configuration the workspace was opened with (read-only)."""
        return self._config

    def subscribe(self, callback: Callback) -> Callable[[], None]:
        """Register an event callback; returns the function that unsubscribes it."""
        return self._events.subscribe(callback)

    def __repr__(self) -> str:
        name = self._project.name
        return f"<Workspace {name!r} revision={self._revision}{' closed' if self._closed else ''}>"

    @property
    def dirty(self) -> bool:
        """Unsaved changes since the last save / load.

        True when the definition revision *or* the stored results changed (every store
        or discard of a result counts, including a discarded preview; G1).
        """
        return (
            self._revision != self._based_on_revision
            or self._results_gen != self._based_on_results_gen
        )

    # ------------------------------------------------- internal service plumbing

    def _require_clean(self, discard_unsaved: bool = False) -> None:
        """Raise ``UnsavedChanges`` if dirty and ``discard_unsaved`` is false."""
        if self.dirty and not discard_unsaved:
            raise UnsavedChanges()

    def _check_revision(self) -> None:
        """``Conflict`` if another connection changed the workspace row."""
        repo.check_revision(self._connection, self._project_pk, self._revision, self._results_gen)

    def _write_clean(self, based_on_pk: int | None) -> None:
        """Record the current revision / results generation as saved (in a transaction)."""
        row_ops.set_based_on(
            self._connection, self._project_pk, based_on_pk, self._revision, self._results_gen
        )

    def _set_clean(self) -> None:
        """Update the in-memory saved markers after :meth:`_write_clean` committed."""
        self._based_on_revision = self._revision
        self._based_on_results_gen = self._results_gen

    def _mark_clean(self, based_on_pk: int | None = None) -> None:
        """Record that the current state was just saved / loaded (own transaction)."""
        self._check_open()
        with transaction(self._connection):
            self._check_revision()
            self._write_clean(based_on_pk)
        self._set_clean()

    def _reload(self, *, operation: str = "reload") -> None:
        """Re-read everything from the database after the rows were replaced elsewhere.

        Load / import replace the workspace rows with repository functions (in one
        transaction that also records the saved state), then call this to refresh the
        cache and notify subscribers (``project_replaced``) with the final state.
        """
        self._check_open()
        self._load_cache()
        self._events.emit(Event(EventKind.PROJECT_REPLACED, self._revision, (), operation))

    def _store_result(self, result: ScheduleResult) -> None:
        """Persist ``result`` as the stored run of its kind (see ``result_store`` rules).

        Does not bump the revision (results are not part of the definition) but bumps
        the results generation, so the workspace becomes dirty; emits
        ``result_stored``. The result may already be stale; ``state()`` reports that.

        Raises:
            Conflict: another connection changed the workspace.
        """
        self._check_open()
        try:
            with transaction(self._connection):
                gen = repo.bump_results_gen(
                    self._connection,
                    self._project_pk,
                    revision=self._revision,
                    results_gen=self._results_gen,
                )
                self._results.store(result)
        except BaseException:
            self._results.reset()
            raise
        self._results_gen = gen
        self._events.emit(
            Event(EventKind.RESULT_STORED, self._revision, (result.kind,), "store_result")
        )

    def _discard_results(self, kinds: Iterable[RunKind | str] | None = None) -> None:
        """Delete stored results of ``kinds`` (all when ``None``); emits ``result_stored``.

        Deleting at least one stored run makes the workspace dirty.
        """
        self._check_open()
        wanted = tuple(RunKind) if kinds is None else tuple(RunKind(k) for k in kinds)
        present = self._results.kinds()
        gen = self._results_gen
        try:
            with transaction(self._connection):
                if present.intersection(wanted):
                    gen = repo.bump_results_gen(
                        self._connection,
                        self._project_pk,
                        revision=self._revision,
                        results_gen=self._results_gen,
                    )
                else:
                    self._check_revision()
                self._results.clear(wanted)
        except BaseException:
            self._results.reset()
            raise
        self._results_gen = gen
        ids = tuple(str(k) for k in wanted)
        self._events.emit(Event(EventKind.RESULT_STORED, self._revision, ids, "discard_results"))

    # ------------------------------------------------------------------- reads

    def project(self) -> Project:
        """Immutable snapshot of the current definition."""
        return self._project

    def node(self, node: str | _HasId) -> WbsNode:
        """A WBS node. Raises ``NotFound``."""
        return self._project.node(_id_of(node))

    def resource(self, resource: str | _HasId) -> Resource:
        """A resource. Raises ``NotFound``."""
        return self._project.resource(_id_of(resource))

    def dependency(self, dependency: str | _HasId) -> Dependency:
        """A dependency. Raises ``NotFound``."""
        return self._project.dependency(_id_of(dependency))

    def _fps(self) -> tuple[str, str]:
        cached = self._fp_cache
        if cached is None or cached[0] != self._revision:
            cached = (self._revision, schedule_fp(self._project), cost_fp(self._project))
            self._fp_cache = cached
        return cached[1], cached[2]

    def _named(self, result: _R) -> _R:
        """Attach node names to ``result`` for notebook display (no effect on its value)."""
        if result is not None:
            from project_planner import notebook

            notebook.attach_names(result, self._project, self._revision)
        return result

    def _poll_jobs(self) -> None:
        from project_planner.services import jobs

        jobs.poll(self)

    def state(self) -> WorkspaceState:
        """Revision, dirty flag and staleness of the stored results."""
        self._poll_jobs()
        runs = self._results.fingerprints()
        stale_dates = stale_costs = False
        if runs:
            sfp, cfp = self._fps()
            stale_dates = any(s != sfp for s, _ in runs.values())
            stale_costs = any(c != cfp for _, c in runs.values())
        return WorkspaceState(
            revision=self._revision,
            dirty=self.dirty,
            stale_dates=stale_dates,
            stale_costs=stale_costs,
            has_preview=RunKind.LEVELING_PREVIEW in runs,
            has_result=RunKind.DEPENDENCY_ONLY in runs or RunKind.LEVELED in runs,
        )

    def result(self) -> ScheduleResult | None:
        """The stored current result: leveled if applied, else dependency-only, else ``None``.

        It may be stale; compare with :meth:`state`.
        """
        self._check_open()
        self._poll_jobs()
        return self._named(self._results.current())

    # ---- facade delegation (T35): compute, jobs, files, read models

    def schedule(self) -> ScheduleResult:
        """Synchronously compute and store the dependency-only schedule."""
        from project_planner.services import compute

        return self._named(compute.schedule(self))

    def level_preview(self) -> LevelingResult:
        """Synchronously compute and store a leveling preview."""
        from project_planner.services import compute

        out = compute.level_preview(self)
        self._named(out.result)
        return out

    def apply_leveling(self) -> ScheduleResult:
        """Make the stored preview the current (leveled) result."""
        from project_planner.services import compute

        return self._named(compute.apply_leveling(self))

    def discard_leveling(self) -> None:
        """Drop the stored leveling preview."""
        from project_planner.services import compute

        compute.discard_leveling(self)

    def reset_to_dependency_schedule(self) -> ScheduleResult | None:
        """Drop leveling and return to the dependency-only result (never recalculates)."""
        from project_planner.services import compute

        return self._named(compute.reset_to_dependency_schedule(self))

    def submit_schedule(self, *, executor: Executor | None = None) -> Job:
        """Start scheduling in the background; returns a :class:`Job`."""
        from project_planner.services import jobs

        return jobs.submit_schedule(self, executor=executor)

    def submit_leveling_preview(self, *, executor: Executor | None = None) -> Job:
        """Start a leveling preview in the background; returns a :class:`Job`."""
        from project_planner.services import jobs

        return jobs.submit_leveling_preview(self, executor=executor)

    def poll_jobs(self) -> list[Job]:
        """Store results of finished jobs; returns the jobs that finished in this call."""
        from project_planner.services import jobs

        return jobs.poll(self)

    def save(self) -> ProjectInfo:
        """Save the project to the library (overwrites its tracked saved copy)."""
        from project_planner.services import files

        return files.save(self)

    def save_as(self, name: str) -> ProjectInfo:
        """Save the project under a new library ``name``."""
        from project_planner.services import files

        return files.save_as(self, name)

    def list_projects(self) -> list[ProjectInfo]:
        """Saved projects, newest first."""
        from project_planner.services import files

        return files.list_projects(self)

    def import_csv(
        self, source: str | Path | TextIO, *, discard_unsaved: bool = False
    ) -> ImportSummary:
        """Replace the project by a CSV file (path, CSV text or open file)."""
        from project_planner.services import files

        return files.import_csv(self, source, discard_unsaved=discard_unsaved)

    def export_csv(self, destination: str | Path | None = None) -> str:
        """CSV text of the project; written to ``destination`` when given."""
        from project_planner.services import files

        return files.export_csv(self, destination)

    def cost_report(self, unit: WorkUnit | str | None = None) -> CostReportView:
        """Cost report of the current result in ``unit`` (``Conflict`` without a result)."""
        from project_planner.services import read_models

        return read_models.cost_report(self, unit)

    def task_details(self, task: str | _HasId, unit: WorkUnit | str | None = None) -> TaskDetails:
        """Detail panel data of one task."""
        from project_planner.services import read_models

        return read_models.task_details(self, _id_of(task), unit)

    def loading(
        self,
        resource: str | _HasId,
        *,
        time_window: tuple[dt.datetime, dt.datetime] | None = None,
        granularity: Literal["segments", "day", "week"] = "segments",
    ) -> LoadingView:
        """Loading of one resource from the current result."""
        from project_planner.services import read_models

        return read_models.loading(
            self, _id_of(resource), time_window=time_window, granularity=granularity
        )

    def wbs_rows(
        self,
        *,
        parent: str | _HasId | None = None,
        expanded_ids: Collection[str] = frozenset(),
        offset: int = 0,
        limit: int | None = None,
    ) -> WbsPage:
        """Windowed visible WBS rows."""
        from project_planner.services import read_models

        return read_models.wbs_rows(
            self,
            parent_id=None if parent is None else _id_of(parent),
            expanded_ids=expanded_ids,
            offset=offset,
            limit=limit,
        )

    def gantt_rows(
        self,
        *,
        expanded_ids: Collection[str] = frozenset(),
        offset: int = 0,
        limit: int | None = None,
        time_window: tuple[dt.datetime, dt.datetime] | None = None,
    ) -> list[GanttRow]:
        """Gantt rows aligned with :meth:`wbs_rows`."""
        from project_planner.services import read_models

        return read_models.gantt_rows(
            self,
            expanded_ids=expanded_ids,
            offset=offset,
            limit=limit,
            time_window=time_window,
        )

    def dependency_links(self, nodes: Iterable[str | _HasId]) -> list[Link]:
        """Dependencies whose both ends are among ``nodes``."""
        from project_planner.services import read_models

        return read_models.dependency_links(self, nodes)

    def nonworking_ranges(self, start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
        """Inclusive merged nonworking date ranges between ``start`` and ``end``."""
        from project_planner.services import read_models

        return read_models.nonworking_ranges(self, start, end)

    def project_summary(self) -> ProjectSummary:
        """Summary figures of the project and its current result."""
        from project_planner.services import read_models

        return read_models.project_summary(self)

    def load(self, project_id: int | str | ProjectInfo, *, discard_unsaved: bool = False) -> None:
        """Load a saved project (``UnsavedChanges`` if dirty unless ``discard_unsaved``).

        ``project_id`` is a ``ProjectInfo`` (from :meth:`list_projects` / :meth:`save_as`)
        or its ``id``.
        """
        from project_planner.services import files

        files.load(self, _project_id(project_id), discard_unsaved=discard_unsaved)

    def delete_project(self, project_id: int | str | ProjectInfo) -> None:
        """Delete a saved project from the library (``ProjectInfo`` or its ``id``)."""
        from project_planner.services import files

        files.delete_project(self, _project_id(project_id))

    # ------------------------------------------------------------ edit machinery

    def _commit(
        self,
        operation: str,
        new_project: Project,
        write: _WriteFn,
        ids: Sequence[str] = (),
        *,
        refresh: bool = False,
    ) -> None:
        """Write one edit atomically, then swap the cache and notify."""
        self._check_open()
        plan = self._results.plan_refresh(new_project) if refresh else {}
        with transaction(self._connection):
            revision = repo.bump_revision(
                self._connection, self._project_pk, expected=self._revision
            )
            write(self._connection, self._project_pk)
            self._results.write_plan(plan)
        self._project = new_project
        self._revision = revision
        self._results.apply_plan(plan)
        for ident in ids:
            self._ids.note(ident)
        self._events.emit(Event(EventKind.EDITED, revision, tuple(ids), operation))

    def _replace(self, **changes: Any) -> Project:
        return dataclasses.replace(self._project, **changes)

    def _set_calendar(self, calendar: Calendar, operation: str, ids: Sequence[str] = ()) -> None:
        old = self._project.calendar
        self._commit(
            operation,
            self._replace(calendar=calendar),
            lambda conn, pk: row_ops.apply_calendar(conn, pk, old, calendar),
            ids,
            refresh=True,
        )

    # -------------------------------------------------------------- new project

    def new_project(
        self,
        name: str,
        start: dt.date,
        *,
        currency: str = _cfg.DEFAULT_CURRENCY,
        cost_report_unit: WorkUnit | str = _cfg.DEFAULT_COST_REPORT_UNIT,
        calendar: CalendarSettings | Calendar | None = None,
        discard_unsaved: bool = False,
    ) -> None:
        """Replace the workspace project by an empty one (default calendar).

        Raises:
            UnsavedChanges: the workspace is dirty and ``discard_unsaved`` is false.
            ValidationFailed: invalid name / currency / unit / calendar settings
                (nothing is replaced).
        """
        self._check_open()
        self._require_clean(discard_unsaved)
        _name(name, ObjectType.PROJECT, None)
        if calendar is None:
            cal = Calendar()
        elif isinstance(calendar, Calendar):
            cal = calendar
        else:
            cal = calendar.to_calendar()
        project = Project(
            id="p1",
            name=name,
            start=start,
            currency=currency,
            cost_report_unit=self._work_unit(cost_report_unit, "p1"),
            calendar=cal,
        )
        conn, pk = self._connection, self._project_pk
        with transaction(conn):
            revision = repo.bump_revision(conn, pk, expected=self._revision)
            repo.check_revision(conn, pk, revision, self._results_gen)
            repo.save_project(conn, pk, project)
            repo.delete_runs(conn, pk)
            row_ops.set_name(conn, pk, name)
            # A fresh project is "clean": nothing to save yet.
            row_ops.set_based_on(conn, pk, None, revision, self._results_gen)
        self._load_cache()
        self._events.emit(Event(EventKind.PROJECT_REPLACED, self._revision, (), "new_project"))

    # ----------------------------------------------------------- project settings

    def set_project_start(self, start: dt.date) -> None:
        """Move the project start (makes the schedule stale)."""
        project = self._replace(start=start)
        self._commit(
            "set_project_start",
            project,
            lambda conn, pk: row_ops.update_project_fields(conn, pk, project),
        )

    def rename_project(self, name: str) -> None:
        """Rename the project."""
        _name(name, ObjectType.PROJECT, self._project.id)
        project = self._replace(name=name)

        def write(conn: sqlite3.Connection, pk: int) -> None:
            row_ops.update_project_fields(conn, pk, project)
            row_ops.set_name(conn, pk, name)

        self._commit("rename_project", project, write)

    def set_currency(self, currency: str) -> None:
        """Set the three-letter currency code (affects no calculation)."""
        project = self._replace(currency=currency)
        self._commit(
            "set_currency",
            project,
            lambda conn, pk: row_ops.update_project_fields(conn, pk, project),
        )

    @staticmethod
    def _work_unit(unit: WorkUnit | str, project_id: str) -> WorkUnit:
        try:
            return WorkUnit(unit)
        except ValueError:
            allowed = ", ".join(u.value for u in WorkUnit)
            _fail(
                "INVALID_VALUE",
                f"cost_report_unit {unit!r} is not one of: {allowed}",
                object_type=ObjectType.PROJECT,
                object_id=project_id,
                field="cost_report_unit",
            )

    def set_cost_report_unit(self, unit: WorkUnit | str) -> None:
        """Set the default cost report unit (recosts views; nothing becomes stale)."""
        project = self._replace(cost_report_unit=self._work_unit(unit, self._project.id))
        self._commit(
            "set_cost_report_unit",
            project,
            lambda conn, pk: row_ops.update_project_fields(conn, pk, project),
            refresh=True,
        )

    # ----------------------------------------------------------------- resources

    def _rate(self, rate: object, resource_id: str | None) -> Decimal | None:
        if rate is None:
            return None
        return _number(
            rate,
            "hourly_rate",
            object_type=ObjectType.RESOURCE,
            object_id=resource_id,
            field="hourly_rate",
        )

    def add_resource(self, name: str, hourly_rate: str | int | Decimal | None = None) -> str:
        """Add a resource; ``hourly_rate=None`` means "rate missing". Returns its ID."""
        ident = self._ids.peek("r", self._project.has_resource)
        resource = Resource(
            ident, _name(name, ObjectType.RESOURCE, ident), self._rate(hourly_rate, ident)
        )
        project = self._replace(resources=(*self._project.resources, resource))
        self._commit(
            "add_resource",
            project,
            lambda conn, pk: row_ops.insert_resource(conn, pk, resource),
            (ident,),
        )
        return ident

    def rename_resource(self, resource: str | _HasId, name: str) -> None:
        """Rename a resource."""
        old = self.resource(resource)
        new = dataclasses.replace(old, name=_name(name, ObjectType.RESOURCE, old.id))
        self._update_resource("rename_resource", new)

    def set_hourly_rate(self, resource: str | _HasId, rate: str | int | Decimal | None) -> None:
        """Set (or clear with ``None``) a resource's hourly rate.

        Costs of results whose dates are still current are recomputed immediately.
        """
        old = self.resource(resource)
        new = Resource(old.id, old.name, self._rate(rate, old.id))
        self._update_resource("set_hourly_rate", new, refresh=True)

    def _update_resource(self, operation: str, new: Resource, *, refresh: bool = False) -> None:
        resources = tuple(new if r.id == new.id else r for r in self._project.resources)
        self._commit(
            operation,
            self._replace(resources=resources),
            lambda conn, pk: row_ops.update_resource(conn, pk, new),
            (new.id,),
            refresh=refresh,
        )

    def remove_resource(self, resource: str | _HasId) -> None:
        """Remove a resource and its assignments (use :meth:`delete_preview` to see them)."""
        nodes, res, deps, _ = self._collect((), [self.resource(resource).id])
        self._apply_delete("remove_resource", nodes, res, deps)

    # --------------------------------------------------------------------- nodes

    def _parent_id(self, parent: str | _HasId | None) -> str | None:
        if parent is None:
            return None
        pid = _id_of(parent, "parent")
        node = self._project.node(pid)
        if node.kind is not NodeKind.GROUP:
            _fail(
                "NODE_PARENT_NOT_GROUP",
                f"parent {pid} is a {node.kind.value}, not a group",
                object_type=ObjectType.NODE,
                field="parent_id",
            )
        return pid

    def _next_order(self, parent_id: str | None) -> int:
        siblings = self._project.children(parent_id)
        return siblings[-1].order + 1 if siblings else 0

    def _add_node(
        self,
        operation: str,
        kind: NodeKind,
        name: str,
        parent: str | _HasId | None,
        mode: SizingMode = SizingMode.NONE,
        sizing: TimeQty | None = None,
    ) -> str:
        parent_id = self._parent_id(parent)
        ident = self._ids.peek(_NODE_PREFIX[kind], self._project.has_node)
        node = WbsNode(
            ident,
            _name(name, ObjectType.NODE, ident),
            kind,
            parent_id,
            self._next_order(parent_id),
            mode,
            sizing,
        )
        project = self._replace(nodes=(*self._project.nodes, node))
        self._commit(
            operation, project, lambda conn, pk: row_ops.insert_node(conn, pk, node), (ident,)
        )
        return ident

    def _sizing_args(
        self,
        node_id: str | None,
        duration: TimeQty | str | None,
        effort: TimeQty | str | None,
    ) -> tuple[SizingMode, TimeQty | None]:
        if duration is not None and effort is not None:
            _fail(
                "SIZING_AMBIGUOUS",
                "give either a duration or an effort, not both",
                object_type=ObjectType.NODE,
                object_id=node_id,
                field="sizing",
            )
        if duration is not None:
            return SizingMode.DURATION, as_time_qty(
                duration, object_type=ObjectType.NODE, object_id=node_id, field="duration"
            )
        if effort is not None:
            return SizingMode.EFFORT, as_time_qty(
                effort, object_type=ObjectType.NODE, object_id=node_id, field="effort"
            )
        return SizingMode.NONE, None

    def add_group(self, name: str, parent: str | _HasId | None = None) -> str:
        """Add a summary group (last child of ``parent``, or top level). Returns its ID."""
        return self._add_node("add_group", NodeKind.GROUP, name, parent)

    def add_task(
        self,
        name: str,
        parent: str | _HasId | None = None,
        *,
        duration: TimeQty | str | None = None,
        effort: TimeQty | str | None = None,
    ) -> str:
        """Add a task, optionally sized by ``duration`` or ``effort`` (``TimeQty`` or ``"5d"``)."""
        mode, sizing = self._sizing_args(None, duration, effort)
        return self._add_node("add_task", NodeKind.TASK, name, parent, mode, sizing)

    def add_milestone(self, name: str, parent: str | _HasId | None = None) -> str:
        """Add a zero-duration milestone. Returns its ID."""
        return self._add_node("add_milestone", NodeKind.MILESTONE, name, parent)

    def _update_nodes(
        self,
        operation: str,
        changed: Sequence[WbsNode],
        write: _WriteFn,
        ids: Sequence[str],
    ) -> None:
        by_id = {n.id: n for n in changed}
        nodes = tuple(by_id.get(n.id, n) for n in self._project.nodes)
        self._commit(operation, self._replace(nodes=nodes), write, ids)

    def rename_node(self, node: str | _HasId, name: str) -> None:
        """Rename a node (does not stale the schedule)."""
        old = self.node(node)
        new = dataclasses.replace(old, name=_name(name, ObjectType.NODE, old.id))
        self._update_nodes(
            "rename_node", [new], lambda conn, pk: row_ops.update_node(conn, pk, new), (old.id,)
        )

    def set_sizing(
        self,
        task: str | _HasId,
        *,
        duration: TimeQty | str | None = None,
        effort: TimeQty | str | None = None,
    ) -> None:
        """Set a task's duration or effort as entered; neither clears the sizing.

        Raises:
            ValidationFailed: both given, negative, or the node is not a task.
            TypeError: a bare number was passed.
        """
        old = self.node(task)
        mode, sizing = self._sizing_args(old.id, duration, effort)
        new = WbsNode(old.id, old.name, old.kind, old.parent_id, old.order, mode, sizing)
        self._update_nodes(
            "set_sizing", [new], lambda conn, pk: row_ops.update_node(conn, pk, new), (old.id,)
        )

    def move_node(self, node: str | _HasId, parent: str | _HasId | None, index: int) -> None:
        """Move a node under ``parent`` (``None`` = top level) at position ``index``.

        ``index`` counts among the new siblings without the moved node
        (``0..len``). Also used to reorder within the same parent.

        Raises:
            ValidationFailed: parent is not a group, a group is moved under itself or a
                descendant, or ``index`` is out of range.
        """
        moving = self.node(node)
        parent_id = self._parent_id(parent)
        walk = parent_id
        while walk is not None:
            if walk == moving.id:
                _fail(
                    "NODE_PARENT_CYCLE",
                    f"cannot move {moving.id} under {parent_id}: it is inside {moving.id}",
                    object_type=ObjectType.NODE,
                    object_id=moving.id,
                    field="parent_id",
                )
            walk = self._project.node(walk).parent_id
        siblings = [c for c in self._project.children(parent_id) if c.id != moving.id]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError(f"index must be an int, got {type(index).__name__}")
        if not 0 <= index <= len(siblings):
            _fail(
                "INDEX_RANGE",
                f"index {index} is out of range 0..{len(siblings)}",
                object_type=ObjectType.NODE,
                object_id=moving.id,
                field="index",
            )
        siblings.insert(index, moving)
        changed: list[WbsNode] = []
        for position, sibling in enumerate(siblings):
            updated = dataclasses.replace(sibling, parent_id=parent_id, order=position)
            if updated != sibling:
                changed.append(updated)
        self._update_nodes(
            "move_node",
            changed,
            lambda conn, pk: row_ops.update_node_positions(conn, pk, changed),
            (moving.id,),
        )

    # --------------------------------------------------------------- assignments

    def set_assignment(
        self, task: str | _HasId, resource: str | _HasId, percent: str | int | Decimal
    ) -> None:
        """Assign ``resource`` to ``task`` at ``percent``; updates an existing pair.

        Raises:
            NotFound: unknown task or resource.
            ValidationFailed: ``percent`` not in ``(0, Config.max_assignment_percent]``
                or the node is not a task.
        """
        node = self.node(task)
        res = self.resource(resource)
        key = f"{node.id}/{res.id}"
        value = _number(
            percent, "percent", object_type=ObjectType.ASSIGNMENT, object_id=key, field="percent"
        )
        issues: list[Issue] = []
        if node.kind is not NodeKind.TASK:
            issues.append(
                Issue.error(
                    "ASSIGN_NOT_TASK",
                    f"assignment {key}: {node.id} is a {node.kind.value}, not a task",
                    object_type=ObjectType.ASSIGNMENT,
                    object_id=key,
                    field="task_id",
                )
            )
        if value <= 0 or value > self._config.max_assignment_percent:
            issues.append(
                Issue.error(
                    "ASSIGN_PERCENT_RANGE",
                    f"assignment {key}: percent {value} must be greater than 0 and at most "
                    f"{self._config.max_assignment_percent}",
                    object_type=ObjectType.ASSIGNMENT,
                    object_id=key,
                    field="percent",
                )
            )
        if issues:
            raise ValidationFailed(issues)
        assignment = Assignment(node.id, res.id, value)
        others = tuple(a for a in self._project.assignments if a.key != assignment.key)
        self._commit(
            "set_assignment",
            self._replace(assignments=(*others, assignment)),
            lambda conn, pk: row_ops.upsert_assignment(conn, pk, assignment),
            (key,),
        )

    def remove_assignment(self, task: str | _HasId, resource: str | _HasId) -> None:
        """Remove one assignment. Raises ``NotFound`` if the pair is not assigned."""
        existing = self._project.assignment(_id_of(task, "task"), _id_of(resource, "resource"))
        others = tuple(a for a in self._project.assignments if a.key != existing.key)
        self._commit(
            "remove_assignment",
            self._replace(assignments=others),
            lambda conn, pk: row_ops.delete_assignment(
                conn, pk, existing.task_id, existing.resource_id
            ),
            (existing.key_text,),
        )

    # -------------------------------------------------------------- dependencies

    @staticmethod
    def _dep_type(value: DependencyType | str, dep_id: str | None) -> DependencyType:
        try:
            return DependencyType(value.upper() if isinstance(value, str) else value)
        except ValueError:
            allowed = ", ".join(t.value for t in DependencyType)
            _fail(
                "INVALID_VALUE",
                f"dependency type {value!r} is not one of: {allowed}",
                object_type=ObjectType.DEPENDENCY,
                object_id=dep_id,
                field="type",
            )

    def _check_duplicate(self, dep_id: str, pred: str, succ: str, dep_type: DependencyType) -> None:
        for other in self._project.dependencies_from(pred):
            if other.succ_id == succ and other.type is dep_type and other.id != dep_id:
                _fail(
                    "DEP_DUPLICATE",
                    f"dependency {other.id} already links {pred} -> {succ} ({dep_type.value})",
                    object_type=ObjectType.DEPENDENCY,
                    object_id=dep_id,
                    field="type",
                )

    def add_dependency(
        self,
        pred: str | _HasId,
        succ: str | _HasId,
        type: DependencyType | str = "FS",  # noqa: A002 - public API name
        lag: TimeQty | str = "0d",
    ) -> str:
        """Add a dependency ``pred -> succ``; returns its ID.

        Raises:
            NotFound: unknown endpoint.
            ValidationFailed: self-dependency, group endpoint, duplicate, or a cycle
                (the issue names every node of the cycle).
            TypeError: a bare number as ``lag``.
        """
        pred_node = self.node(pred)
        succ_node = self.node(succ)
        ident = self._ids.peek("d", self._project.has_dependency)
        dep_type = self._dep_type(type, ident)
        lag_qty = as_time_qty(lag, object_type=ObjectType.DEPENDENCY, object_id=ident, field="lag")
        issues: list[Issue] = []
        if pred_node.id == succ_node.id:
            issues.append(
                Issue.error(
                    "DEP_SELF",
                    f"node {pred_node.id} cannot depend on itself",
                    object_type=ObjectType.DEPENDENCY,
                    object_id=ident,
                    field="succ_id",
                )
            )
        for field_name, endpoint in (("pred_id", pred_node), ("succ_id", succ_node)):
            if endpoint.kind is NodeKind.GROUP:
                issues.append(
                    Issue.error(
                        "DEP_GROUP_ENDPOINT",
                        f"{field_name} {endpoint.id} is a group; depend on its tasks or "
                        f"milestones instead",
                        object_type=ObjectType.DEPENDENCY,
                        object_id=ident,
                        field=field_name,
                    )
                )
        if issues:
            raise ValidationFailed(issues)
        self._check_duplicate(ident, pred_node.id, succ_node.id, dep_type)
        path = _find_path(self._project, succ_node.id, pred_node.id)
        if path is not None:
            raise ValidationFailed([cycle_issue(tuple(sorted(path)))])
        dep = Dependency(ident, pred_node.id, succ_node.id, dep_type, lag_qty)
        self._commit(
            "add_dependency",
            self._replace(dependencies=(*self._project.dependencies, dep)),
            lambda conn, pk: row_ops.insert_dependency(conn, pk, dep),
            (ident,),
        )
        return ident

    def edit_dependency(
        self,
        dependency: str | _HasId,
        *,
        type: DependencyType | str | None = None,  # noqa: A002 - public API name
        lag: TimeQty | str | None = None,
    ) -> None:
        """Change a dependency's type and/or lag (the endpoints are fixed)."""
        old = self.dependency(dependency)
        if type is None and lag is None:
            raise ValueError("edit_dependency needs a new type and/or lag")
        dep_type = old.type if type is None else self._dep_type(type, old.id)
        lag_qty = (
            old.lag
            if lag is None
            else as_time_qty(lag, object_type=ObjectType.DEPENDENCY, object_id=old.id, field="lag")
        )
        self._check_duplicate(old.id, old.pred_id, old.succ_id, dep_type)
        new = dataclasses.replace(old, type=dep_type, lag=lag_qty)
        deps = tuple(new if d.id == old.id else d for d in self._project.dependencies)
        self._commit(
            "edit_dependency",
            self._replace(dependencies=deps),
            lambda conn, pk: row_ops.update_dependency(conn, pk, new),
            (old.id,),
        )

    def remove_dependency(self, dependency: str | _HasId) -> None:
        """Remove a dependency. Raises ``NotFound``."""
        old = self.dependency(dependency)
        deps = tuple(d for d in self._project.dependencies if d.id != old.id)
        self._commit(
            "remove_dependency",
            self._replace(dependencies=deps),
            lambda conn, pk: row_ops.delete_dependency(conn, pk, old.id),
            (old.id,),
        )

    # ------------------------------------------------------------------- deleting

    def _collect(
        self, node_ids: Iterable[str], resource_ids: Iterable[str]
    ) -> tuple[list[str], list[str], list[str], list[tuple[str, str]]]:
        project = self._project
        pending = [project.node(n).id for n in node_ids]
        doomed: set[str] = set()
        while pending:
            current = pending.pop()
            if current in doomed:
                continue
            doomed.add(current)
            pending.extend(c.id for c in project.children(current))
        resources = sorted({project.resource(r).id for r in resource_ids})
        deps: set[str] = set()
        assignments: set[tuple[str, str]] = set()
        for nid in doomed:
            deps.update(d.id for d in project.dependencies_from(nid))
            deps.update(d.id for d in project.dependencies_to(nid))
            assignments.update(a.key for a in project.assignments_for(nid))
        for rid in resources:
            assignments.update(a.key for a in project.assignments_for_resource(rid))
        ordered = [n.id for n in project.wbs_order() if n.id in doomed]
        return ordered, resources, sorted(deps), sorted(assignments)

    def delete_preview(
        self,
        ids: str | _HasId | Iterable[str | _HasId],
        *,
        resources: Iterable[str | _HasId] = (),
    ) -> DeletePreview:
        """List what deleting nodes ``ids`` (and ``resources``) would remove.

        Includes every descendant of a group and every dependency / assignment that
        would disappear with them. Nothing is changed. Raises ``NotFound``.
        """
        if isinstance(ids, str) or hasattr(ids, "id"):
            ids = [ids]  # type: ignore[list-item]
        node_ids = [_id_of(i) for i in ids]
        resource_ids = [_id_of(r, "resource") for r in resources]
        nodes, res, deps, assignments = self._collect(node_ids, resource_ids)
        self._previews = {t: p for t, p in self._previews.items() if p.revision == self._revision}
        preview = DeletePreview(
            token=uuid.uuid4().hex,
            requested=(*node_ids, *resource_ids),
            nodes=tuple(nodes),
            resources=tuple(res),
            dependencies=tuple(deps),
            assignments=tuple(assignments),
            revision=self._revision,
        )
        self._previews[preview.token] = preview
        return preview

    def delete_commit(self, token: str) -> None:
        """Perform the delete previewed under ``token``.

        Raises:
            Conflict: unknown token, or the revision changed since the preview (preview
                again to see the new impact).
        """
        preview = self._previews.get(token)
        if preview is None:
            raise Conflict("unknown or expired delete preview")
        if preview.revision != self._revision:
            self._previews.pop(token, None)
            raise Conflict(
                f"the workspace changed since the preview (revision {preview.revision} -> "
                f"{self._revision}); preview the delete again"
            )
        self._apply_delete("delete", preview.nodes, preview.resources, preview.dependencies)
        self._previews.pop(token, None)

    def _apply_delete(
        self,
        operation: str,
        nodes: Sequence[str],
        resources: Sequence[str],
        dependencies: Sequence[str],
    ) -> None:
        gone_nodes, gone_res = set(nodes), set(resources)
        project = self._project
        new = self._replace(
            nodes=tuple(n for n in project.nodes if n.id not in gone_nodes),
            resources=tuple(r for r in project.resources if r.id not in gone_res),
            assignments=tuple(
                a
                for a in project.assignments
                if a.task_id not in gone_nodes and a.resource_id not in gone_res
            ),
            dependencies=tuple(
                d
                for d in project.dependencies
                if d.pred_id not in gone_nodes and d.succ_id not in gone_nodes
            ),
        )

        def write(conn: sqlite3.Connection, pk: int) -> None:
            row_ops.delete_dependencies(conn, pk, dependencies)  # explicit; cascades also cover it
            row_ops.delete_nodes(conn, pk, nodes)
            row_ops.delete_resources(conn, pk, resources)

        self._commit(operation, new, write, (*nodes, *resources))


def open_workspace(path: str | Path = ":memory:", *, config: Config = DEFAULT_CONFIG) -> Workspace:
    """Open (creating and migrating if needed) the workspace database at ``path``.

    ``":memory:"`` gives a throwaway workspace. A new database starts with an empty
    untitled project dated today; call :meth:`Workspace.new_project` to set up a real one.

    A database file can be open in one workspace at a time (D12); the lock is released
    by :meth:`Workspace.close` (or leaving its ``with`` block, or the process ending).

    Raises:
        Conflict: the file is already open in another workspace (this or another
            process), or its schema is newer than this code.
    """
    lock = None if str(path) in (":memory:", "") else FileLock(path)
    if lock is not None:
        lock.acquire()
    try:
        conn = connect(path)
    except BaseException:
        if lock is not None:
            lock.release()
        raise
    try:
        migrate(conn)
        pk = repo.get_workspace_pk(conn)
        if pk is None:
            with transaction(conn):
                pk = repo.create_workspace(
                    conn, Project(id="p1", name="Untitled", start=dt.date.today())
                )
                row_ops.set_based_on(conn, pk, None, 1, 0)
        return Workspace(conn, pk, config, lock=lock)
    except BaseException:
        _release(conn, lock)
        raise
