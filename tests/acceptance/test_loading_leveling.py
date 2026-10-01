"""A03 (overload visible), A07 / A08 / A19 (leveling) and the leveling state machine.

Unresolvable-overload case (spec section 10, a task whose own assignment exceeds
capacity): NOT covered here. It needs MAX_ASSIGNMENT_PERCENT > 100, which lives in
engine/config.py and is not exposed through the section 1.2 facade; T18 covers it
with engine-level unit tests. The facade-level default (percent > 100 rejected) is
tested in test_sizing.py.
"""

from __future__ import annotations

from datetime import date

import pytest

from .conftest import (
    D,
    current,
    daily_loading,
    id_of,
    loading_segments,
    new_project,
    new_ws,
    node_row,
    oct26,
)

# ---------------------------------------------------------------- A03


@pytest.mark.acceptance
def test_a03_overlapping_assignments_show_130_percent():
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    t1 = ws.add_task("T1", duration="5d")  # [0, 2400) Mon 5 09:00 - Fri 9 17:00
    t2 = ws.add_task("T2", duration="2d")  # [0, 960)  Mon 5 09:00 - Tue 6 17:00
    ws.set_assignment(t1, alice, percent=80)
    ws.set_assignment(t2, alice, percent=50)
    ws.schedule()
    segs = loading_segments(ws, alice)
    over = [s for s in segs if s.overloaded]
    # 80 + 50 = 130% over [0, 960) = Mon 5 09:00 .. Tue 6 17:00, not capped at 100
    assert len(over) == 1
    assert over[0].percent == D(130)
    assert over[0].start == oct26(5, "09:00")
    assert over[0].end == oct26(6, "17:00")
    assert {id_of(x) for x in over[0].task_ids} == {id_of(t1), id_of(t2)}
    # remainder [960, 2400) = Wed 7 09:00 .. Fri 9 17:00 at 80%, not overloaded
    rest = [s for s in segs if not s.overloaded]
    assert [(s.start, s.end, s.percent) for s in rest] == [
        (oct26(7, "09:00"), oct26(9, "17:00"), D(80))
    ]
    days = daily_loading(ws, alice)
    # Mon 5: 130% all day -> peak 130, average 130, assigned 8h x 1.3 / 8h = 1.3 person-days
    assert days[date(2026, 10, 5)].peak_percent == D(130)
    assert days[date(2026, 10, 5)].average_percent == D(130)
    assert days[date(2026, 10, 5)].assigned_days == D("1.3")
    # Wed 7: 80% -> 0.8 person-days
    assert days[date(2026, 10, 7)].assigned_days == D("0.8")


@pytest.mark.acceptance
def test_a03_brief_overload_peak_vs_average():
    ws = new_ws()
    new_project(ws)
    bob = ws.add_resource("Bob", hourly_rate="50")
    t4 = ws.add_task("T4", duration="1d")  # [0, 480)
    t5 = ws.add_task("T5", duration="2h")  # [0, 120) Mon 5 09:00 - 11:00
    ws.set_assignment(t4, bob, percent=80)
    ws.set_assignment(t5, bob, percent=50)
    ws.schedule()
    over = [s for s in loading_segments(ws, bob) if s.overloaded]
    assert [(s.start, s.end, s.percent) for s in over] == [
        (oct26(5, "09:00"), oct26(5, "11:00"), D(130))
    ]
    mon = daily_loading(ws, bob)[date(2026, 10, 5)]
    # peak 130; average = (2 h x 130 + 6 h x 80) / 8 h = (260 + 480) / 8 = 92.5
    assert mon.peak_percent == D(130)
    assert mon.average_percent == D("92.5")
    # assigned = (8 h x 0.8 + 2 h x 0.5) / 8 h = 7.4 / 8 = 0.925 person-days
    assert mon.assigned_days == D("0.925")


# ---------------------------------------------------------------- A07 / A08 / A19


def _overloaded_project():
    """Alice 60% on T1 (3 d) and 60% on T2 (3 d), both at project start; T3 (1 d) FS after T2.

    Dependency-only:
      T1 [0, 1440)    Mon 5 09:00 - Wed 7 17:00
      T2 [0, 1440)    Mon 5 09:00 - Wed 7 17:00   (Alice at 120% Mon-Wed)
      T3 [1440, 1920) Thu 8 09:00 - Thu 8 17:00   -> project finish Thu 8 17:00
    Leveled (T1 placed first: same dependency-only start, earlier WBS order):
      T1 unchanged
      T2 [1440, 2880) Thu 8 09:00 - Mon 12 17:00  (weekend inside; delay 1440 min = 3 d)
      T3 [2880, 3360) Tue 13 09:00 - Tue 13 17:00 -> project finish Tue 13 17:00
    Cost: each of T1, T2 = 24 h x 0.6 x 100 = 1440; T3 unassigned = 0; total 2880.
    """
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    t1 = ws.add_task("T1", duration="3d")
    t2 = ws.add_task("T2", duration="3d")
    t3 = ws.add_task("T3", duration="1d")
    ws.set_assignment(t1, alice, percent=60)
    ws.set_assignment(t2, alice, percent=60)
    ws.add_dependency(t2, t3, "FS")
    return ws, alice, t1, t2, t3


@pytest.mark.acceptance
def test_a07_leveling_delays_exactly_one_whole_task():
    ws, alice, t1, t2, t3 = _overloaded_project()
    base = ws.schedule()
    assert base.project_finish == oct26(8, "17:00")
    assert any(s.overloaded for s in loading_segments(ws, alice))
    preview = ws.level_preview()
    lv = preview.result
    # T1 unchanged
    assert node_row(lv, t1).start == oct26(5, "09:00")
    assert node_row(lv, t1).finish == oct26(7, "17:00")
    # T2 delayed as a whole to Thu 8 09:00 - Mon 12 17:00 (3 working days of delay)
    assert node_row(lv, t2).start == oct26(8, "09:00")
    assert node_row(lv, t2).finish == oct26(12, "17:00")
    assert preview.delays_days[id_of(t2)] == D(3)
    assert preview.delays_days.get(id_of(t1), D(0)) == D(0)
    assert node_row(lv, t2).leveling_delay_days == D(3)
    # dependency T2 -FS-> T3 still holds: T3 Tue 13 09:00 - 17:00
    assert node_row(lv, t3).start == oct26(13, "09:00")
    assert node_row(lv, t3).finish == oct26(13, "17:00")
    assert node_row(lv, t3).start >= node_row(lv, t2).finish
    # project finish recalculated: Tue 13 17:00; delta (3360 - 1920) / 480 = 3 days
    assert lv.project_finish == oct26(13, "17:00")
    assert preview.finish_delta_days == D(3)
    assert list(preview.unresolved) == []
    # durations unchanged
    for t in (t1, t2, t3):
        assert node_row(lv, t).duration_days == node_row(base, t).duration_days
    # assignments and percents unchanged
    for t in (t1, t2):
        assert [(id_of(a.resource_id), a.percent) for a in ws.task_details(t).assignments] == [
            (id_of(alice), D(60))
        ]


@pytest.mark.acceptance
def test_a08_weekend_inside_leveled_task():
    ws, alice, _, t2, _ = _overloaded_project()
    ws.schedule()
    ws.level_preview()
    ws.apply_leveling()
    lv = current(ws)
    row = node_row(lv, t2)
    # Thu 8, Fri 9, (Sat/Sun pause), Mon 12: still exactly 3 working days, not split
    assert (row.start, row.finish) == (oct26(8, "09:00"), oct26(12, "17:00"))
    assert row.duration_days == D(3)
    # no resource exceeds 100% after leveling
    segs = loading_segments(ws, alice)
    assert all(s.percent <= D(100) and not s.overloaded for s in segs)
    # T2 loading: 0.6 person-days each on Thu 8, Fri 9, Mon 12 and nothing at the weekend
    days = daily_loading(ws, alice)
    for d in (8, 9, 12):
        assert days[date(2026, 10, d)].assigned_days == D("0.6")
    for d in (10, 11):
        b = days.get(date(2026, 10, d))
        assert b is None or b.assigned_days == D(0)
    # elapsed calendar span Mon 5 -> Tue 13 exceeds working span 3360 / 480 = 7 days
    assert lv.working_span_days == D(7)
    assert lv.elapsed_span_calendar_days > lv.working_span_days


@pytest.mark.acceptance
def test_a19_leveling_does_not_change_cost():
    ws, alice, t1, t2, t3 = _overloaded_project()
    base = ws.schedule()
    # 24 h x 0.6 x 100 = 1440 per assigned task; total 2880
    assert base.total_cost == D(2880)
    preview = ws.level_preview()
    lv = preview.result
    assert lv.total_cost == D(2880)
    for t, expected in ((t1, D(1440)), (t2, D(1440)), (t3, D(0))):
        assert node_row(base, t).cost == expected
        assert node_row(lv, t).cost == expected
        assert node_row(lv, t).effort_days == node_row(base, t).effort_days
    ws.apply_leveling()
    assert ws.cost_report(unit="person_hours").total_cost == D(2880)


@pytest.mark.acceptance
def test_leveling_preview_apply_discard_reset_transitions():
    ws, _, _, t2, _ = _overloaded_project()
    ws.schedule()
    st = ws.state()
    assert st.has_preview is False
    assert current(ws).kind == "dependency_only"

    ws.level_preview()
    # a preview does not replace the current schedule
    assert ws.state().has_preview is True
    assert current(ws).kind == "dependency_only"
    assert node_row(current(ws), t2).start == oct26(5, "09:00")

    ws.discard_leveling()
    assert ws.state().has_preview is False
    assert node_row(current(ws), t2).start == oct26(5, "09:00")

    ws.level_preview()
    ws.apply_leveling()
    assert ws.state().has_preview is False
    assert ws.state().stale_dates is False
    assert current(ws).kind == "leveled"
    assert node_row(current(ws), t2).start == oct26(8, "09:00")

    ws.reset_to_dependency_schedule()
    assert current(ws).kind == "dependency_only"
    assert node_row(current(ws), t2).start == oct26(5, "09:00")
    assert node_row(current(ws), t2).leveling_delay_days == D(0)


@pytest.mark.acceptance
def test_leveled_schedule_goes_stale_and_is_not_releveled_silently():
    ws, _, _, t2, _ = _overloaded_project()
    ws.schedule()
    ws.level_preview()
    ws.apply_leveling()
    ws.add_task("New", duration="1d")
    assert ws.state().stale_dates is True
    res = ws.schedule()
    # explicit Calculate gives a dependency-only schedule; T2 back at Mon 5
    assert res.kind == "dependency_only"
    assert node_row(res, t2).start == oct26(5, "09:00")
