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
const COST_KEY = "energy_cost_today";
const SAVINGS_KEY = "savings_today";
const SOLAR_SAVINGS_KEY = "solar_savings_today"; // only with a solar meter
const BATTERY_SAVINGS_KEY = "battery_savings_today";

// How long to wait for apexcharts-card to register before falling back to the
// built-in statistics graph. Its resource may still be loading when the
// dashboard is first generated.
const APEX_WAIT_MS = 1500;

function deviceName(hass, deviceId) {
  const device = hass.devices && deviceId && hass.devices[deviceId];
  return (device && (device.name_by_user || device.name)) || "";
}

/** Everything of ours in the registry: the total, and each load's device. */
function discover(hass) {
  const loads = new Map();
  const money = {};
  let total = 0;

  Object.keys(hass.entities || {}).forEach((entityId) => {
    const info = hass.entities[entityId];
    if (info.platform !== PLATFORM) return;
    total += 1;
    if (info.device_id && info.translation_key === LOAD_MARKER_KEY) {
      loads.set(info.device_id, deviceName(hass, info.device_id) || entityId);
    }
    if (info.translation_key) money[info.translation_key] = entityId;
  });

  return {
    total,
    money,
    // [device id, name]: the card is given the id, because two loads may share a
    // name and the card resolves a name to the first match.
    loads: [...loads.entries()].sort((a, b) => a[1].localeCompare(b[1])),
  };
}

/**
 * A view with one card: a panel view shows exactly one card, edge to edge. The
 * cards cap their own width (--emh-max-width), so it sits centred on a big screen.
 */
function singleCardView(title, path, icon, card) {
  return { title, path, icon, panel: true, cards: [card] };
}

/**
 * A view with several cards, in Home Assistant's default masonry layout: they sit
 * side by side in columns on a wide screen and stack on a narrow one.
 */
function masonryView(title, path, icon, cards) {
  return { title, path, icon, cards };
}

/** True if apexcharts-card (a HACS card, not ours) is installed. */
async function apexAvailable() {
  if (customElements.get("apexcharts-card")) return true;
  return Promise.race([
    customElements.whenDefined("apexcharts-card").then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), APEX_WAIT_MS)),
  ]);
}

/**
 * Whole days from 1 January to now, so "this year" matches apexcharts' calendar
 * year. Rounded down: rounding up would start the window in December.
 */
function daysSinceNewYear() {
  const now = new Date();
  const start = new Date(now.getFullYear(), 0, 1);
  return Math.max(1, Math.floor((now - start) / 86400000));
}

/**
 * One money chart over Home Assistant's own long-term statistics. With
 * apexcharts-card it is drawn the same way as the user's hand-made economy
 * dashboard; without it, the built-in statistics-graph card shows the same
 * monthly change.
 */
function trendChart(apex, title, series, { span, stacked = false, line = false }) {
  if (apex) {
    return {
      type: "custom:apexcharts-card",
      header: { show: true, title },
      graph_span: span === "year" ? "1year" : "3year",
      ...(span === "year" ? { span: { start: "year" } } : {}),
      ...(stacked ? { stacked: true } : {}),
      series: series.map(([entity, name]) => ({
        entity,
        name,
        type: line ? "line" : "column",
        // "change", not "sum": sum is the running total, change is each month.
        statistics: { type: "change", period: "month", align: "start" },
      })),
    };
  }
  return {
    type: "statistics-graph",
    title,
    chart_type: line ? "line" : "bar",
    period: "month",
    days_to_show: span === "year" ? daysSinceNewYear() : 1095,
    stat_types: ["change"],
    entities: series.map(([entity]) => entity),
  };
}

/** Cost vs. savings and where the savings came from, by month and over years. */
function trendCards(apex, money) {
  const cost = money[COST_KEY];
  const savings = money[SAVINGS_KEY];
  const solar = money[SOLAR_SAVINGS_KEY];
  const battery = money[BATTERY_SAVINGS_KEY];
  const costVsSavings = [
    cost && [cost, "Cost"],
    savings && [savings, "Savings"],
  ].filter(Boolean);
  if (!costVsSavings.length) return [];

  const cards = [
    trendChart(apex, "This year, cost vs. savings, by month", costVsSavings, { span: "year" }),
  ];
  if (solar && battery) {
    cards.push(
      trendChart(
        apex,
        "Savings by source, by month",
        [
          [solar, "Solar"],
          [battery, "Battery"],
        ],
        { span: "year", stacked: true },
      ),
    );
  }
  cards.push(trendChart(apex, "Multi-year trend", costVsSavings, { span: "years", line: true }));
  cards.push({
    type: "markdown",
    content:
      "Bars come from Home Assistant's own long-term statistics. Exact per-day figures, and totals for any custom range, are on the **Statistics** page (Developer tools -> Statistics).",
  });
  return cards;
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
      masonryView("Overview", "overview", "mdi:view-dashboard", [
        { type: "custom:emhass-overview-card" },
        { type: "custom:emhass-status-card" },
      ]),
      singleCardView("Plan", "plan", "mdi:chart-timeline-variant", {
        type: "custom:emhass-plan-card",
      }),
    ];

    if (found.loads.length) {
      views.push(
        masonryView(
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
      masonryView("Economy", "economy", "mdi:cash", [
        { type: "custom:emhass-economy-card" },
        { type: "custom:emhass-tariff-card" },
      ]),
    );

    // Only wait for apexcharts when there is a money chart to draw.
    const hasMoney = Boolean(found.money[COST_KEY] || found.money[SAVINGS_KEY]);
    const trends = trendCards(hasMoney && (await apexAvailable()), found.money);
    if (trends.length) {
      views.push(masonryView("Trends", "trends", "mdi:chart-line", trends));
    }

    views.push(
      singleCardView("Health", "health", "mdi:heart-pulse", {
        type: "custom:emhass-health-card",
      }),
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
