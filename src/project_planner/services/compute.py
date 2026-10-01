"""Synchronous compute service: scheduling and the leveling state machine (task T32).

Module-level functions taking the :class:`~project_planner.services.workspace.Workspace`
as first argument; ``Workspace`` exposes them as thin methods (``ws.schedule()`` ...).
They run on the caller's thread (the thread that owns the workspace connection). For
non-blocking work with progress and cancellation see :mod:`project_planner.services.jobs`.

Leveling interaction (spec section 10)::

    schedule()  ->  dependency_only           (replaces every stored run)
    level_preview()  ->  + leveling_preview   (the current result is unchanged)
    apply_leveling()  ->  leveled             (preview dropped; ws.result() is leveled)
    discard_leveling()  ->  preview dropped
    reset_to_dependency_schedule()  ->  leveled + preview dropped

Leveling never runs by itself: edits only make stored results stale (``state()``
compares fingerprints); the user has to ask for a new calculation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import project_planner.engine.schedule as engine
from project_planner.engine.errors import Conflict
from project_planner.engine.fingerprint import schedule_fp
from project_planner.engine.results import LevelingResult, ScheduleResult
from project_planner.persistence.records import RunKind

if TYPE_CHECKING:
    from project_planner.services.workspace import Workspace

__all__ = [
    "apply_leveling",
    "current_base",
    "discard_leveling",
    "level_preview",
    "reset_to_dependency_schedule",
    "schedule",
]


def schedule(ws: Workspace) -> ScheduleResult:
    """Calculate the dependency-only schedule of the current definition and store it.

    The new result replaces every stored run (dependency-only, preview and leveled),
    so an applied leveling is dropped; it is never re-leveled automatically.

    Raises:
        ValidationFailed: structural errors (nothing is stored).
    """
    result = engine.schedule(ws.project(), ws.config)
    ws._store_result(result)
    return result


def current_base(ws: Workspace) -> ScheduleResult | None:
    """The stored dependency-only result if its dates match the definition, else ``None``."""
    base = ws._results.get(RunKind.DEPENDENCY_ONLY)
    if base is None or base.schedule_fp != schedule_fp(ws.project()):
        return None
    return base


def level_preview(ws: Workspace) -> LevelingResult:
    """Compute a leveling preview from the current dependency-only schedule and store it.

    If the stored dependency-only result is missing or stale, a fresh one is calculated
    and stored first (like :func:`schedule`; by the store rules this drops stale leveled
    and preview runs). The preview does not change ``ws.result()``; apply or discard it.

    Raises:
        ValidationFailed: structural errors (nothing new is stored).
    """
    base = current_base(ws)
    if base is None:
        base = schedule(ws)
    preview = engine.level(ws.project(), base, ws.config)
    ws._store_result(preview.result)
    return preview


def apply_leveling(ws: Workspace) -> ScheduleResult:
    """Make the stored preview the leveled schedule (``ws.result()``); drops the preview.

    Raises:
        Conflict: no preview is stored, or the definition changed since it was made.
    """
    preview = ws._results.get(RunKind.LEVELING_PREVIEW)
    if preview is None:
        raise Conflict("no leveling preview to apply")
    if preview.schedule_fp != schedule_fp(ws.project()):
        raise Conflict("the leveling preview is stale; level again")
    leveled = engine.with_kind(preview, "leveled")
    ws._store_result(leveled)
    return leveled


def discard_leveling(ws: Workspace) -> None:
    """Drop the stored leveling preview, if any (the current result is unchanged)."""
    ws._discard_results([RunKind.LEVELING_PREVIEW])


def reset_to_dependency_schedule(ws: Workspace) -> ScheduleResult | None:
    """Drop the leveled and preview runs; the dependency-only result becomes current.

    Nothing is recalculated: a stale dependency-only result stays (visibly) stale.
    Returns the new ``ws.result()`` (``None`` if no dependency-only result is stored).
    """
    ws._discard_results([RunKind.LEVELED, RunKind.LEVELING_PREVIEW])
    return ws.result()
