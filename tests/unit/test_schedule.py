from __future__ import annotations

import pickle
from datetime import date, datetime
from decimal import Decimal as D

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.errors import (
    Cancelled,
    Conflict,
    NotFound,
    Severity,
    ValidationFailed,
)
from project_planner.engine.fingerprint import cost_fp, schedule_fp
from project_planner.engine.model import Project
from project_planner.engine.schedule import (
    cost_report,
    level,
    recost,
    schedule,
    with_kind,
)

pytestmark = pytest.mark.unit


def at(day: int, hh: int = 9, mm: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm)


def a16() -> Project:
    return (
        ProjectBuilder()
        .resource("alice", rate=100)
        .resource("bob", rate=50)
        .task("t1", duration="5d")
        .assign("t1", "alice", 80)
        .assign("t1", "bob", 20)
        .build()
    )


def overloaded() -> Project:
    return (
        ProjectBuilder()
        .resource("alice", rate=100)
        .task("t1", duration="3d")
        .task("t2", duration="3d")
        .task("t3", duration="1d")
        .assign("t1", "alice", 60)
        .assign("t2", "alice", 60)
        .dep("t2", "t3")
        .build()
    )


def test_a01_five_days_and_assignment_days() -> None:
    p = (
        ProjectBuilder()
        .resource("alice", rate=100)
        .resource("bob", rate=50)
        .task("t1", effort="40h")
        .assign("t1", "alice", 80)
        .assign("t1", "bob", 20)
        .build()
    )
    r = schedule(p)
    row = r.node("t1")
    assert (row.start, row.finish) == (at(5), at(9, 17))
    assert row.duration_days == 5 and row.effort_days == 5
    assert r.assignment("t1", "alice").assignment_days == 4
    assert r.assignment("t1", "bob").assignment_days == 1
    assert r.kind == "dependency_only"
    assert r.complete and r.project_finish == at(9, 17)
    assert r.working_span_days == 5


def test_a02_duration_fixed_when_allocation_changes() -> None:
    def build(pct: int) -> Project:
        return (
            ProjectBuilder()
            .resource("a", rate=10)
            .task("t1", duration="5d")
            .assign("t1", "a", pct)
            .build()
        )

    r50, r100 = schedule(build(50)), schedule(build(100))
    assert r50.node("t1").duration_days == r100.node("t1").duration_days == 5
    assert r50.node("t1").effort_days == D("2.5")
    assert r100.node("t1").effort_days == 5


def test_a04_holiday_pushes_finish() -> None:
    p = (
        ProjectBuilder()
        .calendar(holidays=[date(2026, 10, 8)])
        .resource("a", rate=1)
        .task("t1", duration="5d")
        .assign("t1", "a")
        .build()
    )
    r = schedule(p)
    assert r.node("t1").finish == at(12, 17)
    assert r.working_span_days == 5
    assert r.elapsed_span_calendar_days == 8
    # one continuous segment of 5 working days (the holiday is not on the axis)
    assert [(s.start, s.end) for s in r.loading["a"]] == [(0, 2400)]


def test_a05_negative_lag_and_ss() -> None:
    p = (
        ProjectBuilder()
        .resource("alice", rate=100)
        .resource("bob", rate=50)
        .task("t1", effort="40h")
        .assign("t1", "alice", 80)
        .assign("t1", "bob", 20)
        .task("t2", duration="2d")
        .task("t3", duration="1d")
        .dep("t1", "t2", "FS", lag="-0.5d", id="d1")
        .dep("t1", "t3", "SS", lag="1d", id="d2")
        .build()
    )
    r = schedule(p)
    assert (r.node("t2").start, r.node("t2").finish) == (at(9, 13), at(13, 13))
    assert (r.node("t3").start, r.node("t3").finish) == (at(6), at(6, 17))
    assert r.dependency("d1").lag_days == D("-0.5")
    assert r.dependency("d1").lag_minutes == -240
    assert str(r.dependency("d1").lag_entered) == "-0.5d"
    assert r.dependency("d2").lag_days == 1
    assert r.working_span_days == D("6.5")
    with pytest.raises(NotFound):
        r.dependency("nope")


@pytest.mark.parametrize("binding", [1, 2, 3])
def test_a06_binding_predecessor(binding: int) -> None:
    lens = {1: "1d", 2: "2d", 3: "3d"}
    lens[binding] = "4d"
    b = ProjectBuilder()
    for i in (1, 2, 3):
        b.task(f"t{i}", duration=lens[i])
    b.task("t4", duration="1d")
    for i in (1, 2, 3):
        b.dep(f"t{i}", "t4")
    r = schedule(b.build())
    assert r.node("t4").start == at(9)  # 4 working days: Mon 5 .. Thu 8
    assert r.node("t4").finish == at(9, 17)


def test_a13_incomplete_with_partial_dates() -> None:
    p = (
        ProjectBuilder()
        .resource("a", rate=1)
        .task("t1", duration="1d")
        .task("t2", effort="8h")  # no allocation
        .task("t3", duration="1d")
        .dep("t2", "t3")
        .build()
    )
    r = schedule(p)
    assert r.complete is False
    assert r.project_finish is None
    assert r.working_span_days is None and r.elapsed_span_calendar_days is None
    assert r.node("t1").finish == at(5, 17)
    assert r.node("t2").scheduled is False and r.node("t2").status == "unschedulable"
    assert r.node("t2").start is None and r.node("t2").duration_days is None
    assert r.node("t3").status == "blocked" and r.node("t3").reason
    assert [i.code for i in r.issues].count("TASK_NO_CAPACITY") == 1


def test_a14_cycle_raises_naming_members() -> None:
    p = (
        ProjectBuilder()
        .task("t1", duration="1d")
        .task("t2", duration="1d")
        .task("t3", duration="1d")
        .dep("t1", "t2")
        .dep("t2", "t3")
        .dep("t3", "t1")
        .build()
    )
    with pytest.raises(ValidationFailed) as ei:
        schedule(p)
    text = " ".join(i.message for i in ei.value.issues)
    assert all(t in text for t in ("t1", "t2", "t3"))


def test_a16_costs() -> None:
    p = a16()
    r = schedule(p)
    assert r.assignment("t1", "alice").cost == 3200
    assert r.assignment("t1", "bob").cost == 400
    assert r.node("t1").cost == 3600 and r.total_cost == 3600 and r.cost_complete
    assert r.assignment("t1", "alice").assignment_hours == 32
    assert r.assignment("t1", "alice").percent == 80
    assert r.assignment("t1", "alice").hourly_rate == 100
    rep = cost_report(r, p, "person_hours")
    assert rep.tasks["t1"].assignments[0].work_qty == 32
    with pytest.raises(NotFound):
        r.assignment("t1", "zed")
    with pytest.raises(NotFound):
        r.node("zzz")


def test_a17_group_cost_and_dates() -> None:
    p = (
        ProjectBuilder()
        .resource("a", rate=100)
        .group("g1")
        .group("g2", parent="g1")
        .task("t1", duration="2d", parent="g2")
        .task("t2", duration="1d", parent="g2")
        .milestone("m1", parent="g1")
        .assign("t1", "a")
        .assign("t2", "a", 50)
        .dep("t1", "t2")
        .dep("t2", "m1")
        .build()
    )
    r = schedule(p)
    # t1 16h * 100 + t2 4h * 100
    assert r.node("g2").cost == 2000 and r.node("g1").cost == 2000
    assert r.node("g1").cost == r.total_cost
    assert (r.node("g1").start, r.node("g1").finish) == (at(5), at(7, 17))
    assert r.node("g1").status == "group" and r.node("g1").scheduled
    assert r.node("g1").effort_days == D("2.5")
    assert r.node("g1").wbs_number == "1"
    assert [n.node_id for n in r.tasks()] == ["t1", "t2", "m1"]
    assert r.node("m1").cost == 0 and r.node("m1").duration_days == 0


def test_a19_a07_leveling() -> None:
    p = overloaded()
    base = schedule(p)
    assert base.project_finish == at(8, 17)
    lv = level(p, base)
    res = lv.result
    assert res.kind == "leveling_preview"
    assert lv.delays_days["t2"] == 3 and "t1" not in lv.delays_days
    assert lv.delays_days["t3"] == 3  # pushed by its predecessor
    assert res.node("t2").leveling_delay_days == 3
    assert res.node("t1").leveling_delay_days == 0
    assert (res.node("t2").start, res.node("t2").finish) == (at(8), at(12, 17))
    assert res.project_finish == at(13, 17)
    assert lv.finish_delta_days == 3
    assert res.working_span_days == 7
    assert res.elapsed_span_calendar_days is not None
    assert res.elapsed_span_calendar_days > D(7)
    assert list(lv.unresolved) == []
    # costs unchanged
    assert res.total_cost == base.total_cost
    assert res.node("t2").cost == base.node("t2").cost
    assert res.schedule_fp == base.schedule_fp
    assert with_kind(res, "leveled").kind == "leveled"
    assert res.kind == "leveling_preview"


def test_a20_missing_rate_incomplete_cost() -> None:
    p = (
        ProjectBuilder()
        .resource("a", rate=100)
        .resource("b", rate=None)
        .task("t1", duration="1d")
        .assign("t1", "a")
        .assign("t1", "b")
        .build()
    )
    r = schedule(p)
    assert r.complete and r.cost_complete is False
    assert r.node("t1").cost_complete is False and r.node("t1").cost == 800
    assert r.missing_rate_resources == ("b",)
    assert r.assignment("t1", "b").cost is None


def test_a20_zero_rate_is_complete() -> None:
    p = ProjectBuilder().resource("a", rate=0).task("t1", duration="1d").assign("t1", "a").build()
    assert schedule(p).cost_complete is True


@pytest.mark.parametrize("mode", ["duration", "effort"])
def test_a24_40h_vs_5d(mode: str) -> None:
    def build(value: str) -> Project:
        b = ProjectBuilder().resource("a", rate=10)
        b.task("t1", **{mode: value})
        return b.assign("t1", "a").build()

    ra, rb = schedule(build("40h")), schedule(build("5d"))
    for f in ("start", "finish", "duration_days", "effort_days", "cost"):
        assert getattr(ra.node("t1"), f) == getattr(rb.node("t1"), f)
    assert ra.node("t1").duration_days == 5


def test_a26_seven_and_a_half_hour_day() -> None:
    p = (
        ProjectBuilder()
        .calendar(hours_per_day="7.5")
        .resource("a", rate=100)
        .task("t1", duration="2d")
        .assign("t1", "a")
        .build()
    )
    r = schedule(p)
    assert r.minutes_per_day == 450
    assert r.node("t1").duration_minutes == 900
    assert r.node("t1").duration_days == 2
    assert r.node("t1").finish == at(6, 16, 30)
    assert r.node("t1").cost == 1500


def test_day_boundary_finish_and_elapsed_across_weekend() -> None:
    r = schedule(ProjectBuilder().task("t1", duration="6d").build())
    assert r.node("t1").finish == at(12, 17)
    assert r.project_finish == at(12, 17)
    assert r.working_span_days == 6
    assert r.elapsed_span_calendar_days == 8
    assert r.elapsed_span_calendar_days > r.working_span_days


def test_milestone_dates_and_start_moved() -> None:
    p = (
        ProjectBuilder(start=date(2026, 10, 3))  # Saturday
        .task("t1", duration="1d")
        .milestone("m1")
        .dep("t1", "m1")
        .build()
    )
    r = schedule(p)
    assert r.project_start == at(5)
    assert r.node("t1").start == at(5)
    assert r.node("m1").start == r.node("m1").finish == at(5, 17)
    assert any(i.code == "CAL_START_MOVED" for i in r.issues)


def test_issue_dedupe_unsized() -> None:
    r = schedule(ProjectBuilder().task("t1").build())
    hits = [i for i in r.issues if i.code == "TASK_UNSIZED"]
    assert len(hits) == 1 and hits[0].severity is Severity.ERROR
    assert r.complete is False


def test_empty_project_complete_without_finish() -> None:
    r = schedule(ProjectBuilder().build())
    assert r.complete and r.project_finish is None and r.working_span_days is None


def test_recost_keeps_dates_updates_costs() -> None:
    r = schedule(a16())
    q = (
        ProjectBuilder()
        .resource("alice", rate=200)
        .resource("bob", rate=50)
        .task("t1", duration="5d")
        .assign("t1", "alice", 80)
        .assign("t1", "bob", 20)
        .build()
    )
    r2 = recost(r, q)
    assert r2.node("t1").start == r.node("t1").start
    assert r2.node("t1").finish == r.node("t1").finish
    assert r2.schedule_fp == r.schedule_fp and r2.cost_fp != r.cost_fp
    assert r2.cost_fp == cost_fp(q)
    assert r2.total_cost == 6800 and r2.assignment("t1", "alice").cost == 6400
    assert r2.loading == r.loading
    assert cost_report(r2, q).cost == 6800


def test_recost_with_group_and_missing_rate() -> None:
    def build(rate: int | None) -> Project:
        return (
            ProjectBuilder()
            .resource("a", rate=rate)
            .group("g1")
            .task("t1", duration="1d", parent="g1")
            .assign("t1", "a")
            .build()
        )

    r = recost(schedule(build(None)), build(100))
    assert r.node("g1").cost == 800 and r.node("g1").cost_complete
    assert r.cost_complete and r.missing_rate_resources == ()


def test_recost_conflict_on_schedule_change() -> None:
    r = schedule(a16())
    other = ProjectBuilder().task("t1", duration="6d").build()
    with pytest.raises(Conflict):
        recost(r, other)


def test_level_stale_base_conflict() -> None:
    base = schedule(overloaded())
    changed = ProjectBuilder().task("t1", duration="1d").build()
    with pytest.raises(Conflict, match="stale"):
        level(changed, base)


def test_level_cancel_and_progress() -> None:
    p = overloaded()
    seen: list[float] = []
    level(p, schedule(p), progress=lambda f, m: seen.append(f))
    assert seen and seen[-1] == 1.0
    with pytest.raises(Cancelled):
        level(p, schedule(p), cancel=lambda: True)


def test_fingerprints_on_result() -> None:
    p = a16()
    r = schedule(p)
    assert r.schedule_fp == schedule_fp(p) and r.cost_fp == cost_fp(p)


def test_all_day_fields_exact_and_pickle() -> None:
    p = (
        ProjectBuilder()
        .calendar(hours_per_day="7.5")
        .resource("a", rate=33)
        .resource("b", rate=17)
        .group("g1")
        .task("t1", effort="13h", parent="g1")
        .assign("t1", "a", 70)
        .assign("t1", "b", 30)
        .task("t2", duration="1.3d", parent="g1")
        .assign("t2", "a", 45)
        .dep("t1", "t2", lag="0.7d", id="d1")
        .milestone("m1")
        .dep("t2", "m1")
        .build()
    )
    r = schedule(p)
    mpd = r.minutes_per_day
    for row in r.nodes.values():
        if row.duration_minutes is not None:
            assert row.duration_days == D(row.duration_minutes) / mpd
        if row.effort_person_minutes is not None:
            assert row.effort_days == row.effort_person_minutes / mpd
        assert row.leveling_delay_days == D(row.leveling_delay_minutes) / mpd
    for a in r.assignments.values():
        assert a.assignment_days == a.assignment_minutes / mpd
    for d in r.dependencies.values():
        assert d.lag_days == D(d.lag_minutes) / mpd
    assert r.working_span_minutes is not None
    assert r.working_span_days == D(r.working_span_minutes) / mpd
    assert pickle.loads(pickle.dumps(r)) == r
    lv = level(p, r)
    assert pickle.loads(pickle.dumps(lv)) == lv
