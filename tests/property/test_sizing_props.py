"""Property tests for engine.sizing (T13)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.builders import ProjectBuilder
from hypothesis import given
from hypothesis import strategies as st

from project_planner.engine.sizing import compute_sizing

pytestmark = pytest.mark.property

percents = st.decimals(min_value=Decimal("0.1"), max_value=Decimal(200), places=1)
percent_lists = st.lists(percents, min_size=1, max_size=5)
qty = st.tuples(st.integers(0, 10_000), st.sampled_from(["h", "d"])).map(lambda t: f"{t[0]}{t[1]}")
hpd = st.sampled_from([6, 8, "7.5", "7.25"])


def _build(mode: str, size: str, pcts: list[Decimal], hours_per_day: object) -> ProjectBuilder:
    b = ProjectBuilder().calendar(hours_per_day=hours_per_day)
    b.task("t1", **{mode: size})
    for i, p in enumerate(pcts):
        b.resource(f"r{i}").assign("t1", f"r{i}", p)
    return b


@given(qty, percent_lists, hpd)
def test_effort_mode_covers_effort_tightly(size: str, pcts: list[Decimal], h: object) -> None:
    s = compute_sizing(_build("effort", size, pcts, h).build())["t1"]
    assert s.duration_minutes is not None
    assert s.effort_person_minutes is not None
    covered = s.duration_minutes * s.capacity
    assert covered >= s.effort_person_minutes
    assert covered - s.effort_person_minutes < s.capacity


@given(qty, percent_lists, hpd)
def test_duration_mode_independent_of_assignments(
    size: str, pcts: list[Decimal], h: object
) -> None:
    with_a = compute_sizing(_build("duration", size, pcts, h).build())["t1"]
    without = compute_sizing(_build("duration", size, [], h).build())["t1"]
    assert with_a.duration_minutes == without.duration_minutes
    assert with_a.effort_person_minutes == Decimal(with_a.duration_minutes or 0) * with_a.capacity


@given(qty, percent_lists, hpd, st.sampled_from(["effort", "duration"]))
def test_assignment_order_irrelevant(size: str, pcts: list[Decimal], h: object, mode: str) -> None:
    a = compute_sizing(_build(mode, size, pcts, h).build())["t1"]
    b = compute_sizing(_build(mode, size, list(reversed(pcts)), h).build())["t1"]
    assert a == b
