"""Unit tests for engine.model, engine.errors and engine.config (task T10)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import pickle
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pytest

from project_planner.engine import config
from project_planner.engine.errors import (
    Cancelled,
    Conflict,
    ImportFailed,
    Issue,
    NotFound,
    ObjectType,
    PlannerError,
    Severity,
    UnsavedChanges,
    ValidationFailed,
)
from project_planner.engine.model import (
    DEFAULT_WORKING_WEEKDAYS,
    Assignment,
    Calendar,
    CalendarException,
    CalendarSettings,
    Dependency,
    DependencyType,
    ExceptionKind,
    Holiday,
    NodeKind,
    Project,
    Resource,
    SizingMode,
    TimeQty,
    TimeUnit,
    WbsNode,
    Weekday,
    WorkUnit,
    as_time_qty,
    days,
    hours,
    minutes_to_days,
    parse_weekdays,
)

pytestmark = pytest.mark.unit

D = Decimal
START = dt.date(2026, 10, 5)


def codes(exc: ValidationFailed) -> list[str]:
    return [i.code for i in exc.issues]


def fields(exc: ValidationFailed | list[Issue]) -> set[str | None]:
    issues = exc.issues if isinstance(exc, ValidationFailed) else exc
    return {i.field for i in issues}


# =============================================================================
# Enums
# =============================================================================


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (NodeKind, ["group", "task", "milestone"]),
        (SizingMode, ["duration", "effort", "none"]),
        (TimeUnit, ["hours", "days"]),
        (DependencyType, ["FS", "SS", "FF", "SF"]),
        (WorkUnit, ["person_hours", "person_days", "person_years"]),
        (ExceptionKind, ["working", "nonworking"]),
        (Weekday, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]),
        (Severity, ["error", "warning", "info"]),
    ],
)
def test_enum_string_values(enum: Any, values: list[str]) -> None:
    assert [m.value for m in enum] == values
    for m, v in zip(enum, values, strict=True):
        assert m == v and str(m) == v and enum(v) is m


def test_weekday_python_conversions() -> None:
    for i in range(7):
        assert Weekday.from_python_weekday(i).python_weekday == i
    assert Weekday.MON.python_weekday == 0 and Weekday.SUN.python_weekday == 6
    assert Weekday.of(START) is Weekday.MON
    assert Weekday.of(dt.date(2026, 10, 11)) is Weekday.SUN
    for bad in (-1, 7, True):
        with pytest.raises(ValueError):
            Weekday.from_python_weekday(bad)


def test_weekday_parse_and_order() -> None:
    assert Weekday.parse(" tUe ") is Weekday.TUE
    for bad in ("Tuesday", "1", ""):
        with pytest.raises(ValueError):
            Weekday.parse(bad)
    assert Weekday.ordered({Weekday.SUN, Weekday.MON, Weekday.FRI}) == (
        Weekday.MON,
        Weekday.FRI,
        Weekday.SUN,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mon-Fri", "Mon Tue Wed Thu Fri"),
        ("mon;TUE; thu", "Mon Tue Thu"),
        ("Sun-Thu", "Sun Mon Tue Wed Thu"),
        ("Sat, Sun", "Sat Sun"),
        ("Mon Mon", "Mon"),
        ("", ""),
    ],
)
def test_parse_weekdays(text: str, expected: str) -> None:
    assert parse_weekdays(text) == frozenset(Weekday(t) for t in expected.split())


@pytest.mark.parametrize("text", ["Mon-", "Monday", "Mon-Xyz", "1-5"])
def test_parse_weekdays_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_weekdays(text)


def test_work_unit_hours_per_unit() -> None:
    cal = Calendar(hours_per_day=D("7.5"), working_days_per_year=200)
    assert WorkUnit.PERSON_HOURS.hours_per_unit(cal) == 1
    assert WorkUnit.PERSON_DAYS.hours_per_unit(cal) == D("7.5")
    assert WorkUnit.PERSON_YEARS.hours_per_unit(cal) == 1500


def test_time_unit_symbols() -> None:
    assert TimeUnit.HOURS.symbol == "h" and TimeUnit.DAYS.symbol == "d"
    assert TimeUnit.from_symbol("h") is TimeUnit.HOURS
    assert TimeUnit.from_symbol("d") is TimeUnit.DAYS
    with pytest.raises(ValueError):
        TimeUnit.from_symbol("H")


# =============================================================================
# TimeQty
# =============================================================================


def test_hours_days_constructors() -> None:
    assert hours(40) == TimeQty(D(40), TimeUnit.HOURS)
    assert days("1.5") == TimeQty(D("1.5"), TimeUnit.DAYS)
    assert days(D("-0.5")).value == D("-0.5")
    assert hours(" 2 ").value == 2
    q = days("1.50")
    assert str(q.value) == "1.50"  # kept exactly as entered
    assert isinstance(hours(1).value, Decimal)


@pytest.mark.parametrize("bad", [1.5, 0.0, True, False, None, [1]])
@pytest.mark.parametrize("ctor", [hours, days])
def test_hours_days_reject_float_bool_and_others(ctor: Any, bad: Any) -> None:
    with pytest.raises(TypeError):
        ctor(bad)


@pytest.mark.parametrize("bad", ["abc", "1.5d", "", "nan", "inf"])
def test_hours_days_reject_bad_strings(bad: str) -> None:
    with pytest.raises(ValidationFailed) as ei:
        days(bad)
    assert codes(ei.value) == ["TIME_INVALID"]


def test_raw_constructor_rules() -> None:
    assert TimeQty(5, "days") == days(5)  # type: ignore[arg-type]
    assert TimeQty(D(5), "days").unit is TimeUnit.DAYS  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TimeQty(1.5, TimeUnit.DAYS)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TimeQty(True, TimeUnit.DAYS)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TimeQty(D(1), "d")  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        TimeQty(D("NaN"), TimeUnit.DAYS)


def test_time_qty_equality_hash_str() -> None:
    assert days("1.5") == days("1.50")
    assert hash(days("1.5")) == hash(days("1.50"))
    assert hours(8) != days(1)
    assert {hours(1), hours("1.0"), days(1)} == {hours(1), days(1)}
    assert str(hours(40)) == "40h"
    assert str(days("-0.5")) == "-0.5d"
    assert str(days("1.50")) == "1.50d"
    with pytest.raises(dataclasses.FrozenInstanceError):
        hours(1).value = D(2)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("text", "value", "unit"),
    [
        ("40h", "40", TimeUnit.HOURS),
        ("5d", "5", TimeUnit.DAYS),
        ("-0.5d", "-0.5", TimeUnit.DAYS),
        ("+2h", "2", TimeUnit.HOURS),
        ("1.5 d", "1.5", TimeUnit.DAYS),
        ("  1.5\tD  ", "1.5", TimeUnit.DAYS),
        ("0.25H", "0.25", TimeUnit.HOURS),
        (".5d", "0.5", TimeUnit.DAYS),
        ("3 hours", "3", TimeUnit.HOURS),
        ("1 Day", "1", TimeUnit.DAYS),
        ("1e1h", "10", TimeUnit.HOURS),
        ("0d", "0", TimeUnit.DAYS),
    ],
)
def test_parse_valid(text: str, value: str, unit: TimeUnit) -> None:
    q = TimeQty.parse(text)
    assert q == TimeQty(D(value), unit)
    assert isinstance(q.value, Decimal)


def test_parse_keeps_digits() -> None:
    assert str(TimeQty.parse("1.50d").value) == "1.50"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("40", "TIME_UNITLESS"),
        ("-0.5", "TIME_UNITLESS"),
        (" 7 ", "TIME_UNITLESS"),
        ("40x", "TIME_BAD_UNIT"),
        ("5 weeks", "TIME_BAD_UNIT"),
        ("5e", "TIME_BAD_UNIT"),
        ("", "TIME_INVALID"),
        ("h", "TIME_INVALID"),
        ("abc", "TIME_INVALID"),
        ("1.2.3d", "TIME_INVALID"),
        ("4 0h", "TIME_INVALID"),
        ("--1d", "TIME_INVALID"),
    ],
)
def test_parse_invalid(text: str, code: str) -> None:
    with pytest.raises(ValidationFailed) as ei:
        TimeQty.parse(text)
    assert codes(ei.value) == [code]


def test_parse_attribution_and_type() -> None:
    with pytest.raises(ValidationFailed) as ei:
        TimeQty.parse("40", object_type="node", object_id="t1", field="sizing")
    (issue,) = ei.value.issues
    assert (issue.severity, issue.object_type, issue.object_id, issue.field) == (
        Severity.ERROR,
        "node",
        "t1",
        "sizing",
    )
    assert "40h" in issue.message and "40d" in issue.message
    with pytest.raises(TypeError):
        TimeQty.parse(40)  # type: ignore[arg-type]


def test_as_time_qty() -> None:
    q = hours(3)
    assert as_time_qty(q) is q
    assert as_time_qty("2d") == days(2)
    with pytest.raises(ValidationFailed) as ei:
        as_time_qty("40", field="effort")
    assert codes(ei.value) == ["TIME_UNITLESS"] and fields(ei.value) == {"effort"}


@pytest.mark.parametrize("bare", [40, 1.5, D("2"), True, None])
def test_as_time_qty_rejects_bare_numbers(bare: Any) -> None:
    with pytest.raises(TypeError) as ei:
        as_time_qty(bare)
    msg = str(ei.value)
    assert "hours" in msg and "days" in msg


@pytest.mark.parametrize(
    ("qty", "mpd", "minutes"),
    [
        (days("1.5"), 480, 720),
        (days(5), 480, 2400),
        (hours(40), 480, 2400),
        (days(5), 450, 2250),
        (hours(40), 450, 2400),  # hours do not depend on hours/day
        (days("0.5"), 450, 225),
        (hours("0.01"), 480, 1),  # 0.6 min -> 1 (ceil)
        (hours("1.001"), 480, 61),  # 60.06 -> 61
        (days("0.001"), 450, 1),  # 0.45 -> 1
        (days(0), 480, 0),
        (days("-0.5"), 480, -240),
        (hours("-0.01"), 480, 0),  # -0.6 min -> 0 (ceil toward +infinity)
        (hours("-0.025"), 480, -1),  # -1.5 min -> -1
    ],
)
def test_to_minutes(qty: TimeQty, mpd: int, minutes: int) -> None:
    assert qty.to_minutes(mpd) == minutes
    assert isinstance(qty.to_minutes(mpd), int)


@pytest.mark.parametrize("bad", [0, -1, True, D(480), "480"])
def test_to_minutes_rejects_bad_minutes_per_day(bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        days(1).to_minutes(bad)


def test_minutes_to_days() -> None:
    assert minutes_to_days(2400, 450) == D(2400) / D(450)
    assert minutes_to_days(720, 480) == D("1.5")
    assert minutes_to_days(-240, 480) == D("-0.5")
    assert minutes_to_days(0, 480) == 0
    assert isinstance(minutes_to_days(1, 3), Decimal)
    with pytest.raises(TypeError):
        minutes_to_days(1.5, 480)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        minutes_to_days(1, 0)


# =============================================================================
# Calendar / CalendarSettings
# =============================================================================


def test_calendar_settings_defaults() -> None:
    s = CalendarSettings()
    assert s.working_weekdays == DEFAULT_WORKING_WEEKDAYS
    assert s.working_weekdays == {Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI}
    assert s.hours_per_day == 8 and isinstance(s.hours_per_day, Decimal)
    assert s.working_days_per_year == 220
    assert s.workday_start == dt.time(9, 0)
    assert s.holidays == () and s.exceptions == ()
    assert s.validate() == []
    assert s == CalendarSettings()
    assert hash(s) == hash(CalendarSettings())
    assert s == CalendarSettings.from_values()
    assert s.to_calendar() == Calendar()
    assert Calendar().settings() == s
    assert Calendar().minutes_per_day == 480
    assert Calendar().workday_start_minute == 540


def test_calendar_settings_normalises_and_compares_by_value() -> None:
    a = CalendarSettings(
        hours_per_day=D("7.5"),
        working_weekdays=frozenset({Weekday.MON}),
        holidays=(Holiday(dt.date(2026, 12, 25), "Xmas"), Holiday(dt.date(2026, 10, 12), "H")),
    )
    b = CalendarSettings.from_values(
        hours_per_day="7.50",
        working_weekdays="mon",
        holidays=[(dt.date(2026, 10, 12), "H"), (dt.date(2026, 12, 25), "Xmas")],
    )
    assert a == b
    assert a.holidays[0].date == dt.date(2026, 10, 12)  # sorted by date
    assert CalendarSettings(hours_per_day=8) == CalendarSettings()  # type: ignore[arg-type]


def test_from_values_accepts_loose_forms() -> None:
    s = CalendarSettings.from_values(
        working_weekdays=["Sun", Weekday.MON, "tue"],
        hours_per_day=D("7.5"),
        working_days_per_year="250",
        workday_start="08:30",
        holidays=[dt.date(2026, 10, 8), (dt.date(2026, 10, 9),), Holiday(dt.date(2026, 10, 7))],
        exceptions=[
            (dt.date(2026, 10, 10), "Working"),
            (dt.date(2026, 10, 13), ExceptionKind.NONWORKING, "Closed"),
            CalendarException(dt.date(2026, 10, 14), ExceptionKind.WORKING),
        ],
    )
    assert s.working_weekdays == {Weekday.SUN, Weekday.MON, Weekday.TUE}
    assert s.working_days_per_year == 250
    assert s.workday_start == dt.time(8, 30)
    assert [h.date.day for h in s.holidays] == [7, 8, 9]
    assert s.exceptions[0].kind is ExceptionKind.WORKING
    assert s.exceptions[1].name == "Closed"
    cal = s.to_calendar()
    assert cal.minutes_per_day == 450 and cal.workday_start_minute == 510
    assert cal.settings() == s


@pytest.mark.parametrize(
    ("kwargs", "code", "field"),
    [
        ({"hours_per_day": 0}, "CAL_HOURS_PER_DAY_RANGE", "hours_per_day"),
        ({"hours_per_day": -1}, "CAL_HOURS_PER_DAY_RANGE", "hours_per_day"),
        ({"hours_per_day": 25}, "CAL_HOURS_PER_DAY_RANGE", "hours_per_day"),
        ({"hours_per_day": "24.01"}, "CAL_HOURS_PER_DAY_RANGE", "hours_per_day"),
        ({"hours_per_day": "7.333"}, "CAL_HOURS_PER_DAY_PRECISION", "hours_per_day"),
        ({"hours_per_day": "abc"}, "CAL_BAD_VALUE", "hours_per_day"),
        ({"hours_per_day": 7.5}, "CAL_BAD_VALUE", "hours_per_day"),
        ({"hours_per_day": True}, "CAL_BAD_VALUE", "hours_per_day"),
        ({"hours_per_day": "Infinity"}, "CAL_BAD_VALUE", "hours_per_day"),
        ({"working_days_per_year": 0}, "CAL_DAYS_PER_YEAR_RANGE", "working_days_per_year"),
        ({"working_days_per_year": 367}, "CAL_DAYS_PER_YEAR_RANGE", "working_days_per_year"),
        ({"working_days_per_year": D("220.5")}, "CAL_BAD_VALUE", "working_days_per_year"),
        ({"working_days_per_year": 220.0}, "CAL_BAD_VALUE", "working_days_per_year"),
        ({"working_days_per_year": "x"}, "CAL_BAD_VALUE", "working_days_per_year"),
        ({"working_weekdays": ""}, "CAL_NO_WORKING_WEEKDAYS", "working_weekdays"),
        ({"working_weekdays": []}, "CAL_NO_WORKING_WEEKDAYS", "working_weekdays"),
        ({"working_weekdays": "Monday"}, "CAL_BAD_VALUE", "working_weekdays"),
        ({"working_weekdays": [1]}, "CAL_BAD_VALUE", "working_weekdays"),
        ({"workday_start": "17:00"}, "CAL_WORKDAY_OVERFLOW", "workday_start"),
        ({"workday_start": "25:00"}, "CAL_BAD_VALUE", "workday_start"),
        ({"workday_start": "9h"}, "CAL_BAD_VALUE", "workday_start"),
        ({"workday_start": dt.time(9, 0, 30)}, "CAL_BAD_VALUE", "workday_start"),
        ({"holidays": "2026-10-12"}, "CAL_BAD_VALUE", "holidays"),
        ({"holidays": [dt.datetime(2026, 10, 12)]}, "CAL_BAD_VALUE", "holidays"),
        ({"holidays": [("2026-10-12", "H")]}, "CAL_BAD_VALUE", "holidays"),
        (
            {"holidays": [dt.date(2026, 10, 12), (dt.date(2026, 10, 12), "B")]},
            "CAL_DUPLICATE_HOLIDAY",
            "holidays",
        ),
        ({"exceptions": [(dt.date(2026, 10, 10), "maybe")]}, "CAL_BAD_VALUE", "exceptions"),
        ({"exceptions": [dt.date(2026, 10, 10)]}, "CAL_BAD_VALUE", "exceptions"),
        (
            {
                "exceptions": [
                    (dt.date(2026, 10, 10), "working"),
                    (dt.date(2026, 10, 10), "nonworking"),
                ]
            },
            "CAL_DUPLICATE_EXCEPTION",
            "exceptions",
        ),
        (
            {
                "holidays": [dt.date(2026, 10, 12)],
                "exceptions": [(dt.date(2026, 10, 12), "working")],
            },
            "CAL_HOLIDAY_EXCEPTION_CONFLICT",
            "exceptions",
        ),
    ],
)
def test_calendar_single_rule(kwargs: dict[str, Any], code: str, field: str) -> None:
    s = CalendarSettings(**kwargs)  # never raises
    issues = s.validate()
    assert [(i.code, i.field) for i in issues] == [(code, field)]
    assert all(i.severity is Severity.ERROR and i.object_type == "calendar" for i in issues)
    for build in (s.to_calendar, lambda: CalendarSettings.from_values(**kwargs)):
        with pytest.raises(ValidationFailed) as ei:
            build()
        assert codes(ei.value) == [code]
    with pytest.raises(ValidationFailed):
        Calendar(**kwargs)


@pytest.mark.parametrize(
    ("hpd", "start"), [("24", "00:00"), ("8", "16:00"), ("7.5", "16:30"), ("0.05", "23:57")]
)
def test_calendar_boundary_values_accepted(hpd: str, start: str) -> None:
    cal = CalendarSettings.from_values(hours_per_day=hpd, workday_start=start).to_calendar()
    assert cal.workday_start_minute + cal.minutes_per_day == 24 * 60 or hpd in ("8", "7.5")
    assert Calendar(working_days_per_year=1).working_days_per_year == 1
    assert Calendar(working_days_per_year=366).working_days_per_year == 366


def test_hours_per_day_precision_is_exact() -> None:
    # 1/60 h rounds to 28 digits; 60 * that is 1.000...0002 exactly -> not whole minutes
    issues = CalendarSettings(hours_per_day=D(1) / D(60)).validate()
    assert [i.code for i in issues] == ["CAL_HOURS_PER_DAY_PRECISION"]
    assert Calendar(hours_per_day=D("0.05")).minutes_per_day == 3


def test_to_minutes_is_exact_beyond_context_precision() -> None:
    tiny = D("1E-30")  # 60 * tiny is far below one minute but positive
    assert hours(tiny).to_minutes(480) == 1
    assert hours(-tiny).to_minutes(480) == 0
    assert days(D("12345.123456789")).to_minutes(480) == 12345 * 480 + 60


def test_cross_field_rule_with_other_errors() -> None:
    issues = CalendarSettings(
        hours_per_day=D(8),
        workday_start="20:00",  # type: ignore[arg-type]
        working_days_per_year=400,
    ).validate()
    assert {(i.code, i.field) for i in issues} == {
        ("CAL_WORKDAY_OVERFLOW", "workday_start"),
        ("CAL_DAYS_PER_YEAR_RANGE", "working_days_per_year"),
    }


def test_cross_field_rule_skipped_when_hours_invalid() -> None:
    issues = CalendarSettings(hours_per_day=D(30), workday_start="20:00").validate()  # type: ignore[arg-type]
    assert [i.code for i in issues] == ["CAL_HOURS_PER_DAY_RANGE"]


def test_multiple_simultaneous_errors_all_reported() -> None:
    with pytest.raises(ValidationFailed) as ei:
        CalendarSettings.from_values(
            hours_per_day=0,
            working_days_per_year=0,
            working_weekdays="",
            workday_start="25:00",
            holidays=[(dt.date(2026, 10, 12), "A"), (dt.date(2026, 10, 12), "B")],
            exceptions=[(dt.date(2026, 10, 10), "working"), (dt.date(2026, 10, 10), "nonworking")],
        )
    assert fields(ei.value) == {
        "hours_per_day",
        "working_days_per_year",
        "working_weekdays",
        "workday_start",
        "holidays",
        "exceptions",
    }
    assert len(ei.value.issues) == 6


def test_calendar_is_frozen_and_hashable() -> None:
    cal = Calendar(holidays=(Holiday(dt.date(2026, 10, 12), "H"),))
    assert hash(cal) == hash(Calendar(holidays=(Holiday(dt.date(2026, 10, 12), "H"),)))
    assert cal != Calendar()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cal.hours_per_day = D(7)  # type: ignore[misc]


def test_holiday_and_exception_types() -> None:
    with pytest.raises(TypeError):
        Holiday(dt.datetime(2026, 10, 12))
    with pytest.raises(TypeError):
        Holiday(dt.date(2026, 10, 12), None)  # type: ignore[arg-type]
    assert CalendarException(dt.date(2026, 10, 10), "working").kind is ExceptionKind.WORKING  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        CalendarException(dt.date(2026, 10, 10), "Working")  # type: ignore[arg-type]


# =============================================================================
# WbsNode, Resource, Assignment, Dependency
# =============================================================================


def task(**kw: Any) -> WbsNode:
    base: dict[str, Any] = {"id": "t1", "name": "T", "kind": NodeKind.TASK}
    base.update(kw)
    return WbsNode(**base)


def test_wbs_node_valid_combinations() -> None:
    assert task().sizing is None and not task().is_sized
    assert task(sizing_mode=SizingMode.DURATION, sizing=days(2)).is_sized
    assert task(sizing_mode="effort", sizing=hours(0)).sizing_mode is SizingMode.EFFORT
    g = WbsNode("g1", "G", "group")  # type: ignore[arg-type]
    assert g.kind is NodeKind.GROUP and g.parent_id is None and g.order == 0
    m = WbsNode("m1", "M", NodeKind.MILESTONE, parent_id="g1", order=-3)
    assert m.sizing_mode is SizingMode.NONE and m.order == -3


@pytest.mark.parametrize(
    ("kw", "code", "field"),
    [
        ({"kind": NodeKind.GROUP, "sizing_mode": SizingMode.DURATION, "sizing": days(1)},
         "NODE_SIZING_NOT_ALLOWED", "sizing_mode"),
        ({"kind": NodeKind.GROUP, "sizing": days(1)}, "NODE_SIZING_NOT_ALLOWED", "sizing"),
        ({"kind": NodeKind.MILESTONE, "sizing_mode": SizingMode.EFFORT},
         "NODE_SIZING_NOT_ALLOWED", "sizing_mode"),
        ({"kind": NodeKind.MILESTONE, "sizing": days(0)}, "NODE_SIZING_NOT_ALLOWED", "sizing"),
        ({"sizing_mode": SizingMode.DURATION}, "NODE_SIZING_PARTIAL", "sizing"),
        ({"sizing": hours(4)}, "NODE_SIZING_PARTIAL", "sizing_mode"),
        ({"sizing_mode": SizingMode.DURATION, "sizing": days("-0.5")},
         "NODE_NEGATIVE_SIZING", "sizing"),
        ({"sizing_mode": SizingMode.EFFORT, "sizing": hours(-1)}, "NODE_NEGATIVE_SIZING", "sizing"),
        ({"kind": "summary"}, "INVALID_VALUE", "kind"),
        ({"sizing_mode": "Duration", "sizing": days(1)}, "INVALID_VALUE", "sizing_mode"),
        ({"id": ""}, "INVALID_ID", "id"),
        ({"id": " t1"}, "INVALID_ID", "id"),
        ({"parent_id": "g1 "}, "INVALID_ID", "parent_id"),
    ],
)  # fmt: skip
def test_wbs_node_rejections(kw: dict[str, Any], code: str, field: str) -> None:
    with pytest.raises(ValidationFailed) as ei:
        task(**kw)
    (issue,) = ei.value.issues
    assert (issue.code, issue.field, issue.object_type) == (code, field, "node")


@pytest.mark.parametrize(
    "kw",
    [
        {"id": 1},
        {"name": None},
        {"parent_id": 7},
        {"order": "1"},
        {"order": True},
        {"kind": 1},
        {"sizing_mode": SizingMode.DURATION, "sizing": "2d"},
    ],
)
def test_wbs_node_type_errors(kw: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        task(**kw)


def test_resource() -> None:
    assert Resource("r1", "Alice").hourly_rate is None
    assert Resource("r1", "Alice", D(0)).hourly_rate == 0
    assert Resource("r1", "Alice", 100).hourly_rate == D(100)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed) as ei:
        Resource("r1", "Alice", D("-0.01"))
    assert codes(ei.value) == ["RESOURCE_NEGATIVE_RATE"] and fields(ei.value) == {"hourly_rate"}
    with pytest.raises(ValidationFailed):
        Resource("r1", "Alice", D("Infinity"))
    for bad in (1.5, "100", True):
        with pytest.raises(TypeError):
            Resource("r1", "Alice", bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Resource(1, "Alice")  # type: ignore[arg-type]


def test_assignment() -> None:
    a = Assignment("t1", "r1", D(80))
    assert a.key == ("t1", "r1") and a.key_text == "t1/r1" and a.fraction == D("0.8")
    assert Assignment("t1", "r1", D(250)).percent == 250  # max is validation's job
    for bad in (D(0), D(-5), D("NaN")):
        with pytest.raises(ValidationFailed) as ei:
            Assignment("t1", "r1", bad)
        (issue,) = ei.value.issues
        assert (issue.code, issue.field, issue.object_id) == (
            "ASSIGNMENT_PERCENT_RANGE",
            "percent",
            "t1/r1",
        )
    with pytest.raises(TypeError):
        Assignment("t1", "r1", 0.8)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Assignment("t1", 2, D(1))  # type: ignore[arg-type]


def test_dependency() -> None:
    d = Dependency("d1", "a", "b")
    assert d.type is DependencyType.FS and d.lag == days(0)
    d2 = Dependency("d2", "a", "b", "SS", hours(-4))  # type: ignore[arg-type]
    assert d2.type is DependencyType.SS and d2.lag.value == -4
    with pytest.raises(ValidationFailed) as ei:
        Dependency("d3", "a", "b", "XX")  # type: ignore[arg-type]
    assert fields(ei.value) == {"type"}
    with pytest.raises(TypeError):
        Dependency("d4", "a", "b", DependencyType.FS, "1d")  # type: ignore[arg-type]
    # Self-dependencies are a cross-reference rule (T12), not a local invariant.
    assert Dependency("d5", "a", "a").pred_id == "a"


def test_value_objects_equal_and_hashable() -> None:
    for make in (
        lambda: task(sizing_mode=SizingMode.DURATION, sizing=days("1.5")),
        lambda: Resource("r1", "A", D("10.5")),
        lambda: Assignment("t1", "r1", D(50)),
        lambda: Dependency("d1", "a", "b", DependencyType.FF, hours(2)),
    ):
        x, y = make(), make()
        assert x == y and hash(x) == hash(y) and x is not y
    assert task(name="A") != task(name="B")


# =============================================================================
# Project
# =============================================================================


def sample_project(**kw: Any) -> Project:
    base: dict[str, Any] = {
        "id": "p1",
        "name": "P",
        "start": START,
        "nodes": [
            WbsNode("g1", "G", NodeKind.GROUP),
            WbsNode("t2", "T2", NodeKind.TASK, "g1", 1, SizingMode.DURATION, days(2)),
            WbsNode("t1", "T1", NodeKind.TASK, "g1", 1, SizingMode.EFFORT, hours(40)),
            WbsNode("t0", "T0", NodeKind.TASK, "g1", 0),
            WbsNode("m1", "M", NodeKind.MILESTONE),
            WbsNode("g0", "G0", NodeKind.GROUP, order=-1),
        ],
        "resources": [Resource("bob", "Bob", D(50)), Resource("alice", "Alice", D(100))],
        "assignments": [
            Assignment("t1", "bob", D(20)),
            Assignment("t1", "alice", D(80)),
            Assignment("t2", "alice", D(100)),
        ],
        "dependencies": [
            Dependency("d2", "t2", "m1"),
            Dependency("d1", "t1", "t2", DependencyType.FS, days("-0.5")),
        ],
    }
    base.update(kw)
    return Project(**base)


def test_project_defaults() -> None:
    p = Project("p1", "Empty", START)
    assert p.currency == "USD" == config.DEFAULT_CURRENCY
    assert p.cost_report_unit is WorkUnit.PERSON_DAYS
    assert p.calendar == Calendar()
    assert (p.nodes, p.resources, p.assignments, p.dependencies) == ((), (), (), ())
    assert p.children(None) == () and p.wbs_order() == ()


def test_project_collections_sorted_and_equal_regardless_of_input_order() -> None:
    p = sample_project()
    assert [n.id for n in p.nodes] == ["g0", "g1", "m1", "t0", "t1", "t2"]
    assert [r.id for r in p.resources] == ["alice", "bob"]
    assert [a.key for a in p.assignments] == [("t1", "alice"), ("t1", "bob"), ("t2", "alice")]
    assert isinstance(p.nodes, tuple)
    q = sample_project(
        nodes=tuple(reversed(p.nodes)),
        resources=tuple(reversed(p.resources)),
        assignments=tuple(reversed(p.assignments)),
        dependencies=tuple(reversed(p.dependencies)),
    )
    assert p == q and hash(p) == hash(q)
    assert p != sample_project(name="Other")
    assert dataclasses.replace(p) == p
    assert pickle.loads(pickle.dumps(p)) == p
    assert pickle.loads(pickle.dumps(p)).node("t1") == p.node("t1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.name = "x"  # type: ignore[misc]


def test_project_lookups() -> None:
    p = sample_project()
    assert p.node("t1").name == "T1"
    assert p.resource("alice").hourly_rate == 100
    assert p.dependency("d1").lag == days("-0.5")
    assert p.assignment("t1", "bob").percent == 20
    assert p.has_node("m1") and not p.has_node("zz")
    assert p.has_resource("bob") and not p.has_resource("t1")
    assert p.has_dependency("d2") and not p.has_dependency("d9")
    assert [n.id for n in p.children(None)] == ["g0", "g1", "m1"]  # by (order, id)
    assert [n.id for n in p.children("g1")] == ["t0", "t1", "t2"]
    assert p.children("t1") == () and p.children("nope") == ()
    assert [a.resource_id for a in p.assignments_for("t1")] == ["alice", "bob"]
    assert p.assignments_for("t0") == ()
    assert [a.task_id for a in p.assignments_for_resource("alice")] == ["t1", "t2"]
    assert [d.id for d in p.dependencies_from("t2")] == ["d2"]
    assert [d.id for d in p.dependencies_to("t2")] == ["d1"]
    assert p.dependencies_to("t1") == ()
    assert [n.id for n in p.nodes_of_kind(NodeKind.TASK)] == ["t0", "t1", "t2"]
    assert [n.id for n in p.wbs_order()] == ["g0", "g1", "t0", "t1", "t2", "m1"]


@pytest.mark.parametrize(
    ("call", "object_type", "object_id"),
    [
        (lambda p: p.node("zz"), "node", "zz"),
        (lambda p: p.resource("zz"), "resource", "zz"),
        (lambda p: p.dependency("zz"), "dependency", "zz"),
        (lambda p: p.assignment("t0", "alice"), "assignment", "t0/alice"),
    ],
)
def test_project_lookup_not_found(call: Any, object_type: str, object_id: str) -> None:
    with pytest.raises(NotFound) as ei:
        call(sample_project())
    assert (ei.value.object_type, ei.value.object_id) == (object_type, object_id)
    assert object_id in str(ei.value)


def test_project_wbs_order_omits_unreachable() -> None:
    p = sample_project(
        nodes=[
            WbsNode("a", "A", NodeKind.GROUP),
            WbsNode("b", "B", NodeKind.TASK, "a"),
            WbsNode("x", "X", NodeKind.TASK, "missing"),
            WbsNode("c1", "C1", NodeKind.GROUP, "c2"),
            WbsNode("c2", "C2", NodeKind.GROUP, "c1"),
        ],
        assignments=(),
        dependencies=(),
    )
    assert [n.id for n in p.wbs_order()] == ["a", "b"]


def test_project_rejects_duplicate_ids_all_reported() -> None:
    with pytest.raises(ValidationFailed) as ei:
        sample_project(
            nodes=[WbsNode("n", "A", NodeKind.TASK), WbsNode("n", "B", NodeKind.GROUP)],
            resources=[Resource("r", "A"), Resource("r", "B"), Resource("r", "C")],
            assignments=(),
            dependencies=[Dependency("d", "a", "b"), Dependency("d", "b", "c")],
        )
    got = [(i.code, i.object_type, i.object_id, i.field) for i in ei.value.issues]
    assert got == [
        ("DUPLICATE_ID", "node", "n", "id"),
        ("DUPLICATE_ID", "resource", "r", "id"),
        ("DUPLICATE_ID", "resource", "r", "id"),
        ("DUPLICATE_ID", "dependency", "d", "id"),
    ]


def test_project_namespaces_are_separate() -> None:
    p = sample_project(
        nodes=[WbsNode("x", "X", NodeKind.TASK)],
        resources=[Resource("x", "X")],
        assignments=[Assignment("x", "x", D(100))],
        dependencies=[Dependency("x", "x", "x")],
    )
    assert p.node("x").name == p.resource("x").name == "X"


def test_project_rejects_duplicate_assignment() -> None:
    with pytest.raises(ValidationFailed) as ei:
        sample_project(
            assignments=[Assignment("t1", "alice", D(80)), Assignment("t1", "alice", D(20))]
        )
    (issue,) = ei.value.issues
    assert (issue.code, issue.object_type, issue.object_id, issue.field) == (
        "DUPLICATE_ASSIGNMENT",
        "assignment",
        "t1/alice",
        "resource_id",
    )


def test_project_does_not_check_cross_references() -> None:
    p = sample_project(
        nodes=[WbsNode("t", "T", NodeKind.TASK, parent_id="nowhere")],
        assignments=[Assignment("ghost", "nobody", D(500))],
        dependencies=[Dependency("d", "t", "t"), Dependency("e", "x", "y")],
    )
    assert len(p.dependencies) == 2


@pytest.mark.parametrize(
    ("kw", "exc"),
    [
        ({"id": 1}, TypeError),
        ({"id": ""}, ValidationFailed),
        ({"start": dt.datetime(2026, 10, 5)}, TypeError),
        ({"start": "2026-10-05"}, TypeError),
        ({"currency": "usd"}, ValidationFailed),
        ({"currency": "US"}, ValidationFailed),
        ({"cost_report_unit": "man_days"}, ValidationFailed),
        ({"calendar": CalendarSettings()}, TypeError),
        ({"nodes": [Resource("r", "R")]}, TypeError),
        ({"resources": "abc"}, TypeError),
    ],
)
def test_project_field_rejections(kw: dict[str, Any], exc: type[Exception]) -> None:
    with pytest.raises(exc):
        sample_project(**kw)


def test_project_accepts_enum_strings() -> None:
    assert sample_project(cost_report_unit="person_years").cost_report_unit is WorkUnit.PERSON_YEARS


# =============================================================================
# errors
# =============================================================================


def test_issue_shape_and_str() -> None:
    i = Issue(Severity.ERROR, "X", "bad", "node", "t1", "sizing")
    assert i.line is None
    assert i == Issue.error("X", "bad", object_type="node", object_id="t1", field="sizing")
    assert hash(i) == hash(
        Issue.error("X", "bad", object_type="node", object_id="t1", field="sizing")
    )
    assert str(i) == "error X [node t1, field sizing]: bad"
    assert Issue.warning("W", "m").severity is Severity.WARNING
    j = Issue.info("CSV_DEFAULT_APPLIED", "m", object_type="CALENDAR", field="x", line=3)
    assert j.severity is Severity.INFO and j.line == 3
    assert str(j) == "info CSV_DEFAULT_APPLIED [CALENDAR, field x] (line 3): m"
    assert str(ObjectType.NODE) == "node"


def test_exceptions() -> None:
    issues = [Issue.error("A", "first"), Issue.error("B", "second", object_id="t9")]
    for cls in (ValidationFailed, ImportFailed):
        e = cls(issues)
        assert isinstance(e, PlannerError)
        assert e.issues == tuple(issues)
        text = str(e)
        assert "2 issues" in text and "first" in text and "t9" in text
        again = pickle.loads(pickle.dumps(e))
        assert isinstance(again, cls) and again.issues == e.issues
    nf = NotFound("node", "t9")
    assert isinstance(nf, PlannerError) and isinstance(nf, LookupError)
    assert (nf.object_type, nf.object_id) == ("node", "t9") and "t9" in str(nf)
    assert pickle.loads(pickle.dumps(nf)).object_id == "t9"
    assert str(Conflict("name taken")) == "name taken"
    for cls2 in (Cancelled, UnsavedChanges):
        assert isinstance(cls2(), PlannerError) and str(cls2())


# =============================================================================
# config
# =============================================================================


def test_config_defaults_and_constants() -> None:
    c = config.DEFAULT_CONFIG
    assert c == config.Config()
    assert c.max_assignment_percent == config.MAX_ASSIGNMENT_PERCENT == D(100)
    assert c.days_display_decimals == config.DAYS_DISPLAY_DECIMALS == 2
    assert c.money_display_decimals == 2
    assert c.money_rounding == ROUND_HALF_EVEN
    assert config.DEFAULT_COST_REPORT_UNIT == WorkUnit.PERSON_DAYS
    assert config.DEFAULT_HOURS_PER_DAY == 8
    assert config.DEFAULT_WORKING_DAYS_PER_YEAR == 220
    assert dt.time(9, 0) == config.DEFAULT_WORKDAY_START
    assert (
        frozenset(Weekday(w) for w in config.DEFAULT_WORKING_WEEKDAYS) == DEFAULT_WORKING_WEEKDAYS
    )
    assert c.round_money(D("2.345")) == D("2.34") and c.round_money(D("2.355")) == D("2.36")
    assert c.round_days(D(2400) / D(450)) == D("5.33")
    assert config.Config(max_assignment_percent=D(200)).max_assignment_percent == 200
    with pytest.raises(ValueError):
        config.Config(max_assignment_percent=D(0))
    assert config.Config(max_assignment_percent=200).max_assignment_percent == D(200)  # type: ignore[arg-type]
    assert config.Config(max_assignment_percent="150").max_assignment_percent == D(150)  # type: ignore[arg-type]
    for bad in (1.5, True):
        with pytest.raises(TypeError):
            config.Config(max_assignment_percent=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        config.Config(days_display_decimals=-1)
