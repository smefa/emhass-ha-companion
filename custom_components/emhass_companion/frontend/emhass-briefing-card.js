/**
 * EMHASS Companion briefing card: the plan in words, then in a strip.
 *
 * An alternative to the overview card, drawn from the same sensors. The
 * overview card is a grid of boxes over a set of lanes; this one is written to
 * be read top to bottom the way a briefing is:
 *
 *   headline  what the house is doing right now, and until when
 *   figures   four live numbers, each one selectable
 *   day       price, solar, battery and loads as thin lanes on one time axis
 *   agenda    what happens next, in order: load starts, battery turns, the
 *             cheapest and the dearest price, the spare-solar window
 *   footer    the money
 *
 * Plain custom element, no library and nothing fetched at runtime. ES2017
 * syntax only (see emhass-core.js).
 */

import {
  balanceText,
  CardEditor,
  cleanSections,
  clipSeries,
  DEFAULT_HISTORY_HOURS,
  findHub,
  findLoads,
  formatAgo,
  formatCountdown,
  formatEnergy,
  formatHour,
  formatPower,
  formatSpan,
  formatTime,
  hourTicks,
  integrate,
  isUsable,
  labelFor,
  LiveCard,
  loadHaForm,
  loadView,
  measuredBy,
  mergeHistory,
  moreInfo,
  num,
  readHistory,
  sectionGrid,
  series,
  showsSection,
  stateOf,
  STUDIO_TOKENS,
  svg,
  tag,
  valueAt,
} from "./emhass-core.js?v=__VERSION__";

/* ---------------------------------------------------------------- options */

const BRIEFING_SECTIONS = [
  ["show_headline", "Headline", "What the house is doing now, in words"],
  ["show_gauge", "Battery gauge", "The charge level as a ring beside the headline"],
  ["show_figures", "Figures", "Four live numbers; choose them below"],
  ["show_price", "Price lane", "The import price across the window"],
  ["show_solar", "Solar lane", "Solar production across the window"],
  ["show_battery", "Battery lane", "The charge level across the window"],
  ["show_loads", "Loads lane", "When the deferrable loads run"],
  ["show_agenda", "Agenda", "What happens next, in order"],
  ["show_footer", "Money", "Forecast balance and today's figures"],
];

const HOUR = 3600000;
const FIGURE_SLOTS = ["figure_1", "figure_2", "figure_3", "figure_4"];
const FIGURE_DEFAULTS = ["solar", "house", "battery", "grid"];
const DEFAULT_AGENDA_ITEMS = 6;

const power = (value) => ({ v: formatPower(value) });
const money = (stateObj) => {
  const unit = stateObj && stateObj.attributes ? stateObj.attributes.unit_of_measurement : "";
  return isUsable(stateObj) ? `${Number(stateObj.state).toFixed(2)}${unit ? ` ${unit}` : ""}` : "–";
};

/**
 * What a figure can show. `source` is the hub sensor a click opens; `read`
 * turns the card's context into a value and an optional second line.
 */
const BRIEFING_FIGURES = {
  none: { label: "Nothing" },
  solar: { label: "Solar", swatch: "solar", source: "sensor.pv_forecast", read: (c) => power(c.pv) },
  house: {
    label: "House", swatch: "house line", source: "sensor.load_forecast", read: (c) => power(c.load),
  },
  battery: {
    label: "Battery",
    swatch: "battery",
    source: "sensor.battery_power",
    read: (c) => ({
      v: formatPower(Math.abs(c.battery)),
      n: !Number.isFinite(c.battery) || Math.abs(c.battery) < 1
        ? "idle"
        : c.battery > 0 ? "discharging" : "charging",
    }),
  },
  grid: {
    label: "Grid",
    swatch: "grid",
    source: "sensor.grid_forecast",
    read: (c) => ({
      v: formatPower(Math.abs(c.grid)),
      n: !Number.isFinite(c.grid) || Math.abs(c.grid) < 1
        ? "balanced"
        : c.grid > 0 ? "importing" : "exporting",
    }),
  },
  soc: {
    label: "Charge level",
    swatch: "battery line",
    source: "sensor.battery_soc",
    read: (c) => ({
      v: Number.isFinite(c.soc) ? `${Math.round(c.soc)} %` : "–",
      n: Number.isFinite(c.target) ? `target ${Math.round(c.target)} %` : "",
    }),
  },
  price: {
    label: "Price",
    swatch: "price",
    source: "sensor.buy_price",
    read: (c) => ({ v: Number.isFinite(c.buy) ? c.buy.toFixed(2) : "–", n: c.unit }),
  },
  sell_price: {
    label: "Export price",
    swatch: "price",
    source: "sensor.sell_price",
    read: (c) => ({ v: Number.isFinite(c.sell) ? c.sell.toFixed(2) : "–", n: c.unit }),
  },
  solar_ahead: {
    label: "Solar ahead",
    swatch: "solar",
    source: "sensor.pv_forecast",
    read: (c) => ({ v: formatEnergy(c.solarAhead), n: "in this plan" }),
  },
  surplus: {
    label: "Spare solar",
    swatch: "solar",
    source: "sensor.solar_surplus_energy",
    read: (c) => ({ v: isUsable(c.surplusEnergy) ? formatEnergy(num(c.surplusEnergy)) : "–" }),
  },
  balance: {
    label: "Balance, 24 h",
    source: "sensor.forecast_cost_24h",
    read: (c) => ({ v: balanceText(c.cost).text, n: "forecast" }),
  },
  savings: {
    label: "Savings, 24 h",
    source: "sensor.forecast_savings_24h",
    read: (c) => ({ v: money(c.savings), n: "forecast" }),
  },
  cost_today: {
    label: "Cost today",
    source: "sensor.energy_cost_today",
    read: (c) => ({ v: money(c.costToday) }),
  },
  saved_today: {
    label: "Saved today",
    source: "sensor.savings_today",
    read: (c) => ({ v: money(c.savedToday) }),
  },
  loads: {
    label: "Loads",
    swatch: "load",
    read: (c) => ({ v: `${c.loadsOn} / ${c.loadsTotal}`, n: "running" }),
  },
};
const BRIEFING_FIGURE_ORDER = Object.keys(BRIEFING_FIGURES);

/* ------------------------------------------------------------------ words */

const finite = (value) => (Number.isFinite(value) ? value : 0);

/** One word for what the house is doing with these four powers. */
function modeOf(pv, load, battery, grid) {
  const solar = finite(pv);
  if (finite(battery) < -50) {
    // "From solar" only while solar's spare covers most of the charge: a
    // little spare beside a large import is charging from the grid.
    const spare = solar - finite(load);
    return spare > 0 && spare >= -finite(battery) / 2 ? "charge_solar" : "charge_grid";
  }
  if (finite(battery) > 50) return "battery";
  if (finite(grid) < -50) return "selling";
  if (finite(grid) > 50) return solar > 50 ? "mixed" : "buying";
  if (solar > 50) return "solar";
  return "idle";
}

// [headline, the same thing mid-sentence]
const MODE_TEXT = {
  charge_solar: ["Charging the battery from solar", "charging from solar"],
  charge_grid: ["Charging the battery from the grid", "charging from the grid"],
  battery: ["Running on the battery", "running on the battery"],
  selling: ["Selling solar to the grid", "selling to the grid"],
  mixed: ["Solar and grid together", "solar and grid together"],
  buying: ["Buying from the grid", "buying from the grid"],
  solar: ["Running on solar", "running on solar"],
  idle: ["Standing by", "standing by"],
};

/** The interval a stepped series is published on. */
function stepOf(points, fallback) {
  let step = Infinity;
  for (let i = 1; i < points.length && i < 24; i++) {
    const gap = points[i].t - points[i - 1].t;
    if (gap > 0) step = Math.min(step, gap);
  }
  return Number.isFinite(step) ? step : fallback;
}

/* ------------------------------------------------------------------- card */

class EmhassBriefingCard extends LiveCard {
  static getStubConfig() {
    return { type: "custom:emhass-briefing-card" };
  }

  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-briefing-card-editor");
  }

  getCardSize() {
    return 8;
  }

  _shows(key) {
    return showsSection(this._config, key);
  }

  _figureKeys() {
    return FIGURE_SLOTS.map((slot, index) => {
      const chosen = this._config[slot];
      return chosen && BRIEFING_FIGURES[chosen] ? chosen : chosen ? "none" : FIGURE_DEFAULTS[index];
    });
  }

  build(card) {
    const pad = tag("div", "pad", card);
    const ui = { lanes: {} };
    const head = tag("div", "head", pad);
    const grow = tag("div", "grow", head);
    ui.name = tag("div", "eyebrow", grow);
    ui.sub = tag("div", "sub", grow);
    ui.pill = tag("div", "pill", head);
    tag("span", "dot", ui.pill);
    ui.pillText = tag("span", null, ui.pill);

    if (this._shows("show_headline")) {
      const hero = tag("div", "hero", pad);
      const words = tag("div", "words", hero);
      ui.headline = tag("div", "headline", words);
      ui.then = tag("div", "then", words);
      ui.snow = tag("div", "then", words);
      if (this._shows("show_gauge")) {
        ui.gauge = tag("div", "gauge", hero);
        ui.gauge.title = "Battery charge level. Click for its history.";
        ui.gauge.addEventListener("click", () => moreInfo(this, this._hub["sensor.battery_soc"]));
      }
    }

    if (this._shows("show_figures")) {
      const row = tag("div", "figures", pad);
      ui.figures = this._figureKeys()
        .filter((key) => key !== "none")
        .map((key) => {
          const spec = BRIEFING_FIGURES[key];
          const root = tag("div", "fig", row);
          const top = tag("div", "k", root);
          if (spec.swatch) tag("i", `swatch ${spec.swatch}`, top);
          tag("span", null, top, spec.label);
          const value = tag("div", "v", root, "–");
          const note = tag("div", "n", root, "");
          if (spec.source) {
            root.classList.add("tap");
            root.addEventListener("click", () => moreInfo(this, this._hub[spec.source]));
          }
          return { spec, value, note };
        });
      if (!ui.figures.length) row.remove();
    }

    const lanes = ["price", "solar", "battery", "loads"].filter((name) =>
      this._shows(`show_${name}`),
    );
    if (lanes.length) {
      ui.day = tag("div", "day", pad);
      const titles = { price: "Price", solar: "Solar", battery: "Battery", loads: "Loads" };
      for (const name of lanes) {
        const row = tag("div", "lane", ui.day);
        tag("div", "lk", row, titles[name]);
        const track = tag("div", `track ${name}`, row);
        const figure = tag("div", "lv", row, "");
        ui.lanes[name] = { row, track, figure };
      }
      const axisRow = tag("div", "lane axis", ui.day);
      tag("div", "lk", axisRow);
      ui.axis = tag("div", "track ticks", axisRow);
      tag("div", "lv", axisRow);
    }

    if (this._shows("show_agenda")) {
      ui.agendaWrap = tag("div", "agenda", pad);
      tag("div", "eyebrow", ui.agendaWrap, "Coming up");
      ui.agenda = tag("div", "items", ui.agendaWrap);
    }
    if (this._shows("show_footer")) ui.footer = tag("div", "totals", pad);

    this._ui = ui;
    this._hub = {};
    this._drawn = null;
  }

  _historyMs() {
    const hours = Number(this._config.history_hours);
    return (Number.isFinite(hours) && hours >= 0 ? hours : DEFAULT_HISTORY_HOURS) * HOUR;
  }

  _history(hass, hub, now) {
    const wanted = {
      solar: this._config.solar_entity || hub["sensor.pv_forecast"],
      soc: hub["sensor.battery_soc"],
      price: hub["sensor.buy_price"],
    };
    return readHistory(this, hass, wanted, [], this._historyMs(), now);
  }

  /** What the card's contents depend on; see the horizon card for why. */
  _signature(hass, hub, loads, past, now) {
    const parts = [Math.floor(now / 30000)];
    const stamp = (entityId) => {
      const state = stateOf(hass, entityId);
      parts.push(state ? `${state.state}@${state.last_updated}` : "-");
    };
    for (const key of Object.keys(hub).sort()) stamp(hub[key]);
    for (const load of loads) {
      for (const key of Object.keys(load.entities).sort()) stamp(load.entities[key]);
    }
    for (const name of Object.keys(past).sort()) {
      const list = past[name];
      parts.push(list.length ? `${list.length}:${list[list.length - 1].t}` : "0");
    }
    const measured = [this._config.solar_entity, this._config.battery_entity];
    const battery = measuredBy(hass, hub, "sensor.battery_power");
    if (battery) measured.push(battery.entity);
    for (const entityId of measured) stamp(entityId);
    const locale = hass.locale || {};
    parts.push(locale.language, locale.time_format);
    return parts.join("|");
  }

  /** A live reading where the house has a meter for it, the plan's otherwise. */
  _live(hass, hub) {
    const solarMeter = stateOf(hass, this._config.solar_entity);
    let battery = num(stateOf(hass, hub["sensor.battery_power"]));
    const chosen = this._config.battery_entity
      ? { entity: this._config.battery_entity, invert: this._config.invert_battery === true }
      : measuredBy(hass, hub, "sensor.battery_power");
    const meter = chosen ? stateOf(hass, chosen.entity) : null;
    if (isUsable(meter) && Number.isFinite(num(meter))) {
      battery = chosen.invert ? -num(meter) : num(meter);
    }
    // The measured charge level where the Companion has one: the plan's own
    // figure is what it expected, which is not the same thing.
    const socMeter = measuredBy(hass, hub, "sensor.battery_soc");
    const socState = socMeter ? stateOf(hass, socMeter.entity) : null;
    const measuredSoc = isUsable(socState) ? num(socState) : NaN;
    return {
      pv: isUsable(solarMeter) ? num(solarMeter) : num(stateOf(hass, hub["sensor.pv_forecast"])),
      load: num(stateOf(hass, hub["sensor.load_forecast"])),
      grid: num(stateOf(hass, hub["sensor.grid_forecast"])),
      battery,
      soc: Number.isFinite(measuredSoc) ? measuredSoc : num(stateOf(hass, hub["sensor.battery_soc"])),
      buy: num(stateOf(hass, hub["sensor.buy_price"])),
      sell: num(stateOf(hass, hub["sensor.sell_price"])),
    };
  }

  update(hass) {
    const ui = this._ui;
    const hub = findHub(hass);
    this._hub = hub;
    const now = Date.now();
    const past = this._history(hass, hub, now);
    const loadList = findLoads(hass);
    const signature = this._signature(hass, hub, loadList, past, now);
    if (signature === this._drawn) return;
    this._drawn = signature;

    const planned = (key) => series(stateOf(hass, hub[key]));
    const plan = {
      pv: planned("sensor.pv_forecast"),
      load: planned("sensor.load_forecast"),
      grid: planned("sensor.grid_forecast"),
      battery: planned("sensor.battery_power"),
      soc: planned("sensor.battery_soc"),
      buy: planned("sensor.buy_price"),
    };
    const status = stateOf(hass, hub["sensor.optimization_status"]);
    const stale = stateOf(hass, hub["binary_sensor.plan_stale"]);
    const isStale = Boolean(stale) && stale.state === "on";
    ui.name.textContent = this._config.title || "Energy briefing";
    ui.pill.className = `pill ${isStale ? "warn" : "on"}`;
    ui.pillText.textContent = isStale ? "Out of date" : status ? labelFor(hass, status) : "–";

    const loads = loadList.map((load) => loadView(hass, load)).filter((view) => view.isEnabled);
    const step = stepOf(plan.pv.length ? plan.pv : plan.battery, HOUR / 2);
    const priceStep = stepOf(plan.buy, step);
    const lists = [plan.pv, plan.load, plan.grid, plan.battery].filter((list) => list.length);
    let t1 = -Infinity;
    for (const list of lists) t1 = Math.max(t1, list[list.length - 1].t + step);
    if (!lists.length && plan.buy.length) t1 = plan.buy[plan.buy.length - 1].t + priceStep;
    const hasPlan = Number.isFinite(t1) && t1 > now;
    if (!hasPlan) t1 = now + step;
    const ahead = Number(this._config.horizon_hours);
    if (Number.isFinite(ahead) && ahead > 0) t1 = Math.min(t1, now + ahead * HOUR);
    let t0 = now - this._historyMs();
    let earliest = now;
    for (const name of Object.keys(past)) {
      if (past[name].length) earliest = Math.min(earliest, past[name][0].t);
    }
    t0 = Math.max(t0, Math.min(earliest, now));

    const staleAttrs = stale && stale.attributes ? stale.attributes : {};
    const plannedAt = staleAttrs.last_successful_run
      ? Date.parse(staleAttrs.last_successful_run)
      : NaN;
    ui.sub.textContent = hasPlan
      ? `Plan to ${formatSpan(now, t1, hass).split("→ ")[1]}` +
        (Number.isFinite(plannedAt) ? ` · updated ${formatAgo(plannedAt, hass)}` : "")
      : "No plan yet. It appears after the first optimisation runs.";

    const live = this._live(hass, hub);
    const target = stateOf(hass, hub["sensor.end_soc_target"]);
    const buyState = stateOf(hass, hub["sensor.buy_price"]);
    const context = Object.assign({}, live, {
      unit: buyState && buyState.attributes ? buyState.attributes.unit_of_measurement || "" : "",
      target: isUsable(target) ? Number(target.state) : NaN,
      solarAhead: integrate(clipSeries(plan.pv, now, t1), t1).up,
      surplusEnergy: stateOf(hass, hub["sensor.solar_surplus_energy"]),
      cost: stateOf(hass, hub["sensor.forecast_cost_24h"]),
      savings: stateOf(hass, hub["sensor.forecast_savings_24h"]),
      costToday: stateOf(hass, hub["sensor.energy_cost_today"]),
      savedToday: stateOf(hass, hub["sensor.savings_today"]),
      loadsOn: loads.filter((view) => view.status === "running" || view.status === "should").length,
      loadsTotal: loads.length,
    });

    if (ui.headline) this._headline(plan, live, hasPlan, step, now, t1, hass);
    if (ui.snow) this._snow(stateOf(hass, hub["binary_sensor.snow_cover"]));
    if (ui.gauge) this._gauge(live.soc);
    if (ui.figures) {
      for (const figure of ui.figures) {
        const shown = figure.spec.read(context);
        figure.value.textContent = shown.v;
        figure.note.textContent = shown.n || "";
      }
    }
    if (ui.day) {
      ui.day.style.display = hasPlan ? "" : "none";
      if (hasPlan) {
        this._day({ plan, past, loads, t0, t1, now, step, priceStep, hass, context });
      }
    }
    if (ui.agenda) this._agenda({ plan, loads, now, t1, step, priceStep, hass, hub, context });
    if (ui.footer) this._footer(context);
  }

  /* ------------------------------------------------------------- headline */

  _headline(plan, live, hasPlan, step, now, t1, hass) {
    const ui = this._ui;
    const mode = modeOf(live.pv, live.load, live.battery, live.grid);
    ui.headline.textContent = MODE_TEXT[mode][0];
    ui.then.textContent = "";
    if (!hasPlan) return;
    const at = (t) =>
      modeOf(
        valueAt(plan.pv, t, step),
        valueAt(plan.load, t, step),
        valueAt(plan.battery, t, step),
        valueAt(plan.grid, t, step),
      );
    // The plan's word for now, not the meters': the question is when the plan
    // moves on, and a cloud over the panels is not the plan moving on.
    const current = at(now);
    // When the meters and the plan disagree the headline is the meters', so
    // the line under it has to say that it is speaking for the plan.
    const agrees = current === mode;
    const lead = agrees ? "" : `The plan has the house ${MODE_TEXT[current][1]} `;
    const times = (plan.battery.length ? plan.battery : plan.pv).map((point) => point.t);
    for (const t of times) {
      if (t <= now || t >= t1) continue;
      const next = at(t);
      if (next === current) continue;
      ui.then.textContent = agrees
        ? `Until ${formatTime(t, hass)}, then ${MODE_TEXT[next][1]}`
        : `${lead}until ${formatTime(t, hass)}`;
      return;
    }
    ui.then.textContent = agrees ? "For the rest of the plan" : `${lead}for the rest of the plan`;
  }

  /** One line, only while the solar forecast is being scaled down for snow. */
  _snow(sensor) {
    const node = this._ui.snow;
    const factor = sensor && sensor.state === "on" ? Number(sensor.attributes.factor) : NaN;
    node.style.display = Number.isFinite(factor) ? "" : "none";
    if (!Number.isFinite(factor)) return;
    node.textContent =
      `Solar forecast reduced ${Math.round((1 - factor) * 100)} %: panels look snow covered`;
  }

  _gauge(soc) {
    const node = this._ui.gauge;
    node.textContent = "";
    node.style.display = Number.isFinite(soc) ? "" : "none";
    if (!Number.isFinite(soc)) return;
    const radius = 25;
    const around = 2 * Math.PI * radius;
    const share = Math.min(Math.max(soc, 0), 100) / 100;
    const root = svg("svg", { viewBox: "0 0 64 64", width: 64, height: 64 }, node);
    svg("circle", { class: "ring", cx: 32, cy: 32, r: radius }, root);
    svg("circle", {
      class: "ring fill", cx: 32, cy: 32, r: radius,
      "stroke-dasharray": `${(around * share).toFixed(1)} ${around.toFixed(1)}`,
      transform: "rotate(-90 32 32)",
    }, root);
    const text = svg("text", { class: "gv", x: 32, y: 36, "text-anchor": "middle" }, root);
    text.textContent = String(Math.round(soc));
    const unit = svg("text", { class: "gu", x: 32, y: 46, "text-anchor": "middle" }, root);
    unit.textContent = "%";
  }

  /* ------------------------------------------------------------ day strip */

  /** One lane's drawing surface: 1000 wide, stretched to whatever it gets. */
  _surface(lane, height, data) {
    lane.track.textContent = "";
    const root = svg("svg", {
      viewBox: `0 0 1000 ${height}`, preserveAspectRatio: "none", height,
    }, lane.track);
    const nowX = data.x(data.now);
    if (nowX > 0) svg("rect", { class: "past", x: 0, y: 0, width: nowX.toFixed(1), height }, root);
    return root;
  }

  _nowLine(root, height, data) {
    const nowX = data.x(data.now);
    if (nowX <= 0 || nowX >= 1000) return;
    svg("line", { class: "now", x1: nowX.toFixed(1), x2: nowX.toFixed(1), y1: 0, y2: height }, root);
  }

  _day(data) {
    const ui = this._ui;
    const x = (t) => ((t - data.t0) / (data.t1 - data.t0)) * 1000;
    data.x = x;
    const fixed = (value) => value.toFixed(1);

    if (ui.lanes.price) {
      const lane = ui.lanes.price;
      const points = clipSeries(mergeHistory(data.past.price || [], data.plan.buy), data.t0, data.t1);
      lane.row.style.display = points.length ? "" : "none";
      const root = this._surface(lane, 18, data);
      let lo = Infinity;
      let hi = -Infinity;
      for (const point of points) {
        lo = Math.min(lo, point.v);
        hi = Math.max(hi, point.v);
      }
      const spread = hi - lo || 1;
      for (let i = 0; i < points.length; i++) {
        const end = Math.min(
          i + 1 < points.length ? points[i + 1].t : points[i].t + data.priceStep,
          data.t1,
        );
        if (end <= points[i].t) continue;
        const share = (points[i].v - lo) / spread;
        // Keep the graph heights, with the overview ribbon's cheap-to-dear colours.
        const tall = 4 + share * 14;
        const cell = svg("rect", {
          fill: `color-mix(in srgb, var(--emh-bad) ${Math.round(share * 100)}%, var(--emh-ok))`,
          x: fixed(x(points[i].t)),
          y: fixed(18 - tall),
          width: fixed(Math.max(x(end) - x(points[i].t) - 1, 0.5)),
          height: fixed(tall),
        }, root);
        const tip = svg("title", null, cell);
        tip.textContent = `${formatTime(points[i].t, data.hass)} · ${points[i].v.toFixed(2)} ${data.context.unit}`;
      }
      this._nowLine(root, 18, data);
      lane.figure.textContent = Number.isFinite(data.context.buy) ? data.context.buy.toFixed(2) : "";
    }

    if (ui.lanes.solar) {
      const lane = ui.lanes.solar;
      const points = clipSeries(mergeHistory(data.past.solar || [], data.plan.pv), data.t0, data.t1);
      lane.row.style.display = points.length ? "" : "none";
      const root = this._surface(lane, 26, data);
      let peak = 0;
      for (const point of points) peak = Math.max(peak, point.v);
      if (peak > 0) {
        let d = `M${fixed(x(points[0].t))},26`;
        for (const point of points) {
          d += `L${fixed(x(point.t))},${fixed(25 - (Math.max(point.v, 0) / peak) * 23)}`;
        }
        const lastT = Math.min(points[points.length - 1].t + data.step, data.t1);
        d += `L${fixed(x(lastT))},26Z`;
        svg("path", { class: "sun", d }, root);
      }
      this._nowLine(root, 26, data);
      lane.figure.textContent = formatEnergy(data.context.solarAhead);
      lane.figure.title = "Solar still to come in this plan";
    }

    if (ui.lanes.battery) {
      const lane = ui.lanes.battery;
      const points = clipSeries(mergeHistory(data.past.soc || [], data.plan.soc), data.t0, data.t1);
      lane.row.style.display = points.length ? "" : "none";
      const root = this._surface(lane, 26, data);
      if (points.length) {
        const y = (value) => 25 - (Math.min(Math.max(value, 0), 100) / 100) * 23;
        let d = "";
        for (let k = 0; k < points.length; k++) {
          d += `${k ? "L" : "M"}${fixed(x(points[k].t))},${fixed(y(points[k].v))}`;
        }
        const first = fixed(x(points[0].t));
        const last = fixed(x(points[points.length - 1].t));
        svg("path", { class: "wash", d: `${d}L${last},26L${first},26Z` }, root);
        svg("path", { class: "level", d }, root);
      }
      this._nowLine(root, 26, data);
      lane.figure.textContent = Number.isFinite(data.context.soc)
        ? `${Math.round(data.context.soc)} %`
        : "";
    }

    if (ui.lanes.loads) {
      const lane = ui.lanes.loads;
      lane.row.style.display = data.loads.length ? "" : "none";
      const root = this._surface(lane, 14, data);
      svg("rect", { class: "rail", x: 0, y: 4, width: 1000, height: 6, rx: 3 }, root);
      let booked = 0;
      for (const view of data.loads) {
        for (const run of view.runs) {
          const from = Math.max(run.start, data.t0);
          const to = Math.min(run.end, data.t1);
          if (to <= from) continue;
          if (to > data.now) booked++;
          const block = svg("rect", {
            class: to <= data.now ? "run done" : "run",
            x: fixed(x(from)), y: 4, width: fixed(Math.max(x(to) - x(from), 4)), height: 6, rx: 3,
          }, root);
          const tip = svg("title", null, block);
          tip.textContent = `${view.name} · ${formatTime(run.start, data.hass)} – ${formatTime(run.end, data.hass)}`;
        }
      }
      this._nowLine(root, 14, data);
      lane.figure.textContent = booked ? `${booked} ${booked === 1 ? "run" : "runs"}` : "none";
    }

    ui.axis.textContent = "";
    const language = data.hass.locale ? data.hass.locale.language : undefined;
    const room = ui.axis.clientWidth || 400;
    for (const tick of hourTicks(data.t0, data.t1, room < 300 ? 3 : 6)) {
      const share = (tick.t - data.t0) / (data.t1 - data.t0);
      if (share < 0.03 || share > 0.97) continue;
      const label = tag(
        "span",
        tick.midnight ? "strong" : null,
        ui.axis,
        tick.midnight
          ? new Date(tick.t).toLocaleDateString(language, { weekday: "short" })
          : formatHour(tick.t, data.hass),
      );
      label.style.left = `${(share * 100).toFixed(2)}%`;
    }
  }

  /* --------------------------------------------------------------- agenda */

  /** Everything worth knowing about that has not happened yet, in order. */
  _events(data) {
    const events = [];
    const hass = data.hass;
    const push = (t, kind, title, detail, entityId) => {
      if (t > data.now && t < data.t1) events.push({ t, kind, title, detail, entityId });
    };

    for (const view of data.loads) {
      for (const run of view.upcoming) {
        const watts = Number.isFinite(run.watts) && run.watts > 0 ? ` · ${formatPower(run.watts)}` : "";
        push(
          run.start,
          "load",
          `${view.name} starts`,
          `until ${formatTime(run.end, hass)}${watts}`,
          view.scheduled ? view.scheduled.entity_id : null,
        );
      }
    }

    // The battery's turns: each stretch of charging or discharging gets one
    // line, at the moment it begins, with the energy it moves.
    // The whole series, not one clipped to now: a stretch already under way
    // starts in the past and `push` drops it, one that starts later is news.
    const battery = data.plan.battery.filter((point) => point.t < data.t1);
    const sign = (value) => (value > 100 ? 1 : value < -100 ? -1 : 0);
    let i = 0;
    while (i < battery.length) {
      const side = sign(battery[i].v);
      let end = i;
      while (end < battery.length && sign(battery[end].v) === side) end++;
      const until = end < battery.length ? battery[end].t : data.t1;
      if (side !== 0) {
        const moved = integrate(battery.slice(i, end), until);
        push(
          battery[i].t,
          "battery",
          side > 0 ? "Battery starts discharging" : "Battery starts charging",
          `${formatEnergy(side > 0 ? moved.up : moved.down)} until ${formatTime(until, hass)}`,
          this._hub["sensor.battery_power"],
        );
      }
      i = end;
    }

    const soc = clipSeries(data.plan.soc, data.now, data.t1);
    for (let k = 1; k < soc.length; k++) {
      if (soc[k].v >= 99.5 && soc[k - 1].v < 99.5) {
        push(soc[k].t, "battery", "Battery full", "", this._hub["sensor.battery_soc"]);
        break;
      }
    }

    let cheapest = null;
    let dearest = null;
    for (const point of data.plan.buy) {
      if (point.t <= data.now || point.t >= data.t1) continue;
      if (!cheapest || point.v < cheapest.v) cheapest = point;
      if (!dearest || point.v > dearest.v) dearest = point;
    }
    if (cheapest && dearest && dearest.v - cheapest.v > 1e-9) {
      const priceId = this._hub["sensor.buy_price"];
      const unit = data.context.unit ? ` ${data.context.unit}` : "";
      push(cheapest.t, "price", "Cheapest price", `${cheapest.v.toFixed(2)}${unit}`, priceId);
      push(dearest.t, "price", "Price peak", `${dearest.v.toFixed(2)}${unit}`, priceId);
    }

    const spareStart = stateOf(hass, data.hub["sensor.solar_surplus_start"]);
    const spareEnd = stateOf(hass, data.hub["sensor.solar_surplus_end"]);
    if (isUsable(spareStart) && isUsable(spareEnd)) {
      const energy = num(data.context.surplusEnergy);
      push(
        Date.parse(spareStart.state),
        "solar",
        "Spare solar begins",
        `${Number.isFinite(energy) ? `${formatEnergy(energy)} ` : ""}until ${formatTime(
          Date.parse(spareEnd.state),
          hass,
        )}`,
        data.hub["sensor.solar_surplus_energy"],
      );
    }
    return events.sort((a, b) => a.t - b.t);
  }

  _agenda(data) {
    const ui = this._ui;
    ui.agenda.textContent = "";
    const wanted = Number(this._config.agenda_items);
    const limit = Number.isFinite(wanted) && wanted > 0 ? wanted : DEFAULT_AGENDA_ITEMS;
    const events = this._events(data).slice(0, limit);
    ui.agendaWrap.style.display = events.length ? "" : "none";
    const language = data.hass.locale ? data.hass.locale.language : undefined;
    const dayOf = (t) => new Date(t).setHours(0, 0, 0, 0);
    let day = dayOf(data.now);
    for (const event of events) {
      if (dayOf(event.t) !== day) {
        day = dayOf(event.t);
        const offset = Math.round((day - dayOf(data.now)) / 86400000);
        tag(
          "div",
          "divider",
          ui.agenda,
          offset === 1
            ? "Tomorrow"
            : new Date(day).toLocaleDateString(language, { weekday: "long" }),
        );
      }
      const row = tag("div", "item", ui.agenda);
      tag("div", "at", row, formatTime(event.t, data.hass));
      tag("i", `swatch ${event.kind}`, row);
      const text = tag("div", "what", row);
      tag("span", "title", text, event.title);
      if (event.detail) tag("span", "detail", text, event.detail);
      tag("div", "in", row, `in ${formatCountdown(event.t - data.now)}`);
      if (event.entityId) {
        row.classList.add("tap");
        row.addEventListener("click", () => moreInfo(this, event.entityId));
      }
    }
  }

  /* --------------------------------------------------------------- footer */

  _footer(context) {
    const ui = this._ui;
    ui.footer.textContent = "";
    const item = (label, stateObj, text, source) => {
      if (!isUsable(stateObj)) return;
      const root = tag("div", "total tap", ui.footer);
      tag("div", "eyebrow", root, label);
      tag("div", "v", root, text);
      root.addEventListener("click", () => moreInfo(this, this._hub[source]));
    };
    item("Balance, 24 h", context.cost, balanceText(context.cost).text, "sensor.forecast_cost_24h");
    item("Savings, 24 h", context.savings, money(context.savings), "sensor.forecast_savings_24h");
    item("Cost today", context.costToday, money(context.costToday), "sensor.energy_cost_today");
    item("Saved today", context.savedToday, money(context.savedToday), "sensor.savings_today");
    ui.footer.style.display = ui.footer.childNodes.length ? "" : "none";
  }
}

EmhassBriefingCard.ticks = true;

EmhassBriefingCard.css = STUDIO_TOKENS + `
  .pad { padding: 16px 18px 16px 18px; }
  .head .eyebrow { color: var(--emh-s-ink); }
  .tap { cursor: pointer; }

  .hero { display: flex; align-items: center; gap: 16px; margin-top: 18px; }
  .words { flex: 1; min-width: 0; }
  .headline { font-size: 1.62rem; font-weight: 600; line-height: 1.16; letter-spacing: -.025em;
              text-wrap: balance; }
  .then { font-size: .9rem; color: var(--emh-dim); margin-top: 6px; }
  .gauge { flex: 0 0 auto; cursor: pointer; }
  .gauge svg { width: 64px; height: 64px; }
  .ring { fill: none; stroke: var(--emh-surface-2); stroke-width: 5; }
  .ring.fill { stroke: var(--emh-s-battery); stroke-linecap: round; }
  .gv { font-family: inherit; font-size: 17px; font-weight: 600; fill: var(--emh-s-ink);
        letter-spacing: -.02em; }
  .gu { font-family: inherit; font-size: 8.5px; fill: var(--emh-dim); }

  .pad { container-type: inline-size; }
  .figures { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr);
             margin-top: 18px; border-top: 1px solid var(--emh-hairline); }
  @container (max-width: 400px) {
    .figures { grid-auto-flow: row; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  .fig { padding: 10px 10px 9px 0; min-width: 0; }
  .fig .k { display: flex; align-items: center; gap: 5px; font-size: .7rem;
            color: var(--emh-dim); white-space: nowrap; }
  .fig .v { font-size: 1.08rem; font-weight: 600; margin-top: 3px; letter-spacing: -.01em;
            white-space: nowrap; }
  .fig .n { font-size: .68rem; color: var(--emh-dim); min-height: 1em; white-space: nowrap; }
  .fig.tap:hover .v { color: var(--emh-accent); }

  .day { margin-top: 4px; padding-top: 9px; border-top: 1px solid var(--emh-hairline); }
  .lane { display: flex; align-items: center; gap: 10px; margin-top: 7px; }
  .lk { flex: 0 0 50px; font-size: .7rem; color: var(--emh-dim); }
  .lv { flex: 0 0 62px; text-align: right; font-size: .78rem; font-weight: 600;
        white-space: nowrap; }
  .track { flex: 1; min-width: 0; position: relative; }
  .track svg { width: 100%; display: block; border-radius: 3px; }
  .past { fill: var(--emh-surface); }
  .now { stroke: var(--emh-s-ink); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
  .sun { fill: var(--emh-s-solar); fill-opacity: .88; }
  .wash { fill: var(--emh-s-battery); fill-opacity: .14; }
  .level { fill: none; stroke: var(--emh-s-battery); stroke-width: 2;
           vector-effect: non-scaling-stroke; stroke-linejoin: round; }
  .rail { fill: var(--emh-surface); }
  .run { fill: var(--emh-accent); }
  .run.done { fill-opacity: .45; }
  .lane.axis { margin-top: 3px; }
  .ticks { height: 15px; }
  .ticks span { position: absolute; top: 0; transform: translateX(-50%); font-size: .66rem;
                color: var(--emh-dim); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .ticks span.strong { color: var(--emh-s-ink); font-weight: 600; }

  .agenda { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--emh-hairline); }
  .items { margin-top: 6px; }
  .item { display: flex; align-items: center; gap: 10px; padding: 7px 0; font-size: .86rem; }
  .item + .item { border-top: 1px solid var(--emh-hairline); }
  .item .at { flex: 0 0 44px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .item .what { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden;
                text-overflow: ellipsis; }
  .item .detail { color: var(--emh-dim); margin-left: 8px; font-size: .78rem; }
  .item .in { flex: 0 0 auto; color: var(--emh-dim); font-size: .74rem;
              font-variant-numeric: tabular-nums; }
  .item.tap:hover .title { color: var(--emh-accent); }
  .divider { font-size: .66rem; font-weight: 600; letter-spacing: .09em; text-transform: uppercase;
             color: var(--emh-dim); padding: 10px 0 4px 0;
             border-top: 1px solid var(--emh-hairline); }
  .divider:first-child { border-top: 0; padding-top: 4px; }

  .totals { display: flex; flex-wrap: wrap; gap: 10px 28px; margin-top: 14px;
            padding-top: 13px; border-top: 1px solid var(--emh-hairline); }
  .total .v { font-size: .98rem; font-weight: 600; margin-top: 3px; letter-spacing: -.01em; }
`;

/* ----------------------------------------------------------------- editor */

const BRIEFING_LABELS = {
  title: "Title",
  history_hours: "Hours of history",
  horizon_hours: "Hours ahead",
  agenda_items: "Agenda length",
  solar_entity: "Measured solar power",
  battery_entity: "Measured battery power",
  invert_battery: "That battery sensor is positive when charging",
};
const BRIEFING_HELPERS = {
  history_hours: "How far back the lanes reach. 0 starts them at now.",
  horizon_hours: "How far ahead the lanes and the agenda reach. 0 uses the whole plan.",
  agenda_items: "The most lines the agenda shows.",
  solar_entity: "Left empty, the solar figure and the past are the forecast the plan used.",
  battery_entity:
    "Left empty, the Companion's own battery power sensor is used, and the plan's figure if it has none.",
  invert_battery: "Only read when a sensor is named above.",
};
for (const section of BRIEFING_SECTIONS) {
  BRIEFING_LABELS[section[0]] = section[1];
  BRIEFING_HELPERS[section[0]] = section[2];
}
FIGURE_SLOTS.forEach((slot, index) => {
  BRIEFING_LABELS[slot] = `Figure ${index + 1}`;
});

const BRIEFING_SCHEMA = [
  { name: "title", selector: { text: {} } },
  sectionGrid(BRIEFING_SECTIONS),
  {
    name: "",
    type: "expandable",
    title: "Figures",
    icon: "mdi:numeric",
    schema: [
      {
        name: "",
        type: "grid",
        column_min_width: "200px",
        schema: FIGURE_SLOTS.map((slot) => ({
          name: slot,
          selector: {
            select: {
              mode: "dropdown",
              options: BRIEFING_FIGURE_ORDER.map((key) => ({
                value: key,
                label: BRIEFING_FIGURES[key].label,
              })),
            },
          },
        })),
      },
    ],
  },
  {
    name: "",
    type: "expandable",
    title: "Window, agenda and meters",
    icon: "mdi:history",
    schema: [
      {
        name: "",
        type: "grid",
        column_min_width: "180px",
        schema: [
          {
            name: "history_hours",
            selector: { number: { min: 0, max: 12, step: 0.5, mode: "box", unit_of_measurement: "h" } },
          },
          {
            name: "horizon_hours",
            selector: { number: { min: 0, max: 72, step: 1, mode: "box", unit_of_measurement: "h" } },
          },
          { name: "agenda_items", selector: { number: { min: 1, max: 20, step: 1, mode: "box" } } },
        ],
      },
      {
        name: "",
        type: "grid",
        column_min_width: "260px",
        schema: ["solar_entity", "battery_entity"].map((name) => ({
          name,
          selector: { entity: { filter: { domain: "sensor" } } },
        })),
      },
      { name: "invert_battery", selector: { boolean: {} } },
    ],
  },
];

class EmhassBriefingCardEditor extends CardEditor {
  get labels() {
    return BRIEFING_LABELS;
  }

  get helpers() {
    return BRIEFING_HELPERS;
  }

  schema() {
    return BRIEFING_SCHEMA;
  }

  data() {
    const data = { title: this._config.title || "" };
    for (const section of BRIEFING_SECTIONS) {
      data[section[0]] = showsSection(this._config, section[0]);
    }
    FIGURE_SLOTS.forEach((slot, index) => {
      const chosen = this._config[slot];
      data[slot] = chosen && BRIEFING_FIGURES[chosen] ? chosen : FIGURE_DEFAULTS[index];
    });
    const hours = Number(this._config.history_hours);
    data.history_hours = Number.isFinite(hours) ? hours : DEFAULT_HISTORY_HOURS;
    const ahead = Number(this._config.horizon_hours);
    data.horizon_hours = Number.isFinite(ahead) ? ahead : 0;
    const items = Number(this._config.agenda_items);
    data.agenda_items = Number.isFinite(items) && items > 0 ? items : DEFAULT_AGENDA_ITEMS;
    data.invert_battery = this._config.invert_battery === true;
    for (const key of ["solar_entity", "battery_entity"]) {
      if (this._config[key]) data[key] = this._config[key];
    }
    return data;
  }

  clean(config) {
    cleanSections(config, BRIEFING_SECTIONS);
    if (!config.title) delete config.title;
    FIGURE_SLOTS.forEach((slot, index) => {
      if (!config[slot] || config[slot] === FIGURE_DEFAULTS[index]) delete config[slot];
    });
    for (const key of ["solar_entity", "battery_entity"]) {
      if (!config[key]) delete config[key];
    }
    if (config.invert_battery !== true) delete config.invert_battery;
    if (
      config.history_hours === undefined ||
      Number(config.history_hours) === DEFAULT_HISTORY_HOURS
    ) {
      delete config.history_hours;
    }
    if (!Number(config.horizon_hours)) delete config.horizon_hours;
    if (!Number(config.agenda_items) || Number(config.agenda_items) === DEFAULT_AGENDA_ITEMS) {
      delete config.agenda_items;
    }
    return config;
  }
}

customElements.define("emhass-briefing-card", EmhassBriefingCard);
customElements.define("emhass-briefing-card-editor", EmhassBriefingCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-briefing-card",
    name: "EMHASS Companion overview (briefing)",
    description:
      "The plan as a briefing: what the house is doing now and until when, live figures, a day strip and an agenda of what happens next.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);
