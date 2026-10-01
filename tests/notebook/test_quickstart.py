"""Executes examples/quickstart.ipynb end to end and checks its key results."""

import re
from decimal import Decimal
from pathlib import Path

import pytest

nbclient = pytest.importorskip("nbclient")
import nbformat  # noqa: E402

NOTEBOOK = Path(__file__).resolve().parents[2] / "examples" / "quickstart.ipynb"


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


def _amount(text: str, label: str) -> Decimal:
    match = re.search(rf"{re.escape(label)}:?\s*([-\d.]+)", text)
    assert match, f"{label!r} not found in notebook output:\n{text}"
    return Decimal(match.group(1))


@pytest.mark.notebook
def test_quickstart_notebook() -> None:
    nb = nbformat.read(NOTEBOOK, as_version=4)
    nbclient.NotebookClient(nb, timeout=120, kernel_name="python3").execute()
    text = _stream_text(nb)

    # A16: Design costs 32 h x $100 + 8 h x $50; Build has no assignments
    assert _amount(text, "Total cost") == Decimal(3600)
    # Review (Alice 60% for 3 d = 24 h x 0.6 x $100 = $1,440) added
    before = _amount(text, "Cost before leveling")
    assert before == Decimal(5040)
    # Design (80%) and Review (60%) overlap on Alice
    assert _amount(text, "Peak load") == Decimal(140)
    # leveling delays whole tasks only: cost unchanged
    assert _amount(text, "Cost after leveling") == before
    # CSV round trip reproduces the same estimate
    assert _amount(text, "Imported project cost") == before
