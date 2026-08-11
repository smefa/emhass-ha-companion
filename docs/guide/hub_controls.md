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
  setting to decide what it actually gets. Only present once a load is set to
  *On spare solar* — see **[Surplus loads](surplus_loads.md)**.

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

- **Planned solar production** — the PV the plan expects.
- **Planned house consumption** — the baseline load it expects.
- **Planned grid power** — positive is import, negative is export.
- **Planned battery power** — positive is discharge, negative is charge.
- **Planned battery level** — the battery percentage over the plan.
- **End SOC target** — the level the plan aims for at the end of the horizon,
  with the reasoning in its attributes. See **[End SOC](../end_soc_plan.md)**.
- **Import price** / **Export price** — your composed prices, after the
  multipliers, additions or templates from the *Buy and sell prices* step.
- **Planned cost** — what the current plan is expected to cost.
- **Optimisation status** — the result of the last solve.
- **Battery action** — what the executor did this cycle, or would have done,
  and why. Worth watching before you turn **Control enabled** on.
- **House load (without deferrables)** — only present if you chose *Create a
  house load sensor*: your total house power minus whatever the deferrable
  loads are drawing. This is the sensor the load forecast is built from.
- **Plan out of date** (binary sensor) — on when the plan is too old to act
  on, usually because runs are failing.
- **Last request to EMHASS** — the exact payload of the last optimisation
  request. Diagnostic, and disabled by default — enable it when you're
  chasing a wrong-looking plan. See
  **[Troubleshooting](../troubleshooting.md)**.

Battery entities only exist if you configured a battery, and the spare-solar
ones only once a load is set to *On spare solar*.
