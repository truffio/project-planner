"""Shared helpers for the acceptance suite (T05, written test-first).

Every test in this package targets the *public facade* from plan section 1.2
(``import project_planner as pp``). The facade does not exist yet, so all tests
are marked ``xfail(strict=True)`` with the ID of the task expected to make them
pass.

IMPORTANT: nothing here touches ``pp.<anything>`` at import time. Every access
to the facade happens inside a helper call made from a test body, so a missing
API surfaces as a test-time failure (counted as xfail) rather than a collection
error. For the same reason there are no fixtures that call the API: tests call
``new_ws()`` themselves.

All assumptions about API details that plan section 1.2 does not pin down are
concentrated in this module (see the "API ASSUMPTIONS" block below) so that T35
can reconcile them in one place.

Calendar reference used throughout (verified with ``datetime``):

    2026-10-03 Sat   2026-10-04 Sun
    2026-10-05 Mon  <- default project start (PROJECT_START)
    2026-10-06 Tue   2026-10-07 Wed   2026-10-08 Thu   2026-10-09 Fri
    2026-10-10 Sat   2026-10-11 Sun
    2026-10-12 Mon   2026-10-13 Tue   2026-10-14 Wed   2026-10-15 Thu
    2026-10-16 Fri

Default calendar: Mon-Fri, 8 h/day, workday start 09:00, so each working day is
09:00-17:00 = 480 working minutes. Working-day index from PROJECT_START with no
holidays: d0 Mon 5, d1 Tue 6, d2 Wed 7, d3 Thu 8, d4 Fri 9, d5 Mon 12,
d6 Tue 13, d7 Wed 14. An axis minute m maps to day d = m // 480, offset
r = m % 480. A *start* at r == 0 is shown as day d 09:00; a *finish* at r == 0
is shown as day d-1 17:00 (plan section 2 #7).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import project_planner as pp

# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

PROJECT_START = date(2026, 10, 5)  # Monday (verified: date(2026, 10, 5).weekday() == 0)


def oct26(day: int, hhmm: str) -> datetime:
    """Naive datetime in October 2026, e.g. ``oct26(9, "17:00")`` = Fri 9 Oct 17:00."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, day, hh, mm)


def D(x: str | int) -> Decimal:
    """Shorthand for an exact Decimal."""
    return Decimal(str(x))


def q(x: Decimal, places: int = 10) -> Decimal:
    """Quantize for comparing values that are repeating decimals (person-years)."""
    return Decimal(x).quantize(Decimal(1).scaleb(-places))


# --------------------------------------------------------------------------
# API ASSUMPTIONS (for T35 to reconcile) -- keep every one of them here.
# --------------------------------------------------------------------------
#
#  A1  pp.open_workspace(path_or_":memory:") -> Workspace; Workspace.close().
#  A2  ws.new_project(name, start=date, currency="USD", cost_report_unit="person_days",
#      calendar=pp.CalendarSettings | None, discard_unsaved=False).
#  A3  add_resource/add_group/add_task/add_milestone/add_dependency return string IDs
#      (id_of() also accepts objects with an ``.id``).
#      ws.add_milestone(name, parent=None) exists.
#      ws.add_dependency(pred, succ, type, lag=...) returns the dependency ID; lag
#      defaults to zero.
#  A4  ws.schedule() -> ScheduleResult; ws.result() -> the current stored result
#      (dependency_only or leveled) or None if nothing has been calculated.
#  A5  ScheduleResult.node(node_id) -> node row with: start, finish (naive datetimes,
#      None if unscheduled; unknown ID -> pp.NotFound), duration_days,
#      effort_days (Decimal), cost (Decimal|None),
#      cost_complete (bool), scheduled (bool), leveling_delay_days (Decimal).
#      ScheduleResult.assignment(task_id, resource_id) -> row with percent (Decimal),
#      assignment_days (Decimal), cost (Decimal|None).
#      ScheduleResult attributes: complete, project_start, project_finish (None when
#      incomplete), working_span_days, elapsed_span_calendar_days, total_cost,
#      cost_complete, kind in {"dependency_only","leveling_preview","leveled"},
#      issues (list of Issue). ScheduleResult.dependency(dep_id) -> row with lag_days.
#  A6  ws.cost_report(unit=None) -> CostReport with: unit (str code), total_cost,
#      complete (bool), missing_rate_resources (list of resource IDs),
#      node(node_id) -> row with work_qty, work_unit, cost, complete, assignments
#      (rows with resource_id, percent, work_qty, work_unit, rate_per_unit, cost).
#  A7  ws.task_details(task_id, unit=None) -> object with ``assignments`` rows having
#      resource_id, percent, assignment_days, work_qty, work_unit, rate_per_unit, cost.
#  A8  ws.loading(resource) -> object with ``segments``: rows with start, end (datetimes,
#      end uses the finish convention), percent (Decimal), task_ids, overloaded.
#      ws.loading(resource, granularity="day") -> object with ``buckets``: rows with
#      period_start (date), assigned_days, average_percent, peak_percent.
#      Days with no working time may be absent (treated as zero). Loading always
#      reflects the current result (ws.result()).
#  A9  ws.state() -> object with revision (int), dirty, stale_dates, stale_costs,
#      has_preview (bools).
#  A10 ws.project() -> immutable, equality-comparable snapshot of the definition
#      (engine.model.Project) exposing name, resources (with .name), nodes (with .id);
#      ws.node(id) -> WbsNode with ``sizing`` (TimeQty with
#      ``value`` Decimal and ``unit`` "hours"/"days", str or str-valued enum);
#      ws.dependency(id) -> Dependency with ``lag`` TimeQty.
#  A11 Edits: ws.set_sizing(task, duration=... | effort=...),
#      ws.set_hourly_rate(resource, rate), ws.set_cost_report_unit(code),
#      ws.calendar.add_holiday(date, name), ws.calendar.set_working_days_per_year(n),
#      ws.calendar.set_hours_per_day(h), ws.calendar.settings() -> pp.CalendarSettings
#      (equality-comparable; pp.CalendarSettings() == the section 2.3 defaults).
#      Calendar holidays as (date, name) tuples, exceptions as (date, "working" |
#      "nonworking") tuples.
#  A12 ws.level_preview() -> LevelingResult(result, delays_days: {task_id: Decimal},
#      unresolved: list, finish_delta_days). ws.apply_leveling(),
#      ws.discard_leveling(), ws.reset_to_dependency_schedule().
#  A13 ws.save(), ws.save_as(name), ws.list_projects() -> rows with id, name,
#      ws.load(project_id, discard_unsaved=False).
#  A14 ws.export_csv(path), ws.import_csv(path, discard_unsaved=False) -> summary.
#      Replacing operations raise pp.UnsavedChanges when dirty unless
#      discard_unsaved=True. After a successful import ws.result() is None
#      (imported result rows are ignored).
#  A15 Errors are exported at package level: pp.ValidationFailed, pp.ImportFailed,
#      pp.UnsavedChanges, pp.Cancelled, pp.NotFound. ValidationFailed / ImportFailed
#      carry ``issues``; each Issue has severity, code, message, object_type,
#      object_id, field; ImportFailed issues additionally have ``line`` = 1-based
#      physical line number in the file (header line = 1, per docs/csv_format.md)
#      and ``field`` = column name.
#  A16 Unit entry: bare numbers (int/float/Decimal) for duration/effort/lag raise
#      TypeError; a unit-less *string* such as "40" raises pp.ValidationFailed.
#      Field-level checks (assignment percent range) raise pp.ValidationFailed
#      immediately from the edit call. Cycles / self-dependencies / group endpoints
#      raise pp.ValidationFailed either from add_dependency or from schedule() (tests
#      accept both); dangling endpoints raise pp.NotFound or pp.ValidationFailed.
#  A17 Jobs: ws.submit_schedule() / ws.submit_leveling_preview() -> Job with status
#      (str: "queued"/"running"/"done"/"cancelled"/"failed"), progress (float 0..1),
#      message (str), cancel(), result(timeout), done(). result() of a cancelled job
#      raises pp.Cancelled. The workspace stores a job's result on completion.
#  A18 CSV export: a header row contains a ``record_type`` column; NODE rows contain
#      the node ID as a cell value; DEPENDENCY rows have a column whose name contains
#      "succ"; PROJECT rows have a column whose name contains "start".
#  A19 Unit codes "person_hours"/"person_days"/"person_years" are plain strings (or
#      str-valued enums) in report.unit / work_unit.


def new_ws(path: str | Path = ":memory:") -> Any:
    return pp.open_workspace(str(path))


def new_project(ws: Any, name: str = "Acceptance", start: date = PROJECT_START, **kw: Any) -> Any:
    return ws.new_project(name, start=start, **kw)


def id_of(x: Any) -> str:
    return str(getattr(x, "id", x))


def code(x: Any) -> str:
    """Normalise an enum-or-string code to its string value."""
    return str(getattr(x, "value", x))


def node_row(result: Any, node_id: Any) -> Any:
    return result.node(id_of(node_id))


def assignment_row(result: Any, task_id: Any, resource_id: Any) -> Any:
    return result.assignment(id_of(task_id), id_of(resource_id))


def current(ws: Any) -> Any:
    return ws.result()


def report_node(report: Any, node_id: Any) -> Any:
    return report.node(id_of(node_id))


def report_assignment(report: Any, task_id: Any, resource_id: Any) -> Any:
    rows = [
        a
        for a in report_node(report, task_id).assignments
        if id_of(a.resource_id) == id_of(resource_id)
    ]
    assert len(rows) == 1, rows
    return rows[0]


def detail_assignment(details: Any, resource_id: Any) -> Any:
    rows = [a for a in details.assignments if id_of(a.resource_id) == id_of(resource_id)]
    assert len(rows) == 1, rows
    return rows[0]


def loading_segments(ws: Any, resource: Any) -> list[Any]:
    return list(ws.loading(id_of(resource)).segments)


def daily_loading(ws: Any, resource: Any) -> dict[date, Any]:
    view = ws.loading(id_of(resource), granularity="day")
    return {b.period_start: b for b in view.buckets}


def day_assigned(buckets: dict[date, Any], day: date) -> Decimal:
    b = buckets.get(day)
    return Decimal(0) if b is None else Decimal(b.assigned_days)


def sizing_of(ws: Any, node_id: Any) -> tuple[Decimal, str]:
    s = ws.node(id_of(node_id)).sizing
    return Decimal(s.value), code(s.unit)


def lag_of(ws: Any, dep_id: Any) -> tuple[Decimal, str]:
    lag = ws.dependency(id_of(dep_id)).lag
    return Decimal(lag.value), code(lag.unit)


def calendar_settings(ws: Any) -> Any:
    return ws.calendar.settings()


def project_id_by_name(ws: Any, name: str) -> str:
    ids = [id_of(p) for p in ws.list_projects() if p.name == name]
    assert len(ids) == 1, ids
    return ids[0]


def issue_fields(exc: Any) -> set[str]:
    return {str(i.field) for i in exc.issues if i.field is not None}


def mentioned_ids(exc: Any) -> set[str]:
    """Every object ID an error mentions: issue object_ids plus IDs found in messages."""
    out: set[str] = set()
    for i in exc.issues:
        if i.object_id is not None:
            out.add(str(i.object_id))
    text = " ".join(str(i.message) for i in exc.issues) + " " + str(exc)
    out |= {tok.strip("',\"[](){}:;.") for tok in text.split()}
    return out


def rejected(action: Callable[[], Any], ws: Any) -> Any:
    """Run ``action`` (an edit that should be invalid).

    Per assumption A16 the rejection may come from the edit itself or from the next
    ``schedule()``. Returns the ``pp.ValidationFailed`` instance either way and checks
    that no valid schedule is claimed.
    """
    rev = ws.state().revision
    try:
        action()
    except pp.ValidationFailed as exc:
        # Rejected at edit time: nothing applied.
        assert ws.state().revision == rev
        return exc
    try:
        ws.schedule()
    except pp.ValidationFailed as exc:
        # Rejected at schedule time: no current valid schedule.
        res = ws.result()
        assert res is None or not res.complete
        return exc
    raise AssertionError("invalid edit was neither rejected nor reported by schedule()")


@dataclass(frozen=True)
class Snapshot:
    revision: int
    dirty: bool
    definition: Any
    dates: tuple[tuple[str, Any, Any], ...]


def snapshot(ws: Any, node_ids: Iterable[Any]) -> Snapshot:
    st = ws.state()
    res = ws.result()
    dates: tuple[tuple[str, Any, Any], ...] = ()
    if res is not None:
        dates = tuple((id_of(n), node_row(res, n).start, node_row(res, n).finish) for n in node_ids)
    return Snapshot(st.revision, st.dirty, ws.project(), dates)


# --------------------------------------------------------------------------
# CSV text helpers (format-agnostic beyond assumption A18)
# --------------------------------------------------------------------------


def read_csv_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    assert "record_type" in header, header
    return header, rows[1:]


def write_csv_rows(path: Path, header: list[str], rows: list[list[str]]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8")


def record_type(header: list[str], row: list[str]) -> str:
    return row[header.index("record_type")] if row else ""
