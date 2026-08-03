# Handing over control

The integration can command your battery and switch your deferrable loads.
It ships unable to do either, and that is deliberate: you almost certainly
arrive with working automations, and handing control to a newly configured
optimiser before watching it make sensible decisions is how a battery ends up
charging at the day's peak price.

While `switch.emhass_control_enabled` is off, the executor still runs on
every cycle and records exactly what it *would* have done — the action, the
power, the reasoning, and the precise service calls — on
`sensor.emhass_battery_action`. That is the migration path:

1. Configure everything, leave control **off**.
2. Watch `sensor.emhass_battery_action` alongside your existing automations
   for a few days. Its `steps` attribute is the literal list of calls it
   would have made.
3. When you agree with its judgement, turn the switch on and retire the
   automations.

**Battery control** is configured under **Configure → Inverter control**, by
choosing an inverter profile — see [Inverter control](inverter_control.md)
for what ships today and the scripts fallback for any inverter that isn't
listed yet. This integration never talks to hardware directly — it only ever
calls entities and services your own inverter integration already provides.
Leave the profile unset and the battery is never touched.

**Deferrable loads** are switched only if you set a control entity on the
load. Leave it empty and the load stays advisory:
`binary_sensor.<load>_should_run` still tracks the plan, and acting on it
remains your automation's job.

Safety behaviours, each covered by a test:

- **Staleness watchdog.** If a plan stops being refreshed for more than
  twice the recalculation interval, the executor stops following it and
  falls back to self-consumption. A stale plan describes a world that no
  longer exists.
- **Deadband.** An unchanged command is not reissued, so a slow bus is not
  hammered every cycle. The deadband never suppresses a *change of action* —
  a switch from charging to discharging is always sent — and never suppresses
  a command that is about to expire on the inverter's own clock; see a
  profile's `control.lifetime` in
  [docs/inverter_profile_template.yaml](inverter_profile_template.yaml).
- **Manual override.** `select.emhass_mode` set to anything but *Automatic*
  suspends the optimiser entirely rather than competing with it.
- **Handover on shutdown.** A profile that needs its control handed back
  (`control.restore_required`, the default) gets its `restore` action run
  when the control switch is turned off, on integration unload, and on Home
  Assistant shutdown — not only when a plan goes stale. This is what stops a
  persistent forced-charge command from outliving a restart.

A failed service call is recorded and retried on the next cycle rather than
being marked as applied — otherwise the deadband would suppress the retry.
