"""CSV import / export of a project definition (task T20; format: ``docs/csv_format.md``).

Public API
----------
* :func:`parse` - read a whole CSV text (``str``, ``bytes`` or a text stream) into an
  :class:`ImportOutcome` (the :class:`~project_planner.engine.model.Project` plus
  informational notes), or raise :class:`~project_planner.engine.errors.ImportFailed`
  carrying **every** error. Nothing is built before the whole input has been read and
  checked. Phase 1 reports per-row format errors; phase 2 reports cross-record model
  errors (it always runs, and skips only checks that need a row that failed phase 1).
  Phase 2 reuses :func:`project_planner.engine.validation.validate` and the engine's
  graph code, mapping engine codes to ``CSV_*`` codes and attributing them to lines.
* :func:`export` - write the full definition (plus ``RESULT_*`` records when a result is
  given) in the deterministic order of the format document, always as UTF-8 text with
  ``\\n`` line endings and no BOM. The caller encodes (``text.encode("utf-8")``).

This module never touches the file system.

Every :class:`~project_planner.engine.errors.Issue` produced by :func:`parse` has
``code`` (a ``CSV_*`` code), ``object_type`` (the record type as written, e.g.
``"NODE"``), ``object_id`` (the row id where the record has one; ``"task/resource"`` for
assignments), ``field`` (the column, ``""`` for whole-row issues) and ``line`` (1-based
physical line of the record start; header = 1).

Imported project id
-------------------
The format has no project id column, so an imported project gets
:data:`IMPORTED_PROJECT_ID`; callers that need another id use ``dataclasses.replace``.

Names must not be blank
-----------------------
``name`` cells of PROJECT, RESOURCE and NODE are required, and a name that is empty
after trimming counts as missing; a blank name therefore does not survive a round trip
(the model allows it, the format does not).

Result protocol for :func:`export` (what task T19's ``ScheduleResult`` must expose)
----------------------------------------------------------------------------------
:func:`export` takes any object matching :class:`ExportableResult`; attribute names are
those of plan sections 2.1/2.2. All ``Decimal`` fields may also be ``int``; ``None``
writes an empty cell.

``ExportableResult``:
  ``complete: bool``, ``kind: str`` (``"dependency_only"`` / ``"leveled"``; a ``StrEnum``
  is fine), ``project_start`` / ``project_finish: datetime | None``,
  ``working_span_days: Decimal | None``, ``elapsed_span_calendar_days: Decimal | None``,
  ``effort_days: Decimal | None``, ``work_qty: Decimal | None``, ``work_unit: str | None``,
  ``cost_total: Decimal | None``, ``cost_complete: bool``,
  ``node_results: Sequence[NodeResultRow]``,
  ``assignment_results: Sequence[AssignmentResultRow]``.

``NodeResultRow``:
  ``node_id: str``, ``start`` / ``finish: datetime | None``, ``duration_days``,
  ``effort_days``, ``leveling_delay_days``, ``work_qty``, ``cost`` (all
  ``Decimal | None``), ``work_unit: str | None``, ``cost_complete: bool``,
  ``status: str`` (``"scheduled"`` / ``"unscheduled"``), ``issue_codes: Sequence[str]``.

``AssignmentResultRow``:
  ``task_id: str``, ``resource_id: str``, ``assignment_days``, ``work_qty``,
  ``rate_per_unit``, ``cost`` (all ``Decimal | None``), ``work_unit: str | None``,
  ``cost_complete: bool``.

``RESULT_PROJECT.schedule_status`` is ``stale`` when ``stale=True`` (takes precedence),
else ``incomplete`` when ``result.complete`` is false, else ``current``. Nodes and
assignments of the project without a result row get ``unscheduled`` /
empty-valued rows with ``cost_complete=false``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Protocol, TextIO, TypeVar

from project_planner.engine import config as _cfg
from project_planner.engine.config import DEFAULT_CONFIG
from project_planner.engine.errors import ImportFailed, Issue, Severity
from project_planner.engine.model import (
    Assignment,
    Calendar,
    CalendarException,
    CalendarSettings,
    Dependency,
    DependencyType,
    ExceptionKind,
    Holiday,
    NodeKind,
    Project,
    Resource,
    SizingMode,
    TimeQty,
    TimeUnit,
    WbsNode,
    Weekday,
    WorkUnit,
)
from project_planner.engine.network import DirectedGraph
from project_planner.engine.validation import validate

__all__ = [
    "COLUMNS",
    "IMPORTED_PROJECT_ID",
    "SCHEMA_VERSION",
    "AssignmentResultRow",
    "ExportableResult",
    "ImportOutcome",
    "NodeResultRow",
    "export",
    "parse",
]

SCHEMA_VERSION = "1"
IMPORTED_PROJECT_ID = "p1"
"""``Project.id`` given to every imported project (the format has no project id)."""

COLUMNS: tuple[str, ...] = (
    "record_type", "schema_version", "id", "name", "start_date", "currency",
    "cost_report_unit", "hours_per_day", "working_days_per_year", "working_weekdays",
    "workday_start", "date", "exception_kind", "hourly_rate", "node_kind", "parent_id",
    "sibling_order", "sizing_mode", "sizing_value", "sizing_unit", "task_id",
    "resource_id", "percent", "pred_id", "succ_id", "dep_type", "lag_value", "lag_unit",
    "schedule_status", "schedule_kind", "start", "finish", "working_span_days",
    "elapsed_span_calendar_days", "duration_days", "effort_days", "leveling_delay_days",
    "assignment_days", "work_qty", "work_unit", "rate_per_unit", "cost", "cost_complete",
    "node_status", "issue_codes",
)  # fmt: skip
"""The 45 columns, in the order the exporter writes them."""

_E = TypeVar("_E", NodeKind, DependencyType, ExceptionKind)
_T = TypeVar("_T", "_ResourceRow", "_NodeRow", "_DepRow")

_COL_INDEX = {c: i for i, c in enumerate(COLUMNS)}

_RESULT_TYPES = ("RESULT_PROJECT", "RESULT_NODE", "RESULT_ASSIGNMENT")

# Column usage per definition record type. R required, O optional, C conditional;
# every column not listed must be empty.
_SPEC: dict[str, dict[str, str]] = {
    "PROJECT": {"name": "R", "start_date": "R", "currency": "O", "cost_report_unit": "O"},
    "CALENDAR": {
        "hours_per_day": "O",
        "working_days_per_year": "O",
        "working_weekdays": "O",
        "workday_start": "O",
    },
    "HOLIDAY": {"name": "O", "date": "R"},
    "EXCEPTION": {"name": "O", "date": "R", "exception_kind": "R"},
    "RESOURCE": {"id": "R", "name": "R", "hourly_rate": "O"},
    "NODE": {
        "id": "R",
        "name": "R",
        "node_kind": "R",
        "parent_id": "O",
        "sibling_order": "R",
        "sizing_mode": "C",
        "sizing_value": "C",
        "sizing_unit": "C",
    },  # fmt: skip
    "ASSIGNMENT": {"task_id": "R", "resource_id": "R", "percent": "R"},
    "DEPENDENCY": {
        "id": "R",
        "pred_id": "R",
        "succ_id": "R",
        "dep_type": "R",
        "lag_value": "C",
        "lag_unit": "C",
    },  # fmt: skip
}
_NO_CHECK_COLUMNS = frozenset({"record_type", "schema_version"})

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_DECIMAL_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_INT_RE = re.compile(r"-?[0-9]+")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """Result of a successful :func:`parse`.

    Attributes:
        project: The imported definition (``id`` is :data:`IMPORTED_PROJECT_ID`).
        notes: Informational issues (``CSV_DEFAULT_APPLIED``, ``CSV_RESULTS_IGNORED``).
    """

    project: Project
    notes: tuple[Issue, ...] = ()


# =============================================================================
# Result protocols (export)
# =============================================================================


class NodeResultRow(Protocol):
    """Per-node result values written to ``RESULT_NODE`` (see module docstring)."""

    @property
    def node_id(self) -> str: ...
    @property
    def start(self) -> dt.datetime | None: ...
    @property
    def finish(self) -> dt.datetime | None: ...
    @property
    def duration_days(self) -> Decimal | None: ...
    @property
    def effort_days(self) -> Decimal | None: ...
    @property
    def leveling_delay_days(self) -> Decimal | None: ...
    @property
    def work_qty(self) -> Decimal | None: ...
    @property
    def work_unit(self) -> str | None: ...
    @property
    def cost(self) -> Decimal | None: ...
    @property
    def cost_complete(self) -> bool: ...
    @property
    def status(self) -> str: ...
    @property
    def issue_codes(self) -> Sequence[str]: ...


class AssignmentResultRow(Protocol):
    """Per-assignment result values written to ``RESULT_ASSIGNMENT``."""

    @property
    def task_id(self) -> str: ...
    @property
    def resource_id(self) -> str: ...
    @property
    def assignment_days(self) -> Decimal | None: ...
    @property
    def work_qty(self) -> Decimal | None: ...
    @property
    def work_unit(self) -> str | None: ...
    @property
    def rate_per_unit(self) -> Decimal | None: ...
    @property
    def cost(self) -> Decimal | None: ...
    @property
    def cost_complete(self) -> bool: ...


class ExportableResult(Protocol):
    """What :func:`export` needs from a schedule result (see module docstring)."""

    @property
    def complete(self) -> bool: ...
    @property
    def kind(self) -> str: ...
    @property
    def project_start(self) -> dt.datetime | None: ...
    @property
    def project_finish(self) -> dt.datetime | None: ...
    @property
    def working_span_days(self) -> Decimal | None: ...
    @property
    def elapsed_span_calendar_days(self) -> Decimal | None: ...
    @property
    def effort_days(self) -> Decimal | None: ...
    @property
    def work_qty(self) -> Decimal | None: ...
    @property
    def work_unit(self) -> str | None: ...
    @property
    def cost_total(self) -> Decimal | None: ...
    @property
    def cost_complete(self) -> bool: ...
    @property
    def node_results(self) -> Sequence[NodeResultRow]: ...
    @property
    def assignment_results(self) -> Sequence[AssignmentResultRow]: ...


# =============================================================================
# Tokenizer
# =============================================================================


def _tokenize(text: str) -> list[tuple[int, list[str]]]:
    """Split RFC 4180 text into ``(start_line, cells)`` records (lenient, never raises)."""
    records: list[tuple[int, list[str]]] = []
    row: list[str] = []
    buf: list[str] = []
    line = 1
    start_line = 1
    in_quotes = False
    quoted = False  # current field was opened with a quote
    started = False  # something was consumed in the current record
    i, n = 0, len(text)

    def end_record() -> None:
        nonlocal row, buf, quoted, started, start_line
        row.append("".join(buf))
        records.append((start_line, row))
        row, buf, quoted, started = [], [], False, False
        start_line = line

    while i < n:
        ch = text[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_quotes = False
            else:
                buf.append(ch)
                if ch == "\n" or (ch == "\r" and not (i + 1 < n and text[i + 1] == "\n")):
                    line += 1
            i += 1
            continue
        started = True
        if ch == '"' and not buf and not quoted:
            in_quotes = quoted = True
        elif ch == ",":
            row.append("".join(buf))
            buf, quoted = [], False
        elif ch in "\r\n":
            if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 1
            line += 1
            end_record()
        else:
            buf.append(ch)
        i += 1
    if started or row or buf or in_quotes:
        end_record()
    return records


def _trim(value: str) -> str:
    return value.strip(" \t")


# =============================================================================
# Import: parsed rows
# =============================================================================


@dataclass
class _Row:
    line: int
    rt: str
    c: dict[str, str]
    oid: str | None = None


@dataclass
class _ProjectRow:
    line: int
    name: str
    start: dt.date | None
    currency: str
    unit: WorkUnit


@dataclass
class _CalendarRow:
    line: int
    hours: Decimal | None
    days: int | None
    weekdays: frozenset[Weekday] | None
    start: dt.time | None


@dataclass
class _DateRow:
    line: int
    kind: str  # "HOLIDAY" | "EXCEPTION"
    date: dt.date
    name: str
    exc_kind: ExceptionKind | None


@dataclass
class _ResourceRow:
    line: int
    id: str
    name: str
    rate: Decimal | None


@dataclass
class _NodeRow:
    line: int
    id: str
    name: str
    kind: NodeKind | None
    parent: str | None
    order: int | None
    mode: SizingMode
    sizing: TimeQty | None
    ok: bool


@dataclass
class _AssignRow:
    line: int
    task: str
    res: str
    percent: Decimal


@dataclass
class _DepRow:
    line: int
    id: str
    pred: str
    succ: str
    type: DependencyType
    lag: TimeQty


@dataclass
class _State:
    errors: list[Issue] = field(default_factory=list)
    notes: list[Issue] = field(default_factory=list)
    project_rows: list[_ProjectRow] = field(default_factory=list)
    calendar_rows: list[_CalendarRow] = field(default_factory=list)
    date_rows: list[_DateRow] = field(default_factory=list)
    resources: list[_ResourceRow] = field(default_factory=list)
    nodes: list[_NodeRow] = field(default_factory=list)
    assigns: list[_AssignRow] = field(default_factory=list)
    deps: list[_DepRow] = field(default_factory=list)
    seen_types: set[str] = field(default_factory=set)
    result_counts: dict[str, int] = field(default_factory=dict)
    first_result_line: int | None = None


def _err(
    st: _State, row: _Row, code: str, column: str, message: str, oid: str | None = None
) -> None:
    st.errors.append(
        Issue.error(
            code,
            message,
            object_type=row.rt,
            object_id=oid if oid is not None else row.oid,
            field=column,
            line=row.line,
        )
    )


def _default_note(st: _State, row: _Row, column: str, value: str) -> None:
    st.notes.append(
        Issue.info(
            "CSV_DEFAULT_APPLIED",
            f"{row.rt} line {row.line}: {column} omitted, default {value!r} applied",
            object_type=row.rt,
            object_id=row.oid,
            field=column,
            line=row.line,
        )
    )


# --- value parsers (return None after reporting) ---------------------------------


def _p_date(st: _State, row: _Row, col: str) -> dt.date | None:
    v = row.c[col]
    if _DATE_RE.fullmatch(v):
        try:
            return dt.date(int(v[:4]), int(v[5:7]), int(v[8:10]))
        except ValueError:
            pass
    _err(st, row, "CSV_BAD_DATE", col, f"{col} {v!r} is not a real date YYYY-MM-DD")
    return None


def _p_decimal(st: _State, row: _Row, col: str, *, negative: bool) -> Decimal | None:
    v = row.c[col]
    if _DECIMAL_RE.fullmatch(v) and (negative or not v.startswith("-")):
        return Decimal(v)
    what = "a plain decimal number" if negative else "a plain non-negative decimal number"
    _err(st, row, "CSV_BAD_NUMBER", col, f"{col} {v!r} is not {what}")
    return None


def _p_int(st: _State, row: _Row, col: str) -> int | None:
    v = row.c[col]
    if _INT_RE.fullmatch(v):
        return int(v)
    _err(st, row, "CSV_BAD_NUMBER", col, f"{col} {v!r} is not a whole number")
    return None


def _p_unit(st: _State, row: _Row, col: str) -> TimeUnit | None:
    v = row.c[col]
    if v in ("h", "d"):
        return TimeUnit.from_symbol(v)
    _err(st, row, "CSV_BAD_UNIT", col, f"{col} {v!r} must be 'h' or 'd'")
    return None


def _p_id(st: _State, row: _Row, col: str) -> str | None:
    """A required or optional reference/ID cell; None if empty or unusable (reported)."""
    v = row.c[col]
    if v == "":
        return None
    if v != v.strip():
        _err(st, row, "CSV_BAD_ID", col, f"{col} {v!r} has leading or trailing whitespace")
        return None
    return v


def _p_enum(st: _State, row: _Row, col: str, enum_cls: type[_E]) -> _E | None:
    v = row.c[col]
    try:
        return enum_cls(v)
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        _err(st, row, "CSV_BAD_ENUM", col, f"{col} {v!r} is not one of: {allowed}")
        return None


def _p_weekdays(st: _State, row: _Row) -> frozenset[Weekday] | None:
    v = row.c["working_weekdays"]
    result: set[Weekday] = set()
    for token in v.split(";"):
        try:
            if token.strip() == "":
                raise ValueError("empty token")
            wd = Weekday.parse(token)
        except ValueError:
            _err(
                st, row, "CSV_BAD_ENUM", "working_weekdays",
                f"working_weekdays {v!r}: bad token {token!r} (use Mon;Tue;...;Sun)",
            )  # fmt: skip
            return None
        if wd in result:
            _err(
                st, row, "CSV_BAD_ENUM", "working_weekdays",
                f"working_weekdays {v!r}: {wd.value} appears more than once",
            )  # fmt: skip
            return None
        result.add(wd)
    return frozenset(result)


# --- row checks shared by all definition types ------------------------------------


def _check_columns(st: _State, row: _Row) -> None:
    spec = _SPEC[row.rt]
    for col in COLUMNS:
        if col in _NO_CHECK_COLUMNS:
            continue
        kind = spec.get(col, "-")
        v = row.c[col]
        if kind == "-":
            if v != "":
                _err(st, row, "CSV_NOT_EMPTY", col, f"{col} must be empty on {row.rt}, got {v!r}")
        elif kind == "R" and (v == "" or (col == "name" and _trim(v) == "")):
            _err(st, row, "CSV_MISSING_REQUIRED", col, f"{col} is required on {row.rt}")


# --- per-type parsing ---------------------------------------------------------------


def _parse_project(st: _State, row: _Row) -> None:
    c = row.c
    start = _p_date(st, row, "start_date") if c["start_date"] else None
    currency = _cfg.DEFAULT_CURRENCY
    if c["currency"]:
        if _CURRENCY_RE.fullmatch(c["currency"]):
            currency = c["currency"]
        else:
            _err(
                st, row, "CSV_BAD_CURRENCY", "currency",
                f"currency {c['currency']!r} must be three upper-case letters",
            )  # fmt: skip
    else:
        _default_note(st, row, "currency", currency)
    unit = WorkUnit(_cfg.DEFAULT_COST_REPORT_UNIT)
    if c["cost_report_unit"]:
        try:
            unit = WorkUnit(c["cost_report_unit"])
        except ValueError:
            _err(
                st, row, "CSV_BAD_ENUM", "cost_report_unit",
                f"cost_report_unit {c['cost_report_unit']!r} is not one of: "
                + ", ".join(m.value for m in WorkUnit),
            )  # fmt: skip
    else:
        _default_note(st, row, "cost_report_unit", unit.value)
    st.project_rows.append(_ProjectRow(row.line, c["name"], start, currency, unit))


def _parse_calendar(st: _State, row: _Row) -> None:
    c = row.c
    hours: Decimal | None = None
    days: int | None = None
    weekdays: frozenset[Weekday] | None = None
    start: dt.time | None = None

    if c["hours_per_day"]:
        v = _p_decimal(st, row, "hours_per_day", negative=True)
        if v is not None:
            num, den = v.as_integer_ratio()
            if not (0 < v <= 24) or (num * 60) % den != 0:
                _err(
                    st, row, "CSV_OUT_OF_RANGE", "hours_per_day",
                    f"hours_per_day {c['hours_per_day']} must be > 0, <= 24 and a whole "
                    f"number of minutes",
                )  # fmt: skip
            else:
                hours = v
    else:
        hours = _cfg.DEFAULT_HOURS_PER_DAY
        _default_note(st, row, "hours_per_day", str(hours))

    if c["working_days_per_year"]:
        iv = _p_int(st, row, "working_days_per_year")
        if iv is not None:
            if 1 <= iv <= 366:
                days = iv
            else:
                _err(
                    st, row, "CSV_OUT_OF_RANGE", "working_days_per_year",
                    f"working_days_per_year {iv} must be in 1..366",
                )  # fmt: skip
    else:
        days = _cfg.DEFAULT_WORKING_DAYS_PER_YEAR
        _default_note(st, row, "working_days_per_year", str(days))

    if c["working_weekdays"]:
        weekdays = _p_weekdays(st, row)
    else:
        weekdays = frozenset(Weekday(w) for w in _cfg.DEFAULT_WORKING_WEEKDAYS)
        _default_note(st, row, "working_weekdays", ";".join(_cfg.DEFAULT_WORKING_WEEKDAYS))

    if c["workday_start"]:
        v_ = c["workday_start"]
        if _TIME_RE.fullmatch(v_):
            start = dt.time(int(v_[:2]), int(v_[3:5]))
        else:
            _err(st, row, "CSV_BAD_TIME", "workday_start", f"workday_start {v_!r} is not HH:MM")
    else:
        start = _cfg.DEFAULT_WORKDAY_START
        _default_note(st, row, "workday_start", f"{start:%H:%M}")
    st.calendar_rows.append(_CalendarRow(row.line, hours, days, weekdays, start))


def _parse_dated(st: _State, row: _Row) -> None:
    d = _p_date(st, row, "date") if row.c["date"] else None
    exc: ExceptionKind | None = None
    if row.rt == "EXCEPTION" and row.c["exception_kind"]:
        exc = _p_enum(st, row, "exception_kind", ExceptionKind)
    if d is not None and (row.rt == "HOLIDAY" or exc is not None):
        st.date_rows.append(_DateRow(row.line, row.rt, d, row.c["name"], exc))


def _parse_resource(st: _State, row: _Row) -> None:
    rid = _p_id(st, row, "id")
    rate: Decimal | None = None
    if row.c["hourly_rate"]:
        rate = _p_decimal(st, row, "hourly_rate", negative=False)
    if rid is not None:
        st.resources.append(_ResourceRow(row.line, rid, row.c["name"], rate))


def _parse_sizing(st: _State, row: _Row) -> tuple[SizingMode, TimeQty | None, bool]:
    c = row.c
    mode_t, val_t, unit_t = c["sizing_mode"], c["sizing_value"], c["sizing_unit"]
    ok = True
    mode = SizingMode.NONE
    if mode_t:
        if mode_t in ("duration", "effort"):
            mode = SizingMode(mode_t)
        else:
            ok = False
            _err(
                st, row, "CSV_BAD_ENUM", "sizing_mode",
                f"sizing_mode {mode_t!r} is not one of: duration, effort",
            )  # fmt: skip
    value: Decimal | None = None
    unit: TimeUnit | None = None
    if val_t:
        value = _p_decimal(st, row, "sizing_value", negative=False)
        ok &= value is not None
        if unit_t:
            unit = _p_unit(st, row, "sizing_unit")
            ok &= unit is not None
        else:
            ok = False
            _err(
                st, row, "CSV_UNITLESS_TIME", "sizing_unit",
                f"sizing_value {val_t!r} has no sizing_unit (use h or d)",
            )  # fmt: skip
    elif unit_t:
        ok = False
        _err(
            st,
            row,
            "CSV_MISSING_REQUIRED",
            "sizing_value",
            "sizing_unit given without sizing_value",
        )
    if (val_t or unit_t) and not mode_t:
        ok = False
        _err(st, row, "CSV_MISSING_REQUIRED", "sizing_mode", "sizing_mode is required with a size")
    if mode_t and not val_t:
        ok = False
        _err(
            st,
            row,
            "CSV_MISSING_REQUIRED",
            "sizing_value",
            "sizing_value is required with sizing_mode",
        )
        if not unit_t:
            _err(
                st,
                row,
                "CSV_MISSING_REQUIRED",
                "sizing_unit",
                "sizing_unit is required with sizing_mode",
            )
    if not ok or value is None or unit is None or mode is SizingMode.NONE:
        return SizingMode.NONE, None, ok
    return mode, TimeQty(value, unit), True


def _parse_node(st: _State, row: _Row) -> None:
    c = row.c
    nid = _p_id(st, row, "id")
    kind = _p_enum(st, row, "node_kind", NodeKind) if c["node_kind"] else None
    parent = _p_id(st, row, "parent_id")
    order = _p_int(st, row, "sibling_order") if c["sibling_order"] else None
    ok = kind is not None and order is not None and not _row_has_errors(st, row)
    mode, sizing = SizingMode.NONE, None
    if kind is NodeKind.TASK:
        mode, sizing, sizing_ok = _parse_sizing(st, row)
        ok = ok and sizing_ok
    if kind in (NodeKind.GROUP, NodeKind.MILESTONE):
        for col in ("sizing_mode", "sizing_value", "sizing_unit"):
            if c[col]:
                ok = False
                _err(st, row, "CSV_NOT_EMPTY", col, f"{col} must be empty on a {kind.value}")
    if nid is not None:
        st.nodes.append(_NodeRow(row.line, nid, c["name"], kind, parent, order, mode, sizing, ok))


def _row_has_errors(st: _State, row: _Row) -> bool:
    return any(e.line == row.line for e in st.errors)


def _parse_assignment(st: _State, row: _Row) -> None:
    before = len(st.errors)
    task = _p_id(st, row, "task_id")
    res = _p_id(st, row, "resource_id")
    percent: Decimal | None = None
    if row.c["percent"]:
        percent = _p_decimal(st, row, "percent", negative=True)
        if percent is not None and not 0 < percent <= _cfg.MAX_ASSIGNMENT_PERCENT:
            _err(
                st, row, "CSV_OUT_OF_RANGE", "percent",
                f"percent {row.c['percent']} must be > 0 and <= {_cfg.MAX_ASSIGNMENT_PERCENT}",
            )  # fmt: skip
            percent = None
    if task is not None and res is not None and percent is not None and len(st.errors) == before:
        st.assigns.append(_AssignRow(row.line, task, res, percent))


def _parse_dependency(st: _State, row: _Row) -> None:
    c = row.c
    before = len(st.errors)
    did = _p_id(st, row, "id")
    pred = _p_id(st, row, "pred_id")
    succ = _p_id(st, row, "succ_id")
    dep_type = _p_enum(st, row, "dep_type", DependencyType) if c["dep_type"] else None
    lag: TimeQty | None = None
    if c["lag_value"] or c["lag_unit"]:
        value: Decimal | None = None
        unit: TimeUnit | None = None
        if c["lag_value"]:
            value = _p_decimal(st, row, "lag_value", negative=True)
            if c["lag_unit"]:
                unit = _p_unit(st, row, "lag_unit")
            else:
                _err(
                    st, row, "CSV_UNITLESS_TIME", "lag_unit",
                    f"lag_value {c['lag_value']!r} has no lag_unit (use h or d)",
                )  # fmt: skip
        else:
            _err(st, row, "CSV_MISSING_REQUIRED", "lag_value", "lag_unit given without lag_value")
        if value is not None and unit is not None:
            lag = TimeQty(value, unit)
    else:
        lag = TimeQty(Decimal(0), TimeUnit.DAYS)
        _default_note(st, row, "lag_value", "0 d")
    if (
        did is not None and pred is not None and succ is not None
        and dep_type is not None and lag is not None and len(st.errors) == before
    ):  # fmt: skip
        st.deps.append(_DepRow(row.line, did, pred, succ, dep_type, lag))


# =============================================================================
# Import: driver
# =============================================================================


def _read_text(source: TextIO | str | bytes) -> tuple[str | None, list[Issue]]:
    try:
        if isinstance(source, str):
            text = source
        elif isinstance(source, bytes | bytearray):
            text = bytes(source).decode("utf-8")
        else:
            data = source.read()
            text = data.decode("utf-8") if isinstance(data, bytes | bytearray) else data
    except UnicodeDecodeError as exc:
        return None, [
            Issue.error(
                "CSV_BAD_ENCODING",
                f"input is not valid UTF-8: {exc.reason}",
                object_type="",
                field="",
                line=1,
            )  # fmt: skip
        ]
    if text.startswith("﻿"):
        text = text[1:]
    return text, []


def parse(source: TextIO | str | bytes) -> ImportOutcome:
    """Parse CSV text (``docs/csv_format.md``) into a project.

    Args:
        source: The whole file as ``str``, UTF-8 ``bytes`` or a readable text stream.
            A leading BOM is accepted and stripped.

    Returns:
        :class:`ImportOutcome` with the project and informational notes.

    Raises:
        ImportFailed: with every error found (phase 1 and phase 2), sorted by line then
            column order; nothing is built.
    """
    text, enc_errors = _read_text(source)
    if text is None:
        raise ImportFailed(enc_errors)
    st = _State()
    stop = _phase1(st, text)
    if stop:
        raise ImportFailed(st.errors)
    project = _phase2(st)
    if st.errors:
        raise ImportFailed(
            sorted(st.errors, key=lambda e: (e.line or 0, _COL_INDEX.get(e.field or "", -1)))
        )
    assert project is not None
    if st.result_counts:
        parts = ", ".join(f"{n} {t}" for t, n in st.result_counts.items())
        st.notes.append(
            Issue.info(
                "CSV_RESULTS_IGNORED",
                f"result records ignored: {parts}",
                line=st.first_result_line,
            )
        )
    return ImportOutcome(project, tuple(st.notes))


def _header_error(st: _State, line: int, message: str, column: str = "") -> bool:
    st.errors.append(
        Issue.error("CSV_MISSING_HEADER", message, object_type="", field=column, line=line)
    )
    return True


def _phase1(st: _State, text: str) -> bool:
    """Read rows and report format errors. Returns True if import must stop at once."""
    records = _tokenize(text)
    if not text:
        return _header_error(st, 0, "the file is empty (no header row)")
    first = next((r for r in records if any(_trim(x) for x in r[1])), None)
    if first is None:
        return _header_error(st, 1, "the file has no header row")
    header_line, header_cells = first
    header = [_trim(h) for h in header_cells]
    missing = [c for c in ("record_type", "schema_version") if c not in header]
    if missing:
        return _header_error(st, header_line, f"header lacks required column(s): {missing}")
    index: dict[str, int] = {}
    unknown: list[str] = []
    for i, name in enumerate(header):
        if name in _COL_INDEX and name not in index:
            index[name] = i
        else:
            unknown.append(name)
    if unknown:
        st.errors.append(
            Issue.error(
                "CSV_UNKNOWN_COLUMN",
                f"unknown or repeated column(s) in header: {', '.join(repr(u) for u in unknown)}",
                object_type="",
                field=unknown[0],
                line=header_line,
            )  # fmt: skip
        )
    width = len(header)
    seen_project = seen_calendar = False

    for line, cells in records:
        if line <= header_line:
            continue
        if all(_trim(x) == "" for x in cells):
            continue
        if len(cells) != width:
            st.errors.append(
                Issue.error(
                    "CSV_BAD_COLUMN_COUNT",
                    f"row has {len(cells)} cells, header has {width}",
                    object_type=_trim(cells[index["record_type"]])
                    if len(cells) > index["record_type"]
                    else "",
                    field="",
                    line=line,
                )  # fmt: skip
            )
            continue
        c = {col: "" for col in COLUMNS}
        for col, i in index.items():
            c[col] = cells[i] if col == "name" else _trim(cells[i])
        rt = c["record_type"]
        version = c["schema_version"]
        if version not in ("", SCHEMA_VERSION):
            st.errors = [
                Issue.error(
                    "CSV_UNKNOWN_SCHEMA_VERSION",
                    f"schema_version {version!r} is not supported (expected {SCHEMA_VERSION})",
                    object_type=rt,
                    field="schema_version",
                    line=line,
                )  # fmt: skip
            ]
            return True
        row = _Row(line, rt, c)
        if rt in _RESULT_TYPES:
            if version == "":
                _err(
                    st, row, "CSV_MISSING_REQUIRED", "schema_version", "schema_version is required"
                )
            st.result_counts[rt] = st.result_counts.get(rt, 0) + 1
            if st.first_result_line is None:
                st.first_result_line = line
            continue
        if rt == "":
            _err(st, row, "CSV_MISSING_REQUIRED", "record_type", "record_type is required")
            continue
        if rt not in _SPEC:
            _err(st, row, "CSV_UNKNOWN_RECORD_TYPE", "record_type", f"unknown record_type {rt!r}")
            continue
        if version == "":
            _err(st, row, "CSV_MISSING_REQUIRED", "schema_version", "schema_version is required")
        st.seen_types.add(rt)
        if rt in ("RESOURCE", "NODE", "DEPENDENCY"):
            row.oid = c["id"] or None
        elif rt == "ASSIGNMENT" and c["task_id"] and c["resource_id"]:
            row.oid = f"{c['task_id']}/{c['resource_id']}"
        if rt == "PROJECT":
            if seen_project:
                _err(st, row, "CSV_DUPLICATE_PROJECT", "record_type", "second PROJECT record")
            seen_project = True
        elif rt == "CALENDAR":
            if seen_calendar:
                _err(st, row, "CSV_DUPLICATE_CALENDAR", "record_type", "second CALENDAR record")
            seen_calendar = True
        _check_columns(st, row)
        _dispatch(st, row)
    return False


def _dispatch(st: _State, row: _Row) -> None:
    rt = row.rt
    if rt == "PROJECT":
        _parse_project(st, row)
    elif rt == "CALENDAR":
        _parse_calendar(st, row)
    elif rt in ("HOLIDAY", "EXCEPTION"):
        _parse_dated(st, row)
    elif rt == "RESOURCE":
        _parse_resource(st, row)
    elif rt == "NODE":
        _parse_node(st, row)
    elif rt == "ASSIGNMENT":
        _parse_assignment(st, row)
    else:
        _parse_dependency(st, row)


# =============================================================================
# Import: phase 2 (model errors)
# =============================================================================


def _model_err(
    st: _State, rt: str, line: int, code: str, column: str, message: str, oid: str | None
) -> None:
    st.errors.append(
        Issue.error(code, message, object_type=rt, object_id=oid, field=column, line=line)
    )


def _dedupe(st: _State, rt: str, rows: list[_T]) -> list[_T]:
    seen: dict[str, int] = {}
    kept: list[_T] = []
    for r in rows:
        if r.id in seen:
            _model_err(
                st, rt, r.line, "CSV_DUPLICATE_ID", "id",
                f"{rt} id {r.id!r} already defined on line {seen[r.id]}", r.id,
            )  # fmt: skip
        else:
            seen[r.id] = r.line
            kept.append(r)
    return kept


def _cycle_path(
    edges: dict[tuple[str, str], int], members: set[str]
) -> tuple[list[str], list[int]]:
    """Shortest cycle through the dependency with the lowest line (BFS inside the SCC).

    Returns the node path ``[a, b, ..., a]`` and the lines of its dependencies.
    """
    inside = {e: ln for e, ln in edges.items() if e[0] in members and e[1] in members}
    start, nxt = min(inside, key=lambda e: (inside[e], e))
    succ: dict[str, list[str]] = {}
    for a, b in sorted(inside):
        succ.setdefault(a, []).append(b)
    prev: dict[str, str] = {}
    queue = [nxt]
    for node in queue:
        if node == start:
            break
        for b in succ.get(node, []):
            if b != nxt and b not in prev:
                prev[b] = node
                queue.append(b)
    back = [start]
    while back[-1] != nxt:
        back.append(prev[back[-1]])
    nodes = [start, *reversed(back)]
    lines = [inside[(x, y)] for x, y in zip(nodes, nodes[1:], strict=False)]
    return nodes, lines


def _phase2(st: _State) -> Project | None:  # noqa: C901 - one linear pipeline
    # --- missing mandatory records ---------------------------------------------------
    for rt, code in (
        ("PROJECT", "CSV_MISSING_PROJECT"),
        ("CALENDAR", "CSV_MISSING_CALENDAR"),
        ("NODE", "CSV_MISSING_NODE"),
    ):
        if rt not in st.seen_types:
            _model_err(st, rt, 1, code, "record_type", f"the file has no {rt} record", None)

    resources = _dedupe(st, "RESOURCE", st.resources)
    nodes = _dedupe(st, "NODE", st.nodes)
    deps = _dedupe(st, "DEPENDENCY", st.deps)

    # --- calendar dates -----------------------------------------------------------------
    holidays: list[Holiday] = []
    exceptions: list[CalendarException] = []
    seen_dates: dict[dt.date, int] = {}
    for d in sorted(st.date_rows, key=lambda r: r.line):
        if d.date in seen_dates:
            _model_err(
                st, d.kind, d.line, "CSV_DUPLICATE_DATE", "date",
                f"date {d.date.isoformat()} already used on line {seen_dates[d.date]}", None,
            )  # fmt: skip
            continue
        seen_dates[d.date] = d.line
        if d.kind == "HOLIDAY":
            holidays.append(Holiday(d.date, d.name))
        else:
            assert d.exc_kind is not None
            exceptions.append(CalendarException(d.date, d.exc_kind, d.name))

    # --- calendar ------------------------------------------------------------------------
    cal_row = st.calendar_rows[0] if st.calendar_rows else None
    calendar = Calendar(holidays=tuple(holidays), exceptions=tuple(exceptions))
    if (
        cal_row is not None and cal_row.hours is not None and cal_row.days is not None
        and cal_row.weekdays is not None and cal_row.start is not None
    ):  # fmt: skip
        settings = CalendarSettings(
            working_weekdays=cal_row.weekdays,
            hours_per_day=cal_row.hours,
            working_days_per_year=cal_row.days,
            workday_start=cal_row.start,
            holidays=tuple(holidays),
            exceptions=tuple(exceptions),
        )
        cal_issues = settings.validate()
        if any(i.code == "CAL_WORKDAY_OVERFLOW" for i in cal_issues):
            _model_err(
                st, "CALENDAR", cal_row.line, "CSV_BAD_CALENDAR", "workday_start",
                f"workday_start {cal_row.start:%H:%M} + {cal_row.hours} h exceeds 24:00", None,
            )  # fmt: skip
        elif not cal_issues:
            calendar = settings.to_calendar()

    # --- model objects (failed rows kept as placeholders so references still resolve) -------
    node_line = {n.id: n.line for n in nodes}
    node_objs: list[WbsNode] = []
    unknown_kind: set[str] = set()
    for n in nodes:
        kind = n.kind
        if kind is None:
            kind = NodeKind.TASK
            unknown_kind.add(n.id)
        keep = n.ok and kind is not None
        node_objs.append(
            WbsNode(
                n.id,
                n.name,
                kind,
                n.parent,
                n.order if n.order is not None else 0,
                n.mode if keep else SizingMode.NONE,
                n.sizing if keep else None,
            )  # fmt: skip
        )
    res_objs = [Resource(r.id, r.name, r.rate) for r in resources]

    assign_objs: list[Assignment] = []
    assign_line: dict[str, int] = {}
    seen_assign: dict[tuple[str, str], int] = {}
    for a in st.assigns:
        key = (a.task, a.res)
        if key in seen_assign:
            _model_err(
                st, "ASSIGNMENT", a.line, "CSV_DUPLICATE_ASSIGNMENT", "resource_id",
                f"resource {a.res!r} already assigned to {a.task!r} on line {seen_assign[key]}",
                f"{a.task}/{a.res}",
            )  # fmt: skip
            continue
        seen_assign[key] = a.line
        assign_objs.append(Assignment(a.task, a.res, a.percent))
        assign_line[f"{a.task}/{a.res}"] = a.line
    dep_objs = [Dependency(d.id, d.pred, d.succ, d.type, d.lag) for d in deps]
    dep_line = {d.id: d.line for d in deps}

    prow = st.project_rows[0] if st.project_rows else None
    project = Project(
        id=IMPORTED_PROJECT_ID,
        name=prow.name if prow else "",
        start=prow.start if prow and prow.start else dt.date(2000, 1, 1),
        currency=prow.currency if prow else _cfg.DEFAULT_CURRENCY,
        cost_report_unit=prow.unit if prow else WorkUnit(_cfg.DEFAULT_COST_REPORT_UNIT),
        calendar=calendar,
        nodes=tuple(node_objs),
        resources=tuple(res_objs),
        assignments=tuple(assign_objs),
        dependencies=tuple(dep_objs),
    )

    # --- engine validation, mapped to CSV codes ------------------------------------------
    for issue in validate(project, DEFAULT_CONFIG):
        if issue.severity is not Severity.ERROR:
            continue
        oid = issue.object_id or ""
        code = issue.code
        if code == "NODE_PARENT_DANGLING":
            _model_err(
                st,
                "NODE",
                node_line[oid],
                "CSV_DANGLING_REFERENCE",
                "parent_id",
                issue.message,
                oid,
            )
        elif code == "NODE_PARENT_NOT_GROUP":
            parent = project.node(oid).parent_id
            if parent not in unknown_kind:
                _model_err(
                    st,
                    "NODE",
                    node_line[oid],
                    "CSV_PARENT_NOT_GROUP",
                    "parent_id",
                    issue.message,
                    oid,
                )
        elif code == "DEP_DANGLING":
            _model_err(
                st,
                "DEPENDENCY",
                dep_line[oid],
                "CSV_DANGLING_REFERENCE",
                issue.field or "",
                issue.message,
                oid,
            )
        elif code == "DEP_SELF":
            _model_err(
                st,
                "DEPENDENCY",
                dep_line[oid],
                "CSV_SELF_DEPENDENCY",
                "succ_id",
                issue.message,
                oid,
            )
        elif code == "DEP_GROUP_ENDPOINT":
            _model_err(
                st,
                "DEPENDENCY",
                dep_line[oid],
                "CSV_GROUP_DEPENDENCY",
                issue.field or "",
                issue.message,
                oid,
            )
        elif code == "DEP_DUPLICATE":
            _model_err(
                st,
                "DEPENDENCY",
                dep_line[oid],
                "CSV_DUPLICATE_DEPENDENCY",
                "dep_type",
                issue.message,
                oid,
            )
        elif code == "ASSIGN_TASK_DANGLING":
            _model_err(
                st,
                "ASSIGNMENT",
                assign_line[oid],
                "CSV_DANGLING_REFERENCE",
                "task_id",
                issue.message,
                oid,
            )
        elif code == "ASSIGN_NOT_TASK":
            if oid.split("/", 1)[0] not in unknown_kind:
                _model_err(
                    st,
                    "ASSIGNMENT",
                    assign_line[oid],
                    "CSV_ASSIGN_NON_TASK",
                    "task_id",
                    issue.message,
                    oid,
                )
        elif code == "ASSIGN_RESOURCE_DANGLING":
            _model_err(
                st,
                "ASSIGNMENT",
                assign_line[oid],
                "CSV_DANGLING_REFERENCE",
                "resource_id",
                issue.message,
                oid,
            )
        elif code == "ASSIGN_PERCENT_RANGE":
            _model_err(
                st,
                "ASSIGNMENT",
                assign_line[oid],
                "CSV_OUT_OF_RANGE",
                "percent",
                issue.message,
                oid,
            )
        # DEP_CYCLE / NODE_PARENT_CYCLE are reported below with ordered paths and lines.

    # --- parent cycles -------------------------------------------------------------------
    parent_edges = {n.id: n.parent for n in nodes if n.parent is not None and n.parent in node_line}
    pgraph = DirectedGraph(node_line, parent_edges.items())
    for members in pgraph.cycles():
        first = min(members, key=lambda m: (node_line[m], m))
        chain = [first]
        while parent_edges[chain[-1]] != first:
            chain.append(parent_edges[chain[-1]])
        _model_err(
            st, "NODE", node_line[first], "CSV_PARENT_CYCLE", "parent_id",
            "parent chain loops: " + " -> ".join([*chain, first]), first,
        )  # fmt: skip

    # --- dependency cycles (self, dangling and group-endpoint rows are excluded) -----------
    kinds = {n.id: n.kind for n in node_objs}
    dep_edges: dict[tuple[str, str], int] = {}
    for dr in deps:
        if (
            dr.pred != dr.succ
            and dr.pred in kinds and dr.succ in kinds
            and kinds[dr.pred] is not NodeKind.GROUP
            and kinds[dr.succ] is not NodeKind.GROUP
        ):  # fmt: skip
            edge = (dr.pred, dr.succ)
            if edge not in dep_edges or dr.line < dep_edges[edge]:
                dep_edges[edge] = dr.line
    dgraph = DirectedGraph(kinds, dep_edges)
    for members in dgraph.cycles():
        path, lines = _cycle_path(dep_edges, set(members))
        first_dep = next(
            dr for dr in deps if (dr.pred, dr.succ) == (path[0], path[1]) and dr.line == lines[0]
        )
        _model_err(
            st, "DEPENDENCY", lines[0], "CSV_CYCLE", "pred_id",
            f"dependency cycle: {' -> '.join(path)} (lines {', '.join(str(x) for x in lines)})",
            first_dep.id,
        )  # fmt: skip

    return project


# =============================================================================
# Export
# =============================================================================


def _num(x: Decimal | int) -> str:
    """Plain decimal notation, exactly as stored (no exponent)."""
    return format(Decimal(x), "f")


def _days(x: Decimal | int | None) -> str:
    return "" if x is None else format(DEFAULT_CONFIG.round_days(Decimal(x)), "f")


def _money(x: Decimal | int | None) -> str:
    return "" if x is None else format(DEFAULT_CONFIG.round_money(Decimal(x)), "f")


def _qty(x: Decimal | int | None) -> str:
    if x is None:
        return ""
    s = format(Decimal(x).quantize(Decimal("0.000001"), ROUND_HALF_EVEN), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _calendar_days(x: Decimal | int | None) -> str:
    if x is None:
        return ""
    d = Decimal(x)
    return str(int(d)) if d == d.to_integral_value() else _days(d)


def _dtm(x: dt.datetime | None) -> str:
    if x is None:
        return ""
    return f"{x.year:04d}-{x.month:02d}-{x.day:02d}T{x.hour:02d}:{x.minute:02d}"


def _bool(x: bool) -> str:
    return "true" if x else "false"


def _quote(cell: str) -> str:
    if any(ch in cell for ch in ',"\r\n'):
        return '"' + cell.replace('"', '""') + '"'
    return cell


def _line(rt: str, **cells: str) -> str:
    unknown = set(cells) - set(COLUMNS)
    assert not unknown, unknown
    out = {"record_type": rt, "schema_version": SCHEMA_VERSION, **cells}
    return ",".join(_quote(out.get(col, "")) for col in COLUMNS) + "\n"


def _node_order(project: Project) -> list[WbsNode]:
    ordered = list(project.wbs_order())
    seen = {n.id for n in ordered}
    ordered.extend(sorted((n for n in project.nodes if n.id not in seen), key=lambda n: n.id))
    return ordered


def export(project: Project, result: ExportableResult | None = None, *, stale: bool = False) -> str:
    """Write ``project`` (and optionally a schedule result) as CSV text.

    Every node is written regardless of any UI state. Output is deterministic
    (format document section 5), UTF-8 safe text with ``\\n`` line endings.

    Args:
        project: The definition to write.
        result: A result matching :class:`ExportableResult`, or ``None`` for none (then
            no ``RESULT_*`` record is written).
        stale: The result is out of date with respect to ``project``; then
            ``RESULT_PROJECT.schedule_status`` is ``stale``. Ignored without a result.
    """
    out: list[str] = [",".join(COLUMNS) + "\n"]
    out.append(
        _line(
            "PROJECT",
            name=project.name,
            start_date=project.start.isoformat(),
            currency=project.currency,
            cost_report_unit=project.cost_report_unit.value,
        )  # fmt: skip
    )
    cal = project.calendar
    out.append(
        _line(
            "CALENDAR",
            hours_per_day=_num(cal.hours_per_day),
            working_days_per_year=str(cal.working_days_per_year),
            working_weekdays=";".join(w.value for w in Weekday.ordered(cal.working_weekdays)),
            workday_start=f"{cal.workday_start:%H:%M}",
        )
    )
    for h in sorted(cal.holidays, key=lambda x: x.date):
        out.append(_line("HOLIDAY", name=h.name, date=h.date.isoformat()))
    for e in sorted(cal.exceptions, key=lambda x: x.date):
        out.append(
            _line("EXCEPTION", name=e.name, date=e.date.isoformat(), exception_kind=e.kind.value)
        )
    for r in sorted(project.resources, key=lambda x: x.id):
        out.append(
            _line(
                "RESOURCE",
                id=r.id,
                name=r.name,
                hourly_rate="" if r.hourly_rate is None else _num(r.hourly_rate),
            )  # fmt: skip
        )
    nodes = _node_order(project)
    for n in nodes:
        sizing: dict[str, str] = {}
        if n.sizing is not None:
            sizing = {
                "sizing_mode": n.sizing_mode.value,
                "sizing_value": _num(n.sizing.value),
                "sizing_unit": n.sizing.unit.symbol,
            }
        out.append(
            _line(
                "NODE",
                id=n.id,
                name=n.name,
                node_kind=n.kind.value,
                parent_id=n.parent_id or "",
                sibling_order=str(n.order),
                **sizing,
            )  # fmt: skip
        )
    position = {n.id: i for i, n in enumerate(nodes)}
    assignments = sorted(
        project.assignments,
        key=lambda a: (position.get(a.task_id, len(position)), a.task_id, a.resource_id),
    )
    for a in assignments:
        out.append(
            _line(
                "ASSIGNMENT", task_id=a.task_id, resource_id=a.resource_id, percent=_num(a.percent)
            )
        )
    for d in sorted(project.dependencies, key=lambda x: x.id):
        out.append(
            _line(
                "DEPENDENCY",
                id=d.id,
                pred_id=d.pred_id,
                succ_id=d.succ_id,
                dep_type=d.type.value,
                lag_value=_num(d.lag.value),
                lag_unit=d.lag.unit.symbol,
            )  # fmt: skip
        )
    if result is not None:
        out.extend(_result_lines(project, nodes, assignments, result, stale))
    return "".join(out)


def _result_lines(
    project: Project,
    nodes: list[WbsNode],
    assignments: list[Assignment],
    result: ExportableResult,
    stale: bool,
) -> list[str]:
    status = "stale" if stale else ("current" if result.complete else "incomplete")
    out = [
        _line(
            "RESULT_PROJECT",
            schedule_status=status,
            schedule_kind=str(result.kind),
            start=_dtm(result.project_start),
            finish=_dtm(result.project_finish),
            working_span_days=_days(result.working_span_days),
            elapsed_span_calendar_days=_calendar_days(result.elapsed_span_calendar_days),
            effort_days=_days(result.effort_days),
            work_qty=_qty(result.work_qty),
            work_unit=result.work_unit or "",
            cost=_money(result.cost_total),
            cost_complete=_bool(result.cost_complete),
        )
    ]
    by_node = {r.node_id: r for r in result.node_results}
    for n in nodes:
        nr = by_node.get(n.id)
        if nr is None:
            out.append(
                _line("RESULT_NODE", id=n.id, cost_complete="false", node_status="unscheduled")
            )
            continue
        out.append(
            _line(
                "RESULT_NODE",
                id=n.id,
                start=_dtm(nr.start),
                finish=_dtm(nr.finish),
                duration_days=_days(nr.duration_days),
                effort_days=_days(nr.effort_days),
                leveling_delay_days=_days(nr.leveling_delay_days),
                work_qty=_qty(nr.work_qty),
                work_unit=nr.work_unit or "",
                cost=_money(nr.cost),
                cost_complete=_bool(nr.cost_complete),
                node_status=nr.status,
                issue_codes=";".join(nr.issue_codes),
            )  # fmt: skip
        )
    by_assign = {(r.task_id, r.resource_id): r for r in result.assignment_results}
    for a in assignments:
        ar = by_assign.get(a.key)
        if ar is None:
            out.append(
                _line(
                    "RESULT_ASSIGNMENT",
                    task_id=a.task_id,
                    resource_id=a.resource_id,
                    cost_complete="false",
                )  # fmt: skip
            )
            continue
        out.append(
            _line(
                "RESULT_ASSIGNMENT",
                task_id=a.task_id,
                resource_id=a.resource_id,
                assignment_days=_days(ar.assignment_days),
                work_qty=_qty(ar.work_qty),
                work_unit=ar.work_unit or "",
                rate_per_unit=_qty(ar.rate_per_unit),
                cost=_money(ar.cost),
                cost_complete=_bool(ar.cost_complete),
            )  # fmt: skip
        )
    return out
