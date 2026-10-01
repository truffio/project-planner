"""Persistence layer tests (task T30)."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.errors import Conflict, Issue, NotFound
from project_planner.engine.model import Project, WbsNode
from project_planner.persistence import migrations
from project_planner.persistence import repositories as repo
from project_planner.persistence.db import connect, transaction
from project_planner.persistence.migrations import LATEST_VERSION, migrate, schema_version
from project_planner.persistence.records import (
    AssignmentResultRecord,
    LoadingSegmentRecord,
    NodeResultRecord,
    RunKind,
    ScheduleRunRecord,
)

pytestmark = pytest.mark.services


def rich_project() -> Project:
    b = (
        ProjectBuilder(name="Rich", id="P-001", currency="EUR", cost_report_unit="person_hours")
        .calendar(
            hours_per_day="7.5",
            working_days_per_year=250,
            working_weekdays="Sun-Thu",
            workday_start="08:30",
            holidays=[(dt.date(2025, 1, 1), "New year"), dt.date(2025, 5, 1)],
            exceptions=[
                (dt.date(2025, 1, 4), "working", "Saturday shift"),
                (dt.date(2025, 1, 8), "nonworking", ""),
            ],
        )
        .resource("001", "Ann", rate="55.50")
        .resource("002", "Bob")
        .resource("003", "Zero", rate=0)
        .group("001-g", "Phase 1")
        .group("002-g", "Sub", parent="001-g")
        .task("001-t", "Design", parent="002-g", duration="1.50d")
        .task("002-t", "Build", parent="001-g", effort="40h")
        .task("003-t", "Unsized", parent="001-g")
        .milestone("001-m", "Done")
        .assign("001-t", "001", "50.25")
        .assign("002-t", "001")
        .assign("002-t", "002", 80)
        .dep("001-t", "002-t", "FS", lag="-0.50d", id="d1")
        .dep("001-t", "003-t", "SS", lag="2h", id="d2")
        .dep("002-t", "001-m", "FF", lag="-1.0h", id="d3")
        .dep("003-t", "001-m", "SF", lag="3d", id="d4")
    )
    return b.build()


def dump(conn: sqlite3.Connection) -> list[str]:
    return list(conn.iterdump())


@pytest.fixture(params=["memory", "file"])
def conn(request: pytest.FixtureRequest, tmp_path) -> Iterator[sqlite3.Connection]:
    path = ":memory:" if request.param == "memory" else tmp_path / "p.db"
    c = connect(path)
    migrate(c)
    yield c
    c.close()


def assert_same(a: Project, b: Project) -> None:
    assert a == b
    # == on Decimal/TimeQty is numeric: check the exact text too.
    assert [str(n.sizing) for n in a.nodes] == [str(n.sizing) for n in b.nodes]
    assert [str(d.lag) for d in a.dependencies] == [str(d.lag) for d in b.dependencies]
    assert [str(r.hourly_rate) for r in a.resources] == [str(r.hourly_rate) for r in b.resources]
    assert [str(x.percent) for x in a.assignments] == [str(x.percent) for x in b.assignments]
    assert str(a.calendar.hours_per_day) == str(b.calendar.hours_per_day)


# --- migrations / connection -------------------------------------------------------


def test_migrate_empty_db_and_idempotent():
    c = connect(":memory:")
    assert schema_version(c) == 0
    assert migrate(c) == 2 == LATEST_VERSION
    before = dump(c)
    assert migrate(c) == LATEST_VERSION
    assert dump(c) == before
    assert schema_version(c) == LATEST_VERSION
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_newer_database_rejected():
    c = connect(":memory:")
    c.execute("PRAGMA user_version = 99")
    with pytest.raises(Conflict):
        migrate(c)


def test_file_db_uses_wal(tmp_path):
    c = connect(tmp_path / "x.db")
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()


def test_file_db_survives_reopen(tmp_path):
    path = tmp_path / "x.db"
    c = connect(path)
    migrate(c)
    project = rich_project()
    pk = repo.create_workspace(c, project)
    c.close()
    c2 = connect(path)
    assert migrate(c2) == LATEST_VERSION
    assert_same(repo.load_project(c2, pk), project)
    c2.close()


# --- project round trip ---------------------------------------------------------------


def test_project_round_trip(conn):
    project = rich_project()
    pk = repo.create_workspace(conn, project)
    loaded = repo.load_project(conn, pk)
    assert_same(loaded, project)
    assert loaded.calendar.workday_start == dt.time(8, 30)
    assert str(loaded.node("001-t").sizing) == "1.50d"
    assert str(loaded.dependency("d1").lag) == "-0.50d"
    assert loaded.resource("002").hourly_rate is None
    assert loaded.resource("003").hourly_rate == Decimal(0)


def test_save_project_replaces_definition(conn):
    pk = repo.create_workspace(conn, rich_project())
    smaller = ProjectBuilder(name="Small").task("a", duration="1d").build()
    repo.save_project(conn, pk, smaller)
    assert_same(repo.load_project(conn, pk), smaller)
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM holidays").fetchone()[0] == 0


def test_minimal_project_round_trip(conn):
    project = ProjectBuilder().build()
    pk = repo.create_workspace(conn, project)
    assert_same(repo.load_project(conn, pk), project)


def test_unknown_pk_not_found(conn):
    with pytest.raises(NotFound):
        repo.load_project(conn, 42)
    with pytest.raises(NotFound):
        repo.save_project(conn, 42, rich_project())
    with pytest.raises(NotFound):
        repo.bump_revision(conn, 42)
    with pytest.raises(NotFound):
        repo.delete_saved_project(conn, 42)
    with pytest.raises(NotFound):
        repo.load_runs(conn, 42)
    with pytest.raises(NotFound):
        repo.delete_runs(conn, 42)
    with pytest.raises(NotFound):
        repo.copy_project(conn, 42, kind="saved", name="x")
    with pytest.raises(NotFound):
        repo.save_run(conn, 42, make_run())
    with pytest.raises(NotFound):
        repo.get_project_record(conn, 42)


# --- constraints -------------------------------------------------------------------------


def test_saved_names_unique(conn):
    project = rich_project()
    repo.create_project(conn, project, "saved", "one")
    with pytest.raises(Conflict):
        repo.create_project(conn, project, "saved", "one")
    assert [n for _, n, _ in repo.list_saved_projects(conn)] == ["one"]
    repo.create_project(conn, project, "saved", "two")
    assert {n for _, n, _ in repo.list_saved_projects(conn)} == {"one", "two"}


def test_single_workspace(conn):
    assert repo.get_workspace_pk(conn) is None
    pk = repo.create_workspace(conn, rich_project())
    assert repo.get_workspace_pk(conn) == pk
    with pytest.raises(Conflict):
        repo.create_workspace(conn, rich_project())
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_workspace_cannot_be_deleted_as_saved(conn):
    pk = repo.create_workspace(conn, rich_project())
    with pytest.raises(Conflict):
        repo.delete_saved_project(conn, pk)


def test_dangling_references_conflict(conn):
    pk = repo.create_workspace(conn, ProjectBuilder().build())
    bad = ProjectBuilder().task("a", duration="1d").dep("a", "ghost", id="d1").build()
    before = dump(conn)
    with pytest.raises(Conflict):
        repo.save_project(conn, pk, bad)
    assert dump(conn) == before  # failed save rolled back, old definition intact


def test_duplicate_run_node_conflict(conn):
    pk = repo.create_workspace(conn, rich_project())
    before = dump(conn)
    with pytest.raises(Conflict):
        repo.save_run(
            conn, pk, make_run(), nodes=[NodeResultRecord("a", "ok"), NodeResultRecord("a", "ok")]
        )
    assert dump(conn) == before


def test_delete_saved_project_cascades(conn):
    pk = repo.create_project(conn, rich_project(), "saved", "s")
    repo.save_run(conn, pk, make_run(), nodes=[NodeResultRecord("a", "ok")])
    repo.delete_saved_project(conn, pk)
    for table in (
        "projects",
        "wbs_nodes",
        "resources",
        "assignments",
        "dependencies",
        "holidays",
        "calendars",
        "schedule_runs",
        "node_results",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_bump_revision(conn):
    pk = repo.create_workspace(conn, rich_project())
    assert repo.get_project_record(conn, pk).revision == 1
    assert repo.bump_revision(conn, pk) == 2
    assert repo.get_project_record(conn, pk).revision == 2


# --- atomicity ---------------------------------------------------------------------------


def test_exception_in_transaction_leaves_db_unchanged(conn):
    pk = repo.create_workspace(conn, rich_project())
    repo.save_run(conn, pk, make_run(), nodes=[NodeResultRecord("x", "ok")])
    before = dump(conn)

    class Boom(Exception):
        pass

    with pytest.raises(Boom), transaction(conn):
        repo.save_project(conn, pk, ProjectBuilder(name="Other").task("z", duration="1d").build())
        repo.bump_revision(conn, pk)
        repo.copy_project(conn, pk, kind="saved", name="copy")
        repo.delete_runs(conn, pk)
        raise Boom
    assert dump(conn) == before
    assert not conn.in_transaction


def test_inner_failure_rolls_back_only_inner_block(conn):
    pk = repo.create_workspace(conn, rich_project())
    with transaction(conn):
        repo.bump_revision(conn, pk)
        with pytest.raises(Conflict):
            repo.create_workspace(conn, rich_project())
        repo.bump_revision(conn, pk)
    assert repo.get_project_record(conn, pk).revision == 3


# --- copy ---------------------------------------------------------------------------------


def make_run(kind: RunKind = RunKind.DEPENDENCY_ONLY, **kw) -> ScheduleRunRecord:
    return ScheduleRunRecord(
        kind=kind,
        schedule_fp="sfp",
        cost_fp="cfp",
        complete=kw.pop("complete", True),
        created_at="2025-01-01T00:00:00+00:00",
        **kw,
    )


def sample_results():
    nodes = [
        NodeResultRecord(
            "t1", "scheduled", True, 0, 450, 450, Decimal("900.0"), 0, Decimal("12.3400")
        ),
        NodeResultRecord("t2", "leveled", False, 600, 1050, 450, None, 150, None),
        NodeResultRecord("t3", "unsized", True),
    ]
    assigns = [
        AssignmentResultRecord("t1", "r1", Decimal("450.5"), True, Decimal("1.10")),
        AssignmentResultRecord("t2", "r1", Decimal("0"), False, None),
    ]
    segs = [
        LoadingSegmentRecord("r1", 0, 450, Decimal("50.25"), ("t1",)),
        LoadingSegmentRecord("r1", 450, 600, Decimal("150"), ("t1", "t2")),
    ]
    issues = [
        Issue.warning("RESOURCE_NO_RATE", "no rate", object_type="resource", object_id="r2"),
        Issue.error("X", "bad", object_type="node", object_id="t1", field="sizing", line=7),
    ]
    return nodes, assigns, segs, issues


def test_runs_round_trip_multiple_kinds(conn):
    pk = repo.create_workspace(conn, rich_project())
    nodes, assigns, segs, issues = sample_results()
    r1 = repo.save_run(
        conn,
        pk,
        make_run(
            RunKind.DEPENDENCY_ONLY, project_finish=dt.date(2025, 2, 3), working_span_minutes=1500
        ),
        nodes,
        assigns,
        segs,
        issues,
    )
    r2 = repo.save_run(conn, pk, make_run(RunKind.LEVELING_PREVIEW, complete=False))
    r3 = repo.save_run(conn, pk, make_run(RunKind.LEVELED), nodes[1:], assigns[:1], segs[1:], [])
    bundles = repo.load_runs(conn, pk)
    assert [b.run.pk for b in bundles] == [r1, r2, r3]
    first = bundles[0]
    assert first.run == replace(
        make_run(
            RunKind.DEPENDENCY_ONLY, project_finish=dt.date(2025, 2, 3), working_span_minutes=1500
        ),
        pk=r1,
    )
    assert list(first.nodes) == nodes
    assert [str(n.cost) for n in first.nodes] == ["12.3400", "None", "None"]
    assert first.nodes[1].leveling_delay_minutes == 150
    assert first.nodes[1].cost_complete is False
    assert list(first.assignments) == assigns
    assert list(first.segments) == segs
    assert list(first.issues) == issues
    assert bundles[1].run.complete is False
    assert bundles[1].nodes == ()
    assert list(bundles[2].nodes) == nodes[1:]
    assert [b.run.pk for b in repo.load_runs(conn, pk, RunKind.LEVELED)] == [r3]
    assert repo.delete_runs(conn, pk, "leveled") == 1
    assert [b.run.pk for b in repo.load_runs(conn, pk)] == [r1, r2]
    assert repo.delete_runs(conn, pk) == 2
    assert repo.load_runs(conn, pk) == []
    assert conn.execute("SELECT COUNT(*) FROM node_results").fetchone()[0] == 0


def test_copy_project_copies_everything_and_is_independent(conn):
    project = rich_project()
    ws = repo.create_workspace(conn, project)
    nodes, assigns, segs, issues = sample_results()
    repo.save_run(conn, ws, make_run(), nodes, assigns, segs, issues)
    repo.save_run(conn, ws, make_run(RunKind.LEVELED), nodes, assigns, segs, issues)
    repo.bump_revision(conn, ws)

    saved = repo.copy_project(conn, ws, kind="saved", name="Snapshot")
    rec = repo.get_project_record(conn, saved)
    assert (rec.kind, rec.name, rec.based_on_pk, rec.based_on_revision) == (
        "saved",
        "Snapshot",
        ws,
        2,
    )
    assert rec.saved_at is not None
    assert_same(repo.load_project(conn, saved), project)
    src_runs, dst_runs = repo.load_runs(conn, ws), repo.load_runs(conn, saved)
    assert len(dst_runs) == 2
    assert [replace(b, run=replace(b.run, pk=None)) for b in dst_runs] == [
        replace(b, run=replace(b.run, pk=None)) for b in src_runs
    ]
    assert {b.run.pk for b in dst_runs}.isdisjoint({b.run.pk for b in src_runs})

    # independence
    edited = replace(project, nodes=(*project.nodes, WbsNode("new", "New", "milestone")))
    repo.save_project(conn, ws, edited)
    repo.delete_runs(conn, ws)
    assert_same(repo.load_project(conn, saved), project)
    assert len(repo.load_runs(conn, saved)) == 2

    # Load: overwrite the workspace from the saved copy
    repo.save_run(conn, ws, make_run(), nodes[:1])
    same = repo.copy_project(conn, saved, ws, kind="workspace", name="Rich")
    assert same == ws
    assert_same(repo.load_project(conn, ws), project)
    assert len(repo.load_runs(conn, ws)) == 2
    assert repo.get_project_record(conn, ws).revision == 3  # bumped by overwrite


def test_copy_name_conflict_leaves_db_unchanged(conn):
    ws = repo.create_workspace(conn, rich_project())
    repo.copy_project(conn, ws, kind="saved", name="dup")
    before = dump(conn)
    with pytest.raises(Conflict):
        repo.copy_project(conn, ws, kind="saved", name="dup")
    assert dump(conn) == before
    with pytest.raises(Conflict):
        repo.copy_project(conn, ws, ws, kind="workspace", name="x")


# --- bulk ----------------------------------------------------------------------------------


def test_bulk_load_query_count_and_speed(tmp_path):
    n = 5000
    b = ProjectBuilder(name="Big").resource("r1", rate=10).group("g")
    for i in range(n):
        b.task(f"t{i:05d}", parent="g", duration="1d", order=i)
        if i:
            b.dep(f"t{i - 1:05d}", f"t{i:05d}")
        if i % 5 == 0:
            b.assign(f"t{i:05d}", "r1", 50)
    project = b.build()
    c = connect(tmp_path / "big.db")
    migrate(c)
    pk = repo.create_workspace(c, project)
    statements: list[str] = []
    c.set_trace_callback(statements.append)
    loaded = repo.load_project(c, pk)
    c.set_trace_callback(None)
    assert loaded == project
    assert len(statements) <= 10
    saves: list[str] = []
    c.set_trace_callback(saves.append)
    repo.save_project(c, pk, project)
    c.set_trace_callback(None)
    # executemany issues one statement trace per row, but only a handful of distinct SQL texts
    assert len({s.split("(")[0] for s in saves if s.startswith("INSERT")}) <= 8
    c.close()


# --- schema v2: AUTOINCREMENT ids, results generation, revision checks (review batch A) ----


def _v1_database(path) -> sqlite3.Connection:
    """A database exactly as the v1 code created it (the frozen v1 DDL, user_version 1)."""
    c = connect(path)
    c.executescript(migrations._v1() + "\nPRAGMA user_version = 1;")
    assert schema_version(c) == 1
    cols = [r[1] for r in c.execute("PRAGMA table_info(projects)")]
    assert "results_gen" not in cols
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'projects'").fetchone()[0]
    assert "AUTOINCREMENT" not in sql
    return c


def _counts(c: sqlite3.Connection) -> dict[str, int]:
    tables = [
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(tables)}


def test_upgrade_v1_file_preserves_data_and_foreign_keys(tmp_path):
    path = tmp_path / "v1.db"
    c = _v1_database(path)
    project = rich_project()
    ws = repo.create_workspace(c, project)
    nodes, assigns, segs, issues = sample_results()
    repo.save_run(c, ws, make_run(), nodes, assigns, segs, issues)
    repo.bump_revision(c, ws)
    a = repo.copy_project(c, ws, kind="saved", name="A")
    b = repo.copy_project(c, ws, kind="saved", name="B")
    c.execute("UPDATE projects SET based_on_pk = ?, based_on_revision = 2 WHERE pk = ?", (b, ws))
    rows_before = c.execute(
        "SELECT pk, kind, name, project_id, project_name, start, currency, cost_report_unit, "
        "revision, based_on_pk, based_on_revision, saved_at FROM projects ORDER BY pk"
    ).fetchall()
    counts_before = _counts(c)
    runs_before = {pk: repo.load_runs(c, pk) for pk in (ws, a, b)}
    c.close()

    c2 = connect(path)
    assert migrate(c2) == LATEST_VERSION == 2
    assert c2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c2.execute("PRAGMA foreign_key_check").fetchall() == []
    sql = c2.execute("SELECT sql FROM sqlite_master WHERE name = 'projects'").fetchone()[0]
    assert "AUTOINCREMENT" in sql
    rows_after = c2.execute(
        "SELECT pk, kind, name, project_id, project_name, start, currency, cost_report_unit, "
        "revision, based_on_pk, based_on_revision, saved_at FROM projects ORDER BY pk"
    ).fetchall()
    assert rows_after == rows_before
    counts_after = _counts(c2)
    assert counts_after == counts_before
    for pk in (ws, a, b):
        assert_same(repo.load_project(c2, pk), project)
        assert repo.load_runs(c2, pk) == runs_before[pk]
        rec = repo.get_project_record(c2, pk)
        assert (rec.results_gen, rec.based_on_results_gen) == (0, 0)
    # unique indexes are back
    with pytest.raises(Conflict):
        repo.copy_project(c2, ws, kind="saved", name="A")
    with pytest.raises(Conflict):
        repo.create_workspace(c2, project)
    # foreign keys point at the rebuilt table: deleting a saved project still cascades
    repo.delete_saved_project(c2, a)
    assert c2.execute("SELECT COUNT(*) FROM wbs_nodes WHERE project_pk = ?", (a,)).fetchone() == (
        0,
    )
    assert (
        c2.execute("SELECT COUNT(*) FROM schedule_runs WHERE project_pk = ?", (a,)).fetchone()[0]
        == 0
    )
    # ids are never reused any more: delete the newest saved project, save again
    repo.delete_saved_project(c2, b)
    new = repo.copy_project(c2, ws, kind="saved", name="C")
    assert new > b
    assert migrate(c2) == LATEST_VERSION  # idempotent
    c2.close()


def test_saved_project_ids_are_not_reused(conn):
    ws = repo.create_workspace(conn, rich_project())
    a = repo.copy_project(conn, ws, kind="saved", name="A")
    b = repo.copy_project(conn, ws, kind="saved", name="B")
    repo.delete_saved_project(conn, b)
    c = repo.copy_project(conn, ws, kind="saved", name="C")
    assert c not in (a, b) and c > b


def test_failed_v2_migration_leaves_v1_database(tmp_path, monkeypatch):
    path = tmp_path / "v1.db"
    c = _v1_database(path)
    ws = repo.create_workspace(c, rich_project())
    before = dump(c)
    bad = migrations._Step(migrations._v2() + "\nSELECT no_such_function();", True)
    monkeypatch.setitem(migrations._MIGRATIONS, 2, bad)
    with pytest.raises(sqlite3.OperationalError):
        migrate(c)
    assert schema_version(c) == 1
    assert not c.in_transaction
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert dump(c) == before
    assert_same(repo.load_project(c, ws), rich_project())
    c.close()


def test_revision_checked_writes(conn):
    pk = repo.create_workspace(conn, rich_project())
    assert repo.bump_revision(conn, pk, expected=1) == 2
    with pytest.raises(Conflict, match="another connection"):
        repo.bump_revision(conn, pk, expected=1)
    assert repo.get_project_record(conn, pk).revision == 2
    with pytest.raises(NotFound):
        repo.bump_revision(conn, 999, expected=1)
    repo.check_revision(conn, pk, 2, 0)
    with pytest.raises(Conflict):
        repo.check_revision(conn, pk, 1)
    assert repo.bump_results_gen(conn, pk, revision=2, results_gen=0) == 1
    with pytest.raises(Conflict, match="results generation"):
        repo.bump_results_gen(conn, pk, revision=2, results_gen=0)
    with pytest.raises(Conflict):
        repo.bump_results_gen(conn, pk, revision=1, results_gen=1)
    assert repo.get_project_record(conn, pk).results_gen == 1
