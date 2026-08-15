# Tariff card — plan

Network tariffs ([network_tariffs_plan.md](network_tariffs_plan.md)) shipped
in 0.9.7.0 with five entities: **Network tariff band**, **Demand charge
rate**, **Billing period peak**, **Peak headroom**, and **Peak target**. All
five are real, all five are useful, and none of them is anywhere but the
entity list — the three [lab status cards](lab_cards.md#three-status-cards)
answer "is the optimiser healthy", "what is it doing to the house" and "why",
and none of the three is "what is my tariff doing to me right now". A user
with a demand charge configured currently has to know those five entity names
exist before they can watch any of them.

This is a frontend-only card, same posture as the three status cards it joins:
no payload change, no new sensor, no backend touch. It reads what
`network_tariffs_plan.md` already publishes.

## Who sees it, and who doesn't

The card only earns a place on a dashboard once a network profile is
configured — most installs never set one, and a card with nothing to show is
worse than no card. Given one, the two tariff shapes documented in
[network_tariffs.md](network_tariffs.md) don't fill the same layout:

- **Bands only** (a Tibber household with a time-differentiated network fee,
  no demand charge) — the band section applies, the demand section has
  nothing to draw and is left out entirely, not shown empty.
- **Demand only** (Amber) — `energy_bands: []`, so the band section is left
  out; the demand section is the whole card.
- **Both** (Göteborg Energi) — both sections draw.

Which sections exist is read off the profile at render time — `sensor.*_
network_tariff_band` existing or not is the same signal `network_tariffs.md`
already documents ("the band and rate sensors exist whenever a network
profile is selected; the peak, headroom and peak-target entities exist only
once that profile defines a `demand_charge`") — not a card option, so there is
nothing to configure wrong.

## What it shows

### Band section

A single-line banner, same register as the status card's control banner but
without its colour semantics (a band isn't good or bad, only cheaper or
dearer): the current band's name, its adder/multiplier, and a countdown to
`next_change` with `next_band`'s name after it — "Höglast (+45 öre) · shoulder
in 2 h 14 m". Ticks over live between optimisation runs; this is a template
sensor's own state, not something the card polls the backend for.

### Demand section

The one visual worth designing rather than tiling: a **headroom arc**, the
same shape as the plan card's charge-level rail turned into a gauge rather
than a timeline, because a demand charge is a single number to stay under,
not a series to read over time.

- The arc fills toward the *effective rate* becoming expensive to test, not
  toward a fixed 100 % — `sensor.*_peak_headroom` at its emptiest is the
  number that matters, so the arc is headroom counting down, red under a
  threshold (default: under 10 % of the period's own peak), not a green bar
  that happens to shrink.
- Centre figure: `sensor.*_peak_headroom` itself, in kW, live — "1.8 kW to a
  new peak".
- Below the arc: the period's billed aggregate (`sensor.*_period_peak`) with
  `days_remaining` from its attributes — "6.4 kW this period · 11 days left".
- A **within window now** pill, sourced from `sensor.*_demand_charge_rate`'s
  `active` attribute — the same fact the diagnostic sensor already carries,
  surfaced where it's actually useful: whether the headroom number means
  anything *right now* or the window simply hasn't opened yet today.
- A **fallback** pill in place of the window pill when `reason` on the rate
  sensor says the priced peak isn't active (see
  [network_tariffs.md, "Version gating"](network_tariffs.md#version-gating)) —
  "Holding to peak target · 6.4 kW" instead, since the arc means a different
  thing (a hard cap, not a price) once EMHASS is on an older backend, and
  saying so beats a headroom number the optimiser isn't actually pricing.

Tapping the arc or either pill opens the entity behind it, same convention as
every other lab card.

### Rate row

Two value boxes under the demand section, filled from
`sensor.*_demand_charge_rate`'s attributes rather than new entities:

| Box | Reads |
| --- | --- |
| Effective rate | `capacity_cost_per_kw` itself, the number actually priced this run |
| Sheet rate | `sheet_rate` + `rate_basis`, e.g. "$0.30/kW/day" — what's on the bill, for checking the conversion by eye |

`aggregate` (`max` or `mean_top_n`) is not its own box — it explains the
period-peak figure above rather than standing alone, so it goes in that box's
tooltip instead of taking a tile.

```yaml
type: custom:emhass-tariff-card
```

No required options — same "finds its own entities through the device,
draws nothing it can't find" contract as the other three status cards.

| Option | Default | Hides |
| --- | --- | --- |
| `show_band` | `true` | the band banner (only ever drawn when the entity exists) |
| `show_demand` | `true` | the whole demand section |
| `show_rate` | `true` | the sheet-rate / effective-rate boxes |
| `headroom_warn_pct` | `10` | the headroom percentage below which the arc turns red |

## Deliverables

1. **`frontend/emhass-tariff-card.js`**, registered in `frontend.py`'s
   `BUNDLES` alongside the other lab cards, `custom:emhass-tariff-card`.
   Same constraints as every other lab card — see
   [lab_cards.md, "Notes for anyone editing these"](lab_cards.md#notes-for-anyone-editing-these):
   plain custom elements, inline SVG, no Lit, no build step, ES2017 only,
   built-once DOM updated in place, checked by `tests/test_packaging.py`.
2. **Visual editor**, built on `loadHaForm()` the way the other three lab
   cards already share it, driven by a `TARIFF_SECTIONS` list the same shape
   as `STATUS_SECTIONS` so the editor needs no separate maintenance.
3. **Existence checks**, not new entities: the card looks up
   `sensor.*_network_tariff_band` and `sensor.*_period_peak` by
   `domain.translation_key` off the hub device (the same lookup
   `lab_cards.md` documents for the existing three) and leaves out whichever
   section finds nothing, per ["Who sees it, and who
   doesn't"](#who-sees-it-and-who-doesnt) above.
4. **Docs** — a new entry in [lab_cards.md](lab_cards.md), same section
   style as the existing three status cards, plus a screenshot once the card
   exists to screenshot.
5. **Version** — a small bump on `dev`, in the usual style. Do not touch
   `main`.

## Out of scope

- **Any control.** Every value here is diagnostic; nothing on this card
  writes anything, unlike `number.*_peak_target` which stays adjustable only
  from its own entity (or a generic number card), not duplicated onto this
  one.
- **A history view of the period's contributing intervals.** `sensor.*_
  period_peak` already lists them in its attributes for anyone who wants to
  dig; a second visualisation of that list is a card of its own, not a box
  on this one, if it turns out to be wanted.
- **Auto-detecting the Amber demand window.** Orthogonal — see
  [network_tariffs_plan.md, Open questions](network_tariffs_plan.md#open-questions).
  This card draws whatever the profile currently believes the window is,
  configured by hand or not.
