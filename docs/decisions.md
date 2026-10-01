# Project Planner — Decision Record

This document records the open decisions from the Functional Specification (§14) that were resolved on 2026-10-01. Each decision is confirmed by the user and implemented as specified here.

Configuration constants and project settings reside in:
- **Application-level limits:** `engine/config.py`
- **Per-project parameters:** Calendar and project settings in `engine.model.Calendar` and `engine.model.Project`

## Summary

| ID | Decision | Status | Spec § | Setting(s) |
|----|----------|--------|--------|-----------|
| D1 | Leveling priority and tie-breaking | Confirmed, 2026-10-01 | 10 | None (behavioral rule) |
| D2 | CSV schema and omitted settings | Confirmed, 2026-10-01 | 12.3 | None (format specification) |
| D3 | Named project storage and Save As | Confirmed, 2026-10-01 | 3.2 | None (behavioral rule) |
| D4 | Individual allocation range | Confirmed, 2026-10-01 | 5.4 | `MAX_ASSIGNMENT_PERCENT = 100` |
| D5 | Dependencies on summary groups | Confirmed, 2026-10-01 | 4.2 | None (behavioral rule) |
| D6 | Immediate vs. explicit recalculation | Confirmed, 2026-10-01 | 8 | None (behavioral rule) |
| D7 | Precision, boundaries, rounding, and time units | Confirmed, 2026-10-01 | 5.1, 6 | `DAYS_DISPLAY_DECIMALS = 2` |
| D8 | Performance targets and test sizes | Confirmed, 2026-10-01 | 4.1 | None (benchmark targets) |
| D9 | Currency and monetary rounding | Confirmed, 2026-10-01 | 5.5 | Project `currency = "USD"` (default) |
| D10 | Cost reporting unit | Confirmed, 2026-10-01 | 5.5 | Project `cost_report_unit = "person_days"` (default) |
| D11 | Calendar parameters: working hours and days per year | Confirmed, 2026-10-01 | 6 | Calendar: `hours_per_day = 8`, `working_days_per_year = 220`, `working_weekdays = Mon–Fri`, `workday_start = 09:00` |

---

## D1 — Leveling priority and tie-breaking

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §10.

**Decision:** Tasks shall be placed one at a time in dependency order. When several tasks are ready (all predecessors scheduled), priority goes to the earliest dependency-only start, then WBS display order, then node ID. Leveling shall produce a preview that the user may apply or discard. The user shall also be able to reset the schedule to the dependency-only (unleveled) state.

**Rationale:** Serial placement with deterministic tie-breaking ensures reproducible results. Preview-apply-discard-reset gives users full control over when to accept leveling changes. No optimality or minimum-project-duration guarantee is implied.

**Configuration:** None (behavioral rule).

---

## D2 — CSV schema and omitted settings

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §12.3.

**Decision:** Import and export shall use a single record-type CSV format. The `PROJECT` and `CALENDAR` records are mandatory in the file. All other record types (holidays, exceptions, resources, assignments, dependencies, and result records on export) are optional. Omitted settings in the `CALENDAR` record take their documented defaults. No information shall ever be taken silently from the previous project.

**Rationale:** One CSV format simplifies implementation and use. Mandatory project and calendar records ensure a valid import context. Documented defaults for optional settings allow users to omit rows they have not configured, and the import summary shall list every default applied.

**Configuration:** None (format specification).

---

## D3 — Named project storage and Save As

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §3.2.

**Decision:** Multiple named projects shall be stored in one SQLite database. The user shall be able to create new, save, save-as (creating a new named project from the current working project), load, list, and delete named projects. A project picker shall allow selection of projects to load.

**Rationale:** Named storage in a single file allows users to maintain multiple planning scenarios without file-system clutter. Save As supports creating variants without overwriting the original.

**Configuration:** None (behavioral rule).

---

## D4 — Individual allocation range

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §5.4.

**Decision:** Each assignment shall have a user-entered percentage. Individual assignment percentages shall be greater than 0% and no greater than a configurable maximum, which defaults to 100%. Total resource loading across tasks shall be allowed to exceed 100% and shall remain visible.

**Rationale:** A minimum of 0% (exclusive) prevents zero-effort assignments. The configurable maximum defaults to 100% but can be extended for testing cases where overload detection is required even before leveling. Allowing task-level loading to exceed 100% shows all overloads transparently.

**Configuration:** `MAX_ASSIGNMENT_PERCENT = 100`.

---

## D5 — Dependencies on summary groups

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §4.2.

**Decision:** Dependencies shall not be attached to summary groups. Attempting to create a dependency with a group as an endpoint shall produce a validation error identifying the group.

**Rationale:** Summary group dates are derived from their descendants; fixing a group's dates independently would conflict with that derivation. Expanding group endpoints to their leaf tasks is deferred to a later version.

**Configuration:** None (behavioral rule).

---

## D6 — Immediate vs. explicit recalculation

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §8.

**Decision:** Schedule recalculation shall happen only when the user explicitly requests it through a `schedule()` action. Edits to calendar, task sizing, dependencies, assignments, or project start shall mark affected calculated results stale. Rate edits are the exception: they shall recalculate affected costs immediately without changing dates or requiring an explicit `schedule()` call.

**Rationale:** Explicit recalculation gives users control over when expensive calculations run. Immediate cost refresh on rate edits follows decision 6 in the spec (A18) and avoids showing stale costs while dates remain valid.

**Configuration:** None (behavioral rule).

---

## D7 — Precision, boundaries, rounding, and time units

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §5.1, §6.

**Decision:** 

- **Internal precision:** Working time shall be calculated at one-minute precision. Effort-based task durations shall be rounded up to the next whole working minute.
- **Boundary convention:** Task intervals run from their start up to, but not including, their finish. A finish that falls on a day boundary shall be displayed as the end of the last working period (e.g., "Monday 17:00"), not as the start of the next working day.
- **Rounding for display:** Day values shall be calculated exactly and displayed to 2 decimal places.
- **Time units:** Task duration, effort, and dependency offsets are entered as a value with a unit of either hours or days, as chosen by the user. The entered value and unit are preserved as entered and shall not be changed by calculation. Planning calculation outputs are reported in working days with fractional days permitted. The elapsed calendar span is reported in calendar days and labeled distinctly.

**Rationale:** Minute-precision working time balances accuracy with computational efficiency. Effort rounding-up ensures tasks do not appear to finish before the last assigned minute. The half-open interval `[start, finish)` makes finish-to-start successors with zero lag begin exactly at the predecessor finish. Preserving user-entered units prevents implicit conversion that could confuse planning decisions. Day-based outputs provide a familiar scale for project planning.

**Configuration:** `DAYS_DISPLAY_DECIMALS = 2`.

---

## D8 — Performance targets and test sizes

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §4.1.

**Decision:** The application shall be benchmarked on three representative project sizes: 1,000, 10,000, and 50,000 tasks. Performance targets are:
- Scheduling a 10,000-task project shall take under 2 seconds.
- Leveling a 10,000-task project shall take under 30 seconds.
- Results for the 1,000- and 50,000-task sizes shall be measured and reported.

**Rationale:** Three representative sizes show scalability across a realistic range. The 10k-task targets ensure acceptable responsiveness for interactive use. Measuring all three sizes identifies where performance degrades.

**Configuration:** None (benchmark targets).

---

## D9 — Currency and monetary rounding

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §5.5.

**Decision:** Each project shall have one currency, with a default code of USD, and no exchange-rate conversion. Rates shall be nonnegative. Monetary calculations shall use exact decimal arithmetic at full precision. Displayed and exported amounts shall be rounded to 2 decimals using round-half-to-even. Totals shall be computed from unrounded contributions, not by summing rounded values.

**Rationale:** One currency per project simplifies cost calculation. Exact decimal arithmetic avoids floating-point rounding errors that accumulate across cost calculations. Round-half-to-even (banker's rounding) is the standard for financial reporting. Computing totals from unrounded values ensures the displayed total matches the sum of individual contributions exactly.

**Configuration:** Project `currency = "USD"` (default).

---

## D10 — Cost reporting unit

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §5.5.

**Decision:** Cost reports shall present assigned work and the corresponding rate in a reporting unit chosen by the user: person-hours (man-hours), person-days (man-days), or person-years (man-years). The default reporting unit is person-days, set per project. Each report may select a different unit. The reporting unit shall change only how work and rates are presented. It shall not change calculated cost amounts, dates, or the user's entered task units.

**Rationale:** Different stakeholders prefer different units for work reporting. Defaulting to person-days aligns with common project-planning practice. Unit-independent costs ensure every unit reports the same total cost, even though the work quantity and rate display change. This gives users flexibility in reporting without ambiguity about the underlying economics.

**Configuration:** Project `cost_report_unit = "person_days"` (default).

---

## D11 — Calendar parameters: working hours and days per year

**Status:** Confirmed by the user, 2026-10-01.

**Spec reference:** §6.

**Decision:** At the start of a project, the user shall configure calendar parameters through a single calendar initialisation function. These parameters are project data, not fixed constants. They shall be saved with the project and included in CSV import and export. The user may edit these parameters later through calendar editing.

Calendar parameters and defaults:

| Parameter | Default | Validation |
|-----------|---------|-----------|
| Working hours per day | 8 | Greater than 0 and no more than 24 |
| Working days per year | 220 | Whole number greater than 0 and no more than 366; used for person-year cost reporting |
| Working weekdays | Monday–Friday | At least one weekday |
| Workday start time | 09:00 | Start time plus working hours per day shall not pass midnight |
| Holidays | None | Unique dates |
| Date exceptions | None | Unique dates, each marked working or nonworking |

Changes to working hours per day, weekdays, start time, holidays, or exceptions shall mark the schedule stale. Changes to working days per year affect only person-year cost reporting and shall not mark the schedule stale.

**Rationale:** Making calendar parameters user-configurable at project start allows teams to model their own working patterns (e.g., 7.5-hour days, a Sun–Thu week, or region-specific holidays). Saving with the project ensures a schedule's dates remain valid when the calendar is revisited later. Marking scheduling-relevant changes stale prevents silent schedule drift; working-days-per-year is cost-reporting-only and does not require recalculation.

**Configuration:** 
- Calendar `hours_per_day = 8` (default)
- Calendar `working_days_per_year = 220` (default)
- Calendar `working_weekdays = Mon–Fri` (default)
- Calendar `workday_start = 09:00` (default)
- Calendar `holidays = None` (default)
- Calendar `exceptions = None` (default)
