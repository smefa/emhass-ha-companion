// EMHASS Companion dashboard strategy.
//
// Registered automatically as a frontend module at startup, like the cards.
// Home Assistant 2026.5+ lists it under Settings -> Dashboards -> Add dashboard
// -> Community dashboards -> "EMHASS Companion". Older versions can use it from
// YAML:
//     strategy:
//       type: custom:emhass
//
// The dashboard is generated, not stored: it finds this integration's entities
// through hass.entities (the frontend's compact entity registry, readable by
// non-admin users too) and lays out the cards that exist for them, one view per
// topic, with a card per deferrable load -- so a new load shows up without
// editing anything.
//
// Unlike the cards, this file is loaded on every page (add_extra_js_url) rather
// than as a Lovelace resource: the "Add dashboard" dialog lives in the settings
// panel, where resources are not loaded. That is also why it imports nothing --
// it is paid for on every page load, so it stays small and self-contained.

const PLATFORM = "emhass_companion";

// Registry translation_keys -- matched instead of entity ids, which are built
// from the translated name and change when a user renames an entity.
const LOAD_MARKER_KEY = "should_run"; // only a per-load device has this one

function deviceName(hass, deviceId) {
  const device = hass.devices && deviceId && hass.devices[deviceId];
  return (device && (device.name_by_user || device.name)) || "";
}

/** Everything of ours in the registry: the total, and each load's device. */
function discover(hass) {
  const loads = new Map();
  let total = 0;

  Object.keys(hass.entities || {}).forEach((entityId) => {
    const info = hass.entities[entityId];
    if (info.platform !== PLATFORM) return;
    total += 1;
    if (info.device_id && info.translation_key === LOAD_MARKER_KEY) {
      loads.set(info.device_id, deviceName(hass, info.device_id) || entityId);
    }
  });

  return {
    total,
    // [device id, name]: the card is given the id, because two loads may share a
    // name and the card resolves a name to the first match.
    loads: [...loads.entries()].sort((a, b) => a[1].localeCompare(b[1])),
  };
}

/**
 * A view that spans the whole screen. A panel view shows exactly one card, edge to
 * edge, so several cards are stacked inside a single vertical-stack.
 */
function fullWidthView(title, path, icon, cards) {
  return {
    title,
    path,
    icon,
    panel: true,
    cards: cards.length === 1 ? cards : [{ type: "vertical-stack", cards }],
  };
}

class EmhassDashboardStrategy extends HTMLElement {
  static getCreateSuggestions(_hass) {
    return { title: "EMHASS Companion", icon: "mdi:solar-power-variant" };
  }

  static async generate(config, hass) {
    const title = config.title || "EMHASS Companion";
    const found = discover(hass);

    if (!found.total) {
      return {
        title,
        views: [
          {
            title: "EMHASS Companion",
            cards: [
              {
                type: "markdown",
                content:
                  "No EMHASS Companion entities found. Add the integration under Settings -> Devices & services.",
              },
            ],
          },
        ],
      };
    }

    const views = [
      fullWidthView("Overview", "overview", "mdi:view-dashboard", [
        { type: "custom:emhass-overview-card" },
        { type: "custom:emhass-status-card" },
      ]),
      fullWidthView("Plan", "plan", "mdi:chart-timeline-variant", [
        { type: "custom:emhass-plan-card" },
      ]),
    ];

    if (found.loads.length) {
      views.push(
        fullWidthView(
          "Loads",
          "loads",
          "mdi:power-plug",
          found.loads.map(([id]) => ({ type: "custom:emhass-deferrable-card", load: id })),
        ),
      );
    }

    // The tariff card is always listed: without a network profile it draws a
    // ribboned sample instead of going blank, which is how it tells the user what
    // to configure.
    views.push(
      fullWidthView("Economy", "economy", "mdi:cash", [
        { type: "custom:emhass-economy-card" },
        { type: "custom:emhass-tariff-card" },
      ]),
    );

    views.push(
      fullWidthView("Health", "health", "mdi:heart-pulse", [{ type: "custom:emhass-health-card" }]),
    );

    return { title, views };
  }
}

customElements.define("ll-strategy-dashboard-emhass", EmhassDashboardStrategy);

window.customStrategies = window.customStrategies || [];
window.customStrategies.push({
  type: "emhass",
  strategyType: "dashboard",
  name: "EMHASS Companion",
  description: "The plan, every deferrable load, the economy and the health of the integration.",
});
