"""A09, A10 (replacement CSV import), A11 (save / reopen / load), A12 (export all nodes).

CSV text is produced by the system itself (export from a second workspace) and,
for A09, corrupted in a format-agnostic way (assumption A18 in conftest.py). This
keeps these tests independent of the fixture files T02 writes in parallel.
"""

from __future__ import annotations

from datetime import date

import pytest

import project_planner as pp

from .conftest import (
    D,
    calendar_settings,
    current,
    id_of,
    new_project,
    new_ws,
    node_row,
    oct26,
    project_id_by_name,
    read_csv_rows,
    record_type,
    sizing_of,
    snapshot,
    write_csv_rows,
)


def _source_project():
    """A small valid project to export: group, 3 tasks, 2 resources, a holiday, deps."""
    ws = new_ws()
    new_project(ws, "Source")
    ws.calendar.initialize(holidays=[(date(2026, 10, 8), "Holiday")])
    alice = ws.add_resource("Alice", hourly_rate="100")
    bob = ws.add_resource("Bob", hourly_rate="50")
    g = ws.add_group("Phase")
    t1 = ws.add_task("Design", parent=g, effort="40h")
    ws.set_assignment(t1, alice, percent=80)
    ws.set_assignment(t1, bob, percent=20)
    t2 = ws.add_task("Build", parent=g, duration="2d")
    t3 = ws.add_task("Test", duration="4h")
    ws.add_dependency(t1, t2, "FS", lag="-4h")
    ws.add_dependency(t2, t3, "SS", lag="1d")
    ws.schedule()
    return ws, [g, t1, t2, t3], (t1, t2)


def _target_project():
    """The project already in the workspace before an import."""
    ws = new_ws()
    new_project(ws, "Old")
    carol = ws.add_resource("Carol", hourly_rate="70")
    old = ws.add_task("Old task", duration="3d")
    ws.set_assignment(old, carol, percent=100)
    ws.schedule()
    return ws, [old]


def _exported(tmp_path, ws, name="export.csv"):
    path = tmp_path / name
    ws.export_csv(str(path))
    return path


# ---------------------------------------------------------------- A09


def _corrupt_missing_project(header, rows):
    return [r for r in rows if record_type(header, r) != "PROJECT"], None


def _corrupt_dangling_successor(header, rows):
    cols = [i for i, h in enumerate(header) if "succ" in h.lower()]
    assert cols, header
    out, line = [], None
    for idx, r in enumerate(rows):
        r = list(r)
        if line is None and record_type(header, r) == "DEPENDENCY":
            for c in cols:
                if r[c]:
                    r[c] = "ZZ-missing"
            line = idx + 2  # header is physical line 1
        out.append(r)
    return out, line


def _corrupt_bad_project_date(header, rows):
    cols = [i for i, h in enumerate(header) if "start" in h.lower()]
    out, line = [], None
    for idx, r in enumerate(rows):
        r = list(r)
        if record_type(header, r) == "PROJECT":
            for c in cols:
                if r[c]:
                    r[c] = "2026-13-45"
            line = idx + 2
        out.append(r)
    return out, line


CORRUPTIONS = {
    "missing_project": (_corrupt_missing_project, None),
    "dangling_successor": (_corrupt_dangling_successor, "succ"),
    "bad_project_date": (_corrupt_bad_project_date, "start"),
}


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T33: failed import leaves workspace intact (A09)")
@pytest.mark.parametrize("kind", sorted(CORRUPTIONS))
def test_a09_invalid_csv_leaves_project_intact(tmp_path, kind):
    src, _, _ = _source_project()
    header, rows = read_csv_rows(_exported(tmp_path, src))
    corrupt, field_hint = CORRUPTIONS[kind]
    bad_rows, bad_line = corrupt(header, rows)
    bad = tmp_path / f"bad_{kind}.csv"
    write_csv_rows(bad, header, bad_rows)

    ws, nodes = _target_project()
    before = snapshot(ws, nodes)
    with pytest.raises(pp.ImportFailed) as ei:
        ws.import_csv(str(bad), discard_unsaved=True)
    issues = list(ei.value.issues)
    assert issues
    if bad_line is not None:
        # row/field errors point at the corrupted line and column
        hits = [i for i in issues if i.line == bad_line]
        assert hits, [(i.line, i.field, i.message) for i in issues]
        assert any(field_hint in str(i.field).lower() for i in hits)
    # workspace unchanged: same revision, dirty flag, definition and dates
    assert snapshot(ws, nodes) == before
    assert [r.name for r in ws.project().resources] == ["Carol"]


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T33: non-planner CSV rejected (A09)")
def test_a09_garbage_file_rejected(tmp_path):
    bad = tmp_path / "garbage.csv"
    bad.write_text("hello,world\n1,2\n", encoding="utf-8")
    ws, nodes = _target_project()
    before = snapshot(ws, nodes)
    with pytest.raises(pp.ImportFailed) as ei:
        ws.import_csv(str(bad), discard_unsaved=True)
    assert list(ei.value.issues)
    assert snapshot(ws, nodes) == before


# ---------------------------------------------------------------- A10


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T33: unsaved-changes guard on import (A10 / spec 3.2)")
def test_a10_import_refuses_to_discard_unsaved_work(tmp_path):
    src, _, _ = _source_project()
    path = _exported(tmp_path, src)
    ws, nodes = _target_project()
    assert ws.state().dirty is True
    before = snapshot(ws, nodes)
    with pytest.raises(pp.UnsavedChanges):
        ws.import_csv(str(path))
    assert snapshot(ws, nodes) == before


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T33: valid import replaces everything (A10)")
def test_a10_valid_csv_replaces_project(tmp_path):
    src, src_nodes, (t1, t2) = _source_project()
    path = _exported(tmp_path, src)
    ws, old_nodes = _target_project()
    summary = ws.import_csv(str(path), discard_unsaved=True)
    assert summary is not None
    proj = ws.project()
    # nothing from the old project survives
    assert proj.name == "Source"
    assert sorted(r.name for r in proj.resources) == ["Alice", "Bob"]
    node_ids = {id_of(n.id) for n in proj.nodes}
    assert node_ids == {id_of(n) for n in src_nodes}
    assert id_of(old_nodes[0]) not in node_ids
    # previous results are gone and imported result rows are not used
    assert current(ws) is None
    # calendar came from the file
    assert calendar_settings(ws) == calendar_settings(src)
    # sizing kept as entered, IDs preserved as text
    assert sizing_of(ws, t1) == (D(40), "hours")
    # recalculation gives the same dates as the source
    res = ws.schedule()
    src_res = current(src)
    for n in src_nodes:
        assert (node_row(res, n).start, node_row(res, n).finish) == (
            node_row(src_res, n).start,
            node_row(src_res, n).finish,
        )
    # spot check: t2 starts 4 h before t1's finish. t1 = 5 working days from Mon 5 with
    # Thu 8 a holiday -> Mon 5, Tue 6, Wed 7, Fri 9, Mon 12 -> finish Mon 12 17:00;
    # t2 starts Mon 12 13:00
    assert node_row(res, t2).start == oct26(12, "13:00")
    assert res.total_cost == D(3600)


# ---------------------------------------------------------------- A11


def _leveled_file_project(db_path):
    """Overload project (see test_loading_leveling) in a file DB, leveled and saved."""
    ws = new_ws(db_path)
    new_project(ws, "Demo")
    ws.calendar.initialize(working_days_per_year=230)
    alice = ws.add_resource("Alice", hourly_rate="100")
    t1 = ws.add_task("T1", duration="3d")
    t2 = ws.add_task("T2", duration="24h")
    t3 = ws.add_task("T3", duration="1d")
    ws.set_assignment(t1, alice, percent=60)
    ws.set_assignment(t2, alice, percent=60)
    ws.add_dependency(t2, t3, "FS")
    ws.schedule()
    ws.level_preview()
    ws.apply_leveling()
    return ws, alice, (t1, t2, t3)


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T33: save / reopen / load restores results (A11)")
def test_a11_save_close_load_restores_definition_and_leveled_schedule(tmp_path):
    db = tmp_path / "plans.db"
    ws, alice, (t1, t2, t3) = _leveled_file_project(db)
    res = current(ws)
    saved_rows = {
        id_of(t): (
            node_row(res, t).start,
            node_row(res, t).finish,
            node_row(res, t).leveling_delay_days,
            node_row(res, t).cost,
        )
        for t in (t1, t2, t3)
    }
    settings = calendar_settings(ws)
    definition = ws.project()
    ws.save()
    assert ws.state().dirty is False
    ws.close()

    ws2 = new_ws(db)
    ws2.load(project_id_by_name(ws2, "Demo"))
    st = ws2.state()
    assert st.dirty is False
    assert st.stale_dates is False
    assert st.stale_costs is False
    loaded = current(ws2)
    assert loaded is not None
    assert loaded.kind == "leveled"
    # restored verbatim (no recalculation into a different, dependency-only schedule)
    for t in (t1, t2, t3):
        row = node_row(loaded, t)
        assert (row.start, row.finish, row.leveling_delay_days, row.cost) == saved_rows[id_of(t)]
    # explicit expected values (see _overloaded_project in test_loading_leveling):
    # T2 delayed 3 d to Thu 8 09:00 - Mon 12 17:00; T3 Tue 13; cost 2880 total
    assert node_row(loaded, t2).start == oct26(8, "09:00")
    assert node_row(loaded, t2).finish == oct26(12, "17:00")
    assert node_row(loaded, t2).leveling_delay_days == D(3)
    assert loaded.project_finish == oct26(13, "17:00")
    assert loaded.total_cost == D(2880)
    assert loaded.schedule_fp == res.schedule_fp
    # definition, rates, as-entered units and calendar settings restored
    assert ws2.project() == definition
    assert sizing_of(ws2, t2) == (D(24), "hours")
    assert calendar_settings(ws2) == settings
    ws2.close()


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T33: save-as, list, unsaved guard, failed load (A11)")
def test_a11_save_as_list_and_load_guards(tmp_path):
    db = tmp_path / "plans.db"
    ws, alice, (t1, t2, t3) = _leveled_file_project(db)
    ws.save()
    ws.save_as("Demo v2")
    assert sorted(p.name for p in ws.list_projects()) == ["Demo", "Demo v2"]
    # an edit makes the active project dirty; loading must not silently discard it
    ws.add_task("Unsaved", duration="1d")
    before = snapshot(ws, [t1, t2, t3])
    with pytest.raises(pp.UnsavedChanges):
        ws.load(project_id_by_name(ws, "Demo"))
    assert snapshot(ws, [t1, t2, t3]) == before
    # failed load (unknown project) leaves the working project intact
    with pytest.raises(pp.NotFound):
        ws.load("no-such-project", discard_unsaved=True)
    assert snapshot(ws, [t1, t2, t3]) == before
    # new_project is a replacing operation too
    with pytest.raises(pp.UnsavedChanges):
        ws.new_project("Other", start=date(2026, 10, 5))
    ws.load(project_id_by_name(ws, "Demo"), discard_unsaved=True)
    assert ws.state().dirty is False
    assert node_row(current(ws), t2).start == oct26(8, "09:00")
    ws.close()


# ---------------------------------------------------------------- A12


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T33/T20: export writes every node (A12)")
def test_a12_export_includes_all_nodes_regardless_of_ui_state(tmp_path):
    ws = new_ws()
    new_project(ws)
    g = ws.add_group("Collapsed in the UI")
    g1 = ws.add_group("Nested", parent=g)
    t1 = ws.add_task("Hidden 1", parent=g1, duration="1d")
    t2 = ws.add_task("Hidden 2", parent=g1, duration="2d")
    m = ws.add_milestone("Hidden milestone", parent=g1)
    t3 = ws.add_task("Visible", duration="1d")
    ws.schedule()
    header, rows = read_csv_rows(_exported(tmp_path, ws))
    node_cells = [set(r) for r in rows if record_type(header, r) == "NODE"]
    all_ids = [g, g1, t1, t2, m, t3]
    # export has no notion of expansion state: every node ID appears in a NODE record
    for n in all_ids:
        assert any(id_of(n) in cells for cells in node_cells), n
    assert len(node_cells) == len(all_ids)
    # and the hidden descendants' names are present
    names = {c for cells in node_cells for c in cells}
    assert {"Hidden 1", "Hidden 2", "Hidden milestone"} <= names


@pytest.mark.acceptance
@pytest.mark.xfail(strict=True, reason="T33/T20: export marks stale results explicitly (spec 12.2)")
def test_export_marks_stale_results(tmp_path):
    ws = new_ws()
    new_project(ws)
    ws.add_task("T", duration="1d")
    ws.schedule()

    def result_cells(path):
        header, rows = read_csv_rows(path)
        return [c.lower() for r in rows if record_type(header, r).startswith("RESULT") for c in r]

    fresh = result_cells(_exported(tmp_path, ws, "fresh.csv"))
    assert fresh  # results are exported
    assert not any("stale" in c for c in fresh)
    ws.add_task("U", duration="1d")
    stale = result_cells(_exported(tmp_path, ws, "stale.csv"))
    assert any("stale" in c for c in stale)
