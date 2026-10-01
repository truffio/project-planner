"""Background compute jobs: ``submit_schedule`` / ``submit_leveling_preview`` -> :class:`Job`.

Threading model (architecture section 8.4)
------------------------------------------
* ``submit_*`` must be called on the thread that owns the workspace (its SQLite
  connection is single-threaded). It takes an immutable snapshot of ``ws.project()``
  (plus the current dependency-only base for leveling), hands it to an executor and
  returns at once. Workers never see the workspace.
* Executors: by default a module-level, lazily created :class:`ProcessPoolExecutor`
  (``spawn`` start method; :func:`shutdown` closes it). :class:`InlineExecutor` runs
  the job synchronously inside ``submit_*`` (notebooks, tests); any other executor,
  e.g. a :class:`~concurrent.futures.ThreadPoolExecutor`, works too.
* Progress and cancellation: the worker writes ``(fraction, message)`` tuples to a
  queue and polls a cancel event (through ``engine.leveling.level(cancel=...)``). For
  process pools both live in a module-level ``multiprocessing`` Manager; otherwise
  they are a plain :class:`queue.Queue` / :class:`threading.Event`.

When results are stored
-----------------------
A finished result is stored in the workspace **only on the owner thread** (the one
that called ``submit_*``), the first time one of these runs there after the worker
finished: ``job.result()``, ``job.done()``, ``job.poll()``, reading ``job.status`` /
``job.progress`` / ``job.message``, or :func:`poll` (``ws``-wide; meant for a UI's
``root.after`` loop and for ``Workspace.state()`` / ``result()``). With
:class:`InlineExecutor` that is before ``submit_*`` returns. Calls made on other
threads only drain progress; there ``status`` stays ``"running"`` until the owner
thread applies the result, and ``result()`` returns the value without storing it.

* A cancelled job never stores anything, even if its worker had already finished;
  ``cancel()`` is terminal at once (the worker stops at its next cancel poll).
* The result reflects the submit-time snapshot. If the definition was edited
  meanwhile it is still stored, carrying its own fingerprints, so ``state()`` shows
  it as stale - never as current. If only rates changed, it is recosted on arrival
  (as edits do with stored results).
* Newer explicit actions win: if, after submission, another result was stored or
  discarded and the workspace now holds a *current* run that this result would
  replace, the result is not stored (``job.stored is False``); the same holds after
  the workspace project was replaced (load / import / new project). A result whose
  kind was explicitly removed after submission is not stored either: a preview after
  ``discard_leveling()``, ``apply_leveling()`` or ``reset_to_dependency_schedule()``,
  and any result after a discard of its kind (finding 14). The fresh dependency-only
  base a leveling job may have calculated is still stored by the usual rules.

Windows / frozen apps: process workers are started with ``spawn``, so worker
functions are top-level and arguments are pickled. A PyInstaller-built entry point
must call :func:`multiprocessing.freeze_support` first thing in ``main``, and
scripts using the process pool need the ``if __name__ == "__main__":`` guard.
"""

from __future__ import annotations

import atexit
import contextlib
import dataclasses
import math
import multiprocessing
import os
import queue
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from concurrent.futures import wait as wait_futures
from concurrent.futures.process import BrokenProcessPool
from multiprocessing.managers import SyncManager
from typing import TYPE_CHECKING, Any, Literal, ParamSpec, Protocol, TypeVar

import project_planner.engine.schedule as engine
from project_planner.engine.config import Config
from project_planner.engine.errors import Cancelled
from project_planner.engine.fingerprint import cost_fp, schedule_fp
from project_planner.engine.model import Project
from project_planner.engine.results import LevelingResult, ScheduleResult
from project_planner.persistence.records import RunKind
from project_planner.services.compute import current_base
from project_planner.services.events import Event, EventKind

if TYPE_CHECKING:
    from project_planner.services.workspace import Workspace

__all__ = [
    "InlineExecutor",
    "Job",
    "JobKind",
    "JobStatus",
    "active_jobs",
    "default_executor",
    "poll",
    "shutdown",
    "submit_leveling_preview",
    "submit_schedule",
]

JobStatus = Literal["pending", "running", "done", "failed", "cancelled"]
JobKind = Literal["schedule", "leveling_preview"]

_P = ParamSpec("_P")
_T = TypeVar("_T")

_TERMINAL: frozenset[str] = frozenset({"done", "failed", "cancelled"})
_KINDS: frozenset[str] = frozenset(k.value for k in RunKind)
_WAIT_SLICE = 0.05  # seconds between checks while result() waits
_PROXY_CANCEL_INTERVAL = 0.02  # seconds between cancel-event round trips to the Manager


# --------------------------------------------------------------------- executors


class InlineExecutor(Executor):
    """Runs each submitted call immediately in the caller's thread (no concurrency)."""

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        future: Future[_T] = Future()
        future.set_running_or_notify_cancel()
        try:
            future.set_result(fn(*args, **kwargs))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            future.set_exception(exc)
        return future


_pool_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_manager: SyncManager | None = None


def _max_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def default_executor() -> ProcessPoolExecutor:
    """The module-level process pool (created on first use, ``spawn`` start method)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(
                max_workers=_max_workers(), mp_context=multiprocessing.get_context("spawn")
            )
        return _pool


def _shared_manager() -> SyncManager:
    global _manager
    with _pool_lock:
        if _manager is None:
            manager = multiprocessing.get_context("spawn").Manager()
            _manager = manager
        return _manager


def _discard_pool(pool: ProcessPoolExecutor) -> None:
    global _pool
    with _pool_lock:
        if _pool is pool:
            _pool = None
    pool.shutdown(wait=False, cancel_futures=True)


def shutdown(wait: bool = True) -> None:
    """Shut down the module process pool and Manager (recreated on next use).

    Pending jobs on the pool are cancelled; running workers finish (``wait=True``
    waits for them).
    """
    global _pool, _manager
    with _pool_lock:
        pool, manager = _pool, _manager
        _pool = _manager = None
    if pool is not None:
        pool.shutdown(wait=wait, cancel_futures=True)
    if manager is not None:
        manager.shutdown()


atexit.register(shutdown, wait=False)


# ------------------------------------------------------------- worker side


class _Queue(Protocol):
    def put(self, item: Any) -> None: ...
    def get_nowait(self) -> Any: ...


class _Event(Protocol):
    def set(self) -> None: ...
    def is_set(self) -> bool: ...


@dataclasses.dataclass(frozen=True)
class _Channel:
    """Progress queue + cancel event shared with the worker (picklable for Manager proxies)."""

    messages: _Queue
    cancel: _Event
    cancel_interval: float = 0.0


class _CancelPoll:
    """``cancel()`` callable for the engine; rate-limits round trips to a Manager event."""

    def __init__(self, event: _Event, interval: float) -> None:
        self._event = event
        self._interval = interval
        self._last = -math.inf

    def __call__(self) -> bool:
        now = time.monotonic()
        if now - self._last < self._interval:
            return False
        self._last = now
        return bool(self._event.is_set())


def _started(channel: _Channel, message: str) -> None:
    if channel.cancel.is_set():
        raise Cancelled("job cancelled before it started")
    channel.messages.put((0.0, message))


def schedule_worker(project: Project, config: Config, channel: _Channel) -> ScheduleResult:
    """Worker entry point of a schedule job (top level so ``spawn`` can import it)."""
    _started(channel, f"Scheduling {len(project.nodes)} nodes")
    result = engine.schedule(project, config)
    channel.messages.put((1.0, "Schedule complete"))
    return result


def leveling_worker(
    project: Project, config: Config, base: ScheduleResult | None, channel: _Channel
) -> tuple[ScheduleResult | None, LevelingResult]:
    """Worker entry point of a leveling job: ``(fresh base or None, preview)``.

    ``base`` is ``None`` when the workspace had no current dependency-only result; the
    worker then calculates one first (it is stored before the preview).
    """
    _started(channel, "Leveling")
    fresh: ScheduleResult | None = None
    low = 0.0
    if base is None:
        channel.messages.put((0.0, "Calculating the dependency-only schedule"))
        fresh = base = engine.schedule(project, config)
        low = 0.1
        channel.messages.put((low, "Leveling"))

    def progress(fraction: float, message: str) -> None:
        channel.messages.put((low + (1.0 - low) * fraction, message))

    preview = engine.level(
        project,
        base,
        config,
        progress=progress,
        cancel=_CancelPoll(channel.cancel, channel.cancel_interval),
    )
    return fresh, preview


# ------------------------------------------------------------- owner side

_registry_lock = threading.Lock()
_registry: weakref.WeakKeyDictionary[Workspace, list[Job]] = weakref.WeakKeyDictionary()


def _replaced_kinds(kind: str) -> tuple[RunKind, ...]:
    """Run kinds a stored result of ``kind`` replaces (mirrors ``ResultStore`` rules)."""
    k = RunKind(kind)
    if k is RunKind.DEPENDENCY_ONLY:
        return (RunKind.DEPENDENCY_ONLY, RunKind.LEVELING_PREVIEW, RunKind.LEVELED)
    if k is RunKind.LEVELED:
        return (RunKind.LEVELED, RunKind.LEVELING_PREVIEW)
    return (RunKind.LEVELING_PREVIEW,)


class Job:
    """Handle of a submitted calculation (plan section 3).

    Attributes (read-only properties): ``kind``, ``status`` (``"pending"`` until the
    worker starts, ``"running"``, then ``"done"`` / ``"failed"`` / ``"cancelled"``),
    ``progress`` (0..1, never decreasing, 1.0 on success), ``message``, ``stored``
    (whether the result was stored in the workspace; ``None`` until known),
    ``error`` (the worker's exception for failed jobs).

    See the module docstring for when results are stored. Not created directly; use
    :func:`submit_schedule` / :func:`submit_leveling_preview`.
    """

    def __init__(
        self, ws: Workspace, kind: JobKind, future: Future[Any], channel: _Channel
    ) -> None:
        self._ws = ws
        self._kind: JobKind = kind
        self._future = future
        self._channel = channel
        self._owner = threading.get_ident()
        self._lock = threading.RLock()
        self._status: JobStatus = "pending"
        self._progress = 0.0
        self._message = "Queued"
        self._value: Any = None
        self._error: BaseException | None = None
        self._stored: bool | None = None
        self._delivering = False
        self._overtaken = False  # a result was stored / discarded by someone else
        self._dropped: set[RunKind] = set()  # kinds explicitly removed after submit
        self._replaced = False  # the workspace project was replaced
        self._unsubscribe: Callable[[], None] | None = ws.subscribe(self._on_event)

    def __repr__(self) -> str:
        return f"<Job {self._kind} {self._status} {self._progress:.0%}>"

    # ----------------------------------------------------------- public API

    @property
    def kind(self) -> JobKind:
        return self._kind

    @property
    def status(self) -> JobStatus:
        self._pump()
        return self._status

    @property
    def progress(self) -> float:
        self._pump()
        return self._progress

    @property
    def message(self) -> str:
        self._pump()
        return self._message

    @property
    def stored(self) -> bool | None:
        self._pump()
        return self._stored

    @property
    def error(self) -> BaseException | None:
        self._pump()
        return self._error

    def done(self) -> bool:
        """Whether the job is finished (done, failed or cancelled); applies a result."""
        self._pump()
        return self._status in _TERMINAL

    def poll(self) -> bool:
        """Drain progress and, on the owner thread, store a finished result; ``done()``."""
        return self.done()

    def cancel(self) -> bool:
        """Cancel the job; nothing will be stored. False if it had already finished."""
        with self._lock:
            if self._status in _TERMINAL or self._delivering:
                return False
            # A Manager that is already gone does not matter: the result is ignored anyway.
            with contextlib.suppress(Exception):
                self._channel.cancel.set()
            self._future.cancel()
            self._finish("cancelled", "Cancelled")
            return True

    def result(self, timeout: float | None = None) -> Any:
        """Wait for the job and return its result (``ScheduleResult`` / ``LevelingResult``).

        On the owner thread the result is stored first (see the module docstring).

        Raises:
            Cancelled: the job was cancelled.
            TimeoutError: not finished within ``timeout`` seconds.
            Exception: the worker's error as raised there (e.g. ``ValidationFailed``).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self._pump()
            with self._lock:
                if self._status in _TERMINAL:
                    break
                if self._future.done() and not self._on_owner_thread():
                    if self._future.cancelled():
                        raise Cancelled("job cancelled")
                    return self._future.result()  # owner thread stores it later
            slice_ = _WAIT_SLICE
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{self._kind} job not finished after {timeout} s")
                slice_ = min(slice_, remaining)
            wait_futures([self._future], timeout=slice_)
        if self._status == "cancelled":
            raise Cancelled("job cancelled")
        if self._status == "failed":
            assert self._error is not None
            raise self._error
        return self._value

    # ----------------------------------------------------------- internals

    def _on_owner_thread(self) -> bool:
        return threading.get_ident() == self._owner

    def _on_event(self, event: Event) -> None:
        if self._delivering:
            return
        if event.kind is EventKind.PROJECT_REPLACED:
            self._replaced = True
        elif event.kind is EventKind.RESULT_STORED:
            self._overtaken = True
            if event.operation == "discard_results":
                self._dropped.update(RunKind(k) for k in event.ids)
            elif RunKind.LEVELED in (RunKind(k) for k in event.ids if k in _KINDS):
                # applying leveling consumes the preview
                self._dropped.add(RunKind.LEVELING_PREVIEW)

    def _on_future_done(self, _future: Future[Any]) -> None:
        if self._on_owner_thread():  # inline executor: store before submit returns
            self._pump()

    def _drain(self) -> None:
        while True:
            try:
                fraction, message = self._channel.messages.get_nowait()
            except queue.Empty:
                return
            except Exception:  # Manager connection closed
                return
            if self._status == "pending":
                self._status = "running"
            self._progress = max(self._progress, min(1.0, float(fraction)))
            self._message = str(message)

    def _finish(self, status: JobStatus, message: str) -> None:
        self._status = status
        self._message = message
        if status == "done":
            self._progress = 1.0
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        with _registry_lock:
            jobs = _registry.get(self._ws)
            if jobs is not None and self in jobs:
                jobs.remove(self)

    def _pump(self) -> None:
        with self._lock:
            if self._status in _TERMINAL or self._delivering:
                return
            self._drain()
            future = self._future
            if not future.done():
                return
            if future.cancelled():
                self._finish("cancelled", "Cancelled")
                return
            error = future.exception()
            if isinstance(error, Cancelled):
                self._finish("cancelled", "Cancelled")
                return
            if error is not None:
                self._error = error
                self._stored = False
                self._finish("failed", f"Failed: {error}")
                return
            if self._status == "pending":
                self._status = "running"
            if not self._on_owner_thread():
                return
            self._delivering = True
            try:
                self._value = self._deliver(future.result())
            except Exception as exc:  # storing failed (e.g. workspace closed)
                self._error = exc
                self._stored = False
                self._finish("failed", f"Failed to store the result: {exc}")
                return
            finally:
                self._delivering = False
            self._finish("done", "Done" if self._stored else "Done (result not stored)")

    def _deliver(self, value: Any) -> Any:
        """Store ``value`` in the workspace (owner thread); returns the job's result."""
        ws = self._ws
        project = ws.project()
        sfp, cfp = schedule_fp(project), cost_fp(project)

        def store(result: ScheduleResult) -> tuple[ScheduleResult, bool]:
            if result.schedule_fp == sfp and result.cost_fp != cfp:
                result = engine.recost(result, project)  # rate-only edits meanwhile
            if self._replaced:
                return result, False
            if RunKind(result.kind) in self._dropped:
                return result, False  # discarded / applied after submission: user wins
            if self._overtaken:
                runs = ws._results.fingerprints()
                if any(runs[k][0] == sfp for k in _replaced_kinds(result.kind) if k in runs):
                    return result, False
            ws._store_result(result)
            return result, True

        if self._kind == "schedule":
            result, self._stored = store(value)
            return result
        fresh, preview = value
        if fresh is not None:
            store(fresh)
        result, self._stored = store(preview.result)
        return dataclasses.replace(preview, result=result)


# ------------------------------------------------------------------ submit


def _channel_for(executor: Executor) -> _Channel:
    if isinstance(executor, ProcessPoolExecutor):
        manager = _shared_manager()
        return _Channel(manager.Queue(), manager.Event(), _PROXY_CANCEL_INTERVAL)
    return _Channel(queue.Queue(), threading.Event())


def _submit(
    ws: Workspace,
    kind: JobKind,
    executor: Executor | None,
    fn: Callable[..., Any],
    *args: Any,
) -> Job:
    if executor is None:
        pool = default_executor()
        channel = _channel_for(pool)
        try:
            future = pool.submit(fn, *args, channel)
        except (BrokenProcessPool, RuntimeError):
            _discard_pool(pool)  # broken or shut down module pool: start a fresh one
            future = default_executor().submit(fn, *args, channel)
    else:
        channel = _channel_for(executor)
        future = executor.submit(fn, *args, channel)
    job = Job(ws, kind, future, channel)
    with _registry_lock:
        _registry.setdefault(ws, []).append(job)
    future.add_done_callback(job._on_future_done)
    return job


def submit_schedule(ws: Workspace, *, executor: Executor | None = None) -> Job:
    """Calculate the dependency-only schedule of the current definition in the background.

    Call on the workspace's thread. ``job.result()`` returns the ``ScheduleResult``
    (stored like :func:`compute.schedule` would, see the module docstring) or raises
    ``ValidationFailed`` / ``Cancelled``.
    """
    return _submit(ws, "schedule", executor, schedule_worker, ws.project(), ws.config)


def submit_leveling_preview(ws: Workspace, *, executor: Executor | None = None) -> Job:
    """Compute a leveling preview in the background (like :func:`compute.level_preview`).

    The current dependency-only result is the base; if it is missing or stale the
    worker calculates a fresh one, stored on arrival before the preview.
    ``job.result()`` returns the ``LevelingResult``.
    """
    return _submit(
        ws,
        "leveling_preview",
        executor,
        leveling_worker,
        ws.project(),
        ws.config,
        current_base(ws),
    )


def active_jobs(ws: Workspace) -> tuple[Job, ...]:
    """Jobs of ``ws`` that are not finished yet (in submission order)."""
    with _registry_lock:
        return tuple(_registry.get(ws, ()))


def poll(ws: Workspace) -> list[Job]:
    """Drain progress of every active job of ``ws``; store finished results.

    Call on the workspace's thread (e.g. from a Tk ``after`` loop). Returns the jobs
    that finished during this call.
    """
    return [job for job in active_jobs(ws) if job.poll()]
