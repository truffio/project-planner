"""Unit tests for the ProjectBuilder test fixture (task T10)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fixtures.builders import DEFAULT_START, ProjectBuilder, dec

from project_planner.engine.errors import ValidationFailed
from project_planner.engine.model import (
    Assignment,
    Calendar,
    CalendarSettings,
    Dependency,
    DependencyType,
    Holiday,
    NodeKind,
    Project,
    Resource,
    SizingMode,
    WbsNode,
    WorkUnit,
    days,
    hours,
)

pytestmark = pytest.mark.unit

D = Decimal


def test_example_from_docstring_builds_expected_project() -> None:
    p = (
        ProjectBuilder(start=dt.date(2026, 10, 5))
        .calendar(hours_per_day=8, holidays=[dt.date(2026, 10, 8)])
        .resource("alice", rate="100")
        .resource("bob", rate="50")
        .group("g1", "Phase 1")
        .task("t1", effort="40h", parent="g1")
        .assign("t1", "alice", 80)
        .assign("t1", "bob", 20)
        .task("t2", duration="2d", parent="g1")
        .milestone("m1")
        .dep("t1", "t2", "FS", lag="0d")
        .build()
    )
    expected = Project(
        id="p1",
        name="Test project",
        start=dt.date(2026, 10, 5),
        currency="USD",
        cost_report_unit=WorkUnit.PERSON_DAYS,
        calendar=Calendar(holidays=(Holiday(dt.date(2026, 10, 8)),)),
        nodes=(
            WbsNode("g1", "Phase 1", NodeKind.GROUP, None, 0),
            WbsNode("t1", "t1", NodeKind.TASK, "g1", 0, SizingMode.EFFORT, hours(40)),
            WbsNode("t2", "t2", NodeKind.TASK, "g1", 1, SizingMode.DURATION, days(2)),
            WbsNode("m1", "m1", NodeKind.MILESTONE, None, 1),
        ),
        resources=(Resource("alice", "alice", D(100)), Resource("bob", "bob", D(50))),
        assignments=(Assignment("t1", "alice", D(80)), Assignment("t1", "bob", D(20))),
        dependencies=(Dependency("d1", "t1", "t2", DependencyType.FS, days(0)),),
    )
    assert p == expected
    assert [n.id for n in p.wbs_order()] == ["g1", "t1", "t2", "m1"]


def test_defaults_and_auto_ids() -> None:
    b = ProjectBuilder()
    b.group()
    g = b.last_id
    b.task(duration="1d", parent=g)
    t = b.last_id
    b.task(parent=g).milestone().resource(rate=0).resource().dep(t, "m1")
    p = b.build()
    assert p.start == DEFAULT_START and p.calendar == Calendar()
    assert [n.id for n in p.nodes] == ["g1", "m1", "t1", "t2"]
    assert p.node("t2").sizing is None and p.node("t2").order == 1
    assert [r.id for r in p.resources] == ["r1", "r2"]
    assert p.resource("r1").hourly_rate == 0 and p.resource("r2").hourly_rate is None
    assert p.dependency("d1").lag == days(0)
    assert b.last_id == "d1"
    assert b.build() == p  # repeatable


def test_auto_ids_skip_explicit_ones() -> None:
    p = ProjectBuilder().task("t1").task().task().build()
    assert [n.id for n in p.nodes] == ["t1", "t2", "t3"]


def test_overrides() -> None:
    p = (
        ProjectBuilder(name="X", id="px", currency="EUR", cost_report_unit="person_hours")
        .calendar(hours_per_day="7.5", working_weekdays="Sun-Thu")
        .calendar(workday_start="08:30")
        .task("a", "Alpha", effort=hours("1.5"), order=7)
        .assign("a", "r", D("12.5"))
        .dep("a", "b", DependencyType.SS, lag="-0.5d", id="dep-x")
        .build()
    )
    assert (p.id, p.name, p.currency, p.cost_report_unit) == ("px", "X", "EUR", "person_hours")
    assert p.calendar.minutes_per_day == 450 and p.calendar.workday_start == dt.time(8, 30)
    assert p.node("a").name == "Alpha" and p.node("a").order == 7
    assert p.assignment("a", "r").percent == D("12.5")
    assert p.dependency("dep-x").lag == days("-0.5")
    assert p.dependency("dep-x").type is DependencyType.SS


def test_builder_allows_invalid_graphs_but_not_local_violations() -> None:
    p = ProjectBuilder().task("t", parent="missing").dep("t", "t").assign("t", "ghost").build()
    assert p.dependency("d1").pred_id == p.dependency("d1").succ_id == "t"
    with pytest.raises(ValidationFailed):
        ProjectBuilder().task("t").task("t").build()
    with pytest.raises(ValidationFailed):
        ProjectBuilder().task("t", duration="-1d")
    with pytest.raises(TypeError):
        ProjectBuilder().task("t", effort=40)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProjectBuilder().task("t", effort="1h", duration="1h")
    with pytest.raises(ValidationFailed):
        ProjectBuilder().calendar(hours_per_day=25).build()
    with pytest.raises(TypeError):
        ProjectBuilder().assign("t", "r", 0.5)  # type: ignore[arg-type]


def test_raw_node_and_calendar_settings() -> None:
    b = (
        ProjectBuilder()
        .node(WbsNode("x", "X", NodeKind.TASK, order=5))
        .calendar(holidays=[(dt.date(2026, 12, 25), "Xmas")])
    )
    assert b.last_id == "x"
    assert b.calendar_settings() == CalendarSettings(
        holidays=(Holiday(dt.date(2026, 12, 25), "Xmas"),)
    )
    assert b.build().node("x").order == 5


def test_dec() -> None:
    assert dec(1) == D(1) and dec("1.5") == D("1.5") and dec(D(2)) == 2
    with pytest.raises(TypeError):
        dec(1.5)  # type: ignore[arg-type]
