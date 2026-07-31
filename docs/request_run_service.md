# Plan: `emhass_companion.request_run` service

*Status: deferred. Depends on on-demand loads
([on_demand_loads.md](on_demand_loads.md)) shipping first. The requested
switch alone covers the trimmed scope; this service adds parameterised
requests on top.*

## Why a service when the switch exists

`switch.<load>_requested` is state — a boolean. A service call is a verb that
can carry arguments a switch physically can't: "this time, 2 hours instead of
1", "finish by 06:30". It is also the natural surface for scripts and voice
assistants ("the dishwasher is loaded", "guests tonight — extra hot water").

Calling the service with no overrides must be exactly equivalent to turning
the switch on. The service is sugar over the same registry flag, never a
second mechanism.

## Shape

```yaml
action: emhass_companion.request_run
target:
  device_id: <the load's device>
data:
  hours: 2.0          # optional — override operating_hours for this request
  finish_by: "06:30"  # optional — override latest_end for this request
  start_after: "22:00" # optional — override earliest_start for this request
```

- Target by device (each load already has one), matching how users think.
- All data fields optional; bare call == switch on.
- Overrides that are given force `use_time_window` semantics for this request
  even if the load normally has none.

## One-shot override semantics (the key decision)

Overrides are **one-shot, not sticky**: they apply to this request only and
revert when the request ends — either auto-disarm at completion or manual
cancel (switch off). "Extra hot water *tonight*" must not quietly become the
new default. Auto-disarm is the natural restore point and already exists in
the on-demand lifecycle.

Implementation: store overrides on `DeferrableRuntime` as separate fields
(`request_hours_override`, `request_finish_by`, …) rather than mutating the
entity-owned values, so:

- the `number.` / `time.` entities keep showing the user's configured
  defaults (no confusing UI flicker),
- clearing on disarm is just nulling the override fields,
- `to_load()` prefers the override when present.

A second `request_run` while one is pending *replaces* the overrides (last
call wins) — simplest to explain, and matches what "actually I need 3 hours"
means.

## Restart persistence

The requested flag survives restarts via the switch's `RestoreEntity`.
Overrides are not entities, so persist them as extra restore data on the
requested switch (`extra_restore_state_data`), keeping the whole request —
flag plus parameters — restored as one unit.

## Validation

- `hours` must quantise to a whole number of timesteps — reuse the same check
  `payload.py` applies to `operating_hours` (`operating_timesteps` +
  `math.isclose` guard) and raise `ServiceValidationError` at call time
  rather than failing later mid-optimisation.
- Target must resolve to a deferrable-load device of this integration;
  anything else is a `ServiceValidationError` naming the valid loads.
- Calling it on a `daily`-recurrence load: **allowed**, meaning "boost today"
  — the overrides apply until completion/midnight. This makes the service
  useful without forcing users to switch modes. (Revisit if it proves
  confusing; the conservative fallback is to reject with a hint to set
  recurrence to on_demand.)

## Cancellation

No `cancel_run` service — turning the requested switch off is the cancel, and
it also clears overrides. Add a service later only if voice-assistant cancel
turns out to matter.

## Tests to write

- Bare call ≡ switch on (registry state identical).
- Overrides applied in `to_load()`, entities unchanged.
- Overrides cleared on auto-disarm and on manual cancel.
- Second call replaces overrides.
- Quantisation and bad-target validation errors.
- Restore round-trip of flag + overrides.

## Open questions

- Should `finish_by` accept a datetime (not just time-of-day) for "by
  tomorrow 07:00" across midnight? Time-of-day plus "next occurrence" rule is
  probably enough, but needs a stated rule.
- Does "boost today" on a daily load interact sanely with
  `completed_timesteps` already accumulated today? (Likely yes — the override
  just raises the target — but verify against EMHASS's
  `def_current_operating_timesteps` handling.)
- Whether to also expose `nominal_power` as an override (probably not: it
  describes the appliance, not the request).
