# Hub controls

The **EMHASS Companion** device is the integration itself, separate from the
devices it creates for each load. Everything the setup wizard didn't ask for
lives here, as a control you can change any time — from the UI or from an
automation.

Find it under **Settings → Devices & Services → EMHASS Companion → 1
device**.

## Controls

- **Mode** (select) — *Automatic* / *Self-consumption* / *Idle*. Anything
  other than *Automatic* takes the optimiser out of the loop and holds that
  mode until you change it back, so all three are steady states you can safely
  leave it in. Only does anything with an inverter profile configured — see
  **[Inverter control](inverter_control.md)**.
- **Control enabled** (switch) — the master gate on writing to your hardware
  at all. **Ships off**, and stays off until you turn it on.
- **Cost function** (select) — what the optimiser is trying to achieve:
    - *Maximize profit* — the default. Weighs what you pay to import against
      what you're paid to export.
    - *Minimize cost* — only counts what you pay, ignoring export revenue.
      Use it when export earns you nothing.
    - *Maximize self-consumption* — keeps as much of your own solar at home
      as it can, even when selling would pay better.
- **Live value weight** (number, 0–1) — how much a model-predictive run
  trusts a live sensor reading over the forecast for the first step it plans.
  0 uses the forecast untouched, 1 replaces that first step entirely with the
  live reading, and 0.5 — the default — splits it evenly. Applies to the
  **Live PV power sensor** from the battery step, and to load whenever the
  load source is *House load sensor*. Day-ahead runs are never blended.
- **Spare solar threshold** (number, W) — what counts as "spare" for the
  reporting sensors below. Display only; each load uses its own headroom
  setting to decide what it actually gets — see
  **[Surplus loads](surplus_loads.md)**.

## Buttons

- **Recalculate now** — runs a model-predictive optimisation from the current
  state, without waiting for the next scheduled one.
- **Rebuild day-ahead plan** — runs a full optimisation across the whole
  planning horizon.
- **Train load forecaster** — only present if your load forecast method is
  *Machine-learning forecaster*. Press it once after setup, and again
  whenever you want the model retrained on more recent history.

The first two also exist as actions — `emhass_companion.run_mpc` and
`emhass_companion.run_dayahead` — for use in scripts and automations.

## Sensors

Each of the planning sensors carries the whole series in its `forecast`
attribute, which is what the dashboard cards draw.

### The plan

- **Planned solar production** — the PV the plan expects.
- **Planned house consumption** — the baseline load it expects.
- **Planned grid power** — positive is import, negative is export.
- **Planned battery power** — positive is discharge, negative is charge.
- **Planned battery level** — the battery percentage over the plan.
- **End SOC target** — the level the plan aims for at the end of the horizon,
  with the reasoning in its attributes. See **[End SOC](../end_soc_plan.md)**.
- **Import price** / **Export price** — your composed prices, after the
  multipliers, additions or templates from the *Buy and sell prices* step.
- **Planned solar curtailment** — PV the plan throws away rather than produce.
  Only if you turned curtailment on.
- **Planned inverter power** — the inverter's own AC power over the plan. Only
  with a hybrid inverter configured.

### Money

The two forecast sensors are always here, because they price the plan and every
setup has one. The four *today* sensors read your meters, so they need the cost
tracking step. See **[Cost and savings](../savings.md)** for how the figures
are built and how to check one that looks wrong.

- **Forecast cost next 24 h** — what the plan is expected to cost over the
  next 24 hours: planned import spend minus planned export income, in your
  Home Assistant currency. Negative on a day the house is paid more than it
  spends. It re-prices the plan on every meter tick, using the same prices
  EMHASS optimised against, and its `hourly_cost` attribute breaks the total
  down per clock hour.
- **Forecast savings next 24 h** — the same window, against the same house
  with no solar and no battery. On both sensors, `covers_full_window` says
  whether the plan really reaches 24 hours ahead; when it doesn't, they report
  what it does cover rather than extrapolating, so a low figure late in the day
  usually means a short horizon rather than a cheap night.
- **Energy cost today** / **Savings today** — the metered day so far, resetting
  at midnight. Home Assistant keeps its own long-term statistics for these, so
  weekly and monthly totals come from a statistics card for free.
- **Solar savings today** / **Battery savings today** — the same saving split
  into what the panels did and what the battery added on top.

With a grid meter but no solar meter that split can't be drawn, so only
**Energy cost today** and **Savings today** appear. Both are still exact —
neither depends on the solar measurement.

### Spare solar

Always present, whether or not any load is set to *On spare solar*. They report
against the **Spare solar threshold** above; each load still decides for itself
what it takes — see **[Surplus loads](surplus_loads.md)**.

- **Spare solar** — PV the plan expects the house not to need right now, in
  watts, before any surplus load takes it. Also a binary sensor, for triggers.
  Not the same as exported power: solar the plan earmarked for the battery is
  still spare.
- **Spare solar energy** — kWh still to come in the current daylight block.
  Usually the more useful trigger of the two.
- **Spare solar from** / **Spare solar until** — when that block runs.

### Checking on it

- **Battery action** — what the executor did this cycle, or would have done,
  and why. Worth watching before you turn **Control enabled** on.
- **Optimisation status** — the result of the last solve.
- **Plan out of date** (binary sensor) — on when the plan is too old to act
  on, usually because runs are failing.
- **Source readings unavailable** (binary sensor) — on when any entity the
  integration reads has stopped reporting. One dead sensor never takes the
  optimisation down, which is exactly why it would otherwise be invisible;
  `blind_critical_entities` lists only the ones severe enough to stand control
  down.
- **Planned cost** — EMHASS's own objective value from the last solve, passed
  through unchanged so it matches the add-on's own figure. Despite the name it
  is not money you can total up: its sign and meaning follow **Cost function**
  — with *Maximize profit* it is a profit, so a house that imports more than it
  exports shows a *negative* number — and it covers whatever horizon was last
  solved rather than a fixed day, so two readings aren't measuring the same
  span. Use **Forecast cost next 24 h** for money, and this for judging a
  solve.
- **Last request to EMHASS** — the exact payload of the last optimisation
  request. Disabled by default — enable it when you're chasing a
  wrong-looking plan. See **[Troubleshooting](../troubleshooting.md)**.

### Your own house load

- **House load (without deferrables)** — only present if you chose *Create a
  house load sensor*: your total house power minus whatever the deferrable
  loads are drawing. This is the sensor the load forecast is built from.

Battery entities only exist if you configured a battery.
