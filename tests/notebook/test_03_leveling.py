"""Executes examples/03_leveling.ipynb end to end and checks its printed lines."""

import re
from pathlib import Path

import pytest

nbclient = pytest.importorskip("nbclient")
import nbformat  # noqa: E402

NOTEBOOK = Path(__file__).resolve().parents[2] / "examples" / "03_leveling.ipynb"


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
def test_leveling_notebook() -> None:
    nb = nbformat.read(NOTEBOOK, as_version=4)
    nbclient.NotebookClient(nb, timeout=180, kernel_name="python3").execute()
    text = _stream_text(nb)

    expected = [
        "Before: finish 2026-10-08 17:00 | Ana peak 170% | cost 3800.00",
        "Delays: B 3 days (resource), C 3 days (dependency), D 5 days (resource)",
        "After: finish 2026-10-13 17:00 | Ana peak 60% | cost 3800.00",
        "Team before: finish 2026-10-20 17:00 | cost 25040.00",
        "Team leveled: finish 2026-10-30 17:00 | max peak 100% or less | cost 25040.00",
        "Move Test plan to Dev + level: finish 2026-10-30 17:00 | cost 24440.00",
        "Move Data migration to Dev + level: finish 2026-10-27 17:00 | cost 23840.00",
    ]
    lines = text.splitlines()
    for line in expected:
        assert line in lines, f"missing line {line!r} in output:\n{text}"

    # every peak printed after leveling is at most 100 %
    after_lines = [ln for ln in lines if ln.startswith("Peaks after:")]
    assert len(after_lines) == 3
    for ln in after_lines:
        values = [int(v) for v in re.findall(r"(\d+)%", ln)]
        assert len(values) == 4
        assert all(v <= 100 for v in values), ln
