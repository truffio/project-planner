"""Review batch B engine fixes: findings 4, 9, 10, 11, 12, 18."""

from __future__ import annotations

import dataclasses
import decimal
import pickle
import subprocess
import sys
from collections.abc import Iterator
from decimal import Decimal as D

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine import csv_io
from project_planner.engine.config import Config
from project_planner.engine.cost import compute_costs, cost_report
from project_planner.engine.errors import ValidationFailed
from project_planner.engine.model import (
    Calendar,
    CalendarSettings,
    Project,
    Resource,
    TimeQty,
    WorkUnit,
    days,
    hours,
    minutes_to_days,
)
from project_planner.engine.schedule import level, recost, schedule
from project_planner.engine.validation import validate

pytestmark = pytest.mark.unit


@pytest.fixture
def low_precision() -> Iterator[None]:
    with decimal.localcontext() as ctx:
        ctx.prec = 5
        yield


def a16_project() -> Project:
    return (
        ProjectBuilder()
        .resource("alice", rate="123.45")
        .resource("bob", rate=50)
        .task("t1", duration="37h")
        .assign("t1", "alice", D("33.3"))
        .assign("t1", "bob", 20)
        .task("t2", effort="40h")
        .assign("t2", "alice", 80)
        .assign("t2", "bob", 20)
        .dep("t1", "t2", "FS", lag="1d")
        .build()
    )


def reference(fn):  # type: ignore[no-untyped-def]
    """Run ``fn`` under the stock default context."""
    with decimal.localcontext(decimal.DefaultContext):
        return fn()


# ------------------------------------------------------------ finding 4


def test_results_do_not_depend_on_the_callers_decimal_context(low_precision: None) -> None:
    p = a16_project()
    low = schedule(p)
    low_py = cost_report(low.costs, p, WorkUnit.PERSON_YEARS)
    low_csv = csv_io.export(p, low)
    ref = reference(lambda: schedule(p))
    ref_py = reference(lambda: cost_report(ref.costs, p, WorkUnit.PERSON_YEARS))
    ref_csv = reference(lambda: csv_io.export(p, ref))
    assert low == ref
    assert len(low.total_cost.as_tuple().digits) > 5  # not squeezed to 5 digits
    assert low_py == ref_py
    assert low_csv == ref_csv


def test_repeating_fraction_cost_is_stable(low_precision: None) -> None:
    p = (
        ProjectBuilder()
        .resource("r", rate="100")
        .task("t", duration="1h")
        .assign("t", "r", 100)
        .build()
    )
    low = compute_costs(p, {"t": 1})
    ref = reference(lambda: compute_costs(p, {"t": 1}))
    assert low == ref
    assert len(str(low.total)) > 20
    assert minutes_to_days(1, 3) == reference(lambda: minutes_to_days(1, 3))
    assert len(str(minutes_to_days(1, 3))) > 20


def test_level_and_recost_under_low_precision(low_precision: None) -> None:
    p = a16_project()
    base = schedule(p)
    cheaper = dataclasses.replace(
        p, resources=tuple(dataclasses.replace(r, hourly_rate=D("77.77")) for r in p.resources)
    )
    ref = reference(lambda: recost(schedule(p), cheaper))
    ref_level = reference(lambda: level(p, schedule(p)))
    assert recost(base, cheaper) == ref
    assert level(p, base) == ref_level


def test_config_rounding_is_context_independent(low_precision: None) -> None:
    assert Config().round_money(D("12345678.905")) == D("12345678.90")
    assert Config().round_days(D("12345678.915")) == D("12345678.92")


# ------------------------------------------------------------ finding 9


def test_recost_refreshes_missing_rate_issue() -> None:
    p = ProjectBuilder().resource("r").task("t", duration="1d").assign("t", "r", 100).build()
    res = schedule(p)
    assert [i.code for i in res.issues] == ["COST_MISSING_RATE"]
    rated = dataclasses.replace(p, resources=(Resource("r", "r", D(10)),))
    again = recost(res, rated)
    assert not again.issues and again.cost_complete
    assert again.issues == schedule(rated).issues
    cleared = recost(again, p)
    assert [i.code for i in cleared.issues] == ["COST_MISSING_RATE"]
    assert cleared.issues == res.issues and not cleared.cost_complete


def test_recost_issue_order_matches_fresh_schedule() -> None:
    b = ProjectBuilder().resource("a", rate=1).resource("z").resource("m", rate=1)
    b = b.task("t", duration="1d").task("u")
    b = b.assign("t", "a", 50).assign("t", "z", 50).assign("t", "m", 50)
    p = b.build()
    res = schedule(p)
    rates = {"a": None, "z": D(3), "m": None}
    q = dataclasses.replace(
        p, resources=tuple(dataclasses.replace(r, hourly_rate=rates[r.id]) for r in p.resources)
    )
    assert recost(res, q).issues == schedule(q).issues


# ------------------------------------------------------------ finding 10


def test_zero_effort_task_has_no_capacity_warning() -> None:
    p = ProjectBuilder().task("z", effort="0h").build()
    assert [i.code for i in validate(p)] == []
    res = schedule(p)
    assert res.complete and not res.issues


def test_nonzero_effort_without_assignment_still_warns() -> None:
    p = ProjectBuilder().task("e", effort="1h").build()
    assert [i.code for i in validate(p)] == ["TASK_NO_CAPACITY"]


# ------------------------------------------------------------ finding 11


def test_result_mappings_are_read_only_and_equal() -> None:
    p = a16_project()
    res = schedule(p)
    for name in ("nodes", "assignments", "dependencies", "loading"):
        m = getattr(res, name)
        with pytest.raises(TypeError):
            m["x"] = None
        with pytest.raises(AttributeError):
            m.clear()
    with pytest.raises(TypeError):
        res.costs.tasks["x"] = None  # type: ignore[index]
    assert res.nodes == dict(res.nodes)
    assert schedule(p) == res
    assert res.node("t1").node_id == "t1"


def test_results_still_pickle() -> None:
    p = a16_project()
    res = schedule(p)
    assert pickle.loads(pickle.dumps(res)) == res
    lv = level(p, res)
    back = pickle.loads(pickle.dumps(lv))
    assert back == lv
    with pytest.raises(TypeError):
        lv.delays_days["x"] = D(1)  # type: ignore[index]


def test_replace_keeps_mappings_read_only() -> None:
    res = schedule(a16_project())
    again = dataclasses.replace(res, nodes=dict(res.nodes))
    with pytest.raises(TypeError):
        again.nodes["x"] = None  # type: ignore[index]


# ------------------------------------------------------------ finding 12


@pytest.mark.parametrize("text", ["1e7d", "36601d", "-36601d", "878401h", "1e999999999h"])
def test_absurd_time_quantities_are_rejected(text: str) -> None:
    with pytest.raises(ValidationFailed) as info:
        TimeQty.parse(text)
    assert info.value.issues[0].code == "TIME_OUT_OF_RANGE"


def test_limit_is_inclusive() -> None:
    assert days(36600).value == 36600 and hours(878400).value == 878400


def test_schedule_never_raises_raw_overflow() -> None:
    q = (
        ProjectBuilder()
        .resource("r")
        .task("a", effort="878400h")
        .assign("a", "r", "0.0001")
        .task("b", duration="36600d")
        .dep("a", "b", "FS", lag="36600d")
        .build()
    )
    with pytest.raises(ValidationFailed) as info:
        schedule(q)
    assert info.value.issues[0].code == "TIME_OUT_OF_RANGE"
    with pytest.raises(ValidationFailed):
        level(q, schedule(dataclasses.replace(q, dependencies=())))


# ------------------------------------------------------------ finding 18


def test_calendar_repr_lists_weekdays_monday_first() -> None:
    for cal in (Calendar(), CalendarSettings()):
        text = repr(cal)
        order = [text.index(f"Weekday.{d}") for d in ("MON", "TUE", "WED", "THU", "FRI")]
        assert order == sorted(order)
    assert "SAT" not in repr(Calendar())


def test_calendar_repr_is_identical_across_hash_seeds() -> None:
    code = (
        "from project_planner.engine.model import Calendar, Weekday;"
        "print(repr(Calendar(working_weekdays=frozenset(Weekday))))"
    )
    outs = {
        subprocess.run(
            [sys.executable, "-c", code],
            env={"PYTHONHASHSEED": str(seed), "SYSTEMROOT": "C:\\Windows"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in (1, 2, 3, 4)
    }
    assert len(outs) == 1
