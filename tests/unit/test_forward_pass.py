"""Unit tests for the forward pass (T15)."""

from __future__ import annotations

from typing import Any

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.forward_pass import NodeTiming, forward_pass
from project_planner.engine.model import Project
from project_planner.engine.sizing import compute_sizing

MPD = 480


def run(project: Project, **kw: Any) -> dict[str, NodeTiming]:
    return forward_pass(project, compute_sizing(project), minutes_per_day=MPD, **kw)


def pair(dtype: str, lag: str) -> dict[str, NodeTiming]:
    b = (
        ProjectBuilder()
        .task("P", duration="2d")
        .task("S", duration="1d")
        .dep("P", "S", dtype, lag=lag)
    )
    return run(b.build(), min_starts={"P": 960})


LAGS = ["1d", "0d", "-4h"]
EXPECTED = {
    "FS": [2400, 1920, 1680],
    "SS": [1440, 960, 720],
    "FF": [1920, 1440, 1200],
    "SF": [960, 480, 240],
}


@pytest.mark.unit
@pytest.mark.parametrize("dtype", list(EXPECTED))
@pytest.mark.parametrize("i", range(3))
def test_types_and_lags(dtype: str, i: int) -> None:
    r = pair(dtype, LAGS[i])
    assert r["P"].start == 960 and r["P"].finish == 1920
    assert r["S"].start == EXPECTED[dtype][i]
    assert r["S"].finish == EXPECTED[dtype][i] + 480


@pytest.mark.unit
def test_half_day_negative_lag() -> None:
    assert pair("FS", "-0.5d")["S"].start == 1680
    assert pair("SF", "-1d")["S"].start == 0
    assert pair("SF", "-2d")["S"].start == 0


@pytest.mark.unit
@pytest.mark.parametrize("dtype", ["FS", "SS", "FF", "SF"])
def test_clamped_at_zero(dtype: str) -> None:
    b = (
        ProjectBuilder()
        .task("P", duration="1d")
        .task("S", duration="1d")
        .dep("P", "S", dtype, lag="-5d")
    )
    r = run(b.build())
    assert r["S"].start == 0 and r["S"].finish == 480


@pytest.mark.unit
def test_multiple_predecessors_binding_middle() -> None:
    b = (
        ProjectBuilder()
        .task("A", duration="1d")
        .task("B", duration="3d")
        .task("C", duration="1d")
        .task("S", duration="2d")
        .dep("A", "S", "FS")  # 480
        .dep("B", "S", "FS", lag="1d")  # 1440 + 480 = 1920
        .dep("C", "S", "FF")  # 480 - 960 < 0
    )
    assert run(b.build())["S"].start == 1920


@pytest.mark.unit
def test_multiple_predecessors_binding_last() -> None:
    b = (
        ProjectBuilder()
        .task("A", duration="1d")
        .task("B", duration="1d")
        .task("C", duration="5d")
        .task("S", duration="1d")
        .dep("A", "S", "FS")
        .dep("B", "S", "SS", lag="1d")
        .dep("C", "S", "FF", lag="1d")  # 2400 + 480 - 480
    )
    assert run(b.build())["S"].start == 2400


@pytest.mark.unit
def test_milestones_and_chain() -> None:
    b = (
        ProjectBuilder()
        .milestone("M0")
        .task("A", duration="1d")
        .milestone("M1")
        .task("B", duration="4h")
        .dep("M0", "A")
        .dep("A", "M1")
        .dep("M1", "B", lag="1d")
    )
    r = run(b.build())
    assert (r["M0"].start, r["M0"].finish) == (0, 0)
    assert (r["A"].start, r["A"].finish) == (0, 480)
    assert (r["M1"].start, r["M1"].finish) == (480, 480)
    assert (r["B"].start, r["B"].finish) == (960, 1200)


@pytest.mark.unit
def test_min_starts_lower_bound() -> None:
    b = ProjectBuilder().task("A", duration="1d").task("B", duration="1d").dep("A", "B")
    r = run(b.build(), min_starts={"A": 100, "B": 300})
    assert r["A"].start == 100
    assert r["B"].start == 580  # predecessor binds
    r = run(b.build(), min_starts={"B": 1000})
    assert r["B"].start == 1000 and r["B"].finish == 1480


@pytest.mark.unit
def test_unschedulable_propagation() -> None:
    b = (
        ProjectBuilder()
        .task("t1", duration="1d")
        .task("t2")  # unsized
        .task("t3", duration="1d")
        .task("t4", duration="1d")
        .task("ok", duration="1d")
        .dep("t1", "t2")
        .dep("t2", "t3")
        .dep("t3", "t4")
    )
    r = run(b.build())
    assert r["t1"].status == "scheduled"
    assert r["t2"].status == "unschedulable" and r["t2"].reason
    assert r["t2"].start is None and r["t2"].finish is None
    assert r["t3"].status == "blocked" and r["t3"].blocked_by == ("t2",)
    assert "t2" in (r["t3"].reason or "") and "unschedulable" in (r["t3"].reason or "")
    assert r["t4"].status == "blocked" and r["t4"].blocked_by == ("t3",)
    assert "t3" in (r["t4"].reason or "") and "t2" in (r["t4"].reason or "")
    assert r["ok"].status == "scheduled" and r["ok"].start == 0


@pytest.mark.unit
def test_blocked_lists_all_bad_predecessors() -> None:
    b = (
        ProjectBuilder()
        .task("u1")
        .task("u2")
        .task("good", duration="1d")
        .task("s", duration="1d")
        .dep("u2", "s")
        .dep("u1", "s")
        .dep("good", "s")
    )
    assert run(b.build())["s"].blocked_by == ("u1", "u2")


@pytest.mark.unit
def test_explicit_order_and_determinism() -> None:
    p = ProjectBuilder().task("A", duration="1d").task("B", duration="1d").dep("A", "B").build()
    assert run(p, order=["A", "B"]) == run(p)
