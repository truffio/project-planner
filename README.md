# Project Planner

A locally run project-planning tool in the spirit of the planning side of Microsoft Project. You describe a work breakdown structure (WBS), task durations or effort, dependencies, people with hourly rates, and one shared work calendar. Project Planner then calculates:

- start and finish dates for every task;
- the project completion date;
- how loaded each person is over time, flagging overloads;
- labour cost per assignment, task, group and project.

It can also level resources on request, by delaying whole tasks.

This repository currently holds the **backend**: the scheduling engine, SQLite storage and a Python API. The API can be driven directly from a Jupyter notebook or a script, and it is the layer the desktop UI will be built on.

## Project status

| Part | Status |
|---|---|
| Functional specification (v1.4) | Agreed — [`Project_Planner_Functional_Specification.md`](Project_Planner_Functional_Specification.md) |
| Backend: engine, persistence, Python API | Complete; all 26 in-scope acceptance scenarios pass |
| Spec-conformance and code reviews | Done; high-priority findings fixed ([reviews](#documentation)) |
| Desktop UI (Tkinter) | Not started |

## What it does

- **Tasks sized your way.** Enter a duration or a total effort, in hours or days (`"40h"`, `"5d"`). Each task keeps exactly what you entered, and every calculated result is reported in working days.
- **Multiple people per task.** Each person has their own allocation percentage. Effort-based tasks derive their duration from the combined allocation.
- **Four dependency types.** FS, SS, FF and SF, each with a positive or negative lag counted in working time.
- **One shared calendar.** Working weekdays, hours per day, workday start time, working days per year, holidays and exception days.
- **Honest resource loading.** 130 % is shown as 130 %, never capped or averaged away.
- **Cost estimates.** Each person contributes their own hours at their own rate. Reports can show work in person-hours, person-days or person-years. A missing rate is flagged, never guessed.
- **Leveling on request.** Preview, then apply or discard. Leveling only delays whole tasks, and it never changes durations, assignments or cost.
- **Named projects in one SQLite file.** Save, save as, load and delete, with an unsaved-changes guard. Loading restores saved results exactly.
- **CSV import and export.** Import replaces the whole project and is fully validated first, so a bad file changes nothing.
- **Change tracking.** Edits never recalculate on their own. The workspace knows when the dates or the costs are out of date.

## Repository tour

```
Project_Planner_Functional_Specification.md   what the product must do (authoritative)
proposed_architectures_backend.md5            architecture options and the chosen design
implementation_plan_backend.md5               task plan used to build the backend (incl. test strategy)
src/project_planner/
  __init__.py      public API: import project_planner as pp
  engine/          pure scheduling logic: calendar, validation, sizing, forward pass,
                   leveling, loading, cost, results, CSV (no database, no UI)
  persistence/     SQLite schema, migrations, repositories
  services/        the Workspace: editing, calculation, background jobs, files, read views
  notebook.py      HTML display and DataFrame helpers for Jupyter
tests/             unit, acceptance (spec scenarios A01–A27), property, services, csv,
                   notebook, perf
examples/          quickstart.ipynb — a runnable tour
docs/              API reference, CSV format, decisions, performance, reviews
tools/             generate_large_project.py — synthetic projects for benchmarks
```

## Installation

You need Python 3.11 or newer. The package has no runtime dependencies beyond the standard library.

```powershell
git clone <repo-url> project
cd project
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

On Linux or macOS, use `.venv/bin/python` instead of `.venv\Scripts\python`. The `dev` extra installs the test and lint tools. To get pandas DataFrames in notebooks as well, install `".[dev,notebook]"`.

## Using the project planner

Everything goes through a **workspace**. A workspace is either an SQLite file holding your saved projects, or `":memory:"` for a throwaway session. The workspace always has one working project open.

### 1. A first schedule

```python
from datetime import date
import project_planner as pp

ws = pp.open_workspace(":memory:")
ws.new_project("Website relaunch", start=date(2026, 10, 5))     # a Monday

design = ws.add_task("Design", duration="3d")
build = ws.add_task("Build", duration="5d")
launch = ws.add_milestone("Launch")
ws.add_dependency(design, build, "FS")
ws.add_dependency(build, launch, "FS")

result = ws.schedule()
for row in result.tasks():
    print(f"{row.node_id:3} {row.start:%a %d %b %H:%M} -> {row.finish:%a %d %b %H:%M}  {row.duration_days} d")
print("Finish:", result.project_finish)
```
```text
t1  Mon 05 Oct 09:00 -> Wed 07 Oct 17:00  3 d
t2  Thu 08 Oct 09:00 -> Wed 14 Oct 17:00  5 d
m1  Wed 14 Oct 17:00 -> Wed 14 Oct 17:00  0 d
Finish: 2026-10-14 17:00:00
```

Weekends are skipped automatically. A task that ends at the close of a day is shown finishing at 17:00 that day, not at 09:00 the next morning.

### 2. People, effort and cost

```python
from datetime import date
import project_planner as pp

ws = pp.open_workspace(":memory:")
ws.new_project("Costing", start=date(2026, 10, 5))
alice = ws.add_resource("Alice", hourly_rate="100")
bob = ws.add_resource("Bob", hourly_rate="50")

# Effort-based task: 40 person-hours shared 80 % / 20 % -> lasts 5 working days
spec = ws.add_task("Write spec", effort="40h")
ws.set_assignment(spec, alice, percent=80)
ws.set_assignment(spec, bob, percent=20)

result = ws.schedule()
print(result.node(spec).duration_days, "days,", result.total_cost)

for unit in ("person_hours", "person_days"):
    for a in ws.cost_report(unit=unit).node(spec).assignments:
        print(f"  {a.resource_id}: {a.work_qty} {a.work_unit} x {a.rate_per_unit} = {a.cost}")
```
```text
5 days, 3600.0
  r1: 32.0 person_hours x 100 = 3200.0
  r2: 8.0 person_hours x 50 = 400.0
  r1: 4.0 person_days x 800 = 3200.0
  r2: 1.0 person_days x 400 = 400.0
```

The reporting unit changes only how work and rates are shown. The money is identical in every unit. Money and rates are passed as strings or `Decimal` and are never floats.

### 3. Your working calendar

```python
from datetime import date
import project_planner as pp

ws = pp.open_workspace(":memory:")
ws.new_project("Calendar demo", start=date(2026, 10, 5))
ws.calendar.initialize(                       # every argument is optional
    hours_per_day="7.5",
    working_days_per_year=225,                # used for person-year cost reports
    working_weekdays="Mon-Fri",
    workday_start="08:30",
    holidays=[(date(2026, 10, 8), "Founders' day")],
)
t = ws.add_task("Audit", duration="4d")
row = ws.schedule().node(t)
print(row.start, "->", row.finish)            # Mon, Tue, Wed, (holiday), Fri
print(ws.node(t).sizing)                      # what you entered is kept
```
```text
2026-10-05 08:30:00 -> 2026-10-09 16:00:00
4d
```

### 4. Overloads and leveling

```python
from datetime import date
import project_planner as pp

ws = pp.open_workspace(":memory:")
ws.new_project("Leveling demo", start=date(2026, 10, 5))
alice = ws.add_resource("Alice", hourly_rate="100")
a = ws.add_task("Task A", duration="2d")
b = ws.add_task("Task B", duration="2d")
ws.set_assignment(a, alice, percent=70)
ws.set_assignment(b, alice, percent=70)

ws.schedule()
print("Peak load:", max(s.percent for s in ws.loading(alice).segments), "%")

preview = ws.level_preview()                  # nothing changes until you apply
print("Delays (days):", dict(preview.delays_days))
ws.apply_leveling()                           # or ws.discard_leveling()
print("Peak after leveling:", max(s.percent for s in ws.loading(alice).segments), "%")
print("Finish:", ws.result().project_finish, "| cost:", ws.result().total_cost)
```
```text
Peak load: 140 %
Delays (days): {'t2': Decimal('2')}
Peak after leveling: 70 %
Finish: 2026-10-08 17:00:00 | cost: 2240.0
```

`ws.reset_to_dependency_schedule()` returns to the schedule without leveling.

### 5. Saving, loading and CSV

```python
from datetime import date
import project_planner as pp

with pp.open_workspace("plans.db") as ws:     # one workspace per file at a time
    ws.new_project("Office move", start=date(2026, 10, 5))
    ws.add_task("Pack", duration="2d")
    ws.schedule()
    ws.save_as("Office move")
    ws.export_csv("office_move.csv")          # portable text copy

with pp.open_workspace("plans.db") as ws:
    print([p.name for p in ws.list_projects()])
    ws.load(ws.list_projects()[0])            # restores the saved schedule as-is
    print(ws.result().project_finish)
    ws.add_task("Unpack", duration="1d")
    print("unsaved:", ws.state().dirty, "| dates out of date:", ws.state().stale_dates)
    try:
        ws.import_csv("office_move.csv")
    except pp.UnsavedChanges:
        print("save or pass discard_unsaved=True first")
    summary = ws.import_csv("office_move.csv", discard_unsaved=True)
    print("imported", summary.nodes, "node(s)")
```
```text
['Office move']
2026-10-06 17:00:00
unsaved: True | dates out of date: True
save or pass discard_unsaved=True first
imported 1 node(s)
```

The CSV layout, including all error codes, is described in [`docs/csv_format.md`](docs/csv_format.md).

### 6. When something is wrong

Errors are typed exceptions. Each one carries field-level issues with a stable `code`, and every problem found in a call is reported together.

```python
from datetime import date
import project_planner as pp

ws = pp.open_workspace(":memory:")
ws.new_project("Errors", start=date(2026, 10, 5))
a = ws.add_task("A", duration="1d")
b = ws.add_task("B", duration="1d")
ws.add_dependency(a, b)
try:
    ws.add_dependency(b, a)                   # would close a cycle
except pp.ValidationFailed as exc:
    for issue in exc.issues:
        print(issue.code, "-", issue.message)
try:
    ws.add_task("C", duration=5)              # unit missing
except TypeError as exc:
    print("TypeError:", exc)
```
```text
DEP_CYCLE - dependency cycle among 2 nodes: t1, t2
TypeError: bare number 5 has no time unit; use hours(5) or days(5), or a string such as '5h' (hours) or '5d' (days)
```

### 7. Background calculation (for UIs)

`ws.schedule()` and `ws.level_preview()` block until they finish, which suits notebooks. A UI should use the `submit_*` forms instead. They return a `Job` with `progress`, `cancel()` and `result()`, and the calculation runs in a separate process.

```python
from datetime import date
import project_planner as pp

if __name__ == "__main__":                    # required: jobs use a process pool
    ws = pp.open_workspace(":memory:")
    ws.new_project("Background", start=date(2026, 10, 5))
    ws.add_task("Big task", duration="10d")
    job = ws.submit_schedule()                # returns immediately
    result = job.result(timeout=60)           # a UI would poll ws.poll_jobs() instead
    print(job.status, result.project_finish)
```
```text
done 2026-10-16 17:00:00
```

A workspace belongs to the thread that opened it. In Tkinter, poll from the UI thread with `root.after(100, ...)`, calling `ws.poll_jobs()` each time. A frozen (PyInstaller) app must also call `multiprocessing.freeze_support()`. See [`docs/api.md`](docs/api.md) for the full pattern.

### In a Jupyter notebook

Results, cost reports, loading views and task details display as tables. They also offer `.to_records()`, and `.to_dataframe()` if pandas is installed. Try these notebooks:

- [`01_hello_world.ipynb`](examples/01_hello_world.ipynb) — your first schedule with two tasks and a dependency.
- [`02_team_allocation.ipynb`](examples/02_team_allocation.ipynb) — a team of four on eight tasks; spot overloads with loading views and text bars.
- [`03_leveling.ipynb`](examples/03_leveling.ipynb) — resource leveling by delaying tasks, and how reassigning work compares.
- [`quickstart.ipynb`](examples/quickstart.ipynb) — a complete tour of the API.

See [`examples/README.md`](examples/README.md) for how to open and test them.

```powershell
.venv\Scripts\python -m pip install notebook
.venv\Scripts\python -m jupyter notebook examples/
```

## Running the tests

Run all of these from the repository root. The everyday run takes about half a minute and skips the slow benchmarks and the notebook test.

```powershell
.venv\Scripts\python -m pytest -q
```

| What | Command |
|---|---|
| Spec acceptance scenarios only (A01–A27) | `.venv\Scripts\python -m pytest -q tests/acceptance` |
| One file | `.venv\Scripts\python -m pytest -q tests/unit/test_leveling.py` |
| Tests whose name matches a word | `.venv\Scripts\python -m pytest -q -k cost` |
| Execute the example notebook | `.venv\Scripts\python -m pytest -q -m notebook` |
| Performance benchmarks (1k / 10k / 50k tasks, ~2 min) | `.venv\Scripts\python -m pytest -q -m perf -s` |
| Thorough property tests (500 random cases per check) | `$env:HYPOTHESIS_PROFILE="ci"; .venv\Scripts\python -m pytest -q tests/property` |
| Coverage report | `.venv\Scripts\python -m pytest -q --cov=project_planner --cov-report=term-missing` |

Useful flags:
- `-v` lists every test by name.
- `-x` stops at the first failure.
- `--lf` re-runs only the tests that failed last time.

Lint and type checks:

```powershell
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m mypy
```

To type `pytest` and `python` directly, activate the environment once per terminal with `.venv\Scripts\Activate.ps1`.

**What the test suite covers:**
- **Acceptance tests:** every spec scenario, with hand-computed expected dates and costs.
- **Unit tests:** each engine module.
- **Property tests (Hypothesis):** random projects that must always satisfy invariants. For example, every dependency holds, leveling never changes cost, and save/load and CSV round-trip exactly.
- **Service tests:** run against real SQLite and include deliberate failure injection, to prove that a failed save, load or import changes nothing.

## Performance

Both of the spec's targets are met with a wide margin, measured on 10,000-task projects:
- scheduling takes about 0.9 s, against a target of 2 s;
- leveling takes about 1.1 s, against a target of 30 s.

Full results for 1k, 10k and 50k tasks are in [`docs/performance.md`](docs/performance.md).

## Documentation

- [`docs/api.md`](docs/api.md): Python API reference and the error and issue codes.
- [`docs/csv_format.md`](docs/csv_format.md): CSV import/export format.
- [`docs/decisions.md`](docs/decisions.md): every resolved design decision (D1–D15).
- [`docs/performance.md`](docs/performance.md): benchmark results and hotspots.
- [`docs/review_spec_conformance.md`](docs/review_spec_conformance.md) and [`docs/review_code.md`](docs/review_code.md): independent reviews and their findings.
- [`docs/contracts.md`](docs/contracts.md): internal module contracts, for contributors.
