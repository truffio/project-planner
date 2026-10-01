"""Property tests for engine.calendar.WorkingAxis."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from project_planner.engine.calendar import WorkingAxis
from project_planner.engine.model import (
    Calendar,
    CalendarException,
    ExceptionKind,
    Holiday,
    Weekday,
)

pytestmark = pytest.mark.property

_BASE = dt.date(2026, 1, 1)
_KINDS = ["holiday", "working", "nonworking"]


@st.composite
def calendars(draw: st.DrawFn) -> Calendar:
    days = draw(st.sets(st.sampled_from(list(Weekday)), min_size=1))
    tenths = draw(st.integers(min_value=1, max_value=240))  # hours in 0.1 h steps
    minutes = tenths * 6
    start_min = draw(st.integers(min_value=0, max_value=24 * 60 - minutes))
    offsets = draw(st.lists(st.integers(min_value=0, max_value=400), max_size=40, unique=True))
    kinds = draw(st.lists(st.sampled_from(_KINDS), min_size=len(offsets), max_size=len(offsets)))
    holidays: list[Holiday] = []
    exceptions: list[CalendarException] = []
    for n, k in zip(offsets, kinds, strict=True):
        d = _BASE + dt.timedelta(days=n)
        if k == "holiday":
            holidays.append(Holiday(d))
        else:
            exceptions.append(CalendarException(d, ExceptionKind(k)))
    return Calendar(
        working_weekdays=frozenset(days),
        hours_per_day=Decimal(tenths) / 10,
        workday_start=dt.time(start_min // 60, start_min % 60),
        holidays=tuple(holidays),
        exceptions=tuple(exceptions),
    )


@st.composite
def axes(draw: st.DrawFn) -> WorkingAxis:
    cal = draw(calendars())
    offset = draw(st.integers(min_value=0, max_value=500))
    return WorkingAxis(cal, _BASE + dt.timedelta(days=offset))


minutes_st = st.integers(min_value=0, max_value=200_000)


@settings(max_examples=100, deadline=None)
@given(axes(), minutes_st)
def test_start_round_trip(a: WorkingAxis, m: int) -> None:
    assert a.to_axis(a.to_datetime(m, "start")) == m


@settings(max_examples=100, deadline=None)
@given(axes(), minutes_st)
def test_finish_round_trip(a: WorkingAxis, m: int) -> None:
    assert a.to_axis(a.to_datetime(m, "finish")) == m


@settings(max_examples=100, deadline=None)
@given(axes(), minutes_st, st.integers(min_value=1, max_value=5_000))
def test_start_monotonic(a: WorkingAxis, m: int, delta: int) -> None:
    assert a.to_datetime(m, "start") < a.to_datetime(m + delta, "start")
    assert a.to_datetime(m, "finish") <= a.to_datetime(m, "start")


@settings(max_examples=50, deadline=None)
@given(axes(), st.integers(min_value=0, max_value=300))
def test_every_working_day_has_constant_minutes(a: WorkingAxis, i: int) -> None:
    d = a.working_date(i)
    nxt = a.working_date(i + 1)
    assert a.is_working_day(d)
    assert a.first_working_minute(d) == i * a.minutes_per_day
    assert a.first_working_minute(nxt) - a.first_working_minute(d) == a.minutes_per_day
    gap = d + dt.timedelta(days=1)
    while gap < nxt:
        assert not a.is_working_day(gap)
        gap += dt.timedelta(days=1)
