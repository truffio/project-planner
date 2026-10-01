"""Labor cost calculation and cost reporting (plan section 2.2).

Costs are computed in hours at full ``Decimal`` precision and are never rounded
here.  Reporting converts only the work quantity and the rate shown beside it;
money is identical in every work unit.

Completeness rules:

* An assignment without a rate has no cost and is incomplete.
* A task is incomplete if any assignment lacks a rate or if its duration is
  unschedulable (``None``/missing); its cost is the sum of available costs.
* Milestones (and groups without leaf tasks) cost 0 and are complete.
* Groups and the project sum descendant *leaf tasks* only; completeness
  propagates upward.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from project_planner.engine.config import DEFAULT_CONFIG, Config
from project_planner.engine.model import NodeKind, Project, WorkUnit

_ZERO = Decimal(0)
_SIXTY = Decimal(60)


@dataclass(frozen=True)
class AssignmentCost:
    task_id: str
    resource_id: str
    percent: Decimal
    fraction: Decimal
    assignment_minutes: Decimal
    assignment_hours: Decimal
    hourly_rate: Decimal | None
    cost: Decimal | None
    cost_complete: bool


@dataclass(frozen=True)
class TaskCost:
    task_id: str
    cost: Decimal
    cost_complete: bool
    assignments: tuple[AssignmentCost, ...]
    missing_rate_resources: tuple[str, ...]
    hours: Decimal = _ZERO
    """Total assigned hours (sum of assignment hours)."""


@dataclass(frozen=True)
class GroupCost:
    group_id: str
    cost: Decimal
    cost_complete: bool
    hours: Decimal = _ZERO


@dataclass(frozen=True)
class CostResult:
    tasks: Mapping[str, TaskCost]
    groups: Mapping[str, GroupCost]
    total: Decimal
    complete: bool
    missing_rate_resources: tuple[str, ...]
    total_hours: Decimal = _ZERO


def compute_costs(project: Project, durations: Mapping[str, int | None]) -> CostResult:
    """Compute exact (unrounded) costs.

    ``durations`` maps node ID to duration in working minutes (None or absent
    means unschedulable).
    """
    tasks: dict[str, TaskCost] = {}
    for node in project.nodes:
        if node.kind is NodeKind.GROUP:
            continue
        if node.kind is NodeKind.MILESTONE:
            tasks[node.id] = TaskCost(node.id, _ZERO, True, (), ())
            continue
        duration = durations.get(node.id)
        rows: list[AssignmentCost] = []
        for a in sorted(project.assignments_for(node.id), key=lambda x: x.resource_id):
            rate = project.resource(a.resource_id).hourly_rate
            minutes = _ZERO if duration is None else Decimal(duration) * a.fraction
            hours = minutes / _SIXTY
            # One division only (Decimal cannot represent /60 exactly in general).
            cost = None if rate is None else minutes * rate / _SIXTY
            rows.append(
                AssignmentCost(
                    node.id,
                    a.resource_id,
                    a.percent,
                    a.fraction,
                    minutes,
                    hours,
                    rate,
                    cost,
                    rate is not None,
                )
            )
        missing = tuple(r.resource_id for r in rows if r.hourly_rate is None)
        tasks[node.id] = TaskCost(
            node.id,
            sum((r.cost for r in rows if r.cost is not None), _ZERO),
            not missing and duration is not None,
            tuple(rows),
            missing,
            sum((r.assignment_hours for r in rows), _ZERO),
        )

    def leaves(parent_id: str) -> list[str]:
        out: list[str] = []
        for child in project.children(parent_id):
            if child.kind is NodeKind.GROUP:
                out.extend(leaves(child.id))
            elif child.kind is NodeKind.TASK:
                out.append(child.id)
        return out

    groups: dict[str, GroupCost] = {}
    for node in project.nodes:
        if node.kind is NodeKind.GROUP:
            ts = [tasks[t] for t in leaves(node.id)]
            groups[node.id] = GroupCost(
                node.id,
                sum((t.cost for t in ts), _ZERO),
                all(t.cost_complete for t in ts),
                sum((t.hours for t in ts), _ZERO),
            )

    leaf_tasks = [tasks[n.id] for n in project.nodes if n.kind is NodeKind.TASK]
    missing_all = tuple(sorted({r for t in leaf_tasks for r in t.missing_rate_resources}))
    return CostResult(
        tasks,
        groups,
        sum((t.cost for t in leaf_tasks), _ZERO),
        all(t.cost_complete for t in leaf_tasks),
        missing_all,
        sum((t.hours for t in leaf_tasks), _ZERO),
    )


# ---------------------------------------------------------------- reporting


@dataclass(frozen=True)
class AssignmentRow:
    task_id: str
    resource_id: str
    work_qty: Decimal
    work_unit: WorkUnit
    rate_per_unit: Decimal | None
    cost: Decimal | None
    cost_complete: bool


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    work_qty: Decimal
    work_unit: WorkUnit
    cost: Decimal
    cost_complete: bool
    assignments: tuple[AssignmentRow, ...]
    missing_rate_resources: tuple[str, ...]


@dataclass(frozen=True)
class GroupRow:
    group_id: str
    work_qty: Decimal
    work_unit: WorkUnit
    cost: Decimal
    cost_complete: bool


@dataclass(frozen=True)
class CostReport:
    unit: WorkUnit
    tasks: Mapping[str, TaskRow]
    groups: Mapping[str, GroupRow]
    work_qty: Decimal
    cost: Decimal
    complete: bool
    missing_rate_resources: tuple[str, ...]


def cost_report(
    costs: CostResult, project: Project, unit: WorkUnit | str | None = None
) -> CostReport:
    """Present ``costs`` in a work unit (default: the project's). Money is unchanged."""
    u = project.cost_report_unit if unit is None else WorkUnit(unit)
    per = u.hours_per_unit(project.calendar)

    def row(a: AssignmentCost) -> AssignmentRow:
        rpu = None if a.hourly_rate is None else a.hourly_rate * per
        return AssignmentRow(
            a.task_id, a.resource_id, a.assignment_hours / per, u, rpu, a.cost, a.cost_complete
        )

    task_rows = {
        tid: TaskRow(
            tid,
            t.hours / per,
            u,
            t.cost,
            t.cost_complete,
            tuple(row(a) for a in t.assignments),
            t.missing_rate_resources,
        )
        for tid, t in costs.tasks.items()
    }
    group_rows = {
        gid: GroupRow(gid, g.hours / per, u, g.cost, g.cost_complete)
        for gid, g in costs.groups.items()
    }
    return CostReport(
        u,
        task_rows,
        group_rows,
        costs.total_hours / per,
        costs.total,
        costs.complete,
        costs.missing_rate_resources,
    )


def round_money(value: Decimal, config: Config = DEFAULT_CONFIG) -> Decimal:
    """Display/export rounding of a money value; never use for totals."""
    return config.round_money(value)
