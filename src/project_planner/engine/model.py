"""Immutable domain model of a project definition (plan sections 2.1-2.3 and 3).

Everything here is a frozen, slotted dataclass or a ``str``-valued enum, so values
are hashable, equality-comparable by value, safe to share between threads, and
compare equal to their string codes (``TimeUnit.HOURS == "hours"``).

Quantities and money are ``Decimal``; ``float`` is rejected everywhere
(``TypeError``) because it cannot represent values such as ``0.1`` exactly.
IDs are non-empty ``str`` without leading/trailing whitespace; there is no
implicit conversion from ``int``.

Error policy of constructors
----------------------------
* Wrong Python type (``float``, ``bool``, ``int`` ID, non-``TimeQty`` sizing...):
  ``TypeError``. These are programming errors.
* Right type, invalid value (negative sizing, percent <= 0, bad calendar...):
  :class:`~project_planner.engine.errors.ValidationFailed` carrying ``Issue``\\ s
  with ``code``, ``object_type``, ``object_id`` and ``field``. Services may let it
  propagate to the user unchanged.
* Enum-typed fields accept the enum member or its exact string value, and are
  normalised to the member.

Boundary with ``engine.validation`` (task T12)
----------------------------------------------
Constructors enforce only **local** invariants: those checkable from one object
(sizing rules, ranges with fixed bounds), plus, in :class:`Project`, unique IDs
per collection and one assignment per (task, resource). Everything that needs
cross-references is T12's job and is *not* checked here: dangling references,
self-dependencies, parent cycles (including a node that is its own parent),
parents that are not groups, dependencies on groups, assignments to non-tasks,
duplicate dependencies between the same pair, and the assignment percent maximum
(``Config.max_assignment_percent``). A ``Project`` can therefore hold an invalid
graph, which is exactly what validation and its tests need.

Issue codes raised here
-----------------------
=================================  ===============================================
``TIME_INVALID``                   time text / number cannot be read, or not finite
``TIME_UNITLESS``                  time text has no unit (``"40"``)
``TIME_BAD_UNIT``                  unit is not ``h``/``hours``/``d``/``days``
``CAL_BAD_VALUE``                  calendar argument of the wrong form / type
``CAL_HOURS_PER_DAY_RANGE``        ``hours_per_day`` not in ``(0, 24]``
``CAL_HOURS_PER_DAY_PRECISION``    ``hours_per_day * 60`` is not a whole number
``CAL_DAYS_PER_YEAR_RANGE``        ``working_days_per_year`` not in ``1..366``
``CAL_NO_WORKING_WEEKDAYS``        empty ``working_weekdays``
``CAL_WORKDAY_OVERFLOW``           ``workday_start + hours_per_day > 24:00``
``CAL_DUPLICATE_HOLIDAY``          two holidays on one date
``CAL_DUPLICATE_EXCEPTION``        two exceptions on one date
``CAL_HOLIDAY_EXCEPTION_CONFLICT`` a date is both a holiday and an exception
``INVALID_ID``                     empty ID, or leading/trailing whitespace
``INVALID_VALUE``                  string that is not a value of the enum field
``NODE_SIZING_NOT_ALLOWED``        group/milestone with a sizing
``NODE_SIZING_PARTIAL``            task with mode but no quantity, or vice versa
``NODE_NEGATIVE_SIZING``           sizing value < 0
``RESOURCE_NEGATIVE_RATE``         hourly rate < 0 or not finite
``ASSIGNMENT_PERCENT_RANGE``       percent <= 0 or not finite
``PROJECT_BAD_CURRENCY``           currency is not three upper-case ASCII letters
``DUPLICATE_ID``                   two nodes / resources / dependencies share an ID
``DUPLICATE_ASSIGNMENT``           two assignments for one (task, resource)
=================================  ===============================================
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, NoReturn, TypeVar

from project_planner.engine import config as _cfg
from project_planner.engine.errors import Issue, NotFound, ObjectType, ValidationFailed

__all__ = [
    "DEFAULT_WORKING_WEEKDAYS",
    "Assignment",
    "Calendar",
    "CalendarException",
    "CalendarSettings",
    "Dependency",
    "DependencyType",
    "ExceptionKind",
    "Holiday",
    "NodeKind",
    "Project",
    "Resource",
    "SizingMode",
    "TimeQty",
    "TimeUnit",
    "WbsNode",
    "Weekday",
    "WorkUnit",
    "as_time_qty",
    "days",
    "hours",
    "minutes_to_days",
    "parse_weekdays",
]

_E = TypeVar("_E", bound=StrEnum)

MINUTES_PER_HOUR = 60
_MINUTES_PER_CALENDAR_DAY = 24 * 60


# =============================================================================
# Enums
# =============================================================================


class NodeKind(StrEnum):
    """Kind of a WBS node."""

    GROUP = "group"
    """Summary node; dates, effort and cost roll up from its descendants. Never sized."""
    TASK = "task"
    """Leaf work item, sized by duration or effort (or unsized)."""
    MILESTONE = "milestone"
    """Zero-duration marker. Never sized."""


class SizingMode(StrEnum):
    """How a task's size is given."""

    DURATION = "duration"
    """``sizing`` is the working duration; effort follows from the assignments."""
    EFFORT = "effort"
    """``sizing`` is the effort in person-time; duration follows from the assignments."""
    NONE = "none"
    """No sizing (groups, milestones, unsized tasks)."""


class TimeUnit(StrEnum):
    """Unit of an entered time quantity (plan 2.1). CSV symbols: ``h`` / ``d``."""

    HOURS = "hours"
    DAYS = "days"

    @property
    def symbol(self) -> str:
        """Short symbol used in text and CSV: ``"h"`` or ``"d"``."""
        return "h" if self is TimeUnit.HOURS else "d"

    @classmethod
    def from_symbol(cls, symbol: str) -> TimeUnit:
        """Return the unit for the exact CSV symbol ``"h"`` or ``"d"``.

        Raises:
            ValueError: for any other text.
        """
        if symbol == "h":
            return cls.HOURS
        if symbol == "d":
            return cls.DAYS
        raise ValueError(f"unknown time unit symbol {symbol!r} (expected 'h' or 'd')")


class DependencyType(StrEnum):
    """Dependency type: which end of the predecessor constrains which end of the successor."""

    FS = "FS"
    """Finish-to-start."""
    SS = "SS"
    """Start-to-start."""
    FF = "FF"
    """Finish-to-finish."""
    SF = "SF"
    """Start-to-finish."""


class WorkUnit(StrEnum):
    """Unit used to *report* work in cost reports (plan 2.2). Never changes money."""

    PERSON_HOURS = "person_hours"
    PERSON_DAYS = "person_days"
    PERSON_YEARS = "person_years"

    def hours_per_unit(self, calendar: Calendar) -> Decimal:
        """Hours in one unit: 1, ``hours_per_day``, or ``hours_per_day * working_days_per_year``."""
        if self is WorkUnit.PERSON_HOURS:
            return Decimal(1)
        if self is WorkUnit.PERSON_DAYS:
            return calendar.hours_per_day
        return calendar.hours_per_day * calendar.working_days_per_year


class ExceptionKind(StrEnum):
    """Kind of a calendar date exception."""

    WORKING = "working"
    """The date is worked even if its weekday is not a working weekday."""
    NONWORKING = "nonworking"
    """The date is not worked even if its weekday is a working weekday."""


class Weekday(StrEnum):
    """Day of the week. Values are the CSV tokens ``Mon`` ... ``Sun``.

    Enum iteration order is Monday first. Note that sorting a set of weekdays
    with ``sorted()`` sorts by *string value*; use :meth:`ordered` instead.
    """

    MON = "Mon"
    TUE = "Tue"
    WED = "Wed"
    THU = "Thu"
    FRI = "Fri"
    SAT = "Sat"
    SUN = "Sun"

    @property
    def python_weekday(self) -> int:
        """The ``datetime.date.weekday()`` number: Monday 0 ... Sunday 6."""
        return _WEEKDAY_INDEX[self]

    @classmethod
    def from_python_weekday(cls, number: int) -> Weekday:
        """Inverse of :attr:`python_weekday` (0 = Monday ... 6 = Sunday).

        Raises:
            ValueError: if ``number`` is not in ``0..6``.
        """
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 6:
            raise ValueError(f"weekday number must be an int in 0..6, got {number!r}")
        return _WEEKDAYS[number]

    @classmethod
    def of(cls, day: dt.date) -> Weekday:
        """Weekday of a date."""
        return _WEEKDAYS[day.weekday()]

    @classmethod
    def parse(cls, token: str) -> Weekday:
        """Parse a three-letter abbreviation, case-insensitively, ignoring surrounding spaces.

        Raises:
            ValueError: for anything else (full names and numbers are not accepted).
        """
        key = token.strip().lower()
        for wd in _WEEKDAYS:
            if wd.value.lower() == key:
                return wd
        raise ValueError(f"unknown weekday {token!r} (expected one of Mon Tue Wed Thu Fri Sat Sun)")

    @classmethod
    def ordered(cls, weekdays: Iterable[Weekday]) -> tuple[Weekday, ...]:
        """Return the distinct weekdays sorted Monday first."""
        return tuple(sorted(set(weekdays), key=lambda w: _WEEKDAY_INDEX[w]))


_WEEKDAYS: tuple[Weekday, ...] = tuple(Weekday)
_WEEKDAY_INDEX: dict[Weekday, int] = {w: i for i, w in enumerate(_WEEKDAYS)}

DEFAULT_WORKING_WEEKDAYS: frozenset[Weekday] = frozenset(
    Weekday(v) for v in _cfg.DEFAULT_WORKING_WEEKDAYS
)
"""Monday to Friday (plan 2.3)."""


def parse_weekdays(text: str) -> frozenset[Weekday]:
    """Parse a weekday list such as ``"Mon-Fri"``, ``"Mon;Tue;Thu"`` or ``"Sun-Thu, Sat"``.

    Items are separated by ``;``, ``,`` or whitespace; an item is a weekday
    abbreviation or an inclusive range ``A-B`` (which wraps past Sunday, so
    ``"Sun-Thu"`` is Sunday to Thursday). Case-insensitive; repeats are allowed.
    An empty or blank string gives the empty set (which a calendar rejects).

    Raises:
        ValueError: if an item is not a weekday or a range of weekdays.
    """
    result: set[Weekday] = set()
    for item in re.split(r"[;,\s]+", text.strip()):
        if not item:
            continue
        if "-" in item:
            first_text, _, last_text = item.partition("-")
            first, last = Weekday.parse(first_text), Weekday.parse(last_text)
            i = first.python_weekday
            while True:
                result.add(_WEEKDAYS[i])
                if i == last.python_weekday:
                    break
                i = (i + 1) % 7
        else:
            result.add(Weekday.parse(item))
    return frozenset(result)


# =============================================================================
# Small helpers
# =============================================================================


def _fail(
    code: str,
    message: str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    field: str | None = None,
) -> NoReturn:
    raise ValidationFailed(
        [Issue.error(code, message, object_type=object_type, object_id=object_id, field=field)]
    )


def _type_name(value: object) -> str:
    return type(value).__name__


def _decimal_field(value: object, what: str) -> Decimal:
    """Accept ``Decimal`` or ``int`` (converted); reject everything else with TypeError."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise TypeError(f"{what} must be a Decimal (or int), got {_type_name(value)} {value!r}")


def _enum_field(
    enum_cls: type[_E], value: object, what: str, object_type: str, object_id: str | None
) -> _E:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            allowed = ", ".join(m.value for m in enum_cls)
            _fail(
                "INVALID_VALUE",
                f"{what} {value!r} is not one of: {allowed}",
                object_type=object_type,
                object_id=object_id,
                field=what,
            )
    raise TypeError(f"{what} must be a {enum_cls.__name__} or str, got {_type_name(value)}")


def _check_id(value: object, what: str, object_type: str, object_id: str | None = None) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a str, got {_type_name(value)} {value!r}")
    if value == "" or value.strip() != value:
        _fail(
            "INVALID_ID",
            f"{what} {value!r} must be non-empty without leading/trailing whitespace",
            object_type=object_type,
            object_id=object_id if object_id is not None else (value or None),
            field=what,
        )
    return value


def _check_str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a str, got {_type_name(value)} {value!r}")
    return value


def _check_date(value: object, what: str) -> dt.date:
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise TypeError(f"{what} must be a datetime.date (not datetime), got {_type_name(value)}")
    return value


# =============================================================================
# Time quantities (plan 2.1)
# =============================================================================


@dataclass(frozen=True, slots=True)
class TimeQty:
    """A time quantity exactly as entered: a ``Decimal`` value and an hours/days unit.

    Used for task duration, task effort (person-time) and dependency lag. The value
    and unit are never rewritten: ``"40h"`` stays 40 hours even if hours/day
    changes. Days are converted with the calendar's hours per day only at
    calculation time (:meth:`to_minutes`).

    Equality is numeric on the value (``1.5d == 1.50d``) and exact on the unit
    (``8h != 1d``). The value may be negative (lags); sizing rules (>= 0) are
    enforced by :class:`WbsNode`.

    ``str()`` gives the compact form ``"40h"`` / ``"-0.5d"`` /``"1.50d"``, which
    :meth:`parse` reads back exactly.

    Construct with :func:`hours`, :func:`days`, :meth:`parse` or :func:`as_time_qty`.
    The raw constructor ``TimeQty(Decimal("1.5"), TimeUnit.DAYS)`` is also fine; it
    accepts ``int`` (converted) and the unit's string value, rejects ``float`` /
    ``bool`` (``TypeError``) and non-finite values (``ValidationFailed``).

    Attributes:
        value: The entered number.
        unit: :attr:`TimeUnit.HOURS` or :attr:`TimeUnit.DAYS`.
    """

    value: Decimal
    unit: TimeUnit

    def __post_init__(self) -> None:
        value = _decimal_field(self.value, "time quantity value")
        if not value.is_finite():
            _fail("TIME_INVALID", f"time quantity value {value!r} must be finite")
        object.__setattr__(self, "value", value)
        if not isinstance(self.unit, TimeUnit):
            try:
                object.__setattr__(self, "unit", TimeUnit(self.unit))
            except ValueError:
                raise TypeError(
                    f"unit must be a TimeUnit ('hours'/'days'), got {self.unit!r}"
                ) from None

    def __str__(self) -> str:
        return f"{self.value}{self.unit.symbol}"

    def to_minutes(self, minutes_per_day: int) -> int:
        """Working minutes on the internal axis, rounded **up** (toward +infinity).

        ``hours``: ``value * 60``; ``days``: ``value * minutes_per_day``. When the
        product is not a whole number it is rounded with ``ROUND_CEILING``:

        * positive values round up: ``hours("0.01")`` -> 1 (never shorter than entered);
        * negative values (leads) round toward zero: -0.5 min -> 0, -1.5 min -> -1.
          A successor is therefore never placed earlier than the exact lag allows.

        The function is monotonic. Durations and effort are never negative
        (enforced by :class:`WbsNode`), so only lags can hit the negative rule.

        Args:
            minutes_per_day: The calendar's working minutes per day
                (:attr:`Calendar.minutes_per_day`), an ``int`` > 0. Used for days only
                but always checked.

        Raises:
            TypeError / ValueError: if ``minutes_per_day`` is not an ``int`` > 0.
        """
        _check_minutes_per_day(minutes_per_day)
        factor = MINUTES_PER_HOUR if self.unit is TimeUnit.HOURS else minutes_per_day
        num, den = self.value.as_integer_ratio()  # exact: no decimal-context rounding
        return -((-num * factor) // den)

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        object_type: str | None = None,
        object_id: str | None = None,
        field: str | None = None,
    ) -> TimeQty:
        """Parse ``"40h"``, ``"5d"``, ``"-0.5d"``, ``"1.5 d"``, ``"2 Days"``, ``"1e1h"``.

        Grammar: optional sign, decimal number (optional exponent), optional
        whitespace, unit. Units (case-insensitive): ``h``, ``hour``, ``hours``,
        ``d``, ``day``, ``days``. Surrounding whitespace is ignored. The number's
        digits are kept exactly (``"1.50d"`` has value ``Decimal("1.50")``).

        The keyword arguments only attribute the issue raised on failure.

        Raises:
            TypeError: if ``text`` is not a ``str``.
            ValidationFailed: code ``TIME_UNITLESS`` for a number without unit
                (``"40"``), ``TIME_BAD_UNIT`` for an unknown unit, ``TIME_INVALID``
                for anything else that is not a quantity.
        """
        if not isinstance(text, str):
            raise TypeError(f"time text must be a str, got {_type_name(text)}")
        where: dict[str, Any] = {
            "object_type": object_type,
            "object_id": object_id,
            "field": field,
        }
        match = _TIME_RE.fullmatch(text.strip())
        if match is None:
            _fail(
                "TIME_INVALID",
                f"{text!r} is not a time quantity (examples: '40h', '5d', '-0.5d')",
                **where,
            )
        number, unit_text = match.group(1), match.group(2)
        if unit_text == "":
            _fail(
                "TIME_UNITLESS",
                f"{text!r} has no unit; write hours or days, e.g. '{number}h' or '{number}d'",
                **where,
            )
        unit = _UNIT_WORDS.get(unit_text.lower())
        if unit is None:
            _fail(
                "TIME_BAD_UNIT",
                f"unknown time unit {unit_text!r} in {text!r} (use h/hours or d/days)",
                **where,
            )
        return cls(Decimal(number), unit)


_TIME_RE = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z]*)")
_UNIT_WORDS: dict[str, TimeUnit] = {
    "h": TimeUnit.HOURS,
    "hour": TimeUnit.HOURS,
    "hours": TimeUnit.HOURS,
    "d": TimeUnit.DAYS,
    "day": TimeUnit.DAYS,
    "days": TimeUnit.DAYS,
}


def _check_minutes_per_day(minutes_per_day: object) -> int:
    if isinstance(minutes_per_day, bool) or not isinstance(minutes_per_day, int):
        raise TypeError(f"minutes_per_day must be an int, got {_type_name(minutes_per_day)}")
    if minutes_per_day <= 0:
        raise ValueError(f"minutes_per_day must be > 0, got {minutes_per_day}")
    return minutes_per_day


def _user_number(x: object, unit: TimeUnit) -> Decimal:
    """Number given to :func:`hours` / :func:`days`."""
    if isinstance(x, bool | float):
        raise TypeError(
            f"{unit.value}() does not accept {_type_name(x)} {x!r}: floats are inexact; "
            f"pass an int, a str such as '1.5' or a Decimal"
        )
    if isinstance(x, Decimal):
        return x
    if isinstance(x, int):
        return Decimal(x)
    if isinstance(x, str):
        try:
            return Decimal(x.strip())
        except InvalidOperation:
            _fail("TIME_INVALID", f"{unit.value}({x!r}): {x!r} is not a number")
    raise TypeError(f"{unit.value}() needs an int, str or Decimal, got {_type_name(x)}")


def hours(x: int | str | Decimal) -> TimeQty:
    """A quantity in hours: ``hours(40)``, ``hours("1.5")``, ``hours(Decimal("0.25"))``.

    Raises:
        TypeError: for ``float``, ``bool`` or other types.
        ValidationFailed: ``TIME_INVALID`` if a string is not a finite number.
    """
    return TimeQty(_user_number(x, TimeUnit.HOURS), TimeUnit.HOURS)


def days(x: int | str | Decimal) -> TimeQty:
    """A quantity in (working / person) days: ``days(5)``, ``days("-0.5")``.

    Raises:
        TypeError: for ``float``, ``bool`` or other types.
        ValidationFailed: ``TIME_INVALID`` if a string is not a finite number.
    """
    return TimeQty(_user_number(x, TimeUnit.DAYS), TimeUnit.DAYS)


def as_time_qty(
    value: TimeQty | str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    field: str | None = None,
) -> TimeQty:
    """Normalise a user-supplied duration / effort / lag to a :class:`TimeQty`.

    Accepts a ``TimeQty`` (returned unchanged) or a string for :meth:`TimeQty.parse`.
    Bare numbers are refused because their unit would be ambiguous (plan 2.1).
    The keyword arguments attribute the issue raised for bad strings.

    Raises:
        TypeError: for numbers (``int``, ``float``, ``Decimal``, ``bool``) and other
            types; the message shows the accepted hours/days forms.
        ValidationFailed: for invalid or unit-less strings (see :meth:`TimeQty.parse`).
    """
    if isinstance(value, TimeQty):
        return value
    if isinstance(value, str):
        return TimeQty.parse(value, object_type=object_type, object_id=object_id, field=field)
    if isinstance(value, int | float | Decimal):
        raise TypeError(
            f"bare number {value!r} has no time unit; use hours({value!r}) or days({value!r}), "
            f"or a string such as '{value}h' (hours) or '{value}d' (days)"
        )
    raise TypeError(
        f"expected a TimeQty or a str such as '40h' (hours) or '5d' (days), got {_type_name(value)}"
    )


def minutes_to_days(minutes: int, minutes_per_day: int) -> Decimal:
    """Working minutes as working days: ``Decimal(minutes) / Decimal(minutes_per_day)``.

    No rounding is applied by this function (results are exact whenever the
    quotient terminates; otherwise they carry the ``decimal`` context precision,
    28 significant digits by default). Round only for display (``Config.round_days``).

    Raises:
        TypeError / ValueError: if ``minutes`` is not an ``int`` or
            ``minutes_per_day`` is not an ``int`` > 0.
    """
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise TypeError(f"minutes must be an int, got {_type_name(minutes)}")
    _check_minutes_per_day(minutes_per_day)
    return Decimal(minutes) / Decimal(minutes_per_day)


# =============================================================================
# Calendar (plan 2.3)
# =============================================================================


@dataclass(frozen=True, slots=True)
class Holiday:
    """A nonworking date with an optional name.

    Attributes:
        date: The date (a ``datetime.date``, not a ``datetime``).
        name: Free text, may be empty.
    """

    date: dt.date
    name: str = ""

    def __post_init__(self) -> None:
        _check_date(self.date, "holiday date")
        _check_str(self.name, "holiday name")


@dataclass(frozen=True, slots=True)
class CalendarException:
    """A date whose working status overrides the weekday pattern.

    Attributes:
        date: The date (a ``datetime.date``, not a ``datetime``).
        kind: :class:`ExceptionKind` (or its string value).
        name: Free text, may be empty.
    """

    date: dt.date
    kind: ExceptionKind
    name: str = ""

    def __post_init__(self) -> None:
        _check_date(self.date, "exception date")
        kind = _enum_field(ExceptionKind, self.kind, "kind", ObjectType.CALENDAR, None)
        object.__setattr__(self, "kind", kind)
        _check_str(self.name, "exception name")


_CAL_FIELDS = (
    "working_weekdays",
    "hours_per_day",
    "working_days_per_year",
    "workday_start",
    "holidays",
    "exceptions",
)


class _Bad(Exception):
    """Internal: a calendar argument could not be coerced."""


def _cal_issue(code: str, message: str, field_name: str) -> Issue:
    return Issue.error(code, message, object_type=ObjectType.CALENDAR, field=field_name)


def _exact_minutes(hours_value: Decimal) -> int | None:
    """``hours_value * 60`` if it is exactly a whole number (no context rounding), else None."""
    num, den = hours_value.as_integer_ratio()
    minutes, rest = divmod(num * MINUTES_PER_HOUR, den)
    return minutes if rest == 0 else None


def _whole_minutes(hours_value: Decimal) -> int:
    minutes = _exact_minutes(hours_value)
    assert minutes is not None
    return minutes


def _coerce_hours_per_day(value: object) -> Decimal:
    if isinstance(value, bool | float):
        raise _Bad(f"hours_per_day {value!r}: floats are not accepted; use int, str or Decimal")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            value = Decimal(value.strip())
        except InvalidOperation:
            raise _Bad(f"hours_per_day {value!r} is not a number") from None
    if not isinstance(value, Decimal):
        raise _Bad(f"hours_per_day must be a number, got {_type_name(value)}")
    if not value.is_finite():
        raise _Bad(f"hours_per_day {value} is not finite")
    return value


def _coerce_days_per_year(value: object) -> int:
    if isinstance(value, bool | float):
        raise _Bad(f"working_days_per_year {value!r} must be a whole number (int)")
    if isinstance(value, int):
        return value
    number: Decimal
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            raise _Bad(f"working_days_per_year {value!r} is not a number") from None
    elif isinstance(value, Decimal):
        number = value
    else:
        raise _Bad(f"working_days_per_year must be an int, got {_type_name(value)}")
    if not number.is_finite() or number != number.to_integral_value():
        raise _Bad(f"working_days_per_year {value!r} must be a whole number")
    return int(number)


def _coerce_weekdays(value: object) -> frozenset[Weekday]:
    try:
        if isinstance(value, str):
            return parse_weekdays(value)
        if isinstance(value, Iterable):
            out: set[Weekday] = set()
            for item in value:
                if isinstance(item, Weekday):
                    out.add(item)
                elif isinstance(item, str):
                    out.add(Weekday.parse(item))
                else:
                    raise _Bad(f"working_weekdays item {item!r} is not a weekday")
            return frozenset(out)
    except ValueError as exc:
        raise _Bad(f"working_weekdays: {exc}") from None
    raise _Bad(f"working_weekdays must be a str or a collection of weekdays, got {value!r}")


_HHMM_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _coerce_start(value: object) -> dt.time:
    if isinstance(value, str):
        match = _HHMM_RE.fullmatch(value.strip())
        if match is None or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise _Bad(f"workday_start {value!r} is not a time HH:MM in 00:00..23:59")
        return dt.time(int(match.group(1)), int(match.group(2)))
    if isinstance(value, dt.time):
        if value.tzinfo is not None or value.second or value.microsecond:
            raise _Bad(f"workday_start {value} must be a naive time on a whole minute")
        return value
    raise _Bad(f"workday_start must be a datetime.time or 'HH:MM', got {value!r}")


def _coerce_holiday(item: object) -> Holiday:
    if isinstance(item, Holiday):
        return item
    try:
        if isinstance(item, dt.date):
            return Holiday(item)
        if isinstance(item, tuple) and 1 <= len(item) <= 2:
            return Holiday(*item)
    except (TypeError, ValueError):
        pass
    raise _Bad(f"holiday {item!r} must be a Holiday, a date or a (date, name) tuple")


def _coerce_exception(item: object) -> CalendarException:
    if isinstance(item, CalendarException):
        return item
    if isinstance(item, tuple) and 2 <= len(item) <= 3:
        kind: Any = item[1].strip().lower() if isinstance(item[1], str) else item[1]
        try:
            return CalendarException(item[0], kind, *item[2:])  # str kind is normalised
        except (TypeError, ValueError, ValidationFailed):
            pass
    raise _Bad(
        f"exception {item!r} must be a CalendarException or a (date, 'working'|'nonworking'"
        f"[, name]) tuple"
    )


def _coerce_dated(value: object, what: str, one: Any) -> tuple[Any, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise _Bad(f"{what} must be a collection, got {_type_name(value)}")
    items = [one(item) for item in value]
    return tuple(sorted(items, key=lambda x: x.date))


def _check_calendar(raw: dict[str, Any]) -> tuple[dict[str, Any], list[Issue]]:
    """Coerce and validate calendar fields together (plan 2.3).

    Returns the values to store (coerced where possible, raw where coercion
    failed) and every issue found. Rules depending on a field that failed
    coercion are skipped, so one bad argument yields one issue.
    """
    values = dict(raw)
    issues: list[Issue] = []
    ok: dict[str, Any] = {}
    coercers: dict[str, Any] = {
        "working_weekdays": _coerce_weekdays,
        "hours_per_day": _coerce_hours_per_day,
        "working_days_per_year": _coerce_days_per_year,
        "workday_start": _coerce_start,
        "holidays": lambda v: _coerce_dated(v, "holidays", _coerce_holiday),
        "exceptions": lambda v: _coerce_dated(v, "exceptions", _coerce_exception),
    }
    for name in _CAL_FIELDS:
        try:
            ok[name] = values[name] = coercers[name](raw[name])
        except _Bad as exc:
            issues.append(_cal_issue("CAL_BAD_VALUE", str(exc), name))

    if "working_weekdays" in ok and not ok["working_weekdays"]:
        issues.append(
            _cal_issue(
                "CAL_NO_WORKING_WEEKDAYS",
                "working_weekdays must contain at least one weekday",
                "working_weekdays",
            )
        )
    hpd_ok = False
    if "hours_per_day" in ok:
        hpd: Decimal = ok["hours_per_day"]
        if not Decimal(0) < hpd <= Decimal(24):
            issues.append(
                _cal_issue(
                    "CAL_HOURS_PER_DAY_RANGE",
                    f"hours_per_day {hpd} must be > 0 and <= 24",
                    "hours_per_day",
                )
            )
        elif _exact_minutes(hpd) is None:
            issues.append(
                _cal_issue(
                    "CAL_HOURS_PER_DAY_PRECISION",
                    f"hours_per_day {hpd} must be a whole number of minutes (e.g. 7.5, not 7.333)",
                    "hours_per_day",
                )
            )
        else:
            hpd_ok = True
    if "working_days_per_year" in ok and not 1 <= ok["working_days_per_year"] <= 366:
        issues.append(
            _cal_issue(
                "CAL_DAYS_PER_YEAR_RANGE",
                f"working_days_per_year {ok['working_days_per_year']} must be in 1..366",
                "working_days_per_year",
            )
        )
    if hpd_ok and "workday_start" in ok:
        start: dt.time = ok["workday_start"]
        end_minute = start.hour * 60 + start.minute + _whole_minutes(ok["hours_per_day"])
        if end_minute > _MINUTES_PER_CALENDAR_DAY:
            issues.append(
                _cal_issue(
                    "CAL_WORKDAY_OVERFLOW",
                    f"workday_start {start:%H:%M} + {ok['hours_per_day']} h ends at "
                    f"{end_minute // 60:02d}:{end_minute % 60:02d}, after 24:00",
                    "workday_start",
                )
            )
    holiday_dates: set[dt.date] = set()
    if "holidays" in ok:
        for h in ok["holidays"]:
            if h.date in holiday_dates:
                issues.append(
                    _cal_issue(
                        "CAL_DUPLICATE_HOLIDAY",
                        f"holiday date {h.date.isoformat()} appears more than once",
                        "holidays",
                    )
                )
            holiday_dates.add(h.date)
    if "exceptions" in ok:
        seen: set[dt.date] = set()
        for e in ok["exceptions"]:
            if e.date in seen:
                issues.append(
                    _cal_issue(
                        "CAL_DUPLICATE_EXCEPTION",
                        f"exception date {e.date.isoformat()} appears more than once",
                        "exceptions",
                    )
                )
            elif e.date in holiday_dates:
                issues.append(
                    _cal_issue(
                        "CAL_HOLIDAY_EXCEPTION_CONFLICT",
                        f"date {e.date.isoformat()} is both a holiday and an exception",
                        "exceptions",
                    )
                )
            seen.add(e.date)
    return values, issues


def _calendar_raw(obj: Any) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in _CAL_FIELDS}


@dataclass(frozen=True, slots=True)
class Calendar:
    """A project's validated working calendar (plan 2.3). Always valid once constructed.

    ``Calendar()`` is the default calendar: Mon-Fri, 8 h/day from 09:00, 220
    working days per year, no holidays or exceptions.

    The constructor normalises its inputs exactly like :class:`CalendarSettings`
    (``int`` hours, weekday strings, ``"HH:MM"``, ``(date, name)`` tuples...),
    stores ``holidays`` and ``exceptions`` sorted by date (so equality ignores
    input order), and raises ``ValidationFailed`` with *every* rule violation.

    Attributes:
        working_weekdays: Weekdays worked unless a holiday/exception says otherwise.
        hours_per_day: Working hours per working day, in ``(0, 24]`` and a whole
            number of minutes.
        working_days_per_year: ``1..366``; only used for person-year reporting.
        workday_start: Clock time the single daily working block starts;
            ``workday_start + hours_per_day <= 24:00``.
        holidays: Nonworking dates, unique, sorted by date.
        exceptions: Working/nonworking date overrides, unique, sorted by date, and
            on dates that are not holidays.
    """

    working_weekdays: frozenset[Weekday] = DEFAULT_WORKING_WEEKDAYS
    hours_per_day: Decimal = _cfg.DEFAULT_HOURS_PER_DAY
    working_days_per_year: int = _cfg.DEFAULT_WORKING_DAYS_PER_YEAR
    workday_start: dt.time = _cfg.DEFAULT_WORKDAY_START
    holidays: tuple[Holiday, ...] = ()
    exceptions: tuple[CalendarException, ...] = ()

    def __post_init__(self) -> None:
        values, issues = _check_calendar(_calendar_raw(self))
        if issues:
            raise ValidationFailed(issues)
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @property
    def minutes_per_day(self) -> int:
        """Working minutes per working day (``hours_per_day * 60``; always whole)."""
        return _whole_minutes(self.hours_per_day)

    @property
    def workday_start_minute(self) -> int:
        """Minutes from midnight to :attr:`workday_start` (09:00 -> 540)."""
        return self.workday_start.hour * 60 + self.workday_start.minute

    def settings(self) -> CalendarSettings:
        """The same values as a :class:`CalendarSettings`."""
        return CalendarSettings(**_calendar_raw(self))


@dataclass(frozen=True, slots=True)
class CalendarSettings:
    """Public input type for calendar initialisation (plan 2.3); may hold invalid values.

    Every field has the plan 2.3 default, so ``CalendarSettings()`` equals the
    default settings and ``CalendarSettings().to_calendar() == Calendar()``.

    Construction never raises for bad *values*: inputs are normalised where
    possible (``int``/``str`` hours, weekday strings like ``"Mon-Fri"``,
    ``"HH:MM"`` times, ``(date, name)`` holiday tuples, ``(date, kind[, name])``
    exception tuples, lists -> tuples sorted by date) and the rest is kept as
    given. :meth:`validate` reports every problem; :meth:`to_calendar` raises them.
    For typed code that passes loosely-typed values, use :meth:`from_values`.

    Attributes: same as :class:`Calendar`.
    """

    working_weekdays: frozenset[Weekday] = DEFAULT_WORKING_WEEKDAYS
    hours_per_day: Decimal = _cfg.DEFAULT_HOURS_PER_DAY
    working_days_per_year: int = _cfg.DEFAULT_WORKING_DAYS_PER_YEAR
    workday_start: dt.time = _cfg.DEFAULT_WORKDAY_START
    holidays: tuple[Holiday, ...] = ()
    exceptions: tuple[CalendarException, ...] = ()
    _issues: tuple[Issue, ...] = field(
        default=(), init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        values, issues = _check_calendar(_calendar_raw(self))
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_issues", tuple(issues))

    @classmethod
    def from_values(
        cls,
        *,
        working_weekdays: str | Iterable[Weekday | str] = DEFAULT_WORKING_WEEKDAYS,
        hours_per_day: int | str | Decimal = _cfg.DEFAULT_HOURS_PER_DAY,
        working_days_per_year: int | str | Decimal = _cfg.DEFAULT_WORKING_DAYS_PER_YEAR,
        workday_start: dt.time | str = _cfg.DEFAULT_WORKDAY_START,
        holidays: Iterable[Holiday | dt.date | tuple[Any, ...]] = (),
        exceptions: Iterable[CalendarException | tuple[Any, ...]] = (),
    ) -> CalendarSettings:
        """Build *validated* settings from loosely-typed arguments (omitted -> default).

        This is the implementation behind ``ws.calendar.initialize(...)``.

        Raises:
            ValidationFailed: with one issue per problem, all fields checked together
                (``Issue.field`` names the argument; the cross-field rule is on
                ``workday_start``).
        """
        args: dict[str, Any] = {
            "working_weekdays": working_weekdays,
            "hours_per_day": hours_per_day,
            "working_days_per_year": working_days_per_year,
            "workday_start": workday_start,
            "holidays": holidays,
            "exceptions": exceptions,
        }
        settings = cls(**args)
        if settings._issues:
            raise ValidationFailed(settings._issues)
        return settings

    def validate(self) -> list[Issue]:
        """Every rule violation of plan 2.3, all fields checked together; empty if valid."""
        return list(self._issues)

    def to_calendar(self) -> Calendar:
        """The validated :class:`Calendar`.

        Raises:
            ValidationFailed: with every issue :meth:`validate` reports.
        """
        if self._issues:
            raise ValidationFailed(self._issues)
        return Calendar(**_calendar_raw(self))


# =============================================================================
# WBS, resources, assignments, dependencies
# =============================================================================


@dataclass(frozen=True, slots=True)
class WbsNode:
    """One node of the work breakdown structure.

    Sizing rules (local invariants, ``ValidationFailed`` otherwise):

    * ``group`` and ``milestone``: ``sizing_mode == SizingMode.NONE`` and
      ``sizing is None`` (``NODE_SIZING_NOT_ALLOWED``).
    * ``task``: either unsized (``NONE`` / ``None``; valid, scheduling reports it
      incomplete) or sized (``DURATION``/``EFFORT`` with a ``TimeQty``); a partial
      combination is ``NODE_SIZING_PARTIAL``.
    * ``sizing.value >= 0`` (``NODE_NEGATIVE_SIZING``).

    Attributes:
        id: Node ID (namespace shared by all node kinds).
        name: Display name (any text).
        kind: :class:`NodeKind`.
        parent_id: ID of the parent group, or ``None`` for a top-level node.
            Existence / kind of the parent is checked by validation, not here.
        order: Sibling order; siblings display by ``(order, id)``.
        sizing_mode: :class:`SizingMode`.
        sizing: The duration or effort exactly as entered, or ``None``.
    """

    id: str
    name: str
    kind: NodeKind
    parent_id: str | None = None
    order: int = 0
    sizing_mode: SizingMode = SizingMode.NONE
    sizing: TimeQty | None = None

    def __post_init__(self) -> None:
        nt = ObjectType.NODE
        node_id = _check_id(self.id, "id", nt)
        _check_str(self.name, "name")
        kind = _enum_field(NodeKind, self.kind, "kind", nt, node_id)
        object.__setattr__(self, "kind", kind)
        if self.parent_id is not None:
            _check_id(self.parent_id, "parent_id", nt, node_id)
        if isinstance(self.order, bool) or not isinstance(self.order, int):
            raise TypeError(f"order must be an int, got {_type_name(self.order)}")
        mode = _enum_field(SizingMode, self.sizing_mode, "sizing_mode", nt, node_id)
        object.__setattr__(self, "sizing_mode", mode)
        sizing = self.sizing
        if sizing is not None and not isinstance(sizing, TimeQty):
            raise TypeError(f"sizing must be a TimeQty or None, got {_type_name(sizing)}")
        if kind is not NodeKind.TASK:
            if mode is not SizingMode.NONE or sizing is not None:
                _fail(
                    "NODE_SIZING_NOT_ALLOWED",
                    f"a {kind.value} cannot have a sizing (got {mode.value} {sizing})",
                    object_type=nt,
                    object_id=node_id,
                    field="sizing_mode" if mode is not SizingMode.NONE else "sizing",
                )
        elif (mode is SizingMode.NONE) != (sizing is None):
            _fail(
                "NODE_SIZING_PARTIAL",
                f"task sizing_mode {mode.value} and sizing {sizing} must be both set or both "
                f"absent",
                object_type=nt,
                object_id=node_id,
                field="sizing" if sizing is None else "sizing_mode",
            )
        if sizing is not None and sizing.value < 0:
            _fail(
                "NODE_NEGATIVE_SIZING",
                f"{mode.value} {sizing} must not be negative",
                object_type=nt,
                object_id=node_id,
                field="sizing",
            )

    @property
    def is_sized(self) -> bool:
        """True for a task with a duration or effort."""
        return self.sizing is not None


@dataclass(frozen=True, slots=True)
class Resource:
    """A person (or other resource) that can be assigned to tasks.

    Attributes:
        id: Resource ID.
        name: Display name.
        hourly_rate: Cost per working hour in the project currency, ``>= 0``;
            ``None`` means the rate is missing (costs are reported incomplete), which
            is different from an explicit ``Decimal(0)``.
    """

    id: str
    name: str
    hourly_rate: Decimal | None = None

    def __post_init__(self) -> None:
        rid = _check_id(self.id, "id", ObjectType.RESOURCE)
        _check_str(self.name, "name")
        if self.hourly_rate is not None:
            rate = _decimal_field(self.hourly_rate, "hourly_rate")
            if not rate.is_finite() or rate < 0:
                _fail(
                    "RESOURCE_NEGATIVE_RATE",
                    f"hourly_rate {rate} must be a finite value >= 0",
                    object_type=ObjectType.RESOURCE,
                    object_id=rid,
                    field="hourly_rate",
                )
            object.__setattr__(self, "hourly_rate", rate)


@dataclass(frozen=True, slots=True)
class Assignment:
    """A resource working on a task at a percentage of full time.

    Attributes:
        task_id: ID of the task (must be a task: checked by validation).
        resource_id: ID of the resource (existence checked by validation).
        percent: Allocation, ``80`` meaning 80 %. Must be ``> 0`` here; the upper
            bound (``Config.max_assignment_percent``) is checked by validation.
    """

    task_id: str
    resource_id: str
    percent: Decimal

    def __post_init__(self) -> None:
        at = ObjectType.ASSIGNMENT
        _check_id(self.task_id, "task_id", at)
        _check_id(self.resource_id, "resource_id", at)
        percent = _decimal_field(self.percent, "percent")
        if not percent.is_finite() or percent <= 0:
            _fail(
                "ASSIGNMENT_PERCENT_RANGE",
                f"percent {percent} must be > 0",
                object_type=at,
                object_id=self.key_text,
                field="percent",
            )
        object.__setattr__(self, "percent", percent)

    @property
    def key(self) -> tuple[str, str]:
        """``(task_id, resource_id)``: unique within a project."""
        return (self.task_id, self.resource_id)

    @property
    def key_text(self) -> str:
        """``"<task_id>/<resource_id>"``, used as ``Issue.object_id`` for assignments."""
        return f"{self.task_id}/{self.resource_id}"

    @property
    def fraction(self) -> Decimal:
        """``percent / 100``."""
        return self.percent / 100


_ZERO_LAG = TimeQty(Decimal(0), TimeUnit.DAYS)


@dataclass(frozen=True, slots=True)
class Dependency:
    """A scheduling constraint between two nodes.

    Attributes:
        id: Dependency ID.
        pred_id: Predecessor node ID.
        succ_id: Successor node ID.
        type: :class:`DependencyType` (default ``FS``).
        lag: Offset exactly as entered; negative is a lead. Default ``0d``.
    """

    id: str
    pred_id: str
    succ_id: str
    type: DependencyType = DependencyType.FS
    lag: TimeQty = _ZERO_LAG

    def __post_init__(self) -> None:
        dtp = ObjectType.DEPENDENCY
        dep_id = _check_id(self.id, "id", dtp)
        _check_id(self.pred_id, "pred_id", dtp, dep_id)
        _check_id(self.succ_id, "succ_id", dtp, dep_id)
        dep_type = _enum_field(DependencyType, self.type, "type", dtp, dep_id)
        object.__setattr__(self, "type", dep_type)
        if not isinstance(self.lag, TimeQty):
            raise TypeError(f"lag must be a TimeQty, got {_type_name(self.lag)}")


# =============================================================================
# Project
# =============================================================================


class _ProjectIndex:
    """Lookup tables of a :class:`Project`, built once at construction (read-only)."""

    __slots__ = (
        "assign_by_key",
        "assign_by_resource",
        "assign_by_task",
        "children",
        "deps_by_id",
        "deps_by_pred",
        "deps_by_succ",
        "nodes_by_id",
        "resources_by_id",
    )

    def __init__(self, project: Project) -> None:
        self.nodes_by_id: dict[str, WbsNode] = {n.id: n for n in project.nodes}
        self.resources_by_id: dict[str, Resource] = {r.id: r for r in project.resources}
        self.deps_by_id: dict[str, Dependency] = {d.id: d for d in project.dependencies}
        self.assign_by_key: dict[tuple[str, str], Assignment] = {
            a.key: a for a in project.assignments
        }
        children: dict[str | None, list[WbsNode]] = {}
        for n in project.nodes:
            children.setdefault(n.parent_id, []).append(n)
        self.children: dict[str | None, tuple[WbsNode, ...]] = {
            k: tuple(sorted(v, key=lambda n: (n.order, n.id))) for k, v in children.items()
        }
        by_task: dict[str, list[Assignment]] = {}
        by_res: dict[str, list[Assignment]] = {}
        for a in project.assignments:  # already sorted by (task_id, resource_id)
            by_task.setdefault(a.task_id, []).append(a)
            by_res.setdefault(a.resource_id, []).append(a)
        self.assign_by_task = {k: tuple(v) for k, v in by_task.items()}
        self.assign_by_resource = {k: tuple(v) for k, v in by_res.items()}
        by_pred: dict[str, list[Dependency]] = {}
        by_succ: dict[str, list[Dependency]] = {}
        for d in project.dependencies:  # already sorted by id
            by_pred.setdefault(d.pred_id, []).append(d)
            by_succ.setdefault(d.succ_id, []).append(d)
        self.deps_by_pred = {k: tuple(v) for k, v in by_pred.items()}
        self.deps_by_succ = {k: tuple(v) for k, v in by_succ.items()}


_CURRENCY_RE = re.compile(r"[A-Z]{3}")


def _sorted_unique(
    items: Iterable[Any], cls: type, key: Any, what: str, issues: list[Issue], dup: Any
) -> tuple[Any, ...]:
    seq = tuple(items)
    for item in seq:
        if not isinstance(item, cls):
            raise TypeError(f"{what} must contain {cls.__name__} values, got {_type_name(item)}")
    out = tuple(sorted(seq, key=key))
    for prev, cur in zip(out, out[1:], strict=False):
        if key(prev) == key(cur):
            issues.append(dup(cur))
    return out


@dataclass(frozen=True, slots=True)
class Project:
    """A complete, immutable project definition (no results).

    Collections are stored as tuples **sorted by key** (nodes, resources and
    dependencies by ``id``; assignments by ``(task_id, resource_id)``), so two
    projects with the same content are equal regardless of input order. Display
    order of the WBS comes from ``WbsNode.order``, never from tuple order.

    Construction checks local invariants only (unique IDs per collection, one
    assignment per (task, resource), currency shape, field types) and raises one
    ``ValidationFailed`` listing every duplicate. Cross-reference rules are
    ``engine.validation``'s job (see module docstring). To edit, build a new value
    with ``dataclasses.replace(project, nodes=...)``.

    Lookup methods use an index built once at construction (not part of equality,
    hash or repr), so they are O(1) and thread-safe.

    Attributes:
        id: Project ID.
        name: Project name.
        start: Project start date (a ``date``, not a ``datetime``).
        currency: ISO-4217-shaped code (three upper-case letters), default ``"USD"``.
        cost_report_unit: Default :class:`WorkUnit` of cost reports (``person_days``).
        calendar: The project :class:`Calendar`.
        nodes: All WBS nodes.
        resources: All resources.
        assignments: All assignments.
        dependencies: All dependencies.
    """

    id: str
    name: str
    start: dt.date
    currency: str = _cfg.DEFAULT_CURRENCY
    cost_report_unit: WorkUnit = WorkUnit(_cfg.DEFAULT_COST_REPORT_UNIT)
    calendar: Calendar = Calendar()
    nodes: tuple[WbsNode, ...] = ()
    resources: tuple[Resource, ...] = ()
    assignments: tuple[Assignment, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    _index: _ProjectIndex | None = field(
        default=None, init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        pt = ObjectType.PROJECT
        pid = _check_id(self.id, "id", pt)
        _check_str(self.name, "name")
        _check_date(self.start, "start")
        _check_str(self.currency, "currency")
        if _CURRENCY_RE.fullmatch(self.currency) is None:
            _fail(
                "PROJECT_BAD_CURRENCY",
                f"currency {self.currency!r} must be three upper-case letters, e.g. 'USD'",
                object_type=pt,
                object_id=pid,
                field="currency",
            )
        unit = _enum_field(WorkUnit, self.cost_report_unit, "cost_report_unit", pt, pid)
        object.__setattr__(self, "cost_report_unit", unit)
        if not isinstance(self.calendar, Calendar):
            raise TypeError(f"calendar must be a Calendar, got {_type_name(self.calendar)}")

        issues: list[Issue] = []

        def dup_id(object_type: ObjectType) -> Any:
            def make(item: Any) -> Issue:
                return Issue.error(
                    "DUPLICATE_ID",
                    f"{object_type.value} ID {item.id!r} is used more than once",
                    object_type=object_type,
                    object_id=item.id,
                    field="id",
                )

            return make

        def dup_assignment(a: Assignment) -> Issue:
            return Issue.error(
                "DUPLICATE_ASSIGNMENT",
                f"resource {a.resource_id!r} is assigned to task {a.task_id!r} more than once",
                object_type=ObjectType.ASSIGNMENT,
                object_id=a.key_text,
                field="resource_id",
            )

        def by_id(x: Any) -> str:
            return str(x.id)

        def by_key(x: Assignment) -> tuple[str, str]:
            return x.key

        collections = (
            ("nodes", WbsNode, by_id, dup_id(ObjectType.NODE)),
            ("resources", Resource, by_id, dup_id(ObjectType.RESOURCE)),
            ("assignments", Assignment, by_key, dup_assignment),
            ("dependencies", Dependency, by_id, dup_id(ObjectType.DEPENDENCY)),
        )
        for name, cls, key, dup in collections:
            value = getattr(self, name)
            if isinstance(value, str | bytes) or not isinstance(value, Iterable):
                raise TypeError(f"{name} must be a collection, got {_type_name(value)}")
            object.__setattr__(self, name, _sorted_unique(value, cls, key, name, issues, dup))
        if issues:
            raise ValidationFailed(issues)
        object.__setattr__(self, "_index", _ProjectIndex(self))

    @property
    def _idx(self) -> _ProjectIndex:
        index = self._index
        assert index is not None
        return index

    # --- single-object lookups (raise NotFound) ---------------------------------

    def node(self, node_id: str) -> WbsNode:
        """The node with this ID. Raises ``NotFound("node", node_id)``."""
        try:
            return self._idx.nodes_by_id[node_id]
        except KeyError:
            raise NotFound(ObjectType.NODE, node_id) from None

    def resource(self, resource_id: str) -> Resource:
        """The resource with this ID. Raises ``NotFound("resource", resource_id)``."""
        try:
            return self._idx.resources_by_id[resource_id]
        except KeyError:
            raise NotFound(ObjectType.RESOURCE, resource_id) from None

    def dependency(self, dependency_id: str) -> Dependency:
        """The dependency with this ID. Raises ``NotFound("dependency", dependency_id)``."""
        try:
            return self._idx.deps_by_id[dependency_id]
        except KeyError:
            raise NotFound(ObjectType.DEPENDENCY, dependency_id) from None

    def assignment(self, task_id: str, resource_id: str) -> Assignment:
        """The assignment of a resource to a task.

        Raises ``NotFound("assignment", "<task_id>/<resource_id>")``.
        """
        try:
            return self._idx.assign_by_key[(task_id, resource_id)]
        except KeyError:
            raise NotFound(ObjectType.ASSIGNMENT, f"{task_id}/{resource_id}") from None

    def has_node(self, node_id: str) -> bool:
        """Whether a node with this ID exists."""
        return node_id in self._idx.nodes_by_id

    def has_resource(self, resource_id: str) -> bool:
        """Whether a resource with this ID exists."""
        return resource_id in self._idx.resources_by_id

    def has_dependency(self, dependency_id: str) -> bool:
        """Whether a dependency with this ID exists."""
        return dependency_id in self._idx.deps_by_id

    # --- collection lookups (never raise; unknown IDs give ()) -------------------

    def children(self, parent_id: str | None) -> tuple[WbsNode, ...]:
        """Nodes whose ``parent_id`` is ``parent_id`` (``None``: top level), by ``(order, id)``."""
        return self._idx.children.get(parent_id, ())

    def assignments_for(self, task_id: str) -> tuple[Assignment, ...]:
        """Assignments of a task, by ``resource_id``."""
        return self._idx.assign_by_task.get(task_id, ())

    def assignments_for_resource(self, resource_id: str) -> tuple[Assignment, ...]:
        """Assignments of a resource, by ``task_id``."""
        return self._idx.assign_by_resource.get(resource_id, ())

    def dependencies_from(self, pred_id: str) -> tuple[Dependency, ...]:
        """Dependencies whose predecessor is ``pred_id`` (outgoing), by ``id``."""
        return self._idx.deps_by_pred.get(pred_id, ())

    def dependencies_to(self, succ_id: str) -> tuple[Dependency, ...]:
        """Dependencies whose successor is ``succ_id`` (incoming), by ``id``."""
        return self._idx.deps_by_succ.get(succ_id, ())

    def nodes_of_kind(self, kind: NodeKind) -> tuple[WbsNode, ...]:
        """All nodes of one kind, by ``id``."""
        return tuple(n for n in self.nodes if n.kind is kind)

    def wbs_order(self) -> tuple[WbsNode, ...]:
        """All nodes reachable from the top level in WBS display order (pre-order).

        Each parent precedes its children; siblings follow ``(order, id)``. Nodes
        whose parent chain does not reach the top level (dangling or cyclic
        parents, which validation rejects) are omitted.
        """
        seen: set[str] = set()

        def visit(nodes: tuple[WbsNode, ...]) -> Iterator[WbsNode]:
            stack = list(reversed(nodes))
            while stack:
                n = stack.pop()
                if n.id in seen:
                    continue
                seen.add(n.id)
                yield n
                stack.extend(reversed(self.children(n.id)))

        return tuple(visit(self.children(None)))
