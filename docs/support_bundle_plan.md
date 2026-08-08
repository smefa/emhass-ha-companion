# Support bundle plan

Goal: when a stranger opens an issue, one click on their side should produce a
file that answers the questions we would otherwise spend five round-trips
asking. The download mechanism already exists — Home Assistant's *Download
diagnostics* on the integration page, backed by
`custom_components/emhass_companion/diagnostics.py`. This plan is about making
the bundle sufficient, and about making it cheap for a maintainer to read.

Nothing here adds a new download path. `async_get_config_entry_diagnostics` is
the feature; it is currently just too thin.

## What the bundle has today

Outputs, and only outputs: `entry.options`, loaded profile keys and errors, the
last run's status, plan row count, series lengths, the end-SOC decision, the
last payload, warnings, and the deferrable order.

That is a good dump for *"the plan looks wrong"* and useless for *"it does not
work at all"*, which is what a new install actually reports.

## The seven gaps

1. **Subentries are a count.** `"subentry_count": len(entry.subentries)` throws
   away every deferrable load, thermal load and load group — the most
   misconfigured part of the integration.
2. **No environment.** No integration version, Home Assistant version, Python
   version, or install method.
3. **No entity health.** Every entity id the config points at — `soc_entity`,
   `pv_live_entity`, `house_load_total_entity`, each load's `control_entity`
   and `sense` entity, plus entity ids inside profile options — is a string we
   never check. "Wrong entity id" and "kW where the code expects W" will be the
   two most common remote failures and both are invisible today.
4. **No backend facts.** Whether EMHASS was reachable at dump time, what
   version it reports, and what `GET /get-config` says its config is. A drift
   between what the Companion pushed and what the add-on holds is a classic
   silent failure.
5. **No custom profile source.** `profile_errors` names the failures without
   showing the YAML that caused them.
6. **No log tail.** The integration logs the reasoning behind most of its
   decisions at debug level and none of it survives into the bundle.
7. **No redaction.** The EMHASS config carries latitude/longitude/altitude and
   the URL can carry credentials. We are asking people to attach this to a
   public issue tracker, so the bundle has to be safe to attach and we have to
   be able to tell them exactly what it contains.

## Deliverables

### 1. Expand `diagnostics.py`

Keep every existing key. `scripts/check_infeasibility.py` and
`tests/test_check_infeasibility.py` read `last_payload` off the bundle;
renaming or nesting it is a breaking change to a script users already have on
disk. Add alongside, do not restructure.

New top-level sections:

**`environment`** — integration `version` from `manifest.json` (via
`homeassistant.loader.async_get_integration`, not by re-reading the file),
Home Assistant `__version__`, `sys.version`, `hass.config.time_zone`, the unit
system name, and whether `hacs` is in `hass.config.components`. No coordinates.

**`backend`** — the configured URL (scrubbed, see redaction), whether it came
from add-on discovery or was typed by hand, and a live probe performed *at dump
time*: `async_get_version()` and `async_get_config()`, each wrapped so a failure
records the error string instead of raising. A short timeout — diagnostics must
not hang for 30s because the add-on is down; that the add-on is down is the
finding. Include the returned EMHASS config verbatim, redacted.

**`subentries`** — a list of `{subentry_id, subentry_type, title, data}` for
every subentry. Entity ids stay in plain text; they are not secrets and they
are the whole point. Sort by type then title so two bundles diff cleanly.

**`entities`** — the health of every entity id referenced anywhere in the
config. Collect by walking `entry.data`, `entry.options` and each subentry's
`data` recursively and matching strings against HA's entity-id shape
(`homeassistant.helpers.config_validation` has the pattern; a local
`re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value)` is acceptable and keeps this
robust to profile options whose schema we do not know). For each id found,
record: `referenced_by` (the config path(s) that named it), `exists`,
`state`, `unit_of_measurement`, `device_class`, `state_class`,
`last_changed`, and whether the entity registry knows it and has it disabled.
A missing or `unavailable` entity is the single highest-value fact in the whole
bundle.

**`custom_profiles`** — for each YAML under the user profile directory
(`/config/emhass_companion/profiles/**`, resolved via `hass.config.path`, never
hard-coded): relative path, size, mtime, and the file contents. Read them off
the event loop with `hass.async_add_executor_job`. Cap any single file at a
sane size (say 64 KB) and note the truncation rather than silently cutting.

**`logs`** — the last ~200 records from the `custom_components.emhass_companion`
logger. This needs a small addition outside `diagnostics.py`: install a
`logging.Handler` backed by a `collections.deque(maxlen=200)` on that logger
during `async_setup_entry`, remove it in `async_unload_entry`, and hold it
somewhere the diagnostics function can reach (the runtime data is the natural
home). Format each record as `{ts, level, message}`. Do not capture below
`INFO` unless the user has debug logging on — read the logger's effective level
rather than forcing one.

**`triage`** — a short list of computed findings so a maintainer sees the
verdict before scrolling 4000 lines of payload. Same checks the script below
performs; sharing one implementation between the two is not worth the coupling,
since the script must stay dependency-free. Keep the in-bundle version to the
cheap facts: backend unreachable, referenced entity missing or unavailable,
no run has ever succeeded, plan stale, profile errors present.

### 2. Redaction

Use `homeassistant.components.diagnostics.async_redact_data`. Redact
`latitude`, `longitude`, `alt`, `altitude`, and any key matching
`password`/`token`/`api_key`/`secret`/`Authorization`. Scrub the EMHASS URL
through `yarl.URL` to drop userinfo and query string while keeping scheme,
host and port — "wrong port" is a real bug and we need to be able to see it.

Deliberately **not** redacted: entity ids, tariff prices, battery capacity,
load names, the payload. Redacting those would defeat the feature.

### 3. `scripts/inspect_bundle.py`

Standalone and dependency-free, mirroring `scripts/check_infeasibility.py`
exactly in style: module docstring explaining what it is and is not, `--` for
dashes, reads a path or `-` for stdin, prints a grouped report, exits non-zero
when it finds an error-level problem.

Checks, roughly in the order a maintainer would ask:

- Backend unreachable, or reporting a version the integration does not support.
- Referenced entity missing, `unavailable`, `unknown`, non-numeric where a
  number is required, or carrying a unit that is off by 1000 from what the
  field expects.
- No run has ever succeeded / last run failed / last run infeasible — and for
  the infeasible case, point at `check_infeasibility.py` rather than
  duplicating its analysis.
- Plan stale relative to the MPC interval, or zero rows.
- Forecast series that stop short of the horizon.
- Profile errors, with the offending custom profile YAML quoted if present.
- Subentry sanity: a deferrable load with zero power, a window narrower than
  its own run time, a `control_entity` that does not exist, a load group
  naming a subentry id that is gone.
- Nothing to optimise at all — no battery, no deferrable loads.

End with a compact summary block a maintainer can paste back into the issue.

### 4. Tests

- `tests/integration/test_diagnostics.py` — a loaded entry with one deferrable
  subentry, one thermal subentry and a deliberately missing entity id. Assert
  every new section exists, that the missing entity is reported as
  `exists: false`, that a latitude in the EMHASS config comes back redacted,
  and that `last_payload` is still at the top level where the old script
  expects it.
- `tests/test_inspect_bundle.py` — plain pytest, no `hass`, in the style of
  `tests/test_check_infeasibility.py`: synthetic bundles, one per check, plus a
  clean bundle that must produce no findings and exit zero.

Run with the clone's `.venv-test`.

### 5. Docs

- Rewrite the head of `docs/troubleshooting.md` so "download diagnostics and
  attach it" is step one of any report, ahead of the existing per-symptom
  sections, which stay.
- New section documenting what the bundle contains and what is redacted, so a
  user can decide to attach it to a public issue without having to read the
  JSON first. This is the part that makes the feature usable by people who are
  appropriately cautious.
- Document `inspect_bundle.py` next to the existing `check_infeasibility.py`
  reference.
- No new `mkdocs.yml` nav entry needed if this lives inside
  `troubleshooting.md`; add one only if it grows into its own page.

### 6. Version

Bump `manifest.json` to `0.9.1`. Work on `dev`. Do not touch `main`, do not
push, do not create a release.

## Out of scope

- A service returning the same dict as response data. The three-dot menu is the
  path a novice will actually manage; add the service later only if people ask.
- Any mechanism for a maintainer to *pull* a bundle remotely. There is none and
  there should not be one — the user downloads and sends it.
- Uploading, or anything that transmits the bundle anywhere on its own.
