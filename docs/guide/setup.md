# Setup

When you add the integration, it asks a handful of questions, one screen at
a time. It only ever offers choices that match integrations you actually
have installed.

## 1. Connect

- **EMHASS address** — where EMHASS is running. Filled in for you if you use
  the add-on.

## 2. Electricity prices

Pick the source that matches what you have installed:

- **Nord Pool (Home Assistant core)**
- **Nord Pool (custom-components/nordpool)**
- **ENTSO-E**
- **Tibber**
- **Fixed tariff** — a flat rate, or a simple peak/off-peak split, if you
  have no dynamic pricing integration.
- **Prices from an entity attribute** — for anything else that exposes a
  price forecast.

You'll then be asked one or two follow-up questions specific to that source
(usually just which sensor to use).

> Don't see your integration? You're not stuck with this list — see
> **[Template additions](../template_additions.md)** to add your own price
> source.

## 3. Buy and sell prices

![The Buy and sell prices step](../assets/setup-buy-sell-prices.png)

Your price source gives raw spot prices. This step turns that into what you
actually pay or get paid, for import and export separately:

- **Import price method** — how your buy price is built: *Linear*
  (multiply, then add), *Already includes costs* (use the price as-is), or
  *Template* (write your own formula).
- **Import multiplier** — multiplies the spot price. Use for VAT.
- **Import addition per kWh** — a flat amount added on top. Use for grid
  fees, energy tax, supplier markup.
- **Import template** — only used if you picked *Template* above.
- **Export price method / multiplier / addition per kWh / template** — the
  same four questions, for what you're paid when selling back to the grid.

## 4. Solar forecast

- **Solcast PV Forecast**
- **EMHASS built-in forecast (Open-Meteo)** — no API key, no forecast
  integration needed.
- **Forecast.Solar (fetched by EMHASS)** — no API key on the free tier.
- **Solar forecast from an entity attribute**
- **No solar** — if you have no PV array.

> Don't see your integration? You're not stuck with this list — see
> **[Template additions](../template_additions.md)** to add your own solar
> source.

Changeable later under **Configure → Solar forecast**. Worth revisiting after
an update: a source's default entity list can gain entries, and those are
offered there rather than applied for you. Solcast users should add
`sensor.solcast_pv_forecast_forecast_day_3`, which lets
[End SOC](../end_soc_plan.md)'s *Optimized* mode see the next solar day past
the horizon instead of inferring it from the previous day.

## 5. House consumption

![The House consumption step](../assets/setup-house-consumption.png)

How EMHASS should predict your household's baseline load:

- **Typical household profile (no sensor needed)** — a generic shape,
  scaled to an average power you type in. Least accurate, but works with no
  sensor at all.
- **Load forecast from an entity attribute**
- **House load sensor** — most accurate, if you have one that excludes your
  own deferrable loads.

## 6. Battery

![The Battery step](../assets/setup-battery.png)

Leave **I have a battery** off if you don't have one — everything else on
this screen is then ignored.

- **Usable capacity** — in Wh.
- **Maximum charge power** / **Maximum discharge power** — the battery's own
  limits, in W.
- **Shared inverter (hybrid)** — on if PV and the battery share one
  inverter's AC-side limit (most home systems). Off if they're two separate
  inverters.
- **Maximum AC output (discharge/export)** / **Maximum AC input
  (charge/import)** — only asked if hybrid; the shared inverter's own
  ceiling.
- **DC to AC efficiency** / **AC to DC efficiency** — conversion losses.
- **Minimum charge level** / **Maximum charge level** — the range the
  optimiser is allowed to plan within.
- **Target charge level** — where it aims to leave the battery at the end of
  the plan.
- **Self-consumption handoff threshold** — how close to zero the grid power
  needs to be before the battery is handed back to plain self-consumption
  instead of a forced charge/discharge.
- **Battery SOC sensor** — read at the start of every plan.

## 7. Grid and schedule

![The Grid connection and schedule step](../assets/setup-grid-schedule.png)

- **Maximum import power** / **Maximum export power** — your connection's
  limits, in W.
- **curtail_on_negative_price** — off by default. Turn on to also curtail
  solar whenever the sell price goes negative and the battery is full.
- **Time resolution (minutes)** — how finely the plan is divided. Filled in
  automatically to match your price source.
- **Recalculate every** — how often the plan is re-optimised, in minutes.
- **Planning horizon** — how many hours ahead it plans.
- **Day-ahead fallback time** — backup time for the once-a-day full
  re-plan, in case new prices haven't shown up yet.

## Changing your mind later

Most of these can be revisited any time from **Settings → Devices &
Services → EMHASS Companion → Configure**:

![The Configure menu](../assets/setup-configure-menu.png)

Electricity price and solar forecast sources are the exception — changing
which *source* you use means removing and re-adding the integration.

To move EMHASS itself to a different address, use **Reconfigure** instead —
everything else you've set up is left untouched:

![The Change the EMHASS address dialog](../assets/setup-reconfigure-address.png)

## Next

Add your first load — see **[Deferrable loads](deferrable_loads.md)** or
**[Thermal loads](thermal_loads.md)**.
