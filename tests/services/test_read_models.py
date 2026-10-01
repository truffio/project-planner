"""Read models (T34): cost report, task details, loading, WBS/Gantt rows, summary."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.errors import Conflict, NotFound
from project_planner.engine.model import DependencyType, NodeKind, TimeQty, TimeUnit, WorkUnit
from project_planner.engine.schedule import schedule
from project_planner.persistence import repositories as repo
from project_planner.services import read_models as rm
from project_planner.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.services

START = date(2026, 10, 5)  # Monday


def D(x: str | int) -> Decimal:
    return Decimal(str(x))


def calc(ws: Workspace) -> None:
    ws._store_result(schedule(ws.project()))


@pytest.fixture
def ws() -> Iterator[Workspace]:
    workspace = open_workspace(":memory:")
    workspace.new_project("Demo", START)
    yield workspace
    workspace.close()


def make_a16(ws: Workspace) -> tuple[str, str, str]:
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="50")
    t = ws.add_task("Design", effort="40h")
    ws.set_assignment(t, alice, 80)
    ws.set_assignment(t, bob, 20)
    return t, alice, bob


# ------------------------------------------------------------------ cost


A16 = [
    ("person_hours", D(32), D(100), D(8), D(50), D(40)),
    ("person_days", D(4), D(800), D(1), D(400), D(5)),
    ("person_years", D(32) / D(1760), D(176000), D(8) / D(1760), D(88000), D(40) / D(1760)),
]


@pytest.mark.parametrize(("unit", "a_work", "a_rate", "b_work", "b_rate", "t_work"), A16)
def test_a16_cost_report_all_units(ws, unit, a_work, a_rate, b_work, b_rate, t_work):
    t, alice, bob = make_a16(ws)
    calc(ws)
    rep = rm.cost_report(ws, unit)
    assert rep.unit == unit
    row = rep.node(t)
    by_res = {a.resource_id: a for a in row.assignments}
    assert by_res[alice].work_unit == unit
    assert by_res[alice].percent == D(80)
    assert by_res[alice].work_qty == a_work
    assert by_res[alice].rate_per_unit == a_rate
    assert by_res[bob].work_qty == b_work
    assert by_res[bob].rate_per_unit == b_rate
    assert row.work_qty == t_work
    assert (by_res[alice].cost, by_res[bob].cost, row.cost) == (D(3200), D(400), D(3600))
    assert rep.total_cost == D(3600)
    assert rep.complete is True and row.complete is True
    assert rep.missing_rate_resources == ()


def test_cost_report_default_unit_is_project_unit(ws):
    t, _, _ = make_a16(ws)
    calc(ws)
    assert rm.cost_report(ws).unit == WorkUnit.PERSON_DAYS
    ws.set_cost_report_unit("person_hours")
    assert rm.cost_report(ws).unit == WorkUnit.PERSON_HOURS
    with pytest.raises(NotFound):
        rm.cost_report(ws).node("nope")


def test_cost_report_group_rows_and_missing_rate(ws):
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob")
    g = ws.add_group("G")
    t = ws.add_task("T", g, duration="8h")
    ws.set_assignment(t, alice, 100)
    ws.set_assignment(t, bob, 100)
    calc(ws)
    rep = rm.cost_report(ws)
    assert rep.complete is False and rep.missing_rate_resources == (bob,)
    assert rep.node(g).cost == D(800) and rep.node(g).assignments == ()
    assert rep.node(g).work_qty == D(2)
    bob_row = [a for a in rep.node(t).assignments if a.resource_id == bob][0]
    assert bob_row.cost is None and bob_row.rate_per_unit is None


def test_cost_report_without_result_raises_conflict(ws):
    make_a16(ws)
    with pytest.raises(Conflict, match="no schedule calculated"):
        rm.cost_report(ws)


def test_task_details_a16(ws):
    t, alice, bob = make_a16(ws)
    calc(ws)
    det = rm.task_details(ws, t, "person_hours")
    rows = {a.resource_id: a for a in det.assignments}
    a, b = rows[alice], rows[bob]
    assert a.resource_name == "Alice"
    assert (a.percent, a.assignment_days, a.work_qty, a.rate_per_unit, a.cost) == (
        D(80), D(4), D(32), D(100), D(3200),
    )  # fmt: skip
    assert (b.percent, b.assignment_days, b.work_qty, b.rate_per_unit, b.cost) == (
        D(20), D(1), D(8), D(50), D(400),
    )  # fmt: skip
    assert det.name == "Design" and det.kind is NodeKind.TASK
    assert det.sizing == TimeQty(D(40), TimeUnit.HOURS)
    assert det.cost == D(3600) and det.cost_complete is True
    assert det.effort_days == D(5)
    assert det.status == "scheduled"
    assert det.start == datetime(2026, 10, 5, 9, 0)
    assert det.finish == datetime(2026, 10, 9, 17, 0)
    assert det.work_qty == D(40)
    dflt = rm.task_details(ws, t)
    assert dflt.unit == WorkUnit.PERSON_DAYS
    assert {a.resource_id: a for a in dflt.assignments}[alice].work_qty == D(4)


def test_task_details_a21(ws):
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="60")
    t = ws.add_task("Sixteen", duration="16h")
    ws.set_assignment(t, alice, 75)
    ws.set_assignment(t, bob, 25)
    calc(ws)
    det = rm.task_details(ws, t, "person_hours")
    rows = {a.resource_id: a for a in det.assignments}
    assert (rows[alice].work_qty, rows[alice].cost) == (D(12), D(1200))
    assert (rows[bob].work_qty, rows[bob].cost) == (D(4), D(240))
    assert rows[alice].assignment_days == D("1.5")
    assert det.duration_days == D(2) and det.effort_days == D(2)


def test_task_details_dependencies_and_no_result(ws):
    a = ws.add_task("A", duration="1d")
    b = ws.add_task("B", duration="1d")
    ws.add_dependency(a, b, "SS", lag="4h")
    det = rm.task_details(ws, b)
    assert det.start is None and det.cost is None and det.status is None
    (p,) = det.predecessors
    assert (p.pred_id, p.succ_id, p.type) == (a, b, DependencyType.SS)
    assert p.lag == TimeQty(D(4), TimeUnit.HOURS) and p.lag_days == D("0.5")
    assert det.successors == ()
    assert [x.dependency_id for x in rm.task_details(ws, a).successors] == [p.dependency_id]
    with pytest.raises(NotFound):
        rm.task_details(ws, "zzz")


# --------------------------------------------------------------- loading


def test_a03_loading_overlap_segments(ws):
    alice = ws.add_resource("Alice", hourly_rate="100")
    t1 = ws.add_task("T1", duration="5d")
    t2 = ws.add_task("T2", duration="2d")
    ws.set_assignment(t1, alice, 80)
    ws.set_assignment(t2, alice, 50)
    calc(ws)
    view = rm.loading(ws, alice)
    assert view.capacity_percent == D(100) and view.buckets == ()
    over = [s for s in view.segments if s.overloaded]
    assert len(over) == 1
    assert over[0].percent == D(130)
    assert (over[0].start, over[0].end) == (datetime(2026, 10, 5, 9), datetime(2026, 10, 6, 17))
    assert set(over[0].task_ids) == {t1, t2}
    rest = [s for s in view.segments if not s.overloaded]
    assert [(s.start, s.end, s.percent) for s in rest] == [
        (datetime(2026, 10, 7, 9), datetime(2026, 10, 9, 17), D(80))
    ]
    days = {b.period_start: b for b in rm.loading(ws, alice, granularity="day").buckets}
    assert days[date(2026, 10, 5)].assigned_days == D("1.3")
    assert days[date(2026, 10, 7)].assigned_days == D("0.8")


def test_a03_day_buckets_average_vs_peak(ws):
    bob = ws.add_resource("Bob", hourly_rate="50")
    t4 = ws.add_task("T4", duration="1d")
    t5 = ws.add_task("T5", duration="2h")
    ws.set_assignment(t4, bob, 80)
    ws.set_assignment(t5, bob, 50)
    calc(ws)
    mon = {b.period_start: b for b in rm.loading(ws, bob, granularity="day").buckets}[START]
    assert mon.peak_percent == D(130) and mon.overloaded is True
    assert mon.average_percent == D("92.5")
    assert mon.assigned_days == D("0.925")
    wk = rm.loading(ws, bob, granularity="week").buckets
    assert [b.period_start for b in wk] == [START]


def test_loading_holiday_has_no_bucket_and_window_clips(ws):
    alice = ws.add_resource("Alice")
    ws.calendar.add_holiday(date(2026, 10, 7), "Holiday")
    t = ws.add_task("T", duration="5d")
    ws.set_assignment(t, alice, 100)
    calc(ws)
    days = {b.period_start for b in rm.loading(ws, alice, granularity="day").buckets}
    assert date(2026, 10, 7) not in days
    assert days == {date(2026, 10, d) for d in (5, 6, 8, 9, 12)}
    clipped = rm.loading(
        ws,
        alice,
        granularity="day",
        time_window=(datetime(2026, 10, 8), datetime(2026, 10, 9, 23)),
    )
    assert {b.period_start for b in clipped.buckets} == {date(2026, 10, 8), date(2026, 10, 9)}
    assert clipped.segments[0].start == datetime(2026, 10, 8, 9)
    assert clipped.segments[-1].end == datetime(2026, 10, 9, 17)


def test_loading_errors_and_empty(ws):
    alice = ws.add_resource("Alice")
    with pytest.raises(NotFound):
        rm.loading(ws, "nobody")
    assert rm.loading(ws, alice).segments == ()  # no result
    with pytest.raises(ValueError):
        rm.loading(ws, alice, granularity="month")  # type: ignore[arg-type]


# -------------------------------------------------------- wbs / gantt rows


def make_tree(ws: Workspace) -> dict[str, str]:
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="50")
    ids: dict[str, str] = {}
    ids["g1"] = ws.add_group("Phase 1")
    ids["g11"] = ws.add_group("Sub", ids["g1"])
    ids["t1"] = ws.add_task("Deep", ids["g11"], duration="1d")
    ids["t2"] = ws.add_task("Direct", ids["g1"], effort="8h")
    ids["g2"] = ws.add_group("Phase 2")
    ids["t3"] = ws.add_task("Later", ids["g2"], duration="2d")
    ids["m"] = ws.add_milestone("Done")
    ids["tu"] = ws.add_task("Unsized")
    ws.set_assignment(ids["t2"], alice, 80)
    ws.set_assignment(ids["t2"], bob, 20)
    ws.add_dependency(ids["t1"], ids["t3"], "FS")
    ws.add_dependency(ids["t3"], ids["m"], "FS", lag="1d")
    return ids


def test_wbs_rows_collapsed_and_expanded(ws):
    ids = make_tree(ws)
    calc(ws)
    page = rm.wbs_rows(ws)
    assert [r.id for r in page.rows] == [ids["g1"], ids["g2"], ids["m"], ids["tu"]]
    assert page.total == 4
    g1 = page.rows[0]
    assert (g1.depth, g1.wbs_number, g1.has_children, g1.expanded) == (0, "1", True, False)
    assert g1.sizing is None

    page = rm.wbs_rows(ws, expanded_ids={ids["g1"]})
    assert [r.id for r in page.rows] == [
        ids["g1"], ids["g11"], ids["t2"], ids["g2"], ids["m"], ids["tu"],
    ]  # fmt: skip
    assert [r.depth for r in page.rows] == [0, 1, 1, 0, 0, 0]
    assert page.rows[1].expanded is False  # g11 not expanded
    assert page.rows[2].wbs_number == "1.2"

    page = rm.wbs_rows(ws, expanded_ids=frozenset(ids.values()))
    assert [r.wbs_number for r in page.rows] == ["1", "1.1", "1.1.1", "1.2", "2", "2.1", "3", "4"]
    assert page.rows[2].depth == 2 and page.rows[2].sizing == TimeQty(D(1), TimeUnit.DAYS)
    assert page.rows[3].assignments_summary == "Alice 80%, Bob 20%"
    assert page.rows[3].sizing == TimeQty(D(8), TimeUnit.HOURS)
    assert page.rows[3].effort_days == D(1) and page.rows[3].cost == D(720)
    assert page.rows[0].cost == D(720) and page.rows[0].status == "group"


def test_wbs_rows_window_parent_and_unscheduled(ws):
    ids = make_tree(ws)
    calc(ws)
    everything = frozenset(ids.values())
    full = rm.wbs_rows(ws, expanded_ids=everything)
    win = rm.wbs_rows(ws, expanded_ids=everything, offset=2, limit=3)
    assert win.total == full.total == 8
    assert win.rows == full.rows[2:5]
    assert rm.wbs_rows(ws, expanded_ids=everything, offset=20).rows == ()
    sub = rm.wbs_rows(ws, parent_id=ids["g1"])
    assert [r.id for r in sub.rows] == [ids["g11"], ids["t2"]]
    assert sub.rows[0].depth == 1 and sub.total == 2
    assert rm.wbs_rows(ws, parent_id=ids["t1"]).rows == ()
    with pytest.raises(NotFound):
        rm.wbs_rows(ws, parent_id="nope")

    unsized = full.rows[-1]
    assert unsized.id == ids["tu"]
    assert unsized.start is None and unsized.finish is None and unsized.status == "unschedulable"
    assert unsized.sizing is None
    with pytest.raises(ValueError):
        rm.wbs_rows(ws, offset=-1)


def test_gantt_rows_align_with_wbs_rows(ws):
    ids = make_tree(ws)
    calc(ws)
    ex = frozenset({ids["g1"], ids["g11"]})
    wbs = rm.wbs_rows(ws, expanded_ids=ex).rows
    gantt = rm.gantt_rows(ws, expanded_ids=ex)
    assert [g.id for g in gantt] == [w.id for w in wbs]
    assert [g.row_index for g in gantt] == list(range(len(wbs)))
    part = rm.gantt_rows(ws, expanded_ids=ex, offset=3, limit=2)
    assert [g.row_index for g in part] == [3, 4]
    assert [g.id for g in part] == [w.id for w in wbs[3:5]]
    by_id = {g.id: g for g in gantt}
    assert by_id[ids["g1"]].is_summary and not by_id[ids["g1"]].is_milestone
    assert by_id[ids["m"]].is_milestone
    assert by_id[ids["tu"]].start is None
    assert by_id[ids["t1"]].start == datetime(2026, 10, 5, 9)
    assert by_id[ids["t1"]].leveling_delay_days == D(0)
    # time window drops non-overlapping and undated rows
    win = rm.gantt_rows(
        ws,
        expanded_ids=ex,
        time_window=(datetime(2026, 10, 7), datetime(2026, 10, 7, 23)),
    )
    assert ids["tu"] not in {g.id for g in win}
    assert ids["t3"] in {g.id for g in win} or ids["t3"] not in by_id
    assert ids["t1"] not in {g.id for g in win}


def test_dependency_links(ws):
    ids = make_tree(ws)
    calc(ws)
    links = rm.dependency_links(ws, [ids["t1"], ids["t3"], ids["m"]])
    assert {(link.pred_id, link.succ_id, link.type, link.lag_days) for link in links} == {
        (ids["t1"], ids["t3"], DependencyType.FS, D(0)),
        (ids["t3"], ids["m"], DependencyType.FS, D(1)),
    }
    only = rm.dependency_links(ws, [ids["t1"], ids["t3"]])
    assert [(link.pred_id, link.succ_id) for link in only] == [(ids["t1"], ids["t3"])]
    assert rm.dependency_links(ws, []) == []
    # works without a result too
    ws._discard_results()
    assert rm.dependency_links(ws, [ids["t3"], ids["m"]])[0].lag_days == D(1)


def test_no_result_rows_keep_definition(ws):
    ids = make_tree(ws)
    page = rm.wbs_rows(ws, expanded_ids=frozenset(ids.values()))
    assert page.total == 8
    row = page.rows[3]
    assert row.name == "Direct" and row.assignments_summary == "Alice 80%, Bob 20%"
    assert row.start is None and row.cost is None and row.status is None
    assert row.duration_days is None and row.cost_complete is None
    g = rm.gantt_rows(ws, expanded_ids=frozenset(ids.values()))
    assert len(g) == 8 and all(x.start is None for x in g)


def test_cache_invalidation_on_edit_and_result_change(ws):
    ids = make_tree(ws)
    calc(ws)
    before = rm.wbs_rows(ws).rows
    assert before[0].name == "Phase 1"
    ws.rename_node(ids["g1"], "Renamed")
    assert rm.wbs_rows(ws).rows[0].name == "Renamed"
    n = rm.wbs_rows(ws).total
    ws.add_milestone("Extra")
    assert rm.wbs_rows(ws).total == n + 1
    # result identity change
    t1 = rm.wbs_rows(ws, expanded_ids=frozenset(ids.values())).rows[2]
    assert t1.finish == datetime(2026, 10, 5, 17)
    ws.set_sizing(ids["t1"], duration="2d")
    assert rm.wbs_rows(ws, expanded_ids=frozenset(ids.values())).rows[2].sizing.value == D(2)
    calc(ws)
    assert rm.wbs_rows(ws, expanded_ids=frozenset(ids.values())).rows[2].finish == datetime(
        2026, 10, 6, 17
    )
    ws._discard_results()
    assert rm.wbs_rows(ws).rows[0].start is None


# ---------------------------------------------------------- calendar etc.


def test_nonworking_ranges(ws):
    ws.calendar.add_holiday(date(2026, 10, 12), "H")
    assert rm.nonworking_ranges(ws, date(2026, 10, 5), date(2026, 10, 14)) == [
        (date(2026, 10, 10), date(2026, 10, 12))
    ]
    assert rm.nonworking_ranges(ws, date(2026, 10, 3), date(2026, 10, 4)) == [
        (date(2026, 10, 3), date(2026, 10, 4))
    ]


def test_project_summary(ws):
    summary = rm.project_summary(ws)
    assert summary.name == "Demo" and summary.start == START
    assert summary.has_result is False and summary.total_cost is None
    ids = make_tree(ws)
    calc(ws)
    s = rm.project_summary(ws)
    assert (s.groups, s.tasks, s.milestones) == (3, 4, 1)
    assert (s.resources, s.assignments, s.dependencies) == (2, 2, 2)
    assert s.currency == "USD" and s.cost_report_unit == WorkUnit.PERSON_DAYS
    assert s.calendar == ws.calendar.settings()
    assert s.has_result and s.result_kind == "dependency_only"
    assert s.complete is False and s.project_finish is None  # unsized task
    assert s.total_cost == D(720) and s.cost_complete is False
    assert s.stale_dates is False and s.stale_costs is False
    ws.set_sizing(ids["t3"], duration="3d")
    s2 = rm.project_summary(ws)
    assert s2.stale_dates is True and s2.revision > s.revision


# ------------------------------------------------------------------ perf


@pytest.mark.slow
def test_10k_tasks_windowed_queries_fast(ws):
    b = ProjectBuilder(start=START)
    b.resource("r1", "R1", rate="10")
    for g in range(100):
        b.group(f"g{g}", f"G{g}")
        for k in range(99):
            b.task(f"t{g}_{k}", f"T{g}.{k}", parent=f"g{g}", duration="1d")
            if k % 3 == 0:
                b.assign(f"t{g}_{k}", "r1", 50)
    project = b.build()
    assert len(project.nodes) == 10_000
    repo.save_project(ws._connection, ws._project_pk, project)
    ws._reload()
    calc(ws)
    expanded = frozenset(n.id for n in ws.project().nodes if n.kind is NodeKind.GROUP)

    def timed(fn, *args, **kw):
        fn(*args, **kw)  # first call builds the cache
        best = float("inf")
        for _ in range(5):
            t0 = time.perf_counter()
            fn(*args, **kw)
            best = min(best, time.perf_counter() - t0)
        return best * 1000

    wbs_ms = timed(rm.wbs_rows, ws, expanded_ids=expanded, offset=5000, limit=50)
    gantt_ms = timed(rm.gantt_rows, ws, expanded_ids=expanded, offset=5000, limit=50)
    collapsed_ms = timed(rm.wbs_rows, ws, limit=50)
    print(f"\n10k timings ms: wbs={wbs_ms:.2f} gantt={gantt_ms:.2f} collapsed={collapsed_ms:.2f}")
    assert wbs_ms < 50 and gantt_ms < 50 and collapsed_ms < 50
    assert rm.wbs_rows(ws, expanded_ids=expanded, limit=1).total == 10_000
