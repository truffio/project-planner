"""Result types of a schedule calculation (plan sections 2.1 and 3).

All types are frozen, plain data and picklable.  Day quantities are exact
``Decimal`` values equal to ``minutes / minutes_per_day`` (never rounded);
the ``*_minutes`` fields keep the internal axis minutes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from project_planner.engine.cost import CostResult
from project_planner.engine.errors import Issue, NotFound, ObjectType
from project_planner.engine.leveling import LevelingDelay, UnresolvedOverload
from project_planner.engine.loading import LoadSegment
from project_planner.engine.model import NodeKind, TimeQty

__all__ = [
    "AssignmentExportRow",
    "AssignmentResult",
    "DependencyResult",
    "LevelingResult",
    "NodeExportRow",
    "NodeResult",
    "ResultKind",
    "ScheduleResult",
]

ResultKind = Literal["dependency_only", "leveling_preview", "leveled"]
NodeStatus = Literal["scheduled", "unschedulable", "blocked", "group"]


@dataclass(frozen=True)
class NodeResult:
    """One WBS node row.  Groups take dates/effort from rollup and cost from cost groups."""

    node_id: str
    kind: NodeKind
    status: NodeStatus
    scheduled: bool
    start: datetime | None
    finish: datetime | None
    start_minutes: int | None
    finish_minutes: int | None
    duration_minutes: int | None
    duration_days: Decimal | None
    effort_person_minutes: Decimal | None
    effort_days: Decimal | None
    leveling_delay_minutes: int
    leveling_delay_days: Decimal
    cost: Decimal
    cost_complete: bool
    reason: str | None
    wbs_number: str


@dataclass(frozen=True)
class AssignmentResult:
    """One assignment row (work behind the cost, in hours/minutes and person-days)."""

    task_id: str
    resource_id: str
    percent: Decimal
    assignment_minutes: Decimal
    assignment_days: Decimal
    assignment_hours: Decimal
    hourly_rate: Decimal | None
    cost: Decimal | None
    cost_complete: bool


@dataclass(frozen=True)
class NodeExportRow:
    """CSV view of a node row (structurally ``csv_io.NodeResultRow``).

    ``status`` is ``"scheduled"`` or ``"unscheduled"``; work is in the project's
    ``cost_report_unit``.
    """

    node_id: str
    start: datetime | None
    finish: datetime | None
    duration_days: Decimal | None
    effort_days: Decimal | None
    leveling_delay_days: Decimal | None
    work_qty: Decimal | None
    work_unit: str | None
    cost: Decimal | None
    cost_complete: bool
    status: str
    issue_codes: tuple[str, ...]


@dataclass(frozen=True)
class AssignmentExportRow:
    """CSV view of an assignment row (structurally ``csv_io.AssignmentResultRow``)."""

    task_id: str
    resource_id: str
    assignment_days: Decimal | None
    work_qty: Decimal | None
    work_unit: str | None
    rate_per_unit: Decimal | None
    cost: Decimal | None
    cost_complete: bool


@dataclass(frozen=True)
class DependencyResult:
    """Echo of a dependency lag in working time (plus the value as entered)."""

    dependency_id: str
    lag_minutes: int
    lag_days: Decimal
    lag_entered: TimeQty


@dataclass(frozen=True)
class ScheduleResult:
    """A calculated schedule.

    ``project_finish`` is None unless ``complete`` (and at least one node is scheduled).
    ``working_span_*`` is the finish axis minute of the project (working time from
    the project start); ``elapsed_span_calendar_days`` is the inclusive count of
    calendar dates from the first working date (axis origin) to the date of the
    finish display datetime (a whole-number ``Decimal``).  Both spans are None when
    ``project_finish`` is None.  ``effort_days`` is the total person-days of all
    sized tasks; ``work_qty``/``work_unit`` the total work in the project's
    ``cost_report_unit``; ``node_results``/``assignment_results`` are the CSV views.
    """

    kind: ResultKind
    nodes: Mapping[str, NodeResult]
    assignments: Mapping[tuple[str, str], AssignmentResult]
    dependencies: Mapping[str, DependencyResult]
    task_ids: tuple[str, ...]
    loading: Mapping[str, tuple[LoadSegment, ...]]
    issues: tuple[Issue, ...]
    complete: bool
    project_start: datetime
    project_finish: datetime | None
    working_span_minutes: int | None
    working_span_days: Decimal | None
    elapsed_span_calendar_days: Decimal | None
    effort_days: Decimal | None
    work_qty: Decimal
    work_unit: str
    node_results: tuple[NodeExportRow, ...]
    assignment_results: tuple[AssignmentExportRow, ...]
    total_cost: Decimal
    cost_complete: bool
    missing_rate_resources: tuple[str, ...]
    costs: CostResult
    schedule_fp: str
    cost_fp: str
    minutes_per_day: int

    @property
    def cost_total(self) -> Decimal:
        """Alias of ``total_cost``."""
        return self.total_cost

    def node(self, node_id: str) -> NodeResult:
        """Row of a node; raises ``NotFound`` for an unknown ID."""
        try:
            return self.nodes[node_id]
        except KeyError:
            raise NotFound(ObjectType.NODE.value, node_id) from None

    def assignment(self, task_id: str, resource_id: str) -> AssignmentResult:
        """Row of an assignment; raises ``NotFound`` for an unknown pair."""
        try:
            return self.assignments[(task_id, resource_id)]
        except KeyError:
            raise NotFound(ObjectType.ASSIGNMENT.value, f"{task_id}/{resource_id}") from None

    def dependency(self, dependency_id: str) -> DependencyResult:
        """Row of a dependency; raises ``NotFound`` for an unknown ID."""
        try:
            return self.dependencies[dependency_id]
        except KeyError:
            raise NotFound(ObjectType.DEPENDENCY.value, dependency_id) from None

    def tasks(self) -> tuple[NodeResult, ...]:
        """Task and milestone rows in WBS order."""
        return tuple(self.nodes[i] for i in self.task_ids)


@dataclass(frozen=True)
class LevelingResult:
    """Outcome of ``level``: a ``leveling_preview`` result plus the delays."""

    result: ScheduleResult
    delays_days: Mapping[str, Decimal]
    delays: tuple[LevelingDelay, ...]
    unresolved: tuple[UnresolvedOverload, ...]
    finish_delta_days: Decimal | None
