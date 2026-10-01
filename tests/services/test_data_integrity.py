"""Review batch A (data integrity): dirty tracking of results (G1), one workspace per
database file + revision checks (D12 / finding 1), id reuse (finding 2), ``ProjectInfo``
arguments (finding 3), single-transaction save / load (finding 15), actionable save_as
errors and fault injection (G5), guarded plumbing (finding 16), bad report units
(finding 13) and display names kept off the frozen results (``_named`` known item)."""

from __future__ import annotations

import gc
import pickle
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import project_planner as pp
from project_planner.engine.config import Config
from project_planner.engine.errors import Conflict, NotFound, UnsavedChanges, ValidationFailed
from project_planner.persistence import repositories as repo
from project_planner.persistence import row_ops
from project_planner.services import compute, jobs
from project_planner.services.events import Event, EventKind
from project_planner.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.services

START = date(2026, 10, 5)
SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture(params=["memory", "file"])
def ws(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Workspace]:
    path = ":memory:" if request.param == "memory" else tmp_path / "plans.db"
    workspace = open_workspace(path)
    workspace.new_project("Demo", START)
    yield workspace
    workspace.close()


def build(ws: Workspace) -> tuple[str, str, str]:
    alice = ws.add_resource("Alice", "100")
    a = ws.add_task("A", duration="2d")
    b = ws.add_task("B", duration="2d")
    ws.set_assignment(a, alice, 100)
    ws.set_assignment(b, alice, 100)  # overlap -> leveling delays B
    return alice, a, b


def dump(ws: Workspace) -> list[str]:
    return list(ws._connection.iterdump())


def snapshot(ws: Workspace) -> tuple[Any, ...]:
    st = ws.state()
    return st, ws.project(), ws.result(), ws._results.get("leveling_preview")


# ------------------------------------------------------- G1: results make dirty


def _schedule(ws: Workspace) -> None:
    compute.schedule(ws)


def _preview(ws: Workspace) -> None:
    compute.level_preview(ws)


def _apply(ws: Workspace) -> None:
    compute.apply_leveling(ws)


def _discard(ws: Workspace) -> None:
    compute.discard_leveling(ws)


def _reset(ws: Workspace) -> None:
    compute.reset_to_dependency_schedule(ws)


def _job(ws: Workspace) -> None:
    job = jobs.submit_schedule(ws, executor=jobs.InlineExecutor())
    assert job.stored is True


# (setup run before the save, operation run after it)
_RESULT_OPS: dict[str, tuple[list[Callable[[Workspace], None]], Callable[[Workspace], None]]] = {
    "schedule": ([], _schedule),
    "level_preview": ([_schedule], _preview),
    "apply_leveling": ([_schedule, _preview], _apply),
    "discard_leveling": ([_schedule, _preview], _discard),
    "reset": ([_schedule, _preview, _apply], _reset),
    "job_result_stored": ([], _job),
}


@pytest.mark.parametrize("op", list(_RESULT_OPS))
def test_result_operations_make_the_workspace_dirty(ws: Workspace, op: str) -> None:
    build(ws)
    setup, action = _RESULT_OPS[op]
    for step in setup:
        step(ws)
    info = ws.save_as("saved")
    assert ws.dirty is False and ws.state().dirty is False
    revision = ws.state().revision
    action(ws)
    assert ws.state().revision == revision  # results are not part of the definition
    assert ws.dirty is True and ws.state().dirty is True
    with pytest.raises(UnsavedChanges):
        ws.load(info)
    with pytest.raises(UnsavedChanges):
        ws.new_project("Other", START)
    with pytest.raises(UnsavedChanges):
        ws.import_csv(ws.export_csv())
    ws.save()
    assert ws.dirty is False
    ws.load(info)  # clean again: no prompt needed


def test_applied_leveling_is_not_dropped_silently_by_load(ws: Workspace) -> None:
    """The G1 repro: save_as -> schedule -> level_preview -> apply_leveling -> load."""
    build(ws)
    info = ws.save_as("v1")
    ws.schedule()
    ws.level_preview()
    leveled = ws.apply_leveling()
    with pytest.raises(UnsavedChanges):
        ws.load(info.id)
    assert ws.result() == leveled
    ws.load(info.id, discard_unsaved=True)
    assert ws.result() is None and ws.dirty is False


def test_discarding_nothing_keeps_the_workspace_clean(ws: Workspace) -> None:
    build(ws)
    ws.save_as("clean")
    ws.discard_leveling()  # no preview stored: the stored result set does not change
    ws.reset_to_dependency_schedule()
    assert ws.dirty is False


def test_result_dirty_state_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "w.db"
    with open_workspace(path) as w:
        w.new_project("P", START)
        build(w)
        w.save_as("s")
        w.schedule()
        assert w.dirty
    with open_workspace(path) as w2:
        assert w2.dirty is True
        w2.save()
    with open_workspace(path) as w3:
        assert w3.dirty is False


# ------------------------------------------- D12: one open workspace per file


def test_second_open_of_the_same_file_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "plans.db"
    first = open_workspace(path)
    with pytest.raises(Conflict, match="already open"):
        open_workspace(path)
    with pytest.raises(Conflict):
        open_workspace(str(path.parent / "." / path.name))  # another spelling, same file
    first.close()
    with open_workspace(path) as again:  # released by close()
        assert again.project().name == "Untitled"
    with open_workspace(path):  # released by the with block
        pass
    reopened = pp.open_workspace(path)
    reopened.close()


def test_lock_released_when_workspace_is_collected(tmp_path: Path) -> None:
    path = tmp_path / "plans.db"
    open_workspace(path)  # never closed, immediately garbage
    gc.collect()
    open_workspace(path).close()


def test_memory_workspaces_are_independent() -> None:
    with open_workspace() as a, open_workspace(":memory:") as b:
        a.new_project("A", START)
        assert b.project().name == "Untitled"


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    env_code = f"import sys; sys.path.insert(0, {str(SRC)!r})\n" + textwrap.dedent(code)
    return subprocess.run(
        [sys.executable, "-c", env_code], capture_output=True, text=True, timeout=120
    )


def test_lock_holds_across_processes_and_is_released_at_exit(tmp_path: Path) -> None:
    path = tmp_path / "plans.db"
    with open_workspace(path):
        child = _run_python(
            f"""
            import project_planner as pp
            try:
                pp.open_workspace({str(path)!r})
            except pp.Conflict as exc:
                print("CONFLICT", exc)
            """
        )
        assert child.returncode == 0, child.stderr
        assert "CONFLICT" in child.stdout and "already open" in child.stdout
    # a process that exits without closing must not leave the file locked
    child = _run_python(
        f"""
        import os, project_planner as pp
        ws = pp.open_workspace({str(path)!r})
        ws.add_task("from child")
        os._exit(0)
        """
    )
    assert child.returncode == 0, child.stderr
    with open_workspace(path) as w:
        assert [n.name for n in w.project().nodes] == ["from child"]


# ------------------------------------------- D12: revision-checked writes


def _foreign_write(ws: Workspace, sql: str) -> None:
    """Change the workspace row behind the workspace's back (another writer)."""
    ws._connection.execute(sql, (ws._project_pk,))


@pytest.mark.parametrize(
    "operation",
    ["edit", "new_project", "store_result", "discard", "save", "save_as", "load", "import"],
)
def test_writes_refuse_a_stale_revision(ws: Workspace, operation: str) -> None:
    build(ws)
    ws.schedule()
    info = ws.save_as("base")
    csv_text = ws.export_csv()
    _foreign_write(ws, "UPDATE projects SET revision = revision + 5 WHERE pk = ?")
    before = dump(ws)
    actions: dict[str, Callable[[], object]] = {
        "edit": lambda: ws.add_task("X"),
        "new_project": lambda: ws.new_project("N", START, discard_unsaved=True),
        "store_result": lambda: ws.schedule(),
        "discard": lambda: ws._discard_results(),
        "save": lambda: ws.save(),
        "save_as": lambda: ws.save_as("other"),
        "load": lambda: ws.load(info, discard_unsaved=True),
        "import": lambda: ws.import_csv(csv_text, discard_unsaved=True),
    }
    with pytest.raises(Conflict, match="another connection"):
        actions[operation]()
    assert dump(ws) == before


def test_results_generation_is_checked_too(ws: Workspace) -> None:
    build(ws)
    _foreign_write(ws, "UPDATE projects SET results_gen = results_gen + 1 WHERE pk = ?")
    with pytest.raises(Conflict, match="results generation"):
        ws.schedule()
    assert ws.result() is None


# ------------------------------------------- finding 2: ids are never reused


def test_deleted_saved_project_id_is_not_reused(ws: Workspace) -> None:
    ws.save_as("A")
    b = ws.save_as("B")
    ws.delete_project(b)
    c = ws.save_as("C")
    assert c.id != b.id
    with pytest.raises(NotFound):
        ws.load(b.id, discard_unsaved=True)


# ------------------------------------------- finding 3: ProjectInfo arguments


def test_load_and_delete_accept_project_info(ws: Workspace) -> None:
    ws.add_task("Saved")
    info = ws.save_as("one")
    ws.add_task("Unsaved")
    ws.load(info, discard_unsaved=True)
    assert [n.name for n in ws.project().nodes] == ["Saved"]
    ws.delete_project(info)
    assert ws.list_projects() == []
    with pytest.raises(NotFound):
        ws.delete_project(info)
    with pytest.raises(TypeError):
        ws.load(object())  # type: ignore[arg-type]


# --------------------------- finding 15: one transaction, events see final state


def _begins(ws: Workspace, action: Callable[[], object]) -> int:
    statements: list[str] = []
    ws._connection.set_trace_callback(statements.append)
    try:
        action()
    finally:
        ws._connection.set_trace_callback(None)
    return sum(1 for s in statements if s.strip().upper().startswith("BEGIN"))


def test_save_save_as_and_load_are_one_transaction_each(ws: Workspace) -> None:
    build(ws)
    ws.schedule()
    assert _begins(ws, lambda: ws.save_as("first")) == 1
    ws.add_task("more")
    assert _begins(ws, ws.save) == 1
    info = ws.list_projects()[0]
    ws.add_task("unsaved")
    assert _begins(ws, lambda: ws.load(info, discard_unsaved=True)) == 1


def test_project_replaced_event_sees_clean_state_after_load(ws: Workspace) -> None:
    build(ws)
    info = ws.save_as("s")
    ws.add_task("unsaved")
    seen: list[tuple[Event, bool, int]] = []
    ws.subscribe(lambda e: seen.append((e, ws.dirty, ws.state().revision)))
    ws.load(info, discard_unsaved=True)
    assert [(e.kind, dirty) for e, dirty, _ in seen] == [(EventKind.PROJECT_REPLACED, False)]
    assert seen[0][0].revision == seen[0][2]


# ------------------------------------------- G5: actionable errors, fault injection


def test_duplicate_save_as_name_is_actionable(ws: Workspace) -> None:
    ws.save_as("Demo v1")
    ws.add_task("x")
    before = dump(ws)
    with pytest.raises(Conflict) as err:
        ws.save_as("Demo v1")
    message = str(err.value)
    assert message == "a saved project named 'Demo v1' already exists — choose another name"
    assert "UNIQUE" not in message and "constraint" not in message
    assert dump(ws) == before
    assert ws.dirty is True


class Injected(Exception):
    pass


def _fail_after_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    real = repo.copy_project

    def copy_then_fail(*args: Any, **kwargs: Any) -> int:
        real(*args, **kwargs)  # the copy has been written inside the transaction
        raise Injected

    monkeypatch.setattr(repo, "copy_project", copy_then_fail)


def _fail_in_mark_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise Injected

    monkeypatch.setattr(row_ops, "set_based_on", boom)


@pytest.mark.parametrize("fault", [_fail_after_copy, _fail_in_mark_clean])
@pytest.mark.parametrize("operation", ["save_first", "save_overwrite", "save_as", "load"])
def test_failure_midway_leaves_workspace_and_saved_copies_unchanged(
    ws: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    fault: Callable[[pytest.MonkeyPatch], None],
) -> None:
    build(ws)
    ws.schedule()
    if operation != "save_first":
        info = ws.save_as("existing")
    ws.level_preview()
    ws.add_task("unsaved edit")  # dirty, with results
    before_db, before_ws = dump(ws), snapshot(ws)
    events: list[Event] = []
    ws.subscribe(events.append)
    fault(monkeypatch)
    with pytest.raises(Injected):
        if operation in ("save_first", "save_overwrite"):
            ws.save()
        elif operation == "save_as":
            ws.save_as("new name")
        else:
            ws.load(info, discard_unsaved=True)
    assert dump(ws) == before_db  # saved copies and the workspace rows
    assert snapshot(ws) == before_ws  # the cached workspace (dirty flag included)
    assert ws.dirty is True
    assert events == []
    monkeypatch.undo()
    ws.save()  # still fully usable
    assert ws.dirty is False


# ------------------------------------------- finding 16: plumbing is internal


def test_plumbing_is_not_public(ws: Workspace) -> None:
    for name in (
        "store_result",
        "discard_results",
        "mark_clean",
        "reload",
        "require_clean",
        "connection",
        "results",
        "project_pk",
        "events",
    ):
        assert not hasattr(ws, name), name
    assert isinstance(ws.config, Config)
    with pytest.raises(AttributeError):
        ws.config = Config()  # type: ignore[misc]
    for name in ("state", "result", "dirty", "subscribe", "save", "load", "schedule"):
        assert hasattr(ws, name), name


# ------------------------------------------- finding 13: bad report units


def test_bad_report_unit_is_a_validation_error(ws: Workspace) -> None:
    _, a, _ = build(ws)
    ws.schedule()
    for call in (lambda: ws.cost_report("weeks"), lambda: ws.task_details(a, unit="weeks")):
        with pytest.raises(ValidationFailed) as err:
            call()
        (issue,) = err.value.issues
        assert (issue.code, issue.field) == ("COST_BAD_UNIT", "unit")
        assert "weeks" in issue.message
    assert ws.cost_report("person_days").unit.value == "person_days"


# ------------------------------------------- display names in a side cache


def test_names_are_not_written_onto_results(ws: Workspace) -> None:
    _, a, _ = build(ws)
    plain = pickle.dumps(compute.schedule(ws))
    res = ws.result()
    assert res is not None
    assert "_names" not in vars(res)
    assert pickle.dumps(res) == plain  # nothing extra travels to workers / pickles
    assert {r["id"]: r["name"] for r in res.to_records()}[a] == "A"  # type: ignore[attr-defined]
    ws.rename_node(a, "Renamed")
    res2 = ws.result()
    assert res2 is res
    assert {r["id"]: r["name"] for r in res2.to_records()}[a] == "Renamed"  # type: ignore[attr-defined]


def test_preview_payload_to_workers_has_no_names(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(ws)
    ws.schedule()  # names attached to the cached base result
    sent: list[bytes] = []
    real = jobs.leveling_worker

    def spy(*args: Any) -> Any:
        sent.append(pickle.dumps(args[:3]))
        return real(*args)

    monkeypatch.setattr(jobs, "leveling_worker", spy)
    jobs.submit_leveling_preview(ws, executor=jobs.InlineExecutor()).result()
    assert sent and b"_names" not in sent[0]


# ------------------------------------------- CSV honours the workspace config


def test_csv_round_trip_uses_workspace_config(tmp_path: Path) -> None:
    cfg = Config(max_assignment_percent=Decimal(200))
    with open_workspace(tmp_path / "w.db", config=cfg) as w:
        w.new_project("Wide", START)
        alice = w.add_resource("Alice")
        w.set_assignment(w.add_task("T", duration="1d"), alice, 150)
        text = w.export_csv()
        w.import_csv(text, discard_unsaved=True)
        assert w.project().assignments[0].percent == Decimal(150)
