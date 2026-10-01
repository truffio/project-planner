"""Issue type and exception hierarchy shared by the engine, services and facade.

Contract (plan section 3, frozen in T10):

* :class:`Issue` is the single, structured description of a problem. Validation,
  scheduling, CSV import and the services all report problems as ``Issue``
  values. CSV import fills ``line`` (1-based physical line) and maps
  ``record type -> object_type``, ``row id -> object_id``, ``column -> field``.
* Every exception raised on purpose by this package derives from
  :class:`PlannerError`.

Conventions:

* ``Issue.code`` is a stable, upper-case identifier (``"TIME_UNITLESS"``,
  ``"CSV_BAD_NUMBER"``...). Tests and UIs match on codes, never on messages.
* ``Issue.object_type`` uses the :class:`ObjectType` values for model objects
  (CSV import uses its record type names instead, e.g. ``"NODE"``).
* ``Issue.field`` is the attribute / argument / CSV column name the issue is
  about (``"hours_per_day"``, ``"percent"``...), or ``None`` for whole-object issues.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Cancelled",
    "Conflict",
    "ImportFailed",
    "Issue",
    "NotFound",
    "ObjectType",
    "PlannerError",
    "Severity",
    "UnsavedChanges",
    "ValidationFailed",
]


class Severity(StrEnum):
    """How serious an :class:`Issue` is."""

    ERROR = "error"
    """Blocks the operation (or makes a schedule incomplete)."""
    WARNING = "warning"
    """Operation proceeds; the result may be incomplete (e.g. a missing rate)."""
    INFO = "info"
    """Informational note (e.g. a default applied on import)."""


class ObjectType(StrEnum):
    """Values used in ``Issue.object_type`` / ``NotFound.object_type`` for model objects."""

    PROJECT = "project"
    CALENDAR = "calendar"
    NODE = "node"
    RESOURCE = "resource"
    ASSIGNMENT = "assignment"
    DEPENDENCY = "dependency"


@dataclass(frozen=True, slots=True)
class Issue:
    """One structured problem report.

    Attributes:
        severity: :class:`Severity` of the issue.
        code: Stable machine-readable code, e.g. ``"CAL_HOURS_PER_DAY_RANGE"``.
        message: Human-readable explanation naming the offending value.
        object_type: Kind of object concerned (an :class:`ObjectType` value, or a CSV
            record type), or ``None`` when not tied to an object.
        object_id: ID of the object concerned, or ``None``. For assignments, which
            have no ID of their own, it is ``"<task_id>/<resource_id>"``.
        field: Attribute / argument / CSV column concerned, or ``None``.
        line: 1-based physical line number in an imported file, else ``None``.
    """

    severity: Severity
    code: str
    message: str
    object_type: str | None = None
    object_id: str | None = None
    field: str | None = None
    line: int | None = None

    @classmethod
    def error(
        cls,
        code: str,
        message: str,
        *,
        object_type: str | None = None,
        object_id: str | None = None,
        field: str | None = None,
        line: int | None = None,
    ) -> Issue:
        """Shorthand for an issue with :attr:`Severity.ERROR`."""
        return cls(Severity.ERROR, code, message, object_type, object_id, field, line)

    @classmethod
    def warning(
        cls,
        code: str,
        message: str,
        *,
        object_type: str | None = None,
        object_id: str | None = None,
        field: str | None = None,
        line: int | None = None,
    ) -> Issue:
        """Shorthand for an issue with :attr:`Severity.WARNING`."""
        return cls(Severity.WARNING, code, message, object_type, object_id, field, line)

    @classmethod
    def info(
        cls,
        code: str,
        message: str,
        *,
        object_type: str | None = None,
        object_id: str | None = None,
        field: str | None = None,
        line: int | None = None,
    ) -> Issue:
        """Shorthand for an issue with :attr:`Severity.INFO`."""
        return cls(Severity.INFO, code, message, object_type, object_id, field, line)

    def __str__(self) -> str:
        """Render as ``"error CODE [node t1, field sizing] (line 5): message"``."""
        where: list[str] = []
        if self.object_type is not None:
            obj = self.object_type
            where.append(obj if self.object_id is None else f"{obj} {self.object_id}")
        elif self.object_id is not None:
            where.append(self.object_id)
        if self.field is not None:
            where.append(f"field {self.field}")
        text = f"{self.severity.value} {self.code}"
        if where:
            text += f" [{', '.join(where)}]"
        if self.line is not None:
            text += f" (line {self.line})"
        return f"{text}: {self.message}"


class PlannerError(Exception):
    """Base class of every exception this package raises on purpose."""


class _IssuesError(PlannerError):
    """Common implementation of exceptions that carry a tuple of issues."""

    _headline = "failed"

    def __init__(self, issues: Iterable[Issue]) -> None:
        self.issues: tuple[Issue, ...] = tuple(issues)
        super().__init__(self.issues)

    def __str__(self) -> str:
        n = len(self.issues)
        lines = [f"{self._headline}: {n} issue{'s' if n != 1 else ''}"]
        lines.extend(f"  - {issue}" for issue in self.issues)
        return "\n".join(lines)


class ValidationFailed(_IssuesError):
    """Input or project definition is invalid; nothing was applied.

    Attributes:
        issues: Every problem found (all are reported together, never just the first).
    """

    _headline = "validation failed"


class ImportFailed(_IssuesError):
    """A CSV file could not be imported; nothing was replaced.

    Attributes:
        issues: Every error found, each with ``line`` set and ``field`` = column name.
    """

    _headline = "import failed"


class NotFound(PlannerError, LookupError):
    """A referenced object does not exist.

    Attributes:
        object_type: Kind of object looked up (an :class:`ObjectType` value or other text).
        object_id: The ID that was not found.
    """

    def __init__(self, object_type: str, object_id: str) -> None:
        self.object_type = object_type
        self.object_id = object_id
        super().__init__(object_type, object_id)

    def __str__(self) -> str:
        return f"{self.object_type} {self.object_id!r} not found"


class Conflict(PlannerError):
    """The operation conflicts with the current state (e.g. a name already in use)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class Cancelled(PlannerError):
    """A long-running operation was cancelled before it produced a result."""

    def __init__(self, message: str = "operation cancelled") -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class UnsavedChanges(PlannerError):
    """A replacing operation (new/load/import) was refused because the project is dirty.

    Retry with ``discard_unsaved=True`` to drop the unsaved changes.
    """

    def __init__(
        self, message: str = "the current project has unsaved changes (use discard_unsaved=True)"
    ) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message
