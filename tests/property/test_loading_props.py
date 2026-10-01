"""Property tests for engine.loading (T17)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.builders import ProjectBuilder
from hypothesis import given, settings
from hypothesis import strategies as st

from project_planner.engine.calendar import WorkingAxis
from project_planner.engine.loading import aggregate, compute_loading

pytestmark = pytest.mark.property

_assign = st.tuples(
    st.integers(0, 2),  # resource
    st.integers(1, 100),  # percent
    st.integers(0, 3000),  # start
    st.integers(0, 1500),  # length (0 allowed)
)


@settings(max_examples=150, deadline=None)
@given(st.lists(_assign, max_size=12))
def test_loading_invariants(specs):
    b = ProjectBuilder()
    for r in range(3):
        b.resource(f"r{r}")
    intervals = {}
    expected = Decimal(0)
    for i, (r, pct, s, ln) in enumerate(specs):
        b.task(f"t{i}").assign(f"t{i}", f"r{r}", pct)
        intervals[f"t{i}"] = (s, s + ln)
        expected += Decimal(ln) * Decimal(pct) / 100
    p = b.build()
    axis = WorkingAxis(p.calendar, p.start)
    loading = compute_loading(p, intervals)

    total = Decimal(0)
    for rid, segs in loading.items():
        prev_end = -1
        seg_minutes = Decimal(0)
        for s in segs:
            assert s.resource_id == rid
            assert s.percent > 0
            assert s.start < s.end
            assert s.start >= prev_end
            prev_end = s.end
            seg_minutes += Decimal(s.end - s.start) * s.percent / 100
        total += seg_minutes
        for gran in ("day", "week"):
            buckets = aggregate(segs, axis, gran)
            assert sum((x.assigned_person_minutes for x in buckets), Decimal(0)) == seg_minutes
            for x in buckets:
                assert x.peak_percent >= x.average_percent
    assert total == expected
