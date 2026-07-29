"""Config and options flow.

The flow is split into short, single-topic steps. The complaint this
integration exists to answer is that EMHASS presents every option at once with
no indication of which apply to a given house; asking a handful of questions at
a time, and only ever offering data sources that are actually installed, is the
substance of that answer.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlowWithReload,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import EmhassClient, EmhassError
from .const import (
    CONF_ADDER,
    CONF_CONTROL_ENTITY,
    CONF_DAYAHEAD_FALLBACK_TIME,
    CONF_EARLIEST_START,
    CONF_HORIZON_HOURS,
    CONF_INVERTER,
    CONF_LATEST_END,
    CONF_LOAD,
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
    CONF_SEMI_CONTINUOUS,
    CONF_SINGLE_CONSTANT,
    CONF_SOC_ENTITY,
    CONF_STARTUP_PENALTY,
    CONF_TEMPLATE,
    CONF_TIME_STEP,
    CONF_URL,
    DEFAULT_DAYAHEAD_FALLBACK_TIME,
    DEFAULT_GRID_EXPORT_MAX,
    DEFAULT_GRID_IMPORT_MAX,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_MPC_INTERVAL,
    DEFAULT_SOC_MAX,
    DEFAULT_SOC_MIN,
    DEFAULT_SOC_TARGET,
    DEFAULT_TIME_STEP,
    DEFAULT_URL,
    DOMAIN,
    MIN_EMHASS_VERSION,
    PROFILE_KIND_INVERTER,
    PROFILE_KIND_LOAD,
    PROFILE_KIND_PRICE,
    PROFILE_KIND_PV,
    SUBENTRY_TYPE_DEFERRABLE,
)
from .profiles import (
    Profile,
    ProfileError,
    async_load_profiles,
    async_resolve_series,
    available_profiles,
)
from .tariff import MODE_LINEAR, MODE_PASSTHROUGH, MODE_TEMPLATE
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
    """Best-effort discovery of a locally installed EMHASS add-on."""
    try:
        from homeassistant.components.hassio import (
            AddonError,
            AddonManager,
            AddonState,
            is_hassio,
        )
    except ImportError:  # pragma: no cover - not a supervised install
        return None

    if not is_hassio(hass):
        return None

    from .const import ADDON_SLUG, DEFAULT_PORT

    manager = AddonManager(hass, _LOGGER, "EMHASS", ADDON_SLUG)
    try:
        info = await manager.async_get_addon_info()
    except AddonError as err:
        _LOGGER.debug("EMHASS add-on not available: %s", err)
        return None

    if info.state != AddonState.RUNNING or not info.hostname:
        return None
    return f"http://{info.hostname}:{DEFAULT_PORT}"


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


def _profile_selector(profiles: list[Profile]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            mode=selector.SelectSelectorMode.LIST,
            options=[
                selector.SelectOptionDict(value=profile.key, label=profile.name)
                for profile in profiles
            ],
        )
    )


def _tariff_side_schema(prefix: str, defaults: dict[str, Any]) -> dict[Any, Any]:
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
            f"{prefix}_{CONF_MULTIPLIER}", default=defaults.get(CONF_MULTIPLIER, 1.0)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10, step="any", mode="box")
        ),
        vol.Optional(
            f"{prefix}_{CONF_ADDER}", default=defaults.get(CONF_ADDER, 0.0)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=-100, max=100, step="any", mode="box")
        ),
        vol.Optional(
            f"{prefix}_{CONF_TEMPLATE}", default=defaults.get(CONF_TEMPLATE, "")
        ): selector.TemplateSelector(),
    }


def _collect_tariff(user_input: dict[str, Any]) -> dict[str, Any]:
    tariff: dict[str, Any] = {}
    for side in ("buy", "sell"):
        tariff[side] = {
            CONF_MODE: user_input[f"{side}_{CONF_MODE}"],
            CONF_MULTIPLIER: user_input.get(f"{side}_{CONF_MULTIPLIER}", 1.0),
            CONF_ADDER: user_input.get(f"{side}_{CONF_ADDER}", 0.0),
            CONF_TEMPLATE: user_input.get(f"{side}_{CONF_TEMPLATE}") or None,
        }
    return tariff


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
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_URL: url, "emhass_version": version}
                )
                return self.async_abort(reason="reconfigure_successful")
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
        return await self._async_profile_step(PROFILE_KIND_LOAD, CONF_LOAD, "load", user_input)

    async def async_step_load_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_profile_options_step(CONF_LOAD, "load_options", user_input)

    # -- plant ----------------------------------------------------------------

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._options["battery"] = {
                key: value for key, value in user_input.items() if key != CONF_SOC_ENTITY
            }
            if soc := user_input.get(CONF_SOC_ENTITY):
                self._options[CONF_SOC_ENTITY] = soc
            return await self.async_step_grid()

        return self.async_show_form(step_id="battery", data_schema=vol.Schema(battery_schema({})))

    async def async_step_grid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options["grid"] = {
                "grid_import_max_w": user_input["grid_import_max_w"],
                "grid_export_max_w": user_input["grid_export_max_w"],
            }
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
            return self.async_create_entry(
                title="EMHASS Companion", data=self._data, options=self._options
            )

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

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required(CONF_PROFILE): _profile_selector(choices)}),
            description_placeholders={
                "profiles": "\n".join(
                    f"- **{profile.name}** — {profile.description or ''}" for profile in choices
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
                "notes": profile.notes or "",
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
        setting can offer.
        """
        return {SUBENTRY_TYPE_DEFERRABLE: DeferrableLoadSubentryFlow}


def deferrable_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    """Fields for adding or reconfiguring a deferrable load.

    Only set-once values live here. Everything a user might change from day to
    day -- power, hours, the time window, the override mode -- is an entity, so
    that changing it does not reload the config entry.
    """
    return {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): selector.TextSelector(),
        vol.Required(
            CONF_NOMINAL_POWER, default=defaults.get(CONF_NOMINAL_POWER, 2000)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=100000, step=10, unit_of_measurement="W", mode="box"
            )
        ),
        vol.Required(
            CONF_OPERATING_HOURS, default=defaults.get(CONF_OPERATING_HOURS, 2)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=24, step=0.25, unit_of_measurement="h", mode="box"
            )
        ),
        vol.Optional(
            CONF_EARLIEST_START, default=defaults.get(CONF_EARLIEST_START, "")
        ): selector.TimeSelector(),
        vol.Optional(
            CONF_LATEST_END, default=defaults.get(CONF_LATEST_END, "")
        ): selector.TimeSelector(),
        vol.Required(
            CONF_SEMI_CONTINUOUS, default=defaults.get(CONF_SEMI_CONTINUOUS, True)
        ): selector.BooleanSelector(),
        vol.Required(
            CONF_SINGLE_CONSTANT, default=defaults.get(CONF_SINGLE_CONSTANT, False)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_STARTUP_PENALTY, default=defaults.get(CONF_STARTUP_PENALTY, 0)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=100, step="any", mode="box")
        ),
        vol.Optional(
            CONF_POWER_SENSOR, default=defaults.get(CONF_POWER_SENSOR, "")
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power")
        ),
        vol.Optional(
            CONF_CONTROL_ENTITY, default=defaults.get(CONF_CONTROL_ENTITY, "")
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "input_boolean", "script"])
        ),
    }


def _clean_deferrable(user_input: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional fields so absent means "unset", not "midnight"."""
    return {key: value for key, value in user_input.items() if value not in (None, "")}


class DeferrableLoadSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure one deferrable load."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            data = _clean_deferrable(user_input)
            return self.async_create_entry(title=data[CONF_NAME], data=data)

        return self.async_show_form(step_id="user", data_schema=vol.Schema(deferrable_schema({})))

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            data = _clean_deferrable(user_input)
            return self.async_update_and_abort(
                self._get_entry(), subentry, title=data[CONF_NAME], data=data
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(deferrable_schema(dict(subentry.data))),
        )


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
        vol.Optional(
            "soc_min", default=defaults.get("soc_min", DEFAULT_SOC_MIN)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=1, step=0.01, mode="slider")
        ),
        vol.Optional(
            "soc_max", default=defaults.get("soc_max", DEFAULT_SOC_MAX)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=1, step=0.01, mode="slider")
        ),
        vol.Optional(
            "soc_target", default=defaults.get("soc_target", DEFAULT_SOC_TARGET)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=1, step=0.01, mode="slider")
        ),
        vol.Optional(
            CONF_SOC_ENTITY, default=defaults.get(CONF_SOC_ENTITY, "")
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="battery")
        ),
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

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["battery", "grid", "tariff", "inverter"],
        )

    async def async_step_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how the battery is actually commanded.

        Optional. Without a profile the integration only ever reads, and the
        plan is yours to act on however you like.
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
                    ): _profile_selector(choices)
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

        stored = (options.get(CONF_INVERTER) or {}).get(CONF_PROFILE_OPTIONS) or {}
        return self.async_show_form(
            step_id="inverter_options",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(profile.selector_schema()), stored
            ),
            description_placeholders={
                "profile": profile.name,
                "notes": profile.notes or "",
            },
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        if user_input is not None:
            options["battery"] = {
                key: value for key, value in user_input.items() if key != CONF_SOC_ENTITY
            }
            options[CONF_SOC_ENTITY] = user_input.get(CONF_SOC_ENTITY) or None
            return self.async_create_entry(data=options)

        defaults = {
            **options.get("battery", {}),
            CONF_SOC_ENTITY: options.get(CONF_SOC_ENTITY) or "",
        }
        return self.async_show_form(
            step_id="battery", data_schema=vol.Schema(battery_schema(defaults))
        )

    async def async_step_grid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        if user_input is not None:
            options["grid"] = {
                "grid_import_max_w": user_input["grid_import_max_w"],
                "grid_export_max_w": user_input["grid_export_max_w"],
            }
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
            data_schema=vol.Schema(grid_schema({**options, **options.get("grid", {})})),
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
