# Template additions (price and solar)

The price and solar lists in [Setup](guide/setup.md) aren't the whole
universe of what you can use — they're just what ships. If your integration
isn't on either list, you don't have to wait for a release: drop in a small
YAML file of your own and it appears as a regular option next to the
built-in ones.

This page covers the fast path — a `template` source, hardcoded to your
entity. It's the same mechanism the **Prices from an entity attribute** and
**Solar forecast from an entity attribute** built-ins use internally, just
written out by hand instead of filled in through a form.

## Where the file goes

```
<config>/emhass_companion/profiles/price/my_source.yaml   # a price source
<config>/emhass_companion/profiles/pv/my_source.yaml      # a solar source
```

Reload the integration after adding or editing one, and it shows up the
next time you (re-)run setup or change the source in **Configure**.

## A price source

```yaml
name: My custom price source
kind: price
version: 1
description: Reads today and tomorrow off my own price sensor.

detect:
  always: true

source:
  type: template
  value: >
    {{ state_attr('sensor.my_prices', 'today')
       + state_attr('sensor.my_prices', 'tomorrow') }}
series:
  time: t
  value: p

unit: currency_per_kwh
```

The template must render to a list of records — whatever field names your
sensor actually uses, mapped by `series.time` / `series.value`. Prices
should be **raw spot**, before VAT and fees; that composition happens
separately in the **Buy and sell prices** step — see [Setup](guide/setup.md).

## A solar source

```yaml
name: My custom solar forecast
kind: pv
version: 1
description: Reads my forecast integration's own sensor.

detect:
  always: true

source:
  type: template
  value: >
    {{ state_attr('sensor.my_forecast', 'forecast') }}
series:
  time: period_start
  value: pv_estimate
  scale: 1000   # only needed if your source reports kW rather than W

unit: watts
```

## Checking it before you rely on it

Call the `emhass_companion.test_profile` action against your new profile —
it renders the template and shows you exactly what came back, without
touching your live configuration.

## Beyond hardcoding

Both examples above bake in one specific entity. If you want the entity (or
the attribute name, or the scale) to be a question asked during setup
instead — so the same file works for more than just your own system, or so
you can contribute it back — that's an `options:` block, covered in
**[Writing a profile](profiles.md)**. That page also covers the other two
source types (`attributes`, `service`), `load` and `inverter` profiles, and
the full `emhass:` escape hatch for handing a source to EMHASS's own
forecasting.
