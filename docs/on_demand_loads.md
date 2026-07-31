# On-demand deferrable loads

*Status: implemented (recurrence select, requested switch, auto-disarm,
tests, translations). Blueprints not yet written. The deferred
parameterised-request service is planned separately in
[request_run_service.md](request_run_service.md).*

## The problem

The companion currently assumes every enabled deferrable load wants its
`operating_hours` every single day. Many loads don't work like that:

- a hot water tank only needs heating when its temperature has dropped,
- a dishwasher only needs a run after someone has loaded it,
- a pool pump may only need to run after heavy use.

These are all the same abstraction: a load with **no standing demand** that
gets **armed by an event** — a sensor crossing a threshold, or a human. The
trigger vocabulary belongs to Home Assistant automations; what belongs in the
integration is the **request lifecycle**, because only the integration knows
when a run has actually completed (it already tracks runtime per load in
`DeferrableRegistry`), and hand-rolling completion detection, midnight
boundaries and restart persistence in user automations is exactly what
everyone would get subtly wrong.

Guiding principle: **automations declare *whether* and *how much*; EMHASS
keeps deciding *when*.** Triggers only flip a switch on the load's device —
they never drive the appliance directly.

## New entities (per deferrable load)

Both follow the existing entity-owned pattern: `Restore*Entity`, registry is
the runtime source of truth, no config-entry reload on change.

- **`select.<load>_recurrence`** — `daily` | `on_demand`. Defaults to `daily`,
  which is exactly today's behaviour, so existing installs are untouched. No
  new subentry form field: the user flips the select after creating the load.
- **`switch.<load>_requested`** — only meaningful in `on_demand` mode. On =
  a run is requested; the load enters the optimisation until the run is
  completed. Off = no demand (and turning it off cancels a pending request).
  A switch rather than a button so that automations can *cancel*, dashboards
  show pending state, and conditions can read it ("don't re-notify if already
  requested").

No separate `binary_sensor` for pending state — the switch itself is the
observable state.

## Registry changes

`DeferrableRuntime` gains two entity-owned fields:

```python
recurrence: str = RECURRENCE_DAILY   # "daily" | "on_demand"
requested: bool = False
```

**Payload:** a load participates in the optimisation when
`enabled and (recurrence == "daily" or requested)`. Not-requested on-demand
loads take the *same* path as `enabled=False` loads — excluded from the
`active` list in `payload.py` — so there are no new payload semantics, and
the per-run `load_order` bookkeeping already copes with loads entering and
leaving the active set.

**Auto-disarm (the part users can't build themselves):** when an on-demand
load's `elapsed_today` reaches its `operating_hours` target, the registry
clears `requested` and notifies, turning the switch off. Checked at the
existing observation points:

- in `_async_source_changed`, on the running→stopped transition, and
- after `assume_from_plan` for sourceless loads, so completion works even
  when the only evidence is our own past plan.

**Midnight:** `reset_day` clears runtime counters but **not** `requested`.
An unfulfilled request deliberately survives the day boundary — a dishwasher
loaded at 23:00 should run in the cheap early-morning hours, and the reset
re-opens the full `operating_hours` for the new day. A fulfilled request has
already disarmed itself before midnight.

**Force modes:** unchanged and orthogonal. `resolve_should_run` still only
overrides the *signal*; an unrequested on-demand load is simply absent from
the plan, exactly like a disabled one.

## Blueprints

Shipped in `blueprints/` in the repo, importable via `my.home-assistant.io`
links in the README. Each takes the load's `requested` switch as its input
and is deliberately tiny — a user who outgrows one copies the generated
automation and edits it.

1. **Request on sensor threshold** — numeric sensor below (or above, for
   cooling-type loads) a threshold *for N minutes* (debounce input), then turn
   the requested switch on. The switch's own lifecycle provides the
   hysteresis: once requested, re-triggers are no-ops until the run completes.
2. **Request from a trigger entity** — a switch / input_boolean / button /
   NFC tag turns the requested switch on. Covers "dishwasher is loaded".

## Tests

- Unit (`tests/test_deferrable.py`): recurrence/requested defaults; on-demand
  load excluded from `to_loads` output when unrequested and included when
  requested; auto-disarm at target; request survives `reset_day`; cancel via
  `requested = False`.
- Integration: new select/switch entities created per load, restore behaviour,
  switch reflects auto-disarm after a simulated completed run.

## Explicitly out of scope (for now)

- `emhass_companion.request_run` service with per-request overrides — planned
  in [request_run_service.md](request_run_service.md).
- Appliance preset profiles (`kind: appliance`) prefill for the subentry flow.
- A thermal-model "hot water tank" flavour of the subentry (min tank
  temperature instead of comfort window, tank sensor as `start_temperature`).
  The threshold blueprint is the accessible version; the thermal model is the
  better one and should eventually be offered alongside it.
