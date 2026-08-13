"""Config and options flow.

The flow is split into short, single-topic steps. The complaint this
integration exists to answer is that EMHASS presents every option at once with
no indication of which apply to a given house; asking a handful of questions at
a time, and only ever offering data sources that are actually installed, is the
substance of that answer.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlowWithReload,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import EmhassClient, EmhassError
from .configuration import EmhassConfig
from .const import (
    CONF_ADDER,
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_INVERT,
    CONF_BATTERY_SOC_DEFICIT_COST,
    CONF_BATTERY_SOC_DEFICIT_THRESHOLD,
    CONF_BATTERY_SOC_SURPLUS_COST,
    CONF_BATTERY_SOC_SURPLUS_THRESHOLD,
    CONF_BATTERY_STRESS_COST,
    CONF_BATTERY_STRESS_SEGMENTS,
    CONF_CAPACITY_COST_PER_KW,
    CONF_COMFORT_END,
    CONF_COMFORT_START,
    CONF_COMFORT_TEMPERATURE,
    CONF_COMPUTE_CURTAILMENT,
    CONF_CONTROL_ENTITY,
    CONF_COOLING_CONSTANT,
    CONF_DAYAHEAD_FALLBACK_TIME,
    CONF_EARLIEST_START,
    CONF_EMHASS_STANDARD_NAMES,
    CONF_END_SOC_MODE,
    CONF_ENERGY_NEEDED,
    CONF_GRID_EXPORT_ENERGY_ENTITY,
    CONF_GRID_EXPORT_LIMIT_ENTITY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_GRID_IMPORT_LIMIT_ENTITY,
    CONF_GROUP_LOAD_IDS,
    CONF_GROUP_MAX_POWER,
    CONF_GROUP_MUTUAL_EXCLUSION,
    CONF_HEATING_RATE,
    CONF_HORIZON_HOURS,
    CONF_HOUSE_LOAD_TOTAL_ENTITY,
    CONF_HYBRID_INVERTER,
    CONF_INVERTER,
    CONF_INVERTER_AC_INPUT_MAX,
    CONF_INVERTER_AC_OUTPUT_MAX,
    CONF_INVERTER_EFFICIENCY_AC_DC,
    CONF_INVERTER_EFFICIENCY_DC_AC,
    CONF_LATEST_END,
    CONF_LOAD,
    CONF_MAX_STARTUPS,
    CONF_MAX_TEMPERATURE,
    CONF_METERING,
    CONF_METERING_ENABLED,
    CONF_MINIMUM_OFF_TIME,
    CONF_MINIMUM_ON_TIME,
    CONF_MINIMUM_POWER,
    CONF_MODE,
    CONF_MPC_INTERVAL,
    CONF_MULTIPLIER,
    CONF_NAME,
    CONF_NOMINAL_POWER,
    CONF_OPERATING_HOURS,
    CONF_POWER_SENSOR,
    CONF_PRICE,
    CONF_PROFILE,
    CONF_PROFILE_OPTIONS,
    CONF_PV,
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_ENTITY,
    CONF_RECURRENCE,
    CONF_SEMI_CONTINUOUS,
    CONF_SENSE,
    CONF_SETBACK_TEMPERATURE,
    CONF_SINGLE_CONSTANT,
    CONF_SOC_ENTITY,
    CONF_STARTUP_PENALTY,
    CONF_SURPLUS_HEADROOM,
    CONF_SURPLUS_PRIORITY,
    CONF_TEMPERATURE,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPLATE,
    CONF_THERMAL_INERTIA,
    CONF_TIME_STEP,
    CONF_URL,
    CONF_WEIGHT_BATTERY_CHARGE,
    CONF_WEIGHT_BATTERY_DISCHARGE,
    CONTROL_ENTITY_DOMAINS,
    DEFAULT_BATTERY_SOC_DEFICIT_COST,
    DEFAULT_BATTERY_SOC_DEFICIT_THRESHOLD,
    DEFAULT_BATTERY_SOC_SURPLUS_COST,
    DEFAULT_BATTERY_SOC_SURPLUS_THRESHOLD,
    DEFAULT_BATTERY_STRESS_COST,
    DEFAULT_BATTERY_STRESS_SEGMENTS,
    DEFAULT_CAPACITY_COST_PER_KW,
    DEFAULT_COMPUTE_CURTAILMENT,
    DEFAULT_DAYAHEAD_FALLBACK_TIME,
    DEFAULT_END_SOC_MODE,
    DEFAULT_GRID_EXPORT_MAX,
    DEFAULT_GRID_IMPORT_MAX,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_INVERTER_EFFICIENCY,
    DEFAULT_MPC_INTERVAL,
    DEFAULT_SELF_CONSUME_THRESHOLD_W,
    DEFAULT_SOC_MAX,
    DEFAULT_SOC_MIN,
    DEFAULT_SOC_TARGET,
    DEFAULT_SURPLUS_HEADROOM_W,
    DEFAULT_SURPLUS_PRIORITY,
    DEFAULT_TIME_STEP,
    DEFAULT_URL,
    DEFAULT_WEIGHT_BATTERY_CHARGE,
    DEFAULT_WEIGHT_BATTERY_DISCHARGE,
    DOMAIN,
    END_SOC_MODES,
    INVERTER_FALLBACK_PROFILE,
    LOAD_PROFILE_CREATE_SENTINEL,
    LOAD_PROFILE_ORDER,
    LOAD_SUBENTRY_TYPES,
    MIN_EMHASS_VERSION,
    PRICE_PROFILE_ORDER,
    PROFILE_KEY_LOAD_SENSOR,
    PROFILE_KIND_INVERTER,
    PROFILE_KIND_LOAD,
    PROFILE_KIND_PRICE,
    PROFILE_KIND_PV,
    PROFILE_KIND_TEMPERATURE,
    PV_PROFILE_ORDER,
    RECURRENCE_DAILY,
    RECURRENCE_SURPLUS,
    RECURRENCES,
    SUBENTRY_TYPE_DEFERRABLE,
    SUBENTRY_TYPE_LOAD_GROUP,
    SUBENTRY_TYPE_THERMAL,
    TEMPERATURE_PROFILE_ORDER,
)
from .metering import async_energy_dashboard_defaults
from .naming import async_taken_standard_ids
from .profiles import (
    Profile,
    ProfileError,
    async_load_profiles,
    async_resolve_series,
    available_profiles,
)
from .tariff import MODE_LINEAR, MODE_PASSTHROUGH, MODE_TEMPLATE
from .thermal import (
    DEFAULT_COMFORT_END,
    DEFAULT_COMFORT_START,
    DEFAULT_COMFORT_TEMPERATURE,
    DEFAULT_COOLING_CONSTANT,
    DEFAULT_HEATING_RATE,
    DEFAULT_MAX_TEMPERATURE,
    DEFAULT_SETBACK_TEMPERATURE,
    DEFAULT_THERMAL_INERTIA,
    SENSE_HEAT,
    SENSES,
)
from .util import version_at_least

_LOGGER = logging.getLogger(__name__)


async def _async_validate_connection(hass: HomeAssistant, url: str) -> tuple[str, str]:
    """Check EMHASS is reachable and new enough. Returns (version, error_key)."""
    client = EmhassClient(async_get_clientsession(hass), url)
    try:
        version = await client.async_get_version()
    except EmhassError as err:
        _LOGGER.debug("Cannot connect to EMHASS at %s: %s", url, err)
        return "", "cannot_connect"

    if not version:
        return "", "unknown_version"
    if not version_at_least(version, MIN_EMHASS_VERSION):
        return version, "version_too_old"
    return version, ""


async def _async_addon_url(hass: HomeAssistant) -> str | None:
    """Best-effort discovery of a locally installed EMHASS add-on.

    "localhost" only ever reaches Home Assistant's own container, never a
    sibling add-on's -- so a fallback default has to be either this discovered
    hostname or a URL the user types in themselves.

    Every step logs why it gave up. Discovery failing looks identical to
    having no add-on -- a URL field defaulted to localhost -- so without
    these there is nothing to read when it goes wrong.
    """
    try:
        from homeassistant.components.hassio import (
            AddonError,
            AddonManager,
            AddonState,
            get_supervisor_client,
        )
    except ImportError as err:  # pragma: no cover - not a supervised install
        _LOGGER.debug("Supervisor add-on API unavailable: %s", err)
        return None

    try:
        from homeassistant.helpers.hassio import is_hassio
    except ImportError:  # pragma: no cover - HA older than 2024.6
        from homeassistant.components.hassio import is_hassio  # type: ignore[no-redef]

    if not is_hassio(hass):
        _LOGGER.debug("Not a supervised install, cannot discover the EMHASS add-on")
        return None

    from .const import ADDON_NAME, DEFAULT_PORT

    try:
        installed = await get_supervisor_client(hass).addons.list()
    except Exception as err:  # noqa: BLE001 - best-effort discovery only
        _LOGGER.debug("Could not list installed add-ons: %s", err)
        return None

    slug = next((addon.slug for addon in installed if addon.name == ADDON_NAME), None)
    if slug is None:
        _LOGGER.debug("No add-on named %r among the %d installed", ADDON_NAME, len(installed))
        return None

    manager = AddonManager(hass, _LOGGER, ADDON_NAME, slug)
    try:
        info = await manager.async_get_addon_info()
    except AddonError as err:
        _LOGGER.debug("EMHASS add-on not available: %s", err)
        return None

    if info.state != AddonState.RUNNING or not info.hostname:
        _LOGGER.debug("EMHASS add-on %s is %s with hostname %r", slug, info.state, info.hostname)
        return None
    url = f"http://{info.hostname}:{DEFAULT_PORT}"
    _LOGGER.debug("Discovered the EMHASS add-on at %s", url)
    return url


async def _async_detect_time_step(
    hass: HomeAssistant, profiles: dict[str, Profile], price_selection: dict[str, Any]
) -> int | None:
    """Best-effort read of the chosen price source's native resolution.

    Nord Pool has published 15-minute prices since October 2025; other
    sources are still hourly. There is no reason to make someone count
    minutes between two price timestamps by hand when the profile they just
    picked already knows the answer -- this is only ever used to choose a
    *default*, never to silently override a value the user goes on to type.
    """
    profile = profiles.get(price_selection.get(CONF_PROFILE, ""))
    if profile is None or not profile.produces_series:
        return None

    try:
        series = await async_resolve_series(
            hass, profile, price_selection.get(CONF_PROFILE_OPTIONS) or {}
        )
    except ProfileError as err:
        _LOGGER.debug("Could not detect price resolution: %s", err)
        return None

    step = series.step()
    if step is None:
        return None
    minutes = round(step.total_seconds() / 60)
    return minutes if minutes > 0 else None


def _suggested_entities(hass: HomeAssistant, profile: Profile) -> dict[str, str]:
    """Pre-fill each option with the first suggested entity that exists.

    Deliberately not detection: this runs only after the user has already said
    which inverter they own, so a wrong guess shows up as a visibly wrong
    entity id in a form field rather than as a silently wrong control model.
    An option whose suggestions all miss is simply left blank.
    """
    suggestions: dict[str, str] = {}
    for key, option in profile.options.items():
        for entity_id in option.get("suggest") or []:
            if hass.states.get(entity_id) is not None:
                suggestions[key] = entity_id
                break
    return suggestions


_PROFILE_ORDER_BY_KIND: dict[str, tuple[str, ...]] = {
    PROFILE_KIND_PRICE: PRICE_PROFILE_ORDER,
    PROFILE_KIND_PV: PV_PROFILE_ORDER,
}


def _rank_profiles(profiles: list[Profile], order: tuple[str, ...]) -> list[Profile]:
    """``profiles``, with anything named in ``order`` moved to the front.

    Used to put Nord Pool/Solcast (PRICE_PROFILE_ORDER/PV_PROFILE_ORDER) ahead
    of available_profiles()'s alphabetical file-load order, the same
    convention LOAD_PROFILE_ORDER already uses for the load picker. A profile
    not named in ``order`` sorts after these, in whatever order it was loaded.
    """
    rank = {key: index for index, key in enumerate(order)}
    return sorted(profiles, key=lambda profile: rank.get(profile.key, len(rank)))


def _profile_label(profile: Profile) -> str:
    """The picker label, marking a profile nobody has run on real hardware.

    The marker belongs in the label rather than only in the profile's notes
    because the picker is where the choice is actually made -- by the time the
    notes are on screen the inverter has already been chosen.
    """
    return f"{profile.name} — UNTESTED" if profile.untested else profile.name


UNTESTED_NOTICE = (
    "**This profile is untested.** It was written from the integration's source "
    "and the inverter's register map, but nobody has confirmed it against this "
    "hardware yet. Check the entities below, and watch the first few plans "
    "closely — the Control enabled switch hands it back at any time. Please "
    "report back either way so it can be marked as working.\n\n"
)


def _profile_notes(profile: Profile) -> str:
    """What the setup form shows above the entity fields."""
    notes = profile.notes or ""
    return f"{UNTESTED_NOTICE}{notes}" if profile.untested else notes


def _profile_selector(
    profiles: list[Profile], order: tuple[str, ...] = ()
) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            mode=selector.SelectSelectorMode.LIST,
            options=[
                selector.SelectOptionDict(value=profile.key, label=_profile_label(profile))
                for profile in _rank_profiles(profiles, order)
            ],
        )
    )


def _inverter_profile_selector(profiles: list[Profile]) -> selector.SelectSelector:
    """The inverter picker: hardware A-Z, with the script fallback last.

    Alphabetical rather than ranked, because the user knows what they own and
    there is nothing to guess at. ``_rank_profiles`` cannot express this -- it
    moves names to the *front*, and what this list needs is one entry held at
    the back however many brands get added in front of it.
    """
    ranked = sorted(
        profiles,
        key=lambda profile: (profile.key == INVERTER_FALLBACK_PROFILE, profile.name.lower()),
    )
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            mode=selector.SelectSelectorMode.LIST,
            options=[
                selector.SelectOptionDict(value=profile.key, label=_profile_label(profile))
                for profile in ranked
            ],
        )
    )


def _default_profile_options(profile: Profile, *, skip: set[str] = frozenset()) -> dict[str, Any]:
    """A profile's declared option defaults, for a flow that skips its options form.

    Every other path into ``CONF_PROFILE_OPTIONS`` goes through that profile's
    own form, whose voluptuous schema bakes each option's ``default:`` in at
    submission -- so the stored dict always has every key populated, and nothing
    downstream (the ``emhass:``/``source:`` template rendering) expects otherwise.
    A flow that never shows that form has to fill it in the same way, or a
    profile option with no default resolves to an undefined Jinja value at
    render time instead of the string the profile author actually intended.
    """
    return {
        key: option["default"]
        for key, option in profile.options.items()
        if key not in skip and "default" in option
    }


def _load_profile_selector(profiles: list[Profile]) -> selector.SelectSelector:
    """The load-profile picker, with "Create a house load sensor" first.

    Also reorders the real profiles per LOAD_PROFILE_ORDER: available_profiles
    otherwise returns them in alphabetical file-load order (emhass_native,
    forecast_entity, sensor), which buries "point me at a sensor" -- the
    option most users with a whole-house meter want -- under two others.
    """
    order = {key: index for index, key in enumerate(LOAD_PROFILE_ORDER)}
    ranked = sorted(profiles, key=lambda profile: order.get(profile.key, len(order)))
    options = [
        selector.SelectOptionDict(
            value=LOAD_PROFILE_CREATE_SENTINEL, label="Create a house load sensor"
        ),
        *(selector.SelectOptionDict(value=profile.key, label=profile.name) for profile in ranked),
    ]
    return selector.SelectSelector(
        selector.SelectSelectorConfig(mode=selector.SelectSelectorMode.LIST, options=options)
    )


def _tariff_side_schema(prefix: str, defaults: dict[str, Any]) -> dict[Any, Any]:
    # A 0 export multiplier zeroes out the entire sell-price series, silently
    # discarding solar export revenue from every optimisation rather than
    # producing an obviously-wrong result -- treated as "not set" instead of
    # taken at face value. `or 1.0` catches both a genuinely absent key and
    # (the same dict.get gotcha fixed for `template` above) an already
    # persisted 0 or None, which `.get(key, default)` would otherwise hand
    # straight back.
    multiplier_default = defaults.get(CONF_MULTIPLIER, 1.0)
    if prefix == "sell":
        multiplier_default = multiplier_default or 1.0
    return {
        vol.Required(
            f"{prefix}_{CONF_MODE}", default=defaults.get(CONF_MODE, MODE_LINEAR)
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="tariff_mode",
                options=[MODE_LINEAR, MODE_PASSTHROUGH, MODE_TEMPLATE],
            )
        ),
        # "any" rather than a fixed step: NumberSelector rejects steps below
        # 0.001, and per-kWh prices routinely need more precision than that.
        vol.Optional(
            f"{prefix}_{CONF_MULTIPLIER}", default=multiplier_default
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10, step="any", mode="box")
        ),
        vol.Optional(
            f"{prefix}_{CONF_ADDER}", default=defaults.get(CONF_ADDER, 0.0)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=-100, max=100, step="any", mode="box")
        ),
        vol.Optional(
            # `or ""` rather than a bare `.get(..., "")`: an *already persisted*
            # None (from before this was fixed, or from any future bug) must
            # fall back the same as a genuinely absent key. `.get(key,
            # default)` only applies `default` when the key is missing, so a
            # stored `None` would otherwise flow straight into
            # TemplateSelector's validator (`cv.template`), which raises
            # "template value is None" on every subsequent load of this step
            # -- including one that never touches the template field at all,
            # since voluptuous validates a field's default whenever the
            # submitted form omits that key.
            f"{prefix}_{CONF_TEMPLATE}",
            default=defaults.get(CONF_TEMPLATE) or "",
        ): selector.TemplateSelector(),
    }


def _collect_tariff(user_input: dict[str, Any]) -> dict[str, Any]:
    tariff: dict[str, Any] = {}
    for side in ("buy", "sell"):
        multiplier = user_input.get(f"{side}_{CONF_MULTIPLIER}", 1.0)
        # Export multiplier specifically: see the matching comment in
        # _tariff_side_schema. A blank submission already falls back to 1.0
        # via the schema default above; this catches an explicit 0 typed into
        # the field, which the schema's `min=0` does not reject.
        if side == "sell" and not multiplier:
            multiplier = 1.0
        tariff[side] = {
            CONF_MODE: user_input[f"{side}_{CONF_MODE}"],
            CONF_MULTIPLIER: multiplier,
            CONF_ADDER: user_input.get(f"{side}_{CONF_ADDER}", 0.0),
        }
        # Omit rather than store None: an absent key falls back to "" the next
        # time this step loads (see the matching comment in
        # _tariff_side_schema); a stored None does not, because dict.get's
        # default only applies when the key itself is missing.
        if template := user_input.get(f"{side}_{CONF_TEMPLATE}"):
            tariff[side][CONF_TEMPLATE] = template
    return tariff


def _collect_grid(user_input: dict[str, Any]) -> dict[str, Any]:
    """The grid step's own keys, split out from the schedule ones beside them.

    Shared by setup and options so the two cannot drift -- which is exactly
    what happened to ``capacity_cost_per_kw``: the schema asked for it and
    both handlers dropped it on the floor, leaving it stuck at its default no
    matter what anyone typed.
    """
    return {
        "grid_import_max_w": user_input["grid_import_max_w"],
        "grid_export_max_w": user_input["grid_export_max_w"],
        CONF_CAPACITY_COST_PER_KW: user_input[CONF_CAPACITY_COST_PER_KW],
        CONF_COMPUTE_CURTAILMENT: user_input[CONF_COMPUTE_CURTAILMENT],
        # Absent when left blank (see _optional_blank), and stored as None
        # rather than dropped so that clearing the field in the options flow
        # actually clears it instead of leaving the old entity in place.
        CONF_GRID_IMPORT_LIMIT_ENTITY: user_input.get(CONF_GRID_IMPORT_LIMIT_ENTITY) or None,
        CONF_GRID_EXPORT_LIMIT_ENTITY: user_input.get(CONF_GRID_EXPORT_LIMIT_ENTITY) or None,
    }


class EmhassCompanionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Guided setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._profiles: dict[str, Profile] = {}

    # -- connection -----------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        suggested = await _async_addon_url(self.hass) or DEFAULT_URL

        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            version, error = await _async_validate_connection(self.hass, url)
            if error:
                errors["base"] = error
            else:
                self._data[CONF_URL] = url
                self._data["emhass_version"] = version
                self._profiles = (await async_load_profiles(self.hass)).profiles
                return await self.async_step_price()
            suggested = url

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_URL, default=suggested): selector.TextSelector()}
            ),
            errors=errors,
            description_placeholders={"min_version": MIN_EMHASS_VERSION},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the EMHASS address without re-running the whole wizard.

        Everything else (price/pv/load sources, tariff, battery, grid) stays
        as configured -- only connectivity is in question here, so only
        connectivity is re-checked.
        """
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        suggested = entry.data[CONF_URL]

        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            version, error = await _async_validate_connection(self.hass, url)
            if error:
                errors["base"] = error
            else:
                # Reload, not just update: the EmhassClient built in
                # async_setup_entry captured the old address and keeps using
                # it. Updating the entry alone leaves every request going to
                # the previous host until Home Assistant restarts -- which is
                # exactly the situation someone reaches this step to fix.
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_URL: url, "emhass_version": version},
                )
            suggested = url

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_URL, default=suggested): selector.TextSelector()}
            ),
            errors=errors,
            description_placeholders={"min_version": MIN_EMHASS_VERSION},
        )

    # -- price ----------------------------------------------------------------

    async def async_step_price(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_profile_step(PROFILE_KIND_PRICE, CONF_PRICE, "price", user_input)

    async def async_step_price_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_profile_options_step(CONF_PRICE, "price_options", user_input)

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options["tariff"] = _collect_tariff(user_input)
            return await self.async_step_pv()

        schema = {
            **_tariff_side_schema("buy", {}),
            **_tariff_side_schema("sell", {CONF_MODE: MODE_LINEAR}),
        }
        return self.async_show_form(step_id="tariff", data_schema=vol.Schema(schema))

    # -- solar ----------------------------------------------------------------

    async def async_step_pv(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_profile_step(PROFILE_KIND_PV, CONF_PV, "pv", user_input)

    async def async_step_pv_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_profile_options_step(CONF_PV, "pv_options", user_input)

    # -- load -----------------------------------------------------------------

    async def async_step_load(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        choices = available_profiles(self.hass, self._profiles, PROFILE_KIND_LOAD)
        if not choices:
            # Should not happen: every kind ships an always-available profile.
            return self.async_abort(reason="no_profiles")

        if user_input is not None:
            if user_input[CONF_PROFILE] == LOAD_PROFILE_CREATE_SENTINEL:
                return await self.async_step_load_create()
            self._options[CONF_LOAD] = {
                CONF_PROFILE: user_input[CONF_PROFILE],
                CONF_PROFILE_OPTIONS: {},
            }
            return await self.async_step_load_options()

        return self.async_show_form(
            step_id="load",
            data_schema=vol.Schema({vol.Required(CONF_PROFILE): _load_profile_selector(choices)}),
            description_placeholders={
                "profiles": "\n".join(
                    f"- **{profile.name}** — {profile.description or ''}" for profile in choices
                )
            },
        )

    async def async_step_load_create(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Build "load/sensor" against a sensor this integration creates.

        Goes on to :meth:`async_step_load_create_options` rather than
        :meth:`async_step_load_options`: the profile it configures is fixed,
        and its own "entity" option is resolved dynamically at runtime
        (EmhassConfig._resolve_net_house_load_entity) rather than stored here,
        since the sensor's own entity id does not exist yet at this point in
        the flow -- so that one option is left out of the form the next step
        shows.
        """
        if user_input is not None:
            self._options[CONF_HOUSE_LOAD_TOTAL_ENTITY] = user_input[CONF_HOUSE_LOAD_TOTAL_ENTITY]
            return await self.async_step_load_create_options()

        return self.async_show_form(
            step_id="load_create",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOUSE_LOAD_TOTAL_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", device_class="power")
                    )
                }
            ),
        )

    async def async_step_load_create_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        profile = self._profiles[PROFILE_KEY_LOAD_SENSOR]
        schema = profile.selector_schema(skip={"entity"})

        if user_input is not None or not schema:
            self._options[CONF_LOAD] = {
                CONF_PROFILE: PROFILE_KEY_LOAD_SENSOR,
                CONF_PROFILE_OPTIONS: user_input
                or _default_profile_options(profile, skip={"entity"}),
            }
            return await self.async_step_battery()

        return self.async_show_form(
            step_id="load_create_options",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "profile": profile.name,
                "notes": _profile_notes(profile),
            },
        )

    async def async_step_load_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_profile_options_step(CONF_LOAD, "load_options", user_input)

    # -- plant ----------------------------------------------------------------

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._options["battery"] = _battery_storage_from_input(user_input)
            if soc := user_input.get(CONF_SOC_ENTITY):
                self._options[CONF_SOC_ENTITY] = soc
            if battery_power := user_input.get(CONF_BATTERY_POWER_ENTITY):
                self._options[CONF_BATTERY_POWER_ENTITY] = battery_power
                self._options[CONF_BATTERY_POWER_INVERT] = bool(
                    user_input.get(CONF_BATTERY_POWER_INVERT)
                )
            if pv_live := user_input.get(CONF_PV_ENTITY):
                self._options[CONF_PV_ENTITY] = pv_live
            return await self.async_step_inverter()

        return self.async_show_form(step_id="battery", data_schema=vol.Schema(battery_schema({})))

    async def async_step_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how the plan is written to hardware, if it is at all.

        Optional, and skippable with a blank answer -- the integration is
        perfectly useful read-only. It comes before the grid step rather than
        being left to Configure afterwards because the grid step asks about
        curtailment, and whether curtailment is even a question depends on the
        profile chosen here defining a curtail/uncurtail pair.
        """
        choices = available_profiles(self.hass, self._profiles, PROFILE_KIND_INVERTER)

        if user_input is not None:
            key = user_input.get(CONF_PROFILE)
            if not key:
                self._options[CONF_INVERTER] = {}
                return await self.async_step_grid()
            self._options[CONF_INVERTER] = {CONF_PROFILE: key, CONF_PROFILE_OPTIONS: {}}
            return await self.async_step_inverter_options()

        return self.async_show_form(
            step_id="inverter",
            data_schema=vol.Schema(
                {vol.Optional(CONF_PROFILE): _inverter_profile_selector(choices)}
            ),
        )

    async def async_step_inverter_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        profile = self._profiles[self._options[CONF_INVERTER][CONF_PROFILE]]

        if user_input is not None or not profile.options:
            self._options[CONF_INVERTER][CONF_PROFILE_OPTIONS] = user_input or {}
            return await self.async_step_grid()

        return self.async_show_form(
            step_id="inverter_options",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(profile.selector_schema()), _suggested_entities(self.hass, profile)
            ),
            description_placeholders={"profile": profile.name, "notes": _profile_notes(profile)},
        )

    async def async_step_grid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options["grid"] = _collect_grid(user_input)
            self._options.update(
                {
                    key: user_input[key]
                    for key in (
                        CONF_TIME_STEP,
                        CONF_MPC_INTERVAL,
                        CONF_HORIZON_HOURS,
                        CONF_DAYAHEAD_FALLBACK_TIME,
                    )
                }
            )
            return await self.async_step_compatibility()

        detected = await _async_detect_time_step(
            self.hass, self._profiles, self._options.get(CONF_PRICE) or {}
        )
        defaults = {CONF_TIME_STEP: detected} if detected else {}
        return self.async_show_form(
            step_id="grid",
            data_schema=vol.Schema(grid_schema(defaults)),
            description_placeholders={
                "detected_resolution": (
                    f"\n\nDetected {detected}-minute resolution from your price "
                    "source and set it as the default below — change it if "
                    "you'd rather use something else."
                )
                if detected
                else ""
            },
        )

    # -- compatibility --------------------------------------------------------

    async def async_step_compatibility(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer EMHASS's own entity ids -- but only to someone migrating.

        Shown only when those entities are already present, which is the one
        situation where the answer is not obviously "no": this user has
        dashboards, templates or automations written against those ids, and
        this option is what keeps them working. Everybody else finishes at the
        previous step and never sees this, and can still turn it on later from
        the options menu.
        """
        existing = async_taken_standard_ids(self.hass)
        if not existing:
            return self._async_finish()

        if user_input is not None:
            self._options[CONF_EMHASS_STANDARD_NAMES] = user_input[CONF_EMHASS_STANDARD_NAMES]
            return self._async_finish()

        return self.async_show_form(
            step_id="compatibility",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMHASS_STANDARD_NAMES, default=False
                    ): selector.BooleanSelector()
                }
            ),
            description_placeholders={
                "existing": "\n".join(f"- `{entity_id}`" for entity_id in existing)
            },
        )

    def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title="EMHASS Companion", data=self._data, options=self._options
        )

    # -- shared profile plumbing ---------------------------------------------

    async def _async_profile_step(
        self,
        kind: str,
        option_key: str,
        step_id: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        choices = available_profiles(self.hass, self._profiles, kind)
        if not choices:
            # Should not happen: every kind ships an always-available profile.
            return self.async_abort(reason="no_profiles")

        if user_input is not None:
            self._options[option_key] = {
                CONF_PROFILE: user_input[CONF_PROFILE],
                CONF_PROFILE_OPTIONS: {},
            }
            return await getattr(self, f"async_step_{step_id}_options")()

        order = _PROFILE_ORDER_BY_KIND.get(kind, ())
        ranked = _rank_profiles(choices, order)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required(CONF_PROFILE): _profile_selector(choices, order)}),
            description_placeholders={
                "profiles": "\n".join(
                    f"- **{profile.name}** — {profile.description or ''}" for profile in ranked
                )
            },
        )

    async def _async_profile_options_step(
        self, option_key: str, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        profile = self._profiles[self._options[option_key][CONF_PROFILE]]
        next_step = {
            CONF_PRICE: self.async_step_tariff,
            CONF_PV: self.async_step_load,
            CONF_LOAD: self.async_step_battery,
        }[option_key]

        if not profile.options:
            return await next_step()

        if user_input is not None:
            self._options[option_key][CONF_PROFILE_OPTIONS] = user_input
            return await next_step()

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(profile.selector_schema()),
            description_placeholders={
                "profile": profile.name,
                "notes": _profile_notes(profile),
            },
        )

    # -- options and subentries -----------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> EmhassCompanionOptionsFlow:
        return EmhassCompanionOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Deferrable loads are subentries, not a count.

        Each load then gets its own device page, its own entities and
        independent removal -- none of which a "number of deferrable loads"
        setting can offer. Thermal loads are a type of their own so the UI
        offers "Add thermal load" as its own button, with its own questions.
        """
        return {
            SUBENTRY_TYPE_DEFERRABLE: DeferrableLoadSubentryFlow,
            SUBENTRY_TYPE_THERMAL: ThermalLoadSubentryFlow,
            SUBENTRY_TYPE_LOAD_GROUP: LoadGroupSubentryFlow,
        }


def _optional_blank(key: str, defaults: dict[str, Any]) -> vol.Optional:
    """An optional field that can genuinely be left empty.

    A ``default`` is fatal here: voluptuous fills the absent key with it and
    hands the result to the selector, and neither ``TimeSelector`` nor
    ``EntitySelector`` accepts an empty string -- so an empty default made a
    blank field fail with "Invalid time specified:" / "Entity is neither a
    valid entity ID nor a valid UUID" before the step ever ran. That also put
    the cleanup below out of reach, since the schema runs first.

    A *suggested* value prefills the form without becoming a value the schema
    has to validate, so a blank field simply arrives as an absent key.
    """
    if (current := defaults.get(key)) in (None, ""):
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": current})


def deferrable_kind_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    """The two questions that decide what the rest of the form should ask.

    A config flow cannot show or hide a field in reaction to another field in
    the *same* form, so a load whose recurrence changes which settings even
    apply has to be asked in two steps. Surplus is the case that forces it: a
    load taking whatever solar is spare has no operating hours and no time
    window at all -- both are derived from the plan (see docs/surplus_loads.md)
    -- and offering boxes whose contents are never read is worse than asking
    one extra question.
    """
    return {
        # Length-checked because the name becomes the subentry title: an empty
        # string passes TextSelector, then gets stripped by _clean_deferrable,
        # and the title lookup raises KeyError instead of a form error.
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): vol.All(
            selector.TextSelector(), vol.Length(min=1)
        ),
        vol.Required(
            CONF_RECURRENCE, default=defaults.get(CONF_RECURRENCE, RECURRENCE_DAILY)
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                mode=selector.SelectSelectorMode.LIST,
                translation_key="recurrence",
                options=list(RECURRENCES),
            )
        ),
    }


def deferrable_schema(
    defaults: dict[str, Any],
    step_minutes: int = DEFAULT_TIME_STEP,
    *,
    initial: bool = True,
    recurrence: str = RECURRENCE_DAILY,
) -> dict[Any, Any]:
    """Fields for adding or reconfiguring a deferrable load.

    Split by who owns the value afterwards. The subentry owns what the load
    *is* -- its name, its meter, what the executor switches -- and only those
    fields appear when reconfiguring. Everything the optimiser is *told* is
    owned by an entity from the moment the load exists, so that an automation
    can change it without reloading the config entry; the fields here are the
    values a new load starts from, and re-offering them later would show an
    edit box whose contents the restored entity immediately overrides.

    The name is asked in :func:`deferrable_kind_schema` when adding, alongside
    the recurrence that decides which of the fields below are worth showing.
    Reconfiguring still offers it here, since renaming a load is one of the few
    things that step is for.
    """
    schema: dict[Any, Any] = {}

    if not initial:
        schema[vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, ""))] = vol.All(
            selector.TextSelector(), vol.Length(min=1)
        )

    if initial:
        schema.update(
            {
                vol.Required(
                    CONF_NOMINAL_POWER, default=defaults.get(CONF_NOMINAL_POWER, 2000)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10, max=100000, step=10, unit_of_measurement="W", mode="box"
                    )
                ),
                # Shown unconditionally even though it only bites when the load
                # is not semi-continuous: a config flow form cannot show or hide
                # a field in reaction to another field in the same form, and the
                # same call is already made for the hybrid inverter settings
                # below. The description says when it applies.
                vol.Optional(
                    CONF_MINIMUM_POWER, default=defaults.get(CONF_MINIMUM_POWER, 0)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=100000, step=10, unit_of_measurement="W", mode="box"
                    )
                ),
                vol.Required(
                    CONF_SEMI_CONTINUOUS, default=defaults.get(CONF_SEMI_CONTINUOUS, True)
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_STARTUP_PENALTY, default=defaults.get(CONF_STARTUP_PENALTY, 0)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=100, step="any", mode="box")
                ),
            }
        )

    if initial and recurrence == RECURRENCE_SURPLUS:
        # No hours and no window: both are derived from the exported power in
        # the plan on every run. What a surplus load takes instead is an
        # optional total, and a margin against PV forecast error.
        #
        # "Must run in one unbroken block" and "Most starts per plan" are left
        # out too. Both are hard constraints, and on a broken-cloud day they
        # drive the load straight through the gap between two peaks and import
        # to satisfy themselves; the startup penalty above buys the same
        # unbroken run on the days it is actually free. Neither is gone -- both
        # are still switches on the load, for anyone who wants them.
        schema.update(
            {
                vol.Optional(
                    CONF_ENERGY_NEEDED, default=defaults.get(CONF_ENERGY_NEEDED, 0)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=500, step=0.5, unit_of_measurement="kWh", mode="box"
                    )
                ),
                vol.Optional(
                    CONF_SURPLUS_HEADROOM,
                    default=defaults.get(CONF_SURPLUS_HEADROOM, DEFAULT_SURPLUS_HEADROOM_W),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=10000, step=50, unit_of_measurement="W", mode="box"
                    )
                ),
                # Only matters with two or more surplus loads; harmless to ask
                # unconditionally since it defaults to the existing name order.
                vol.Optional(
                    CONF_SURPLUS_PRIORITY,
                    default=defaults.get(CONF_SURPLUS_PRIORITY, DEFAULT_SURPLUS_PRIORITY),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=100, step=1, mode="box")
                ),
            }
        )
    elif initial:
        schema.update(
            {
                # Stepped by the optimisation timestep, which is the only
                # granularity EMHASS can honour for a load that is either fully
                # on or fully off (see payload.operating_timesteps).
                vol.Required(
                    CONF_OPERATING_HOURS, default=defaults.get(CONF_OPERATING_HOURS, 2)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=24,
                        step=round(step_minutes / 60, 4),
                        unit_of_measurement="h",
                        mode="box",
                    )
                ),
                _optional_blank(CONF_EARLIEST_START, defaults): selector.TimeSelector(),
                _optional_blank(CONF_LATEST_END, defaults): selector.TimeSelector(),
                vol.Required(
                    CONF_SINGLE_CONSTANT, default=defaults.get(CONF_SINGLE_CONSTANT, False)
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_MAX_STARTUPS, default=defaults.get(CONF_MAX_STARTUPS, 0)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=100, step=1, mode="box")
                ),
                # Minimum dwell time once the load switches on/off, protecting
                # compressor-driven loads from short-cycling. Excluded for
                # surplus loads for the same reason as max_startups above: both
                # are hard timing constraints a broken-cloud day can make
                # infeasible against a derived, not configured, run window.
                vol.Optional(
                    CONF_MINIMUM_ON_TIME, default=defaults.get(CONF_MINIMUM_ON_TIME, 0)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=240, step=1, unit_of_measurement="min", mode="box"
                    )
                ),
                vol.Optional(
                    CONF_MINIMUM_OFF_TIME, default=defaults.get(CONF_MINIMUM_OFF_TIME, 0)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=240, step=1, unit_of_measurement="min", mode="box"
                    )
                ),
            }
        )

    schema.update(
        {
            # Two very different kinds of "yes/no" both belong here: a numeric
            # power sensor (state_to_power reads and unit-converts it) and a
            # binary_sensor (state_to_power already treats on/off as
            # nominal-or-zero -- the same path control_entity's fallback uses).
            # Not every load has a meter; a door/vibration/current-clamp
            # binary_sensor is often all that exists.
            _optional_blank(CONF_POWER_SENSOR, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    filter=[
                        {"domain": "sensor", "device_class": "power"},
                        {"domain": "binary_sensor"},
                    ]
                )
            ),
            _optional_blank(CONF_CONTROL_ENTITY, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=list(CONTROL_ENTITY_DOMAINS))
            ),
        }
    )
    return schema


def thermal_schema(
    defaults: dict[str, Any], step_minutes: int = DEFAULT_TIME_STEP, *, initial: bool = True
) -> dict[Any, Any]:
    """Fields for adding or reconfiguring a thermal load.

    A thermal load is one whose *temperature* the optimiser controls rather
    than its run time: a heat pump, storage heating, an air conditioner. The
    same ownership split as :func:`deferrable_schema` applies -- the subentry
    owns what the load is (its name, its sensors, whether it heats or cools),
    while everything the optimiser is told (comfort band, physics) becomes a
    control on the load's page from the moment it exists.

    ``step_minutes`` is accepted for signature parity with
    :func:`deferrable_schema` -- nothing here is quantised by the timestep.
    """
    schema: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): vol.All(
            selector.TextSelector(), vol.Length(min=1)
        ),
        # The room (or tank) the comfort band applies to. Optional but strongly
        # recommended: without it every plan starts from EMHASS's default
        # start temperature instead of the actual room.
        _optional_blank(CONF_TEMPERATURE_SENSOR, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(
                filter=[{"domain": "sensor", "device_class": "temperature"}]
            )
        ),
        vol.Required(
            CONF_SENSE, default=defaults.get(CONF_SENSE, SENSE_HEAT)
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="sense",
                options=list(SENSES),
            )
        ),
    }

    if initial:
        schema.update(
            {
                vol.Required(
                    CONF_NOMINAL_POWER, default=defaults.get(CONF_NOMINAL_POWER, 2000)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10, max=100000, step=10, unit_of_measurement="W", mode="box"
                    )
                ),
                vol.Required(
                    CONF_COMFORT_TEMPERATURE,
                    default=defaults.get(CONF_COMFORT_TEMPERATURE, DEFAULT_COMFORT_TEMPERATURE),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=35, step=0.5, unit_of_measurement="°C", mode="box"
                    )
                ),
                vol.Required(
                    CONF_SETBACK_TEMPERATURE,
                    default=defaults.get(CONF_SETBACK_TEMPERATURE, DEFAULT_SETBACK_TEMPERATURE),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=35, step=0.5, unit_of_measurement="°C", mode="box"
                    )
                ),
                vol.Required(
                    CONF_MAX_TEMPERATURE,
                    default=defaults.get(CONF_MAX_TEMPERATURE, DEFAULT_MAX_TEMPERATURE),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=40, step=0.5, unit_of_measurement="°C", mode="box"
                    )
                ),
                vol.Required(
                    CONF_COMFORT_START,
                    default=defaults.get(CONF_COMFORT_START, str(DEFAULT_COMFORT_START)),
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_COMFORT_END,
                    default=defaults.get(CONF_COMFORT_END, str(DEFAULT_COMFORT_END)),
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_HEATING_RATE,
                    default=defaults.get(CONF_HEATING_RATE, DEFAULT_HEATING_RATE),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.1, max=20, step="any", unit_of_measurement="°C/h", mode="box"
                    )
                ),
                vol.Required(
                    CONF_COOLING_CONSTANT,
                    default=defaults.get(CONF_COOLING_CONSTANT, DEFAULT_COOLING_CONSTANT),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=2, step="any", mode="box")
                ),
                vol.Optional(
                    CONF_THERMAL_INERTIA,
                    default=defaults.get(CONF_THERMAL_INERTIA, DEFAULT_THERMAL_INERTIA),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=6, step=0.25, unit_of_measurement="h", mode="box"
                    )
                ),
            }
        )

    schema.update(
        {
            _optional_blank(CONF_POWER_SENSOR, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    filter=[
                        {"domain": "sensor", "device_class": "power"},
                        {"domain": "binary_sensor"},
                    ]
                )
            ),
            _optional_blank(CONF_CONTROL_ENTITY, defaults): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=list(CONTROL_ENTITY_DOMAINS))
            ),
        }
    )
    return schema


def _clean_deferrable(user_input: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional fields so absent means "unset", not "midnight"."""
    return {key: value for key, value in user_input.items() if value not in (None, "")}


class DeferrableLoadSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure one deferrable load."""

    _schema = staticmethod(deferrable_schema)
    _kind_schema: Any = staticmethod(deferrable_kind_schema)
    """The first step's fields, or None for a load type with only one step.
    Set to None by the thermal flow, whose demand is a comfort band rather than
    a run time and so has no recurrence to choose."""

    _kind: dict[str, Any]
    """What the first step answered, carried into the second."""

    # Optional entity pickers that a reconfigure may clear: a blank field
    # arrives as an absent key, and must *remove* the stored value rather than
    # leave it behind.
    _clearable_keys: tuple[str, ...] = (CONF_POWER_SENSOR, CONF_CONTROL_ENTITY)

    @property
    def _step_minutes(self) -> int:
        return int(self._get_entry().options.get(CONF_TIME_STEP, DEFAULT_TIME_STEP))

    def _placeholders(self) -> dict[str, str]:
        """Values the form's own text refers to.

        The timestep is worth stating outright: it is the granularity of every
        answer EMHASS can give, and it is configured two menus away from here.
        """
        step = self._step_minutes
        return {"time_step": str(step), "time_step_hours": f"{step / 60:g}"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if self._kind_schema is None:
            # One step: nothing asked here changes which fields follow.
            if user_input is not None:
                return self._create(_clean_deferrable(user_input))
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(self._schema({}, self._step_minutes)),
                description_placeholders=self._placeholders(),
            )

        if user_input is not None:
            self._kind = _clean_deferrable(user_input)
            return await self.async_step_settings()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(self._kind_schema({})),
            description_placeholders=self._placeholders(),
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """The fields the recurrence chosen in the first step actually uses."""
        if user_input is not None:
            return self._create({**self._kind, **_clean_deferrable(user_input)})

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                self._schema(
                    {},
                    self._step_minutes,
                    recurrence=self._kind.get(CONF_RECURRENCE, RECURRENCE_DAILY),
                )
            ),
            description_placeholders=self._placeholders(),
        )

    def _create(self, data: dict[str, Any]) -> SubentryFlowResult:
        # Entities for a load are built once, in async_setup_entry, from
        # entry.runtime_data.loads.all() -- creating a subentry here does
        # not by itself add anything. __init__.py's SIGNAL_CONFIG_ENTRY_CHANGED
        # listener is what schedules the reload, and deliberately not this
        # step: ConfigSubentryFlowManager.async_finish_flow only calls
        # async_add_subentry *after* this step returns, so reloading from
        # here would race it -- sometimes running before the subentry
        # exists to be picked up.
        return self.async_create_entry(title=data[CONF_NAME], data=data)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            # The fields this step no longer offers are still part of the
            # subentry -- they are what a load starts from, and dropping them
            # here would reset a load to the defaults on its next reload.
            data = {**subentry.data, **_clean_deferrable(user_input)}
            for key in self._clearable_keys:
                if key not in user_input:
                    data.pop(key, None)
            return self.async_update_and_abort(
                self._get_entry(), subentry, title=data[CONF_NAME], data=data
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                self._schema(dict(subentry.data), self._step_minutes, initial=False)
            ),
            description_placeholders=self._placeholders(),
        )


class ThermalLoadSubentryFlow(DeferrableLoadSubentryFlow):
    """Add or reconfigure one thermal load.

    The thermal questions instead of the deferrable ones, asked in a single
    step: a thermal load's demand is its comfort band, which stands every day
    by nature, so there is no recurrence to pick and nothing that would change
    which fields follow. The temperature sensor joins the clearable keys so a
    blanked picker actually forgets the sensor.
    """

    _schema = staticmethod(thermal_schema)
    _kind_schema = None
    _clearable_keys = (CONF_POWER_SENSOR, CONF_CONTROL_ENTITY, CONF_TEMPERATURE_SENSOR)


def load_group_schema(
    defaults: dict[str, Any], load_options: list[selector.SelectOptionDict]
) -> dict[Any, Any]:
    """Fields for adding or reconfiguring a load group.

    A group is a relationship between existing deferrable-load subentries, not
    a load of its own -- it has no power, no run time, nothing the registry
    tracks. ``max_power_w`` is genuinely optional but only when mutual
    exclusion is on; that dependency is checked in the flow's step handler,
    not the schema, so the error can name the actual reason.
    """
    return {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): vol.All(
            selector.TextSelector(), vol.Length(min=1)
        ),
        vol.Required(
            CONF_GROUP_LOAD_IDS, default=defaults.get(CONF_GROUP_LOAD_IDS, [])
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                mode=selector.SelectSelectorMode.LIST,
                multiple=True,
                options=load_options,
            )
        ),
        vol.Required(
            CONF_GROUP_MUTUAL_EXCLUSION,
            default=defaults.get(CONF_GROUP_MUTUAL_EXCLUSION, False),
        ): selector.BooleanSelector(),
        _optional_blank(CONF_GROUP_MAX_POWER, defaults): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100000, step=10, unit_of_measurement="W", mode="box"
            )
        ),
    }


class LoadGroupSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure one load group.

    A group expresses a relationship between existing deferrable-load
    subentries -- a shared power budget or mutual exclusion -- rather than
    being a load itself, so it does not subclass DeferrableLoadSubentryFlow:
    the field shape is unrelated. It needs no entities of its own; Home
    Assistant already lists subentries under the integration's config entry.
    """

    def _load_options(self) -> list[selector.SelectOptionDict]:
        return [
            selector.SelectOptionDict(value=subentry_id, label=subentry.title)
            for subentry_id, subentry in self._get_entry().subentries.items()
            if subentry.subentry_type in LOAD_SUBENTRY_TYPES
        ]

    @staticmethod
    def _errors(data: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if len(data.get(CONF_GROUP_LOAD_IDS, [])) < 2:
            errors[CONF_GROUP_LOAD_IDS] = "too_few_loads"
        elif not data.get(CONF_GROUP_MUTUAL_EXCLUSION) and CONF_GROUP_MAX_POWER not in data:
            errors[CONF_GROUP_MAX_POWER] = "max_power_required"
        return errors

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._errors(user_input)
            if not errors:
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(load_group_schema(user_input or {}, self._load_options())),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._errors(user_input)
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=user_input[CONF_NAME], data=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                load_group_schema(user_input or dict(subentry.data), self._load_options())
            ),
            errors=errors,
        )


_SOC_PERCENT_FIELDS = (
    "soc_min",
    "soc_max",
    "soc_target",
    # EMHASS wants these as 0-1 fractions too, so they convert on the same path
    # as the sliders above rather than being stored in the form's own percent.
    CONF_BATTERY_SOC_DEFICIT_THRESHOLD,
    CONF_BATTERY_SOC_SURPLUS_THRESHOLD,
)


def _battery_storage_from_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """The battery step's form values, as stored in options (and sent to EMHASS).

    ``soc_min``/``soc_max``/``soc_target`` are entered as percent in the form
    but stored as a 0-1 fraction, matching what EMHASS's own API expects
    (see payload.py) and what the rest of the integration assumes.
    """
    return {
        key: value / 100 if key in _SOC_PERCENT_FIELDS else value
        for key, value in user_input.items()
        if key
        not in (
            CONF_SOC_ENTITY,
            CONF_BATTERY_POWER_ENTITY,
            CONF_BATTERY_POWER_INVERT,
            CONF_PV_ENTITY,
        )
    }


def battery_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Required(
            "use_battery", default=defaults.get("use_battery", False)
        ): selector.BooleanSelector(),
        vol.Optional(
            "capacity_wh", default=defaults.get("capacity_wh", 0)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=1000000, step=100, unit_of_measurement="Wh", mode="box"
            )
        ),
        vol.Optional(
            "charge_power_max_w", default=defaults.get("charge_power_max_w", 0)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100000, step=100, unit_of_measurement="W", mode="box"
            )
        ),
        vol.Optional(
            "discharge_power_max_w", default=defaults.get("discharge_power_max_w", 0)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100000, step=100, unit_of_measurement="W", mode="box"
            )
        ),
        # EMHASS's own default is True, which silently blocks the battery from
        # ever selling -- even a plan that would otherwise discharge into a
        # price spike sits idle. Default False here so export works unless
        # deliberately turned off.
        vol.Required(
            "no_discharge_to_grid", default=defaults.get("no_discharge_to_grid", False)
        ): selector.BooleanSelector(),
        # A hybrid inverter shares one AC-side throughput limit between PV and
        # battery. Left off, the fields below are collected but never sent to
        # EMHASS (see payload.py's _hybrid_inverter_settings) -- harmless to
        # show unconditionally, matching how the battery fields above already
        # behave when "use_battery" is off.
        vol.Required(
            CONF_HYBRID_INVERTER, default=defaults.get(CONF_HYBRID_INVERTER, False)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_INVERTER_AC_OUTPUT_MAX, default=defaults.get(CONF_INVERTER_AC_OUTPUT_MAX, 0)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100000, step=100, unit_of_measurement="W", mode="box"
            )
        ),
        vol.Optional(
            CONF_INVERTER_AC_INPUT_MAX, default=defaults.get(CONF_INVERTER_AC_INPUT_MAX, 0)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100000, step=100, unit_of_measurement="W", mode="box"
            )
        ),
        vol.Optional(
            CONF_INVERTER_EFFICIENCY_DC_AC,
            default=defaults.get(CONF_INVERTER_EFFICIENCY_DC_AC, DEFAULT_INVERTER_EFFICIENCY),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.5, max=1.0, step=0.01, mode="slider")
        ),
        vol.Optional(
            CONF_INVERTER_EFFICIENCY_AC_DC,
            default=defaults.get(CONF_INVERTER_EFFICIENCY_AC_DC, DEFAULT_INVERTER_EFFICIENCY),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.5, max=1.0, step=0.01, mode="slider")
        ),
        # Cycle cost per kWh of throughput, in the tariff's own currency.
        # Both default to 0.0 (EMHASS's default): no wear cost, so the plan
        # chases any price spread at all. A box rather than a slider -- the
        # useful range is a few cents wide and varies with battery chemistry
        # and the local price spread, so there is no sensible slider scale.
        vol.Optional(
            CONF_WEIGHT_BATTERY_DISCHARGE,
            default=defaults.get(CONF_WEIGHT_BATTERY_DISCHARGE, DEFAULT_WEIGHT_BATTERY_DISCHARGE),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10, step=0.001, mode="box")
        ),
        vol.Optional(
            CONF_WEIGHT_BATTERY_CHARGE,
            default=defaults.get(CONF_WEIGHT_BATTERY_CHARGE, DEFAULT_WEIGHT_BATTERY_CHARGE),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10, step=0.001, mode="box")
        ),
        # Dwell costs on the SOC level, each paired with the threshold it is
        # measured from. Thresholds are sliders in percent like soc_min/soc_max
        # (converted by _battery_storage_from_input); the costs are boxes, on
        # the same reasoning as the cycle costs above. Both costs default to 0,
        # which is what keeps the pre-filled thresholds inert.
        vol.Optional(
            CONF_BATTERY_SOC_DEFICIT_THRESHOLD,
            default=round(
                defaults.get(
                    CONF_BATTERY_SOC_DEFICIT_THRESHOLD, DEFAULT_BATTERY_SOC_DEFICIT_THRESHOLD
                )
                * 100
            ),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100, step=1, unit_of_measurement="%", mode="slider"
            )
        ),
        vol.Optional(
            CONF_BATTERY_SOC_DEFICIT_COST,
            default=defaults.get(CONF_BATTERY_SOC_DEFICIT_COST, DEFAULT_BATTERY_SOC_DEFICIT_COST),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10, step=0.001, mode="box")
        ),
        vol.Optional(
            CONF_BATTERY_SOC_SURPLUS_THRESHOLD,
            default=round(
                defaults.get(
                    CONF_BATTERY_SOC_SURPLUS_THRESHOLD, DEFAULT_BATTERY_SOC_SURPLUS_THRESHOLD
                )
                * 100
            ),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100, step=1, unit_of_measurement="%", mode="slider"
            )
        ),
        vol.Optional(
            CONF_BATTERY_SOC_SURPLUS_COST,
            default=defaults.get(CONF_BATTERY_SOC_SURPLUS_COST, DEFAULT_BATTERY_SOC_SURPLUS_COST),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10, step=0.001, mode="box")
        ),
        # C-rate penalty. Segments is a solver knob rather than a plant
        # property -- bounded well below EMHASS's unbounded read because each
        # extra segment adds two constraints per timestep per battery for
        # steadily less curve accuracy.
        vol.Optional(
            CONF_BATTERY_STRESS_SEGMENTS,
            default=defaults.get(CONF_BATTERY_STRESS_SEGMENTS, DEFAULT_BATTERY_STRESS_SEGMENTS),
        ): vol.All(
            selector.NumberSelector(
                selector.NumberSelectorConfig(min=2, max=50, step=1, mode="slider")
            ),
            vol.Coerce(int),
        ),
        vol.Optional(
            CONF_BATTERY_STRESS_COST,
            default=defaults.get(CONF_BATTERY_STRESS_COST, DEFAULT_BATTERY_STRESS_COST),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10, step=0.001, mode="box")
        ),
        # Stored, and sent to EMHASS, as a 0-1 fraction -- the form shows
        # percent because a slider labelled "0.10" is not obviously "10%".
        # See _battery_storage_from_input for the conversion back.
        vol.Optional(
            "soc_min", default=round(defaults.get("soc_min", DEFAULT_SOC_MIN) * 100)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100, step=1, unit_of_measurement="%", mode="slider"
            )
        ),
        vol.Optional(
            "soc_max", default=round(defaults.get("soc_max", DEFAULT_SOC_MAX) * 100)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100, step=1, unit_of_measurement="%", mode="slider"
            )
        ),
        vol.Optional(
            "soc_target", default=round(defaults.get("soc_target", DEFAULT_SOC_TARGET) * 100)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100, step=1, unit_of_measurement="%", mode="slider"
            )
        ),
        # How the end-of-horizon SOC target is chosen (terminal.py). Sits next
        # to soc_target because that slider feeds both non-"Same as start"
        # modes: it is the reserve floor Optimized never plans below, and the
        # exact value "Fixed %" asks for.
        vol.Optional(
            CONF_END_SOC_MODE,
            default=defaults.get(CONF_END_SOC_MODE, DEFAULT_END_SOC_MODE),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                mode=selector.SelectSelectorMode.DROPDOWN,
                options=list(END_SOC_MODES),
                translation_key="end_soc_mode",
            )
        ),
        # Below this |P_grid|, the plan wants no grid exchange at all -- the
        # battery is handed to the inverter's own self-consumption mode
        # instead of being forced. Not the same knob as soc_min/soc_max above:
        # those are what the optimiser may plan between, this is a real-time
        # mode choice read from the plan's own grid-power column.
        vol.Optional(
            "self_consume_threshold_w",
            default=defaults.get("self_consume_threshold_w", DEFAULT_SELF_CONSUME_THRESHOLD_W),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=5000, step=50, unit_of_measurement="W", mode="box"
            )
        ),
        _optional_blank(CONF_SOC_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="battery")
        ),
        # Nothing in the optimisation reads this pair; the dashboard cards do.
        # Asked for here rather than on each card so that the sensor and, more
        # to the point, which way round it counts are answered once. See
        # CONF_BATTERY_POWER_ENTITY.
        _optional_blank(CONF_BATTERY_POWER_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power")
        ),
        vol.Optional(
            CONF_BATTERY_POWER_INVERT,
            default=defaults.get(CONF_BATTERY_POWER_INVERT, False),
        ): selector.BooleanSelector(),
        # Not a battery setting -- there is no PV-specific step in either flow
        # to put it in (the setup flow's PV step is profile selection only,
        # and PV has no options-flow step at all), so it rides along with
        # soc_entity as the form that already collects miscellaneous live
        # sensor readings. Blended into the first naive-mpc-optim forecast
        # step (payload.build_payload) when set; see number.MixBetaNumber for
        # the blend weight.
        _optional_blank(CONF_PV_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power")
        ),
    }


METERING_SECTION: Final = "meters"
"""Section key the override pickers are nested under in the form payload."""

METERING_KEYS: Final = (
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_GRID_EXPORT_ENERGY_ENTITY,
    CONF_PV_ENERGY_ENTITY,
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
)
"""The five meters, in the order they are shown.

Deliberately the Energy dashboard's own grid, solar and battery sources and
nothing else, which is what lets the whole screen collapse to one switch: the
answers are already on file, so they can be resolved rather than asked for.

Two fields that the first version of this screen had are gone on purpose:

* a **grid power** sensor, as a fallback for houses with no import/export
  counters. Those houses cannot use Home Assistant's own Energy dashboard
  either, so they have already built a Riemann-sum helper -- and offering a
  second, worse integration of the same signal is a field, a code path and a
  way to be quietly less accurate, for nobody.
* a **house consumption** counter. The entry already asks for a whole-house
  power sensor for the load forecast, and the energy balance covers the rest,
  so the question could only ever be asked of people already answered.

One tuple, read by the schema, the summary and the step that collects it, so a
field can never be offered and then dropped on save -- which is what happened
to ``capacity_cost_per_kw`` (see the note on the setup flow's grid step).
"""


def _metering_summary(resolved: dict[str, Any], config: EmhassConfig) -> str:
    """What the switch will actually use, written out for the description.

    The screen has one control, so this text is the only thing standing between
    the user and a number they cannot trace. It names every meter, says which
    were resolved from the Energy dashboard against which came from sensors
    already configured here, and -- most importantly -- says plainly when the
    grid pair is missing, since that is the one case where turning the switch
    on produces nothing at all.
    """
    labels = {
        CONF_GRID_IMPORT_ENERGY_ENTITY: "Grid import",
        CONF_GRID_EXPORT_ENERGY_ENTITY: "Grid export",
        CONF_PV_ENERGY_ENTITY: "Solar",
        CONF_BATTERY_CHARGE_ENERGY_ENTITY: "Battery charged",
        CONF_BATTERY_DISCHARGE_ENERGY_ENTITY: "Battery discharged",
    }
    lines = [
        f"- **{label}**: `{resolved[key]}`" for key, label in labels.items() if resolved.get(key)
    ]

    # The fallbacks are not on this form at all -- they are sensors the entry
    # already has for other reasons -- so they would be invisible without this.
    # Read straight off the config rather than through build_meters, which
    # answers for a *configured* entry and returns nothing while the switch is
    # still off, i.e. exactly when this screen is being looked at.
    if not resolved.get(CONF_PV_ENERGY_ENTITY) and config.pv_live_entity:
        lines.append(f"- **Solar**: `{config.pv_live_entity}` (your live PV sensor)")
    if config.house_load_total_entity:
        lines.append(
            f"- **House consumption**: `{config.house_load_total_entity}` (your house load sensor)"
        )
    else:
        lines.append("- **House consumption**: derived from the other meters")

    missing = not (
        resolved.get(CONF_GRID_IMPORT_ENERGY_ENTITY)
        and resolved.get(CONF_GRID_EXPORT_ENERGY_ENTITY)
    )
    if missing:
        return (
            "\n\n**No grid import/export counters were found**, and they are the one "
            "requirement -- without them there is no actual cost to compare anything "
            "against. Set them under *Meters* below, or add them to your Energy "
            "dashboard first and come back."
        )
    return "\n\nUsing:\n" + "\n".join(lines)


def metering_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    """The override pickers, shown collapsed.

    Every field optional, and pre-filled with whatever was resolved, so this
    section is empty-handed only for a house the Energy dashboard could not
    answer for. Filtered to the energy device class: these are counters, and
    offering the whole sensor domain would bury them.
    """
    energy = selector.EntitySelectorConfig(domain="sensor", device_class="energy")
    return {
        _optional_blank(key, defaults): selector.EntitySelector(energy) for key in METERING_KEYS
    }


STANDARD_TIME_STEPS = ("5", "10", "15", "20", "30", "60")


def _time_step_options(defaults: dict[str, Any]) -> list[str]:
    """The dropdown presets, plus whatever was detected or already stored.

    A price source's real resolution is not guaranteed to be one of the
    common values -- keeping it in the list (rather than only reachable via
    typing it in) is what makes the auto-detected default actually show up
    as a normal, selected option instead of looking unset.
    """
    options = set(STANDARD_TIME_STEPS)
    if (current := defaults.get(CONF_TIME_STEP)) is not None:
        options.add(str(current))
    return sorted(options, key=int)


def grid_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Required(
            "grid_import_max_w",
            default=defaults.get("grid_import_max_w", DEFAULT_GRID_IMPORT_MAX),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100000, step=100, unit_of_measurement="W", mode="box"
            )
        ),
        vol.Required(
            "grid_export_max_w",
            default=defaults.get("grid_export_max_w", DEFAULT_GRID_EXPORT_MAX),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=100000, step=100, unit_of_measurement="W", mode="box"
            )
        ),
        # Live overrides for the two numbers above, for a connection whose
        # usable limit is not constant -- most often an unbalanced three-phase
        # service, where the fuse that binds is the worst phase's, not the sum.
        # Not filtered on device_class="power" like soc_entity/pv_entity are:
        # what belongs here is nearly always a template sensor the user wrote,
        # and those are routinely declared without a device class, which would
        # leave the picker looking empty for exactly the people who need it.
        _optional_blank(CONF_GRID_IMPORT_LIMIT_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        _optional_blank(CONF_GRID_EXPORT_LIMIT_ENTITY, defaults): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        # Demand charge on the horizon's peak import, in currency per kW. Sits
        # with the grid limits rather than the battery because it prices grid
        # power: deferrable loads can shave a peak with no battery in play.
        vol.Optional(
            CONF_CAPACITY_COST_PER_KW,
            default=defaults.get(CONF_CAPACITY_COST_PER_KW, DEFAULT_CAPACITY_COST_PER_KW),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=1000, step=0.01, mode="box")
        ),
        # The only curtailment question there is: without this, no run ever
        # produces a P_PV_curtailment column, and strategy.decide_curtailment
        # has nothing to act on.
        vol.Required(
            CONF_COMPUTE_CURTAILMENT,
            default=defaults.get(CONF_COMPUTE_CURTAILMENT, DEFAULT_COMPUTE_CURTAILMENT),
        ): selector.BooleanSelector(),
        # A fixed list would leave anyone whose price source publishes at some
        # other resolution stuck rounding to the nearest preset; custom_value
        # lets them type the real number instead. Coerced and range-checked
        # here rather than left as a free string, so a typo is a form error
        # now instead of a crash reading the config entry months later.
        vol.Required(
            CONF_TIME_STEP, default=str(defaults.get(CONF_TIME_STEP, DEFAULT_TIME_STEP))
        ): vol.All(
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    options=_time_step_options(defaults),
                    custom_value=True,
                )
            ),
            vol.Coerce(int),
            vol.Range(min=1, max=180),
        ),
        vol.Required(
            CONF_MPC_INTERVAL,
            default=defaults.get(CONF_MPC_INTERVAL, DEFAULT_MPC_INTERVAL),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=5, max=60, step=5, unit_of_measurement="min", mode="box"
            )
        ),
        vol.Required(
            CONF_HORIZON_HOURS,
            default=defaults.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=6, max=48, step=1, unit_of_measurement="h", mode="box"
            )
        ),
        vol.Required(
            CONF_DAYAHEAD_FALLBACK_TIME,
            default=defaults.get(CONF_DAYAHEAD_FALLBACK_TIME, DEFAULT_DAYAHEAD_FALLBACK_TIME),
        ): selector.TimeSelector(),
    }


class EmhassCompanionOptionsFlow(OptionsFlowWithReload):
    """Retune an existing entry.

    A menu rather than a re-run of the whole wizard: changing the grid export
    limit should not mean re-answering every question about price sources.
    """

    def __init__(self) -> None:
        self._inverter_key: str = ""
        self._inverter_profiles: dict[str, Profile] = {}
        self._temperature_key: str = ""
        self._temperature_profiles: dict[str, Profile] = {}
        self._load_key: str = ""
        self._load_profiles: dict[str, Profile] = {}
        self._pv_key: str = ""
        self._pv_profiles: dict[str, Profile] = {}
        self._house_load_total_entity: str = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "load",
                "pv",
                "battery",
                "grid",
                "tariff",
                "inverter",
                "temperature",
                "metering",
                "compatibility",
            ],
        )

    async def async_step_metering(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Turn cost and savings tracking on, and say which meters it used.

        Its own step, and options-only rather than part of the setup wizard,
        because nothing in the optimisation depends on any of it: these are
        what the house *did*, and everything else the wizard asks for is what
        EMHASS should plan against. A user who never opens this screen loses
        the savings sensors and nothing else.

        **One visible field.** The five meters this needs are the Energy
        dashboard's own grid, solar and battery sources, so they are resolved
        from it and simply *shown* in the description; the pickers live in a
        collapsed section for the houses that resolution cannot answer. Asking
        five entity-picker questions whose answers were already on file was the
        first version of this screen and it was tedious for no gain.

        Resolving on submit and storing the result -- rather than looking the
        dashboard up at runtime -- is what the switch really buys. Otherwise a
        user reorganising their Energy dashboard next spring silently repoints
        a savings figure at a different meter, or breaks it outright, with
        nothing anywhere saying so.
        """
        options = dict(self.config_entry.options)
        stored = options.get(CONF_METERING) or {}
        suggested = await async_energy_dashboard_defaults(self.hass)
        resolved = {key: stored.get(key) or suggested.get(key) or "" for key in METERING_KEYS}

        if user_input is not None:
            overrides = user_input.get(METERING_SECTION) or {}
            options[CONF_METERING] = {
                CONF_METERING_ENABLED: user_input[CONF_METERING_ENABLED],
                # An override wins; anything left blank keeps whatever was
                # resolved, so clearing a field falls back rather than blanking
                # the quantity.
                **{key: (overrides.get(key) or resolved.get(key) or None) for key in METERING_KEYS},
            }
            return self.async_create_entry(data=options)

        config = EmhassConfig.from_entry(self.hass, self.config_entry)
        return self.async_show_form(
            step_id="metering",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_METERING_ENABLED,
                        default=bool(stored.get(CONF_METERING_ENABLED, bool(stored))),
                    ): selector.BooleanSelector(),
                    vol.Required(METERING_SECTION): section(
                        vol.Schema(metering_schema(resolved)), {"collapsed": True}
                    ),
                }
            ),
            description_placeholders={"found": _metering_summary(resolved, config)},
        )

    async def async_step_compatibility(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Whether to publish under EMHASS's own entity ids.

        Its own menu entry rather than a field on an existing step: every other
        entry here describes the house -- its battery, its prices, its
        inverter -- while this one only changes how the integration presents
        itself to Home Assistant. Folding it into `grid`, the nearest thing to
        a miscellaneous step, would make that step a junk drawer.
        """
        options = dict(self.config_entry.options)
        if user_input is not None:
            options[CONF_EMHASS_STANDARD_NAMES] = user_input[CONF_EMHASS_STANDARD_NAMES]
            return self.async_create_entry(data=options)

        taken = async_taken_standard_ids(self.hass, self.config_entry)
        return self.async_show_form(
            step_id="compatibility",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMHASS_STANDARD_NAMES,
                        default=options.get(CONF_EMHASS_STANDARD_NAMES, False),
                    ): selector.BooleanSelector()
                }
            ),
            description_placeholders={
                "conflicts": (
                    "\n\n**These ids are already taken**, most likely by EMHASS's own "
                    "publishing. Those sensors would keep their current names until you "
                    "turn that off:\n" + "\n".join(f"- `{entity_id}`" for entity_id in taken)
                )
                if taken
                else ""
            },
        )

    async def async_step_load(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the load forecast source, e.g. switching to/from mlforecaster.

        Unlike inverter/temperature, a load source is mandatory -- there is no
        "leave it unset" branch here.
        """
        options = dict(self.config_entry.options)
        profiles = (await async_load_profiles(self.hass)).profiles
        choices = available_profiles(self.hass, profiles, PROFILE_KIND_LOAD)

        if user_input is not None:
            if user_input[CONF_PROFILE] == LOAD_PROFILE_CREATE_SENTINEL:
                self._load_profiles = profiles
                return await self.async_step_load_create()
            self._load_key = user_input[CONF_PROFILE]
            self._load_profiles = profiles
            return await self.async_step_load_options()

        load = options.get(CONF_LOAD) or {}
        current = load.get(CONF_PROFILE)
        if current == PROFILE_KEY_LOAD_SENSOR and "entity" not in (
            load.get(CONF_PROFILE_OPTIONS) or {}
        ):
            # "Create a house load sensor" also stores profile "load/sensor" (its
            # "entity" is resolved dynamically instead, see async_step_load_create),
            # so profile alone can't tell the two apart -- without this, this step
            # would always preselect "House load sensor (without deferrables)" even
            # when the entry was actually built through the create flow.
            current = LOAD_PROFILE_CREATE_SENTINEL
        return self.async_show_form(
            step_id="load",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROFILE, description={"suggested_value": current}
                    ): _load_profile_selector(choices)
                }
            ),
        )

    async def async_step_load_create(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Build "load/sensor" against a sensor this integration creates.

        Goes on to :meth:`async_step_load_create_options` rather than
        :meth:`async_step_load_options` -- see the matching step on the setup
        flow for why "entity" is left out of the form that shows.
        """
        if user_input is not None:
            self._house_load_total_entity = user_input[CONF_HOUSE_LOAD_TOTAL_ENTITY]
            return await self.async_step_load_create_options()

        current = self.config_entry.options.get(CONF_HOUSE_LOAD_TOTAL_ENTITY)
        return self.async_show_form(
            step_id="load_create",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOUSE_LOAD_TOTAL_ENTITY, description={"suggested_value": current}
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", device_class="power")
                    )
                }
            ),
        )

    async def async_step_load_create_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        profile = self._load_profiles[PROFILE_KEY_LOAD_SENSOR]
        schema = profile.selector_schema(skip={"entity"})

        options = dict(self.config_entry.options)
        options[CONF_HOUSE_LOAD_TOTAL_ENTITY] = self._house_load_total_entity

        if user_input is not None or not schema:
            options[CONF_LOAD] = {
                CONF_PROFILE: PROFILE_KEY_LOAD_SENSOR,
                CONF_PROFILE_OPTIONS: user_input
                or _default_profile_options(profile, skip={"entity"}),
            }
            return self.async_create_entry(data=options)

        stored = (options.get(CONF_LOAD) or {}).get(CONF_PROFILE_OPTIONS) or {}
        return self.async_show_form(
            step_id="load_create_options",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(schema), stored),
            description_placeholders={
                "profile": profile.name,
                "notes": _profile_notes(profile),
            },
        )

    async def async_step_load_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        profile = self._load_profiles[self._load_key]

        if user_input is not None or not profile.options:
            options[CONF_LOAD] = {
                CONF_PROFILE: self._load_key,
                CONF_PROFILE_OPTIONS: user_input or {},
            }
            return self.async_create_entry(data=options)

        stored = (options.get(CONF_LOAD) or {}).get(CONF_PROFILE_OPTIONS) or {}
        return self.async_show_form(
            step_id="load_options",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(profile.selector_schema()), stored
            ),
            description_placeholders={
                "profile": profile.name,
                "notes": _profile_notes(profile),
            },
        )

    async def async_step_pv(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the solar forecast source, or just retune the current one.

        Set once at setup until now, which left an existing install with no
        way to pick up a profile template's newer defaults -- Solcast's day-3
        sensor among them, which End SOC's Optimized mode wants so it can see
        the next solar day past the horizon (see terminal.py).
        """
        options = dict(self.config_entry.options)
        profiles = (await async_load_profiles(self.hass)).profiles
        choices = available_profiles(self.hass, profiles, PROFILE_KIND_PV)
        if not choices:
            # Should not happen: every kind ships an always-available profile.
            return self.async_abort(reason="no_profiles")

        if user_input is not None:
            self._pv_key = user_input[CONF_PROFILE]
            self._pv_profiles = profiles
            return await self.async_step_pv_options()

        current = (options.get(CONF_PV) or {}).get(CONF_PROFILE)
        ranked = _rank_profiles(choices, PV_PROFILE_ORDER)
        return self.async_show_form(
            step_id="pv",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROFILE, description={"suggested_value": current}
                    ): _profile_selector(choices, PV_PROFILE_ORDER)
                }
            ),
            description_placeholders={
                "profiles": "\n".join(
                    f"- **{profile.name}** — {profile.description or ''}" for profile in ranked
                )
            },
        )

    async def async_step_pv_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        profile = self._pv_profiles[self._pv_key]

        if user_input is not None or not profile.options:
            options[CONF_PV] = {
                CONF_PROFILE: self._pv_key,
                CONF_PROFILE_OPTIONS: user_input or {},
            }
            return self.async_create_entry(data=options)

        # Suggest what is stored, not the template's defaults: a changed
        # default (day 3 arriving in the Solcast profile) is an offer, not
        # something to apply behind the user's back. Switching to a different
        # profile has nothing to carry over, so that starts from its defaults.
        stored = (options.get(CONF_PV) or {}).get(CONF_PROFILE_OPTIONS) or {}
        if (options.get(CONF_PV) or {}).get(CONF_PROFILE) != self._pv_key:
            stored = {}
        return self.async_show_form(
            step_id="pv_options",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(profile.selector_schema()), stored
            ),
            description_placeholders={
                "profile": profile.name,
                "notes": _profile_notes(profile),
            },
        )

    async def async_step_temperature(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose where the outdoor temperature forecast comes from.

        Only consulted once a thermal load exists -- the forecast is what the
        thermal model cools towards.
        """
        options = dict(self.config_entry.options)
        profiles = (await async_load_profiles(self.hass)).profiles
        choices = available_profiles(self.hass, profiles, PROFILE_KIND_TEMPERATURE)

        if user_input is not None:
            self._temperature_key = user_input[CONF_PROFILE]
            self._temperature_profiles = profiles
            return await self.async_step_temperature_options()

        current = (options.get(CONF_TEMPERATURE) or {}).get(CONF_PROFILE)
        ranked = _rank_profiles(choices, TEMPERATURE_PROFILE_ORDER)
        return self.async_show_form(
            step_id="temperature",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROFILE, description={"suggested_value": current}
                    ): _profile_selector(choices, TEMPERATURE_PROFILE_ORDER)
                }
            ),
            description_placeholders={
                "profiles": "\n".join(
                    f"- **{profile.name}** — {profile.description or ''}" for profile in ranked
                )
            },
        )

    async def async_step_temperature_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        profile = self._temperature_profiles[self._temperature_key]

        if user_input is not None or not profile.options:
            options[CONF_TEMPERATURE] = {
                CONF_PROFILE: self._temperature_key,
                CONF_PROFILE_OPTIONS: user_input or {},
            }
            return self.async_create_entry(data=options)

        stored = (options.get(CONF_TEMPERATURE) or {}).get(CONF_PROFILE_OPTIONS) or {}
        return self.async_show_form(
            step_id="temperature_options",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(profile.selector_schema()), stored
            ),
            description_placeholders={
                "profile": profile.name,
                "notes": _profile_notes(profile),
            },
        )

    async def async_step_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how the battery is actually commanded.

        Optional. Without a profile the integration only ever reads, and the
        plan is yours to act on however you like. Advanced users can add a
        YAML profile of their own for an inverter that has no built-in
        profile, rather than being limited to the shipped choices.
        """
        options = dict(self.config_entry.options)
        profiles = (await async_load_profiles(self.hass)).profiles
        choices = available_profiles(self.hass, profiles, PROFILE_KIND_INVERTER)

        if user_input is not None:
            key = user_input.get(CONF_PROFILE)
            if not key:
                options[CONF_INVERTER] = {}
                return self.async_create_entry(data=options)
            self._inverter_key = key
            self._inverter_profiles = profiles
            return await self.async_step_inverter_options()

        current = (options.get(CONF_INVERTER) or {}).get(CONF_PROFILE)
        return self.async_show_form(
            step_id="inverter",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PROFILE, description={"suggested_value": current}
                    ): _inverter_profile_selector(choices)
                }
            ),
        )

    async def async_step_inverter_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        profile = self._inverter_profiles[self._inverter_key]

        if user_input is not None or not profile.options:
            options[CONF_INVERTER] = {
                CONF_PROFILE: self._inverter_key,
                CONF_PROFILE_OPTIONS: user_input or {},
            }
            return self.async_create_entry(data=options)

        # What the user already answered wins; the profile's own suggestions
        # fill the rest. Having named their inverter, they should be reading a
        # filled-in form rather than typing entity ids.
        stored = (options.get(CONF_INVERTER) or {}).get(CONF_PROFILE_OPTIONS) or {}
        prefill = {**_suggested_entities(self.hass, profile), **stored}
        return self.async_show_form(
            step_id="inverter_options",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(profile.selector_schema()), prefill
            ),
            description_placeholders={
                "profile": profile.name,
                "notes": _profile_notes(profile),
            },
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        if user_input is not None:
            options["battery"] = _battery_storage_from_input(user_input)
            options[CONF_SOC_ENTITY] = user_input.get(CONF_SOC_ENTITY) or None
            options[CONF_BATTERY_POWER_ENTITY] = user_input.get(CONF_BATTERY_POWER_ENTITY) or None
            options[CONF_BATTERY_POWER_INVERT] = bool(user_input.get(CONF_BATTERY_POWER_INVERT))
            options[CONF_PV_ENTITY] = user_input.get(CONF_PV_ENTITY) or None
            return self.async_create_entry(data=options)

        defaults = {
            **options.get("battery", {}),
            CONF_SOC_ENTITY: options.get(CONF_SOC_ENTITY) or "",
            CONF_BATTERY_POWER_ENTITY: options.get(CONF_BATTERY_POWER_ENTITY) or "",
            CONF_BATTERY_POWER_INVERT: bool(options.get(CONF_BATTERY_POWER_INVERT)),
            CONF_PV_ENTITY: options.get(CONF_PV_ENTITY) or "",
        }
        return self.async_show_form(
            step_id="battery", data_schema=vol.Schema(battery_schema(defaults))
        )

    async def async_step_grid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        stored = options.get("grid", {})
        if user_input is not None:
            options["grid"] = _collect_grid(user_input)
            options.update(
                {
                    key: user_input[key]
                    for key in (
                        CONF_TIME_STEP,
                        CONF_MPC_INTERVAL,
                        CONF_HORIZON_HOURS,
                        CONF_DAYAHEAD_FALLBACK_TIME,
                    )
                }
            )
            return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="grid",
            data_schema=vol.Schema(grid_schema({**options, **stored})),
        )

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        if user_input is not None:
            options["tariff"] = _collect_tariff(user_input)
            return self.async_create_entry(data=options)

        tariff = options.get("tariff", {})
        schema = {
            **_tariff_side_schema("buy", tariff.get("buy", {})),
            **_tariff_side_schema("sell", tariff.get("sell", {})),
        }
        return self.async_show_form(step_id="tariff", data_schema=vol.Schema(schema))
