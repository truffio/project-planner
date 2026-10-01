from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal as D

import pytest
from fixtures.builders import ProjectBuilder
from hypothesis import given
from hypothesis import strategies as st

from project_planner.engine.cost import compute_costs, cost_report
from project_planner.engine.model import Project

pytestmark = pytest.mark.property

assigns = st.lists(st.tuples(st.integers(1, 100), st.integers(0, 500)), min_size=1, max_size=5)


def build(
    items: Sequence[tuple[int, int]],
    task_ids: Sequence[str],
    *,
    reverse: bool = False,
    nested: bool = False,
) -> Project:
    b = ProjectBuilder()
    for i, (_, rate) in enumerate(items):
        b.resource(f"r{i}", rate=rate)
    parent = None
    if nested:
        b.group("g1").group("g2", parent="g1")
        parent = "g2"
    for t in task_ids:
        b.task(t, parent=parent, duration="1d")
        order = list(enumerate(items))
        if reverse:
            order.reverse()
        for i, (pct, _) in order:
            b.assign(t, f"r{i}", pct)
    return b.build()


@given(assigns, st.integers(0, 100000))
def test_task_cost_exact(items: list[tuple[int, int]], d: int) -> None:
    r = compute_costs(build(items, ["t1"]), {"t1": d})
    expected = D(d) / 60 * sum((D(pct) / 100 * D(rate) for pct, rate in items), D(0))
    assert abs(r.tasks["t1"].cost - expected) <= D("1e-20")  # Decimal /60 is not exact


@given(assigns, st.lists(st.integers(0, 5000), min_size=1, max_size=4), st.booleans())
def test_total_is_sum_of_leaves(items: list[tuple[int, int]], ds: list[int], nested: bool) -> None:
    durs = {f"t{i}": d for i, d in enumerate(ds)}
    r = compute_costs(build(items, list(durs), nested=nested), durs)
    assert r.total == sum((t.cost for t in r.tasks.values()), D(0))
    if nested:
        assert r.groups["g1"].cost == r.total == r.groups["g2"].cost


@given(assigns, st.integers(0, 100000))
def test_order_independent(items: list[tuple[int, int]], d: int) -> None:
    a = compute_costs(build(items, ["t1"]), {"t1": d})
    b = compute_costs(build(items, ["t1"], reverse=True), {"t1": d})
    assert a.tasks["t1"].cost == b.tasks["t1"].cost and a.total == b.total


@given(assigns, st.integers(0, 100000))
def test_report_totals_unit_independent(items: list[tuple[int, int]], d: int) -> None:
    p = build(items, ["t1"], nested=True)
    r = compute_costs(p, {"t1": d})
    reps = [cost_report(r, p, u) for u in ("person_hours", "person_days", "person_years")]
    assert all(rep.cost == r.total for rep in reps)
    assert all(rep.tasks["t1"].cost == r.tasks["t1"].cost for rep in reps)
    assert all(rep.groups["g1"].cost == r.groups["g1"].cost for rep in reps)
