# Load groups

A group expresses a relationship between two or more existing deferrable
loads that a single load's own settings cannot: a shared power budget (a
subpanel or fuse limit) or mutual exclusion (only one may run at a time) — an
EV charger and an immersion heater on the same circuit, for instance. Without
a group, every load is planned as if it had its own unconstrained circuit.

*Status: implemented.*

## Setup

Add the loads you want to relate first — a group only references existing
deferrable or thermal loads, it does not create any of its own. Then, under
**Settings → Devices & Services → EMHASS Companion → Add load group**:

1. **Name** the group.
2. Pick **Loads in this group** — at least two.
3. Turn on **Only one may run at a time** if the loads must never overlap, or
   leave it off and set a **Shared power budget** (W) — the combined power
   the group's loads may draw at once. One of the two is required: a group
   with neither would place no constraint on the solver at all.

A group has no device or entities of its own — it is not a load, only a
relationship between loads already visible under the integration's own
subentries. Reconfigure or remove it the same way as a load, from the
integration's subentry list.

## What EMHASS is told

Sent as `deferrable_load_groups`, one entry per group:

```json
{"names": ["deferrable0", "deferrable2"], "mutual_exclusion": false, "max_power": 3500}
```

`names` are resolved from the group's member loads by their position among
*every* configured load, the same `deferrable{k}` numbering every other
per-load setting uses — not just the loads that happen to be running or
enabled on a given request.

A load removed or renamed since the group was created is dropped from the
group (with a warning) rather than failing the request; a group left with
fewer than two valid members is dropped entirely, also with a warning. The
`deferrable_load_groups` key itself is only sent when at least one group
survives that filtering — no groups configured is a no-op, not an empty list
on every request.
