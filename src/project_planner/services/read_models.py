"""Read models (T34): UI / notebook queries over the workspace.

Every function is read-only, takes the :class:`Workspace` first and returns plain frozen
dataclasses.  Work is done from ``ws.project()`` and ``ws.result()``.  When there is no
stored result, rows are still produced but their calculated fields are ``None``; the
exception is :func:`cost_report`, which raises ``Conflict("no schedule calculated")``.

A stored result may be stale (``ws.state().stale_dates``); read models then show the
stored numbers next to the current definition and never raise because of it.

Display ordering and indices: :func:`wbs_rows` and :func:`gantt_rows` share one
visible-row list, so ``GanttRow.row_index`` equals the absolute index of the same row in
the full visible list (``offset`` + position within the page).

Caching: the expensive per-workspace structures (WBS numbers, visible-row lists, the
working axis) are cached per workspace and invalidated when ``ws.state().revision`` or
the identity of the stored result changes.

T35 wires these onto ``Workspace`` as methods: ``cost_report``, ``task_details``,
``loading``, ``wbs_rows``, ``gantt_rows``, ``dependency_links``, ``nonworking_ranges``,
``project_summary``.
"""

from __future__ import annotations

import datetime as dt
import weakref
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from project_planner.engine.calendar import WorkingAxis
from project_planner.engine.cost import AssignmentCost
from project_planner.engine.errors import Conflict, Issue, NotFound, ObjectType, ValidationFailed
from project_planner.engine.loading import LoadSegment, aggregate
from project_planner.engine.model import (
    CalendarSettings,
    DependencyType,
    NodeKind,
    Project,
    SizingMode,
    TimeQty,
    TimeUnit,
    WbsNode,
    WorkUnit,
)
from project_planner.engine.results import NodeResult, ScheduleResult
from project_planner.engine.rollup import wbs_numbers
from project_planner.services.workspace import Workspace

__all__ = [
    "CostNodeRow",
    "CostReportAssignmentRow",
    "CostReportView",
    "DependencyInfo",
    "GanttRow",
    "Link",
    "LoadingBucketRow",
    "LoadingSegmentRow",
    "LoadingView",
    "ProjectSummary",
    "TaskAssignmentRow",
    "TaskDetails",
    "WbsPage",
    "WbsRow",
    "cost_report",
    "dependency_links",
    "gantt_rows",
    "loading",
    "nonworking_ranges",
    "project_summary",
    "task_details",
    "wbs_rows",
]

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)
_MAX_VISIBLE_CACHE = 8

Granularity = Literal["segments", "day", "week"]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class _Index:
    """Derived structures of one (revision, result) pair of a workspace."""

    def __init__(self, revision: int, project: Project, result: ScheduleResult | None) -> None:
        self.revision = revision
        self.project = project
        self.result = result  # strong ref keeps ``is`` comparison valid
        self._numbers: dict[str, str] | None = None
        self._axis: WorkingAxis | None = None
        self._depth: dict[str, int] = {}
        self.visible: dict[tuple[str | None, frozenset[str]], list[tuple[WbsNode, int]]] = {}

    @property
    def numbers(self) -> dict[str, str]:
        if self._numbers is None:
            self._numbers = wbs_numbers(self.project)
        return self._numbers

    @property
    def axis(self) -> WorkingAxis:
        if self._axis is None:
            self._axis = WorkingAxis(self.project.calendar, self.project.start)
        return self._axis

    def depth_of(self, node_id: str) -> int:
        cached = self._depth.get(node_id)
        if cached is not None:
            return cached
        depth = 0
        cur = self.project.node(node_id)
        while cur.parent_id is not None and depth <= len(self.project.nodes):
            depth += 1
            cur = self.project.node(cur.parent_id)
        self._depth[node_id] = depth
        return depth


_CACHE: weakref.WeakKeyDictionary[Workspace, _Index] = weakref.WeakKeyDictionary()


def _index(ws: Workspace) -> _Index:
    revision = ws.state().revision
    result = ws.result()
    idx = _CACHE.get(ws)
    if idx is not None and idx.revision == revision and idx.result is result:
        return idx
    idx = _Index(revision, ws.project(), result)
    _CACHE[ws] = idx
    return idx


def _id_of(value: object) -> str:
    ident = getattr(value, "id", value)
    return str(ident)


# ---------------------------------------------------------------------------
# Cost report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostReportAssignmentRow:
    resource_id: str
    percent: Decimal
    work_qty: Decimal
    work_unit: WorkUnit
    rate_per_unit: Decimal | None
    cost: Decimal | None
    cost_complete: bool


@dataclass(frozen=True)
class CostNodeRow:
    """A task, milestone or group row of the cost report (groups have no assignments)."""

    node_id: str
    kind: NodeKind
    work_qty: Decimal
    work_unit: WorkUnit
    cost: Decimal
    complete: bool
    assignments: tuple[CostReportAssignmentRow, ...]
    missing_rate_resources: tuple[str, ...]


@dataclass(frozen=True)
class CostReportView:
    unit: WorkUnit
    total_cost: Decimal
    work_qty: Decimal
    complete: bool
    missing_rate_resources: tuple[str, ...]
    nodes: tuple[CostNodeRow, ...]
    """Rows in WBS order."""

    def node(self, node_id: str) -> CostNodeRow:
        """Row of a node; raises ``NotFound`` for an unknown ID."""
        node_id = _id_of(node_id)
        try:
            return self._by_id()[node_id]
        except KeyError:
            raise NotFound(ObjectType.NODE.value, node_id) from None

    def _by_id(self) -> dict[str, CostNodeRow]:
        cached: dict[str, CostNodeRow] | None = self.__dict__.get("_map")
        if cached is None:
            cached = {r.node_id: r for r in self.nodes}
            object.__setattr__(self, "_map", cached)
        return cached


def _assignment_row(a: AssignmentCost, unit: WorkUnit, per: Decimal) -> CostReportAssignmentRow:
    rate = None if a.hourly_rate is None else a.hourly_rate * per
    return CostReportAssignmentRow(
        a.resource_id, a.percent, a.assignment_hours / per, unit, rate, a.cost, a.cost_complete
    )


def _report_unit(project: Project, unit: WorkUnit | str | None) -> WorkUnit:
    """``unit`` as a ``WorkUnit`` (default: the project's); ``ValidationFailed`` otherwise."""
    if unit is None:
        return project.cost_report_unit
    try:
        return WorkUnit(unit)
    except ValueError:
        allowed = ", ".join(u.value for u in WorkUnit)
        raise ValidationFailed(
            [
                Issue.error(
                    "COST_BAD_UNIT",
                    f"unit {unit!r} is not one of: {allowed}",
                    object_type=ObjectType.PROJECT,
                    object_id=project.id,
                    field="unit",
                )
            ]
        ) from None


def cost_report(ws: Workspace, unit: WorkUnit | str | None = None) -> CostReportView:
    """Cost report in ``unit`` (default: the project's ``cost_report_unit``).

    Money is identical in every unit; only work quantities and rates per unit change.

    Raises:
        Conflict: ``"no schedule calculated"`` when no result is stored.
        ValidationFailed: ``COST_BAD_UNIT`` for an unknown ``unit``.
    """
    idx = _index(ws)
    result = idx.result
    if result is None:
        raise Conflict("no schedule calculated")
    project = idx.project
    u = _report_unit(project, unit)
    per = u.hours_per_unit(project.calendar)
    costs = result.costs
    rows: list[CostNodeRow] = []
    for node in project.wbs_order():
        tc = costs.tasks.get(node.id)
        if tc is not None:
            rows.append(
                CostNodeRow(
                    node.id,
                    node.kind,
                    tc.hours / per,
                    u,
                    tc.cost,
                    tc.cost_complete,
                    tuple(_assignment_row(a, u, per) for a in tc.assignments),
                    tc.missing_rate_resources,
                )
            )
            continue
        gc = costs.groups.get(node.id)
        if gc is not None:
            rows.append(
                CostNodeRow(
                    node.id, node.kind, gc.hours / per, u, gc.cost, gc.cost_complete, (), ()
                )
            )
    return CostReportView(
        unit=u,
        total_cost=costs.total,
        work_qty=costs.total_hours / per,
        complete=costs.complete,
        missing_rate_resources=costs.missing_rate_resources,
        nodes=tuple(rows),
    )


# ---------------------------------------------------------------------------
# Task details
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyInfo:
    dependency_id: str
    pred_id: str
    pred_name: str
    succ_id: str
    succ_name: str
    type: DependencyType
    lag: TimeQty
    lag_days: Decimal


@dataclass(frozen=True)
class TaskAssignmentRow:
    resource_id: str
    resource_name: str
    percent: Decimal
    assignment_days: Decimal | None
    work_qty: Decimal | None
    work_unit: WorkUnit
    rate_per_unit: Decimal | None
    cost: Decimal | None
    cost_complete: bool | None


@dataclass(frozen=True)
class TaskDetails:
    """One node in full; calculated fields are ``None`` without a result."""

    id: str
    name: str
    kind: NodeKind
    parent_id: str | None
    wbs_number: str
    sizing_mode: SizingMode
    sizing: TimeQty | None
    status: str | None
    reason: str | None
    start: dt.datetime | None
    finish: dt.datetime | None
    duration_days: Decimal | None
    effort_days: Decimal | None
    leveling_delay_days: Decimal | None
    unit: WorkUnit
    work_qty: Decimal | None
    cost: Decimal | None
    cost_complete: bool | None
    predecessors: tuple[DependencyInfo, ...]
    successors: tuple[DependencyInfo, ...]
    assignments: tuple[TaskAssignmentRow, ...]


def _lag_days(
    project: Project, result: ScheduleResult | None, dep_id: str, lag: TimeQty
) -> Decimal:
    if result is not None:
        row = result.dependencies.get(dep_id)
        if row is not None:
            return row.lag_days
    if lag.unit is TimeUnit.DAYS:
        return lag.value
    return lag.value * 60 / Decimal(project.calendar.minutes_per_day)


def task_details(ws: Workspace, task_id: str, unit: WorkUnit | str | None = None) -> TaskDetails:
    """Details of one node.  Raises ``NotFound`` (unknown ID), ``ValidationFailed`` (bad unit)."""
    idx = _index(ws)
    project, result = idx.project, idx.result
    node = project.node(_id_of(task_id))
    u = _report_unit(project, unit)
    per = u.hours_per_unit(project.calendar)
    nr = result.nodes.get(node.id) if result is not None else None

    def dep_info(d_id: str) -> DependencyInfo:
        d = project.dependency(d_id)
        return DependencyInfo(
            d.id,
            d.pred_id,
            project.node(d.pred_id).name,
            d.succ_id,
            project.node(d.succ_id).name,
            d.type,
            d.lag,
            _lag_days(project, result, d.id, d.lag),
        )

    costs = result.costs if result is not None else None
    task_cost = costs.tasks.get(node.id) if costs is not None else None
    by_resource = {a.resource_id: a for a in task_cost.assignments} if task_cost else {}
    rows: list[TaskAssignmentRow] = []
    for a in project.assignments_for(node.id):
        res = project.resource(a.resource_id)
        ac = by_resource.get(a.resource_id)
        ar = result.assignments.get((node.id, a.resource_id)) if result is not None else None
        rows.append(
            TaskAssignmentRow(
                a.resource_id,
                res.name,
                a.percent,
                None if ar is None else ar.assignment_days,
                None if ac is None else ac.assignment_hours / per,
                u,
                None if ac is None or ac.hourly_rate is None else ac.hourly_rate * per,
                None if ac is None else ac.cost,
                None if ac is None else ac.cost_complete,
            )
        )

    work_qty: Decimal | None = None
    if task_cost is not None:
        work_qty = task_cost.hours / per
    elif costs is not None and node.id in costs.groups:
        work_qty = costs.groups[node.id].hours / per

    return TaskDetails(
        id=node.id,
        name=node.name,
        kind=node.kind,
        parent_id=node.parent_id,
        wbs_number=idx.numbers.get(node.id, ""),
        sizing_mode=node.sizing_mode,
        sizing=node.sizing,
        status=None if nr is None else nr.status,
        reason=None if nr is None else nr.reason,
        start=None if nr is None else nr.start,
        finish=None if nr is None else nr.finish,
        duration_days=None if nr is None else nr.duration_days,
        effort_days=None if nr is None else nr.effort_days,
        leveling_delay_days=None if nr is None else nr.leveling_delay_days,
        unit=u,
        work_qty=work_qty,
        cost=None if nr is None else nr.cost,
        cost_complete=None if nr is None else nr.cost_complete,
        predecessors=tuple(dep_info(d.id) for d in project.dependencies_to(node.id)),
        successors=tuple(dep_info(d.id) for d in project.dependencies_from(node.id)),
        assignments=tuple(rows),
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadingSegmentRow:
    start: dt.datetime
    end: dt.datetime
    """Finish convention: a segment ending on a day boundary ends at the previous 17:00."""
    percent: Decimal
    task_ids: tuple[str, ...]
    overloaded: bool


@dataclass(frozen=True)
class LoadingBucketRow:
    period_start: dt.date
    """The day, or the Monday of the ISO week."""
    assigned_days: Decimal
    average_percent: Decimal
    peak_percent: Decimal
    overloaded: bool
    """True when the peak exceeds 100 %."""


@dataclass(frozen=True)
class LoadingView:
    resource_id: str
    resource_name: str
    granularity: Granularity
    capacity_percent: Decimal
    segments: tuple[LoadingSegmentRow, ...]
    buckets: tuple[LoadingBucketRow, ...]
    """Empty for ``granularity="segments"``; only periods with load are present."""


def _clip(segments: Iterable[LoadSegment], lo: int, hi: int) -> list[LoadSegment]:
    out: list[LoadSegment] = []
    for s in segments:
        a, b = max(s.start, lo), min(s.end, hi)
        if a < b:
            out.append(LoadSegment(s.resource_id, a, b, s.percent, s.task_ids))
    return out


def loading(
    ws: Workspace,
    resource_id: str,
    *,
    time_window: tuple[dt.datetime, dt.datetime] | None = None,
    granularity: Granularity = "segments",
) -> LoadingView:
    """Loading of one resource from the current result (empty without one).

    ``segments`` are always filled; ``buckets`` only for ``"day"`` / ``"week"``.
    ``time_window`` clips segments (and therefore buckets) to ``[start, end]``.

    Raises:
        NotFound: unknown resource.
        ValueError: unknown ``granularity``.
    """
    if granularity not in ("segments", "day", "week"):
        raise ValueError(f"granularity must be 'segments', 'day' or 'week', got {granularity!r}")
    idx = _index(ws)
    rid = _id_of(resource_id)
    res = idx.project.resource(rid)
    axis = idx.axis
    segs: list[LoadSegment] = (
        list(idx.result.loading.get(rid, ())) if idx.result is not None else []
    )
    if time_window is not None:
        segs = _clip(segs, axis.to_axis(time_window[0]), axis.to_axis(time_window[1]))
    rows = tuple(
        LoadingSegmentRow(
            axis.to_datetime(s.start, "start"),
            axis.to_datetime(s.end, "finish"),
            s.percent,
            s.task_ids,
            s.overloaded,
        )
        for s in segs
    )
    buckets: tuple[LoadingBucketRow, ...] = ()
    if granularity != "segments":
        buckets = tuple(
            LoadingBucketRow(
                b.period_start,
                b.assigned_days,
                b.average_percent,
                b.peak_percent,
                b.peak_percent > _HUNDRED,
            )
            for b in aggregate(segs, axis, granularity)
        )
    return LoadingView(rid, res.name, granularity, _HUNDRED, rows, buckets)


# ---------------------------------------------------------------------------
# WBS rows / Gantt rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WbsRow:
    id: str
    name: str
    kind: NodeKind
    depth: int
    wbs_number: str
    has_children: bool
    expanded: bool
    sizing: TimeQty | None
    duration_days: Decimal | None
    effort_days: Decimal | None
    start: dt.datetime | None
    finish: dt.datetime | None
    assignments_summary: str
    cost: Decimal | None
    cost_complete: bool | None
    status: str | None


@dataclass(frozen=True)
class WbsPage:
    rows: tuple[WbsRow, ...]
    total: int
    """Number of visible rows (before windowing)."""


@dataclass(frozen=True)
class GanttRow:
    row_index: int
    id: str
    kind: NodeKind
    start: dt.datetime | None
    finish: dt.datetime | None
    is_milestone: bool
    is_summary: bool
    leveling_delay_days: Decimal | None


@dataclass(frozen=True)
class Link:
    pred_id: str
    succ_id: str
    type: DependencyType
    lag_days: Decimal


def _visible(
    idx: _Index, parent_id: str | None, expanded: frozenset[str]
) -> list[tuple[WbsNode, int]]:
    key = (parent_id, expanded)
    cached = idx.visible.get(key)
    if cached is not None:
        return cached
    project = idx.project
    if parent_id is None:
        roots = project.children(None)
        base = 0
    else:
        parent = project.node(parent_id)
        roots = project.children(parent_id) if parent.kind is NodeKind.GROUP else ()
        base = idx.depth_of(parent_id) + 1
    out: list[tuple[WbsNode, int]] = []
    stack = [(n, base) for n in reversed(roots)]
    while stack:
        node, depth = stack.pop()
        out.append((node, depth))
        if node.id in expanded:
            kids = project.children(node.id)
            if kids:
                stack.extend((k, depth + 1) for k in reversed(kids))
    if len(idx.visible) >= _MAX_VISIBLE_CACHE:
        idx.visible.clear()
    idx.visible[key] = out
    return out


def _window(
    items: list[tuple[WbsNode, int]], offset: int, limit: int | None
) -> list[tuple[int, WbsNode, int]]:
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError("offset and limit must be >= 0")
    end = len(items) if limit is None else offset + limit
    return [(i, n, d) for i, (n, d) in enumerate(items[offset:end], start=offset)]


def _pct_text(p: Decimal) -> str:
    return format(p.normalize(), "f")


def wbs_rows(
    ws: Workspace,
    *,
    parent_id: str | None = None,
    expanded_ids: Collection[str] = frozenset(),
    offset: int = 0,
    limit: int | None = None,
) -> WbsPage:
    """Visible WBS rows in display order, windowed by ``offset`` / ``limit``.

    Only groups in ``expanded_ids`` show their children.  ``parent_id`` restricts the
    list to the children of that group (shown even if the group itself is not in
    ``expanded_ids``); ``depth`` stays the absolute depth in the tree.  Calculated
    fields are ``None`` for rows without a result (or unscheduled dates).

    Raises:
        NotFound: unknown ``parent_id``.
    """
    idx = _index(ws)
    expanded = expanded_ids if isinstance(expanded_ids, frozenset) else frozenset(expanded_ids)
    items = _visible(idx, parent_id, expanded)
    project = idx.project
    nodes = idx.result.nodes if idx.result is not None else {}
    rows: list[WbsRow] = []
    for _, node, depth in _window(items, offset, limit):
        nr: NodeResult | None = nodes.get(node.id)
        has_children = bool(project.children(node.id))
        assigns = project.assignments_for(node.id)
        summary = ", ".join(
            f"{project.resource(a.resource_id).name} {_pct_text(a.percent)}%" for a in assigns
        )
        rows.append(
            WbsRow(
                id=node.id,
                name=node.name,
                kind=node.kind,
                depth=depth,
                wbs_number=idx.numbers.get(node.id, ""),
                has_children=has_children,
                expanded=has_children and node.id in expanded,
                sizing=node.sizing if node.kind is NodeKind.TASK else None,
                duration_days=None if nr is None else nr.duration_days,
                effort_days=None if nr is None else nr.effort_days,
                start=None if nr is None else nr.start,
                finish=None if nr is None else nr.finish,
                assignments_summary=summary,
                cost=None if nr is None else nr.cost,
                cost_complete=None if nr is None else nr.cost_complete,
                status=None if nr is None else nr.status,
            )
        )
    return WbsPage(tuple(rows), len(items))


def gantt_rows(
    ws: Workspace,
    *,
    expanded_ids: Collection[str] = frozenset(),
    offset: int = 0,
    limit: int | None = None,
    time_window: tuple[dt.datetime, dt.datetime] | None = None,
) -> list[GanttRow]:
    """Gantt rows with the same visible ordering and indices as :func:`wbs_rows`.

    ``offset`` / ``limit`` select the row window first; ``time_window`` then drops rows
    whose bar does not overlap it (rows without dates are dropped too), so fewer than
    ``limit`` rows may come back.  ``row_index`` is the absolute visible-row index.
    """
    idx = _index(ws)
    expanded = expanded_ids if isinstance(expanded_ids, frozenset) else frozenset(expanded_ids)
    items = _visible(idx, None, expanded)
    nodes = idx.result.nodes if idx.result is not None else {}
    out: list[GanttRow] = []
    for i, node, _ in _window(items, offset, limit):
        nr = nodes.get(node.id)
        start = None if nr is None else nr.start
        finish = None if nr is None else nr.finish
        if time_window is not None and (
            start is None or finish is None or finish < time_window[0] or start > time_window[1]
        ):
            continue
        out.append(
            GanttRow(
                i,
                node.id,
                node.kind,
                start,
                finish,
                node.kind is NodeKind.MILESTONE,
                node.kind is NodeKind.GROUP,
                None if nr is None else nr.leveling_delay_days,
            )
        )
    return out


def dependency_links(ws: Workspace, ids: Iterable[object]) -> list[Link]:
    """Dependencies whose both ends are among ``ids`` (IDs or objects with ``.id``)."""
    idx = _index(ws)
    project = idx.project
    wanted = {_id_of(i) for i in ids}
    links: list[Link] = []
    for pid in sorted(wanted):
        if not project.has_node(pid):
            continue
        for d in project.dependencies_from(pid):
            if d.succ_id in wanted:
                links.append(
                    Link(d.pred_id, d.succ_id, d.type, _lag_days(project, idx.result, d.id, d.lag))
                )
    return links


# ---------------------------------------------------------------------------
# Calendar shading and project summary
# ---------------------------------------------------------------------------


def nonworking_ranges(ws: Workspace, start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Inclusive, merged date ranges of nonworking days between ``start`` and ``end``."""
    axis = _index(ws).axis
    zero = dt.time()
    return axis.nonworking_ranges(dt.datetime.combine(start, zero), dt.datetime.combine(end, zero))


@dataclass(frozen=True)
class ProjectSummary:
    name: str
    start: dt.date
    currency: str
    cost_report_unit: WorkUnit
    calendar: CalendarSettings
    groups: int
    tasks: int
    milestones: int
    resources: int
    assignments: int
    dependencies: int
    revision: int
    dirty: bool
    has_result: bool
    result_kind: str | None
    complete: bool | None
    project_finish: dt.datetime | None
    working_span_days: Decimal | None
    elapsed_span_calendar_days: Decimal | None
    total_cost: Decimal | None
    cost_complete: bool | None
    stale_dates: bool
    stale_costs: bool
    has_preview: bool


def project_summary(ws: Workspace) -> ProjectSummary:
    """Definition counts, settings, headline results and staleness flags."""
    idx = _index(ws)
    p = idx.project
    state = ws.state()
    r = idx.result
    counts = {k: 0 for k in NodeKind}
    for n in p.nodes:
        counts[n.kind] += 1
    return ProjectSummary(
        name=p.name,
        start=p.start,
        currency=p.currency,
        cost_report_unit=p.cost_report_unit,
        calendar=p.calendar.settings(),
        groups=counts[NodeKind.GROUP],
        tasks=counts[NodeKind.TASK],
        milestones=counts[NodeKind.MILESTONE],
        resources=len(p.resources),
        assignments=len(p.assignments),
        dependencies=len(p.dependencies),
        revision=state.revision,
        dirty=state.dirty,
        has_result=r is not None,
        result_kind=None if r is None else r.kind,
        complete=None if r is None else r.complete,
        project_finish=None if r is None else r.project_finish,
        working_span_days=None if r is None else r.working_span_days,
        elapsed_span_calendar_days=None if r is None else r.elapsed_span_calendar_days,
        total_cost=None if r is None else r.total_cost,
        cost_complete=None if r is None else r.cost_complete,
        stale_dates=state.stale_dates,
        stale_costs=state.stale_costs,
        has_preview=state.has_preview,
    )
