# Request-run service (planned)

*Status: planned, not shipped. Depends on [on-demand loads](on_demand_loads.md),
which is already implemented — this adds parameterised requests on top of
the `requested` switch that exists today.*

## Why a service, when the switch already exists

`switch.<load>_requested` is a boolean: a run is wanted, or it isn't. A
service call is a verb that can carry arguments a switch can't — "2 hours
this time, not the usual 1", "finish by 06:30" — and is the natural surface
for scripts and voice assistants ("the dishwasher is loaded", "guests
tonight, extra hot water").

Calling the service with no extra data will be exactly equivalent to turning
the switch on. It's sugar over the same mechanism, not a second one.

## Planned shape

```yaml
action: emhass_companion.request_run
target:
  device_id: <the load's device>
data:
  hours: 2.0    # optional — override this request's hours needed
  within: 4.0   # optional — override this request's deadline, in hours
```

- Targets the load's device, like every other action on it.
- Both fields are optional; a bare call behaves exactly like flipping
  `switch.<load>_requested` on.
- Overrides are **one-shot**: they apply to this request only and revert
  once it ends (completion or manual cancel) — "extra hot water tonight"
  should not quietly become the new everyday default.
- A wall-clock deadline (`finish_by`) is deliberately not offered — it would
  be ambiguous across midnight and would collide with the load's own time
  window. `within` (a duration from the moment of the call) avoids both
  problems; see the deadline section in [on-demand loads](on_demand_loads.md).
- Calling it on a load whose recurrence is still *Every day* will be
  allowed, as a one-off "boost today".
- No separate cancel service — turning `switch.<load>_requested` off cancels
  the request and clears any overrides, same as it does today.

There's nothing to configure for this yet. If your use case needs
per-request overrides sooner than this ships, the workaround today is a
script that adjusts `number.<load>_run_within` before turning the requested
switch on.
