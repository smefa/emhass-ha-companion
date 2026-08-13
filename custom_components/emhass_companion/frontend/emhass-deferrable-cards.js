/**
 * Extracted from the single-bundle emhass-cards.js, which shipped all fourteen
 * elements in one module: when the frontend's own fixed element-registration
 * timeout (home-assistant/frontend#52960) is lost, the whole module fails, so
 * one lost race took down every card at once. Split into a shared core module
 * plus one bundle per card family, a lost race costs one family's elements.
 */

import {
  COLORS,
  CardEditor,
  LiveCard,
  callService,
  cleanSections,
  findLoads,
  formatCountdown,
  formatEnergy,
  formatHours,
  formatPower,
  formatSpan,
  formatTime,
  haptic,
  integrate,
  labelFor,
  loadHaForm,
  loadView,
  moreInfo,
  num,
  pressButton,
  sectionGrid,
  series,
  svg,
  tag,
  trackSvg,
  valueBox,
} from "./emhass-core.js?v=__VERSION__";


/**
 * The same duration read as a deadline: "in 4 h 20 m".
 *
 * A bare duration is what a label like "limit 30 m" wants; a figure standing
 * on its own under the heading "Deadline" wants the preposition, or the
 * reader has to be told separately that it is a wait and not an elapsed time.
 * "overdue" takes neither.
 */
function formatDue(ms) {
  const text = formatCountdown(ms);
  return text === "overdue" || text === "–" ? text : `in ${text}`;
}

function resolveLoad(hass, wanted) {
  const loads = findLoads(hass);
  if (!loads.length) return null;
  if (!wanted) return loads[0];
  const needle = String(wanted).toLowerCase();
  return (
    loads.find((load) => load.name.toLowerCase() === needle || load.id === wanted) || null
  );
}

function toggleEntity(hass, entityId) {
  const domain = entityId.split(".")[0];
  return callService(hass, domain, "toggle", { entity_id: entityId });
}

function setNumber(hass, entityId, value) {
  return callService(hass, "number", "set_value", { entity_id: entityId, value });
}

/* ------------------------------------------------------------- primitives */

function toggleRow(parent, label, sublabel) {
  const row = tag("div", "row", parent);
  const text = tag("div", "label", row);
  tag("span", null, text, label);
  const small = tag("small", null, text, sublabel || "");
  const button = tag("button", "tgl", row);
  button.setAttribute("role", "switch");
  row.setState = (checked, hint) => {
    button.setAttribute("aria-checked", checked ? "true" : "false");
    if (hint !== undefined) small.textContent = hint;
  };
  row.button = button;
  return row;
}

/**
 * A slider row bound to a `number` entity.
 *
 * The live value is only pushed into the input when the user is not touching
 * it. Home Assistant sends a new `hass` object on every state change in the
 * house, and writing the slider on each one would drag the thumb out from
 * under a finger mid-gesture.
 */
function sliderRow(parent, label, format) {
  const row = tag("div", "row", parent);
  const text = tag("div", "label", row);
  tag("span", null, text, label);
  const input = document.createElement("input");
  input.type = "range";
  row.appendChild(input);
  const value = tag("div", "val", row, "–");
  let dragging = false;

  input.addEventListener("pointerdown", () => {
    dragging = true;
  });
  input.addEventListener("input", () => {
    value.textContent = format(Number(input.value));
  });
  const commit = () => {
    dragging = false;
    if (row.onCommit) row.onCommit(Number(input.value));
  };
  input.addEventListener("change", commit);
  input.addEventListener("pointerup", commit);
  input.addEventListener("pointercancel", () => {
    dragging = false;
  });

  row.setState = (stateObj) => {
    if (!stateObj) return;
    const attrs = stateObj.attributes || {};
    input.min = attrs.min !== undefined ? attrs.min : 0;
    input.max = attrs.max !== undefined ? attrs.max : 100;
    input.step = attrs.step !== undefined ? attrs.step : 1;
    if (dragging) return;
    const current = num(stateObj);
    input.value = Number.isFinite(current) ? current : input.min;
    value.textContent = Number.isFinite(current) ? format(current) : "–";
  };
  return row;
}

/**
 * The five states a load can be in, and how each one looks.
 *
 * Colour carries the distinction that matters at a glance and the icon says
 * which of the two green ones it is: **running** is the only state that is
 * changing while you look at it, so it is green with a play icon and the only
 * one that pulses; **run now** is green too, because the plan wants power
 * flowing this minute even if the appliance has not confirmed it. A **planned**
 * run is booked but not now, which is a different kind of fact altogether and
 * takes the accent colour and a calendar rather than a dimmed version of
 * either green -- a dim green would read as "running, weakly".
 */
const STATUS_META = {
  running: { text: "Running", cls: "on", icon: "mdi:play-circle", sq: "on" },
  should: { text: "Run now", cls: "on", icon: "mdi:flash", sq: "on" },
  planned: { text: "Planned", cls: "wait", icon: "mdi:calendar-clock", sq: "wait" },
  idle: { text: "Idle", cls: "", icon: "mdi:sleep", sq: "" },
  unknown: { text: "No plan", cls: "warn", icon: "mdi:help-circle-outline", sq: "" },
};

/**
 * A one-line answer to "why is it doing that".
 *
 * The state pill says what; this says why, which is the question that
 * actually gets asked when a load is not running and the user thinks it
 * should be.
 */
function subtitleFor(view, hass) {
  if (!view.isEnabled) return "Disabled — left out of the plan";
  if ((view.onDemand || view.onSurplus) && !view.isRequested) return "Waiting to be asked";
  if (view.status === "running") {
    return Number.isFinite(view.ranToday) ? `Running · ${formatHours(view.ranToday)} today` : "Running";
  }
  if (view.deadline) return `Due within ${formatCountdown(view.deadline - Date.now())}`;
  // The window rather than the start, when the plan has one: "13:30 → 15:30"
  // answers "will it be done before I need it" as well as "when", and the
  // plan's own series still has it on the runs where next_start does not.
  if (view.nextRun) {
    return `Planned ${formatSpan(view.nextRun.start, view.nextRun.end, hass)}`;
  }
  if (view.next && view.next !== "–") return `Next start ${view.next}`;
  return labelFor(hass, view.recurrence);
}

/** The header block every deferrable card opens with, and its updater. */
function loadHeader(parent, onIcon) {
  const head = tag("div", "head", parent);
  const square = tag("div", "sq", head);
  const icon = document.createElement("ha-icon");
  square.appendChild(icon);
  square.addEventListener("click", onIcon);
  const grow = tag("div", "grow", head);
  const name = tag("div", "name", grow);
  const nameText = tag("span", null, name);
  const slot = tag("span", "chip-id", name, "");
  const sub = tag("div", "sub", grow, "");
  const pill = tag("div", "pill", head);
  tag("span", "dot", pill);
  const pillText = tag("span", null, pill, "");

  head.set = (view, hass) => {
    const meta = STATUS_META[view.status];
    nameText.textContent = view.name;
    slot.textContent =
      view.slot === null || view.slot === undefined ? "" : `P_deferrable${view.slot}`;
    slot.style.display = slot.textContent ? "" : "none";
    sub.textContent = subtitleFor(view, hass);
    icon.setAttribute("icon", meta.icon);
    square.className = `sq ${meta.sq}${view.status === "running" ? " pulsing" : ""}`;
    pill.className = `pill ${meta.cls}`;
    pillText.textContent = meta.text;
  };
  return head;
}

/* -------------------------------------------------------- deferrable card */

/**
 * The parts of the deferrable card, in the order they are drawn.
 *
 * `show_controls` is the one that starts *off*. The card used to end with an
 * embedded entities card -- Run now, Requested, and a slider -- which made a
 * status card into a control panel and cost it half its height on a dashboard
 * of eight loads. The swipe card exists for the houses that want the controls;
 * this one is now a read at a glance, and the switch brings the rows back for
 * anyone who was relying on them.
 */
const DEFERRABLE_SECTIONS = [
  ["show_facts", "Figures", "Scheduled power, next start, runtime and deadline"],
  ["show_timeline", "Timeline", "The load's schedule as one track"],
  ["show_controls", "Controls", "Run now and the request controls, as entity rows"],
];

const DEFERRABLE_OFF_BY_DEFAULT = new Set(["show_controls"]);

function showsDeferrablePart(config, key) {
  const value = config ? config[key] : undefined;
  if (value === undefined) return !DEFERRABLE_OFF_BY_DEFAULT.has(key);
  return value !== false;
}

/** One deferrable load: what the plan has in mind for it, at a glance. */
class EmhassDeferrableCard extends HTMLElement {
  static getStubConfig(hass) {
    const loads = findLoads(hass);
    return {
      type: "custom:emhass-deferrable-card",
      load: (loads[0] && loads[0].name) || "",
    };
  }

  /** The visual editor, with Home Assistant's own form widgets pulled in. */
  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-deferrable-card-editor");
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
    return showsDeferrablePart(this._config, "show_controls") ? 4 : 2;
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
          ha-card { padding: 12px 16px 16px 16px; }
          .head { display:flex; justify-content:space-between; align-items:center; }
          .name { font-size:1.1em; font-weight:500; }
          .name .slot { font-size:.75em; font-weight:400; color: var(--secondary-text-color);
                        margin-left:6px; }
          .state { font-size:.9em; padding:2px 8px; border-radius:10px; }
          .on  { background: var(--success-color, #43a047); color: white; }
          .planned { background: var(--primary-color, #03a9f4); color: white; }
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

    const load = resolveLoad(hass, this._config.load);
    const name = this.shadowRoot.querySelector(".name");
    const state = this.shadowRoot.querySelector(".state");
    const facts = this.shadowRoot.querySelector(".facts");
    const chart = this.shadowRoot.querySelector(".chart");

    name.textContent = "";
    if (!load) {
      name.textContent = "No deferrable load";
      state.textContent = "";
      facts.textContent = "";
      const empty = document.createElement("span");
      empty.className = "empty";
      empty.textContent = "Add one under the EMHASS Companion integration.";
      facts.appendChild(empty);
      chart.textContent = "";
      this.shadowRoot.querySelector(".controls").textContent = "";
      return;
    }

    const view = loadView(hass, load);
    const budget = view.onSurplus ? view.find("surplus_budget") : null;

    // EMHASS calls its loads P_deferrable0, P_deferrable1, ... and never says
    // which appliance a number is. Showing it here is what lets someone read
    // EMHASS's own charts and logs against this dashboard. It is read live
    // rather than stored, because the number shifts as loads join and leave
    // the optimisation.
    name.textContent = view.name;
    if (view.slot !== null && view.slot !== undefined) {
      const chip = document.createElement("span");
      chip.className = "slot";
      chip.textContent = `P_deferrable${view.slot}`;
      name.appendChild(chip);
    }

    // Unknown is shown distinctly from off: with no usable plan the correct
    // answer is "we do not know", and rendering that as "off" would quietly
    // suggest the load should stay idle. Planned is distinct from idle for the
    // same reason -- "nothing now" and "nothing booked" are different facts.
    const meta = STATUS_META[view.status];
    const badge = { running: "on", should: "on", planned: "planned", idle: "off" };
    state.className = `state ${badge[view.status] || "unknown"}`;
    state.textContent = meta.text;

    facts.textContent = "";
    if (showsDeferrablePart(this._config, "show_facts")) {
      const figures = [
        ["Scheduled", formatPower(view.scheduledW)],
        ["Next start", view.next],
      ];
      // Shown only while a request is actually pending: a deadline is a
      // property of one request, so an empty slot the rest of the time would
      // be noise on every daily load.
      if (view.deadline) figures.push(["Deadline", formatDue(view.deadline - Date.now())]);
      // A surplus load asks for no fixed run time, so "hours needed" would be
      // meaningless; what it actually got from the last plan is the number
      // worth showing, and the only visible sign of why it is or is not
      // running today.
      if (budget && Number.isFinite(num(budget))) {
        figures.push(["Spare solar", `${num(budget).toFixed(1)} h`]);
      }
      figures.push([
        "Ran today",
        Number.isFinite(view.ranToday) ? `${view.ranToday.toFixed(1)} h` : "–",
      ]);
      for (const [label, text] of figures) {
        const cell = document.createElement("div");
        cell.appendChild(document.createTextNode(label));
        const value = document.createElement("b");
        value.textContent = text;
        cell.appendChild(value);
        facts.appendChild(cell);
      }
    }

    chart.textContent = "";
    if (showsDeferrablePart(this._config, "show_timeline") && view.points.length > 1) {
      chart.appendChild(this._timeline(view.points, view.deadline));
    }

    this._controls(load, view.onDemand, view.onSurplus);
  }

  _timeline(points, deadline) {
    const width = 400;
    const height = 26;
    const root = svg("svg", {
      viewBox: `0 0 ${width} ${height}`,
      preserveAspectRatio: "none",
    });
    const t0 = points[0].t;
    const t1 = points[points.length - 1].t;
    const x = (t) => ((t - t0) / (t1 - t0 || 1)) * width;

    svg("rect", { x: 0, y: 6, width, height: 12, fill: COLORS.grid, rx: 3 }, root);

    for (let i = 0; i < points.length - 1; i++) {
      if (points[i].v <= 0) continue;
      svg("rect", {
        x: x(points[i].t), y: 6,
        width: Math.max(x(points[i + 1].t) - x(points[i].t), 1), height: 12,
        fill: COLORS.pv, rx: 2,
      }, root);
    }

    const now = Date.now();
    if (now >= t0 && now <= t1) {
      svg("line", {
        x1: x(now), x2: x(now), y1: 2, y2: 22,
        stroke: COLORS.load, "stroke-width": 2,
      }, root);
    }

    // The one question a deadline raises is whether the plan actually finishes
    // the load before it, which is exactly what this line answers at a glance.
    // Dashed so it reads as a constraint rather than as scheduled power.
    if (deadline !== null && deadline >= t0 && deadline <= t1) {
      svg("line", {
        x1: x(deadline), x2: x(deadline), y1: 0, y2: 24,
        stroke: COLORS.deadline, "stroke-width": 2, "stroke-dasharray": "3 2",
      }, root);
    }
    return root;
  }

  _controls(load, onDemand, onSurplus) {
    const container = this.shadowRoot.querySelector(".controls");
    if (!showsDeferrablePart(this._config, "show_controls")) {
      container.textContent = "";
      this._controlsKey = null;
      this._entitiesCard = null;
      return;
    }
    // The request controls are meaningless on a daily load -- its entities
    // report unavailable -- so they are left out rather than shown greyed.
    // A surplus load has no deadline to set; what it takes instead is an
    // optional energy cap.
    //
    // `load_enabled` is deliberately not here. Turning it off takes the load
    // out of the optimisation entirely, which is a setup decision rather than
    // a daily one, and a switch that far-reaching does not belong under a
    // thumb on a card that gets tapped every day. The header still reads
    // "Disabled" while it is off, and the switch is one tap away on the
    // load's own device page.
    let wanted = ["run_now"];
    if (onDemand) {
      wanted = ["run_now", "load_requested", "run_within"];
    } else if (onSurplus) {
      wanted = ["run_now", "load_requested", "energy_needed"];
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

/* ------------------------------------------------------ load info boxes */

/**
 * What one of a deferrable card's info boxes can be set to show.
 *
 * Everything here is read out of the `loadView` the card already built, so a
 * box costs nothing but its own line. Each `read` returns `{v, s}`: the value,
 * and a second line saying what the value is measured against -- a bare "2.4 h"
 * under "Planned" is a number nobody can act on, "2 runs" beside it is.
 *
 * `entity` names the load entity the box opens when tapped, by translation
 * key. A box with no obvious entity behind it is simply not tappable rather
 * than opening something loosely related, which would be worse than nothing.
 *
 * The keys are what ends up in the YAML, so they are named for the quantity
 * rather than for the entity that happens to carry it.
 */
const LOAD_METRICS = {
  none: { label: "Nothing", read: () => ({}) },
  scheduled: {
    label: "Scheduled power",
    entity: "scheduled_power",
    read: (v) => ({
      v: formatPower(v.scheduledW),
      s: v.status === "running" ? "running now" : "",
    }),
  },
  next_start: {
    label: "Next start",
    entity: "next_start",
    read: (v) => ({
      v: v.next,
      s: v.nextRun ? `for ${formatHours((v.nextRun.end - v.nextRun.start) / 3600000)}` : "",
    }),
  },
  ran_today: {
    label: "Ran today",
    entity: "runtime_today",
    read: (v) => {
      if (!Number.isFinite(v.ranToday)) return { v: "–" };
      return {
        v: formatHours(v.ranToday),
        s: Number.isFinite(v.needed) ? `of ${formatHours(v.needed)}` : "",
      };
    },
  },
  // What box four has always shown: the deadline while a request is pending,
  // and the load's mode the rest of the time. Kept as one metric rather than
  // split in two, because a deadline is a property of one request and a box
  // that is empty on every daily load is a column of nothing.
  recurrence: {
    label: "Deadline or mode",
    entity: "recurrence",
    read: (v, hass) => {
      if (v.deadline) return { v: formatCountdown(v.deadline - Date.now()), s: "until deadline" };
      return { v: labelFor(hass, v.recurrence), s: "" };
    },
  },
  deadline: {
    label: "Deadline",
    entity: "load_requested",
    read: (v, hass) => {
      if (!v.deadline) return { v: "–", s: "nothing requested" };
      return { v: formatCountdown(v.deadline - Date.now()), s: formatTime(v.deadline, hass) };
    },
  },
  mode: {
    label: "Mode",
    entity: "recurrence",
    read: (v, hass) => ({ v: labelFor(hass, v.recurrence), s: "" }),
  },
  planned: {
    label: "Planned run time",
    entity: "scheduled_power",
    read: (v) => {
      let hours = 0;
      for (const run of v.runs) hours += (run.end - run.start) / 3600000;
      if (!v.runs.length) return { v: "–", s: "not in the plan" };
      return { v: formatHours(hours), s: v.runs.length === 1 ? "1 run" : `${v.runs.length} runs` };
    },
  },
  planned_energy: {
    label: "Planned energy",
    entity: "scheduled_power",
    read: (v) => {
      if (v.points.length < 2) return { v: "–" };
      const last = v.points[v.points.length - 1];
      const total = integrate(v.points, last.t);
      return { v: formatEnergy(total.up), s: total.peak ? `peak ${formatPower(total.peak)}` : "" };
    },
  },
  finishes: {
    label: "Plan finishes",
    entity: "scheduled_power",
    read: (v, hass) => {
      if (!v.runs.length) return { v: "–", s: "nothing scheduled" };
      const end = v.runs[v.runs.length - 1].end;
      return { v: formatTime(end, hass), s: formatDue(end - Date.now()) };
    },
  },
  remaining: {
    label: "Still to run",
    entity: "operating_hours",
    read: (v) => {
      if (!Number.isFinite(v.needed)) return { v: "–" };
      const left = v.needed - (Number.isFinite(v.ranToday) ? v.ranToday : 0);
      return { v: formatHours(Math.max(left, 0)), s: `of ${formatHours(v.needed)} needed` };
    },
  },
  state: {
    label: "State",
    entity: "should_run",
    read: (v) => ({ v: STATUS_META[v.status].text, s: v.isEnabled ? "" : "disabled" }),
  },
  slot: {
    label: "EMHASS slot",
    entity: "should_run",
    read: (v) => ({
      v: v.slot === null || v.slot === undefined ? "–" : `P_deferrable${v.slot}`,
      s: v.reason ? String(v.reason).replace(/_/g, " ") : "",
    }),
  },
  requested: {
    label: "Requested",
    entity: "load_requested",
    read: (v) => {
      if (!v.onDemand && !v.onSurplus) return { v: "–", s: "not on request" };
      return { v: v.isRequested ? "Yes" : "No", s: v.isRequested ? "" : "waiting to be asked" };
    },
  },
  enabled: {
    label: "Enabled",
    entity: "load_enabled",
    read: (v) => ({
      v: v.isEnabled ? "Yes" : "No",
      s: v.isEnabled ? "in the plan" : "left out of the plan",
    }),
  },
  power: {
    label: "Nominal power",
    entity: "nominal_power",
    read: (v) => ({ v: formatPower(num(v.find("nominal_power"))), s: "" }),
  },
  needed: {
    label: "Hours needed",
    entity: "operating_hours",
    read: (v) => ({ v: formatHours(num(v.find("operating_hours"))), s: "each day" }),
  },
  surplus_budget: {
    label: "Spare solar",
    entity: "surplus_budget",
    read: (v) => {
      const budget = num(v.find("surplus_budget"));
      if (!Number.isFinite(budget)) return { v: "–", s: "not a surplus load" };
      return { v: formatHours(budget), s: "of spare solar" };
    },
  },
  energy_needed: {
    label: "Energy cap",
    entity: "energy_needed",
    read: (v) => {
      const cap = num(v.find("energy_needed"));
      return { v: Number.isFinite(cap) ? formatEnergy(cap) : "–", s: "" };
    },
  },
};

/** The dropdown's order, which is the order they are worth reaching for. */
const LOAD_METRIC_ORDER = [
  "none",
  "scheduled",
  "next_start",
  "ran_today",
  "recurrence",
  "deadline",
  "mode",
  "planned",
  "planned_energy",
  "finishes",
  "remaining",
  "state",
  "slot",
  "requested",
  "enabled",
  "power",
  "needed",
  "surplus_budget",
  "energy_needed",
];

// Boxes one to four are what the card has always shown, so an existing card is
// unchanged by the two new ones. Five and six default to something rather than
// to "Nothing" -- having them was the whole reason to add them -- and both are
// read off the plan's own schedule, which nothing else on the front page says.
const LOAD_BOX_DEFAULTS = ["scheduled", "next_start", "ran_today", "recurrence", "planned", "finishes"];

function loadMetric(config, index) {
  const wanted = config ? config[`box_${index}`] : undefined;
  return wanted && LOAD_METRICS[wanted] ? wanted : LOAD_BOX_DEFAULTS[index - 1];
}

/* ------------------------------------------------- variant 1: swipe deck */

/**
 * One load as three swipeable faces: control, schedule and details.
 *
 * Built on CSS scroll snapping rather than a carousel library: the browser
 * does the physics, the gesture is the one a phone user already expects, and
 * on a desktop the dots still work as buttons. It costs one scroll listener.
 */
class EmhassDeferrableSwipeCard extends LiveCard {
  static getStubConfig(hass) {
    const loads = findLoads(hass);
    return {
      type: "custom:emhass-deferrable-swipe-card",
      load: loads.length ? loads[0].name : "",
    };
  }

  /** The visual editor, with Home Assistant's own form widgets pulled in. */
  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-deferrable-swipe-card-editor");
  }

  getCardSize() {
    return 5;
  }

  build(card) {
    const ui = {};
    this._ui = ui;
    const pad = tag("div", "pad", card);
    ui.head = loadHeader(pad, () => moreInfo(this, this._entity("should_run")));

    ui.deck = tag("div", "deck", card);
    ui.pages = [0, 1, 2].map(() => tag("div", "page", ui.deck));

    /* page 1: the numbers */
    ui.stats = tag("div", "stats", ui.pages[0]);
    // A box set to "Nothing" is not built at all rather than left empty: the
    // row is an auto-fit grid, and an empty cell would still take a column.
    ui.boxes = LOAD_BOX_DEFAULTS.map((_default, offset) => {
      const metric = loadMetric(this._config, offset + 1);
      if (metric === "none") return null;
      const spec = LOAD_METRICS[metric];
      const box = valueBox(ui.stats, spec.label);
      box.metric = metric;
      if (spec.entity) {
        box.classList.add("tap");
        box.addEventListener("click", () => moreInfo(this, this._entity(spec.entity)));
      }
      return box;
    });
    if (!ui.stats.childElementCount) ui.stats.style.display = "none";
    ui.track = tag("div", "track", ui.pages[0]);

    /* page 2: the controls */
    ui.requestRow = toggleRow(ui.pages[1], "Requested", "Ask for a run");
    ui.requestRow.button.addEventListener("click", () => this._toggle("load_requested"));
    ui.withinRow = sliderRow(ui.pages[1], "Run within", (value) => `${value} h`);
    ui.withinRow.onCommit = (value) => this._setNumber("run_within", value);
    ui.energyRow = sliderRow(ui.pages[1], "Energy needed", (value) => `${value} kWh`);
    ui.energyRow.onCommit = (value) => this._setNumber("energy_needed", value);
    ui.runButton = tag("button", "btn primary wide", ui.pages[1], "Run now");
    ui.runButton.style.marginTop = "10px";
    ui.runButton.addEventListener("click", () => {
      const entityId = this._entity("run_now");
      if (entityId) {
        haptic("medium");
        pressButton(this._hass, entityId);
      }
    });

    /* page 3: the plan */
    ui.bigTrack = tag("div", "big-track", ui.pages[2]);
    ui.runList = tag("div", "runs", ui.pages[2]);

    ui.dots = tag("div", "dots", card);
    ui.dotList = ["Now", "Control", "Plan"].map((label, index) => {
      const dot = tag("button", "dot", ui.dots);
      dot.title = label;
      dot.addEventListener("click", () => {
        ui.deck.scrollTo({ left: index * ui.deck.clientWidth, behavior: "smooth" });
      });
      return dot;
    });
    ui.deck.addEventListener("scroll", () => {
      const index = Math.round(ui.deck.scrollLeft / (ui.deck.clientWidth || 1));
      ui.dotList.forEach((dot, i) => {
        if (i === index) dot.setAttribute("data-active", "");
        else dot.removeAttribute("data-active");
      });
    });
    ui.dotList[0].setAttribute("data-active", "");
  }

  _entity(key) {
    const load = resolveLoad(this._hass, this._config.load);
    return load ? load.entities[key] : null;
  }

  _toggle(key) {
    const entityId = this._entity(key);
    if (entityId) {
      haptic("light");
      toggleEntity(this._hass, entityId);
    }
  }

  _setNumber(key, value) {
    const entityId = this._entity(key);
    if (entityId) setNumber(this._hass, entityId, value);
  }

  update(hass) {
    const ui = this._ui;
    const load = resolveLoad(hass, this._config.load);
    if (!load) {
      ui.head.set({ name: "No deferrable load", slot: null, status: "unknown", isEnabled: true }, hass);
      return;
    }
    const view = loadView(hass, load);
    ui.head.set(view, hass);

    for (const box of ui.boxes) {
      if (!box) continue;
      const shown = LOAD_METRICS[box.metric].read(view, hass);
      box.set(shown.v, shown.s);
    }

    for (const [node, height] of [[ui.track, 22], [ui.bigTrack, 34]]) {
      node.textContent = "";
      if (view.points.length > 1) {
        node.appendChild(
          trackSvg({
            points: view.points,
            deadline: view.deadline,
            height,
            labels: height > 24,
            color: "var(--emh-solar)",
            hass,
          }),
        );
      }
    }

    const asks = view.onDemand || view.onSurplus;
    ui.requestRow.style.display = asks ? "" : "none";
    ui.requestRow.setState(view.isRequested);
    ui.withinRow.style.display = view.onDemand ? "" : "none";
    ui.withinRow.setState(view.find("run_within"));
    ui.energyRow.style.display = view.onSurplus ? "" : "none";
    ui.energyRow.setState(view.find("energy_needed"));

    ui.runList.textContent = "";
    if (!view.runs.length) tag("div", "empty", ui.runList, "Nothing scheduled in this plan.");
    for (const run of view.runs) {
      const row = tag("div", "run", ui.runList);
      tag("span", "when", row, `${formatTime(run.start, hass)} – ${formatTime(run.end, hass)}`);
      tag("span", "watts", row, formatPower(run.watts));
    }
  }
}

EmhassDeferrableSwipeCard.ticks = true;
EmhassDeferrableSwipeCard.css = `
  .pad { padding-bottom: 4px; }
  .deck { display: flex; overflow-x: auto; scroll-snap-type: x mandatory;
          scrollbar-width: none; -webkit-overflow-scrolling: touch; }
  .deck::-webkit-scrollbar { display: none; }
  .page { flex: 0 0 100%; scroll-snap-align: start; padding: 4px 16px 8px 16px;
          min-width: 0; }
  .stats { margin-top: 4px; }
  .track { margin-top: 12px; }
  .big-track { margin-top: 6px; margin-bottom: 8px; }
  .run { display: flex; justify-content: space-between; font-size: .84rem;
         padding: 5px 0; }
  .run + .run { border-top: 1px solid var(--emh-hairline); }
  .run .watts { color: var(--emh-solar); font-weight: 500; }
  .dots { display: flex; justify-content: center; gap: 6px; padding: 6px 0 12px 0; }
  .dots .dot { width: 6px; height: 6px; border-radius: 999px; border: 0; padding: 0;
               background: var(--emh-surface-2); cursor: pointer;
               transition: width 260ms var(--emh-ease), background 200ms; }
  .dots .dot[data-active] { width: 18px; background: var(--emh-accent); }
`;

/* ------------------------------------------------- variant 2: strip card */

/**
 * One load in the height of a tile, with the whole day behind it.
 *
 * The day is drawn as fixed-width buckets rather than proportional blocks --
 * the trick uptime cards use -- so a load that switches twenty times and one
 * that switches twice have the same visual density, and the strip stays
 * readable at 6 px per bucket. Controls are behind a chevron, so a dashboard
 * of eight loads stays a list rather than eight control panels.
 */
class EmhassDeferrableStripCard extends LiveCard {
  static getStubConfig(hass) {
    const loads = findLoads(hass);
    return {
      type: "custom:emhass-deferrable-strip-card",
      load: loads.length ? loads[0].name : "",
    };
  }

  /** The visual editor, with Home Assistant's own form widgets pulled in. */
  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-deferrable-strip-card-editor");
  }

  getCardSize() {
    return 2;
  }

  build(card) {
    const ui = {};
    this._ui = ui;
    this._expanded = false;
    const pad = tag("div", "pad", card);

    const row = tag("div", "strip", pad);
    ui.square = tag("div", "sq", row);
    ui.icon = document.createElement("ha-icon");
    ui.square.appendChild(ui.icon);
    ui.square.addEventListener("click", () => moreInfo(this, this._entity("should_run")));

    const middle = tag("div", "mid", row);
    const line = tag("div", "line", middle);
    ui.name = tag("span", "nm", line, "");
    ui.state = tag("span", "st", line, "");
    ui.buckets = tag("div", "buckets", middle);
    ui.sub = tag("div", "sub", middle, "");

    // Bubble-card's sub-buttons: the two actions worth taking without opening
    // anything, pinned to the trailing edge.
    const side = tag("div", "side", row);
    ui.runButton = tag("button", "mini", side);
    const runIcon = document.createElement("ha-icon");
    runIcon.setAttribute("icon", "mdi:flash");
    ui.runButton.appendChild(runIcon);
    ui.runButton.addEventListener("click", () => {
      const entityId = this._entity("run_now");
      if (entityId) {
        haptic("medium");
        pressButton(this._hass, entityId);
      }
    });
    ui.chevron = tag("button", "mini", side);
    ui.chevronIcon = document.createElement("ha-icon");
    ui.chevronIcon.setAttribute("icon", "mdi:chevron-down");
    ui.chevron.appendChild(ui.chevronIcon);
    ui.chevron.addEventListener("click", () => {
      this._expanded = !this._expanded;
      haptic("light");
      this._syncDrawer();
    });

    ui.drawer = tag("div", "drawer", pad);
    ui.drawerBody = tag("div", "drawer-body", ui.drawer);
    ui.requestRow = toggleRow(ui.drawerBody, "Requested", "Ask for a run");
    ui.requestRow.button.addEventListener("click", () => this._toggle("load_requested"));
    ui.withinRow = sliderRow(ui.drawerBody, "Run within", (value) => `${value} h`);
    ui.withinRow.onCommit = (value) => {
      const entityId = this._entity("run_within");
      if (entityId) setNumber(this._hass, entityId, value);
    };
  }

  _syncDrawer() {
    const ui = this._ui;
    ui.drawer.style.height = this._expanded ? `${ui.drawerBody.offsetHeight}px` : "0px";
    ui.chevronIcon.setAttribute("icon", this._expanded ? "mdi:chevron-up" : "mdi:chevron-down");
  }

  _entity(key) {
    const load = resolveLoad(this._hass, this._config.load);
    return load ? load.entities[key] : null;
  }

  _toggle(key) {
    const entityId = this._entity(key);
    if (entityId) toggleEntity(this._hass, entityId);
  }

  update(hass) {
    const ui = this._ui;
    const load = resolveLoad(hass, this._config.load);
    if (!load) {
      ui.name.textContent = "No deferrable load";
      return;
    }
    const view = loadView(hass, load);
    const meta = STATUS_META[view.status];

    ui.name.textContent = view.name;
    ui.state.textContent = meta.text;
    ui.state.className = `st ${meta.cls}`;
    ui.icon.setAttribute("icon", meta.icon);
    ui.square.className = `sq ${meta.sq}${view.status === "running" ? " pulsing" : ""}`;
    ui.sub.textContent = subtitleFor(view, hass);

    this._buckets(view);

    ui.runButton.style.display = this._entity("run_now") ? "" : "none";
    const asks = view.onDemand || view.onSurplus;
    ui.requestRow.style.display = asks ? "" : "none";
    ui.requestRow.setState(view.isRequested);
    ui.withinRow.style.display = view.onDemand ? "" : "none";
    ui.withinRow.setState(view.find("run_within"));
    if (this._expanded) this._syncDrawer();
  }

  /** 48 half-hour buckets across the plan's own horizon. */
  _buckets(view) {
    const ui = this._ui;
    const count = 48;
    ui.buckets.textContent = "";
    if (view.points.length < 2) return;
    const t0 = view.points[0].t;
    const t1 = view.points[view.points.length - 1].t;
    const span = (t1 - t0) / count;
    const now = Date.now();

    for (let i = 0; i < count; i++) {
      const from = t0 + i * span;
      const to = from + span;
      let on = false;
      for (const run of view.runs) {
        if (run.start < to && run.end > from) on = true;
      }
      const cell = tag("i", "b", ui.buckets);
      if (on) cell.setAttribute("data-on", "");
      if (now >= from && now < to) cell.setAttribute("data-now", "");
      if (view.deadline && view.deadline >= from && view.deadline < to) {
        cell.setAttribute("data-deadline", "");
      }
    }
  }
}

EmhassDeferrableStripCard.ticks = true;
EmhassDeferrableStripCard.css = `
  .pad { padding: 12px 14px; }
  .strip { display: flex; align-items: center; gap: 12px; }
  .mid { flex: 1; min-width: 0; }
  .line { display: flex; align-items: baseline; gap: 8px; }
  .nm { font-size: .98rem; font-weight: 500; flex: 1; min-width: 0;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .st { font-size: .74rem; color: var(--emh-dim); flex: 0 0 auto; }
  .st.on { color: var(--emh-ok); }
  .st.warn { color: var(--emh-warn); }
  .buckets { display: flex; gap: 1px; margin: 6px 0 4px 0; height: 14px; }
  .buckets .b { flex: 1; border-radius: 1px; background: var(--emh-surface-2);
                transition: background 300ms; }
  .buckets .b[data-on] { background: var(--emh-solar); }
  .buckets .b[data-now] { background: var(--primary-text-color); }
  .buckets .b[data-deadline] { background: var(--emh-warn); }
  .sub { font-size: .72rem; }
  .side { display: flex; flex-direction: column; gap: 4px; flex: 0 0 auto; }
  .mini { width: 30px; height: 30px; border-radius: 50%; border: 0; padding: 0;
          display: grid; place-items: center; cursor: pointer;
          background: var(--emh-surface); color: var(--emh-dim);
          transition: transform 120ms var(--emh-ease), background 200ms; }
  .mini:active { transform: scale(.9); }
  .mini ha-icon { --mdc-icon-size: 17px; }
  .drawer { height: 0; overflow: hidden; transition: height 300ms var(--emh-ease); }
  .drawer-body { padding-top: 6px; }
`;

/**
 * The "which load" field, as a dropdown of the loads that exist.
 *
 * Typed free text is still accepted, because a dashboard is routinely written
 * before the load it names is added -- and a picker that silently discards a
 * name it does not recognise is worse than a text box.
 */
function loadField(hass) {
  const options = findLoads(hass).map((load) => ({ value: load.name, label: load.name }));
  return {
    name: "load",
    selector: options.length
      ? { select: { mode: "dropdown", custom_value: true, options } }
      : { text: {} },
  };
}

/* -------------------------------------------------- deferrable card editor */

const DEFERRABLE_LABELS = { load: "Load" };
const DEFERRABLE_HELPERS = {
  load: "Which deferrable load this card is about. Left empty, it shows the first one.",
};
for (const section of DEFERRABLE_SECTIONS) {
  DEFERRABLE_LABELS[section[0]] = section[1];
  DEFERRABLE_HELPERS[section[0]] = section[2];
}

/** Which load, and which parts of the deferrable card to draw. */
class EmhassDeferrableCardEditor extends CardEditor {
  get labels() {
    return DEFERRABLE_LABELS;
  }

  get helpers() {
    return DEFERRABLE_HELPERS;
  }

  schema(hass) {
    return [loadField(hass), sectionGrid(DEFERRABLE_SECTIONS)];
  }

  data() {
    const data = { load: this._config.load || "" };
    for (const section of DEFERRABLE_SECTIONS) {
      data[section[0]] = showsDeferrablePart(this._config, section[0]);
    }
    return data;
  }

  clean(config) {
    cleanSections(config, DEFERRABLE_SECTIONS, DEFERRABLE_OFF_BY_DEFAULT);
    if (!config.load) delete config.load;
    return config;
  }
}

/* ------------------------------------------------------- swipe card editor */

const SWIPE_LABELS = { load: "Load" };
const SWIPE_HELPERS = {
  load: "Which deferrable load this card is about. Left empty, it shows the first one.",
};
for (let i = 1; i <= LOAD_BOX_DEFAULTS.length; i++) SWIPE_LABELS[`box_${i}`] = `Box ${i}`;

const LOAD_METRIC_OPTIONS = LOAD_METRIC_ORDER.map((key) => ({
  value: key,
  label: LOAD_METRICS[key].label,
}));

/** Which load, and what its six info boxes carry. */
class EmhassDeferrableSwipeCardEditor extends CardEditor {
  get labels() {
    return SWIPE_LABELS;
  }

  get helpers() {
    return SWIPE_HELPERS;
  }

  schema(hass) {
    return [
      loadField(hass),
      {
        name: "",
        type: "expandable",
        title: "Info boxes",
        icon: "mdi:view-grid-outline",
        expanded: true,
        schema: [
          {
            name: "",
            type: "grid",
            column_min_width: "220px",
            schema: LOAD_BOX_DEFAULTS.map((_default, index) => ({
              name: `box_${index + 1}`,
              selector: { select: { mode: "dropdown", options: LOAD_METRIC_OPTIONS } },
            })),
          },
        ],
      },
    ];
  }

  data() {
    const data = { load: this._config.load || "" };
    for (let i = 1; i <= LOAD_BOX_DEFAULTS.length; i++) data[`box_${i}`] = loadMetric(this._config, i);
    return data;
  }

  clean(config) {
    for (let i = 1; i <= LOAD_BOX_DEFAULTS.length; i++) {
      if (config[`box_${i}`] === LOAD_BOX_DEFAULTS[i - 1]) delete config[`box_${i}`];
    }
    if (!config.load) delete config.load;
    return config;
  }
}

/* ------------------------------------------------------- strip card editor */

/** Which load the strip is about. It has nothing else to configure. */
class EmhassDeferrableStripCardEditor extends CardEditor {
  get labels() {
    return DEFERRABLE_LABELS;
  }

  get helpers() {
    return DEFERRABLE_HELPERS;
  }

  schema(hass) {
    return [loadField(hass)];
  }

  data() {
    return { load: this._config.load || "" };
  }

  clean(config) {
    if (!config.load) delete config.load;
    return config;
  }
}

customElements.define("emhass-deferrable-card", EmhassDeferrableCard);
customElements.define("emhass-deferrable-card-editor", EmhassDeferrableCardEditor);
customElements.define("emhass-deferrable-swipe-card", EmhassDeferrableSwipeCard);
customElements.define("emhass-deferrable-swipe-card-editor", EmhassDeferrableSwipeCardEditor);
customElements.define("emhass-deferrable-strip-card", EmhassDeferrableStripCard);
customElements.define("emhass-deferrable-strip-card-editor", EmhassDeferrableStripCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-deferrable-card",
    name: "EMHASS Companion deferrable load",
    description: "One deferrable load: whether it should run, and when the plan has it running.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
  {
    type: "emhass-deferrable-swipe-card",
    name: "EMHASS Companion load (swipe)",
    description: "One load, three swipeable pages: now, control, plan.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
  {
    type: "emhass-deferrable-strip-card",
    name: "EMHASS Companion load (strip)",
    description: "One load in a single compact row, with the whole day behind it.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);
