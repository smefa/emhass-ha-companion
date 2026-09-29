"""Parameter sweeps around the shipped shadow-plan rules, for ``bench --variants``.

Nothing here reaches the integration. Each entry is a candidate built from the
shadow solve in ``terminal.py`` with one knob moved, so a change to
``CENTRE_TOLERANCE`` or ``CENTRE_SHIFT`` can be tried against the same moments
as everything else before it is written into the module.
"""

from __future__ import annotations

from collections.abc import Callable

from custom_components.emhass_companion import terminal
from custom_components.emhass_companion.terminal import EndSocDecision, _Candidate, _Tail

# Currency bands scored around the shipped 0.75, and the SOC shifts below the middle.
TOLERANCES: tuple[float, ...] = (0.05, 0.25, 0.5, 0.75, 1.0, 2.0, 3.0)
SHIFTS: tuple[float, ...] = (0.0, 0.025, 0.05, 0.075, 0.10)
# Test 8 before the shift was added, and Test 7's top-of-the-flat tie-break, for reference.
DRAW_MARGINS: tuple[float, ...] = (0.05, 0.10, 0.25)


def _pick(tolerance: float, position: str, shift: float) -> Callable[[_Tail], EndSocDecision]:
    """The pin at ``position`` (low, mid, high) of the near-optimal band, less ``shift``."""

    def compute(tail: _Tail) -> EndSocDecision:
        solution = terminal._shadow_solution(tail)
        if solution is None:
            return terminal._no_room(tail)
        band = [
            index
            for index, value in enumerate(solution.total)
            if value <= solution.best + tolerance
        ]
        index = {"low": band[0], "mid": band[len(band) // 2], "high": band[-1]}[position]
        soc = tail.clamp(max(solution.soc_grid[index] - shift, solution.floor))
        return EndSocDecision(soc=round(soc, 4), reason="", details={})

    return compute


def _draw(margin: float) -> Callable[[_Tail], EndSocDecision]:
    """Test 8's first form: lift the plan's pin towards the night cover when nearly free."""

    def compute(tail: _Tail) -> EndSocDecision:
        solution = terminal._shadow_solution(tail)
        if solution is None:
            return terminal._no_room(tail)
        capacity = tail.battery.capacity_wh
        reset_from, _kind = terminal._cover_horizon(tail)
        cover = terminal._required_soc(tail, reset_from=reset_from)
        sale = terminal._sale_credit(tail, cover.binding_at, cover.energy_wh)
        cover_soc = tail.clamp(max(tail.reserve, cover.soc - sale.energy_wh / capacity))
        allowance = margin * solution.residual
        soc = solution.soc
        if cover_soc > soc + 1e-6:
            between = [level for level in solution.soc_grid if soc < level < cover_soc]
            for level in [cover_soc, *reversed(between)]:
                cost = solution.cost_at(level) - solution.best
                if cost <= allowance * (level - soc) * capacity / 1000:
                    soc = level
                    break
        return EndSocDecision(soc=round(soc, 4), reason="", details={})

    return compute


def load() -> tuple[_Candidate, ...]:
    """Every sweep entry, keyed so the report says which knob setting it is."""
    found = [
        _Candidate(f"centred_tol{tolerance:g}_shift{shift:g}", "", _pick(tolerance, "mid", shift))
        for tolerance in TOLERANCES
        for shift in SHIFTS
    ]
    found += [
        _Candidate(f"top_tol{tolerance:g}", "", _pick(tolerance, "high", 0.0))
        for tolerance in TOLERANCES
    ]
    found += [_Candidate(f"draw{margin:g}", "", _draw(margin)) for margin in DRAW_MARGINS]
    return tuple(found)
