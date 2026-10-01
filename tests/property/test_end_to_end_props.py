"""Cross-module property tests (T41): random projects through the public API."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest
from fixtures.generators import projects
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import project_planner as pp
from project_planner.engine import csv_io
from project_planner.engine.model import NodeKind, Project
from project_planner.engine.schedule import schedule as engine_schedule

pytestmark = pytest.mark.property

_SETTINGS = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
_FILE_SETTINGS = settings(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    max_examples=min(settings().max_examples, 60),
)


def _load(p: Project, ws: pp.Workspace | None = None) -> pp.Workspace:
    ws = ws or pp.open_workspace(":memory:")
    ws.import_csv(csv_io.export(p), discard_unsaved=True)
    return ws


def _leaf_tasks(p: Project) -> list[str]:
    return [n.id for n in p.nodes if n.kind is NodeKind.TASK]


def _descendant_leaves(p: Project, group_id: str) -> list[str]:
    out: list[str] = []
    stack = [group_id]
    while stack:
        for c in p.children(stack.pop()):
            if c.kind is NodeKind.GROUP:
                stack.append(c.id)
            elif c.kind is NodeKind.TASK:
                out.append(c.id)
    return out


def _check_dependencies(p: Project, res: Any) -> None:
    for dep in p.dependencies:
        a, s = res.node(dep.pred_id), res.node(dep.succ_id)
        if a.start_minutes is None or s.start_minutes is None:
            continue
        assert a.finish_minutes is not None and s.finish_minutes is not None
        lag = res.dependency(dep.id).lag_minutes
        ok = {
            "FS": s.start_minutes >= a.finish_minutes + lag,
            "SS": s.start_minutes >= a.start_minutes + lag,
            "FF": s.finish_minutes >= a.finish_minutes + lag,
            "SF": s.finish_minutes >= a.start_minutes + lag,
        }[dep.type.value]
        assert ok, (dep, a, s, lag)


def _signature(res: Any) -> dict[str, Any]:
    """Comparable summary of a result: dates, costs, delays, kind, fingerprints."""
    return {
        "kind": res.kind,
        "complete": res.complete,
        "finish": res.project_finish,
        "total_cost": res.total_cost,
        "schedule_fp": res.schedule_fp,
        "cost_fp": res.cost_fp,
        "nodes": {
            nid: (
                n.status,
                n.start,
                n.finish,
                n.duration_minutes,
                n.cost,
                n.cost_complete,
                n.leveling_delay_minutes,
            )
            for nid, n in res.nodes.items()
        },
    }


def _dates_and_costs(res: Any) -> dict[str, Any]:
    return {
        "total": res.total_cost,
        "finish": res.project_finish,
        "nodes": {nid: (n.start, n.finish, n.cost) for nid, n in res.nodes.items()},
    }


@_SETTINGS
@given(projects())
def test_dependencies_hold_before_and_after_leveling(p: Project) -> None:
    ws = _load(p)
    base = ws.schedule()
    _check_dependencies(ws.project(), base)
    ws.level_preview()
    leveled = ws.apply_leveling()
    assert leveled.kind == "leveled"
    _check_dependencies(ws.project(), leveled)
    ws.close()


@_SETTINGS
@given(projects())
def test_leveling_preserves_cost_and_durations(p: Project) -> None:
    ws = _load(p)
    base = ws.schedule()
    ws.level_preview()
    leveled = ws.apply_leveling()
    assert leveled.total_cost == base.total_cost
    assert leveled.cost_complete == base.cost_complete
    assert leveled.complete == base.complete
    assert set(leveled.nodes) == set(base.nodes)
    for nid, n in base.nodes.items():
        m = leveled.nodes[nid]
        if n.kind is not NodeKind.GROUP:  # group spans move with their children
            assert m.duration_minutes == n.duration_minutes, nid
        assert m.cost == n.cost, nid
        assert m.status == n.status, nid
        if n.start_minutes is not None:
            assert m.start_minutes is not None and m.start_minutes >= n.start_minutes
    ws.close()


@_SETTINGS
@given(projects())
def test_group_sums_equal_leaf_sums(p: Project) -> None:
    ws = _load(p)
    res = ws.schedule()
    proj = ws.project()
    leaf_cost = {t: res.node(t).cost for t in _leaf_tasks(proj)}
    assert res.total_cost == sum(leaf_cost.values(), Decimal(0))
    for n in proj.nodes:
        if n.kind is not NodeKind.GROUP:
            continue
        leaves = _descendant_leaves(proj, n.id)
        row = res.node(n.id)
        assert row.cost == sum((leaf_cost[t] for t in leaves), Decimal(0)), n.id
        # Group effort counts scheduled descendants only (blocked ones are excluded).
        efforts = [
            res.node(t).effort_person_minutes for t in leaves if res.node(t).status == "scheduled"
        ]
        if all(e is not None for e in efforts):
            assert row.effort_person_minutes == sum(
                (e for e in efforts if e is not None), Decimal(0)
            ), n.id
    ws.close()


@_SETTINGS
@given(projects())
def test_csv_round_trip_gives_same_schedule(p: Project) -> None:
    ws = _load(p)
    first = ws.schedule()
    ws.level_preview()
    ws.apply_leveling()
    text = ws.export_csv()
    ws.schedule()  # back to dependency-only so the comparison is like for like
    ws2 = pp.open_workspace(":memory:")
    ws2.import_csv(text, discard_unsaved=True)
    again = ws2.schedule()
    assert _dates_and_costs(again) == _dates_and_costs(first)
    assert (again.schedule_fp, again.cost_fp) == (first.schedule_fp, first.cost_fp)
    ws.close()
    ws2.close()


@_FILE_SETTINGS
@given(projects(), st.booleans())
def test_save_reopen_load_is_identical(
    tmp_path_factory: pytest.TempPathFactory, p: Project, level: bool
) -> None:
    path = tmp_path_factory.mktemp("db") / "ws.sqlite"
    ws = _load(p, pp.open_workspace(path))
    ws.schedule()
    if level:
        ws.level_preview()
        ws.apply_leveling()
    expected = ws.result()
    assert expected is not None
    info = ws.save_as("saved")
    ws.close()

    with pp.open_workspace(path) as ws2:
        listed = ws2.list_projects()
        assert [i.name for i in listed] == ["saved"]
        ws2.load(info.id, discard_unsaved=True)
        got = ws2.result()
        assert got is not None
        assert _signature(got) == _signature(expected)
        assert got.kind == ("leveled" if level else "dependency_only")
        assert ws2.project() == ws2.project()
        assert not ws2.state().stale_dates and not ws2.state().stale_costs


@_SETTINGS
@given(projects(), st.randoms(use_true_random=False))
def test_fingerprints_ignore_insertion_order(p: Project, rnd: Any) -> None:
    def shuffled(items: tuple[Any, ...]) -> tuple[Any, ...]:
        out = list(items)
        rnd.shuffle(out)
        return tuple(out)

    q = dataclasses.replace(
        p,
        nodes=shuffled(p.nodes),
        resources=shuffled(p.resources),
        assignments=shuffled(p.assignments),
        dependencies=shuffled(p.dependencies),
    )
    a, b = engine_schedule(p), engine_schedule(q)
    assert (a.schedule_fp, a.cost_fp) == (b.schedule_fp, b.cost_fp)
    assert _dates_and_costs(a) == _dates_and_costs(b)

    ws = _load(p)
    assert (ws.schedule().schedule_fp, ws.schedule().cost_fp) == (a.schedule_fp, a.cost_fp)
    ws.close()


@_SETTINGS
@given(projects())
def test_loading_is_never_capped(p: Project) -> None:
    ws = _load(p)
    res = ws.schedule()
    proj = ws.project()
    for stage in (res, _leveled(ws)):
        for rid, segs in stage.loading.items():
            for seg in segs:
                total = sum((proj.assignment(t, rid).percent for t in seg.task_ids), Decimal(0))
                assert seg.percent == total, (rid, seg)
                assert seg.overloaded == (total > 100)
    ws.close()


def _leveled(ws: pp.Workspace) -> Any:
    ws.level_preview()
    return ws.apply_leveling()


def test_overlapping_assignments_sum_beyond_100() -> None:
    from fixtures.builders import ProjectBuilder

    p = (
        ProjectBuilder()
        .resource("r1", rate=10)
        .task("a", duration="1d")
        .task("b", duration="1d")
        .task("c", duration="1d")
        .assign("a", "r1", 100)
        .assign("b", "r1", 70)
        .assign("c", "r1", 60)
        .build()
    )
    ws = _load(p)
    res = ws.schedule()
    (seg,) = res.loading["r1"]
    assert seg.percent == 230 and seg.overloaded
    view = ws.loading("r1")
    assert [s.percent for s in view.segments] == [Decimal(230)]
    ws.close()


@_SETTINGS
@given(projects())
def test_unschedulable_propagation(p: Project) -> None:
    ws = _load(p)
    res = ws.schedule()
    proj = ws.project()
    if res.complete:
        for n in res.nodes.values():
            assert n.status != "blocked" and n.status != "unschedulable"
        ws.close()
        return
    assert res.project_finish is None
    assert res.working_span_days is None
    bad = {nid for nid, n in res.nodes.items() if n.status in ("unschedulable", "blocked")}
    assert bad
    roots = {nid for nid in bad if res.node(nid).status == "unschedulable"}
    assert roots, "incomplete result without an unschedulable root"

    def ancestors(nid: str) -> set[str]:
        seen: set[str] = set()
        stack = [nid]
        while stack:
            for d in proj.dependencies_to(stack.pop()):
                if d.pred_id not in seen:
                    seen.add(d.pred_id)
                    stack.append(d.pred_id)
        return seen

    for nid in bad:
        n = res.node(nid)
        assert n.reason
        assert not n.scheduled
        if n.status == "blocked":
            assert ancestors(nid) & roots, nid
    # Every task with an unschedulable predecessor chain is blocked, nothing else is.
    for wn in proj.nodes:
        if wn.kind is NodeKind.GROUP:
            continue
        if res.node(wn.id).status == "scheduled":
            assert not (ancestors(wn.id) & bad), wn.id
    ws.close()


def test_generator_covers_interesting_cases() -> None:
    """Sanity: the strategy produces complete and incomplete projects and overloads."""
    seen: set[str] = set()

    @settings(max_examples=150, deadline=None, database=None)
    @given(projects())
    def probe(p: Project) -> None:
        res = engine_schedule(p)
        seen.add("complete" if res.complete else "incomplete")
        if any(s.overloaded for segs in res.loading.values() for s in segs):
            seen.add("overload")
        if p.dependencies:
            seen.add("deps")

    probe()
    assert seen == {"complete", "incomplete", "overload", "deps"}
