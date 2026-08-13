/**
 * Extracted from the single-bundle emhass-cards.js, which shipped all fourteen
 * elements in one module: when the frontend's own fixed element-registration
 * timeout (home-assistant/frontend#52960) is lost, the whole module fails, so
 * one lost race took down every card at once. Split into a shared core module
 * plus one bundle per card family, a lost race costs one family's elements.
 */

import {
  CardEditor,
  LiveCard,
  cleanSections,
  findHub,
  findLoads,
  formatAgo,
  formatCountdown,
  formatEnergy,
  formatPower,
  formatTime,
  haptic,
  isUsable,
  labelFor,
  loadHaForm,
  moreInfo,
  num,
  pressButton,
  sectionGrid,
  series,
  showsSection,
  statTile,
  stateOf,
  tag,
  valueBox,
} from "./emhass-core.js?v=__VERSION__";


/* ====================================================== status/info cards */

/** A labelled meter bar, 0..1, coloured by the caller. */
function meter(parent, label) {
  const root = tag("div", "meter", parent);
  const head = tag("div", "meter-head", root);
  const key = tag("span", null, head, label);
  const value = tag("span", "meter-value", head, "");
  const rail = tag("div", "rail", root);
  const fill = tag("div", "fill", rail);
  root.set = (fraction, text, color) => {
    const clamped = Math.max(0, Math.min(1, Number.isFinite(fraction) ? fraction : 0));
    fill.style.width = `${clamped * 100}%`;
    if (color) fill.style.background = color;
    value.textContent = text === undefined ? "" : text;
  };
  root.setLabel = (text) => {
    key.textContent = text;
  };
  return root;
}

/* --------------------------------------------- info card 1: optimiser health */

/**
 * The sections the health card is built from, in the order they are drawn.
 *
 * One list drives all three of them: what `build()` puts on the card, what the
 * visual editor offers, and which keys are worth writing into the YAML -- the
 * same arrangement the overview card uses, so a section added here appears in
 * the editor without the editor being touched.
 */
const HEALTH_SECTIONS = [
  ["show_freshness", "Plan freshness", "The plan's age, against the limit it is dropped at"],
  ["show_stats", "Info boxes", "The row of value boxes under the header"],
  ["show_stages", "Where the time went", "The run's stage timings, as one stacked bar"],
  ["show_problems", "Warnings", "Warnings and errors collected by the last run"],
  ["show_actions", "Buttons", "Recalculate, day-ahead and train"],
];

/**
 * The warning that is not news.
 *
 * A day-ahead price source (Nord Pool and friends) does not publish tomorrow
 * until early afternoon, so the payload back-fills the tail of the horizon by
 * repeating the previous day's shape -- and says so, every run, for well over
 * half of every day. It is correct and it is expected, and a box that carries
 * it permanently is a box the eye learns to skip, including on the day
 * something real turns up in it. So it can be filtered out, and only it:
 * the *other* short-series warning ("EMHASS will hold the last value") is the
 * one that means a forecast genuinely ran out, and stays.
 */
const FILL_WARNING = /remainder was filled in by repeating/i;

/** A percentage-valued sensor, as a box value. */
function boxPercent(stateObj) {
  return isUsable(stateObj) ? `${num(stateObj).toFixed(0)} %` : "–";
}

/**
 * What the two spare info boxes can be pointed at.
 *
 * Deliberately wider than the card's own subject: the four fixed boxes already
 * answer "did the run go well", so what is worth choosing here is the context
 * you happen to want next to that answer -- which settings were in force, what
 * the plan is doing right now, how much of the horizon it actually covers.
 *
 * Each entry may name an `entity`, which is both what the box opens when
 * tapped and, where the value is just that entity's state, where the value
 * comes from. `read` returns `{v, s}`: the value, and the second line that
 * says what the value is being measured against.
 */
const HEALTH_METRICS = {
  none: { label: "Nothing", read: () => ({}) },
  goes_stale: {
    label: "Goes stale in",
    entity: "binary_sensor.plan_stale",
    read: (c) => ({
      v: c.limitMs && Number.isFinite(c.age) ? formatCountdown(c.limitMs - c.age) : "–",
      s: c.limitMs ? `limit ${formatCountdown(c.limitMs)}` : "",
    }),
  },
  action: {
    label: "Last action",
    entity: "sensor.optimization_status",
    read: (c) => ({
      v: c.attrs.action ? String(c.attrs.action).replace(/_/g, "-") : "–",
      s: c.attrs.infeasible === true ? "infeasible" : "",
    }),
  },
  version: {
    label: "EMHASS version",
    entity: "sensor.optimization_status",
    read: (c) => ({
      v: c.attrs.emhass_version ? String(c.attrs.emhass_version) : "–",
      s: c.attrs.schema_version ? `schema ${c.attrs.schema_version}` : "",
    }),
  },
  warnings: {
    label: "Warnings",
    entity: "sensor.optimization_status",
    read: (c) => ({
      v: String(c.warnings.length),
      // Counting what the card is not showing would make the box disagree
      // with the list under it, so the filtered ones are named separately.
      s: c.hidden ? `${c.hidden} filtered` : "from the last run",
    }),
  },
  slowest_stage: {
    label: "Slowest stage",
    entity: "sensor.optimization_status",
    read: (c) => {
      const stages = c.attrs.stage_times;
      if (!stages || typeof stages !== "object") return { v: "–" };
      let worst = null;
      for (const key of Object.keys(stages)) {
        const seconds = Number(stages[key]);
        if (!Number.isFinite(seconds)) continue;
        if (!worst || seconds > worst[1]) worst = [key, seconds];
      }
      if (!worst) return { v: "–" };
      return { v: worst[0].replace(/_/g, " "), s: `${worst[1].toFixed(1)} s` };
    },
  },
  mode: {
    label: "System mode",
    entity: "select.system_mode",
    read: (c) => ({ v: c.state ? labelFor(c.hass, c.state) : "–" }),
  },
  goal: {
    label: "Optimisation goal",
    entity: "select.cost_fun",
    read: (c) => ({ v: c.state ? labelFor(c.hass, c.state) : "–" }),
  },
  control: {
    label: "Control",
    entity: "switch.control_enabled",
    read: (c) => ({
      v: c.state ? labelFor(c.hass, c.state) : "–",
      s: c.state && c.state.state === "off" ? "dry run" : "",
    }),
  },
  soc: {
    label: "Charge level",
    entity: "sensor.battery_soc",
    read: (c) => ({ v: boxPercent(c.state) }),
  },
  end_soc: {
    label: "End charge target",
    entity: "sensor.end_soc_target",
    read: (c) => ({ v: boxPercent(c.state) }),
  },
  battery: {
    label: "Battery now",
    entity: "sensor.battery_power",
    read: (c) => {
      const watts = num(c.state);
      return {
        v: formatPower(Math.abs(watts)),
        s: !Number.isFinite(watts) || watts === 0 ? "idle" : watts < 0 ? "charging" : "discharging",
      };
    },
  },
  battery_action: {
    label: "Battery action",
    entity: "sensor.battery_action",
    read: (c) => ({ v: c.state ? labelFor(c.hass, c.state) : "–" }),
  },
  solar: {
    label: "Solar now",
    entity: "sensor.pv_forecast",
    read: (c) => ({ v: formatPower(num(c.state)) }),
  },
  house: {
    label: "House now",
    entity: "sensor.load_forecast",
    read: (c) => ({ v: formatPower(num(c.state)) }),
  },
  grid: {
    label: "Grid now",
    entity: "sensor.grid_forecast",
    read: (c) => {
      const watts = num(c.state);
      return {
        v: formatPower(Math.abs(watts)),
        s: !Number.isFinite(watts) ? "" : watts < 0 ? "exporting" : "importing",
      };
    },
  },
  price: {
    label: "Import price now",
    entity: "sensor.buy_price",
    read: (c) => ({ v: isUsable(c.state) ? num(c.state).toFixed(3) : "–", s: unitOf(c.state) }),
  },
  sell_price: {
    label: "Export price now",
    entity: "sensor.sell_price",
    read: (c) => ({ v: isUsable(c.state) ? num(c.state).toFixed(3) : "–", s: unitOf(c.state) }),
  },
  surplus: {
    label: "Spare solar",
    entity: "sensor.solar_surplus_energy",
    read: (c) => {
      const start = stateOf(c.hass, c.hub["sensor.solar_surplus_start"]);
      const end = stateOf(c.hass, c.hub["sensor.solar_surplus_end"]);
      const window =
        isUsable(start) && isUsable(end)
          ? `${formatTime(Date.parse(start.state), c.hass)} – ${formatTime(
              Date.parse(end.state),
              c.hass,
            )}`
          : "";
      return { v: isUsable(c.state) ? formatEnergy(num(c.state)) : "–", s: window };
    },
  },
  plan_end: {
    label: "Plan runs to",
    entity: "sensor.pv_forecast",
    read: (c) => {
      if (c.points.length < 2) return { v: "–" };
      const last = c.points[c.points.length - 1].t;
      return { v: formatTime(last, c.hass), s: `in ${formatCountdown(last - Date.now())}` };
    },
  },
  steps: {
    label: "Plan points",
    entity: "sensor.pv_forecast",
    read: (c) => {
      if (c.points.length < 2) return { v: "–" };
      const step = Math.round((c.points[1].t - c.points[0].t) / 60000);
      return { v: String(c.points.length), s: step > 0 ? `${step} min apart` : "" };
    },
  },
  loads: {
    label: "Deferrable loads",
    read: (c) => {
      // Load entities are keyed by the bare translation key, unlike the hub's.
      const loads = findLoads(c.hass);
      const on = loads.filter((load) => {
        const enabled = stateOf(c.hass, load.entities["load_enabled"]);
        return !enabled || enabled.state === "on";
      });
      return { v: String(loads.length), s: loads.length ? `${on.length} enabled` : "" };
    },
  },
};

/** The dropdown's order, which is the order they are worth reaching for. */
const HEALTH_METRIC_ORDER = [
  "none",
  "goes_stale",
  "action",
  "version",
  "warnings",
  "slowest_stage",
  "mode",
  "goal",
  "control",
  "soc",
  "end_soc",
  "battery",
  "battery_action",
  "solar",
  "house",
  "grid",
  "price",
  "sell_price",
  "surplus",
  "plan_end",
  "steps",
  "loads",
];

// Boxes five and six, numbered from the four fixed ones they sit beside. Both
// default to something rather than to "Nothing", since the whole reason to add
// them was to have them -- and both are context the card could not show
// before rather than a second copy of something it already does.
const HEALTH_DEFAULTS = ["goes_stale", "mode"];

function healthMetric(config, index) {
  const wanted = config ? config[`box_${index}`] : undefined;
  return wanted && HEALTH_METRICS[wanted] ? wanted : HEALTH_DEFAULTS[index - 5];
}

function unitOf(stateObj) {
  return stateObj && stateObj.attributes && stateObj.attributes.unit_of_measurement
    ? String(stateObj.attributes.unit_of_measurement)
    : "";
}

/**
 * Is the optimiser healthy, and when did it last say so.
 *
 * Everything here already exists as attributes on the status sensor, where
 * nobody looks: the run's duration, its stage breakdown, the EMHASS version
 * that answered, and the warnings it collected. A card is the only place any
 * of it gets read before something has already gone wrong.
 */
class EmhassHealthCard extends LiveCard {
  static getStubConfig() {
    return { type: "custom:emhass-health-card" };
  }

  /** The visual editor, with Home Assistant's own form widgets pulled in. */
  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-health-card-editor");
  }

  getCardSize() {
    let size = 2;
    for (const section of HEALTH_SECTIONS) {
      if (showsSection(this._config, section[0])) size += 1;
    }
    return size;
  }

  build(card) {
    const ui = {};
    this._ui = ui;
    const shows = (key) => showsSection(this._config, key);
    const pad = tag("div", "pad", card);

    const head = tag("div", "head", pad);
    const square = tag("div", "sq", head);
    ui.icon = document.createElement("ha-icon");
    ui.icon.setAttribute("icon", "mdi:chart-timeline-variant");
    square.appendChild(ui.icon);
    ui.square = square;
    square.addEventListener("click", () => moreInfo(this, this._hub["sensor.optimization_status"]));
    const grow = tag("div", "grow", head);
    tag("div", "name", grow, this._config.title || "EMHASS");
    ui.version = tag("div", "sub", grow, "");
    ui.pill = tag("div", "pill", head);
    tag("span", "dot", ui.pill);
    ui.pillText = tag("span", null, ui.pill, "");

    if (shows("show_freshness")) ui.freshness = meter(pad, "Plan freshness");

    if (shows("show_stats")) {
      ui.stats = tag("div", "stats", pad);
      ui.tileRun = statTile(ui.stats, "Last run");
      ui.tileSolve = statTile(ui.stats, "Solve time");
      ui.tileCost = statTile(ui.stats, "Planned cost");
      ui.tileHorizon = statTile(ui.stats, "Horizon");
      // The chosen boxes carry a second line, because most of what can go in
      // them is a number that means nothing without what it is measured
      // against ("limit 30 m", "charging"). A box set to "Nothing" is not
      // built at all rather than left empty: the row is an auto-fit grid, and
      // an empty cell would still take a column.
      ui.extra = HEALTH_DEFAULTS.map((fallback, offset) => {
        const metric = healthMetric(this._config, offset + 5);
        if (metric === "none") return null;
        const spec = HEALTH_METRICS[metric];
        const box = valueBox(ui.stats, spec.label);
        box.metric = metric;
        if (spec.entity) {
          box.classList.add("tap");
          box.addEventListener("click", () => moreInfo(this, this._hub[spec.entity]));
        }
        return box;
      });
    }

    if (shows("show_stages")) {
      ui.stagesWrap = tag("div", "stages-wrap", pad);
      tag("div", "section", ui.stagesWrap, "Where the time went");
      ui.stages = tag("div", "stages", ui.stagesWrap);
      ui.stageKey = tag("div", "stage-key", ui.stagesWrap);
    }

    if (shows("show_problems")) ui.problems = tag("div", "problems", pad);

    if (shows("show_actions")) {
      const actions = tag("div", "actions", pad);
      ui.buttonMpc = tag("button", "btn primary", actions, "Recalculate");
      ui.buttonDay = tag("button", "btn", actions, "Day-ahead");
      ui.buttonFit = tag("button", "btn", actions, "Train");
      ui.buttonMpc.addEventListener("click", () => this._press("button.run_mpc"));
      ui.buttonDay.addEventListener("click", () => this._press("button.run_dayahead"));
      ui.buttonFit.addEventListener("click", () => this._press("button.run_forecast_fit"));
    }
  }

  _press(key) {
    const entityId = this._hub[key];
    if (!entityId) return;
    haptic("medium");
    pressButton(this._hass, entityId);
  }

  update(hass) {
    const ui = this._ui;
    this._hub = findHub(hass);
    const hub = this._hub;
    const status = stateOf(hass, hub["sensor.optimization_status"]);
    const stale = stateOf(hass, hub["binary_sensor.plan_stale"]);
    const cost = stateOf(hass, hub["sensor.plan_cost"]);
    const pv = stateOf(hass, hub["sensor.pv_forecast"]);

    if (!status) {
      ui.pillText.textContent = "Not set up";
      return;
    }
    const attrs = status.attributes || {};
    const problems = this._collect(attrs);

    /* --- headline state --- */
    const infeasible = attrs.infeasible === true;
    const failed = Boolean(attrs.error_message);
    const isStale = Boolean(stale) && stale.state === "on";
    let cls = "on";
    let text = labelFor(hass, status);
    if (failed) {
      cls = "bad";
      text = "Run failed";
    } else if (infeasible) {
      cls = "bad";
      text = "Infeasible";
    } else if (isStale) {
      cls = "warn";
      text = "Out of date";
    }
    ui.pill.className = `pill ${cls}`;
    ui.pillText.textContent = text;
    ui.square.className = `sq ${cls === "on" ? "on" : cls}`;
    ui.icon.setAttribute(
      "icon",
      failed || infeasible ? "mdi:alert-circle-outline" : "mdi:chart-timeline-variant",
    );

    // "EMHASS" is dropped from the version bit -- the card is already titled
    // EMHASS, so it is dead weight this line cannot afford: on a narrow card
    // the one-line ellipsis in .sub cuts the schema version, the one bit here
    // that actually explains a "no plan" card (see _schema_supported).
    const bits = [];
    if (attrs.emhass_version) bits.push(attrs.emhass_version);
    if (attrs.action) bits.push(String(attrs.action).replace(/_/g, "-"));
    if (attrs.schema_version) bits.push(`schema ${attrs.schema_version}`);
    ui.version.textContent = bits.join(" · ");
    ui.version.title = bits.join(" · ");

    /* --- freshness, as a fraction of the age at which the plan is dropped --- */
    const staleAttrs = stale && stale.attributes ? stale.attributes : {};
    const lastMs = staleAttrs.last_successful_run
      ? Date.parse(staleAttrs.last_successful_run)
      : NaN;
    const limitMs = parseDuration(staleAttrs.stale_after);
    const age = Number.isFinite(lastMs) ? Date.now() - lastMs : NaN;
    const fraction = Number.isFinite(age) && limitMs ? age / limitMs : isStale ? 1 : 0;
    if (ui.freshness) {
      ui.freshness.set(
        fraction,
        // The limit itself is left out here -- the fill bar already shows the
        // age as a fraction of it, and spelling it out too ("of 30 m") reads
        // like the age is wrong when it is only the buffer being generous.
        Number.isFinite(age) ? `${formatCountdown(age)} old` : "never run",
        fraction >= 1 ? "var(--emh-bad)" : fraction > 0.7 ? "var(--emh-warn)" : "var(--emh-ok)",
      );
    }

    const points = series(pv);
    if (ui.stats) {
      ui.tileRun.set(Number.isFinite(lastMs) ? formatAgo(lastMs, hass) : "never");
      ui.tileSolve.set(
        Number.isFinite(Number(attrs.duration_seconds))
          ? `${Number(attrs.duration_seconds).toFixed(1)} s`
          : "–",
      );
      const costUnit = unitOf(cost) ? ` ${unitOf(cost)}` : "";
      ui.tileCost.set(isUsable(cost) ? `${Number(cost.state).toFixed(2)}${costUnit}` : "–");
      ui.tileHorizon.set(
        points.length > 1
          ? `${Math.round((points[points.length - 1].t - points[0].t) / 3600000)} h`
          : "–",
      );

      const context = {
        hass,
        hub,
        attrs,
        status,
        stale,
        lastMs,
        limitMs,
        age,
        points,
        warnings: problems.warnings,
        hidden: problems.hidden,
      };
      for (const box of ui.extra) {
        if (!box) continue;
        const spec = HEALTH_METRICS[box.metric];
        // The entity is resolved for the metric rather than by it, so a box
        // whose value *is* an entity state does not have to say so twice.
        context.state = spec.entity ? stateOf(hass, hub[spec.entity]) : undefined;
        const shown = spec.read(context);
        box.set(shown.v, shown.s);
      }
    }

    this._stages(attrs.stage_times);
    this._problems(problems);
  }

  /**
   * The last run's complaints, deduplicated and filtered.
   *
   * Computed once per update rather than inside `_problems`, because the
   * "Warnings" info box has to count exactly what the list below it shows --
   * a box reading 3 over a list of one is worse than no box at all.
   *
   * Word-for-word duplicates say nothing the first one did not: two identical
   * rows read as two problems and cost the card twice the height for one.
   * Deduplicated by text rather than by position, since a run can collect the
   * same warning from stages that never see each other.
   */
  _collect(attrs) {
    const hide = this._config && this._config.hide_fill_warnings === true;
    const seen = new Set();
    const warnings = [];
    let hidden = 0;
    for (const raw of Array.isArray(attrs.warnings) ? attrs.warnings : []) {
      const warning = String(raw);
      if (seen.has(warning)) continue;
      seen.add(warning);
      if (hide && FILL_WARNING.test(warning)) {
        hidden += 1;
        continue;
      }
      warnings.push(warning);
    }
    return { error: attrs.error_message ? String(attrs.error_message) : "", warnings, hidden };
  }

  /**
   * The per-stage timings as one stacked bar.
   *
   * A list of numbers makes you do the comparison yourself; the whole question
   * asked of stage timings is which stage dominates, which is a length.
   */
  _stages(stageTimes) {
    const ui = this._ui;
    if (!ui.stagesWrap) return;
    ui.stages.textContent = "";
    ui.stageKey.textContent = "";
    if (!stageTimes || typeof stageTimes !== "object") {
      ui.stagesWrap.style.display = "none";
      return;
    }
    const entries = Object.keys(stageTimes)
      .map((key) => [key, Number(stageTimes[key])])
      .filter((entry) => Number.isFinite(entry[1]) && entry[1] > 0);
    if (!entries.length) {
      ui.stagesWrap.style.display = "none";
      return;
    }
    ui.stagesWrap.style.display = "";
    const total = entries.reduce((sum, entry) => sum + entry[1], 0);
    const palette = [
      "var(--emh-accent)",
      "var(--emh-solar)",
      "var(--emh-battery)",
      "var(--emh-grid)",
      "var(--emh-warn)",
    ];
    entries.forEach((entry, index) => {
      const segment = tag("div", "stage", ui.stages);
      segment.style.flexGrow = entry[1];
      segment.style.background = palette[index % palette.length];
      segment.title = `${entry[0]}: ${entry[1].toFixed(2)} s`;

      const key = tag("span", "legend", ui.stageKey);
      const swatch = tag("i", "swatch", key);
      swatch.style.background = palette[index % palette.length];
      tag("span", null, key, `${entry[0].replace(/_/g, " ")} ${entry[1].toFixed(1)}s`);
    });
    const key = tag("span", "legend total", ui.stageKey, `total ${total.toFixed(1)}s`);
    key.title = "sum of all stages";
  }

  _problems(problems) {
    const ui = this._ui;
    if (!ui.problems) return;
    ui.problems.textContent = "";
    const note = (cls, icon, text) => {
      const box = tag("div", `note ${cls}`, ui.problems);
      const element = document.createElement("ha-icon");
      element.setAttribute("icon", icon);
      box.appendChild(element);
      tag("span", null, box, text);
      return box;
    };
    if (problems.error) note("bad", "mdi:alert-circle", problems.error);
    for (const warning of problems.warnings) note("warn", "mdi:alert-outline", warning);
    if (!problems.error && !problems.warnings.length) {
      // Saying nothing at all here would leave a filtered-away warning
      // indistinguishable from a clean run, which is the one thing the filter
      // must not do -- so the count of what was hidden stays on the card.
      note(
        "ok",
        "mdi:check-circle-outline",
        problems.hidden
          ? `No warnings from the last run, ${problems.hidden} filtered out.`
          : "No warnings from the last run.",
      );
    } else if (problems.hidden) {
      note("muted", "mdi:filter-outline", `${problems.hidden} filtered out.`);
    }
  }
}

/** "0:30:00" and "1 day, 0:00:00" -- Python's timedelta, as milliseconds. */
function parseDuration(text) {
  if (!text) return 0;
  const match = /(?:(\d+) day[s]?, )?(\d+):(\d+):(\d+)/.exec(String(text));
  if (!match) return 0;
  const days = match[1] ? Number(match[1]) : 0;
  return (
    ((days * 24 + Number(match[2])) * 3600 + Number(match[3]) * 60 + Number(match[4])) * 1000
  );
}

EmhassHealthCard.ticks = true;
EmhassHealthCard.css = `
  .meter { margin-top: 14px; }
  .meter-head { display: flex; justify-content: space-between; font-size: .74rem;
                color: var(--emh-dim); margin-bottom: 5px; }
  .meter-value { font-variant-numeric: tabular-nums; }
  .rail { height: 6px; border-radius: 999px; background: var(--emh-surface-2);
          overflow: hidden; }
  .rail .fill { height: 100%; width: 0; border-radius: 999px;
                background: var(--emh-ok);
                transition: width 500ms var(--emh-ease), background 300ms; }
  .section { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em;
             color: var(--emh-dim); margin: 16px 0 6px 0; }
  .stages { display: flex; gap: 2px; height: 10px; }
  .stages .stage { border-radius: 2px; min-width: 3px; }
  .stage-key { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 7px;
               font-size: .72rem; color: var(--emh-dim); }
  .legend { display: inline-flex; align-items: center; gap: 5px; }
  .legend .swatch { width: 8px; height: 8px; border-radius: 2px; }
  .legend.total { font-weight: 500; color: var(--primary-text-color); }
  .problems { margin-top: 14px; display: grid; gap: 6px; }
  .note { display: flex; gap: 8px; align-items: flex-start; font-size: .8rem;
          padding: 8px 10px; border-radius: 10px; background: var(--emh-surface);
          color: var(--emh-dim); }
  .note ha-icon { --mdc-icon-size: 18px; flex: 0 0 auto; }
  .note.ok { color: var(--emh-ok); }
  .note.warn { color: var(--emh-warn); background: rgba(255, 166, 0, .12);
               background: color-mix(in srgb, var(--emh-warn) 12%, transparent); }
  .note.bad { color: var(--emh-bad); background: rgba(219, 68, 55, .12);
              background: color-mix(in srgb, var(--emh-bad) 12%, transparent); }
  /* What the filter swallowed is a footnote, not a problem: it must be
     readable without competing with the warnings it sits under. */
  .note.muted { font-size: .74rem; padding: 5px 10px; background: none; }
  .actions { display: flex; gap: 8px; margin-top: 14px; }
  .actions .btn { flex: 1; }
`;

/* --------------------------------------------------- health visual editor */

const HEALTH_LABELS = {
  title: "Title",
  box_5: "Box 5",
  box_6: "Box 6",
  hide_fill_warnings: "Hide price back-fill warnings",
};
const HEALTH_HELPERS = {
  hide_fill_warnings:
    "The one that says the price forecast was extended by repeating the previous day. " +
    "Expected on a day-ahead price source, and on the card for half of every day.",
};
for (const section of HEALTH_SECTIONS) {
  HEALTH_LABELS[section[0]] = section[1];
  HEALTH_HELPERS[section[0]] = section[2];
}

const HEALTH_METRIC_OPTIONS = HEALTH_METRIC_ORDER.map((key) => ({
  value: key,
  label: HEALTH_METRICS[key].label,
}));

const HEALTH_SCHEMA = [
  { name: "title", selector: { text: {} } },
  sectionGrid(HEALTH_SECTIONS),
  // Folded away by default: two dropdowns and a filter are worth having, but
  // not worth standing between the title and the switches.
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
        schema: [
          { name: "box_5", selector: { select: { mode: "dropdown", options: HEALTH_METRIC_OPTIONS } } },
          { name: "box_6", selector: { select: { mode: "dropdown", options: HEALTH_METRIC_OPTIONS } } },
        ],
      },
    ],
  },
  {
    name: "",
    type: "expandable",
    title: "Warnings",
    icon: "mdi:filter-outline",
    schema: [{ name: "hide_fill_warnings", selector: { boolean: {} } }],
  },
];

/** Which parts of the health card to draw, and what the spare boxes carry. */
class EmhassHealthCardEditor extends CardEditor {
  get labels() {
    return HEALTH_LABELS;
  }

  get helpers() {
    return HEALTH_HELPERS;
  }

  schema() {
    return HEALTH_SCHEMA;
  }

  data() {
    const data = {
      title: this._config.title || "",
      hide_fill_warnings: this._config.hide_fill_warnings === true,
    };
    for (const section of HEALTH_SECTIONS) {
      data[section[0]] = showsSection(this._config, section[0]);
    }
    for (let i = 5; i <= 6; i++) data[`box_${i}`] = healthMetric(this._config, i);
    return data;
  }

  clean(config) {
    cleanSections(config, HEALTH_SECTIONS);
    for (let i = 5; i <= 6; i++) {
      if (config[`box_${i}`] === HEALTH_DEFAULTS[i - 5]) delete config[`box_${i}`];
    }
    if (config.hide_fill_warnings !== true) delete config.hide_fill_warnings;
    if (!config.title) delete config.title;
    return config;
  }
}

customElements.define("emhass-health-card", EmhassHealthCard);
customElements.define("emhass-health-card-editor", EmhassHealthCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-health-card",
    name: "EMHASS Companion health",
    description: "Optimiser health: freshness, timings, warnings and re-run buttons.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);
