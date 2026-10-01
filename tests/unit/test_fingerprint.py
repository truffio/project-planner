from __future__ import annotations

import dataclasses
from datetime import date

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.fingerprint import cost_fp, schedule_fp
from project_planner.engine.model import Project

pytestmark = pytest.mark.unit


def base(
    *,
    name: str = "Task one",
    rate: int = 100,
    duration: str = "5d",
    lag: str = "0d",
    hours: int = 8,
    per_year: int = 220,
    start: date = date(2026, 10, 5),
    currency: str = "USD",
) -> Project:
    p = (
        ProjectBuilder(start=start)
        .calendar(hours_per_day=hours, working_days_per_year=per_year)
        .resource("r1", "Alice", rate=rate)
        .task("t1", name, duration=duration)
        .task("t2", "Two", duration="1d")
        .assign("t1", "r1", 50)
        .dep("t1", "t2", lag=lag, id="d1")
        .build()
    )
    return dataclasses.replace(p, currency=currency)


def fps(p: Project) -> tuple[str, str]:
    return schedule_fp(p), cost_fp(p)


def test_rename_changes_nothing() -> None:
    assert fps(base()) == fps(base(name="Renamed"))


def test_rate_change_alters_cost_fp_only() -> None:
    a, b = base(), base(rate=120)
    assert schedule_fp(a) == schedule_fp(b)
    assert cost_fp(a) != cost_fp(b)


@pytest.mark.parametrize(
    "change",
    [
        {"duration": "6d"},
        {"duration": "40h"},
        {"lag": "1d"},
        {"hours": 7},
        {"start": date(2026, 10, 6)},
    ],
)
def test_schedule_inputs_change_both(change: dict[str, object]) -> None:
    a, b = base(), base(**change)  # type: ignore[arg-type]
    assert schedule_fp(a) != schedule_fp(b)
    assert cost_fp(a) != cost_fp(b)


def test_working_days_per_year_and_currency_change_neither() -> None:
    assert fps(base()) == fps(base(per_year=200))
    assert fps(base()) == fps(base(currency="EUR"))


def test_cost_report_unit_changes_neither() -> None:
    from project_planner.engine.model import WorkUnit

    p = base()
    q = dataclasses.replace(p, cost_report_unit=WorkUnit.PERSON_HOURS)
    assert fps(p) == fps(q)


def test_insertion_order_does_not_matter() -> None:
    a = (
        ProjectBuilder()
        .resource("r1", rate=1)
        .resource("r2", rate=2)
        .task("t1", duration="1d", order=0)
        .task("t2", duration="2d", order=1)
        .assign("t1", "r1")
        .assign("t2", "r2")
        .build()
    )
    b = (
        ProjectBuilder()
        .resource("r2", rate=2)
        .resource("r1", rate=1)
        .task("t2", duration="2d", order=1)
        .task("t1", duration="1d", order=0)
        .assign("t2", "r2")
        .assign("t1", "r1")
        .build()
    )
    assert fps(a) == fps(b)


def test_equal_decimal_values_hash_equal() -> None:
    assert fps(base(duration="5d")) == fps(base(duration="5.0d"))


def test_hex_sha256() -> None:
    s, c = fps(base())
    assert len(s) == len(c) == 64 and s != c
