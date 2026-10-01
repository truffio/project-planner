from __future__ import annotations

import csv
import dataclasses
import io
import pickle
from decimal import Decimal as D

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.csv_io import ExportableResult, export
from project_planner.engine.model import Project, WorkUnit
from project_planner.engine.results import LevelingResult, ScheduleResult
from project_planner.engine.schedule import level, recost, schedule

pytestmark = pytest.mark.unit


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


def records(text: str, record_type: str) -> list[dict[str, str]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    return [r for r in rows if r["record_type"] == record_type]


def as_exportable(r: ScheduleResult) -> ExportableResult:
    return r  # mypy: ScheduleResult must satisfy the protocol structurally


def test_csv_export_of_a16() -> None:
    p = a16()
    text = export(p, as_exportable(schedule(p)))
    (proj,) = records(text, "RESULT_PROJECT")
    assert proj["cost"] in ("3600", "3600.00")
    assert proj["schedule_status"] == "current"
    assert proj["work_unit"] == "person_days"
    nodes = records(text, "RESULT_NODE")
    assert [n["id"] for n in nodes] == ["t1"]
    assert nodes[0]["node_status"] == "scheduled"
    assert nodes[0]["cost"] in ("3600", "3600.00")
    assigns = {a["resource_id"]: a for a in records(text, "RESULT_ASSIGNMENT")}
    assert assigns["alice"]["cost"] in ("3200", "3200.00")
    assert assigns["bob"]["cost"] in ("400", "400.00")
    assert D(assigns["alice"]["work_qty"]) == D(4)
    assert D(assigns["alice"]["rate_per_unit"]) == D(800)
    assert D(assigns["alice"]["assignment_days"]) == D(4)


def test_export_views_follow_report_unit() -> None:
    p = dataclasses.replace(a16(), cost_report_unit=WorkUnit.PERSON_HOURS)
    r = schedule(p)
    assert r.work_unit == "person_hours" and r.work_qty == 40
    alice = next(a for a in r.assignment_results if a.resource_id == "alice")
    assert (alice.work_qty, alice.rate_per_unit, alice.assignment_days) == (D(32), D(100), D(4))
    assert r.effort_days == 5
    assert r.cost_total == r.total_cost == 3600
    # recost picks up a changed report unit
    q = dataclasses.replace(p, cost_report_unit=WorkUnit.PERSON_DAYS)
    assert recost(r, q).work_unit == "person_days"


def test_export_rows_status_and_issue_codes() -> None:
    p = ProjectBuilder().task("t1").group("g1").task("t2", duration="1d", parent="g1").build()
    r = schedule(p)
    by_id = {n.node_id: n for n in r.node_results}
    assert by_id["t1"].status == "unscheduled"
    assert by_id["t1"].issue_codes == ("TASK_UNSIZED",)
    assert by_id["t2"].status == "scheduled" and by_id["t2"].issue_codes == ()
    assert by_id["g1"].status == "scheduled" and by_id["g1"].work_unit == "person_days"
    text = export(p, as_exportable(r))
    assert records(text, "RESULT_PROJECT")[0]["schedule_status"] == "incomplete"


def test_leveling_result_shape() -> None:
    p = (
        ProjectBuilder()
        .resource("a", rate=100)
        .task("t1", duration="3d")
        .task("t2", duration="3d")
        .assign("t1", "a", 60)
        .assign("t2", "a", 60)
        .build()
    )
    lv = level(p, schedule(p))
    assert isinstance(lv, LevelingResult)
    assert lv.result.kind == "leveling_preview"
    assert export(p, as_exportable(lv.result))
    assert {n.node_id: n.leveling_delay_days for n in lv.result.node_results}["t2"] == 3


def test_results_are_frozen_and_picklable() -> None:
    r = schedule(a16())
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.complete = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.node("t1").cost = D(0)  # type: ignore[misc]
    assert pickle.loads(pickle.dumps(r)) == r
