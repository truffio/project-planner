"""Workspace edit service (T31): edits, rejections, atomicity, calendar, state, delete."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.errors import (
    Conflict,
    NotFound,
    UnsavedChanges,
    ValidationFailed,
)
from project_planner.engine.model import (
    CalendarSettings,
    DependencyType,
    SizingMode,
    TimeQty,
    TimeUnit,
    WorkUnit,
    days,
    hours,
)
from project_planner.engine.schedule import schedule
from project_planner.persistence import repositories as repo
from project_planner.services.events import Event, EventKind
from project_planner.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.services

START = date(2026, 10, 5)


@pytest.fixture(params=["memory", "file"])
def ws(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Workspace]:
    path = ":memory:" if request.param == "memory" else tmp_path / "plans.db"
    workspace = open_workspace(path)
    workspace.new_project("Demo", START)
    yield workspace
    workspace.close()


def assert_synced(ws: Workspace) -> None:
    """The cached project equals what the database holds, and so does the revision."""
    assert repo.load_project(ws.connection, ws.project_pk) == ws.project()
    record = repo.get_project_record(ws.connection, ws.project_pk)
    assert record.revision == ws.state().revision


def collect(ws: Workspace) -> list[Event]:
    events: list[Event] = []
    ws.subscribe(events.append)
    return events


@pytest.fixture
def plan(ws: Workspace) -> dict[str, str]:
    """g1 > (t1, t2), t3, m1; resources r1, r2; dependency d1 t1 -> t2; assignment t1/r1."""
    ids = {
        "g": ws.add_group("Phase 1"),
        "r1": ws.add_resource("Alice", "100"),
        "r2": ws.add_resource("Bob"),
    }
    ids["t1"] = ws.add_task("Design", ids["g"], effort="40h")
    ids["t2"] = ws.add_task("Build", ids["g"], duration="2d")
    ids["t3"] = ws.add_task("Test")
    ids["m1"] = ws.add_milestone("Done")
    ids["d1"] = ws.add_dependency(ids["t1"], ids["t2"])
    ws.set_assignment(ids["t1"], ids["r1"], 80)
    return ids


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_fresh_workspace_has_untitled_project_and_is_clean() -> None:
    with open_workspace(":memory:") as w:
        assert w.project().name == "Untitled"
        st = w.state()
        assert (st.dirty, st.stale_dates, st.stale_costs, st.has_preview, st.has_result) == (
            False,
            False,
            False,
            False,
            False,
        )
        assert w.result() is None


def test_new_project_settings_and_calendar_argument(ws: Workspace) -> None:
    cal = CalendarSettings.from_values(hours_per_day="7.5", holidays=[(date(2026, 10, 12), "H")])
    ws.new_project("Other", date(2026, 11, 2), currency="EUR", cost_report_unit="person_hours",
                   calendar=cal)  # fmt: skip
    p = ws.project()
    assert (p.name, p.start, p.currency, p.cost_report_unit) == (
        "Other",
        date(2026, 11, 2),
        "EUR",
        WorkUnit.PERSON_HOURS,
    )
    assert ws.calendar.settings() == cal
    assert p.nodes == () and p.resources == ()
    assert_synced(ws)
    assert ws.state().dirty is False


def test_new_project_refuses_to_discard_unsaved_work(ws: Workspace) -> None:
    ws.add_task("x", duration="1d")
    assert ws.state().dirty
    before = ws.project()
    with pytest.raises(UnsavedChanges):
        ws.new_project("Other", START)
    assert ws.project() == before
    ws.new_project("Other", START, discard_unsaved=True)
    assert ws.project().name == "Other"
    assert ws.project().nodes == ()
    assert not ws.state().dirty


def test_new_project_invalid_leaves_workspace_untouched(ws: Workspace) -> None:
    ws.add_task("x", duration="1d")
    before, rev = ws.project(), ws.state().revision
    with pytest.raises(ValidationFailed):
        ws.new_project("Other", START, currency="usd", discard_unsaved=True)
    with pytest.raises(ValidationFailed):
        ws.new_project("Other", START, cost_report_unit="furlongs", discard_unsaved=True)
    assert (ws.project(), ws.state().revision) == (before, rev)


def test_new_project_resets_revision_monotonically_and_drops_results(ws: Workspace) -> None:
    t = ws.add_task("x", duration="1d")
    ws.store_result(schedule(ws.project()))
    rev = ws.state().revision
    ws.new_project("Other", START, discard_unsaved=True)
    assert ws.state().revision > rev
    assert ws.result() is None
    assert t not in {n.id for n in ws.project().nodes}


def test_reopen_file_restores_project_and_ids(tmp_path: Path) -> None:
    path = tmp_path / "w.db"
    with open_workspace(path) as w:
        w.new_project("Persisted", START)
        w.calendar.initialize(hours_per_day=7, holidays=[(date(2026, 10, 12), "H")])
        r = w.add_resource("Alice", "12.50")
        t = w.add_task("A", effort="3d")
        w.set_assignment(t, r, "60")
        definition, rev = w.project(), w.state().revision
    with open_workspace(path) as w2:
        assert w2.project() == definition
        assert w2.state().revision == rev
        assert w2.state().dirty is True  # edited since creation, never saved
        assert w2.add_task("B") == "t2"  # id counter rebuilt from the data


def test_operations_after_close_raise(ws: Workspace) -> None:
    ws.close()
    ws.close()  # idempotent
    with pytest.raises(RuntimeError):
        ws.add_task("x")


# ---------------------------------------------------------------------------
# every edit: effect, revision bump, event, DB in sync
# ---------------------------------------------------------------------------

Edit = Callable[[Workspace, dict[str, str]], None]

EDITS: dict[str, Edit] = {
    "add_resource": lambda w, i: w.add_resource("Carol", "75"),
    "rename_resource": lambda w, i: w.rename_resource(i["r1"], "Alicia"),
    "set_hourly_rate": lambda w, i: w.set_hourly_rate(i["r2"], "60"),
    "clear_hourly_rate": lambda w, i: w.set_hourly_rate(i["r1"], None),
    "remove_resource": lambda w, i: w.remove_resource(i["r1"]),
    "add_group": lambda w, i: w.add_group("G2", i["g"]),
    "add_task": lambda w, i: w.add_task("T", i["g"], duration=days(3)),
    "add_milestone": lambda w, i: w.add_milestone("M", i["g"]),
    "rename_node": lambda w, i: w.rename_node(i["t1"], "Design v2"),
    "move_node": lambda w, i: w.move_node(i["t3"], i["g"], 0),
    "reorder_node": lambda w, i: w.move_node(i["t2"], i["g"], 0),
    "set_sizing": lambda w, i: w.set_sizing(i["t2"], effort="16h"),
    "clear_sizing": lambda w, i: w.set_sizing(i["t2"]),
    "set_assignment_new": lambda w, i: w.set_assignment(i["t2"], i["r2"], 50),
    "set_assignment_update": lambda w, i: w.set_assignment(i["t1"], i["r1"], 40),
    "remove_assignment": lambda w, i: w.remove_assignment(i["t1"], i["r1"]),
    "add_dependency": lambda w, i: w.add_dependency(i["t2"], i["t3"], "SS", lag="-4h"),
    "edit_dependency": lambda w, i: w.edit_dependency(i["d1"], type="FF", lag=days(1)),
    "remove_dependency": lambda w, i: w.remove_dependency(i["d1"]),
    "set_project_start": lambda w, i: w.set_project_start(date(2026, 11, 2)),
    "rename_project": lambda w, i: w.rename_project("Renamed"),
    "set_currency": lambda w, i: w.set_currency("EUR"),
    "set_cost_report_unit": lambda w, i: w.set_cost_report_unit("person_years"),
    "cal_hours": lambda w, i: w.calendar.set_hours_per_day(6),
    "cal_wdpy": lambda w, i: w.calendar.set_working_days_per_year(200),
    "cal_weekdays": lambda w, i: w.calendar.set_working_weekdays("Mon-Thu"),
    "cal_start": lambda w, i: w.calendar.set_workday_start("08:30"),
    "cal_add_holiday": lambda w, i: w.calendar.add_holiday(date(2026, 10, 12), "H"),
    "cal_add_exception": lambda w, i: w.calendar.add_exception(date(2026, 10, 10), "working"),
    "cal_initialize": lambda w, i: w.calendar.initialize(hours_per_day=7),
}


@pytest.mark.parametrize("name", list(EDITS))
def test_edit_bumps_revision_emits_event_and_syncs_db(
    ws: Workspace, plan: dict[str, str], name: str
) -> None:
    events = collect(ws)
    rev = ws.state().revision
    before = ws.project()
    EDITS[name](ws, plan)
    assert ws.project() != before
    assert ws.state().revision == rev + 1
    assert [e.kind for e in events] == [EventKind.EDITED]
    assert events[0].revision == rev + 1
    assert events[0].operation
    assert ws.state().dirty
    assert_synced(ws)


def test_add_operations_return_readable_sequential_ids(ws: Workspace) -> None:
    assert [ws.add_resource("A"), ws.add_resource("B")] == ["r1", "r2"]
    assert [ws.add_group("G"), ws.add_task("T"), ws.add_milestone("M")] == ["g1", "t1", "m1"]
    assert ws.add_task("T2") == "t2"
    assert ws.add_dependency("t1", "t2") == "d1"
    ws.remove_dependency("d1")
    assert ws.add_dependency("t1", "t2") == "d2"  # not reused within a session, never collides


def test_ids_skip_existing_numbers(ws: Workspace) -> None:
    ws.new_project("Imported", START, discard_unsaved=True)
    ws.connection.execute(
        "INSERT INTO wbs_nodes VALUES (?, 't7', 'x', 'task', NULL, 0, 'none', NULL, NULL)",
        (ws.project_pk,),
    )
    ws.reload()
    assert ws.add_task("next") == "t8"


def test_objects_and_ids_are_interchangeable(ws: Workspace) -> None:
    class Handle:
        def __init__(self, ident: str) -> None:
            self.id = ident

    t = Handle(ws.add_task("A"))
    u = Handle(ws.add_task("B"))
    r = Handle(ws.add_resource("R"))
    ws.set_assignment(t, r, 50)
    d = ws.add_dependency(t, u)
    ws.rename_node(ws.node(t), "A2")
    ws.set_sizing(t, duration="1d")
    ws.edit_dependency(Handle(d), lag="1d")
    assert ws.node(t.id).name == "A2"
    assert ws.node(t).sizing == days(1)
    assert ws.resource(r).id == r.id
    assert ws.dependency(d).lag == days(1)


def test_reads_raise_not_found(ws: Workspace) -> None:
    for read in (ws.node, ws.resource, ws.dependency):
        with pytest.raises(NotFound):
            read("nope")


# --- details of individual edits ---------------------------------------------


def test_sizing_is_stored_as_entered(ws: Workspace) -> None:
    t = ws.add_task("A", effort="40h")
    assert (ws.node(t).sizing_mode, ws.node(t).sizing) == (SizingMode.EFFORT, hours(40))
    ws.set_sizing(t, duration=days("1.5"))
    node = ws.node(t)
    assert node.sizing_mode is SizingMode.DURATION
    assert node.sizing == TimeQty(Decimal("1.5"), TimeUnit.DAYS)
    ws.set_sizing(t)
    assert (ws.node(t).sizing_mode, ws.node(t).sizing) == (SizingMode.NONE, None)
    assert_synced(ws)


def test_bare_numbers_are_type_errors_unitless_strings_are_validation_errors(
    ws: Workspace,
) -> None:
    t = ws.add_task("A")
    u = ws.add_task("B")
    d = ws.add_dependency(t, u)
    rev = ws.state().revision
    with pytest.raises(TypeError):
        ws.add_task("x", duration=5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ws.set_sizing(t, effort=2.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ws.add_dependency(t, u, lag=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ws.edit_dependency(d, lag=Decimal(1))  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        ws.set_sizing(t, duration="40")
    assert ws.state().revision == rev


def test_sizing_rejections(ws: Workspace) -> None:
    t = ws.add_task("A")
    g = ws.add_group("G")
    with pytest.raises(ValidationFailed) as both:
        ws.set_sizing(t, duration="1d", effort="8h")
    assert both.value.issues[0].field == "sizing"
    with pytest.raises(ValidationFailed):
        ws.add_task("x", duration="1d", effort="8h")
    with pytest.raises(ValidationFailed):
        ws.set_sizing(t, duration="-1d")
    with pytest.raises(ValidationFailed):
        ws.set_sizing(g, duration="1d")
    with pytest.raises(NotFound):
        ws.set_sizing("nope", duration="1d")


def test_assignment_upsert_updates_the_existing_pair(ws: Workspace) -> None:
    t, r = ws.add_task("A", duration="1d"), ws.add_resource("R")
    ws.set_assignment(t, r, 30)
    ws.set_assignment(t, r, "45.5")
    assert [(a.key, a.percent) for a in ws.project().assignments] == [((t, r), Decimal("45.5"))]
    rows = ws.connection.execute("SELECT percent FROM assignments").fetchall()
    assert rows == [("45.5",)]
    ws.remove_assignment(t, r)
    assert ws.project().assignments == ()
    with pytest.raises(NotFound):
        ws.remove_assignment(t, r)


@pytest.mark.parametrize("percent", [0, -5, 100.5, 101, "abc", "NaN"])
def test_assignment_percent_out_of_range_is_rejected_on_the_spot(
    ws: Workspace, percent: Any
) -> None:
    t, r = ws.add_task("A", duration="1d"), ws.add_resource("R")
    rev = ws.state().revision
    if isinstance(percent, float):
        with pytest.raises(TypeError):
            ws.set_assignment(t, r, percent)
    else:
        with pytest.raises(ValidationFailed) as ei:
            ws.set_assignment(t, r, percent)
        assert {i.field for i in ei.value.issues} == {"percent"}
        assert ei.value.issues[0].object_id == f"{t}/{r}"
    assert ws.state().revision == rev
    assert ws.project().assignments == ()


def test_assignment_maximum_percent_follows_config(tmp_path: Path) -> None:
    from project_planner.engine.config import Config

    with open_workspace(":memory:", config=Config(max_assignment_percent=Decimal(200))) as w:
        t, r = w.add_task("A", duration="1d"), w.add_resource("R")
        w.set_assignment(t, r, 150)
        with pytest.raises(ValidationFailed):
            w.set_assignment(t, r, 201)
        w.set_assignment(t, r, 100)  # exactly the limit of the default would also pass


def test_assignment_target_checks(ws: Workspace, plan: dict[str, str]) -> None:
    with pytest.raises(NotFound):
        ws.set_assignment("nope", plan["r1"], 10)
    with pytest.raises(NotFound):
        ws.set_assignment(plan["t1"], "nope", 10)
    for not_task in (plan["g"], plan["m1"]):
        with pytest.raises(ValidationFailed) as ei:
            ws.set_assignment(not_task, plan["r1"], 10)
        assert ei.value.issues[0].code == "ASSIGN_NOT_TASK"


def test_rate_validation(ws: Workspace) -> None:
    with pytest.raises(ValidationFailed) as ei:
        ws.add_resource("R", "-1")
    assert ei.value.issues[0].field == "hourly_rate"
    with pytest.raises(TypeError):
        ws.add_resource("R", 1.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        ws.add_resource("R", "cheap")
    r = ws.add_resource("R", Decimal("0"))
    assert ws.resource(r).hourly_rate == Decimal(0)  # an explicit zero is not "missing"
    ws.set_hourly_rate(r, None)
    assert ws.resource(r).hourly_rate is None
    assert_synced(ws)


def test_blank_names_are_rejected(ws: Workspace) -> None:
    t = ws.add_task("A")
    for call in (
        lambda: ws.add_task("  "),
        lambda: ws.add_group(""),
        lambda: ws.add_resource(""),
        lambda: ws.rename_node(t, " "),
        lambda: ws.rename_project(""),
    ):
        with pytest.raises(ValidationFailed) as ei:
            call()
        assert ei.value.issues[0].field == "name"


def test_project_setting_rejections(ws: Workspace) -> None:
    with pytest.raises(ValidationFailed):
        ws.set_currency("dollars")
    with pytest.raises(ValidationFailed) as ei:
        ws.set_cost_report_unit("person_weeks")
    assert ei.value.issues[0].field == "cost_report_unit"
    with pytest.raises(TypeError):
        ws.set_project_start("2026-10-05")  # type: ignore[arg-type]
    ws.set_cost_report_unit(WorkUnit.PERSON_HOURS)
    assert ws.project().cost_report_unit is WorkUnit.PERSON_HOURS


def test_rename_project_updates_row_name(ws: Workspace) -> None:
    ws.rename_project("Brand new")
    assert ws.project().name == "Brand new"
    assert repo.get_project_record(ws.connection, ws.project_pk).name == "Brand new"


# --- node structure -----------------------------------------------------------


def test_parent_must_be_a_group(ws: Workspace, plan: dict[str, str]) -> None:
    for bad_parent in (plan["t1"], plan["m1"]):
        for call in (
            lambda p=bad_parent: ws.add_task("x", p),
            lambda p=bad_parent: ws.add_group("x", p),
            lambda p=bad_parent: ws.add_milestone("x", p),
            lambda p=bad_parent: ws.move_node(plan["t3"], p, 0),
        ):
            with pytest.raises(ValidationFailed) as ei:
                call()
            assert ei.value.issues[0].code == "NODE_PARENT_NOT_GROUP"
            assert ei.value.issues[0].field == "parent_id"
    with pytest.raises(NotFound):
        ws.add_task("x", "nope")


def test_new_nodes_are_appended_last_among_siblings(ws: Workspace, plan: dict[str, str]) -> None:
    new = ws.add_task("last", plan["g"])
    assert [n.id for n in ws.project().children(plan["g"])] == [plan["t1"], plan["t2"], new]
    top = ws.add_task("top")
    assert [n.id for n in ws.project().children(None)][-1] == top


def test_move_and_reorder(ws: Workspace, plan: dict[str, str]) -> None:
    g, t1, t2, t3 = plan["g"], plan["t1"], plan["t2"], plan["t3"]
    ws.move_node(t3, g, 1)
    assert [n.id for n in ws.project().children(g)] == [t1, t3, t2]
    assert ws.node(t3).parent_id == g
    ws.move_node(t1, g, 2)  # index counts siblings without the moved node
    assert [n.id for n in ws.project().children(g)] == [t3, t2, t1]
    ws.move_node(t2, None, 0)
    assert ws.project().children(None)[0].id == t2
    assert_synced(ws)


def test_move_updates_only_changed_rows(ws: Workspace, plan: dict[str, str]) -> None:
    statements: list[str] = []
    ws.connection.set_trace_callback(statements.append)
    ws.move_node(plan["t3"], None, 0)
    ws.connection.set_trace_callback(None)
    updates = [s for s in statements if s.startswith("UPDATE wbs_nodes")]
    assert 1 <= len(updates) <= 4


def test_move_rejections(ws: Workspace, plan: dict[str, str]) -> None:
    sub = ws.add_group("Sub", plan["g"])
    deeper = ws.add_group("Deeper", sub)
    rev = ws.state().revision
    for target in (plan["g"], sub, deeper):  # itself or a descendant
        with pytest.raises(ValidationFailed) as ei:
            ws.move_node(plan["g"], target, 0)
        assert ei.value.issues[0].code == "NODE_PARENT_CYCLE"
    with pytest.raises(ValidationFailed) as ei2:
        ws.move_node(plan["t1"], plan["g"], 99)
    assert ei2.value.issues[0].field == "index"
    with pytest.raises(ValidationFailed):
        ws.move_node(plan["t1"], plan["g"], -1)
    with pytest.raises(NotFound):
        ws.move_node("nope", None, 0)
    with pytest.raises(NotFound):
        ws.move_node(plan["t1"], "nope", 0)
    assert ws.state().revision == rev
    assert_synced(ws)


# --- dependencies -------------------------------------------------------------


def test_dependency_defaults_and_as_entered_lag(ws: Workspace) -> None:
    a, b = ws.add_task("A", duration="1d"), ws.add_task("B", duration="1d")
    d = ws.add_dependency(a, b, "ss", lag="-4h")
    dep = ws.dependency(d)
    assert (dep.type, dep.lag) == (DependencyType.SS, hours(-4))
    d2 = ws.add_dependency(b, ws.add_task("C"))
    assert (ws.dependency(d2).type, ws.dependency(d2).lag) == (DependencyType.FS, days(0))
    ws.edit_dependency(d, lag=days("-0.5"))
    assert ws.dependency(d).lag == TimeQty(Decimal("-0.5"), TimeUnit.DAYS)
    assert ws.dependency(d).type is DependencyType.SS
    ws.edit_dependency(d, type=DependencyType.FF)
    assert ws.dependency(d).type is DependencyType.FF
    with pytest.raises(ValueError):
        ws.edit_dependency(d)
    assert_synced(ws)


def test_dependency_self_group_and_unknown_endpoints(ws: Workspace, plan: dict[str, str]) -> None:
    rev = ws.state().revision
    with pytest.raises(ValidationFailed) as ei:
        ws.add_dependency(plan["t1"], plan["t1"])
    assert ei.value.issues[0].code == "DEP_SELF"
    with pytest.raises(ValidationFailed) as eg:
        ws.add_dependency(plan["g"], plan["t3"])
    assert (eg.value.issues[0].code, eg.value.issues[0].field) == ("DEP_GROUP_ENDPOINT", "pred_id")
    with pytest.raises(ValidationFailed) as eg2:
        ws.add_dependency(plan["g"], plan["g"])
    assert {i.code for i in eg2.value.issues} == {"DEP_SELF", "DEP_GROUP_ENDPOINT"}
    with pytest.raises(NotFound):
        ws.add_dependency("nope", plan["t3"])
    with pytest.raises(NotFound):
        ws.add_dependency(plan["t3"], "nope")
    with pytest.raises(ValidationFailed) as et:
        ws.add_dependency(plan["t1"], plan["t3"], "XY")
    assert et.value.issues[0].field == "type"
    assert ws.state().revision == rev


def test_dependency_cycle_names_every_member(ws: Workspace) -> None:
    a, b, c, other = (ws.add_task(n, duration="1d") for n in "ABCD")
    ws.add_dependency(a, b)
    ws.add_dependency(b, c)
    ws.add_dependency(other, a)
    rev = ws.state().revision
    with pytest.raises(ValidationFailed) as ei:
        ws.add_dependency(c, a)
    (issue,) = ei.value.issues
    assert issue.code == "DEP_CYCLE"
    assert all(member in issue.message for member in (a, b, c))
    assert other not in issue.message
    assert ws.state().revision == rev
    # an independent second route is fine, a reverse of the existing edge is not
    ws.add_dependency(a, c)
    with pytest.raises(ValidationFailed):
        ws.add_dependency(b, a)


def test_duplicate_dependency_rejected_but_other_type_allowed(ws: Workspace) -> None:
    a, b = ws.add_task("A"), ws.add_task("B")
    d = ws.add_dependency(a, b, "FS")
    with pytest.raises(ValidationFailed) as ei:
        ws.add_dependency(a, b, "FS", lag="1d")
    assert ei.value.issues[0].code == "DEP_DUPLICATE"
    d2 = ws.add_dependency(a, b, "SS")
    with pytest.raises(ValidationFailed):
        ws.edit_dependency(d2, type="FS")
    ws.edit_dependency(d, type="FS", lag="2d")  # editing itself is not a duplicate
    ws.remove_dependency(d2)
    with pytest.raises(NotFound):
        ws.remove_dependency(d2)


# ---------------------------------------------------------------------------
# atomicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["add_task", "set_assignment_update", "move_node", "cal_hours"])
def test_failure_mid_edit_leaves_database_cache_and_listeners_untouched(
    ws: Workspace, plan: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    events = collect(ws)
    before, rev = ws.project(), ws.state().revision

    def boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("disk on fire")

    # fails after the edit's own rows were written, inside the transaction
    monkeypatch.setattr(repo, "bump_revision", boom)
    with pytest.raises(RuntimeError):
        EDITS.get(name, lambda w, i: w.move_node(i["t3"], i["g"], 0))(ws, plan)
    monkeypatch.undo()
    assert ws.project() == before
    assert ws.state().revision == rev
    assert events == []
    assert_synced(ws)
    assert ws.add_task("after") == "t4"  # id counters were not consumed


def test_failure_in_row_write_leaves_everything_unchanged(
    ws: Workspace, plan: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_planner.persistence import row_ops

    before, rev = ws.project(), ws.state().revision

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("nope")

    monkeypatch.setattr(row_ops, "update_node_positions", boom)
    with pytest.raises(RuntimeError):
        ws.move_node(plan["t3"], plan["g"], 0)
    monkeypatch.undo()
    assert (ws.project(), ws.state().revision) == (before, rev)
    assert_synced(ws)


def test_database_constraint_failure_surfaces_as_conflict_and_changes_nothing(
    ws: Workspace, plan: dict[str, str]
) -> None:
    before, rev = ws.project(), ws.state().revision
    # sneak a row in behind the service's back so the next insert violates the key
    ws.connection.execute(
        "INSERT INTO wbs_nodes VALUES (?, 't9', 'ghost', 'task', NULL, 0, 'none', NULL, NULL)",
        (ws.project_pk,),
    )
    ws._ids.note("t8")  # force the allocator onto the colliding id
    with pytest.raises(Conflict):
        ws.add_task("collides")
    ws.connection.execute("DELETE FROM wbs_nodes WHERE id = 't9'")
    assert (ws.project(), ws.state().revision) == (before, rev)
    assert_synced(ws)


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------


def test_calendar_initialize_defaults(ws: Workspace) -> None:
    ws.calendar.initialize(hours_per_day=7, working_days_per_year=200, workday_start="08:00")
    ws.calendar.initialize()  # omitted arguments take the section 2.3 defaults, not "keep"
    assert ws.calendar.settings() == CalendarSettings()
    s = ws.calendar.settings()
    assert (s.hours_per_day, s.working_days_per_year) == (Decimal(8), 220)
    assert str(s.workday_start)[:5] == "09:00"
    assert (s.holidays, s.exceptions) == ((), ())
    assert_synced(ws)


def test_calendar_initialize_full_and_persisted(ws: Workspace) -> None:
    ws.calendar.initialize(
        hours_per_day="7.5",
        working_days_per_year=210,
        working_weekdays="Mon-Thu",
        workday_start="08:30",
        holidays=[(date(2026, 10, 12), "Holiday")],
        exceptions=[(date(2026, 10, 10), "working", "Saturday shift")],
    )
    s = ws.calendar.settings()
    assert s.hours_per_day == Decimal("7.5")
    assert [(h.date, h.name) for h in s.holidays] == [(date(2026, 10, 12), "Holiday")]
    assert [(e.date, e.kind.value, e.name) for e in s.exceptions] == [
        (date(2026, 10, 10), "working", "Saturday shift")
    ]
    assert_synced(ws)


def test_calendar_initialize_is_all_or_nothing_and_reports_every_bad_field(
    ws: Workspace,
) -> None:
    ws.calendar.initialize(hours_per_day="7.5", holidays=[(date(2026, 10, 12), "H")])
    before, rev = ws.calendar.settings(), ws.state().revision
    with pytest.raises(ValidationFailed) as ei:
        ws.calendar.initialize(
            hours_per_day=0,
            working_days_per_year=400,
            working_weekdays="",
            workday_start="25:00",
            holidays=[(date(2026, 10, 12), "A"), (date(2026, 10, 12), "B")],
            exceptions=[(date(2026, 10, 10), "working"), (date(2026, 10, 10), "nonworking")],
        )
    fields = {i.field for i in ei.value.issues}
    assert {
        "hours_per_day",
        "working_days_per_year",
        "working_weekdays",
        "workday_start",
        "holidays",
        "exceptions",
    } <= fields
    assert ws.calendar.settings() == before
    assert ws.state().revision == rev
    assert_synced(ws)


def test_calendar_cross_field_rule(ws: Workspace) -> None:
    with pytest.raises(ValidationFailed) as ei:
        ws.calendar.initialize(hours_per_day=8, workday_start="20:00")
    assert "workday_start" in {i.field for i in ei.value.issues}
    with pytest.raises(ValidationFailed):
        ws.calendar.set_workday_start("17:00")  # 17:00 + 8 h > 24:00
    with pytest.raises(TypeError):
        ws.calendar.initialize(nonsense=1)  # type: ignore[call-arg]


def test_calendar_incremental_edits_and_their_rejections(ws: Workspace) -> None:
    day = date(2026, 10, 12)
    ws.calendar.add_holiday(day, "H")
    with pytest.raises(ValidationFailed):
        ws.calendar.add_holiday(day, "again")
    with pytest.raises(ValidationFailed):
        ws.calendar.add_exception(day, "working")  # conflicts with the holiday
    ws.calendar.remove_holiday(day)
    with pytest.raises(NotFound):
        ws.calendar.remove_holiday(day)
    ws.calendar.add_exception(date(2026, 10, 10), "working", "x")
    with pytest.raises(ValidationFailed):
        ws.calendar.add_exception(date(2026, 10, 11), "sometimes")
    ws.calendar.remove_exception(date(2026, 10, 10))
    with pytest.raises(NotFound):
        ws.calendar.remove_exception(date(2026, 10, 10))
    with pytest.raises(ValidationFailed) as e1:
        ws.calendar.set_hours_per_day(25)
    assert e1.value.issues[0].field == "hours_per_day"
    with pytest.raises(ValidationFailed) as e2:
        ws.calendar.set_working_days_per_year("220.5")
    assert e2.value.issues[0].field == "working_days_per_year"
    ws.calendar.set_working_days_per_year(366)
    assert ws.calendar.settings().working_days_per_year == 366
    assert_synced(ws)


def test_new_project_with_calendar_object_instance(ws: Workspace) -> None:
    ws.new_project("X", START, calendar=CalendarSettings.from_values(hours_per_day=6))
    assert ws.calendar.settings().hours_per_day == 6
    assert "6" in repr(ws.calendar)


# ---------------------------------------------------------------------------
# state transitions and staleness
# ---------------------------------------------------------------------------


def test_state_transitions(ws: Workspace) -> None:
    st = ws.state()
    assert (st.dirty, st.stale_dates, st.stale_costs, st.has_result, st.has_preview) == (
        False,
        False,
        False,
        False,
        False,
    )
    alice = ws.add_resource("Alice", "100")
    t = ws.add_task("A", duration="2d")
    other = ws.add_task("B", duration="1d")
    ws.set_assignment(t, alice, 100)
    assert ws.state().dirty and not ws.state().stale_dates  # no result yet

    ws.store_result(schedule(ws.project()))
    st = ws.state()
    assert (st.has_result, st.stale_dates, st.stale_costs, st.has_preview) == (
        True,
        False,
        False,
        False,
    )
    result = ws.result()
    assert result is not None and result.total_cost == Decimal(1600)

    ws.rename_node(t, "Renamed")
    ws.rename_resource(alice, "Alicia")
    ws.set_currency("EUR")
    st = ws.state()
    assert (st.stale_dates, st.stale_costs) == (False, False)

    ws.set_hourly_rate(alice, "150")  # rates only: costs recomputed immediately
    st = ws.state()
    assert (st.stale_dates, st.stale_costs) == (False, False)
    refreshed = ws.result()
    assert refreshed is not None
    assert refreshed.total_cost == Decimal(2400)
    assert refreshed.node(t).start == result.node(t).start
    assert refreshed.node(t).finish == result.node(t).finish
    assert refreshed.schedule_fp == result.schedule_fp
    assert refreshed.cost_fp != result.cost_fp

    ws.set_sizing(other, duration="3d")
    st = ws.state()
    assert st.stale_dates and st.stale_costs
    assert ws.result() is not None  # the stale result is kept, not discarded
    assert_synced(ws)


def test_recost_is_persisted_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "w.db"
    with open_workspace(path) as w:
        w.new_project("P", START)
        r = w.add_resource("R", "100")
        t = w.add_task("A", duration="1d")
        w.set_assignment(t, r, 100)
        w.store_result(schedule(w.project()))
        w.set_hourly_rate(r, "200")
        expected = w.result()
    with open_workspace(path) as w2:
        assert not w2.state().stale_costs and not w2.state().stale_dates
        assert w2.result() == expected
        assert expected is not None and expected.total_cost == Decimal(1600)


@pytest.mark.parametrize(
    ("edit", "stales"),
    [
        (lambda w, i: w.calendar.set_hours_per_day(7), True),
        (lambda w, i: w.calendar.set_working_weekdays("Mon-Thu"), True),
        (lambda w, i: w.calendar.set_workday_start("08:00"), True),
        (lambda w, i: w.calendar.add_holiday(date(2026, 10, 7), "H"), True),
        (lambda w, i: w.calendar.add_exception(date(2026, 10, 10), "working"), True),
        (lambda w, i: w.calendar.initialize(hours_per_day=6), True),
        (lambda w, i: w.calendar.set_working_days_per_year(180), False),
        (lambda w, i: w.set_cost_report_unit("person_hours"), False),
        (lambda w, i: w.set_project_start(date(2026, 10, 12)), True),
        (lambda w, i: w.add_task("new", duration="1d"), True),
        (lambda w, i: w.set_sizing(i["t"], duration="3d"), True),
        (lambda w, i: w.add_dependency(i["t"], i["u"]), True),
        (lambda w, i: w.set_assignment(i["t"], i["r"], 50), True),
        (lambda w, i: w.move_node(i["u"], None, 0), True),
        (lambda w, i: w.set_hourly_rate(i["r"], "1"), False),
    ],
)
def test_staleness_rules(
    ws: Workspace, edit: Callable[[Workspace, dict[str, str]], None], stales: bool
) -> None:
    ids = {"t": ws.add_task("A", duration="2d"), "u": ws.add_task("B", duration="1d")}
    ids["r"] = ws.add_resource("R", "10")
    ws.set_assignment(ids["t"], ids["r"], 100)
    ws.store_result(schedule(ws.project()))
    assert not ws.state().stale_dates
    edit(ws, ids)
    assert ws.state().stale_dates is stales
    assert ws.state().stale_costs is stales


def test_report_unit_change_refreshes_stored_views(ws: Workspace) -> None:
    r = ws.add_resource("R", "100")
    t = ws.add_task("A", duration="1d")
    ws.set_assignment(t, r, 100)
    ws.store_result(schedule(ws.project()))
    first = ws.result()
    assert first is not None and first.work_unit == "person_days"
    ws.set_cost_report_unit("person_hours")
    second = ws.result()
    assert second is not None
    assert (second.work_unit, second.work_qty) == ("person_hours", Decimal(8))
    assert second.total_cost == first.total_cost
    ws.calendar.set_working_days_per_year(100)  # not stale, still no change to dates
    assert not ws.state().stale_dates


def test_store_result_does_not_bump_revision_and_emits(ws: Workspace) -> None:
    ws.add_task("A", duration="1d")
    events = collect(ws)
    rev = ws.state().revision
    ws.store_result(schedule(ws.project()))
    assert ws.state().revision == rev
    assert [(e.kind, e.revision) for e in events] == [(EventKind.RESULT_STORED, rev)]


def test_dirty_follows_mark_clean(ws: Workspace) -> None:
    ws.add_task("A")
    assert ws.state().dirty
    ws.mark_clean()
    assert not ws.state().dirty
    with pytest.raises(UnsavedChanges):
        ws.add_task("B")
        ws.require_clean()
    ws.require_clean(discard_unsaved=True)


def test_unsubscribe_and_failing_callbacks(ws: Workspace) -> None:
    events: list[Event] = []
    off = ws.subscribe(events.append)

    def bad(event: Event) -> None:
        raise RuntimeError("listener bug")

    ws.subscribe(bad)
    ws.add_task("A")  # the failing listener must not break the edit or the other listener
    assert len(events) == 1
    off()
    off()
    ws.add_task("B")
    assert len(events) == 1


# ---------------------------------------------------------------------------
# delete preview / commit
# ---------------------------------------------------------------------------


def test_delete_preview_lists_descendants_dependencies_and_assignments(
    ws: Workspace, plan: dict[str, str]
) -> None:
    ws.add_dependency(plan["t2"], plan["t3"])
    ws.set_assignment(plan["t2"], plan["r2"], 10)
    rev = ws.state().revision
    before = ws.project()
    preview = ws.delete_preview([plan["g"]])
    assert preview.nodes == (plan["g"], plan["t1"], plan["t2"])
    assert set(preview.dependencies) == {"d1", "d2"}
    assert set(preview.assignments) == {(plan["t1"], plan["r1"]), (plan["t2"], plan["r2"])}
    assert preview.resources == ()
    assert ws.project() == before and ws.state().revision == rev  # a preview changes nothing
    assert ws.delete_preview(plan["t3"]).dependencies == ("d2",)


def test_delete_commit_removes_everything_in_the_preview(
    ws: Workspace, plan: dict[str, str]
) -> None:
    ws.add_dependency(plan["t2"], plan["t3"])
    events = collect(ws)
    rev = ws.state().revision
    preview = ws.delete_preview([plan["g"]])
    ws.delete_commit(preview.token)
    p = ws.project()
    assert {n.id for n in p.nodes} == {plan["t3"], plan["m1"]}
    assert p.dependencies == () and p.assignments == ()
    assert {r.id for r in p.resources} == {plan["r1"], plan["r2"]}
    assert ws.state().revision == rev + 1
    assert [e.kind for e in events] == [EventKind.EDITED]
    assert set(events[0].ids) >= set(preview.nodes)
    assert_synced(ws)
    with pytest.raises(Conflict):  # a token is single-use
        ws.delete_commit(preview.token)


def test_delete_commit_conflicts_when_revision_changed(ws: Workspace, plan: dict[str, str]) -> None:
    preview = ws.delete_preview([plan["t3"]])
    ws.rename_node(plan["t1"], "touched")
    before = ws.project()
    with pytest.raises(Conflict):
        ws.delete_commit(preview.token)
    assert ws.project() == before
    fresh = ws.delete_preview([plan["t3"]])
    ws.delete_commit(fresh.token)
    assert not ws.project().has_node(plan["t3"])


def test_delete_commit_unknown_token_and_unknown_ids(ws: Workspace) -> None:
    with pytest.raises(Conflict):
        ws.delete_commit("no-such-token")
    with pytest.raises(NotFound):
        ws.delete_preview(["nope"])
    with pytest.raises(NotFound):
        ws.delete_preview([], resources=["nope"])


def test_delete_resource_through_preview_and_directly(ws: Workspace, plan: dict[str, str]) -> None:
    preview = ws.delete_preview([], resources=[plan["r1"]])
    assert preview.assignments == ((plan["t1"], plan["r1"]),)
    ws.delete_commit(preview.token)
    assert not ws.project().has_resource(plan["r1"]) and ws.project().assignments == ()
    ws.set_assignment(plan["t3"], plan["r2"], 100)
    ws.remove_resource(plan["r2"])
    assert ws.project().resources == () and ws.project().assignments == ()
    assert_synced(ws)
    with pytest.raises(NotFound):
        ws.remove_resource(plan["r2"])


def test_deleting_marks_results_stale(ws: Workspace, plan: dict[str, str]) -> None:
    ws.store_result(schedule(ws.project()))
    ws.delete_commit(ws.delete_preview(plan["t3"]).token)
    assert ws.state().stale_dates


# ---------------------------------------------------------------------------
# performance: an edit must not rewrite the project
# ---------------------------------------------------------------------------


def test_edit_on_10k_task_project_writes_only_touched_rows(tmp_path: Path) -> None:
    n = 10_000
    builder = ProjectBuilder(start=START).resource("r1", rate=100)
    builder.group("g1")
    for i in range(n):
        builder.task(f"t{i + 1}", duration="1d", parent="g1")
    big = builder.build()
    with open_workspace(tmp_path / "big.db") as w:
        repo.save_project(w.connection, w.project_pk, big)
        w.reload()
        assert len(w.project().nodes) == n + 1

        statements: list[str] = []
        w.connection.set_trace_callback(statements.append)
        started = time.perf_counter()
        w.set_sizing("t5000", effort="3d")
        w.rename_node("t7", "renamed")
        w.set_assignment("t9", "r1", 50)
        w.add_dependency("t1", "t2")
        new_id = w.add_task("one more", "g1", duration="2d")
        elapsed = time.perf_counter() - started
        w.connection.set_trace_callback(None)

        assert new_id == f"t{n + 1}"
        writes = [s for s in statements if s.split(None, 1)[0] in {"INSERT", "UPDATE", "DELETE"}]
        assert len(writes) <= 5 * 3, writes[:10]  # one row write + revision bump per edit
        assert len(statements) <= 5 * 10, len(statements)
        assert elapsed < 5.0, elapsed  # five edits; generous bound for slow CI
        assert_synced(w)
