"""A14 (cycles, dangling and invalid references) and A15 (staleness after edits)."""

from __future__ import annotations

from datetime import date

import pytest

import project_planner as pp

from .conftest import (
    D,
    current,
    id_of,
    mentioned_ids,
    new_project,
    new_ws,
    node_row,
    oct26,
    rejected,
    report_assignment,
)

# ---------------------------------------------------------------- A14


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T12: cycle error names every member (A14)")
def test_a14_cycle_rejected_and_all_members_named():
    ws = new_ws()
    new_project(ws)
    t1 = ws.add_task("T1", duration="1d")
    t2 = ws.add_task("T2", duration="1d")
    t3 = ws.add_task("T3", duration="1d")
    ws.add_task("Unrelated", duration="1d")
    ws.add_dependency(t1, t2, "FS")
    ws.add_dependency(t2, t3, "SS")
    # closing edge T3 -> T1 makes the cycle T1 -> T2 -> T3 -> T1
    exc = rejected(lambda: ws.add_dependency(t3, t1, "FF"), ws)
    assert {id_of(t1), id_of(t2), id_of(t3)} <= mentioned_ids(exc)


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T12: self-dependency rejected (A14)")
def test_a14_self_dependency_rejected():
    ws = new_ws()
    new_project(ws)
    t1 = ws.add_task("T1", duration="1d")
    exc = rejected(lambda: ws.add_dependency(t1, t1, "FS"), ws)
    assert id_of(t1) in mentioned_ids(exc)


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T12: dependency on a summary group rejected (decision 5)")
def test_a14_dependency_on_group_rejected_naming_group():
    ws = new_ws()
    new_project(ws)
    g = ws.add_group("G")
    ws.add_task("Child", parent=g, duration="1d")
    t = ws.add_task("T", duration="1d")
    exc = rejected(lambda: ws.add_dependency(g, t, "FS"), ws)
    assert id_of(g) in mentioned_ids(exc)


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T12: dangling endpoint reference (A14)")
def test_a14_dangling_reference_rejected():
    ws = new_ws()
    new_project(ws)
    t1 = ws.add_task("T1", duration="1d")
    rev = ws.state().revision
    with pytest.raises((pp.NotFound, pp.ValidationFailed)) as ei:
        ws.add_dependency(t1, "no-such-node", "FS")
    exc = ei.value
    text = str(exc) + " ".join(str(getattr(i, "message", "")) for i in getattr(exc, "issues", []))
    ids = {str(getattr(i, "object_id", "")) for i in getattr(exc, "issues", [])}
    ids.add(str(getattr(exc, "object_id", "")))
    assert "no-such-node" in ids or "no-such-node" in text
    # nothing applied
    assert ws.state().revision == rev
    res = ws.schedule()
    assert res.complete is True
    assert node_row(res, t1).finish == oct26(5, "17:00")


# ---------------------------------------------------------------- A15


def _scheduled_project():
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    a = ws.add_task("A", duration="5d")  # Mon 5 09:00 - Fri 9 17:00
    b = ws.add_task("B", duration="1d")
    ws.set_assignment(a, alice, percent=100)
    res = ws.schedule()
    assert ws.state().stale_dates is False
    return ws, res, alice, a, b


STALE_EDITS = [
    "sizing",
    "assignment",
    "dependency",
    "holiday",
    "hours_per_day",
    "new_task",
]


def _apply_edit(name, ws, alice, a, b):
    if name == "sizing":
        ws.set_sizing(a, duration="3d")
    elif name == "assignment":
        ws.set_assignment(b, alice, percent=50)
    elif name == "dependency":
        ws.add_dependency(a, b, "FS")
    elif name == "holiday":
        ws.calendar.add_holiday(date(2026, 10, 7), "Holiday")
    elif name == "hours_per_day":
        ws.calendar.set_hours_per_day(D("7.5"))
    elif name == "new_task":
        ws.add_task("C", duration="1d")
    else:  # pragma: no cover
        raise AssertionError(name)


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T31: edits mark the schedule stale (A15)")
@pytest.mark.parametrize("edit", STALE_EDITS)
def test_a15_edit_after_schedule_marks_results_stale(edit):
    ws, res, alice, a, b = _scheduled_project()
    rev = ws.state().revision
    _apply_edit(edit, ws, alice, a, b)
    st = ws.state()
    assert st.stale_dates is True
    assert st.dirty is True
    assert st.revision > rev
    # the old result is kept (not silently recalculated) but is no longer current
    assert node_row(current(ws), a).finish == oct26(9, "17:00")
    # explicit recalculation clears staleness
    ws.schedule()
    assert ws.state().stale_dates is False


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T31: recalculation reflects the edit (A15)")
def test_a15_recalculation_replaces_stale_results():
    ws, res, alice, a, b = _scheduled_project()
    ws.set_sizing(a, duration="3d")
    ws.add_dependency(a, b, "FS")
    new = ws.schedule()
    # A: 3 d -> Mon 5 09:00 - Wed 7 17:00; B FS -> Thu 8 09:00 - Thu 8 17:00
    assert node_row(new, a).finish == oct26(7, "17:00")
    assert node_row(new, b).start == oct26(8, "09:00")
    assert new.project_finish == oct26(8, "17:00")
    # A's cost now 24 h x 100 = 2400
    assert node_row(new, a).cost == D(2400)


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T31: working_days_per_year edit is not stale (plan 2.3)")
def test_working_days_per_year_change_does_not_stale_schedule():
    ws, res, alice, a, b = _scheduled_project()
    ws.calendar.set_working_days_per_year(250)
    st = ws.state()
    assert st.stale_dates is False
    assert st.stale_costs is False
    # money unchanged: 40 h x 100 = 4000; person-years now 40 / (8 x 250) = 0.02
    rep = ws.cost_report(unit="person_years")
    assert rep.total_cost == D(4000)
    assert report_assignment(rep, a, alice).work_qty == D("0.02")
