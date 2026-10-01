"""Pytest configuration and fixtures."""

import os

from hypothesis import settings

# Register Hypothesis profiles
settings.register_profile("dev", max_examples=50)
settings.register_profile("ci", max_examples=500, deadline=None)

# Load profile from environment variable, default to 'dev'
profile = os.environ.get("HYPOTHESIS_PROFILE", "dev")
settings.load_profile(profile)
