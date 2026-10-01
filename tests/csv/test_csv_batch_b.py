"""Review batch B: empty projects, Config in CSV, formula-injection guard (G6, G7, finding 17)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from fixtures.builders import ProjectBuilder
from test_csv_io import FakeResult, worked_example

from project_planner.engine.config import Config
from project_planner.engine.csv_io import COLUMNS, export, parse
from project_planner.engine.errors import ImportFailed
from project_planner.engine.model import Project

pytestmark = pytest.mark.csv


def roundtrip(p: Project) -> Project:
    return parse(export(p)).project


# ------------------------------------------------------------------ G6


def test_empty_project_round_trips() -> None:
    p = ProjectBuilder().build()
    assert not p.nodes
    q = roundtrip(p)
    assert not q.nodes and q.start == p.start and q.calendar == p.calendar


def test_project_and_calendar_remain_mandatory() -> None:
    with pytest.raises(ImportFailed) as info:
        parse("record_type,schema_version\nRESOURCE,1\n")
    assert {i.code for i in info.value.issues} >= {"CSV_MISSING_PROJECT", "CSV_MISSING_CALENDAR"}


# ------------------------------------------------------------------ G7


def _project_150() -> Project:
    return (
        ProjectBuilder()
        .resource("r", rate=1)
        .task("t", duration="1d")
        .assign("t", "r", 150)
        .build()
    )


def test_parse_honours_max_assignment_percent() -> None:
    text = export(_project_150())
    with pytest.raises(ImportFailed) as info:
        parse(text)
    assert [i.code for i in info.value.issues] == ["CSV_OUT_OF_RANGE"]
    cfg = Config(max_assignment_percent=D(200))
    assert parse(text, config=cfg).project.assignments[0].percent == 150
    with pytest.raises(ImportFailed):
        parse(text, config=Config(max_assignment_percent=120))


def test_export_uses_config_rounding() -> None:
    p, result = worked_example()
    result = FakeResult(
        **{**result.__dict__, "cost_total": D("3600.12345"), "effort_days": D("5.4321")}
    )
    rows = {}
    for cfg in (Config(), Config(money_display_decimals=3, days_display_decimals=1)):
        lines = export(p, result, config=cfg).splitlines()
        rows[cfg.money_display_decimals] = next(x for x in lines if x.startswith("RESULT_PROJECT"))
    assert ",3600.12," in rows[2] and ",5.43," in rows[2]
    assert ",3600.123," in rows[3] and ",5.4," in rows[3]


def test_work_qty_and_rate_per_unit_keep_six_decimals_whatever_config() -> None:
    p, result = worked_example()
    result = FakeResult(**{**result.__dict__, "work_qty": D("0.0070005681")})
    text = export(p, result, config=Config(days_display_decimals=0, money_display_decimals=0))
    assert ",0.007001," in next(x for x in text.splitlines() if x.startswith("RESULT_PROJECT"))


def test_stale_incomplete_status_is_explicit() -> None:
    p, result = worked_example()
    text = export(p, FakeResult(**{**result.__dict__, "complete": False}), stale=True)
    line = next(x for x in text.splitlines() if x.startswith("RESULT_PROJECT"))
    assert line.split(",")[COLUMNS.index("schedule_status")] == "stale_incomplete"


# ------------------------------------------------------------------ finding 17

NASTY = [
    "=cmd|' /C calc'!A0",
    "+1",
    "-1",
    "@SUM(A1)",
    "\tTab",
    "\rCR",
    "'=already",
    "''-x",
    "'plain",
    "it's",
    "a=b",
    "- dash",
]


@pytest.mark.parametrize("name", NASTY)
def test_formula_text_round_trips_and_is_neutralised(name: str) -> None:
    p = ProjectBuilder().resource("r", name=name).task("t", name=name, duration="1d").build()
    text = export(p)
    for line in text.splitlines():
        for cell in line.split(","):
            assert not cell.startswith(("=", "+", "@", "-")), cell
    q = parse(text).project
    assert q.node("t").name == name
    assert q.resource("r").name == name


def test_injection_prefix_is_a_single_apostrophe() -> None:
    p = ProjectBuilder().task("t", name="=1+1", duration="1d").build()
    assert ",'=1+1," in export(p)


def test_ids_with_formula_characters_round_trip() -> None:
    p = (
        ProjectBuilder()
        .resource("-r", rate=1)
        .task("=t", duration="1d")
        .task("+u", duration="1d")
        .assign("=t", "-r", 50)
        .dep("=t", "+u", "FS")
        .build()
    )
    assert roundtrip(p) == p


def test_negative_lag_stays_a_plain_number() -> None:
    p = (
        ProjectBuilder()
        .task("a", duration="1d")
        .task("b", duration="1d")
        .dep("a", "b", "FS", lag="-2d")
        .build()
    )
    text = export(p)
    assert ",-2,d" in text and "'-2" not in text
    assert roundtrip(p) == p
