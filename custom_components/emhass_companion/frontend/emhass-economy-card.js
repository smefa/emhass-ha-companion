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
      hourlySavings: Array.isArray(forecastAttrs.hourly_savings) ? forecastAttrs.hourly_savings : [],
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

/** Whole units on the axis. The gutter is too narrow for decimals. */
function formatAxisValue(value) {
  if (!Number.isFinite(value)) return "–";
  const rounded = Math.round(value);
  return rounded === 0 ? "0" : String(rounded);
}

/**
 * Axis text in HTML, the bars in the SVG.
 *
 * The plot uses a 1000-wide viewBox with preserveAspectRatio off so the bars
 * stretch to the card. That same stretch turns a font sized in viewBox units
 * into a sliver a few pixels wide, which is why the hour and month labels
 * were hard to read. HTML is in real pixels, so it stays readable.
 */
function mountChart(plot, yMax, yMin, ticks) {
  const frame = tag("div", "chart-frame");
  const yAxis = tag("div", "y-axis", frame);
  tag("span", "y-max", yAxis, formatAxisValue(yMax));
  tag("span", "y-min", yAxis, formatAxisValue(yMin));
  const plotWrap = tag("div", "plot", frame);
  plotWrap.appendChild(plot);
  if (!ticks.length) return frame;
  const xAxis = tag("div", "x-axis", frame);
  for (const tick of ticks) {
    const label = tag("span", "x-tick", xAxis, tick.text);
    label.style.left = `${tick.pct}%`;
    if (tick.pct <= 8) label.classList.add("start");
    else if (tick.pct >= 92) label.classList.add("end");
  }
  return frame;
}

/**
 * One pair of bars per forecast hour, around a zero line -- balance on the
 * left (cost below, income above), savings on the right (what that hour
 * avoided against a no-solar-no-battery house).
 *
 * The side labels are that shared scale: the largest amount drawn upward, and
 * the largest drawn downward. Earn and savings used to share --emh-ok, so the
 * two greens in one slot could not be told apart.
 */
function hourlyBars(costPoints, savingsPoints, hass, unit) {
  const byTime = new Map();
  for (const point of costPoints) {
    if (!Number.isFinite(point.t) || !Number.isFinite(point.v)) continue;
    byTime.set(point.t, { t: point.t, cost: point.v, savings: 0 });
  }
  for (const point of savingsPoints) {
    if (!Number.isFinite(point.t) || !Number.isFinite(point.v)) continue;
    const existing = byTime.get(point.t);
    if (existing) existing.savings = point.v;
    else byTime.set(point.t, { t: point.t, cost: 0, savings: point.v });
  }
  const points = [...byTime.values()].sort((a, b) => a.t - b.t);
  if (points.length < 1) {
    const empty = svg("svg", { viewBox: "0 0 1000 64", preserveAspectRatio: "none", role: "img" });
    empty.style.height = "64px";
    return empty;
  }

  // Below zero is a cost or a negative saving. With neither, that half is
  // empty, so it collapses to a baseline and the upward half stays the size
  // it has today. Any downward value restores the current split.
  const downMax = Math.max(0, ...points.map((p) => Math.max(p.cost, -p.savings)));
  const hasDown = downMax > 0;
  const above = 32;
  const below = hasDown ? 32 : 4;
  const height = above + below;
  const zero = above;
  const maxUp = Math.max(0.001, ...points.map((p) => Math.max(-p.cost, p.savings, 0)));
  const maxAbs = hasDown ? Math.max(maxUp, downMax) : maxUp;
  const scale = (above - 4) / maxAbs;

  const root = svg("svg", { viewBox: `0 0 1000 ${height}`, preserveAspectRatio: "none", role: "img" });
  root.style.height = `${height}px`;

  const stepMs = points.length > 1 ? points[1].t - points[0].t : 3600000;
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t + stepMs;
  const x = (t) => ((t - t0) / (t1 - t0 || 1)) * 1000;

  const hour = 3600000;
  const step = (t1 - t0) / hour > 18 ? 6 * hour : 3 * hour;
  const first = Math.ceil(t0 / step) * step;
  const ticks = [];
  for (let t = first; t < t1; t += step) {
    svg("line", {
      x1: x(t), x2: x(t), y1: 0, y2: height,
      stroke: "var(--emh-hairline)", "stroke-width": 1, "stroke-dasharray": "2 2",
    }, root);
    ticks.push({ pct: (x(t) / 1000) * 100, text: formatHour(t, hass) });
  }
  svg("line", { x1: 0, x2: 1000, y1: zero, y2: zero, stroke: "var(--emh-hairline)", "stroke-width": 1 }, root);

  const slotW = 1000 / points.length;
  const barW = Math.max(slotW * 0.28, 2);
  const unitText = unit ? ` ${unit}` : "";

  for (const point of points) {
    const cx = x(point.t) + (x(point.t + stepMs) - x(point.t)) / 2;
    const span = `${formatTime(point.t, hass)} – ${formatTime(point.t + stepMs, hass)}`;

    const costH = Math.abs(point.cost) * scale;
    const costBar = svg("rect", {
      x: cx - barW - 1,
      y: point.cost >= 0 ? zero : zero - costH,
      width: barW,
      height: Math.max(costH, Number.isFinite(point.cost) ? 1 : 0),
      rx: 2,
      fill: point.cost === 0 ? "var(--emh-dim)" : point.cost > 0 ? "var(--emh-bad)" : "var(--emh-earn)",
      "fill-opacity": 0.95,
    }, root);
    costBar.title = `${span}: balance ${point.cost.toFixed(2)}${unitText}`;

    const saveH = Math.abs(point.savings) * scale;
    const saveBar = svg("rect", {
      x: cx + 1,
      y: point.savings >= 0 ? zero - saveH : zero,
      width: barW,
      height: Math.max(saveH, Number.isFinite(point.savings) ? 1 : 0),
      rx: 2,
      fill: point.savings === 0 ? "var(--emh-dim)" : point.savings > 0 ? "var(--emh-save)" : "var(--emh-bad)",
      "fill-opacity": 0.95,
    }, root);
    saveBar.title = `${span}: savings ${point.savings.toFixed(2)}${unitText}`;
  }

  const now = Date.now();
  if (now >= t0 && now <= t1) {
    svg("line", {
      x1: x(now), x2: x(now), y1: -2, y2: height + 2,
      stroke: "var(--primary-text-color)", "stroke-width": 2,
    }, root);
  }
  return mountChart(root, maxAbs, hasDown ? -maxAbs : 0, ticks);
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
  const root = svg("svg", { viewBox: `0 0 1000 ${height}`, preserveAspectRatio: "none", role: "img" });
  root.style.height = `${height}px`;
  const months = costPoints.length >= savingsPoints.length ? costPoints : savingsPoints;
  if (!months.length) return root;

  const maxV = Math.max(1, ...costPoints.map((p) => p.v), ...savingsPoints.map((p) => p.v));
  const scale = height / maxV;
  const slot = 1000 / months.length;
  const barW = Math.max(slot * 0.32, 2);
  const language = hass && hass.locale ? hass.locale.language : undefined;

  svg("line", { x1: 0, x2: 1000, y1: height, y2: height, stroke: "var(--emh-hairline)", "stroke-width": 1 }, root);

  const unitText = unit ? ` ${unit}` : "";
  const ticks = [];
  // Short month names at a readable size collide once a year of them is squeezed
  // onto a phone. Every other month still places the ones that remain.
  const stride = months.length > 8 ? 2 : 1;
  for (let i = 0; i < months.length; i++) {
    const cx = slot * i + slot / 2;
    const cost = costPoints[i] ? costPoints[i].v : 0;
    const savings = savingsPoints[i] ? savingsPoints[i].v : 0;
    const costH = cost * scale;
    const savingsH = savings * scale;
    const when = new Date(months[i].t);
    const monthLabel = when.toLocaleDateString(language, { month: "short", year: "numeric" });
    const costBar = svg("rect", {
      x: cx - barW - 1, y: height - costH, width: barW, height: Math.max(costH, 1),
      rx: 2, fill: "var(--emh-bad)", "fill-opacity": 0.95,
    }, root);
    costBar.title = `${monthLabel} balance: ${cost.toFixed(2)}${unitText}`;
    const savingsBar = svg("rect", {
      x: cx + 1, y: height - savingsH, width: barW, height: Math.max(savingsH, 1),
      rx: 2, fill: "var(--emh-save)", "fill-opacity": 0.95,
    }, root);
    savingsBar.title = `${monthLabel} savings: ${savings.toFixed(2)}${unitText}`;
    // The last month is always named. Drop the tick beside it, or "Nov" and
    // "Dec" land on neighbouring slots and overlap at this size.
    const last = i === months.length - 1;
    const besideLast = stride > 1 && i === months.length - 2 && (months.length - 1) % stride !== 0;
    if ((i % stride === 0 && !besideLast) || last) {
      ticks.push({
        pct: (cx / 1000) * 100,
        text: when.toLocaleDateString(language, { month: "short" }),
      });
    }
  }
  return mountChart(root, maxV, 0, ticks);
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
        "Planned grid balance and savings by hour. Left bar is balance (below costs, above earns); right bar is what that hour saves against a no-solar-no-battery house.";
      ui.hourlyLegend = tag("div", "legend", pad);
      ui.hourlyLegendCost = tag("span", "chip balance", ui.hourlyLegend, "Balance");
      ui.hourlyLegendSavings = tag("span", "chip savings", ui.hourlyLegend, "Savings");
      ui.hourlyLegendCost.title = "Net grid spend that hour -- below the line costs money, above it earns.";
      ui.hourlyLegendSavings.title =
        "What that hour saves against buying every kWh from the grid with no solar and no battery.";
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

      const costPoints = view.forecast.hourly
        .map((p) => ({ t: Date.parse(p.time), v: Number(p.value) }))
        .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
        .sort((a, b) => a.t - b.t);
      const savingsPoints = view.forecast.hourlySavings
        .map((p) => ({ t: Date.parse(p.time), v: Number(p.value) }))
        .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
        .sort((a, b) => a.t - b.t);
      ui.hourlyWrap.textContent = "";
      ui.hourlyWrap.appendChild(hourlyBars(costPoints, savingsPoints, hass, view.forecast.unit));
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
  :host {
    /* Lime for balance when the hour earns. Savings keeps the theme green.
       A nearby green shade still read as the same bar at this width. */
    --emh-earn: #cddc39;
    --emh-save: var(--emh-ok);
  }

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
  .chip.balance { color: var(--primary-text-color); }
  .chip.balance::before {
    height: 10px;
    background: linear-gradient(to bottom, var(--emh-earn) 0 4px, transparent 4px 6px, var(--emh-bad) 6px 10px);
  }
  .chip.savings { color: var(--emh-save); }

  .chart-wrap { margin-top: 4px; }
  .chart-frame { display: grid; grid-template-columns: auto minmax(0, 1fr);
                 column-gap: 8px; align-items: stretch; }
  .y-axis { grid-column: 1; grid-row: 1; display: flex; flex-direction: column;
            justify-content: space-between; align-items: flex-end;
            font-size: .68rem; line-height: 1; font-variant-numeric: tabular-nums;
            color: var(--primary-text-color); }
  .plot { grid-column: 2; grid-row: 1; min-width: 0; }
  .x-axis { grid-column: 2; grid-row: 2; position: relative; height: 1rem;
            margin-top: 2px; }
  .x-tick { position: absolute; top: 0; transform: translateX(-50%);
            font-size: .68rem; line-height: 1rem; white-space: nowrap;
            font-variant-numeric: tabular-nums; color: var(--primary-text-color); }
  .x-tick.start { transform: none; }
  .x-tick.end { transform: translateX(-100%); }
`;

/* ---------------------------------------------------- economy visual editor */

const ECONOMY_SECTIONS = [
  ["show_tiles", "Today tiles", "Balance, savings, solar savings and battery savings for today"],
  ["show_breakdown", "Savings breakdown", "Solar vs. battery share of today's savings"],
  ["show_battery_detail", "Battery detail", "Average charge/discharge price, round-trip loss and self-sufficiency"],
  ["show_checking", "Checking the numbers", "Unpriced energy and balance residual, for auditing the day's total"],
  ["show_forecast", "Next 24h forecast", "Forecast balance and savings, and the planned balance and savings hour by hour"],
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
