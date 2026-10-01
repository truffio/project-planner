"""Background jobs (T32): executors, progress, cancellation, stale-on-arrival results."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import project_planner.engine.schedule as engine
from project_planner.engine.config import Config
from project_planner.engine.errors import Cancelled, NotFound, ValidationFailed
from project_planner.engine.results import ScheduleResult
from project_planner.services import compute, jobs
from project_planner.services.jobs import InlineExecutor, Job
from project_planner.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.services

START = date(2026, 10, 5)
DAY = 480
TIMEOUT = 120.0  # generous: process-pool start-up on Windows is slow


# ------------------------------------------------------------------ helpers


def build(ws: Workspace, n_tasks: int = 20) -> tuple[str, list[str]]:
    """n tasks of 1 d, all on Alice at 60%, all at project start (leveling serialises them)."""
    ws.new_project("Demo", START, discard_unsaved=True)
    alice = ws.add_resource("Alice", hourly_rate="100")
    tasks = []
    for i in range(n_tasks):
        t = ws.add_task(f"T{i:05d}", duration="1d")
        ws.set_assignment(t, alice, percent=60)
        tasks.append(t)
    return alice, tasks


def invalid_workspace(path: Path) -> Workspace:
    """Workspace whose definition fails validation (150% entered under a 200% config)."""
    with open_workspace(path, config=Config(max_assignment_percent=Decimal(200))) as w:
        w.new_project("Bad", START)
        alice = w.add_resource("Alice")
        w.set_assignment(w.add_task("T1", duration="1d"), alice, percent=150)
    return open_workspace(path)


class GatedExecutor(ThreadPoolExecutor):
    """Thread pool whose calls wait for ``gate`` before running (holds jobs 'pending')."""

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.gate = threading.Event()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        def gated() -> Any:
            assert self.gate.wait(TIMEOUT)
            return fn(*args, **kwargs)

        return super().submit(gated)


def wait_until(predicate: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def watch(job: Job) -> list[float]:
    """Poll the job on this (owner) thread until done; returns the progress values seen."""
    seen = []
    deadline = time.monotonic() + TIMEOUT
    while not job.done():
        assert time.monotonic() < deadline, "job did not finish in time"
        seen.append(job.progress)
        assert isinstance(job.message, str)
        time.sleep(0.002)
    seen.append(job.progress)
    return seen


def timings(result: ScheduleResult) -> dict[str, tuple[int | None, int | None]]:
    return {n.node_id: (n.start_minutes, n.finish_minutes) for n in result.nodes.values()}


@pytest.fixture
def ws() -> Iterator[Workspace]:
    workspace = open_workspace()
    yield workspace
    workspace.close()


@pytest.fixture
def threads() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


@pytest.fixture
def gated() -> Iterator[GatedExecutor]:
    pool = GatedExecutor()
    yield pool
    pool.gate.set()
    pool.shutdown(wait=True)


@pytest.fixture(scope="module")
def process_pool() -> Iterator[None]:
    """The module-level process pool; shut down after the module's tests."""
    yield None
    jobs.shutdown()


@pytest.fixture(params=["inline", "thread"])
def executor(request: pytest.FixtureRequest) -> Iterator[Executor]:
    if request.param == "inline":
        yield InlineExecutor()
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            yield pool


# ------------------------------------------------- inline / thread executors


def test_schedule_job_matches_sync(ws: Workspace, executor: Executor) -> None:
    build(ws)
    sync = compute.schedule(ws)
    ws._discard_results()
    job = jobs.submit_schedule(ws, executor=executor)
    seen = watch(job)
    res = job.result(timeout=TIMEOUT)
    assert job.status == "done" and job.stored is True
    assert res == sync
    assert ws.result() == sync
    assert seen == sorted(seen) and seen[-1] == 1.0
    assert jobs.active_jobs(ws) == ()


def test_inline_job_is_stored_before_submit_returns(ws: Workspace) -> None:
    build(ws, 3)
    job = jobs.submit_schedule(ws, executor=InlineExecutor())
    assert ws.state().has_result is True
    assert job.status == "done" and job.progress == 1.0


def test_leveling_job_matches_sync(ws: Workspace, executor: Executor) -> None:
    _, tasks = build(ws, 30)
    compute.schedule(ws)
    sync = compute.level_preview(ws)
    compute.discard_leveling(ws)
    job = jobs.submit_leveling_preview(ws, executor=executor)
    seen = watch(job)
    preview = job.result(timeout=TIMEOUT)
    assert preview == sync
    assert ws.state().has_preview is True
    assert ws._results.get("leveling_preview") == sync.result
    assert len([d for d in preview.delays_days.values() if d > 0]) == len(tasks) - 1
    assert all(0.0 <= p <= 1.0 for p in seen)
    assert seen == sorted(seen) and seen[-1] == 1.0
    compute.apply_leveling(ws)
    current = ws.result()
    assert current is not None and current.kind == "leveled"


def test_leveling_progress_forwarded_from_engine(
    ws: Workspace, threads: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(ws, 300)
    compute.schedule(ws)
    real_level = engine.level

    def slow_level(*args: Any, progress: Any = None, **kwargs: Any) -> Any:
        def slow_progress(fraction: float, message: str) -> None:
            progress(fraction, message)
            time.sleep(0.003)

        return real_level(*args, progress=slow_progress, **kwargs)

    monkeypatch.setattr(engine, "level", slow_level)
    job = jobs.submit_leveling_preview(ws, executor=threads)
    seen: list[tuple[float, str]] = []
    while not job.done():
        seen.append((job.progress, job.message))
        time.sleep(0.001)
    job.result(timeout=TIMEOUT)
    intermediate = [(p, m) for p, m in seen if 0.0 < p < 1.0]
    assert intermediate, "no intermediate progress seen"
    assert any(m.startswith("Leveled ") for _, m in intermediate)
    fractions = [p for p, _ in seen] + [job.progress]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0


def test_leveling_job_calculates_missing_base(ws: Workspace, executor: Executor) -> None:
    _, tasks = build(ws, 5)
    job = jobs.submit_leveling_preview(ws, executor=executor)
    preview = job.result(timeout=TIMEOUT)
    st = ws.state()
    assert (st.has_result, st.has_preview, st.stale_dates) == (True, True, False)
    base = ws.result()
    assert base is not None and base.kind == "dependency_only"
    assert base.node(tasks[1]).start_minutes == 0
    assert preview.result.node(tasks[1]).start_minutes == DAY
    assert job.progress == 1.0


def test_worker_validation_error_surfaces(tmp_path: Path, executor: Executor) -> None:
    with invalid_workspace(tmp_path / "bad.db") as bad:
        for submit in (jobs.submit_schedule, jobs.submit_leveling_preview):
            job = submit(bad, executor=executor)
            with pytest.raises(ValidationFailed) as info:
                job.result(timeout=TIMEOUT)
            assert any(i.code == "ASSIGN_PERCENT_RANGE" for i in info.value.issues)
            assert job.status == "failed" and job.done() is True
            assert isinstance(job.error, ValidationFailed)
            assert job.stored is False
        assert bad.result() is None
        assert bad.state().has_preview is False


def test_result_timeout(ws: Workspace, gated: GatedExecutor) -> None:
    build(ws, 2)
    job = jobs.submit_schedule(ws, executor=gated)
    assert job.status == "pending"
    with pytest.raises(TimeoutError):
        job.result(timeout=0.05)
    gated.gate.set()
    assert job.result(timeout=TIMEOUT).kind == "dependency_only"


# --------------------------------------------------------------- cancellation


def test_cancel_before_running_stores_nothing(ws: Workspace, gated: GatedExecutor) -> None:
    build(ws, 5)
    base = compute.schedule(ws)
    sched_job = jobs.submit_schedule(ws, executor=gated)
    level_job = jobs.submit_leveling_preview(ws, executor=gated)
    assert sched_job.cancel() is True and level_job.cancel() is True
    assert sched_job.cancel() is False  # already cancelled
    for job in (sched_job, level_job):
        with pytest.raises(Cancelled):
            job.result(timeout=TIMEOUT)
        assert job.status == "cancelled" and job.done() is True
    gated.gate.set()
    gated.shutdown(wait=True)
    assert jobs.poll(ws) == []
    assert ws.result() is base
    assert ws.state().has_preview is False


def test_cancel_while_leveling_runs(
    ws: Workspace, threads: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(ws, 50)
    base = compute.schedule(ws)
    started, release = threading.Event(), threading.Event()
    outcome: list[BaseException] = []
    real_level = engine.level

    def blocking_level(*args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(TIMEOUT)
        try:
            return real_level(*args, **kwargs)
        except BaseException as exc:
            outcome.append(exc)
            raise

    monkeypatch.setattr(engine, "level", blocking_level)
    job = jobs.submit_leveling_preview(ws, executor=threads)
    assert started.wait(TIMEOUT)
    wait_until(lambda: job.status == "running")
    assert job.cancel() is True
    release.set()
    with pytest.raises(Cancelled):
        job.result(timeout=TIMEOUT)
    wait_until(lambda: bool(outcome))
    assert isinstance(outcome[0], Cancelled)  # the engine saw the cancel flag
    assert job.status == "cancelled"
    assert ws.result() is base
    assert ws.state().has_preview is False


def test_cancel_after_worker_finished_applies_nothing(ws: Workspace, gated: GatedExecutor) -> None:
    build(ws, 5)
    base = compute.schedule(ws)
    job = jobs.submit_leveling_preview(ws, executor=gated)
    gated.gate.set()
    wait_until(lambda: job._future.done())  # finished, not yet applied (no owner poll)
    assert job.cancel() is True
    with pytest.raises(Cancelled):
        job.result()
    assert ws.state().has_preview is False
    assert ws.result() is base


def test_cancel_after_done_is_noop(ws: Workspace) -> None:
    build(ws, 2)
    job = jobs.submit_schedule(ws, executor=InlineExecutor())
    assert job.cancel() is False
    assert job.status == "done"
    assert job.result().kind == "dependency_only"


# -------------------------------------------------- results arriving late


def test_edit_while_schedule_job_runs_gives_stale_result(
    ws: Workspace, gated: GatedExecutor
) -> None:
    _, tasks = build(ws, 10)
    job = jobs.submit_schedule(ws, executor=gated)
    late = ws.add_task("Added during job", duration="1d")
    gated.gate.set()
    res = job.result(timeout=TIMEOUT)
    assert job.stored is True
    assert ws.state().stale_dates is True
    assert ws.result() == res
    with pytest.raises(NotFound):
        res.node(late)
    assert res.node(tasks[0]).finish_minutes == DAY


def test_edit_while_leveling_job_runs_gives_stale_preview(
    ws: Workspace, gated: GatedExecutor
) -> None:
    _, tasks = build(ws, 10)
    compute.schedule(ws)
    job = jobs.submit_leveling_preview(ws, executor=gated)
    ws.set_sizing(tasks[0], duration="2d")
    gated.gate.set()
    job.result(timeout=TIMEOUT)
    st = ws.state()
    assert (st.has_preview, st.stale_dates) == (True, True)
    with pytest.raises(Exception, match="stale"):
        compute.apply_leveling(ws)


def test_rate_edit_while_job_runs_is_recosted(ws: Workspace, gated: GatedExecutor) -> None:
    alice, _ = build(ws, 4)
    job = jobs.submit_schedule(ws, executor=gated)
    ws.set_hourly_rate(alice, "200")
    gated.gate.set()
    res = job.result(timeout=TIMEOUT)
    st = ws.state()
    assert (st.stale_dates, st.stale_costs) == (False, False)
    assert res.total_cost == Decimal(4 * 8 * 200) * Decimal("0.6")
    assert ws.result() == res


def test_newer_sync_result_is_not_overwritten(ws: Workspace, gated: GatedExecutor) -> None:
    build(ws, 4)
    job = jobs.submit_schedule(ws, executor=gated)
    late = ws.add_task("Late", duration="1d")
    fresh = compute.schedule(ws)
    gated.gate.set()
    res = job.result(timeout=TIMEOUT)
    assert job.status == "done" and job.stored is False
    with pytest.raises(NotFound):
        res.node(late)
    assert ws.result() is fresh
    assert ws.state().stale_dates is False


def test_applied_leveling_not_dropped_by_late_schedule_job(
    ws: Workspace, gated: GatedExecutor
) -> None:
    build(ws, 4)
    job = jobs.submit_schedule(ws, executor=gated)
    compute.level_preview(ws)
    leveled = compute.apply_leveling(ws)
    gated.gate.set()
    job.result(timeout=TIMEOUT)
    assert job.stored is False
    assert ws.result() == leveled


def test_result_not_stored_after_project_replaced(ws: Workspace, gated: GatedExecutor) -> None:
    build(ws, 4)
    job = jobs.submit_schedule(ws, executor=gated)
    ws.new_project("Other", START, discard_unsaved=True)
    gated.gate.set()
    job.result(timeout=TIMEOUT)
    assert job.stored is False
    assert ws.result() is None


def test_other_thread_waits_owner_stores(ws: Workspace, threads: ThreadPoolExecutor) -> None:
    build(ws, 4)
    job = jobs.submit_schedule(ws, executor=threads)
    got: list[Any] = []
    waiter = threading.Thread(target=lambda: got.append(job.result(timeout=TIMEOUT)))
    waiter.start()
    waiter.join(TIMEOUT)
    assert got and got[0].kind == "dependency_only"
    assert job.stored is True  # reading on the owner thread applied it
    assert ws.result() == got[0]


def test_poll_applies_finished_jobs(ws: Workspace, gated: GatedExecutor) -> None:
    build(ws, 4)
    job = jobs.submit_schedule(ws, executor=gated)
    assert jobs.active_jobs(ws) == (job,)
    gated.gate.set()
    wait_until(lambda: job._future.done())
    assert ws._results.current() is None  # nobody polled yet (state() itself polls, T35)
    assert jobs.poll(ws) == [job]
    assert ws.state().has_result is True
    assert jobs.active_jobs(ws) == ()


# ------------------------------------------------------------ process pool


@pytest.mark.slow
def test_process_pool_jobs_match_sync(ws: Workspace, process_pool: None) -> None:
    _, tasks = build(ws, 40)
    sync = compute.schedule(ws)
    sync_preview = compute.level_preview(ws)
    ws._discard_results()
    job = jobs.submit_schedule(ws)
    assert job.result(timeout=TIMEOUT) == sync
    assert job.status == "done" and ws.result() == sync
    job = jobs.submit_leveling_preview(ws)
    seen = watch(job)
    preview = job.result(timeout=TIMEOUT)
    assert timings(preview.result) == timings(sync_preview.result)
    assert preview == sync_preview
    assert seen == sorted(seen) and seen[-1] == 1.0
    assert ws.state().has_preview is True
    assert len([d for d in preview.delays_days.values() if d > 0]) == len(tasks) - 1


@pytest.mark.slow
def test_process_pool_validation_error(tmp_path: Path, process_pool: None) -> None:
    with invalid_workspace(tmp_path / "bad.db") as bad:
        job = jobs.submit_leveling_preview(bad)
        with pytest.raises(ValidationFailed) as info:
            job.result(timeout=TIMEOUT)
        assert any(i.code == "ASSIGN_PERCENT_RANGE" for i in info.value.issues)
        assert job.status == "failed"
        assert bad.result() is None


@pytest.mark.slow
def test_process_pool_edit_while_running_is_stale(ws: Workspace, process_pool: None) -> None:
    build(ws, 50)
    job = jobs.submit_schedule(ws)
    late = ws.add_task("Added during job", duration="1d")
    res = job.result(timeout=TIMEOUT)
    assert ws.state().stale_dates is True
    with pytest.raises(NotFound):
        res.node(late)


@pytest.mark.slow
def test_process_pool_cancel_large_leveling(ws: Workspace, process_pool: None) -> None:
    _, tasks = build(ws, 1500)
    base = compute.schedule(ws)
    rev = ws.state().revision
    job = jobs.submit_leveling_preview(ws)
    job.cancel()
    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        job.result(timeout=TIMEOUT)
    assert time.monotonic() - t0 < 5.0
    assert job.done() is True and job.status == "cancelled"
    st = ws.state()
    assert (st.has_preview, st.revision) == (False, rev)
    assert ws.result() is base
    # the pool is still usable afterwards
    after = jobs.submit_schedule(ws)
    assert timings(after.result(timeout=TIMEOUT)) == timings(base)


# ------------------------------- explicit discard / apply after submit (finding 14)


@pytest.mark.parametrize("action", ["discard_leveling", "apply_leveling", "reset"])
def test_late_preview_not_stored_after_explicit_action(
    ws: Workspace, gated: GatedExecutor, action: str
) -> None:
    build(ws, 4)
    compute.schedule(ws)
    if action != "discard_leveling":
        compute.level_preview(ws)
    if action == "reset":
        compute.apply_leveling(ws)
    job = jobs.submit_leveling_preview(ws, executor=gated)  # worker held at the gate
    if action == "discard_leveling":
        compute.discard_leveling(ws)
        expected_kind = "dependency_only"
    elif action == "apply_leveling":
        compute.apply_leveling(ws)
        expected_kind = "leveled"
    else:
        compute.reset_to_dependency_schedule(ws)
        expected_kind = "dependency_only"
    gated.gate.set()
    job.result(timeout=TIMEOUT)
    assert job.status == "done" and job.stored is False
    assert ws.state().has_preview is False
    current = ws.result()
    assert current is not None and current.kind == expected_kind


def test_late_schedule_job_not_stored_after_discard_results(
    ws: Workspace, gated: GatedExecutor
) -> None:
    build(ws, 4)
    compute.schedule(ws)
    job = jobs.submit_schedule(ws, executor=gated)
    ws._discard_results()
    gated.gate.set()
    job.result(timeout=TIMEOUT)
    assert job.stored is False
    assert ws.result() is None


def test_preview_job_still_stored_without_explicit_action(
    ws: Workspace, gated: GatedExecutor
) -> None:
    build(ws, 4)
    compute.schedule(ws)
    job = jobs.submit_leveling_preview(ws, executor=gated)
    gated.gate.set()
    job.result(timeout=TIMEOUT)
    assert job.stored is True and ws.state().has_preview is True
