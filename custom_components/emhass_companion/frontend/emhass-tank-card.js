/**
 * The temperature plan of one thermal deferrable load -- a hot-water tank or a
 * room heater -- drawn the way the plan card draws everything else.
 *
 * What it shows is the optimiser's own picture of the load: the temperature it
 * expects the load to be at, between the floor and the ceiling it was told to
 * stay inside. The shape is the point. A tank that is heated in a burst while
 * electricity is cheap and then coasts down towards the floor is the plan
 * working; a line that hugs the floor all day, or the ceiling, says the
 * comfort band or the heating rate is wrong.
 */

import {
  COLORS,
  CardEditor,
  DEFAULT_PLAN_HISTORY_HOURS,
  cleanSections,
  clipSeries,
  findLoads,
  formatHours,
  formatTime,
  loadHaForm,
  readHistory,
  sectionGrid,
  series,
  showsSection,
  stateOf,
  svg,
} from "./emhass-core.js?v=__VERSION__";


const TANK_SECTIONS = [
  ["show_measured", "Measured temperature", "The probe's own recorded reading, before now"],
  ["show_legend", "Legend", "The colour key under the chart"],
];

const TANK_COLORS = {
  planned: COLORS.pv,
  min: COLORS.gridOut,
  max: COLORS.gridIn,
  measured: COLORS.load,
};

/** Loads that have a temperature plan: the thermal ones. */
function thermalLoads(hass) {
  return findLoads(hass).filter((load) => "planned_temperature" in load.entities);
}

function pickLoad(hass, wanted) {
  const loads = thermalLoads(hass);
  if (!loads.length) return null;
  if (!wanted) return loads[0];
  const needle = String(wanted).toLowerCase();
  return loads.find((load) => load.name.toLowerCase() === needle || load.id === wanted) || null;
}

/** A tick spacing that gives three to six lines over the range. */
function tickStep(span) {
  const rough = span / 4;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough || 1)));
  for (const factor of [1, 2, 5, 10]) {
    if (magnitude * factor >= rough) return magnitude * factor;
  }
  return magnitude * 10;
}

class EmhassTankCard extends HTMLElement {
  static getStubConfig() {
    return { type: "custom:emhass-tank-card" };
  }

  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-tank-card-editor");
  }

  setConfig(config) {
    this._config = Object.assign({}, config);
    this._root = null;
  }

  getCardSize() {
    return 4;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  connectedCallback() {
    this._observe();
  }

  disconnectedCallback() {
    if (this._resizer) this._resizer.disconnect();
    this._resizer = null;
  }

  _observe() {
    if (this._resizer || typeof ResizeObserver !== "function" || !this._root) return;
    const body = this.shadowRoot.querySelector(".body");
    if (!body) return;
    this._resizer = new ResizeObserver(() => {
      if (Math.abs(Math.round(body.clientWidth) - (this._width || 0)) > 1) this.refresh();
    });
    this._resizer.observe(body);
  }

  /** Redraw with the state already in hand, for history that arrived late. */
  refresh() {
    if (this._hass) this._render();
  }

  _historyMs() {
    const hours = Number(this._config.history_hours);
    return (
      (Number.isFinite(hours) && hours >= 0 ? hours : DEFAULT_PLAN_HISTORY_HOURS) * 3600000
    );
  }

  _render() {
    const hass = this._hass;
    if (!hass) return;

    if (!this._root) {
      // Attached at most once per element: the editor calls setConfig again on
      // the same element, and a second attachShadow throws.
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `
        <style>
          :host { display: block; max-width: var(--emh-max-width, 960px); margin-inline: auto; }
          ha-card { padding: 12px 8px 4px 8px; }
          .head { display:flex; justify-content:space-between; align-items:baseline;
                  padding: 0 8px 4px 8px; }
          .title { font-size: 1.1em; font-weight: 500; }
          .status { color: var(--secondary-text-color); font-size: .85em; }
          .legend { display:flex; flex-wrap:wrap; gap:10px; padding: 4px 8px 8px 8px;
                    font-size:.7em; color: var(--secondary-text-color); }
          .legend span { display:flex; align-items:center; gap:4px; }
          .swatch { width:10px; height:3px; border-radius:2px; display:inline-block; }
          .swatch.band { height:8px; opacity:.25; }
          .empty { padding: 16px; color: var(--secondary-text-color); }
          svg { width: 100%; display: block; }
        </style>
        <ha-card><div class="head"><div class="title"></div><div class="status"></div></div>
        <div class="body"></div><div class="legend"></div></ha-card>`;
      this._root = this.shadowRoot.querySelector("ha-card");
      if (this._resizer) this._resizer.disconnect();
      this._resizer = null;
      this._observe();
    }

    const title = this.shadowRoot.querySelector(".title");
    const status = this.shadowRoot.querySelector(".status");
    const body = this.shadowRoot.querySelector(".body");
    const legend = this.shadowRoot.querySelector(".legend");
    body.textContent = "";
    legend.textContent = "";

    const load = pickLoad(hass, this._config.load);
    title.textContent =
      this._config.title || (load ? `${load.name} temperature` : "Temperature plan");
    status.textContent = "";

    const empty = (text) => {
      const node = document.createElement("div");
      node.className = "empty";
      node.textContent = text;
      body.appendChild(node);
    };

    if (!load) {
      empty(
        this._config.load
          ? `No thermal load named "${this._config.load}".`
          : "No thermal load yet. Add a water tank or a heating load to see its temperature plan.",
      );
      return;
    }

    const plannedState = stateOf(hass, load.entities.planned_temperature);
    const planned = series(plannedState);
    const floor = series(plannedState, "min_forecast");
    const ceiling = series(plannedState, "max_forecast");
    if (!planned.length) {
      empty("No temperature plan yet. It appears after the first optimisation runs.");
      return;
    }

    const now = Date.now();
    const probe = load.entities.measured_temperature;
    const showMeasured = showsSection(this._config, "show_measured") && Boolean(probe);
    const measured = showMeasured
      ? readHistory(this, hass, { measured: probe }, [], this._historyMs(), now).measured
      : [];

    const nowState = stateOf(hass, probe) || plannedState;
    const reading = Number(nowState.state);
    if (Number.isFinite(reading)) {
      const unit = (nowState.attributes && nowState.attributes.unit_of_measurement) || "°C";
      status.textContent = `${reading.toFixed(1)} ${unit}`;
    }

    this._width = Math.max(300, Math.round(body.clientWidth) || 600);
    body.appendChild(this._chart({ planned, floor, ceiling, measured, now, width: this._width }));
    this._legend(floor.length > 0, ceiling.length > 0, measured.length > 0);
  }

  _legend(hasFloor, hasCeiling, hasMeasured) {
    const node = this.shadowRoot.querySelector(".legend");
    if (!showsSection(this._config, "show_legend")) return;
    const items = [["Planned temperature", TANK_COLORS.planned]];
    if (hasFloor) items.push(["Minimum", TANK_COLORS.min]);
    if (hasCeiling) items.push(["Maximum", TANK_COLORS.max]);
    if (hasMeasured) items.push(["Measured", TANK_COLORS.measured]);
    for (const [label, color] of items) {
      const span = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = "swatch";
      swatch.style.background = color;
      span.appendChild(swatch);
      span.appendChild(document.createTextNode(label));
      node.appendChild(span);
    }
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

  /** The spacing of the plan's own samples, for how long the last one holds. */
  _interval(points) {
    for (let i = points.length - 1; i > 0; i--) {
      const span = points[i].t - points[i - 1].t;
      if (span > 0 && span <= 6 * 3600000) return span;
    }
    return 1800000;
  }

  /** Held flat until the next sample, then a vertical step. */
  _stepPath(points, x, y) {
    const hold = this._interval(points);
    let d = "";
    for (let i = 0; i < points.length; i++) {
      const left = points[i].t;
      const right = i + 1 < points.length ? points[i + 1].t : left + hold;
      const yy = y(points[i].v);
      d += `${i ? "L" : "M"}${x(left)},${yy}L${x(right)},${yy}`;
    }
    return d;
  }

  _linePath(points, x, y) {
    return points.map((p, i) => `${i ? "L" : "M"}${x(p.t)},${y(p.v)}`).join("");
  }

  _chart(data) {
    const width = data.width;
    const padL = 44;
    const padR = 12;
    const top = 12;
    const plotH = 200;
    const height = top + plotH + 28;

    const root = svg("svg", {
      viewBox: `0 0 ${width} ${height}`,
      preserveAspectRatio: "none",
      role: "img",
    });
    root.style.height = `${height}px`;

    const planEnd = data.planned[data.planned.length - 1].t + this._interval(data.planned);
    const t1 = Math.max(planEnd, data.now + 60000);
    const t0 = Math.min(data.now - this._historyMs(), t1 - 60000);
    const x = (t) => padL + ((t - t0) / (t1 - t0 || 1)) * (width - padL - padR);
    const clip = (points) => clipSeries(points, t0, t1);

    const planned = clip(data.planned);
    const floor = clip(data.floor);
    const ceiling = clip(data.ceiling);
    const measured = clip(data.measured);

    let lo = Infinity;
    let hi = -Infinity;
    for (const list of [planned, floor, ceiling, measured]) {
      for (const point of list) {
        if (point.v < lo) lo = point.v;
        if (point.v > hi) hi = point.v;
      }
    }
    if (!Number.isFinite(lo)) {
      lo = 0;
      hi = 1;
    }
    const step = tickStep(Math.max(hi - lo, 1));
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    if (hi === lo) hi = lo + step;
    const y = (v) => top + plotH - ((v - lo) / (hi - lo)) * plotH;

    const dark = Boolean(this._hass && this._hass.themes && this._hass.themes.darkMode);
    const fill = dark
      ? "color-mix(in srgb, var(--card-background-color, #1c1c1c) 80%, black)"
      : "var(--secondary-background-color, #f5f5f5)";
    const frame = { x: padL, y: top, width: width - padL - padR, height: plotH, rx: 6 };
    svg("rect", Object.assign({ fill }, frame), root);

    // The part that has already happened is shaded, so a recorded reading and
    // a plan do not read as one kind of line.
    if (data.now > t0) {
      const clipId = "emh-tank-box";
      const clipPath = svg("clipPath", { id: clipId }, root);
      svg("rect", frame, clipPath);
      svg("rect", {
        x: x(t0), y: top, width: Math.max(x(data.now) - x(t0), 0), height: plotH,
        fill: COLORS.past, "fill-opacity": 0.12, "clip-path": `url(#${clipId})`,
      }, root);
    }

    for (let value = lo; value <= hi + step / 2; value += step) {
      svg("line", {
        x1: padL, x2: width - padR, y1: y(value), y2: y(value),
        stroke: COLORS.grid, "stroke-width": 1,
      }, root);
      svg("text", {
        x: padL - 4, y: y(value) + 3, fill: COLORS.muted,
        "font-size": 10, "text-anchor": "end",
      }, root).textContent = `${Math.round(value * 10) / 10}°`;
    }
    svg("rect", Object.assign({
      fill: "none", stroke: COLORS.grid, "stroke-width": 1,
    }, frame), root);

    const draw = (points, color, width2, stepped) => {
      if (!points.length) return;
      svg("path", {
        d: stepped ? this._stepPath(points, x, y) : this._linePath(points, x, y),
        fill: "none", stroke: color, "stroke-width": width2,
        "stroke-linejoin": stepped ? "miter" : "round",
        "vector-effect": "non-scaling-stroke",
      }, root);
    };
    draw(ceiling, TANK_COLORS.max, 2, true);
    draw(floor, TANK_COLORS.min, 2, true);
    draw(planned, TANK_COLORS.planned, 2.5, true);
    draw(measured, TANK_COLORS.measured, 1.5, false);

    if (data.now >= t0 && data.now <= t1) {
      svg("line", {
        x1: x(data.now), x2: x(data.now), y1: top, y2: top + plotH,
        stroke: COLORS.muted, "stroke-width": 1.5,
      }, root);
    }

    const ticks = Math.max(2, Math.min(12, Math.round((width - padL - padR) / 130)));
    for (let i = 0; i <= ticks; i++) {
      const t = t0 + ((t1 - t0) * i) / ticks;
      svg("text", {
        x: x(t), y: height - 8, fill: COLORS.muted,
        "font-size": 10, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle",
      }, root).textContent = formatTime(t, this._hass);
    }
    return root;
  }
}

/* ------------------------------------------------------- tank card editor */

const TANK_LABELS = {
  load: "Load",
  title: "Title",
  history_hours: "Hours of history",
};
const TANK_HELPERS = {
  load: "Which thermal load this card is about. Left empty, it shows the first one.",
  history_hours: "How far back the chart reaches before now. 0 starts it at now.",
};
for (const section of TANK_SECTIONS) {
  TANK_LABELS[section[0]] = section[1];
  TANK_HELPERS[section[0]] = section[2];
}

class EmhassTankCardEditor extends CardEditor {
  get labels() {
    return TANK_LABELS;
  }

  get helpers() {
    return TANK_HELPERS;
  }

  schema(hass) {
    const options = thermalLoads(hass).map((load) => ({ value: load.name, label: load.name }));
    return [
      {
        name: "load",
        selector: options.length
          ? { select: { mode: "dropdown", custom_value: true, options } }
          : { text: {} },
      },
      { name: "title", selector: { text: {} } },
      sectionGrid(TANK_SECTIONS),
      {
        name: "history_hours",
        selector: { number: { min: 0, max: 12, step: 0.5, mode: "box", unit_of_measurement: "h" } },
      },
    ];
  }

  data() {
    const data = { load: this._config.load || "", title: this._config.title || "" };
    for (const section of TANK_SECTIONS) {
      data[section[0]] = showsSection(this._config, section[0]);
    }
    const hours = Number(this._config.history_hours);
    data.history_hours = Number.isFinite(hours) ? hours : DEFAULT_PLAN_HISTORY_HOURS;
    return data;
  }

  clean(config) {
    cleanSections(config, TANK_SECTIONS);
    if (!config.load) delete config.load;
    if (!config.title) delete config.title;
    if (
      config.history_hours === undefined ||
      Number(config.history_hours) === DEFAULT_PLAN_HISTORY_HOURS
    ) {
      delete config.history_hours;
    }
    return config;
  }
}

customElements.define("emhass-tank-card", EmhassTankCard);
customElements.define("emhass-tank-card-editor", EmhassTankCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "emhass-tank-card",
  name: "EMHASS Companion tank temperature",
  description:
    "A thermal load's planned temperature against its minimum and maximum: a hot-water tank or a heater.",
  preview: true,
  documentationURL: "https://github.com/smefa/emhass-ha-companion",
});
