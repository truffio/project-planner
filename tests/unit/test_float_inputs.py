"""D14: floats in the public pp.hours()/pp.days(); the engine-level ones stay strict."""

from __future__ import annotations

from decimal import Decimal

import pytest

import project_planner as pp
from project_planner.engine import model

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("x", [-0.5, 1.25, 2.0, 0.0, 40.0, 0.0001, 12345.678901, -3.5])
def test_short_float_literals_are_accepted_exactly(x: float) -> None:
    assert pp.days(x).value == Decimal(repr(x))
    assert pp.hours(x).value == Decimal(repr(x))


@pytest.mark.parametrize(
    "x",
    [
        0.1 + 0.2,
        1 / 3,
        0.0000001,
        1e-7,
        0.000001,
        1e20,
        1e16,
        2**0.5,
        float("nan"),
        float("inf"),
        -float("inf"),
    ],
)
def test_noisy_or_exponent_floats_are_rejected_with_a_string_hint(x: float) -> None:
    for fn in (pp.days, pp.hours):
        with pytest.raises(TypeError, match="string"):
            fn(x)


def test_documented_example() -> None:
    with pytest.raises(TypeError):
        pp.days(0.1 + 0.2)
    assert str(pp.days(-0.5)) == "-0.5d"


def test_other_inputs_unchanged() -> None:
    assert pp.days(2).value == 2 and pp.hours("1.5").value == Decimal("1.5")
    assert pp.hours(Decimal("0.25")).value == Decimal("0.25")
    with pytest.raises(TypeError):
        pp.days(True)


def test_engine_level_constructors_still_reject_every_float() -> None:
    for fn in (model.days, model.hours):
        for x in (0.5, 1.0, 0.1 + 0.2):
            with pytest.raises(TypeError):
                fn(x)  # type: ignore[arg-type]
