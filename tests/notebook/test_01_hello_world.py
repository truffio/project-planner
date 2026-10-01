"""Executes examples/01_hello_world.ipynb end to end and checks its output."""

from pathlib import Path

import pytest

nbclient = pytest.importorskip("nbclient")
import nbformat  # noqa: E402

NOTEBOOK = Path(__file__).resolve().parents[2] / "examples" / "01_hello_world.ipynb"


def _stream_text(nb: nbformat.NotebookNode) -> str:
    """Extract all stream output text from a notebook."""
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
def test_hello_world_notebook() -> None:
    """Test that the hello world notebook executes correctly and produces expected output."""
    nb = nbformat.read(NOTEBOOK, as_version=4)
    nbclient.NotebookClient(nb, timeout=120, kernel_name="python3").execute()
    text = _stream_text(nb)

    # Check each expected printed line appears verbatim
    expected_lines = [
        "Say hello: 2026-10-05 09:00 -> 2026-10-05 17:00",
        "Celebrate: 2026-10-06 09:00 -> 2026-10-06 17:00",
        "Project finish: 2026-10-06 17:00",
        "Out of date after edit: True",
        "New project finish: 2026-10-07 17:00",
    ]

    for line in expected_lines:
        assert (
            line in text
        ), f"Expected line not found in output:\n{line!r}\n\nActual output:\n{text}"
