"""Application-level limits and documented per-project defaults (plan section 2).

Two kinds of settings live here:

* **Application limits** (not project data): :class:`Config` and its module
  constants (``MAX_ASSIGNMENT_PERCENT``, ``DAYS_DISPLAY_DECIMALS``...). Engine
  entry points take a ``Config``; use :data:`DEFAULT_CONFIG` unless a test needs
  another limit.
* **Per-project defaults**: values a new project / calendar gets when the user
  does not say otherwise (``DEFAULT_CURRENCY``, calendar defaults of plan 2.3).
  The values themselves are stored per project in ``engine.model``; these
  constants only define the defaults.

This module is a leaf: it imports nothing from the package, so ``engine.model``
can depend on it. ``DEFAULT_COST_REPORT_UNIT`` and ``DEFAULT_WORKING_WEEKDAYS``
are therefore plain strings; ``engine.model`` turns them into enum members.
"""

from __future__ import annotations

import datetime as dt
import functools
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import ParamSpec, TypeVar

__all__ = [
    "DAYS_DISPLAY_DECIMALS",
    "DAYS_ROUNDING",
    "DEFAULT_CONFIG",
    "DEFAULT_COST_REPORT_UNIT",
    "DEFAULT_CURRENCY",
    "DEFAULT_HOURS_PER_DAY",
    "DEFAULT_WORKDAY_START",
    "DEFAULT_WORKING_DAYS_PER_YEAR",
    "DEFAULT_WORKING_WEEKDAYS",
    "MAX_ASSIGNMENT_PERCENT",
    "MONEY_DISPLAY_DECIMALS",
    "MONEY_ROUNDING",
    "ENGINE_CONTEXT",
    "Config",
    "engine_context",
]

_P = ParamSpec("_P")
_R = TypeVar("_R")

ENGINE_CONTEXT: Context = Context(prec=28, rounding=ROUND_HALF_EVEN)
"""The ``decimal`` context all engine arithmetic runs in (independent of the caller's).

28 significant digits equals the Python default, so results are unchanged by the isolation.
"""


def engine_context(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run ``func`` inside ``localcontext(ENGINE_CONTEXT)`` (finding 4).

    Results then never depend on the caller's (or a worker process's) global
    ``decimal`` context. Nesting is harmless.
    """

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with localcontext(ENGINE_CONTEXT):
            return func(*args, **kwargs)

    return wrapper


# --- application limits (plan section 2, decisions 4, 7, 9) ---------------------

MAX_ASSIGNMENT_PERCENT: Decimal = Decimal(100)
"""Default upper bound (inclusive) of one assignment's percent. Lower bound: > 0."""

DAYS_DISPLAY_DECIMALS: int = 2
"""Decimals used when displaying / exporting day values (never inside calculations)."""

DAYS_ROUNDING: str = ROUND_HALF_EVEN
"""``decimal`` rounding mode for displayed day values."""

MONEY_DISPLAY_DECIMALS: int = 2
"""Decimals used when displaying / exporting money amounts."""

MONEY_ROUNDING: str = ROUND_HALF_EVEN
"""``decimal`` rounding mode for displayed money amounts (banker's rounding)."""

# --- per-project defaults (plan 2.2, 2.3; decisions D9-D11) ------------------------

DEFAULT_CURRENCY: str = "USD"
"""Currency code of a new project."""

DEFAULT_COST_REPORT_UNIT: str = "person_days"
"""``WorkUnit`` value used by cost reports of a new project."""

DEFAULT_HOURS_PER_DAY: Decimal = Decimal(8)
"""Working hours per working day."""

DEFAULT_WORKING_DAYS_PER_YEAR: int = 220
"""Working days per year (person-year cost reporting only)."""

DEFAULT_WORKING_WEEKDAYS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri")
"""``Weekday`` values of the default working week."""

DEFAULT_WORKDAY_START: dt.time = dt.time(9, 0)
"""Clock time at which each working day starts."""


@dataclass(frozen=True, slots=True)
class Config:
    """Application-level limits passed to engine entry points.

    Attributes:
        max_assignment_percent: Inclusive maximum of an assignment percent (> 0 always
            required). Raise it to allow "own allocation exceeds capacity" cases.
        days_display_decimals: Decimals for displayed / exported day values.
        money_display_decimals: Decimals for displayed / exported money.
        money_rounding: ``decimal`` rounding mode for money display (ROUND_HALF_EVEN).
        days_rounding: ``decimal`` rounding mode for day display (ROUND_HALF_EVEN).
    """

    max_assignment_percent: Decimal = MAX_ASSIGNMENT_PERCENT
    days_display_decimals: int = DAYS_DISPLAY_DECIMALS
    money_display_decimals: int = MONEY_DISPLAY_DECIMALS
    money_rounding: str = MONEY_ROUNDING
    days_rounding: str = DAYS_ROUNDING

    def __post_init__(self) -> None:
        mp = self.max_assignment_percent
        if isinstance(mp, int) and not isinstance(mp, bool):
            object.__setattr__(self, "max_assignment_percent", Decimal(mp))
        elif isinstance(mp, str):
            try:
                object.__setattr__(self, "max_assignment_percent", Decimal(mp.strip()))
            except ArithmeticError:
                raise ValueError(f"max_assignment_percent {mp!r} is not a number") from None
        elif not isinstance(mp, Decimal):
            raise TypeError("max_assignment_percent must be a Decimal (or int / str)")
        if not self.max_assignment_percent.is_finite() or self.max_assignment_percent <= 0:
            raise ValueError("max_assignment_percent must be a finite value > 0")
        for name in ("days_display_decimals", "money_display_decimals"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be an int >= 0")

    @engine_context
    def round_days(self, value: Decimal) -> Decimal:
        """Round a day value for display/export (``days_display_decimals``)."""
        return value.quantize(Decimal(1).scaleb(-self.days_display_decimals), self.days_rounding)

    @engine_context
    def round_money(self, value: Decimal) -> Decimal:
        """Round a money amount for display/export (``money_display_decimals``)."""
        return value.quantize(Decimal(1).scaleb(-self.money_display_decimals), self.money_rounding)


DEFAULT_CONFIG: Config = Config()
"""The configuration used unless a caller passes another one."""
