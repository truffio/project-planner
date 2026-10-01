# Python API reference

Everything below is importable from the package root (`import project_planner as pp`). `pp.__all__` is the supported surface; anything else is internal. See the [README](../README.md) for installation and key concepts, [csv_format.md](csv_format.md) for the CSV format and [decisions.md](decisions.md) for the resolved design decisions.

```python
import project_planner as pp
from datetime import date

ws = pp.open_workspace(":memory:")
ws.new_project("Demo", start=date(2026, 10, 5))
t = ws.add_task("Design", effort=pp.hours(40))
print(ws.schedule().working_span_days)
```

Conventions: all IDs are strings (except saved-project ids, see Files); every method that takes an ID also accepts an object with an `.id`; money is `Decimal` (pass `str` or `Decimal`, never `float`); planning outputs are in days.

## Workspace lifecycle

- `pp.open_workspace(path=":memory:", *, config=pp.Config())` returns a `Workspace` (creates and migrates the SQLite file if needed). A fresh database holds an empty "Untitled" project.
- `pp.Config` (frozen dataclass) holds limits and rounding: `max_assignment_percent=Decimal(100)`, `days_display_decimals=2`, `money_display_decimals=2`, `money_rounding` and `days_rounding` (`"ROUND_HALF_EVEN"`).
- `ws.close()`, or `with pp.open_workspace(...) as ws:`.
- A database file can be open in only one workspace at a time (in this or any other process). A second `pp.open_workspace(path)` raises `Conflict` ("... is already open in another workspace ..."). The lock (an OS lock on a `<file>.lock` sidecar file, which may stay on disk harmlessly) is released by `ws.close()`, by leaving the `with` block, when the workspace is garbage collected, and when the process ends, even after a crash. `":memory:"` workspaces are independent. As a second safeguard every write checks that the database still holds the revision the workspace last saw, and raises `Conflict` if another connection changed it.
- `ws.config` is the read-only `Config` the workspace was opened with.
- `ws.new_project(name, start, *, currency="USD", cost_report_unit="person_days", calendar=None, discard_unsaved=False)` (`calendar` is an optional `pp.CalendarSettings`).
- `ws.subscribe(callback) -> unsubscribe` for change events. `callback(event)` receives an event with `kind` (`"edited"`, `"result_stored"`, `"project_replaced"`), `revision`, `ids` and `operation`; it runs synchronously on the calling thread after the change commits, and an exception in a callback is logged, not propagated.
- `ws.dirty` is the same flag as `state().dirty` (unsaved changes). It is true after any edit of the definition and after any change to the stored results (calculating, a leveling preview, applying or discarding leveling, resetting, a job result being stored) since the last save or load; `save()`, `save_as()` and `load()` clear it. `ws.state()` returns `WorkspaceState(revision, dirty, stale_dates, stale_costs, has_preview, has_result)`. `ws.result()` is the stored current result (leveled if applied, else dependency-only, else `None`); it may be stale, compare with `state()`.
- Extension internals: the service modules use underscore-prefixed workspace members (`_store_result`, `_discard_results`, `_mark_clean`, `_reload`, `_require_clean`, `_connection`, `_results`, `_project_pk`). They are not part of the public API and may change; application code uses the methods documented here (`discard_leveling()` / `reset_to_dependency_schedule()` to drop results).
- A workspace is bound to the thread that opened it. Use it from that thread only.

## Editing

Resources: `add_resource(name, hourly_rate=None) -> id` (a `None` rate means "missing": costs are reported incomplete; `0` is an explicit zero rate), `rename_resource`, `set_hourly_rate` (costs refresh immediately, dates do not go stale), `remove_resource`.

Nodes: `add_group(name, parent=None)`, `add_task(name, parent=None, *, duration=None, effort=None)` (give at most one; neither makes an unsized task, which schedules as incomplete; both raise `ValidationFailed` `SIZING_AMBIGUOUS`), `add_milestone(name, parent=None)`, `rename_node`, `move_node(node, parent, index)`, `set_sizing(task, *, duration=None, effort=None)`.

Assignments: `set_assignment(task, resource, percent)` (upsert; percent is `int`, `str` or `Decimal`, greater than 0 and at most `Config.max_assignment_percent`), `remove_assignment`.

Dependencies: `add_dependency(pred, succ, type="FS", lag="0d") -> id`, `edit_dependency(dep, *, type=None, lag=None)`, `remove_dependency`. Cycles, self-dependencies, group endpoints and duplicates raise `ValidationFailed` immediately.

Project settings: `set_project_start`, `rename_project`, `set_currency`, `set_cost_report_unit`.

Deleting: `ws.delete_preview(ids, *, resources=()) -> DeletePreview` (`token`, `requested`, `nodes`, `resources`, `dependencies`, `assignments`, `revision`: everything that would go, including descendants and attached links), then `ws.delete_commit(preview.token)`. A token is only valid for the revision it was made at (`Conflict` otherwise).

Reads: `ws.project() -> Project` (immutable snapshot), `ws.node(id) -> WbsNode`, `ws.resource(id) -> Resource`, `ws.dependency(id) -> Dependency` (`NotFound` for an unknown ID).

Editing never recalculates the schedule. Results become stale (see `state()`); call `schedule()` again.

### Time inputs

Duration, effort and lag are `TimeQty` values: `pp.hours(40)`, `pp.days(2)`, `pp.days("-0.5")`, or strings such as `"40h"`, `"2d"`, `"-0.5d"`. Bare numbers (`duration=5`) raise `TypeError` naming both forms; a unit-less string such as `"40"` raises `ValidationFailed`. `pp.hours()` / `pp.days()` also accept float literals (`pp.days(-0.5)`), converted exactly through their shortest decimal text. Quantities are stored exactly as entered; days convert with the calendar's hours per day at calculation time.

## Calendar

`ws.calendar` (`CalendarApi`):

- `settings() -> pp.CalendarSettings` (equality-comparable; `pp.CalendarSettings()` equals the defaults). Fields: `working_weekdays`, `hours_per_day`, `working_days_per_year`, `workday_start`, `holidays` (`pp.Holiday(date, name)`), `exceptions` (`pp.CalendarException(date, kind, name)`).
- `initialize(*, working_weekdays="Mon-Fri", hours_per_day=8, working_days_per_year=220, workday_start="09:00", holidays=(), exceptions=())`. All arguments optional; one `ValidationFailed` lists every invalid field and nothing is applied. `working_weekdays` is `"Mon-Fri"`, `"Mon;Tue"` or an iterable of `pp.Weekday` / names. Holidays are `(date, name)` (or `pp.Holiday`), exceptions `(date, "working" | "nonworking"[, name])` (or `pp.CalendarException`). A project start on a non-working day is moved to the next working day (info issue `CAL_START_MOVED`).
- Setters: `set_hours_per_day`, `set_working_days_per_year` (affects person-year figures only), `set_working_weekdays`, `set_workday_start`, `add_holiday(date, name="")`, `remove_holiday`, `add_exception(date, kind, name="")`, `remove_exception`.
- In a notebook, `ws.calendar` renders the current settings as a table.

## Compute and jobs

Every compute operation has a synchronous form and a `submit_*()` form that returns a `Job`.

| Synchronous (blocks, returns the result) | Background (returns a `Job` immediately) |
|---|---|
| `ws.schedule() -> ScheduleResult` | `ws.submit_schedule(executor=None)` |
| `ws.level_preview() -> LevelingResult` | `ws.submit_leveling_preview(executor=None)` |

Leveling state machine: `schedule()` stores a dependency-only result (replacing every stored run); `level_preview()` stores a preview without changing `ws.result()`; `apply_leveling()` makes the preview the current result (`Conflict` if there is no fresh preview); `discard_leveling()` drops the preview; `reset_to_dependency_schedule()` drops applied leveling and returns the dependency-only result without recalculating. Leveling never runs by itself.

### Jobs

`Job` has `kind`, `status` (`"pending" | "running" | "done" | "failed" | "cancelled"`), `progress` (0..1), `message`, `done()`, `poll()`, `cancel() -> bool`, `result(timeout=None)` (raises `Cancelled` for a cancelled job, re-raises a failure), `error`, `stored`.

Results are stored on the workspace's own thread, the first time you call `job.result()`, `job.done()`, `job.poll()`, read `job.status`, call `ws.poll_jobs()`, or call `ws.state()` / `ws.result()` (both poll first). A job never overwrites a newer stored result or a replaced project, and a result you explicitly removed after submitting stays removed (a preview job finishing after `discard_leveling()`, `apply_leveling()` or `reset_to_dependency_schedule()` is not stored; `job.stored` is `False`); cancelling stores nothing; edits made while a job runs make its result stale on arrival.

### Tkinter polling

(Illustrative snippet: `ws`, `root`, `progress_bar` and `refresh_views` belong to your application, so this block is not executed by the docs check.)

The workspace must be used from the thread that opened it, so do not touch it from worker threads. Poll from the Tk main loop:

```python
def tick():
    for job in ws.poll_jobs():          # jobs that finished during this call
        refresh_views()
    progress_bar["value"] = job.progress * 100   # `job` is the one returned by submit_schedule()
    if not job.done():
        root.after(100, tick)

job = ws.submit_schedule()
root.after(100, tick)
```

### Process pool and entry points

The default executor is a spawn-based `ProcessPoolExecutor`. Scripts that submit jobs must guard their entry point:

```python
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()    # needed when frozen with PyInstaller
    main()
```

For tests and simple scripts pass `executor=pp.InlineExecutor()` to run the job on the calling thread.

## Files and CSV

- `ws.save()` overwrites the tracked saved copy (the first save uses the project name; a name collision raises `Conflict`). `ws.save_as(name)` raises `Conflict("a saved project named 'X' already exists — choose another name")` for a name in use. Both return `ProjectInfo(id, name, saved_at)`.
- `ws.list_projects() -> list[ProjectInfo]` (newest first), `ws.load(project, *, discard_unsaved=False)`, `ws.delete_project(project)`; `project` is a `ProjectInfo` or its `id`. `ProjectInfo.id` is an `int` (the one exception to "IDs are strings"; a decimal string is accepted too) and is never reused, so a stale `ProjectInfo` raises `NotFound` instead of reaching another project. Stored results are restored verbatim on load.
- `save`, `save_as` and `load` are all-or-nothing: on any failure neither the workspace nor the saved projects change. Change events are sent after the operation has completed (after `load`, subscribers already see `dirty == False`).
- `ws.export_csv(destination=None) -> str` returns the CSV text and writes the file (UTF-8, no BOM) when a path is given. Stale results are marked stale in the export.
- `ws.import_csv(source, *, discard_unsaved=False) -> ImportSummary` (`nodes, resources, assignments, dependencies, holidays, exceptions` counts, `notes` (info `Issue`s such as `CSV_DEFAULT_APPLIED`, `CSV_RESULTS_IGNORED`), `defaults_applied` messages). `source` is a path, a `Path`, an open text file, or CSV text (a string containing a newline). The file is parsed completely before anything changes; errors raise `ImportFailed` with every `Issue` (1-based `line`, column in `field`) and leave the workspace untouched. After a successful import the workspace is dirty and `ws.result()` is `None`.
- Replacing operations (`load`, `import_csv`, `new_project`) raise `UnsavedChanges` when the workspace is dirty unless `discard_unsaved=True`. The CSV format is described in `docs/csv_format.md`.

## Read models

All take IDs or objects and are cheap to call repeatedly (cached per revision and result).

- `ws.cost_report(unit=None) -> CostReportView` (`unit`, `total_cost`, `work_qty`, `complete`, `missing_rate_resources`, `nodes`, `.node(id)`). `unit` is `"person_hours" | "person_days" | "person_years"` (anything else raises `ValidationFailed` with code `COST_BAD_UNIT`, field `unit`); cost totals are identical across units. `Conflict` without a result.
- `ws.task_details(task, unit=None) -> TaskDetails` (same `unit` values and check) (sizing, dates, cost, predecessors, successors, `assignments`).
- `ws.loading(resource, *, time_window=None, granularity="segments") -> LoadingView`. `granularity` is `"segments"`, `"day"` or `"week"`; `time_window` is a `(start, end)` datetime pair. `segments` (always filled) are `(start, end, percent, task_ids, overloaded)`; `buckets` (day/week only) are `(period_start, assigned_days, average_percent, peak_percent, overloaded)`. Also `resource_id`, `resource_name`, `capacity_percent`.
- `ws.wbs_rows(*, parent=None, expanded_ids=(), offset=0, limit=None) -> WbsPage(rows, total)` (`WbsRow`: `id, name, kind, depth, wbs_number, has_children, expanded, sizing, duration_days, effort_days, start, finish, assignments_summary, cost, cost_complete, status`), `ws.gantt_rows(*, expanded_ids=(), offset=0, limit=None, time_window=None) -> list[GanttRow]` (`row_index, id, kind, start, finish, is_milestone, is_summary, leveling_delay_days`), `ws.dependency_links(nodes) -> list[Link]` (`pred_id, succ_id, type, lag_days`), `ws.nonworking_ranges(start, end) -> list[tuple[date, date]]` (inclusive, merged), `ws.project_summary() -> ProjectSummary` (counts, calendar, revision, dirty, result kind, finish, span, cost, staleness flags).

## Results

`ScheduleResult`: `kind` (`"dependency_only" | "leveling_preview" | "leveled"`), `complete`, `project_start`, `project_finish` (`None` unless complete), `working_span_days`, `elapsed_span_calendar_days`, `effort_days`, `total_cost` / `cost_total`, `cost_complete`, `missing_rate_resources`, `issues`, and lookups `node(id)`, `assignment(task, resource)`, `dependency(id)` (raise `NotFound`), `tasks()`.

`NodeResult`: `node_id`, `kind`, `scheduled`, `start`, `finish` (naive datetimes; finish uses the end-of-working-period convention, e.g. 17:00), `duration_days`, `effort_days`, `leveling_delay_days`, `cost`, `cost_complete`, `status` (`"scheduled"` or `"unscheduled"`), `reason` (why unscheduled, else `None`), `wbs_number`. `AssignmentResult` has `task_id, resource_id, percent, assignment_days, assignment_hours, hourly_rate, cost, cost_complete`; `DependencyResult` has `dependency_id, lag_days, lag_entered`.

`LevelingResult`: `result` (kind `leveling_preview`), `delays_days`, `delays`, `unresolved`, `finish_delta_days`.

### Notebook helpers

`ScheduleResult`, `LevelingResult`, `CostReportView`, `LoadingView`, `TaskDetails` and `ws.calendar` render as HTML tables in Jupyter (`_repr_html_`). Each has:

- `.to_records() -> list[dict]` (plain values, no dependencies). For a `ScheduleResult` this is one row per task with `id, wbs, name, kind, status, start, finish, duration_days, effort_days, leveling_delay_days, cost, cost_complete`. Task names are attached by the workspace when you obtain the result through `ws.schedule()` / `ws.result()`; otherwise the ID is shown.
- `.to_dataframe()` builds a pandas `DataFrame`. When pandas is not installed it raises `ImportError` telling you to run `pip install project_planner[notebook]`.

## Model types

Snapshot types returned by `ws.project()`, `ws.node()` and friends are immutable dataclasses: `Project(id, name, start, currency, cost_report_unit, calendar, nodes, resources, assignments, dependencies)`, `WbsNode(id, name, kind, parent_id, order, sizing_mode, sizing)`, `Resource(id, name, hourly_rate)`, `Assignment(task_id, resource_id, percent)`, `Dependency(id, pred_id, succ_id, type, lag)`, `Holiday(date, name)`, `CalendarException(date, kind, name)`, `TimeQty(value, unit)` (`str()` gives `"40h"` / `"-0.5d"`; equality is numeric on the value, exact on the unit).

String enums (members compare equal to their text; the API accepts either): `NodeKind` (`group`, `task`, `milestone`), `SizingMode` (`duration`, `effort`, `none`), `TimeUnit` (`hours`, `days`), `DependencyType` (`FS`, `SS`, `FF`, `SF`), `WorkUnit` (`person_hours`, `person_days`, `person_years`), `ExceptionKind` (`working`, `nonworking`), `Weekday` (`Mon` ... `Sun`), `Severity` (`error`, `warning`, `info`), `ObjectType` (`project`, `calendar`, `node`, `resource`, `assignment`, `dependency`).

`pp.__version__` is the package version.

## Errors and issue codes

All derive from `pp.PlannerError`: `ValidationFailed(issues)`, `ImportFailed(issues)`, `NotFound(object_type, object_id)` (also a `LookupError`), `Conflict`, `UnsavedChanges`, `Cancelled`. An `Issue` carries `severity`, `code`, `message`, `object_type`, `object_id`, `field` and (imports) `line`. One exception reports every problem found in the call. Wrong Python types (a bare number as a duration, a float as money) raise `TypeError`.

Engine issue codes (`Issue.code`; severity is `error` unless noted). They appear in `ValidationFailed.issues`, in `ScheduleResult.issues`, and (as `issue_codes`) in CSV result records.

| Code | Meaning | Field |
|---|---|---|
| `TIME_INVALID`, `TIME_UNITLESS`, `TIME_BAD_UNIT` | Time quantity unreadable / not finite, text without a unit (`"40"`), unit other than `h`/`d`. | `sizing`, `lag` |
| `NAME_EMPTY` | Blank name. | `name` |
| `INVALID_ID`, `DUPLICATE_ID`, `INVALID_VALUE` | Empty or whitespace-padded ID, repeated ID, text that is not a value of the enum field. | `id`, various |
| `SIZING_AMBIGUOUS` | Both `duration` and `effort` given. | `sizing` |
| `NODE_SIZING_NOT_ALLOWED`, `NODE_SIZING_PARTIAL`, `NODE_NEGATIVE_SIZING` | Sizing on a group/milestone, mode without quantity (or reverse), value below 0. | `sizing` |
| `INDEX_RANGE` | `move_node` index outside `0..len(siblings)`. | `index` |
| `NODE_PARENT_DANGLING`, `NODE_PARENT_NOT_GROUP`, `NODE_PARENT_CYCLE` | Parent missing, not a group, or the parent chain loops. | `parent_id` |
| `DEP_DANGLING`, `DEP_SELF`, `DEP_GROUP_ENDPOINT`, `DEP_DUPLICATE`, `DEP_CYCLE` | Dependency endpoint missing, self-dependency, endpoint is a group, same pred/succ/type twice, dependency cycle (message lists the nodes). | `pred_id`, `succ_id`, `type` |
| `ASSIGN_TASK_DANGLING`, `ASSIGN_RESOURCE_DANGLING`, `ASSIGN_NOT_TASK`, `ASSIGN_PERCENT_RANGE`, `ASSIGNMENT_PERCENT_RANGE`, `DUPLICATE_ASSIGNMENT` | Assignment target missing or not a task; percent not in `(0, max_assignment_percent]`; pair repeated. | `task_id`, `resource_id`, `percent` |
| `RESOURCE_NEGATIVE_RATE`, `PROJECT_BAD_CURRENCY` | Negative or non-finite hourly rate; currency not three uppercase letters. | `hourly_rate`, `currency` |
| `COST_BAD_UNIT` | `cost_report` / `task_details` unit is not `person_hours`, `person_days` or `person_years`. | `unit` |
| `COST_MISSING_RATE` (warning) | An assigned resource has no hourly rate; costs are incomplete. | `hourly_rate` |
| `TASK_UNSIZED`, `TASK_NO_CAPACITY` | Task has no duration/effort; effort-sized task has no assigned capacity. Warnings from validation, errors on the schedule result (the task is unscheduled and the result is incomplete). | `sizing`, `assignments` |
| `CAL_BAD_VALUE`, `CAL_HOURS_PER_DAY_RANGE`, `CAL_HOURS_PER_DAY_PRECISION`, `CAL_DAYS_PER_YEAR_RANGE`, `CAL_NO_WORKING_WEEKDAYS`, `CAL_WORKDAY_OVERFLOW` | Calendar arguments: wrong form, hours/day outside `(0, 24]` or not a whole number of minutes, days/year outside `1..366`, no working weekday, workday start plus hours past 24:00. | calendar field name |
| `CAL_DUPLICATE_HOLIDAY`, `CAL_DUPLICATE_EXCEPTION`, `CAL_HOLIDAY_EXCEPTION_CONFLICT` | Two holidays or two exceptions on one date; a date that is both. | `holidays`, `exceptions` |
| `CAL_START_MOVED` (info) | Project start is not a working day and moved to the next one. | `start` |

CSV import errors use `CSV_*` codes with a 1-based `line`; they are listed in [csv_format.md](csv_format.md) section 6.3.

## Units

| Quantity | Input | Output |
|---|---|---|
| Duration, effort, lag | hours or days, kept as entered | days (`Decimal`) |
| Work/cost report | `unit` code | `person_hours`, `person_days`, `person_years` (plain strings) |
| Money | `str` / `Decimal` | `Decimal`, ROUND_HALF_EVEN to 2 places for display and export |

Person-years use `hours_per_day x working_days_per_year`. Intervals are half-open internally; displayed finishes are end-of-working-period.
