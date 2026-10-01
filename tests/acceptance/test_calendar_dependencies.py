"""A04 (holiday crossing), A05 (dependency types x lag), A06 (several predecessors).

Hand-computed reference (default calendar, start Mon 5 Oct 2026, 480 min/day):
working-day index d0 Mon 5, d1 Tue 6, d2 Wed 7, d3 Thu 8, d4 Fri 9, d5 Mon 12,
d6 Tue 13, d7 Wed 14. Axis minute m -> day m // 480, clock 09:00 + m % 480 minutes.
Finish exactly on a day boundary is shown as the previous day 17:00.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

import project_planner as pp

from .conftest import (
    PROJECT_START,
    D,
    code,
    daily_loading,
    day_assigned,
    lag_of,
    new_project,
    new_ws,
    node_row,
    oct26,
)


def _lag(spec: str, as_object: bool):
    """Turn "-4h" / "1d" into either the string itself or pp.hours()/pp.days()."""
    if not as_object:
        return spec
    value = Decimal(spec[:-1])
    return pp.hours(value) if spec.endswith("h") else pp.days(value)


# ---------------------------------------------------------------- A04


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T11: holiday excluded from working time (A04)")
def test_a04_task_crossing_holiday_finishes_later_and_has_no_load_on_holiday():
    ws = new_ws()
    new_project(ws)
    ws.calendar.initialize(holidays=[(date(2026, 10, 8), "Holiday")])  # Thursday
    alice = ws.add_resource("Alice", hourly_rate="100")
    t = ws.add_task("Five days", duration=pp.days(5))
    ws.set_assignment(t, alice, percent=100)
    res = ws.schedule()
    row = node_row(res, t)
    # Working days: Mon 5, Tue 6, Wed 7, (Thu 8 holiday), Fri 9, Mon 12 -> finish Mon 12 17:00
    # (without the holiday it would be Fri 9 17:00).
    assert row.start == oct26(5, "09:00")
    assert row.finish == oct26(12, "17:00")
    # working duration is still 5 days; the holiday is a pause, not work
    assert row.duration_days == D(5)
    # cost counts only working hours: 40 h x 1.0 x 100 = 4000
    assert row.cost == D(4000)
    buckets = daily_loading(ws, alice)
    # no work or capacity on the holiday
    assert day_assigned(buckets, date(2026, 10, 8)) == D(0)
    # one person-day on each of the five working days
    for d in (5, 6, 7, 9, 12):
        assert day_assigned(buckets, date(2026, 10, d)) == D(1)
    assert sum(day_assigned(buckets, k) for k in buckets) == D(5)


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T11: fractional days and lag across a holiday (A04)")
def test_a04_fractional_chain_and_lag_across_holiday():
    ws = new_ws()
    new_project(ws)
    ws.calendar.initialize(holidays=[(date(2026, 10, 8), "Holiday")])
    a = ws.add_task("A", duration="2.5d")
    b = ws.add_task("B", duration="1.5d")
    c = ws.add_task("C", duration="4h")
    ws.add_dependency(a, b, "FS")
    ws.add_dependency(a, c, "FS", lag=pp.days(1))
    res = ws.schedule()
    # A: 2.5 d = 1200 min from Mon 5 09:00: Mon, Tue, Wed 09:00-13:00 -> finish Wed 7 13:00
    assert node_row(res, a).finish == oct26(7, "13:00")
    # B (FS 0) starts at A's finish Wed 7 13:00; 1.5 d = Wed 13:00-17:00 (0.5 d),
    # Thu 8 holiday, Fri 9 09:00-17:00 (1 d) -> finish Fri 9 17:00
    assert node_row(res, b).start == oct26(7, "13:00")
    assert node_row(res, b).finish == oct26(9, "17:00")
    # C: lag 1 d of working time after Wed 7 13:00 = Wed 13-17 (4 h) + Fri 09-13 (4 h)
    # -> start Fri 9 13:00; 4 h -> finish Fri 9 17:00
    assert node_row(res, c).start == oct26(9, "13:00")
    assert node_row(res, c).finish == oct26(9, "17:00")


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T11: working / nonworking date exceptions")
def test_calendar_exceptions_working_saturday_and_nonworking_weekday():
    ws = new_ws()
    new_project(ws)
    ws.calendar.initialize(
        exceptions=[(date(2026, 10, 6), "nonworking"), (date(2026, 10, 10), "working")]
    )
    t = ws.add_task("Five days", duration="5d")
    res = ws.schedule()
    # Working days: Mon 5, (Tue 6 nonworking), Wed 7, Thu 8, Fri 9, Sat 10 (working)
    # -> 5 days finish Sat 10 Oct 17:00
    assert node_row(res, t).start == oct26(5, "09:00")
    assert node_row(res, t).finish == oct26(10, "17:00")


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T11: start on nonworking day moves to next boundary")
def test_project_start_on_weekend_moves_to_next_working_boundary():
    ws = new_ws()
    new_project(ws, start=date(2026, 10, 3))  # Saturday
    t = ws.add_task("T", duration="1d")
    res = ws.schedule()
    # Sat 3 / Sun 4 nonworking -> first working boundary Mon 5 09:00; 1 d -> Mon 5 17:00
    assert node_row(res, t).start == oct26(5, "09:00")
    assert node_row(res, t).finish == oct26(5, "17:00")
    # the user is informed
    assert any(code(i.severity) == "info" for i in res.issues)


# ---------------------------------------------------------------- A05


def _a05_project(dep_type: str, lag, succ_duration: str = "1d"):
    """P0 (2 d, Mon 5-Tue 6) -FS-> P (2 d): P runs axis [960, 1920) = Wed 7 09:00-Thu 8 17:00.

    S (1 d = 480 min) depends on P with the given type and lag.
    """
    ws = new_ws()
    new_project(ws)
    p0 = ws.add_task("P0", duration="2d")
    p = ws.add_task("P", duration="2d")
    s = ws.add_task("S", duration=succ_duration)
    ws.add_dependency(p0, p, "FS")
    ws.add_dependency(p, s, dep_type, lag=lag)
    res = ws.schedule()
    assert node_row(res, p).start == oct26(7, "09:00")
    assert node_row(res, p).finish == oct26(8, "17:00")
    return res, p, s


# (type, lag, as_object, expected S start, expected S finish)
# ps = 960, pf = 1920, D = 480.
A05_CASES = [
    # FS: start = pf + lag
    ("FS", "1d", False, (12, "09:00"), (12, "17:00")),  # 2400 = d5 Mon 12 (lag crosses weekend)
    ("FS", "0d", False, (9, "09:00"), (9, "17:00")),  # 1920 = d4 Fri 9 09:00 -> 2400 Fri 17:00
    ("FS", "-4h", True, (8, "13:00"), (9, "13:00")),  # 1680 = d3+240; finish 2160 = d4+240
    ("FS", "4h", True, (9, "13:00"), (12, "13:00")),  # 2160 = d4+240; finish 2640 = d5+240
    ("FS", "-0.5d", True, (8, "13:00"), (9, "13:00")),  # -0.5 d = -240 min, same as -4h
    # SS: start = ps + lag
    ("SS", "1d", True, (8, "09:00"), (8, "17:00")),  # 1440 = d3; finish 1920 = Thu 17:00
    ("SS", "0h", False, (7, "09:00"), (7, "17:00")),  # 960 = d2; finish 1440 = Wed 17:00
    ("SS", "-4h", False, (6, "13:00"), (7, "13:00")),  # 720 = d1+240; finish 1200 = d2+240
    # FF: start = pf + lag - D
    ("FF", "1d", False, (9, "09:00"), (9, "17:00")),  # 1920+480-480 = 1920; finish 2400
    ("FF", "0d", True, (8, "09:00"), (8, "17:00")),  # 1440; finish 1920 = pf
    ("FF", "-4h", False, (7, "13:00"), (8, "13:00")),  # 1200; finish 1680 = pf - 240
    # SF: start = ps + lag - D
    ("SF", "1d", False, (7, "09:00"), (7, "17:00")),  # 960+480-480 = 960; finish 1440
    ("SF", "0h", True, (6, "09:00"), (6, "17:00")),  # 480; finish 960 = ps
    ("SF", "-4h", False, (5, "13:00"), (6, "13:00")),  # 240; finish 720
    ("SF", "-1d", False, (5, "09:00"), (5, "17:00")),  # 0 exactly (not clamped); finish 480
]


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T15: forward pass, each type x signed lag (A05)")
@pytest.mark.parametrize(("dep_type", "lag", "as_object", "start", "finish"), A05_CASES)
def test_a05_dependency_type_and_lag(dep_type, lag, as_object, start, finish):
    res, _, s = _a05_project(dep_type, _lag(lag, as_object))
    assert node_row(res, s).start == oct26(*start)
    assert node_row(res, s).finish == oct26(*finish)
    assert node_row(res, s).duration_days == D(1)


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T15: negative lag clamped at project start (A05)")
@pytest.mark.parametrize(
    ("dep_type", "lag"),
    [
        ("SS", "-3d"),  # 960 - 1440 = -480 -> clamped to 0
        ("FS", "-5d"),  # 1920 - 2400 = -480 -> clamped to 0
        ("SF", "-12h"),  # 960 - 720 - 480 = -240 -> clamped to 0
    ],
)
def test_a05_negative_lag_never_precedes_project_start(dep_type, lag):
    res, _, s = _a05_project(dep_type, lag)
    # start = max(0, constraint) = 0 -> Mon 5 09:00; 1 d -> Mon 5 17:00
    assert node_row(res, s).start == oct26(5, "09:00")
    assert node_row(res, s).finish == oct26(5, "17:00")


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T15: lag echoed in days (A05)")
def test_a05_lag_echoed_in_days():
    ws = new_ws()
    new_project(ws)
    a = ws.add_task("A", duration="1d")
    b = ws.add_task("B", duration="1d")
    dep = ws.add_dependency(a, b, "FS", lag="-4h")
    res = ws.schedule()
    # -4 h / 8 h per day = -0.5 working days; as-entered value/unit kept
    assert res.dependency(dep).lag_days == D("-0.5")
    assert lag_of(ws, dep) == (D(-4), "hours")


# ---------------------------------------------------------------- A06


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T15: several predecessors, binding one is last (A06)")
def test_a06_three_predecessors_last_one_binds():
    ws = new_ws()
    new_project(ws)
    a = ws.add_task("A", duration="1d")  # [0, 480)
    b = ws.add_task("B", duration="2d")  # [0, 960)
    c = ws.add_task("C", duration="4d")  # [0, 1920)  Mon 5 - Thu 8
    s = ws.add_task("S", duration="1d")  # D = 480
    ws.add_dependency(a, s, "FS")  # start >= 480
    ws.add_dependency(b, s, "SS", lag="4h")  # start >= 0 + 240 = 240
    ws.add_dependency(c, s, "FF")  # start >= 1920 - 480 = 1440   <- binding
    res = ws.schedule()
    # start = max(480, 240, 1440) = 1440 = d3 Thu 8 09:00; finish 1920 = Thu 8 17:00
    assert node_row(res, s).start == oct26(8, "09:00")
    assert node_row(res, s).finish == oct26(8, "17:00")
    # all three hold: S.start >= A.finish, S.start >= B.start + 4 h, S.finish >= C.finish
    assert node_row(res, s).start >= node_row(res, a).finish
    assert node_row(res, s).finish == node_row(res, c).finish
    assert res.project_finish == oct26(8, "17:00")


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T15: several predecessors, binding one is middle (A06)")
def test_a06_three_predecessors_middle_one_binds():
    ws = new_ws()
    new_project(ws)
    a = ws.add_task("A", duration="1d")  # [0, 480)
    b = ws.add_task("B", duration="2d")  # [0, 960)
    c = ws.add_task("C", duration="3d")  # [0, 1440)
    s = ws.add_task("S", duration="1d")  # D = 480
    ws.add_dependency(a, s, "FS")  # start >= 480
    # SF 3.5 d: start >= 0 + 1680 - 480 = 1200   <- binding
    ws.add_dependency(b, s, "SF", lag=pp.days(Decimal("3.5")))
    ws.add_dependency(c, s, "SS", lag="1d")  # start >= 0 + 480 = 480
    res = ws.schedule()
    # start = max(480, 1200, 480) = 1200 = d2 + 240 = Wed 7 13:00; finish 1680 = Thu 8 13:00
    assert node_row(res, s).start == oct26(7, "13:00")
    assert node_row(res, s).finish == oct26(8, "13:00")


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T19: plan section 1.2 example end to end (A05/A16)")
def test_plan_section_1_2_example():
    ws = new_ws()
    ws.new_project("Demo", start=PROJECT_START, currency="USD", cost_report_unit="person_days")
    ws.calendar.initialize(
        hours_per_day=8,
        working_days_per_year=220,
        working_weekdays="Mon-Fri",
        workday_start="09:00",
        holidays=[(date(2026, 10, 12), "Holiday")],
        exceptions=[],
    )
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="50")
    ph1 = ws.add_group("Phase 1")
    t1 = ws.add_task("Design", parent=ph1, effort=pp.hours(40))
    ws.set_assignment(t1, alice, percent=80)
    ws.set_assignment(t1, bob, percent=20)
    t2 = ws.add_task("Build", parent=ph1, duration=pp.days(2))
    ws.add_dependency(t1, t2, "FS", lag=pp.days(-0.5))
    res = ws.schedule()
    # t1: 40 h / 1.0 = 2400 min -> Mon 5 09:00 - Fri 9 17:00
    assert node_row(res, t1).finish == oct26(9, "17:00")
    # t2 start = 2400 - 240 = 2160 = Fri 9 13:00; 2 d = Fri 13-17, (Mon 12 holiday),
    # Tue 13 full, Wed 14 09:00-13:00 -> finish Wed 14 13:00 (axis 3120)
    assert node_row(res, t2).start == oct26(9, "13:00")
    assert node_row(res, t2).finish == oct26(14, "13:00")
    assert res.complete is True
    assert res.project_finish == oct26(14, "13:00")
    # working span 3120 / 480 = 6.5 (5 - 0.5 lag + 2)
    assert res.working_span_days == D("6.5")
    # elapsed calendar span (Mon 5 -> Wed 14) exceeds the working span
    assert res.elapsed_span_calendar_days > res.working_span_days
    # Alice 32 h x 100 + Bob 8 h x 50 = 3600; t2 is unassigned -> 0
    assert res.total_cost == D(3600)
    assert node_row(res, ph1).cost == D(3600)
