"""Unit tests for engine.calendar.WorkingAxis."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fixtures.builders import DEFAULT_START

from project_planner.engine.calendar import WorkingAxis
from project_planner.engine.errors import Severity
from project_planner.engine.model import Calendar, CalendarSettings

pytestmark = pytest.mark.unit

D = dt.date
T = dt.datetime


def cal(**kw: Any) -> Calendar:
    return CalendarSettings.from_values(**kw).to_calendar()


def axis(start: dt.date = DEFAULT_START, **kw: Any) -> WorkingAxis:
    return WorkingAxis(cal(**kw), start)


def test_start_is_monday() -> None:
    assert DEFAULT_START.weekday() == 0


def test_origin_and_day_boundary_conventions() -> None:
    a = axis()
    assert a.minutes_per_day == 480
    assert a.issues == ()
    assert a.to_datetime(0, "start") == T(2026, 10, 5, 9, 0)
    assert a.to_datetime(0, "finish") == T(2026, 10, 5, 9, 0)
    assert a.to_datetime(480, "finish") == T(2026, 10, 5, 17, 0)
    assert a.to_datetime(480, "start") == T(2026, 10, 6, 9, 0)


def test_weekend_skipped_fs_zero_lag() -> None:
    a = axis()
    m = 5 * 480
    assert a.to_datetime(m, "finish") == T(2026, 10, 9, 17, 0)  # Fri
    assert a.to_datetime(m, "start") == T(2026, 10, 12, 9, 0)  # Mon
    assert a.to_axis(T(2026, 10, 9, 17, 0)) == m
    assert a.to_axis(T(2026, 10, 12, 9, 0)) == m
    # Weekend datetimes map to the next working start.
    assert a.to_axis(T(2026, 10, 10, 12, 0)) == m
    assert a.to_axis(T(2026, 10, 11, 23, 59)) == m


def test_twelve_hours_is_one_and_a_half_days() -> None:
    a = axis()
    assert a.to_datetime(720, "start") == T(2026, 10, 6, 13, 0)
    assert a.to_datetime(720, "finish") == T(2026, 10, 6, 13, 0)


def test_clamping() -> None:
    a = axis()
    assert a.to_axis(T(2026, 10, 6, 7, 0)) == 480
    assert a.to_axis(T(2026, 10, 6, 20, 0)) == 960
    assert a.to_axis(T(2026, 10, 6, 9, 0)) == 480
    assert a.to_axis(T(2026, 10, 6, 12, 30)) == 480 + 210
    # Before origin clamps to 0.
    assert a.to_axis(T(2020, 1, 1)) == 0
    assert a.to_axis(T(2026, 10, 5, 6, 0)) == 0


def test_negative_minute_raises() -> None:
    a = axis()
    with pytest.raises(ValueError):
        a.to_datetime(-1, "start")
    with pytest.raises(ValueError):
        a.day_index(-1)
    with pytest.raises(ValueError):
        a.working_date(-1)


def test_holiday_midweek() -> None:
    a = axis(holidays=[D(2026, 10, 7)])
    assert a.to_datetime(2 * 480, "start") == T(2026, 10, 8, 9, 0)
    assert a.to_datetime(2 * 480, "finish") == T(2026, 10, 6, 17, 0)
    assert a.working_date(2) == D(2026, 10, 8)
    assert a.first_working_minute(D(2026, 10, 7)) == 2 * 480


def test_exceptional_working_saturday() -> None:
    a = axis(exceptions=[(D(2026, 10, 10), "working")])
    assert a.to_datetime(5 * 480, "start") == T(2026, 10, 10, 9, 0)
    assert a.to_datetime(6 * 480, "start") == T(2026, 10, 12, 9, 0)
    assert a.to_datetime(6 * 480, "finish") == T(2026, 10, 10, 17, 0)
    assert a.first_working_minute(D(2026, 10, 10)) == 5 * 480


def test_nonworking_weekday_exception() -> None:
    a = axis(exceptions=[(D(2026, 10, 6), "nonworking")])
    assert a.to_datetime(480, "start") == T(2026, 10, 7, 9, 0)
    assert a.first_working_minute(D(2026, 10, 6)) == 480


def test_year_boundary() -> None:
    assert D(2026, 12, 31).weekday() == 3
    a = axis(start=D(2026, 12, 31))
    assert a.to_datetime(0, "start") == T(2026, 12, 31, 9, 0)
    assert a.to_datetime(480, "start") == T(2027, 1, 1, 9, 0)  # Friday
    assert a.to_datetime(960, "start") == T(2027, 1, 4, 9, 0)  # Monday
    assert a.to_axis(T(2027, 1, 1, 9, 0)) == 480


def test_start_on_holiday_moves_with_info_issue() -> None:
    a = axis(holidays=[DEFAULT_START])
    assert a.origin == D(2026, 10, 6)
    assert a.to_datetime(0, "start") == T(2026, 10, 6, 9, 0)
    (issue,) = a.issues
    assert issue.severity is Severity.INFO
    assert issue.code == "CAL_START_MOVED"
    assert issue.object_type == "project"
    assert issue.field == "start"


def test_start_on_saturday_moves() -> None:
    sat = D(2026, 10, 10)
    assert sat.weekday() == 5
    a = axis(start=sat)
    assert a.to_datetime(0, "start") == T(2026, 10, 12, 9, 0)
    assert len(a.issues) == 1


def test_7_5h_from_0830() -> None:
    a = axis(hours_per_day="7.5", workday_start="08:30")
    assert a.minutes_per_day == 450
    assert a.to_datetime(0, "start") == T(2026, 10, 5, 8, 30)
    assert a.to_datetime(450, "finish") == T(2026, 10, 5, 16, 0)
    assert a.to_datetime(450, "start") == T(2026, 10, 6, 8, 30)
    assert a.to_datetime(225, "start") == T(2026, 10, 5, 12, 15)
    assert a.to_axis(T(2026, 10, 5, 16, 0)) == 450
    assert a.to_axis(T(2026, 10, 5, 8, 0)) == 0


def test_sunday_to_thursday_week() -> None:
    a = axis(working_weekdays="Sun,Mon,Tue,Wed,Thu")
    # Start Monday 2026-10-05; Thu is day 3; Fri/Sat skipped; Sunday next.
    assert a.to_datetime(4 * 480, "start") == T(2026, 10, 11, 9, 0)
    assert D(2026, 10, 11).weekday() == 6
    assert a.to_datetime(4 * 480, "finish") == T(2026, 10, 8, 17, 0)
    assert a.to_axis(T(2026, 10, 9, 10, 0)) == 4 * 480


def test_six_day_week() -> None:
    a = axis(working_weekdays="Mon,Tue,Wed,Thu,Fri,Sat")
    assert a.to_datetime(5 * 480, "start") == T(2026, 10, 10, 9, 0)
    assert a.to_datetime(6 * 480, "start") == T(2026, 10, 12, 9, 0)
    assert a.to_datetime(6 * 480, "finish") == T(2026, 10, 10, 17, 0)


def test_working_date_and_day_index() -> None:
    a = axis()
    assert a.day_index(0) == 0
    assert a.day_index(479) == 0
    assert a.day_index(480) == 1
    assert a.working_date(0) == DEFAULT_START
    assert a.working_date(5) == D(2026, 10, 12)


def test_first_working_minute_before_origin_is_zero() -> None:
    assert axis().first_working_minute(D(2000, 1, 1)) == 0


def test_long_horizon() -> None:
    a = axis()
    m = 480 * 220 * 40
    d = a.to_datetime(m, "start")
    assert a.to_axis(d) == m
    assert d.year >= 2060


def test_nonworking_ranges_merge() -> None:
    a = axis(holidays=[D(2026, 10, 12)])  # Monday holiday extends the weekend
    r = a.nonworking_ranges(T(2026, 10, 5), T(2026, 10, 18, 12))
    assert r == [(D(2026, 10, 10), D(2026, 10, 12)), (D(2026, 10, 17), D(2026, 10, 18))]


def test_nonworking_ranges_window_edges() -> None:
    a = axis()
    assert a.nonworking_ranges(T(2026, 10, 5), T(2026, 10, 9)) == []
    assert a.nonworking_ranges(T(2026, 10, 11), T(2026, 10, 11)) == [
        (D(2026, 10, 11), D(2026, 10, 11))
    ]
    assert a.nonworking_ranges(T(2026, 10, 9), T(2026, 10, 10)) == [
        (D(2026, 10, 10), D(2026, 10, 10))
    ]


def test_bad_boundary() -> None:
    with pytest.raises(ValueError):
        axis().to_datetime(1, "middle")  # type: ignore[arg-type]
