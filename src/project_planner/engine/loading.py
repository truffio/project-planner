"""Resource loading (T17): uncapped piecewise-constant load and day/week aggregation.

Loading is computed per resource by a sweep-line over assignment interval endpoints.
Percentages are summed, never capped, so a brief overload stays visible. Aggregation
works on axis working days, so nonworking time is never counted as capacity.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from project_planner.engine.calendar import WorkingAxis
from project_planner.engine.model import Project

__all__ = [
    "LoadBucket",
    "LoadSegment",
    "aggregate",
    "compute_loading",
    "overloads",
]

_HUNDRED = Decimal(100)
_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class LoadSegment:
    """A maximal span ``[start, end)`` (axis minutes) of constant load on a resource."""

    resource_id: str
    start: int
    end: int
    percent: Decimal
    task_ids: tuple[str, ...]

    @property
    def overloaded(self) -> bool:
        """Whether the summed allocation exceeds 100 %."""
        return self.percent > _HUNDRED


@dataclass(frozen=True, slots=True)
class LoadBucket:
    """Aggregated load of one resource over a day or an ISO week."""

    resource_id: str
    period_start: dt.date
    assigned_person_minutes: Decimal
    assigned_days: Decimal
    average_percent: Decimal
    peak_percent: Decimal


def compute_loading(
    project: Project, intervals: Mapping[str, tuple[int, int] | None]
) -> dict[str, tuple[LoadSegment, ...]]:
    """Load segments per resource id (every project resource present, possibly empty).

    Args:
        project: The project (assignments and resources).
        intervals: Task id -> scheduled ``(start, end)`` axis minutes, or ``None`` /
            missing when unscheduled. Zero-length intervals contribute nothing.
    """
    result: dict[str, tuple[LoadSegment, ...]] = {r.id: () for r in project.resources}
    by_resource: dict[str, list[tuple[int, int, str, Decimal]]] = defaultdict(list)
    for a in project.assignments:
        iv = intervals.get(a.task_id)
        if iv is None or iv[1] <= iv[0] or a.percent <= 0:
            continue
        by_resource[a.resource_id].append((iv[0], iv[1], a.task_id, a.percent))
    for rid, items in by_resource.items():
        result[rid] = _sweep(rid, items)
    return result


def _sweep(rid: str, items: list[tuple[int, int, str, Decimal]]) -> tuple[LoadSegment, ...]:
    # Events: (time, kind, task, percent), kind 1 = start, 0 = end. All events at one
    # time are applied before a segment is emitted up to the next distinct time.
    events: list[tuple[int, int, str, Decimal]] = []
    for s, e, tid, pct in items:
        events.append((s, 1, tid, pct))
        events.append((e, 0, tid, pct))
    events.sort(key=lambda ev: (ev[0], ev[1], ev[2]))

    active: dict[str, Decimal] = {}
    total = _ZERO
    out: list[LoadSegment] = []
    n = len(events)
    i = 0
    while i < n:
        t = events[i][0]
        while i < n and events[i][0] == t:
            _, kind, tid, pct = events[i]
            if kind == 1:
                active[tid] = pct
                total += pct
            else:
                del active[tid]
                total -= pct
            i += 1
        if i >= n or not active or total <= 0:
            continue
        nxt = events[i][0]
        tasks = tuple(sorted(active))
        last = out[-1] if out else None
        if last is not None and last.end == t and last.percent == total and last.task_ids == tasks:
            out[-1] = LoadSegment(rid, last.start, nxt, total, tasks)
        else:
            out.append(LoadSegment(rid, t, nxt, total, tasks))
    return tuple(out)


def overloads(loading: Mapping[str, Iterable[LoadSegment]]) -> list[LoadSegment]:
    """All segments above 100 %, ordered by resource id then start."""
    return [seg for rid in sorted(loading) for seg in loading[rid] if seg.overloaded]


def aggregate(
    segments: Iterable[LoadSegment],
    axis: WorkingAxis,
    granularity: Literal["day", "week"],
) -> tuple[LoadBucket, ...]:
    """Aggregate segments into day or ISO-week buckets (periods with load only).

    Segments are split at working-day boundaries. Average % is assigned person-minutes
    over the working minutes of the period (for a week: its working days on or after
    the axis origin) times 100; peak % is the highest segment percent in the period.
    Buckets are grouped per segment resource id and sorted by (resource, period).
    """
    if granularity not in ("day", "week"):
        raise ValueError(f"granularity must be 'day' or 'week', got {granularity!r}")
    mpd = axis.minutes_per_day
    acc: dict[tuple[str, dt.date], list[Decimal]] = {}  # [person_minutes, peak]
    for seg in segments:
        t = seg.start
        while t < seg.end:
            idx = t // mpd
            piece_end = min(seg.end, (idx + 1) * mpd)
            day = axis.working_date(idx)
            period = day if granularity == "day" else day - dt.timedelta(days=day.weekday())
            entry = acc.setdefault((seg.resource_id, period), [_ZERO, _ZERO])
            entry[0] += Decimal(piece_end - t) * seg.percent / _HUNDRED
            entry[1] = max(entry[1], seg.percent)
            t = piece_end

    buckets: list[LoadBucket] = []
    for (rid, period), (minutes, peak) in sorted(acc.items()):
        if granularity == "day":
            capacity_days = 1
        else:
            capacity_days = sum(
                1
                for k in range(7)
                if (d := period + dt.timedelta(days=k)) >= axis.origin and axis.is_working_day(d)
            )
        capacity = Decimal(capacity_days * mpd)
        buckets.append(
            LoadBucket(
                resource_id=rid,
                period_start=period,
                assigned_person_minutes=minutes,
                assigned_days=minutes / Decimal(mpd),
                average_percent=minutes / capacity * _HUNDRED,
                peak_percent=peak,
            )
        )
    return tuple(buckets)
