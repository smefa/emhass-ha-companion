/**
 * EMHASS Companion horizon card: the plan as an energy balance.
 *
 * An alternative to the plan card, drawn from the same sensors. Where the plan
 * card overlays five lines on one panel, this one answers a different question
 * per panel and never shares a y-axis between two units:
 *
 *   power    where the house's energy comes from (stacked above zero) and
 *            where the spare goes (below it), with consumption as a line
 *   price    one bar per price interval, darker when dearer
 *   battery  the charge level on its own 0-100 % scale
 *   loads    one thin rail per deferrable load
 *
 * A readout above the chart names every series and gives its value at the
 * cursor, so it doubles as the legend; with no cursor it reads "now".
 *
 * Plain custom element, inline SVG in real pixels, no library and nothing
 * fetched at runtime. ES2017 syntax only (see emhass-core.js).
 */

import {
  balanceText,
  CardEditor,
  cleanSections,
  clipSeries,
  DEFAULT_PLAN_HISTORY_HOURS,
  findHub,
  findLoads,
  formatAgo,
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
  niceScale,
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

const HORIZON_SECTIONS = [
  ["show_readout", "Readout", "Every series and its value at the cursor, or now"],
  ["show_power", "Power panel", "Where the energy comes from and where it goes"],
  ["show_price", "Price panel", "One bar per price interval"],
  ["show_soc", "Battery panel", "The planned charge level, 0 to 100 %"],
  ["show_loads", "Load rows", "One rail per deferrable load"],
  ["show_totals", "Totals", "Energy and balance for the rest of the plan"],
];

const HORIZON_SERIES = [
  ["show_solar", "Solar", "Solar production in the power panel"],
  ["show_battery", "Battery", "Battery charge and discharge in the power panel"],
  ["show_grid", "Grid", "Import and export in the power panel"],
  ["show_house", "Consumption", "The house consumption line"],
  ["show_sell", "Export price", "The export price line in the price panel"],
  ["show_extremes", "Price labels", "Name the cheapest and the dearest interval ahead"],
];

// Where each series' recorded side comes from; the same options as the plan
// card, so a config can be moved between the two by changing `type:` alone.
const HORIZON_HISTORY = [
  ["pv", "sensor.pv_forecast", "solar_entity"],
  ["load", "sensor.load_forecast", "house_entity"],
  ["grid", "sensor.grid_forecast", "grid_entity"],
  ["battery", "sensor.battery_power", "battery_entity"],
  ["soc", "sensor.battery_soc", null],
  ["buy", "sensor.buy_price", null],
  ["sell", "sensor.sell_price", null],
];

const HOUR = 3600000;
const FALLBACK_STEP = HOUR / 2;

/* ------------------------------------------------------------------ maths */

/** The interval a stepped series is published on. */
function stepOf(points) {
  let step = Infinity;
  for (let i = 1; i < points.length && i < 24; i++) {
    const gap = points[i].t - points[i - 1].t;
    if (gap > 0) step = Math.min(step, gap);
  }
  return Number.isFinite(step) ? step : NaN;
}

/**
 * A stepped series averaged onto a regular grid, NaN where it says nothing.
 *
 * The plan's series are already on the grid; recorded history is not, and a
 * stack can only be built from series that share their x positions.
 */
function binned(points, starts, step, end) {
  const out = [];
  let i = 0;
  for (const start of starts) {
    const stop = start + step;
    while (i + 1 < points.length && points[i + 1].t <= start) i++;
    let sum = 0;
    let covered = 0;
    for (let j = i; j < points.length && points[j].t < stop; j++) {
      const from = Math.max(points[j].t, start);
      const to = Math.min(j + 1 < points.length ? points[j + 1].t : end, stop);
      if (to > from) {
        sum += points[j].v * (to - from);
        covered += to - from;
      }
    }
    out.push(covered > 0 ? sum / covered : NaN);
  }
  return out;
}

const px = (value) => Math.round(value * 10) / 10;

/** The outline of one stacked layer: along its top, back along its bottom. */
function bandPath(edges, lower, upper, y) {
  const filled = (index) => Math.abs(upper[index] - lower[index]) > 1e-6;
  let d = "";
  let i = 0;
  while (i < upper.length) {
    if (!filled(i)) {
      i++;
      continue;
    }
    let end = i;
    while (end < upper.length && filled(end)) end++;
    d += `M${px(edges[i])},${px(y(upper[i]))}`;
    for (let k = i; k < end; k++) {
      d += `H${px(edges[k + 1])}`;
      if (k + 1 < end) d += `V${px(y(upper[k + 1]))}`;
    }
    d += `V${px(y(lower[end - 1]))}`;
    for (let m = end - 1; m >= i; m--) {
      d += `H${px(edges[m])}`;
      if (m > i) d += `V${px(y(lower[m - 1]))}`;
    }
    d += "Z";
    i = end;
  }
  return d;
}

/** A stepped line over the same grid, broken where the series is silent. */
function stepPath(edges, values, y) {
  let d = "";
  let open = false;
  for (let i = 0; i < values.length; i++) {
    if (!Number.isFinite(values[i])) {
      open = false;
      continue;
    }
    d += open ? `V${px(y(values[i]))}` : `M${px(edges[i])},${px(y(values[i]))}`;
    d += `H${px(edges[i + 1])}`;
    open = true;
  }
  return d;
}

/* ------------------------------------------------------------------- card */

class EmhassHorizonCard extends LiveCard {
  static getStubConfig() {
    return { type: "custom:emhass-horizon-card" };
  }

  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-horizon-card-editor");
  }

  getCardSize() {
    return 9;
  }

  build(card) {
    const pad = tag("div", "pad", card);
    const head = tag("div", "head", pad);
    const grow = tag("div", "grow", head);
    const ui = { rows: {} };
    ui.name = tag("div", "name", grow);
    ui.sub = tag("div", "sub", grow);
    ui.pill = tag("div", "pill", head);
    tag("span", "dot", ui.pill);
    ui.pillText = tag("span", null, ui.pill);

    if (showsSection(this._config, "show_readout")) ui.readout = tag("div", "readout", pad);
    ui.body = tag("div", "plot", pad);
    if (showsSection(this._config, "show_totals")) ui.totals = tag("div", "totals", pad);
    this._ui = ui;
    this._drawn = null;
    this._cursor = null;

    if (this._resizer) this._resizer.disconnect();
    this._resizer = null;
    this._observe();
  }

  /** The chart is drawn in real pixels, so a new width is a new drawing. */
  _observe() {
    if (this._resizer || !this._ui || !this.isConnected) return;
    if (typeof ResizeObserver !== "function") return;
    const body = this._ui.body;
    this._resizer = new ResizeObserver(() => {
      if (Math.abs(Math.round(body.clientWidth) - (this._width || 0)) > 1) this.refresh();
    });
    this._resizer.observe(body);
  }

  connectedCallback() {
    super.connectedCallback();
    this._observe();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._resizer) this._resizer.disconnect();
    this._resizer = null;
  }

  _shows(key) {
    return showsSection(this._config, key);
  }

  _historyMs() {
    const hours = Number(this._config.history_hours);
    return (Number.isFinite(hours) && hours >= 0 ? hours : DEFAULT_PLAN_HISTORY_HOURS) * HOUR;
  }

  _history(hass, hub, now) {
    const wanted = {};
    const invert = [];
    const measured = measuredBy(hass, hub, "sensor.battery_power");
    for (const [name, key, option] of HORIZON_HISTORY) {
      const chosen = option ? this._config[option] : null;
      if (name === "battery") {
        const source = chosen
          ? { entity: chosen, invert: this._config.invert_battery === true }
          : measured;
        wanted[name] = source ? source.entity : hub[key];
        if (source && source.invert) invert.push(name);
        continue;
      }
      wanted[name] = chosen || hub[key];
    }
    return readHistory(this, hass, wanted, invert, this._historyMs(), now);
  }

  /**
   * What the drawing depends on, as one string.
   *
   * Home Assistant hands every card a new `hass` on every state change in the
   * house. Redrawing a few hundred SVG nodes for a doorbell is waste, and it
   * would also throw away the cursor under someone's finger.
   */
  _signature(hass, hub, loads, past, width, now) {
    const parts = [width, Math.floor(now / 60000)];
    const stamp = (entityId) => {
      const state = stateOf(hass, entityId);
      parts.push(state ? `${state.state}@${state.last_updated}` : "-");
    };
    for (const key of Object.keys(hub).sort()) stamp(hub[key]);
    for (const load of loads) {
      for (const key of ["scheduled_power", "should_run", "running"]) stamp(load.entities[key]);
    }
    for (const name of Object.keys(past).sort()) {
      const list = past[name];
      parts.push(list.length ? `${list.length}:${list[list.length - 1].t}` : "0");
    }
    const locale = hass.locale || {};
    parts.push(locale.language, locale.time_format);
    return parts.join("|");
  }

  update(hass) {
    const ui = this._ui;
    const hub = findHub(hass);
    const now = Date.now();
    const past = this._history(hass, hub, now);
    const loads = this._shows("show_loads") ? findLoads(hass) : [];
    const width = Math.max(280, Math.round(ui.body.clientWidth) || 560);
    const signature = this._signature(hass, hub, loads, past, width, now);
    if (signature === this._drawn) return;
    this._drawn = signature;
    this._width = width;

    const status = stateOf(hass, hub["sensor.optimization_status"]);
    const stale = stateOf(hass, hub["binary_sensor.plan_stale"]);
    const isStale = Boolean(stale) && stale.state === "on";
    ui.name.textContent = this._config.title || "Energy plan";
    ui.pill.className = `pill ${isStale ? "warn" : "on"}`;
    ui.pillText.textContent = isStale ? "Out of date" : status ? labelFor(hass, status) : "–";

    const model = this._model(hass, hub, loads, past, now, width);
    this._modelNow = model;
    ui.body.textContent = "";
    if (!model) {
      ui.sub.textContent = "";
      tag("div", "empty", ui.body, "No plan yet. It appears after the first optimisation runs.");
      if (ui.readout) ui.readout.textContent = "";
      if (ui.totals) ui.totals.textContent = "";
      return;
    }

    const staleAttrs = stale && stale.attributes ? stale.attributes : {};
    const plannedAt = staleAttrs.last_successful_run
      ? Date.parse(staleAttrs.last_successful_run)
      : NaN;
    ui.sub.textContent =
      formatSpan(model.t0, model.t1, hass) +
      (Number.isFinite(plannedAt) ? ` · planned ${formatAgo(plannedAt, hass)}` : "");

    if (ui.readout) this._buildReadout(model);
    ui.body.appendChild(this._chart(model, hass));
    if (ui.totals) this._totals(model, hass, hub);
    // The cursor survives a redraw as a time, not as a pixel.
    this._point(this._cursor);
  }

  /* ---------------------------------------------------------------- data */

  _model(hass, hub, loads, past, now, width) {
    const planned = (key) => series(stateOf(hass, hub[key]));
    const plan = {
      pv: planned("sensor.pv_forecast"),
      load: planned("sensor.load_forecast"),
      grid: planned("sensor.grid_forecast"),
      battery: planned("sensor.battery_power"),
      soc: planned("sensor.battery_soc"),
      buy: planned("sensor.buy_price"),
      sell: planned("sensor.sell_price"),
    };
    const power = [plan.pv, plan.load, plan.grid, plan.battery].filter((list) => list.length);
    if (!power.length && !plan.buy.length) return null;

    let step = NaN;
    for (const list of power) {
      const own = stepOf(list);
      if (Number.isFinite(own)) step = Number.isFinite(step) ? Math.min(step, own) : own;
    }
    if (!Number.isFinite(step)) step = FALLBACK_STEP;
    step = Math.max(step, 5 * 60000);
    const priceStep = stepOf(plan.buy) || step;

    let planStart = Infinity;
    let planEnd = -Infinity;
    for (const list of power) {
      planStart = Math.min(planStart, list[0].t);
      planEnd = Math.max(planEnd, list[list.length - 1].t + step);
    }
    if (!power.length) {
      planStart = plan.buy[0].t;
      planEnd = plan.buy[plan.buy.length - 1].t + priceStep;
    }

    const merged = {};
    let earliest = Math.min(planStart, now);
    for (const name of Object.keys(plan)) {
      merged[name] = mergeHistory(past[name] || [], plan[name]);
      if (past[name] && past[name].length) earliest = Math.min(earliest, past[name][0].t);
    }

    // No further back than there is anything to draw: a recorder that has not
    // answered yet would otherwise open the card on hours of blank rail.
    const t0 = Math.max(now - this._historyMs(), earliest);
    let t1 = Math.max(planEnd, now + step);
    const ahead = Number(this._config.horizon_hours);
    if (Number.isFinite(ahead) && ahead > 0) t1 = Math.min(t1, now + ahead * HOUR);

    // The grid is laid from the plan's own first step, backwards and forwards,
    // so a bin is exactly one plan interval wherever the window happens to open.
    const first = planStart - Math.ceil((planStart - t0) / step) * step;
    const starts = [];
    for (let t = first; t < t1 && starts.length < 2000; t += step) starts.push(t);

    const on = (key) => this._shows(key);
    const bins = {
      pv: binned(merged.pv, starts, step, planEnd),
      load: binned(merged.load, starts, step, planEnd),
      grid: binned(merged.grid, starts, step, planEnd),
      battery: binned(merged.battery, starts, step, planEnd),
    };
    if (!on("show_solar")) bins.pv = starts.map(() => NaN);
    if (!on("show_battery")) bins.battery = starts.map(() => NaN);
    if (!on("show_grid")) bins.grid = starts.map(() => NaN);
    if (!on("show_house")) bins.load = starts.map(() => NaN);

    const views = loads.map((load) => loadView(hass, load));
    const buyState = stateOf(hass, hub["sensor.buy_price"]);
    const unit = buyState && buyState.attributes ? buyState.attributes.unit_of_measurement : "";
    const target = stateOf(hass, hub["sensor.end_soc_target"]);

    return {
      hass,
      now,
      t0,
      t1,
      step,
      priceStep,
      first,
      starts,
      bins,
      width,
      planEnd,
      plan,
      soc: clipSeries(merged.soc, t0, t1),
      buy: clipSeries(merged.buy, t0, t1),
      sell: on("show_sell") ? clipSeries(merged.sell, t0, t1) : [],
      rawSoc: merged.soc,
      rawBuy: merged.buy,
      rawSell: merged.sell,
      priceUnit: unit || "",
      target: isUsable(target) ? Number(target.state) : NaN,
      loads: views.filter((view) => view.isEnabled),
      hasBattery: merged.battery.length > 0 || merged.soc.length > 0,
    };
  }

  /* -------------------------------------------------------------- readout */

  _buildReadout(model) {
    const ui = this._ui;
    ui.readout.textContent = "";
    ui.rows = {};
    const when = tag("div", "when", ui.readout);
    ui.whenKind = tag("div", "eyebrow", when, "Now");
    ui.whenTime = tag("div", "clock", when, "–");

    const cell = (key, label, swatch) => {
      const root = tag("div", "cell", ui.readout);
      const top = tag("div", "k", root);
      tag("i", `swatch ${swatch}`, top);
      tag("span", null, top, label);
      const value = tag("div", "v", root, "–");
      const note = tag("div", "n", root, "");
      ui.rows[key] = { value, note };
    };
    const power = this._shows("show_power");
    if (power && this._shows("show_solar")) cell("pv", "Solar", "solar");
    if (power && this._shows("show_house")) cell("load", "House", "house line");
    if (power && this._shows("show_battery") && model.hasBattery) {
      cell("battery", "Battery", "battery");
    }
    if (power && this._shows("show_grid")) cell("grid", "Grid", "grid");
    if (this._shows("show_soc") && model.rawSoc.length) cell("soc", "Charge level", "battery line");
    if (this._shows("show_price") && model.rawBuy.length) cell("price", "Price", "price");
  }

  /** Fill the readout for one instant; null means "now". */
  _point(time) {
    const model = this._modelNow;
    const ui = this._ui;
    if (!model) return;
    const hovering = Number.isFinite(time);
    const t = hovering ? Math.min(Math.max(time, model.t0), model.t1 - 1) : model.now;
    const index = Math.min(
      Math.max(Math.floor((t - model.first) / model.step), 0),
      model.starts.length - 1,
    );
    const from = model.starts[index];

    if (this._hair) {
      const x0 = model.xOf(Math.max(from, model.t0));
      const x1 = model.xOf(Math.min(from + model.step, model.t1));
      this._hair.style.display = hovering ? "" : "none";
      this._hairBand.setAttribute("x", px(x0));
      this._hairBand.setAttribute("width", px(Math.max(x1 - x0, 1)));
      this._hairLine.setAttribute("d", model.segments((x0 + x1) / 2));
    }
    if (!ui.readout) return;

    if (hovering) {
      const today = new Date(model.now).setHours(0, 0, 0, 0);
      const day = new Date(from).setHours(0, 0, 0, 0);
      const language = model.hass.locale ? model.hass.locale.language : undefined;
      const weekday =
        day === today ? "" : `${new Date(from).toLocaleDateString(language, { weekday: "short" })} `;
      ui.whenKind.textContent = from + model.step <= model.now ? "Recorded" : "Planned";
      ui.whenTime.textContent = `${weekday}${formatTime(from, model.hass)}`;
    } else {
      ui.whenKind.textContent = "Now";
      ui.whenTime.textContent = formatTime(model.now, model.hass);
    }

    const set = (key, text, note) => {
      const row = ui.rows[key];
      if (!row) return;
      row.value.textContent = text;
      row.note.textContent = note || "";
    };
    const at = (name) => model.bins[name][index];
    set("pv", formatPower(at("pv")));
    set("load", formatPower(at("load")));
    const battery = at("battery");
    set(
      "battery",
      formatPower(Math.abs(battery)),
      !Number.isFinite(battery) || Math.abs(battery) < 1
        ? "idle"
        : battery > 0
          ? "discharging"
          : "charging",
    );
    const grid = at("grid");
    set(
      "grid",
      formatPower(Math.abs(grid)),
      !Number.isFinite(grid) || Math.abs(grid) < 1 ? "balanced" : grid > 0 ? "importing" : "exporting",
    );
    const soc = valueAt(model.rawSoc, t);
    set("soc", Number.isFinite(soc) ? `${Math.round(soc)} %` : "–");
    const buy = valueAt(model.rawBuy, t, model.priceStep);
    const sell = valueAt(model.rawSell, t, model.priceStep);
    set(
      "price",
      Number.isFinite(buy) ? buy.toFixed(2) : "–",
      Number.isFinite(sell) && this._shows("show_sell") ? `export ${sell.toFixed(2)}` : model.priceUnit,
    );
  }

  /* ---------------------------------------------------------------- chart */

  _chart(model, hass) {
    const narrow = model.width < 440;
    const left = 40;
    const right = 10;
    const plotW = model.width - left - right;
    const xOf = (t) => left + ((t - model.t0) / (model.t1 - model.t0)) * plotW;
    model.xOf = xOf;
    model.left = left;
    model.plotW = plotW;

    const panels = [];
    const hasPower = Object.keys(model.bins).some((name) =>
      model.bins[name].some((value) => Number.isFinite(value)),
    );
    if (this._shows("show_power") && hasPower) {
      panels.push({ kind: "power", height: narrow ? 150 : 190 });
    }
    if (this._shows("show_price") && model.buy.length) {
      panels.push({ kind: "price", height: narrow ? 78 : 92 });
    }
    if (this._shows("show_soc") && model.soc.length) {
      panels.push({ kind: "soc", height: narrow ? 64 : 76 });
    }
    if (model.loads.length) {
      panels.push({ kind: "loads", height: model.loads.length * 30 - 6 });
    }

    const titleRow = 20;
    const gap = 12;
    let y = 0;
    for (const panel of panels) {
      panel.top = y + titleRow;
      y = panel.top + panel.height + gap;
    }
    const axisTop = panels.length ? y - gap + 4 : 0;
    const height = axisTop + 22;

    const root = svg("svg", {
      viewBox: `0 0 ${model.width} ${height}`,
      width: model.width,
      height,
      role: "img",
      "aria-label": "Energy plan chart",
    });
    const plotTop = panels.length ? panels[0].top : 0;
    const plotBottom = panels.length ? panels[panels.length - 1].top + panels[panels.length - 1].height : 0;

    // What has already happened, one tone darker, behind everything.
    const nowX = Math.min(Math.max(xOf(model.now), left), left + plotW);
    for (const panel of panels) {
      if (nowX > left + 0.5) {
        svg("rect", {
          class: "past", x: left, y: panel.top, width: px(nowX - left), height: panel.height,
        }, root);
      }
    }

    const ticks = hourTicks(model.t0, model.t1, Math.max(Math.floor(plotW / 62), 2));
    // A vertical rule is drawn panel by panel, so it never runs through the
    // captions between them.
    const segments = (x) =>
      panels.map((panel) => `M${px(x)},${panel.top}V${panel.top + panel.height}`).join("");
    model.segments = segments;
    for (const tick of ticks) {
      if (tick.midnight) svg("path", { class: "day", d: segments(xOf(tick.t)) }, root);
    }

    for (const panel of panels) {
      if (panel.kind === "power") this._power(root, model, panel);
      if (panel.kind === "price") this._price(root, model, panel);
      if (panel.kind === "soc") this._soc(root, model, panel);
      if (panel.kind === "loads") this._loads(root, model, panel, hass);
    }

    // Now: one line through every panel, named once, on the axis it belongs to.
    if (panels.length && model.now > model.t0 && model.now < model.t1) {
      svg("path", { class: "now", d: segments(nowX) }, root);
    }
    const language = hass.locale ? hass.locale.language : undefined;
    for (const tick of ticks) {
      const x = xOf(tick.t);
      if (Math.abs(x - nowX) < 30 && model.now > model.t0) continue;
      const label = svg("text", {
        class: tick.midnight ? "tick strong" : "tick", x: px(x), y: axisTop + 13,
        "text-anchor": x < left + 14 ? "start" : x > left + plotW - 14 ? "end" : "middle",
      }, root);
      label.textContent = tick.midnight
        ? new Date(tick.t).toLocaleDateString(language, { weekday: "short" })
        : formatHour(tick.t, hass);
    }
    if (panels.length && model.now > model.t0 && model.now < model.t1) {
      const pillX = Math.min(Math.max(nowX - 17, left), left + plotW - 34);
      svg("rect", { class: "nowpill", x: px(pillX), y: axisTop + 2, width: 34, height: 16, rx: 8 }, root);
      const text = svg("text", {
        class: "nowtext", x: px(pillX + 17), y: axisTop + 13.5, "text-anchor": "middle",
      }, root);
      text.textContent = "Now";
    }

    // The cursor, drawn once and moved; the readout above carries its values.
    const hair = svg("g", { class: "hair" }, root);
    hair.style.display = "none";
    this._hairBand = svg("rect", {
      class: "hairband", x: left, y: plotTop, width: 1, height: Math.max(plotBottom - plotTop, 0),
    }, hair);
    this._hairLine = svg("path", { class: "hairline", d: "" }, hair);
    this._hair = hair;

    const timeAt = (event) => {
      const box = root.getBoundingClientRect();
      const scale = box.width ? model.width / box.width : 1;
      const x = (event.clientX - box.left) * scale;
      const share = Math.min(Math.max((x - left) / plotW, 0), 1);
      return model.t0 + share * (model.t1 - model.t0);
    };
    const move = (event) => {
      this._cursor = timeAt(event);
      this._point(this._cursor);
    };
    const leave = () => {
      this._cursor = null;
      this._point(null);
    };
    root.addEventListener("pointermove", move);
    root.addEventListener("pointerdown", move);
    root.addEventListener("pointerleave", leave);
    root.addEventListener("pointercancel", leave);
    return root;
  }

  /** A panel's caption and its horizontal rules, labelled in the gutter. */
  _frame(root, model, panel, title, unit, ticks, y, format) {
    const caption = svg("text", { class: "cap", x: model.left, y: panel.top - 7 }, root);
    caption.textContent = title;
    if (unit) {
      const small = svg("tspan", { class: "unit", dx: 5 }, caption);
      small.textContent = unit;
    }
    for (const value of ticks) {
      const at = px(y(value));
      svg("line", {
        class: value === 0 ? "rule zero" : "rule",
        x1: model.left, x2: model.left + model.plotW, y1: at, y2: at,
      }, root);
      const label = svg("text", { class: "ylab", x: model.left - 6, y: at + 3.5, "text-anchor": "end" }, root);
      label.textContent = format(value);
    }
  }

  _edges(model) {
    const edges = model.starts.map((start) =>
      model.xOf(Math.min(Math.max(start, model.t0), model.t1)),
    );
    const last = model.starts[model.starts.length - 1] + model.step;
    edges.push(model.xOf(Math.min(last, model.t1)));
    return edges;
  }

  _power(root, model, panel) {
    const bins = model.bins;
    const count = model.starts.length;
    const zero = [];
    const solarTop = [];
    const batteryTop = [];
    const gridTop = [];
    const chargeLow = [];
    const exportLow = [];
    let hi = 0;
    let lo = 0;
    const positive = (value) => (Number.isFinite(value) && value > 0 ? value : 0);
    const negative = (value) => (Number.isFinite(value) && value < 0 ? value : 0);
    for (let i = 0; i < count; i++) {
      zero.push(0);
      solarTop.push(positive(bins.pv[i]));
      batteryTop.push(solarTop[i] + positive(bins.battery[i]));
      gridTop.push(batteryTop[i] + positive(bins.grid[i]));
      chargeLow.push(negative(bins.battery[i]));
      exportLow.push(chargeLow[i] + negative(bins.grid[i]));
      hi = Math.max(hi, gridTop[i], positive(bins.load[i]));
      lo = Math.min(lo, exportLow[i]);
    }
    // The domain is the data's, the rules are round numbers inside it: rounding
    // the domain itself outwards can leave a third of the panel empty.
    const topW = Math.max(hi, 500) * 1.06;
    const floorW = lo * 1.06;
    // A few minutes of kettle or car charger would otherwise set the scale for
    // the whole day. When the peak stands that far above the ordinary, the
    // axis is square-rooted: zero stays put, the peak still reads as the peak,
    // and the ordinary hours get the room.
    const wanted = this._config.power_scale;
    let compressed = wanted === "compressed";
    if (wanted !== "compressed" && wanted !== "linear") {
      const sizes = [];
      for (let i = 0; i < count; i++) {
        const size = Math.max(gridTop[i], positive(bins.load[i]), -exportLow[i]);
        if (size > 0) sizes.push(size);
      }
      sizes.sort((p, q) => p - q);
      const usual = sizes.length ? sizes[Math.floor(0.9 * (sizes.length - 1))] : 0;
      compressed = usual > 0 && Math.max(hi, -lo) > 2.5 * usual;
    }
    const bend = compressed
      ? (watts) => Math.sign(watts) * Math.sqrt(Math.abs(watts) / 1000)
      : (watts) => watts / 1000;
    const y = (watts) =>
      panel.top +
      panel.height -
      ((bend(watts) - bend(floorW)) / (bend(topW) - bend(floorW))) * panel.height;
    let rules;
    if (compressed) {
      rules = [0];
      for (const side of [1, -1]) {
        let last = y(0);
        for (const kw of [0.5, 1, 2, 5, 10, 20, 50, 100]) {
          const watts = side * kw * 1000;
          if (watts > topW || watts < floorW) break;
          if (Math.abs(y(watts) - last) < 17) continue;
          rules.push(side * kw);
          last = y(watts);
        }
      }
    } else {
      rules = niceScale(floorW / 1000, topW / 1000, panel.height > 160 ? 4 : 3).ticks.filter(
        (kw) => kw * 1000 <= topW && kw * 1000 >= floorW,
      );
    }
    this._frame(
      root, model, panel, "Power", compressed ? "kW · compressed scale" : "kW", rules,
      (kw) => y(kw * 1000),
      (kw) => String(Math.round(kw * 10) / 10),
    );

    const edges = this._edges(model);
    const layer = (name, lower, upper) => {
      const d = bandPath(edges, lower, upper, y);
      if (d) svg("path", { class: `band ${name}`, d }, root);
    };
    layer("solar", zero, solarTop);
    layer("battery", solarTop, batteryTop);
    layer("grid", batteryTop, gridTop);
    layer("battery", chargeLow, zero);
    layer("grid", exportLow, chargeLow);

    const line = stepPath(edges, bins.load, y);
    if (line) {
      svg("path", { class: "house halo", d: line }, root);
      svg("path", { class: "house", d: line }, root);
    }

    // Which side of zero means what, said once at the right-hand edge.
    if (floorW < 0) {
      const used = svg("text", {
        class: "side", x: model.left + model.plotW, y: panel.top + 10, "text-anchor": "end",
      }, root);
      used.textContent = "supply";
      const spare = svg("text", {
        class: "side", x: model.left + model.plotW, y: panel.top + panel.height - 4,
        "text-anchor": "end",
      }, root);
      spare.textContent = "stored · sold";
    }
  }

  _price(root, model, panel) {
    const reach = (points) => {
      let most = -Infinity;
      let least = Infinity;
      for (const point of points) {
        most = Math.max(most, point.v);
        least = Math.min(least, point.v);
      }
      return { hi: most, lo: least };
    };
    const wanted = this._config.price_scale;
    const bought = reach(model.buy.length ? model.buy : model.sell);
    const both = reach(model.buy.concat(model.sell));
    let hi = both.hi;
    let lo = both.lo;
    // The export price usually sits well under the buy price. Fitting both
    // would spend most of the panel on the gap between them, so when it would
    // more than half again the span, the axis is the buy price's and the export
    // price stays in the readout. One axis either way: never a second scale.
    let sold = model.sell.length > 0 && model.buy.length > 0;
    if (sold && wanted !== "both" && wanted !== "zero") {
      const own = bought.hi - bought.lo;
      if (both.hi - both.lo > own * 1.5 + 1e-9) {
        sold = false;
        hi = bought.hi;
        lo = bought.lo;
      }
    }
    // Fitted to the prices there are, not to zero: a day that moves between
    // 0.92 and 1.34 is otherwise a wall with a ripple on top. A line may start
    // off zero where a bar may not, which is why this is a line.
    if (wanted === "zero") {
      hi = Math.max(hi, 0);
      lo = Math.min(lo, 0);
    }
    const span = hi - lo || Math.max(Math.abs(hi), 1) * 0.1;
    // Head-room above the dearest price, for the label that names it.
    const top = hi + span * 0.34;
    // And room under the cheapest, for the other label.
    let floor = lo - span * 0.4;
    if (lo >= 0) floor = Math.max(floor, 0);
    const offScale = model.sell.length > 0 && model.buy.length > 0 && !sold;
    const unit = offScale
      ? `${model.priceUnit ? `${model.priceUnit} · ` : ""}export price off this scale`
      : model.priceUnit;
    const scale = niceScale(floor, top, 5);
    const rules = scale.ticks.filter((value) => value <= hi + span * 0.05 && value >= floor);
    const y = (value) => panel.top + panel.height - ((value - floor) / (top - floor)) * panel.height;
    const decimals = scale.step < 0.1 ? 2 : scale.step < 1 ? 1 : 0;
    this._frame(root, model, panel, "Price", unit, rules, y, (value) =>
      value.toFixed(decimals),
    );

    const stepped = (points) => {
      let d = "";
      let endX = 0;
      for (let k = 0; k < points.length; k++) {
        const until = Math.min(
          k + 1 < points.length ? points[k + 1].t : points[k].t + model.priceStep,
          model.t1,
        );
        if (until <= points[k].t) continue;
        endX = model.xOf(until);
        d += `${d ? "V" : `M${px(model.xOf(points[k].t))},`}${px(y(points[k].v))}H${px(endX)}`;
      }
      return { d, endX };
    };

    let cheapest = null;
    let dearest = null;
    for (let i = 0; i < model.buy.length; i++) {
      const point = model.buy[i];
      const end = Math.min(
        i + 1 < model.buy.length ? model.buy[i + 1].t : point.t + model.priceStep,
        model.t1,
      );
      if (end <= point.t || end <= model.now) continue;
      const mark = {
        x: (Math.max(model.xOf(point.t), model.xOf(model.now)) + model.xOf(end)) / 2,
        top: y(point.v),
        v: point.v,
      };
      if (!cheapest || point.v < cheapest.v) cheapest = mark;
      if (!dearest || point.v > dearest.v) dearest = mark;
    }

    if (sold || !model.buy.length) {
      const sell = stepped(model.sell);
      if (sell.d) svg("path", { class: "sell", d: sell.d }, root);
    }
    const buy = stepped(model.buy);
    if (buy.d) {
      const base = px(panel.top + panel.height);
      const startX = px(model.xOf(model.buy[0].t));
      svg("path", { class: "pwash", d: `${buy.d}V${base}H${startX}Z` }, root);
      svg("path", { class: "buy", d: buy.d }, root);
    }

    if (!this._shows("show_extremes") || !cheapest || !dearest || cheapest === dearest) return;
    if (dearest.v - cheapest.v < 1e-9) return;
    const name = (mark, word) => {
      const x = Math.min(Math.max(mark.x, model.left + 24), model.left + model.plotW - 24);
      svg("circle", { class: "pin", cx: px(mark.x), cy: px(mark.top), r: 2.5 }, root);
      // The low is named from underneath when there is room: above it is the line.
      const under = word === "low" && mark.top + 15 <= panel.top + panel.height - 2;
      const label = svg("text", {
        class: "note", x: px(x), "text-anchor": "middle",
        y: px(under ? mark.top + 14 : Math.max(mark.top - 6, panel.top + 9)),
      }, root);
      label.textContent = `${word} ${mark.v.toFixed(2)}`;
    };
    name(dearest, "peak");
    // Two labels a few pixels apart are one unreadable label.
    if (Math.abs(dearest.x - cheapest.x) > 70) name(cheapest, "low");
  }

  _soc(root, model, panel) {
    const y = (value) =>
      panel.top + panel.height - (Math.min(Math.max(value, 0), 100) / 100) * panel.height;
    this._frame(root, model, panel, "Battery", "%", [0, 50, 100], y, (value) => String(value));

    const points = model.soc;
    let d = "";
    for (let i = 0; i < points.length; i++) {
      d += `${i ? "L" : "M"}${px(model.xOf(points[i].t))},${px(y(points[i].v))}`;
    }
    const lastPoint = points[points.length - 1];
    const firstX = px(model.xOf(points[0].t));
    const lastX = px(model.xOf(lastPoint.t));
    svg("path", { class: "wash", d: `${d}L${lastX},${px(y(0))}L${firstX},${px(y(0))}Z` }, root);
    svg("path", { class: "level", d }, root);

    // The end of the line is the number the plan is steering for.
    svg("circle", { class: "dot", cx: lastX, cy: px(y(lastPoint.v)), r: 3.5 }, root);
    const endLabel = svg("text", {
      class: "note", x: lastX - 7, y: px(Math.min(Math.max(y(lastPoint.v) - 7, panel.top + 9), panel.top + panel.height - 4)),
      "text-anchor": "end",
    }, root);
    const goal = Number.isFinite(model.target) ? ` · target ${Math.round(model.target)} %` : "";
    endLabel.textContent = `${Math.round(lastPoint.v)} %${goal}`;
  }

  _loads(root, model, panel, hass) {
    const caption = svg("text", { class: "cap", x: model.left, y: panel.top - 7 }, root);
    caption.textContent = "Loads";
    model.loads.forEach((view, row) => {
      const top = panel.top + row * 30;
      const label = svg("text", { class: "lname", x: model.left + 2, y: top + 10 }, root);
      label.textContent = view.name;
      const upcoming = view.upcoming.filter((run) => run.start < model.t1);
      const summary = svg("text", {
        class: "lwhen", x: model.left + model.plotW, y: top + 10, "text-anchor": "end",
      }, root);
      // Always with the time it ends, and the deadline when the load has one.
      const next = upcoming.length ? upcoming[0] : null;
      const started = next && next.start <= model.now;
      const due = view.deadline && view.deadline > model.now
        ? ` · by ${formatTime(view.deadline, hass)}`
        : "";
      summary.textContent = next
        ? (started || view.status === "running"
            ? `running until ${formatTime(next.end, hass)}`
            : `${formatTime(next.start, hass)} – ${formatTime(next.end, hass)}`) + due
        : view.status === "running"
          ? `running now${due}`
          : `not scheduled${due}`;
      svg("rect", {
        class: "rail", x: model.left, y: top + 16, width: model.plotW, height: 6, rx: 3,
      }, root);
      for (const run of view.runs) {
        const from = Math.max(run.start, model.t0);
        const to = Math.min(run.end, model.t1);
        if (to <= from) continue;
        svg("rect", {
          class: to <= model.now ? "run done" : "run",
          x: px(model.xOf(from)), y: top + 16,
          width: px(Math.max(model.xOf(to) - model.xOf(from), 3)), height: 6, rx: 3,
        }, root);
      }
      if (view.deadline && view.deadline > model.t0 && view.deadline < model.t1) {
        const at = px(model.xOf(view.deadline));
        svg("path", { class: "due", d: `M${at},${top + 13}V${top + 25}` }, root);
      }
    });
  }

  /* --------------------------------------------------------------- totals */

  _totals(model, hass, hub) {
    const ui = this._ui;
    ui.totals.textContent = "";
    const ahead = (points) => integrate(clipSeries(points, model.now, model.t1), model.t1);
    const item = (label, value, note) => {
      if (value === null) return;
      const root = tag("div", "total", ui.totals);
      tag("div", "eyebrow", root, label);
      tag("div", "v", root, value);
      if (note) tag("div", "n", root, note);
    };
    const energy = (points, side) => (points.length ? formatEnergy(ahead(points)[side]) : null);
    if (this._shows("show_solar")) item("Solar ahead", energy(model.plan.pv, "up"));
    if (this._shows("show_house")) item("Consumption", energy(model.plan.load, "up"));
    if (this._shows("show_grid")) {
      item("Import", energy(model.plan.grid, "up"));
      item("Export", energy(model.plan.grid, "down"));
    }
    if (this._shows("show_battery") && model.plan.battery.length) {
      const flow = ahead(model.plan.battery);
      item("Battery", `${formatEnergy(flow.down)} in`, `${formatEnergy(flow.up)} out`);
    }
    const cost = stateOf(hass, hub["sensor.forecast_cost_24h"]);
    if (isUsable(cost)) item("Balance, 24 h", balanceText(cost).text);
  }
}

// The window and the now line move with the clock, not only with new states.
EmhassHorizonCard.ticks = true;

EmhassHorizonCard.css = STUDIO_TOKENS + `
  .pad { padding: 16px 16px 14px 16px; }
  .name { font-size: 1.12rem; font-weight: 600; letter-spacing: -.01em; }

  .readout { display: grid; grid-template-columns: repeat(auto-fit, minmax(74px, 1fr));
             gap: 0; margin: 14px 0 6px 0; border-top: 1px solid var(--emh-hairline);
             border-bottom: 1px solid var(--emh-hairline); }
  .readout > div { padding: 9px 10px 8px 0; min-width: 0; }
  .when .clock { font-size: 1.3rem; font-weight: 600; letter-spacing: -.02em;
                 line-height: 1.2; margin-top: 2px; white-space: nowrap; }
  .cell .k { display: flex; align-items: center; gap: 5px; font-size: .7rem;
             color: var(--emh-dim); white-space: nowrap; overflow: hidden; }
  .cell .k span { overflow: hidden; text-overflow: ellipsis; }
  .cell .v { font-size: 1rem; font-weight: 600; margin-top: 3px; white-space: nowrap;
             letter-spacing: -.01em; }
  .cell .n { font-size: .68rem; color: var(--emh-dim); min-height: 1em; white-space: nowrap; }

  .plot { margin-top: 6px; }
  .plot svg { width: 100%; height: auto; touch-action: pan-y; cursor: crosshair;
              -webkit-tap-highlight-color: transparent; user-select: none; }
  text { font-family: inherit; font-size: 10.5px; fill: var(--emh-dim); }
  .cap { font-size: 10px; font-weight: 600; letter-spacing: .09em; text-transform: uppercase;
         fill: var(--emh-s-ink); }
  .cap .unit { font-weight: 400; letter-spacing: 0; text-transform: none; fill: var(--emh-dim); }
  .ylab, .tick { font-variant-numeric: tabular-nums; }
  .tick.strong { fill: var(--emh-s-ink); font-weight: 600; }
  .side { font-size: 9.5px; opacity: .8; }
  .note { fill: var(--emh-s-ink); font-size: 10.5px; font-weight: 500;
          paint-order: stroke; stroke: var(--emh-s-card); stroke-width: 3px;
          stroke-linejoin: round; }
  .lname { fill: var(--emh-s-ink); font-size: 11px; font-weight: 500;
           paint-order: stroke; stroke: var(--emh-s-card); stroke-width: 3px;
           stroke-linejoin: round; }
  .hairline { fill: none; }

  .past { fill: var(--emh-surface); }
  .rule { stroke: var(--emh-hairline); stroke-width: 1; opacity: .7; shape-rendering: crispEdges; }
  .rule.zero { stroke: var(--emh-dim); opacity: .55; }
  .day, .now { fill: none; }
  .day { stroke: var(--emh-hairline); stroke-width: 1; shape-rendering: crispEdges; }
  .now { stroke: var(--emh-s-ink); stroke-width: 1; opacity: .7; shape-rendering: crispEdges; }
  .nowpill { fill: var(--emh-s-ink); }
  .nowtext { fill: var(--emh-s-card); font-size: 9.5px; font-weight: 600; letter-spacing: .03em; }

  .band { stroke: var(--emh-s-card); stroke-width: 1; stroke-linejoin: round; fill-opacity: .86; }
  .band.solar { fill: var(--emh-s-solar); }
  .band.battery { fill: var(--emh-s-battery); }
  .band.grid { fill: var(--emh-s-grid); }
  .house, .sell, .level { fill: none; stroke-linejoin: round; stroke-linecap: round; }
  .house { stroke: var(--emh-s-ink); stroke-width: 1.75; }
  .halo { stroke: var(--emh-s-card); stroke-width: 3.5; opacity: .85; }
  .buy { fill: none; stroke: var(--emh-s-price); stroke-width: 2; stroke-linejoin: round; }
  .pwash { fill: var(--emh-s-price); fill-opacity: .14; }
  .due { fill: none; stroke: var(--emh-s-ink); stroke-width: 1.5; }
  .sell { stroke: var(--emh-s-ink); stroke-opacity: .5; stroke-width: 1.25; }
  .pin { fill: var(--emh-s-ink); stroke: var(--emh-s-card); stroke-width: 1.5; }
  .level { stroke: var(--emh-s-battery); stroke-width: 2; }
  .wash { fill: var(--emh-s-battery); fill-opacity: .12; }
  .dot { fill: var(--emh-s-battery); stroke: var(--emh-s-card); stroke-width: 2; }
  .rail { fill: var(--emh-surface); }
  .run { fill: var(--emh-accent); }
  .run.done { fill-opacity: .45; }

  .hairband { fill: var(--emh-s-ink); fill-opacity: .07; }
  .hairline { stroke: var(--emh-s-ink); stroke-width: 1; shape-rendering: crispEdges; }

  .totals { display: flex; flex-wrap: wrap; gap: 10px 26px; margin-top: 12px;
            padding-top: 12px; border-top: 1px solid var(--emh-hairline); }
  .readout:empty, .totals:empty { display: none; }
  .total .v { font-size: .98rem; font-weight: 600; margin-top: 3px; letter-spacing: -.01em; }
  .total .n { font-size: .68rem; color: var(--emh-dim); }
`;

/* ----------------------------------------------------------------- editor */

const HORIZON_LABELS = {
  title: "Title",
  history_hours: "Hours of history",
  horizon_hours: "Hours ahead",
  solar_entity: "Measured solar power",
  house_entity: "Measured house consumption",
  grid_entity: "Measured grid power",
  battery_entity: "Measured battery power",
  invert_battery: "That battery sensor is positive when charging",
  power_scale: "Power axis",
  price_scale: "Price axis",
};
const HORIZON_HELPERS = {
  power_scale:
    "Compressed gives short peaks less height so the ordinary hours show more detail. Automatic compresses only when a peak dwarfs the rest.",
  price_scale:
    "Fitted starts the axis near the lowest price, which shows small differences. An export price far below the buy price is then left to the readout.",
  history_hours: "How far back the chart reaches before now. 0 starts it at now.",
  horizon_hours: "How far ahead the chart reaches. 0 draws the whole plan.",
  solar_entity: "Left empty, the past is drawn from the forecast the plan used.",
  house_entity: "Left empty, the past is drawn from the plan's own consumption forecast.",
  grid_entity: "Left empty, the past is drawn from the plan's own grid figure.",
  battery_entity:
    "Left empty, the Companion's own battery power sensor is used, and the plan's figure if it has none.",
  invert_battery: "Only read when a sensor is named above.",
};
for (const row of HORIZON_SECTIONS.concat(HORIZON_SERIES)) {
  HORIZON_LABELS[row[0]] = row[1];
  HORIZON_HELPERS[row[0]] = row[2];
}

const HORIZON_SCALES = {
  power_scale: [["auto", "Automatic"], ["linear", "Linear"], ["compressed", "Compressed"]],
  price_scale: [
    ["fit", "Fitted to the buy price"],
    ["both", "Fitted to buy and export"],
    ["zero", "From zero"],
  ],
};

const HORIZON_ENTITY_OPTIONS = ["solar_entity", "house_entity", "grid_entity", "battery_entity"];

const HORIZON_SCHEMA = [
  { name: "title", selector: { text: {} } },
  sectionGrid(HORIZON_SECTIONS),
  {
    name: "",
    type: "expandable",
    title: "Series",
    icon: "mdi:chart-timeline-variant",
    schema: [sectionGrid(HORIZON_SERIES)],
  },
  {
    name: "",
    type: "expandable",
    title: "Axes",
    icon: "mdi:axis-arrow",
    schema: [
      {
        name: "",
        type: "grid",
        column_min_width: "200px",
        schema: Object.keys(HORIZON_SCALES).map((name) => ({
          name,
          selector: {
            select: {
              mode: "dropdown",
              options: HORIZON_SCALES[name].map((row) => ({ value: row[0], label: row[1] })),
            },
          },
        })),
      },
    ],
  },
  {
    name: "",
    type: "expandable",
    title: "Window and history",
    icon: "mdi:history",
    schema: [
      {
        name: "",
        type: "grid",
        column_min_width: "200px",
        schema: [
          {
            name: "history_hours",
            selector: { number: { min: 0, max: 12, step: 0.5, mode: "box", unit_of_measurement: "h" } },
          },
          {
            name: "horizon_hours",
            selector: { number: { min: 0, max: 72, step: 1, mode: "box", unit_of_measurement: "h" } },
          },
        ],
      },
      {
        name: "",
        type: "grid",
        column_min_width: "260px",
        schema: HORIZON_ENTITY_OPTIONS.map((name) => ({
          name,
          selector: { entity: { filter: { domain: "sensor" } } },
        })),
      },
      { name: "invert_battery", selector: { boolean: {} } },
    ],
  },
];

class EmhassHorizonCardEditor extends CardEditor {
  get labels() {
    return HORIZON_LABELS;
  }

  get helpers() {
    return HORIZON_HELPERS;
  }

  schema() {
    return HORIZON_SCHEMA;
  }

  data() {
    const data = { title: this._config.title || "" };
    for (const row of HORIZON_SECTIONS.concat(HORIZON_SERIES)) {
      data[row[0]] = showsSection(this._config, row[0]);
    }
    const hours = Number(this._config.history_hours);
    data.history_hours = Number.isFinite(hours) ? hours : DEFAULT_PLAN_HISTORY_HOURS;
    const ahead = Number(this._config.horizon_hours);
    data.horizon_hours = Number.isFinite(ahead) ? ahead : 0;
    data.invert_battery = this._config.invert_battery === true;
    for (const name of Object.keys(HORIZON_SCALES)) {
      const known = HORIZON_SCALES[name].map((row) => row[0]);
      data[name] = known.indexOf(this._config[name]) >= 0 ? this._config[name] : known[0];
    }
    for (const key of HORIZON_ENTITY_OPTIONS) {
      if (this._config[key]) data[key] = this._config[key];
    }
    return data;
  }

  clean(config) {
    cleanSections(config, HORIZON_SECTIONS);
    cleanSections(config, HORIZON_SERIES);
    if (!config.title) delete config.title;
    for (const key of HORIZON_ENTITY_OPTIONS) {
      if (!config[key]) delete config[key];
    }
    if (config.invert_battery !== true) delete config.invert_battery;
    if (
      config.history_hours === undefined ||
      Number(config.history_hours) === DEFAULT_PLAN_HISTORY_HOURS
    ) {
      delete config.history_hours;
    }
    if (!Number(config.horizon_hours)) delete config.horizon_hours;
    for (const name of Object.keys(HORIZON_SCALES)) {
      if (!config[name] || config[name] === HORIZON_SCALES[name][0][0]) delete config[name];
    }
    return config;
  }
}

customElements.define("emhass-horizon-card", EmhassHorizonCard);
customElements.define("emhass-horizon-card-editor", EmhassHorizonCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-horizon-card",
    name: "EMHASS Companion plan (horizon)",
    description:
      "The plan as an energy balance: supply stacked against consumption, price bars, battery level and load rails, with a readout that follows the cursor.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);
