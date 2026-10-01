"""Project Planner backend: scheduling engine, persistence, and Python API.

Typical use::

    import project_planner as pp

    ws = pp.open_workspace(":memory:")
    ws.new_project("Demo", start=date(2026, 10, 5))
    t = ws.add_task("Design", effort=pp.hours(40))
    result = ws.schedule()

See ``docs/api.md`` for the reference.
"""

from decimal import Decimal

from project_planner import notebook as _notebook  # registers _repr_html_ / to_records
from project_planner.engine import model as _model
from project_planner.engine.config import Config
from project_planner.engine.errors import (
    Cancelled,
    Conflict,
    ImportFailed,
    Issue,
    NotFound,
    ObjectType,
    PlannerError,
    Severity,
    UnsavedChanges,
    ValidationFailed,
)
from project_planner.engine.model import (
    Assignment,
    CalendarException,
    CalendarSettings,
    Dependency,
    DependencyType,
    ExceptionKind,
    Holiday,
    NodeKind,
    Project,
    Resource,
    SizingMode,
    TimeQty,
    TimeUnit,
    WbsNode,
    Weekday,
    WorkUnit,
)
from project_planner.engine.results import (
    AssignmentResult,
    DependencyResult,
    LevelingResult,
    NodeResult,
    ScheduleResult,
)
from project_planner.services.dto import DeletePreview, WorkspaceState
from project_planner.services.files import ImportSummary, ProjectInfo
from project_planner.services.jobs import InlineExecutor, Job
from project_planner.services.read_models import (
    CostReportView,
    GanttRow,
    Link,
    LoadingView,
    ProjectSummary,
    TaskDetails,
    WbsPage,
    WbsRow,
)
from project_planner.services.workspace import CalendarApi, Workspace, open_workspace

__version__ = "0.1.0"


def hours(x: int | str | Decimal | float) -> TimeQty:
    """A quantity in hours: ``pp.hours(40)``, ``pp.hours("1.5")``.

    Unlike :func:`project_planner.engine.model.hours`, a ``float`` literal such as ``0.5``
    is accepted (converted through its shortest decimal text, so ``0.5`` is exactly
    ``0.5``) so that the plan 1.2 example ``pp.days(-0.5)`` works. ``bool`` is rejected.
    """
    return _model.hours(_exact(x))


def days(x: int | str | Decimal | float) -> TimeQty:
    """A quantity in days: ``pp.days(2)``, ``pp.days(-0.5)``, ``pp.days("-0.5")``.

    Floats are accepted; see :func:`hours`.
    """
    return _model.days(_exact(x))


def _exact(x: int | str | Decimal | float) -> int | str | Decimal:
    if isinstance(x, float):
        return Decimal(repr(x))
    return x


__all__ = [
    "Assignment",
    "AssignmentResult",
    "CalendarApi",
    "CalendarException",
    "CalendarSettings",
    "Cancelled",
    "Config",
    "Conflict",
    "CostReportView",
    "DeletePreview",
    "Dependency",
    "DependencyResult",
    "DependencyType",
    "ExceptionKind",
    "GanttRow",
    "Holiday",
    "ImportFailed",
    "ImportSummary",
    "InlineExecutor",
    "Issue",
    "Job",
    "LevelingResult",
    "Link",
    "LoadingView",
    "NodeKind",
    "NodeResult",
    "NotFound",
    "ObjectType",
    "PlannerError",
    "Project",
    "ProjectInfo",
    "ProjectSummary",
    "Resource",
    "ScheduleResult",
    "Severity",
    "SizingMode",
    "TaskDetails",
    "TimeQty",
    "TimeUnit",
    "UnsavedChanges",
    "ValidationFailed",
    "WbsNode",
    "WbsPage",
    "WbsRow",
    "Weekday",
    "WorkUnit",
    "Workspace",
    "WorkspaceState",
    "__version__",
    "days",
    "hours",
    "open_workspace",
]

del _notebook
