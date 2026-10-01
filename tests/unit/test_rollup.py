"""Unit tests for engine.rollup."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.rollup import rollup, wbs_numbers

pytestmark = pytest.mark.unit

D = Decimal


def test_nested_groups_three_levels() -> None:
    p = (
        ProjectBuilder()
        .group("g1")
        .group("g2", parent="g1")
        .group("g3", parent="g2")
        .task("t1", parent="g3", duration="1d")
        .task("t2", parent="g2", duration="1d")
        .task("t3", parent="g1", duration="1d")
        .build()
    )
    iv = {"t1": (10, 20), "t2": (5, 15), "t3": (30, 40)}
    ef = {"t1": D(100), "t2": D(50), "t3": D(25)}
    r = rollup(p, iv, ef)
    assert (r["g3"].start, r["g3"].finish, r["g3"].effort_person_minutes) == (10, 20, 100)
    assert (r["g2"].start, r["g2"].finish, r["g2"].effort_person_minutes) == (5, 20, 150)
    assert (r["g1"].start, r["g1"].finish, r["g1"].effort_person_minutes) == (5, 40, 175)
    assert all(s.complete and not s.unscheduled_descendants for s in r.values())


def test_overlapping_children_span_not_sum() -> None:
    p = ProjectBuilder().group("g").task("a", parent="g").task("b", parent="g").build()
    r = rollup(p, {"a": (0, 100), "b": (50, 120)}, {"a": D(1), "b": D(1)})
    assert (r["g"].start, r["g"].finish) == (0, 120)


def test_milestone_only_group() -> None:
    p = ProjectBuilder().group("g").milestone("m", parent="g").build()
    r = rollup(p, {"m": (30, 30)}, {})
    assert r["g"].start == 30 and r["g"].finish == 30
    assert r["g"].effort_person_minutes == 0
    assert r["g"].complete


def test_empty_group() -> None:
    p = ProjectBuilder().group("g").build()
    s = rollup(p, {}, {})["g"]
    assert (s.start, s.finish, s.effort_person_minutes, s.complete) == (None, None, 0, True)
    assert s.unscheduled_descendants == ()


def test_unscheduled_child_incomplete_but_partial_dates_shown() -> None:
    p = (
        ProjectBuilder()
        .group("g")
        .group("h", parent="g")
        .task("a", parent="g", order=0)
        .task("b", parent="h")
        .build()
    )
    r = rollup(p, {"a": (5, 9), "b": None}, {"a": D(10), "b": None})
    assert (r["g"].start, r["g"].finish) == (5, 9)
    assert r["g"].effort_person_minutes == 10
    assert not r["g"].complete
    assert r["g"].unscheduled_descendants == ("b",)
    assert not r["h"].complete and r["h"].unscheduled_descendants == ("b",)


def test_only_unscheduled_descendants() -> None:
    p = ProjectBuilder().group("g").task("a", parent="g").milestone("m", parent="g").build()
    s = rollup(p, {"a": None}, {})["g"]
    assert s.start is None and s.finish is None and not s.complete
    assert set(s.unscheduled_descendants) == {"a", "m"}


def test_unknown_effort_makes_incomplete() -> None:
    p = ProjectBuilder().group("g").task("a", parent="g").build()
    s = rollup(p, {"a": (0, 5)}, {"a": None})["g"]
    assert not s.complete and s.unscheduled_descendants == ()
    assert (s.start, s.finish) == (0, 5)


def test_only_groups_in_result() -> None:
    p = ProjectBuilder().group("g").task("a", parent="g").build()
    assert set(rollup(p, {"a": (0, 1)}, {"a": D(1)})) == {"g"}


def test_wbs_numbers_reordered_siblings() -> None:
    p = (
        ProjectBuilder()
        .group("g1", order=2)
        .group("g2", order=1)
        .task("a", parent="g1", order=5)
        .task("b", parent="g1", order=3)
        .task("c", parent="g2")
        .task("top", order=0)
        .build()
    )
    assert wbs_numbers(p) == {
        "top": "1",
        "g2": "2",
        "c": "2.1",
        "g1": "3",
        "b": "3.1",
        "a": "3.2",
    }


def test_deep_chain_50k_no_recursion() -> None:
    n = 50_000
    b = ProjectBuilder()
    for i in range(n):
        b.group(f"g{i}", parent=f"g{i - 1}" if i else None)
    b.task("leaf", parent=f"g{n - 1}")
    p = b.build()
    r = rollup(p, {"leaf": (3, 8)}, {"leaf": D(7)})
    assert len(r) == n
    assert (r["g0"].start, r["g0"].finish) == (3, 8)
    assert r["g0"].effort_person_minutes == 7
    assert wbs_numbers(p)[f"g{n - 1}"] == ".".join(["1"] * n)
