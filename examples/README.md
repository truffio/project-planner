# Examples

Runnable Jupyter notebooks showing the Project Planner Python API.

| # | Notebook | What you learn |
|---|---|---|
| 1 | [`01_hello_world.ipynb`](01_hello_world.ipynb) | Build two tasks with a dependency, schedule them, and see results go out of date after an edit. |
| 2 | [`02_team_allocation.ipynb`](02_team_allocation.ipynb) | A team of four on eight tasks — spot an overloaded person (Ana at 250%) and an under-used one (Dev at 4%) with loading views, a per-person summary and text bars. |
| 3 | [`03_leveling.ipynb`](03_leveling.ipynb) | Resource leveling — preview, apply and reset; delays and their causes; why leveling never changes cost; and why reassigning the bottleneck's work beats reassigning just any work. |
| 4 | [`quickstart.ipynb`](quickstart.ipynb) | A full tour of the API (calendar, costs, loading, leveling, save/load, CSV). |

## How to open them

```powershell
.venv\Scripts\python -m pip install notebook
.venv\Scripts\python -m jupyter notebook examples/
```

The notebooks are stored without outputs. Run them top to bottom using **Kernel → Restart & Run All**.

## Charts

Notebook 02 draws text bars built from the loading data. If `matplotlib` is installed, it also draws a bar chart; otherwise that cell is skipped quietly.

## Testing

Every notebook is executed by a test that checks its printed numbers:

```powershell
.venv\Scripts\python -m pytest -q -m notebook
```

These tests verify that the notebooks' expected outputs haven't changed.
