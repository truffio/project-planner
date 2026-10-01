"""Compute service (T32): synchronous schedule and the leveling state machine."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from project_planner.engine.config import Config
from project_planner.engine.errors import Conflict, ValidationFailed
from project_planner.engine.fingerprint import schedule_fp
from project_planner.services import compute
from project_planner.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.services

START = date(2026, 10, 5)
DAY = 480  # minutes per working day (8 h)


def build_overloaded(ws: Workspace) -> tuple[str, str, str]:
    """Alice 60% on T1 (3 d) and T2 (3 d) at project start; T3 (1 d) FS after T2.

    Leveling delays T2 by 3 days (1440 min) and T3 with it.
    """
    ws.new_project("Demo", START, discard_unsaved=True)
    alice = ws.add_resource("Alice", hourly_rate="100")
    t1 = ws.add_task("T1", duration="3d")
    t2 = ws.add_task("T2", duration="3d")
    t3 = ws.add_task("T3", duration="1d")
    ws.set_assignment(t1, alice, percent=60)
    ws.set_assignment(t2, alice, percent=60)
    ws.add_dependency(t2, t3, "FS")
    return t1, t2, t3


@pytest.fixture
def ws() -> Iterator[Workspace]:
    workspace = open_workspace()
    yield workspace
    workspace.close()


@pytest.fixture
def tasks(ws: Workspace) -> tuple[str, str, str]:
    return build_overloaded(ws)


def start_of(result: object, node_id: str) -> int | None:
    return result.node(node_id).start_minutes  # type: ignore[attr-defined, no-any-return]


def invalid_workspace(path: Path) -> Workspace:
    """A file workspace whose definition fails validation under the default config.

    The assignment (150%) was entered under a config allowing 200% and is rejected by
    ``validate`` once the workspace is reopened with the default 100% maximum.
    """
    with open_workspace(path, config=Config(max_assignment_percent=Decimal(200))) as w:
        w.new_project("Bad", START)
        alice = w.add_resource("Alice")
        w.set_assignment(w.add_task("T1", duration="1d"), alice, percent=150)
    return open_workspace(path)


def test_schedule_stores_dependency_only_result(ws: Workspace, tasks: tuple[str, ...]) -> None:
    assert ws.result() is None
    result = compute.schedule(ws)
    assert result.kind == "dependency_only"
    assert ws.result() == result
    st = ws.state()
    assert (st.has_result, st.has_preview, st.stale_dates) == (True, False, False)
    t1, t2, t3 = tasks
    assert (start_of(result, t1), start_of(result, t2), start_of(result, t3)) == (0, 0, 3 * DAY)


def test_schedule_validation_failure_stores_nothing(tmp_path: Path) -> None:
    with invalid_workspace(tmp_path / "bad.db") as bad:
        with pytest.raises(ValidationFailed) as info:
            compute.schedule(bad)
        assert any(i.code == "ASSIGN_PERCENT_RANGE" for i in info.value.issues)
        assert bad.result() is None
        assert bad.state().has_result is False
        with pytest.raises(ValidationFailed):
            compute.level_preview(bad)
        assert bad.state().has_preview is False


def test_preview_does_not_replace_current_result(ws: Workspace, tasks: tuple[str, ...]) -> None:
    _, t2, t3 = tasks
    base = compute.schedule(ws)
    preview = compute.level_preview(ws)
    assert preview.result.kind == "leveling_preview"
    assert start_of(preview.result, t2) == 3 * DAY
    assert start_of(preview.result, t3) == 6 * DAY
    assert preview.delays_days[t2] == Decimal(3)
    assert ws.state().has_preview is True
    assert ws.result() == base
    assert ws._results.get("leveling_preview") == preview.result
    # the stored base was current, so it was reused (not recalculated / replaced)
    assert ws._results.get("dependency_only") is base


def test_level_preview_calculates_missing_base(ws: Workspace, tasks: tuple[str, ...]) -> None:
    preview = compute.level_preview(ws)
    st = ws.state()
    assert (st.has_result, st.has_preview, st.stale_dates) == (True, True, False)
    current = ws.result()
    assert current is not None and current.kind == "dependency_only"
    assert start_of(current, tasks[1]) == 0
    assert start_of(preview.result, tasks[1]) == 3 * DAY


def test_level_preview_recalculates_stale_base(ws: Workspace, tasks: tuple[str, ...]) -> None:
    old = compute.schedule(ws)
    late = ws.add_task("Late", duration="1d")
    assert ws.state().stale_dates is True
    compute.level_preview(ws)
    st = ws.state()
    assert (st.stale_dates, st.has_preview) == (False, True)
    base = ws.result()
    assert base is not None and base != old
    assert start_of(base, late) == 0


def test_apply_without_preview_conflicts(ws: Workspace, tasks: tuple[str, ...]) -> None:
    with pytest.raises(Conflict):
        compute.apply_leveling(ws)
    compute.schedule(ws)
    with pytest.raises(Conflict):
        compute.apply_leveling(ws)
    compute.level_preview(ws)
    compute.discard_leveling(ws)
    with pytest.raises(Conflict):
        compute.apply_leveling(ws)
    current = ws.result()
    assert current is not None and current.kind == "dependency_only"


def test_apply_stale_preview_conflicts(ws: Workspace, tasks: tuple[str, ...]) -> None:
    compute.level_preview(ws)
    ws.add_task("Late", duration="1d")
    with pytest.raises(Conflict):
        compute.apply_leveling(ws)
    assert ws.state().has_preview is True  # left for the user to discard
    current = ws.result()
    assert current is not None and current.kind == "dependency_only"


def test_state_machine(ws: Workspace, tasks: tuple[str, ...]) -> None:
    _, t2, _ = tasks
    compute.schedule(ws)
    compute.level_preview(ws)
    compute.discard_leveling(ws)
    assert ws.state().has_preview is False
    current = ws.result()
    assert current is not None and current.kind == "dependency_only"
    assert start_of(current, t2) == 0

    compute.level_preview(ws)
    leveled = compute.apply_leveling(ws)
    assert leveled.kind == "leveled"
    st = ws.state()
    assert (st.has_preview, st.stale_dates, st.has_result) == (False, False, True)
    assert ws.result() == leveled
    assert start_of(leveled, t2) == 3 * DAY
    assert leveled.node(t2).leveling_delay_days == Decimal(3)

    # a new preview on top of an applied leveling keeps the leveled result current
    compute.level_preview(ws)
    assert ws.state().has_preview is True
    assert ws.result() == leveled

    restored = compute.reset_to_dependency_schedule(ws)
    assert restored is not None and restored.kind == "dependency_only"
    assert ws.result() == restored
    assert ws.state().has_preview is False
    assert start_of(restored, t2) == 0
    assert restored.node(t2).leveling_delay_days == Decimal(0)

    compute.discard_leveling(ws)  # nothing to discard: no-op
    assert ws.result() == restored


def test_reset_keeps_stale_dependency_result_stale(ws: Workspace, tasks: tuple[str, ...]) -> None:
    base = compute.schedule(ws)
    compute.level_preview(ws)
    compute.apply_leveling(ws)
    ws.add_task("Late", duration="1d")
    restored = compute.reset_to_dependency_schedule(ws)
    assert restored is not None and restored.kind == "dependency_only"
    assert restored.schedule_fp == base.schedule_fp != schedule_fp(ws.project())
    assert ws.state().stale_dates is True


def test_reset_without_results(ws: Workspace, tasks: tuple[str, ...]) -> None:
    assert compute.reset_to_dependency_schedule(ws) is None


def test_leveled_result_goes_stale_and_is_not_releveled(
    ws: Workspace, tasks: tuple[str, ...]
) -> None:
    _, t2, _ = tasks
    compute.schedule(ws)
    compute.level_preview(ws)
    leveled = compute.apply_leveling(ws)
    ws.set_sizing(t2, duration="4d")
    st = ws.state()
    assert st.stale_dates is True
    assert st.has_preview is False
    assert ws.result() == leveled  # still the old leveled result, visibly stale
    fresh = compute.schedule(ws)  # explicit Calculate: dependency-only, leveling dropped
    assert fresh.kind == "dependency_only"
    assert start_of(fresh, t2) == 0
    assert ws.result() == fresh
    assert ws.state().stale_dates is False


def test_rate_edit_keeps_leveled_result_current(ws: Workspace, tasks: tuple[str, ...]) -> None:
    compute.level_preview(ws)
    compute.apply_leveling(ws)
    alice = ws.project().resources[0].id
    ws.set_hourly_rate(alice, "200")
    st = ws.state()
    assert (st.stale_dates, st.stale_costs) == (False, False)
    current = ws.result()
    assert current is not None and current.kind == "leveled"
    assert current.total_cost == Decimal(2 * 24 * 200) * Decimal("0.6")
