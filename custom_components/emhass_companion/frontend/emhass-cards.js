/**
 * EMHASS Companion dashboard cards.
 *
 * Deliberately plain custom elements with inline SVG: no Lit, no charting
 * library, no build step. A bundler would mean an npm toolchain in CI, a
 * committed artefact that can drift from its source, and Node as a
 * prerequisite for contributors -- none of which two cards justify. It also
 * means nothing is fetched at runtime, so the cards work on an isolated
 * network and cannot be broken by a CDN.
 *
 * Colours come from Home Assistant's energy-dashboard CSS variables, so both
 * cards follow the active theme. Every variable has a fallback in case a
 * theme does not define it.
 */

const PLATFORM = "emhass_companion";

const COLORS = {
  pv: "var(--energy-solar-color, #ff9800)",
  load: "var(--primary-text-color, #212121)",
  gridIn: "var(--energy-grid-consumption-color, #488fc2)",
  gridOut: "var(--energy-grid-return-color, #8353d1)",
  battery: "var(--energy-battery-out-color, #4db6ac)",
  soc: "var(--energy-battery-in-color, #f06292)",
  buy: "var(--error-color, #db4437)",
  sell: "var(--success-color, #43a047)",
  grid: "var(--divider-color, #e0e0e0)",
  muted: "var(--secondary-text-color, #727272)",
  deadline: "var(--warning-color, #ffa600)",
};

/* ------------------------------------------------------------------ utils */

const ns = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(ns, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  if (parent) parent.appendChild(node);
  return node;
}

/** Parse a `[{time, value}]` attribute into sorted numeric points. */
function series(stateObj, attribute = "forecast") {
  const raw = stateObj && stateObj.attributes ? stateObj.attributes[attribute] : null;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((point) => ({
      t: Date.parse(point.time),
      v: Number(point.value),
    }))
    .filter((point) => Number.isFinite(point.t) && Number.isFinite(point.v))
    .sort((a, b) => a.t - b.t);
}

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

function formatPower(watts) {
  if (!Number.isFinite(watts)) return "–";
  return Math.abs(watts) >= 1000
    ? `${(watts / 1000).toFixed(1)} kW`
    : `${Math.round(watts)} W`;
}

function formatTime(ms, hass) {
  const language = (hass && hass.locale && hass.locale.language) || undefined;
  return new Date(ms).toLocaleTimeString(language, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Time remaining, as a deadline is best read: "in 4 h 20 m".
 *
 * A wall-clock time would make the reader do the subtraction, which is the
 * arithmetic a duration-based deadline exists to avoid in the first place.
 * Past due is called out rather than shown as a negative.
 */
function formatCountdown(ms) {
  if (!Number.isFinite(ms)) return "–";
  if (ms <= 0) return "overdue";
  const minutes = Math.floor(ms / 60000);
  const hours = Math.floor(minutes / 60);
  return hours > 0 ? `in ${hours} h ${minutes % 60} m` : `in ${minutes} m`;
}

/* --------------------------------------------------------------- discovery */

/**
 * Find this integration's entities.
 *
 * Matching on the entity registry's platform rather than on entity_id
 * prefixes, because entity ids are renameable and a rename should not
 * silently empty the card.
 */
function findEntities(hass) {
  const found = {};
  const registry = hass.entities || {};
  for (const [entityId, entry] of Object.entries(registry)) {
    if (entry.platform !== PLATFORM) continue;
    const uid = entry.unique_id || "";
    const key = uid.split("_").slice(1).join("_") || entityId;
    found[key] = entityId;
  }
  return found;
}

/**
 * Entities of this integration grouped by their device (one per load).
 *
 * Each load's `entities` is a map of unique_id key -> entity id, for the same
 * reason findEntities keys on unique_id: a load entity's unique_id is
 * `{entry_id}_{subentry_id}_{key}`, and only that trailing key is stable.
 * The entity *id* is built from the entity's translated name, so on a Swedish
 * install `should_run` is `binary_sensor.<load>_ska_kora` -- matching English
 * fragments of it found nothing at all, and a user renaming an entity broke it
 * the same way. Neither entry_id nor subentry_id is a ULID containing an
 * underscore, so the key is everything from the third segment on.
 */
function findLoads(hass) {
  const devices = hass.devices || {};
  const registry = hass.entities || {};
  const loads = new Map();

  for (const [entityId, entry] of Object.entries(registry)) {
    if (entry.platform !== PLATFORM || !entry.device_id) continue;
    const device = devices[entry.device_id];
    if (!device) continue;
    const name = device.name_by_user || device.name || "";
    if (!loads.has(entry.device_id)) {
      loads.set(entry.device_id, { id: entry.device_id, name, entities: {} });
    }
    const key = (entry.unique_id || "").split("_").slice(2).join("_");
    if (key) loads.get(entry.device_id).entities[key] = entityId;
  }

  // The hub device holds the plan sensors; only per-load devices carry a
  // should_run entity, which is what distinguishes them. The hub also has its
  // own binary_sensor (plan_stale), so matching on the domain alone would
  // wrongly count it as a load too -- and then surface its control_enabled
  // switch and system_mode select inside a per-load card.
  return [...loads.values()].filter((load) => "should_run" in load.entities);
}

function pick(hass, entities, suffix) {
  const match = Object.entries(entities).find(([key]) => key.endsWith(suffix));
  return match ? hass.states[match[1]] : undefined;
}

/* ------------------------------------------------------------- plan card */

class EmhassPlanCard extends HTMLElement {
  static getStubConfig() {
    return { type: `custom:emhass-plan-card` };
  }

  setConfig(config) {
    this._config = { hours: 24, ...config };
    this._root = null;
  }

  getCardSize() {
    return 8;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
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
          .empty { padding: 16px; color: var(--secondary-text-color); }
          svg { width: 100%; display: block; }
        </style>
        <ha-card><div class="head"><div class="title"></div><div class="status"></div></div>
        <div class="body"></div><div class="legend"></div></ha-card>`;
      this._root = this.shadowRoot.querySelector("ha-card");
    }

    const entities = findEntities(hass);
    const pv = series(pick(hass, entities, "pv_forecast"));
    const load = series(pick(hass, entities, "load_forecast"));
    const grid = series(pick(hass, entities, "grid_forecast"));
    const battery = series(pick(hass, entities, "battery_power"));
    const soc = series(pick(hass, entities, "battery_soc"));
    const buy = series(pick(hass, entities, "buy_price"));
    const sell = series(pick(hass, entities, "sell_price"));
    const status = pick(hass, entities, "optimization_status");

    this.shadowRoot.querySelector(".title").textContent =
      this._config.title || "Energy plan";
    this.shadowRoot.querySelector(".status").textContent = status
      ? `${status.state}`
      : "";

    const body = this.shadowRoot.querySelector(".body");
    body.textContent = "";

    if (!pv.length && !load.length && !grid.length && !buy.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent =
        "No plan yet. It appears after the first optimisation runs.";
      body.appendChild(empty);
      return;
    }

    const loads = findLoads(hass).map((entry) => ({
      name: entry.name,
      points: series(
        hass.states[entry.entities.scheduled_power || ""],
        "schedule",
      ),
    }));

    body.appendChild(
      this._chart({ pv, load, grid, battery, soc, buy, sell, loads }),
    );
    this._legend(loads.length > 0);
  }

  _legend(hasLoads) {
    const items = [
      ["Solar", COLORS.pv],
      ["Consumption", COLORS.load],
      ["Grid", COLORS.gridIn],
      ["Battery", COLORS.battery],
      ["Charge level", COLORS.soc],
      ["Import price", COLORS.buy],
      ["Export price", COLORS.sell],
    ];
    if (hasLoads) items.push(["Scheduled loads", COLORS.gridOut]);
    this.shadowRoot.querySelector(".legend").innerHTML = items
      .map(
        ([label, color]) =>
          `<span><i class="swatch" style="background:${color}"></i>${label}</span>`,
      )
      .join("");
  }

  _chart(data) {
    const width = 600;
    const padL = 44;
    const padR = 44;
    const powerH = 150;
    const priceH = 70;
    const rowH = 16;
    const gap = 26;
    const loadsH = data.loads.length ? data.loads.length * rowH + 8 : 0;
    const height = powerH + gap + priceH + (loadsH ? gap + loadsH : 0) + 26;

    const svg = el("svg", {
      viewBox: `0 0 ${width} ${height}`,
      preserveAspectRatio: "none",
      role: "img",
    });

    const [t0, t1] = timeExtent([
      data.pv,
      data.load,
      data.grid,
      data.buy,
      ...data.loads.map((l) => l.points),
    ]);
    const x = (t) => padL + ((t - t0) / (t1 - t0 || 1)) * (width - padL - padR);

    /* ---- power panel ---- */
    const powerTop = 8;
    const [pLo, pHi] = extent([data.pv, data.load, data.grid, data.battery]);
    const lo = Math.min(pLo, 0);
    const y = (v) => powerTop + powerH - ((v - lo) / (pHi - lo || 1)) * powerH;

    this._axis(svg, padL, width - padR, powerTop, powerH, [lo, pHi], formatPower);
    if (lo < 0) {
      // Zero line matters here: above it is import, below is export.
      el("line", {
        x1: padL, x2: width - padR, y1: y(0), y2: y(0),
        stroke: COLORS.muted, "stroke-width": 1, "stroke-dasharray": "2 2",
      }, svg);
    }

    this._area(svg, data.pv, x, y, COLORS.pv, y(lo));
    this._line(svg, data.grid, x, y, COLORS.gridIn, 1.5);
    this._line(svg, data.battery, x, y, COLORS.battery, 1.5);
    this._line(svg, data.load, x, y, COLORS.load, 2);

    /* ---- price panel, with charge level on the right ---- */
    const priceTop = powerTop + powerH + gap;
    const [cLo, cHi] = extent([data.buy, data.sell]);
    const yP = (v) => priceTop + priceH - ((v - cLo) / (cHi - cLo || 1)) * priceH;
    this._axis(svg, padL, width - padR, priceTop, priceH, [cLo, cHi], (v) =>
      v.toFixed(2),
    );
    this._line(svg, data.buy, x, yP, COLORS.buy, 1.5);
    this._line(svg, data.sell, x, yP, COLORS.sell, 1.5);

    if (data.soc.length) {
      const yS = (v) => priceTop + priceH - (v / 100) * priceH;
      this._line(svg, data.soc, x, yS, COLORS.soc, 1.5, "3 2");
      for (const value of [0, 50, 100]) {
        el("text", {
          x: width - padR + 4, y: yS(value) + 3,
          fill: COLORS.soc, "font-size": 9,
        }, svg).textContent = `${value}%`;
      }
    }

    /* ---- deferrable load rows ---- */
    if (loadsH) {
      let rowY = priceTop + priceH + gap;
      data.loads.forEach((entry) => {
        el("text", {
          x: 0, y: rowY + 10, fill: COLORS.muted, "font-size": 9,
        }, svg).textContent = entry.name.slice(0, 7);
        this._blocks(svg, entry.points, x, rowY, rowH - 5);
        rowY += rowH;
      });
    }

    /* ---- time axis and the now marker ---- */
    const now = Date.now();
    if (now >= t0 && now <= t1) {
      el("line", {
        x1: x(now), x2: x(now), y1: powerTop, y2: height - 20,
        stroke: COLORS.muted, "stroke-width": 1.5,
      }, svg);
    }
    for (let i = 0; i <= 4; i++) {
      const t = t0 + ((t1 - t0) * i) / 4;
      el("text", {
        x: x(t), y: height - 6, fill: COLORS.muted,
        "font-size": 9, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle",
      }, svg).textContent = formatTime(t, this._hass);
    }

    return svg;
  }

  _axis(svg, x0, x1, top, height, [lo, hi], format) {
    for (const [value, offset] of [
      [hi, 0],
      [lo, height],
    ]) {
      el("line", {
        x1: x0, x2: x1, y1: top + offset, y2: top + offset,
        stroke: COLORS.grid, "stroke-width": 1,
      }, svg);
      el("text", {
        x: x0 - 4, y: top + offset + 3, fill: COLORS.muted,
        "font-size": 9, "text-anchor": "end",
      }, svg).textContent = format(value);
    }
  }

  _path(points, x, y) {
    return points.map((p, i) => `${i ? "L" : "M"}${x(p.t)},${y(p.v)}`).join("");
  }

  _line(svg, points, x, y, color, width, dash) {
    if (points.length < 2) return;
    el("path", {
      d: this._path(points, x, y),
      fill: "none", stroke: color, "stroke-width": width,
      "stroke-dasharray": dash || null,
      "stroke-linejoin": "round",
    }, svg);
  }

  _area(svg, points, x, y, color, baseline) {
    if (points.length < 2) return;
    const d =
      this._path(points, x, y) +
      `L${x(points[points.length - 1].t)},${baseline}` +
      `L${x(points[0].t)},${baseline}Z`;
    el("path", { d, fill: color, "fill-opacity": 0.25, stroke: "none" }, svg);
    this._line(svg, points, x, y, color, 1.5);
  }

  _blocks(svg, points, x, top, height) {
    // A block spans from a point to the next one, so the last point needs a
    // width taken from the preceding interval rather than being dropped.
    for (let i = 0; i < points.length; i++) {
      if (points[i].v <= 0) continue;
      const start = points[i].t;
      const end =
        i + 1 < points.length
          ? points[i + 1].t
          : start + (points[i].t - (i > 0 ? points[i - 1].t : start - 1800000));
      el("rect", {
        x: x(start), y: top,
        width: Math.max(x(end) - x(start), 1), height,
        fill: COLORS.gridOut, "fill-opacity": 0.8, rx: 2,
      }, svg);
    }
  }
}

/* -------------------------------------------------------- deferrable card */

class EmhassDeferrableCard extends HTMLElement {
  static getStubConfig(hass) {
    const loads = findLoads(hass);
    return {
      type: `custom:emhass-deferrable-card`,
      load: (loads[0] && loads[0].name) || "",
    };
  }

  setConfig(config) {
    this._config = config || {};
    this._root = null;
    // The markup -- and with it the .controls container -- is rebuilt on the
    // next render, so the embedded entities card is about to be detached.
    // Forgetting it here is what makes _controls rebuild it rather than skip
    // the work as unchanged and then push hass into an orphan.
    this._controlsKey = null;
    this._entitiesCard = null;
  }

  getCardSize() {
    return 4;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _resolve() {
    const loads = findLoads(this._hass);
    if (!loads.length) return null;
    if (!this._config.load) return loads[0];
    const wanted = String(this._config.load).toLowerCase();
    return (
      loads.find(
        (load) =>
          load.name.toLowerCase() === wanted || load.id === this._config.load,
      ) || null
    );
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
          ha-card { padding: 12px 16px 16px 16px; }
          .head { display:flex; justify-content:space-between; align-items:center; }
          .name { font-size:1.1em; font-weight:500; }
          .name .slot { font-size:.75em; font-weight:400; color: var(--secondary-text-color);
                        margin-left:6px; }
          .state { font-size:.9em; padding:2px 8px; border-radius:10px; }
          .on  { background: var(--success-color, #43a047); color: white; }
          .off { background: var(--divider-color, #e0e0e0);
                 color: var(--secondary-text-color); }
          .unknown { background: var(--warning-color, #ffa600); color: white; }
          .facts { display:flex; gap:20px; margin: 10px 0 6px 0;
                   font-size:.85em; color: var(--secondary-text-color); }
          .facts b { display:block; color: var(--primary-text-color);
                     font-weight:500; font-size:1.15em; }
          .controls { margin-top: 8px; }
          .empty { color: var(--secondary-text-color); }
          svg { width:100%; display:block; margin-top:4px; }
        </style>
        <ha-card>
          <div class="head"><div class="name"></div><div class="state"></div></div>
          <div class="facts"></div>
          <div class="chart"></div>
          <div class="controls"></div>
        </ha-card>`;
      this._root = this.shadowRoot.querySelector("ha-card");
    }

    const load = this._resolve();
    const name = this.shadowRoot.querySelector(".name");
    const state = this.shadowRoot.querySelector(".state");
    const facts = this.shadowRoot.querySelector(".facts");
    const chart = this.shadowRoot.querySelector(".chart");

    if (!load) {
      name.textContent = "No deferrable load";
      state.textContent = "";
      facts.innerHTML =
        `<span class="empty">Add one under the EMHASS Companion integration.</span>`;
      chart.textContent = "";
      return;
    }

    const find = (key) => hass.states[load.entities[key] || ""];

    const shouldRun = find("should_run");
    const scheduled = find("scheduled_power");
    const nextStart = find("next_start");
    const runtime = find("runtime_today");
    const recurrence = find("recurrence");
    const requested = find("requested");

    const onDemand = recurrence && recurrence.state === "on_demand";
    const onSurplus = recurrence && recurrence.state === "surplus";
    const budget = onSurplus ? find("surplus_budget") : null;
    // Only a pending request has a deadline, and the integration is the one
    // that decides that -- reading the attribute rather than recomputing it
    // here keeps the card from disagreeing with what was actually sent.
    const deadline =
      requested && requested.attributes ? Date.parse(requested.attributes.deadline_at) : NaN;
    const hasDeadline = Number.isFinite(deadline);

    // EMHASS calls its loads P_deferrable0, P_deferrable1, ... and never says
    // which appliance a number is. Showing it here is what lets someone read
    // EMHASS's own charts and logs against this dashboard. It is read live
    // rather than stored, because the number shifts as loads join and leave
    // the optimisation.
    const slot = shouldRun && shouldRun.attributes
      ? shouldRun.attributes.emhass_deferrable
      : null;
    name.textContent = load.name;
    if (slot !== null && slot !== undefined) {
      const tag = document.createElement("span");
      tag.className = "slot";
      tag.textContent = `P_deferrable${slot}`;
      name.appendChild(tag);
    }

    // Unknown is shown distinctly from off: with no usable plan the correct
    // answer is "we do not know", and rendering that as "off" would quietly
    // suggest the load should stay idle.
    const value = shouldRun ? shouldRun.state : undefined;
    state.className = `state ${value === "on" ? "on" : value === "off" ? "off" : "unknown"}`;
    state.textContent =
      value === "on" ? "Run now" : value === "off" ? "Idle" : "No plan";

    const NEXT_START_REASONS = {
      no_plan: "No plan yet",
      already_running: "Running now",
      not_scheduled: "Not scheduled",
    };
    facts.innerHTML = [
      ["Scheduled", scheduled ? formatPower(Number(scheduled.state)) : "–"],
      [
        "Next start",
        nextStart && !["unknown", "unavailable"].includes(nextStart.state)
          ? formatTime(Date.parse(nextStart.state), hass)
          : NEXT_START_REASONS[
              (nextStart && nextStart.attributes && nextStart.attributes.reason) || ""
            ] || "–",
      ],
      // Shown only while a request is actually pending: a deadline is a
      // property of one request, so an empty slot the rest of the time would
      // be noise on every daily load.
      ...(hasDeadline ? [["Deadline", formatCountdown(deadline - Date.now())]] : []),
      // A surplus load asks for no fixed run time, so "hours needed" would be
      // meaningless; what it actually got from the last plan is the number
      // worth showing, and the only visible sign of why it is or is not
      // running today.
      ...(budget && Number.isFinite(Number(budget.state))
        ? [["Spare solar", `${Number(budget.state).toFixed(1)} h`]]
        : []),
      [
        "Ran today",
        runtime && Number.isFinite(Number(runtime.state))
          ? `${Number(runtime.state).toFixed(1)} h`
          : "–",
      ],
    ]
      .map(([label, text]) => `<div>${label}<b>${text}</b></div>`)
      .join("");

    chart.textContent = "";
    const points = series(scheduled, "schedule");
    if (points.length > 1) {
      chart.appendChild(this._timeline(points, hasDeadline ? deadline : null));
    }

    this._controls(load, onDemand, onSurplus);
  }

  _timeline(points, deadline) {
    const width = 400;
    const height = 26;
    const svg = el("svg", {
      viewBox: `0 0 ${width} ${height}`,
      preserveAspectRatio: "none",
    });
    const t0 = points[0].t;
    const t1 = points[points.length - 1].t;
    const x = (t) => ((t - t0) / (t1 - t0 || 1)) * width;

    el("rect", {
      x: 0, y: 6, width, height: 12,
      fill: COLORS.grid, rx: 3,
    }, svg);

    for (let i = 0; i < points.length - 1; i++) {
      if (points[i].v <= 0) continue;
      el("rect", {
        x: x(points[i].t), y: 6,
        width: Math.max(x(points[i + 1].t) - x(points[i].t), 1), height: 12,
        fill: COLORS.pv, rx: 2,
      }, svg);
    }

    const now = Date.now();
    if (now >= t0 && now <= t1) {
      el("line", {
        x1: x(now), x2: x(now), y1: 2, y2: 22,
        stroke: COLORS.load, "stroke-width": 2,
      }, svg);
    }

    // The one question a deadline raises is whether the plan actually finishes
    // the load before it, which is exactly what this line answers at a glance.
    // Dashed so it reads as a constraint rather than as scheduled power.
    if (deadline !== null && deadline >= t0 && deadline <= t1) {
      el("line", {
        x1: x(deadline), x2: x(deadline), y1: 0, y2: 24,
        stroke: COLORS.deadline, "stroke-width": 2, "stroke-dasharray": "3 2",
      }, svg);
    }
    return svg;
  }

  _controls(load, onDemand, onSurplus) {
    const container = this.shadowRoot.querySelector(".controls");
    // The request controls are meaningless on a daily load -- its entities
    // report unavailable -- so they are left out rather than shown greyed.
    // A surplus load is armed by the same switch but has no deadline to set;
    // what it takes instead is an optional energy cap.
    let wanted = ["run_now", "enabled"];
    if (onDemand) {
      wanted = ["run_now", "enabled", "requested", "run_within"];
    } else if (onSurplus) {
      wanted = ["run_now", "enabled", "requested", "energy_needed"];
    }
    // Listed in `wanted` order rather than registry order, so the controls
    // sit in the same places on every load's card.
    const ids = wanted.map((key) => load.entities[key]).filter(Boolean);
    const config = ids.map((entity) => ({ entity }));

    if (!config.length) {
      container.textContent = "";
      this._entitiesCard = null;
      return;
    }
    if (this._controlsKey !== ids.join()) {
      this._controlsKey = ids.join();
      container.textContent = "";
      const card = document.createElement("hui-entities-card");
      card.setConfig({ type: "entities", entities: config });
      this._entitiesCard = card;
      container.appendChild(card);
    }
    // Re-set on every render, recreated or not: hass is a new object on every
    // update, and this embedded card only reflects its states when it is.
    this._entitiesCard.hass = this._hass;
  }
}

/* ------------------------------------------------------------- registration */

customElements.define("emhass-plan-card", EmhassPlanCard);
customElements.define("emhass-deferrable-card", EmhassDeferrableCard);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-plan-card",
    name: "EMHASS plan",
    description:
      "The optimisation plan: solar, consumption, grid, battery, prices and scheduled loads.",
    preview: false,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
  {
    type: "emhass-deferrable-card",
    name: "EMHASS deferrable load",
    description: "One deferrable load: when it will run, and its controls.",
    preview: false,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);

console.info(
  "%c EMHASS-COMPANION %c cards loaded ",
  "color:white;background:#03a9f4;font-weight:700",
  "color:#03a9f4;background:white;font-weight:700",
);
