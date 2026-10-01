"""Property tests for TimeQty conversions (task T10)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from project_planner.engine.model import TimeQty, TimeUnit, days, hours, minutes_to_days

pytestmark = pytest.mark.property

# minutes per day for hours_per_day in (0, 24] at minute precision
minutes_per_day = st.integers(min_value=1, max_value=24 * 60)
decimals = st.decimals(
    min_value=Decimal(-10_000), max_value=Decimal(10_000), allow_nan=False, places=4
)


def _terminating_part(n: int) -> int:
    """Largest divisor of ``n`` of the form 2**a * 5**b."""
    g = 1
    for p in (2, 5):
        while n % p == 0:
            n //= p
            g *= p
    return g


@given(
    mpd=minutes_per_day, k=st.integers(min_value=-36600, max_value=36600)
)  # |x| within the TimeQty bound
def test_days_round_trip_when_whole_minutes(mpd: int, k: int) -> None:
    # Every exact decimal x with x * mpd whole is k / g with g = the 2**a * 5**b part of mpd
    # (its reduced denominator divides mpd and has only factors 2 and 5).
    g = _terminating_part(mpd)
    x = Decimal(k) / Decimal(g)  # exact: terminating decimal
    minutes = k * (mpd // g)
    assert x * mpd == minutes
    assert days(x).to_minutes(mpd) == minutes
    assert minutes_to_days(days(x).to_minutes(mpd), mpd) == x


@given(value=decimals.filter(lambda d: d > 0), mpd=minutes_per_day)
def test_hours_to_minutes_is_ceiling(value: Decimal, mpd: int) -> None:
    m = hours(value).to_minutes(mpd)
    assert 0 <= m - value * 60 < 1


@given(value=decimals, mpd=minutes_per_day)
def test_to_minutes_is_ceiling_for_any_sign(value: Decimal, mpd: int) -> None:
    for q, exact in ((hours(value), value * 60), (days(value), value * mpd)):
        m = q.to_minutes(mpd)
        assert 0 <= m - exact < 1


@given(a=decimals, b=decimals, mpd=minutes_per_day)
def test_to_minutes_monotonic(a: Decimal, b: Decimal, mpd: int) -> None:
    lo, hi = min(a, b), max(a, b)
    assert days(lo).to_minutes(mpd) <= days(hi).to_minutes(mpd)


@given(value=decimals, unit=st.sampled_from(TimeUnit))
def test_parse_str_round_trip(value: Decimal, unit: TimeUnit) -> None:
    q = TimeQty(value, unit)
    back = TimeQty.parse(str(q))
    assert back == q
    assert str(back.value) == str(q.value)  # digits preserved exactly
