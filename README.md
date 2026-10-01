# Project Planner

Project Planner backend: a scheduling engine (dependencies, working calendar, resource loading, cost, resource leveling), SQLite persistence with named projects and CSV import/export, and a Python API (`import project_planner as pp`) that a desktop UI or a Jupyter notebook can drive directly. There is no server and no UI in this package.

## Requirements

- Python 3.11 or newer.
- No runtime dependencies (standard library and `sqlite3` only).
- Optional: `pandas`, via the `notebook` extra, for `.to_dataframe()`.

## Installation

```bash
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"      # development: pytest, hypothesis, ruff, mypy, nbclient, ipykernel
```

On Linux or macOS use `.venv/bin/python`. To use the package without the development tools, run `pip install -e .`; add `".[notebook]"` for pandas.

## Quickstart

```python
import tempfile
from datetime import date
from pathlib import Path

import project_planner as pp

ws = pp.open_workspace(":memory:")             # or a file path such as "plans.db"
ws.new_project("Demo", start=date(2026, 10, 5), currency="USD", cost_report_unit="person_days")
ws.calendar.initialize(                        # all arguments optional
    hours_per_day=8,
    working_days_per_year=220,
    working_weekdays="Mon-Fri",
    workday_start="09:00",
    holidays=[(date(2026, 10, 12), "Holiday")],
)

alice = ws.add_resource("Alice", hourly_rate="100")   # money: str or Decimal, never float
bob = ws.add_resource("Bob", hourly_rate="50")
phase1 = ws.add_group("Phase 1")
design = ws.add_task("Design", parent=phase1, effort=pp.hours(40))   # input in hours...
ws.set_assignment(design, alice, percent=80)
ws.set_assignment(design, bob, percent=20)
build = ws.add_task("Build", parent=phase1, duration=pp.days(2))     # ...or in days
ws.add_dependency(design, build, "FS", lag=pp.days(-0.5))            # "40h", "2d", "-0.5d" also work

result = ws.schedule()                          # synchronous; edits never recalculate by themselves
print(result.complete, result.project_finish, result.total_cost)     # True 2026-10-14 13:00:00 3600.0
print(result.working_span_days)                                      # 6.5 (outputs are in days)
for row in result.tasks():
    print(row.node_id, row.start, row.finish, row.duration_days, row.effort_days, row.cost)

print(ws.cost_report(unit="person_hours").total_cost)  # cost totals are identical in every unit
print(ws.loading(alice).segments[0].percent)            # piecewise loading with overload flags

# Resource leveling: preview, then apply (or discard / reset).
review = ws.add_task("Review", parent=phase1, duration=pp.days(3))
ws.set_assignment(review, alice, percent=60)            # overlaps Design: Alice is at 140 %
ws.schedule()
preview = ws.level_preview()
print(preview.delays_days, preview.finish_delta_days)
ws.apply_leveling()                                     # the preview becomes ws.result()
ws.reset_to_dependency_schedule()                       # back to the dependency-only schedule

# Save, CSV round trip.
with tempfile.TemporaryDirectory() as tmp:
    info = ws.save_as("Demo")
    print(info.name, [p.name for p in ws.list_projects()])
    path = Path(tmp) / "demo.csv"
    ws.export_csv(path)
    other = pp.open_workspace(":memory:")
    summary = other.import_csv(path)
    print(summary.nodes, other.schedule().total_cost)
    other.close()
ws.close()
```

The same flow is available as an executed notebook: [`examples/quickstart.ipynb`](examples/quickstart.ipynb).

## Key concepts

- **Working-time axis.** Scheduling runs on an integer-minute axis of working time only (non-working days, holidays and hours outside the workday do not exist on it). Intervals are half-open, `[start, finish)`; displayed finishes use the end-of-working-period convention (a task ending Friday shows `Friday 17:00`, not Monday 09:00).
- **Units.** Duration, effort and lag are entered in hours or days (`pp.hours(40)`, `pp.days(2)`, `"40h"`, `"-0.5d"`) and stored exactly as entered; bare numbers are rejected. Days convert using the calendar's hours per day at calculation time. All planning outputs are in **days** (`duration_days`, `effort_days`, `leveling_delay_days`, ...), as `Decimal`. Effort days are person-days.
- **Cost reporting units.** `cost_report_unit` is `person_hours`, `person_days` (default) or `person_years` (hours per day times working days per year). It changes work quantities only; cost totals are identical. Money is `Decimal`, one currency code per project, rounded half-even to 2 places for display and export.
- **Calendar initialisation.** Every new project starts with a calendar (Mon-Fri, 8 h/day, 220 days/year, 09:00 start). `ws.calendar.initialize(...)` sets everything at once and validates all arguments together; setters adjust single fields. CSV import never inherits calendar values from the previously open project.
- **Dependencies.** Types `FS`, `SS`, `FF`, `SF`. Lag is signed (`-0.5d` is a lead) and in hours or days. Cycles, self-dependencies, duplicates and dependencies on groups are rejected when you add them.
- **Leveling.** Never runs by itself. `level_preview()` computes delays without changing the current result; `apply_leveling()` makes the preview current; `discard_leveling()` drops it; `reset_to_dependency_schedule()` returns to the dependency-only result. Priority is earliest dependency-only start, then WBS order, then ID.
- **Staleness and fingerprints.** Editing does not recalculate. The workspace compares fingerprints of the project definition against those stored with the result, and `ws.state()` reports `stale_dates` and `stale_costs`. A rate change refreshes costs immediately without staling dates; anything that can move dates marks them stale until you call `schedule()` again.
- **Save/load vs CSV.** `save`, `save_as`, `load`, `list_projects` and `delete_project` manage named projects in the SQLite file, restoring stored results verbatim. CSV (`export_csv`, `import_csv`) is a portable text description of one project, parsed completely before anything changes; calculated columns in a CSV are never imported. Replacing operations (`new_project`, `load`, `import_csv`) raise `UnsavedChanges` on a dirty workspace unless `discard_unsaved=True`.

## Usage patterns

**Notebooks and scripts** call the synchronous forms (`ws.schedule()`, `ws.level_preview()`). Results render as HTML tables in Jupyter and have `.to_records()` (plain dicts) and `.to_dataframe()` (needs pandas).

**UIs** call the `submit_*` forms, which return a `Job` immediately (`ws.submit_schedule()`, `ws.submit_leveling_preview()`). A workspace belongs to the thread that opened it, so do not touch it from worker threads. Instead poll from the UI thread; with Tkinter use `after()`:

```python
def tick():
    for job in ws.poll_jobs():        # jobs that finished since the last call
        refresh_views()
    progress_bar["value"] = job.progress * 100
    if not job.done():
        root.after(100, tick)

job = ws.submit_schedule()
root.after(100, tick)
```

(Tkinter snippet, illustrative only; `ws`, `root`, `progress_bar` and `refresh_views` belong to your application.)

Jobs run in a spawn-based process pool, so scripts that submit jobs must guard their entry point, and a frozen (PyInstaller) app must call `freeze_support()`:

```python
if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
```

For tests and simple scripts, `ws.submit_schedule(executor=pp.InlineExecutor())` runs the job on the calling thread.

## Error model

Failures are typed exceptions deriving from `pp.PlannerError`: `ValidationFailed` and `ImportFailed` (both carry a list of `Issue`s), `NotFound`, `Conflict`, `UnsavedChanges`, `Cancelled`. Each `Issue` is field-addressed: `severity`, a stable `code`, `message`, `object_type`, `object_id`, `field` and, for CSV imports, the 1-based `line`. All problems found in one call are reported together. Wrong Python types (a bare number as a duration, a `float` as money) raise `TypeError`. See the code table in [docs/api.md](docs/api.md).

```python
try:
    ws.add_dependency(design, design)
except pp.ValidationFailed as exc:
    for issue in exc.issues:
        print(issue.code, issue.object_type, issue.field)   # DEP_SELF dependency succ_id
```

## Running tests, lint and type checks

```bash
.venv\Scripts\python -m pytest                    # default: everything except perf and notebook
.venv\Scripts\python -m pytest -m notebook        # executes examples/quickstart.ipynb
.venv\Scripts\python -m pytest -m perf            # benchmarks (slow)
set HYPOTHESIS_PROFILE=ci                         # more property-test examples (PowerShell: $env:HYPOTHESIS_PROFILE="ci")
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m mypy
```

## Project layout

```
src/project_planner/
  __init__.py       public facade (pp.__all__ is the supported surface)
  notebook.py       HTML reprs, to_records, to_dataframe
  engine/           pure scheduling logic: model, calendar, validation, sizing, forward pass,
                    leveling, loading, cost, csv_io, results (no I/O)
  persistence/      SQLite schema, migrations, repositories
  services/         workspace facade, edit/compute/files services, background jobs, read models
tests/              unit, acceptance, property, services, csv, notebook, perf
examples/           quickstart.ipynb
docs/               api.md, csv_format.md, decisions.md, contracts.md, performance.md
```

## Documentation

- [docs/api.md](docs/api.md): Python API reference.
- [docs/csv_format.md](docs/csv_format.md): CSV import/export format and error codes.
- [docs/decisions.md](docs/decisions.md): resolved design decisions (leveling, units, rounding, ...).
- [docs/performance.md](docs/performance.md): benchmark results and targets.
- [examples/quickstart.ipynb](examples/quickstart.ipynb): runnable tour of the API.
