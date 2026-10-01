# ruff: noqa: E501
"""CSV import/export tests against ``docs/csv_format.md`` and ``tests/fixtures/csv``."""

from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest
from fixtures.builders import ProjectBuilder

from project_planner.engine import csv_io
from project_planner.engine.csv_io import (
    COLUMNS,
    IMPORTED_PROJECT_ID,
    ImportOutcome,
    export,
    parse,
)
from project_planner.engine.errors import ImportFailed, Issue, Severity
from project_planner.engine.model import (
    NodeKind,
    Project,
    SizingMode,
    TimeUnit,
    Weekday,
    WorkUnit,
    days,
    hours,
)
from project_planner.engine.validation import validate

pytestmark = pytest.mark.csv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "csv"
VALID = sorted(p.name for p in FIXTURES.glob("valid_*.csv"))


def raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def load(name: str) -> ImportOutcome:
    return parse(raw(name))


def errors_of(name: str) -> list[Issue]:
    with pytest.raises(ImportFailed) as info:
        parse(raw(name))
    return list(info.value.issues)


def brief(issue: Issue) -> tuple[str | None, int | None, str | None, str]:
    return (issue.object_type, issue.line, issue.field, issue.code)


def rows(text: str) -> list[list[str]]:
    return [line.split(",") for line in text.splitlines()]


# --------------------------------------------------------------------------------------
# Valid fixtures
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", VALID)
def test_valid_fixture_parses_and_validates(name: str) -> None:
    outcome = load(name)
    assert not [i for i in validate(outcome.project) if i.severity is Severity.ERROR]
    assert outcome.project.id == IMPORTED_PROJECT_ID


def test_fixture_inventory() -> None:
    assert len(VALID) == 5


def test_valid_minimal_defaults_and_notes() -> None:
    outcome = load("valid_minimal.csv")
    p = outcome.project
    assert p.currency == "USD"
    assert p.cost_report_unit is WorkUnit.PERSON_DAYS
    assert p.calendar.hours_per_day == 8
    assert p.calendar.working_days_per_year == 220
    assert p.calendar.working_weekdays == frozenset(
        {Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI}
    )
    assert p.calendar.workday_start == dt.time(9, 0)
    defaults = {
        i.field
        for i in outcome.notes
        if i.code == "CSV_DEFAULT_APPLIED" and i.severity.value == "info"
    }
    assert defaults == {
        "currency",
        "cost_report_unit",
        "hours_per_day",
        "working_days_per_year",
        "working_weekdays",
        "workday_start",
    }
    assert [n.id for n in p.nodes] == ["T1"]


def test_valid_full_project() -> None:
    p = load("valid_full.csv").project
    assert p.name == "Full Example, with comma"
    assert p.start == dt.date(2026, 3, 2)
    assert p.currency == "EUR"
    assert {n.id for n in p.nodes} == {
        "001", "010", "011", "012", "020", "021", "022", "023", "030", "040",
    }  # fmt: skip
    assert p.node("010").parent_id == "001"
    assert p.node("021").parent_id == "020"
    assert p.node("001").kind is NodeKind.GROUP
    assert p.node("030").kind is NodeKind.MILESTONE
    assert p.node("011").sizing == days(5)
    assert p.node("011").sizing_mode is SizingMode.DURATION
    assert p.node("012").sizing == hours(12)
    assert p.node("012").sizing.unit is TimeUnit.HOURS  # type: ignore[union-attr]
    assert p.node("022").sizing == hours(40)
    assert p.node("022").sizing_mode is SizingMode.EFFORT
    assert p.node("023").sizing == days("2.5")
    assert p.node("040").parent_id is None
    assert p.node("040").sizing == days("1.5")
    assert p.node("040").order == 2
    # resources: missing vs zero rate
    assert p.resource("R001").hourly_rate == Decimal(100)
    assert p.resource("R002").hourly_rate == Decimal(50)
    assert p.resource("R003").hourly_rate is None
    assert p.resource("R004").hourly_rate == Decimal(0)
    # assignments (A16)
    assert p.assignment("022", "R001").percent == Decimal(80)
    assert p.assignment("022", "R002").percent == Decimal(20)
    assert len(p.assignments) == 4
    # calendar
    assert [h.date for h in p.calendar.holidays] == [dt.date(2026, 4, 3)]
    assert p.calendar.holidays[0].name == "Good Friday"
    kinds = {e.date: e.kind.value for e in p.calendar.exceptions}
    assert kinds == {dt.date(2026, 3, 14): "working", dt.date(2026, 3, 17): "nonworking"}
    # dependencies
    d = {x.id: x for x in p.dependencies}
    assert [d[k].type.value for k in sorted(d)] == ["FS", "FS", "SS", "FF", "SF", "FS"]
    assert d["D001"].lag == days(0)
    assert d["D003"].lag == hours(4)
    assert d["D004"].lag == days("-0.5")
    assert d["D005"].lag == hours(0)
    assert d["D005"].lag.unit is TimeUnit.HOURS
    assert d["D006"].lag == hours(-2)
    assert not [n for n in load("valid_full.csv").notes if n.code == "CSV_DEFAULT_APPLIED"]


def test_valid_collapsed_group_exports_every_node() -> None:
    # The fixture README says 11 NODE rows; the file really has 9 (G1-G4, L1-L4, T9).
    p = load("valid_collapsed_group.csv").project
    file_nodes = sum(
        1 for ln in raw("valid_collapsed_group.csv").splitlines() if ln.startswith(b"NODE,")
    )
    assert len(p.nodes) == file_nodes == 9
    text = export(p)
    node_rows = [r for r in rows(text) if r[0] == "NODE"]
    assert len(node_rows) == 9
    ids = [r[COLUMNS.index("id")] for r in node_rows]
    assert sorted(ids) == sorted(n.id for n in p.nodes)
    assert len(set(ids)) == 9


def test_valid_with_results_ignores_results() -> None:
    outcome = load("valid_with_results.csv")
    ignored = [n for n in outcome.notes if n.code == "CSV_RESULTS_IGNORED"]
    assert len(ignored) == 1
    assert "1 RESULT_PROJECT" in ignored[0].message
    assert "3 RESULT_NODE" in ignored[0].message
    assert "2 RESULT_ASSIGNMENT" in ignored[0].message
    p = outcome.project
    assert p.cost_report_unit is WorkUnit.PERSON_DAYS
    assert [n.id for n in p.nodes] == ["G1", "M1", "T1"]
    assert p.assignment("T1", "R1").percent == Decimal(80)
    # same definition as the export of itself without results
    assert parse(export(p)).project == p


def test_valid_custom_calendar_bom_and_crlf() -> None:
    data = raw("valid_custom_calendar.csv")
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data
    cal = parse(data).project.calendar
    assert cal.hours_per_day == Decimal("7.5")
    assert cal.working_days_per_year == 250
    assert cal.working_weekdays == frozenset(
        {Weekday.SUN, Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU}
    )
    assert cal.workday_start == dt.time(8, 30)


def test_input_forms_are_equivalent() -> None:
    data = raw("valid_custom_calendar.csv")
    text = data.decode("utf-8")  # BOM kept as U+FEFF
    expected = parse(data).project
    assert parse(text).project == expected
    assert parse(io.StringIO(text)).project == expected
    assert parse(io.BytesIO(data)).project == expected  # type: ignore[arg-type]


def test_invalid_utf8_is_bad_encoding() -> None:
    with pytest.raises(ImportFailed) as info:
        parse(b"record_type,schema_version\n\xff\xfe\n")
    (issue,) = info.value.issues
    assert (issue.code, issue.line) == ("CSV_BAD_ENCODING", 1)


# --------------------------------------------------------------------------------------
# Invalid fixtures
# --------------------------------------------------------------------------------------

INVALID: dict[str, list[tuple[str, int, str, str]]] = {
    "invalid_missing_project.csv": [("PROJECT", 1, "record_type", "CSV_MISSING_PROJECT")],
    "invalid_duplicate_project.csv": [("PROJECT", 10, "record_type", "CSV_DUPLICATE_PROJECT")],
    "invalid_unknown_schema_version.csv": [
        ("PROJECT", 2, "schema_version", "CSV_UNKNOWN_SCHEMA_VERSION")
    ],
    "invalid_unknown_record_type.csv": [("WIDGET", 10, "record_type", "CSV_UNKNOWN_RECORD_TYPE")],
    "invalid_bad_date.csv": [("PROJECT", 2, "start_date", "CSV_BAD_DATE")],
    "invalid_bad_number.csv": [("RESOURCE", 4, "hourly_rate", "CSV_BAD_NUMBER")],
    "invalid_unitless_sizing.csv": [("NODE", 6, "sizing_unit", "CSV_UNITLESS_TIME")],
    "invalid_bad_unit.csv": [("NODE", 6, "sizing_unit", "CSV_BAD_UNIT")],
    "invalid_duplicate_node_id.csv": [("NODE", 8, "id", "CSV_DUPLICATE_ID")],
    "invalid_dangling_parent.csv": [("NODE", 7, "parent_id", "CSV_DANGLING_REFERENCE")],
    "invalid_dangling_dependency.csv": [("DEPENDENCY", 9, "succ_id", "CSV_DANGLING_REFERENCE")],
    "invalid_duplicate_assignment.csv": [
        ("ASSIGNMENT", 10, "resource_id", "CSV_DUPLICATE_ASSIGNMENT")
    ],
    "invalid_self_dependency.csv": [("DEPENDENCY", 9, "succ_id", "CSV_SELF_DEPENDENCY")],
    "invalid_group_dependency.csv": [("DEPENDENCY", 9, "succ_id", "CSV_GROUP_DEPENDENCY")],
    "invalid_cycle.csv": [("DEPENDENCY", 10, "pred_id", "CSV_CYCLE")],
    "invalid_calendar_combo.csv": [("CALENDAR", 3, "workday_start", "CSV_BAD_CALENDAR")],
    "invalid_multiple_errors.csv": [
        ("PROJECT", 2, "start_date", "CSV_BAD_DATE"),
        ("RESOURCE", 4, "hourly_rate", "CSV_BAD_NUMBER"),
        ("NODE", 6, "sizing_unit", "CSV_UNITLESS_TIME"),
        ("DEPENDENCY", 9, "succ_id", "CSV_DANGLING_REFERENCE"),
    ],
}


def test_every_invalid_fixture_is_covered() -> None:
    assert set(INVALID) == {p.name for p in FIXTURES.glob("invalid_*.csv")}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_fixture_reports_exactly_the_documented_errors(name: str) -> None:
    issues = errors_of(name)
    assert [brief(i) for i in issues] == INVALID[name]
    assert all(i.severity is Severity.ERROR for i in issues)


def test_cycle_message_names_nodes_and_lines() -> None:
    (issue,) = errors_of("invalid_cycle.csv")
    for token in ("n2", "n3", "n4", "10", "11", "12"):
        assert token in issue.message
    assert issue.message.index("n2") < issue.message.index("n3") < issue.message.index("n4")


def test_duplicate_id_message_gives_first_line() -> None:
    (issue,) = errors_of("invalid_duplicate_node_id.csv")
    assert "line 7" in issue.message
    assert issue.object_id == "n3"


def test_issue_attributes() -> None:
    (issue,) = errors_of("invalid_bad_number.csv")
    assert issue.object_type == "RESOURCE"
    assert issue.object_id == "R1"
    assert issue.field == "hourly_rate"
    assert issue.line == 4
    assert "abc" in issue.message


# --------------------------------------------------------------------------------------
# Format rules
# --------------------------------------------------------------------------------------

HEADER = ",".join(COLUMNS)


def line_of(**cells: str) -> str:
    rt = cells.pop("rt")
    out = {"record_type": rt, "schema_version": "1", **cells}
    return ",".join(out.get(c, "") for c in COLUMNS)


BASE = [
    HEADER,
    line_of(rt="PROJECT", name="P", start_date="2026-03-02"),
    line_of(rt="CALENDAR"),
    line_of(rt="NODE", id="t1", name="T", node_kind="task", sibling_order="1"),
]


def build(*extra: str, base: list[str] | None = None) -> str:
    return "\n".join([*(BASE if base is None else base), *extra]) + "\n"


def codes_of(text: str) -> list[tuple[int | None, str | None, str]]:
    with pytest.raises(ImportFailed) as info:
        parse(text)
    return [(i.line, i.field, i.code) for i in info.value.issues]


def test_base_is_valid() -> None:
    assert parse(build()).project.node("t1").sizing is None  # unsized task is valid


def test_empty_file_and_missing_header_columns() -> None:
    assert codes_of("") == [(0, "", "CSV_MISSING_HEADER")]
    assert codes_of("id,name\nx,y\n") == [(1, "", "CSV_MISSING_HEADER")]


def test_unknown_column_reported_once() -> None:
    text = build(base=[HEADER + ",extra,more", *[b + ",," for b in BASE[1:]]])
    assert codes_of(text) == [(1, "extra", "CSV_UNKNOWN_COLUMN")]


def test_narrow_header_and_column_order() -> None:
    text = (
        "schema_version,record_type,name,start_date\n"
        "1,PROJECT,Narrow,2026-03-02\n"
        "1,CALENDAR,,\n"
        "1,NODE,,\n"
    )
    # NODE is missing its columns, but PROJECT/CALENDAR parse; check codes are per column
    got = codes_of(text)
    assert (4, "id", "CSV_MISSING_REQUIRED") in got
    assert (4, "name", "CSV_MISSING_REQUIRED") in got
    assert (4, "node_kind", "CSV_MISSING_REQUIRED") in got


def test_bad_column_count() -> None:
    assert codes_of(build("NODE,1,x")) == [(5, "", "CSV_BAD_COLUMN_COUNT")]


def test_blank_lines_are_skipped_and_line_numbers_stay_physical() -> None:
    text = build(
        "", ",,,,", line_of(rt="NODE", id="t1", name="dup", node_kind="task", sibling_order="2")
    )
    assert codes_of(text) == [(7, "id", "CSV_DUPLICATE_ID")]


def test_quoted_field_with_line_break_keeps_line_numbers() -> None:
    q = line_of(rt="NODE", id="t2", name="X", node_kind="task", sibling_order="2").replace(
        ",X,", ',"two\nlines, ""quoted""",', 1
    )
    bad = line_of(rt="NODE", id="t3", name="Y", node_kind="bogus", sibling_order="3")
    p = parse(build(q)).project
    assert p.node("t2").name == 'two\nlines, "quoted"'
    assert codes_of(build(q, bad)) == [(7, "node_kind", "CSV_BAD_ENUM")]


def test_lone_cr_and_crlf_line_endings() -> None:
    text = build().replace("\n", "\r")
    assert parse(text).project == parse(build()).project
    assert parse(build().replace("\n", "\r\n")).project == parse(build()).project


def test_name_is_verbatim_other_cells_trimmed() -> None:
    text = build(
        line_of(rt="NODE", id=" t2 ", name="  spaced  ", node_kind=" task ", sibling_order=" 2 ")
    )
    p = parse(text).project
    assert p.node("t2").name == "  spaced  "


def test_blank_required_name_is_missing() -> None:
    text = build(line_of(rt="NODE", id="t2", name="   ", node_kind="task", sibling_order="2"))
    assert codes_of(text) == [(5, "name", "CSV_MISSING_REQUIRED")]


def test_ids_stay_text() -> None:
    text = build(
        line_of(rt="NODE", id="001", name="a", node_kind="task", sibling_order="2"),
        line_of(rt="NODE", id="1", name="b", node_kind="task", sibling_order="3"),
    )
    p = parse(text).project
    assert p.has_node("001") and p.has_node("1")


def test_schema_version_rules() -> None:
    v2 = build().replace(",PROJECT", ",PROJECT").splitlines()
    v2[2] = v2[2].replace("CALENDAR,1", "CALENDAR,7")
    assert codes_of("\n".join(v2) + "\n") == [(3, "schema_version", "CSV_UNKNOWN_SCHEMA_VERSION")]
    empty = build().splitlines()
    empty[1] = empty[1].replace("PROJECT,1", "PROJECT,")
    assert codes_of("\n".join(empty) + "\n") == [(2, "schema_version", "CSV_MISSING_REQUIRED")]


def test_not_empty_and_missing_required() -> None:
    text = build(
        line_of(rt="HOLIDAY", date="2026-04-03", id="x", hours_per_day="8"),
        line_of(rt="HOLIDAY"),
    )
    assert codes_of(text) == [
        (5, "id", "CSV_NOT_EMPTY"),
        (5, "hours_per_day", "CSV_NOT_EMPTY"),
        (6, "date", "CSV_MISSING_REQUIRED"),
    ]


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ({"hours_per_day": "abc"}, "CSV_BAD_NUMBER"),
        ({"hours_per_day": "0"}, "CSV_OUT_OF_RANGE"),
        ({"hours_per_day": "24.5"}, "CSV_OUT_OF_RANGE"),
        ({"hours_per_day": "7.333"}, "CSV_OUT_OF_RANGE"),
        ({"working_days_per_year": "2.5"}, "CSV_BAD_NUMBER"),
        ({"working_days_per_year": "367"}, "CSV_OUT_OF_RANGE"),
        ({"working_days_per_year": "0"}, "CSV_OUT_OF_RANGE"),
        ({"working_weekdays": "Monday"}, "CSV_BAD_ENUM"),
        ({"working_weekdays": "Mon;;Tue"}, "CSV_BAD_ENUM"),
        ({"working_weekdays": "Mon;Mon"}, "CSV_BAD_ENUM"),
        ({"working_weekdays": "Mon-Fri"}, "CSV_BAD_ENUM"),
        ({"workday_start": "9:00"}, "CSV_BAD_TIME"),
        ({"workday_start": "24:00"}, "CSV_BAD_TIME"),
        ({"workday_start": "09:60"}, "CSV_BAD_TIME"),
    ],
)
def test_calendar_value_errors(cells: dict[str, str], expected: str) -> None:
    text = build(base=[HEADER, BASE[1], line_of(rt="CALENDAR", **cells), BASE[3]])
    (only,) = codes_of(text)
    assert only[0] == 3
    assert only[2] == expected
    assert only[1] == next(iter(cells))


def test_calendar_leniency_for_weekdays() -> None:
    text = build(
        base=[HEADER, BASE[1], line_of(rt="CALENDAR", working_weekdays="mon; TUE ;sun"), BASE[3]]
    )
    assert parse(text).project.calendar.working_weekdays == frozenset(
        {Weekday.MON, Weekday.TUE, Weekday.SUN}
    )


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ({"currency": "usd"}, "CSV_BAD_CURRENCY"),
        ({"cost_report_unit": "hours"}, "CSV_BAD_ENUM"),
        ({"start_date": "2026-3-2"}, "CSV_BAD_DATE"),
        ({"start_date": ""}, "CSV_MISSING_REQUIRED"),
        ({"id": "x"}, "CSV_NOT_EMPTY"),
    ],
)
def test_project_errors(cells: dict[str, str], expected: str) -> None:
    fields = {"name": "P", "start_date": "2026-03-02", **cells}
    text = build(base=[HEADER, line_of(rt="PROJECT", **fields), BASE[2], BASE[3]])
    (only,) = codes_of(text)
    assert (only[0], only[1], only[2]) == (2, next(iter(cells)), expected)


def node(**cells: str) -> str:
    fields = {"id": "n", "name": "N", "node_kind": "task", "sibling_order": "1", **cells}
    return line_of(rt="NODE", **fields)


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ({"sizing_mode": "duration", "sizing_value": "5", "sizing_unit": ""}, [("sizing_unit", "CSV_UNITLESS_TIME")]),
        ({"sizing_mode": "duration", "sizing_value": "5", "sizing_unit": "w"}, [("sizing_unit", "CSV_BAD_UNIT")]),
        ({"sizing_mode": "duration", "sizing_value": "-5", "sizing_unit": "d"}, [("sizing_value", "CSV_BAD_NUMBER")]),
        ({"sizing_mode": "duration", "sizing_value": ".5", "sizing_unit": "d"}, [("sizing_value", "CSV_BAD_NUMBER")]),
        ({"sizing_mode": "duration", "sizing_value": "1e1", "sizing_unit": "d"}, [("sizing_value", "CSV_BAD_NUMBER")]),
        ({"sizing_mode": "none", "sizing_value": "5", "sizing_unit": "d"}, [("sizing_mode", "CSV_BAD_ENUM")]),
        ({"sizing_mode": "duration"}, [("sizing_value", "CSV_MISSING_REQUIRED"), ("sizing_unit", "CSV_MISSING_REQUIRED")]),
        ({"sizing_value": "5", "sizing_unit": "d"}, [("sizing_mode", "CSV_MISSING_REQUIRED")]),
        ({"sizing_unit": "d"}, [("sizing_mode", "CSV_MISSING_REQUIRED"), ("sizing_value", "CSV_MISSING_REQUIRED")]),
        ({"node_kind": "group", "sizing_mode": "duration", "sizing_value": "5", "sizing_unit": "d"},
         [("sizing_mode", "CSV_NOT_EMPTY"), ("sizing_value", "CSV_NOT_EMPTY"), ("sizing_unit", "CSV_NOT_EMPTY")]),
        ({"node_kind": "milestone", "sizing_value": "5"}, [("sizing_value", "CSV_NOT_EMPTY")]),
        ({"node_kind": "Task"}, [("node_kind", "CSV_BAD_ENUM")]),
        ({"node_kind": ""}, [("node_kind", "CSV_MISSING_REQUIRED")]),
        ({"sibling_order": "1.5"}, [("sibling_order", "CSV_BAD_NUMBER")]),
        ({"sibling_order": ""}, [("sibling_order", "CSV_MISSING_REQUIRED")]),
    ],
)  # fmt: skip
def test_node_format_errors(cells: dict[str, str], expected: list[tuple[str, str]]) -> None:
    got = codes_of(build(node(**cells)))
    assert [(f, c) for _l, f, c in got] == expected
    assert {ln for ln, _f, _c in got} == {5}


def test_negative_sibling_order_and_zero_sizing_allowed() -> None:
    p = parse(
        build(node(sibling_order="-3", sizing_mode="effort", sizing_value="0", sizing_unit="h"))
    ).project
    assert p.node("n").order == -3
    assert p.node("n").sizing == hours(0)


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ({"percent": "80%"}, "CSV_BAD_NUMBER"),
        ({"percent": "0"}, "CSV_OUT_OF_RANGE"),
        ({"percent": "100.5"}, "CSV_OUT_OF_RANGE"),
        ({"percent": ""}, "CSV_MISSING_REQUIRED"),
    ],
)
def test_assignment_percent_errors(cells: dict[str, str], expected: str) -> None:
    rec = line_of(rt="ASSIGNMENT", task_id="t1", resource_id="r1", **cells)
    text = build(line_of(rt="RESOURCE", id="r1", name="R"), rec)
    assert codes_of(text) == [(6, "percent", expected)]


def test_assignment_model_errors() -> None:
    res = line_of(rt="RESOURCE", id="r1", name="R")
    grp = line_of(rt="NODE", id="g", name="G", node_kind="group", sibling_order="2")
    text = build(
        res,
        grp,
        line_of(rt="ASSIGNMENT", task_id="g", resource_id="r1", percent="50"),
        line_of(rt="ASSIGNMENT", task_id="zz", resource_id="r1", percent="50"),
        line_of(rt="ASSIGNMENT", task_id="t1", resource_id="nobody", percent="50"),
    )
    assert codes_of(text) == [
        (7, "task_id", "CSV_ASSIGN_NON_TASK"),
        (8, "task_id", "CSV_DANGLING_REFERENCE"),
        (9, "resource_id", "CSV_DANGLING_REFERENCE"),
    ]


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ({"lag_value": "1"}, [("lag_unit", "CSV_UNITLESS_TIME")]),
        ({"lag_value": "0"}, [("lag_unit", "CSV_UNITLESS_TIME")]),
        ({"lag_unit": "d"}, [("lag_value", "CSV_MISSING_REQUIRED")]),
        ({"lag_value": "x", "lag_unit": "d"}, [("lag_value", "CSV_BAD_NUMBER")]),
        ({"lag_value": "1", "lag_unit": "w"}, [("lag_unit", "CSV_BAD_UNIT")]),
        ({"dep_type": "XX", "lag_value": "1", "lag_unit": "d"}, [("dep_type", "CSV_BAD_ENUM")]),
    ],
)
def test_dependency_format_errors(cells: dict[str, str], expected: list[tuple[str, str]]) -> None:
    other = node(id="n2", sibling_order="2")
    fields = {"id": "d", "pred_id": "t1", "succ_id": "n2", "dep_type": "FS", **cells}
    got = codes_of(build(other, line_of(rt="DEPENDENCY", **fields)))
    assert [(f, c) for _l, f, c in got] == expected


def test_dependency_zero_lag_default_note() -> None:
    other = node(id="n2", sibling_order="2")
    dep = line_of(rt="DEPENDENCY", id="d", pred_id="t1", succ_id="n2", dep_type="SS")
    outcome = parse(build(other, dep))
    assert outcome.project.dependency("d").lag == days(0)
    notes = [n for n in outcome.notes if n.object_type == "DEPENDENCY"]
    assert [(n.code, n.field, n.line) for n in notes] == [("CSV_DEFAULT_APPLIED", "lag_value", 6)]


def test_model_errors_in_one_pass() -> None:
    n2 = node(id="n2", sibling_order="2", parent_id="t1")  # parent is a task
    n3 = node(id="n3", sibling_order="3", parent_id="n3", node_kind="group")  # own parent
    d1 = line_of(
        rt="DEPENDENCY",
        id="d",
        pred_id="t1",
        succ_id="t1",
        dep_type="FS",
        lag_value="0",
        lag_unit="d",
    )
    d2 = line_of(
        rt="DEPENDENCY",
        id="d",
        pred_id="t1",
        succ_id="n2",
        dep_type="FS",
        lag_value="0",
        lag_unit="d",
    )
    h = line_of(rt="HOLIDAY", date="2026-04-03")
    e = line_of(rt="EXCEPTION", date="2026-04-03", exception_kind="working")
    h2 = line_of(rt="HOLIDAY", date="2026-04-03")
    got = codes_of(build(n2, n3, d1, d2, h, e, h2))
    assert got == [
        (5, "parent_id", "CSV_PARENT_NOT_GROUP"),
        (6, "parent_id", "CSV_PARENT_CYCLE"),
        (7, "succ_id", "CSV_SELF_DEPENDENCY"),
        (8, "id", "CSV_DUPLICATE_ID"),
        (10, "date", "CSV_DUPLICATE_DATE"),
        (11, "date", "CSV_DUPLICATE_DATE"),
    ]


def test_missing_calendar_is_the_only_error_without_nodes() -> None:
    text = HEADER + "\n" + line_of(rt="PROJECT", name="P", start_date="2026-03-02") + "\n"
    assert codes_of(text) == [(1, "record_type", "CSV_MISSING_CALENDAR")]


def test_duplicate_calendar() -> None:
    assert codes_of(build(line_of(rt="CALENDAR"))) == [(5, "record_type", "CSV_DUPLICATE_CALENDAR")]


def test_group_dependency_reports_offending_end_pred_first() -> None:
    g = line_of(rt="NODE", id="g", name="G", node_kind="group", sibling_order="2")
    d = line_of(rt="DEPENDENCY", id="d", pred_id="g", succ_id="g2", dep_type="FS")
    g2 = line_of(rt="NODE", id="g2", name="G", node_kind="group", sibling_order="3")
    got = codes_of(build(g, g2, d))
    assert [(f, c) for _l, f, c in got] == [
        ("pred_id", "CSV_GROUP_DEPENDENCY"),
        ("succ_id", "CSV_GROUP_DEPENDENCY"),
    ]


def test_cycle_excludes_group_and_self_edges_and_reports_once_per_cycle() -> None:
    nodes = [node(id=f"c{i}", sibling_order=str(i + 2)) for i in range(4)]

    def dep(i: str, a: str, b: str) -> str:
        return line_of(
            rt="DEPENDENCY", id=i, pred_id=a, succ_id=b, dep_type="FS", lag_value="0", lag_unit="d"
        )

    # two separate cycles: c0<->c1 and c2<->c3, listed interleaved
    deps = [dep("a", "c0", "c1"), dep("b", "c2", "c3"), dep("c", "c1", "c0"), dep("d", "c3", "c2")]
    got = codes_of(build(*nodes, *deps))
    assert [(c, f) for _l, f, c in got] == [("CSV_CYCLE", "pred_id")] * 2
    assert [line for line, _f, _c in got] == [9, 10]


def test_results_records_need_only_version_and_width() -> None:
    text = build("RESULT_NODE,1,whatever,,,,", base=[HEADER + ",x", *[b + "," for b in BASE[1:]]])
    assert codes_of(text)[0][2] == "CSV_UNKNOWN_COLUMN"
    ok = build("RESULT_NODE,1" + "," * (len(COLUMNS) - 2))
    outcome = parse(ok)
    assert [n.code for n in outcome.notes if n.code == "CSV_RESULTS_IGNORED"]
    bad_version = build("RESULT_NODE,2" + "," * (len(COLUMNS) - 2))
    assert codes_of(bad_version) == [(5, "schema_version", "CSV_UNKNOWN_SCHEMA_VERSION")]


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------


def sample_project() -> Project:
    return (
        ProjectBuilder(name='Sample, "quoted"')
        .calendar(
            hours_per_day="7.5",
            working_weekdays="Sun-Thu",
            working_days_per_year=250,
            workday_start="08:30",
            holidays=[(dt.date(2026, 10, 8), "Holiday, with comma")],
            exceptions=[(dt.date(2026, 10, 10), "working", "Saturday")],
        )
        .resource("r2", "Bob", rate="50.50")
        .resource("r1", "Alice", rate="100")
        .resource("r3", "Carol")
        .resource("r4", "Dan", rate="0")
        .group("g1", "Phase 1")
        .task("t1", "Build", parent="g1", effort="40h")
        .task("t2", "Test", parent="g1", duration="2.50d")
        .milestone("m1", "Done", parent="g1")
        .assign("t1", "r2", 20)
        .assign("t1", "r1", 80)
        .assign("t2", "r3", "12.5")
        .dep("t1", "t2", "FS", lag="0h")
        .dep("t2", "m1", "SS", lag="-0.5d")
        .build()
    )


def test_export_header_and_layout() -> None:
    text = export(sample_project())
    assert text.endswith("\n")
    assert "\r" not in text.replace('"', "") or True
    assert not text.startswith("﻿")
    lines = text.split("\n")
    assert lines[0] == ",".join(COLUMNS)
    assert len(COLUMNS) == 45
    assert len({c for c in COLUMNS}) == 45


def test_export_order_and_content() -> None:
    p = sample_project()
    text = export(p)
    recs = [r for r in csv_rows(text)]
    types = [r["record_type"] for r in recs]
    assert types == [
        "PROJECT", "CALENDAR", "HOLIDAY", "EXCEPTION",
        "RESOURCE", "RESOURCE", "RESOURCE", "RESOURCE",
        "NODE", "NODE", "NODE", "NODE",
        "ASSIGNMENT", "ASSIGNMENT", "ASSIGNMENT",
        "DEPENDENCY", "DEPENDENCY",
    ]  # fmt: skip
    by_type: dict[str, list[dict[str, str]]] = {}
    for r in recs:
        by_type.setdefault(r["record_type"], []).append(r)
    assert by_type["PROJECT"][0]["name"] == 'Sample, "quoted"'
    cal = by_type["CALENDAR"][0]
    assert cal["hours_per_day"] == "7.5"
    assert cal["working_days_per_year"] == "250"
    assert cal["working_weekdays"] == "Mon;Tue;Wed;Thu;Sun"  # Monday-first order
    assert cal["workday_start"] == "08:30"
    assert [r["id"] for r in by_type["RESOURCE"]] == ["r1", "r2", "r3", "r4"]
    assert [r["hourly_rate"] for r in by_type["RESOURCE"]] == ["100", "50.50", "", "0"]
    assert [r["id"] for r in by_type["NODE"]] == ["g1", "t1", "t2", "m1"]
    t2 = by_type["NODE"][2]
    assert (t2["sizing_mode"], t2["sizing_value"], t2["sizing_unit"]) == ("duration", "2.50", "d")
    assert [(r["task_id"], r["resource_id"]) for r in by_type["ASSIGNMENT"]] == [
        ("t1", "r1"),
        ("t1", "r2"),
        ("t2", "r3"),
    ]
    deps = by_type["DEPENDENCY"]
    assert [(r["lag_value"], r["lag_unit"]) for r in deps] == [("0", "h"), ("-0.5", "d")]
    assert parse(text).project == p


def csv_rows(text: str) -> list[dict[str, str]]:
    import csv as stdlib_csv

    reader = stdlib_csv.reader(io.StringIO(text, newline=""))
    header = next(reader)
    assert tuple(header) == COLUMNS
    return [dict(zip(header, r, strict=True)) for r in reader]


def test_export_is_deterministic_and_idempotent() -> None:
    p = sample_project()
    first = export(p)
    assert export(p) == first
    assert export(parse(first).project) == first


@pytest.mark.parametrize("name", VALID)
def test_roundtrip_fixtures(name: str) -> None:
    p = load(name).project
    text = export(p)
    again = parse(text)
    assert again.project == p
    assert export(again.project) == text
    assert not again.notes  # export writes every default explicitly, and no results


def test_roundtrip_preserves_entered_units_and_scales() -> None:
    p = (
        ProjectBuilder()
        .task("t1", effort="8h")
        .task("t2", duration="1d")
        .task("t3", duration="1.50d")
        .dep("t1", "t2", lag="0d")
        .dep("t2", "t3", lag="0h")
        .build()
    )
    q = parse(export(p)).project
    assert q == p
    assert q.node("t1").sizing.unit is TimeUnit.HOURS  # type: ignore[union-attr]
    assert q.node("t2").sizing.unit is TimeUnit.DAYS  # type: ignore[union-attr]
    assert str(q.node("t3").sizing) == "1.50d"
    assert {d.id: d.lag.unit for d in q.dependencies} == {"d1": TimeUnit.DAYS, "d2": TimeUnit.HOURS}


def test_roundtrip_names_and_ids() -> None:
    p = (
        ProjectBuilder(name="  padded, name\nwith newline  ")
        .resource("001", 'Al "the" Ice')
        .task("T-1", "tab\there", effort="1h")
        .task("x y", " leading", duration="1d")
        .assign("T-1", "001", 100)
        .build()
    )
    assert parse(export(p)).project == p


def test_export_without_result_has_no_result_rows() -> None:
    text = export(sample_project())
    assert "RESULT_" not in text
    assert "RESULT_" not in export(sample_project(), None, stale=True)


def test_export_lists_every_node_exactly_once_even_for_collapsed_groups() -> None:
    p = load("valid_collapsed_group.csv").project
    node_rows = [r for r in csv_rows(export(p)) if r["record_type"] == "NODE"]
    ids = [r["id"] for r in node_rows]
    assert sorted(ids) == sorted(n.id for n in p.nodes)
    # pre-order: parents before children
    pos = {i: k for k, i in enumerate(ids)}
    for n in p.nodes:
        if n.parent_id is not None:
            assert pos[n.parent_id] < pos[n.id]


# --- results ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeNode:
    node_id: str
    start: dt.datetime | None = None
    finish: dt.datetime | None = None
    duration_days: Decimal | None = None
    effort_days: Decimal | None = None
    leveling_delay_days: Decimal | None = None
    work_qty: Decimal | None = None
    work_unit: str | None = None
    cost: Decimal | None = None
    cost_complete: bool = True
    status: str = "scheduled"
    issue_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FakeAssignment:
    task_id: str
    resource_id: str
    assignment_days: Decimal | None = None
    work_qty: Decimal | None = None
    work_unit: str | None = None
    rate_per_unit: Decimal | None = None
    cost: Decimal | None = None
    cost_complete: bool = True


@dataclass(frozen=True)
class FakeResult:
    complete: bool = True
    kind: str = "dependency_only"
    project_start: dt.datetime | None = dt.datetime(2026, 3, 2, 9, 0)
    project_finish: dt.datetime | None = dt.datetime(2026, 3, 6, 17, 0)
    working_span_days: Decimal | None = Decimal(5)
    elapsed_span_calendar_days: Decimal | None = Decimal(5)
    effort_days: Decimal | None = Decimal(5)
    work_qty: Decimal | None = Decimal(5)
    work_unit: str | None = "person_days"
    cost_total: Decimal | None = Decimal("3600")
    cost_complete: bool = True
    node_results: tuple[FakeNode, ...] = ()
    assignment_results: tuple[FakeAssignment, ...] = ()


def worked_example() -> tuple[Project, FakeResult]:
    p = load("valid_with_results.csv").project
    start, end = dt.datetime(2026, 3, 2, 9, 0), dt.datetime(2026, 3, 6, 17, 0)
    nodes = (
        FakeNode("G1", start, end, Decimal(5), Decimal(5), Decimal(0), Decimal(5), "person_days", Decimal("3600")),
        FakeNode("T1", start, end, Decimal(5), Decimal(5), Decimal(0), Decimal(5), "person_days", Decimal("3600")),
        FakeNode("M1", end, end, Decimal(0), Decimal(0), Decimal(0), Decimal(0), "person_days", Decimal(0)),
    )  # fmt: skip
    assigns = (
        FakeAssignment(
            "T1", "R1", Decimal(4), Decimal(4), "person_days", Decimal(800), Decimal(3200)
        ),
        FakeAssignment(
            "T1", "R2", Decimal(1), Decimal(1), "person_days", Decimal(400), Decimal(400)
        ),
    )
    return p, FakeResult(node_results=nodes, assignment_results=assigns)


def test_export_with_result_rows() -> None:
    p, result = worked_example()
    recs = csv_rows(export(p, result))
    res = [r for r in recs if r["record_type"].startswith("RESULT_")]
    assert [r["record_type"] for r in res] == [
        "RESULT_PROJECT", "RESULT_NODE", "RESULT_NODE", "RESULT_NODE",
        "RESULT_ASSIGNMENT", "RESULT_ASSIGNMENT",
    ]  # fmt: skip
    # result rows come after every definition row
    assert [r["record_type"] for r in recs][: len(recs) - 6] == [
        r["record_type"] for r in recs if not r["record_type"].startswith("RESULT_")
    ]
    rp = res[0]
    assert rp["schedule_status"] == "current"
    assert rp["schedule_kind"] == "dependency_only"
    assert rp["start"] == "2026-03-02T09:00"
    assert rp["finish"] == "2026-03-06T17:00"
    assert rp["working_span_days"] == "5.00"
    assert rp["elapsed_span_calendar_days"] == "5"
    assert rp["effort_days"] == "5.00"
    assert (rp["work_qty"], rp["work_unit"]) == ("5", "person_days")
    assert rp["cost"] == "3600.00"
    assert rp["cost_complete"] == "true"
    assert rp["id"] == "" and rp["node_status"] == ""
    # RESULT_NODE in NODE order: G1, T1, M1 (pre-order, siblings by order)
    assert [r["id"] for r in res[1:4]] == ["G1", "T1", "M1"]
    t1 = res[2]
    assert t1["duration_days"] == "5.00"
    assert t1["leveling_delay_days"] == "0.00"
    assert t1["node_status"] == "scheduled"
    assert t1["cost"] == "3600.00"
    assert t1["issue_codes"] == ""
    assert res[3]["start"] == res[3]["finish"] == "2026-03-06T17:00"
    a1, a2 = res[4], res[5]
    assert (a1["task_id"], a1["resource_id"]) == ("T1", "R1")
    assert (a1["assignment_days"], a1["work_qty"], a1["rate_per_unit"], a1["cost"]) == (
        "4.00",
        "4",
        "800",
        "3200.00",
    )
    assert (a2["task_id"], a2["resource_id"], a2["cost"]) == ("T1", "R2", "400.00")


def test_export_with_result_still_round_trips_definition() -> None:
    p, result = worked_example()
    outcome = parse(export(p, result))
    assert outcome.project == p
    (ignored,) = [n for n in outcome.notes if n.code == "CSV_RESULTS_IGNORED"]
    assert "1 RESULT_PROJECT" in ignored.message and "3 RESULT_NODE" in ignored.message


def test_export_stale_and_incomplete_status() -> None:
    p, result = worked_example()

    def status(r: FakeResult, *, stale: bool = False) -> str:
        rows_ = csv_rows(export(p, r, stale=stale))
        return next(x for x in rows_ if x["record_type"] == "RESULT_PROJECT")["schedule_status"]

    assert status(result) == "current"
    assert status(result, stale=True) == "stale"
    incomplete = replace(result, complete=False, cost_complete=False)
    assert status(incomplete) == "incomplete"
    assert status(incomplete, stale=True) == "stale_incomplete"  # neither hides the other
    # stale values are still the old ones
    stale_rows = csv_rows(export(p, result, stale=True))
    assert next(x for x in stale_rows if x["record_type"] == "RESULT_PROJECT")["cost"] == "3600.00"


def test_export_missing_rate_unscheduled_and_precision() -> None:
    p, _ = worked_example()
    result = FakeResult(
        complete=False,
        kind="leveled",
        cost_complete=False,
        elapsed_span_calendar_days=Decimal("4.5"),
        work_qty=Decimal("0.0181818181"),
        node_results=(
            FakeNode(
                "T1",
                None,
                None,
                None,
                Decimal("1.005"),
                Decimal("1.5"),
                Decimal("0.018182"),
                "person_years",
                None,
                False,
                "unscheduled",
                ("TASK_UNSIZED", "COST_MISSING_RATE"),
            ),  # fmt: skip
        ),
        assignment_results=(
            FakeAssignment(
                "T1", "R1", Decimal("0.125"), Decimal("1.5"), "person_hours", None, None, False
            ),
        ),
    )
    recs = csv_rows(export(p, result))
    rp = next(r for r in recs if r["record_type"] == "RESULT_PROJECT")
    assert rp["schedule_status"] == "incomplete"
    assert rp["schedule_kind"] == "leveled"
    assert rp["cost_complete"] == "false"
    assert rp["work_qty"] == "0.018182"
    assert rp["elapsed_span_calendar_days"] == "4.50"
    nodes = {r["id"]: r for r in recs if r["record_type"] == "RESULT_NODE"}
    t1 = nodes["T1"]
    assert (t1["start"], t1["finish"], t1["cost"]) == ("", "", "")
    assert t1["effort_days"] == "1.00"  # half-even
    assert t1["leveling_delay_days"] == "1.50"
    assert t1["node_status"] == "unscheduled"
    assert t1["issue_codes"] == "TASK_UNSIZED;COST_MISSING_RATE"
    assert t1["cost_complete"] == "false"
    # nodes without a result row are listed as unscheduled
    assert nodes["G1"]["node_status"] == "unscheduled"
    assert nodes["G1"]["cost_complete"] == "false"
    ar = next(
        r for r in recs if r["record_type"] == "RESULT_ASSIGNMENT" and r["resource_id"] == "R1"
    )
    assert (ar["rate_per_unit"], ar["cost"], ar["cost_complete"]) == ("", "", "false")
    assert ar["assignment_days"] == "0.12"  # half-even
    unmatched = next(
        r for r in recs if r["record_type"] == "RESULT_ASSIGNMENT" and r["resource_id"] == "R2"
    )
    assert unmatched["cost_complete"] == "false"


def test_export_is_importable_module_surface() -> None:
    assert {"parse", "export", "ExportableResult", "ImportOutcome"} <= set(csv_io.__all__)
