# Setup

The config flow asks a few questions at a time, and **only ever offers data
sources you actually have installed**.

| Step | What it asks |
|---|---|
| Connect | Where EMHASS is |
| Electricity prices | Which price source, and its settings |
| Buy and sell prices | What your supplier and grid operator add on top of spot |
| Solar forecast | Which forecast source — or *No solar* |
| House consumption | Which sensor, and how to forecast from it |
| Battery | Capacity and limits — or leave it off |
| Grid and schedule | Connection limits, time resolution, how often to recalculate |

Most of these can be revisited any time from **Settings → Devices & Services
→ EMHASS Companion → Configure**:

![The Configure menu, listing House consumption, Battery, Grid and schedule, Buy and sell prices, Inverter control and Outdoor temperature](assets/setup-configure-menu.png)

**Inverter control** and **Outdoor temperature** only appear once they're
relevant — a battery for the former, a thermal load for the latter — and are
covered in [Inverter control](inverter_control.md) and
[Thermal loads](thermal_loads.md) rather than here.

Electricity price and solar forecast sources are asked only during initial
setup — changing which *source* you use means removing and re-adding the
integration, since it decides which other questions even apply.

## Connect

The first question, during initial setup, is simply where EMHASS is. The
add-on's address is filled in automatically if it's running; otherwise
enter it yourself (for example `http://localhost:5000`).

If EMHASS itself moves — a new host, a different port — change just that
via the integration's **Reconfigure** option, without touching anything
else you've configured:

![The Change the EMHASS address dialog, with a URL field and a note that everything else stays as configured](assets/setup-reconfigure-address.png)

## House consumption

How EMHASS should predict your household's baseline load:

![The House consumption step, with three source options](assets/setup-house-consumption.png)

- **Typical household profile (no sensor needed)** — a generic shape scaled
  to an average power you enter. See [Data sources](data_sources.md).
- **Load forecast from an entity attribute** — the escape hatch for any
  integration exposing a forecast as a list attribute.
- **House load sensor** — a real power sensor, most accurate once you have
  one that excludes your own deferrable loads.

## Buy and sell prices

![The Buy and sell prices step, with linear multiplier/adder fields and a template option for both import and export](assets/setup-buy-sell-prices.png)

Price sources return **raw spot prices**. Composing your real prices happens
separately, because tariff structures differ enormously between markets and
change from year to year. Three modes, chosen independently for import and
export:

- **Linear** — `spot × multiplier + adder`. Use the multiplier for VAT and the
  adder for grid fees, energy tax and supplier markup.
- **Already includes costs** — some price integrations can bake fees in
  themselves. If yours does, use this and do not add them twice.
- **Template** — a Jinja template with `spot` and `time` (a local-time
  `datetime`) available, for tiered or time-of-day structures.

> Tax and subsidy schemes change. Nothing here is kept current for you — check
> your own numbers each year.

## Battery

![The Battery step, with capacity, power limits, hybrid inverter fields, efficiencies and charge levels](assets/setup-battery.png)

Leave **I have a battery** off and everything else on this step is ignored —
the optimiser plans around PV, load and price alone.

| Setting | Meaning |
|---|---|
| Battery SOC sensor | Read at the start of every run so the plan starts from reality |
| Usable capacity | In Wh |
| Maximum charge / discharge power | The battery's own limits |
| Minimum / maximum charge level | The optimiser will not plan the battery outside this range |
| Target charge level | Where the optimiser aims to leave the battery at the end of the horizon |
| Shared inverter (hybrid) | On if PV and the battery share one inverter's AC-side power limit — true for most home battery systems. Off if they are two independent inverters, each with its own limit |
| Maximum AC output / input | Only asked if hybrid — the shared inverter's own throughput ceiling, separate from the battery's own charge/discharge limits |
| DC to AC / AC to DC efficiency | Conversion losses, only asked if hybrid |
| Self-consumption handoff threshold | See below |
| Live PV power sensor | Optional. See "Blending live PV/load into MPC" below |

**Self-consumption handoff threshold.** When the plan's grid power is within
this many watts of zero, the battery is handed to the inverter's own
self-consumption mode instead of the plan's forced charge/discharge — even a
large planned charge or discharge is overridden, because self-consumption
tracks real-time PV and load better than a forecast can at small imbalances.
Once handed over, grid power must exceed **twice** this value before the plan
can force the battery again, so it doesn't flip modes every recalculation.
This is not the same as the charge-level limits above, which only bound what
the optimiser may plan *between*.

Actually commanding the battery is a separate step — see
[Inverter control](inverter_control.md).

### Blending live PV/load into MPC

An MPC run's very first forecast step covers the next few minutes, which a
live sensor reading knows better than any forecast. If a **Live PV power
sensor** is set here, its current reading is blended into the first step of
every MPC PV forecast. Load is blended the same way automatically whenever
the load profile is **House load sensor** (Configure > House consumption) —
that profile already asks for a live load-power sensor, so nothing extra is
needed for it.

How much either blend trusts the live reading over the forecast is the
**Live value weight** number entity on the EMHASS Companion device (a slider
from 0 to 1): 0 uses the forecast untouched, 1 replaces the first step
entirely with the live reading, and 0.5 — the default — splits it evenly.
Day-ahead runs are never blended; a day-ahead forecast starts at midnight, not
now.

## Time resolution

![The Grid connection and schedule step, with import/export limits, curtail-on-negative-price, time resolution, recalculation interval, planning horizon and day-ahead fallback time](assets/setup-grid-schedule.png)

The **Grid and schedule** step defaults the optimisation time step to whatever
your chosen price source actually publishes at — Nord Pool has been 15-minute
since October 2025, most other sources are still hourly — detected
automatically, not something you have to work out yourself. Change it if
you'd rather trade detail for a faster solve; it isn't limited to a fixed
list, type any number of minutes.

This is only the *resolution EMHASS plans at*. How often the plan is
recalculated (below) is a separate setting.

**Curtail on negative price**, on the same step, is off by default. The plan's
own PV-curtailment column already tells the executor when to curtail if
EMHASS's own curtailment cost function is enabled — that is the primary rule.
This option adds a second, independent one: curtail whenever the sell price
is negative *and* the battery has no headroom (already at its maximum charge
level) to absorb the surplus instead. Leave it off unless you specifically
want that behaviour — it is a second optimiser competing with EMHASS's own,
which already prices this in when its curtailment is enabled.

## Scheduling

Two cadences:

- **Model-predictive** — re-optimises from the current state every 15 minutes
  by default.
- **Day-ahead** — a full plan across the whole horizon.

The day-ahead run is **event-driven, not a fixed clock time**. It fires when
the price series actually gains a new day. Markets publish at different hours
and publication slips; a fixed time means quietly optimising against
yesterday's prices whenever it does. A configurable fallback time exists as a
backstop.
