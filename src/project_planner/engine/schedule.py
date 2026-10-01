"""Engine orchestration: schedule, level, recost (plan section 3, T19).

Pipeline of :func:`schedule`: validate -> sizing -> working axis -> topological
order -> forward pass -> rollup -> loading -> cost -> result assembly.

Conventions chosen here:

* A zero-duration node (milestone, zero-length task) shows the same datetime for
  start and finish, using the "finish" convention (``17:00`` of the last working
  day when it falls on a day boundary).
* Cost uses the durations of *scheduled* nodes only; an unscheduled task has cost
  0 and is cost-incomplete.
* ``working_span`` = finish axis minute of the latest-finishing scheduled node;
  ``elapsed_span_calendar_days`` = inclusive count of calendar dates from the
  axis origin (first working date) to the date of ``project_finish``.  Both are
  ``None`` unless the result is complete with at least one scheduled node.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from project_planner.engine.calendar import WorkingAxis
from project_planner.engine.config import DEFAULT_CONFIG, Config
from project_planner.engine.cost import CostReport, CostResult, compute_costs
from project_planner.engine.cost import cost_report as _cost_report
from project_planner.engine.errors import Conflict, Issue, Severity, ValidationFailed
from project_planner.engine.fingerprint import cost_fp, schedule_fp
from project_planner.engine.forward_pass import NodeTiming, forward_pass
from project_planner.engine.leveling import LevelingOutcome
from project_planner.engine.leveling import level as _level
from project_planner.engine.loading import compute_loading
from project_planner.engine.model import NodeKind, Project, WorkUnit
from project_planner.engine.network import topological_order
from project_planner.engine.results import (
    AssignmentExportRow,
    AssignmentResult,
    DependencyResult,
    LevelingResult,
    NodeExportRow,
    NodeResult,
    ResultKind,
    ScheduleResult,
)
from project_planner.engine.rollup import rollup, wbs_numbers
from project_planner.engine.sizing import TaskSizing, compute_sizing
from project_planner.engine.validation import validate

__all__ = ["cost_report", "level", "recost", "schedule", "with_kind"]

_ZERO = Decimal(0)
_SIZING_CODES = frozenset({"TASK_UNSIZED", "TASK_NO_CAPACITY"})


class _Prepared:
    """Validated project with sizing, axis, forward pass and merged issues."""

    def __init__(self, project: Project, config: Config) -> None:
        found = validate(project, config)
        errors = [i for i in found if i.severity is Severity.ERROR and i.code not in _SIZING_CODES]
        if errors:
            raise ValidationFailed(errors)
        self.sizing: dict[str, TaskSizing] = compute_sizing(project)
        self.axis = WorkingAxis(project.calendar, project.start)
        self.mpd = self.axis.minutes_per_day
        order = topological_order(project)
        self.base = forward_pass(project, self.sizing, minutes_per_day=self.mpd, order=order)
        merged: list[Issue] = list(found)
        for sz in self.sizing.values():
            merged.extend(sz.issues)
        merged.extend(self.axis.issues)
        self.issues = _dedupe(merged)


def _dedupe(issues: list[Issue]) -> tuple[Issue, ...]:
    """One issue per (code, object_id) for the sizing codes, preferring the error."""
    best: dict[tuple[str, str | None], Issue] = {}
    for i in issues:
        if i.code not in _SIZING_CODES:
            continue
        key = (i.code, i.object_id)
        cur = best.get(key)
        if cur is None or (cur.severity is not Severity.ERROR and i.severity is Severity.ERROR):
            best[key] = i
    out: list[Issue] = []
    emitted: set[tuple[str, str | None]] = set()
    for i in issues:
        if i.code in _SIZING_CODES:
            key = (i.code, i.object_id)
            if key in emitted:
                continue
            emitted.add(key)
            out.append(best[key])
        else:
            out.append(i)
    return tuple(out)


def _days(minutes: int | Decimal | None, mpd: int) -> Decimal | None:
    return None if minutes is None else Decimal(minutes) / mpd


def _datetimes(axis: WorkingAxis, start: int, finish: int) -> tuple[datetime, datetime]:
    if finish == start:
        both = axis.to_datetime(start, "finish")
        return both, both
    return axis.to_datetime(start, "start"), axis.to_datetime(finish, "finish")


def _assemble(
    project: Project,
    prep: _Prepared,
    timings: Mapping[str, NodeTiming],
    kind: ResultKind,
    delays: Mapping[str, int],
    config: Config,
) -> ScheduleResult:
    del config
    mpd, axis = prep.mpd, prep.axis
    intervals: dict[str, tuple[int, int] | None] = {}
    durations: dict[str, int | None] = {}
    for nid, t in timings.items():
        if t.status == "scheduled" and t.start is not None and t.finish is not None:
            intervals[nid] = (t.start, t.finish)
            durations[nid] = t.finish - t.start
        else:
            intervals[nid] = None
            durations[nid] = None
    efforts = {nid: sz.effort_person_minutes for nid, sz in prep.sizing.items()}
    groups = rollup(project, intervals, efforts)
    loading = compute_loading(project, intervals)
    costs = compute_costs(project, durations)
    numbers = wbs_numbers(project)

    nodes: dict[str, NodeResult] = {}
    sdt: datetime | None
    fdt: datetime | None
    for n in project.nodes:
        number = numbers.get(n.id, "")
        if n.kind is NodeKind.GROUP:
            g = groups[n.id]
            gc = costs.groups[n.id]
            if g.start is not None and g.finish is not None:
                sdt, fdt = _datetimes(axis, g.start, g.finish)
                dur: int | None = g.finish - g.start
            else:
                sdt = fdt = None
                dur = None
            reason = (
                None
                if g.complete
                else "incomplete: " + ", ".join(g.unscheduled_descendants)
                if g.unscheduled_descendants
                else "incomplete: effort unknown"
            )
            nodes[n.id] = NodeResult(
                n.id, n.kind, "group", g.start is not None, sdt, fdt, g.start, g.finish,
                dur, _days(dur, mpd), g.effort_person_minutes,
                _days(g.effort_person_minutes, mpd), 0, _ZERO, gc.cost, gc.cost_complete,
                reason, number,
            )  # fmt: skip
            continue
        t = timings[n.id]
        tc = costs.tasks[n.id]
        iv = intervals[n.id]
        delay = delays.get(n.id, 0)
        eff = prep.sizing[n.id].effort_person_minutes
        if iv is not None:
            sdt, fdt = _datetimes(axis, iv[0], iv[1])
            dur = iv[1] - iv[0]
            smin: int | None = iv[0]
            fmin: int | None = iv[1]
        else:
            sdt = fdt = None
            dur = smin = fmin = None
        nodes[n.id] = NodeResult(
            n.id, n.kind, t.status, iv is not None, sdt, fdt, smin, fmin,
            dur, _days(dur, mpd), eff, _days(eff, mpd), delay, Decimal(delay) / mpd,
            tc.cost, tc.cost_complete, t.reason, number,
        )  # fmt: skip

    task_ids = tuple(n.id for n in project.wbs_order() if n.kind is not NodeKind.GROUP)
    complete = all(nodes[i].scheduled for i in task_ids)
    ends = [iv[1] for iv in intervals.values() if iv is not None]
    finish_dt: datetime | None = None
    span: int | None = None
    elapsed: Decimal | None = None
    if complete and ends:
        span = max(ends)
        finish_dt = axis.to_datetime(span, "finish")
        elapsed = Decimal((finish_dt.date() - axis.origin).days + 1)

    result = ScheduleResult(
        kind=kind,
        nodes=nodes,
        dependencies={
            d.id: DependencyResult(d.id, (m := d.lag.to_minutes(mpd)), Decimal(m) / mpd, d.lag)
            for d in project.dependencies
        },
        task_ids=task_ids,
        loading=loading,
        issues=prep.issues,
        complete=complete,
        project_start=axis.to_datetime(0, "start"),
        project_finish=finish_dt,
        working_span_minutes=span,
        working_span_days=_days(span, mpd),
        elapsed_span_calendar_days=elapsed,
        effort_days=_days(sum((e for e in efforts.values() if e is not None), _ZERO), mpd),
        schedule_fp=schedule_fp(project),
        cost_fp=cost_fp(project),
        minutes_per_day=mpd,
        **_cost_views(project, nodes, costs, prep.issues, mpd),
    )
    return result


def _cost_views(
    project: Project,
    nodes: Mapping[str, NodeResult],
    costs: CostResult,
    issues: tuple[Issue, ...],
    mpd: int,
) -> dict[str, Any]:
    """Every ScheduleResult field that depends on costs or the report unit."""
    report = _cost_report(costs, project)
    assignments = {
        (a.task_id, a.resource_id): AssignmentResult(
            a.task_id,
            a.resource_id,
            a.percent,
            a.assignment_minutes,
            a.assignment_minutes / mpd,
            a.assignment_hours,
            a.hourly_rate,
            a.cost,
            a.cost_complete,
        )
        for t in costs.tasks.values()
        for a in t.assignments
    }
    codes: dict[str, list[str]] = {}
    for i in issues:
        if i.object_id is not None:
            codes.setdefault(i.object_id, []).append(i.code)
    unit = report.unit.value
    node_rows: list[NodeExportRow] = []
    for nid, row in nodes.items():
        trow = report.tasks.get(nid)
        grow = report.groups.get(nid)
        qty = trow.work_qty if trow is not None else grow.work_qty if grow is not None else None
        node_rows.append(
            NodeExportRow(
                nid,
                row.start,
                row.finish,
                row.duration_days,
                row.effort_days,
                row.leveling_delay_days,
                qty,
                unit,
                row.cost,
                row.cost_complete,
                "scheduled" if row.scheduled else "unscheduled",
                tuple(dict.fromkeys(codes.get(nid, ()))),
            )  # fmt: skip
        )
    assignment_rows = tuple(
        AssignmentExportRow(
            ar.task_id,
            ar.resource_id,
            assignments[(ar.task_id, ar.resource_id)].assignment_days,
            ar.work_qty,
            unit,
            ar.rate_per_unit,
            ar.cost,
            ar.cost_complete,
        )  # fmt: skip
        for t in report.tasks.values()
        for ar in t.assignments
    )
    return {
        "assignments": assignments,
        "node_results": tuple(node_rows),
        "assignment_results": assignment_rows,
        "work_qty": report.work_qty,
        "work_unit": unit,
        "total_cost": costs.total,
        "cost_complete": costs.complete,
        "missing_rate_resources": costs.missing_rate_resources,
        "costs": costs,
    }


def schedule(project: Project, config: Config = DEFAULT_CONFIG) -> ScheduleResult:
    """Dependency-only schedule (kind ``dependency_only``).

    Raises:
        ValidationFailed: on any structural error (cycles, dangling references...).
            Unsized or capacity-less tasks do not raise; they yield an incomplete result.
    """
    prep = _Prepared(project, config)
    return _assemble(project, prep, prep.base, "dependency_only", {}, config)


def level(
    project: Project,
    base: ScheduleResult,
    config: Config = DEFAULT_CONFIG,
    progress: Callable[[float, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> LevelingResult:
    """Resource-level ``project`` starting from the dependency-only ``base``.

    Raises:
        Conflict: if ``base`` was not calculated from the current schedule inputs.
        ValidationFailed: on structural errors.
        Cancelled: if ``cancel()`` returns True.
    """
    if base.schedule_fp != schedule_fp(project):
        raise Conflict("base schedule is stale")
    prep = _Prepared(project, config)
    outcome: LevelingOutcome = _level(
        project,
        prep.sizing,
        prep.base,
        minutes_per_day=prep.mpd,
        config=config,
        progress=progress,
        cancel=cancel,
    )
    delay_minutes = {d.task_id: d.minutes for d in outcome.delays}
    result = _assemble(project, prep, outcome.timings, "leveling_preview", delay_minutes, config)
    return LevelingResult(
        result=result,
        delays_days={tid: Decimal(m) / prep.mpd for tid, m in delay_minutes.items()},
        delays=outcome.delays,
        unresolved=outcome.unresolved,
        finish_delta_days=_days(outcome.finish_delta_minutes, prep.mpd),
    )


def with_kind(result: ScheduleResult, kind: ResultKind) -> ScheduleResult:
    """Copy of ``result`` with another kind (e.g. preview -> ``leveled`` on apply)."""
    return dataclasses.replace(result, kind=kind)


def recost(result: ScheduleResult, project: Project) -> ScheduleResult:
    """Refresh costs after a rate-only edit; dates are kept untouched.

    Raises:
        Conflict: if ``project`` differs from ``result`` in any schedule input.
    """
    if result.schedule_fp != schedule_fp(project):
        raise Conflict("schedule inputs changed; recalculate the schedule")
    mpd = result.minutes_per_day
    durations: dict[str, int | None] = {
        i: result.nodes[i].duration_minutes for i in result.task_ids
    }
    costs = compute_costs(project, durations)
    nodes: dict[str, NodeResult] = {}
    for nid, row in result.nodes.items():
        if row.kind is NodeKind.GROUP:
            g = costs.groups[nid]
            nodes[nid] = dataclasses.replace(row, cost=g.cost, cost_complete=g.cost_complete)
        else:
            t = costs.tasks[nid]
            nodes[nid] = dataclasses.replace(row, cost=t.cost, cost_complete=t.cost_complete)
    return dataclasses.replace(
        result,
        nodes=nodes,
        cost_fp=cost_fp(project),
        **_cost_views(project, nodes, costs, result.issues, mpd),
    )


def cost_report(
    result: ScheduleResult, project: Project, unit: WorkUnit | str | None = None
) -> CostReport:
    """Cost report of ``result`` in a work unit (default: the project's)."""
    return _cost_report(result.costs, project, unit)
