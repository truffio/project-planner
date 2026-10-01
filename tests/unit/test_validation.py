"""Unit tests for engine.validation (task T12). Each rule: a positive and a negative case."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine.config import Config
from project_planner.engine.errors import Issue, Severity, ValidationFailed
from project_planner.engine.model import NodeKind, Project, WbsNode
from project_planner.engine.validation import ensure_valid, schedule_blocking, validate

pytestmark = pytest.mark.unit


def codes(issues: list[Issue]) -> list[str]:
    return [i.code for i in issues]


def only(issues: list[Issue], code: str) -> list[Issue]:
    return [i for i in issues if i.code == code]


def base() -> ProjectBuilder:
    b = ProjectBuilder().group("g1").task("t1", parent="g1", duration="1d")
    return b.task("t2", duration="1d")


def test_valid_project_has_no_issues() -> None:
    p = base().resource("r1", rate=10).assign("t1", "r1", 50).dep("t1", "t2").build()
    assert validate(p) == []
    ensure_valid(p)


# --- dependencies ------------------------------------------------------------------


def test_dep_dangling_pred_and_succ() -> None:
    p = base().dep("nope", "t1", id="d1").dep("t1", "ghost", id="d2").build()
    got = only(validate(p), "DEP_DANGLING")
    assert [(i.object_type, i.object_id, i.field, i.severity) for i in got] == [
        ("dependency", "d1", "pred_id", Severity.ERROR),
        ("dependency", "d2", "succ_id", Severity.ERROR),
    ]


def test_dep_not_dangling_when_valid() -> None:
    assert not only(validate(base().dep("t1", "t2").build()), "DEP_DANGLING")


def test_dep_self() -> None:
    issues = validate(base().dep("t1", "t1", id="d1").build())
    (i,) = only(issues, "DEP_SELF")
    assert (i.object_id, i.field) == ("d1", "succ_id")
    assert "DEP_CYCLE" not in codes(issues)
    assert not only(validate(base().dep("t1", "t2").build()), "DEP_SELF")


def test_dep_group_endpoint_names_group() -> None:
    issues = validate(base().dep("g1", "t2", id="d1").dep("t2", "g1", id="d2").build())
    got = only(issues, "DEP_GROUP_ENDPOINT")
    assert {(i.object_id, i.field) for i in got} == {("d1", "pred_id"), ("d2", "succ_id")}
    assert all("g1" in i.message for i in got)
    assert not only(validate(base().dep("t1", "t2").build()), "DEP_GROUP_ENDPOINT")


def test_dep_duplicate_same_type_only() -> None:
    b = base().dep("t1", "t2", id="d1").dep("t1", "t2", id="d2").dep("t1", "t2", "SS", id="d3")
    (i,) = only(validate(b.build()), "DEP_DUPLICATE")
    assert (i.object_id, i.field, i.severity) == ("d2", "type", Severity.ERROR)
    assert "d1" in i.message
    q = base().dep("t1", "t2", "FS").dep("t1", "t2", "SS").build()
    assert not only(validate(q), "DEP_DUPLICATE")


def test_dep_cycle_three_nodes_lists_all() -> None:
    p = (
        ProjectBuilder()
        .task("c", duration="1d")
        .task("a", duration="1d")
        .task("b", duration="1d")
        .dep("a", "b")
        .dep("b", "c")
        .dep("c", "a")
        .build()
    )
    (i,) = only(validate(p), "DEP_CYCLE")
    assert i.object_id == "a"
    assert i.object_type == "node"
    assert i.field == "dependencies"
    assert i.severity is Severity.ERROR
    assert all(m in i.message for m in ("a", "b", "c"))


def test_two_disjoint_cycles_two_issues() -> None:
    b = ProjectBuilder()
    for n in "abcd":
        b.task(n, duration="1d")
    p = b.dep("a", "b").dep("b", "a").dep("c", "d").dep("d", "c").build()
    got = only(validate(p), "DEP_CYCLE")
    assert [i.object_id for i in got] == ["a", "c"]


def test_acyclic_has_no_cycle_issue() -> None:
    assert not only(validate(base().dep("t1", "t2").build()), "DEP_CYCLE")


# --- WBS ---------------------------------------------------------------------------


def test_parent_dangling() -> None:
    p = ProjectBuilder().task("t1", parent="ghost", duration="1d").build()
    (i,) = only(validate(p), "NODE_PARENT_DANGLING")
    assert (i.object_type, i.object_id, i.field) == ("node", "t1", "parent_id")
    assert not only(validate(base().build()), "NODE_PARENT_DANGLING")


def test_parent_not_group() -> None:
    p = ProjectBuilder().task("t1", duration="1d").task("t2", parent="t1", duration="1d").build()
    (i,) = only(validate(p), "NODE_PARENT_NOT_GROUP")
    assert (i.object_id, i.field) == ("t2", "parent_id")
    assert not only(validate(base().build()), "NODE_PARENT_NOT_GROUP")


def test_parent_cycle_and_self_parent() -> None:
    p = Project(
        id="p",
        name="p",
        start=ProjectBuilder().build().start,
        nodes=(
            WbsNode("g1", "g1", NodeKind.GROUP, parent_id="g2"),
            WbsNode("g2", "g2", NodeKind.GROUP, parent_id="g3"),
            WbsNode("g3", "g3", NodeKind.GROUP, parent_id="g1"),
            WbsNode("s", "s", NodeKind.GROUP, parent_id="s"),
        ),
    )
    got = only(validate(p), "NODE_PARENT_CYCLE")
    assert [i.object_id for i in got] == ["g1", "s"]
    assert all(m in got[0].message for m in ("g1", "g2", "g3"))
    assert not only(validate(base().build()), "NODE_PARENT_CYCLE")


# --- assignments -------------------------------------------------------------------


def test_assignment_task_dangling_and_not_task() -> None:
    p = base().resource("r1", rate=1).assign("ghost", "r1").assign("g1", "r1").build()
    issues = validate(p)
    (d,) = only(issues, "ASSIGN_TASK_DANGLING")
    assert (d.object_type, d.object_id, d.field) == ("assignment", "ghost/r1", "task_id")
    (n,) = only(issues, "ASSIGN_NOT_TASK")
    assert (n.object_id, n.field) == ("g1/r1", "task_id")
    ok = validate(base().resource("r1", rate=1).assign("t1", "r1").build())
    assert not only(ok, "ASSIGN_TASK_DANGLING") and not only(ok, "ASSIGN_NOT_TASK")


def test_assignment_resource_dangling() -> None:
    p = base().assign("t1", "ghost").build()
    (i,) = only(validate(p), "ASSIGN_RESOURCE_DANGLING")
    assert (i.object_id, i.field) == ("t1/ghost", "resource_id")
    ok = base().resource("r1", rate=1).assign("t1", "r1").build()
    assert not only(validate(ok), "ASSIGN_RESOURCE_DANGLING")


def test_assignment_percent_range_uses_config() -> None:
    p = base().resource("r1", rate=1).assign("t1", "r1", 150).assign("t2", "r1", 100).build()
    (i,) = only(validate(p), "ASSIGN_PERCENT_RANGE")
    assert (i.object_id, i.field) == ("t1/r1", "percent")
    assert not only(
        validate(p, Config(max_assignment_percent=Decimal(200))), "ASSIGN_PERCENT_RANGE"
    )


# --- warnings ----------------------------------------------------------------------


def test_missing_rate_only_with_assignments() -> None:
    p = base().resource("r1").resource("r2").assign("t1", "r1").build()
    (i,) = only(validate(p), "COST_MISSING_RATE")
    assert (i.severity, i.object_type, i.object_id, i.field) == (
        Severity.WARNING,
        "resource",
        "r1",
        "hourly_rate",
    )
    q = base().resource("r1", rate=0).assign("t1", "r1").build()
    assert not only(validate(q), "COST_MISSING_RATE")


def test_task_unsized() -> None:
    p = ProjectBuilder().task("t1").task("t2", duration="1d").milestone("m1").build()
    (i,) = only(validate(p), "TASK_UNSIZED")
    assert (i.severity, i.object_id, i.field) == (Severity.WARNING, "t1", "sizing")


def test_task_no_capacity_effort_only() -> None:
    p = (
        ProjectBuilder()
        .resource("r1", rate=1)
        .task("e1", effort="8h")
        .task("e2", effort="8h")
        .task("d1", duration="1d")
        .assign("e2", "r1", 50)
        .build()
    )
    (i,) = only(validate(p), "TASK_NO_CAPACITY")
    assert (i.severity, i.object_id, i.field) == (Severity.WARNING, "e1", "assignments")


# --- aggregate behaviour -----------------------------------------------------------


def test_all_issues_reported_together_and_sorted() -> None:
    p = (
        ProjectBuilder()
        .task("t1", parent="ghost", duration="1d")
        .task("t2")
        .resource("r1")
        .assign("t1", "r1", 500)
        .assign("t1", "nores", 10)
        .dep("t1", "t1", id="d1")
        .dep("x", "t2", id="d2")
        .build()
    )
    issues = validate(p)
    assert set(codes(issues)) >= {
        "NODE_PARENT_DANGLING",
        "TASK_UNSIZED",
        "COST_MISSING_RATE",
        "ASSIGN_PERCENT_RANGE",
        "ASSIGN_RESOURCE_DANGLING",
        "DEP_SELF",
        "DEP_DANGLING",
    }
    keys = [(i.object_type or "", i.object_id or "", i.field or "", i.code) for i in issues]
    assert keys == sorted(keys)
    assert validate(p) == issues


def test_deterministic_regardless_of_input_order() -> None:
    def build(order: list[str]) -> Project:
        b = ProjectBuilder()
        for n in order:
            b.task(n, duration="1d")
        return b.dep("a", "b").dep("b", "c").dep("c", "a").build()

    assert validate(build(["a", "b", "c"])) == validate(build(["c", "a", "b"]))


def test_schedule_blocking_and_ensure_valid() -> None:
    warn_only = base().resource("r1").assign("t1", "r1").build()
    issues = validate(warn_only)
    assert issues and not schedule_blocking(issues)
    ensure_valid(warn_only)

    bad = base().resource("r1").assign("t1", "r1").dep("t1", "t1").build()
    assert schedule_blocking(validate(bad))
    with pytest.raises(ValidationFailed) as exc:
        ensure_valid(bad)
    assert exc.value.issues
    assert all(i.severity is Severity.ERROR for i in exc.value.issues)
