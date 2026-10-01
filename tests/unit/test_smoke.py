"""Tooling smoke test."""

import pytest

import project_planner


@pytest.mark.unit
def test_import_works():
    """Test that project_planner can be imported."""
    assert project_planner is not None


@pytest.mark.unit
def test_version_present():
    """Test that __version__ is a non-empty string."""
    assert isinstance(project_planner.__version__, str)
    assert len(project_planner.__version__) > 0
