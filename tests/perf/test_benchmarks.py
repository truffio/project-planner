"""Performance benchmarks (T42). Run with ``pytest -m perf tests/perf -s``.

Measures the engine and workspace operations on generated projects of 1k, 10k and
50k tasks, prints a results table, and asserts only the spec 4.1 targets for 10k
(schedule < 2 s, leveling < 30 s). Everything else is recorded, not asserted.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import project_planner as pp
from project_planner.engine import csv_io
from project_planner.engine import schedule as engine_schedule

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from generate_large_project import generate  # noqa: E402

pytestmark = pytest.mark.perf

SIZES = (1_000, 10_000, 50_000)
SEED = 0
SCHEDULE_TARGET_10K = 2.0
LEVEL_TARGET_10K = 30.0

RESULTS: dict[int, dict[str, float]] = {}
OPS = (
    "generate",
    "schedule",
    "level",
    "import_csv",
    "save_as",
    "load",
    "export_csv",
    "wbs_rows(50)",
    "gantt_rows(50)",
    "set_sizing",
)


def _time(fn: Callable[[], object], repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def _print_table() -> None:
    sizes = sorted(RESULTS)
    lines = [
        "",
        "Benchmark results (seconds, best of N)",
        f"{'operation':<16}" + "".join(f"{n:>12,}" for n in sizes),
    ]
    for op in OPS:
        row = "".join(
            f"{RESULTS[n][op]:>12.4f}" if op in RESULTS[n] else f"{'-':>12}" for n in sizes
        )
        lines.append(f"{op:<16}{row}")
    print("\n".join(lines))


@pytest.mark.parametrize("n", SIZES)
def test_benchmark(n: int, tmp_path: Path) -> None:
    repeats = 1 if n >= 50_000 else 3
    res: dict[str, float] = {}
    RESULTS[n] = res

    t0 = time.perf_counter()
    project = generate(n, SEED)
    res["generate"] = time.perf_counter() - t0

    base = engine_schedule.schedule(project)
    assert base.complete
    res["schedule"] = _time(lambda: engine_schedule.schedule(project), repeats)
    res["level"] = _time(lambda: engine_schedule.level(project, base), repeats)

    csv_path = tmp_path / "project.csv"
    csv_path.write_text(csv_io.export(project), encoding="utf-8", newline="")

    with pp.open_workspace(tmp_path / "bench.db") as ws:

        def do_import() -> None:
            ws.import_csv(csv_path, discard_unsaved=True)

        res["import_csv"] = _time(do_import, repeats)
        ws.schedule()

        saved: list[pp.ProjectInfo] = []

        def do_save() -> None:
            saved.append(ws.save_as(f"bench-{len(saved)}"))

        res["save_as"] = _time(do_save, repeats)

        def do_load() -> None:
            ws.load(saved[-1].id, discard_unsaved=True)

        res["load"] = _time(do_load, repeats)
        assert ws.result() is not None

        out = tmp_path / "export.csv"
        res["export_csv"] = _time(lambda: ws.export_csv(out), repeats)

        ws.wbs_rows(limit=50)  # warm the caches
        ws.gantt_rows(limit=50)
        res["wbs_rows(50)"] = _time(lambda: ws.wbs_rows(limit=50), 5)
        res["gantt_rows(50)"] = _time(lambda: ws.gantt_rows(limit=50), 5)

        task = next(nd for nd in ws.project().nodes if nd.kind.value == "task")
        res["set_sizing"] = _time(lambda: ws.set_sizing(task.id, duration=pp.days(3)), repeats)

    _print_table()
    if n == 10_000:
        assert res["schedule"] < SCHEDULE_TARGET_10K, f"schedule 10k took {res['schedule']:.2f}s"
        assert res["level"] < LEVEL_TARGET_10K, f"level 10k took {res['level']:.2f}s"
