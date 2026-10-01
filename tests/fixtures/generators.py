"""Hypothesis strategies producing valid random :class:`~project_planner.engine.model.Project`s.

``projects()`` yields projects that pass structural validation (no dangling references,
cycles, group endpoints or duplicate dependencies) but may be *incomplete* (unsized
tasks, effort-sized tasks without assigned capacity), so downstream code sees both
complete and unschedulable/blocked outcomes.

Sizes stay small: at most ``max_nodes`` (15) WBS nodes and 25 dependencies.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from hypothesis import strategies as st

from fixtures.builders import ProjectBuilder
from project_planner.engine.model import Project

__all__ = ["calendars", "projects"]

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_START = dt.date(2026, 10, 5)
_MAX_GROUP_DEPTH = 3


@st.composite
def calendars(draw: Any, start: dt.date = _START) -> dict[str, Any]:
    """Keyword arguments for ``ProjectBuilder.calendar`` (a random valid calendar)."""
    # Whole minutes 60..720 (1-12 h); multiples of 3 keep hours_per_day a finite decimal.
    minutes = draw(st.integers(20, 240)) * 3
    weekdays = draw(st.sets(st.sampled_from(_WEEKDAYS), min_size=1))
    offsets = draw(st.lists(st.integers(0, 45), unique=True, max_size=6))
    holidays: list[tuple[dt.date, str]] = []
    exceptions: list[tuple[dt.date, str]] = []
    for off in offsets:
        day = start + dt.timedelta(days=off)
        if draw(st.booleans()):
            holidays.append((day, "holiday"))
        else:
            exceptions.append((day, draw(st.sampled_from(["working", "nonworking"]))))
    return {
        "hours_per_day": Decimal(minutes) / Decimal(60),
        "working_weekdays": ";".join(w for w in _WEEKDAYS if w in weekdays),
        "holidays": holidays,
        "exceptions": exceptions,
    }


def _time(draw: Any, *, low: int, high: int, signed: bool = False) -> str:
    unit = draw(st.sampled_from(["h", "d"]))
    n = draw(st.integers(-high if signed else low, high))
    if unit == "d" and draw(st.booleans()):
        return f"{Decimal(n) / 2}d"  # half days
    return f"{n}{unit}"


@st.composite
def projects(draw: Any, max_nodes: int = 15, max_deps: int = 25) -> Project:
    """A valid random project (see module docstring)."""
    start = _START + dt.timedelta(days=draw(st.integers(0, 14)))
    b = ProjectBuilder(start=start)
    b.calendar(**draw(calendars(start)))

    n_res = draw(st.integers(0, 3))
    rids = [f"r{i}" for i in range(n_res)]
    for rid in rids:
        rate = draw(st.one_of(st.none(), st.integers(1, 300)))
        b.resource(rid, rate=rate)

    n_nodes = draw(st.integers(1, max_nodes))
    groups: list[tuple[str, int]] = []  # (id, depth)
    leaves: list[str] = []
    tasks: list[str] = []
    for i in range(n_nodes):
        open_groups = [g for g, depth in groups if depth < _MAX_GROUP_DEPTH]
        parent = draw(st.sampled_from([None, *open_groups])) if open_groups else None
        depth = 1 + next((d for g, d in groups if g == parent), 0)
        kind = draw(st.sampled_from(["group", "task", "task", "task", "milestone"]))
        if kind == "group":
            nid = f"g{i}"
            b.group(nid, parent=parent)
            groups.append((nid, depth))
        elif kind == "milestone":
            nid = f"m{i}"
            b.milestone(nid, parent=parent)
            leaves.append(nid)
        else:
            nid = f"t{i}"
            sizing = draw(st.sampled_from(["duration", "duration", "effort", "effort", "none"]))
            if sizing == "duration":
                b.task(nid, parent=parent, duration=_time(draw, low=0, high=40))
            elif sizing == "effort":
                b.task(nid, parent=parent, effort=_time(draw, low=0, high=40))
            else:
                b.task(nid, parent=parent)
            leaves.append(nid)
            tasks.append(nid)

    if rids and tasks:
        for tid in tasks:
            for rid in draw(st.sets(st.sampled_from(rids), max_size=len(rids))):
                b.assign(tid, rid, draw(st.integers(1, 100)))

    # DAG: edges go forward in a random order of the leaves.
    if len(leaves) >= 2:
        order = draw(st.permutations(leaves))
        pairs = [(order[i], order[j]) for j in range(1, len(order)) for i in range(j)]
        chosen = draw(st.lists(st.sampled_from(pairs), unique=True, max_size=max_deps))
        for pred, succ in chosen:
            dep_type = draw(st.sampled_from(["FS", "SS", "FF", "SF"]))
            b.dep(pred, succ, dep_type, lag=_time(draw, low=0, high=16, signed=True))
    return b.build()
