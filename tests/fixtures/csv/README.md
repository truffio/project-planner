# CSV fixtures

Format: `docs/csv_format.md` (schema version 1). Every file uses the full 45-column header. Line numbers are 1-based physical lines (header = line 1).

## Valid files (import must succeed)

| File | Supports | Notes |
|---|---|---|
| `valid_minimal.csv` | A10 | PROJECT + CALENDAR (all four calendar cells empty) + one task. Expect `CSV_DEFAULT_APPLIED` notes for currency (`USD`), cost_report_unit (`person_days`), hours_per_day (8), working_days_per_year (220), working_weekdays (Mon-Fri), workday_start (09:00). |
| `valid_full.csv` | A10, A16 data, round trip | Every definition record type; 3-level nesting (`001` > `020` > `021`); tasks sized in `h` and `d`; effort task `022` = 40 h with R001 80 % (rate 100) and R002 20 % (rate 50); milestone `030`; FS/SS/FF/SF with lags 0 d, 1 d, 4 h, -0.5 d, 0 h, -2 h; one holiday; one working and one nonworking exception; R003 has no rate, R004 rate 0; IDs with leading zeros; a name containing a comma (quoting). |
| `valid_collapsed_group.csv` | A12 | `G1` > `G2` > `G3` > `G4` > leaves `L1`..`L3`; `L4` under `G2`; top-level `T9`. Test: export contains all 11 NODE rows. |
| `valid_with_results.csv` | A10 (results ignored) | Definition plus 1 RESULT_PROJECT, 3 RESULT_NODE, 2 RESULT_ASSIGNMENT. Import must ignore them (info `CSV_RESULTS_IGNORED`) and yield the same definition as without them. |
| `valid_custom_calendar.csv` | A26 | 7.5 h/day, 250 days/year, `Sun;Mon;Tue;Wed;Thu`, start 08:30. **Saved with a UTF-8 BOM and CRLF line endings** on purpose, to test lenient import. Do not normalise (if git `autocrlf` rewrites it, the BOM test still holds and CRLF is merely accepted as LF). |

## Invalid files (import must fail; existing project must remain intact; A09)

All invalid files are derived from one small base project (line 2 PROJECT, 3 CALENDAR, 4 RESOURCE `R1`, 5 NODE `n1` group, 6 NODE `n2` task, 7 NODE `n3` task, 8 ASSIGNMENT `n2`/`R1`, 9 DEPENDENCY `d1` `n2`->`n3`) with exactly one fault, except where stated. Each is expected to produce exactly the listed errors and no others.

| File | Expected error(s): record type, line, column, code |
|---|---|
| `invalid_missing_project.csv` | `PROJECT`, line 1, `record_type`, `CSV_MISSING_PROJECT` |
| `invalid_duplicate_project.csv` | `PROJECT`, line 10, `record_type`, `CSV_DUPLICATE_PROJECT` |
| `invalid_unknown_schema_version.csv` | all rows carry `schema_version` 2. `PROJECT`, line 2, `schema_version`, `CSV_UNKNOWN_SCHEMA_VERSION` (single error; import stops) |
| `invalid_unknown_record_type.csv` | `WIDGET`, line 10, `record_type`, `CSV_UNKNOWN_RECORD_TYPE` |
| `invalid_bad_date.csv` | `PROJECT`, line 2, `start_date` (`2026-02-30`), `CSV_BAD_DATE` |
| `invalid_bad_number.csv` | `RESOURCE`, line 4, `hourly_rate` (`abc`), `CSV_BAD_NUMBER` |
| `invalid_unitless_sizing.csv` | `NODE`, line 6, `sizing_unit` (value `5`, unit empty), `CSV_UNITLESS_TIME` |
| `invalid_bad_unit.csv` | `NODE`, line 6, `sizing_unit` (`w`), `CSV_BAD_UNIT` |
| `invalid_duplicate_node_id.csv` | `NODE`, line 8, `id` (`n3`, first seen line 7), `CSV_DUPLICATE_ID` |
| `invalid_dangling_parent.csv` | `NODE`, line 7, `parent_id` (`nX`), `CSV_DANGLING_REFERENCE` |
| `invalid_dangling_dependency.csv` | `DEPENDENCY`, line 9, `succ_id` (`n9`), `CSV_DANGLING_REFERENCE` |
| `invalid_duplicate_assignment.csv` | `ASSIGNMENT`, line 10 (`n2`/`R1` again), `resource_id`, `CSV_DUPLICATE_ASSIGNMENT` |
| `invalid_self_dependency.csv` | `DEPENDENCY`, line 9, `succ_id` (`n2` -> `n2`), `CSV_SELF_DEPENDENCY` |
| `invalid_group_dependency.csv` | `DEPENDENCY`, line 9, `succ_id` (`n1` is a group), `CSV_GROUP_DEPENDENCY` |
| `invalid_cycle.csv` | adds `n4` (line 8, task) and dependencies `d1` n2->n3 (line 10), `d2` n3->n4 (line 11), `d3` n4->n2 (line 12). One error: `DEPENDENCY`, line 10, `pred_id`, `CSV_CYCLE`; message must name `n2`, `n3` and `n4` (and lines 10, 11, 12) |
| `invalid_calendar_combo.csv` | `CALENDAR`, line 3, `workday_start` (`20:00` with `hours_per_day` 8), `CSV_BAD_CALENDAR` |
| `invalid_multiple_errors.csv` | Exactly four errors, all must be reported: (1) `PROJECT`, line 2, `start_date` (`not-a-date`), `CSV_BAD_DATE`; (2) `RESOURCE`, line 4, `hourly_rate` (`abc`), `CSV_BAD_NUMBER`; (3) `NODE`, line 6, `sizing_unit`, `CSV_UNITLESS_TIME`; (4) `DEPENDENCY`, line 9, `succ_id` (`n9`), `CSV_DANGLING_REFERENCE` |
