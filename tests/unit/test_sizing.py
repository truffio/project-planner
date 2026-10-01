"""Unit tests for engine.sizing (T13; acceptance A01, A02, A13, A24)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.errors import Severity
from project_planner.engine.sizing import TaskSizing, compute_sizing

pytestmark = pytest.mark.unit


def _one(builder: ProjectBuilder, node: str = "t1") -> TaskSizing:
    return compute_sizing(builder.build())[node]


def test_a01_effort_with_two_assignments() -> None:
    b = (
        ProjectBuilder()
        .resource("a")
        .resource("b")
        .task("t1", effort="40h")
        .assign("t1", "a", 80)
        .assign("t1", "b", 20)
    )
    s = _one(b)
    assert s.duration_minutes == 2400
    assert s.effort_person_minutes == Decimal(2400)
    assert s.capacity == Decimal(1)
    assert s.schedulable
    assert s.issues == ()


@pytest.mark.parametrize(("pct", "effort"), [(50, 1200), (100, 2400)])
def test_a02_duration_effort_follows_assignments(pct: int, effort: int) -> None:
    s = _one(ProjectBuilder().resource("a").task("t1", duration="5d").assign("t1", "a", pct))
    assert s.duration_minutes == 2400
    assert s.effort_person_minutes == Decimal(effort)


def test_a13_effort_without_assignments() -> None:
    s = _one(ProjectBuilder().task("t1", effort="8h"))
    assert not s.schedulable
    assert s.duration_minutes is None
    assert s.effort_person_minutes is None
    (issue,) = s.issues
    assert issue.code == "TASK_NO_CAPACITY"
    assert issue.severity is Severity.ERROR
    assert issue.object_type == "node"
    assert issue.object_id == "t1"
    assert issue.field == "assignments"


def test_a24_hours_vs_days() -> None:
    b8 = ProjectBuilder().calendar(hours_per_day=8).task("a", duration="40h")
    r8 = compute_sizing(b8.task("b", duration="5d").build())
    assert r8["a"].duration_minutes == r8["b"].duration_minutes == 2400
    b75 = ProjectBuilder().calendar(hours_per_day="7.5").task("a", duration="40h")
    r75 = compute_sizing(b75.task("b", duration="5d").build())
    assert r75["b"].duration_minutes == 2250
    assert r75["a"].duration_minutes == 2400


def test_effort_in_days_half_time() -> None:
    s = _one(ProjectBuilder().resource("a").task("t1", effort="5d").assign("t1", "a", 50))
    assert s.effort_person_minutes == Decimal(2400)
    assert s.duration_minutes == 4800


def test_rounding_up_keeps_entered_effort() -> None:
    b = ProjectBuilder().task("t1", effort="1h")
    for r in "abc":
        b.resource(r).assign("t1", r, 33)
    s = _one(b)
    assert s.capacity == Decimal("0.99")
    assert s.duration_minutes == 61  # ceil(60 / 0.99)
    assert s.effort_person_minutes == Decimal(60)


def test_exact_division_not_rounded() -> None:
    s = _one(ProjectBuilder().resource("a").task("t1", effort="1h").assign("t1", "a", 50))
    assert s.duration_minutes == 120


def test_fractional_percent() -> None:
    s = _one(ProjectBuilder().resource("a").task("t1", effort="999h").assign("t1", "a", "33.3"))
    assert s.capacity == Decimal("0.333")
    assert s.duration_minutes == 180000  # 59940 / 0.333 exactly


def test_fractional_effort_not_rounded_first() -> None:
    # 0.01h = 0.6 person-minutes; at 100% -> ceil(0.6) = 1
    s = _one(ProjectBuilder().resource("a").task("t1", effort="0.01h").assign("t1", "a", 100))
    assert s.duration_minutes == 1
    assert s.effort_person_minutes == Decimal("0.6")


def test_duration_mode_over_100_percent_capacity() -> None:
    b = ProjectBuilder().resource("a").resource("b").task("t1", duration="1d")
    s = _one(b.assign("t1", "a", 100).assign("t1", "b", 100))
    assert s.duration_minutes == 480
    assert s.effort_person_minutes == Decimal(960)


def test_duration_without_assignments_has_zero_effort() -> None:
    s = _one(ProjectBuilder().task("t1", duration="2d"))
    assert s.duration_minutes == 960
    assert s.effort_person_minutes == Decimal(0)
    assert s.capacity == Decimal(0)


def test_unsized_task() -> None:
    s = _one(ProjectBuilder().task("t1"))
    assert not s.schedulable
    assert s.duration_minutes is None
    (issue,) = s.issues
    assert issue.code == "TASK_UNSIZED"
    assert issue.severity is Severity.ERROR


def test_milestone_and_group_coverage() -> None:
    p = ProjectBuilder().group("g1").milestone("m1", parent="g1").task("t1", parent="g1").build()
    r = compute_sizing(p)
    assert set(r) == {"m1", "t1"}
    assert r["m1"].duration_minutes == 0
    assert r["m1"].effort_person_minutes == Decimal(0)
    assert r["m1"].schedulable


def test_zero_sized_task_schedulable() -> None:
    s = _one(ProjectBuilder().task("t1", duration="0h"))
    assert s.duration_minutes == 0
    assert s.schedulable
    s = _one(ProjectBuilder().task("t1", effort="0h"))
    assert s.duration_minutes == 0
    assert s.schedulable


def test_input_not_modified() -> None:
    p = ProjectBuilder().calendar(hours_per_day=6).task("t1", duration="1d").build()
    before = p.node("t1").sizing
    compute_sizing(p)
    assert p.node("t1").sizing == before
    assert str(before) == "1d"
