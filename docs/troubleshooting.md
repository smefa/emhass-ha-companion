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

**"Start as early as possible" changes nothing.** Check the debug line it logs
every run:

```
start-as-early-as-possible asking for 23059 Wh at 6361 W (94% of the block's
24596 Wh, keeping the rest to modulate with) in 22 timesteps, 16 of them run
```

If the timestep count is the whole block, the modulation margin found no
placement freedom to buy. Two ordinary reasons: the block is **flat**, so the
run genuinely fills it whatever the margin (`credit_wh / peak` is the slot
count by construction), or the load's configured power sits **below the block's
peak**, in which case no window covers the credited energy at all. Setting
*Energy needed* to less than the day's surplus gives the clamp something firmer
to work with. See
[Surplus loads](surplus_loads.md#the-modulation-margin).

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
