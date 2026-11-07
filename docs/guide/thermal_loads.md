# Thermal loads

A thermal load is like a deferrable load, but its **temperature** is
controlled instead of its run time — a heat pump, direct electric heating,
an air conditioner. Add one from **Settings → Devices & Services → EMHASS
Companion → Add thermal load** — a separate button from *Add deferrable
load*.

## Add a thermal load

![The Add a thermal load dialog](../assets/thermal-add-load.png)

- **Name**
- **Temperature sensor** — the room (or tank) the settings below apply to.
  Strongly recommended: without it, every plan starts from a guessed
  temperature instead of the real one.
- **Heats or cools** — whether running the load raises the temperature (a
  heat pump, a heater) or lowers it (an air conditioner).
- **Power when running** — roughly what it draws while on, in W.
- **Comfort temperature** — the temperature to hold while the comfort
  window (below) is active. May go above this when electricity is cheap,
  never below.
- **Setback temperature** — the lowest acceptable temperature outside the
  comfort window.
- **Maximum temperature** — a hard ceiling, at all times.
- **Comfort from** / **Comfort until** — when the comfort temperature
  applies each day. May cross midnight.
- **Advanced settings** — the collapsed section holds the three model
  settings below (*Heating rate*, *Heat loss constant*, *Response delay*). The
  defaults are a reasonable starting point.
- **Heating rate** — degrees per hour at full power.
- **Heat loss constant** — degrees lost per hour, per degree of difference
  to the outdoor temperature.
- **Response delay** — how long after switching on before the temperature
  responds. An hour or more for underfloor heating in concrete, 0 for a
  radiator or fan.
- **Heat pump COP curve** — optional, also under **Advanced settings**. For a
  heat pump whose efficiency drops in the cold, enter outdoor temperature and
  COP pairs, such as `7: 5.2, 0: 4.0, -10: 3.0`. The first pair is the outdoor
  temperature where you measured the **Heating rate**. The heating rate is
  lowered to match the coldest hour of the forecast, so cold spells get more
  heating hours. This needs a **Home Assistant weather entity** or an entity
  attribute as the outdoor temperature source. Leave it empty to ignore it.
  Heating only.
- **Running sensor** — optional; tells the optimiser whether the load is
  actually running.
- **Control entity** — optional; if set, the integration switches this on
  and off to follow the plan, once you turn control on.

Temperature targets accept **5-95 °C** in both setup and the device controls.
This is the input range; the room-heating defaults are unchanged. Higher
targets do not switch models: a **Room** load retains its simple model with
heat loss relative to outdoor temperature. Choose **Water tank** explicitly
for water storage and estimated hot-water consumption.

## Its own device page

Every answer above becomes a setting on the load's device page, changeable
any time. A thermal load also keeps the same **Enabled**, time window
(**Earliest start** / **Latest finish** / **Restrict to a time window**),
**Most starts per plan** and **Cost of starting** settings as an ordinary
deferrable load — but has none of the run-time-target ones (*Hours needed per
day*, *Must run in one unbroken block*, *Recurrence*, *Requested*, *Run
now*), since its demand is the comfort band, not a number of hours.

It also gets one extra sensor:

- **Planned temperature** — the room temperature the plan currently expects,
  with the full forecast in its `forecast` attribute.

**Edit thermal load** in the integration's menu only asks for the things that
aren't controls: **Name**, **Temperature sensor**, **Heats or cools**,
**Running sensor** and **Control entity**. Everything else is on the load's
own page. A **Room, advanced heat pump** load also edits its house and heat
pump settings there (see below).

## Outdoor temperature

![The Outdoor temperature step](../assets/thermal-outdoor-temperature.png)

Once you have at least one thermal load, **Configure → Outdoor
temperature** appears, asking where the outdoor temperature forecast comes
from — it's what the room drifts towards while the load is off. A source is
required. A **Room, advanced heat pump** load, or a room with a **Heat pump
COP curve**, needs one of the first two sources:

- **Home Assistant weather entity** — hourly forecast via
  `weather.get_forecasts`. Pick one that actually offers hourly data; daily or
  twice-daily forecasts are too coarse to schedule heating against.
  The other two sources are under the collapsed **Advanced settings** section
  (it opens by itself if one of them is already selected).
- **Temperature forecast from an entity attribute** — for a weather
  integration that exposes its forecast as a list-valued attribute instead of
  through an action, or a template sensor you build yourself.
- **EMHASS built-in (Open-Meteo)** — no weather integration or API key
  needed, but requires your home's location to be set correctly under Home
  Assistant's Settings → System → Home information, and is only correct when
  the solar forecast is also EMHASS built-in.

The follow-up screen asks for what that source needs. **Home Assistant
weather entity** wants just a **Weather entity**. **Temperature forecast from
an entity attribute** wants a **Forecast entity**, an **Attribute name**, a
**Timestamp field** and a **Temperature field** — values must be in degrees
Celsius, which is what EMHASS's thermal model assumes. **EMHASS built-in**
asks for nothing.

## Room, advanced heat pump

Choose **Room, advanced heat pump** under **Add thermal load** if you would
rather describe your house than measure a heating rate. EMHASS then calculates
the heat pump's efficiency (COP) and the house's heat demand from the outdoor
forecast at each step. A cold night costs more heat and more electricity
without any tuning. Use **Room** instead if you have measured a heating rate
that works, or if the load cools.

### Before you start

- An outdoor temperature source the Companion can read: a **Home Assistant
  weather entity** or a **Temperature forecast from an entity attribute**.
  **EMHASS built-in (Open-Meteo)** is not enough, because the Companion needs
  the forecast itself. Without one, no plan is made and the error names the
  load.
- EMHASS **0.18.8 or newer**.
- A rough idea of the house's yearly heat demand. An energy certificate, or
  last year's delivered heat divided by the heated floor area, is a good
  start. Delivered heat is not the electricity bought: multiply the heat
  pump's yearly electricity use by its seasonal COP.

### The setup form

- **Room temperature sensor** — required. Every plan starts from this reading.
- **Electrical power when running** — what the heat pump draws while it runs,
  in W. EMHASS multiplies it by the calculated COP to get the heat delivered.
- **Comfort / Setback / Maximum temperature** and **Comfort starts / ends** —
  the same band as for a **Room** load. Setback must not be above comfort, and
  comfort must not be above maximum.
- **Thermal mass** — the volume of concrete-equivalent mass that stores the
  heat, in m³. A larger value means the temperature changes more slowly. If
  you are unsure, start low and raise it if the plan expects the room to cool
  faster than it really does.
- **Heated floor area** — in m².
- **Specific heating demand** — the house's yearly heat demand per m², in
  kWh/m². Often 50 to 150 for a house.
- **Supply temperature** — the water temperature sent to the radiators or
  floor heating. Typically 30 to 35 °C for floor heating and 45 to 55 °C for
  radiators. A lower value gives a higher COP. Starts at 35.
- **Running sensor** / **Control entity** — optional, as for any thermal load.
- **Advanced settings** — the defaults suit most houses:
  - **Carnot efficiency** — how close the heat pump gets to an ideal one.
    0.4 is typical. EMHASS keeps the resulting COP between 1 and 8.
  - **Base temperature** — the outdoor temperature above which the house needs
    no heating. Starts at 18 °C.
  - **Annual heating degree days** — the degree days of the year your specific
    heating demand refers to. Starts at 3000.
  - **Thermal inertia** — hours before delivered heat shows in the room, for
    example 1 to 3 for floor heating in concrete. Starts at 0 (immediately).

### After setup

Power, the three temperatures and the comfort times become controls on the
load's device page, like for a **Room** load. The house and heat pump settings
are not device controls. To change them, use **Edit thermal load**, which asks
for the name, sensors, control entity and every model setting above. The
**Heating rate**, **Heat loss constant** and **Response delay** controls do not
exist for this type.

If the room is too cold to reach its comfort temperature in time, the plan
does not fail. It aims for the warmest temperature the heat pump can reach
instead, and the *Last request to EMHASS* sensor's `warnings` attribute says so.
If the room sensor is unavailable, EMHASS starts the plan from 20 °C.

### Getting the numbers right

Compare **Planned temperature** with the real room temperature over a few
days:

- The planned temperature **falls faster** than the real one → raise
  **Thermal mass**, or lower **Specific heating demand**.
- The plan **runs more** than the heat pump really needs → lower **Specific
  heating demand**, or check **Supply temperature**.
- Comfort is **missed** at the start of the window → raise **Specific heating
  demand**, or set **Thermal inertia** if heat takes time to arrive.

## Water tank with an immersion heater

Choose **Water tank** under **Add thermal load**. This first version is one tank
with one electric immersion element. Room is still the default, and existing
loads are never converted based on their temperature targets. Heat pumps,
multiple sources, stratification, automatic consumption learning and disinfection
schedules are not included.

Provide tank volume (litres), heater power (watts), a tank temperature sensor,
and explicit **Lowest standby temperature**, **Required hot-water temperature**
and **Maximum temperature** limits within your hardware's permitted range.

Choose **Number of hot-water periods** (1–8), then fill in one page per period:
optional name, **Hot water needed from / until**, and **Estimated consumption
for this period** in kWh of heat withdrawn, not heater electricity. For example,
create Morning and Evening periods with separate consumption estimates.

Water must be ready at each start, with the required tank temperature maintained
throughout the period. The lowest standby temperature applies between periods;
prices and PV still determine heating times. Zero consumption means no expected
withdrawal; availability targets and standby heat loss still apply. Consumption
from overlapping periods is added.

Each period has start/end time controls, a consumption number and an enable
switch on the tank device. Changes save immediately and request a fresh
optimisation without reloading the integration when the tank load is enabled.
While the tank load is disabled, edits save without requesting optimisation.
All settings-triggered solves share a three-second delay that restarts with
each change; values save immediately, including whole-plan action updates.
Disabled periods keep their
settings but contribute no withdrawal or required-temperature window. With all
periods disabled, only the lowest standby temperature applies.

Automations can use those entities or replace the whole plan atomically:

```yaml
action: emhass_companion.set_hot_water_plan
data:
  device_id: YOUR_TANK_DEVICE_ID
  periods:
    - name: Morning
      draw_start: "06:00:00"
      draw_end: "08:00:00"
      draw_energy_kwh: 3
      enabled: true
```

Supply 1–8 periods. Omitted `enabled` defaults to true. To suspend hot-water
availability, disable the periods; an empty plan is rejected. The action
validates every period before saving, then requests one optimisation. Adding
periods creates their controls live; controls for removed slots become
unavailable. Slot numbers and entity IDs stay stable when renamed or disabled.

Power, temperature limits and **Standby loss** remain restored device controls.
Edit volume, period count and schedules by reconfiguring the tank. Existing
periods are prefilled, and changes save only after the final page. Reducing the
count keeps the first periods. Legacy single-window tanks run unchanged until
edited: their old consumption becomes one period, whose times also set hot-water
availability when saved. New tanks have no separate comfort-time controls.
Standby loss is continuous heat loss in watts, not hot-water consumption. It is
under **Advanced settings** when the tank is first created and starts at 60 W.
The room-model tuning controls do not appear for tanks.

**Heater COP** (1 for an immersion element; a heat-pump water heater's seasonal
COP otherwise) and **Comfort penalty** (0 keeps the required temperature a hard
limit; above 0 it becomes a priced target) are also restored device controls.
Like standby loss, both are offered under **Advanced settings** when the tank is
first created.

Use the manufacturer's standing loss or calibrate during a no-draw period;
compare predicted temperature with the actual sensor before relying on the
schedule unattended. A single sensor cannot describe a stratified tank perfectly,
and unexpected draws can still exhaust the hot water. Estimated consumption is
not automatically learned from temperature changes.

The tank model needs a known EMHASS **0.18.8 or newer**, but no outdoor forecast
for immersion heating. Other forecast profiles keep their own requirements.
Windows follow local time and may cross midnight. Equal consumption endpoints
mean a full day. Daily energy is conserved through daylight-saving changes;
partial horizons receive only their overlapping share. Pre-heating ramps use actual
elapsed time.
ambiguous endpoints use the first occurrence and missing endpoints advance to
the next valid local time.

For enabled tanks, invalid/missing telemetry blocks new solves and switching, including
forced-on mode; Should run reports unknown. Existing switch state is left alone,
and control resumes only after a valid solve. Hardware thermostats and
over-temperature protection remain authoritative. Do not configure two loads
for the same physical heater or tank.

## Getting the numbers right

Start with the defaults and adjust from what you observe:

- Plan heats **too early** → heat loss constant too high, or heating rate
  too low.
- Comfort **missed** at the start of the window → heating rate too
  optimistic, or the response delay is real and set to 0.
- The plan never pre-heats → widen the gap between comfort and maximum
  temperature; that gap is what it has to play with.
