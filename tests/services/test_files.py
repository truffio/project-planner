"""Project files (T33): save / load / list / delete / CSV import and export."""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from project_planner.engine import schedule as sched
from project_planner.engine.errors import Conflict, ImportFailed, NotFound, UnsavedChanges
from project_planner.services import files
from project_planner.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.services

START = date(2026, 10, 5)
CSV_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "csv"


@pytest.fixture(params=["memory", "file"])
def ws(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Workspace]:
    path = ":memory:" if request.param == "memory" else tmp_path / "plans.db"
    workspace = open_workspace(path)
    workspace.new_project("Demo", START)
    yield workspace
    workspace.close()


def build(ws: Workspace) -> None:
    r1 = ws.add_resource("Alice", "100")
    g = ws.add_group("Phase")
    a = ws.add_task("A", g, duration="3d")
    b = ws.add_task("B", g, duration="2d")
    c = ws.add_task("C", duration="2d")
    ws.add_dependency(a, c)
    ws.set_assignment(a, r1, 100)
    ws.set_assignment(b, r1, 100)  # overlaps A -> leveling delay


def schedule_and_level(ws: Workspace) -> None:
    base = sched.schedule(ws.project(), ws.config)
    ws.store_result(base)
    lev = sched.level(ws.project(), base, ws.config)
    ws.store_result(sched.with_kind(lev.result, "leveled"))


def snapshot(ws: Workspace) -> tuple[Any, Any, Any]:
    return ws.state().revision, ws.project(), ws.result()


# --------------------------------------------------------------- save / load


def test_a11_roundtrip_without_recalculation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "p.db"
    with open_workspace(db) as w:
        w.new_project("Plan", START)
        build(w)
        schedule_and_level(w)
        project, result = w.project(), w.result()
        assert result is not None and result.kind == "leveled"
        assert any(n.leveling_delay_days for n in result.node_results)
        info = files.save_as(w, "v1")
        assert not w.state().dirty
        assert [i.name for i in files.list_projects(w)] == ["v1"]
    with open_workspace(db) as w2:
        for name in ("schedule", "level"):
            monkeypatch.setattr(sched, name, _boom)
        monkeypatch.setattr("project_planner.engine.forward_pass.forward_pass", _boom)
        w2.new_project("Other", START)
        files.load(w2, info.id)
        assert w2.project() == project
        assert w2.result() == result
        assert not w2.state().dirty
        assert w2.state().has_result


def _boom(*_a: Any, **_k: Any) -> None:
    raise AssertionError("scheduler must not run on load")


def test_save_first_then_overwrite(ws: Workspace) -> None:
    build(ws)
    info = files.save(ws)
    assert info.name == "Demo" and not ws.state().dirty
    ws.add_task("Extra", duration="1d")
    assert ws.state().dirty
    again = files.save(ws)
    assert again.id == info.id
    assert len(files.list_projects(ws)) == 1
    assert not ws.state().dirty
    files.load(ws, info.id)
    assert any(n.name == "Extra" for n in ws.project().nodes)


def test_first_save_name_collision_conflicts(ws: Workspace) -> None:
    files.save_as(ws, "Demo")
    ws.new_project("Demo", START, discard_unsaved=True)
    with pytest.raises(Conflict):
        files.save(ws)
    files.save_as(ws, "Demo 2")


def test_save_as_duplicate_and_tracking(ws: Workspace) -> None:
    a = files.save_as(ws, "one")
    with pytest.raises(Conflict):
        files.save_as(ws, "one")
    b = files.save_as(ws, "two")
    assert b.id != a.id
    ws.add_task("x", duration="1d")
    assert files.save(ws).id == b.id  # tracks the latest save_as
    assert [i.name for i in files.list_projects(ws)][0] in {"one", "two"}


def test_list_newest_first_and_delete(ws: Workspace) -> None:
    a = files.save_as(ws, "a")
    b = files.save_as(ws, "b")
    assert [i.id for i in files.list_projects(ws)] == [b.id, a.id]
    files.delete_project(ws, a.id)
    assert [i.id for i in files.list_projects(ws)] == [b.id]
    with pytest.raises(NotFound):
        files.delete_project(ws, a.id)
    with pytest.raises(NotFound):
        files.delete_project(ws, ws.project_pk)
    # workspace untouched and still dirty-free
    assert ws.project().name == "Demo"


def test_delete_tracked_project_then_save_creates_new(ws: Workspace) -> None:
    a = files.save_as(ws, "a")
    files.delete_project(ws, a.id)
    assert not ws.state().dirty
    info = files.save(ws)
    assert info.name == "Demo" and [i.name for i in files.list_projects(ws)] == ["Demo"]


def test_load_unknown_leaves_workspace(ws: Workspace) -> None:
    build(ws)
    before = snapshot(ws)
    with pytest.raises(NotFound):
        files.load(ws, 9999, discard_unsaved=True)
    with pytest.raises(NotFound):
        files.load(ws, ws.project_pk, discard_unsaved=True)
    assert snapshot(ws) == before


def test_unsaved_guard(ws: Workspace) -> None:
    saved = files.save_as(ws, "s")
    ws.add_task("dirty", duration="1d")
    before = snapshot(ws)
    with pytest.raises(UnsavedChanges):
        files.load(ws, saved.id)
    with pytest.raises(UnsavedChanges):
        files.import_csv(ws, CSV_DIR / "valid_minimal.csv")
    assert snapshot(ws) == before
    files.load(ws, saved.id, discard_unsaved=True)
    assert not any(n.name == "dirty" for n in ws.project().nodes)
    ws.add_task("dirty", duration="1d")
    files.import_csv(ws, CSV_DIR / "valid_minimal.csv", discard_unsaved=True)


def test_a26_calendar_survives_save_load(ws: Workspace) -> None:
    ws.calendar.set_hours_per_day("7.5")
    ws.calendar.set_working_days_per_year(250)
    ws.calendar.set_working_weekdays("Sun-Thu")
    ws.calendar.set_workday_start("08:30")
    ws.calendar.add_holiday(date(2026, 12, 25), "Xmas")
    ws.calendar.add_exception(date(2026, 12, 26), "working", "Catch-up")
    settings = ws.calendar.settings()
    info = files.save_as(ws, "cal")
    ws.new_project("Blank", START)
    files.load(ws, str(info.id))
    assert ws.calendar.settings() == settings


# --------------------------------------------------------------------- import


@pytest.mark.parametrize(
    "name",
    sorted(p.name for p in CSV_DIR.glob("invalid_*.csv")),
)
def test_a09_invalid_files_leave_workspace_unchanged(ws: Workspace, name: str) -> None:
    build(ws)
    schedule_and_level(ws)
    before = snapshot(ws)
    with pytest.raises(ImportFailed) as err:
        files.import_csv(ws, CSV_DIR / name, discard_unsaved=True)
    assert err.value.issues
    assert snapshot(ws) == before


def test_a09_multiple_errors_reported(ws: Workspace) -> None:
    with pytest.raises(ImportFailed) as err:
        files.import_csv(ws, CSV_DIR / "invalid_multiple_errors.csv")
    assert len(err.value.issues) == 4


def test_garbage_files_fail(ws: Workspace, tmp_path: Path) -> None:
    junk = tmp_path / "junk.csv"
    junk.write_bytes(bytes(range(256)) * 4)
    before = snapshot(ws)
    with pytest.raises(ImportFailed):
        files.import_csv(ws, junk)
    with pytest.raises(ImportFailed):
        files.import_csv(ws, "not a csv\nat all\n")
    assert snapshot(ws) == before


def test_a10_valid_full_replaces_everything(ws: Workspace) -> None:
    build(ws)
    schedule_and_level(ws)
    files.save_as(ws, "keep")
    summary = files.import_csv(ws, CSV_DIR / "valid_full.csv", discard_unsaved=True)
    p = ws.project()
    assert p.name == "Full Example, with comma"
    assert {r.name for r in p.resources} == {
        "Alice",
        "Bob",
        "Carol (rate unknown)",
        "Dan (volunteer)",
    }
    assert "Alice" not in {n.name for n in p.nodes}
    assert ws.result() is None
    assert (summary.nodes, summary.resources) == (len(p.nodes), 4)
    assert summary.assignments == len(p.assignments) == 4
    assert summary.dependencies == len(p.dependencies) == 6
    assert (summary.holidays, summary.exceptions) == (1, 2)
    assert ws.state().dirty  # imported, not saved anywhere
    assert len(files.list_projects(ws)) == 1  # saved copies untouched


def test_import_minimal_defaults_and_results_note(ws: Workspace) -> None:
    summary = files.import_csv(ws, CSV_DIR / "valid_minimal.csv")
    assert summary.defaults_applied
    summary = files.import_csv(ws, CSV_DIR / "valid_with_results.csv", discard_unsaved=True)
    assert "CSV_RESULTS_IGNORED" in {n.code for n in summary.notes}
    assert ws.result() is None


def test_import_sources(ws: Workspace) -> None:
    text = (CSV_DIR / "valid_minimal.csv").read_text(encoding="utf-8")
    files.import_csv(ws, text)  # CSV text (has newlines)
    expected = ws.project()
    files.import_csv(ws, str(CSV_DIR / "valid_minimal.csv"), discard_unsaved=True)
    files.import_csv(ws, io.StringIO(text), discard_unsaved=True)
    assert ws.project() == expected


def test_a26_custom_calendar_csv(ws: Workspace) -> None:
    files.import_csv(ws, CSV_DIR / "valid_custom_calendar.csv")
    settings = ws.calendar.settings()
    assert str(settings.hours_per_day) == "7.5" and settings.working_days_per_year == 250
    text = files.export_csv(ws)
    files.import_csv(ws, text, discard_unsaved=True)
    assert ws.calendar.settings() == settings


# --------------------------------------------------------------------- export


def test_export_results_stale_and_roundtrip(ws: Workspace, tmp_path: Path) -> None:
    build(ws)
    no_result = files.export_csv(ws)
    assert "RESULT_" not in no_result
    ws.store_result(sched.schedule(ws.project(), ws.config))
    target = tmp_path / "out.csv"
    text = files.export_csv(ws, target)
    assert "RESULT_NODE" in text
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8") == text.replace("\r\n", "\n") or raw.decode("utf-8") == text
    fresh = files.export_csv(ws)
    ws.add_task("later", duration="1d")
    stale = files.export_csv(ws)
    assert stale != fresh.replace("later", "")  # marked stale / extended
    assert "stale" in stale.lower() and "stale" not in fresh.lower().replace("schedule_status", "")
    project = ws.project()
    files.import_csv(ws, files.export_csv(ws), discard_unsaved=True)
    assert ws.project() == project
