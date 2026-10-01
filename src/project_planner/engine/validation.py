"""Cross-reference validation of a project definition (plan sections 2 and 3).

:func:`validate` collects *all* issues, sorted by ``(object_type, object_id, field,
code)``. The model checks local invariants; this module checks everything that
needs cross-references.

=============================  ========  ===========  ===============
code                           severity  object_type  field
=============================  ========  ===========  ===============
``DEP_DANGLING``               error     dependency   pred_id/succ_id
``DEP_SELF``                   error     dependency   succ_id
``DEP_GROUP_ENDPOINT``         error     dependency   pred_id/succ_id
``DEP_DUPLICATE``              error     dependency   type
``DEP_CYCLE``                  error     node         dependencies
``NODE_PARENT_DANGLING``       error     node         parent_id
``NODE_PARENT_NOT_GROUP``      error     node         parent_id
``NODE_PARENT_CYCLE``          error     node         parent_id
``ASSIGN_TASK_DANGLING``       error     assignment   task_id
``ASSIGN_NOT_TASK``            error     assignment   task_id
``ASSIGN_RESOURCE_DANGLING``   error     assignment   resource_id
``ASSIGN_PERCENT_RANGE``       error     assignment   percent
``COST_MISSING_RATE``          warning   resource     hourly_rate
``TASK_UNSIZED``               warning   node         sizing
``TASK_NO_CAPACITY``           warning   node         assignments
=============================  ========  ===========  ===============

Decisions: a duplicate dependency (same predecessor, successor and type) is an
*error*, reported on every dependency after the first (by ID). A self-dependency
is reported as ``DEP_SELF`` only, not additionally as a one-node ``DEP_CYCLE``.
"""

from __future__ import annotations

from project_planner.engine.config import DEFAULT_CONFIG, Config
from project_planner.engine.errors import Issue, ObjectType, Severity, ValidationFailed
from project_planner.engine.model import NodeKind, Project, SizingMode
from project_planner.engine.network import DependencyGraph, DirectedGraph, cycle_issue

__all__ = ["ensure_valid", "issue_sort_key", "missing_rate_issues", "schedule_blocking", "validate"]


def issue_sort_key(issue: Issue) -> tuple[str, str, str, str]:
    """The ordering of :func:`validate`'s output."""
    return (issue.object_type or "", issue.object_id or "", issue.field or "", issue.code)


def validate(project: Project, config: Config = DEFAULT_CONFIG) -> list[Issue]:
    """Every issue of the project, in deterministic order (never stops at the first)."""
    issues: list[Issue] = []
    _check_nodes(project, issues)
    _check_dependencies(project, issues)
    _check_assignments(project, config, issues)
    _check_costs_and_sizing(project, issues)
    issues.sort(key=issue_sort_key)
    return issues


def schedule_blocking(issues: list[Issue]) -> bool:
    """True if any issue is an error (warnings do not block scheduling)."""
    return any(i.severity is Severity.ERROR for i in issues)


def ensure_valid(project: Project, config: Config = DEFAULT_CONFIG) -> None:
    """Raise :class:`ValidationFailed` with the error-severity issues, if any."""
    errors = [i for i in validate(project, config) if i.severity is Severity.ERROR]
    if errors:
        raise ValidationFailed(errors)


def _check_nodes(project: Project, issues: list[Issue]) -> None:
    for node in project.nodes:
        pid = node.parent_id
        if pid is None:
            continue
        if not project.has_node(pid):
            issues.append(
                Issue.error(
                    "NODE_PARENT_DANGLING",
                    f"node {node.id} has parent {pid}, which does not exist",
                    object_type=ObjectType.NODE,
                    object_id=node.id,
                    field="parent_id",
                )
            )
        elif project.node(pid).kind is not NodeKind.GROUP:
            issues.append(
                Issue.error(
                    "NODE_PARENT_NOT_GROUP",
                    f"node {node.id} has parent {pid}, which is a {project.node(pid).kind.value}"
                    f", not a group",
                    object_type=ObjectType.NODE,
                    object_id=node.id,
                    field="parent_id",
                )
            )
    parent_graph = DirectedGraph(
        (n.id for n in project.nodes),
        ((n.id, n.parent_id) for n in project.nodes if n.parent_id is not None),
    )
    for members in parent_graph.cycles():
        text = (
            f"parent cycle among {len(members)} nodes: {', '.join(members)}"
            if len(members) > 1
            else f"node {members[0]} is its own parent"
        )
        issues.append(
            Issue.error(
                "NODE_PARENT_CYCLE",
                text,
                object_type=ObjectType.NODE,
                object_id=members[0],
                field="parent_id",
            )
        )


def _check_dependencies(project: Project, issues: list[Issue]) -> None:
    seen: dict[tuple[str, str, str], str] = {}
    for dep in project.dependencies:
        for field, endpoint in (("pred_id", dep.pred_id), ("succ_id", dep.succ_id)):
            if not project.has_node(endpoint):
                issues.append(
                    Issue.error(
                        "DEP_DANGLING",
                        f"dependency {dep.id}: {field} {endpoint} does not exist",
                        object_type=ObjectType.DEPENDENCY,
                        object_id=dep.id,
                        field=field,
                    )
                )
            elif project.node(endpoint).kind is NodeKind.GROUP:
                issues.append(
                    Issue.error(
                        "DEP_GROUP_ENDPOINT",
                        f"dependency {dep.id}: {field} {endpoint} is a group; "
                        f"depend on its tasks or milestones instead",
                        object_type=ObjectType.DEPENDENCY,
                        object_id=dep.id,
                        field=field,
                    )
                )
        if dep.pred_id == dep.succ_id:
            issues.append(
                Issue.error(
                    "DEP_SELF",
                    f"dependency {dep.id}: node {dep.pred_id} depends on itself",
                    object_type=ObjectType.DEPENDENCY,
                    object_id=dep.id,
                    field="succ_id",
                )
            )
        key = (dep.pred_id, dep.succ_id, dep.type.value)
        if key in seen:
            issues.append(
                Issue.error(
                    "DEP_DUPLICATE",
                    f"dependency {dep.id} duplicates {seen[key]} "
                    f"({dep.pred_id} -> {dep.succ_id}, {dep.type.value})",
                    object_type=ObjectType.DEPENDENCY,
                    object_id=dep.id,
                    field="type",
                )
            )
        else:
            seen[key] = dep.id
    for members in DependencyGraph(project).cycles():
        if len(members) > 1:  # self-loops are reported as DEP_SELF
            issues.append(cycle_issue(members))


def _check_assignments(project: Project, config: Config, issues: list[Issue]) -> None:
    for a in project.assignments:
        oid = a.key_text
        if not project.has_node(a.task_id):
            issues.append(
                Issue.error(
                    "ASSIGN_TASK_DANGLING",
                    f"assignment {a.key_text}: task {a.task_id} does not exist",
                    field="task_id",
                    object_type=ObjectType.ASSIGNMENT,
                    object_id=oid,
                )
            )
        elif project.node(a.task_id).kind is not NodeKind.TASK:
            issues.append(
                Issue.error(
                    "ASSIGN_NOT_TASK",
                    f"assignment {a.key_text}: {a.task_id} is a "
                    f"{project.node(a.task_id).kind.value}, not a task",
                    field="task_id",
                    object_type=ObjectType.ASSIGNMENT,
                    object_id=oid,
                )
            )
        if not project.has_resource(a.resource_id):
            issues.append(
                Issue.error(
                    "ASSIGN_RESOURCE_DANGLING",
                    f"assignment {a.key_text}: resource {a.resource_id} does not exist",
                    field="resource_id",
                    object_type=ObjectType.ASSIGNMENT,
                    object_id=oid,
                )
            )
        if a.percent > config.max_assignment_percent:
            issues.append(
                Issue.error(
                    "ASSIGN_PERCENT_RANGE",
                    f"assignment {a.key_text}: percent {a.percent} exceeds the maximum "
                    f"{config.max_assignment_percent}",
                    field="percent",
                    object_type=ObjectType.ASSIGNMENT,
                    object_id=oid,
                )
            )


def missing_rate_issues(project: Project) -> list[Issue]:
    """``COST_MISSING_RATE`` warnings: assigned resources without an hourly rate."""
    return [
        Issue.warning(
            "COST_MISSING_RATE",
            f"resource {r.id} has assignments but no hourly rate; costs will be incomplete",
            object_type=ObjectType.RESOURCE,
            object_id=r.id,
            field="hourly_rate",
        )
        for r in project.resources
        if r.hourly_rate is None and project.assignments_for_resource(r.id)
    ]


def _check_costs_and_sizing(project: Project, issues: list[Issue]) -> None:
    issues.extend(missing_rate_issues(project))
    for n in project.nodes:
        if n.kind is not NodeKind.TASK:
            continue
        if not n.is_sized:
            issues.append(
                Issue.warning(
                    "TASK_UNSIZED",
                    f"task {n.id} has no duration or effort and will be scheduled as incomplete",
                    object_type=ObjectType.NODE,
                    object_id=n.id,
                    field="sizing",
                )
            )
        elif (
            n.sizing_mode is SizingMode.EFFORT
            and n.sizing is not None
            and n.sizing.value != 0  # zero effort is schedulable (duration 0)
            and not any(a.percent > 0 for a in project.assignments_for(n.id))
        ):
            issues.append(
                Issue.warning(
                    "TASK_NO_CAPACITY",
                    f"effort task {n.id} has no assigned capacity; its duration cannot be computed",
                    object_type=ObjectType.NODE,
                    object_id=n.id,
                    field="assignments",
                )
            )
