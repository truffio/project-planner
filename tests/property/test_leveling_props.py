"""Property tests for resource leveling (T18)."""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest
from fixtures.builders import ProjectBuilder
from hypothesis import given, settings
from hypothesis import strategies as st

from project_planner.engine.config import Config
from project_planner.engine.cost import compute_costs
from project_planner.engine.forward_pass import NodeTiming, forward_pass
from project_planner.engine.leveling import LevelingOutcome, level
from project_planner.engine.loading import compute_loading
from project_planner.engine.model import Project
from project_planner.engine.network import topological_order
from project_planner.engine.sizing import compute_sizing

pytestmark = pytest.mark.property

MPD = 480
CFG = Config(max_assignment_percent=D(150))


@st.composite
def projects(draw: Any, allow_over: bool = False) -> Project:
    n = draw(st.integers(1, 12))
    n_res = draw(st.integers(1, 3))
    hi = 150 if allow_over else 100
    b = ProjectBuilder()
    for r in range(n_res):
        b.resource(f"r{r}", rate=str(draw(st.integers(1, 200))))
    for i in range(n):
        kind = draw(st.sampled_from(["task", "task", "task", "task", "milestone"]))
        tid = f"n{i:02d}"
        if kind == "milestone":
            b.milestone(tid, order=draw(st.integers(0, 20)))
            continue
        b.task(tid, duration=f"{draw(st.integers(0, 12)) * 2}h", order=draw(st.integers(0, 20)))
        for r in draw(st.sets(st.integers(0, n_res - 1), max_size=n_res)):
            b.assign(tid, f"r{r}", draw(st.integers(10, hi)))
    for j in range(1, n):
        for i in range(j):
            if draw(st.integers(0, 3)) == 0:
                t = draw(st.sampled_from(["FS", "SS", "FF", "SF"]))
                lag = draw(st.integers(-16, 16))
                b.dep(f"n{i:02d}", f"n{j:02d}", t, lag=f"{lag}h")
    return b.build()


def run(p: Project) -> tuple[dict[str, NodeTiming], LevelingOutcome]:
    sz = compute_sizing(p)
    base = forward_pass(p, sz, minutes_per_day=MPD, order=topological_order(p))
    return base, level(p, sz, base, minutes_per_day=MPD, config=CFG)


def _iv(ts: dict[str, NodeTiming]) -> dict[str, tuple[int, int] | None]:
    return {
        k: (t.start, t.finish) if t.start is not None and t.finish is not None else None
        for k, t in ts.items()
    }


def _dur(ts: dict[str, NodeTiming]) -> dict[str, int | None]:
    return {k: (v[1] - v[0] if v else None) for k, v in _iv(ts).items()}


def _check(p: Project, base: dict[str, NodeTiming], out: LevelingOutcome) -> None:
    lv = out.timings
    assert set(lv) == set(base)
    for nid, t in lv.items():
        b = base[nid]
        assert t.status == b.status
        if b.start is None:
            assert t == b
            continue
        assert t.start is not None and t.finish is not None and b.finish is not None
        assert t.start >= b.start >= 0
        assert t.finish - t.start == b.finish - b.start
    for dep in p.dependencies:
        pt, s = lv[dep.pred_id], lv[dep.succ_id]
        assert pt.start is not None and pt.finish is not None
        assert s.start is not None and s.finish is not None
        lag = dep.lag.to_minutes(MPD)
        ok = {
            "FS": s.start >= pt.finish + lag,
            "SS": s.start >= pt.start + lag,
            "FF": s.finish >= pt.finish + lag,
            "SF": s.finish >= pt.start + lag,
        }[dep.type.value]
        assert ok, dep
    reported = {(u.resource_id, u.start, u.end) for u in out.unresolved}
    for rid, segs in compute_loading(p, _iv(lv)).items():
        for seg in segs:
            if seg.percent > 100:
                assert (rid, seg.start, seg.end) in reported
    assert compute_costs(p, _dur(base)) == compute_costs(p, _dur(lv))
    delayed = {d.task_id: d.minutes for d in out.delays}
    for nid, t in lv.items():
        b = base[nid]
        if b.start is not None and t.start is not None and t.start > b.start:
            assert delayed.pop(nid) == t.start - b.start
    assert delayed == {}


@settings(max_examples=200, deadline=None)
@given(projects())
def test_leveling_invariants_within_capacity(p: Project) -> None:
    base, out = run(p)
    _check(p, base, out)
    assert out.unresolved == ()


@settings(max_examples=200, deadline=None)
@given(projects(allow_over=True))
def test_leveling_invariants_with_over_capacity(p: Project) -> None:
    base, out = run(p)
    _check(p, base, out)
    for u in out.unresolved:
        assert u.percent > 100 and u.reason


@settings(max_examples=50, deadline=None)
@given(projects(allow_over=True))
def test_leveling_deterministic(p: Project) -> None:
    assert run(p) == run(p)
