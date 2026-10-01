"""A26, A27 -- calendar initialisation (plan 2.3) and hours/day changes (plan 2.1).

7.5 h/day calendar, workday start 09:00 -> working day 09:00-16:30 = 450 minutes.
Repeating day values (40 h / 7.5 h = 5.333...) are compared EXACTLY against
Decimal(2400) / Decimal(450), i.e. axis_minutes / minutes_per_day as plan 2.1
defines; the 2-decimal display value (5.33) is checked as well.
"""

from __future__ import annotations

from datetime import date

import pytest

import project_planner as pp

from .conftest import (
    D,
    calendar_settings,
    code,
    issue_fields,
    new_project,
    new_ws,
    node_row,
    oct26,
    project_id_by_name,
    q,
    report_assignment,
    sizing_of,
)


def _two_tasks(ws):
    alice = ws.add_resource("Alice", hourly_rate="100")
    t5d = ws.add_task("Five days", duration="5d")
    t40h = ws.add_task("Forty hours", duration=pp.hours(40))
    ws.set_assignment(t5d, alice, percent=100)
    return alice, t5d, t40h


# ---------------------------------------------------------------- A26


@pytest.mark.acceptance
def test_a26_seven_and_a_half_hour_days():
    ws = new_ws()
    new_project(ws)
    ws.calendar.initialize(hours_per_day=D("7.5"), working_days_per_year=250)
    alice, t5d, t40h = _two_tasks(ws)
    res = ws.schedule()
    # 5 d x 7.5 h = 37.5 h = 2250 min = 5 x 450 -> Mon 5 09:00 - Fri 9 16:30
    assert node_row(res, t5d).duration_days == D(5)
    assert node_row(res, t5d).start == oct26(5, "09:00")
    assert node_row(res, t5d).finish == oct26(9, "16:30")
    # 40 h = 2400 min = 5 days (2250) + 150 min -> Mon 12 09:00 + 2 h 30 = 11:30
    assert node_row(res, t40h).finish == oct26(12, "11:30")
    # exact: 2400 / 450 = 5.333... (Decimal division), displayed 5.33
    assert node_row(res, t40h).duration_days == D(2400) / D(450)
    assert node_row(res, t40h).duration_days.quantize(D("0.01")) == D("5.33")
    rep_h = ws.cost_report(unit="person_hours")
    # 37.5 person-hours x 100 = 3750
    assert report_assignment(rep_h, t5d, alice).work_qty == D("37.5")
    assert report_assignment(rep_h, t5d, alice).cost == D(3750)
    rep_y = ws.cost_report(unit="person_years")
    # 1 person-year = 7.5 x 250 = 1875 h; 37.5 / 1875 = 0.02 py; rate 100 x 1875 = 187500
    assert report_assignment(rep_y, t5d, alice).work_qty == D("0.02")
    assert report_assignment(rep_y, t5d, alice).rate_per_unit == D(187500)
    assert rep_y.total_cost == D(3750)
    # entered units untouched
    assert sizing_of(ws, t5d) == (D(5), "days")
    assert sizing_of(ws, t40h) == (D(40), "hours")


@pytest.mark.acceptance
def test_a26_working_days_per_year_changes_person_years_only():
    def build(wdpy):
        ws = new_ws()
        new_project(ws)
        ws.calendar.initialize(hours_per_day=D("7.5"), working_days_per_year=wdpy)
        alice, t5d, t40h = _two_tasks(ws)
        res = ws.schedule()
        rows = [(node_row(res, t).start, node_row(res, t).finish) for t in (t5d, t40h)]
        days = report_assignment(ws.cost_report(unit="person_days"), t5d, alice)
        years = report_assignment(ws.cost_report(unit="person_years"), t5d, alice)
        return rows, res.total_cost, (days.work_qty, days.rate_per_unit), years

    rows220, cost220, days220, years220 = build(220)
    rows250, cost250, days250, years250 = build(250)
    # dates, costs and person-day figures identical (5 pd x 750 = 3750)
    assert rows220 == rows250
    assert cost220 == cost250 == D(3750)
    assert days220 == days250 == (D(5), D(750))
    # person-years: 37.5 / (7.5 x 220 = 1650) vs 37.5 / 1875 = 0.02
    assert q(years220.work_qty) == q(D("37.5") / D(1650))
    assert years220.rate_per_unit == D(165000)
    assert years250.work_qty == D("0.02")
    assert years250.rate_per_unit == D(187500)
    assert years220.cost == years250.cost == D(3750)


@pytest.mark.acceptance
def test_a26_calendar_settings_passed_to_new_project():
    ws = new_ws()
    settings = pp.CalendarSettings(hours_per_day=D("7.5"), working_days_per_year=250)
    new_project(ws, calendar=settings)
    assert calendar_settings(ws) == settings
    _, t5d, _ = _two_tasks(ws)
    # same as initialize(): 5 x 450 min -> Fri 9 16:30
    assert node_row(ws.schedule(), t5d).finish == oct26(9, "16:30")


@pytest.mark.acceptance
def test_hours_per_day_change_8_to_7_5():
    ws = new_ws()
    new_project(ws)
    _, t5d, t40h = _two_tasks(ws)
    res = ws.schedule()
    # at 8 h/day both are 2400 min = 5 days, finish Fri 9 17:00
    assert node_row(res, t5d).duration_days == node_row(res, t40h).duration_days == D(5)
    assert node_row(res, t40h).finish == oct26(9, "17:00")
    ws.calendar.set_hours_per_day(D("7.5"))
    assert ws.state().stale_dates is True
    res = ws.schedule()
    # 5d stays 5 working days (now 37.5 h); 40h stays 40 h = 2400 / 450 days
    assert node_row(res, t5d).duration_days == D(5)
    assert node_row(res, t40h).duration_days == D(2400) / D(450)
    assert node_row(res, t5d).finish == oct26(9, "16:30")
    assert node_row(res, t40h).finish == oct26(12, "11:30")
    assert sizing_of(ws, t5d) == (D(5), "days")
    assert sizing_of(ws, t40h) == (D(40), "hours")


@pytest.mark.acceptance
def test_a26_settings_survive_save_and_load(tmp_path):
    db = tmp_path / "cal.db"
    ws = new_ws(db)
    new_project(ws, "Cal")
    ws.calendar.initialize(
        hours_per_day=D("7.5"),
        working_days_per_year=250,
        workday_start="08:30",
        holidays=[(date(2026, 10, 12), "Holiday")],
        exceptions=[(date(2026, 10, 10), "working")],
    )
    before = calendar_settings(ws)
    ws.save()
    ws.close()
    ws2 = new_ws(db)
    ws2.load(project_id_by_name(ws2, "Cal"))
    assert calendar_settings(ws2) == before
    assert calendar_settings(ws2).working_days_per_year == 250
    assert calendar_settings(ws2).hours_per_day == D("7.5")
    ws2.close()


@pytest.mark.acceptance
def test_a26_settings_survive_csv_round_trip(tmp_path):
    ws = new_ws()
    new_project(ws, "Cal")
    ws.calendar.initialize(
        hours_per_day=D("7.5"),
        working_days_per_year=250,
        workday_start="08:30",
        holidays=[(date(2026, 10, 12), "Holiday")],
        exceptions=[(date(2026, 10, 10), "working")],
    )
    # the CSV format requires at least one NODE record (docs/csv_format.md, CSV_MISSING_NODE)
    ws.add_task("Task", duration="1d")
    path = tmp_path / "cal.csv"
    ws.export_csv(str(path))
    ws2 = new_ws()
    ws2.import_csv(str(path))
    assert calendar_settings(ws2) == calendar_settings(ws)
    # project setting travels too
    assert code(ws2.project().cost_report_unit) == "person_days"


# ---------------------------------------------------------------- A27


@pytest.mark.acceptance
def test_initialize_without_arguments_gives_defaults():
    ws = new_ws()
    new_project(ws)
    ws.calendar.initialize()
    s = calendar_settings(ws)
    assert s == pp.CalendarSettings()
    # 8 h/day, 220 days/year, 09:00 start, no holidays / exceptions, Mon-Fri
    assert s.hours_per_day == 8
    assert s.working_days_per_year == 220
    assert str(s.workday_start)[:5] == "09:00"
    assert len(s.holidays) == 0
    assert len(s.exceptions) == 0
    t = ws.add_task("T", duration="5d")
    # Mon-Fri, 8 h from 09:00 -> Mon 5 09:00 - Fri 9 17:00
    assert node_row(ws.schedule(), t).finish == oct26(9, "17:00")


def _initialised_nondefault(ws):
    ws.calendar.initialize(hours_per_day=D("7.5"), holidays=[(date(2026, 10, 12), "H")])
    return calendar_settings(ws), ws.state().revision


@pytest.mark.acceptance
def test_a27_invalid_combination_applies_nothing_and_names_each_field():
    ws = new_ws()
    new_project(ws)
    before, rev = _initialised_nondefault(ws)
    with pytest.raises(pp.ValidationFailed) as ei:
        # 20:00 + 8 h = 28:00 > 24:00 (cross-field rule reported on workday_start);
        # 400 > 366 working days per year
        ws.calendar.initialize(hours_per_day=8, workday_start="20:00", working_days_per_year=400)
    assert {"workday_start", "working_days_per_year"} <= issue_fields(ei.value)
    # nothing applied: still 7.5 h/day and the holiday, same revision
    assert calendar_settings(ws) == before
    assert ws.state().revision == rev


@pytest.mark.acceptance
def test_a27_several_independent_bad_fields_all_reported():
    ws = new_ws()
    new_project(ws)
    before, rev = _initialised_nondefault(ws)
    with pytest.raises(pp.ValidationFailed) as ei:
        ws.calendar.initialize(
            hours_per_day=0,  # must be > 0
            working_days_per_year=0,  # must be > 0
            holidays=[(date(2026, 10, 12), "A"), (date(2026, 10, 12), "B")],  # duplicate
            exceptions=[(date(2026, 10, 10), "working"), (date(2026, 10, 10), "nonworking")],
        )
    assert {"hours_per_day", "working_days_per_year", "holidays", "exceptions"} <= issue_fields(
        ei.value
    )
    assert calendar_settings(ws) == before
    assert ws.state().revision == rev


@pytest.mark.acceptance
@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"hours_per_day": 25}, "hours_per_day"),  # > 24
        ({"working_days_per_year": 367}, "working_days_per_year"),  # > 366
        ({"working_days_per_year": D("220.5")}, "working_days_per_year"),  # not an integer
        ({"working_weekdays": ""}, "working_weekdays"),  # at least one weekday
        ({"workday_start": "17:00"}, "workday_start"),  # 17:00 + 8 h = 25:00
        ({"workday_start": "25:00"}, "workday_start"),  # not a time
    ],
)
def test_a27_single_invalid_parameter(kwargs, field):
    ws = new_ws()
    new_project(ws)
    before, rev = _initialised_nondefault(ws)
    with pytest.raises(pp.ValidationFailed) as ei:
        ws.calendar.initialize(**kwargs)
    assert field in issue_fields(ei.value)
    assert calendar_settings(ws) == before
    assert ws.state().revision == rev
