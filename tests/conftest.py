"""Fixtures for tests that need no running Home Assistant.

The Home Assistant test plugin only loads on Unix (it imports ``fcntl``), so
everything that can be tested without a ``hass`` instance lives here and in the
sibling modules, and stays runnable on any platform. Tests that do need a
running Home Assistant live under ``tests/integration/``.
"""

from __future__ import annotations

from homeassistant.util import dt as dt_util
import pytest


@pytest.fixture
def stockholm_timezone():
    """Pin the local timezone to one with DST, restoring it afterwards.

    Wall-clock deferrable windows are interpreted in local time, so the tests
    that matter most are the ones run somewhere that actually changes offset.
    """
    original = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Stockholm"))
    yield
    dt_util.set_default_time_zone(original)
