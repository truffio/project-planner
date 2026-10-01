"""User-requested resource leveling (T18, spec section 10, decision 1).

Serial placement of whole tasks. Tasks are processed so that every predecessor is
placed before its successors; among the ready tasks the one with the smallest
``(dependency-only start, WBS display rank, id)`` goes first. Each task gets the
dependency-earliest start computed from its already-placed predecessors (the
``forward_pass`` formula, never earlier than its dependency-only start) and is
then placed at the earliest start ``s`` at or after that bound such that, for
every resource it uses, placed load + its own percent stays within capacity
(100 %) throughout ``[s, s + D)``. Tasks are never split and durations,
assignments and percents never change; calendar pauses are ordinary because
times are working-axis minutes.

Load structure: per resource a piecewise-constant step function stored as two
parallel lists, sorted breakpoints ``times`` and ``loads`` (``loads[i]`` holds on
``[times[i], times[i + 1])``, the last step is always 0 and extends to infinity).
Adjacent steps with equal load are merged, so a resource fully booked at 100 %
by back-to-back tasks is a single step. Locating a time is a ``bisect``
(O(log k) for k steps); the feasibility search walks forward over the steps
covered by the candidate window and jumps the candidate to the end of the first
step that is too full, so the candidates are exactly the bound and placed-step
end points and every step is visited at most once per resource per round.
Inserting a task splits at most two steps and adds its percent to the covered
steps (O(k) list moves in the worst case, executed in C).

Unresolvable allocations: a task whose own percent for some resource exceeds
capacity (only possible when ``Config.max_assignment_percent`` > 100) cannot be
fixed by shifting. It is placed at its dependency-earliest start without a
resource search, and its load is still added to the structure, so later tasks
treat that resource as full there and are never placed on top of it. Every
step above 100 % in the final load (which can only involve such forced tasks)
is reported as an :class:`UnresolvedOverload` with an explanation.

No optimality guarantee is implied. The result is deterministic.
"""

from __future__ import annotations

import heapq
from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from project_planner.engine.config import DEFAULT_CONFIG, Config, engine_context
from project_planner.engine.errors import Cancelled
from project_planner.engine.forward_pass import NodeTiming
from project_planner.engine.loading import compute_loading
from project_planner.engine.model import DependencyType, Project
from project_planner.engine.sizing import TaskSizing

__all__ = ["LevelingDelay", "LevelingOutcome", "UnresolvedOverload", "level"]

_CAPACITY = Decimal(100)
_ZERO = Decimal(0)
OWN_ALLOCATION_REASON = "own allocation exceeds capacity"


@dataclass(frozen=True, slots=True)
class LevelingDelay:
    """A task started later than in the dependency-only schedule.

    ``cause`` is ``"resource"`` when the task itself was shifted past its
    dependency-earliest start by a resource conflict (even if a predecessor also
    moved it), and ``"dependency"`` when it only moved because a delayed
    predecessor pushed its dependency-earliest start.
    """

    task_id: str
    minutes: int
    cause: Literal["resource", "dependency"]


@dataclass(frozen=True, slots=True)
class UnresolvedOverload:
    """A span ``[start, end)`` where a resource stays above capacity after leveling."""

    resource_id: str
    task_ids: tuple[str, ...]
    start: int
    end: int
    percent: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class LevelingOutcome:
    """Engine-level leveling result (plain, picklable data)."""

    timings: dict[str, NodeTiming]
    delays: tuple[LevelingDelay, ...]
    unresolved: tuple[UnresolvedOverload, ...]
    base_finish: int | None
    leveled_finish: int | None
    finish_delta_minutes: int | None


class _Load:
    """Piecewise-constant load of one resource (see module docstring)."""

    __slots__ = ("loads", "times")

    def __init__(self) -> None:
        self.times: list[int] = [0]
        self.loads: list[Decimal] = [_ZERO]

    def earliest_fit(self, s: int, duration: int, cap: Decimal) -> int:
        """Earliest ``t >= s`` with load <= ``cap`` on all of ``[t, t + duration)``.

        Requires ``cap >= 0`` (the final step is 0, so the search terminates).
        """
        times, loads = self.times, self.loads
        n = len(times)
        i = bisect_right(times, s) - 1
        end = s + duration
        j = i
        while j < n and times[j] < end:
            if loads[j] > cap:
                s = times[j + 1]  # j < n - 1: the last step is 0 <= cap
                end = s + duration
            j += 1
        return s

    def add(self, s: int, e: int, pct: Decimal) -> None:
        """Add ``pct`` on ``[s, e)`` (``e > s >= 0``)."""
        times, loads = self.times, self.loads
        i = self._split(s)
        k = self._split(e)
        for x in range(i, k):
            loads[x] += pct
        # merge equal neighbours at the two boundaries (interior steps stay distinct)
        if k < len(times) and loads[k] == loads[k - 1]:
            del times[k]
            del loads[k]
        if i > 0 and loads[i] == loads[i - 1]:
            del times[i]
            del loads[i]

    def _split(self, t: int) -> int:
        times = self.times
        i = bisect_right(times, t) - 1
        if times[i] == t:
            return i
        times.insert(i + 1, t)
        self.loads.insert(i + 1, self.loads[i])
        return i + 1


@engine_context
def level(
    project: Project,
    sizing: Mapping[str, TaskSizing],
    base: Mapping[str, NodeTiming],
    *,
    minutes_per_day: int,
    config: Config = DEFAULT_CONFIG,
    progress: Callable[[float, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> LevelingOutcome:
    """Level ``base`` (the dependency-only forward pass of a valid ``project``).

    Args:
        project: The project (validated; the same one ``base`` was computed from).
        sizing: ``compute_sizing(project)``.
        base: ``forward_pass`` timings of every task and milestone.
        minutes_per_day: Working minutes per day (lag conversion).
        config: Engine configuration (accepted for symmetry; capacity is 100 %).
        progress: Called with ``(fraction, message)``; fractions never decrease
            and the last call is ``1.0``.
        cancel: Polled before each task is placed; ``True`` raises ``Cancelled``.

    Unschedulable and blocked nodes are copied from ``base`` unchanged and take no
    part in leveling.
    """
    del config  # capacity is fixed at 100 %; over-100 allocations come from config limits
    scheduled = {
        nid: t
        for nid, t in base.items()
        if t.status == "scheduled" and t.start is not None and t.finish is not None
    }
    rank = {n.id: i for i, n in enumerate(project.wbs_order())}
    no_rank = len(rank)

    def duration_of(nid: str) -> int:
        sz = sizing.get(nid)
        if sz is not None and sz.duration_minutes is not None:
            return sz.duration_minutes
        t = scheduled[nid]
        assert t.start is not None and t.finish is not None
        return t.finish - t.start

    def key(nid: str) -> tuple[int, int, str]:
        start = scheduled[nid].start
        assert start is not None
        return (start, rank.get(nid, no_rank), nid)

    # incoming constraints among scheduled nodes, and readiness counters
    incoming: dict[str, list[tuple[str, DependencyType, int]]] = {}
    successors: dict[str, list[str]] = {}
    pending: dict[str, int] = {}
    for nid in scheduled:
        ins: list[tuple[str, DependencyType, int]] = []
        preds: set[str] = set()
        for dep in project.dependencies_to(nid):
            if dep.pred_id in scheduled:
                ins.append((dep.pred_id, dep.type, dep.lag.to_minutes(minutes_per_day)))
                preds.add(dep.pred_id)
        incoming[nid] = ins
        pending[nid] = len(preds)
        for p in sorted(preds):
            successors.setdefault(p, []).append(nid)

    heap = [key(nid) for nid, c in pending.items() if c == 0]
    heapq.heapify(heap)

    loads: dict[str, _Load] = {}
    placed: dict[str, tuple[int, int]] = {}
    forced: dict[str, list[tuple[str, Decimal]]] = {}  # task -> [(resource, own %)] > 100
    causes: dict[str, Literal["resource", "dependency"]] = {}
    total = len(scheduled)
    step = max(1, total // 100)
    if progress is not None:
        progress(0.0, f"Leveling {total} tasks")

    done = 0
    while heap:
        if cancel is not None and cancel():
            raise Cancelled("leveling cancelled")
        _, _, nid = heapq.heappop(heap)
        d = duration_of(nid)
        base_start = scheduled[nid].start
        assert base_start is not None
        bound = max(0, base_start)
        for pred, typ, lag in incoming[nid]:
            ps, pf = placed[pred]
            if typ is DependencyType.FS:
                c = pf + lag
            elif typ is DependencyType.SS:
                c = ps + lag
            elif typ is DependencyType.FF:
                c = pf + lag - d
            else:
                c = ps + lag - d
            if c > bound:
                bound = c

        uses = (
            [(a.resource_id, a.percent) for a in project.assignments_for(nid) if a.percent > 0]
            if d > 0
            else []
        )
        over = [(r, p) for r, p in uses if p > _CAPACITY]
        s = bound
        if over:
            forced[nid] = over
        elif uses:
            while True:
                moved = False
                for rid, pct in uses:
                    ld = loads.get(rid)
                    if ld is None:
                        continue
                    fit = ld.earliest_fit(s, d, _CAPACITY - pct)
                    if fit != s:
                        s = fit
                        moved = True
                if not moved:
                    break
        for rid, pct in uses:
            ld = loads.get(rid)
            if ld is None:
                ld = loads[rid] = _Load()
            ld.add(s, s + d, pct)
        placed[nid] = (s, s + d)
        if s > bound:
            causes[nid] = "resource"
        elif s > base_start:
            causes[nid] = "dependency"

        for succ in successors.get(nid, ()):
            pending[succ] -= 1
            if pending[succ] == 0:
                heapq.heappush(heap, key(succ))
        done += 1
        if progress is not None and done % step == 0 and done < total:
            progress(done / total, f"Leveled {done} of {total} tasks")

    if len(placed) != total:  # only possible with a cyclic graph (validation rejects it)
        raise ValueError("leveling requires an acyclic dependency graph")

    timings: dict[str, NodeTiming] = {}
    for nid, timing in base.items():
        if nid in placed:
            s, f = placed[nid]
            timings[nid] = NodeTiming(nid, s, f, "scheduled", None)
        else:
            timings[nid] = timing

    delays = tuple(
        LevelingDelay(nid, placed[nid][0] - (scheduled[nid].start or 0), causes[nid])
        for nid in sorted(causes)
    )
    unresolved = _unresolved(project, placed, forced) if forced else ()

    base_finish = max((t.finish for t in scheduled.values() if t.finish is not None), default=None)
    leveled_finish = max((f for _, f in placed.values()), default=None)
    delta = (
        leveled_finish - base_finish
        if base_finish is not None and leveled_finish is not None
        else None
    )
    if progress is not None:
        progress(1.0, "Leveling complete")
    return LevelingOutcome(timings, delays, unresolved, base_finish, leveled_finish, delta)


def _unresolved(
    project: Project,
    placed: Mapping[str, tuple[int, int]],
    forced: Mapping[str, list[tuple[str, Decimal]]],
) -> tuple[UnresolvedOverload, ...]:
    """Every remaining overloaded span, explained by the forced (over-capacity) tasks."""
    own = {(tid, rid): pct for tid, items in forced.items() for rid, pct in items}
    loading = compute_loading(project, dict(placed))
    out: list[UnresolvedOverload] = []
    for rid in sorted(loading):
        for seg in loading[rid]:
            if not seg.overloaded:
                continue
            selfs = [t for t in seg.task_ids if (t, rid) in own]
            if selfs:
                details = ", ".join(f"task {t} is assigned {own[(t, rid)]}%" for t in selfs)
                reason = (
                    f"{OWN_ALLOCATION_REASON}: {details} of resource {rid} "
                    f"(capacity {_CAPACITY}%); delaying the task cannot resolve this"
                )
            else:
                culprits = sorted(t for t in seg.task_ids if t in forced)
                details = "; ".join(
                    f"task {t} ({', '.join(f'{r} at {p}%' for r, p in forced[t])})"
                    for t in culprits
                )
                reason = (
                    f"{OWN_ALLOCATION_REASON} on another resource: {details} was placed at "
                    f"its dependency-earliest start without resource checks"
                )
            out.append(
                UnresolvedOverload(rid, seg.task_ids, seg.start, seg.end, seg.percent, reason)
            )
    return tuple(out)
