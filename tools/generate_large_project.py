"""Deterministic generator of large, realistic projects for benchmarks.

Usage::

    python tools/generate_large_project.py 10000 out.csv [--seed N]

or from Python: ``generate(n_tasks, seed=0) -> engine.model.Project``.

Structure: phases -> work packages -> leaf nodes (depth 3), about 10 leaves per
work package and 10 work packages per phase. About 5% of the leaves are
milestones. Tasks are sized by duration or effort in hours or days; about one
resource exists per 25 tasks, each task has 1-3 assignments at 25-100%.
Dependencies point only forward in document order (so the graph is acyclic):
mostly FS links inside a work package plus cross-package links of all four types
with signed lags, about 1.3 predecessors per leaf on average.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from decimal import Decimal
from pathlib import Path

from project_planner.engine.model import (
    Assignment,
    Calendar,
    Dependency,
    DependencyType,
    Holiday,
    NodeKind,
    Project,
    Resource,
    SizingMode,
    TimeQty,
    TimeUnit,
    WbsNode,
)

__all__ = ["generate"]

START = dt.date(2026, 10, 5)
_LEAVES_PER_WP = 10
_WPS_PER_PHASE = 10
_DEP_TYPES = (DependencyType.FS, DependencyType.SS, DependencyType.FF, DependencyType.SF)


def generate(n_tasks: int, seed: int = 0) -> Project:
    """Build a project with ``n_tasks`` leaf nodes (tasks plus milestones)."""
    if n_tasks < 1:
        raise ValueError("n_tasks must be >= 1")
    rng = random.Random(seed)
    nodes: list[WbsNode] = []
    leaves: list[tuple[str, str]] = []  # (node id, work package id), document order
    n_wps = -(-n_tasks // _LEAVES_PER_WP)
    n_phases = -(-n_wps // _WPS_PER_PHASE)
    for ph in range(n_phases):
        nodes.append(WbsNode(f"g{ph + 1}", f"Phase {ph + 1}", NodeKind.GROUP, None, ph))
    leaf_no = 0
    for wp in range(n_wps):
        wp_id = f"w{wp + 1}"
        phase_id = f"g{wp // _WPS_PER_PHASE + 1}"
        nodes.append(
            WbsNode(wp_id, f"Work package {wp + 1}", NodeKind.GROUP, phase_id, wp % _WPS_PER_PHASE)
        )
        for k in range(min(_LEAVES_PER_WP, n_tasks - leaf_no)):
            leaf_no += 1
            if rng.random() < 0.05:
                nid = f"m{leaf_no}"
                nodes.append(WbsNode(nid, f"Milestone {leaf_no}", NodeKind.MILESTONE, wp_id, k))
            else:
                nid = f"t{leaf_no}"
                unit = TimeUnit.HOURS if rng.random() < 0.5 else TimeUnit.DAYS
                value = Decimal(
                    rng.randint(4, 80) if unit is TimeUnit.HOURS else rng.randint(1, 10)
                )
                mode = SizingMode.DURATION if rng.random() < 0.4 else SizingMode.EFFORT
                nodes.append(
                    WbsNode(
                        nid, f"Task {leaf_no}", NodeKind.TASK, wp_id, k, mode, TimeQty(value, unit)
                    )
                )
            leaves.append((nid, wp_id))

    n_res = max(2, n_tasks // 25)
    resources = [
        Resource(f"r{i + 1}", f"Resource {i + 1}", Decimal(rng.randint(40, 150)))
        for i in range(n_res)
    ]
    assignments: list[Assignment] = []
    for node in nodes:
        if node.kind is not NodeKind.TASK:
            continue
        picked = rng.sample(range(n_res), min(n_res, rng.randint(1, 3)))
        for r in picked:
            percent = Decimal(rng.choice((25, 50, 75, 100)))
            assignments.append(Assignment(node.id, resources[r].id, percent))

    deps: list[Dependency] = []
    seen: set[tuple[str, str]] = set()

    def link(pred: str, succ: str, typ: DependencyType, lag: TimeQty) -> None:
        if (pred, succ) in seen:
            return
        seen.add((pred, succ))
        deps.append(Dependency(f"d{len(deps) + 1}", pred, succ, typ, lag))

    wp_start: dict[str, int] = {}
    for idx, (_, wp_id) in enumerate(leaves):
        wp_start.setdefault(wp_id, idx)
    zero = TimeQty(Decimal(0), TimeUnit.DAYS)
    for idx, (nid, wp_id) in enumerate(leaves):
        first = wp_start[wp_id]
        if idx > first and rng.random() < 0.8:  # within the work package, mostly FS
            back = 1 if rng.random() < 0.7 else rng.randint(1, idx - first)
            link(leaves[idx - back][0], nid, DependencyType.FS, zero)
        if first > 0 and rng.random() < 0.6:  # cross-package, from an earlier package
            pidx = rng.randint(max(0, first - 3 * _LEAVES_PER_WP), first - 1)
            typ = _DEP_TYPES[rng.choice((0, 0, 1, 2, 3))]
            if rng.random() < 0.5:
                lag = TimeQty(Decimal(rng.randint(-8, 16)), TimeUnit.HOURS)
            else:
                lag = TimeQty(Decimal(rng.randint(-1, 3)), TimeUnit.DAYS)
            link(leaves[pidx][0], nid, typ, lag)

    holidays = tuple(
        Holiday(dt.date(2026, 12, 25) + dt.timedelta(days=365 * i), "Holiday") for i in range(4)
    )
    holidays += (
        Holiday(dt.date(2027, 1, 1), "New Year"),
        Holiday(dt.date(2026, 11, 26), "Thanksgiving"),
    )
    return Project(
        id="p1",
        name=f"Large project {n_tasks}",
        start=START,
        calendar=Calendar(holidays=holidays),
        nodes=tuple(nodes),
        resources=tuple(resources),
        assignments=tuple(assignments),
        dependencies=tuple(deps),
    )


def main(argv: list[str] | None = None) -> int:
    from project_planner.engine import csv_io

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("n_tasks", type=int)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    project = generate(args.n_tasks, args.seed)
    args.output.write_text(csv_io.export(project), encoding="utf-8", newline="")
    print(
        f"wrote {args.output}: {args.n_tasks} leaves, {len(project.nodes)} nodes, "
        f"{len(project.resources)} resources, {len(project.dependencies)} dependencies",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
