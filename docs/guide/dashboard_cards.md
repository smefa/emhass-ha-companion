# Dashboard cards

Two cards ship with the integration and add themselves to Home Assistant —
no separate install, and nothing that can end up on a different version
than the integration itself.

## Plan card

```yaml
type: custom:emhass-plan-card
```

The whole plan in one view: solar as a filled area, consumption and grid as
lines, battery charge level and both prices below it, and a row per
deferrable load showing when it's scheduled to run. A vertical line marks
*now*.

![The plan card: solar and consumption as a filled area and lines above, charge level and both prices below, and a row per deferrable load](../assets/plan-card.png)

## Deferrable load card

```yaml
type: custom:emhass-deferrable-card
load: Dishwasher    # omit to show the first load
```

One load at a time: whether it should run now, its scheduled power, next
start, runtime today, a timeline of its schedule, and its enable/mode
controls.

![A deferrable load card: scheduled power, next start, deadline and runtime today, over a timeline of the load's schedule](../assets/deferrable-card.png)

## Adding a card

In a dashboard, **Add card → search for "EMHASS"**, or paste the YAML above
into an existing view in YAML mode. Both cards find their entities
automatically, so renaming an entity later won't break them.

> If you manage your dashboards entirely in YAML rather than through the
> UI, the integration can't register itself for you — check the log after a
> restart for the exact resource line to add.
