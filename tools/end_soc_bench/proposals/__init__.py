"""Candidate end-SOC rules that are being tried out, but do not ship.

A proposal is a module in this package with three names::

    KEY = "peak_guard"
    LABEL = "Test 7 -- peak guard"
    def compute(tail) -> EndSocDecision: ...

``bench --proposals`` appends every one of them to ``terminal.CANDIDATES``, so
they are scored on exactly the same moments, against the same oracle, as the
rules that already ship. Nothing here is imported by the integration; a
proposal only becomes real by being written into ``terminal.py``, and it should
only get there once the backtest says it earns its place.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import pkgutil

from custom_components.emhass_companion.terminal import _Candidate


def load() -> tuple[_Candidate, ...]:
    """Every proposal in this package, in name order."""
    found: list[_Candidate] = []
    for module_info in sorted(pkgutil.iter_modules([str(Path(__file__).parent)])):
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        if not all(hasattr(module, name) for name in ("KEY", "LABEL", "compute")):
            continue
        found.append(_Candidate(module.KEY, module.LABEL, module.compute))
    return tuple(found)
