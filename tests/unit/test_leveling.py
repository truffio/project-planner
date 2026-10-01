"""Unit tests for engine.leveling (T18)."""

from __future__ import annotations

import datetime as dt
import pickle
import random
import time
from decimal import Decimal as D

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.calendar import WorkingAxis
from project_planner.engine.config import Config
from project_planner.engine.cost import compute_costs
from project_planner.engine.errors import Cancelled
from project_planner.engine.forward_pass import NodeTiming, forward_pass
from project_planner.engine.leveling import (
    OWN_ALLOCATION_REASON,
    LevelingDelay,
    LevelingOutcome,
    level,
)
from project_planner.engine.loading import compute_loading
from project_planner.engine.model import Project
from project_planner.engine.network import topological_order
from project_planner.engine.sizing import compute_sizing

pytestmark = pytest.mark.unit

MPD = 480


def run(
    p: Project, config: Config | None = None, **kw: object
) -> tuple[dict[str, NodeTiming], LevelingOutcome]:
    sz = compute_sizing(p)
    base = forward_pass(p, sz, minutes_per_day=MPD, order=topological_order(p))
    extra = {} if config is None else {"config": config}
    out = level(p, sz, base, minutes_per_day=MPD, **extra, **kw)  # type: ignore[arg-type]
    return base, out


def iv(out: LevelingOutcome, nid: str) -> tuple[int | None, int | None]:
    t = out.timings[nid]
    return (t.start, t.finish)


def intervals(timings: dict[str, NodeTiming]) -> dict[str, tuple[int, int] | None]:
    return {
        k: (t.start, t.finish) if t.start is not None and t.finish is not None else None
        for k, t in timings.items()
    }


def max_load(p: Project, out: LevelingOutcome) -> D:
    loading = compute_loading(p, intervals(out.timings))
    return max((s.percent for segs in loading.values() for s in segs), default=D(0))


def a07_project() -> Project:
    return (
        ProjectBuilder()
        .resource("alice", rate="100")
        .task("t1", duration="3d")
        .task("t2", duration="3d")
        .task("t3", duration="1d")
        .assign("t1", "alice", 60)
        .assign("t2", "alice", 60)
        .dep("t2", "t3", "FS")
        .build()
    )


# ---------------------------------------------------------------- A07 / A08 / A19


def test_a07_one_whole_task_delayed() -> None:
    p = a07_project()
    base, out = run(p)
    assert iv(out, "t1") == (0, 1440)
    assert iv(out, "t2") == (1440, 2880)
    assert iv(out, "t3") == (2880, 3360)
    assert out.delays == (
        LevelingDelay("t2", 1440, "resource"),
        LevelingDelay("t3", 1440, "dependency"),
    )
    assert out.unresolved == ()
    assert (out.base_finish, out.leveled_finish, out.finish_delta_minutes) == (1920, 3360, 1440)
    for nid, t in out.timings.items():
        assert t.finish - t.start == base[nid].finish - base[nid].start  # type: ignore[operator]
    assert max_load(p, out) <= D(100)
    assert [(a.task_id, a.resource_id, a.percent) for a in p.assignments] == [
        ("t1", "alice", D(60)),
        ("t2", "alice", D(60)),
    ]


def test_a08_weekend_inside_delayed_task() -> None:
    p = a07_project()
    _, out = run(p)
    s, f = iv(out, "t2")
    assert s is not None and f is not None
    assert f - s == 3 * MPD  # no extra interruption on the working axis
    axis = WorkingAxis(p.calendar, p.start)
    assert axis.to_datetime(s, "start") == dt.datetime(2026, 10, 8, 9, 0)
    assert axis.to_datetime(f, "finish") == dt.datetime(2026, 10, 12, 17, 0)


def test_a19_costs_unchanged() -> None:
    p = a07_project()
    base, out = run(p)

    def durations(ts: dict[str, NodeTiming]) -> dict[str, int | None]:
        return {
            k: (t.finish - t.start if t.start is not None and t.finish is not None else None)
            for k, t in ts.items()
        }

    before = compute_costs(p, durations(base))
    after = compute_costs(p, durations(out.timings))
    assert before.total == after.total == D(2880)
    assert before == after


# ---------------------------------------------------------------- chains & types


def test_chain_push_through_all_dependency_types() -> None:
    p = (
        ProjectBuilder()
        .resource("r")
        .task("a", duration="2d")
        .task("b", duration="2d")
        .task("fs", duration="1d")
        .task("ss", duration="1d")
        .task("ff", duration="1d")
        .task("sf", duration="1d")
        .assign("a", "r", 100)
        .assign("b", "r", 100)
        .dep("b", "fs", "FS", lag="-4h")
        .dep("b", "ss", "SS", lag="2h")
        .dep("b", "ff", "FF", lag="1d")
        .dep("b", "sf", "SF", lag="3d")
        .build()
    )
    _, out = run(p)
    assert iv(out, "a") == (0, 960)
    bs, bf = iv(out, "b")
    assert (bs, bf) == (960, 1920)
    assert iv(out, "fs") == (1920 - 240, 1920 - 240 + 480)
    assert iv(out, "ss") == (960 + 120, 960 + 120 + 480)
    assert iv(out, "ff") == (1920 + 480 - 480, 1920 + 480)
    assert iv(out, "sf") == (960 + 1440 - 480, 960 + 1440)
    causes = {d.task_id: d.cause for d in out.delays}
    assert causes == {
        "b": "resource",
        "fs": "dependency",
        "ss": "dependency",
        "ff": "dependency",
        "sf": "dependency",
    }
    _assert_constraints(p, out)


def test_successor_with_own_conflict_is_resource_cause() -> None:
    # b is delayed by a; c follows b (FS) and also conflicts with d placed earlier
    p = (
        ProjectBuilder()
        .resource("r")
        .resource("q")
        .task("a", duration="1d")
        .task("b", duration="1d")
        .task("c", duration="1d")
        .task("d", duration="3d")
        .assign("a", "r")
        .assign("b", "r")
        .assign("c", "q")
        .assign("d", "q")
        .dep("b", "c")
        .build()
    )
    _, out = run(p)
    assert iv(out, "b") == (480, 960)
    assert iv(out, "d") == (0, 1440)
    assert iv(out, "c") == (1440, 1920)
    assert {d.task_id: d.cause for d in out.delays} == {"b": "resource", "c": "resource"}


def test_multi_resource_task_waits_for_all() -> None:
    p = (
        ProjectBuilder()
        .resource("r1")
        .resource("r2")
        .task("x", duration="1d")
        .task("y", duration="2d")
        .task("z", duration="1d")
        .assign("x", "r1", 70)
        .assign("y", "r2", 70)
        .assign("z", "r1", 50)
        .assign("z", "r2", 50)
        .build()
    )
    _, out = run(p)
    assert iv(out, "z") == (960, 1440)
    assert max_load(p, out) <= D(100)


def test_partial_fits_share_capacity() -> None:
    p = (
        ProjectBuilder()
        .resource("r")
        .task("a", duration="2d")
        .task("b", duration="2d")
        .task("c", duration="1d")
        .assign("a", "r", 50)
        .assign("b", "r", 50)
        .assign("c", "r", 50)
        .build()
    )
    _, out = run(p)
    assert iv(out, "a") == (0, 960)
    assert iv(out, "b") == (0, 960)
    assert iv(out, "c") == (960, 1440)


# ---------------------------------------------------------------- priority


def test_tie_break_by_wbs_order() -> None:
    p = (
        ProjectBuilder()
        .resource("r")
        .task("zz", duration="1d", order=0)
        .task("aa", duration="1d", order=1)
        .assign("zz", "r")
        .assign("aa", "r")
        .build()
    )
    _, out = run(p)
    assert iv(out, "zz") == (0, 480)
    assert iv(out, "aa") == (480, 960)


def test_earlier_dependency_only_start_wins_over_wbs() -> None:
    p = (
        ProjectBuilder()
        .resource("r")
        .task("pre", duration="1d")
        .task("late", duration="2d")  # base start 480 (after pre), earlier in WBS
        .task("early", duration="2d")  # base start 240 via SS lag
        .task("pre2", duration="1d")
        .assign("late", "r")
        .assign("early", "r")
        .dep("pre", "late")
        .dep("pre2", "early", "SS", lag="4h")
        .build()
    )
    _, out = run(p)
    assert iv(out, "early") == (240, 1200)
    assert iv(out, "late") == (1200, 2160)


# ---------------------------------------------------------------- unresolvable


def test_own_allocation_over_capacity_reported() -> None:
    cfg = Config(max_assignment_percent=D(150))
    p = (
        ProjectBuilder()
        .resource("r")
        .task("big", duration="1d")
        .task("other", duration="1d")
        .assign("big", "r", 150)
        .assign("other", "r", 30)
        .build()
    )
    _, out = run(p, cfg)
    assert iv(out, "big") == (0, 480)
    assert iv(out, "other") == (480, 960)  # never placed on top of the over-full span
    assert len(out.unresolved) == 1
    u = out.unresolved[0]
    assert (u.resource_id, u.task_ids, u.start, u.end, u.percent) == ("r", ("big",), 0, 480, D(150))
    assert OWN_ALLOCATION_REASON in u.reason and "big" in u.reason and "150" in u.reason


def test_forced_task_collision_on_other_resource_reported() -> None:
    cfg = Config(max_assignment_percent=D(150))
    p = (
        ProjectBuilder()
        .resource("r")
        .resource("q")
        .task("a", duration="1d")
        .task("big", duration="1d")
        .assign("a", "q", 80)
        .assign("big", "r", 120)
        .assign("big", "q", 50)
        .build()
    )
    _, out = run(p, cfg)
    assert iv(out, "big") == (0, 480)
    by_res = {u.resource_id: u for u in out.unresolved}
    assert set(by_res) == {"q", "r"}
    assert by_res["q"].task_ids == ("a", "big") and by_res["q"].percent == D(130)
    assert "another resource" in by_res["q"].reason


# ---------------------------------------------------------------- special nodes


def test_milestones_and_zero_duration_consume_nothing() -> None:
    p = (
        ProjectBuilder()
        .resource("r")
        .task("a", duration="1d")
        .task("z", duration="0d")
        .milestone("m")
        .task("b", duration="1d")
        .assign("a", "r")
        .assign("z", "r")
        .assign("b", "r")
        .dep("a", "m")
        .build()
    )
    _, out = run(p)
    assert iv(out, "z") == (0, 0)
    assert iv(out, "m") == (480, 480)
    assert iv(out, "b") == (480, 960)
    assert [d.task_id for d in out.delays] == ["b"]


def test_unschedulable_and_blocked_untouched() -> None:
    p = (
        ProjectBuilder()
        .resource("r")
        .task("a", duration="1d")
        .task("u")  # unsized
        .task("blk", duration="1d")
        .task("b", duration="1d")
        .assign("a", "r")
        .assign("blk", "r")
        .assign("b", "r")
        .dep("u", "blk")
        .build()
    )
    base, out = run(p)
    assert out.timings["u"] == base["u"] and out.timings["u"].status == "unschedulable"
    assert out.timings["blk"] == base["blk"] and out.timings["blk"].status == "blocked"
    assert iv(out, "b") == (480, 960)
    assert list(out.timings) == list(base)


def test_no_dependencies_no_conflict_unchanged() -> None:
    p = ProjectBuilder().resource("r").task("a", duration="1d").assign("a", "r", 40).build()
    base, out = run(p)
    assert out.timings == base
    assert out.delays == () and out.finish_delta_minutes == 0


def test_empty_project() -> None:
    p = ProjectBuilder().build()
    _, out = run(p)
    assert out.timings == {}
    assert (out.base_finish, out.leveled_finish, out.finish_delta_minutes) == (None, None, None)


# ---------------------------------------------------------------- progress / cancel


def _many(n: int) -> Project:
    b = ProjectBuilder().resource("r")
    for i in range(n):
        b.task(f"t{i:03d}", duration="1d").assign(f"t{i:03d}", "r", 60)
    return b.build()


def test_cancellation_after_k_tasks() -> None:
    p = _many(20)
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls > 5

    with pytest.raises(Cancelled):
        run(p, cancel=cancel)
    assert calls == 6


def test_progress_non_decreasing_ending_at_one() -> None:
    p = _many(250)
    seen: list[float] = []
    _, out = run(p, progress=lambda f, m: seen.append(f), cancel=lambda: False)
    assert seen[-1] == 1.0
    assert seen == sorted(seen)
    assert len(seen) > 3
    assert len(out.delays) == 249


def test_deterministic_and_picklable() -> None:
    p = _many(30)
    _, a = run(p)
    _, b = run(p)
    assert a == b
    assert pickle.loads(pickle.dumps(a)) == a


# ---------------------------------------------------------------- helpers


def _assert_constraints(p: Project, out: LevelingOutcome) -> None:
    for dep in p.dependencies:
        pt, st_ = out.timings[dep.pred_id], out.timings[dep.succ_id]
        if pt.start is None or st_.start is None:
            continue
        assert pt.finish is not None and st_.finish is not None
        lag = dep.lag.to_minutes(MPD)
        ok = {
            "FS": st_.start >= pt.finish + lag,
            "SS": st_.start >= pt.start + lag,
            "FF": st_.finish >= pt.finish + lag,
            "SF": st_.finish >= pt.start + lag,
        }[dep.type.value]
        assert ok, dep


# ---------------------------------------------------------------- benchmark


def _big_project(n: int, n_res: int, seed: int = 7) -> Project:
    rng = random.Random(seed)
    b = ProjectBuilder()
    for r in range(n_res):
        b.resource(f"r{r:03d}", rate="50")
    group_size = 50
    for g in range(n // group_size):
        gid = f"g{g:04d}"
        b.group(gid)
        for k in range(group_size):
            i = g * group_size + k
            tid = f"t{i:05d}"
            b.task(tid, parent=gid, duration=f"{rng.randint(1, 10)}d")
            for rid in rng.sample(range(n_res), rng.randint(1, 2)):
                b.assign(tid, f"r{rid:03d}", rng.choice([25, 50, 60, 75, 100]))
            if k > 0:
                for _ in range(rng.randint(0, 2)):
                    j = g * group_size + rng.randrange(k)
                    t = rng.choice(["FS", "FS", "FS", "SS", "FF", "SF"])
                    b.dep(f"t{j:05d}", tid, t, lag=f"{rng.randint(-2, 3)}d")
    return b.build()


@pytest.mark.slow
def test_benchmark_10k_tasks() -> None:
    p = _big_project(10_000, 200)
    sz = compute_sizing(p)
    base = forward_pass(p, sz, minutes_per_day=MPD)
    t0 = time.perf_counter()
    out = level(p, sz, base, minutes_per_day=MPD)
    elapsed = time.perf_counter() - t0
    print(f"\nleveling 10k tasks: {elapsed:.2f} s, {len(out.delays)} delayed")
    assert len(out.timings) == 10_000
    assert out.unresolved == ()
    assert max_load(p, out) <= D(100)
