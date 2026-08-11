# Fronius GEN24 — the scripts recipe

There is deliberately no Fronius profile. Every packaged route to a GEN24 has a
way to accept a command, report success, and change nothing — see
[Not planned: Fronius GEN24](inverter_profile_roadmap.md#not-planned-fronius-gen24)
for the survey behind that decision.

This page is the alternative: the [Scripts route](inverter_control.md#scripts-the-fallback-for-any-inverter)
wired up for a GEN24 specifically, built around the one thing a profile could
not give you here — **every write is read back, and a write that did not stick
fails loudly** instead of leaving the plan and the hardware quietly disagreeing.

!!! warning "Written from source, not from hardware"

    The register addresses and semantics below were read out of working
    implementations (see [Provenance](#provenance)) and cross-checked against
    the SunSpec model 124 layout. Nobody has run *this recipe* against a GEN24.
    Work through [Before you trust it](#before-you-trust-it) with
    `switch.emhass_companion_control_enabled` still **off**.

## Prerequisites

In the inverter's web UI (`http://<inverter-ip>`), under **Communication →
Modbus**:

| Setting | Required value | Why |
|---|---|---|
| Modbus TCP | **on**, port 502 | The transport |
| SunSpec Model Type | **int + SF** | The addresses below are the int+SF map. The float map puts these registers somewhere else entirely, and you will read plausible nonsense |
| Inverter control via Modbus | **on** | Without it the writes are accepted and ignored — the first silent-failure mode |
| Restrict control to IP | your HA host, or off | If set to something else, same outcome |

Then under **Components → Battery** (naming varies by firmware): if you want
the plan to ever charge from the grid, **allow battery charging from the grid**
must be on here *as well*. The Modbus register alone does not unlock it — the
two are AND-ed.

!!! note "One Modbus client at a time"

    A GEN24 accepts a small number of simultaneous Modbus TCP connections. If
    you already run `callifo/fronius_modbus`, point this recipe at the same
    inverter only after checking both stay connected — or drop that integration
    and read what you need from the sensors below.

## The registers

All addresses are the raw wire addresses HA's `modbus:` integration expects —
no ±1 adjustment. Unit (device) address is **1**.

| Address | Name | Type | Meaning |
|---|---|---|---|
| 40345 | `WChaMax` | uint16 | Max charge power, W. The denominator for every rate below |
| 40348 | `StorCtl_Mod` | uint16 | Bitfield: `1` = charge rate active, `2` = discharge rate active, `3` = both, `0` = Auto (rates ignored) |
| 40350 | `MinRsvPct` | uint16 | Minimum reserve, % ×100 |
| 40351 | `ChaState` | uint16 | SOC, % ×100 |
| 40355 | `OutWRte` | **int16** | Discharge rate, % of `WChaMax` ×100. **Negative = forced charge** |
| 40356 | `InWRte` | **int16** | Charge rate, % of `WChaMax` ×100 |
| 40358 | `InOutWRte_RvrtTms` | uint16 | Seconds until the inverter reverts the rates on its own. `0` = never |
| 40360 | `ChaGriSet` | uint16 | `0` = no grid charging, `1` = grid charging permitted |

40345, 40348, 40350, 40355, 40356 and 40360 are confirmed against a working
implementation. **40358 is derived** from the model 124 layout — it sits
between two confirmed anchors, and no implementation surveyed decodes it, which
is precisely why it is worth writing. Read it back before relying on it.

Two conventions worth stating out loud, because both are easy to get wrong:

- **The rates are percentages of `WChaMax`, in hundredths.** 3000 W on a 10 kW
  `WChaMax` is `3000 / 10000 × 10000` = `3000`. Not watts.
- **Forced charge is a *negative* `OutWRte`**, not a positive `InWRte`. A
  positive `InWRte` is a *ceiling* on charging, not a command to charge. This
  sign trick is undocumented by Fronius and cross-confirmed by three
  independent implementations.

## Step 1 — the read-back sensors

These are not decoration. They are how a write gets verified.

```yaml
# configuration.yaml
modbus:
  - name: fronius
    type: tcp
    host: 192.168.1.50      # your inverter
    port: 502
    delay: 5
    timeout: 5
    sensors:
      - name: Fronius WChaMax
        device_address: 1
        address: 40345
        data_type: uint16
        unit_of_measurement: W
        scan_interval: 300
      - name: Fronius StorCtl Mod
        device_address: 1
        address: 40348
        data_type: uint16
        scan_interval: 5
      - name: Fronius OutWRte
        device_address: 1
        address: 40355
        data_type: int16
        scan_interval: 5
      - name: Fronius InWRte
        device_address: 1
        address: 40356
        data_type: int16
        scan_interval: 5
      - name: Fronius RvrtTms
        device_address: 1
        address: 40358
        data_type: uint16
        scan_interval: 30
      - name: Fronius ChaGriSet
        device_address: 1
        address: 40360
        data_type: uint16
        scan_interval: 30
```

`data_type: int16` on 40355 and 40356 is load-bearing — read as `uint16`, a
forced charge of −30 % comes back as 62536 and every check below fails.

## Step 2 — write-and-verify

One helper script, used by all four actions:

```yaml
# scripts.yaml
fronius_write_verify:
  alias: "Fronius — write a register and verify it stuck"
  mode: queued
  max: 10
  fields:
    address: {selector: {number: {min: 0, max: 65535}}}
    value: {selector: {number: {min: 0, max: 65535}}}
    expected: {selector: {number: {min: -32768, max: 65535}}}
    readback: {selector: {entity: {}}}
  sequence:
    - action: modbus.write_register
      data:
        hub: fronius
        slave: 1
        address: "{{ address | int }}"
        value: "{{ value | int }}"
    - wait_template: >-
        {{ states(readback) | int(-99999) == expected | int }}
      timeout: "00:00:20"
      continue_on_timeout: true
    - if:
        - condition: template
          value_template: "{{ not wait.completed }}"
      then:
        - action: persistent_notification.create
          data:
            title: Fronius write did not stick
            message: >-
              Wrote {{ value }} to register {{ address }}; {{ readback }} still
              reads {{ states(readback) }} after 20 s, expected {{ expected }}.
        - stop: "Fronius register {{ address }} did not accept the write"
          error: true
```

Two things about this that matter:

- `modbus.write_register` validates its `value` as a **positive** integer, so a
  negative `OutWRte` has to be sent as its two's complement (`65536 - rate`).
  `expected` is separate from `value` for exactly this reason: you send 62536
  and read back −3000.
- Calling it as `action: script.fronius_write_verify` **waits** for it to
  finish, so a failed verification aborts the caller. `script.turn_on` would
  not, and the sequence would carry on writing into the dark.

## Step 3 — the four actions

```yaml
# scripts.yaml
emhass_fronius_charge:
  alias: "EMHASS — Fronius force charge"
  mode: queued
  max: 3
  sequence:
    - variables:
        wmax: "{{ states('sensor.fronius_wchamax') | float(0) }}"
        rate: >-
          {% set w = power_w | float(0) %}
          {% if wmax > 0 %}
            {{ [[ (w / wmax * 10000) | round(0) | int, 10000 ] | min, 0 ] | max }}
          {% else %}0{% endif %}
    - condition: template
      value_template: "{{ wmax > 0 }}"
    # The dead man's handle goes first: if the sequence dies after this point,
    # the inverter releases by itself instead of staying pinned.
    - action: script.fronius_write_verify
      data: {address: 40358, value: 900, expected: 900, readback: sensor.fronius_rvrttms}
    - action: script.fronius_write_verify
      data: {address: 40360, value: 1, expected: 1, readback: sensor.fronius_chagriset}
    - action: script.fronius_write_verify
      data:
        address: 40355
        value: "{{ (65536 - rate | int) % 65536 }}"
        expected: "{{ 0 - rate | int }}"
        readback: sensor.fronius_outwrte
    - action: script.fronius_write_verify
      data: {address: 40348, value: 2, expected: 2, readback: sensor.fronius_storctl_mod}

emhass_fronius_discharge:
  alias: "EMHASS — Fronius discharge"
  mode: queued
  max: 3
  sequence:
    - variables:
        wmax: "{{ states('sensor.fronius_wchamax') | float(0) }}"
        rate: >-
          {% set w = power_w | float(0) %}
          {% if wmax > 0 %}
            {{ [[ (w / wmax * 10000) | round(0) | int, 10000 ] | min, 0 ] | max }}
          {% else %}0{% endif %}
    - condition: template
      value_template: "{{ wmax > 0 }}"
    - action: script.fronius_write_verify
      data: {address: 40358, value: 900, expected: 900, readback: sensor.fronius_rvrttms}
    - action: script.fronius_write_verify
      data:
        address: 40355
        value: "{{ rate | int }}"
        expected: "{{ rate | int }}"
        readback: sensor.fronius_outwrte
    - action: script.fronius_write_verify
      data: {address: 40348, value: 2, expected: 2, readback: sensor.fronius_storctl_mod}

emhass_fronius_idle:
  alias: "EMHASS — Fronius idle"
  mode: queued
  max: 3
  sequence:
    - action: script.fronius_write_verify
      data: {address: 40358, value: 900, expected: 900, readback: sensor.fronius_rvrttms}
    - action: script.fronius_write_verify
      data: {address: 40355, value: 0, expected: 0, readback: sensor.fronius_outwrte}
    - action: script.fronius_write_verify
      data: {address: 40356, value: 0, expected: 0, readback: sensor.fronius_inwrte}
    - action: script.fronius_write_verify
      data: {address: 40348, value: 3, expected: 3, readback: sensor.fronius_storctl_mod}

emhass_fronius_self_consume:
  alias: "EMHASS — Fronius self-consumption"
  mode: queued
  max: 3
  sequence:
    # Release control first, then tidy up behind it.
    - action: script.fronius_write_verify
      data: {address: 40348, value: 0, expected: 0, readback: sensor.fronius_storctl_mod}
    - action: script.fronius_write_verify
      data: {address: 40355, value: 10000, expected: 10000, readback: sensor.fronius_outwrte}
    - action: script.fronius_write_verify
      data: {address: 40356, value: 10000, expected: 10000, readback: sensor.fronius_inwrte}
    - action: script.fronius_write_verify
      data: {address: 40360, value: 0, expected: 0, readback: sensor.fronius_chagriset}
```

`emhass_fronius_self_consume` is the one that earns the whole design. The
reported GEN24 desync — *inverter stays in forced recharge after Auto was
written* — is a failed write to 40348. Here that write is read back, and if the
inverter did not take it you get a notification and a failed script instead of
a battery quietly doing the opposite of the plan.

### Why 900 seconds

`InOutWRte_RvrtTms` is a dead man's handle. At 900 s the inverter releases
control by itself roughly 15 minutes after HA stops writing — a crashed
Companion, a failed add-on, a pulled network cable all end in Auto rather than
in a forced mode held forever. Set it comfortably longer than your optimisation
interval so a normal cycle always renews it before it fires; `0` disables the
revert entirely and is the wrong choice here, however tempting.

This is the register no surveyed implementation writes at all.

## Step 4 — wire it into the Companion

**Settings → Devices & Services → EMHASS Companion → Configure → Inverter
control → Scripts**, then point the four fields at
`script.emhass_fronius_self_consume`, `script.emhass_fronius_charge`,
`script.emhass_fronius_discharge` and `script.emhass_fronius_idle`.

Only self-consumption is mandatory — it is what the executor falls back to when
a plan goes stale. Each script is called with `power_w`, `soc` and `soc_target`
as variables; the ones above use `power_w`.

## Before you trust it

With `switch.emhass_companion_control_enabled` **off**, from Developer Tools →
Actions:

1. Call `script.emhass_fronius_self_consume`. It should complete cleanly. If it
   raises here, nothing below is worth trying — fix the prerequisites first.
2. Call `script.emhass_fronius_charge` with `power_w: 2000`. Watch the actual
   battery power. Two failure modes to separate: the script raising (the write
   was refused — good, that is the design working) versus the script succeeding
   while the battery does nothing (the register took the value but the inverter
   is ignoring it — check *Inverter control via Modbus* and the grid-charge
   permission).
3. Check what you actually got. There is a live report of firmware **capping
   grid charge at roughly 500 W** on a GEN24 paired with a BYD HVM, with
   external control correctly enabled. If you ask for 2000 W and get 500 W,
   that is hardware, not this recipe, and the plan will be wrong by the
   difference every time it schedules a charge.
4. Call `script.emhass_fronius_discharge` with `power_w: 2000` while the house
   is drawing less than that, and watch. See below.
5. Leave it in a forced mode and stop touching it for 20 minutes. It should
   return to Auto on its own — that verifies 40358 really is the revert timer.

Only then turn control on, and read
[Handing over control](handing_over_control.md) first.

## What this does not do

- **Discharge is a ceiling, not a target.** `StorCtl_Mod = 2` with a positive
  `OutWRte` caps how fast the battery *may* discharge; it does not force it to
  export. The battery will still only discharge to serve house load. If your
  plan expects forced discharge to grid, this recipe does not deliver it —
  Fronius exposes that as a separate extended control mode not reachable from
  these registers.
- **No curtailment.** PV export limiting lives in a different SunSpec model and
  is not wired up here.
- **No `WChaMax` management.** The recipe reads it as the denominator and never
  writes it.

## Provenance

- Register addresses, types and scale factors: read from
  [`callifo/fronius_modbus`](https://github.com/callifo/fronius_modbus)'s
  Modbus client at HEAD (2026-08-01), which reads 24 registers from 40345 —
  40358 falls inside that window and is never decoded.
- The mode/sign mapping, including forced charge as a negative `OutWRte`
  alongside `ChaGriSet = 1`:
  [evcc's `fronius-gen24.yaml`](https://github.com/evcc-io/evcc/blob/master/templates/definition/meter/fronius-gen24.yaml).
- The web-UI AND-gate on grid charging, and the ~500 W firmware cap: field
  reports collected in [the roadmap](inverter_profile_roadmap.md#not-planned-fronius-gen24).
- `modbus.write_register` argument validation and the `device_address` /
  `slave` split: Home Assistant core 2026.8.1.
