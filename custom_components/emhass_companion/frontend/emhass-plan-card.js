/**
 * NOTE FOR REVIEWERS: this file was mechanically extracted from the single
 * the original single-bundle emhass-cards.js, splitting it into a shared
 * core module and one bundle per card family so that a lost race against the frontend's
 * fixed element-registration timeout (home-assistant/frontend#52960) only
 * takes down 1-3 elements instead of all 14. Every line below is verbatim
 * from the original file; only import/export wiring and file boundaries
 * (and this note) are new.
 */

import {
  COLORS,
  CardEditor,
  DEFAULT_HISTORY_HOURS,
  DEFAULT_PLAN_HISTORY_HOURS,
  cleanSections,
  clipSeries,
  findHub,
  findLoads,
  formatHours,
  formatPower,
  formatTime,
  loadHaForm,
  measuredBy,
  mergeHistory,
  readHistory,
  sectionGrid,
  series,
  showsSection,
  stateOf,
  svg,
} from "./emhass-core.js?v=__VERSION__";


/** The lowest and highest value across several series, for a y-axis. */
function extent(lists) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const list of lists) {
    for (const point of list) {
      if (point.v < lo) lo = point.v;
      if (point.v > hi) hi = point.v;
    }
  }
  if (!Number.isFinite(lo)) return [0, 1];
  if (lo === hi) return [lo - 1, hi + 1];
  return [lo, hi];
}

/** The first and last instant across several series, for a time axis. */
function timeExtent(lists) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const list of lists) {
    for (const point of list) {
      if (point.t < lo) lo = point.t;
      if (point.t > hi) hi = point.t;
    }
  }
  return Number.isFinite(lo) ? [lo, hi] : [Date.now(), Date.now() + 86400000];
}

/* ------------------------------------------------------------- plan card */

/**
 * The sections the plan card is built from, in the order they are drawn.
 *
 * One list drives all three of them: what `_chart` puts on the card, what the
 * visual editor offers, and which keys are worth writing into the YAML -- the
 * same arrangement the later cards use, so a section added here appears in the
 * editor without the editor being touched.
 */
const PLAN_SECTIONS = [
  ["show_power", "Power panel", "Solar, consumption, grid and battery"],
  ["show_price", "Price panel", "Import and export price"],
  ["show_soc", "Charge level", "The planned battery level, over the price panel"],
  ["show_loads", "Load rows", "One row per deferrable load, when it is scheduled"],
  ["show_legend", "Legend", "The colour key under the chart"],
];

/**
 * Where each series' historic side comes from: the plan's own sensor, or the
 * house's own meter when the config names one.
 *
 * Prices have no override, because there is nothing to override them with: the
 * Companion's own price sensor *is* the meter, and its recorded history is
 * what the house was actually charged.
 */
const PLAN_HISTORY = [
  ["pv", "sensor.pv_forecast", "solar_entity"],
  ["load", "sensor.load_forecast", "house_entity"],
  ["grid", "sensor.grid_forecast", "grid_entity"],
  ["battery", "sensor.battery_power", "battery_entity"],
  ["soc", "sensor.battery_soc", null],
  ["buy", "sensor.buy_price", null],
  ["sell", "sensor.sell_price", null],
];

/**
 * The whole plan in one chart: power, price, charge level and every load.
 *
 * The window is a rolling one -- a couple of hours back from now, out to the
 * end of the plan -- rather than whatever extent the data happens to have.
 * Drawing the data's own extent puts the left edge at last midnight, because
 * that is where a day-ahead price sensor begins publishing, and by noon that
 * is a third of the card spent on a morning nobody can do anything about.
 */
class EmhassPlanCard extends HTMLElement {
  static getStubConfig() {
    return { type: "custom:emhass-plan-card" };
  }

  /** The visual editor, with Home Assistant's own form widgets pulled in. */
  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-plan-card-editor");
  }

  setConfig(config) {
    this._config = Object.assign({}, config);
    this._root = null;
  }

  getCardSize() {
    return 8;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  /** Redraw with the state already in hand, for history that arrived late. */
  refresh() {
    if (this._hass) this._render();
  }

  /** How far back the window reaches, in milliseconds. */
  _historyMs() {
    const hours = Number(this._config.history_hours);
    return (Number.isFinite(hours) && hours >= 0 ? hours : DEFAULT_HISTORY_HOURS) * 3600000;
  }

  /**
   * What each series actually did, before the plan starts.
   *
   * Without this the card had nothing at all to the left of now -- an MPC run
   * begins at the timestep it was made in, so every line simply started at the
   * present and the whole left-hand side was blank rail. The recorder has the
   * answer for all seven series in one call.
   */
  _history(hass, hub, now) {
    const wanted = {};
    const invert = [];
    // This card's own option wins where it is set -- pointing one card at a
    // different meter is a deliberate choice -- and the Companion's configured
    // sensor is the default under it.
    const measured = measuredBy(hass, hub, "sensor.battery_power");
    for (const [name, key, option] of PLAN_HISTORY) {
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

  _render() {
    const hass = this._hass;
    if (!hass) return;

    if (!this._root) {
      // Attached at most once per element: setConfig clears _root so the
      // markup below is rebuilt, and the card editor calls setConfig again on
      // the *same* element every time an option changes. A second attachShadow
      // throws NotSupportedError, which red-cards the whole card.
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `
        <style>
          ha-card { padding: 12px 8px 4px 8px; }
          .head { display:flex; justify-content:space-between; align-items:baseline;
                  padding: 0 8px 4px 8px; }
          .title { font-size: 1.1em; font-weight: 500; }
          .status { color: var(--secondary-text-color); font-size: .85em; }
          .legend { display:flex; flex-wrap:wrap; gap:10px; padding: 4px 8px 8px 8px;
                    font-size:.75em; color: var(--secondary-text-color); }
          .legend span { display:flex; align-items:center; gap:4px; }
          .swatch { width:10px; height:3px; border-radius:2px; display:inline-block; }
          .swatch.band { height:8px; opacity:.25; }
          .empty { padding: 16px; color: var(--secondary-text-color); }
          svg { width: 100%; display: block; }
        </style>
        <ha-card><div class="head"><div class="title"></div><div class="status"></div></div>
        <div class="body"></div><div class="legend"></div></ha-card>`;
      this._root = this.shadowRoot.querySelector("ha-card");
    }

    const hub = findHub(hass);
    const status = stateOf(hass, hub["sensor.optimization_status"]);
    const now = Date.now();
    const past = this._history(hass, hub, now);

    // History in front of the plan on every lane, so the chart reads left to
    // right as one story: what happened, then what is going to.
    const planned = (key) => series(stateOf(hass, hub[key]));
    const pv = mergeHistory(past.pv, planned("sensor.pv_forecast"));
    const load = mergeHistory(past.load, planned("sensor.load_forecast"));
    const grid = mergeHistory(past.grid, planned("sensor.grid_forecast"));
    const battery = mergeHistory(past.battery, planned("sensor.battery_power"));
    const soc = mergeHistory(past.soc, planned("sensor.battery_soc"));
    const buy = mergeHistory(past.buy, planned("sensor.buy_price"));
    const sell = mergeHistory(past.sell, planned("sensor.sell_price"));

    this.shadowRoot.querySelector(".title").textContent =
      this._config.title || "Energy plan";
    this.shadowRoot.querySelector(".status").textContent = status ? `${status.state}` : "";

    const body = this.shadowRoot.querySelector(".body");
    body.textContent = "";

    if (!pv.length && !load.length && !grid.length && !buy.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No plan yet. It appears after the first optimisation runs.";
      body.appendChild(empty);
      this.shadowRoot.querySelector(".legend").textContent = "";
      return;
    }

    const loads = showsSection(this._config, "show_loads")
      ? findLoads(hass).map((entry) => ({
          name: entry.name,
          points: series(stateOf(hass, entry.entities.scheduled_power), "schedule"),
        }))
      : [];

    body.appendChild(
      this._chart({ pv, load, grid, battery, soc, buy, sell, loads, now }),
    );
    this._legend(loads.length > 0);
  }

  _legend(hasLoads) {
    const node = this.shadowRoot.querySelector(".legend");
    if (!showsSection(this._config, "show_legend")) {
      node.textContent = "";
      return;
    }
    const items = [];
    if (showsSection(this._config, "show_power")) {
      items.push(
        ["Solar", COLORS.pv],
        ["Consumption", COLORS.load],
        ["Grid", COLORS.gridIn],
        ["Battery", COLORS.battery],
      );
    }
    if (showsSection(this._config, "show_soc")) items.push(["Charge level", COLORS.soc]);
    if (showsSection(this._config, "show_price")) {
      items.push(["Import price", COLORS.buy], ["Export price", COLORS.sell]);
    }
    if (hasLoads) items.push(["Scheduled loads", COLORS.gridOut]);
    node.textContent = "";
    for (const [label, color] of items) {
      const span = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = "swatch";
      swatch.style.background = color;
      span.appendChild(swatch);
      span.appendChild(document.createTextNode(label));
      node.appendChild(span);
    }
    // Named rather than left to be guessed: a recorded reading and a forecast
    // look identical once they are drawn, and the shading is the only thing
    // that says which half of the chart is a record.
    const hours = this._historyMs() / 3600000;
    if (hours > 0) {
      const span = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = "swatch band";
      swatch.style.background = COLORS.past;
      span.appendChild(swatch);
      span.appendChild(document.createTextNode(`Recorded, last ${formatHours(hours)}`));
      node.appendChild(span);
    }
  }

  _chart(data) {
    const width = 600;
    const padL = 44;
    const padR = 44;
    const showPower = showsSection(this._config, "show_power");
    const showPrice = showsSection(this._config, "show_price");
    const showSoc = showsSection(this._config, "show_soc");
    const powerH = showPower ? 150 : 0;
    const priceH = showPrice || showSoc ? 70 : 0;
    const rowH = 16;
    const gap = 26;
    const loadsH = data.loads.length ? data.loads.length * rowH + 8 : 0;
    const panels = [powerH, priceH, loadsH].filter(Boolean);
    const height = panels.reduce((sum, size) => sum + size, 0) + gap * (panels.length - 1) + 34;

    const root = svg("svg", {
      viewBox: `0 0 ${width} ${height}`,
      preserveAspectRatio: "none",
      role: "img",
    });

    // A rolling window, not the data's own extent: the price series alone
    // reaches back to last midnight, and eleven hours of spent morning squeeze
    // the part of the plan that can still be changed into a third of the card.
    // The end is still the data's, since that is the horizon.
    const lists = [data.pv, data.load, data.grid, data.buy, ...data.loads.map((l) => l.points)];
    const t1 = Math.max(timeExtent(lists)[1], data.now);
    const t0 = Math.min(data.now - this._historyMs(), t1 - 60000);
    const x = (t) => padL + ((t - t0) / (t1 - t0 || 1)) * (width - padL - padR);
    // Trimmed to the window rather than left to run off the edge, so a series
    // that starts at last midnight cannot draw over the y-axis labels.
    const clip = (points) => clipSeries(points, t0, t1);

    let top = 8;

    /* ---- power panel ---- */
    if (showPower) {
      const pv = clip(data.pv);
      const houseLoad = clip(data.load);
      const grid = clip(data.grid);
      const battery = clip(data.battery);
      const [pLo, pHi] = extent([pv, houseLoad, grid, battery]);
      const lo = Math.min(pLo, 0);
      const y = (v) => top + powerH - ((v - lo) / (pHi - lo || 1)) * powerH;

      this._past(root, x, t0, data.now, top, powerH);
      this._axis(root, padL, width - padR, top, powerH, [lo, pHi], formatPower);
      if (lo < 0) {
        // Zero line matters here: above it is import, below is export.
        svg("line", {
          x1: padL, x2: width - padR, y1: y(0), y2: y(0),
          stroke: COLORS.muted, "stroke-width": 1, "stroke-dasharray": "2 2",
        }, root);
      }

      this._area(root, pv, x, y, COLORS.pv, y(lo));
      this._line(root, grid, x, y, COLORS.gridIn, 1.5);
      this._line(root, battery, x, y, COLORS.battery, 1.5);
      this._line(root, houseLoad, x, y, COLORS.load, 2);
      top += powerH + gap;
    }

    /* ---- price panel, with charge level on the right ---- */
    if (priceH) {
      const buy = clip(data.buy);
      const sell = clip(data.sell);
      this._past(root, x, t0, data.now, top, priceH);
      if (showPrice) {
        const [cLo, cHi] = extent([buy, sell]);
        const yP = (v) => top + priceH - ((v - cLo) / (cHi - cLo || 1)) * priceH;
        this._axis(root, padL, width - padR, top, priceH, [cLo, cHi], (v) => v.toFixed(2));
        this._line(root, buy, x, yP, COLORS.buy, 1.5);
        this._line(root, sell, x, yP, COLORS.sell, 1.5);
      }

      const soc = clip(data.soc);
      if (showSoc && soc.length) {
        const panelTop = top;
        const yS = (v) => panelTop + priceH - (v / 100) * priceH;
        if (!showPrice) {
          this._axis(root, padL, width - padR, top, priceH, [0, 100], (v) => `${v.toFixed(0)}%`);
        }
        this._line(root, soc, x, yS, COLORS.soc, 1.5, "3 2");
        for (const value of [0, 50, 100]) {
          svg("text", {
            x: width - padR + 4, y: yS(value) + 3,
            fill: COLORS.soc, "font-size": 9,
          }, root).textContent = `${value}%`;
        }
      }
      top += priceH + gap;
    }

    /* ---- deferrable load rows ---- */
    if (loadsH) {
      this._past(root, x, t0, data.now, top, loadsH);
      let rowY = top;
      data.loads.forEach((entry) => {
        svg("text", {
          x: 0, y: rowY + 10, fill: COLORS.muted, "font-size": 9,
        }, root).textContent = entry.name.slice(0, 7);
        this._blocks(root, clip(entry.points), x, rowY, rowH - 5);
        rowY += rowH;
      });
      top += loadsH + gap;
    }

    /* ---- time axis and the now marker ---- */
    if (data.now >= t0 && data.now <= t1) {
      svg("line", {
        x1: x(data.now), x2: x(data.now), y1: 8, y2: height - 20,
        stroke: COLORS.muted, "stroke-width": 1.5,
      }, root);
    }
    for (let i = 0; i <= 4; i++) {
      const t = t0 + ((t1 - t0) * i) / 4;
      svg("text", {
        x: x(t), y: height - 6, fill: COLORS.muted,
        "font-size": 9, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle",
      }, root).textContent = formatTime(t, this._hass);
    }

    return root;
  }

  /**
   * A darker band over the part of a panel that has already happened.
   *
   * The now marker alone is not enough once a panel carries history as well as
   * plan: it is a single line, and the eye reads the whole chart as one kind of
   * thing. Shading says which half is a record and which is an intention.
   */
  _past(root, x, t0, now, top, height) {
    if (!(now > t0)) return;
    svg("rect", {
      x: x(t0), y: top, width: Math.max(x(now) - x(t0), 0), height,
      fill: COLORS.past, "fill-opacity": 0.12,
    }, root);
  }

  _axis(root, x0, x1, top, height, bounds, format) {
    for (const [value, offset] of [
      [bounds[1], 0],
      [bounds[0], height],
    ]) {
      svg("line", {
        x1: x0, x2: x1, y1: top + offset, y2: top + offset,
        stroke: COLORS.grid, "stroke-width": 1,
      }, root);
      svg("text", {
        x: x0 - 4, y: top + offset + 3, fill: COLORS.muted,
        "font-size": 9, "text-anchor": "end",
      }, root).textContent = format(value);
    }
  }

  _path(points, x, y) {
    return points.map((p, i) => `${i ? "L" : "M"}${x(p.t)},${y(p.v)}`).join("");
  }

  _line(root, points, x, y, color, width, dash) {
    if (points.length < 2) return;
    svg("path", {
      d: this._path(points, x, y),
      fill: "none", stroke: color, "stroke-width": width,
      "stroke-dasharray": dash || null,
      "stroke-linejoin": "round",
    }, root);
  }

  _area(root, points, x, y, color, baseline) {
    if (points.length < 2) return;
    const d =
      this._path(points, x, y) +
      `L${x(points[points.length - 1].t)},${baseline}` +
      `L${x(points[0].t)},${baseline}Z`;
    svg("path", { d, fill: color, "fill-opacity": 0.25, stroke: "none" }, root);
    this._line(root, points, x, y, color, 1.5);
  }

  _blocks(root, points, x, top, height) {
    // A block spans from a point to the next one, so the last point needs a
    // width taken from the preceding interval rather than being dropped.
    for (let i = 0; i < points.length; i++) {
      if (points[i].v <= 0) continue;
      const start = points[i].t;
      const end =
        i + 1 < points.length
          ? points[i + 1].t
          : start + (i > 0 ? points[i].t - points[i - 1].t : 1800000);
      svg("rect", {
        x: x(start), y: top,
        width: Math.max(x(end) - x(start), 1), height,
        fill: COLORS.gridOut, "fill-opacity": 0.8, rx: 2,
      }, root);
    }
  }
}

/* ------------------------------------------------------- plan card editor */

const PLAN_LABELS = {
  title: "Title",
  history_hours: "Hours of history",
  solar_entity: "Measured solar power",
  house_entity: "Measured house consumption",
  grid_entity: "Measured grid power",
  battery_entity: "Measured battery power",
  invert_battery: "That battery sensor is positive when charging",
};
const PLAN_HELPERS = {
  history_hours: "How far back the chart reaches before now. 0 starts it at now.",
  solar_entity: "Left empty, the past is drawn from the forecast the plan used.",
  house_entity: "Left empty, the past is drawn from the plan's own consumption forecast.",
  grid_entity: "Left empty, the past is drawn from the plan's own grid figure.",
  battery_entity:
    "Left empty, the Companion's own battery power sensor is used, and the plan's figure if it has none. Set here only to draw this card from a different meter.",
  invert_battery:
    "Only read when a sensor is named above; the Companion carries its own convention. The chart draws positive as discharge, which is the plan's convention.",
};
for (const section of PLAN_SECTIONS) {
  PLAN_LABELS[section[0]] = section[1];
  PLAN_HELPERS[section[0]] = section[2];
}

// The entity overrides, in the order the lanes are drawn.
const PLAN_ENTITY_OPTIONS = ["solar_entity", "house_entity", "grid_entity", "battery_entity"];

const PLAN_SCHEMA = [
  { name: "title", selector: { text: {} } },
  sectionGrid(PLAN_SECTIONS),
  // Folded away by default: the window and four pickers are worth having, but
  // not worth being the first thing between the title and the switches.
  {
    name: "",
    type: "expandable",
    title: "History",
    icon: "mdi:history",
    schema: [
      {
        name: "history_hours",
        selector: { number: { min: 0, max: 12, step: 0.5, mode: "box", unit_of_measurement: "h" } },
      },
      {
        name: "",
        type: "grid",
        column_min_width: "260px",
        schema: PLAN_ENTITY_OPTIONS.map((name) => ({
          name,
          selector: { entity: { filter: { domain: "sensor" } } },
        })),
      },
      { name: "invert_battery", selector: { boolean: {} } },
    ],
  },
];

/** What the plan card draws, and where its historic side comes from. */
class EmhassPlanCardEditor extends CardEditor {
  get labels() {
    return PLAN_LABELS;
  }

  get helpers() {
    return PLAN_HELPERS;
  }

  schema() {
    return PLAN_SCHEMA;
  }

  data() {
    const data = { title: this._config.title || "" };
    for (const section of PLAN_SECTIONS) {
      data[section[0]] = showsSection(this._config, section[0]);
    }
    const hours = Number(this._config.history_hours);
    data.history_hours = Number.isFinite(hours) ? hours : DEFAULT_PLAN_HISTORY_HOURS;
    data.invert_battery = this._config.invert_battery === true;
    // Left out rather than set empty: an entity selector reads "" as a
    // selection that does not exist and clears itself on the next keystroke.
    for (const key of PLAN_ENTITY_OPTIONS) {
      if (this._config[key]) data[key] = this._config[key];
    }
    return data;
  }

  clean(config) {
    cleanSections(config, PLAN_SECTIONS);
    if (!config.title) delete config.title;
    for (const key of PLAN_ENTITY_OPTIONS) {
      if (!config[key]) delete config[key];
    }
    if (config.invert_battery !== true) delete config.invert_battery;
    if (
      config.history_hours === undefined ||
      Number(config.history_hours) === DEFAULT_PLAN_HISTORY_HOURS
    ) {
      delete config.history_hours;
    }
    return config;
  }
}

customElements.define("emhass-plan-card", EmhassPlanCard);
customElements.define("emhass-plan-card-editor", EmhassPlanCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-plan-card",
    name: "EMHASS Companion plan",
    description: "The optimisation plan: solar, consumption, grid, battery, prices and scheduled loads.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);
