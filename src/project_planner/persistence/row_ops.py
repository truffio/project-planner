"""Targeted single-row writes for workspace edits.

``repositories.save_project`` rewrites a whole aggregate; an edit of one task in a
10k-task project must not. These helpers touch only the rows an edit changes. They
do not open transactions: the caller groups them (together with the revision bump and
any result updates) in one :func:`~project_planner.persistence.db.transaction`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence

from project_planner.engine.model import (
    Assignment,
    Calendar,
    Dependency,
    Project,
    Resource,
    WbsNode,
    Weekday,
)

__all__ = [
    "apply_calendar",
    "delete_assignment",
    "delete_dependencies",
    "delete_dependency",
    "delete_nodes",
    "delete_resources",
    "insert_dependency",
    "insert_node",
    "insert_resource",
    "set_based_on",
    "set_name",
    "update_dependency",
    "update_node",
    "update_node_positions",
    "update_project_fields",
    "update_resource",
    "upsert_assignment",
]

_CHUNK = 500  # stay far below SQLite's variable limit


def _node_values(pk: int, n: WbsNode) -> tuple[object, ...]:
    return (
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


def insert_node(conn: sqlite3.Connection, pk: int, node: WbsNode) -> None:
    """Insert one WBS node."""
    conn.execute("INSERT INTO wbs_nodes VALUES (?,?,?,?,?,?,?,?,?)", _node_values(pk, node))


def update_node(conn: sqlite3.Connection, pk: int, node: WbsNode) -> None:
    """Rewrite every column of one WBS node (matched by ID)."""
    conn.execute(
        "UPDATE wbs_nodes SET name = ?, kind = ?, parent_id = ?, sort_order = ?, "
        "sizing_mode = ?, sizing_value = ?, sizing_unit = ? WHERE project_pk = ? AND id = ?",
        (*_node_values(pk, node)[2:], pk, node.id),
    )


def update_node_positions(conn: sqlite3.Connection, pk: int, nodes: Iterable[WbsNode]) -> None:
    """Update only ``parent_id`` and ``sort_order`` of the given nodes."""
    conn.executemany(
        "UPDATE wbs_nodes SET parent_id = ?, sort_order = ? WHERE project_pk = ? AND id = ?",
        [(n.parent_id, n.order, pk, n.id) for n in nodes],
    )


def _delete_in(
    conn: sqlite3.Connection, table: str, column: str, pk: int, ids: Sequence[str]
) -> None:
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        marks = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM {table} WHERE project_pk = ? AND {column} IN ({marks})", (pk, *chunk)
        )


def delete_nodes(conn: sqlite3.Connection, pk: int, ids: Sequence[str]) -> None:
    """Delete nodes; their assignments and dependencies go via ``ON DELETE CASCADE``."""
    _delete_in(conn, "wbs_nodes", "id", pk, ids)


def insert_resource(conn: sqlite3.Connection, pk: int, resource: Resource) -> None:
    """Insert one resource."""
    rate = None if resource.hourly_rate is None else str(resource.hourly_rate)
    conn.execute("INSERT INTO resources VALUES (?,?,?,?)", (pk, resource.id, resource.name, rate))


def update_resource(conn: sqlite3.Connection, pk: int, resource: Resource) -> None:
    """Update name and rate of one resource."""
    rate = None if resource.hourly_rate is None else str(resource.hourly_rate)
    conn.execute(
        "UPDATE resources SET name = ?, hourly_rate = ? WHERE project_pk = ? AND id = ?",
        (resource.name, rate, pk, resource.id),
    )


def delete_resources(conn: sqlite3.Connection, pk: int, ids: Sequence[str]) -> None:
    """Delete resources; their assignments go via ``ON DELETE CASCADE``."""
    _delete_in(conn, "resources", "id", pk, ids)


def upsert_assignment(conn: sqlite3.Connection, pk: int, assignment: Assignment) -> None:
    """Insert the assignment, or update the percent of the existing (task, resource) pair."""
    conn.execute(
        "INSERT INTO assignments VALUES (?,?,?,?) "
        "ON CONFLICT(project_pk, task_id, resource_id) DO UPDATE SET percent = excluded.percent",
        (pk, assignment.task_id, assignment.resource_id, str(assignment.percent)),
    )


def delete_assignment(conn: sqlite3.Connection, pk: int, task_id: str, resource_id: str) -> None:
    """Delete one assignment."""
    conn.execute(
        "DELETE FROM assignments WHERE project_pk = ? AND task_id = ? AND resource_id = ?",
        (pk, task_id, resource_id),
    )


def insert_dependency(conn: sqlite3.Connection, pk: int, dep: Dependency) -> None:
    """Insert one dependency."""
    conn.execute(
        "INSERT INTO dependencies VALUES (?,?,?,?,?,?,?)",
        (
            pk,
            dep.id,
            dep.pred_id,
            dep.succ_id,
            dep.type.value,
            str(dep.lag.value),
            dep.lag.unit.value,
        ),
    )


def update_dependency(conn: sqlite3.Connection, pk: int, dep: Dependency) -> None:
    """Update type and lag of one dependency."""
    conn.execute(
        "UPDATE dependencies SET type = ?, lag_value = ?, lag_unit = ? "
        "WHERE project_pk = ? AND id = ?",
        (dep.type.value, str(dep.lag.value), dep.lag.unit.value, pk, dep.id),
    )


def delete_dependency(conn: sqlite3.Connection, pk: int, dep_id: str) -> None:
    """Delete one dependency."""
    _delete_in(conn, "dependencies", "id", pk, [dep_id])


def delete_dependencies(conn: sqlite3.Connection, pk: int, ids: Sequence[str]) -> None:
    """Delete several dependencies."""
    _delete_in(conn, "dependencies", "id", pk, ids)


def update_project_fields(conn: sqlite3.Connection, pk: int, project: Project) -> None:
    """Write the scalar project settings (not the revision, name column or definition rows)."""
    conn.execute(
        "UPDATE projects SET project_id = ?, project_name = ?, start = ?, currency = ?, "
        "cost_report_unit = ? WHERE pk = ?",
        (
            project.id,
            project.name,
            project.start.isoformat(),
            project.currency,
            project.cost_report_unit.value,
            pk,
        ),
    )


def set_name(conn: sqlite3.Connection, pk: int, name: str) -> None:
    """Set the ``projects.name`` column (library name; the workspace mirrors the project name)."""
    conn.execute("UPDATE projects SET name = ? WHERE pk = ?", (name, pk))


def set_based_on(
    conn: sqlite3.Connection,
    pk: int,
    based_on_pk: int | None,
    based_on_revision: int | None,
    based_on_results_gen: int | None = None,
) -> None:
    """Record which saved project / revision the workspace was last saved to or loaded at.

    ``based_on_results_gen`` (schema v2) is the results generation at that moment; ``None``
    leaves the stored value unchanged.
    """
    if based_on_results_gen is None:
        conn.execute(
            "UPDATE projects SET based_on_pk = ?, based_on_revision = ? WHERE pk = ?",
            (based_on_pk, based_on_revision, pk),
        )
    else:
        conn.execute(
            "UPDATE projects SET based_on_pk = ?, based_on_revision = ?, "
            "based_on_results_gen = ? WHERE pk = ?",
            (based_on_pk, based_on_revision, based_on_results_gen, pk),
        )


def _calendar_scalars(cal: Calendar) -> tuple[str, str, int, str]:
    return (
        ",".join(w.value for w in Weekday.ordered(cal.working_weekdays)),
        str(cal.hours_per_day),
        cal.working_days_per_year,
        cal.workday_start.strftime("%H:%M"),
    )


def apply_calendar(conn: sqlite3.Connection, pk: int, old: Calendar, new: Calendar) -> None:
    """Make the stored calendar equal ``new`` by writing only the differences."""
    if _calendar_scalars(old) != _calendar_scalars(new):
        conn.execute(
            "UPDATE calendars SET working_weekdays = ?, hours_per_day = ?, "
            "working_days_per_year = ?, workday_start = ? WHERE project_pk = ?",
            (*_calendar_scalars(new), pk),
        )
    for table, old_rows, new_rows in (
        (
            "holidays",
            {h.date: (h.name,) for h in old.holidays},
            {h.date: (h.name,) for h in new.holidays},
        ),
        (
            "calendar_exceptions",
            {e.date: (e.kind.value, e.name) for e in old.exceptions},
            {e.date: (e.kind.value, e.name) for e in new.exceptions},
        ),
    ):
        for day in old_rows.keys() - new_rows.keys():
            conn.execute(
                f"DELETE FROM {table} WHERE project_pk = ? AND date = ?", (pk, day.isoformat())
            )
        for day, values in new_rows.items():
            if old_rows.get(day) == values:
                continue
            marks = ",".join("?" * (len(values) + 2))
            conn.execute(
                f"INSERT OR REPLACE INTO {table} VALUES ({marks})", (pk, day.isoformat(), *values)
            )
