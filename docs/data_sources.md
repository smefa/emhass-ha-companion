# Data sources

Every source is a **YAML profile** — data, not code. Adding support for
another integration means writing one file, and you can drop it in yourself
without waiting for a release.

Built in today:

| Kind | Profiles |
|---|---|
| Price | Nord Pool (core), Nord Pool (HACS), ENTSO-E, Tibber, Fixed tariff, Any entity attribute |
| Solar | Solcast, EMHASS built-in (Open-Meteo), Forecast.Solar, Any entity attribute, No solar |
| Load | House load sensor, Any entity attribute, Typical household (no sensor needed) |
| Inverter | Mode select plus power number, Scripts |

The **Any entity attribute** profiles are the escape hatch: point them at any
entity exposing a forecast as a list attribute, name the fields, and they
work. If you get one working for an integration others use, it is worth
contributing as a named profile.

> **Note on Forecast.Solar.** The Forecast.Solar profile has EMHASS query the
> service directly rather than reading Home Assistant's `forecast_solar`
> integration. That integration exposes only single-value sensors —
> production now, next hour, today's total — and no forecast series, so
> there is nothing to read from it.

See [profiles.md](profiles.md) to write your own or contribute one.
