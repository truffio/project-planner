"""Pure graph utilities over a :class:`~project_planner.engine.model.Project`.

* :class:`DirectedGraph` - generic directed graph with an *iterative* Tarjan
  strongly-connected-components algorithm (no recursion, so 50k-node chains work).
* :class:`DependencyGraph` - the dependency graph of a project (nodes are node IDs,
  one edge per dependency, ``pred_id -> succ_id``), with deterministic
  :meth:`DependencyGraph.topological_order`.

Group endpoints are rejected by validation (decision 5). :func:`expand_group_endpoint`
is the documented hook where a later version would expand a group endpoint into its
leaf descendants; it is not implemented.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable

from project_planner.engine.errors import Issue, ObjectType, ValidationFailed
from project_planner.engine.model import NodeKind, Project

__all__ = [
    "DependencyGraph",
    "DirectedGraph",
    "cycle_issue",
    "expand_group_endpoint",
    "topological_order",
]


class DirectedGraph:
    """Directed graph over string node IDs. Parallel edges are collapsed."""

    def __init__(self, nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> None:
        adj: dict[str, set[str]] = {n: set() for n in nodes}
        for a, b in edges:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set())
        self._adj: dict[str, tuple[str, ...]] = {n: tuple(sorted(s)) for n, s in adj.items()}

    @property
    def nodes(self) -> tuple[str, ...]:
        """All node IDs, sorted."""
        return tuple(sorted(self._adj))

    def successors(self, node: str) -> tuple[str, ...]:
        """Sorted successors of a node (``()`` for unknown nodes)."""
        return self._adj.get(node, ())

    def strongly_connected_components(self) -> list[tuple[str, ...]]:
        """All SCCs (iterative Tarjan), each a sorted tuple; list sorted by first member."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        result: list[tuple[str, ...]] = []
        counter = 0
        for root in sorted(self._adj):
            if root in index:
                continue
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            work: list[tuple[str, int]] = [(root, 0)]
            while work:
                node, i = work.pop()
                succs = self._adj[node]
                descended = False
                while i < len(succs):
                    nxt = succs[i]
                    i += 1
                    if nxt not in index:
                        work.append((node, i))
                        index[nxt] = low[nxt] = counter
                        counter += 1
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, 0))
                        descended = True
                        break
                    if nxt in on_stack:
                        low[node] = min(low[node], index[nxt])
                if descended:
                    continue
                if low[node] == index[node]:
                    comp: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        comp.append(member)
                        if member == node:
                            break
                    result.append(tuple(sorted(comp)))
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
        result.sort()
        return result

    def cycles(self) -> list[tuple[str, ...]]:
        """SCCs with more than one node, plus single nodes with a self-loop (sorted tuples)."""
        return [
            c for c in self.strongly_connected_components() if len(c) > 1 or c[0] in self._adj[c[0]]
        ]


class DependencyGraph(DirectedGraph):
    """Dependency graph of a project: ``pred_id -> succ_id`` for every dependency.

    Nodes are all node IDs of the project plus any dangling dependency endpoints.
    """

    def __init__(self, project: Project) -> None:
        self.project = project
        super().__init__(
            (n.id for n in project.nodes),
            ((d.pred_id, d.succ_id) for d in project.dependencies),
        )

    def topological_order(self) -> list[str]:
        """IDs of all tasks and milestones, predecessors first.

        Deterministic tie-break: among ready nodes, the one earliest in WBS display
        order (``project.wbs_order()``) comes first, then by ID. Groups are not part
        of the order; edges touching non-schedulable nodes (groups, dangling IDs)
        are ignored.

        Raises:
            ValidationFailed: code ``DEP_CYCLE`` (one issue per cycle) if the
                dependency graph contains a cycle.
        """
        cyc = self.cycles()
        if cyc:
            raise ValidationFailed([cycle_issue(c) for c in cyc])
        project = self.project
        schedulable = {n.id for n in project.nodes if n.kind in (NodeKind.TASK, NodeKind.MILESTONE)}
        rank = {n.id: i for i, n in enumerate(project.wbs_order())}
        unranked = len(rank)

        def key(node_id: str) -> tuple[int, str]:
            return (rank.get(node_id, unranked), node_id)

        indegree = dict.fromkeys(schedulable, 0)
        for n in schedulable:
            for s in self._adj[n]:
                if s in schedulable:
                    indegree[s] += 1
        ready = [key(n) for n, d in indegree.items() if d == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            node = heapq.heappop(ready)[1]
            order.append(node)
            for s in self._adj[node]:
                if s in schedulable:
                    indegree[s] -= 1
                    if indegree[s] == 0:
                        heapq.heappush(ready, key(s))
        return order


def cycle_issue(members: tuple[str, ...]) -> Issue:
    """The ``DEP_CYCLE`` error for one cycle (``members`` sorted; names all of them)."""
    if len(members) == 1:
        text = f"node {members[0]} depends on itself"
    else:
        text = f"dependency cycle among {len(members)} nodes: {', '.join(members)}"
    return Issue.error(
        "DEP_CYCLE",
        text,
        object_type=ObjectType.NODE,
        object_id=members[0],
        field="dependencies",
    )


def topological_order(project: Project) -> list[str]:
    """Shorthand for ``DependencyGraph(project).topological_order()``."""
    return DependencyGraph(project).topological_order()


def expand_group_endpoint(project: Project, group_id: str) -> tuple[str, ...]:
    """Hook for future group-endpoint expansion (decision 5 rejects group endpoints now).

    A later version would return the leaf task/milestone IDs a dependency on the
    group stands for. Not implemented.

    Raises:
        NotImplementedError: always.
    """
    raise NotImplementedError("dependencies on groups are not supported (decision 5)")
