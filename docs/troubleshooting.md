# Troubleshooting

**Before anything else: download diagnostics and attach it.** Settings ->
Devices & services -> EMHASS Companion -> the device page -> the three-dot
menu -> Download diagnostics. It answers most of what a maintainer would
otherwise have to ask for by hand -- whether the add-on is reachable, whether
every entity id the config points at actually exists and holds a sane value,
what EMHASS's own configuration currently is, and the integration's own
recent log lines -- in one file. See [What the bundle
contains](#what-the-bundle-contains) below for exactly what is in it and what
is redacted before you attach it anywhere public.

Running [`inspect_bundle.py`](https://github.com/smefa/emhass-ha-companion/blob/main/scripts/inspect_bundle.py)
against it first is worth doing even before opening an issue -- it is a
standalone, dependency-free script that reads the same download and flags the
patterns that most often turn out to be the actual cause:

```sh
python3 scripts/inspect_bundle.py diagnostics.json
```

It prints a short summary (backend reachability, last run, plan age, how many
referenced entities have a problem) followed by a graded list of findings,
and exits non-zero if it found anything at the CRITICAL level. It cannot run
EMHASS's solver, so "the optimisation problem is infeasible" specifically has
its own script -- see below.

## What the bundle contains

Diagnostics is a JSON file built fresh every time it is downloaded. Besides
the entry's own options and the last optimisation's outputs (plan, payload,
warnings), it now also carries:

- **environment** -- the integration and Home Assistant versions, the Python
  version, the configured time zone and unit system, and whether HACS is
  installed. No coordinates.
- **backend** -- the EMHASS URL (see redaction below), whether it looks like
  it came from add-on discovery or was typed in by hand, and a live probe
  taken at download time: the version EMHASS reports and its current
  `/get-config` response. If the add-on is down, this section says so instead
  of the download hanging or failing outright.
- **subentries** -- every deferrable load, thermal load and load group, in
  full, including their entity ids.
- **entities** -- every entity id referenced anywhere in the config (not just
  the well-known fields -- this also walks profile options), and for each
  one: whether it exists, its current state, unit, device class and when it
  last changed, and whether the entity registry has it disabled.
- **custom_profiles** -- the contents of every custom profile YAML file
  under `emhass_companion/profiles/` in the Home Assistant config directory,
  so a broken one can be read without SSH access to the machine.
- **logs** -- the last ~200 lines this integration itself logged, at INFO
  level or above unless debug logging is turned on for
  `custom_components.emhass_companion`, in which case its debug reasoning
  (deferrable window derivation, end-SOC anchoring, forecast fallbacks, ...)
  is included too.
- **triage** -- a short, pre-computed list of the cheap findings (backend
  unreachable, a referenced entity missing or unavailable, no run has ever
  succeeded, the plan is stale, a profile failed to load), so a maintainer
  sees the verdict before reading the rest.

**What is redacted.** Latitude, longitude and altitude (EMHASS's own config
carries these) are replaced with `**REDACTED**`, as is any field whose name
looks like a credential (`password`, `token`, `api_key`, `secret`,
`Authorization`). The EMHASS URL keeps its scheme, host and port -- "wrong
port" is a real, common misconfiguration and needs to stay visible -- but
loses any userinfo or query string a reverse proxy might have added.

**What is deliberately not redacted**, because redacting it would defeat the
point of the bundle: entity ids, tariff prices, battery capacity, deferrable
load names, and the EMHASS request payload itself. None of that is a secret,
and it is exactly what a maintainer needs to see.

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
start-as-early-as-possible asking for 8750 Wh at 5683 W (87% of the block's
10013 Wh, keeping 13% to modulate with) in 22 timesteps, 7 of them run
```

The tail says which case you are in. `no margin -- it would not have narrowed
the window` means derating was tried and dropped: the tail slots the run would
have stopped needing were too weak to move the covering slot. That happens on a
**flat** block, where the run fills it whatever the margin (`credit_wh / peak`
is the slot count by construction), and on any block whose last slots are worth
less than the margin itself. If instead the margin was charged but the timestep
count is still the whole block, the load's configured power sits **below the
block's peak**, so no window covers the credited energy at all.

Setting *Energy needed* to less than the day's surplus gives the clamp something
firmer to work with — and it is delivered in full, never derated. See
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
