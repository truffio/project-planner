"""A16, A17, A18, A20, A21, A25 -- cost calculation and cost-report units (plan 2.2).

Person-year values repeat (e.g. 32 / 1760), so they are compared after quantizing
to 10 decimal places (helper ``q``) against the exact fraction; money and
person-hour / person-day values are compared exactly as Decimal.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import project_planner as pp

from .conftest import (
    D,
    assignment_row,
    code,
    current,
    detail_assignment,
    id_of,
    new_project,
    new_ws,
    node_row,
    oct26,
    q,
    report_assignment,
    report_node,
    sizing_of,
)

UNITS = ["person_hours", "person_days", "person_years"]


def _a16(bob_rate: str | None = "50"):
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = (
        ws.add_resource("Bob", hourly_rate=bob_rate)
        if bob_rate is not None
        else ws.add_resource("Bob")
    )
    t = ws.add_task("Design", effort=pp.hours(40))
    ws.set_assignment(t, alice, percent=80)
    ws.set_assignment(t, bob, percent=20)
    return ws, t, alice, bob


# 8 h/day, 220 days/year -> 1 person-year = 1760 h.
# (unit, Alice work, Alice rate/unit, Bob work, Bob rate/unit, task work)
A16_BREAKDOWN = [
    # 32 h x 100 = 3200; 8 h x 50 = 400
    ("person_hours", D(32), D(100), D(8), D(50), D(40)),
    # 32 / 8 = 4 d x (100 x 8 = 800) ; 8 / 8 = 1 d x (50 x 8 = 400); task 40 / 8 = 5
    ("person_days", D(4), D(800), D(1), D(400), D(5)),
    # 32 / 1760 py x (100 x 1760 = 176000); 8 / 1760 py x 88000; task 40 / 1760
    (
        "person_years",
        D(32) / D(1760),
        D(176000),
        D(8) / D(1760),
        D(88000),
        D(40) / D(1760),
    ),
]


@pytest.mark.acceptance
def test_a16_assignment_and_task_costs():
    ws, t, alice, bob = _a16()
    res = ws.schedule()
    # Alice 40 h x 0.8 = 32 h x 100 = 3200; Bob 40 h x 0.2 = 8 h x 50 = 400
    assert assignment_row(res, t, alice).cost == D(3200)
    assert assignment_row(res, t, bob).cost == D(400)
    # task = 3200 + 400 = 3600 (not 40 h x average rate 75 = 3000)
    assert node_row(res, t).cost == D(3600)
    assert res.total_cost == D(3600)
    assert isinstance(res.total_cost, Decimal)
    assert res.cost_complete is True


@pytest.mark.acceptance
@pytest.mark.parametrize(("unit", "a_work", "a_rate", "b_work", "b_rate", "t_work"), A16_BREAKDOWN)
def test_a16_cost_report_in_every_unit(unit, a_work, a_rate, b_work, b_rate, t_work):
    ws, t, alice, bob = _a16()
    ws.schedule()
    rep = ws.cost_report(unit=unit)
    assert code(rep.unit) == unit
    ra, rb = report_assignment(rep, t, alice), report_assignment(rep, t, bob)
    assert code(ra.work_unit) == unit
    assert q(ra.work_qty) == q(a_work)
    assert ra.rate_per_unit == a_rate
    assert q(rb.work_qty) == q(b_work)
    assert rb.rate_per_unit == b_rate
    assert q(report_node(rep, t).work_qty) == q(t_work)
    # money identical in every unit: 3200 / 400 / 3600
    assert ra.cost == D(3200)
    assert rb.cost == D(400)
    assert report_node(rep, t).cost == D(3600)
    assert rep.total_cost == D(3600)
    assert rep.complete is True


@pytest.mark.acceptance
def test_a16_task_details_show_percent_work_rate_cost():
    ws, t, alice, bob = _a16()
    ws.schedule()
    det = ws.task_details(t, unit="person_hours")
    a, b = detail_assignment(det, alice), detail_assignment(det, bob)
    # Alice: 80%, 4 person-days, 32 h x $100/h = 3200
    assert (a.percent, a.assignment_days, a.work_qty, a.rate_per_unit, a.cost) == (
        D(80),
        D(4),
        D(32),
        D(100),
        D(3200),
    )
    # Bob: 20%, 1 person-day, 8 h x $50/h = 400
    assert (b.percent, b.assignment_days, b.work_qty, b.rate_per_unit, b.cost) == (
        D(20),
        D(1),
        D(8),
        D(50),
        D(400),
    )
    # default unit of task details = project cost_report_unit = person_days
    det_default = ws.task_details(t)
    assert code(detail_assignment(det_default, alice).work_unit) == "person_days"
    assert detail_assignment(det_default, alice).work_qty == D(4)


@pytest.mark.acceptance
def test_a17_nested_groups_sum_leaf_costs_once():
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="50")
    g = ws.add_group("Phase")
    g1 = ws.add_group("Sub 1", parent=g)
    g2 = ws.add_group("Sub 2", parent=g)
    t1 = ws.add_task("T1", parent=g1, duration="1d")
    t2 = ws.add_task("T2", parent=g1, duration="2d")
    m = ws.add_milestone("M", parent=g1)
    t3 = ws.add_task("T3", parent=g2, duration="0.5d")
    t4 = ws.add_task("T4", duration="1d")  # top level, outside the groups
    ws.set_assignment(t1, alice, percent=100)
    ws.set_assignment(t2, bob, percent=50)
    ws.set_assignment(t3, alice, percent=100)
    ws.set_assignment(t4, bob, percent=100)
    res = ws.schedule()
    # T1 8 h x 1.0 x 100 = 800; T2 16 h x 0.5 x 50 = 400; T3 4 h x 1.0 x 100 = 400;
    # T4 8 h x 1.0 x 50 = 400; milestone 0
    for node, cost in ((t1, 800), (t2, 400), (t3, 400), (t4, 400), (m, 0)):
        assert node_row(res, node).cost == D(cost)
    # G1 = 800 + 400 + 0 = 1200; G2 = 400; G = 1200 + 400 = 1600 (no double counting)
    assert node_row(res, g1).cost == D(1200)
    assert node_row(res, g2).cost == D(400)
    assert node_row(res, g).cost == D(1600)
    # project = all leaves = 800 + 400 + 400 + 400 = 2000
    assert res.total_cost == D(2000)
    # effort: G1 = 1 + 1 = 2 pd; G2 = 0.5; G = 2.5
    assert node_row(res, g1).effort_days == D(2)
    assert node_row(res, g2).effort_days == D("0.5")
    assert node_row(res, g).effort_days == D("2.5")
    # dates: G1 = [min start, max finish] = Mon 5 09:00 .. Tue 6 17:00 -> 2 days, not 1 + 2
    assert node_row(res, g1).start == oct26(5, "09:00")
    assert node_row(res, g1).finish == oct26(6, "17:00")
    assert node_row(res, g1).duration_days == D(2)
    # G2 = T3 = Mon 5 09:00 .. 13:00
    assert node_row(res, g2).finish == oct26(5, "13:00")
    assert node_row(res, g).duration_days == D(2)
    # same totals in the cost report
    rep = ws.cost_report(unit="person_hours")
    assert report_node(rep, g).cost == D(1600)
    assert rep.total_cost == D(2000)


@pytest.mark.acceptance
def test_a18_rate_change_updates_costs_only():
    ws, t, alice, bob = _a16()
    res = ws.schedule()
    before_dates = (node_row(res, t).start, node_row(res, t).finish)
    ws.set_hourly_rate(alice, "120")
    st = ws.state()
    # rate edits recalculate costs immediately and never stale the dates
    assert st.stale_dates is False
    assert st.stale_costs is False
    now = current(ws)
    assert (node_row(now, t).start, node_row(now, t).finish) == before_dates
    # Alice 32 h x 120 = 3840; Bob unchanged 400; task 4240
    assert assignment_row(now, t, alice).cost == D(3840)
    assert node_row(now, t).cost == D(4240)
    assert now.total_cost == D(4240)
    rep = ws.cost_report(unit="person_days")
    # 4 d x (120 x 8 = 960) = 3840
    assert report_assignment(rep, t, alice).rate_per_unit == D(960)
    assert rep.total_cost == D(4240)


@pytest.mark.acceptance
def test_a20_missing_rate_makes_estimate_incomplete():
    ws, t, alice, bob = _a16(bob_rate=None)
    res = ws.schedule()
    # schedule is unaffected by a missing rate
    assert res.complete is True
    assert node_row(res, t).finish == oct26(9, "17:00")
    # Alice's part is known (3200); Bob's is not
    assert assignment_row(res, t, alice).cost == D(3200)
    assert assignment_row(res, t, bob).cost is None
    assert node_row(res, t).cost_complete is False
    assert res.cost_complete is False
    rep = ws.cost_report()
    assert rep.complete is False
    assert id_of(bob) in [id_of(r) for r in rep.missing_rate_resources]


@pytest.mark.acceptance
def test_a20_explicit_zero_rate_is_complete():
    ws, t, alice, bob = _a16(bob_rate="0")
    res = ws.schedule()
    # 3200 + 8 h x 0 = 3200, complete
    assert assignment_row(res, t, bob).cost == D(0)
    assert res.total_cost == D(3200)
    assert res.cost_complete is True


def _a21():
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="60")
    t = ws.add_task("Sixteen hours", duration=pp.hours(16))
    ws.set_assignment(t, alice, percent=75)
    ws.set_assignment(t, bob, percent=25)
    return ws, t, alice, bob


@pytest.mark.acceptance
def test_a21_duration_task_weighted_costs():
    ws, t, alice, bob = _a21()
    res = ws.schedule()
    # 16 h / 8 h per day = 2 working days: Mon 5 09:00 - Tue 6 17:00
    assert node_row(res, t).duration_days == D(2)
    assert node_row(res, t).finish == oct26(6, "17:00")
    # Alice 16 h x 0.75 = 12 h = 1.5 pd -> 12 x 100 = 1200
    assert assignment_row(res, t, alice).assignment_days == D("1.5")
    assert assignment_row(res, t, alice).cost == D(1200)
    # Bob 16 h x 0.25 = 4 h = 0.5 pd -> 4 x 60 = 240
    assert assignment_row(res, t, bob).assignment_days == D("0.5")
    assert assignment_row(res, t, bob).cost == D(240)
    # total 1200 + 240 = 1440; effort 2 d x (0.75 + 0.25) = 2 pd
    assert node_row(res, t).cost == D(1440)
    assert node_row(res, t).effort_days == D(2)


A21_BREAKDOWN = [
    # Alice 12 h x 100; Bob 4 h x 60
    ("person_hours", D(12), D(100), D(4), D(60)),
    # Alice 1.5 d x 800; Bob 0.5 d x 480
    ("person_days", D("1.5"), D(800), D("0.5"), D(480)),
    # Alice 12/1760 py x 176000; Bob 4/1760 py x (60 x 1760 = 105600)
    ("person_years", D(12) / D(1760), D(176000), D(4) / D(1760), D(105600)),
]


@pytest.mark.acceptance
@pytest.mark.parametrize(("unit", "a_work", "a_rate", "b_work", "b_rate"), A21_BREAKDOWN)
def test_a21_cost_report_in_every_unit(unit, a_work, a_rate, b_work, b_rate):
    ws, t, alice, bob = _a21()
    ws.schedule()
    rep = ws.cost_report(unit=unit)
    ra, rb = report_assignment(rep, t, alice), report_assignment(rep, t, bob)
    assert q(ra.work_qty) == q(a_work)
    assert ra.rate_per_unit == a_rate
    assert q(rb.work_qty) == q(b_work)
    assert rb.rate_per_unit == b_rate
    # money identical in every unit: 1200 / 240 / 1440
    assert (ra.cost, rb.cost, rep.total_cost) == (D(1200), D(240), D(1440))


@pytest.mark.acceptance
def test_a25_switching_report_unit_changes_no_cost_dates_or_staleness():
    ws, t, alice, bob = _a16()
    res = ws.schedule()
    dates = (node_row(res, t).start, node_row(res, t).finish, res.project_finish)
    rev = ws.state().revision
    totals = {u: ws.cost_report(unit=u).total_cost for u in UNITS}
    # querying reports is not an edit
    assert ws.state().revision == rev
    assert totals == {u: D(3600) for u in UNITS}
    # default project report unit is person_days (plan 2.2)
    assert code(ws.cost_report().unit) == "person_days"

    # changing the project's cost_report_unit setting
    ws.set_cost_report_unit("person_years")
    st = ws.state()
    assert st.stale_dates is False
    assert st.stale_costs is False
    assert code(ws.cost_report().unit) == "person_years"
    assert ws.cost_report().total_cost == D(3600)
    now = current(ws)
    assert (node_row(now, t).start, node_row(now, t).finish, now.project_finish) == dates
    assert now.total_cost == D(3600)
    # task sizing as entered is untouched
    assert sizing_of(ws, t) == (D(40), "hours")


@pytest.mark.acceptance
def test_a25_cost_report_unit_set_at_project_creation():
    ws = new_ws()
    new_project(ws, cost_report_unit="person_hours")
    alice = ws.add_resource("Alice", hourly_rate="100")
    t = ws.add_task("T", duration="1d")
    ws.set_assignment(t, alice, percent=100)
    ws.schedule()
    rep = ws.cost_report()
    # 8 h x 100 = 800 reported as 8 person-hours at $100/h
    assert code(rep.unit) == "person_hours"
    assert report_assignment(rep, t, alice).work_qty == D(8)
    assert rep.total_cost == D(800)
