# Inverter profile roadmap

*For contributors, and for anyone checking whether their inverter is on the
way. To configure battery control with what ships today, see
[Inverter control](inverter_control.md).*

**Status:** the framework is built. The profile catalogue is meant to grow by
contribution, one inverter at a time, using
**[inverter_profile_template.yaml](inverter_profile_template.yaml)** — a single
self-contained file to copy. The goal was to make contributing an inverter
cheap, not to ship every inverter up front.

Built (schema `version: 2`, all tested):

- `control:` block — archetype, power unit, sign, lifetime, deadband, write floor
- engine-side unit conversion (`w` / `kw` / `percent_of_rated` / `percent_hundredths`)
  plus `charge_boost`, `invert_sign` and `round_to`
- lifetime-driven re-issue, replacing the single global deadband
- `prepare` once per session, `restore` on shutdown / unload / control-off
- `suggest:` entity pre-fill in the setup form
- `p_pv_curtailment` on `PlanRow`; `curtail` / `allow_grid_charge` action names

Shipped built-in inverter profiles:

- **Sungrow SH-RT** — the first profile built against the template, for the
  mkaiser Modbus package.
- **Scripts (works with any inverter)** — the fallback for anything without a
  dedicated profile yet, `version: 1`, given the pre-`control` defaults.

The older *mode select plus a power number* profile was removed once Sungrow —
the inverter it existed to approximate — got a real, correctly-shaped profile
of its own. It could not express Sungrow's actual control model (one select to
arm forced mode, a second to choose direction) in the first place; see finding
1 below. There is no longer a middle tier between "a profile for your exact
hardware" and "write a script" — a generic single-select profile turned out to
be a worse fit for real hardware than either end of that spectrum.

## The goal

All inverters should eventually be listed by name. Until a given one is, the
script profile is the answer — never "approximate it with a generic profile
that doesn't quite match your hardware's actual shape."

The user should choose **their inverter**, and everything else should follow.

---

## What the research changed

Five brand surveys (Huawei, Sungrow, SolarEdge, Fronius, GoodWe, Deye/Sunsynk,
Growatt, SolaX, Fox ESS, Sigenergy, Ferroamp, Tesla, sonnen, Victron) plus a
survey of *how those brands are exposed in Home Assistant* produced six findings
that the current design cannot express.

### 1. Brand is the wrong key

Growatt alone spans three incompatible control schemes by inverter family: TOU
slots (SPH GEN3), TOU periods gated on a grid-charge enable (MOD GEN4), and a
signed ±100 % setpoint with a watchdog (WIT only). Deye, Sunsynk and Sol-Ark are
the same silicon under three badges. `wills106/homeassistant-solax-modbus` is
*one* HACS repo covering SolaX, Growatt, Sofar, Solis, Alpha ESS, Sunsynk/Deye
and more — with **per-plugin entity naming**, so one adapter for that repo is
impossible.

> A profile is keyed on **(how it is installed, model family)**, and *carries* a
> brand label for display. Brand is a grouping in the picker, never an identity.

### 2. There are five control archetypes, not one

| Archetype | Command shape | Examples |
|---|---|---|
| `signed_power` | one signed number, magnitude *and* direction | SolaX GEN4/5/6 (`remotecontrol_active_power`, ±30000 W), Sigenergy (`active_power_fixed_adjustment`, ±100 **kW**) |
| `mode_and_magnitude` | direction from a select, magnitude from a number | Sungrow, SolarEdge, GoodWe, Fox ESS |
| `command_with_duration` | a service call carrying power **and** a lifetime | Huawei (`forcible_charge`), Growatt WIT |
| `grid_setpoint` | the write is a *grid* target, not a battery one | Victron ESS `AcPowerSetPoint` |
| `tou_rewrite` | rewrite a time-of-use slot, no session concept | Deye/Sunsynk (Prog1–6), Growatt SPH/MOD |

### 3. Command lifetime is a property of the inverter, and the executor gets it wrong

- **Huawei** forced charge **auto-reverts** after its `duration` — it must be
  re-issued every slot. [`executor.py`](https://github.com/smefa/emhass-ha-companion/blob/main/custom_components/emhass_companion/executor.py)
  `_unchanged()` would suppress exactly that re-issue, and the battery silently
  falls back mid-plan.
- **Sungrow, GoodWe, Deye, Sigenergy** registers **persist forever**. Here the
  deadband is correct — but nothing must ever leave them stuck in forced mode.
- **SolaX and Fox ESS** have a *watchdog reload*: a countdown that must be
  refreshed or the inverter reverts on its own.
- **Victron** reverts to internal ESS control unless the setpoint is re-sent.

One global `DEFAULT_POWER_DEADBAND_W` cannot serve all four.

### 4. Not everything is watts

- **GoodWe** `eco_mode_power` is **% of rated inverter power**.
- **Fronius** SunSpec `InWRte`/`OutWRte` are **% in hundredths** (0–10000).
- **Growatt** (most families) is % of rated.
- **Sigenergy** is **kW**; **Fox ESS** is kW.
- **Victron** needs a negative value encoded as unsigned 16-bit two's complement.

Every profile author hand-rolling `{{ (power_w / rated * 10000) | round }}` is
how a 100× error reaches somebody's inverter.

### 5. Forced control usually has a prerequisite, and grid charging a *separate* one

Nearly every brand gates control behind a mode that must be set first:
SolarEdge `storage_control_mode = Remote Control`, Victron ESS *External
control*, Sigenergy `remote_ems_enable`, SolaX `remotecontrol_power_control`.

Independently, **grid charging is usually a separate permission**: Growatt
`allow_grid_charge` (reg 3049), Deye `grid_charge_enabled` (reg 232), SolarEdge
`ac_charge_policy`, Huawei `storage_power_of_charge_from_grid`, SolaX
`remotecontrol_import_limit`. EMHASS knows nothing about it. If it is off, EMHASS
plans a cheap-hours grid charge, the inverter quietly refuses, and nothing in the
current executor notices.

### 6. Auto-detection is not worth building — the user knows what they own

`detect: {integration: ...}` is the only mechanism today, and it cannot be made
to work for inverters:

- **GoodWe core and `mletenay/home-assistant-goodwe-inverter` share the domain
  `goodwe`** — core was merged *from* that repo. The domain cannot distinguish
  them.
- **The Sungrow mkaiser package has no domain at all** — it is a core `modbus:`
  YAML package, and it is the most popular Sungrow route.
- **ESPHome and MQTT setups** (Solar Assistant is very common) have no
  brand-specific domain either.

Rather than build entity-shape fingerprinting to work around this, **inverter
profiles do not auto-detect at all.** The list is a plain list; the user picks
their own hardware, which they know and we can only guess at. Source profiles
(price, PV, load) keep `detect:` — there the question is "is Solcast installed",
which the domain genuinely answers.

> **Detection moves down a level: we do not guess the *model*, but once told the
> model we pre-fill the *entities*.** That is the `suggest:` mechanism in
> `roles:`, and it is safe precisely because the user has already told us what
> the hardware is.

What survives from this finding is a **labelling** requirement, not a detection
one. Several core integrations expose good battery sensors and **cannot write a
setpoint at all**: `solaredge` (cloud), `fronius`, `growatt_server`, `powerwall`,
`enphase_envoy`, `solax`, `victron_ble`. A user picking "SolarEdge" and finding
control impossible is the worst outcome available, so each profile name states
the route it needs — *"SolarEdge Home Hybrid (needs solaredge-modbus-multi)"* —
and the picker carries a one-line note when the required integration is a
different one from the obvious core integration of the same name.

---

## The design

### Selection: one question, then a confirmation

**Step 1 — "Which inverter do you have?"**

A single searchable list of model profiles, grouped by brand, alphabetical. No
detection, no reordering, no cleverness — the user knows what is bolted to their
wall:

```
Deye / Sunsynk hybrid            (needs kellerza/sunsynk or ha-solarman)      [not yet shipped]
Fox ESS H1 / H3                  (needs nathanmarlor/foxess_modbus)           [not yet shipped]
GoodWe ET / EH                   (needs the goodwe integration)               [not yet shipped]
Huawei SUN2000 + LUNA2000        (needs wlcrs/huawei_solar)                   [not yet shipped]
Sigenergy Sigenstor              (needs Sigenergy-Local-Modbus)               [not yet shipped]
SolaX GEN4 / GEN5 hybrid         (needs solax_modbus — not core solax)        [not yet shipped]
SolarEdge Home Hybrid            (needs solaredge-modbus-multi — not core solaredge)  [not yet shipped]
Sungrow SH-RT                    (needs the mkaiser Modbus package)           ✓ shipped
─────
My inverter isn't listed
  Custom — call my scripts
```

The parenthetical is part of the profile and always shown: it is the only defence
against someone picking their brand when the integration they have installed is
the read-only cloud one. The goal is every row above eventually shipped; until
then, the script profile is the honest answer, not a generic approximation. The
older *mode select plus a power number* profile that used to sit at the bottom
was removed — see the note at the top of this document for why.

**Step 2 — "Confirm the entities."**

The profile declares *roles*; the flow pre-fills each from the profile's
`suggest:` patterns matched against entities that actually exist. Normally the
user reads a filled-in form and presses Next. This is the "select its sensors"
step — but as *verification*, not data entry.

This is where the guessing lives, and it is safe here in a way model detection
never was: the user has already said what the hardware is, so a wrong suggestion
is a visibly wrong entity id in a form field, not a silently wrong control model.
A role whose `suggest:` finds nothing is simply left blank for the user to fill.

**Step 3 — "Use this model's defaults?"**

A model profile carries `limits:` (capacity, max charge/discharge, rated AC
power). Accepting them fills in the Battery and hybrid-inverter settings that are
typed by hand today ([`models.py`](https://github.com/smefa/emhass-ha-companion/blob/main/custom_components/emhass_companion/models.py)
`BatteryConfig`, `HybridInverterConfig`), including EMHASS's `inverter_is_hybrid`.

### Schema — `version: 2` inverter profiles

The engine gains **no brand knowledge**. It gains a vocabulary for describing
*command semantics*, so the executor can decide **when** to write. **What** to
write stays in `actions:`, exactly as now.

```yaml
name: Sungrow SH-RT hybrid (Modbus package)
kind: inverter
version: 2

brand: Sungrow                    # display grouping only
models: [SH5.0RT, SH6.0RT, SH8.0RT, SH10RT, SH15T, SH20T, SH25T]
requires: Sungrow-SHx Modbus package   # shown in the picker, always
docs: https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant

# No `detect:` block. Inverter profiles are always offered; the user picks.

control:
  archetype: mode_and_magnitude
  power_unit: w                   # w | kw | percent_of_rated | percent_hundredths
  lifetime: persistent            # persistent | expires | watchdog | until_soc
  deadband_w: 100
  min_write_interval: 1s          # never hammer a Modbus bus
  restore_required: true          # persistent registers MUST be handed back

capabilities:
  - force_charge
  - force_discharge
  - idle
  - self_consume
  - curtail
  - grid_charge_gate

roles:                            # what step 2 asks about
  mode_select:
    name: EMS mode
    suggest: [select.ems_mode]
    selector: {entity: {domain: select}}
  direction_select:
    name: Forced charge/discharge
    suggest: [select.battery_forced_charge_discharge]
    selector: {entity: {domain: select}}
  power_number:
    suggest: [number.battery_forced_charge_discharge_power]
    selector: {entity: {domain: number}}

sensors:                          # finally wired up — see "loose ends"
  battery_soc: sensor.battery_level
  battery_power: sensor.battery_power
  grid_power: sensor.total_active_power
  pv_power: sensor.total_dc_power
  load_power: sensor.load_power

limits:
  battery_capacity_wh: "{{ states('sensor.battery_capacity_high_precision') | float * 1000 }}"
  charge_power_max_w: "{{ states('number.battery_max_charge_power') | float }}"
  discharge_power_max_w: "{{ states('number.battery_max_discharge_power') | float }}"

actions:
  prepare: []                     # nothing needed before forcing on Sungrow
  force_charge:
    - service: number.set_value
      target: {entity_id: "{{ roles.power_number }}"}
      data: {value: "{{ power }}"}       # already unit-converted by the engine
    - service: select.select_option
      target: {entity_id: "{{ roles.mode_select }}"}
      data: {option: Forced mode}
    - service: select.select_option
      target: {entity_id: "{{ roles.direction_select }}"}
      data: {option: Forced charge}
  force_discharge: …
  idle: …
  self_consume: …
  restore: *self_consume           # what to write when handing control back
  curtail:
    - service: switch.turn_on
      target: {entity_id: "{{ roles.export_limit_switch }}"}
    - service: number.set_value
      target: {entity_id: "{{ roles.export_limit_number }}"}
      data: {value: "{{ curtail_w }}"}
```

Two deliberate constraints carried over from
[profiles.md](profiles.md): a profile is **data, never code**, and anything the
vocabulary cannot express falls back to a `script` action rather than a new
keyword.

### Unit conversion belongs in the engine

`control.power_unit` plus a `rated_power_w` role means the engine hands each
action a ready-to-write `{{ power }}`:

| `power_unit` | Engine renders |
|---|---|
| `w` | `1500` |
| `kw` | `1.5` |
| `percent_of_rated` | `15` (of a 10 kW unit) |
| `percent_hundredths` | `1500` |

`{{ power_w }}` stays available as the raw signed watts for anything unusual.

### Lifetime replaces the global deadband

The executor re-issues when **any** holds:

1. the action changed (never suppressed — already true today), or
2. `|Δpower| > control.deadband_w`, or
3. `lifetime != persistent` **and** the command is closer than half its lifetime
   to expiring.

For `lifetime: expires`, actions additionally receive `{{ duration_min }}`, sized
to cover the slot plus refresh margin — which is what makes Huawei's
`forcible_charge` correct rather than a battery that quietly stops after 60 min.

### Restore is a first-class action, and runs on shutdown

`restore_required: true` brands (Sungrow, GoodWe, Deye, Sigenergy — every
persistent-register system) must never be left in forced mode. The `restore`
action runs on:

- plan going stale (already implemented),
- `switch.emhass_control_enabled` being turned **off** (not implemented),
- config entry unload / reload (not implemented),
- `EVENT_HOMEASSISTANT_STOP` (not implemented).

The last three are the gap that leaves a battery force-charging at peak price
because somebody restarted Home Assistant at the wrong moment.

### Preconditions and the grid-charge gate

- `actions.prepare` runs before the first forced command of a session — SolarEdge
  into `Remote Control`, Victron into *External control*, Sigenergy
  `remote_ems_enable`, SolaX `remotecontrol_power_control`.
- `capabilities: [grid_charge_gate]` declares that grid charging is separately
  permissioned. When the plan schedules charging while PV cannot cover it, the
  executor asserts the gate — and if the profile cannot, it raises a **repair
  issue** rather than letting EMHASS plan something the hardware will refuse.

### Honest degradation

`capabilities:` is also how a profile says what it *cannot* do. Tesla Powerwall
has no watt setpoint at all — only operation mode and `backup_reserve` %. A
profile declaring no `force_charge` capability makes the config flow say so at
setup, and makes the executor record the fidelity loss in
`sensor.emhass_battery_action`'s reason, instead of silently pretending the plan
was followed.

---

## Loose ends this closes

Found while reading the current implementation:

| Gap | Location |
|---|---|
| `sensors:` and `limits:` are in the schema but have **no non-test callers** — `resolve_sensors` / `resolve_limits` are dead | `profiles/engine.py` |
| `p_pv_curtailment` is absent from `PlanRow`, so curtailment cannot be acted on at all | `models.py` |
| `p_grid` exists on `PlanRow` but is never passed to an action — a `grid_setpoint` profile is impossible | `executor.py` |
| Action templates receive only `power_w`, `soc`, `soc_target` — no duration, no slot length, no grid target | `executor.py` |
| `test_profile` on an inverter profile falls through to `produces_series == False` and returns a misleading "contributes EMHASS settings" note | `services.py` |
| Nothing restores inverter state on unload, shutdown, or control-switch-off | `executor.py`, `__init__.py` |

---

## Build order

1. **Schema `version: 2`** — `brand`, `models`, `requires`, `control`,
   `capabilities`, `roles`, `restore`. `version: 1` profiles keep loading.
2. **Engine** — unit conversion, `roles` (with `suggest:` prefill) replacing bare
   `options` for entity mapping, `duration_min` / `curtail_w` / `p_grid` in the
   action scope.
3. **Executor** — lifetime-driven refresh, `prepare`, `restore` on all four
   triggers, capability-aware degradation, grid-charge gate assertion.
4. **Config flow** — the three-step selection above, with prefill.
5. **Plan plumbing** — `p_pv_curtailment` on `PlanRow`, `curtail` action.
6. **Profile catalogue** — the eight below.
7. **Contribution pipeline** — `test_profile` dry-runs an inverter action;
   a `dump_inverter_entities` diagnostic service produces a fixture from a
   contributor's own install.

## Catalogue

Scope is **current-generation DC-coupled hybrids from the major brands**.
Rebadges (Sol-Ark, Ginlong), legacy generations that lack a remote-control
register (SolaX GEN2/GEN3), and off-grid-only lines (Growatt SPF) are out —
they are served by the script escape hatch, not by a profile.

**Phase 1 — seven profiles, one shipped:**

| Profile | Route | Archetype | Lifetime | Confidence |
|---|---|---|---|---|
| Sungrow SH-RT | mkaiser Modbus package | `mode_and_magnitude` | persistent | **shipped** — validated against a live install |
| Huawei SUN2000 + LUNA2000 | `wlcrs/huawei_solar` | `command_with_duration` | expires | high |
| SolaX GEN4 / GEN5 hybrid | `solax_modbus` | `signed_power` | watchdog | high |
| Fox ESS H1 / H3 | `nathanmarlor/foxess_modbus` | `mode_and_magnitude` | watchdog | high |
| SolarEdge Home Hybrid | `solaredge_modbus_multi` | `mode_and_magnitude` | watchdog (writable timeout) | high |
| GoodWe ET / EH | `goodwe` | `mode_and_magnitude` (% of rated) | persistent | medium — `eco_mode_power` is a percentage, easiest unit trap in the set |
| Sigenergy Sigenstor | `TypQxQ/Sigenergy-Local-Modbus` | `signed_power` (kW) | persistent | medium — sign convention unconfirmed |

These seven need only **three** archetypes: `signed_power`,
`mode_and_magnitude`, `command_with_duration`.

**Phase 2 — after the first seven are proven:**

| Profile | Route | Archetype | Why deferred |
|---|---|---|---|
| Deye / Sunsynk hybrid | `sunsynk` or `solarman` | `tou_rewrite` | Sole owner of an entire archetype with no session concept — rewriting a TOU slot every 30 min is a different lifecycle from everything else, and the register map varies by phase and model |
| Ferroamp EnergyHub | `henricm/ha-ferroamp` | service charge/discharge | Swedish and worth having, but the sign convention is undocumented and needs a live install |

**Not planned.** Fronius GEN24 (core integration is read-only, control only via a
fork using an authenticated web API, revert timing unreliable); Growatt (three
incompatible schemes across SPH/MOD/WIT — needs per-family profiles for a brand
whose modern hybrid share is small here); Tesla Powerwall, sonnen, Victron
(AC-coupled, and Powerwall has no watt setpoint at all).

> **Recommendation:** deferring `tou_rewrite` to phase 2 removes a whole
> archetype from the executor and is the single biggest complexity saving
> available. The cost is that Deye/Sunsynk owners use the script escape hatch
> until then.

## Verification status

Verified from source or official docs: entity ids, option strings and register
semantics for Sungrow, Huawei, SolarEdge, SolaX, Fox ESS, Sigenergy, GoodWe,
Deye; EMHASS's own sign convention (`p_batt_forecast` negative = charging).

**Unverified, needs a real install before shipping the profile:** Sigenergy sign
convention; Sungrow register scaling on non-SH10RT models (reported to differ
between W, kW×100 and %); Ferroamp charge/discharge sign (phase 2).

Of the seven phase-1 profiles, only **Sungrow** can be validated on this system.
The other six need either a contributor with the hardware or an explicit
"untested — report back" marking in the picker. Shipping an unvalidated profile
that writes to a battery is worse than shipping no profile: the script escape
hatch at least fails visibly.
