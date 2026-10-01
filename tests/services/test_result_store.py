"""Result persistence (T31): ScheduleResult <-> run records, restored without rescheduling."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from project_planner.engine.model import Project
from project_planner.engine.results import ScheduleResult
from project_planner.engine.schedule import level, schedule, with_kind
from project_planner.persistence import repositories as repo
from project_planner.persistence.records import RunKind
from project_planner.services.events import EventKind
from project_planner.services.result_store import ResultStore, restore_result, result_to_records
from project_planner.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.services

START = date(2026, 10, 5)


def build_overloaded(ws: Workspace) -> dict[str, str]:
    """Alice is double-booked 60 % + 60 % on t1 / t2, so leveling delays t2."""
    ws.new_project("Demo", START)
    ids = {"alice": ws.add_resource("Alice", "100")}
    ids["g"] = ws.add_group("Phase")
    ids["t1"] = ws.add_task("One", ids["g"], duration="3d")
    ids["t2"] = ws.add_task("Two", ids["g"], effort="24h")
    ids["t3"] = ws.add_task("Three", duration="1d")
    ids["m"] = ws.add_milestone("Done")
    ws.set_assignment(ids["t1"], ids["alice"], 60)
    ws.set_assignment(ids["t2"], ids["alice"], 60)
    ws.add_dependency(ids["t2"], ids["t3"])
    ws.add_dependency(ids["t3"], ids["m"])
    return ids


def leveled_pair(project: Project) -> tuple[ScheduleResult, ScheduleResult, ScheduleResult]:
    base = schedule(project)
    lv = level(project, base)
    return base, lv.result, with_kind(lv.result, "leveled")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "results.db"


def forbid_scheduling(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("loading a stored result must not run the scheduler")

    monkeypatch.setattr("project_planner.engine.schedule.forward_pass", boom)
    monkeypatch.setattr("project_planner.engine.schedule._level", boom)
    monkeypatch.setattr("project_planner.engine.forward_pass.forward_pass", boom)
    monkeypatch.setattr("project_planner.engine.leveling.level", boom)


def test_dependency_only_round_trip_is_equal_and_does_not_reschedule(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_workspace(db) as ws:
        build_overloaded(ws)
        original = schedule(ws.project())
        ws._store_result(original)
    forbid_scheduling(monkeypatch)
    with open_workspace(db) as ws2:
        restored = ws2.result()
        assert restored is not None
        assert restored == original
        state = ws2.state()
        assert (state.has_result, state.has_preview, state.stale_dates, state.stale_costs) == (
            True,
            False,
            False,
            False,
        )


def test_leveled_round_trip_keeps_dates_delays_costs_and_fingerprints(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_workspace(db) as ws:
        ids = build_overloaded(ws)
        base, preview, leveled = leveled_pair(ws.project())
        assert leveled.node(ids["t2"]).leveling_delay_days > 0
        ws._store_result(base)
        ws._store_result(leveled)
    forbid_scheduling(monkeypatch)
    with open_workspace(db) as ws2:
        restored = ws2.result()
        assert restored is not None
        assert restored.kind == "leveled"
        assert restored == leveled
        for nid, row in leveled.nodes.items():
            got = restored.node(nid)
            assert (got.start, got.finish, got.duration_days, got.effort_days, got.cost) == (
                row.start,
                row.finish,
                row.duration_days,
                row.effort_days,
                row.cost,
            )
            assert (got.leveling_delay_minutes, got.leveling_delay_days, got.cost_complete) == (
                row.leveling_delay_minutes,
                row.leveling_delay_days,
                row.cost_complete,
            )
        assert restored.total_cost == leveled.total_cost
        assert restored.project_finish == leveled.project_finish
        assert (restored.schedule_fp, restored.cost_fp) == (leveled.schedule_fp, leveled.cost_fp)
        assert restored.loading == leveled.loading
        assert restored.assignments == leveled.assignments
        assert restored.issues == leveled.issues
        # the dependency-only run is still there, untouched, behind the leveled one
        base_again = ws2._results.get(RunKind.DEPENDENCY_ONLY)
        assert base_again == base
        assert not ws2.state().stale_dates


def test_preview_is_stored_separately_and_current_prefers_leveled(db: Path) -> None:
    with open_workspace(db) as ws:
        build_overloaded(ws)
        base, preview, leveled = leveled_pair(ws.project())
        ws._store_result(base)
        assert ws.state().has_preview is False
        ws._store_result(preview)
        assert ws.state().has_preview is True
        current = ws.result()
        assert current is not None and current.kind == "dependency_only"
        ws._store_result(leveled)  # applying: the preview is consumed
        assert ws.state().has_preview is False
        current = ws.result()
        assert current is not None and current.kind == "leveled"
    with open_workspace(db) as ws2:
        assert ws2._results.kinds() == {RunKind.DEPENDENCY_ONLY, RunKind.LEVELED}
        result = ws2.result()
        assert result is not None and result.kind == "leveled"


def test_new_dependency_only_result_replaces_leveling(db: Path) -> None:
    with open_workspace(db) as ws:
        build_overloaded(ws)
        base, preview, leveled = leveled_pair(ws.project())
        for r in (base, preview, leveled):
            ws._store_result(r)
        ws._store_result(schedule(ws.project()))
        assert ws._results.kinds() == {RunKind.DEPENDENCY_ONLY}
        rows = ws._connection.execute("SELECT COUNT(*) FROM schedule_runs").fetchone()
        assert rows == (1,)
        # no orphaned child rows
        assert ws._connection.execute("SELECT COUNT(*) FROM node_results").fetchone()[0] == len(
            base.nodes
        )


def test_discard_results_by_kind(db: Path) -> None:
    with open_workspace(db) as ws:
        build_overloaded(ws)
        base, preview, leveled = leveled_pair(ws.project())
        ws._store_result(base)
        ws._store_result(preview)
        events = []
        ws.subscribe(events.append)
        ws._discard_results([RunKind.LEVELING_PREVIEW])
        assert ws.state().has_preview is False and ws.state().has_result is True
        assert [e.kind for e in events] == [EventKind.RESULT_STORED]
        ws._discard_results()
        assert ws.result() is None and ws.state().has_result is False
    with open_workspace(db) as ws2:
        assert ws2.result() is None


def test_incomplete_result_with_issues_round_trips(db: Path) -> None:
    with open_workspace(db) as ws:
        ws.new_project("Incomplete", START)
        a = ws.add_task("unsized")
        ws.add_task("sized", duration="1d")
        b = ws.add_task("after", duration="1d")
        ws.add_dependency(a, b)
        r = ws.add_resource("NoRate")  # missing rate -> incomplete cost
        c = ws.add_task("worked", duration="1d")
        ws.set_assignment(c, r, 100)
        original = schedule(ws.project())
        assert not original.complete and not original.cost_complete
        assert original.issues
        ws._store_result(original)
    with open_workspace(db) as ws2:
        restored = ws2.result()
        assert restored == original
        assert restored is not None and restored.project_finish is None
        assert restored.node(b).status == "blocked" and restored.node(b).reason


def test_stored_stale_result_stays_stale_after_reopen(db: Path) -> None:
    with open_workspace(db) as ws:
        ids = build_overloaded(ws)
        ws._store_result(schedule(ws.project()))
        ws.set_sizing(ids["t3"], duration="5d")
        ws.add_task("brand new", duration="1d")
        ws.delete_commit(ws.delete_preview(ids["m"]).token)
        assert ws.state().stale_dates
    with open_workspace(db) as ws2:
        st = ws2.state()
        assert st.stale_dates and st.stale_costs and st.has_result
        stale = ws2.result()  # best-effort view; must not raise
        assert stale is not None
        stored = ws2._results.fingerprints()[RunKind.DEPENDENCY_ONLY]
        assert (stale.schedule_fp, stale.cost_fp) == stored
        assert stale.node(ids["t1"]).scheduled


def test_edit_after_reopen_recosts_a_current_stored_result(db: Path) -> None:
    with open_workspace(db) as ws:
        ids = build_overloaded(ws)
        ws._store_result(schedule(ws.project()))
    with open_workspace(db) as ws2:
        ws2.set_hourly_rate(ids["alice"], "10")
        result = ws2.result()
        assert result is not None
        assert result.total_cost > 0
        assert not ws2.state().stale_costs
        assert result == schedule(ws2.project())


def test_store_is_atomic(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with open_workspace(db) as ws:
        build_overloaded(ws)
        original = schedule(ws.project())
        ws._store_result(original)
        other = schedule(ws.project())

        def boom(*args: object, **kwargs: object) -> int:
            raise RuntimeError("write failed")

        monkeypatch.setattr(repo, "save_run", boom)
        with pytest.raises(RuntimeError):
            ws._store_result(other)
        monkeypatch.undo()
        # the previous run survived the failed replacement
        assert ws._results.kinds() == {RunKind.DEPENDENCY_ONLY}
        assert ws._connection.execute("SELECT COUNT(*) FROM schedule_runs").fetchone() == (1,)
    with open_workspace(db) as ws2:
        assert ws2.result() == original


def test_records_mapping_is_complete(db: Path) -> None:
    with open_workspace(":memory:") as ws:
        build_overloaded(ws)
        _, _, leveled = leveled_pair(ws.project())
        run, nodes, assignments, segments = result_to_records(leveled)
        assert run.kind is RunKind.LEVELED
        assert (run.schedule_fp, run.cost_fp) == (leveled.schedule_fp, leveled.cost_fp)
        assert run.complete is leveled.complete
        assert leveled.project_finish is not None
        assert run.project_finish == leveled.project_finish.date()
        assert run.working_span_minutes == leveled.working_span_minutes
        assert {n.node_id for n in nodes} == set(leveled.nodes)
        assert len(assignments) == len(leveled.assignments)
        assert len(segments) == sum(len(s) for s in leveled.loading.values())
        pk = repo.save_run(ws._connection, ws._project_pk, run, nodes, assignments, segments, ())
        bundle = repo.load_runs(ws._connection, ws._project_pk)[0]
        assert bundle.run.pk == pk
        restored = restore_result(ws.project(), bundle)
        assert restored.nodes == leveled.nodes
        assert restored.loading == leveled.loading


def test_result_store_standalone_cache_and_reset(db: Path) -> None:
    with open_workspace(db) as ws:
        build_overloaded(ws)
        store = ResultStore(ws._connection, ws._project_pk, ws.project)
        assert store.current() is None and store.kinds() == frozenset()
        result = schedule(ws.project())
        store.store(result)
        assert store.get("dependency_only") is result  # cached object, not re-restored
        store.reset()
        again = store.get(RunKind.DEPENDENCY_ONLY)
        assert again == result and again is not result
        assert store.has(RunKind.DEPENDENCY_ONLY) and not store.has(RunKind.LEVELED)
