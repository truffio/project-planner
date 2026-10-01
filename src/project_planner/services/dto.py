"""Plain data objects returned by the workspace service."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DeletePreview", "WorkspaceState"]


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """Cheap status of the workspace (plan section 1.2, assumption A9).

    Attributes:
        revision: Bumped by every edit (not by storing a result).
        dirty: The revision differs from the one last saved / loaded.
        stale_dates: A result exists whose schedule fingerprint no longer matches.
        stale_costs: A result exists whose cost fingerprint no longer matches.
        has_preview: A leveling preview is stored.
        has_result: A dependency-only or leveled result is stored.
    """

    revision: int
    dirty: bool
    stale_dates: bool
    stale_costs: bool
    has_preview: bool
    has_result: bool


@dataclass(frozen=True, slots=True)
class DeletePreview:
    """What a delete would remove; pass ``token`` to ``delete_commit``.

    Attributes:
        token: Opaque; valid only while the revision is unchanged.
        requested: The IDs asked for (nodes first, then resources).
        nodes: Every node removed, including descendants (WBS order).
        resources: Resources removed.
        dependencies: Every dependency touching a removed node.
        assignments: Every ``(task_id, resource_id)`` removed (by node or resource).
        revision: Revision the preview was taken at.
    """

    token: str
    requested: tuple[str, ...]
    nodes: tuple[str, ...]
    resources: tuple[str, ...]
    dependencies: tuple[str, ...]
    assignments: tuple[tuple[str, str], ...]
    revision: int
