"""Working-time axis: maps real datetimes to integer working minutes and back.

The axis unit is the *working minute*.  Minute 0 is the first working instant on
or after the project start date.  Every working day contributes exactly
``calendar.minutes_per_day`` minutes as one continuous block beginning at
``calendar.workday_start``; nonworking days contribute nothing.

Intervals are half-open ``[start, finish)``.  At a day boundary (a multiple of
``minutes_per_day``) the two boundary conventions differ::

    to_datetime(m, "start")  -> next working day's workday_start   (Mon 09:00)
    to_datetime(m, "finish") -> previous working day's day end     (Fri 17:00)

so an FS successor with zero lag starts exactly where its predecessor finishes
on the axis, while displaying naturally.

Design: the list of working dates is built lazily (a growing sorted list of date
ordinals, extended in chunks) and queried with :mod:`bisect`, so conversions are
O(log n) and projects spanning decades are cheap.

This module is pure: only the stdlib and ``project_planner.engine`` are used.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left
from typing import Literal

from project_planner.engine.errors import Issue, Severity
from project_planner.engine.model import Calendar, ExceptionKind

__all__ = ["WorkingAxis"]

_CHUNK_DAYS = 366
# Safety cap on how far the lazy scan may look beyond the origin (days).
_MAX_SCAN_DAYS = 366 * 9000
_MAX_ORDINAL = dt.date.max.toordinal()


class WorkingAxis:
    """Bidirectional mapping between datetimes and working minutes.

    Args:
        calendar: A validated :class:`~project_planner.engine.model.Calendar`.
        start: The project start date.  If it is not a working day, the axis
            origin moves to the next working day's ``workday_start`` and an info
            :class:`Issue` (``CAL_START_MOVED``) is recorded in :attr:`issues`.

    Raises:
        OverflowError: if no working day can be found (practically impossible).
    """

    def __init__(self, calendar: Calendar, start: dt.date) -> None:
        self._calendar = calendar
        self._mpd = calendar.minutes_per_day
        self._ws_min = calendar.workday_start_minute
        self._weekdays = frozenset(w.python_weekday for w in calendar.working_weekdays)
        self._holidays = frozenset(h.date for h in calendar.holidays)
        self._exc: dict[dt.date, ExceptionKind] = {e.date: e.kind for e in calendar.exceptions}
        self._start = start
        self._days: list[int] = []  # ordinals of working days, ascending
        self._scanned = start.toordinal() - 1  # last ordinal examined
        self._origin_limit = start.toordinal() + _MAX_SCAN_DAYS

        self._extend_until_len(1)
        origin = dt.date.fromordinal(self._days[0])
        self.issues: tuple[Issue, ...] = ()
        if origin != start:
            self.issues = (
                Issue(
                    Severity.INFO,
                    "CAL_START_MOVED",
                    f"Project start {start.isoformat()} is not a working day; "
                    f"moved to {origin.isoformat()}",
                    object_type="project",
                    field="start",
                ),
            )

    # ------------------------------------------------------------------ basics

    @property
    def minutes_per_day(self) -> int:
        """Working minutes per working day."""
        return self._mpd

    @property
    def origin(self) -> dt.date:
        """The date of axis minute 0 (first working day on or after the start)."""
        return dt.date.fromordinal(self._days[0])

    def is_working_day(self, d: dt.date) -> bool:
        """Whether ``d`` is a working day under the calendar (independent of the origin)."""
        kind = self._exc.get(d)
        if kind is ExceptionKind.WORKING:
            return True
        if kind is ExceptionKind.NONWORKING or d in self._holidays:
            return False
        return d.weekday() in self._weekdays

    # --------------------------------------------------------- lazy day lookup

    def _extend(self) -> None:
        """Scan another chunk of days, appending working ones."""
        if self._scanned >= self._origin_limit or self._scanned >= _MAX_ORDINAL:
            raise OverflowError("working-time axis cannot be extended any further")
        stop = min(self._scanned + _CHUNK_DAYS, _MAX_ORDINAL)
        for o in range(self._scanned + 1, stop + 1):
            if self.is_working_day(dt.date.fromordinal(o)):
                self._days.append(o)
        self._scanned = stop

    def _extend_until_len(self, n: int) -> None:
        while len(self._days) < n:
            self._extend()

    def _extend_until_ordinal(self, o: int) -> None:
        while self._scanned < o:
            self._extend()

    # ------------------------------------------------------------- day lookups

    def day_index(self, minute: int) -> int:
        """Index of the working day containing ``minute`` (0 = origin day).

        Raises:
            ValueError: if ``minute`` is negative.
        """
        if minute < 0:
            raise ValueError(f"minute must be >= 0, got {minute}")
        return minute // self._mpd

    def working_date(self, index: int) -> dt.date:
        """The date of the ``index``-th working day (0 = origin day).

        Raises:
            ValueError: if ``index`` is negative.
        """
        if index < 0:
            raise ValueError(f"day index must be >= 0, got {index}")
        self._extend_until_len(index + 1)
        return dt.date.fromordinal(self._days[index])

    def _index_on_or_after(self, d: dt.date) -> tuple[int, bool]:
        """Index of the first working day >= ``d`` and whether it is ``d`` itself."""
        o = d.toordinal()
        if o <= self._days[0]:
            return 0, self._days[0] == o
        self._extend_until_ordinal(o)
        i = bisect_left(self._days, o)
        self._extend_until_len(i + 1)
        return i, self._days[i] == o

    def first_working_minute(self, d: dt.date) -> int:
        """Axis minute at the start of ``d``'s working block.

        For a nonworking date this is the next working day's start.  Dates before
        the origin give 0.
        """
        return self._index_on_or_after(d)[0] * self._mpd

    # ------------------------------------------------------------- conversions

    def to_axis(self, value: dt.datetime) -> int:
        """Map a datetime to an axis minute.

        Rules: on a working day, times before the workday clamp to its start and
        times after its end clamp to its end (= the next working day's start
        minute); on a nonworking day the result is the next working day's start
        minute; anything before the axis origin clamps to 0.
        """
        idx, exact = self._index_on_or_after(value.date())
        base = idx * self._mpd
        if not exact:
            return base
        # Sub-minute precision is rounded up so that to_axis never under-reports.
        sub_minute = 1 if value.second or value.microsecond else 0
        minute_of_day = value.hour * 60 + value.minute + sub_minute
        offset = min(max(minute_of_day - self._ws_min, 0), self._mpd)
        return base + offset

    def to_datetime(self, minute: int, boundary: Literal["start", "finish"]) -> dt.datetime:
        """Map an axis minute to a datetime.

        Args:
            minute: Axis minute, >= 0.
            boundary: ``"start"`` for the start of an interval, ``"finish"`` for
                its end.  They differ only on a day boundary: ``"start"`` gives
                the next working day's ``workday_start``, ``"finish"`` the
                previous working day's end.  Minute 0 with ``"finish"`` has no
                previous day, so it gives the origin's start instant.

        Raises:
            ValueError: if ``minute`` is negative or ``boundary`` is unknown.
        """
        if boundary not in ("start", "finish"):
            raise ValueError(f"boundary must be 'start' or 'finish', got {boundary!r}")
        if minute < 0:
            raise ValueError(f"minute must be >= 0, got {minute}")
        idx, offset = divmod(minute, self._mpd)
        if boundary == "finish" and offset == 0 and idx > 0:
            idx -= 1
            offset = self._mpd
        day = self.working_date(idx)
        return dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=self._ws_min + offset)

    # ------------------------------------------------------------------ ranges

    def nonworking_ranges(
        self, dt_from: dt.datetime, dt_to: dt.datetime
    ) -> list[tuple[dt.date, dt.date]]:
        """Inclusive date ranges of nonworking days overlapping the window.

        Both end dates (``dt_from.date()`` and ``dt_to.date()``) are included;
        consecutive nonworking days are merged into one range.  Independent of
        the axis origin (days before the project start are judged by the calendar).
        """
        ranges: list[tuple[dt.date, dt.date]] = []
        one = dt.timedelta(days=1)
        day = dt_from.date()
        last = dt_to.date()
        run_start: dt.date | None = None
        while day <= last:
            if self.is_working_day(day):
                if run_start is not None:
                    ranges.append((run_start, day - one))
                    run_start = None
            elif run_start is None:
                run_start = day
            day += one
        if run_start is not None:
            ranges.append((run_start, last))
        return ranges
