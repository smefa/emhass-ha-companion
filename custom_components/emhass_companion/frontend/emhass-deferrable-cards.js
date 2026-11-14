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
 * How far today's run has got, as `{v, s}` for a box or a figure.
 *
 * A sequence load reads its progress from the cycle, not from measured
 * runtime: a 0 W or low stage measures as not running, so the measured hours
 * of a finished wash still fall short of what it needed.
 */
function progressOf(view) {
  const cycle = view.sequence;
  if (cycle && cycle.step) return { v: `Step ${cycle.step}`, s: `of ${cycle.steps}` };
  if (cycle && cycle.doneToday) return { v: "Done", s: "cycle finished today" };
  if (!Number.isFinite(view.ranToday)) return { v: "–" };
  return {
    v: formatHours(view.ranToday),
    s: Number.isFinite(view.needed) ? `of ${formatHours(view.needed)}` : "",
  };
}

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
function sliderRow(parent, label, format, step) {
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

  // `floor` lets a caller raise the minimum past the entity's own `min`
  // attribute -- e.g. "Run within" should never go below the hours the load
  // still needs, even though the number entity itself allows a lower value.
  // `step` overrides the entity's own step; min and max are then pulled onto
  // that grid, since a range input counts its steps from `min`.
  row.setState = (stateObj, floor) => {
    if (!stateObj) return;
    const attrs = stateObj.attributes || {};
    const attrMin = attrs.min !== undefined ? attrs.min : 0;
    let min = Number.isFinite(floor) ? Math.max(attrMin, floor) : attrMin;
    let max = attrs.max !== undefined ? attrs.max : 100;
    if (step) {
      min = Math.ceil(min / step) * step;
      max = Math.max(min, Math.floor(max / step) * step);
    }
    input.min = min;
    input.max = max;
    input.step = step || (attrs.step !== undefined ? attrs.step : 1);
    if (dragging) return;
    const current = num(stateObj);
    // Setting a value below input.min clamps it to min, so re-read it.
    input.value = Number.isFinite(current) ? current : input.min;
    value.textContent = Number.isFinite(current) ? format(Number(input.value)) : "–";
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
  const cycle = view.sequence;
  // Before "waiting to be asked": Run now starts a cycle with nothing requested.
  if (cycle && cycle.step) return `Running · step ${cycle.step} of ${cycle.steps}`;
  if ((view.onDemand || view.onSurplus) && !view.isRequested) {
    return cycle && cycle.doneToday ? "Cycle done today" : "Waiting to be asked";
  }
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
          :host { display: block; max-width: var(--emh-max-width, 960px); margin-inline: auto; }
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
      const cycle = view.sequence;
      figures.push(
        cycle && cycle.step
          ? ["Cycle", `step ${cycle.step} of ${cycle.steps}`]
          : cycle && cycle.doneToday
            ? ["Cycle", "done today"]
            : ["Ran today", Number.isFinite(view.ranToday) ? `${view.ranToday.toFixed(1)} h` : "–"],
      );
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
    read: (v) => progressOf(v),
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
      const cycle = v.sequence;
      if (cycle && cycle.doneToday) return { v: formatHours(0), s: "cycle finished today" };
      // The current step counts as still to run, to the step.
      const left =
        cycle && cycle.step
          ? (v.needed * (cycle.steps - cycle.step + 1)) / cycle.steps
          : v.needed - (Number.isFinite(v.ranToday) ? v.ranToday : 0);
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
    // A sequence leaves the number unavailable; its peak is what the box shows.
    read: (v) =>
      v.sequence && v.sequence.values.length
        ? { v: formatPower(v.sequence.peak), s: `sequence, ${v.sequence.values.length} steps` }
        : { v: formatPower(num(v.find("nominal_power"))), s: "" },
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
    ui.withinRow = sliderRow(ui.pages[1], "Run within", (value) => `${value} h`, 0.5);
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
    ui.withinRow.setState(view.find("run_within"), num(view.find("operating_hours")));
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
const STRIP_CONTROLS = [
  ["show_requested", "Requested", "Show the request toggle for on-demand and surplus loads."],
  ["show_run_now", "Run now", "Show the manual start button."],
  ["show_run_within", "Run within", "Show the deadline control for on-demand loads."],
  ["show_hours_needed", "Hours needed", "Show the operating-hours control. Off by default."],
];
const STRIP_OFF_BY_DEFAULT = new Set(["show_hours_needed"]);

function showsStripControl(config, key) {
  return config[key] === undefined ? !STRIP_OFF_BY_DEFAULT.has(key) : config[key] !== false;
}

/** A compact number button; its slider lives in the top layer, outside the drawer clip. */
function stripNumberControl(parent, label, onCommit) {
  const button = tag("button", "control number-control", parent);
  button.title = label;
  tag("span", "control-label", button, label);
  const short = tag("span", "control-short", button, label === "Run within" ? "Within" : "Hours");
  short.setAttribute("aria-hidden", "true");
  const value = tag("span", "control-value", button);
  const caret = tag("ha-icon", "control-caret", button);
  caret.setAttribute("icon", "mdi:chevron-down");
  caret.setAttribute("aria-hidden", "true");
  const dialog = tag("dialog", "number-dialog", parent);
  const heading = tag("div", "number-heading", dialog, label);
  dialog.setAttribute("aria-label", label);
  const close = tag("button", "mini", heading, "\u00d7");
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", () => dialog.close());
  const input = tag("input", null, dialog);
  input.type = "range";
  input.setAttribute("aria-label", label);
  const output = tag("output", null, dialog);
  button.setAttribute("aria-haspopup", "dialog");
  button.addEventListener("click", () => dialog.showModal());
  input.addEventListener("input", () => {
    output.textContent = value.textContent = formatHours(Number(input.value));
    input.setAttribute("aria-valuetext", output.textContent);
    button.setAttribute("aria-label", `${label}: ${output.textContent}`);
  });
  input.addEventListener("change", () => onCommit(Number(input.value)));
  button.setState = (state, floor) => {
    button.disabled = !state || !Number.isFinite(num(state));
    if (button.disabled && dialog.open) dialog.close();
    // A live HA update must not reset a slider while the user edits it.
    if (dialog.open) return;
    const attrs = (state && state.attributes) || {};
    const step = Number(attrs.step) || 0.5;
    const min = Math.max(Number(attrs.min) || 0, Number.isFinite(floor) ? floor : 0);
    input.min = Math.ceil(min / step) * step;
    input.max = Math.max(Number(input.min), Number(attrs.max) || 24);
    input.step = step;
    input.value = button.disabled ? input.min : num(state);
    output.textContent = value.textContent = button.disabled ? "\u2013" : formatHours(Number(input.value));
    input.setAttribute("aria-valuetext", output.textContent);
    button.setAttribute("aria-label", `${label}: ${output.textContent}`);
  };
  button.close = () => { if (dialog.open) dialog.close(); };
  return button;
}

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
    const timeline = tag("div", "timeline", middle);
    ui.buckets = tag("div", "buckets", timeline);
    ui.horizon = tag("span", "horizon", timeline);
    ui.sub = tag("div", "sub", middle, "");

    const side = tag("div", "side", row);
    ui.chevron = tag("button", "mini", side);
    ui.chevron.setAttribute("aria-label", "Expand controls");
    ui.chevron.setAttribute("aria-expanded", "false");
    ui.chevronIcon = document.createElement("ha-icon");
    ui.chevronIcon.setAttribute("icon", "mdi:chevron-down");
    ui.chevron.appendChild(ui.chevronIcon);
    ui.chevron.addEventListener("click", () => {
      this._expanded = !this._expanded;
      haptic("light");
      this._syncDrawer();
    });

    ui.drawer = tag("div", "drawer", pad);
    ui.drawer.id = "strip-controls";
    ui.chevron.setAttribute("aria-controls", ui.drawer.id);
    ui.drawerBody = tag("div", "drawer-body", ui.drawer);
    ui.requestButton = tag("button", "control", ui.drawerBody);
    ui.requestButton.title = "Requested";
    const dot = tag("span", "request-dot", ui.requestButton);
    dot.setAttribute("aria-hidden", "true");
    tag("span", "control-label", ui.requestButton, "Requested");
    ui.requestButton.addEventListener("click", () => this._toggle("load_requested"));
    ui.runButton = tag("button", "control", ui.drawerBody);
    ui.runButton.title = "Run now";
    const runIcon = tag("ha-icon", "control-icon", ui.runButton);
    runIcon.setAttribute("icon", "mdi:flash");
    runIcon.setAttribute("aria-hidden", "true");
    tag("span", "control-label", ui.runButton, "Run now");
    ui.runButton.addEventListener("click", () => {
      const entityId = this._entity("run_now");
      if (entityId) { haptic("medium"); pressButton(this._hass, entityId); }
    });
    const set = (key, value) => {
      const entityId = this._entity(key);
      if (entityId) setNumber(this._hass, entityId, value);
    };
    ui.withinButton = stripNumberControl(ui.drawerBody, "Run within", (value) => set("run_within", value));
    ui.hoursButton = stripNumberControl(ui.drawerBody, "Hours needed", (value) => set("operating_hours", value));
  }

  _syncDrawer() {
    const ui = this._ui;
    ui.drawer.style.height = this._expanded ? "auto" : "0px";
    ui.chevronIcon.setAttribute("icon", this._expanded ? "mdi:chevron-up" : "mdi:chevron-down");
    ui.chevron.setAttribute("aria-expanded", String(this._expanded));
    ui.chevron.setAttribute("aria-label", this._expanded ? "Collapse controls" : "Expand controls");
    ui.drawer.inert = !this._expanded;
    if (!this._expanded) { ui.withinButton.close(); ui.hoursButton.close(); }
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

    const asks = view.onDemand || view.onSurplus;
    const controls = [
      [ui.requestButton, "show_requested", "load_requested", asks],
      [ui.runButton, "show_run_now", "run_now", true],
      [ui.withinButton, "show_run_within", "run_within", view.onDemand],
      [ui.hoursButton, "show_hours_needed", "operating_hours", true],
    ];
    let visible = 0;
    for (const [button, option, entity, applicable] of controls) {
      button.hidden = !showsStripControl(this._config, option) || !applicable || !this._entity(entity);
      if (!button.hidden) visible++;
      else if (button.close) button.close();
      const state = view.find(entity);
      // A HA button is "unknown" until its first press; it is still usable.
      button.disabled = !state || state.state === "unavailable" ||
        (entity !== "run_now" && state.state === "unknown");
    }
    const attrs = view.shouldRun && view.shouldRun.attributes || {};
    const runNowActive = attrs.run_now_active === true ||
      (attrs.run_now_active === undefined && attrs.mode === "force_on");
    ui.requestButton.setAttribute("aria-pressed", String(view.isRequested));
    ui.requestButton.classList.toggle("enabled", view.isRequested && !runNowActive && !ui.requestButton.disabled);
    ui.runButton.classList.toggle("enabled", runNowActive && !ui.runButton.disabled);
    ui.runButton.title = runNowActive ? "Run now is active" : "Run now";
    ui.withinButton.setState(view.find("run_within"), num(view.find("operating_hours")));
    ui.hoursButton.setState(view.find("operating_hours"));
    ui.withinButton.style.marginLeft = "auto";
    ui.hoursButton.style.marginLeft = ui.withinButton.hidden ? "auto" : "0";
    ui.drawerBody.classList.toggle("four-controls", visible === 4);
    ui.chevron.hidden = visible === 0;
    if (!visible) this._expanded = false;
    this._syncDrawer();
  }

  /** 48 half-hour buckets across the plan's own horizon. */
  _buckets(view) {
    const ui = this._ui;
    const count = 48;
    ui.buckets.textContent = "";
    ui.buckets.style.removeProperty("--deadline-position");
    ui.buckets.classList.toggle("has-deadline", false);
    ui.horizon.textContent = "";
    if (view.points.length < 2) return;
    const t0 = view.points[0].t;
    // Schedule timestamps are step starts, including the final point.
    const last = view.points[view.points.length - 1];
    const t1 = last.t + (view.points[1].t - t0);
    const span = (t1 - t0) / count;
    ui.horizon.textContent = formatHours((t1 - t0) / 3600000);
    ui.horizon.title = "Schedule time span";
    const now = Date.now();
    if (view.deadline !== null && view.deadline >= t0 && view.deadline <= t1) {
      ui.buckets.style.setProperty("--deadline-position", `${100 * (view.deadline - t0) / (t1 - t0)}%`);
      ui.buckets.classList.toggle("has-deadline", true);
    }

    for (let i = 0; i < count; i++) {
      const from = t0 + i * span;
      const to = from + span;
      let on = false;
      for (const run of view.runs) {
        const end = !view.sequence && last.v > 0 && run.end === last.t ? t1 : run.end;
        if (run.start < to && end > from) on = true;
      }
      const cell = tag("i", "b", ui.buckets);
      if (on) cell.setAttribute("data-on", "");
      if (now >= from && now < to) cell.setAttribute("data-now", "");
    }
  }
}

EmhassDeferrableStripCard.ticks = true;
EmhassDeferrableStripCard.css = `
  .pad { padding: 12px 14px; container-type: inline-size; container-name: strip-card; }
  .strip { display: flex; align-items: center; gap: 12px; }
  .mid { flex: 1; min-width: 0; }
  .line { display: flex; align-items: baseline; gap: 8px; }
  .nm { font-size: .98rem; font-weight: 500; flex: 1; min-width: 0;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .st { font-size: .74rem; color: var(--emh-dim); flex: 0 0 auto; }
  .st.on { color: var(--emh-ok); }
  .st.warn { color: var(--emh-warn); }
  .timeline { display: flex; align-items: center; gap: 6px; margin: 6px 0 4px; }
  .horizon { font-size: .65rem; color: var(--emh-dim); white-space: nowrap; }
  .buckets { position: relative; display: flex; flex: 1; min-width: 0; gap: 1px; height: 14px; }
  .buckets .b { flex: 1; border-radius: 1px; background: var(--emh-surface-2);
                transition: background 300ms; }
  .buckets .b[data-on] { background: var(--emh-solar); }
  .buckets .b[data-now] { background: var(--primary-text-color); }
  .buckets.has-deadline::after { content: ""; position: absolute; top: -2px; bottom: -2px;
    left: var(--deadline-position); border-left: 1px dotted var(--emh-warn);
    transform: translateX(-.5px); pointer-events: none; }
  .sub { font-size: .72rem; }
  .side { display: flex; flex-direction: column; gap: 4px; flex: 0 0 auto; }
  .mini { width: 30px; height: 30px; border-radius: 50%; border: 0; padding: 0;
          display: grid; place-items: center; cursor: pointer;
          background: var(--emh-surface); color: var(--emh-dim);
          transition: transform 120ms var(--emh-ease), background 200ms; }
  .mini:active { transform: scale(.9); }
  .mini ha-icon { --mdc-icon-size: 17px; }
  .drawer { height: 0; overflow: hidden; transition: height 300ms var(--emh-ease); }
  [hidden] { display: none !important; }
  .drawer-body { position: relative; display: flex; align-items: center; gap: 8px; padding-top: 28px; }
  .drawer-body::before { content: ""; position: absolute; top: 12px; left: 0; right: 0;
                         border-top: 1px solid var(--divider-color); }
  .control { display: inline-flex; align-items: center; justify-content: center; gap: 6px;
             flex: 0 1 auto; min-width: 0; min-height: 38px; border: 1px solid var(--divider-color);
             border-radius: 12px; padding: 8px 12px; font: inherit; font-size: .78rem;
             background: var(--emh-surface); color: var(--emh-dim); cursor: pointer;
             white-space: nowrap; }
  .control-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .control-short { display: none; }
  .request-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex: none; }
  .control-icon, .control-caret { --mdc-icon-size: 15px; flex: none; }
  .control.enabled { color: var(--emh-ok);
    background: color-mix(in srgb, var(--emh-ok) 14%, var(--card-background-color));
    border-color: color-mix(in srgb, var(--emh-ok) 40%, var(--divider-color)); }
  .control:disabled { opacity: .5; cursor: default; }
  .control-value { flex: none; font-weight: 500; font-variant-numeric: tabular-nums; }
  .number-dialog { width: min(280px, calc(100vw - 48px)); padding: 16px;
    border: 1px solid var(--divider-color); border-radius: 14px;
    background: var(--card-background-color); color: var(--primary-text-color); }
  .number-dialog::backdrop { background: rgb(0 0 0 / .25); }
  .number-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .number-dialog input { width: 100%; accent-color: var(--emh-ok); }
  .number-dialog output { display: block; text-align: right; margin-top: 8px; }
  @media (pointer: coarse) { .control, .mini { min-height: 44px; } }
  @container strip-card (max-width: 440px) {
    .drawer-body { gap: 5px; }
    .control { padding-inline: 7px; gap: 4px; font-size: .72rem; }
    .control-caret { display: none; }
    .four-controls .control { padding-inline: 4px; gap: 3px; }
    .four-controls .control-short { display: inline; }
    .four-controls .number-control .control-label,
    .four-controls .request-dot, .four-controls .control-icon { display: none; }
  }
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

const STRIP_LABELS = { ...DEFERRABLE_LABELS };
const STRIP_HELPERS = { ...DEFERRABLE_HELPERS };
for (const [key, label, helper] of STRIP_CONTROLS) {
  STRIP_LABELS[key] = label;
  STRIP_HELPERS[key] = helper;
}

/** Which load, and which compact controls appear in its expanded row. */
class EmhassDeferrableStripCardEditor extends CardEditor {
  get labels() {
    return STRIP_LABELS;
  }

  get helpers() {
    return STRIP_HELPERS;
  }

  schema(hass) {
    return [loadField(hass), sectionGrid(STRIP_CONTROLS)];
  }

  data() {
    const data = { load: this._config.load || "" };
    for (const [key] of STRIP_CONTROLS) data[key] = showsStripControl(this._config, key);
    return data;
  }

  clean(config) {
    cleanSections(config, STRIP_CONTROLS, STRIP_OFF_BY_DEFAULT);
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
