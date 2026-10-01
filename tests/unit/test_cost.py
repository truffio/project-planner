from __future__ import annotations

from decimal import Decimal as D

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.cost import (
    CostReport,
    compute_costs,
    cost_report,
    round_money,
)
from project_planner.engine.model import Project, WorkUnit

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


def alice_row(rep: CostReport):
    return next(a for a in rep.tasks["t1"].assignments if a.resource_id == "alice")


def test_a16_costs() -> None:
    r = compute_costs(a16(), {"t1": 2400})
    t = r.tasks["t1"]
    by = {a.resource_id: a for a in t.assignments}
    assert by["alice"].assignment_minutes == 1920
    assert by["alice"].assignment_hours == 32
    assert by["alice"].cost == 3200
    assert by["bob"].assignment_hours == 8
    assert by["bob"].cost == 400
    assert t.cost == 3600 and t.cost_complete
    assert r.total == 3600 and r.complete


def test_a16_report_units() -> None:
    p = a16()
    r = compute_costs(p, {"t1": 2400})
    ph = cost_report(r, p, "person_hours")
    pd = cost_report(r, p, "person_days")
    py = cost_report(r, p, WorkUnit.PERSON_YEARS)
    assert (alice_row(ph).work_qty, alice_row(ph).rate_per_unit) == (32, 100)
    assert (alice_row(pd).work_qty, alice_row(pd).rate_per_unit) == (4, 800)
    assert alice_row(py).work_qty == D(32) / 1760
    assert alice_row(py).rate_per_unit == 176000
    assert abs(alice_row(py).work_qty * 176000 - 3200) < D("0.000001")
    for rep in (ph, pd, py):
        assert alice_row(rep).cost == 3200
        assert rep.cost == 3600 and rep.tasks["t1"].cost == 3600


def test_default_unit_is_project_unit() -> None:
    p = a16()
    r = compute_costs(p, {"t1": 2400})
    rep = cost_report(r, p)
    assert rep.unit is WorkUnit.PERSON_DAYS
    assert rep.work_qty == 4 + D("1")  # 40 h / 8 h per day = 5 d


def test_a21() -> None:
    p = (
        ProjectBuilder()
        .resource("alice", rate=100)
        .resource("bob", rate=60)
        .task("t1", duration="2d")
        .assign("t1", "alice", 75)
        .assign("t1", "bob", 25)
        .build()
    )
    r = compute_costs(p, {"t1": 960})
    by = {a.resource_id: a for a in r.tasks["t1"].assignments}
    assert (by["alice"].assignment_hours, by["alice"].cost) == (12, 1200)
    assert (by["bob"].assignment_hours, by["bob"].cost) == (4, 240)
    assert r.total == 1440


def test_a17_nested_groups() -> None:
    p = (
        ProjectBuilder()
        .resource("r1", rate=60)
        .group("g1")
        .group("g2", parent="g1")
        .task("t1", parent="g2", duration="1d")
        .task("t2", parent="g2", duration="1d")
        .task("t3", parent="g1", duration="1d")
        .task("t4", duration="1d")
        .milestone("m1", parent="g1")
        .assign("t1", "r1")
        .assign("t2", "r1")
        .assign("t3", "r1")
        .assign("t4", "r1")
        .build()
    )
    r = compute_costs(p, {"t1": 480, "t2": 480, "t3": 480, "t4": 480, "m1": 0})
    assert r.groups["g2"].cost == 960
    assert r.groups["g1"].cost == 1440
    assert r.total == 1920
    rep = cost_report(r, p, "person_hours")
    assert rep.groups["g1"].work_qty == 24 and rep.work_qty == 32


def test_a20_missing_rate_and_zero_rate() -> None:
    p = (
        ProjectBuilder()
        .resource("a", rate=None)
        .resource("z", rate=0)
        .resource("c", rate=10)
        .group("g1")
        .task("t1", parent="g1", duration="1d")
        .task("t2", duration="1d")
        .assign("t1", "a")
        .assign("t1", "c")
        .assign("t2", "z")
        .build()
    )
    r = compute_costs(p, {"t1": 60, "t2": 60})
    assert not r.tasks["t1"].cost_complete
    assert r.tasks["t1"].missing_rate_resources == ("a",)
    assert r.tasks["t1"].cost == 10
    assert r.tasks["t1"].assignments[0].cost is None
    assert r.tasks["t2"].cost_complete and r.tasks["t2"].cost == 0
    assert not r.groups["g1"].cost_complete
    assert not r.complete and r.missing_rate_resources == ("a",)


def test_milestone_and_unschedulable() -> None:
    p = (
        ProjectBuilder()
        .resource("r", rate=10)
        .milestone("m1")
        .task("t1")
        .assign("t1", "r")
        .build()
    )
    r = compute_costs(p, {"m1": 0, "t1": None})
    assert r.tasks["m1"].cost == 0 and r.tasks["m1"].cost_complete
    assert r.tasks["t1"].cost == 0 and not r.tasks["t1"].cost_complete
    assert not r.complete


def test_a25_unit_changes_only_qty_and_rate() -> None:
    p = a16()
    r = compute_costs(p, {"t1": 2400})
    a = cost_report(r, p, "person_hours")
    b = cost_report(r, p, "person_years")
    assert a.cost == b.cost
    x, y = alice_row(a), alice_row(b)
    assert x.cost == y.cost
    assert x.work_qty != y.work_qty and x.rate_per_unit != y.rate_per_unit


def test_round_money() -> None:
    assert round_money(D("1.005")) == D("1.00")
    assert round_money(D("2.675")) == D("2.68")
