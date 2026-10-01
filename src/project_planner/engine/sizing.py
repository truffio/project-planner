"""Task sizing (plan section 2 row 7, 2.1; spec 5.1-5.3): duration, effort and capacity.

Pure functions turning each task's as-entered size and assignments into
working minutes on the internal axis. Inputs are never modified.

Units
-----
* Durations: working minutes; ``d`` means working days of ``minutes_per_day``.
* Effort: **person-minutes**; ``d`` means person-days of ``minutes_per_day``.
* ``capacity`` is the sum of assignment fractions (``percent / 100``).

Rules
-----
* Duration mode: ``D = sizing.to_minutes(mpd)`` (rounded up, per ``TimeQty``);
  ``effort = D * capacity`` (exact ``Decimal``, may be fractional).
* Effort mode: ``E`` is computed exactly (hours * 60 or days * mpd, no rounding);
  ``D = ceil(E / capacity)`` to a whole minute. ``effort_person_minutes`` is
  ``E`` itself, the user's effort, *not* ``D * capacity`` (which can exceed
  ``E`` by less than ``capacity`` person-minutes because of the ceiling).
  With ``capacity <= 0`` and ``E > 0`` the task is unschedulable
  (``TASK_NO_CAPACITY``).
* Zero-sized task (``0h``/``0d``): ``D = 0`` and schedulable, even in effort mode
  without assignments (nothing to staff).
* Unsized task: unschedulable (``TASK_UNSIZED``). Milestone: ``D = 0``, effort 0.
* Groups have no sizing and are excluded from :func:`compute_sizing`.

Issue codes: ``TASK_UNSIZED``, ``TASK_NO_CAPACITY`` (both severity error: the
task cannot be scheduled).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Context, Decimal
from fractions import Fraction

from project_planner.engine.errors import Issue, ObjectType
from project_planner.engine.model import (
    MINUTES_PER_HOUR,
    Assignment,
    NodeKind,
    Project,
    SizingMode,
    TimeUnit,
    WbsNode,
)

__all__ = ["TaskSizing", "compute_sizing", "size_node"]

_EXACT = Context(prec=200)  # far beyond any realistic input; keeps products exact


@dataclass(frozen=True, slots=True)
class TaskSizing:
    """Sizing result of one task or milestone.

    Attributes:
        node_id: The node.
        duration_minutes: Working minutes, or ``None`` when unschedulable.
        effort_person_minutes: Person-minutes (exact), or ``None`` when unschedulable.
        capacity: Sum of assignment fractions (``percent / 100``).
        issues: Problems that make the task unschedulable (empty otherwise).
        schedulable: ``True`` when ``duration_minutes`` is set.
    """

    node_id: str
    duration_minutes: int | None
    effort_person_minutes: Decimal | None
    capacity: Decimal
    issues: tuple[Issue, ...]
    schedulable: bool


def _capacity(assignments: Iterable[Assignment]) -> Fraction:
    """Exact sum of fractions (independent of order)."""
    total = Fraction(0)
    for a in assignments:
        num, den = a.percent.as_integer_ratio()
        total += Fraction(num, den * 100)
    return total


def _fraction_to_decimal(x: Fraction) -> Decimal:
    return _EXACT.divide(Decimal(x.numerator), Decimal(x.denominator))


def size_node(node: WbsNode, assignments: Iterable[Assignment], minutes_per_day: int) -> TaskSizing:
    """Size one task or milestone.

    Args:
        node: A task or milestone.
        assignments: Its assignments (others are not filtered out; pass only its own).
        minutes_per_day: Calendar working minutes per day.

    Raises:
        ValueError: if ``node`` is a group.
    """
    if node.kind is NodeKind.GROUP:
        raise ValueError(f"group {node.id!r} has no sizing")
    cap = _capacity(assignments)
    capacity = _fraction_to_decimal(cap)

    def unschedulable(issue: Issue) -> TaskSizing:
        return TaskSizing(node.id, None, None, capacity, (issue,), False)

    if node.kind is NodeKind.MILESTONE:
        return TaskSizing(node.id, 0, Decimal(0), capacity, (), True)
    if node.sizing is None or node.sizing_mode is SizingMode.NONE:
        return unschedulable(
            Issue.error(
                "TASK_UNSIZED",
                f"task {node.id!r} has no duration or effort, so it cannot be scheduled",
                object_type=ObjectType.NODE,
                object_id=node.id,
                field="sizing",
            )
        )
    if node.sizing_mode is SizingMode.DURATION:
        duration = node.sizing.to_minutes(minutes_per_day)
        return TaskSizing(
            node.id, duration, _EXACT.multiply(Decimal(duration), capacity), capacity, (), True
        )

    # Effort mode: E exact, in person-minutes.
    factor = MINUTES_PER_HOUR if node.sizing.unit is TimeUnit.HOURS else minutes_per_day
    num, den = node.sizing.value.as_integer_ratio()
    effort = Fraction(num * factor, den)
    effort_dec = _EXACT.multiply(node.sizing.value, Decimal(factor))
    if effort == 0:
        return TaskSizing(node.id, 0, effort_dec, capacity, (), True)
    if cap <= 0:
        return unschedulable(
            Issue.error(
                "TASK_NO_CAPACITY",
                f"effort task {node.id!r} ({node.sizing}) has no positive resource allocation, "
                f"so its duration cannot be derived; assign a resource",
                object_type=ObjectType.NODE,
                object_id=node.id,
                field="assignments",
            )
        )
    q = effort / cap
    duration = -((-q.numerator) // q.denominator)  # ceil
    return TaskSizing(node.id, duration, effort_dec, capacity, (), True)


def compute_sizing(project: Project) -> dict[str, TaskSizing]:
    """Size every task and milestone of ``project``, keyed by node ID (groups excluded)."""
    mpd = project.calendar.minutes_per_day
    return {
        n.id: size_node(n, project.assignments_for(n.id), mpd)
        for n in project.nodes
        if n.kind is not NodeKind.GROUP
    }
