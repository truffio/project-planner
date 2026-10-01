"""A01, A02, A13, A24 and the unit-entry rules (plan section 2.1)."""

from __future__ import annotations

from decimal import Decimal

import pytest

import project_planner as pp

from .conftest import (
    D,
    assignment_row,
    code,
    detail_assignment,
    id_of,
    lag_of,
    loading_segments,
    new_project,
    new_ws,
    node_row,
    oct26,
    report_assignment,
    sizing_of,
)


def _a01_project():
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="50")
    t = ws.add_task("Design", effort=pp.hours(40))
    ws.set_assignment(t, alice, percent=80)
    ws.set_assignment(t, bob, percent=20)
    return ws, t, alice, bob


@pytest.mark.acceptance
def test_a01_effort_task_duration_and_individual_efforts():
    ws, t, alice, bob = _a01_project()
    res = ws.schedule()
    row = node_row(res, t)
    # 40 h effort / (0.8 + 0.2) = 40 h = 2400 min; 2400 / 480 min per day = 5 working days
    assert row.duration_days == D(5)
    # effort = 40 h = 5 person-days
    assert row.effort_days == D(5)
    # Alice: 40 h x 0.8 = 32 h = 4 person-days; Bob: 40 h x 0.2 = 8 h = 1 person-day
    assert assignment_row(res, t, alice).assignment_days == D(4)
    assert assignment_row(res, t, bob).assignment_days == D(1)
    # Starts at project start Mon 5 Oct 09:00; 5 full days Mon..Fri -> finish Fri 9 Oct 17:00
    assert row.start == oct26(5, "09:00")
    assert row.finish == oct26(9, "17:00")
    assert res.complete is True
    assert res.project_finish == oct26(9, "17:00")
    # project working span = 2400 / 480 = 5 working days
    assert res.working_span_days == D(5)


@pytest.mark.acceptance
def test_a01_person_hours_work_quantities():
    ws, t, alice, bob = _a01_project()
    ws.schedule()
    rep = ws.cost_report(unit="person_hours")
    # Alice 32 h, Bob 8 h (spec section 5.3 example)
    assert report_assignment(rep, t, alice).work_qty == D(32)
    assert report_assignment(rep, t, bob).work_qty == D(8)
    assert code(report_assignment(rep, t, alice).work_unit) == "person_hours"


@pytest.mark.acceptance
def test_a02_duration_fixed_when_allocation_changes():
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    t = ws.add_task("Build", duration=pp.days(5))
    ws.set_assignment(t, alice, percent=50)
    res = ws.schedule()
    # duration fixed at 5 d; effort = 5 d x 0.5 = 2.5 person-days
    assert node_row(res, t).duration_days == D(5)
    assert node_row(res, t).effort_days == D("2.5")
    assert node_row(res, t).finish == oct26(9, "17:00")
    # loading: a single 50% segment over the whole task
    assert [s.percent for s in loading_segments(ws, alice)] == [D(50)]
    # cost: 40 h x 0.5 x 100 = 2000
    assert node_row(res, t).cost == D(2000)

    # Setting the same pair again updates the existing assignment (spec 5.4).
    ws.set_assignment(t, alice, percent=100)
    assert ws.state().stale_dates is True
    res = ws.schedule()
    # duration still 5 d (Mon 5 09:00 - Fri 9 17:00); effort = 5 x 1.0 = 5 person-days
    assert node_row(res, t).duration_days == D(5)
    assert node_row(res, t).start == oct26(5, "09:00")
    assert node_row(res, t).finish == oct26(9, "17:00")
    assert node_row(res, t).effort_days == D(5)
    assert [s.percent for s in loading_segments(ws, alice)] == [D(100)]
    # cost: 40 h x 1.0 x 100 = 4000
    assert node_row(res, t).cost == D(4000)
    details = ws.task_details(t)
    assert len(details.assignments) == 1
    assert detail_assignment(details, alice).percent == D(100)


@pytest.mark.acceptance
def test_a13_effort_task_without_allocation_is_unschedulable():
    ws = new_ws()
    new_project(ws)
    unassigned = ws.add_task("Unassigned effort", effort=pp.hours(16))
    blocked = ws.add_task("Successor", duration=pp.days(1))
    ws.add_dependency(unassigned, blocked, "FS")
    independent = ws.add_task("Independent", duration=pp.days(1))
    res = ws.schedule()
    # no positive assigned capacity -> unschedulable, with an explanation naming the task
    assert node_row(res, unassigned).scheduled is False
    assert node_row(res, unassigned).start is None
    assert any(id_of(i.object_id) == id_of(unassigned) for i in res.issues)
    # its successor cannot be scheduled either
    assert node_row(res, blocked).scheduled is False
    # an unrelated task is still scheduled: 1 d from Mon 5 09:00 -> Mon 5 17:00
    assert node_row(res, independent).scheduled is True
    assert node_row(res, independent).finish == oct26(5, "17:00")
    # result is incomplete and claims no project finish
    assert res.complete is False
    assert res.project_finish is None


def _single_task_result(**sizing):
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="50")
    t = ws.add_task("T", **sizing)
    ws.set_assignment(t, alice, percent=80)
    ws.set_assignment(t, bob, percent=20)
    res = ws.schedule()
    row = node_row(res, t)
    return (
        ws,
        t,
        (
            row.start,
            row.finish,
            row.duration_days,
            row.effort_days,
            row.cost,
            assignment_row(res, t, alice).assignment_days,
            assignment_row(res, t, bob).assignment_days,
            res.project_finish,
            res.total_cost,
        ),
    )


@pytest.mark.acceptance
@pytest.mark.parametrize("mode", ["duration", "effort"])
def test_a24_40h_and_5d_identical_at_8h_per_day(mode):
    _, _, by_hours_obj = _single_task_result(**{mode: pp.hours(40)})
    _, _, by_days_obj = _single_task_result(**{mode: pp.days(5)})
    _, _, by_hours_str = _single_task_result(**{mode: "40h"})
    _, _, by_days_str = _single_task_result(**{mode: "5d"})
    # 40 h = 2400 min; 5 d x 480 min/day = 2400 min -> every result field identical
    assert by_hours_obj == by_days_obj == by_hours_str == by_days_str
    # and reported in days: duration_days == 5 (40 h / 8 h per day)
    assert by_hours_obj[2] == D(5)


@pytest.mark.acceptance
def test_a24_entered_value_and_unit_preserved_after_schedule():
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    h = ws.add_task("In hours", duration="40h")
    d = ws.add_task("In days", effort=pp.days(5))
    f = ws.add_task("Fractional", duration="1.5d")
    ws.set_assignment(d, alice, percent=100)
    dep = ws.add_dependency(h, f, "FS", lag="-0.5d")
    before = (sizing_of(ws, h), sizing_of(ws, d), sizing_of(ws, f))
    res = ws.schedule()
    after = (sizing_of(ws, h), sizing_of(ws, d), sizing_of(ws, f))
    assert (
        before
        == after
        == (
            (D(40), "hours"),
            (D(5), "days"),
            (D("1.5"), "days"),
        )
    )
    assert lag_of(ws, dep) == (D("-0.5"), "days")
    # outputs are still in days: 40 h -> 5; 1.5 d -> 1.5 (= 12 h / 8 h per day)
    assert node_row(res, h).duration_days == D(5)
    assert node_row(res, f).duration_days == D("1.5")


@pytest.mark.acceptance
@pytest.mark.parametrize("bad", [40, 2.5, Decimal("5")])
@pytest.mark.parametrize("field", ["duration", "effort"])
def test_unit_less_numbers_rejected_for_sizing(field, bad):
    ws = new_ws()
    new_project(ws)
    rev = ws.state().revision
    # Choice (assumption A16): a bare number is a *type* error, message names both forms.
    with pytest.raises(TypeError) as ei:
        ws.add_task("X", **{field: bad})
    msg = str(ei.value)
    assert "hours" in msg and "days" in msg
    assert ws.state().revision == rev


@pytest.mark.acceptance
def test_unit_less_number_rejected_for_lag():
    ws = new_ws()
    new_project(ws)
    a = ws.add_task("A", duration="1d")
    b = ws.add_task("B", duration="1d")
    rev = ws.state().revision
    with pytest.raises(TypeError):
        ws.add_dependency(a, b, "FS", lag=0)
    assert ws.state().revision == rev


@pytest.mark.acceptance
def test_unit_less_string_rejected():
    ws = new_ws()
    new_project(ws)
    rev = ws.state().revision
    # Choice (assumption A16): a string of the right type but without unit is a value error.
    with pytest.raises(pp.ValidationFailed):
        ws.add_task("X", duration="40")
    assert ws.state().revision == rev


@pytest.mark.acceptance
@pytest.mark.parametrize("percent", [0, -10, 150])
def test_assignment_percent_out_of_range_rejected(percent):
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    t = ws.add_task("T", duration="1d")
    rev = ws.state().revision
    # > 0 and <= MAX_ASSIGNMENT_PERCENT (default 100)
    with pytest.raises(pp.ValidationFailed) as ei:
        ws.set_assignment(t, alice, percent=percent)
    assert "percent" in {str(i.field) for i in ei.value.issues}
    assert ws.state().revision == rev
