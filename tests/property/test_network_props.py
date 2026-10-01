"""Property tests for engine.network / engine.validation cycle detection (task T12)."""

from __future__ import annotations

import pytest
from fixtures.builders import ProjectBuilder
from hypothesis import given
from hypothesis import strategies as st

from project_planner.engine.model import Project
from project_planner.engine.network import DependencyGraph, topological_order
from project_planner.engine.validation import validate

pytestmark = pytest.mark.property


def _project(n: int, edges: set[tuple[int, int]]) -> Project:
    b = ProjectBuilder()
    for i in range(n):
        b.task(f"n{i}", duration="1d")
    for a, c in sorted(edges):
        b.dep(f"n{a}", f"n{c}")
    return b.build()


@st.composite
def dags(draw: st.DrawFn) -> tuple[int, set[tuple[int, int]]]:
    n = draw(st.integers(min_value=1, max_value=12))
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    edges = set(draw(st.lists(st.sampled_from(pairs), unique=True))) if pairs else set()
    return n, edges


@given(dags())
def test_random_dag_has_no_cycle_and_order_respects_edges(
    dag: tuple[int, set[tuple[int, int]]],
) -> None:
    n, edges = dag
    p = _project(n, edges)
    assert not [i for i in validate(p) if i.code == "DEP_CYCLE"]
    order = topological_order(p)
    pos = {name: k for k, name in enumerate(order)}
    assert sorted(order) == sorted(f"n{i}" for i in range(n))
    for a, c in edges:
        assert pos[f"n{a}"] < pos[f"n{c}"]


@given(dags(), st.data())
def test_back_edge_along_a_path_yields_exactly_one_cycle(
    dag: tuple[int, set[tuple[int, int]]], data: st.DataObject
) -> None:
    n, edges = dag
    n = max(n, 2)
    path = sorted(data.draw(st.sets(st.integers(0, n - 1), min_size=2, max_size=n)))
    edges = set(edges)
    for a, c in zip(path, path[1:], strict=False):
        edges.add((a, c))
    edges.add((path[-1], path[0]))  # back edge closing the path
    p = _project(n, edges)
    cycles = [i for i in validate(p) if i.code == "DEP_CYCLE"]
    assert len(cycles) == 1
    for node in path:
        assert f"n{node}" in cycles[0].message.replace(",", " ").split()
    assert len(DependencyGraph(p).cycles()) == 1
