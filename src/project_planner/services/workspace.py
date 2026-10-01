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
delegates to; marked ``DELEGATION POINT`` below):

* T32 ``services/compute.py`` + ``jobs.py``: ``schedule``, ``level_preview``,
  ``apply_leveling``, ``submit_*`` ... They read ``ws.project()``, call the engine and
  hand results to :meth:`Workspace.store_result` / :meth:`Workspace.discard_results`.
* T33 ``services/files.py``: ``save``, ``save_as``, ``load``, ``list_projects``,
  ``import_csv`` ... They use :attr:`Workspace.connection`, :attr:`Workspace.project_pk`,
  :meth:`Workspace.require_clean`, :meth:`Workspace.reload` after replacing the
  workspace rows, and :meth:`Workspace.mark_clean` after a save / load.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import sqlite3
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import Any, NoReturn, Protocol

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
from project_planner.engine.results import ScheduleResult
from project_planner.persistence import repositories as repo
from project_planner.persistence import row_ops
from project_planner.persistence.db import connect, transaction
from project_planner.persistence.migrations import migrate
from project_planner.persistence.records import RunKind
from project_planner.services.dto import DeletePreview, WorkspaceState
from project_planner.services.events import Callback, Event, EventBus, EventKind
from project_planner.services.result_store import ResultStore

__all__ = ["CalendarApi", "Workspace", "open_workspace"]

_ID_RE = re.compile(r"^([A-Za-z_]+)(\d+)$")

_NODE_PREFIX = {NodeKind.GROUP: "g", NodeKind.TASK: "t", NodeKind.MILESTONE: "m"}


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
    """Working project of one SQLite database. Create with :func:`open_workspace`."""

    def __init__(
        self, conn: sqlite3.Connection, project_pk: int, config: Config = DEFAULT_CONFIG
    ) -> None:
        self._conn = conn
        self._pk = project_pk
        self.config = config
        self.events = EventBus()
        self.calendar = CalendarApi(self)
        self._closed = False
        self._previews: dict[str, DeletePreview] = {}
        self._fp_cache: tuple[int, str, str] | None = None
        self._project: Project
        self._revision = 0
        self._based_on_revision = 1
        self._ids: _IdAllocator
        self._results = ResultStore(conn, project_pk, lambda: self._project)
        self._load_cache()

    # --------------------------------------------------------------- lifecycle

    def _load_cache(self) -> None:
        record = repo.get_project_record(self._conn, self._pk)
        self._project = repo.load_project(self._conn, self._pk)
        self._revision = record.revision
        self._based_on_revision = (
            record.based_on_revision if record.based_on_revision is not None else 1
        )
        self._ids = _IdAllocator(self._project)
        self._previews.clear()
        self._fp_cache = None
        self._results.reset(self._pk)

    def close(self) -> None:
        """Close the database connection (idempotent)."""
        if not self._closed:
            self._closed = True
            self._conn.close()

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

    def subscribe(self, callback: Callback) -> Callable[[], None]:
        """Register an event callback; returns the function that unsubscribes it."""
        return self.events.subscribe(callback)

    def __repr__(self) -> str:
        name = self._project.name
        return f"<Workspace {name!r} revision={self._revision}{' closed' if self._closed else ''}>"

    # ------------------------------------------------- service-extension surface

    @property
    def connection(self) -> sqlite3.Connection:
        """The workspace connection (for T33 file operations and T32 job storage)."""
        return self._conn

    @property
    def project_pk(self) -> int:
        """Primary key of the workspace project row."""
        return self._pk

    @property
    def results(self) -> ResultStore:
        """The result store (cache + persistence) of the workspace project."""
        return self._results

    @property
    def dirty(self) -> bool:
        """Whether the revision differs from the one last saved / loaded."""
        return self._revision != self._based_on_revision

    def require_clean(self, discard_unsaved: bool = False) -> None:
        """Raise ``UnsavedChanges`` if dirty and ``discard_unsaved`` is false."""
        if self.dirty and not discard_unsaved:
            raise UnsavedChanges()

    def mark_clean(self, based_on_pk: int | None = None) -> None:
        """Record that the current revision was just saved / loaded (T33 calls this)."""
        self._check_open()
        with transaction(self._conn):
            row_ops.set_based_on(self._conn, self._pk, based_on_pk, self._revision)
        self._based_on_revision = self._revision

    def reload(self, *, operation: str = "reload") -> None:
        """Re-read everything from the database after the rows were replaced elsewhere.

        T33 (load / import) replaces the workspace rows with repository functions, then
        calls this to refresh the cache and notify subscribers (``project_replaced``).
        """
        self._check_open()
        self._load_cache()
        self.events.emit(Event(EventKind.PROJECT_REPLACED, self._revision, (), operation))

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

    def state(self) -> WorkspaceState:
        """Revision, dirty flag and staleness of the stored results."""
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
        return self._results.current()

    # DELEGATION POINT (T32, ``services/compute.py``): ``schedule()``,
    # ``level_preview()``, ``submit_schedule()``, ``submit_leveling_preview()``,
    # ``apply_leveling()``, ``discard_leveling()``, ``reset_to_dependency_schedule()``.
    # They are added here as thin methods calling the compute module, and persist
    # with ``store_result`` / ``discard_results`` below.

    # DELEGATION POINT (T33, ``services/files.py``): ``save()``, ``save_as()``,
    # ``load()``, ``list_projects()``, ``delete_project()``, ``import_csv()``,
    # ``export_csv()``. ``new_project`` below is already implemented because the
    # calendar tests need it; T33 may wrap it.

    def store_result(self, result: ScheduleResult) -> None:
        """Persist ``result`` as the stored run of its kind (see ``result_store`` rules).

        Does not bump the revision (results are not part of the definition); emits
        ``result_stored``. The result may already be stale; ``state()`` reports that.
        """
        self._check_open()
        self._results.store(result)
        self.events.emit(
            Event(EventKind.RESULT_STORED, self._revision, (result.kind,), "store_result")
        )

    def discard_results(self, kinds: Iterable[RunKind | str] | None = None) -> None:
        """Delete stored results of ``kinds`` (all when ``None``); emits ``result_stored``."""
        self._check_open()
        wanted = None if kinds is None else tuple(RunKind(k) for k in kinds)
        self._results.clear(wanted)
        ids = tuple(str(k) for k in (wanted if wanted is not None else tuple(RunKind)))
        self.events.emit(Event(EventKind.RESULT_STORED, self._revision, ids, "discard_results"))

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
        with transaction(self._conn):
            write(self._conn, self._pk)
            revision = repo.bump_revision(self._conn, self._pk)
            self._results.write_plan(plan)
        self._project = new_project
        self._revision = revision
        self._results.apply_plan(plan)
        for ident in ids:
            self._ids.note(ident)
        self.events.emit(Event(EventKind.EDITED, revision, tuple(ids), operation))

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
        self.require_clean(discard_unsaved)
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
        with transaction(self._conn):
            repo.save_project(self._conn, self._pk, project)
            repo.delete_runs(self._conn, self._pk)
            row_ops.set_name(self._conn, self._pk, name)
            revision = repo.bump_revision(self._conn, self._pk)
            row_ops.set_based_on(self._conn, self._pk, None, revision)
        self._load_cache()
        self.events.emit(Event(EventKind.PROJECT_REPLACED, self._revision, (), "new_project"))

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
        if value <= 0 or value > self.config.max_assignment_percent:
            issues.append(
                Issue.error(
                    "ASSIGN_PERCENT_RANGE",
                    f"assignment {key}: percent {value} must be greater than 0 and at most "
                    f"{self.config.max_assignment_percent}",
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
    """
    conn = connect(path)
    try:
        migrate(conn)
        pk = repo.get_workspace_pk(conn)
        if pk is None:
            with transaction(conn):
                pk = repo.create_workspace(
                    conn, Project(id="p1", name="Untitled", start=dt.date.today())
                )
                row_ops.set_based_on(conn, pk, None, 1)
        return Workspace(conn, pk, config)
    except BaseException:
        conn.close()
        raise
