"""Public facade: exported names, the plan 1.2 example, notebook helpers (T35)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import project_planner as pp

pytestmark = pytest.mark.unit

EXPECTED_NAMES = {
    "open_workspace",
    "Workspace",
    "hours",
    "days",
    "TimeQty",
    "CalendarSettings",
    "NodeKind",
    "SizingMode",
    "TimeUnit",
    "DependencyType",
    "WorkUnit",
    "ExceptionKind",
    "Weekday",
    "ScheduleResult",
    "LevelingResult",
    "NodeResult",
    "Job",
    "InlineExecutor",
    "PlannerError",
    "ValidationFailed",
    "ImportFailed",
    "UnsavedChanges",
    "Cancelled",
    "NotFound",
    "Conflict",
    "Issue",
    "Severity",
    "Config",
    "__version__",
}


def test_public_names_exist_and_all_is_consistent() -> None:
    assert set(pp.__all__) >= EXPECTED_NAMES
    for name in pp.__all__:
        assert hasattr(pp, name), name
    assert len(pp.__all__) == len(set(pp.__all__))
    assert isinstance(pp.__version__, str)


def test_float_literals_accepted_by_facade_constructors() -> None:
    assert pp.days(-0.5) == pp.days("-0.5")
    assert pp.hours(1.25) == pp.hours("1.25")
    with pytest.raises(TypeError):
        pp.days(True)


def _build_demo(tmp_path: Path | None = None) -> tuple[pp.Workspace, dict[str, str]]:
    ws = pp.open_workspace(":memory:")
    ws.new_project("Demo", start=date(2026, 10, 5), currency="USD", cost_report_unit="person_days")
    ws.calendar.initialize(
        hours_per_day=8,
        working_days_per_year=220,
        working_weekdays="Mon-Fri",
        workday_start="09:00",
        holidays=[(date(2026, 10, 12), "Holiday")],
        exceptions=[],
    )
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="50")
    ph1 = ws.add_group("Phase 1")
    t1 = ws.add_task("Design", parent=ph1, effort=pp.hours(40))
    ws.set_assignment(t1, alice, percent=80)
    ws.set_assignment(t1, bob, percent=20)
    t2 = ws.add_task("Build", parent=ph1, duration=pp.days(2))
    ws.add_dependency(t1, t2, "FS", lag=pp.days(-0.5))
    return ws, {"alice": alice, "bob": bob, "t1": t1, "t2": t2}


def test_plan_section_1_2_example_runs(tmp_path: Path) -> None:
    ws, ids = _build_demo()
    alice = ids["alice"]

    result = ws.schedule()
    assert result.complete is True
    assert result.project_finish is not None
    assert result.total_cost == Decimal(3600)
    assert result.working_span_days == Decimal("6.5")
    assert len(result.tasks()) == 2
    report = ws.cost_report(unit="person_days")
    assert report.total_cost == Decimal(3600)
    assert ws.cost_report(unit="person_hours").total_cost == Decimal(3600)
    assert ws.loading(alice).segments

    preview = ws.level_preview()
    assert preview.result.kind == "leveling_preview"
    ws.apply_leveling()
    ws.discard_leveling()
    ws.reset_to_dependency_schedule()

    ws.save()
    ws.save_as("Demo v2")
    assert {p.name for p in ws.list_projects()} >= {"Demo v2"}
    first = next(p for p in ws.list_projects() if p.name == "Demo v2")
    ws.load(first.id, discard_unsaved=True)

    csv_path = tmp_path / "demo.csv"
    ws.export_csv(str(csv_path))
    summary = ws.import_csv(str(csv_path), discard_unsaved=True)
    assert summary.nodes == 3
    ws.close()


def test_objects_and_ids_interchangeable() -> None:
    ws, ids = _build_demo()
    ws.schedule()
    assert ws.task_details(ids["t1"]).id == ids["t1"]
    assert ws.loading(ids["alice"]).resource_id == ids["alice"]
    assert ws.wbs_rows().total == 1
    assert ws.wbs_rows(expanded_ids={"g1"}).total == 3
    assert ws.wbs_rows(parent="g1").total == 2
    assert ws.gantt_rows()
    assert ws.dependency_links([ids["t1"], ids["t2"]])
    assert ws.nonworking_ranges(date(2026, 10, 5), date(2026, 10, 18))
    assert ws.project_summary() is not None


def test_job_results_stored_via_state_polling() -> None:
    ws, _ = _build_demo()
    job = ws.submit_schedule(executor=pp.InlineExecutor())
    ws.poll_jobs()
    assert job.done()
    assert ws.state().has_result
    assert ws.result() is not None


def test_repr_html_contains_task_names() -> None:
    ws, ids = _build_demo()
    result = ws.schedule()
    html = result._repr_html_()  # type: ignore[attr-defined]
    assert "<table" in html
    assert "Design" in html and "Build" in html
    assert "Design" in ws.task_details(ids["t1"])._repr_html_()  # type: ignore[attr-defined]
    cost_html = ws.cost_report()._repr_html_()  # type: ignore[attr-defined]
    assert "<table" in cost_html and "person_days" in cost_html
    load_html = ws.loading(ids["alice"])._repr_html_()  # type: ignore[attr-defined]
    assert "Alice" in load_html
    cal_html = ws.calendar._repr_html_()  # type: ignore[attr-defined]
    assert "hours_per_day" in cal_html and "Holiday" in cal_html
    lev_html = ws.level_preview()._repr_html_()  # type: ignore[attr-defined]
    assert "Design" in lev_html


def test_html_escapes_names() -> None:
    ws = pp.open_workspace(":memory:")
    ws.new_project("P", start=date(2026, 10, 5))
    ws.add_task("<b>x</b>", duration=pp.days(1))
    html = ws.schedule()._repr_html_()  # type: ignore[attr-defined]
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_to_records_shape() -> None:
    ws, ids = _build_demo()
    result = ws.schedule()
    records = result.to_records()  # type: ignore[attr-defined]
    assert isinstance(records, list)
    assert [r["name"] for r in records] == ["Design", "Build"]
    assert {"id", "start", "finish", "duration_days", "effort_days", "cost"} <= set(records[0])
    assert records[0]["id"] == ids["t1"]
    assert ws.cost_report().to_records()  # type: ignore[attr-defined]
    assert ws.loading(ids["alice"]).to_records()  # type: ignore[attr-defined]
    assert ws.task_details(ids["t1"]).to_records()[0]["resource_id"]  # type: ignore[attr-defined]


def test_to_dataframe_without_pandas_raises_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    ws, _ = _build_demo()
    result = ws.schedule()
    monkeypatch.setitem(sys.modules, "pandas", None)
    with pytest.raises(ImportError, match=r"pip install project_planner\[notebook\]"):
        result.to_dataframe()  # type: ignore[attr-defined]


@pytest.mark.skipif(importlib.util.find_spec("pandas") is None, reason="pandas not installed")
def test_to_dataframe_with_pandas() -> None:
    ws, _ = _build_demo()
    frame = ws.schedule().to_dataframe()  # type: ignore[attr-defined]
    assert list(frame["name"]) == ["Design", "Build"]
