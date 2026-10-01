"""Workspace change notifications (callback interface; no threading assumptions).

Callbacks run synchronously, on the caller's thread, after the change is committed.
A callback that raises is logged and does not affect the edit or other callbacks.
UIs that need to hop threads (e.g. Tk ``root.after``) do so inside their callback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Event", "EventBus", "EventKind"]

_log = logging.getLogger(__name__)


class EventKind(StrEnum):
    """What happened."""

    EDITED = "edited"
    RESULT_STORED = "result_stored"
    PROJECT_REPLACED = "project_replaced"


@dataclass(frozen=True, slots=True)
class Event:
    """One notification.

    Attributes:
        kind: :class:`EventKind`.
        revision: Workspace revision after the change.
        ids: Affected object IDs (nodes, resources, dependencies, ``"t1/r1"`` for
            assignments; empty for project-wide changes).
        operation: Name of the operation (``"add_task"``, ``"store_result"``, ...).
    """

    kind: EventKind
    revision: int
    ids: tuple[str, ...] = ()
    operation: str = ""


Callback = Callable[[Event], None]


class EventBus:
    """Ordered list of subscribers."""

    def __init__(self) -> None:
        self._callbacks: list[Callback] = []

    def subscribe(self, callback: Callback) -> Callable[[], None]:
        """Register ``callback``; returns a function that unsubscribes it (idempotent)."""
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe

    def emit(self, event: Event) -> None:
        """Deliver ``event`` to every subscriber (a snapshot of the list)."""
        for callback in list(self._callbacks):
            try:
                callback(event)
            except Exception:
                _log.exception("workspace event callback failed")
