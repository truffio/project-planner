# Performance benchmarks (T42)

Targets from the functional specification section 4.1: scheduling 10k tasks in under 2 s, leveling 10k tasks in under 30 s. Everything else below is recorded for information.

## Method

- Reproduce: `python -m pytest -q -m perf tests/perf -s` (excluded from the default run). Total time of the perf run: about 110 s.
- Projects come from `tools/generate_large_project.py` (`generate(n_tasks, seed=0)`, seed 0): phases -> work packages -> leaves (depth 3, about 10 leaves per package, 10 packages per phase), about 5% milestones, duration or effort sizing in h and d, about 1 resource per 25 tasks, 1-3 assignments per task at 25-100%, about 1.3 dependencies per leaf (mostly FS inside packages, cross-package links of all four types with signed lags), six holidays. The 10k project has 11,100 nodes, about 12.2k dependencies and about 18.9k assignments. `python tools/generate_large_project.py 10000 out.csv` writes it as CSV.
- Timings are wall-clock `time.perf_counter`, best of 3 for 1k and 10k, a single run for 50k. `wbs_rows`/`gantt_rows` are warm (caches primed, best of 5). `import_csv`, `save_as`, `load`, `export_csv` run on a workspace backed by a file database in a pytest `tmp_path`; `import_csv` reads a CSV file path; `save_as` and `load` include the stored schedule result (a schedule is computed before saving); `set_sizing` is one edit on the loaded workspace. `schedule` and `level` call `engine.schedule.schedule` / `level` directly (level uses the dependency-only result as base).
- Machine: Windows-10-10.0.26200-SP0, AMD64 Family 25 Model 97 (AMD, 12 logical CPUs), CPython 3.11.11. Numbers vary by machine and load.

## Results (seconds)

| Operation | 1,000 | 10,000 | 50,000 |
|---|---:|---:|---:|
| generate (not product code) | 0.0154 | 0.1789 | 1.2015 |
| `schedule` | 0.0573 | 0.8510 | 7.2649 |
| `level` | 0.0701 | 1.0942 | 9.2904 |
| `import_csv` | 0.1840 | 2.0778 | 16.6160 |
| `save_as` | 0.0219 | 0.2426 | 1.6404 |
| `load` | 0.0769 | 0.9988 | 6.2525 |
| `export_csv` | 0.1507 | 1.5658 | 10.9362 |
| `wbs_rows(limit=50)` warm | < 0.0001 | 0.0002 | 0.0001 |
| `gantt_rows(limit=50)` warm | < 0.0001 | 0.0001 | 0.0001 |
| `set_sizing` (one edit) | 0.0032 | 0.0434 | 0.8046 |

## Against the targets

| Target (10k tasks) | Measured | Met |
|---|---:|---|
| schedule < 2 s | 0.85 s | yes |
| level < 30 s | 1.09 s | yes |

The test asserts both. Scaling from 10k to 50k is roughly linear to slightly super-linear (schedule 8.5x, level 8.5x, import 8x, load 6x, set_sizing 18x for 5x the size).

## Hotspots (cProfile, 10k, profiler overhead inflates absolute times)

Not optimized; reported for the orchestrator.

**`schedule` (1.38 s under profile).** `_assemble` (result assembly) takes about 63% (0.88 s); the forward pass itself is small. Within it: `ScheduleResult.__init__` 0.51 s, fingerprints (`schedule_fp`/`cost_fp`, `_schedule_payload`) about 0.16-0.2 s, `_cost_views` 0.19 s (`compute_costs` 0.15 s), `compute_sizing` 0.18 s (`size_node` x10k), `validate` 0.19 s (`network.cycles`/`strongly_connected_components` run 3 times, 0.11 s), `compute_loading` 0.09 s, `topological_order` 0.09 s. About 110k `sorted` calls cost 0.19 s in total.

**`level`.** 2.5 s under profile, but `leveling.level` itself is only 0.43 s; the rest (about 1.3 s) is again `_assemble` of the leveled result plus fingerprints (0.35 s), so leveling is dominated by result assembly, not the algorithm.

**`import_csv` (6.4 s under profile).** `csv_io.parse` is 5.4 s: `_phase1` 4.5 s, of which `_tokenize` (pure-Python tokenizer) 1.9 s, `_check_columns` 0.7 s (43k calls), `_trim` 2.0M calls 0.46 s; `_phase2` 0.9 s. Then `reload` 0.62 s (`load_project` 0.58 s), `save_project` 0.42 s (sqlite `executemany` 0.36 s), `validate` 0.36 s. The wide 45-column header means about 2M cells per 10k tasks.

**`export_csv`.** 4.1 s under profile, nearly all in `_line`/`_quote`: 1.96M `_quote` calls each doing a per-character `any(...)` over the cell (9.8M generator steps). A fast-path check (e.g. a regex or `str.translate` test) would remove most of it.

**`set_sizing`.** 0.22 s at 10k under profile, all in `Workspace._replace` -> `dataclasses.replace(Project)` -> `Project.__post_init__` (re-sorting and re-indexing all collections, `_sorted_unique`, key dictionary). Every single edit pays an O(n) project rebuild, hence 0.8 s at 50k.

## Surprises

- The engine passes are cheap; fixed costs around them (result assembly, fingerprints, repeated validation/SCC passes, CSV tokenizing/quoting, O(n) project rebuild per edit) dominate.
- Per-edit cost is O(n): about 43 ms at 10k and 0.8 s at 50k, which may matter for interactive editing of very large projects.
- Windowed reads are effectively free once warm (about 0.1 ms).
- All sizes are far inside the 10k targets; 50k remains under about 17 s for every operation.
