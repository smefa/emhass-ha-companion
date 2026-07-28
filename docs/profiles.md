# Writing a profile

A profile teaches EMHASS Companion how to read one data source, or how to drive one inverter. It is **data, not code** — a YAML file with no Python in it.

That choice is deliberate. A Python plugin folder inside the integration would be wiped by every HACS update, is an arbitrary-code-execution surface, and breaks whenever Home Assistant changes an API. Profiles survive updates, are reviewable, are testable as fixtures, and need no Python to contribute.

## Where profiles live

```
custom_components/emhass_companion/profiles/builtin/<kind>/   shipped
<config>/emhass_companion/profiles/<kind>/                    yours
```

`<kind>` is `price`, `pv`, `load` or `inverter`.

Your directory is outside the integration, so **HACS updates never delete your files**. A file whose name matches a built-in *replaces* it — which means you can patch a broken built-in locally instead of waiting for a release.

Reload the integration after adding or editing a profile.

## Minimum shape

```yaml
name: Human readable name        # shown during setup
kind: price                      # price | pv | load | inverter
version: 1                       # profile schema version
description: One line, shown when choosing a source.
```

A source profile must then do at least one of:

- define a **`source`** block, to fetch a time series itself, or
- define an **`emhass`** block, to hand the job to EMHASS's own forecasting

A profile doing neither is rejected at load time, because it would contribute nothing.

## `detect` — when to offer this profile

```yaml
detect:
  integration: solcast_solar      # only offered if this integration is loaded
```

```yaml
detect:
  any_integration: [nordpool, entsoe]
```

```yaml
detect:
  always: true                    # always offered
```

This is what keeps the setup flow short: nobody is asked to choose between thirty sources when only two are installed.

## `options` — the questions asked during setup

Each option becomes a field, using any [Home Assistant selector](https://www.home-assistant.io/docs/blueprint/selectors/).

```yaml
options:
  entity:
    name: Nord Pool sensor
    description: The sensor exposing raw_today and raw_tomorrow.
    required: true                # default
    selector:
      entity:
        integration: nordpool
        domain: sensor
```

Answers are available everywhere else in the profile as `{{ options.<key> }}`.

> Selector configuration is validated when the profile loads. Note `NumberSelector` rejects a `step` below `0.001` — use `step: any` for prices and scale factors.

## `source` — fetching a series

Three types. Anything they cannot express belongs in a `template` source, **not** in a new keyword.

### `attributes` — read list-valued attributes

The common case: an integration already exposes a forecast as an attribute.

```yaml
source:
  type: attributes
  entity: "{{ options.entity }}"      # a string, or a list of entity ids
  attributes: [raw_today, raw_tomorrow]   # concatenated in order
series:
  time: start
  value: value
```

A missing attribute is skipped with a debug log, not an error — `raw_tomorrow` is legitimately absent until the market publishes. A missing *entity* is an error.

That behaviour is also what lets one profile cover both the one-attribute and
two-attribute cases: list an optional second attribute whose name comes from an
option, and a blank value simply finds nothing and is skipped.

### `service` — call an action that returns data

```yaml
source:
  type: service
  service: nordpool.get_prices_for_date
  for_days: [0, 1]                    # engine iterates; exposes {{ day }}
  data:
    config_entry: "{{ options.config_entry }}"
    date: "{{ day }}"
  response_path: "{{ options.area }}" # dotted path into the response
series:
  time: start
  value: price
  scale: 0.001                        # per MWh -> per kWh
```

A call for a *future* day that fails is skipped rather than failing the run, since tomorrow's data does not exist for most of the day. A failure on day `0` is an error.

### `template` — the escape hatch

Must render to a list of records. This is deliberately the same thing people already write inside `rest_command` payloads, so porting an existing setup is copy-paste.

```yaml
source:
  type: template
  value: >
    {{ state_attr('sensor.my_prices', 'today')
       + state_attr('sensor.my_prices', 'tomorrow') }}
series:
  time: t
  value: p
```

## `series` — mapping records

```yaml
series:
  time: period_start     # field holding the timestamp
  value: pv_estimate     # field holding the number
  scale: 1000            # optional, default 1
  offset: 0              # optional, default 0, applied after scale
```

Every field may be a template, so a profile can expose the choice as an option rather than shipping one variant per combination:

```yaml
series:
  time: period_start
  value: "{{ options.estimate }}"     # pv_estimate / pv_estimate10 / pv_estimate90
  scale: 1000
```

Rules the engine applies:

- Timestamps **must carry a timezone**. Everything is normalised to UTC internally.
- Records are sorted; on duplicate timestamps the **later record wins** (concatenated "today" and "tomorrow" blocks often share a boundary).
- A record whose value is `null` is skipped as a data gap. A non-numeric value is an error.

Declare what you produced:

```yaml
unit: watts               # pv and load profiles
unit: currency_per_kwh    # price profiles — raw spot, before tariff composition
```

Price profiles should return **raw spot**. Buy/sell composition is the user's tariff configuration. If your source already includes VAT and fees and cannot be made to exclude them, say so in `notes:` and tell users to select *Already includes costs*.

## `emhass` — contributing settings instead

For sources that fetch nothing and instead let EMHASS do the work:

```yaml
emhass:
  set_use_pv: true
  weather_forecast_method: open-meteo
  pv_module_model: ["{{ options.peak_power_w }}"]
```

Keys are EMHASS runtime parameters. Use the current long names (`operating_hours_of_each_deferrable_load`), not the legacy short aliases. Templates render with their real types, so numbers arrive as numbers.

These settings are merged into every optimisation request, after the integration's own defaults — so a profile can override a default, which is how *No solar* works.

## `inverter` profiles

Map roles to your existing entities, and actions to service calls against your existing integration. This integration never talks to hardware directly.

```yaml
name: Example hybrid inverter
kind: inverter
version: 1
sensors:
  battery_soc: "{{ options.soc_entity }}"
limits:
  battery_capacity_wh: 10000
actions:
  self_consume:
    - service: select.select_option
      target: {entity_id: "{{ options.mode_select }}"}
      data: {option: "Self Consumption"}
  force_charge:
    - service: number.set_value
      target: {entity_id: "{{ options.power_number }}"}
      data: {value: "{{ power_w }}"}
  force_discharge: ...
  idle: ...
```

Action templates get `power_w`, `soc` and `soc_target`. Any action can call `script.turn_on` instead, which covers inverters needing multi-step or conditional sequences without a plugin loader.

> Inverter profiles are defined but not yet executed — that is phase 3.

## Testing a profile

```yaml
action: emhass_companion.test_profile
data:
  profile: price/nordpool_custom
  limit: 20
response_variable: result
```

It returns the resolved series, the point count, the detected step, the settings contributed, and any error. Use it rather than guessing: a YAML source that silently yields nothing is much harder to diagnose than a Python one that raises.

## Contributing a profile

1. Put the file in `custom_components/emhass_companion/profiles/builtin/<kind>/`.
2. Add a fixture in `tests/fixtures/` — a recorded attribute blob or service response from a real installation.
3. Add a test asserting the series it resolves to.

The fixture is required. It is what lets maintainers verify a profile for an integration none of them has installed, and it makes reviewing your PR mechanical rather than a matter of trust.

## Before writing a source profile: check there is a series to read

Not every integration exposes one. Home Assistant's core `forecast_solar`, for
example, has only single-value sensors — production now, next hour, today's
total — with no list attribute and no action returning a series. A profile
reading from it could not work, so the shipped Forecast.Solar profile has
EMHASS query the service directly instead, via an `emhass:` block.

Look for one of these before starting:

- a list-valued attribute on some entity (most common), or
- an action returning response data containing a list

If neither exists, the source cannot be a `source` profile. It may still be
worth an `emhass:` profile if EMHASS itself can fetch it.

## Versioning

`version: 1` is the current profile schema. The schema is a public API: changing the meaning of an existing key requires a version bump and a documented migration. A profile declaring a version newer than the integration understands is rejected rather than being misread.
