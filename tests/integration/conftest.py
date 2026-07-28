"""Fixtures for tests that need a running Home Assistant instance.

These rely on ``pytest-homeassistant-custom-component``, which auto-loads via
its pytest entry point. That plugin imports ``fcntl`` and so does not load on
Windows; CI runs these on Linux. The platform-independent tests live in the
parent directory.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make the custom integration loadable in every test in this package."""
    return
