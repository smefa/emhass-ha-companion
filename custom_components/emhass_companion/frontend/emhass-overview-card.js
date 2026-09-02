/**
 * Extracted from the single-bundle emhass-cards.js, which shipped all fourteen
 * elements in one module: when the frontend's own fixed element-registration
 * timeout (home-assistant/frontend#52960) is lost, the whole module fails, so
 * one lost race took down every card at once. Split into a shared core module
 * plus one bundle per card family, a lost race costs one family's elements.
 */

import {
  CardEditor,
  DEFAULT_HISTORY_HOURS,
  DEFAULT_PLAN_HISTORY_HOURS,
  LiveCard,
  balanceText,
  cleanSections,
  clipSeries,
  findHub,
  findLoads,
  formatAgo,
  formatEnergy,
  formatHours,
  formatPower,
  formatSpan,
  formatTime,
  integrate,
  isUsable,
  labelFor,
  loadHaForm,
  loadView,
  measuredBy,
  mergeHistory,
  moreInfo,
  num,
  pastBand,
  readHistory,
  sectionGrid,
  series,
  showsSection,
  statTile,
  stateOf,
  svg,
  tag,
  trackSvg,
} from "./emhass-core.js?v=__VERSION__";


/**
 * A power series as a filled profile, on the same rail a track uses.
 *
 * The loads are on/off, so a track of blocks says everything about them. Solar
 * and the battery are quantities, and a block would throw away the only thing
 * worth knowing about them -- how much. Same rail, same ticks and same now
 * marker as `trackSvg`, so a lane of this stacks under a lane of that and the
 * two are read against one clock.
 *
 * `signed` puts the baseline in the middle and colours the two halves apart:
 * the battery's sign is the whole story (charging is not a small discharge),
 * and a bar above or below a line is that story without a legend.
 */
function profileSvg(options) {
  const points = options.points || [];
  const height = options.height || 26;
  const root = svg("svg", {
    viewBox: `0 0 1000 ${height}`,
    preserveAspectRatio: "none",
    role: "img",
  });
  // Pinned for the same reason trackSvg pins it: with preserveAspectRatio off
  // the height would otherwise scale with the card's width.
  root.style.height = `${height}px`;

  const t0 = options.from;
  const t1 = options.to;
  if (!Number.isFinite(t0) || !Number.isFinite(t1)) return root;
  const x = (t) => ((t - t0) / (t1 - t0 || 1)) * 1000;

  svg("rect", {
    x: 0, y: 0, width: 1000, height,
    rx: Math.min(7, height / 2), fill: "var(--emh-surface)",
  }, root);
  pastBand(root, options.past, x, t0, height);

  const hour = 3600000;
  const step = (t1 - t0) / hour > 30 ? 6 * hour : 3 * hour;
  for (let t = Math.ceil(t0 / step) * step; t < t1; t += step) {
    svg("line", {
      x1: x(t), x2: x(t), y1: 0, y2: height,
      stroke: "var(--emh-hairline)", "stroke-width": 1,
    }, root);
  }

  const signed = options.signed === true;
  const base = signed ? height / 2 : height;
  const amp = (signed ? height / 2 : height) - 1;
  let peak = 0;
  for (const point of points) peak = Math.max(peak, Math.abs(point.v));

  if (signed) {
    svg("line", {
      x1: 0, x2: 1000, y1: base, y2: base,
      stroke: "var(--emh-hairline)", "stroke-width": 1,
    }, root);
  }

  if (peak > 0) {
    // One filled path per run of the same sign, rather than one path per
    // point: a 30-minute plan over two days is 96 rectangles, and the seams
    // between them show as hairlines at any opacity below 1.
    const runs = [];
    let open = null;
    for (let i = 0; i < points.length; i++) {
      const value = points[i].v;
      const sign = value > 0 ? 1 : value < 0 ? -1 : 0;
      if (!open || open.sign !== sign) {
        open = { sign, steps: [] };
        runs.push(open);
      }
      open.steps.push({
        start: points[i].t,
        end: i + 1 < points.length ? points[i + 1].t : t1,
        v: value,
      });
    }
    for (const run of runs) {
      if (!run.sign) continue;
      const last = run.steps[run.steps.length - 1];
      const parts = [`M ${x(run.steps[0].start).toFixed(2)} ${base}`];
      for (const stepRun of run.steps) {
        const y = base - (stepRun.v / peak) * amp;
        parts.push(`L ${x(stepRun.start).toFixed(2)} ${y.toFixed(2)}`);
        parts.push(`L ${x(stepRun.end).toFixed(2)} ${y.toFixed(2)}`);
      }
      parts.push(`L ${x(last.end).toFixed(2)} ${base}`, "Z");
      svg("path", {
        d: parts.join(" "),
        fill: run.sign > 0 ? options.color : options.negativeColor || options.color,
        "fill-opacity": 0.85,
      }, root);
    }
  }

  // An optional second series on its own scale, drawn as a line: the battery
  // lane's charge blocks are the cause and the state of charge is the effect,
  // and the two are only legible together.
  const line = options.line;
  if (line && line.points && line.points.length > 1) {
    const span = (line.max - line.min) || 1;
    const y = (v) => height - 1 - ((v - line.min) / span) * (height - 2);
    const parts = line.points.map(
      (point, i) => `${i ? "L" : "M"} ${x(point.t).toFixed(2)} ${y(point.v).toFixed(2)}`,
    );
    svg("path", {
      d: parts.join(" "),
      fill: "none",
      stroke: line.color,
      "stroke-width": 1.5,
      "stroke-opacity": 0.9,
      "stroke-linejoin": "round",
    }, root);
  }

  const now = Date.now();
  if (now >= t0 && now <= t1) {
    svg("line", {
      x1: x(now), x2: x(now), y1: -1, y2: height + 1,
      stroke: "var(--primary-text-color)", "stroke-width": 2,
    }, root);
  }
  return root;
}

/**
 * One `name | lane | figure` row, the shape the overview's gantt is built from.
 *
 * Shared so the solar and battery lanes are laid out by the same code as the
 * load lanes rather than by a copy of it: the three only mean anything read
 * against each other, and that requires their lanes to start and end on the
 * same pixel.
 */
function laneRow(parent, name, color) {
  const row = tag("div", "grow-row", parent);
  const label = tag("div", "glabel", row);
  const dot = tag("i", "gdot", label);
  dot.style.background = color || "var(--emh-surface-2)";
  tag("span", null, label, name);
  row.lane = tag("div", "lane", row);
  const figure = tag("div", "ghours", row, "–");
  row.setFigure = (text) => {
    figure.textContent = text;
  };
  row.setLane = (node) => {
    row.lane.textContent = "";
    row.lane.appendChild(node);
  };
  return row;
}

/* ---------------------------------------- info card 3: household overview */

/**
 * The sections the card is built from, in the order they are drawn.
 *
 * One list drives all three of them: what `build()` puts on the card, what the
 * visual editor offers, and which keys are worth writing into the YAML. A
 * section added here appears in the editor without the editor being touched,
 * which is the only way the two stay in step.
 */
const OVERVIEW_SECTIONS = [
  ["show_stats", "Info boxes", "The row of value boxes under the title"],
  ["show_price", "Import price", "The ribbon everything else is read against"],
  ["show_surplus", "Spare solar", "The surplus window, when there is one"],
  ["show_solar", "Solar lane", "The solar forecast, as a filled profile"],
  ["show_battery", "Battery lane", "Discharge above the line, charging below it"],
  ["show_soc", "State of charge", "The planned level, drawn over the battery lane"],
  ["show_loads", "Loads", "One lane per deferrable load"],
];

/**
 * What an info box can be set to show.
 *
 * Each entry reads one thing out of a context the card assembles once per
 * update, and returns its own label with it: "Grid" is "Grid import" or "Grid
 * export" depending on the sign, and a box whose caption cannot follow its
 * value is a box that lies for half the day.
 *
 * The keys are what ends up in the YAML, so they are named for the quantity
 * rather than for the entity that happens to carry it.
 */
const TILE_METRICS = {
  none: { label: "Nothing", read: () => ({ k: "", v: "" }) },
  solar: {
    label: "Solar now",
    hint: "Solar power the plan forecast for this moment.",
    entity: "sensor.pv_forecast",
    read: (c) => ({ k: "Solar", v: formatPower(num(c.pv)) }),
  },
  house: {
    label: "House now",
    hint: "The whole house's forecast power draw for this moment.",
    entity: "sensor.load_forecast",
    read: (c) => ({ k: "House", v: formatPower(num(c.house)) }),
  },
  grid: {
    label: "Grid now",
    hint: "Power crossing the meter: import when positive, export when negative.",
    entity: "sensor.grid_forecast",
    read: (c) => {
      const watts = num(c.grid);
      return {
        k: Number.isFinite(watts) && watts < 0 ? "Grid export" : "Grid import",
        v: formatPower(Math.abs(watts)),
      };
    },
  },
  battery: {
    label: "Battery now",
    hint: "Battery power and charge level: discharging when positive, charging when negative.",
    entity: "sensor.battery_power",
    read: (c) => {
      const watts = num(c.battery);
      return {
        k: Number.isFinite(watts) && watts < 0 ? "Battery charging" : "Battery",
        v: `${formatPower(Math.abs(watts))}${
          isUsable(c.soc) ? ` · ${num(c.soc).toFixed(0)}%` : ""
        }`,
      };
    },
  },
  soc: {
    label: "Charge level",
    hint: "The battery's state of charge right now.",
    entity: "sensor.battery_soc",
    read: (c) => ({
      k: "Charge level",
      v: isUsable(c.soc) ? `${num(c.soc).toFixed(0)} %` : "–",
    }),
  },
  end_soc: {
    label: "End charge target",
    hint: "The state of charge the plan is aiming to end its horizon at.",
    entity: "sensor.end_soc_target",
    read: (c) => ({
      k: "Target at end",
      v: isUsable(c.endSoc) ? `${num(c.endSoc).toFixed(0)} %` : "–",
    }),
  },
  price: {
    label: "Import price now",
    hint: "What buying a kWh from the grid costs right now.",
    entity: "sensor.buy_price",
    read: (c) => ({ k: "Price now", v: isUsable(c.buy) ? num(c.buy).toFixed(3) : "–" }),
  },
  sell_price: {
    label: "Export price now",
    hint: "What selling a kWh back to the grid pays right now.",
    entity: "sensor.sell_price",
    read: (c) => ({ k: "Export price", v: isUsable(c.sell) ? num(c.sell).toFixed(3) : "–" }),
  },
  cost: {
    label: "Forecast balance",
    hint:
      "What the plan expects buying and selling grid electricity to net out to -- positive is a " +
      "net gain, negative a net cost. Not EMHASS's own objective value: that one stops being real " +
      "money under some cost functions, so this reads the Companion's own forecast instead.",
    entity: "sensor.forecast_cost_24h",
    read: (c) => ({ k: "Forecast balance", v: balanceText(c.cost).text }),
  },
  solar_planned: {
    label: "Solar in the plan",
    hint: "Solar energy the plan expects to generate over its horizon.",
    entity: "sensor.pv_forecast",
    read: (c) => ({ k: "Solar planned", v: formatEnergy(c.solarEnergy) }),
  },
  charge_planned: {
    label: "Charging in the plan",
    hint: "Energy the plan expects to put into the battery over its horizon.",
    entity: "sensor.battery_power",
    read: (c) => ({ k: "Charging planned", v: formatEnergy(c.chargeEnergy) }),
  },
  surplus: {
    label: "Spare solar",
    hint: "Solar the plan expects to have no use for during the surplus window.",
    entity: "sensor.solar_surplus_energy",
    read: (c) => ({
      k: "Spare solar",
      v: isUsable(c.surplusEnergy) ? formatEnergy(num(c.surplusEnergy)) : "–",
    }),
  },
  loads: {
    label: "Loads running",
    hint: "How many deferrable loads are on right now, out of how many are configured.",
    read: (c) => ({
      k: "Loads on",
      v: c.loadsTotal ? `${c.loadsOn} of ${c.loadsTotal}` : "–",
    }),
  },
  age: {
    label: "Plan age",
    hint: "How long ago the last successful optimisation finished.",
    entity: "binary_sensor.plan_stale",
    read: (c) => ({
      k: "Planned",
      v: Number.isFinite(c.plannedAt) ? formatAgo(c.plannedAt, c.hass) : "–",
    }),
  },
};

// The order the dropdown offers them in: the four "right now" readings first,
// since those are what a box is usually set to, then the plan's own figures.
const TILE_ORDER = [
  "none",
  "solar",
  "house",
  "grid",
  "battery",
  "soc",
  "end_soc",
  "price",
  "sell_price",
  "cost",
  "solar_planned",
  "charge_planned",
  "surplus",
  "loads",
  "age",
];

// Six boxes, the first four being what the card showed before it had any say
// in the matter, so an existing card is unchanged by the new ones.
const TILE_DEFAULTS = ["solar", "house", "grid", "battery", "price", "cost"];

function tileMetric(config, index) {
  const wanted = config ? config[`tile_${index}`] : undefined;
  return wanted && TILE_METRICS[wanted] ? wanted : TILE_DEFAULTS[index - 1];
}

/**
 * The whole plan on one time axis: prices, solar, the battery and every load.
 *
 * The existing plan card answers "what will the power do"; this answers "why
 * is it doing that", by putting the price ribbon, the two power profiles and
 * the loads on the *same* axis. A load block sitting under the cheapest hour
 * of the night, or a charge block under the fattest part of the solar curve,
 * is the optimiser explaining itself, and it takes no reading of a chart to
 * see. Every row is laid out by `laneRow`, so the lanes start and end on the
 * same pixel -- without that the comparison the card is for is a lie.
 */
class EmhassOverviewCard extends LiveCard {
  static getStubConfig() {
    return { type: "custom:emhass-overview-card" };
  }

  /** The visual editor, with Home Assistant's own form widgets pulled in. */
  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-overview-card-editor");
  }

  getCardSize() {
    let size = 2;
    if (showsSection(this._config, "show_stats")) size += 1;
    if (showsSection(this._config, "show_price")) size += 1;
    if (
      showsSection(this._config, "show_solar") ||
      showsSection(this._config, "show_battery")
    ) {
      size += 2;
    }
    if (showsSection(this._config, "show_loads")) size += 2;
    return size;
  }

  build(card) {
    const ui = {};
    this._ui = ui;
    const shows = (key) => showsSection(this._config, key);
    const pad = tag("div", "pad", card);

    const head = tag("div", "head", pad);
    const grow = tag("div", "grow", head);
    tag("div", "name", grow, this._config.title || "Today's plan");
    ui.sub = tag("div", "sub", grow, "");
    ui.pill = tag("div", "pill", head);
    tag("span", "dot", ui.pill);
    ui.pillText = tag("span", null, ui.pill, "");

    if (shows("show_stats")) {
      ui.stats = tag("div", "stats", pad);
      // A box set to "Nothing" is not built at all rather than emptied: the
      // row is an auto-fit grid, and an empty cell would still take a column.
      ui.tiles = TILE_DEFAULTS.map((_default, index) => {
        const metric = tileMetric(this._config, index + 1);
        if (metric === "none") return null;
        const spec = TILE_METRICS[metric];
        const tile = statTile(ui.stats, spec.label);
        tile.metric = metric;
        if (spec.hint) tile.title = spec.hint;
        if (spec.entity) {
          tile.classList.add("tap");
          tile.addEventListener("click", () => moreInfo(this, this._hubId(spec.entity)));
        }
        return tile;
      });
      if (!ui.stats.childElementCount) ui.stats.style.display = "none";
    }

    if (shows("show_price")) {
      tag("div", "section", pad, "Import price");
      // The price sits in a lane of its own rather than spanning the card, so
      // it lines up with the lanes below it. A ribbon that is 90 px wider than
      // the loads it explains puts every block under the wrong hour.
      ui.priceRow = laneRow(pad, "Price", "var(--emh-accent)");
      ui.priceRow.title = "Import price, cheapest to most expensive by colour. Click for the price history.";
      ui.ribbon = tag("div", "ribbon", ui.priceRow.lane);
      ui.ribbonScale = tag("div", "scale", pad);
      ui.priceRow.addEventListener("click", () => moreInfo(this, this._hubId("sensor.buy_price")));
    }

    if (shows("show_surplus")) {
      ui.surplusNote = tag("div", "surplus tap", pad);
      ui.surplusNote.title =
        "The window during which the plan expects more solar than the house can use. Click for the surplus energy history.";
      ui.surplusNote.addEventListener("click", () =>
        moreInfo(this, this._hubId("sensor.solar_surplus_energy")),
      );
    }

    ui.showSolar = shows("show_solar");
    ui.showBattery = shows("show_battery");
    ui.showSoc = shows("show_soc");
    if (ui.showSolar || ui.showBattery) {
      ui.powerSection = tag(
        "div",
        "section",
        pad,
        ui.showSolar && ui.showBattery ? "Solar and battery" : ui.showSolar ? "Solar" : "Battery",
      );
      ui.power = tag("div", "gantt", pad);
      if (ui.showSolar) {
        ui.solarRow = laneRow(ui.power, "Solar", "var(--emh-solar)");
        ui.solarRow.addEventListener("click", () =>
          moreInfo(this, this._hubId("sensor.pv_forecast")),
        );
      }
      if (ui.showBattery) {
        ui.batteryRow = laneRow(ui.power, "Battery", "var(--emh-battery)");
        ui.batteryRow.addEventListener("click", () =>
          moreInfo(this, this._hubId("sensor.battery_power")),
        );
      }
      ui.powerKey = tag("div", "key", pad);
    }

    if (shows("show_loads")) {
      ui.loadsSection = tag("div", "section", pad, "Loads");
      ui.gantt = tag("div", "gantt", pad);
    }

    if (ui.priceRow || ui.power || ui.gantt) ui.axis = tag("div", "axis", pad);
  }

  _hubId(key) {
    return this._hub ? this._hub[key] : null;
  }

  /** How far back the window reaches, in milliseconds. */
  _historyMs() {
    const hours = Number(this._config.history_hours);
    return (Number.isFinite(hours) && hours >= 0 ? hours : DEFAULT_PLAN_HISTORY_HOURS) * 3600000;
  }

  /**
   * What solar, the battery and the price actually did, from the recorder.
   *
   * The plan is no use behind the present: an MPC run starts at the timestep
   * it was made in, so the moment the window reaches into the past these
   * lanes would start with blank rail. The backend trims `buy_price` the same
   * way it trims every plan series -- to `window_start` onward -- so the
   * ribbon needs its own recorder lookup exactly as the power lanes do. The
   * recorder has the answer, and it is one call for all three.
   *
   * Solar and battery are read from the Companion's own sensors by default,
   * whose sign convention is known -- positive is discharge -- and which need
   * no configuration to work. `solar_entity` and `battery_entity` point it at
   * the house's own meters instead, which is the difference between "what was
   * forecast" and "what happened".
   */
  _history(hass, hub, now) {
    // Card option first, then whatever the Companion itself was configured
    // with, then the plan's own figure.
    const battery = this._config.battery_entity
      ? { entity: this._config.battery_entity, invert: this._config.invert_battery === true }
      : measuredBy(hass, hub, "sensor.battery_power");
    const wanted = {
      solar: this._config.solar_entity || hub["sensor.pv_forecast"],
      battery: battery ? battery.entity : hub["sensor.battery_power"],
      price: hub["sensor.buy_price"],
    };
    // Only an outside sensor can have the other sign convention: the plan's
    // own battery series is positive while discharging by definition.
    const invert = battery && battery.invert ? ["battery"] : [];
    return readHistory(this, hass, wanted, invert, this._historyMs(), now);
  }

  update(hass) {
    const ui = this._ui;
    const hub = findHub(hass);
    this._hub = hub;
    const pv = stateOf(hass, hub["sensor.pv_forecast"]);
    const houseLoad = stateOf(hass, hub["sensor.load_forecast"]);
    const grid = stateOf(hass, hub["sensor.grid_forecast"]);
    const battery = stateOf(hass, hub["sensor.battery_power"]);
    const soc = stateOf(hass, hub["sensor.battery_soc"]);
    const buy = stateOf(hass, hub["sensor.buy_price"]);
    const sell = stateOf(hass, hub["sensor.sell_price"]);
    const cost = stateOf(hass, hub["sensor.forecast_cost_24h"]);
    const endSoc = stateOf(hass, hub["sensor.end_soc_target"]);
    const status = stateOf(hass, hub["sensor.optimization_status"]);
    const stale = stateOf(hass, hub["binary_sensor.plan_stale"]);
    const surplusStart = stateOf(hass, hub["sensor.solar_surplus_start"]);
    const surplusEnd = stateOf(hass, hub["sensor.solar_surplus_end"]);
    const surplusEnergy = stateOf(hass, hub["sensor.solar_surplus_energy"]);

    const isStale = Boolean(stale) && stale.state === "on";
    ui.pill.className = `pill ${isStale ? "warn" : "on"}`;
    ui.pillText.textContent = isStale ? "Out of date" : status ? labelFor(hass, status) : "–";

    /* --- one axis for everything below --- */
    const now = Date.now();
    const planSolar = series(pv);
    const planBattery = series(battery);
    const history = this._history(hass, hub, now);
    // The backend trims the price forecast to start at "now" like every
    // other plan series, so the ribbon needs the same recorder backfill the
    // power lanes get below -- without it, everything before "now" reads as
    // "no prices published yet".
    const prices = mergeHistory(history.price, series(buy));
    const solarPoints = mergeHistory(history.solar, planSolar);
    const batteryPoints = mergeHistory(history.battery, planBattery);
    const socPoints = series(soc);
    const loads = findLoads(hass).map((load) => loadView(hass, load));
    // A rolling window rather than the extent of the data: the plan's own
    // series only reach back to "now", and eleven hours of spent morning
    // squeeze the part of the plan that can still be changed into a third of
    // the card. The end is still the data's, since that is the horizon.
    const lists = [prices, solarPoints, batteryPoints]
      .concat(loads.map((view) => view.points))
      .filter((list) => list.length);
    let t1 = -Infinity;
    for (const list of lists) t1 = Math.max(t1, list[list.length - 1].t);
    const t0 = now - this._historyMs();
    // A plan that has run out entirely would otherwise be drawn on a window
    // running backwards, which puts every block off the left of the card.
    if (t1 < now) t1 = now;

    // The boxes are filled before the "no plan yet" bail-out below: most of
    // them are live readings, and a house that has never been optimised is
    // exactly when someone looks at the card.
    if (ui.tiles) {
      const staleAttrs = stale && stale.attributes ? stale.attributes : {};
      const plannedAt = staleAttrs.last_successful_run
        ? Date.parse(staleAttrs.last_successful_run)
        : NaN;
      this._tiles({
        hass,
        pv,
        house: houseLoad,
        grid,
        battery,
        soc,
        endSoc,
        buy,
        sell,
        cost,
        surplusEnergy,
        // The plan's own series, not the merged one: a box captioned "in the
        // plan" must not quietly count the two hours of recorded history the
        // lanes now carry in front of it.
        solarEnergy: integrate(planSolar, t1).up,
        chargeEnergy: integrate(planBattery, t1).down,
        loadsOn: loads.filter((view) => view.status === "running" || view.status === "should")
          .length,
        loadsTotal: loads.length,
        plannedAt,
      });
    }

    if (!lists.length) {
      ui.sub.textContent = "No plan yet — it appears after the first optimisation.";
      return;
    }
    ui.sub.textContent = formatSpan(t0, t1, hass);
    ui.sub.title = "Everything below is drawn between these two times";

    // Every series is trimmed to the window before it is drawn. The lanes are
    // laid out from t0 and t1 alone, so a point outside them is not clipped by
    // the SVG -- it is drawn off the side of the card.
    if (ui.ribbon) this._ribbon(clipSeries(prices, t0, t1), buy, t0, t1, now, hass);
    if (ui.surplusNote) this._surplus(surplusStart, surplusEnd, surplusEnergy, hass);
    if (ui.power) {
      this._power(
        clipSeries(solarPoints, t0, t1),
        clipSeries(batteryPoints, t0, t1),
        clipSeries(socPoints, t0, t1),
        t0,
        t1,
        now,
      );
    }
    if (ui.gantt) this._gantt(loads, t0, t1, now, hass);
    if (ui.axis) this._axis(t0, t1, hass);
  }

  /** Each info box asks its own metric for a caption and a value. */
  _tiles(context) {
    for (const tile of this._ui.tiles) {
      if (!tile) continue;
      const shown = TILE_METRICS[tile.metric].read(context);
      tile.set(shown.v, shown.k);
    }
  }

  /**
   * The price curve as a colour ribbon rather than a line.
   *
   * A line would need its own y-axis and its own vertical space; what the
   * lanes below are being compared against is only ever "is this hour cheap",
   * which is one dimension, and one dimension fits in colour.
   */
  _ribbon(prices, buy, t0, t1, now, hass) {
    const ui = this._ui;
    ui.ribbon.textContent = "";
    ui.ribbonScale.textContent = "";
    ui.priceRow.setFigure(isUsable(buy) ? num(buy).toFixed(3) : "–");
    if (prices.length < 2) return;
    let lo = Infinity;
    let hi = -Infinity;
    for (const point of prices) {
      lo = Math.min(lo, point.v);
      hi = Math.max(hi, point.v);
    }
    const span = hi - lo || 1;
    // The second gap, not the first: the window opens mid-interval, so the
    // first point has been moved to the edge and its gap is a fragment.
    const step = prices.length > 2 ? prices[2].t - prices[1].t : prices[1].t - prices[0].t;

    // The ribbon is a flex row, so it only lines up with the lanes below if
    // its cells add up to the whole axis. Prices rarely cover it: a day-ahead
    // market publishes tomorrow in the early afternoon, so for most of the
    // morning the plan runs hours past the last known price. Those hours get
    // a blank cell rather than being absorbed by the last price -- stretching
    // 23:45's price across eleven unknown hours is a colour that means
    // nothing, in the one place on the card that is read as meaning.
    if (prices[0].t > t0) this._ribbonGap(t0, prices[0].t, hass);

    for (let i = 0; i < prices.length; i++) {
      const start = prices[i].t;
      const end = Math.min(i + 1 < prices.length ? prices[i + 1].t : start + step, t1);
      if (end <= start) continue;
      const cell = tag("i", "cell", ui.ribbon);
      cell.style.flexGrow = Math.max(end - start, 1);
      // Spent hours are faded rather than dropped: the ribbon is what the
      // lanes are read against, and a load that ran an hour ago still has to
      // have a price behind it.
      if (end <= now) cell.style.opacity = 0.45;
      const share = (prices[i].v - lo) / span;
      // Cheap is green, expensive is red, mixed in the middle: one hue ramp,
      // so a glance at the ribbon reads as a single quantity.
      cell.style.background = `color-mix(in srgb, var(--emh-bad) ${Math.round(
        share * 100,
      )}%, var(--emh-ok))`;
      cell.title = `${formatTime(start, hass)} · ${prices[i].v.toFixed(3)}`;
    }

    const covered = prices[prices.length - 1].t + step;
    if (covered < t1) this._ribbonGap(covered, t1, hass);
    tag("span", null, ui.ribbonScale, `cheapest ${lo.toFixed(3)}`);
    tag("span", null, ui.ribbonScale, `most expensive ${hi.toFixed(3)}`);
  }

  /** A stretch of the axis with no published price behind it. */
  _ribbonGap(from, to, hass) {
    const cell = tag("i", "cell gap", this._ui.ribbon);
    cell.style.flexGrow = Math.max(to - from, 1);
    cell.title = `${formatTime(from, hass)} → ${formatTime(to, hass)} · no prices published yet`;
  }

  _surplus(start, end, energy, hass) {
    const ui = this._ui;
    ui.surplusNote.textContent = "";
    if (!isUsable(start) || !isUsable(end)) {
      ui.surplusNote.style.display = "none";
      return;
    }
    ui.surplusNote.style.display = "";
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", "mdi:white-balance-sunny");
    ui.surplusNote.appendChild(icon);
    const kwh = num(energy);
    tag(
      "span",
      null,
      ui.surplusNote,
      `Spare solar ${formatTime(Date.parse(start.state), hass)} – ${formatTime(
        Date.parse(end.state),
        hass,
      )}${Number.isFinite(kwh) ? ` · ${kwh.toFixed(1)} kWh` : ""}`,
    );
  }

  /**
   * The solar figure, latched to today's highest so far.
   *
   * The window this card draws from is a rolling few hours of history plus
   * whatever forecast is still ahead, so once midday's real peak ages out of
   * that window the figure would otherwise fall back to the afternoon's lower
   * window max -- a solar number that drops as the sun goes down reads as a
   * fault. Bounding to today and latching it to the running max keeps the
   * day's actual best on screen until the day turns over, at which point it
   * has to fall since the new day hasn't produced one yet. Tomorrow's
   * forecast is excluded on purpose: this figure describes today, not a
   * preview of the plan running past midnight.
   */
  _solarDayPeak(points, now) {
    const day = new Date(now).toDateString();
    if (!this._solarPeak || this._solarPeak.day !== day) {
      this._solarPeak = { day, value: 0 };
    }
    const endOfToday = new Date(now).setHours(24, 0, 0, 0);
    const today = clipSeries(points, -Infinity, endOfToday);
    this._solarPeak.value = Math.max(this._solarPeak.value, integrate(today, endOfToday).peak);
    return this._solarPeak.value;
  }

  /**
   * Solar and the battery as profiles under the same ribbon as the loads.
   *
   * The figure on the right of each lane is that lane's *peak*, because the
   * profile has no y-axis: without it the tallest point is a shape rather than
   * a number. The energies go in the key underneath, where there is room to
   * say which is which.
   */
  _power(solarPoints, batteryPoints, socPoints, t0, t1, now) {
    const ui = this._ui;
    const solar = integrate(solarPoints, t1);
    const battery = integrate(batteryPoints, t1);
    const hasSolar = Boolean(ui.solarRow) && solarPoints.length > 0;
    const hasBattery = Boolean(ui.batteryRow) && batteryPoints.length > 0;

    if (ui.solarRow) {
      ui.solarRow.style.display = hasSolar ? "" : "none";
      if (hasSolar) {
        ui.solarRow.setLane(
          profileSvg({
            points: solarPoints,
            from: t0,
            to: t1,
            past: now,
            height: 26,
            color: "var(--emh-solar)",
          }),
        );
        ui.solarRow.setFigure(formatPower(this._solarDayPeak(solarPoints, now)));
        // Which record the shaded part is, named rather than implied: a
        // forecast and a meter reading look identical once they are drawn.
        ui.solarRow.title = `Solar forecast, today's peak on the right. ${
          this._config.solar_entity
            ? `Past: measured, from ${this._config.solar_entity}`
            : "Past: the forecast the plan was using at the time"
        }. Click for the history.`;
      }
    }

    if (ui.batteryRow) {
      ui.batteryRow.style.display = hasBattery ? "" : "none";
      if (hasBattery) {
        const showSoc = ui.showSoc && socPoints.length > 1;
        ui.batteryRow.setLane(
          profileSvg({
            points: batteryPoints,
            from: t0,
            to: t1,
            past: now,
            height: 30,
            signed: true,
            // The plan's own convention: positive is discharge, negative is
            // charge. Discharging is the battery doing the same job as the
            // solar lane above it, so it keeps the battery colour; charging is
            // a decision to spend, so it gets the accent instead.
            color: "var(--emh-battery)",
            negativeColor: "var(--emh-accent)",
            line: showSoc
              ? { points: socPoints, min: 0, max: 100, color: "var(--emh-dim)" }
              : null,
          }),
        );
        ui.batteryRow.setFigure(formatPower(battery.peak));
        ui.batteryRow.title = `Battery power: discharging above the line, charging below it. ${
          this._config.battery_entity
            ? `Past: measured, from ${this._config.battery_entity}`
            : "Past: the battery power the plan had for that moment"
        }. Click for the history.`;
      }
    }

    const visible = hasSolar || hasBattery;
    ui.powerSection.style.display = visible ? "" : "none";
    ui.power.style.display = visible ? "" : "none";

    ui.powerKey.textContent = "";
    ui.powerKey.style.display = visible ? "" : "none";
    const legend = [];
    if (hasSolar) legend.push(["var(--emh-solar)", `Solar ${formatEnergy(solar.up)}`, false]);
    if (hasBattery) {
      legend.push(["var(--emh-battery)", `Discharge ${formatEnergy(battery.up)}`, false]);
      legend.push(["var(--emh-accent)", `Charge ${formatEnergy(battery.down)}`, false]);
      if (ui.showSoc && socPoints.length > 1) {
        legend.push(["var(--emh-dim)", "Charge level 0–100 %", true]);
      }
    }
    for (const [color, text, isLine] of legend) {
      const item = tag("span", "leg", ui.powerKey);
      tag("i", `sw${isLine ? " line" : ""}`, item).style.background = color;
      tag("span", null, item, text);
    }
  }

  _gantt(loads, t0, t1, now, hass) {
    const ui = this._ui;
    ui.gantt.textContent = "";
    const visible = loads.length > 0;
    ui.loadsSection.style.display = visible ? "" : "none";
    ui.gantt.style.display = visible ? "" : "none";
    if (!visible) return;
    for (const view of loads) {
      const row = laneRow(
        ui.gantt,
        view.name,
        view.status === "running" || view.status === "should"
          ? "var(--emh-ok)"
          : "var(--emh-surface-2)",
      );
      row.setLane(
        trackSvg({
          points: clipSeries(view.points, t0, t1),
          from: t0,
          to: t1,
          past: now,
          deadline: view.deadline,
          height: 18,
          labels: false,
          color: view.isEnabled ? "var(--emh-solar)" : "var(--emh-dim)",
          hass,
        }),
      );
      row.setFigure(Number.isFinite(view.ranToday) ? formatHours(view.ranToday) : "–");
      row.title = "Filled blocks show when the plan expects this load to run. Click for its history.";
      row.addEventListener("click", () =>
        moreInfo(this, view.find("should_run") ? view.find("should_run").entity_id : null),
      );
    }
  }

  _axis(t0, t1, hass) {
    const ui = this._ui;
    ui.axis.textContent = "";
    for (let i = 0; i <= 4; i++) {
      tag("span", null, ui.axis, formatTime(t0 + ((t1 - t0) * i) / 4, hass));
    }
  }
}

EmhassOverviewCard.ticks = true;
EmhassOverviewCard.css = `
  /* One geometry for every row on the card. The lanes only mean anything read
     against each other, so the label and figure columns are declared once and
     the axis is inset by exactly those two widths. */
  .pad { --gcol-label: 82px; --gcol-figure: 58px; --gcol-gap: 10px; }
  .section { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em;
             color: var(--emh-dim); margin: 16px 0 6px 0; }
  .ribbon { display: flex; gap: 1px; height: 14px; border-radius: 4px;
            overflow: hidden; }
  .ribbon .cell { display: block; }
  .ribbon .gap { background: repeating-linear-gradient(135deg,
                 var(--emh-surface) 0 4px, var(--emh-surface-2) 4px 8px); }
  .scale { display: flex; justify-content: space-between; font-size: .68rem;
           color: var(--emh-dim); margin-top: 4px;
           padding-left: calc(var(--gcol-label) + var(--gcol-gap));
           padding-right: calc(var(--gcol-figure) + var(--gcol-gap)); }
  .surplus { display: flex; align-items: center; gap: 6px; font-size: .78rem;
             color: var(--emh-solar); margin-top: 10px; }
  .surplus ha-icon { --mdc-icon-size: 17px; }
  .surplus.tap { cursor: pointer; }
  .surplus.tap:hover { opacity: .8; }
  .gantt { display: grid; gap: 4px; }
  .grow-row { display: flex; align-items: center; gap: var(--gcol-gap);
              cursor: pointer; border-radius: 8px; padding: 2px 0;
              transition: background 160ms; }
  .grow-row:hover { background: var(--emh-surface); }
  .glabel { flex: 0 0 var(--gcol-label); display: flex; align-items: center;
            gap: 6px; font-size: .76rem; min-width: 0; }
  .glabel span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .gdot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; }
  .lane { flex: 1; min-width: 0; }
  .ghours { flex: 0 0 var(--gcol-figure); text-align: right; font-size: .74rem;
            color: var(--emh-dim); font-variant-numeric: tabular-nums; }
  .axis { display: flex; justify-content: space-between; font-size: .68rem;
          color: var(--emh-dim); margin-top: 6px;
          padding-left: calc(var(--gcol-label) + var(--gcol-gap));
          padding-right: calc(var(--gcol-figure) + var(--gcol-gap)); }
  .key { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 7px;
         font-size: .72rem; color: var(--emh-dim);
         padding-left: calc(var(--gcol-label) + var(--gcol-gap)); }
  .key .leg { display: inline-flex; align-items: center; gap: 5px; }
  .key .sw { width: 8px; height: 8px; border-radius: 2px; }
  .key .sw.line { height: 2px; border-radius: 1px; }
`;

/* ------------------------------------------------- overview visual editor */

const OVERVIEW_LABELS = {
  title: "Title",
  history_hours: "Hours of history",
  solar_entity: "Measured solar power",
  battery_entity: "Measured battery power",
  invert_battery: "That sensor is positive when charging",
};
const OVERVIEW_HELPERS = {
  history_hours: "How far back the window reaches. 0 starts the card at now.",
  solar_entity: "Left empty, the past is drawn from the forecast the plan used.",
  battery_entity:
    "Left empty, the Companion's own battery power sensor is used, and the plan's figure if it has none. Set here only to draw this card from a different meter.",
  invert_battery:
    "Only read when a sensor is named above; the Companion carries its own convention. The card draws positive as discharge, which is the plan's convention.",
};
for (const section of OVERVIEW_SECTIONS) {
  OVERVIEW_LABELS[section[0]] = section[1];
  OVERVIEW_HELPERS[section[0]] = section[2];
}

for (let i = 1; i <= TILE_DEFAULTS.length; i++) OVERVIEW_LABELS[`tile_${i}`] = `Box ${i}`;

const TILE_OPTIONS = TILE_ORDER.map((key) => ({ value: key, label: TILE_METRICS[key].label }));

const OVERVIEW_SCHEMA = [
  { name: "title", selector: { text: {} } },
  sectionGrid(OVERVIEW_SECTIONS),
  // Folded away by default: the window and the six dropdowns are worth having,
  // but not worth being the first thing between the title and the switches.
  {
    name: "",
    type: "expandable",
    title: "Window and history",
    icon: "mdi:history",
    schema: [
      {
        name: "history_hours",
        selector: { number: { min: 0, max: 12, step: 0.5, mode: "box", unit_of_measurement: "h" } },
      },
      { name: "solar_entity", selector: { entity: { domain: "sensor" } } },
      { name: "battery_entity", selector: { entity: { domain: "sensor" } } },
      { name: "invert_battery", selector: { boolean: {} } },
    ],
  },
  {
    name: "",
    type: "expandable",
    title: "Info boxes",
    icon: "mdi:view-grid-outline",
    schema: [
      {
        name: "",
        type: "grid",
        column_min_width: "220px",
        schema: TILE_DEFAULTS.map((_default, index) => ({
          name: `tile_${index + 1}`,
          selector: { select: { mode: "dropdown", options: TILE_OPTIONS } },
        })),
      },
    ],
  },
];

/** Which parts of the overview card to draw, and what its boxes carry. */
class EmhassOverviewCardEditor extends CardEditor {
  get labels() {
    return OVERVIEW_LABELS;
  }

  get helpers() {
    return OVERVIEW_HELPERS;
  }

  schema() {
    return OVERVIEW_SCHEMA;
  }

  data() {
    const data = { title: this._config.title || "" };
    for (const section of OVERVIEW_SECTIONS) {
      data[section[0]] = showsSection(this._config, section[0]);
    }
    for (let i = 1; i <= TILE_DEFAULTS.length; i++) {
      data[`tile_${i}`] = tileMetric(this._config, i);
    }
    const hours = Number(this._config.history_hours);
    data.history_hours = Number.isFinite(hours) ? hours : DEFAULT_HISTORY_HOURS;
    data.invert_battery = this._config.invert_battery === true;
    // Left out rather than set empty: an entity selector reads "" as a
    // selection that does not exist and clears itself on the next keystroke.
    if (this._config.solar_entity) data.solar_entity = this._config.solar_entity;
    if (this._config.battery_entity) data.battery_entity = this._config.battery_entity;
    return data;
  }

  clean(config) {
    cleanSections(config, OVERVIEW_SECTIONS);
    for (let i = 1; i <= TILE_DEFAULTS.length; i++) {
      if (config[`tile_${i}`] === TILE_DEFAULTS[i - 1]) delete config[`tile_${i}`];
    }
    if (!config.title) delete config.title;
    if (!config.solar_entity) delete config.solar_entity;
    if (!config.battery_entity) delete config.battery_entity;
    if (config.invert_battery !== true) delete config.invert_battery;
    if (
      config.history_hours === undefined ||
      Number(config.history_hours) === DEFAULT_HISTORY_HOURS
    ) {
      delete config.history_hours;
    }
    return config;
  }
}

customElements.define("emhass-overview-card", EmhassOverviewCard);
customElements.define("emhass-overview-card-editor", EmhassOverviewCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-overview-card",
    name: "EMHASS Companion overview",
    description: "Prices, solar, the battery and every load on one shared time axis.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);
