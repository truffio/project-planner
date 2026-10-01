"""Unit tests for engine.network (task T12)."""

from __future__ import annotations

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.errors import ValidationFailed
from project_planner.engine.network import (
    DependencyGraph,
    DirectedGraph,
    expand_group_endpoint,
    topological_order,
)

pytestmark = pytest.mark.unit


def _tasks(*ids: str) -> ProjectBuilder:
    b = ProjectBuilder()
    for i in ids:
        b.task(i, duration="1d")
    return b


def test_scc_and_cycles_three_node_cycle() -> None:
    p = _tasks("a", "b", "c", "d").dep("a", "b").dep("b", "c").dep("c", "a").dep("c", "d").build()
    g = DependencyGraph(p)
    assert g.cycles() == [("a", "b", "c")]
    assert sorted(g.strongly_connected_components()) == [("a", "b", "c"), ("d",)]


def test_two_disjoint_cycles_and_self_loop() -> None:
    p = (
        _tasks("a", "b", "c", "d", "e")
        .dep("a", "b")
        .dep("b", "a")
        .dep("c", "d")
        .dep("d", "c")
        .dep("e", "e")
        .build()
    )
    assert DependencyGraph(p).cycles() == [("a", "b"), ("c", "d"), ("e",)]


def test_acyclic_has_no_cycles() -> None:
    p = _tasks("a", "b").dep("a", "b").build()
    assert DependencyGraph(p).cycles() == []


def test_directed_graph_successors_and_nodes() -> None:
    g = DirectedGraph(["x"], [("a", "b"), ("a", "b")])
    assert g.nodes == ("a", "b", "x")
    assert g.successors("a") == ("b",)
    assert g.successors("zzz") == ()


def test_topological_order_basic() -> None:
    p = _tasks("a", "b", "c").dep("c", "b").dep("b", "a").build()
    assert topological_order(p) == ["c", "b", "a"]


def test_topological_tie_break_follows_wbs_order_then_id() -> None:
    b = ProjectBuilder()
    b.group("g1")
    b.task("z", parent="g1", duration="1d", order=0)
    b.task("a", parent="g1", duration="1d", order=1)
    b.milestone("m")  # top level, after g1 in display order
    assert topological_order(b.build()) == ["z", "a", "m"]
    # a dependency overrides display order
    b.dep("a", "z")
    assert topological_order(b.build()) == ["a", "z", "m"]


def test_topological_order_excludes_groups_and_ignores_group_edges() -> None:
    b = ProjectBuilder().group("g1").task("t1", parent="g1", duration="1d")
    b.task("t2", duration="1d").dep("g1", "t2")
    assert topological_order(b.build()) == ["t1", "t2"]


def test_topological_order_raises_dep_cycle() -> None:
    p = _tasks("a", "b", "c").dep("a", "b").dep("b", "c").dep("c", "a").build()
    with pytest.raises(ValidationFailed) as exc:
        topological_order(p)
    (issue,) = exc.value.issues
    assert issue.code == "DEP_CYCLE"
    assert issue.object_id == "a"
    assert all(m in issue.message for m in "abc")


def test_expand_group_endpoint_is_a_stub() -> None:
    with pytest.raises(NotImplementedError):
        expand_group_endpoint(_tasks("a").build(), "g")


def test_50k_chain_no_recursion_error() -> None:
    n = 50_000
    b = ProjectBuilder()
    for i in range(n):
        b.task(f"n{i}", duration="1d")
    for i in range(n - 1):
        b.dep(f"n{i}", f"n{i + 1}")
    order = topological_order(b.build())
    assert order == [f"n{i}" for i in range(n)]
    # close the chain: one huge cycle, still no recursion
    b.dep(f"n{n - 1}", "n0")
    cycles = DependencyGraph(b.build()).cycles()
    assert len(cycles) == 1
    assert len(cycles[0]) == n
