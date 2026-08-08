# Troubleshooting

**A source returns nothing, or the wrong values.** Call the
`emhass_companion.test_profile` action. It resolves the profile and returns
exactly what came back:

```yaml
action: emhass_companion.test_profile
data:
  profile: price/nordpool_custom
response_variable: result
```

**A plan looks wrong.** Enable the *Last request to EMHASS* sensor (disabled
by default) — its `payload` attribute is precisely what EMHASS was asked to
solve, and its `warnings` attribute flags problems such as a forecast that
does not reach the end of the horizon. Download diagnostics from the
integration page for the same thing plus profile state.

**Forecast coverage warnings.** EMHASS forward-fills a forecast that stops
short, so the plan looks healthy while being built on a price that
flat-lined. These warnings are how you find out.

**"Start as early as possible" changes nothing.** Expected, if that load's
*Energy needed* is 0. An uncapped surplus load asks for all the spare solar
there is, which is already the whole sunny stretch, so there is no placement
freedom for the switch to remove and the plan comes out identical either way.
Set *Energy needed* to what you actually want. Confirm it at debug level: a
line reading `start-as-early-as-possible window widened from N to M timesteps`
where `M` is the whole block, every run, is the switch doing nothing. See
[Surplus loads](surplus_loads.md#it-needs-an-energy-cap-to-do-anything).

**"The optimisation problem is infeasible."** Download diagnostics (or grab
the `payload` attribute off the *Last request to EMHASS* sensor) and run
[`scripts/check_infeasibility.py`](https://github.com/smefa/emhass-ha-companion/blob/main/scripts/check_infeasibility.py) against
it:

```sh
python3 scripts/check_infeasibility.py diagnostics.json
```

It's a standalone, dependency-free script, so it also runs fine on a plain
`python3` outside a full checkout. It checks for the patterns that actually
turn out to cause this — a deferrable load whose window is narrower than
its own run time, a battery that can only be charged by PV and never the
grid, forecast arrays that disagree on length, and power deficits/surpluses
no combination of grid, battery and inverter limits can cover — and explains
which EMHASS constraint each one mirrors. It cannot prove a plan *is*
feasible, only find reasons it might not be.
