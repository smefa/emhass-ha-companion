# Thermal loads

A thermal load is a deferrable load whose *temperature* the optimiser
controls rather than its run time: a heat pump, direct electric heating, an
air conditioner. Instead of "run three hours a day", the contract is "keep
this room between 18 and 24 °C, and at least 21 °C while we are awake" — and
EMHASS picks the cheapest hours that satisfy it, pre-heating into cheap
electricity where the band allows.

This maps onto [EMHASS's thermal model](https://emhass.readthedocs.io/en/latest/thermal_model.html)
(the recommended `min_temperatures` / `max_temperatures` form), with the
Companion translating human-scale settings into the per-timestep lists EMHASS
wants.

## Adding one

Settings → Devices & services → EMHASS Companion → **Add thermal load** — a
separate button from *Add deferrable load*, with its own questions:

![The Add a thermal load dialog, with all fields visible: temperature sensor, heats or cools, power, comfort/setback/maximum temperature, comfort window, heating rate, heat loss rate, response delay, running sensor and control entity](assets/thermal-add-load.png)

| Question | Meaning |
| --- | --- |
| Temperature sensor | The room (or tank) the comfort band applies to. Read live on every optimisation as the plan's start temperature. |
| Heats or cools | Whether running the load raises the temperature (heat pump) or lowers it (air conditioning). |
| Power when running | Electrical draw at full power — this is the power EMHASS schedules. |
| Comfort / setback / maximum temperature | The band. Comfort holds inside the comfort window, setback outside it, and the maximum is a hard ceiling at all times. |
| Comfort from / until | The daily comfort window, which may cross midnight. |
| Heating rate | Degrees per hour at full power. |
| Heat loss rate | Degrees lost per hour, per degree of difference to outdoors (EMHASS's `cooling_constant`). |
| Response delay | Lag between switching on and the temperature responding (EMHASS's `thermal_inertia`) — an hour or more for underfloor heating in concrete, 0 for a radiator or fan. |
| Running sensor / control entity | Same as an ordinary deferrable load. |

Like an ordinary load, the answers only *seed* the load: from the moment it
exists, every optimiser-facing value (the comfort band, the window, the
physics) is an entity on the load's own device page, adjustable from a
dashboard or an automation without reloading anything.

## What a thermal load does not have

There is no operating-hours target, so everything anchored to one is absent
rather than disabled: the *Hours needed* number, the *one unbroken block*
switch, recurrence/on-demand and the *Requested* switch. Its demand is the
comfort band, which stands every day by nature. Enabled, force on/off, the
run-time window, startup penalty and max starts all still apply.

## Ownership and payload mechanics

- The subentry owns identity: name, temperature sensor, heats/cools, running
  sensor, control entity. *Edit thermal load* re-offers exactly these.
- Entities own everything the optimiser is told, restored across restarts the
  same way as every other load setting.
- On every request the registry builds a fresh `ThermalConfig`
  (`deferrable.DeferrableRuntime.thermal_config`), reading the room sensor at
  that moment (unit-converted to Celsius; an unavailable sensor omits
  `start_temperature` and lets EMHASS use its own default).
- `payload._thermal_settings` emits `def_load_config` with one entry per
  configured load — `{}` for ordinary ones — because EMHASS overwrites
  `number_of_deferrable_loads` with that list's length. A disabled thermal
  load is parked like any other and gets an empty entry: temperature targets
  are exactly the demand a parked load must not carry.
- Thermal loads are sent with 0 operating hours. EMHASS skips the energy
  equality for them (their temperature constraints replace it) and pins
  `param_load_active` to 1, so 0 hours does not deactivate them.

## Outdoor temperature

![The Outdoor temperature step, choosing between EMHASS built-in, an entity attribute, or a Home Assistant weather entity](assets/thermal-outdoor-temperature.png)

The model needs an outdoor temperature forecast — it is what the room drifts
towards while the load is off. Configure → **Outdoor temperature** offers the
same profile engine as every other input (`profiles/builtin/temperature/`):

- **Home Assistant weather entity** — hourly forecast via
  `weather.get_forecasts`.
- **Entity attribute** — a list-valued attribute on any entity.
- **EMHASS built-in (Open-Meteo)** — no series; sets
  `weather_forecast_method: open-meteo` so EMHASS fetches it itself. Only
  correct when the solar forecast is also EMHASS built-in.

Left unset, nothing is sent and EMHASS falls back to its own weather data.
The series and settings are only fetched/sent once a thermal load actually
exists (`DeferrableRegistry.has_thermal`).

## Output

Alongside the usual per-load sensors, a thermal load gets **Planned
temperature**: the room temperature EMHASS expects right now under its own
plan (`predicted_temp_heater{k}` in the plan output), with the full horizon
in a `forecast` attribute. EMHASS's docs suggest feeding this to a climate
entity as its setpoint — the plan already accounts for price, so following
the predicted trajectory is what realises the savings.

## Calibration

Start conservative (the defaults) and tune from observed behaviour:

- Plan heats **too early** → heat loss rate too high, or heating rate too low.
- Comfort **missed** at window start → heating rate too optimistic, or the
  response delay is real and unset.
- The optimiser never pre-heats → the gap between comfort and maximum
  temperature is the storage it plays with; widen it.
