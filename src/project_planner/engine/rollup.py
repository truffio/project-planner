"""WBS summary rollup (T16).

Group dates derive from scheduled descendants only (never by adding child
durations).  Groups with unscheduled descendants are flagged incomplete but
still report the dates/effort of their scheduled descendants.

Everything is iterative (no recursion) and O(N) apart from the size of the
reported ``unscheduled_descendants`` tuples.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from project_planner.engine.config import engine_context
from project_planner.engine.model import NodeKind, Project, WbsNode

__all__ = ["GroupSummary", "rollup", "wbs_numbers"]

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class GroupSummary:
    group_id: str
    start: int | None
    finish: int | None
    effort_person_minutes: Decimal
    complete: bool
    unscheduled_descendants: tuple[str, ...]


def _children_map(project: Project) -> tuple[list[WbsNode], dict[str, list[WbsNode]]]:
    """Return (roots, children-by-parent) in display order (order, id).

    Nodes whose parent does not exist are treated as roots; nodes caught in a
    parent cycle are unreachable and ignored (validation reports them).
    """
    ids = {n.id for n in project.nodes}
    roots: list[WbsNode] = []
    kids: dict[str, list[WbsNode]] = {}
    for n in sorted(project.nodes, key=lambda x: (x.order, x.id)):
        if n.parent_id is None or n.parent_id not in ids:
            roots.append(n)
        else:
            kids.setdefault(n.parent_id, []).append(n)
    return roots, kids


def _preorder(roots: list[WbsNode], kids: dict[str, list[WbsNode]]) -> list[WbsNode]:
    out: list[WbsNode] = []
    stack = list(reversed(roots))
    while stack:
        n = stack.pop()
        out.append(n)
        ch = kids.get(n.id)
        if ch:
            stack.extend(reversed(ch))
    return out


def wbs_numbers(project: Project) -> dict[str, str]:
    """Display numbers "1", "1.2", "1.2.3" from parent and (order, id)."""
    roots, kids = _children_map(project)
    numbers: dict[str, str] = {}
    stack: list[tuple[WbsNode, str]] = [(n, str(i)) for i, n in enumerate(roots, 1)]
    stack.reverse()
    while stack:
        n, num = stack.pop()
        numbers[n.id] = num
        ch = kids.get(n.id)
        if ch:
            stack.extend((c, f"{num}.{i}") for i, c in reversed(list(enumerate(ch, 1))))
    return numbers


@engine_context
def rollup(
    project: Project,
    intervals: Mapping[str, tuple[int, int] | None],
    effort: Mapping[str, Decimal | None],
) -> dict[str, GroupSummary]:
    """Summarise every group node.

    ``intervals`` maps task/milestone id to (start, finish) or None
    (unscheduled; missing ids count as unscheduled).  ``effort`` maps task id
    to person-minutes or None.  A scheduled task with unknown effort makes its
    groups incomplete (but is not listed as unscheduled).  Effort sums scheduled
    leaf tasks only.  Dates are shown even when a group is incomplete.
    """
    roots, kids = _children_map(project)
    order = _preorder(roots, kids)
    index = {n.id: i for i, n in enumerate(order)}

    # Per-node aggregates: [start, finish, effort, effort_unknown]
    start: list[int | None] = [None] * len(order)
    finish: list[int | None] = [None] * len(order)
    total: list[Decimal] = [_ZERO] * len(order)
    unknown: list[bool] = [False] * len(order)
    end = list(range(1, len(order) + 1))  # exclusive end of subtree in preorder
    unsched_pos: list[int] = []  # preorder positions of unscheduled leaves

    parent_pos: list[int] = [-1] * len(order)
    for i, n in enumerate(order):
        if n.parent_id is not None and n.parent_id in index:
            parent_pos[i] = index[n.parent_id]

    # Reverse preorder visits children before parents.
    for i in range(len(order) - 1, -1, -1):
        n = order[i]
        if n.kind is not NodeKind.GROUP:
            iv = intervals.get(n.id)
            if iv is None:
                unsched_pos.append(i)
            else:
                start[i], finish[i] = iv
                if n.kind is NodeKind.TASK:
                    e = effort.get(n.id)
                    if e is None:
                        unknown[i] = True
                    else:
                        total[i] = e
        p = parent_pos[i]
        if p >= 0:
            if end[i] > end[p]:
                end[p] = end[i]
            si, fi = start[i], finish[i]
            if si is not None and fi is not None:
                sp, fp = start[p], finish[p]
                if sp is None or si < sp:
                    start[p] = si
                if fp is None or fi > fp:
                    finish[p] = fi
            total[p] += total[i]
            if unknown[i]:
                unknown[p] = True
    unsched_pos.reverse()

    result: dict[str, GroupSummary] = {}
    for i, n in enumerate(order):
        if n.kind is not NodeKind.GROUP:
            continue
        lo = bisect_left(unsched_pos, i + 1)
        hi = bisect_left(unsched_pos, end[i])
        missing = tuple(order[j].id for j in unsched_pos[lo:hi])
        result[n.id] = GroupSummary(
            group_id=n.id,
            start=start[i],
            finish=finish[i],
            effort_person_minutes=total[i],
            complete=not missing and not unknown[i],
            unscheduled_descendants=missing,
        )
    return result
