"""Notebook ergonomics: HTML tables, ``to_records()`` and optional pandas ``to_dataframe()``.

Importing this module (the package does it) registers ``_repr_html_``, ``to_records`` and
``to_dataframe`` on the public result/view classes, so they render as tables in Jupyter
without touching the engine modules.

* ``to_records()`` returns a list of plain dicts (one per row) and has no dependencies.
* ``to_dataframe()`` imports pandas lazily; when pandas is missing it raises
  ``ImportError`` that names ``pip install project_planner[notebook]``.
* Task *names* are not part of an engine result. ``Workspace.schedule()`` / ``result()``
  attach them (see :func:`attach_names`; kept in a side cache, the frozen result is
  never modified) so the tables show names; without them the node IDs are shown.
"""

from __future__ import annotations

import datetime as dt
import html
import importlib
import weakref
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal
from enum import Enum
from typing import Any

from project_planner.engine.model import Project
from project_planner.engine.results import LevelingResult, NodeResult, ScheduleResult
from project_planner.services.read_models import CostReportView, LoadingView, TaskDetails
from project_planner.services.workspace import CalendarApi

__all__ = ["attach_names", "html_table", "names_of", "to_dataframe_from_records"]

Record = dict[str, Any]

_INSTALL_HINT = (
    "pandas is required for to_dataframe(); install it with: pip install project_planner[notebook]"
)


# ----------------------------------------------------------------------- helpers


def _plain(value: Any) -> Any:
    """Convert a value to a plain, display-friendly Python value."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list | frozenset | set):
        return ", ".join(str(_plain(v)) for v in value)
    return value


def _cell(value: Any) -> str:
    value = _plain(value)
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return (
            format(value.normalize(), "f") if value == value.to_integral() else format(value, "f")
        )
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def html_table(records: Iterable[Mapping[str, Any]], *, title: str | None = None) -> str:
    """Render ``records`` (same keys per row) as an HTML table; values are escaped."""
    rows = list(records)
    parts = ["<div>"]
    if title:
        parts.append(f"<b>{html.escape(title)}</b>")
    parts.append("<table border='1' class='dataframe'>")
    if rows:
        parts.append(
            "<thead><tr>"
            + "".join(f"<th>{html.escape(str(k))}</th>" for k in rows[0])
            + "</tr></thead>"
        )
    parts.append("<tbody>")
    for row in rows:
        parts.append(
            "<tr>" + "".join(f"<td>{html.escape(_cell(v))}</td>" for v in row.values()) + "</tr>"
        )
    parts.append("</tbody></table></div>")
    return "".join(parts)


def to_dataframe_from_records(records: list[Record]) -> Any:
    """Build a pandas ``DataFrame``; raises a helpful ``ImportError`` if pandas is absent."""
    try:
        pandas = importlib.import_module("pandas")
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc
    return pandas.DataFrame(records)


# Display names live in a side cache keyed by result identity, never on the (frozen,
# shared) result object itself: nothing is mutated, equality and pickling are
# unaffected, and job workers never receive name maps.
_NamesEntry = tuple[int, Mapping[str, str]]
_NAMES: dict[int, tuple[weakref.ref[ScheduleResult], _NamesEntry]] = {}
_NAMES_BY_FP: dict[tuple[str, str, str], _NamesEntry] = {}  # results without weakref support
_NAMES_BY_FP_MAX = 64


def _fp_key(result: ScheduleResult) -> tuple[str, str, str]:
    return (str(result.kind), result.schedule_fp, result.cost_fp)


def _held(result: ScheduleResult) -> _NamesEntry | None:
    entry = _NAMES.get(id(result))
    if entry is not None and entry[0]() is result:
        return entry[1]
    return _NAMES_BY_FP.get(_fp_key(result))


def attach_names(result: ScheduleResult, project: Project, revision: int) -> None:
    """Remember node names for displaying ``result`` (refreshed when ``revision`` changes).

    The names are kept in a module side cache keyed by the result object (dropped when
    the result is garbage collected); the result itself is not modified.
    """
    held = _held(result)
    if held is not None and held[0] == revision:
        return
    entry: _NamesEntry = (revision, {n.id: n.name for n in project.nodes})
    key = id(result)

    def _forget(_ref: weakref.ref[ScheduleResult], key: int = key) -> None:
        current = _NAMES.get(key)
        if current is not None and current[0] is _ref:
            del _NAMES[key]

    try:
        ref = weakref.ref(result, _forget)
    except TypeError:  # result type without __weakref__: fall back to its fingerprints
        if len(_NAMES_BY_FP) >= _NAMES_BY_FP_MAX:
            _NAMES_BY_FP.pop(next(iter(_NAMES_BY_FP)))
        _NAMES_BY_FP[_fp_key(result)] = entry
        return
    _NAMES[key] = (ref, entry)


def names_of(result: ScheduleResult) -> Mapping[str, str]:
    """Node names attached to ``result`` by :func:`attach_names` (empty if none)."""
    held = _held(result)
    return {} if held is None else held[1]


# ----------------------------------------------------------------------- records


def _node_record(row: NodeResult, names: Mapping[str, str]) -> Record:
    return {
        "id": row.node_id,
        "wbs": row.wbs_number,
        "name": names.get(row.node_id, row.node_id),
        "kind": row.kind.value,
        "status": row.status,
        "start": row.start,
        "finish": row.finish,
        "duration_days": row.duration_days,
        "effort_days": row.effort_days,
        "leveling_delay_days": row.leveling_delay_days,
        "cost": row.cost,
        "cost_complete": row.cost_complete,
    }


def _schedule_records(self: ScheduleResult) -> list[Record]:
    names = names_of(self)
    return [_node_record(r, names) for r in self.tasks()]


def _schedule_html(self: ScheduleResult) -> str:
    finish = "n/a" if self.project_finish is None else _cell(self.project_finish)
    title = (
        f"ScheduleResult ({self.kind}) complete={self.complete} "
        f"finish={finish} total_cost={_cell(self.total_cost)}"
    )
    return html_table(_schedule_records(self), title=title)


def _leveling_records(self: LevelingResult) -> list[Record]:
    return _schedule_records(self.result)


def _leveling_html(self: LevelingResult) -> str:
    delta = "n/a" if self.finish_delta_days is None else _cell(self.finish_delta_days)
    title = f"LevelingResult finish_delta_days={delta} unresolved={len(self.unresolved)}"
    return html_table(_leveling_records(self), title=title)


def _cost_records(self: CostReportView) -> list[Record]:
    return [
        {
            "node_id": r.node_id,
            "kind": r.kind.value,
            "work_qty": r.work_qty,
            "work_unit": r.work_unit.value,
            "cost": r.cost,
            "complete": r.complete,
            "missing_rate_resources": r.missing_rate_resources,
        }
        for r in self.nodes
    ]


def _cost_html(self: CostReportView) -> str:
    title = (
        f"Cost report ({self.unit.value}) total={_cell(self.total_cost)} complete={self.complete}"
    )
    return html_table(_cost_records(self), title=title)


def _loading_records(self: LoadingView) -> list[Record]:
    if self.buckets:
        return [
            {
                "resource_id": self.resource_id,
                "period_start": b.period_start,
                "assigned_days": b.assigned_days,
                "average_percent": b.average_percent,
                "peak_percent": b.peak_percent,
                "overloaded": b.overloaded,
            }
            for b in self.buckets
        ]
    return [
        {
            "resource_id": self.resource_id,
            "start": s.start,
            "end": s.end,
            "percent": s.percent,
            "task_ids": s.task_ids,
            "overloaded": s.overloaded,
        }
        for s in self.segments
    ]


def _loading_html(self: LoadingView) -> str:
    return html_table(_loading_records(self), title=f"Loading of {self.resource_name}")


def _details_records(self: TaskDetails) -> list[Record]:
    return [
        {
            "resource_id": a.resource_id,
            "resource_name": a.resource_name,
            "percent": a.percent,
            "assignment_days": a.assignment_days,
            "work_qty": a.work_qty,
            "work_unit": a.work_unit.value,
            "rate_per_unit": a.rate_per_unit,
            "cost": a.cost,
        }
        for a in self.assignments
    ]


def _details_html(self: TaskDetails) -> str:
    summary = [
        {
            "id": self.id,
            "wbs": self.wbs_number,
            "name": self.name,
            "kind": self.kind.value,
            "status": self.status,
            "start": self.start,
            "finish": self.finish,
            "duration_days": self.duration_days,
            "effort_days": self.effort_days,
            "cost": self.cost,
        }
    ]
    return html_table(summary, title="Task") + html_table(
        _details_records(self), title="Assignments"
    )


def _calendar_records(self: CalendarApi) -> list[Record]:
    s = self.settings()
    return [
        {"setting": "working_weekdays", "value": ", ".join(d.value for d in _ordered(s))},
        {"setting": "hours_per_day", "value": s.hours_per_day},
        {"setting": "working_days_per_year", "value": s.working_days_per_year},
        {"setting": "workday_start", "value": s.workday_start.strftime("%H:%M")},
        {
            "setting": "holidays",
            "value": "; ".join(f"{h.date} {h.name}".strip() for h in s.holidays),
        },
        {
            "setting": "exceptions",
            "value": "; ".join(f"{e.date} {e.kind.value} {e.name}".strip() for e in s.exceptions),
        },
    ]


def _ordered(settings: Any) -> list[Any]:
    return sorted(settings.working_weekdays, key=lambda d: d.python_weekday)


def _calendar_html(self: CalendarApi) -> str:
    return html_table(_calendar_records(self), title="Calendar")


# ----------------------------------------------------------------- registration


def _dataframe_method(records: Callable[[Any], list[Record]]) -> Callable[[Any], Any]:
    def to_dataframe(self: Any) -> Any:
        """Rows as a pandas DataFrame (needs ``pip install project_planner[notebook]``)."""
        return to_dataframe_from_records(records(self))

    return to_dataframe


def _register(
    cls: type, records: Callable[[Any], list[Record]], to_html: Callable[[Any], str]
) -> None:
    cls.to_records = records  # type: ignore[attr-defined]
    cls.to_dataframe = _dataframe_method(records)  # type: ignore[attr-defined]
    cls._repr_html_ = to_html  # type: ignore[attr-defined]


_register(ScheduleResult, _schedule_records, _schedule_html)
_register(LevelingResult, _leveling_records, _leveling_html)
_register(CostReportView, _cost_records, _cost_html)
_register(LoadingView, _loading_records, _loading_html)
_register(TaskDetails, _details_records, _details_html)
_register(CalendarApi, _calendar_records, _calendar_html)
