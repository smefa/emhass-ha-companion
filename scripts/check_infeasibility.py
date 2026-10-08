#!/usr/bin/env python3
"""Diagnose "EMHASS reported the optimisation problem as infeasible".

The checks live in custom_components/emhass_companion/infeasibility.py, which
the integration also runs itself to name the cause in its repair issue. That
file is standalone: download it on its own and run it the same way. This
wrapper only loads it by path, so it needs no Home Assistant install either.

Usage
-----
    python3 check_infeasibility.py path/to/diagnostics.json
    cat diagnostics.json | python3 check_infeasibility.py -
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "emhass_companion"
    / "infeasibility.py"
)
_spec = importlib.util.spec_from_file_location("emhass_companion_infeasibility", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
# Registered before executing: dataclasses look their own module up in
# sys.modules while the class body is processed.
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

Severity = _module.Severity
Finding = _module.Finding
Report = _module.Report
load_payload = _module.load_payload
run_checks = _module.run_checks
diagnose = _module.diagnose
headline = _module.headline
main = _module.main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
