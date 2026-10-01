# Spec-conformance review (T50)

Reviewer: fresh agent, no implementation history. Date: 2026-10-01. Branch `backend0` at `2b90b4c`.

Sources: `Project_Planner_Functional_Specification.md` v1.4 (authoritative), `docs/decisions.md`, `implementation_plan_backend.md5` §2–§2.3, `docs/api.md`, `docs/csv_format.md`, `docs/performance.md`, `docs/contracts.md`, code under `src/project_planner/`, tests under `tests/`.

How I checked:
- Read the implementation for every row.
- Ran the default suite: `1042 passed, 4 deselected` in 29 s.
- Ran the 1k/10k perf benchmarks (`-m perf`): 2 passed.
- Ran ad-hoc scripts with `.venv\Scripts\python` (kept outside the repo) for the behaviour below. Results are quoted in the Notes column where relevant:
  - stale export marking, unsaved-changes guards, the nonworking start notice
  - day/money display rounding, missing-rate incompleteness
  - leveling report, loading buckets
  - CSV defaults, `001` IDs, empty-project round trip
  - configured max percent through CSV
  - the dirty flag after compute operations

Path abbreviations: `ws` = `services/workspace.py`, `files` = `services/files.py`, `rm` = `services/read_models.py`, `jobs` = `services/jobs.py`, `compute` = `services/compute.py`, `rs` = `services/result_store.py`, `eng/*` = `engine/*`. Test paths are relative to `tests/`.

---

## 1. Summary

| Status | Count |
|---|---:|
| Met | 156 |
| Partially met | 12 |
| Not met | 0 |
| UI phase | 17 |
| **Total rows** | **185** |

The 185 rows are 147 "shall" rows from §2–§12, 27 acceptance scenarios and 11 decisions.

**Verdict: the backend substantially conforms to the specification.**
- All 26 backend acceptance scenarios (A01–A21, A23–A27) are implemented and pass, and both §4.1 performance targets are met by a wide margin (10k schedule 0.85 s, 10k level 1.09 s).
- No "shall" is wholly unmet.
- One High finding, a Partially met row: the unsaved-changes flag ignores calculated results, so an applied leveling can be lost silently.
- Four Medium findings, all Partially met rows or untested behaviour:
  - Long file operations have no non-blocking form.
  - There is no per-task overload status.
  - Display rounding is inconsistent outside CSV `*_days`/`cost`.
  - A failed save gives a non-actionable error.
- The rest are Low.

---

## 2. Conformance table

### §2 Technology and operating environment

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §2 | Runs locally as a desktop app with a Tkinter UI | UI phase | — | Backend is a library. |
| §2 | Needs only standard Python or a packaged exe; no browser, web server, Node, npm or build step | Met | `pyproject.toml` (`dependencies = []`); `jobs.py` docs and `docs/api.md` cover `freeze_support` for PyInstaller | Backend has zero runtime deps. Packaging and launch belong to A22 (UI phase). |
| §2 | Gantt and resource charts use native Tkinter Canvas | UI phase | — | |
| §2 | UI calls scheduling and persistence through a service layer and holds no scheduling rules | UI phase | Service layer exists: `ws.Workspace` facade delegating to `compute`, `jobs`, `files`, `rm` | Backend side is ready. |
| §2 | Scheduling rules live in independent modules that do not depend on the UI | Met | `engine/*` imports only stdlib and `project_planner.engine.*` (checked every import line) | **Untested.** Plan §8 DoD requires an import-inspection test, and none exists. |
| §2 | Long-running operations do not block the interface | Partially met | `jobs.submit_schedule`, `jobs.submit_leveling_preview` (process pool, progress, cancel); `acceptance/test_jobs.py`, `services/test_jobs.py` | `import_csv`, `export_csv`, `load` and `save/save_as` are synchronous only. The workspace is bound to its thread (SQLite `check_same_thread`), so the UI cannot move them to a worker. Measured times: import 2.1 s / 16.6 s, export 1.6 s / 10.9 s, load 1.0 s / 6.3 s, one edit 0.04 s / 0.8 s (10k / 50k). See G2. |

### §3 Project lifecycle

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §3.1 | Project holds name, start, one calendar, WBS, resources with rates, assignments, dependencies, calculated schedule, costs | Met | `eng/model.Project`; `eng/results.ScheduleResult` | |
| §3.1 | User can create, edit, calculate, level, save, load | Met | `ws.new_project`, edit methods, `schedule`, `level_preview`/`apply_leveling`, `save`, `load`; `unit/test_public_api.py::test_plan_section_1_2_example_runs` | |
| §3.2 | Save preserves definition, rates, costs, dates, schedule status and applied leveling delays | Met | `files.save` → `repo.copy_project` (runs included); `rs`; `acceptance/test_files_csv.py::test_a11_save_close_load_restores_definition_and_leveled_schedule`; `services/test_result_store.py::test_leveled_round_trip_keeps_dates_delays_costs_and_fingerprints`, `::test_stored_stale_result_stays_stale_after_reopen` | |
| §3.2 | Load restores those values without silently rebuilding a different schedule | Met | `rs` restores through `eng/schedule.assemble` and never reschedules; `services/test_files.py::test_a11_roundtrip_without_recalculation` (monkeypatches the scheduler to fail) | |
| §3.2 | Interface indicates whether there are unsaved changes | Partially met | `ws.Workspace.dirty`, `state().dirty`, `project_summary().dirty`; `services/test_workspace_edits.py::test_dirty_follows_mark_clean` | `dirty` follows definition edits only. `schedule()`, `apply_leveling()`, `discard_leveling()` and `reset_to_dependency_schedule()` do not set it. Verified: `save_as` → `schedule` → `level_preview` → `apply_leveling` leaves `dirty == False`, and `load()` then drops the applied leveling with no `UnsavedChanges`. This is by design (`test_store_result_does_not_bump_revision_and_emits`), but it conflicts with §3.2. See G1. |
| §3.2 | Load, New and replacement CSV import offer save/discard | Met (backend) | `ws.require_clean` raising `UnsavedChanges`, used by `files.load`, `files.import_csv`, `ws.new_project`; `services/test_files.py::test_unsaved_guard`, `services/test_workspace_edits.py::test_new_project_refuses_to_discard_unsaved_work`, `acceptance/test_files_csv.py::test_a10_import_refuses_to_discard_unsaved_work` | Verified all three. The prompt itself is UI phase. Same blind spot as the row above (G1). |
| §3.2 | Failed save/load leaves the working project intact and shows an actionable error | Partially met | All replacing operations run in one transaction; `services/test_files.py::test_load_unknown_leaves_workspace`, `::test_save_as_duplicate_and_tracking`, `::test_first_save_name_collision_conflicts` | A duplicate `save_as` raises `Conflict("database constraint violated: UNIQUE constraint failed: projects.name")`, which is not actionable. (`save` pre-checks the name and gives a good message.) There is no fault-injection test of a failure partway through `save`, `save_as` or `load`; edits do have one. See G5. |
| §3.2 / D3 | Multiple named projects in SQLite; New, Save, Save As, Load, Delete; project picker | Met (backend) | `files.list_projects/save/save_as/load/delete_project`, `ws.new_project`; `services/test_files.py::test_list_newest_first_and_delete`, `::test_save_first_then_overwrite` | The picker widget is UI phase; `list_projects()` feeds it. |
| §3.2 | Save As creates a new named project, which becomes the active one | Met | `files.save_as` → `ws.mark_clean(new_pk)`, so later `save()` writes to the new copy; `services/test_files.py::test_save_as_duplicate_and_tracking` | Verified that `save()` after `save_as` targets the new id. |

### §4 Work breakdown structure

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §4.1 | Nested groups | Met | `ws.add_group(parent=...)`; `unit/test_rollup.py::test_nested_groups_three_levels` | |
| §4.1 | Add, rename, edit, move, reorder and delete nodes | Met (backend) | `ws.add_*`, `rename_node`, `set_sizing`, `move_node`, `delete_preview`/`delete_commit`; `services/test_workspace_edits.py::test_move_and_reorder` | Widgets are UI phase. |
| §4.1 | Deleting nodes with descendants or dependency references identifies the affected items before commit | Met | `ws.delete_preview` → `DeletePreview(nodes, dependencies, assignments, token)`, then `delete_commit(token)` (revision-checked); `services/test_workspace_edits.py::test_delete_preview_lists_descendants_dependencies_and_assignments`, `::test_delete_commit_conflicts_when_revision_changed` | Verified. There is no direct node-delete path that bypasses the preview. |
| §4.1 | Each node has stable unique ID, name, type, parent, sibling order | Met | `eng/model.WbsNode` | |
| §4.1 | IDs stay stable when names or positions change; WBS number may change | Met | `services/test_workspace_edits.py::test_move_and_reorder`; `unit/test_rollup.py::test_wbs_numbers_reordered_siblings` | |
| §4.1 | No imposed task-count limit | Met | No limit in model, services or CSV; 50k benchmark runs | |
| §4.1 | Large WBS views do not render every row at once | Met (backend) | `rm.wbs_rows/gantt_rows(offset, limit, expanded_ids)`; `services/test_read_models.py::test_10k_tasks_windowed_queries_fast` | Rendering is UI phase. |
| §4.1 / D8 | Schedule 10k tasks in < 2 s | Met | `perf/test_benchmarks.py::test_benchmark` (`-m perf`); `docs/performance.md`: 0.85 s | Re-run passed. Perf tests are excluded from the default run. |
| §4.1 / D8 | Level 10k tasks in < 30 s | Met | same; 1.09 s | |
| §4.1 / D8 | 1k and 50k results measured and reported | Met | `docs/performance.md` | |
| §4.2 | Group start/finish = earliest/latest descendant | Met | `eng/rollup.rollup`; `unit/test_rollup.py` | |
| §4.2 | Group span is not a sum of child durations | Met | `unit/test_rollup.py::test_overlapping_children_span_not_sum` | |
| §4.2 | Group effort aggregates leaf effort without double counting | Met | `eng/rollup`; `property/test_end_to_end_props.py::test_group_sums_equal_leaf_sums` | |
| §4.2 | Dependencies between tasks and milestones | Met | `ws.add_dependency` | |
| §4.2 / D5 | No dependencies on groups; the error identifies the group | Met | `DEP_GROUP_ENDPOINT` in `ws.add_dependency`, `eng/validation`, `csv_io`; `acceptance/test_validation_staleness.py::test_a14_dependency_on_group_rejected_naming_group` | Verified the message names `g1`. |

### §5 Task sizing, assignments, cost

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §5.1 | Task has name, ID, parent, sizing mode, value+unit, assignments, dependencies | Met | `eng/model.WbsNode`, `Assignment`, `Dependency` | |
| §5.1 | Calculated fields: start, finish, working duration, total effort, validation or overload status | Partially met | `eng/results.NodeResult` (start, finish, `duration_days`, `effort_days`, `status`, `reason`) | Validation status is present. **No per-task overload status.** `NodeResult`, `WbsRow` and `TaskDetails` carry none. Overload only appears in per-resource `rm.loading` segments, and there is no project-wide overload read model. See G3. |
| §5.1 | Duration, effort and lag entered as value + `h`/`d`; days convert with hours/day | Met | `eng/model.TimeQty`, `as_time_qty`; `acceptance/test_sizing.py::test_a24_40h_and_5d_identical_at_8h_per_day`, `::test_unit_less_*` | |
| §5.1 | Entered value and unit kept as entered; effort in days = person-days | Met | `acceptance/test_sizing.py::test_a24_entered_value_and_unit_preserved_after_schedule`; `csv/test_csv_io.py::test_roundtrip_preserves_entered_units_and_scales` | |
| §5.1 | Outputs (duration, effort, assignment work, offsets, delays, span) in working days, fractions allowed | Met | `*_days` fields in `eng/results`; `DependencyResult.lag_days`; `unit/test_schedule.py::test_all_day_fields_exact_and_pickle` | |
| §5.1 | Elapsed span in calendar days, labelled distinctly | Met | `ScheduleResult.elapsed_span_calendar_days` | It is counted from the first working date, so a weekend start counts from Monday, not from the selected start. Not documented in `api.md` (G11). |
| §5.2 | Duration fixed when assignments change; effort = D × Σfractions | Met | `eng/sizing.size_node`; `acceptance/test_sizing.py::test_a02_duration_fixed_when_allocation_changes` | |
| §5.2 | Unassigned duration task schedules with no loading | Met | `unit/test_sizing.py::test_duration_without_assignments_has_zero_effort` | |
| §5.3 | Duration = effort / Σfractions | Met | `acceptance/test_sizing.py::test_a01_*` | |
| §5.3 | Effort task without positive capacity is unschedulable with an explanation; no invented assignment | Met | `TASK_NO_CAPACITY`, `NodeResult.reason`; `acceptance/test_sizing.py::test_a13_effort_task_without_allocation_is_unschedulable` | Verified the reason text ("…has no positive resource allocation… assign a resource"). |
| §5.4 | Several people with individual, constant percentages over the same interval | Met | `eng/sizing`, `eng/loading` | |
| §5.4 | No redistribution or alteration; same person once per task (edit updates) | Met | `ws.set_assignment` upsert; `services/test_workspace_edits.py::test_assignment_upsert_updates_the_existing_pair` | Verified. |
| §5.4 / D4 | Percent > 0 and ≤ configurable max (default 100) | Partially met | `Config.max_assignment_percent`; `ws.set_assignment`; `services/test_workspace_edits.py::test_assignment_maximum_percent_follows_config` | The API honours the config. **CSV import hard-codes 100**: with `Config(max_assignment_percent=200)`, a project with a 150 % assignment exports but fails to import (`CSV_OUT_OF_RANGE … <= 100`). See G7. |
| §5.4 | Total loading may exceed 100 % and stays visible | Met | `property/test_end_to_end_props.py::test_loading_is_never_capped` | |
| §5.5 | User-editable hourly rate per resource | Met | `ws.add_resource`, `set_hourly_rate` | |
| §5.5 | assignment cost = D × fraction × rate, each computed individually, no averaging | Met | `eng/cost.compute_costs`; `acceptance/test_cost.py::test_a16_*`, `::test_a21_*` | |
| §5.5 | Task details show each resource's %, assigned work, rate, cost | Met | `rm.task_details` → `TaskAssignmentRow`; `acceptance/test_cost.py::test_a16_task_details_show_percent_work_rate_cost` | |
| §5.5 / D10 | Costs in the rate's native unit; reports in person-hours, -days or -years | Met | `rm.cost_report(unit)`; `acceptance/test_cost.py::test_a16_cost_report_in_every_unit` | |
| §5.5 | Person-days use hours/day; person-years use working days/year | Met | `eng/model.WorkUnit.hours_per_unit`; `acceptance/test_calendar_init.py::test_a26_working_days_per_year_changes_person_years_only` | |
| §5.5 / D10 | Default unit person-days per project; each report may choose | Met | `services/test_read_models.py::test_cost_report_default_unit_is_project_unit`; `acceptance/test_cost.py::test_a25_cost_report_unit_set_at_project_creation` | |
| §5.5 | Unit changes presentation only | Met | `acceptance/test_cost.py::test_a25_switching_report_unit_changes_no_cost_dates_or_staleness` | |
| §5.5 | Group cost sums leaves once; project cost sums leaves; milestones cost 0 | Met | `eng/cost`; `acceptance/test_cost.py::test_a17_nested_groups_sum_leaf_costs_once`; `unit/test_cost.py::test_milestone_and_unschedulable` | |
| §5.5 | Effort task uses calculated duration and entered % (A16 example) | Met | `acceptance/test_cost.py::test_a16_assignment_and_task_costs` | |
| §5.5 | Leveling delay does not change cost; nonworking time and lag cost nothing; overlapping assignments each costed | Met | `acceptance/test_loading_leveling.py::test_a19_leveling_does_not_change_cost`; `property/test_end_to_end_props.py::test_leveling_preserves_cost_and_durations` | |
| §5.5 | UI shows rates, assigned work and costs in details, task/group costs in WBS, project total | UI phase | Backend: `task_details`, `WbsRow.cost`/`cost_complete`, `cost_report`, `project_summary().total_cost` | |
| §5.5 / D6 | Rate edits recalculate costs immediately without changing dates | Met | `ws.set_hourly_rate` (`refresh=True` → `rs.plan_refresh`); `acceptance/test_cost.py::test_a18_rate_change_updates_costs_only`; `services/test_compute.py::test_rate_edit_keeps_leveled_result_current` | Verified: cost changed, finish unchanged, not stale. |
| §5.5 | Missing rates flagged; an incomplete estimate is not shown as a complete total | Met | `COST_MISSING_RATE`; `cost_complete=False`; `missing_rate_resources`; CSV `cost_complete=false`; `acceptance/test_cost.py::test_a20_missing_rate_makes_estimate_incomplete` | Verified. `total_cost` still holds the partial sum (3233.33) with `complete=False`, so the UI must label it. |
| §5.5 | Explicit zero rate is valid | Met | `acceptance/test_cost.py::test_a20_explicit_zero_rate_is_complete` | |
| §5.5 / D9 | One currency per project (default USD), no conversion, nonnegative rates | Met | `Project.currency`, `ws.set_currency`; `RESOURCE_NEGATIVE_RATE`; `services/test_workspace_edits.py::test_rate_validation`, `::test_project_setting_rejections` | |
| §5.5 / D9 | Exact decimal money arithmetic | Met | `Decimal` throughout; floats refused (`ws._number`) | |
| §5.5 / D9 | Displayed and exported amounts rounded to 2 decimals (half-even); totals from unrounded parts | Partially met | CSV `cost` via `csv_io._money` (`Config.round_money`); `csv/test_csv_io.py::test_export_missing_rate_unscheduled_and_precision`; `unit/test_cost.py::test_round_money` | Totals from unrounded parts: verified. Gaps: notebook `_repr_html_`/`to_records` show `33.3333` unrounded; CSV `rate_per_unit` is written to 6 decimals (documented in csv_format §4.2, but a deviation); read models return raw `Decimal` and offer no formatting helper. Export uses `DEFAULT_CONFIG`, not the workspace config. See G4. |
| §5.5 | Rates uniform; no overtime, date-dependent or task-specific rates | Met | By construction | |

### §6 Shared work calendar

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §6 | One calendar for everything: weekdays, hours/day, holidays, exceptions | Met | `eng/model.Calendar`; `eng/calendar.WorkingAxis` | |
| §6 / D11 | Single calendar initialisation function | Met | `ws.CalendarApi.initialize`; `acceptance/test_calendar_init.py` | |
| §6 | Parameters are project data, saved, in CSV import/export | Met | `acceptance/test_calendar_init.py::test_a26_settings_survive_save_and_load`, `::test_a26_settings_survive_csv_round_trip` | |
| §6 | Parameter rules: hours (0, 24]; days/year whole 1..366; ≥ 1 weekday; start + hours ≤ midnight; unique holidays/exceptions | Met | `CalendarSettings.validate`; `acceptance/test_calendar_init.py::test_a27_single_invalid_parameter` | Extra rules: hours/day must be a whole number of minutes, and a date may not be both holiday and exception. Both follow from one-minute precision and are documented. |
| §6 | Omitted parameters take defaults | Met | `acceptance/test_calendar_init.py::test_initialize_without_arguments_gives_defaults` | |
| §6 | Validated together; nothing applied if invalid; each bad field named | Met | `acceptance/test_calendar_init.py::test_a27_invalid_combination_applies_nothing_and_names_each_field`, `::test_a27_several_independent_bad_fields_all_reported` | |
| §6 | Changing hours/day, weekdays, start time, holidays or exceptions makes the schedule stale; entered units kept | Met | `eng/fingerprint.schedule_fp`; `services/test_workspace_edits.py::test_staleness_rules`; `acceptance/test_calendar_init.py::test_hours_per_day_change_8_to_7_5` | |
| §6 | Changing days/year does not make it stale | Met | `acceptance/test_validation_staleness.py::test_working_days_per_year_change_does_not_stale_schedule` | Verified. |
| §6 | Work and offsets count working time only; nonworking time gives no progress or capacity; tasks may span weekends | Met | `acceptance/test_calendar_dependencies.py::test_a04_*`; `acceptance/test_loading_leveling.py::test_a08_weekend_inside_leveled_task` | |
| §6 | Fractional working days (12 h = 1.5 d) | Met | `unit/test_calendar.py::test_twelve_hours_is_one_and_a_half_days` | |
| §6 | Consistent boundary convention; FS with zero lag starts at predecessor finish | Met | `unit/test_calendar.py::test_weekend_skipped_fs_zero_lag` | |
| §6 / D7 | One-minute precision | Met | Integer-minute axis (`eng/calendar`) | |
| §6 / D7 | Effort-derived duration rounded up to the next whole minute | Met | `eng/sizing`; `unit/test_sizing.py::test_rounding_up_keeps_entered_effort` | Verified: 1 h at 30 % gives 200 min. |
| §6 / D7 | Intervals are half-open | Met | `unit/test_loading.py::test_touching_intervals_do_not_overlap` | |
| §6 / D7 | Day values calculated exactly and displayed to 2 decimals | Partially met | CSV `*_days` via `csv_io._days` (2 decimals); `csv/test_csv_io.py::test_export_missing_rate_unscheduled_and_precision` | Calculation: exact minutes; `Decimal` days use the 28-digit context, e.g. `0.41666…67` (G13). Display gaps: notebook shows `0.125`; CSV `work_qty` in person-days is written to 6 decimals (`5.041667`); read models are raw (the UI must apply `Config.round_days`). See G4. |
| §6 / D7 | Finish on a day boundary shown as end of the last working period | Met | `WorkingAxis.to_datetime(..., "finish")`; `unit/test_calendar.py::test_origin_and_day_boundary_conventions`; `unit/test_schedule.py::test_day_boundary_finish_and_elapsed_across_weekend` | Verified 17:00. |
| §6 | Nonworking project start moves to the next working boundary and the user is informed | Met | `CAL_START_MOVED` (info) in `ScheduleResult.issues`; `acceptance/test_calendar_dependencies.py::test_project_start_on_weekend_moves_to_next_working_boundary`; `unit/test_calendar.py::test_start_on_holiday_moves_with_info_issue` | Verified with a Saturday start: start became Monday 09:00 with the info issue. The notice appears at schedule time, not at `set_project_start` (acceptable). |

### §7 Dependencies

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §7 | Multiple predecessors and successors | Met | `acceptance/test_calendar_dependencies.py::test_a06_*` | |
| §7 | Each dependency has stable endpoint IDs, one type, signed working-time offset | Met | `eng/model.Dependency` | |
| §7 | Add, edit, remove dependencies | Met (backend) | `ws.add_dependency/edit_dependency/remove_dependency` | Editor is UI phase. |
| §7 | FS/SS/FF/SF semantics; positive and negative offsets in working time | Met | `eng/forward_pass`; `acceptance/test_calendar_dependencies.py::test_a05_dependency_type_and_lag` | |
| §7 | All incoming constraints satisfied together, not as equalities | Met | `property/test_end_to_end_props.py::test_dependencies_hold_before_and_after_leveling` | |
| §7 | Negative lag never starts a successor before project start | Met | `forward_pass` uses `max(0, …)`; `acceptance/test_calendar_dependencies.py::test_a05_negative_lag_never_precedes_project_start` | Verified. |
| §7 | Invalid endpoints, self-dependencies and cycles give explicit errors | Met | `DEP_DANGLING`, `DEP_SELF`, `DEP_CYCLE`; `acceptance/test_validation_staleness.py::test_a14_*` | |
| §7 | All cycles rejected; error names every node of each cycle | Met | `eng/network.cycle_issue`; `services/test_workspace_edits.py::test_dependency_cycle_names_every_member`; `csv/test_csv_io.py::test_cycle_excludes_group_and_self_edges_and_reports_once_per_cycle` | Verified. |

### §8 Schedule calculation

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §8 | Forward schedule from project start using durations, calendar and dependencies | Met | `eng/schedule.schedule` | |
| §8 | Before leveling, resources not limited to 100 % | Met | `eng/forward_pass` ignores capacity | |
| §8 | Start/finish for every schedulable task and milestone | Met | `NodeResult` | |
| §8 | Derived group dates and effort | Met | `eng/rollup` | |
| §8 | Completion date = latest finish of scheduled tasks and milestones | Met | `ScheduleResult.project_finish` | |
| §8 | Working span and elapsed span clearly distinguished | Met | `working_span_days` vs `elapsed_span_calendar_days` | |
| §8 | Resource loading and overload intervals | Met | `ScheduleResult.loading`; `rm.loading` (`overloaded`) | |
| §8 | Assignment, task, group and project cost with completeness | Met | `costs`, `cost_complete` | |
| §8 | Errors for items that cannot be scheduled | Met | `NodeResult.status/reason`; `issues` | Verified: blocked successors get a chained reason. Only the root cause carries an `Issue`. |
| §8 | No valid completion date while tasks are unscheduled; partial result marked incomplete | Met | `project_finish=None` unless complete; `acceptance/test_sizing.py::test_a13_*`; `unit/test_schedule.py::test_a13_incomplete_with_partial_dates` | Verified. |
| §8 | Changes to calendar, sizing, dependencies, assignments or start invalidate results | Met | Fingerprint comparison in `ws.state`; `acceptance/test_validation_staleness.py::test_a15_edit_after_schedule_marks_results_stale` | Granularity is the whole result, which is conservative and acceptable. |
| §8 | Stale results clearly marked and not shown as current in exports | Met | `files.export_csv(stale=…)` → `RESULT_PROJECT.schedule_status`; `acceptance/test_files_csv.py::test_export_marks_stale_results`; `csv/test_csv_io.py::test_export_stale_and_incomplete_status` | Verified `current` → `stale`. When a result is both stale and incomplete, only `stale` is written (G8). |
| §8 / D6 | Recalculation only on an explicit Calculate | Met | Edits never schedule; `acceptance/test_validation_staleness.py::test_a15_recalculation_replaces_stale_results` | |
| §8 / D6 | Rate edits recalculate costs immediately, dates unchanged | Met | See §5.5 | |

### §9 Resource loading

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §9 | loading % = sum of active allocations | Met | `eng/loading.compute_loading` | |
| §9 | Not capped, and not averaged so as to hide brief overloads | Met | `acceptance/test_loading_leveling.py::test_a03_brief_overload_peak_vs_average` | |
| §9 | 80 % + 50 % shows 130 % and is flagged | Met | `acceptance/test_loading_leveling.py::test_a03_overlapping_assignments_show_130_percent` | |
| §9 | Inspect each person's loading over time and the tasks behind an overload | Met (backend) | `rm.loading(resource, time_window, granularity)` segments with `task_ids` | Chart is UI phase. |
| §9 | Display shows a 100 % capacity reference | UI phase | `LoadingView.capacity_percent = 100` | |
| §9 | Daily/weekly summaries separate average, peak and assigned work (person-days) | Met | `LoadingBucketRow(assigned_days, average_percent, peak_percent)`; `services/test_read_models.py::test_a03_day_buckets_average_vs_peak`; `unit/test_loading.py::test_week_aggregation_sums_days` | Verified the week buckets. |
| §9 | Nonworking periods are not available capacity | Met | `unit/test_loading.py::test_holiday_gives_no_bucket_or_capacity` | |

### §10 Resource leveling

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §10 | Runs only on request | Met | `compute.level_preview` only | |
| §10 | Delays whole tasks, keeping effort/duration, assignments and %, continuity, dependencies and the start bound | Met | `eng/leveling.level`; `property/test_leveling_props.py::test_leveling_invariants_*`; `property/test_end_to_end_props.py::test_dependencies_hold_before_and_after_leveling` | |
| §10 | No splitting, % reduction, removal or substitution | Met | By construction; same property tests | |
| §10 | Related tasks pushed to keep FS/SS/FF/SF; new finish recalculated and shown | Met | `unit/test_leveling.py::test_chain_push_through_all_dependency_types`; `LevelingResult.finish_delta_days` | |
| §10 | Reports delayed tasks, added delay, resulting finish and unresolved overloads | Met | `LevelingResult.delays/delays_days/result.project_finish/unresolved` | Verified. `UnresolvedOverload.start/end` are axis minutes, not datetimes, and the stored or applied result does not keep `unresolved` (G10). |
| §10 | A task whose own assignment exceeds capacity is explained, not looped on | Met | `unit/test_leveling.py::test_own_allocation_over_capacity_reported` | Verified the reason text. |
| §10 / D1 | Preview, then Apply or Discard | Met | `compute.level_preview/apply_leveling/discard_leveling`; `acceptance/test_loading_leveling.py::test_leveling_preview_apply_discard_reset_transitions` | |
| §10 / D1 | Return to the dependency-only schedule | Met | `compute.reset_to_dependency_schedule` | |
| §10 / D1 | One task at a time in dependency order; priority: dependency-only start, then WBS order, then ID | Met | `eng/leveling` heap key; `unit/test_leveling.py::test_tie_break_by_wbs_order`, `::test_earlier_dependency_only_start_wins_over_wbs` | The ID key is unreachable because WBS order is total, hence untested. |
| §10 | Each task at the earliest start that respects dependencies and capacity | Met | `property/test_leveling_props.py` | |
| §10 | Invalidating edits make a leveled schedule stale; no silent re-run | Met | `acceptance/test_loading_leveling.py::test_leveled_schedule_goes_stale_and_is_not_releveled_silently`; `services/test_compute.py::test_leveled_result_goes_stale_and_is_not_releveled` | |

### §11 User interface

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §11.1 | Main view: WBS table, Gantt, loading view | UI phase | — | |
| §11.1 | WBS table columns: name, sizing, duration, effort, start, finish, assignments, cost, status | UI phase | `rm.WbsRow` has every column | There is no overload status (G3). |
| §11.1 | Selecting a task opens a properties editor | UI phase | `rm.task_details` | |
| §11.1 | Table and Gantt share row order, expansion and scrolling | UI phase | `gantt_rows` `row_index` matches `wbs_rows`; `services/test_read_models.py::test_gantt_rows_align_with_wbs_rows` | |
| §11.2 | Gantt shows bars, milestones, summary spans, nonworking dates | UI phase | `GanttRow.is_milestone/is_summary/leveling_delay_days`; `rm.nonworking_ranges`; `services/test_read_models.py::test_nonworking_ranges` | |
| §11.2 | Horizontal scroll, zoom, selection | UI phase | `gantt_rows(time_window=…)` | |
| §11.2 | Dependency connections inspectable, with clutter reduction | UI phase | `rm.dependency_links(visible_ids)` | |
| §11.2 | Gantt bars not draggable | UI phase | No API sets dates (by construction) | |
| §11.3 | Settings, calendar, resources, task, dependency editors, save/load, CSV, schedule, level | UI phase | All backend operations exist | |
| §11.3 | Validation errors identify the field or object | Met (backend) | `Issue(object_type, object_id, field, line)`; `ValidationFailed.issues` | |
| §11.3 | Long operations show progress/busy without freezing | UI phase | `Job.progress/message/cancel`; `ws.poll_jobs` | See G2 for operations that have no job form. |
| §11.3 | Closing the window offers save/discard | UI phase | `ws.dirty` | Subject to G1. |

### §12 CSV import and export

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| §12.1 | Import replaces the whole definition, with no merge or retained data | Met | `files.import_csv` (`save_project` + `delete_runs`); `acceptance/test_files_csv.py::test_a10_valid_csv_replaces_project` | |
| §12.1 | Whole file parsed and validated before commit; failure preserves the project | Met | `csv_io.parse` before the transaction; `acceptance/test_files_csv.py::test_a09_invalid_csv_leaves_project_intact` | |
| §12.1 | Replacement summary and unsaved-work handling | Met | `ImportSummary` (counts, notes, `defaults_applied`); `UnsavedChanges` | |
| §12.1 | Dependencies from the file or entered later | Met | | |
| §12.1 | Calculated dates in a CSV do not override scheduling | Met | `CSV_RESULTS_IGNORED`; `csv/test_csv_io.py::test_valid_with_results_ignores_results` | Verified `result() is None` after import. |
| §12.2 | Export includes every node, collapsed or filtered | Met | `acceptance/test_files_csv.py::test_a12_export_includes_all_nodes_regardless_of_ui_state`; `csv/test_csv_io.py::test_export_lists_every_node_exactly_once_even_for_collapsed_groups` | |
| §12.2 | Export fields: IDs, hierarchy, sizing, assignments, dependencies and offsets, start/finish, duration, effort, rates, assignment/task/group/project cost, cost completeness, leveling delay, status | Met | `csv_io.export` header (45 columns); `csv/test_csv_io.py::test_export_with_result_rows` | Verified the header. |
| §12.2 | Export identifies incomplete or stale scheduling explicitly | Met | `schedule_status` ∈ {current, stale, incomplete}; `csv/test_csv_io.py::test_export_stale_and_incomplete_status` | G8: "stale" hides "incomplete". |
| §12.3 | Single CSV with a record-type column and schema version | Met | csv_format §2 | |
| §12.3 | Record types as listed; export adds result records | Met | `PROJECT, CALENDAR, HOLIDAY, EXCEPTION, RESOURCE, NODE, ASSIGNMENT, DEPENDENCY, RESULT_*` | |
| §12.3 | Each assignment and dependency is its own record | Met | | |
| §12.3 / D2 | Project and calendar records mandatory; the rest optional | Partially met | `csv_io` raises `CSV_MISSING_NODE` | **NODE is also mandatory** (documented in csv_format §3, but stricter than the spec). Verified: exporting an empty project (`new_project` and nothing else) cannot be re-imported. See G6. |
| §12.3 | Calendar record carries the §6 parameters; omitted settings (incl. currency, unit) get defaults; summary lists each default | Met | `CSV_DEFAULT_APPLIED`; `services/test_files.py::test_import_minimal_defaults_and_results_note`; `csv/test_csv_io.py::test_valid_minimal_defaults_and_notes` | Verified 6 default messages. |
| §12.3 | Missing information never taken from the previous project | Met | Parse is independent of the workspace | |
| §12.3 | Import template documents UTF-8, ISO 8601, decimal point, `h`/`d` units, nested fields, schema version | Met | `docs/csv_format.md`; `tests/fixtures/csv/valid_*.csv` | Low: there is no user-facing template file under `examples/`. |
| §12.3 | IDs kept as text (`001`) | Met | `csv/test_csv_io.py::test_ids_stay_text` | Verified `001` round trip. |

### §13 Acceptance scenarios

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| A01 | 40 h effort, 80/20 % → 5 d; 4 and 1 person-days | Met | `acceptance/test_sizing.py::test_a01_effort_task_duration_and_individual_efforts`, `::test_a01_person_hours_work_quantities` | |
| A02 | 5 d duration, allocation 50 → 100 % | Met | `acceptance/test_sizing.py::test_a02_duration_fixed_when_allocation_changes` | |
| A03 | 80 % + 50 % overlap → 130 % flagged | Met | `acceptance/test_loading_leveling.py::test_a03_*` | |
| A04 | Duration crosses a holiday | Met | `acceptance/test_calendar_dependencies.py::test_a04_*` | |
| A05 | Each dependency type with ± lag | Met | `acceptance/test_calendar_dependencies.py::test_a05_*` | |
| A06 | Several predecessors | Met | `acceptance/test_calendar_dependencies.py::test_a06_*` | |
| A07 | Resolvable overload leveled | Met | `acceptance/test_loading_leveling.py::test_a07_leveling_delays_exactly_one_whole_task` | |
| A08 | Weekend inside a leveled task | Met | `acceptance/test_loading_leveling.py::test_a08_weekend_inside_leveled_task` | |
| A09 | Invalid CSV: project intact, row/field errors | Met | `acceptance/test_files_csv.py::test_a09_*` | |
| A10 | Valid CSV replaces the project | Met | `acceptance/test_files_csv.py::test_a10_*` | |
| A11 | Save, close, load restore the schedule incl. leveling delays | Met | `acceptance/test_files_csv.py::test_a11_*` | G1 is about the *unsaved* side. |
| A12 | Export with a collapsed group | Met | `acceptance/test_files_csv.py::test_a12_*` | |
| A13 | Effort task without allocation | Met | `acceptance/test_sizing.py::test_a13_*` | |
| A14 | Cycle or dangling reference | Met | `acceptance/test_validation_staleness.py::test_a14_*` | |
| A15 | Edit after scheduling → stale | Met | `acceptance/test_validation_staleness.py::test_a15_*` | |
| A16 | Alice/Bob $3,200 + $400 = $3,600 | Met | `acceptance/test_cost.py::test_a16_*` | |
| A17 | Nested groups sum leaves once | Met | `acceptance/test_cost.py::test_a17_*` | |
| A18 | Rate change: costs update, dates unchanged | Met | `acceptance/test_cost.py::test_a18_*` | |
| A19 | Leveling keeps cost | Met | `acceptance/test_loading_leveling.py::test_a19_*` | |
| A20 | Missing rate → incomplete | Met | `acceptance/test_cost.py::test_a20_*` | |
| A21 | 16 h, 75/25 % → $1,200 + $240 = $1,440 | Met | `acceptance/test_cost.py::test_a21_*` | |
| A22 | Launch on Windows without Node, npm or a browser | UI phase | Zero runtime deps; `freeze_support` documented | |
| A23 | Large run stays responsive, shows progress, can be cancelled; cancel applies nothing | Met (backend) | `acceptance/test_jobs.py::test_a23_*`; `services/test_jobs.py::test_cancel_while_leveling_runs`, `::test_process_pool_cancel_large_leveling` | Schedule-job progress is coarse (0 → 1), and the schedule worker never polls cancel, though a cancelled job still stores nothing (G12). |
| A24 | 40 h vs 5 d identical, entered units kept | Met | `acceptance/test_sizing.py::test_a24_*` | |
| A25 | Report unit switch changes no costs or dates | Met | `acceptance/test_cost.py::test_a25_*` | |
| A26 | 7.5 h/day, 250 d/yr; survives save/load and CSV | Met | `acceptance/test_calendar_init.py::test_a26_*` | |
| A27 | Invalid calendar: nothing applied, each field named | Met | `acceptance/test_calendar_init.py::test_a27_*` | |

### §14 Resolved decisions

| Spec ref | Requirement (short) | Status | Evidence | Notes |
|---|---|---|---|---|
| D1 | Leveling order, tie-break, preview/apply/discard, reset | Met | See §10 | |
| D2 | Single record-type CSV; project and calendar mandatory; documented defaults | Partially met | See §12.3 | NODE is also mandatory (G6). |
| D3 | Named projects; New, Save, Save As, Load, Delete; picker | Met | See §3.2 | Picker is UI phase. |
| D4 | Allocation (0, configurable max = 100] | Partially met | See §5.4 | CSV ignores the configured max (G7). |
| D5 | No group dependencies | Met | See §4.2 | |
| D6 | Explicit Calculate; rate edits recost immediately | Met | See §8 | |
| D7 | Minute precision, round-up, 2-decimal days, end-of-period finish, units kept, day outputs | Partially met | See §6 | Display rounding outside CSV `*_days` (G4). |
| D8 | 1k/10k/50k; schedule < 2 s, level < 30 s | Met | `docs/performance.md` | |
| D9 | One currency, exact decimals, half-even 2-decimal display/export | Partially met | See §5.5 | Notebook display and `rate_per_unit` (G4). |
| D10 | Cost reporting unit | Met | See §5.5 | |
| D11 | Calendar parameters user-configurable with defaults, editable later | Met | See §6 | |

---

## 3. Gaps and deviations

| ID | Spec ref | What is missing or different | Severity | Suggested fix (module / owner task) |
|---|---|---|---|---|
| G1 | §3.2, §11.3, A11 | `dirty` tracks definition revisions only. Calculating, applying or discarding leveling, and resetting, change the stored results (which Save persists) without marking unsaved work. Then `load`, `new_project`, `import_csv` and, later, window close throw away an applied leveling without a prompt. Reproduced: `save_as` → `schedule` → `level_preview` → `apply_leveling` gives `dirty=False`, and `load()` succeeds and drops the leveled result. | High | `services/workspace.py` + `result_store.py` (T31/T33): track a results generation, bumped by `store_result` and `discard_results`, next to the revision. Make `dirty` (and therefore `require_clean`) true when it differs from the saved copy, and reset it in `mark_clean`. Update `test_store_result_does_not_bump_revision_and_emits` and add a guard test. |
| G2 | §2, §11.3, A23 | Only schedule and leveling have a non-blocking form. `import_csv` (2.1 s at 10k, 16.6 s at 50k), `export_csv` (1.6 / 10.9 s), `load` (1.0 / 6.3 s), `save_as` and large edits (0.8 s at 50k) block. The workspace is thread-bound, so the UI cannot offload them. | Medium | T32/T33: add `submit_import_csv` (parse in a worker, commit on the owner thread at poll time) and `submit_export_csv` (export a `Project` + result snapshot in a worker). Optionally speed up `csv_io._tokenize/_quote` and the O(n) `Project.__post_init__` per edit (see `docs/performance.md` hotspots). |
| G3 | §5.1, §11.1 | No per-task overload status. `NodeResult`, `WbsRow` and `TaskDetails` have no overload flag, and no read model lists all overloads project-wide. The UI would have to call `loading()` for every resource and cross-reference `task_ids`. | Medium | T19/T34: add `overloaded: bool` (and `overloaded_resources`) to `NodeResult`/`WbsRow`/`TaskDetails`, derived from `ScheduleResult.loading`. Add `ws.overloads()` returning every overloaded segment with datetimes. |
| G4 | §6 "displayed to 2 decimals", §5.5/D9 "displayed and exported amounts … 2 decimals" | Notebook `_repr_html_`/`to_records` show unrounded values (`0.125`, `33.3333`). CSV `work_qty` (person-days) and `rate_per_unit` go to 6 decimals. Read models expose raw `Decimal` with no display helper. CSV export uses `DEFAULT_CONFIG` instead of the workspace `Config`. | Medium | `notebook.py` (T35): round `*_days` with `Config.round_days` and money with `round_money` in `_cell`/records. `csv_io` (T20): pass the workspace config. Get a user decision on `rate_per_unit`/`work_qty` precision (person-years need > 2 decimals) and record it in `decisions.md`. Optionally add `format_days`/`format_money` helpers for the UI. |
| G5 | §3.2 "actionable error" | A duplicate `save_as` name surfaces the raw SQLite text `database constraint violated: UNIQUE constraint failed: projects.name`. Nothing injects a failure partway through `save`, `save_as` or `load` to prove the workspace stays intact. | Medium | `services/files.py::save_as` (T33): pre-check the name and raise `Conflict("a saved project named 'X' already exists; choose another name")`. Add fault-injection tests (monkeypatch `repo.copy_project` to raise mid-transaction) for save, save_as and load. |
| G6 | §12.3, D2 | NODE records are mandatory (`CSV_MISSING_NODE`), but the spec makes only PROJECT and CALENDAR mandatory. A freshly created empty project exports a CSV that cannot be imported. | Low | `engine/csv_io.py` (T20): accept zero NODE rows, or record an explicit user-approved deviation in `decisions.md`. |
| G7 | §5.4, D4 | CSV import checks percent against the default 100 and ignores `Config.max_assignment_percent`, so export → import fails under a non-default max. | Low | `csv_io.parse(source, config=…)` and pass `ws.config` from `files.import_csv` (T20/T33). |
| G8 | §8, §12.2 | When a stored result is both stale and incomplete, `schedule_status` only says `stale`, so the incomplete condition is not explicit. | Low | `csv_io` (T20): add a separate `complete` cell on `RESULT_PROJECT`, or a combined value. Update csv_format §4. |
| G9 | §2, plan §8 DoD | No test enforces that `engine/` does not import `sqlite3`, `services`, `persistence` or UI libraries. It is true today by inspection. | Low | `tests/unit/test_engine_imports.py` (T40): AST-scan `engine/*.py`. |
| G10 | §10 | `UnresolvedOverload.start/end` are internal axis minutes in a public type, not datetimes. The applied (stored) result keeps no `unresolved` list or delay causes, so it is lost after Apply or reload. Overloads stay visible in loading. | Low | `engine/results`/`leveling` (T19): add datetime fields, and persist the unresolved list with the run (T30). |
| G11 | §5.1, §8 | `elapsed_span_calendar_days` counts from the first working date (the moved start), not the selected project start. This is reasonable but undocumented in `api.md`. | Low | `docs/api.md` (T44). |
| G12 | A23 | Schedule-job progress is only 0 → 1, and the schedule worker never polls cancel. A cancelled job still stores nothing, and a 10k schedule takes < 1 s, so this is acceptable. | Low | Optional: phase progress messages in `jobs.schedule_worker` (T32). |
| G13 | §6 "calculated exactly" | Day values are `Decimal` quotients at 28 significant digits (e.g. `0.4166…67`), so repeating fractions are not exact. Exact integer minutes are kept alongside. | Low | Document in `docs/api.md`/`decisions.md` D7 (T44). |

---

## 4. Untested requirements

- **Engine independence from UI/persistence** (§2; plan §8 DoD requires a test). Holds by inspection, but nothing enforces it (G9).
- **Dirty state after compute operations** (§3.2). The existing test asserts the opposite of what the spec implies (G1).
- **Failed save, save_as or load mid-transaction leaves the workspace intact** (§3.2). Only edits have fault-injection tests (`test_failure_in_row_write_leaves_everything_unchanged`). Load has only the unknown-id case (G5).
- **Display rounding to 2 decimals** in notebook views (§6, D7, D9). No test, and current behaviour does not round (G4).
- **CSV import honouring a non-default `max_assignment_percent`** (D4). No test, and it fails (G7).
- **Exporting an empty project and re-importing it.** No test, and it fails (G6).
- **Leveling tie-break by node ID** (§10, third key). Unreachable while WBS order is a total order, so there is no test (not a defect).
- **Performance targets** (§4.1). Tested only under `-m perf`, which the default run excludes. Re-run during this review: pass.
- **Blocked successors**: no test that they appear in `issues` (only `reason`). Minor, behaviour is acceptable.

---

## 5. Deferred to the UI phase and existing backend support

| Spec item | Backend support available |
|---|---|
| §2 Tkinter desktop app; Canvas charts; A22 launch without Node, npm or a browser | Zero runtime dependencies. Spawn-based process pool documented with `if __name__ == "__main__"` and `multiprocessing.freeze_support()` for PyInstaller (`docs/api.md`). |
| §2 UI goes only through the service layer | `Workspace` facade covers every operation. `subscribe(callback)` gives change events (`edited`, `result_stored`, `project_replaced`). |
| §3.2 Unsaved indicator and save/discard prompts (load, new, import, close) | `ws.dirty`, `state().dirty`, `UnsavedChanges` from `load`/`new_project`/`import_csv`, `discard_unsaved=True`. **G1 must be fixed first.** |
| §3.2 Project picker | `list_projects()` returns `ProjectInfo(id, name, saved_at)`, newest first. Also `save`, `save_as`, `load`, `delete_project`. |
| §4.1 Large WBS views | `wbs_rows(expanded_ids, offset, limit)` returns `WbsPage(rows, total)`, about 0.1 ms warm at 10k/50k. |
| §4.1 Delete confirmation | `delete_preview(ids, resources=…)` lists every node, dependency and assignment, then `delete_commit(token)` (revision-guarded). |
| §5.5 / §11.1 Cost columns and details | `WbsRow.cost/cost_complete`, `task_details(unit)` (per-assignment %, work, rate, cost), `cost_report(unit)`, `project_summary().total_cost/cost_complete`. Rounding: `Config.round_money` / `round_days` (G4). |
| §6 Nonworking start notice | `CAL_START_MOVED` info issue in `ScheduleResult.issues`. |
| §9 Loading chart with 100 % reference | `loading(resource, time_window, granularity="segments" \| "day" \| "week")` returns segments with `percent`, `task_ids`, `overloaded`, and buckets with `assigned_days`, `average_percent`, `peak_percent`. `capacity_percent=100`. Project-wide overload list missing (G3). |
| §11.1 Table and Gantt alignment | `gantt_rows(expanded_ids, offset, limit)` uses the same visible ordering and `row_index` as `wbs_rows` (tested). |
| §11.2 Gantt contents | `GanttRow(start, finish, is_milestone, is_summary, leveling_delay_days)`; `nonworking_ranges(start, end)` (merged, inclusive); `time_window` filter for zoom and horizontal windowing; `dependency_links(visible_ids)` for clutter reduction. No date-setting API exists, so bars cannot be dragged. |
| §11.3 Editors | Calendar (`ws.calendar.initialize` / setters / holidays / exceptions), resources, task sizing, assignments, dependencies, project settings (`set_project_start`, `set_currency`, `set_cost_report_unit`, `rename_project`). `Issue.object_type/object_id/field/line` points at the field in error. |
| §11.3 Progress, busy state, cancel | `submit_schedule`, `submit_leveling_preview` return a `Job` (`status`, `progress`, `message`, `cancel()`). Use `ws.poll_jobs()` from `root.after` (documented). File operations have no job form yet (G2). |
| §10 Leveling preview UI | `level_preview()` returns `LevelingResult(delays_days, delays, unresolved, finish_delta_days, result)`. Also `apply_leveling`, `discard_leveling`, `reset_to_dependency_schedule`, and `state().has_preview`. |
| §8 Stale display | `state().stale_dates/stale_costs` and `project_summary()` flags (whole-result granularity). |
