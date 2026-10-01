"""Property tests for the forward pass (T15)."""

from __future__ import annotations

import random
from typing import Any

import pytest
from fixtures.builders import ProjectBuilder
from hypothesis import given, settings
from hypothesis import strategies as st

from project_planner.engine.forward_pass import NodeTiming, forward_pass
from project_planner.engine.model import Project
from project_planner.engine.sizing import compute_sizing

MPD = 480

Edge = tuple[int, int, str, str]


@st.composite
def dags(draw: Any) -> tuple[list[int], list[Edge]]:
    n = draw(st.integers(1, 8))
    durs = [draw(st.integers(0, 6)) for _ in range(n)]
    edges: list[Edge] = []
    for j in range(1, n):
        for i in range(j):
            if draw(st.booleans()):
                t = draw(st.sampled_from(["FS", "SS", "FF", "SF"]))
                lag = draw(st.integers(-30, 30))
                unit = draw(st.sampled_from(["h", "d"]))
                edges.append((i, j, t, f"{lag}{unit}"))
    return durs, edges


def build(durs: list[int], edges: list[Edge], shuffle_seed: int | None = None) -> Project:
    b = ProjectBuilder()
    for i, d in enumerate(durs):
        b.task(f"t{i}", duration=f"{d * 4}h")
    es = list(edges)
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(es)
    for i, j, t, lag in es:
        b.dep(f"t{i}", f"t{j}", t, lag=lag)
    return b.build()


def run(p: Project) -> dict[str, NodeTiming]:
    return forward_pass(p, compute_sizing(p), minutes_per_day=MPD)


@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(dags(), st.integers(0, 1000))
def test_constraints_tight_and_order_independent(
    g: tuple[list[int], list[Edge]], seed: int
) -> None:
    durs, edges = g
    p = build(durs, edges)
    r = run(p)
    for nid, t in r.items():
        assert t.status == "scheduled" and t.start is not None and t.finish is not None
        assert t.start >= 0
        d = t.finish - t.start
        bounds = [0]
        for dep in p.dependencies_to(nid):
            ps, pf = r[dep.pred_id].start, r[dep.pred_id].finish
            assert ps is not None and pf is not None
            lag = dep.lag.to_minutes(MPD)
            c = {"FS": pf + lag, "SS": ps + lag, "FF": pf + lag - d, "SF": ps + lag - d}[
                dep.type.value
            ]
            assert t.start >= c
            bounds.append(c)
        assert t.start == max(bounds)
    assert run(build(durs, edges, seed)) == r
