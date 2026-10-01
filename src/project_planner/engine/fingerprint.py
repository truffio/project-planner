"""Canonical-JSON fingerprints used for stale detection (plan section 3).

``schedule_fp`` hashes exactly the inputs that influence dates: project start,
calendar (except ``working_days_per_year`` and holiday/exception names), node
kinds/parents/order/sizing, assignments (which carry the resource IDs that matter) and
dependencies. Resources themselves (IDs, names, rates) are not schedule inputs, so adding or
removing an unassigned resource leaves dates current.  Names,
rates, currency, ``cost_report_unit`` and ``working_days_per_year`` are excluded.

``cost_fp`` hashes the schedule inputs plus the hourly rates of *assigned* resources (an
unassigned resource's rate costs nothing), so a rate change
alters ``cost_fp`` only and a rename alters neither.

Numbers are written as normalized plain strings (``5``, ``5.0`` and ``5.00``
hash identically), because they are equal in value and give identical results.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from project_planner.engine.config import engine_context
from project_planner.engine.model import Project, TimeQty

__all__ = ["cost_fp", "schedule_fp"]


def _num(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _qty(q: TimeQty | None) -> list[str] | None:
    if q is None:
        return None
    return [_num(q.value), q.unit.value]


def _digest(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _schedule_payload(project: Project) -> dict[str, Any]:
    cal = project.calendar
    return {
        "start": project.start.isoformat(),
        "calendar": {
            "weekdays": sorted(w.python_weekday for w in cal.working_weekdays),
            "hours_per_day": _num(cal.hours_per_day),
            "workday_start": cal.workday_start.isoformat(),
            "holidays": sorted(h.date.isoformat() for h in cal.holidays),
            "exceptions": sorted([e.date.isoformat(), e.kind.value] for e in cal.exceptions),
        },
        "nodes": sorted(
            (
                [
                    n.id,
                    n.kind.value,
                    n.parent_id,
                    n.order,
                    n.sizing_mode.value,
                    _qty(n.sizing),
                ]
                for n in project.nodes
            ),
            key=lambda row: row[0],
        ),
        "assignments": sorted(
            [a.task_id, a.resource_id, _num(a.percent)] for a in project.assignments
        ),
        "dependencies": sorted(
            [d.id, d.pred_id, d.succ_id, d.type.value, _qty(d.lag)] for d in project.dependencies
        ),
    }


@engine_context
def schedule_fp(project: Project) -> str:
    """SHA-256 (hex) of the inputs that affect dates."""
    return _digest({"schedule": _schedule_payload(project)})


@engine_context
def cost_fp(project: Project) -> str:
    """SHA-256 (hex) of the schedule inputs plus hourly rates."""
    assigned = {a.resource_id for a in project.assignments}
    rates = sorted(
        [r.id, None if r.hourly_rate is None else _num(r.hourly_rate)]
        for r in project.resources
        if r.id in assigned
    )
    return _digest({"schedule": _schedule_payload(project), "rates": rates})
