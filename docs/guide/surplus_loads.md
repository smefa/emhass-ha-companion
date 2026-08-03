# Surplus loads

A "surplus load" isn't a separate thing you add — it's a deferrable load
with **Runs** (or **Recurrence**) set to **On spare solar**. See
**[Deferrable loads](deferrable_loads.md)** for how to add and configure
one. This page covers the extra sensors that appear once you have at least
one.

## On the load itself

![A surplus load's Configuration](../assets/surplus-load-configuration.png)

- **14. Energy needed** — an optional total cap, in kWh. 0 means no cap —
  the load takes whatever spare solar is going for as long as it's armed.
- **15. Spare solar headroom** — extra margin, in W, above the load's own
  draw before a moment counts as "spare" for it.
- **16. Surplus priority** — with more than one spare-solar load, the lower
  number gets first claim.
- **Spare solar budget** (diagnostic sensor) — how many hours the last plan
  allowed this load to ask for.

![A surplus load's Diagnostic section](../assets/surplus-load-diagnostic.png)

## On the hub device

Once any load is set to *On spare solar*, these sensors appear:

- **Spare solar** (sensor) — spare power right now, with the full forecast
  in its `forecast` attribute.
- **Spare solar energy** (sensor) — kWh of spare solar still ahead in the
  plan's horizon.
- **Spare solar from** / **Spare solar until** (sensors) — the start and end
  of the current reporting window.
- **Spare solar** (binary sensor) — on when spare solar is above the
  reporting threshold, below.
- **Spare solar threshold** (number) — what counts as "spare" for these
  reporting sensors. This is display-only — it doesn't change what any load
  actually gets; each load uses its own headroom setting for that.
