# Network tariffs

If your electricity bill has a second charge beyond the per-kWh energy price —
a time-of-day network fee, or a charge based on your *highest* power draw in a
month — this is what teaches the plan about it, so it stops scheduling
everything into the same expensive hour.

It's optional and off by default. Configure it under **Settings → Devices &
Services → EMHASS Companion → Configure → Network tariff**.

## Network tariff step

You're offered every `network`-kind profile available — two ship in-box:

- **Göteborg Energi** — a Swedish high-load-season tariff: a winter weekday
  window with a higher per-kWh fee, plus a demand charge on your three highest
  hourly peaks.
- **Amber Electric** — an Australian demand tariff: a single daily window in
  which your highest 30-minute draw sets a charge for the whole month.

Neither matches your operator? Leave this step unset — nothing changes — or
write your own profile; see [Writing a profile](../profiles.md).

## Configuring the profile

Every number on the tariff sheet is a field here, not something baked into the
profile — so a rate change next year is an edit on this screen, not a wait for
an update. For **Göteborg Energi**:

- **High-load energy fee** / **Low-load energy fee** — öre/kWh inside and
  outside the winter weekday window.
- **Demand charge** — kr/kW/month on the high-load peak.
- **Workday sensor** — a `binary_sensor` from the
  [Workday](https://www.home-assistant.io/integrations/workday/) integration,
  set up for Sweden with weekends and holidays excluded. This is what tells
  the profile which dates are red days. Don't have one yet? Add the Workday
  integration first, and while you're there, add Christmas Eve and New Year's
  Eve under its "Add holidays" — Göteborg Energi excludes both from the
  high-load window even though neither is an official Swedish holiday.
- **Subscribed capacity** — optional. Only set this if you want the plan held
  under your contracted tier as a hard ceiling; leave it blank otherwise.

For **Amber Electric**:

- **Demand rate** — $/kW/day, exactly as quoted on your Amber plan page.
- **Demand window start** / **end** — the daily window your distributor sets;
  check your plan page, it varies by distributor.
- **Months the window applies** — leave every box unticked for a window that
  runs all year; tick specific months only if yours is seasonal.

Once saved, set **Time resolution** (under **Configure → Setup**) to match the
tariff's own metering interval — 60 minutes for Göteborg, 30 minutes for
Amber. A coarser optimisation step under-protects the window; a finer one is
just extra caution.

## What you'll see

New sensors appear on the integration's hub device:

- **Network tariff band** — which price band is active right now (e.g.
  "höglast"/"låglast" for Göteborg), with the next change time in its
  attributes.
- **Demand charge rate** (diagnostic) — the actual number being sent to
  EMHASS to price the peak, converted from the sheet rate on the tariff sheet.
  Its attributes explain the arithmetic and, if it reads 0, why.
- **Billing period peak** — the demand-charge aggregate incurred so far this
  billing period, with every contributing interval listed in its attributes.
- **Peak headroom** — how much more you can draw right now before setting a
  new peak. This is the one worth wiring into your own automations — e.g. hold
  an EV charger back when it's tight.
- **Peak target** (number, under Configure) — a manual ceiling used as a
  fallback on an EMHASS add-on older than 0.18.1, which can't yet price a
  windowed demand charge directly (see the note below). Pre-filled from your
  current period's peak; adjust it if you want the plan to hold under a
  different level.

!!! note "Why the plan sometimes just holds a hard limit"

    Pricing a demand charge so the optimiser can trade it off against energy
    cost needs a feature (a "demand window mask") that reached a stable EMHASS
    release in **0.18.1**. On that version or newer, a tariff with a real time
    window (both built-in profiles have one) prices cleanly. On an older
    add-on it's enforced as a hard ceiling using the **Peak target** number
    instead of a price — it works, but it's a blunter tool than the priced
    version — see
    [Network tariffs, "Version gating"](../network_tariffs.md#version-gating)
    for the full picture.

## What this doesn't do

It won't stop a real-time spike — a kettle, an oven and the car charger all
switching on in the same half-hour can still blow the window the plan thought
was safe. The plan only avoids *scheduling* peaks; the **Peak headroom**
sensor is what lets your own automations react to one forming in real time.
