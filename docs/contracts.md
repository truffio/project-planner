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
