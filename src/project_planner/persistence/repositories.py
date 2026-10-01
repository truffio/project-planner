"""Repositories: whole-aggregate load/store of projects and their schedule runs.

Every function takes the connection first and performs its writes inside
:func:`~project_planner.persistence.db.transaction` (re-entrant, so callers may group
several calls in one outer transaction). Loads use one query per table (no N+1).

``Project`` objects may hold an invalid graph (dangling references); the foreign keys
of ``assignments`` and ``dependencies`` then make ``save_project`` raise ``Conflict``.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from project_planner.engine.errors import Conflict, Issue, NotFound, ObjectType, Severity
from project_planner.engine.model import (
    Assignment,
    Calendar,
    CalendarException,
    Dependency,
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
from project_planner.persistence.db import transaction
from project_planner.persistence.records import (
    AssignmentResultRecord,
    LoadingSegmentRecord,
    NodeResultRecord,
    ProjectRecord,
    RunBundle,
    RunKind,
    ScheduleRunRecord,
)

__all__ = [
    "bump_revision",
    "copy_project",
    "create_project",
    "create_workspace",
    "delete_runs",
    "delete_saved_project",
    "get_project_record",
    "get_workspace_pk",
    "list_saved_projects",
    "load_project",
    "load_runs",
    "save_project",
    "save_run",
]

_DEFINITION_TABLES = ("calendars", "holidays", "calendar_exceptions", "wbs_nodes", "resources")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _dec(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# =============================================================================
# Project rows
# =============================================================================


def _require(conn: sqlite3.Connection, project_pk: int) -> sqlite3.Row | tuple[Any, ...]:
    row = conn.execute("SELECT kind FROM projects WHERE pk = ?", (project_pk,)).fetchone()
    if row is None:
        raise NotFound(ObjectType.PROJECT.value, str(project_pk))
    return row  # type: ignore[no-any-return]


def get_project_record(conn: sqlite3.Connection, project_pk: int) -> ProjectRecord:
    """Metadata of a stored project. Raises ``NotFound``."""
    row = conn.execute(
        "SELECT pk, kind, name, revision, based_on_pk, based_on_revision, saved_at "
        "FROM projects WHERE pk = ?",
        (project_pk,),
    ).fetchone()
    if row is None:
        raise NotFound(ObjectType.PROJECT.value, str(project_pk))
    return ProjectRecord(*row)


def get_workspace_pk(conn: sqlite3.Connection) -> int | None:
    """Primary key of the single workspace project, or ``None``."""
    row = conn.execute("SELECT pk FROM projects WHERE kind = 'workspace'").fetchone()
    return None if row is None else int(row[0])


def create_project(
    conn: sqlite3.Connection,
    project: Project,
    kind: str,
    name: str | None = None,
    *,
    saved_at: str | None = None,
) -> int:
    """Insert a new project row of ``kind`` ('saved'/'workspace') with its definition.

    ``name`` defaults to ``project.name``. Saved rows get ``saved_at`` (default: now).
    Raises ``Conflict`` for a duplicate saved name or a second workspace.
    """
    if saved_at is None and kind == "saved":
        saved_at = _now()
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO projects(kind, name, project_id, project_name, start, currency, "
            "cost_report_unit, revision, saved_at) VALUES (?,?,?,?,?,?,?,1,?)",
            (
                kind,
                project.name if name is None else name,
                project.id,
                project.name,
                project.start.isoformat(),
                project.currency,
                project.cost_report_unit.value,
                saved_at,
            ),
        )
        pk = int(cur.lastrowid or 0)
        _insert_definition(conn, pk, project)
    return pk


def create_workspace(conn: sqlite3.Connection, project: Project) -> int:
    """Create the workspace row (revision 1). Raises ``Conflict`` if one exists."""
    return create_project(conn, project, "workspace")


def bump_revision(conn: sqlite3.Connection, project_pk: int) -> int:
    """Increment and return the project's revision. Raises ``NotFound``."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE projects SET revision = revision + 1 WHERE pk = ?", (project_pk,)
        )
        if cur.rowcount == 0:
            raise NotFound(ObjectType.PROJECT.value, str(project_pk))
        row = conn.execute("SELECT revision FROM projects WHERE pk = ?", (project_pk,)).fetchone()
    return int(row[0])


def list_saved_projects(conn: sqlite3.Connection) -> list[tuple[int, str, str | None]]:
    """``(pk, name, saved_at)`` of saved projects, newest first."""
    rows = conn.execute(
        "SELECT pk, name, saved_at FROM projects WHERE kind = 'saved' "
        "ORDER BY saved_at DESC, pk DESC"
    ).fetchall()
    return [(int(pk), str(name), saved_at) for pk, name, saved_at in rows]


def delete_saved_project(conn: sqlite3.Connection, project_pk: int) -> None:
    """Delete a saved project with everything under it.

    Raises:
        NotFound: unknown pk.
        Conflict: the row is the workspace.
    """
    with transaction(conn):
        kind = _require(conn, project_pk)[0]
        if kind != "saved":
            raise Conflict("only saved projects can be deleted")
        conn.execute("DELETE FROM projects WHERE pk = ?", (project_pk,))


# =============================================================================
# Project definition
# =============================================================================


def _insert_definition(conn: sqlite3.Connection, pk: int, p: Project) -> None:
    cal = p.calendar
    conn.execute(
        "INSERT INTO calendars VALUES (?,?,?,?,?)",
        (
            pk,
            ",".join(w.value for w in Weekday.ordered(cal.working_weekdays)),
            str(cal.hours_per_day),
            cal.working_days_per_year,
            cal.workday_start.strftime("%H:%M"),
        ),
    )
    conn.executemany(
        "INSERT INTO holidays VALUES (?,?,?)",
        [(pk, h.date.isoformat(), h.name) for h in cal.holidays],
    )
    conn.executemany(
        "INSERT INTO calendar_exceptions VALUES (?,?,?,?)",
        [(pk, e.date.isoformat(), e.kind.value, e.name) for e in cal.exceptions],
    )
    conn.executemany(
        "INSERT INTO wbs_nodes VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                pk,
                n.id,
                n.name,
                n.kind.value,
                n.parent_id,
                n.order,
                n.sizing_mode.value,
                None if n.sizing is None else str(n.sizing.value),
                None if n.sizing is None else n.sizing.unit.value,
            )
            for n in p.nodes
        ],
    )
    conn.executemany(
        "INSERT INTO resources VALUES (?,?,?,?)",
        [(pk, r.id, r.name, _str(r.hourly_rate)) for r in p.resources],
    )
    conn.executemany(
        "INSERT INTO assignments VALUES (?,?,?,?)",
        [(pk, a.task_id, a.resource_id, str(a.percent)) for a in p.assignments],
    )
    conn.executemany(
        "INSERT INTO dependencies VALUES (?,?,?,?,?,?,?)",
        [
            (pk, d.id, d.pred_id, d.succ_id, d.type.value, str(d.lag.value), d.lag.unit.value)
            for d in p.dependencies
        ],
    )


def _clear_definition(conn: sqlite3.Connection, pk: int) -> None:
    # assignments and dependencies go via ON DELETE CASCADE of nodes/resources
    for table in _DEFINITION_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE project_pk = ?", (pk,))


def save_project(conn: sqlite3.Connection, project_pk: int, project: Project) -> None:
    """Replace the stored definition of ``project_pk`` with ``project`` (whole aggregate).

    Revision, kind, name and results are left untouched (callers bump the revision and
    delete stale runs as they see fit).

    Raises:
        NotFound: unknown pk.
        Conflict: a constraint failed (e.g. an assignment refers to a missing node).
    """
    with transaction(conn):
        _require(conn, project_pk)
        conn.execute(
            "UPDATE projects SET project_id = ?, project_name = ?, start = ?, currency = ?, "
            "cost_report_unit = ? WHERE pk = ?",
            (
                project.id,
                project.name,
                project.start.isoformat(),
                project.currency,
                project.cost_report_unit.value,
                project_pk,
            ),
        )
        _clear_definition(conn, project_pk)
        _insert_definition(conn, project_pk, project)


def load_project(conn: sqlite3.Connection, project_pk: int) -> Project:
    """Load the definition of ``project_pk`` (8 queries). Raises ``NotFound``."""
    head = conn.execute(
        "SELECT project_id, project_name, start, currency, cost_report_unit "
        "FROM projects WHERE pk = ?",
        (project_pk,),
    ).fetchone()
    if head is None:
        raise NotFound(ObjectType.PROJECT.value, str(project_pk))
    cal_row = conn.execute(
        "SELECT working_weekdays, hours_per_day, working_days_per_year, workday_start "
        "FROM calendars WHERE project_pk = ?",
        (project_pk,),
    ).fetchone()
    q = (project_pk,)
    holidays = tuple(
        Holiday(dt.date.fromisoformat(d), n)
        for d, n in conn.execute("SELECT date, name FROM holidays WHERE project_pk = ?", q)
    )
    exceptions = tuple(
        CalendarException(dt.date.fromisoformat(d), ExceptionKind(k), n)
        for d, k, n in conn.execute(
            "SELECT date, kind, name FROM calendar_exceptions WHERE project_pk = ?", q
        )
    )
    calendar = Calendar(
        working_weekdays=frozenset(Weekday(w) for w in cal_row[0].split(",")),
        hours_per_day=Decimal(cal_row[1]),
        working_days_per_year=int(cal_row[2]),
        workday_start=dt.time.fromisoformat(cal_row[3]),
        holidays=holidays,
        exceptions=exceptions,
    )
    nodes = tuple(
        WbsNode(
            id=i,
            name=name,
            kind=NodeKind(kind),
            parent_id=parent,
            order=order,
            sizing_mode=SizingMode(mode),
            sizing=None if value is None else TimeQty(Decimal(value), TimeUnit(unit)),
        )
        for i, name, kind, parent, order, mode, value, unit in conn.execute(
            "SELECT id, name, kind, parent_id, sort_order, sizing_mode, sizing_value, sizing_unit "
            "FROM wbs_nodes WHERE project_pk = ?",
            q,
        )
    )
    resources = tuple(
        Resource(i, name, _dec(rate))
        for i, name, rate in conn.execute(
            "SELECT id, name, hourly_rate FROM resources WHERE project_pk = ?", q
        )
    )
    assignments = tuple(
        Assignment(t, r, Decimal(pct))
        for t, r, pct in conn.execute(
            "SELECT task_id, resource_id, percent FROM assignments WHERE project_pk = ?", q
        )
    )
    dependencies = tuple(
        Dependency(i, pred, succ, type_, TimeQty(Decimal(lv), TimeUnit(lu)))
        for i, pred, succ, type_, lv, lu in conn.execute(
            "SELECT id, pred_id, succ_id, type, lag_value, lag_unit "
            "FROM dependencies WHERE project_pk = ?",
            q,
        )
    )
    return Project(
        id=head[0],
        name=head[1],
        start=dt.date.fromisoformat(head[2]),
        currency=head[3],
        cost_report_unit=WorkUnit(head[4]),
        calendar=calendar,
        nodes=nodes,
        resources=resources,
        assignments=assignments,
        dependencies=dependencies,
    )


# =============================================================================
# Copy
# =============================================================================

_COPY_SQL = (
    "INSERT INTO calendars SELECT :d, working_weekdays, hours_per_day, working_days_per_year, "
    "workday_start FROM calendars WHERE project_pk = :s",
    "INSERT INTO holidays SELECT :d, date, name FROM holidays WHERE project_pk = :s",
    "INSERT INTO calendar_exceptions SELECT :d, date, kind, name FROM calendar_exceptions "
    "WHERE project_pk = :s",
    "INSERT INTO wbs_nodes SELECT :d, id, name, kind, parent_id, sort_order, sizing_mode, "
    "sizing_value, sizing_unit FROM wbs_nodes WHERE project_pk = :s",
    "INSERT INTO resources SELECT :d, id, name, hourly_rate FROM resources WHERE project_pk = :s",
    "INSERT INTO assignments SELECT :d, task_id, resource_id, percent FROM assignments "
    "WHERE project_pk = :s",
    "INSERT INTO dependencies SELECT :d, id, pred_id, succ_id, type, lag_value, lag_unit "
    "FROM dependencies WHERE project_pk = :s",
)

_RUN_CHILD_COPY_SQL = (
    "INSERT INTO node_results SELECT :n, node_id, start_minute, finish_minute, duration_minutes, "
    "effort_person_minutes, leveling_delay_minutes, cost, cost_complete, status "
    "FROM node_results WHERE run_pk = :o ORDER BY rowid",
    "INSERT INTO assignment_results SELECT :n, task_id, resource_id, assignment_minutes, cost, "
    "cost_complete FROM assignment_results WHERE run_pk = :o ORDER BY rowid",
    "INSERT INTO loading_segments SELECT :n, resource_id, start_minute, end_minute, percent, "
    "task_ids FROM loading_segments WHERE run_pk = :o ORDER BY rowid",
    "INSERT INTO issues SELECT :n, severity, code, message, object_type, object_id, field, line "
    "FROM issues WHERE run_pk = :o ORDER BY rowid",
)


def copy_project(
    conn: sqlite3.Connection,
    src_pk: int,
    dst_pk: int | None = None,
    *,
    kind: str,
    name: str,
    saved_at: str | None = None,
) -> int:
    """Copy definition and all schedule runs of ``src_pk`` in one transaction.

    With ``dst_pk=None`` a new row is created (revision 1); otherwise the existing
    row ``dst_pk`` is overwritten (its revision is incremented). The destination
    records ``based_on_pk`` / ``based_on_revision`` of the source. ``kind`` and
    ``name`` are applied to the destination; saved rows get ``saved_at`` (default now).

    Returns:
        The destination pk.

    Raises:
        NotFound: unknown source or destination.
        Conflict: duplicate saved name, second workspace, or ``dst_pk == src_pk``.
    """
    if dst_pk == src_pk:
        raise Conflict("cannot copy a project onto itself")
    if saved_at is None and kind == "saved":
        saved_at = _now()
    with transaction(conn):
        src = conn.execute(
            "SELECT project_id, project_name, start, currency, cost_report_unit, revision "
            "FROM projects WHERE pk = ?",
            (src_pk,),
        ).fetchone()
        if src is None:
            raise NotFound(ObjectType.PROJECT.value, str(src_pk))
        if dst_pk is None:
            cur = conn.execute(
                "INSERT INTO projects(kind, name, project_id, project_name, start, currency, "
                "cost_report_unit, revision, based_on_pk, based_on_revision, saved_at) "
                "VALUES (?,?,?,?,?,?,?,1,?,?,?)",
                (kind, name, *src[:5], src_pk, src[5], saved_at),
            )
            dst = int(cur.lastrowid or 0)
        else:
            _require(conn, dst_pk)
            dst = dst_pk
            _clear_definition(conn, dst)
            conn.execute("DELETE FROM schedule_runs WHERE project_pk = ?", (dst,))
            conn.execute(
                "UPDATE projects SET kind = ?, name = ?, project_id = ?, project_name = ?, "
                "start = ?, currency = ?, cost_report_unit = ?, revision = revision + 1, "
                "based_on_pk = ?, based_on_revision = ?, saved_at = ? WHERE pk = ?",
                (kind, name, *src[:5], src_pk, src[5], saved_at, dst),
            )
        for sql in _COPY_SQL:
            conn.execute(sql, {"d": dst, "s": src_pk})
        runs = conn.execute(
            "SELECT pk, kind, schedule_fp, cost_fp, complete, project_finish, "
            "working_span_minutes, created_at FROM schedule_runs WHERE project_pk = ? ORDER BY pk",
            (src_pk,),
        ).fetchall()
        for old_pk, *rest in runs:
            cur = conn.execute(
                "INSERT INTO schedule_runs(project_pk, kind, schedule_fp, cost_fp, complete, "
                "project_finish, working_span_minutes, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (dst, *rest),
            )
            for sql in _RUN_CHILD_COPY_SQL:
                conn.execute(sql, {"n": cur.lastrowid, "o": old_pk})
    return dst


# =============================================================================
# Schedule runs
# =============================================================================


def save_run(
    conn: sqlite3.Connection,
    project_pk: int,
    run: ScheduleRunRecord,
    nodes: Sequence[NodeResultRecord] = (),
    assignments: Sequence[AssignmentResultRecord] = (),
    segments: Sequence[LoadingSegmentRecord] = (),
    issues: Sequence[Issue] = (),
) -> int:
    """Store a run and its child rows; returns the new run pk.

    Raises:
        NotFound: unknown project.
        Conflict: a constraint failed (e.g. a node result appears twice).
    """
    with transaction(conn):
        _require(conn, project_pk)
        cur = conn.execute(
            "INSERT INTO schedule_runs(project_pk, kind, schedule_fp, cost_fp, complete, "
            "project_finish, working_span_minutes, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                project_pk,
                run.kind.value,
                run.schedule_fp,
                run.cost_fp,
                int(run.complete),
                None if run.project_finish is None else run.project_finish.isoformat(),
                run.working_span_minutes,
                run.created_at,
            ),
        )
        rpk = int(cur.lastrowid or 0)
        conn.executemany(
            "INSERT INTO node_results VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    rpk,
                    n.node_id,
                    n.start_minute,
                    n.finish_minute,
                    n.duration_minutes,
                    _str(n.effort_person_minutes),
                    n.leveling_delay_minutes,
                    _str(n.cost),
                    int(n.cost_complete),
                    n.status,
                )
                for n in nodes
            ],
        )
        conn.executemany(
            "INSERT INTO assignment_results VALUES (?,?,?,?,?,?)",
            [
                (
                    rpk,
                    a.task_id,
                    a.resource_id,
                    str(a.assignment_minutes),
                    _str(a.cost),
                    int(a.cost_complete),
                )
                for a in assignments
            ],
        )
        conn.executemany(
            "INSERT INTO loading_segments VALUES (?,?,?,?,?,?)",
            [
                (
                    rpk,
                    s.resource_id,
                    s.start_minute,
                    s.end_minute,
                    str(s.percent),
                    json.dumps(list(s.task_ids)),
                )
                for s in segments
            ],
        )
        conn.executemany(
            "INSERT INTO issues VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    rpk,
                    i.severity.value,
                    i.code,
                    i.message,
                    i.object_type,
                    i.object_id,
                    i.field,
                    i.line,
                )
                for i in issues
            ],
        )
    return rpk


def load_runs(
    conn: sqlite3.Connection, project_pk: int, kind: RunKind | str | None = None
) -> list[RunBundle]:
    """Load the runs of a project (optionally of one kind), oldest first.

    Uses one query per table, independent of the number of runs. Raises ``NotFound``.
    """
    _require(conn, project_pk)
    sql = (
        "SELECT pk, kind, schedule_fp, cost_fp, complete, project_finish, "
        "working_span_minutes, created_at FROM schedule_runs WHERE project_pk = ?"
    )
    params: tuple[Any, ...] = (project_pk,)
    if kind is not None:
        sql += " AND kind = ?"
        params += (RunKind(kind).value,)
    run_rows = conn.execute(sql + " ORDER BY pk", params).fetchall()
    if not run_rows:
        return []
    scope = "SELECT pk FROM schedule_runs WHERE project_pk = ?" + (
        " AND kind = ?" if kind is not None else ""
    )

    nodes: dict[int, list[NodeResultRecord]] = defaultdict(list)
    for rpk, nid, s, f, d, eff, lvl, cost, cc, status in conn.execute(
        f"SELECT run_pk, node_id, start_minute, finish_minute, duration_minutes, "
        f"effort_person_minutes, leveling_delay_minutes, cost, cost_complete, status "
        f"FROM node_results WHERE run_pk IN ({scope}) ORDER BY rowid",
        params,
    ):
        nodes[rpk].append(
            NodeResultRecord(nid, status, bool(cc), s, f, d, _dec(eff), lvl, _dec(cost))
        )
    assigns: dict[int, list[AssignmentResultRecord]] = defaultdict(list)
    for rpk, t, r, minutes, cost, cc in conn.execute(
        f"SELECT run_pk, task_id, resource_id, assignment_minutes, cost, cost_complete "
        f"FROM assignment_results WHERE run_pk IN ({scope}) ORDER BY rowid",
        params,
    ):
        assigns[rpk].append(AssignmentResultRecord(t, r, Decimal(minutes), bool(cc), _dec(cost)))
    segs: dict[int, list[LoadingSegmentRecord]] = defaultdict(list)
    for rpk, r, s, e, pct, ids in conn.execute(
        f"SELECT run_pk, resource_id, start_minute, end_minute, percent, task_ids "
        f"FROM loading_segments WHERE run_pk IN ({scope}) ORDER BY rowid",
        params,
    ):
        segs[rpk].append(LoadingSegmentRecord(r, s, e, Decimal(pct), tuple(json.loads(ids))))
    iss: dict[int, list[Issue]] = defaultdict(list)
    for rpk, sev, code, msg, ot, oid, fld, line in conn.execute(
        f"SELECT run_pk, severity, code, message, object_type, object_id, field, line "
        f"FROM issues WHERE run_pk IN ({scope}) ORDER BY rowid",
        params,
    ):
        iss[rpk].append(Issue(Severity(sev), code, msg, ot, oid, fld, line))

    bundles: list[RunBundle] = []
    for pk, k, sfp, cfp, complete, finish, span, created in run_rows:
        record = ScheduleRunRecord(
            kind=RunKind(k),
            schedule_fp=sfp,
            cost_fp=cfp,
            complete=bool(complete),
            created_at=created,
            project_finish=None if finish is None else dt.date.fromisoformat(finish),
            working_span_minutes=span,
            pk=pk,
        )
        bundles.append(
            RunBundle(record, tuple(nodes[pk]), tuple(assigns[pk]), tuple(segs[pk]), tuple(iss[pk]))
        )
    return bundles


def delete_runs(
    conn: sqlite3.Connection, project_pk: int, kind: RunKind | str | None = None
) -> int:
    """Delete all runs of a project (or of one kind); returns how many. Raises ``NotFound``."""
    with transaction(conn):
        _require(conn, project_pk)
        if kind is None:
            cur = conn.execute("DELETE FROM schedule_runs WHERE project_pk = ?", (project_pk,))
        else:
            cur = conn.execute(
                "DELETE FROM schedule_runs WHERE project_pk = ? AND kind = ?",
                (project_pk, RunKind(kind).value),
            )
    return cur.rowcount
