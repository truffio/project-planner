"""Forward-pass scheduling (T15).

Computes earliest start/finish (axis minutes, half-open intervals) for every task
and milestone in topological order.

Input assumption: the project has passed ``validation.ensure_valid`` (no cycles,
no dangling references, no group endpoints). Dependencies whose predecessor is
not part of ``order`` are ignored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from project_planner.engine.model import DependencyType, Project
from project_planner.engine.network import topological_order
from project_planner.engine.sizing import TaskSizing

__all__ = ["NodeTiming", "forward_pass"]


@dataclass(frozen=True, slots=True)
class NodeTiming:
    """Forward-pass result of one node (``start``/``finish`` are None unless scheduled)."""

    node_id: str
    start: int | None
    finish: int | None
    status: Literal["scheduled", "unschedulable", "blocked"]
    reason: str | None
    blocked_by: tuple[str, ...] = ()


def forward_pass(
    project: Project,
    sizing: Mapping[str, TaskSizing],
    *,
    minutes_per_day: int,
    order: Sequence[str] | None = None,
    min_starts: Mapping[str, int] | None = None,
) -> dict[str, NodeTiming]:
    """Earliest start/finish per node.

    ``start = max(0, min_starts[id], FS: pf+lag, SS: ps+lag, FF: pf+lag-D, SF: ps+lag-D)``
    over all incoming dependencies; ``finish = start + D``. Nodes that cannot be
    sized are ``unschedulable``; nodes with an unschedulable or blocked predecessor
    are ``blocked``. Runs in O(N + E).
    """
    seq = list(order) if order is not None else topological_order(project)
    mins = min_starts or {}
    result: dict[str, NodeTiming] = {}
    for nid in seq:
        sz = sizing[nid]
        deps = project.dependencies_to(nid)
        bad = sorted(
            {
                d.pred_id
                for d in deps
                if d.pred_id in result and result[d.pred_id].status != "scheduled"
            }
        )
        if bad:
            parts = []
            for b in bad:
                pt = result[b]
                text = f"predecessor {b} is {pt.status}"
                if pt.reason:
                    text += f" ({pt.reason})"
                parts.append(text)
            reason = "blocked: " + "; ".join(parts)
            result[nid] = NodeTiming(nid, None, None, "blocked", reason, tuple(bad))
            continue
        duration = sz.duration_minutes
        if duration is None:
            detail = "; ".join(i.message for i in sz.issues) or "not schedulable"
            result[nid] = NodeTiming(nid, None, None, "unschedulable", detail)
            continue
        start = max(0, mins.get(nid, 0))
        for dep in deps:
            p = result.get(dep.pred_id)
            if p is None or p.start is None or p.finish is None:
                continue
            lag = dep.lag.to_minutes(minutes_per_day)
            if dep.type is DependencyType.FS:
                c = p.finish + lag
            elif dep.type is DependencyType.SS:
                c = p.start + lag
            elif dep.type is DependencyType.FF:
                c = p.finish + lag - duration
            else:
                c = p.start + lag - duration
            start = max(start, c)
        result[nid] = NodeTiming(nid, start, start + duration, "scheduled", None)
    return result
