from __future__ import annotations

from datetime import date

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine import forward_pass as fp_module
from project_planner.engine import leveling as leveling_module
from project_planner.engine.forward_pass import NodeTiming
from project_planner.engine.model import NodeKind, Project
from project_planner.engine.results import ScheduleResult
from project_planner.engine.schedule import assemble, level, schedule, with_kind

pytestmark = pytest.mark.unit


def stored_timings(result: ScheduleResult) -> dict[str, NodeTiming]:
    """What persistence keeps per node: minutes and status, but no reason texts."""
    return {
        i: NodeTiming(i, r.start_minutes, r.finish_minutes, r.status, None)  # type: ignore[arg-type]
        for i, r in result.nodes.items()
        if r.kind is not NodeKind.GROUP
    }


def stored_delays(result: ScheduleResult) -> dict[str, int]:
    return {
        i: r.leveling_delay_minutes for i, r in result.nodes.items() if r.leveling_delay_minutes
    }


def rebuild(project: Project, result: ScheduleResult) -> ScheduleResult:
    return assemble(
        project,
        stored_timings(result),
        kind=result.kind,
        delays=stored_delays(result),
        issues=result.issues,
    )


def overloaded() -> Project:
    return (
        ProjectBuilder()
        .resource("alice", rate=100)
        .group("g1", "Phase")
        .task("t1", duration="3d", parent="g1")
        .task("t2", duration="3d", parent="g1")
        .task("t3", duration="1d")
        .milestone("m1")
        .assign("t1", "alice", 60)
        .assign("t2", "alice", 60)
        .dep("t2", "t3")
        .dep("t3", "m1")
        .build()
    )


def test_assemble_equals_schedule() -> None:
    p = overloaded()
    r = schedule(p)
    assert rebuild(p, r) == r


def test_assemble_equals_leveled_result() -> None:
    p = overloaded()
    lv = level(p, schedule(p))
    assert lv.result.nodes["t2"].leveling_delay_minutes > 0
    assert rebuild(p, lv.result) == lv.result
    leveled = with_kind(lv.result, "leveled")
    again = assemble(
        p,
        stored_timings(leveled),
        kind="leveled",
        delays=lv.delays,
        issues=leveled.issues,
    )
    assert again == leveled


def test_assemble_rederives_unschedulable_and_blocked_reasons() -> None:
    p = (
        ProjectBuilder()
        .resource("alice", rate=100)
        .task("t1", name="unsized")
        .task("t2", effort="8h")  # effort without capacity
        .task("t3", duration="1d")
        .task("t4", duration="1d")
        .milestone("m1")
        .dep("t1", "t3")
        .dep("t3", "t4")
        .dep("t2", "m1")
        .build()
    )
    r = schedule(p)
    assert not r.complete
    assert {r.nodes[i].status for i in ("t1", "t2", "t3", "t4", "m1")} == {
        "unschedulable",
        "blocked",
    }
    assert rebuild(p, r) == r


def test_assemble_does_not_run_forward_pass_or_leveling(monkeypatch: pytest.MonkeyPatch) -> None:
    p = overloaded()
    lv = level(p, schedule(p))

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("scheduler must not run")

    monkeypatch.setattr(fp_module, "forward_pass", boom)
    monkeypatch.setattr("project_planner.engine.schedule.forward_pass", boom)
    monkeypatch.setattr(leveling_module, "level", boom)
    monkeypatch.setattr("project_planner.engine.schedule._level", boom)
    assert rebuild(p, lv.result) == lv.result


def test_assemble_accepts_leveling_delay_objects() -> None:
    p = overloaded()
    lv = level(p, schedule(p))
    a = assemble(p, stored_timings(lv.result), kind="leveling_preview", delays=lv.delays)
    assert a.nodes["t2"].leveling_delay_days == lv.delays_days["t2"]


def test_assemble_tolerates_edited_project() -> None:
    p = overloaded()
    r = schedule(p)
    timings = stored_timings(r)
    bigger = (
        ProjectBuilder(start=date(2026, 10, 5))
        .resource("alice", rate=100)
        .task("t1", duration="3d")
        .task("new", duration="1d")
        .build()
    )
    out = assemble(bigger, timings, kind="dependency_only", issues=())
    assert set(out.nodes) == {"t1", "new"}
    assert out.nodes["t1"].scheduled
    assert out.nodes["new"].status == "unschedulable"
    assert not out.complete
