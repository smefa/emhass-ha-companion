/**
 * The daily savings ledger, forecast and a monthly trend, in one card -- what
 * dashboards/economy.yaml otherwise assembles from three third-party cards
 * (mushroom, apexcharts) and two markdown templates. See planning notes in
 * the PR that introduced this file.
 */

import {
  CardEditor,
  LiveCard,
  balanceText,
  cleanSections,
  findHub,
  formatEnergy,
  formatHour,
  formatTime,
  isUsable,
  loadHaForm,
  moreInfo,
  num,
  sectionGrid,
  stateOf,
  statTile,
  svg,
  tag,
  valueBox,
} from "./emhass-core.js?v=__VERSION__";

/** "12.34 SEK", or "-" when the sensor has nothing usable yet. */
function formatMoney(stateObj) {
  if (!isUsable(stateObj)) return "–";
  const unit = stateObj.attributes ? stateObj.attributes.unit_of_measurement : null;
  return `${Number(stateObj.state).toFixed(2)}${unit ? ` ${unit}` : ""}`;
}

/**
 * Everything the card renders, read once per update from the hub's entities.
 *
 * One shape for every section, computed up front, so the render functions
 * below never touch `hass` directly -- the same split `tariffView` uses.
 */
function economyView(hass, hub) {
  const costState = stateOf(hass, hub["sensor.energy_cost_today"]);
  const savingsState = stateOf(hass, hub["sensor.savings_today"]);
  const solarState = stateOf(hass, hub["sensor.solar_savings_today"]);
  const batteryState = stateOf(hass, hub["sensor.battery_savings_today"]);
  const forecastCostState = stateOf(hass, hub["sensor.forecast_cost_24h"]);
  const forecastSavingsState = stateOf(hass, hub["sensor.forecast_savings_24h"]);

  const savingsAttrs = savingsState && savingsState.attributes ? savingsState.attributes : {};
  const batteryAttrs = batteryState && batteryState.attributes ? batteryState.attributes : {};
  const forecastAttrs = forecastCostState && forecastCostState.attributes ? forecastCostState.attributes : {};

  return {
    tiles: { cost: costState, savings: savingsState, solar: solarState, battery: batteryState },
    breakdown: {
      solar: isUsable(solarState) ? num(solarState) : NaN,
      battery: isUsable(batteryState) ? num(batteryState) : NaN,
      unit: costState && costState.attributes ? costState.attributes.unit_of_measurement : null,
    },
    batteryDetail: {
      chargePrice: Number(batteryAttrs.average_charge_price),
      dischargePrice: Number(batteryAttrs.average_discharge_price),
      roundTripLoss: Number(batteryAttrs.round_trip_loss_kwh),
      selfSufficiency: Number(savingsAttrs.self_sufficiency_percent),
      currency: batteryState && batteryState.attributes ? batteryState.attributes.unit_of_measurement : null,
    },
    checking: {
      unpriced: Number(savingsAttrs.unpriced_kwh),
      residual: Number(savingsAttrs.balance_residual_kwh),
    },
    forecast: {
      cost: forecastCostState,
      savings: forecastSavingsState,
      hourly: Array.isArray(forecastAttrs.hourly_cost) ? forecastAttrs.hourly_cost : [],
      unit: forecastCostState && forecastCostState.attributes ? forecastCostState.attributes.unit_of_measurement : null,
    },
    entities: {
      cost: hub["sensor.energy_cost_today"],
      savings: hub["sensor.savings_today"],
      solar: hub["sensor.solar_savings_today"],
      battery: hub["sensor.battery_savings_today"],
      forecastCost: hub["sensor.forecast_cost_24h"],
      forecastSavings: hub["sensor.forecast_savings_24h"],
    },
  };
}

/* --------------------------------------------------------- savings breakdown */

function renderBreakdown(ui, breakdown) {
  if (!ui.breakdown) return;
  // Raw, unclamped figures for anything the user reads as text -- a
  // component that lost money must say so, not disappear into "0.00" and
  // read as "did nothing".
  const solarRaw = Number.isFinite(breakdown.solar) ? breakdown.solar : 0;
  const batteryRaw = Number.isFinite(breakdown.battery) ? breakdown.battery : 0;
  const rawTotal = solarRaw + batteryRaw;

  // The bar itself can only show two non-negative segments, so a negative
  // component is floored at zero for width purposes only.
  const solarBar = Math.max(solarRaw, 0);
  const batteryBar = Math.max(batteryRaw, 0);
  const barTotal = solarBar + batteryBar;
  const solarPct = barTotal > 0 ? (solarBar / barTotal) * 100 : 50;
  ui.segSolar.style.width = `${solarPct}%`;
  ui.segBattery.style.width = `${barTotal > 0 ? 100 - solarPct : 50}%`;
  ui.breakdown.classList.toggle("empty-bar", barTotal <= 0);

  const unit = breakdown.unit ? ` ${breakdown.unit}` : "";
  const solarText = `Solar ${solarRaw.toFixed(2)}${unit}`;
  const batteryText = `Battery ${batteryRaw.toFixed(2)}${unit}`;
  ui.legendSolar.textContent = solarText;
  ui.legendBattery.textContent = batteryText;
  const solarTitle = `${solarText} -- ${rawTotal > 0 ? (solarRaw / rawTotal * 100).toFixed(0) + "% of today's savings" : "today's savings are negative"}`;
  const batteryTitle = `${batteryText} -- ${rawTotal > 0 ? (batteryRaw / rawTotal * 100).toFixed(0) + "% of today's savings" : "today's savings are negative"}`;
  ui.segSolar.title = solarTitle;
  ui.legendSolar.title = solarTitle;
  ui.segBattery.title = batteryTitle;
  ui.legendBattery.title = batteryTitle;
}

/* -------------------------------------------------------------- hourly bars */

/**
 * One bar per forecast hour, above or below a zero line -- an hour that nets
 * income (selling more than it buys) reads the same colour as a savings
 * figure everywhere else on the card, not as a second kind of cost.
 */
function hourlyBars(points, hass, unit) {
  const height = 64;
  const totalH = height + 14;
  const root = svg("svg", { viewBox: `0 0 1000 ${totalH}`, preserveAspectRatio: "none", role: "img" });
  root.style.height = `${totalH}px`;
  if (points.length < 1) return root;

  const stepMs = points.length > 1 ? points[1].t - points[0].t : 3600000;
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t + stepMs;
  const x = (t) => ((t - t0) / (t1 - t0 || 1)) * 1000;
  const zero = height / 2;
  const maxAbs = Math.max(...points.map((p) => Math.abs(p.v)), 0.001);
  const scale = (zero - 4) / maxAbs;

  const hour = 3600000;
  const step = (t1 - t0) / hour > 18 ? 6 * hour : 3 * hour;
  const first = Math.ceil(t0 / step) * step;
  for (let t = first; t < t1; t += step) {
    svg("line", {
      x1: x(t), x2: x(t), y1: 0, y2: height,
      stroke: "var(--emh-hairline)", "stroke-width": 1, "stroke-dasharray": "2 2",
    }, root);
    svg("text", {
      x: x(t), y: totalH - 2, fill: "var(--emh-dim)", "font-size": 10, "text-anchor": "middle",
    }, root).textContent = formatHour(t, hass);
  }
  svg("line", { x1: 0, x2: 1000, y1: zero, y2: zero, stroke: "var(--emh-hairline)", "stroke-width": 1 }, root);

  const barW = Math.max((1000 / points.length) * 0.6, 2);
  for (const point of points) {
    if (!Number.isFinite(point.v)) continue;
    const cx = x(point.t) + (x(point.t + stepMs) - x(point.t)) / 2;
    const barH = Math.abs(point.v) * scale;
    const bar = svg("rect", {
      x: cx - barW / 2,
      y: point.v >= 0 ? zero - barH : zero,
      width: barW,
      height: Math.max(barH, 1),
      rx: 2,
      fill: point.v === 0 ? "var(--emh-dim)" : point.v > 0 ? "var(--emh-bad)" : "var(--emh-ok)",
      "fill-opacity": 0.85,
    }, root);
    const unitText = unit ? ` ${unit}` : "";
    bar.title = `${formatTime(point.t, hass)} – ${formatTime(point.t + stepMs, hass)}: ${point.v.toFixed(2)}${unitText}`;
  }

  const now = Date.now();
  if (now >= t0 && now <= t1) {
    svg("line", {
      x1: x(now), x2: x(now), y1: -2, y2: height + 2,
      stroke: "var(--primary-text-color)", "stroke-width": 2,
    }, root);
  }
  return root;
}

/* --------------------------------------------------------------- trend bars */

/** Cumulative `sum` rows from the statistics API, turned into one delta per period. */
function monthlyDeltas(rows) {
  const points = [];
  for (let i = 1; i < rows.length; i++) {
    const prev = Number(rows[i - 1].sum);
    const cur = Number(rows[i].sum);
    const start = Date.parse(rows[i].start);
    if (!Number.isFinite(prev) || !Number.isFinite(cur) || !Number.isFinite(start)) continue;
    points.push({ t: start, v: Math.max(cur - prev, 0) });
  }
  return points;
}

/**
 * Monthly balance vs. savings from Home Assistant's own long-term statistics.
 *
 * Fetched at most once every five minutes -- a month's own total does not
 * move within a day the way live state does, so `readHistory`'s one-minute
 * window would only be a wasted round trip here. Kept local to this file
 * rather than added to emhass-core.js: no other card reads statistics, and
 * core.js holds only what is actually shared.
 */
function readStatistics(card, hass, ids, now) {
  const names = Object.keys(ids);
  const empty = {};
  for (const name of names) empty[name] = [];
  if (typeof hass.callWS !== "function") return card._statsPoints || empty;

  const entityIds = [];
  for (const name of names) {
    if (ids[name] && entityIds.indexOf(ids[name]) === -1) entityIds.push(ids[name]);
  }
  if (!entityIds.length) return card._statsPoints || empty;

  const key = entityIds.join("|");
  const fresh = card._statsAt && now - card._statsAt < 300000 && card._statsKey === key;
  if (!fresh) {
    card._statsAt = now;
    card._statsKey = key;
    // One extra month back of range, dropped once diffed into deltas: the
    // first displayed month needs a `sum` from before it to subtract against,
    // or its own bar would read as "everything since this sensor existed."
    const start = new Date(now);
    start.setDate(1);
    start.setHours(0, 0, 0, 0);
    start.setMonth(start.getMonth() - 12);
    hass
      .callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        end_time: new Date(now).toISOString(),
        statistic_ids: entityIds,
        period: "month",
        types: ["sum"],
      })
      .then((result) => {
        const points = {};
        for (const name of names) {
          const rows = result && result[ids[name]];
          points[name] = monthlyDeltas(Array.isArray(rows) ? rows : []);
        }
        card._statsPoints = points;
        card.refresh();
      })
      .catch(() => {
        card._statsPoints = empty;
      });
  }
  return card._statsPoints || empty;
}

function monthlyBars(costPoints, savingsPoints, hass, unit) {
  const height = 78;
  const totalH = height + 16;
  const root = svg("svg", { viewBox: `0 0 1000 ${totalH}`, preserveAspectRatio: "none", role: "img" });
  root.style.height = `${totalH}px`;
  const months = costPoints.length >= savingsPoints.length ? costPoints : savingsPoints;
  if (!months.length) return root;

  const maxV = Math.max(1, ...costPoints.map((p) => p.v), ...savingsPoints.map((p) => p.v));
  const scale = height / maxV;
  const slot = 1000 / months.length;
  const barW = Math.max(slot * 0.32, 2);
  const language = hass && hass.locale ? hass.locale.language : undefined;

  svg("line", { x1: 0, x2: 1000, y1: height, y2: height, stroke: "var(--emh-hairline)", "stroke-width": 1 }, root);

  const unitText = unit ? ` ${unit}` : "";
  for (let i = 0; i < months.length; i++) {
    const cx = slot * i + slot / 2;
    const cost = costPoints[i] ? costPoints[i].v : 0;
    const savings = savingsPoints[i] ? savingsPoints[i].v : 0;
    const costH = cost * scale;
    const savingsH = savings * scale;
    const monthLabel = new Date(months[i].t).toLocaleDateString(language, { month: "short", year: "numeric" });
    const costBar = svg("rect", {
      x: cx - barW - 1, y: height - costH, width: barW, height: Math.max(costH, 1),
      rx: 2, fill: "var(--emh-bad)", "fill-opacity": 0.85,
    }, root);
    costBar.title = `${monthLabel} balance: ${cost.toFixed(2)}${unitText}`;
    const savingsBar = svg("rect", {
      x: cx + 1, y: height - savingsH, width: barW, height: Math.max(savingsH, 1),
      rx: 2, fill: "var(--emh-ok)", "fill-opacity": 0.85,
    }, root);
    savingsBar.title = `${monthLabel} savings: ${savings.toFixed(2)}${unitText}`;
    svg("text", {
      x: cx, y: totalH - 2, fill: "var(--emh-dim)", "font-size": 9, "text-anchor": "middle",
    }, root).textContent = new Date(months[i].t).toLocaleDateString(language, { month: "short" });
  }
  return root;
}

/**
 * The savings ledger, forecast and monthly trend, in one card -- otherwise
 * spread across a grid of generic tiles, an apexcharts pie and two markdown
 * templates in dashboards/economy.yaml.
 */
class EmhassEconomyCard extends LiveCard {
  static getStubConfig() {
    return { type: "custom:emhass-economy-card" };
  }

  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-economy-card-editor");
  }

  getCardSize() {
    const layout = this._layout || {};
    let size = 1;
    if (layout.tiles) size += 1;
    if (layout.breakdown) size += 1;
    if (layout.batteryDetail) size += 1;
    if (layout.checking) size += 1;
    if (layout.forecast) size += 3;
    if (layout.trend) size += 2;
    return size;
  }

  build(card) {
    const ui = {};
    this._ui = ui;

    const layout = {
      tiles: showsEconomySection(this._config, "show_tiles"),
      breakdown: showsEconomySection(this._config, "show_breakdown"),
      batteryDetail: showsEconomySection(this._config, "show_battery_detail"),
      checking: showsEconomySection(this._config, "show_checking"),
      forecast: showsEconomySection(this._config, "show_forecast"),
      trend: showsEconomySection(this._config, "show_trend"),
    };
    this._layout = layout;

    const pad = tag("div", "pad", card);

    if (layout.tiles) {
      const stats = tag("div", "stats", pad);
      ui.costTile = statTile(stats, "Balance today");
      ui.costTile.classList.add("tap");
      ui.costTile.title =
        "What buying and selling grid electricity has actually netted out to today -- positive is a net gain, negative a net cost.";
      ui.costTile.addEventListener("click", () => moreInfo(this, ui.costEntity));
      ui.savingsTile = statTile(stats, "Savings today");
      ui.savingsTile.classList.add("tap", "savings");
      ui.savingsTile.title = "Today's cost against a home with no solar and no battery.";
      ui.savingsTile.addEventListener("click", () => moreInfo(this, ui.savingsEntity));
      ui.solarTile = statTile(stats, "Solar savings");
      ui.solarTile.classList.add("tap", "solar");
      ui.solarTile.title = "The share of today's savings attributable to solar production.";
      ui.solarTile.addEventListener("click", () => moreInfo(this, ui.solarEntity));
      ui.batteryTile = statTile(stats, "Battery savings");
      ui.batteryTile.classList.add("tap", "battery");
      ui.batteryTile.title =
        "Arbitrage and solar shifted into the evening by the battery -- the two aren't separable, so both are folded in here.";
      ui.batteryTile.addEventListener("click", () => moreInfo(this, ui.batteryEntity));
    }

    if (layout.breakdown) {
      tag("div", "section", pad, "Where today's savings came from");
      ui.breakdown = tag("div", "hbar", pad);
      ui.segSolar = tag("div", "seg solar", ui.breakdown);
      ui.segBattery = tag("div", "seg battery", ui.breakdown);
      ui.legend = tag("div", "legend", pad);
      ui.legendSolar = tag("span", "chip solar", ui.legend);
      ui.legendBattery = tag("span", "chip battery", ui.legend);
    }

    if (layout.batteryDetail) {
      tag("div", "section", pad, "Battery detail");
      const stats = tag("div", "stats", pad);
      ui.chargeBox = valueBox(stats, "Avg charge price", "Average price paid per kWh while charging the battery today.");
      ui.dischargeBox = valueBox(
        stats,
        "Avg discharge price",
        "Average price the battery's discharged energy was worth today.",
      );
      ui.lossBox = valueBox(
        stats,
        "Round-trip loss",
        "Energy lost to charge/discharge inefficiency today -- charged minus discharged.",
      );
      ui.selfSuffBox = valueBox(
        stats,
        "Self-sufficiency",
        "Share of today's house load met without importing from the grid.",
      );
    }

    if (layout.checking) {
      tag("div", "section", pad, "Checking the numbers");
      const stats = tag("div", "stats", pad);
      ui.unpricedBox = valueBox(
        stats,
        "Unpriced",
        "Energy that flowed today while no price was known for it -- the day's totals are understated by this much.",
      );
      ui.unpricedBox.classList.add("tap");
      ui.unpricedBox.addEventListener("click", () => moreInfo(this, ui.checkingEntity));
      ui.residualBox = valueBox(
        stats,
        "Balance residual",
        "Non-zero only when a measured house-load sensor disagrees with the energy balance the other meters imply.",
      );
      ui.residualBox.classList.add("tap");
      ui.residualBox.addEventListener("click", () => moreInfo(this, ui.checkingEntity));
      tag("div", "hint", pad, "The sources attribute on Savings today has the per-meter breakdown behind these.");
    }

    if (layout.forecast) {
      tag("div", "section", pad, "Next 24h");
      const stats = tag("div", "stats", pad);
      ui.forecastCostTile = statTile(stats, "Forecast balance");
      ui.forecastCostTile.classList.add("tap");
      ui.forecastCostTile.title =
        "What the plan expects buying and selling grid electricity to net out to -- positive is a net gain, negative a net cost.";
      ui.forecastCostTile.addEventListener("click", () => moreInfo(this, ui.forecastCostEntity));
      ui.forecastSavingsTile = statTile(stats, "Forecast savings");
      ui.forecastSavingsTile.classList.add("tap");
      ui.forecastSavingsTile.title =
        "Expected savings over the plan's next 24 hours, against a no-solar-no-battery baseline.";
      ui.forecastSavingsTile.addEventListener("click", () => moreInfo(this, ui.forecastSavingsEntity));
      ui.hourlyWrap = tag("div", "chart-wrap", pad);
      ui.hourlyWrap.title =
        "Planned grid balance by hour. Above the line costs money, below the line earns it; grey means no grid activity.";
    }

    if (layout.trend) {
      tag("div", "section", pad, "Monthly balance vs. savings");
      ui.trendWrap = tag("div", "chart-wrap", pad);
      ui.trendWrap.title = "Balance and savings from Home Assistant's own long-term statistics, month by month.";
      ui.trendHint = tag(
        "div",
        "hint",
        pad,
        "Not enough history yet -- this fills in once a full month has passed.",
      );
    }
  }

  update(hass) {
    const ui = this._ui;
    const layout = this._layout;
    const hub = findHub(hass);
    const view = economyView(hass, hub);

    if (layout.tiles) {
      ui.costEntity = view.entities.cost;
      const balance = balanceText(view.tiles.cost);
      ui.costTile.set(balance.text);
      ui.costTile.classList.toggle("bad", balance.sign < 0);
      ui.costTile.classList.toggle("good", balance.sign > 0);
      ui.savingsEntity = view.entities.savings;
      ui.savingsTile.set(formatMoney(view.tiles.savings));
      ui.solarEntity = view.entities.solar;
      ui.solarTile.set(formatMoney(view.tiles.solar));
      ui.batteryEntity = view.entities.battery;
      ui.batteryTile.set(formatMoney(view.tiles.battery));
    }

    if (layout.breakdown) renderBreakdown(ui, view.breakdown);

    if (layout.batteryDetail) {
      const detail = view.batteryDetail;
      const priceUnit = detail.currency ? ` ${detail.currency}/kWh` : "";
      ui.chargeBox.set(Number.isFinite(detail.chargePrice) ? `${detail.chargePrice.toFixed(2)}${priceUnit}` : "–");
      ui.dischargeBox.set(
        Number.isFinite(detail.dischargePrice) ? `${detail.dischargePrice.toFixed(2)}${priceUnit}` : "–",
      );
      ui.lossBox.set(Number.isFinite(detail.roundTripLoss) ? formatEnergy(detail.roundTripLoss) : "–");
      ui.selfSuffBox.set(Number.isFinite(detail.selfSufficiency) ? `${detail.selfSufficiency.toFixed(1)}%` : "–");
    }

    if (layout.checking) {
      ui.checkingEntity = view.entities.savings;
      ui.unpricedBox.set(Number.isFinite(view.checking.unpriced) ? formatEnergy(view.checking.unpriced) : "–");
      ui.residualBox.set(Number.isFinite(view.checking.residual) ? formatEnergy(view.checking.residual) : "–");
    }

    if (layout.forecast) {
      ui.forecastCostEntity = view.entities.forecastCost;
      const forecastBalance = balanceText(view.forecast.cost);
      ui.forecastCostTile.set(forecastBalance.text);
      ui.forecastCostTile.classList.toggle("bad", forecastBalance.sign < 0);
      ui.forecastCostTile.classList.toggle("good", forecastBalance.sign > 0);
      ui.forecastSavingsEntity = view.entities.forecastSavings;
      ui.forecastSavingsTile.set(formatMoney(view.forecast.savings));

      const points = view.forecast.hourly
        .map((p) => ({ t: Date.parse(p.time), v: Number(p.value) }))
        .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
        .sort((a, b) => a.t - b.t);
      ui.hourlyWrap.textContent = "";
      ui.hourlyWrap.appendChild(hourlyBars(points, hass, view.forecast.unit));
    }

    if (layout.trend) {
      const stats = readStatistics(
        this,
        hass,
        { cost: view.entities.cost, savings: view.entities.savings },
        Date.now(),
      );
      const hasTrend = (stats.cost && stats.cost.length) || (stats.savings && stats.savings.length);
      ui.trendHint.style.display = hasTrend ? "none" : "";
      ui.trendWrap.style.display = hasTrend ? "" : "none";
      ui.trendWrap.textContent = "";
      ui.trendWrap.appendChild(monthlyBars(stats.cost || [], stats.savings || [], hass, view.breakdown.unit));
    }
  }
}

EmhassEconomyCard.ticks = false;
EmhassEconomyCard.css = `
  .section { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em;
             color: var(--emh-dim); margin: 16px 0 8px 0; }
  .section:first-child { margin-top: 0; }

  .stat.bad .v { color: var(--emh-bad); }
  .stat.good .v { color: var(--emh-ok); }
  .stat.savings .v { color: var(--emh-ok); }
  .stat.solar .v { color: var(--emh-solar); }
  .stat.battery .v { color: var(--emh-battery); }

  /* --- savings breakdown bar ------------------------------------------ */
  .hbar { display: flex; height: 12px; border-radius: 6px; overflow: hidden;
          background: var(--emh-surface); }
  .hbar.empty-bar { opacity: .4; }
  .seg { height: 100%; transition: width 400ms var(--emh-ease); }
  .seg.solar { background: var(--emh-solar); }
  .seg.battery { background: var(--emh-battery); }
  .legend { display: flex; gap: 16px; margin-top: 8px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; font-size: .78rem;
          color: var(--emh-dim); }
  .chip::before { content: ""; width: 8px; height: 8px; border-radius: 2px; background: currentColor; }
  .chip.solar { color: var(--emh-solar); }
  .chip.battery { color: var(--emh-battery); }

  .chart-wrap { margin-top: 4px; }
`;

/* ---------------------------------------------------- economy visual editor */

const ECONOMY_SECTIONS = [
  ["show_tiles", "Today tiles", "Balance, savings, solar savings and battery savings for today"],
  ["show_breakdown", "Savings breakdown", "Solar vs. battery share of today's savings"],
  ["show_battery_detail", "Battery detail", "Average charge/discharge price, round-trip loss and self-sufficiency"],
  ["show_checking", "Checking the numbers", "Unpriced energy and balance residual, for auditing the day's total"],
  ["show_forecast", "Next 24h forecast", "Forecast balance and savings, and the planned balance hour by hour"],
  ["show_trend", "Monthly trend", "Balance vs. savings, month by month, over the last year"],
];

const ECONOMY_LABELS = {};
const ECONOMY_HELPERS = {};
for (const section of ECONOMY_SECTIONS) {
  ECONOMY_LABELS[section[0]] = section[1];
  ECONOMY_HELPERS[section[0]] = section[2];
}

const ECONOMY_SCHEMA = [sectionGrid(ECONOMY_SECTIONS)];
const ECONOMY_KEYS = ECONOMY_SECTIONS.map((section) => section[0]);

/** "Checking the numbers" is an audit tool, not something most users need
 * to see every day -- off unless someone turns it on. */
const ECONOMY_OFF_BY_DEFAULT = new Set(["show_checking"]);

function showsEconomySection(config, key) {
  const value = config ? config[key] : undefined;
  if (value === undefined) return !ECONOMY_OFF_BY_DEFAULT.has(key);
  return value !== false;
}

class EmhassEconomyCardEditor extends CardEditor {
  get labels() {
    return ECONOMY_LABELS;
  }

  get helpers() {
    return ECONOMY_HELPERS;
  }

  schema() {
    return ECONOMY_SCHEMA;
  }

  data() {
    const data = {};
    for (const key of ECONOMY_KEYS) data[key] = showsEconomySection(this._config, key);
    return data;
  }

  clean(config) {
    return cleanSections(config, ECONOMY_SECTIONS, ECONOMY_OFF_BY_DEFAULT);
  }
}

customElements.define("emhass-economy-card", EmhassEconomyCard);
customElements.define("emhass-economy-card-editor", EmhassEconomyCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-economy-card",
    name: "EMHASS Companion economy",
    description: "Today's balance and savings, the solar/battery split, and a forecast and monthly trend.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);
