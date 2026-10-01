"""Executes examples/02_team_allocation.ipynb end to end and checks its printed lines."""

from pathlib import Path

import pytest

nbclient = pytest.importorskip("nbclient")
import nbformat  # noqa: E402

NOTEBOOK = Path(__file__).resolve().parents[2] / "examples" / "02_team_allocation.ipynb"


def _stream_text(nb: nbformat.NotebookNode) -> str:
    parts = []
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        for output in cell.outputs:
            if output.output_type == "error":
                pytest.fail(f"cell failed: {output.ename}: {output.evalue}")
            if output.output_type == "stream":
                parts.append(output.text)
    return "".join(parts)


@pytest.mark.notebook
def test_team_allocation_notebook() -> None:
    nb = nbformat.read(NOTEBOOK, as_version=4)
    nbclient.NotebookClient(nb, timeout=120, kernel_name="python3").execute()
    lines = _stream_text(nb).splitlines()

    expected = [
        "Ana: peak 250% | 20 person-days | utilization 166.7% | cost 14400.00",
        "Ben: peak 100% | 7.5 person-days | utilization 62.5% | cost 4800.00",
        "Chloe: peak 100% | 10 person-days | utilization 83.3% | cost 5600.00",
        "Dev: peak 25% | 0.5 person-days | utilization 4.2% | cost 240.00",
        "Total cost: 25040.00",
        "Project finish: 2026-10-20 17:00",
    ]
    for line in expected:
        assert line in lines, f"missing {line!r} in:\n" + "\n".join(lines)

    ana = next(ln for ln in lines if ln.startswith("Ana ") and "█" in ln)
    assert ana.count("█") > 4
    assert "|" in ana
