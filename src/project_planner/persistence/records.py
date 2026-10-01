"""Persistence-level records mirroring the result tables.

The service layer maps between these and the engine's ``ScheduleResult``. Minutes
are ``int``; Decimals are exact (stored as TEXT).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from project_planner.engine.errors import Issue

__all__ = [
    "AssignmentResultRecord",
    "LoadingSegmentRecord",
    "NodeResultRecord",
    "ProjectRecord",
    "RunBundle",
    "RunKind",
    "ScheduleRunRecord",
]


class RunKind(StrEnum):
    """Kind of a stored schedule run."""

    DEPENDENCY_ONLY = "dependency_only"
    LEVELING_PREVIEW = "leveling_preview"
    LEVELED = "leveled"


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """The ``projects`` row (metadata, not the definition)."""

    pk: int
    kind: str
    name: str
    revision: int
    based_on_pk: int | None
    based_on_revision: int | None
    saved_at: str | None


@dataclass(frozen=True, slots=True)
class ScheduleRunRecord:
    """One ``schedule_runs`` row. ``pk`` is ``None`` before the run is saved."""

    kind: RunKind
    schedule_fp: str
    cost_fp: str
    complete: bool
    created_at: str
    project_finish: dt.date | None = None
    working_span_minutes: int | None = None
    pk: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RunKind(self.kind))


@dataclass(frozen=True, slots=True)
class NodeResultRecord:
    """One ``node_results`` row."""

    node_id: str
    status: str
    cost_complete: bool = True
    start_minute: int | None = None
    finish_minute: int | None = None
    duration_minutes: int | None = None
    effort_person_minutes: Decimal | None = None
    leveling_delay_minutes: int = 0
    cost: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AssignmentResultRecord:
    """One ``assignment_results`` row."""

    task_id: str
    resource_id: str
    assignment_minutes: Decimal
    cost_complete: bool = True
    cost: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LoadingSegmentRecord:
    """One ``loading_segments`` row (a constant-load interval of a resource)."""

    resource_id: str
    start_minute: int
    end_minute: int
    percent: Decimal
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunBundle:
    """A stored run with all its child rows, as returned by ``load_runs``."""

    run: ScheduleRunRecord
    nodes: tuple[NodeResultRecord, ...] = field(default=())
    assignments: tuple[AssignmentResultRecord, ...] = field(default=())
    segments: tuple[LoadingSegmentRecord, ...] = field(default=())
    issues: tuple[Issue, ...] = field(default=())
