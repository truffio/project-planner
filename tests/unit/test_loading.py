"""Unit tests for engine.loading (T17)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.calendar import WorkingAxis
from project_planner.engine.loading import aggregate, compute_loading, overloads

pytestmark = pytest.mark.unit

D = Decimal


def _project(*assigns: tuple[str, str, int], tasks: int = 3):
    b = ProjectBuilder().resource("alice").resource("bob")
    for i in range(1, tasks + 1):
        b.task(f"t{i}")
    for t, r, p in assigns:
        b.assign(t, r, p)
    return b.build()


def test_a03_overload_with_both_tasks():
    p = _project(("t1", "alice", 80), ("t2", "alice", 50))
    out = compute_loading(p, {"t1": (0, 2400), "t2": (960, 1920)})["alice"]
    assert [(s.start, s.end, s.percent, s.task_ids) for s in out] == [
        (0, 960, D(80), ("t1",)),
        (960, 1920, D(130), ("t1", "t2")),
        (1920, 2400, D(80), ("t1",)),
    ]
    assert [s.overloaded for s in out] == [False, True, False]
    assert overloads({"alice": out}) == [out[1]]


def test_touching_intervals_do_not_overlap():
    p = _project(("t1", "alice", 60), ("t2", "alice", 70))
    out = compute_loading(p, {"t1": (0, 480), "t2": (480, 960)})["alice"]
    assert [(s.start, s.end, s.percent) for s in out] == [(0, 480, 60), (480, 960, 70)]
    assert all(not s.overloaded for s in out)


def test_identical_percent_different_tasks_not_merged():
    p = _project(("t1", "alice", 60), ("t2", "alice", 60))
    out = compute_loading(p, {"t1": (0, 480), "t2": (480, 960)})["alice"]
    assert [(s.start, s.end, s.task_ids) for s in out] == [(0, 480, ("t1",)), (480, 960, ("t2",))]


def test_same_percent_and_tasks_merge_across_events():
    # t3 starts and ends inside nothing for alice: t1 and t2 split at 100 with no change
    p = _project(("t1", "alice", 50), ("t2", "alice", 50), ("t3", "alice", 0 + 1))
    out = compute_loading(p, {"t1": (0, 200), "t2": (0, 200), "t3": (50, 50)})["alice"]
    assert len(out) == 1 and (out[0].start, out[0].end, out[0].percent) == (0, 200, 100)


def test_ignored_cases_and_independent_resources():
    p = _project(("t1", "alice", 100), ("t2", "alice", 100), ("t3", "bob", 40))
    out = compute_loading(p, {"t1": (100, 100), "t2": None, "t3": (0, 480)})
    assert out["alice"] == ()
    assert [(s.start, s.end, s.percent) for s in out["bob"]] == [(0, 480, 40)]
    assert set(out) == {"alice", "bob"}


def test_resource_without_assignments_empty():
    p = _project()
    assert compute_loading(p, {}) == {"alice": (), "bob": ()}


def _agg_setup():
    p = _project(("t1", "alice", 100), ("t2", "alice", 50))
    return p, WorkingAxis(p.calendar, p.start)


def test_day_average_vs_peak():
    p, axis = _agg_setup()
    seg = compute_loading(p, {"t1": (0, 480), "t2": (0, 120)})["alice"]
    (b,) = aggregate(seg, axis, "day")
    assert b.period_start == axis.origin
    assert b.peak_percent == 150
    assert b.assigned_person_minutes == D(480 + 60)
    assert b.assigned_days == D(540) / D(480)
    assert b.average_percent == D(540) / D(480) * 100
    assert b.average_percent < b.peak_percent


def test_week_aggregation_sums_days():
    p, axis = _agg_setup()
    seg = compute_loading(p, {"t1": (0, 960), "t2": (480, 1440)})["alice"]
    (b,) = aggregate(seg, axis, "week")
    assert b.period_start == dt.date(2026, 10, 5)
    assert b.assigned_person_minutes == D(960 + 480)
    assert b.assigned_days == D(3)
    assert b.average_percent == D(1440) / D(5 * 480) * 100
    assert b.peak_percent == 150
    assert len(aggregate(seg, axis, "day")) == 3


def test_holiday_gives_no_bucket_or_capacity():
    b = ProjectBuilder().calendar(holidays=[(dt.date(2026, 10, 6), "H")])
    b.resource("alice").task("t1").assign("t1", "alice", 100)
    p = b.build()
    axis = WorkingAxis(p.calendar, p.start)
    seg = compute_loading(p, {"t1": (0, 960)})["alice"]
    days = aggregate(seg, axis, "day")
    assert [x.period_start for x in days] == [dt.date(2026, 10, 5), dt.date(2026, 10, 7)]
    (w,) = aggregate(seg, axis, "week")
    assert w.average_percent == D(960) / D(4 * 480) * 100


def test_bad_granularity():
    _, axis = _agg_setup()
    with pytest.raises(ValueError):
        aggregate((), axis, "month")  # type: ignore[arg-type]
