"""Property tests for engine.rollup."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.builders import ProjectBuilder
from hypothesis import given, settings
from hypothesis import strategies as st

from project_planner.engine.rollup import rollup, wbs_numbers

pytestmark = pytest.mark.property


@st.composite
def trees(draw: st.DrawFn):  # type: ignore[no-untyped-def]
    n = draw(st.integers(min_value=1, max_value=40))
    # kind[i] in group/task/milestone; leaves (non-groups) cannot have children.
    kinds: list[str] = []
    parents: list[int | None] = []
    for i in range(n):
        groups = [j for j in range(i) if kinds[j] == "g"]
        parent = draw(st.sampled_from([None, *groups]))
        parents.append(parent)
        kinds.append(draw(st.sampled_from(["g", "t", "m"])))
    orders = draw(st.lists(st.integers(0, 5), min_size=n, max_size=n))
    ivs: list[tuple[int, int] | None] = []
    efs: list[int | None] = []
    for _ in range(n):
        if draw(st.booleans()):
            s = draw(st.integers(0, 1000))
            ivs.append((s, s + draw(st.integers(0, 500))))
        else:
            ivs.append(None)
        efs.append(draw(st.integers(0, 1000)))
    return kinds, parents, orders, ivs, efs


def _build(kinds, parents, orders):  # type: ignore[no-untyped-def]
    b = ProjectBuilder()
    for i, (k, par, o) in enumerate(zip(kinds, parents, orders, strict=True)):
        pid = None if par is None else f"n{par}"
        if k == "g":
            b.group(f"n{i}", parent=pid, order=o)
        elif k == "t":
            b.task(f"n{i}", parent=pid, order=o)
        else:
            b.milestone(f"n{i}", parent=pid, order=o)
    return b.build()


@settings(max_examples=100, deadline=None)
@given(trees())
def test_group_aggregates_match_descendants(data) -> None:  # type: ignore[no-untyped-def]
    kinds, parents, orders, ivs, efs = data
    p = _build(kinds, parents, orders)
    intervals = {f"n{i}": ivs[i] for i in range(len(kinds)) if kinds[i] != "g"}
    effort = {f"n{i}": Decimal(efs[i] or 0) for i in range(len(kinds)) if kinds[i] == "t"}
    res = rollup(p, intervals, effort)

    def descendants(g: int) -> list[int]:
        out: list[int] = []
        stack = [g]
        while stack:
            x = stack.pop()
            for j, par in enumerate(parents):
                if par == x:
                    out.append(j)
                    stack.append(j)
        return out

    for i, k in enumerate(kinds):
        if k != "g":
            assert f"n{i}" not in res
            continue
        d = [j for j in descendants(i) if kinds[j] != "g"]
        sched = [j for j in d if ivs[j] is not None]
        s = res[f"n{i}"]
        if sched:
            assert s.start == min(ivs[j][0] for j in sched)  # type: ignore[index]
            assert s.finish == max(ivs[j][1] for j in sched)  # type: ignore[index]
        else:
            assert s.start is None and s.finish is None
        assert s.effort_person_minutes == sum(
            (Decimal(efs[j] or 0) for j in sched if kinds[j] == "t"), Decimal(0)
        )
        missing = {f"n{j}" for j in d if ivs[j] is None}
        assert set(s.unscheduled_descendants) == missing
        assert s.complete == (not missing)


@settings(max_examples=100, deadline=None)
@given(trees())
def test_numbering_bijection_and_prefix(data) -> None:  # type: ignore[no-untyped-def]
    kinds, parents, orders, _, _ = data
    p = _build(kinds, parents, orders)
    nums = wbs_numbers(p)
    assert set(nums) == {f"n{i}" for i in range(len(kinds))}
    assert len(set(nums.values())) == len(nums)
    for i, par in enumerate(parents):
        num = nums[f"n{i}"]
        if par is None:
            assert "." not in num
        else:
            assert num.rpartition(".")[0] == nums[f"n{par}"]
