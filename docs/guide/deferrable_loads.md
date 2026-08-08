# Deferrable loads

A deferrable load is anything whose **timing** can move — a dishwasher, a
car charger, a pool pump. Add one from **Settings → Devices & Services →
EMHASS Companion → Add deferrable load**.

## Add a deferrable load

![The Add a deferrable load dialog](../assets/deferrable-add-load.png)

- **Name**
- **Runs** — decides which questions come next, and which settings the load
  ends up with:
    - **Every day** — it needs a set number of hours every day, and the
      optimiser picks when.
    - **On demand** — it only runs when something arms it, for a set number
      of hours. See the *Recurrence* and *Requested* entries below.
    - **On spare solar** — it takes whatever solar the house, the other
      loads and the battery aren't already using, for as long as it's
      armed. Has no hours and no time window of its own — both come from
      the plan. This mode is a Companion feature; EMHASS has none of its
      own, so there's nothing to set up for it on the EMHASS side. See
      **[Surplus loads](surplus_loads.md)** for the extra sensors this adds.

## Load settings

The next screen seeds the load. Which fields it shows depends on the **Runs**
answer above, and every one of them becomes a control on the load's own
device page afterwards — so nothing here is a permanent decision.

- **Power when running** — roughly what it draws while on, in W.
- **Lowest power while running** — the least it may draw, if it can
  modulate. Only used when *Runs at full power only* is off.
- **Runs at full power only** — on for a dishwasher or pump, which are
  either on or off. Off for something whose power can be varied, such as a
  car charger.
- **Cost of starting** — discourages switching on and off repeatedly. Leave
  at zero unless that's a problem. On spare solar this is the right way to
  stop short-cycling: it prices a restart rather than forbidding a break, so
  the load still runs in one block on a clear day but may split across two
  peaks on a cloudy one.
- **Hours needed per day** — how long it needs to run in total; the optimiser
  chooses when. Rounded to a whole number of plan timesteps.
- **Earliest start** / **Latest finish** — the time window. Only used if you
  turn the load's *Restrict to a time window* switch on afterwards. The
  finish may be earlier than the start, for an overnight window.
- **Must run in one unbroken block** — on if interrupting it would be a
  problem, such as a wash cycle.
- **Most starts per plan** — a cap on how many times it may switch on.
- **Minimum time on** / **Minimum time off** — once switched on or off, it
  must stay that way at least this long. Protects compressor-driven loads
  from short-cycling. Zero means no minimum.
- **Energy needed** — an optional total, in kWh: the load stops asking once
  it's had this much. Zero lets it run on whatever solar is spare until you
  turn its *Requested* switch off.
- **Spare solar headroom** — how much spare solar there must be *beyond* this
  load's own draw before a timestep counts. A margin against the forecast
  coming in light.
- **Surplus priority** — only matters with more than one load on spare solar.
  The lowest number claims the shared surplus first; ties break by name.
- **Running sensor** — optional, and the most reliable way to tell the
  optimiser whether the load actually ran — catching a run that finished
  early, never happened, or started at a different time. A power sensor gives
  the most detail (any unit is converted to watts); a plain on/off
  `binary_sensor` works too.
- **Control entity** — optional. If set, the integration switches this on and
  off to follow the plan once you enable control, and its state doubles as
  the running signal when there's no power sensor. Leave it empty to keep
  control in your own automations.

With neither a running sensor nor a control entity, the optimiser falls back
to trusting its own previous plan — assuming the load ran whenever it was
scheduled to, which is only as accurate as your own follow-through.

## Configuration

Every field the load's device page can show, in the order it appears:

- **01. Enabled** — include this load in the plan at all. Off parks it —
  the optimiser skips over it entirely.
- **02. Hours needed per day** — how many hours it should run each day.
  *Every day* loads only.
- **03. Power when running** — how much it draws at full power, in W.
- **04. Lowest power while running** — the least it may draw while on, if
  it can modulate. Only used when *12. Runs at full power only* is off.
- **05. Earliest start** — the window may not start before this time.
- **06. Latest finish** — the window may not run past this time. Together
  with 05, only applies when *13. Restrict to a time window* is on.
- **07. Run within** — a deadline, in hours from when it's armed. *On
  demand* loads only.
- **08. Most starts per plan** — a cap on how many times it may switch on.
  0 means no cap.
- **09. Cost of starting** — a small penalty added each time it switches on,
  to discourage it from starting and stopping repeatedly.
- **10. Recurrence** — *Every day* / *On demand* / *On spare solar*. Same
  choice as *Runs* above — change it here any time.
- **11. Must run in one unbroken block** — forces a single continuous run
  instead of splitting across several.
- **12. Runs at full power only** — on: strictly on/off at full power. Off:
  it can modulate down to *04. Lowest power while running*.
- **13. Restrict to a time window** — turns *05* and *06* on or off.
- **14. Energy needed** — an optional total energy cap, in kWh. 0 means no
  cap. *On demand* and *On spare solar* loads only.
- **15. Spare solar headroom** — extra margin, in W, a moment must clear
  above the load's own draw before it counts as "spare". *On spare solar*
  loads only.
- **16. Surplus priority** — which load gets first claim on spare solar when
  you have more than one. Lower number goes first. *On spare solar* loads
  only.
- **17. Minimum time on** / **18. Minimum time off** — once the load switches
  on or off, it must stay that way at least this long, so the plan can't ask
  it to cycle faster than the hardware allows. Protects compressor-driven
  loads (a heat pump, a freezer) from short-cycling. 0 means no minimum.
  *Every day* and *on demand* loads only — a load on spare solar has no hard
  timing constraints of its own.

## Editing a load

**Edit deferrable load** in the integration's menu only asks for the three
things that aren't controls: **Name**, **Running sensor** and **Control
entity**. Everything else — power, run time, the time window, the load model
— is on the load's own page, changeable there or from an automation.

## Load groups

Two or more of your loads can share a circuit — a subpanel or fuse limit, or
an EV charger and an immersion heater that must never run at once. That's a
**load group**, added the same way as a load, from **Add load group** in the
integration's menu:

- **Name**
- **Loads in this group** — at least two existing deferrable loads.
- **Only one may run at a time** — on if these loads must never overlap, such
  as two appliances on the same fuse.
- **Shared power budget** — the combined power they may draw at once, in W.
  Required unless *Only one may run at a time* is on.

See **[Load groups](../load_groups.md)** in the technical reference for the
full detail.

## Controls & Sensors

![A load's Controls and Sensors](../assets/surplus-load-controls-sensors.png)

- **Requested** (switch) — arm a run. *On demand* and *On spare solar*
  loads only. Turning it off cancels a pending run.
- **Start as early as possible** (switch) — take the spare solar as soon as
  there is any, instead of letting the optimiser pick a moment later in the
  day. *On spare solar* loads only. It pays for that by claiming a few per cent
  less of the day's surplus. Off by default, and off is usually the better
  setting — see **[Surplus loads](surplus_loads.md)**.
- **Run now** (button) — force the load on right now, regardless of the
  plan. Not available on *On spare solar* loads, where ignoring the plan
  would mean running off the grid at night; use *Start as early as
  possible* there instead.
- **Next start** (sensor) — when it's next due to start.
- **Running** (binary sensor) — whether it's actually observed running.
- **Runtime today** (sensor) — how long it's run today.
- **Scheduled power** (sensor) — what the plan wants right now.
- **Should run** (binary sensor) — **the one to automate against.** Combines
  the plan with any manual override.

## Diagnostic

- **EMHASS deferrable number** — which numbered slot (`P_deferrable0`,
  `P_deferrable1`, …) this load occupies in EMHASS's own output.
- **Spare solar budget** — how many hours the last plan allowed this load to
  ask for. *On spare solar* loads only — see
  **[Surplus loads](surplus_loads.md)**.

## Making it actually run something

Nothing above switches your appliance on by itself unless you set a
**Control entity** when adding the load. Without one, the load is advisory
only, and you automate against **Should run** yourself:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.dishwasher_should_run
    to: "on"
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.dishwasher
```
