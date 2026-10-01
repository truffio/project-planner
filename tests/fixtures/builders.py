"""Fluent builder of :class:`~project_planner.engine.model.Project` values for tests.

Example::

    from datetime import date
    from fixtures.builders import ProjectBuilder   # ``tests/`` is the pytest rootdir on sys.path

    p = (
        ProjectBuilder(start=date(2026, 10, 5))
        .calendar(hours_per_day=8, holidays=[date(2026, 10, 8)])
        .resource("alice", rate="100").resource("bob", rate="50")
        .group("g1", "Phase 1")
        .task("t1", effort="40h", parent="g1").assign("t1", "alice", 80).assign("t1", "bob", 20)
        .task("t2", duration="2d", parent="g1").milestone("m1")
        .dep("t1", "t2", "FS", lag="0d")
        .build()
    )

Rules:

* Every method returns the builder, so calls chain; :meth:`ProjectBuilder.build`
  returns the immutable ``Project``. A builder can be built several times.
* **IDs** are optional: omitted IDs are generated per kind (``g1``, ``g2``... for
  groups, ``t1``... tasks, ``m1``... milestones, ``r1``... resources, ``d1``...
  dependencies), skipping IDs already used. The ID just created is available as
  :attr:`ProjectBuilder.last_id`. Avoid mixing auto IDs with explicit IDs of the
  same pattern.
* **Names** default to the ID.
* **Sibling order** defaults to the number of nodes already added under the same
  parent (0, 1, 2...), i.e. insertion order. Pass ``order=`` to override.
* **Sizing**: ``task(duration=...)`` or ``task(effort=...)`` takes a ``TimeQty`` or a
  string (``"40h"``, ``"2d"``, ``"-0.5d"``). Bare numbers raise ``TypeError``, as in
  the public API. Neither gives an unsized task.
* **Numbers** (``rate``, ``percent``) take ``int``, ``str`` or ``Decimal``; never ``float``.
* **Calendar**: :meth:`ProjectBuilder.calendar` takes the arguments of
  ``CalendarSettings.from_values`` (holidays as dates or ``(date, name)``;
  exceptions as ``(date, "working"|"nonworking"[, name])``; weekdays as
  ``"Mon-Fri"``...). Repeated calls update only the arguments given.
* **No cross-reference checks**: parents, assignment and dependency endpoints may
  name nodes that do not exist or are added later, and self-dependencies or
  cycles can be built. This is deliberate, so validation tests can build invalid
  projects. Only the model's local invariants are enforced (in ``build()``, or
  in the method that creates the offending object).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Self

from project_planner.engine.model import (
    Assignment,
    CalendarSettings,
    Dependency,
    DependencyType,
    NodeKind,
    Project,
    Resource,
    SizingMode,
    TimeQty,
    WbsNode,
    WorkUnit,
    as_time_qty,
)

__all__ = ["DEFAULT_START", "ProjectBuilder", "dec"]

DEFAULT_START = dt.date(2026, 10, 5)
"""Monday 5 October 2026, the project start used throughout the test suites."""


def dec(x: int | str | Decimal) -> Decimal:
    """Exact ``Decimal`` from an ``int``, ``str`` or ``Decimal``; ``float`` raises ``TypeError``."""
    if isinstance(x, bool | float):
        raise TypeError(f"use int, str or Decimal, not {type(x).__name__} {x!r}")
    return x if isinstance(x, Decimal) else Decimal(x)


@dataclass
class ProjectBuilder:
    """Accumulates project parts and builds a :class:`Project` (see module docstring).

    Args:
        start: Project start date (default :data:`DEFAULT_START`, a Monday).
        name: Project name.
        id: Project ID.
        currency: Currency code.
        cost_report_unit: Default cost-report unit (``WorkUnit`` or its value).
    """

    start: dt.date = DEFAULT_START
    name: str = "Test project"
    id: str = "p1"
    currency: str = "USD"
    cost_report_unit: WorkUnit | str = WorkUnit.PERSON_DAYS
    _calendar_args: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _nodes: list[WbsNode] = field(default_factory=list, init=False, repr=False)
    _resources: list[Resource] = field(default_factory=list, init=False, repr=False)
    _assignments: list[Assignment] = field(default_factory=list, init=False, repr=False)
    _dependencies: list[Dependency] = field(default_factory=list, init=False, repr=False)
    _used: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)
    _counters: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _sibling_count: dict[str | None, int] = field(default_factory=dict, init=False, repr=False)
    _last_id: str | None = field(default=None, init=False, repr=False)

    # --- helpers ------------------------------------------------------------------

    @property
    def last_id(self) -> str:
        """ID of the most recently added node, resource or dependency."""
        if self._last_id is None:
            raise LookupError("nothing has been added yet")
        return self._last_id

    def _new_id(self, given: str | None, prefix: str, namespace: str) -> str:
        used = self._used.setdefault(namespace, set())
        if given is None:
            n = self._counters.get(prefix, 0)
            while True:
                n += 1
                given = f"{prefix}{n}"
                if given not in used:
                    break
            self._counters[prefix] = n
        used.add(given)
        self._last_id = given
        return given

    def _add_node(
        self,
        kind: NodeKind,
        prefix: str,
        node_id: str | None,
        name: str | None,
        parent: str | None,
        order: int | None,
        sizing_mode: SizingMode = SizingMode.NONE,
        sizing: TimeQty | None = None,
    ) -> Self:
        nid = self._new_id(node_id, prefix, "node")
        siblings = self._sibling_count.get(parent, 0)
        self._sibling_count[parent] = siblings + 1
        self._nodes.append(
            WbsNode(
                id=nid,
                name=nid if name is None else name,
                kind=kind,
                parent_id=parent,
                order=siblings if order is None else order,
                sizing_mode=sizing_mode,
                sizing=sizing,
            )
        )
        return self

    # --- project parts --------------------------------------------------------------

    def calendar(self, **settings: Any) -> Self:
        """Set calendar arguments (those of ``CalendarSettings.from_values``)."""
        self._calendar_args.update(settings)
        return self

    def resource(
        self,
        id: str | None = None,
        name: str | None = None,
        *,
        rate: int | str | Decimal | None = None,
    ) -> Self:
        """Add a resource; ``rate=None`` means a missing hourly rate."""
        rid = self._new_id(id, "r", "resource")
        hourly = None if rate is None else dec(rate)
        self._resources.append(Resource(rid, rid if name is None else name, hourly))
        return self

    def group(
        self,
        id: str | None = None,
        name: str | None = None,
        *,
        parent: str | None = None,
        order: int | None = None,
    ) -> Self:
        """Add a summary group."""
        return self._add_node(NodeKind.GROUP, "g", id, name, parent, order)

    def task(
        self,
        id: str | None = None,
        name: str | None = None,
        *,
        parent: str | None = None,
        duration: TimeQty | str | None = None,
        effort: TimeQty | str | None = None,
        order: int | None = None,
    ) -> Self:
        """Add a task sized by ``duration`` or ``effort`` (not both), or unsized."""
        if duration is not None and effort is not None:
            raise ValueError("give duration or effort, not both")
        mode, sizing = SizingMode.NONE, None
        if duration is not None:
            mode, sizing = SizingMode.DURATION, as_time_qty(duration)
        elif effort is not None:
            mode, sizing = SizingMode.EFFORT, as_time_qty(effort)
        return self._add_node(NodeKind.TASK, "t", id, name, parent, order, mode, sizing)

    def milestone(
        self,
        id: str | None = None,
        name: str | None = None,
        *,
        parent: str | None = None,
        order: int | None = None,
    ) -> Self:
        """Add a milestone."""
        return self._add_node(NodeKind.MILESTONE, "m", id, name, parent, order)

    def node(self, node: WbsNode) -> Self:
        """Add a ready-made node unchanged (escape hatch; no auto order)."""
        self._used.setdefault("node", set()).add(node.id)
        self._last_id = node.id
        self._nodes.append(node)
        return self

    def assign(self, task: str, resource: str, percent: int | str | Decimal = 100) -> Self:
        """Assign ``resource`` to ``task`` at ``percent`` (80 = 80 %)."""
        self._assignments.append(Assignment(task, resource, dec(percent)))
        return self

    def dep(
        self,
        pred: str,
        succ: str,
        type: DependencyType | str = DependencyType.FS,
        *,
        lag: TimeQty | str = "0d",
        id: str | None = None,
    ) -> Self:
        """Add a dependency ``pred -> succ`` of ``type`` with ``lag`` (default ``"0d"``)."""
        did = self._new_id(id, "d", "dependency")
        dep_type = type if isinstance(type, DependencyType) else DependencyType(type)
        self._dependencies.append(Dependency(did, pred, succ, dep_type, as_time_qty(lag)))
        return self

    # --- result ---------------------------------------------------------------------

    def calendar_settings(self) -> CalendarSettings:
        """The validated calendar settings accumulated so far."""
        return CalendarSettings.from_values(**self._calendar_args)

    def build(self) -> Project:
        """Build the immutable :class:`Project` (raises ``ValidationFailed`` on duplicates)."""
        return Project(
            id=self.id,
            name=self.name,
            start=self.start,
            currency=self.currency,
            cost_report_unit=WorkUnit(self.cost_report_unit),
            calendar=self.calendar_settings().to_calendar(),
            nodes=tuple(self._nodes),
            resources=tuple(self._resources),
            assignments=tuple(self._assignments),
            dependencies=tuple(self._dependencies),
        )
