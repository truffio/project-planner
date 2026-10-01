# Frozen engine contracts (T10, 2026-10-01)

Reference listing for Wave 1+ agents. The code in `src/project_planner/engine/{model,errors,config}.py` is authoritative; this summary exists so agents can work from a short brief. Changes require a note here and orchestrator approval.

## engine.errors
- `Severity(StrEnum)`: error, warning, info. `ObjectType(StrEnum)`: project, calendar, node, resource, assignment, dependency.
- `Issue` (frozen): `severity, code, message, object_type=None, object_id=None, field=None, line=None`; classmethods `Issue.error/warning/info(code, message, *, object_type, object_id, field, line)`.
- `PlannerError(Exception)` base → `ValidationFailed(issues)`, `ImportFailed(issues)` (`.issues: tuple[Issue, ...]`, picklable), `NotFound(object_type, object_id)` (also `LookupError`), `Conflict(message)`, `Cancelled`, `UnsavedChanges`.

## engine.config (leaf module)
- `Config` (frozen): `max_assignment_percent=Decimal(100)`, `days_display_decimals=2`, `money_display_decimals=2`, `money_rounding=ROUND_HALF_EVEN`, `days_rounding=ROUND_HALF_EVEN`; `round_days(x)`, `round_money(x)`. `DEFAULT_CONFIG`.
- Constants: `MAX_ASSIGNMENT_PERCENT`, `DAYS_DISPLAY_DECIMALS`, `DAYS_ROUNDING`, `MONEY_DISPLAY_DECIMALS`, `MONEY_ROUNDING`, `DEFAULT_CURRENCY="USD"`, `DEFAULT_COST_REPORT_UNIT="person_days"`, `DEFAULT_HOURS_PER_DAY=Decimal(8)`, `DEFAULT_WORKING_DAYS_PER_YEAR=220`, `DEFAULT_WORKING_WEEKDAYS` (Mon–Fri), `DEFAULT_WORKDAY_START=time(9,0)`.

## engine.model
- StrEnums: `NodeKind` (group/task/milestone), `SizingMode` (duration/effort/none), `TimeUnit` (hours/days; `.symbol` h/d, `from_symbol`), `DependencyType` (FS/SS/FF/SF), `WorkUnit` (person_hours/person_days/person_years; `.hours_per_unit(calendar)`), `ExceptionKind` (working/nonworking), `Weekday` ("Mon".."Sun"; `.python_weekday`, `from_python_weekday`, `of(date)`, `parse`, `ordered`). `parse_weekdays(text)` accepts "Mon-Fri", "Mon;Tue", wrapping ranges.
- `TimeQty(value: Decimal, unit: TimeUnit)`; `str()` → "40h"/"-0.5d"; `TimeQty.parse(text, *, object_type, object_id, field)` (codes `TIME_UNITLESS`, `TIME_BAD_UNIT`, `TIME_INVALID`); `.to_minutes(minutes_per_day) -> int` — **ceil toward +∞** (positive rounds up; negative lags round toward zero).
- Module functions: `hours(x)`, `days(x)` (int | str | Decimal; float/bool → TypeError); `as_time_qty(value, ...)` (TimeQty or str; numbers → TypeError naming hours()/days()); `minutes_to_days(minutes, minutes_per_day) -> Decimal` (exact).
- `Holiday(date, name="")`, `CalendarException(date, kind, name="")`.
- `Calendar(working_weekdays, hours_per_day, working_days_per_year, workday_start, holidays, exceptions)` — defaults per plan §2.3, always valid; `.minutes_per_day`, `.workday_start_minute`, `.settings()`.
- `CalendarSettings` — same fields; never raises on construction; `.validate() -> list[Issue]`; `.to_calendar()`; `CalendarSettings.from_values(...)` loose-input entry for `calendar.initialize()`. Codes `CAL_BAD_VALUE`, `CAL_HOURS_PER_DAY_RANGE`, `CAL_HOURS_PER_DAY_PRECISION`, `CAL_DAYS_PER_YEAR_RANGE`, `CAL_NO_WORKING_WEEKDAYS`, `CAL_WORKDAY_OVERFLOW` (field `workday_start`), `CAL_DUPLICATE_HOLIDAY`, `CAL_DUPLICATE_EXCEPTION`, `CAL_HOLIDAY_EXCEPTION_CONFLICT`.
- `WbsNode(id, name, kind, parent_id=None, order=0, sizing_mode=NONE, sizing=None)`; `.is_sized`. Unsized tasks are allowed (scheduled as incomplete).
- `Resource(id, name, hourly_rate: Decimal | None = None)` (None = missing rate).
- `Assignment(task_id, resource_id, percent: Decimal)`; `.key`, `.key_text` ("t1/r1"), `.fraction`.
- `Dependency(id, pred_id, succ_id, type=FS, lag=0d)`.
- `Project(id, name, start, currency="USD", cost_report_unit=PERSON_DAYS, calendar=Calendar(), nodes=(), resources=(), assignments=(), dependencies=())` — collections stored sorted by key (equality ignores input order). Raising lookups: `node`, `resource`, `dependency`, `assignment`. Non-raising: `has_*`, `children(parent_id|None)` (by order, id), `assignments_for`, `assignments_for_resource`, `dependencies_from`, `dependencies_to`, `nodes_of_kind`, `wbs_order()`.
- **Boundary:** the model enforces local invariants only (unique IDs, one assignment per pair, sizing shape, non-negative values). Cross-reference rules (dangling refs, self-dependency, cycles, group endpoints, parent cycles, max percent) belong to T12 `engine/validation.py`.

## tests/fixtures/builders.py
`from fixtures.builders import ProjectBuilder, dec, DEFAULT_START` (2026-10-05). Fluent: `.calendar(**from_values_args)`, `.resource(id, name, *, rate)`, `.group(...)`, `.task(id, name, *, parent, duration, effort, order)`, `.milestone(...)`, `.node(WbsNode)`, `.assign(task, res, percent=100)`, `.dep(pred, succ, type="FS", *, lag="0d", id)`, `.last_id`, `.build()`. Auto IDs g1/t1/m1/r1/d1; no cross-reference checks (tests may build invalid graphs).

## engine.calendar (T11)
`WorkingAxis(calendar: Calendar, start: date)`: `.issues` (INFO `CAL_START_MOVED` when start is nonworking), `.origin` (date of minute 0), `.minutes_per_day`, `.is_working_day(d)`, `.day_index(minute)`, `.working_date(index)`, `.first_working_minute(d)`, `.to_axis(dt) -> int` (clamps; never raises), `.to_datetime(minute, "start"|"finish") -> datetime` (at a day boundary "start" = next day 09:00, "finish" = previous day 17:00; minute 0 "finish" = origin start), `.nonworking_ranges(dt_from, dt_to) -> list[(date, date)]` (inclusive, merged).

## engine.network / engine.validation (T12)
- `validate(project, config=DEFAULT_CONFIG) -> list[Issue]` (all issues, sorted by object_type, object_id, field, code); `ensure_valid(project, config)` raises `ValidationFailed` with errors only; `schedule_blocking(issues) -> bool`.
- Codes (error unless noted): `DEP_DANGLING`, `DEP_SELF`, `DEP_GROUP_ENDPOINT`, `DEP_DUPLICATE`, `DEP_CYCLE` (object_type node, all members in message), `NODE_PARENT_DANGLING`, `NODE_PARENT_NOT_GROUP`, `NODE_PARENT_CYCLE`, `ASSIGN_TASK_DANGLING`, `ASSIGN_NOT_TASK`, `ASSIGN_RESOURCE_DANGLING`, `ASSIGN_PERCENT_RANGE`; warnings `COST_MISSING_RATE`, `TASK_UNSIZED`, `TASK_NO_CAPACITY`.
- `DirectedGraph(nodes, edges)` (`.successors`, `.strongly_connected_components()`, `.cycles()`), `DependencyGraph(project)` (`.topological_order()`), `topological_order(project) -> list[str]` (tasks + milestones; ties by WBS order then id; raises `DEP_CYCLE`), `cycle_issue(members)`.

## engine.sizing (T13)
`compute_sizing(project) -> dict[str, TaskSizing]` (tasks + milestones), `size_node(node, assignments, minutes_per_day)`. `TaskSizing(node_id, duration_minutes: int | None, effort_person_minutes: Decimal | None, capacity: Decimal, issues, schedulable)`. Unschedulable → durations None with error-severity `TASK_UNSIZED` / `TASK_NO_CAPACITY` (the same codes appear as warnings from `validate()`; result assembly (T19) keeps one entry per (code, object_id), preferring the error).

## engine.cost (T14)
`compute_costs(project, durations: Mapping[str, int | None]) -> CostResult`; `AssignmentCost(task_id, resource_id, percent, fraction, assignment_minutes, assignment_hours, hourly_rate, cost, cost_complete)`, `TaskCost(task_id, cost, cost_complete, assignments, missing_rate_resources, hours)`, `GroupCost(group_id, cost, cost_complete, hours)`, `CostResult(tasks, groups, total, complete, missing_rate_resources, total_hours)`. `cost_report(costs, project, unit=None) -> CostReport` with `AssignmentRow/TaskRow/GroupRow(work_qty, work_unit, rate_per_unit, cost, ...)`. `round_money(value, config)`.

## persistence (T30)
- `db.connect(path | ":memory:", *, check_same_thread=True)` (FKs on, WAL for files, autocommit); `db.transaction(conn)` re-entrant (nested → savepoint), maps `IntegrityError` → `Conflict`.
- `migrations.migrate(conn) -> int`, `schema_version(conn)`, `LATEST_VERSION = 1`.
- `repositories` (conn first): `create_project(project, kind, name=None, *, saved_at=None) -> pk`, `create_workspace(project) -> pk`, `get_workspace_pk() -> pk | None`, `get_project_record(pk) -> ProjectRecord`, `bump_revision(pk) -> int`, `list_saved_projects() -> [(pk, name, saved_at)]` newest first, `delete_saved_project(pk)`, `save_project(pk, project)` (does not touch revision/runs), `load_project(pk) -> Project`, `copy_project(src_pk, dst_pk=None, *, kind, name, saved_at=None) -> pk` (overwriting a destination bumps its revision), `save_run(project_pk, run, nodes, assignments, segments, issues) -> run_pk`, `load_runs(project_pk, kind=None) -> list[RunBundle]`, `delete_runs(project_pk, kind=None) -> int`.
- `records`: `RunKind`, `ProjectRecord`, `ScheduleRunRecord`, `NodeResultRecord`, `AssignmentResultRecord`, `LoadingSegmentRecord`, `RunBundle(run, nodes, assignments, segments, issues)`. Minutes as int, Decimals as exact TEXT; `projects.name` = library name (saved-name uniqueness), `project_name` = `Project.name`.

## engine.forward_pass (T15)
`NodeTiming(node_id, start: int | None, finish: int | None, status: "scheduled"|"unschedulable"|"blocked", reason: str | None, blocked_by: tuple[str, ...] = ())`. `forward_pass(project, sizing, *, minutes_per_day, order=None, min_starts=None) -> dict[str, NodeTiming]` — requires a project that passed `ensure_valid`; `start = max(0, min_starts[id], FS pf+lag, SS ps+lag, FF pf+lag−D, SF ps+lag−D)`; blocked check precedes own-sizing check; nested reason chains.

## engine.rollup (T16)
`GroupSummary(group_id, start, finish, effort_person_minutes, complete, unscheduled_descendants)`; `rollup(project, intervals: Mapping[id, (start, finish) | None], effort: Mapping[id, Decimal | None]) -> dict[group_id, GroupSummary]` (incomplete groups still show partial dates); `wbs_numbers(project) -> dict[id, "1.2.3"]`.

## engine.loading (T17)
`LoadSegment(resource_id, start, end, percent, task_ids)` (`.overloaded` = percent > 100), `LoadBucket(resource_id, period_start, assigned_person_minutes, assigned_days, average_percent, peak_percent)`. `compute_loading(project, intervals) -> dict[resource_id, tuple[LoadSegment, ...]]` (every resource present), `overloads(loading) -> list[LoadSegment]`, `aggregate(segments, axis, "day"|"week") -> tuple[LoadBucket, ...]` (buckets only for loaded periods).

## engine.leveling (T18)
`level(project, sizing, base: Mapping[id, NodeTiming], *, minutes_per_day, config=DEFAULT_CONFIG, progress=None, cancel=None) -> LevelingOutcome`. `LevelingOutcome(timings, delays: tuple[LevelingDelay, ...], unresolved: tuple[UnresolvedOverload, ...], base_finish, leveled_finish, finish_delta_minutes)`; `LevelingDelay(task_id, minutes, cause: "resource"|"dependency")` (every delayed task, incl. ones pushed by predecessors); `UnresolvedOverload(resource_id, task_ids, start, end, percent, reason)`. `progress(fraction, message)` 0.0 → 1.0 non-decreasing; `cancel()` polled per task → raises `Cancelled`. Picklable. 10k tasks ≈ 0.1 s.
