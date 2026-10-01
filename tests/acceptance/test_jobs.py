"""A23 (backend part): submit_* -> Job, progress, cancellation, results arriving stale.

A22 (launching the desktop application window on Windows) is a UI / packaging
scenario and is OUT OF SCOPE for this backend phase; it has no test here.

Timing note: the cancellation test cancels immediately after submit on a project
built to make leveling slow (many mutually overlapping tasks on one resource).
If an implementation ever finishes such a job before cancel() takes effect the
test would fail rather than pass silently; generous timeouts (JOB_TIMEOUT) guard
against slow process-pool start-up on Windows.
"""

from __future__ import annotations

import time

import pytest

import project_planner as pp

from .conftest import current, id_of, new_project, new_ws, node_row, oct26

JOB_TIMEOUT = 120.0  # seconds


def _overloaded_project(n_tasks: int):
    """n tasks of 1 d, all on Alice at 60%, all starting at project start (heavy overload)."""
    ws = new_ws()
    new_project(ws)
    alice = ws.add_resource("Alice", hourly_rate="100")
    tasks = []
    for i in range(n_tasks):
        t = ws.add_task(f"T{i:05d}", duration="1d")
        ws.set_assignment(t, alice, percent=60)
        tasks.append(t)
    return ws, alice, tasks


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T32: async schedule equals sync schedule (A23)")
def test_a23_submit_schedule_matches_synchronous_schedule():
    ws, _, tasks = _overloaded_project(20)
    sync = ws.schedule()
    job = ws.submit_schedule()
    res = job.result(timeout=JOB_TIMEOUT)
    assert job.done() is True
    assert job.status == "done"
    for t in tasks:
        assert (node_row(res, t).start, node_row(res, t).finish) == (
            node_row(sync, t).start,
            node_row(sync, t).finish,
        )
    # every 1 d task: Mon 5 09:00 - Mon 5 17:00 (no dependencies, loading uncapped)
    assert node_row(res, tasks[0]).finish == oct26(5, "17:00")


@pytest.mark.acceptance
@pytest.mark.slow
@pytest.mark.xfail(strict=True, reason="T32: job progress reported (A23)")
def test_a23_leveling_job_reports_progress():
    ws, _, tasks = _overloaded_project(200)
    ws.schedule()
    job = ws.submit_leveling_preview()
    seen = []
    deadline = time.monotonic() + JOB_TIMEOUT
    while not job.done():
        assert time.monotonic() < deadline, "leveling job did not finish in time"
        seen.append(float(job.progress))
        assert isinstance(job.message, str)
        time.sleep(0.01)
    seen.append(float(job.progress))
    preview = job.result(timeout=JOB_TIMEOUT)
    assert all(0.0 <= p <= 1.0 for p in seen)
    assert seen == sorted(seen)  # monotone non-decreasing
    assert seen[-1] == 1.0
    assert ws.state().has_preview is True
    # Two 60% tasks never overlap after leveling: tasks are serialised one per working day,
    # so the preview delays 199 of the 200 tasks.
    assert len([d for d in preview.delays_days.values() if d > 0]) == len(tasks) - 1


@pytest.mark.acceptance
@pytest.mark.slow
@pytest.mark.xfail(strict=True, reason="T32: cancelled leveling applies nothing (A23)")
def test_a23_cancelled_leveling_applies_nothing():
    ws, _, tasks = _overloaded_project(1500)
    base = ws.schedule()
    rev = ws.state().revision
    job = ws.submit_leveling_preview()
    job.cancel()
    with pytest.raises(pp.Cancelled):
        job.result(timeout=JOB_TIMEOUT)
    assert job.done() is True
    assert job.status == "cancelled"
    st = ws.state()
    # no partial result: no preview, current schedule untouched
    assert st.has_preview is False
    assert st.revision == rev
    now = current(ws)
    assert now.kind == "dependency_only"
    for t in tasks[:: max(1, len(tasks) // 50)]:
        assert node_row(now, t).start == node_row(base, t).start


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T32: result arriving after an edit is stale (A23)")
def test_a23_edit_while_job_runs_yields_stale_result():
    ws, alice, tasks = _overloaded_project(50)
    job = ws.submit_schedule()
    # Whether the job is still running or has just finished, the edit is newer than the
    # snapshot the job was computed from, so the stored result must be stale.
    late = ws.add_task("Added during job", duration="1d")
    res = job.result(timeout=JOB_TIMEOUT)
    assert ws.state().stale_dates is True
    # the result reflects the snapshot taken at submit time
    with pytest.raises(pp.NotFound):
        node_row(res, late)
    assert node_row(res, tasks[0]).finish == oct26(5, "17:00")
    assert id_of(late) not in {id_of(t) for t in tasks}
