# Python API reference

Everything below is importable from the package root (`import project_planner as pp`). `pp.__all__` is the supported surface; anything else is internal.

```python
import project_planner as pp
from datetime import date

ws = pp.open_workspace(":memory:")
ws.new_project("Demo", start=date(2026, 10, 5))
t = ws.add_task("Design", effort=pp.hours(40))
print(ws.schedule().working_span_days)
```

Conventions: all IDs are strings; every method that takes an ID also accepts an object with an `.id`; money is `Decimal` (pass `str` or `Decimal`, never `float`); planning outputs are in days.

## Workspace lifecycle

- `pp.open_workspace(path=":memory:", *, config=pp.Config())` returns a `Workspace` (creates and migrates the SQLite file if needed). A fresh database holds an empty "Untitled" project.
- `ws.close()`, or `with pp.open_workspace(...) as ws:`.
- `ws.new_project(name, start, *, currency="USD", cost_report_unit="person_days", calendar=None, discard_unsaved=False)`.
- `ws.subscribe(callback) -> unsubscribe` for change events.
- `ws.state()` returns `WorkspaceState(revision, dirty, stale_dates, stale_costs, has_preview, has_result)`. `ws.result()` is the stored current result (leveled if applied, else dependency-only, else `None`); it may be stale, compare with `state()`.
- A workspace is bound to the thread that opened it. Use it from that thread only.

## Editing

Resources: `add_resource(name, hourly_rate=None) -> id`, `rename_resource`, `set_hourly_rate` (costs refresh immediately, dates do not go stale), `remove_resource`.

Nodes: `add_group(name, parent=None)`, `add_task(name, parent=None, duration=..., effort=...)`, `add_milestone(name, parent=None)`, `rename_node`, `move_node(node, parent, index)`, `set_sizing(task, duration=... | effort=...)`.

Assignments: `set_assignment(task, resource, percent)` (upsert), `remove_assignment`.

Dependencies: `add_dependency(pred, succ, type="FS", lag=0)`, `edit_dependency`, `remove_dependency`. Cycles, self-dependencies, group endpoints and duplicates raise `ValidationFailed` immediately.

Project settings: `set_project_start`, `rename_project`, `set_currency`, `set_cost_report_unit`.

Deleting: `ws.delete_preview(ids, resources=()) -> DeletePreview`, then `ws.delete_commit(preview.token)`.

Reads: `ws.project()` (immutable snapshot), `ws.node(id)`, `ws.resource(id)`, `ws.dependency(id)`.

Editing never recalculates the schedule. Results become stale (see `state()`); call `schedule()` again.

### Time inputs

Duration, effort and lag are `TimeQty` values: `pp.hours(40)`, `pp.days(2)`, `pp.days("-0.5")`, or strings such as `"40h"`, `"2d"`, `"-0.5d"`. Bare numbers (`duration=5`) raise `TypeError` naming both forms; a unit-less string such as `"40"` raises `ValidationFailed`. `pp.hours()` / `pp.days()` also accept float literals (`pp.days(-0.5)`), converted exactly through their shortest decimal text. Quantities are stored exactly as entered; days convert with the calendar's hours per day at calculation time.

## Calendar

`ws.calendar` (`CalendarApi`):

- `settings() -> pp.CalendarSettings` (equality-comparable; `pp.CalendarSettings()` equals the defaults).
- `initialize(*, working_weekdays="Mon-Fri", hours_per_day=8, working_days_per_year=220, workday_start="09:00", holidays=(), exceptions=())`. All arguments optional; one `ValidationFailed` lists every invalid field and nothing is applied. Holidays are `(date, name)`, exceptions `(date, "working" | "nonworking"[, name])`.
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

Results are stored on the workspace's own thread, the first time you call `job.result()`, `job.done()`, `job.poll()`, read `job.status`, call `ws.poll_jobs()`, or call `ws.state()` / `ws.result()` (both poll first). A job never overwrites a newer stored result or a replaced project; cancelling stores nothing; edits made while a job runs make its result stale on arrival.

### Tkinter polling

The workspace must be used from the thread that opened it, so do not touch it from worker threads. Poll from the Tk main loop:

```python
def tick():
    for job in ws.poll_jobs():          # jobs that finished during this call
        refresh_views()
    if job_is_running:
        root.after(100, tick)

job = ws.submit_schedule()
root.after(100, tick)
progress_bar["value"] = job.progress * 100   # read in the same tick
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

- `ws.save()` overwrites the tracked saved copy (the first save uses the project name; a name collision raises `Conflict`). `ws.save_as(name)`. Both return `ProjectInfo(id, name, saved_at)`.
- `ws.list_projects() -> list[ProjectInfo]` (newest first), `ws.load(project_id, discard_unsaved=False)`, `ws.delete_project(project_id)`. Stored results are restored verbatim on load.
- `ws.export_csv(destination=None) -> str` returns the CSV text and writes the file (UTF-8, no BOM) when a path is given. Stale results are marked stale in the export.
- `ws.import_csv(source, discard_unsaved=False) -> ImportSummary`. `source` is a path, a `Path`, an open text file, or CSV text (a string containing a newline). The file is parsed completely before anything changes; errors raise `ImportFailed` with every `Issue` (1-based `line`, column in `field`) and leave the workspace untouched. After a successful import the workspace is dirty and `ws.result()` is `None`.
- Replacing operations (`load`, `import_csv`, `new_project`) raise `UnsavedChanges` when the workspace is dirty unless `discard_unsaved=True`. The CSV format is described in `docs/csv_format.md`.

## Read models

All take IDs or objects and are cheap to call repeatedly (cached per revision and result).

- `ws.cost_report(unit=None) -> CostReportView` (`unit`, `total_cost`, `work_qty`, `complete`, `missing_rate_resources`, `nodes`, `.node(id)`). `unit` is `"person_hours" | "person_days" | "person_years"`; cost totals are identical across units. `Conflict` without a result.
- `ws.task_details(task, unit=None) -> TaskDetails` (sizing, dates, cost, predecessors, successors, `assignments`).
- `ws.loading(resource, *, time_window=None, granularity="segments" | "day" | "week") -> LoadingView` (`segments` with `overloaded` flags, `buckets` for day/week).
- `ws.wbs_rows(*, parent=None, expanded_ids=(), offset=0, limit=None) -> WbsPage(rows, total)`, `ws.gantt_rows(...)`, `ws.dependency_links(nodes)`, `ws.nonworking_ranges(start, end)`, `ws.project_summary()`.

## Results

`ScheduleResult`: `kind` (`"dependency_only" | "leveling_preview" | "leveled"`), `complete`, `project_start`, `project_finish` (`None` unless complete), `working_span_days`, `elapsed_span_calendar_days`, `effort_days`, `total_cost` / `cost_total`, `cost_complete`, `missing_rate_resources`, `issues`, and lookups `node(id)`, `assignment(task, resource)`, `dependency(id)` (raise `NotFound`), `tasks()`.

`NodeResult`: `start`, `finish` (naive datetimes; finish uses the end-of-working-period convention, e.g. 17:00), `duration_days`, `effort_days`, `leveling_delay_days`, `cost`, `cost_complete`, `status`, `reason`, `wbs_number`.

`LevelingResult`: `result` (kind `leveling_preview`), `delays_days`, `delays`, `unresolved`, `finish_delta_days`.

### Notebook helpers

`ScheduleResult`, `LevelingResult`, `CostReportView`, `LoadingView`, `TaskDetails` and `ws.calendar` render as HTML tables in Jupyter (`_repr_html_`). Each has:

- `.to_records() -> list[dict]` (plain values, no dependencies). For a `ScheduleResult` this is one row per task with `id, wbs, name, kind, status, start, finish, duration_days, effort_days, leveling_delay_days, cost, cost_complete`. Task names are attached by the workspace when you obtain the result through `ws.schedule()` / `ws.result()`; otherwise the ID is shown.
- `.to_dataframe()` builds a pandas `DataFrame`. When pandas is not installed it raises `ImportError` telling you to run `pip install project_planner[notebook]`.

## Errors

All derive from `pp.PlannerError`: `ValidationFailed(issues)`, `ImportFailed(issues)`, `NotFound(object_type, object_id)` (also a `LookupError`), `Conflict`, `UnsavedChanges`, `Cancelled`. An `Issue` carries `severity`, `code`, `message`, `object_type`, `object_id`, `field` and (imports) `line`. Wrong Python types (a bare number as a duration, a float as money) raise `TypeError`.

## Units

| Quantity | Input | Output |
|---|---|---|
| Duration, effort, lag | hours or days, kept as entered | days (`Decimal`) |
| Work/cost report | `unit` code | `person_hours`, `person_days`, `person_years` (plain strings) |
| Money | `str` / `Decimal` | `Decimal`, ROUND_HALF_EVEN to 2 places for display and export |

Person-years use `hours_per_day x working_days_per_year`. Intervals are half-open internally; displayed finishes are end-of-working-period.
