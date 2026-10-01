"""Persisting and restoring schedule results (``ScheduleResult`` <-> run records).

Per project the store keeps at most one run per kind:

* ``dependency_only`` - the current dependency-only schedule;
* ``leveling_preview`` - an optional, not yet applied leveling preview;
* ``leveled`` - an optional applied leveling.

Restoring never reschedules: node start/finish minutes, statuses and leveling delays
come from the stored rows and :func:`project_planner.engine.schedule.assemble`
derives everything else. Stored fingerprints are kept verbatim, so a result that is
stale against the (edited) definition stays visibly stale.

Storing rules (:meth:`ResultStore.store`): a new ``dependency_only`` result replaces
every stored run (leveling was based on the old schedule); a ``leveled`` result
replaces the ``leveled`` run and drops the preview it was applied from; a
``leveling_preview`` replaces the previous preview.

Compute services (T32) call ``Workspace.store_result`` /
``Workspace.discard_results``, which delegate here.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from typing import Literal

from project_planner.engine.fingerprint import schedule_fp
from project_planner.engine.forward_pass import NodeTiming
from project_planner.engine.model import Project
from project_planner.engine.results import ScheduleResult
from project_planner.engine.schedule import assemble, recost
from project_planner.persistence import repositories as repo
from project_planner.persistence.db import transaction
from project_planner.persistence.records import (
    AssignmentResultRecord,
    LoadingSegmentRecord,
    NodeResultRecord,
    RunBundle,
    RunKind,
    ScheduleRunRecord,
)

__all__ = ["ResultStore", "restore_result", "result_to_records"]


def _status(text: str) -> Literal["scheduled", "unschedulable", "blocked"]:
    if text == "scheduled":
        return "scheduled"
    if text == "blocked":
        return "blocked"
    return "unschedulable"


def result_to_records(
    result: ScheduleResult,
) -> tuple[
    ScheduleRunRecord,
    list[NodeResultRecord],
    list[AssignmentResultRecord],
    list[LoadingSegmentRecord],
]:
    """Map a result to its run row and child rows (issues are stored as they are)."""
    run = ScheduleRunRecord(
        kind=RunKind(result.kind),
        schedule_fp=result.schedule_fp,
        cost_fp=result.cost_fp,
        complete=result.complete,
        created_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        project_finish=None if result.project_finish is None else result.project_finish.date(),
        working_span_minutes=result.working_span_minutes,
    )
    nodes = [
        NodeResultRecord(
            node_id=n.node_id,
            status=n.status,
            cost_complete=n.cost_complete,
            start_minute=n.start_minutes,
            finish_minute=n.finish_minutes,
            duration_minutes=n.duration_minutes,
            effort_person_minutes=n.effort_person_minutes,
            leveling_delay_minutes=n.leveling_delay_minutes,
            cost=n.cost,
        )
        for n in result.nodes.values()
    ]
    assignments = [
        AssignmentResultRecord(
            a.task_id, a.resource_id, a.assignment_minutes, a.cost_complete, a.cost
        )
        for a in result.assignments.values()
    ]
    segments = [
        LoadingSegmentRecord(s.resource_id, s.start, s.end, s.percent, s.task_ids)
        for segs in result.loading.values()
        for s in segs
    ]
    return run, nodes, assignments, segments


def restore_result(project: Project, bundle: RunBundle) -> ScheduleResult:
    """Rebuild the result of a stored run from its per-node timings (no rescheduling).

    ``project`` is the current definition. When it still matches the stored
    fingerprints the result equals the one that was stored; otherwise it is a
    best-effort view of the stale result (unknown tasks show as not calculated) and
    keeps the stored fingerprints, so staleness checks still detect it.
    """
    timings: dict[str, NodeTiming] = {}
    delays: dict[str, int] = {}
    for rec in bundle.nodes:
        if rec.status == "group":
            continue
        timings[rec.node_id] = NodeTiming(
            rec.node_id,
            rec.start_minute,
            rec.finish_minute,
            _status(rec.status),
            None,
        )
        if rec.leveling_delay_minutes:
            delays[rec.node_id] = rec.leveling_delay_minutes
    result = assemble(
        project, timings, kind=bundle.run.kind.value, delays=delays, issues=bundle.issues
    )
    return dataclasses.replace(
        result, schedule_fp=bundle.run.schedule_fp, cost_fp=bundle.run.cost_fp
    )


class ResultStore:
    """Result persistence and cache for one workspace project.

    Args:
        conn: Workspace connection.
        project_pk: Primary key of the workspace project (change with :meth:`rebind`).
        project: Callable returning the *current* definition (used when restoring).
    """

    def __init__(
        self, conn: sqlite3.Connection, project_pk: int, project: Callable[[], Project]
    ) -> None:
        self._conn = conn
        self._pk = project_pk
        self._project = project
        self._cache: dict[RunKind, ScheduleResult] = {}
        self._runs: dict[RunKind, tuple[str, str]] | None = None

    # ------------------------------------------------------------------ reads

    def reset(self, project_pk: int | None = None) -> None:
        """Forget cached results (after the stored rows were replaced elsewhere)."""
        if project_pk is not None:
            self._pk = project_pk
        self._cache.clear()
        self._runs = None

    def _stored(self) -> dict[RunKind, tuple[str, str]]:
        if self._runs is None:
            rows = self._conn.execute(
                "SELECT kind, schedule_fp, cost_fp FROM schedule_runs WHERE project_pk = ? "
                "ORDER BY pk",
                (self._pk,),
            ).fetchall()
            self._runs = {RunKind(k): (s, c) for k, s, c in rows}
        return self._runs

    def kinds(self) -> frozenset[RunKind]:
        """Kinds that have a stored run."""
        return frozenset(self._stored())

    def fingerprints(self) -> dict[RunKind, tuple[str, str]]:
        """``(schedule_fp, cost_fp)`` of every stored run, without restoring any result."""
        return dict(self._stored())

    def has(self, kind: RunKind | str) -> bool:
        """Whether a run of ``kind`` is stored."""
        return RunKind(kind) in self.kinds()

    def get(self, kind: RunKind | str) -> ScheduleResult | None:
        """The stored result of ``kind`` (restored on first use), or ``None``."""
        k = RunKind(kind)
        cached = self._cache.get(k)
        if cached is not None:
            return cached
        if k not in self.kinds():
            return None
        bundles = repo.load_runs(self._conn, self._pk, k)
        if not bundles:
            return None
        restored = restore_result(self._project(), bundles[-1])
        self._cache[k] = restored
        return restored

    def current(self) -> ScheduleResult | None:
        """The leveled result if one is applied, else the dependency-only result."""
        return self.get(RunKind.LEVELED) or self.get(RunKind.DEPENDENCY_ONLY)

    # ----------------------------------------------------------------- writes

    @staticmethod
    def _replaced_kinds(kind: RunKind) -> tuple[RunKind, ...]:
        if kind is RunKind.DEPENDENCY_ONLY:
            return (RunKind.DEPENDENCY_ONLY, RunKind.LEVELING_PREVIEW, RunKind.LEVELED)
        if kind is RunKind.LEVELED:
            return (RunKind.LEVELED, RunKind.LEVELING_PREVIEW)
        return (RunKind.LEVELING_PREVIEW,)

    def store(self, result: ScheduleResult) -> None:
        """Persist ``result`` atomically, replacing runs as described in the module doc."""
        kind = RunKind(result.kind)
        run, nodes, assignments, segments = result_to_records(result)
        with transaction(self._conn):
            for k in self._replaced_kinds(kind):
                repo.delete_runs(self._conn, self._pk, k)
            repo.save_run(self._conn, self._pk, run, nodes, assignments, segments, result.issues)
        for k in self._replaced_kinds(kind):
            self._cache.pop(k, None)
        runs = self._stored()
        for k in self._replaced_kinds(kind):
            runs.pop(k, None)
        runs[kind] = (result.schedule_fp, result.cost_fp)
        self._cache[kind] = result

    def clear(self, kinds: Iterable[RunKind | str] | None = None) -> None:
        """Delete stored runs of ``kinds`` (all kinds when ``None``)."""
        wanted = [RunKind(k) for k in kinds] if kinds is not None else list(RunKind)
        with transaction(self._conn):
            for k in wanted:
                repo.delete_runs(self._conn, self._pk, k)
        for k in wanted:
            self._cache.pop(k, None)
        runs = self._stored()
        for k in wanted:
            runs.pop(k, None)

    # ----------------------------------------------- rate-only result refresh

    def plan_refresh(self, project: Project) -> dict[RunKind, ScheduleResult]:
        """Recosted results for the stored runs whose dates still match ``project``.

        Used for edits that change costs or report views only (hourly rates,
        ``cost_report_unit``, ``working_days_per_year``). Runs whose schedule
        fingerprint differs are left alone (they are stale); unchanged ones are omitted.
        """
        plan: dict[RunKind, ScheduleResult] = {}
        if not self.kinds():
            return plan
        fp = schedule_fp(project)
        for kind in self.kinds():
            old = self.get(kind)
            if old is None or old.schedule_fp != fp:
                continue
            new = recost(old, project)
            if new != old:
                plan[kind] = new
        return plan

    def write_plan(self, plan: Mapping[RunKind, ScheduleResult]) -> None:
        """Replace the stored runs of the planned kinds (call inside the edit's transaction)."""
        for kind, result in plan.items():
            run, nodes, assignments, segments = result_to_records(result)
            repo.delete_runs(self._conn, self._pk, kind)
            repo.save_run(self._conn, self._pk, run, nodes, assignments, segments, result.issues)

    def apply_plan(self, plan: Mapping[RunKind, ScheduleResult]) -> None:
        """Update the cache after :meth:`write_plan` committed."""
        self._cache.update(plan)
        runs = self._stored()
        for kind, result in plan.items():
            runs[kind] = (result.schedule_fp, result.cost_fp)
