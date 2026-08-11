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
  mkaiser Modbus package. The only one validated against real hardware.
- **Sungrow SH-T 15–25 kW** *(untested)* — same package, same registers; a
  separate entry because of the 5000 W default power ceiling, the T-series
  export-limit scaling history and the SBH pack's different Modbus path.
- **Huawei SUN2000 M1 / MB0** *(untested)* — `wlcrs/huawei_solar`,
  `command_with_duration`, the first profile using `lifetime: expires`.
- **Deye SUN-5K–25K-SG01HP3-EU** *(untested)* — `davidrapan/ha-solarman`.
- **SolaX X3-Hybrid G4 / X3-Ultra** *(untested)* — `solax_modbus`, the first
  `signed_power` profile and the first using `invert_sign`.
- **Growatt MOD / MID TL3-XH** *(untested)* — `solax_modbus`, the first
  `tou_rewrite` profile and the first using `percent_of_rated`.
- **Sigenergy SigenStor** *(untested)* — `TypQxQ/Sigenergy-Local-Modbus`, the
  first profile to use `prepare`, and the first in kilowatts.
- **Scripts (works with any inverter)** — the fallback for anything without a
  dedicated profile yet, `version: 1`, given the pre-`control` defaults. Pinned
  to the bottom of the picker however many brands sit above it.

### Untested profiles are marked as such

The catalogue below used to say that shipping an unvalidated profile was worse
than shipping none, because the script escape hatch at least fails visibly.
That was the right instinct and the wrong conclusion: it left owners of the
most common inverters in Europe with nothing, in order to avoid a risk that a
label fixes.

So a profile now carries `untested: true` until somebody with the hardware
confirms it. That adds **"— UNTESTED"** to the picker label and a warning above
the setup form, saying what it means and asking for a report back. The
alternative — a profile that looks exactly as trustworthy as a validated one —
is what actually makes the first person to try it an unwitting tester of writes
to their own battery.

Two things make this a fair trade rather than a gamble. Control is off until
the user turns it on, and turning it back off hands the inverter back
immediately. And every profile below was written from the integration's own
source and the inverter's register map, with the parts that could not be
confirmed from source written into that profile's notes rather than guessed —
sign conventions, scaling and gating prerequisites especially.

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
Deye SUN-5K-25K-SG01HP3-EU       (needs davidrapan/ha-solarman)               — UNTESTED
Fox ESS H1 / H3                  (needs nathanmarlor/foxess_modbus)           [not yet shipped]
GoodWe ET / EH                   (needs the goodwe integration)               [not yet shipped]
Growatt MOD / MID TL3-XH         (needs solax_modbus — not growatt_server)    — UNTESTED
Huawei SUN2000 M1 / MB0          (needs wlcrs/huawei_solar)                   — UNTESTED
Sigenergy SigenStor              (needs Sigenergy-Local-Modbus)              — UNTESTED
SolaX X3-Hybrid G4 / X3-Ultra    (needs solax_modbus — not core solax)        — UNTESTED
SolarEdge Home Hybrid            (needs solaredge-modbus-multi — not core solaredge)  [not yet shipped]
Sungrow SH-RT                    (needs the mkaiser Modbus package)           ✓ shipped
Sungrow SH-T 15–25 kW            (needs the mkaiser Modbus package)           — UNTESTED
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

**Phase 1 — six of seven shipped, one validated:**

| Profile | Route | Archetype | Lifetime | Confidence |
|---|---|---|---|---|
| Sungrow SH-RT | mkaiser Modbus package | `mode_and_magnitude` | persistent | **shipped** — validated against a live install |
| Huawei SUN2000 M1 / MB0 | `wlcrs/huawei_solar` | `command_with_duration` | expires | **shipped, untested** — services and units confirmed from source; what the inverter does at duration expiry is firmware behaviour nobody has documented |
| SolaX X3-Hybrid G4 / X3-Ultra | `solax_modbus` | `signed_power` | watchdog | **shipped, untested** — one plugin serves both; entity ids may carry a "(mode 1)" suffix on a recent install |
| Fox ESS H1 / H3 | `nathanmarlor/foxess_modbus` | `mode_and_magnitude` | watchdog | high |
| SolarEdge Home Hybrid | `solaredge_modbus_multi` | `mode_and_magnitude` | watchdog (writable timeout) | high |
| GoodWe ET / EH | `goodwe` | `mode_and_magnitude` (% of rated) | persistent | medium — `eco_mode_power` is a percentage, easiest unit trap in the set |
| Sigenergy SigenStor | `TypQxQ/Sigenergy-Local-Modbus` | `mode_and_magnitude` (kW) | persistent | **shipped, untested** — deliberately *not* `signed_power`: the signed setpoint's sign is undocumented and it targets AC output rather than the battery, so direction comes from the mode select instead |

These seven need only **three** archetypes: `signed_power`,
`mode_and_magnitude`, `command_with_duration`.

**Phase 2:**

| Profile | Route | Archetype | Status |
|---|---|---|---|
| Deye SUN-5K–25K-SG01HP3-EU | `davidrapan/ha-solarman` | — | **shipped, untested.** Brought forward. The `tou_rewrite` lifecycle that made this the deferred case turned out to be avoidable: the profile steers work mode and the battery **current** limits, which act immediately, and never touches the user's six TOU programs. The cost is that the plan becomes a ceiling rather than a setpoint, and that the watts-to-amps conversion needs the pack voltage |
| Growatt MOD / MID TL3-XH | `solax_modbus` | `tou_rewrite` | **shipped, untested.** Genuinely TOU-based — there is no setpoint register on this family — so it dedicates one slot to the plan and moves that slot's priority mode. Percentages of rated power, so it needs the nameplate rating. No curtailment: two candidate export-limit registers exist, in different units, and which one the family honours is unresolved |
| Ferroamp EnergyHub | `henricm/ha-ferroamp` | service charge/discharge | Swedish and worth having, but the sign convention is undocumented and needs a live install |

### Not planned: Fronius GEN24

This one was investigated three times, because each answer looked like it was
about the wrong thing.

The core `fronius` integration is read-only — Solar API, no write platforms at
all. The obvious alternative, hand-written core `modbus:` YAML against SunSpec
model 124, is reported to *report success while the write fails* underneath on
this hardware, which is why every serious community project writes its own
Modbus client instead of using it.

That looked like an argument against the transport rather than against the
brand, so the second pass asked whether a profile driving a real custom
integration — `callifo/fronius_modbus`, the maintained fork of
`redpomodoro/fronius_modbus` — would sidestep it. A component that owns its
client *should* be able to manage the watchdog and the scaling internally. It
does not:

- **Control writes are silently dropped while the hub is polling.** Every
  control method is wrapped in a busy flag that returns without writing, and
  without raising or logging, if a poll is in flight. An automation calling
  `number.set_value` cannot tell the difference between a write that landed and
  one that was eaten. This is an open issue against the integration.
- **The revert watchdog is never touched.** `InOutWRte_RvrtTms` appears nowhere
  in the register table or in any write path — the integration inherits
  whatever timeout the inverter already had. There is a live report of the
  resulting desync, where the inverter stayed in forced recharge after Auto was
  written.
- **Firmware caps grid charging to roughly 500 W** on at least one current
  GEN24 build paired with a BYD HVM, with external control correctly enabled.
  No amount of software above it can route around that.
- Grid charging also depends on an undocumented sign trick (a negative
  `OutWRte`) AND-linked to a web-UI toggle that the integration only asserts
  when web-API credentials were configured, best-effort, in a try/except that
  logs a warning and carries on.

Any one of those is survivable with a warning. Together they describe a route
where a profile can look configured, look applied, and have changed nothing —
and that is the one failure mode the `untested:` marking does *not* cover. That
marking says "nobody has confirmed this yet", not "commands may silently
vanish".

A correction to an earlier draft of this section: the silent-drop behaviour was
described here as an open issue upstream. It is not — no such issue exists in
that tracker. The finding is first-hand from the source, which is stronger
evidence but has no corroboration from other users.

**The third pass** asked the obvious follow-up: is there a *better* component
to drive? Four routes were checked, at source rather than at README:

| Route | Verdict |
|---|---|
| `callifo/fronius_modbus` | Re-verified at HEAD (2026-08-01). `toggle_busy` still returns bare on collision, decorating ~15 control methods; `RvrtTms` still appears nowhere. New: open issue #121 — the integration **fails to load on HA 2026.7+** (`unpack_bitstring` removed from pymodbus) without a hand patch. Closed issue #97 is the tell: grid charge power was displayed in W while holding raw SunSpec percent, ≈205× off on a BYD HVM |
| `redpomodoro/fronius_modbus` | The sibling repo. Same `toggle_busy` pattern, last commit 2025-09-09. Strictly worse. The `smartenergycontrol-be` and `Bigert` forks are copies of callifo |
| `tz8/home-assistant-gen24-energy-control` | A genuinely different transport — local HTTP `/config/timeofuse`, no Modbus. Not usable as an executor: it is a **rival planner** (its own EPEX + Solcast logic; its only service is `apply_policy`, meaning *apply my plan*), it writes 00:00–23:59 `CHARGE_MAX`/`DISCHARGE_MAX` limits rather than a setpoint, it POSTs the whole TOU table — an unwritable decision posts `{"timeofuse": []}`, wiping the owner's entries — and its HTTP client carries **no authentication at all** |
| `evcc` | The most-deployed Fronius battery controller anywhere, and it confirms the ceiling rather than clearing it: four *modes* only, no watt setpoint, `maxchargerate` a static config percentage. It does not manage the revert timer either. Its value here is corroboration — the negative-`OutWRte` grid-charge trick and `ChaGriSet` semantics are now cross-confirmed |

Honourable mentions, neither an executor: `wiggal/GEN24_Ladesteuerung` (a
standalone forecast/price planner that writes charge power over HTTP — it would
fight EMHASS for the same battery) and `jtbnz/fronius-modbus-controller` (a
Python library, so it lands where a script lands, plus a dependency).

So the verdict stands, and the survey strengthens it: every serious project on
this hardware either drops to modes or owns the whole plan. Neither shape fits
a setpoint executor.

Fronius GEN24 owners get the script escape hatch, where a script can read back
what it wrote and fail visibly — written up concretely in
[Fronius GEN24 — the scripts recipe](fronius_gen24_scripts.md). The register
semantics themselves are well understood and cross-confirmed by three
independent implementations; it is only the delivery that has no home.

**If this is ever revisited**, the interesting finding is the web-API route.
`/config/timeofuse` is persistent configuration — no watchdog to desync
against — and it is readable back with a `GET`. What makes it a script recipe
rather than a profile is the authentication: the GEN24 uses a non-standard
digest handshake, challenged over `X-WWW-Authenticate` with a SHA-256 (or MD5,
per `authenticationOptions`) secret, which neither `rest_command` nor
`curl --digest` can speak. That needs a helper of ~60 lines, which is a
dependency the Companion should not grow. The Modbus-plus-read-back recipe
needs no authentication at all, which is why it is the one documented.

Also not planned: Tesla Powerwall, sonnen, Victron (AC-coupled, and Powerwall
has no watt setpoint at all).

> **Note on the archetypes.** Shipping Deye and Growatt did not require the
> executor to learn `tou_rewrite` semantics after all. Growatt's profile expresses
> the whole slot rewrite — mode, times, enable flag, then the commit button — as
> ordinary service calls, and Deye's sidesteps TOU entirely. `tou_rewrite`
> remains a label on the `control:` block describing what the hardware is, not a
> code path in the executor. That is the outcome the original recommendation was
> reaching for.

## Verification status

Verified from source or official docs: entity ids, option strings and register
semantics for Sungrow, Huawei, SolarEdge, SolaX, Fox ESS, Sigenergy, GoodWe,
Deye, Growatt; EMHASS's own sign convention (`p_batt_forecast` negative =
charging).

Only **Sungrow SH-RT** can be validated on this system. Everything else ships
marked `untested: true` and carries what could not be confirmed in its own
notes, where the person setting it up will actually read it. The specific gaps,
per profile:

| Profile | What is not confirmed |
|---|---|
| Sungrow SH-T | Register 13052 is Watts per Sungrow's V1.1.9 spec, which lists these models in the same table — but no field report from a T-series owner exists. The export-limit register has had a factor-of-ten bug on SH15T |
| Huawei | What the inverter does when a `forcible_charge` duration elapses; whether power 0 is a genuine hold (which is why the profile defines no `idle`); the exact battery sensor entity ids |
| Deye | The battery and grid power sign — the two integrations that read this hardware disagree, one negating what the other passes through. Whether the global grid-charge switch or a per-program bit takes precedence |
| SolaX | Whether a recent install's entity ids carry the "(mode 1)" suffix the current display names imply |
| Growatt | Whether Battery First at a zero charge rate is a true idle; which of the two export-limit registers this family honours |
| Sigenergy | Nothing load-bearing: the profile avoids the one undocumented fact (the signed setpoint's sign) rather than guessing it. Whether the per-inverter setpoint needs the plant-level enable is unconfirmed, but this profile uses the plant-level entities throughout |
| Ferroamp | Charge/discharge sign (not yet shipped) |
| Fronius GEN24 | Not shipped at all — see "Not planned" above. The register semantics are well confirmed; the delivery path is not. The [scripts recipe](fronius_gen24_scripts.md) documents the registers but has not been run against hardware, and `InOutWRte_RvrtTms` in particular is derived from the model 124 layout rather than read out of a working implementation |

The earlier conclusion here — that shipping an unvalidated profile is worse
than shipping none — has been replaced by the `untested:` marking described at
the top of this document.
