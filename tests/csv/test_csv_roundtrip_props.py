"""Hypothesis round-trip properties of the CSV exporter / importer."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fixtures.builders import DEFAULT_START, ProjectBuilder
from hypothesis import given
from hypothesis import strategies as st

from project_planner.engine.csv_io import export, parse
from project_planner.engine.errors import Severity
from project_planner.engine.model import Project, WorkUnit
from project_planner.engine.validation import validate

pytestmark = [pytest.mark.csv, pytest.mark.property]

_NAME = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"), blacklist_characters="\x85"
    )
    | st.sampled_from([",", '"', " ", "\n", "\r"]),
    min_size=1,
    max_size=12,
).filter(lambda s: s.strip(" \t") != "")
_ID = st.from_regex(r"[A-Za-z0-9_.\-]{1,6}", fullmatch=True)


def _scaled(low: int, high: int, max_scale: int) -> st.SearchStrategy[Decimal]:
    """Decimals with a random number of fraction digits, e.g. ``1.50`` (scale is kept)."""
    return st.integers(0, max_scale).flatmap(
        lambda scale: st.integers(low * 10**scale, high * 10**scale).map(
            lambda n: Decimal(n).scaleb(-scale)
        )
    )


_DEC = _scaled(0, 1000, 3)
_SIZE = st.builds(lambda v, u: f"{v:f}{u}", _DEC, st.sampled_from(["h", "d"]))
_LAG = st.builds(lambda v, u: f"{v:f}{u}", _scaled(-100, 100, 2), st.sampled_from(["h", "d"]))
_DATES = st.dates(min_value=dt.date(2000, 1, 1), max_value=dt.date(2040, 12, 31))


@st.composite
def projects(draw: st.DrawFn) -> Project:
    b = ProjectBuilder(
        start=draw(_DATES),
        name=draw(_NAME),
        currency=draw(st.sampled_from(["USD", "EUR", "JPY"])),
        cost_report_unit=draw(st.sampled_from(list(WorkUnit))),
    )
    hours_per_day = draw(st.sampled_from(["8", "7.5", "6.25", "24", "1"]))
    start_minutes = draw(st.integers(0, min(24 * 60 - int(Decimal(hours_per_day) * 60), 1000)))
    holiday_dates = draw(st.lists(_DATES, max_size=4, unique=True))
    exception_dates = draw(st.lists(_DATES, max_size=3, unique=True))
    exception_dates = [d for d in exception_dates if d not in holiday_dates]
    b.calendar(
        hours_per_day=hours_per_day,
        working_days_per_year=draw(st.integers(1, 366)),
        working_weekdays=draw(
            st.lists(
                st.sampled_from(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]),
                min_size=1,
                unique=True,
            )
        ),
        workday_start=dt.time(start_minutes // 60, start_minutes % 60),
        holidays=[(d, draw(st.text(max_size=6))) for d in holiday_dates],
        exceptions=[
            (d, draw(st.sampled_from(["working", "nonworking"])), draw(st.text(max_size=6)))
            for d in exception_dates
        ],
    )
    resource_ids = draw(st.lists(_ID, max_size=4, unique=True))
    for rid in resource_ids:
        rate = draw(st.one_of(st.none(), _DEC))
        b.resource(rid, draw(_NAME), rate=rate)

    # nodes: groups first (tree), then leaves
    n_groups = draw(st.integers(0, 3))
    n_leaves = draw(st.integers(1, 8))
    group_ids = [f"g{i}" for i in range(n_groups)]
    for i, gid in enumerate(group_ids):
        parent = draw(st.sampled_from([None, *group_ids[:i]]))
        b.group(gid, draw(_NAME), parent=parent, order=draw(st.integers(-3, 5)))
    leaf_ids: list[str] = []
    for i in range(n_leaves):
        lid = f"L{i}"
        parent = draw(st.sampled_from([None, *group_ids]))
        order = draw(st.integers(-3, 5))
        name = draw(_NAME)
        kind = draw(st.sampled_from(["duration", "effort", "unsized", "milestone"]))
        if kind == "milestone":
            b.milestone(lid, name, parent=parent, order=order)
        elif kind == "unsized":
            b.task(lid, name, parent=parent, order=order)
        elif kind == "duration":
            b.task(lid, name, parent=parent, order=order, duration=draw(_SIZE))
        else:
            b.task(lid, name, parent=parent, order=order, effort=draw(_SIZE))
        leaf_ids.append(lid)
    tasks = [n.id for n in b.build().nodes if n.kind.value == "task"]
    if resource_ids and tasks:
        pairs = draw(
            st.lists(st.tuples(st.sampled_from(tasks), st.sampled_from(resource_ids)), unique=True)
        )
        for task, res in pairs:
            percent = draw(_scaled(1, 100, 2).filter(lambda x: x > 0))
            b.assign(task, res, percent)
    # acyclic dependencies: only forward in leaf index order
    if len(leaf_ids) > 1:
        edges = draw(
            st.lists(
                st.tuples(st.integers(0, len(leaf_ids) - 2), st.integers(1, len(leaf_ids) - 1)),
                max_size=6,
                unique=True,
            )
        )
        seen: set[tuple[int, int, str]] = set()
        for a, c in edges:
            if a >= c:
                continue
            dtype = draw(st.sampled_from(["FS", "SS", "FF", "SF"]))
            if (a, c, dtype) in seen:
                continue
            seen.add((a, c, dtype))
            b.dep(leaf_ids[a], leaf_ids[c], dtype, lag=draw(_LAG), id=f"D{len(seen)}")
    return b.build()


@given(projects())
def test_generated_projects_are_valid_and_round_trip(project: Project) -> None:
    assert not [i for i in validate(project) if i.severity is Severity.ERROR]
    text = export(project)
    outcome = parse(text)
    assert outcome.project == project
    assert export(outcome.project) == text  # byte-identical re-export
    assert export(project) == text  # deterministic


@given(projects())
def test_export_lists_every_node_once(project: Project) -> None:
    import csv
    import io

    reader = csv.DictReader(io.StringIO(export(project), newline=""))
    ids = [r["id"] for r in reader if r["record_type"] == "NODE"]
    assert sorted(ids) == sorted(n.id for n in project.nodes)


def test_default_start_constant_is_used() -> None:
    p = ProjectBuilder().task("t1", duration="1d").build()
    assert p.start == DEFAULT_START
    assert parse(export(p)).project == p
