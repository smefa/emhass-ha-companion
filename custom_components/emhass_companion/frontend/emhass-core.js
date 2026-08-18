/**
 * EMHASS Companion dashboard cards: everything the card bundles share.
 *
 * The cards used to ship as one emhass-cards.js holding all fourteen custom
 * elements. When the frontend's own fixed element-registration timeout
 * (home-assistant/frontend#52960) is lost, the whole module fails, so one lost
 * race took down every card at once. They are now one bundle per card family,
 * importing their common code from here; a lost race costs one family.
 *
 * That makes this file the one place a mistake is still expensive, and the
 * failure mode is specific: a name used by a bundle but missing from the
 * `export` list at the bottom does not break that name, it stops the whole
 * importing module from being instantiated -- an entire family of cards
 * replaced by "Configuration error" boxes. tests/test_packaging.py checks the
 * two lists against each other for that reason.
 *
 * Deliberately plain custom elements with inline SVG: no Lit, no charting
 * library, no build step. A bundler would mean an npm toolchain in CI, a
 * committed artefact that can drift from its source, and Node as a
 * prerequisite for contributors -- none of which these cards justify. It also
 * means nothing is fetched at runtime, so the cards work on an isolated
 * network and cannot be broken by a CDN.
 *
 * ES2017 syntax only -- no optional chaining and no nullish coalescing, both
 * of which defeat the pure-Python parser in tests/test_packaging.py, which is
 * the only automated checking these files get at all.
 *
 * Colours come from Home Assistant's own theme variables wherever one exists,
 * so every card follows the active theme rather than inventing a second
 * palette that only matches the default one.
 *
 * Two generations of card are built on this. The plan and deferrable cards
 * rebuild their markup on every `hass` update, which is fine for a chart; the
 * five that came later extend `LiveCard`, build their DOM once and update it
 * in place, because a rebuild during a drag throws away the element under the
 * finger several times a second.
 */

const PLATFORM = "emhass_companion";
const SVGNS = "http://www.w3.org/2000/svg";

/** The plan card's palette, from Home Assistant's energy dashboard. */
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
  past: "var(--secondary-text-color, #727272)",
};

/* ------------------------------------------------------------------ utils */

function svg(tag, attrs, parent) {
  const node = document.createElementNS(SVGNS, tag);
  const map = attrs || {};
  for (const key of Object.keys(map)) {
    if (map[key] !== null && map[key] !== undefined) node.setAttribute(key, map[key]);
  }
  if (parent) parent.appendChild(node);
  return node;
}

function tag(name, className, parent, text) {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  if (parent) parent.appendChild(node);
  return node;
}

/** Parse a `[{time, value}]` attribute into sorted numeric points. */
function series(stateObj, attribute) {
  const key = attribute || "forecast";
  const raw = stateObj && stateObj.attributes ? stateObj.attributes[key] : null;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((point) => ({ t: Date.parse(point.time), v: Number(point.value) }))
    .filter((point) => Number.isFinite(point.t) && Number.isFinite(point.v))
    .sort((a, b) => a.t - b.t);
}

function num(stateObj) {
  if (!stateObj) return NaN;
  const value = Number(stateObj.state);
  return Number.isFinite(value) ? value : NaN;
}

function isUsable(stateObj) {
  return Boolean(stateObj) && stateObj.state !== "unknown" && stateObj.state !== "unavailable";
}

function formatPower(watts) {
  if (!Number.isFinite(watts)) return "–";
  return Math.abs(watts) >= 1000
    ? `${(watts / 1000).toFixed(1)} kW`
    : `${Math.round(watts)} W`;
}

function formatEnergy(kwh) {
  if (!Number.isFinite(kwh)) return "–";
  return `${kwh.toFixed(1)} kWh`;
}

/**
 * The clock the user asked for, not the one their language implies.
 *
 * `toLocaleTimeString` follows the *language*, which is a different question:
 * someone on en-US who has set a 24-hour clock in their Home Assistant
 * profile still gets "1:30 PM" out of the language alone. The profile setting
 * lives on `hass.locale.time_format`, and "language" / "system" both mean
 * "don't override" -- against the HA language and against the browser's own
 * default respectively.
 *
 * hourCycle rather than hour12, because `hour12: false` renders midnight as
 * "24:00" in several locales.
 */
function timeFormat(hass, options) {
  const locale = hass && hass.locale ? hass.locale : {};
  const wanted = locale.time_format;
  const twelve = wanted === "12" || wanted === "am_pm";
  const merged = Object.assign({}, options);
  if (twelve) {
    merged.hourCycle = "h12";
    // 12-hour clocks are not written with a leading zero anywhere.
    if (merged.hour === "2-digit") merged.hour = "numeric";
  } else if (wanted === "24" || wanted === "twenty_four") {
    merged.hourCycle = "h23";
  }
  return {
    language: wanted === "system" ? undefined : locale.language || undefined,
    options: merged,
  };
}

function formatTime(ms, hass) {
  if (!Number.isFinite(ms)) return "–";
  const format = timeFormat(hass, { hour: "2-digit", minute: "2-digit" });
  return new Date(ms).toLocaleTimeString(format.language, format.options);
}

/** An axis tick, on the user's own clock. */
function formatHour(ms, hass) {
  if (!Number.isFinite(ms)) return "";
  const wanted = hass && hass.locale ? hass.locale.time_format : undefined;
  // "2 PM" rather than "2:00 PM": a tick has no room, and on an hour boundary
  // the minutes are always zero. A 24-hour tick keeps them, because "14"
  // on its own does not read as a time.
  const bare = wanted === "12" || wanted === "am_pm";
  const format = timeFormat(hass, bare ? { hour: "numeric" } : { hour: "numeric", minute: "2-digit" });
  return new Date(ms).toLocaleTimeString(format.language, format.options);
}

/**
 * A time range, with the day named when it does not end on the day it starts.
 *
 * A plan routinely runs past midnight, and two bare clock times then read
 * backwards -- "00:00 → 11:00" looks like eleven hours of this morning when it
 * is really a day and a half. The end is the one that needs saying, since the
 * start is nearly always today.
 */
function formatSpan(t0, t1, hass) {
  const label = `${formatTime(t0, hass)} → ${formatTime(t1, hass)}`;
  const midnight = (ms) => new Date(ms).setHours(0, 0, 0, 0);
  if (midnight(t0) === midnight(t1)) return label;
  const days = Math.round((midnight(t1) - midnight(Date.now())) / 86400000);
  if (days === 1) return `${label} tomorrow`;
  const language = hass && hass.locale ? hass.locale.language : undefined;
  return `${label} ${new Date(t1).toLocaleDateString(language, {
    weekday: "short",
    day: "numeric",
    month: "short",
  })}`;
}

function formatHours(hours) {
  if (!Number.isFinite(hours)) return "–";
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  const whole = Math.floor(hours);
  const minutes = Math.round((hours - whole) * 60);
  return minutes ? `${whole} h ${minutes} m` : `${whole} h`;
}

function formatCountdown(ms) {
  if (!Number.isFinite(ms)) return "–";
  if (ms <= 0) return "overdue";
  const minutes = Math.floor(ms / 60000);
  const hours = Math.floor(minutes / 60);
  return hours > 0 ? `${hours} h ${minutes % 60} m` : `${minutes} m`;
}

/**
 * "3 min ago", in the user's language.
 *
 * A wall-clock timestamp is the wrong answer for freshness: the question a
 * status card is asked is "is this still current", and that is a duration.
 */
function formatAgo(ms, hass) {
  if (!Number.isFinite(ms)) return "never";
  const language = hass && hass.locale ? hass.locale.language : undefined;
  const seconds = Math.round((ms - Date.now()) / 1000);
  const steps = [
    ["second", 60],
    ["minute", 60],
    ["hour", 24],
    ["day", 7],
  ];
  let value = seconds;
  let unit = "second";
  for (const [name, span] of steps) {
    unit = name;
    if (Math.abs(value) < span) break;
    value = Math.round(value / span);
  }
  try {
    return new Intl.RelativeTimeFormat(language, { numeric: "auto" }).format(value, unit);
  } catch (err) {
    return `${Math.abs(value)} ${unit} ago`;
  }
}

/** Merge consecutive on-points into `[{start, end, watts}]` run windows. */
function windows(points) {
  const found = [];
  let open = null;
  for (let i = 0; i < points.length; i++) {
    const on = points[i].v > 0;
    if (on && !open) open = { start: points[i].t, end: points[i].t, watts: points[i].v };
    if (on && open) {
      open.end = i + 1 < points.length ? points[i + 1].t : points[i].t;
      open.watts = Math.max(open.watts, points[i].v);
    }
    if (!on && open) {
      found.push(open);
      open = null;
    }
  }
  if (open) found.push(open);
  return found;
}

/**
 * Trim a stepped series to a window, keeping the value in force at its start.
 *
 * Dropping the points outside the window is not enough: the value that applies
 * at the left edge is usually carried by a point *before* it -- a price
 * published at 09:00 is what 09:30 costs -- so that point is kept and moved to
 * the edge. Without it a window opening mid-interval starts blank.
 */
function clipSeries(points, from, to) {
  const kept = [];
  for (let i = 0; i < points.length; i++) {
    if (points[i].t >= to) break;
    const next = i + 1 < points.length ? points[i + 1].t : Infinity;
    if (next <= from) continue;
    kept.push({ t: Math.max(points[i].t, from), v: points[i].v });
  }
  return kept;
}

/**
 * One entity out of a `history/history_during_period` answer, as points.
 *
 * The compressed form is what that call returns with `minimal_response`: `s`
 * for the state and `lu` for the time it was set, in seconds. Non-numeric
 * states -- `unavailable` while an inverter reboots -- are dropped rather than
 * read as zero, which would draw a dead inverter as a battery at rest.
 */
function recordedSeries(result, entityId, invert) {
  const rows = result && entityId ? result[entityId] : null;
  if (!Array.isArray(rows)) return [];
  const points = [];
  for (const row of rows) {
    const value = Number(row.s !== undefined ? row.s : row.state);
    // `lu` on a compressed row, `lc` when only the changed-at survived, and
    // the spelled-out key if the response was not compressed at all.
    let seconds = row.lu !== undefined ? row.lu : row.lc;
    if (seconds === undefined) seconds = row.last_updated;
    if (!Number.isFinite(value) || !Number.isFinite(Number(seconds))) continue;
    points.push({ t: Number(seconds) * 1000, v: invert ? -value : value });
  }
  return points.sort((a, b) => a.t - b.t);
}

/**
 * What a set of entities actually did, from the recorder, cached on the card.
 *
 * The plan is no use behind the present: an MPC run starts at the timestep it
 * was made in, so the moment a card's window reaches into the past every lane
 * would start with blank rail. The recorder has the answer, and it is one
 * call for all of them.
 *
 * Fetched at most once a minute, and never on the `hass` object alone: Home
 * Assistant sends a new one on every state change in the house, and a
 * websocket round trip per doorbell press is not a chart, it is a leak. The
 * answer is stashed on the card and returned synchronously; the fetch calls
 * `card.refresh()` when it lands.
 *
 * `wanted` maps a name to an entity id, `invert` names the ones whose sensor
 * is positive while charging. Ids are deduplicated, since two names pointed at
 * one sensor is a configuration a user can and does write.
 */
function readHistory(card, hass, wanted, invert, span, now) {
  const names = Object.keys(wanted);
  const empty = {};
  for (const name of names) empty[name] = [];
  if (!span || typeof hass.callWS !== "function") return card._historyPoints || empty;

  const ids = [];
  for (const name of names) {
    if (wanted[name] && ids.indexOf(wanted[name]) === -1) ids.push(wanted[name]);
  }
  if (!ids.length) return card._historyPoints || empty;

  const key = `${ids.join("|")}|${span}`;
  const fresh = card._historyAt && now - card._historyAt < 60000 && card._historyKey === key;
  if (!fresh) {
    card._historyAt = now;
    card._historyKey = key;
    hass
      .callWS({
        type: "history/history_during_period",
        start_time: new Date(now - span).toISOString(),
        end_time: new Date(now).toISOString(),
        entity_ids: ids,
        minimal_response: true,
        no_attributes: true,
        significant_changes_only: false,
      })
      .then((result) => {
        const points = {};
        for (const name of names) {
          points[name] = recordedSeries(result, wanted[name], invert.indexOf(name) !== -1);
        }
        card._historyPoints = points;
        card.refresh();
      })
      .catch(() => {
        // A sensor excluded from the recorder is a configuration choice, not
        // an error worth a broken card: the lanes simply start at now.
        card._historyPoints = empty;
      });
  }
  return card._historyPoints || empty;
}

/**
 * Recorded history in front of a planned series.
 *
 * Only the part strictly before the plan starts is taken from history: where
 * the two overlap the plan is the better answer, since it is the series every
 * other lane is drawn from.
 */
function mergeHistory(history, plan) {
  if (!plan.length) return history.slice();
  const cut = plan[0].t;
  return history.filter((point) => point.t < cut).concat(plan);
}

/**
 * The energy under a stepped power series, split by sign.
 *
 * Each point holds until the next one, which is what a plan actually says --
 * these are interval decisions, not samples of a curve, so a trapezoid would
 * invent a ramp the optimiser never planned. The last point is held until
 * `endT`, since the series carries no end of its own.
 */
function integrate(points, endT) {
  let up = 0;
  let down = 0;
  let peak = 0;
  for (let i = 0; i < points.length; i++) {
    const end = i + 1 < points.length ? points[i + 1].t : endT;
    const hours = (end - points[i].t) / 3600000;
    peak = Math.max(peak, Math.abs(points[i].v));
    if (!Number.isFinite(hours) || hours <= 0) continue;
    const kwh = (points[i].v * hours) / 1000;
    if (kwh > 0) up += kwh;
    else down -= kwh;
  }
  return { up, down, net: up - down, peak };
}

/* --------------------------------------------------------------- discovery */

/**
 * The hub's own entities, keyed by `domain.translation_key`.
 *
 * The domain is part of the key here, unlike in the shipping bundle: this
 * integration publishes `solar_surplus` twice, once as a sensor (watts) and
 * once as a binary sensor (is there any), and a flat map silently keeps
 * whichever the registry happened to iterate last.
 */
function findHub(hass) {
  const loadDevices = new Set(findLoads(hass).map((load) => load.id));
  const found = {};
  const registry = hass.entities || {};
  for (const entityId of Object.keys(registry)) {
    const entry = registry[entityId];
    if (entry.platform !== PLATFORM) continue;
    if (entry.device_id && loadDevices.has(entry.device_id)) continue;
    if (!entry.translation_key) continue;
    found[`${entityId.split(".")[0]}.${entry.translation_key}`] = entityId;
  }
  return found;
}

/**
 * This integration's entities grouped by device, one device per load.
 *
 * Keyed on translation_key rather than entity id or unique_id: an entity id is
 * built from the *translated* name and changes when a user renames it, and
 * unique_id is not in the display registry the frontend is sent at all.
 */
function findLoads(hass) {
  const devices = hass.devices || {};
  const registry = hass.entities || {};
  const loads = new Map();

  for (const entityId of Object.keys(registry)) {
    const entry = registry[entityId];
    if (entry.platform !== PLATFORM || !entry.device_id) continue;
    const device = devices[entry.device_id];
    if (!device) continue;
    const name = device.name_by_user || device.name || "";
    if (!loads.has(entry.device_id)) {
      loads.set(entry.device_id, { id: entry.device_id, name, entities: {} });
    }
    if (entry.translation_key) {
      loads.get(entry.device_id).entities[entry.translation_key] = entityId;
    }
  }

  // Only a per-load device carries should_run; the hub has binary sensors of
  // its own, so matching on the domain alone would count it as a load.
  return [...loads.values()].filter((load) => "should_run" in load.entities);
}

/* ----------------------------------------------------------------- actions */

function stateOf(hass, entityId) {
  return entityId ? hass.states[entityId] : undefined;
}

/**
 * The sensor the Companion was told measures what one of its own sensors plans.
 *
 * A card cannot read the integration's configuration -- `findHub` discovers its
 * entities and nothing else -- so each planned sensor carries a pointer to its
 * measured counterpart in its own attributes. That is what lets one answer, in
 * the integration's settings, serve every card at once. Before this, the same
 * battery power sensor had to be named on the plan card, the overview card and
 * the status card, under two different option names, with its sign convention
 * declared separately on each of the two that asked at all.
 *
 * Returns null when nothing is configured, and `invert` is a boolean only when
 * the convention is actually known -- see the callers, which say direction only
 * then.
 */
function measuredBy(hass, hub, plannedKey) {
  const planned = stateOf(hass, hub ? hub[plannedKey] : null);
  const attrs = planned && planned.attributes ? planned.attributes : null;
  if (!attrs || !attrs.measured_entity) return null;
  return { entity: attrs.measured_entity, invert: attrs.measured_invert === true };
}

function callService(hass, domain, service, data) {
  return hass.callService(domain, service, data);
}

function pressButton(hass, entityId) {
  return callService(hass, "button", "press", { entity_id: entityId });
}

/** Open Home Assistant's own dialog for an entity. */
function moreInfo(node, entityId) {
  if (!entityId) return;
  node.dispatchEvent(
    new CustomEvent("hass-more-info", {
      detail: { entityId },
      bubbles: true,
      composed: true,
    }),
  );
}

function haptic(kind) {
  window.dispatchEvent(new CustomEvent("haptic", { detail: kind || "light" }));
}

/**
 * A state's translated label, from Home Assistant itself where possible.
 *
 * The alternative is a hard-coded map in this file, which then has to be kept
 * in step with strings.json and gets no translations at all.
 */
function labelFor(hass, stateObj, value) {
  if (!stateObj) return "–";
  const wanted = value === undefined ? stateObj.state : value;
  if (typeof hass.formatEntityState === "function") {
    try {
      return hass.formatEntityState(stateObj, wanted);
    } catch (err) {
      /* fall through to the raw state */
    }
  }
  return String(wanted).replace(/_/g, " ");
}

/* -------------------------------------------------------------- shared css */

/*
 * One token block for every card in this bundle. Values are Home Assistant's
 * own theme variables wherever one exists, so these follow the active theme
 * rather than inventing a second palette that only matches the default one.
 *
 * Each colour that uses color-mix() declares a flat rgba first: a browser
 * without color-mix drops the second declaration and keeps a usable grey
 * instead of rendering nothing.
 */
const TOKENS = `
  :host {
    display: block;
    --emh-radius: 14px;
    --emh-gap: 10px;
    --emh-ease: cubic-bezier(.2, 0, 0, 1);
    --emh-accent: var(--primary-color, #03a9f4);
    --emh-ok: var(--success-color, #43a047);
    --emh-warn: var(--warning-color, #ffa600);
    --emh-bad: var(--error-color, #db4437);
    --emh-solar: var(--energy-solar-color, #ff9800);
    --emh-grid: var(--energy-grid-consumption-color, #488fc2);
    --emh-battery: var(--energy-battery-out-color, #4db6ac);
    --emh-dim: var(--secondary-text-color, #727272);
    --emh-surface: rgba(127, 127, 127, .10);
    --emh-surface: color-mix(in srgb, var(--primary-text-color) 7%, transparent);
    --emh-surface-2: rgba(127, 127, 127, .18);
    --emh-surface-2: color-mix(in srgb, var(--primary-text-color) 13%, transparent);
    --emh-hairline: var(--divider-color, rgba(127, 127, 127, .3));
  }
  * { box-sizing: border-box; }
  ha-card { overflow: hidden; }
  .pad { padding: 14px 16px 16px 16px; }

  /* --- header ------------------------------------------------------- */
  .head { display: flex; align-items: center; gap: 12px; }
  .head .grow { flex: 1; min-width: 0; }
  .name { font-size: 1.05rem; font-weight: 500; line-height: 1.25;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub { font-size: .78rem; color: var(--emh-dim); margin-top: 2px;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chip-id { font-size: .68rem; font-weight: 500; letter-spacing: .02em;
             padding: 2px 6px; border-radius: 6px; background: var(--emh-surface);
             color: var(--emh-dim); margin-left: 6px; vertical-align: 1px; }

  /* --- icon squircle, tinted by state ------------------------------- */
  .sq { width: 40px; height: 40px; border-radius: 13px; flex: 0 0 auto;
        display: grid; place-items: center; color: var(--emh-dim);
        background: var(--emh-surface); transition: background 240ms var(--emh-ease),
        color 240ms var(--emh-ease); cursor: pointer; }
  .sq ha-icon { --mdc-icon-size: 22px; }
  .sq.on { color: var(--emh-ok); background: rgba(67, 160, 71, .18);
           background: color-mix(in srgb, var(--emh-ok) 18%, transparent); }
  .sq.wait { color: var(--emh-accent); background: rgba(3, 169, 244, .18);
             background: color-mix(in srgb, var(--emh-accent) 18%, transparent); }
  .sq.warn { color: var(--emh-warn); background: rgba(255, 166, 0, .18);
             background: color-mix(in srgb, var(--emh-warn) 18%, transparent); }
  .sq.bad { color: var(--emh-bad); background: rgba(219, 68, 55, .18);
            background: color-mix(in srgb, var(--emh-bad) 18%, transparent); }

  /* Running is the one state worth animating: it is the only one that is
     changing while you look at it. */
  .sq.pulse::after { content: ""; position: absolute; }
  .pulsing { animation: emh-pulse 2.4s ease-in-out infinite; }
  @keyframes emh-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(67, 160, 71, .40); }
    70% { box-shadow: 0 0 0 10px rgba(67, 160, 71, 0); }
  }

  /* --- status pill --------------------------------------------------- */
  .pill { display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto;
          font-size: .76rem; font-weight: 500; padding: 4px 10px 4px 8px;
          border-radius: 999px; background: var(--emh-surface);
          color: var(--emh-dim); }
  .pill .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  .pill.on { color: var(--emh-ok); background: rgba(67, 160, 71, .16);
             background: color-mix(in srgb, var(--emh-ok) 16%, transparent); }
  .pill.wait { color: var(--emh-accent); background: rgba(3, 169, 244, .16);
               background: color-mix(in srgb, var(--emh-accent) 16%, transparent); }
  .pill.warn { color: var(--emh-warn); background: rgba(255, 166, 0, .16);
               background: color-mix(in srgb, var(--emh-warn) 16%, transparent); }
  .pill.bad { color: var(--emh-bad); background: rgba(219, 68, 55, .16);
              background: color-mix(in srgb, var(--emh-bad) 16%, transparent); }

  /* --- stat tiles ---------------------------------------------------- */
  .stats { display: grid; gap: 8px; margin-top: 12px;
           grid-template-columns: repeat(auto-fit, minmax(84px, 1fr)); }
  .stat { background: var(--emh-surface); border-radius: 11px; padding: 8px 10px;
          min-width: 0; }
  .stat .k { font-size: .68rem; color: var(--emh-dim); text-transform: uppercase;
             letter-spacing: .04em; white-space: nowrap; overflow: hidden;
             text-overflow: ellipsis; }
  .stat .v { font-size: 1.02rem; font-weight: 500; margin-top: 2px;
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stat .v small { font-size: .7rem; font-weight: 400; color: var(--emh-dim); }
  /* The second line a chosen box carries: most of what can go in one is a
     number that means nothing without what it is measured against ("limit
     30 m", "charging", "2 runs"). */
  .stat .s { font-size: .68rem; color: var(--emh-dim); margin-top: 1px;
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stat.tap { cursor: pointer; transition: background 160ms; }
  .stat.tap:hover { background: var(--emh-surface-2); }

  /* --- control rows --------------------------------------------------- */
  .row { display: flex; align-items: center; gap: 12px; padding: 7px 0; }
  .row + .row { border-top: 1px solid var(--emh-hairline); }
  .row .label { flex: 1; min-width: 0; font-size: .9rem; }
  .row .label small { display: block; font-size: .72rem; color: var(--emh-dim); }
  .row.disabled { opacity: .45; pointer-events: none; }

  .tgl { position: relative; flex: 0 0 auto; width: 46px; height: 28px; border: 0;
         border-radius: 999px; background: var(--emh-surface-2); cursor: pointer;
         padding: 0; transition: background 220ms var(--emh-ease); }
  .tgl::after { content: ""; position: absolute; top: 3px; left: 3px;
                width: 22px; height: 22px; border-radius: 50%; background: #fff;
                box-shadow: 0 1px 3px rgba(0, 0, 0, .3);
                transition: transform 260ms var(--emh-ease); }
  .tgl[aria-checked="true"] { background: var(--emh-accent); }
  .tgl[aria-checked="true"]::after { transform: translateX(18px); }

  .btn { appearance: none; border: 0; font: inherit; font-size: .82rem;
         font-weight: 500; padding: 8px 14px; border-radius: 10px; cursor: pointer;
         background: var(--emh-surface-2); color: var(--primary-text-color);
         transition: transform 120ms var(--emh-ease), background 200ms; }
  .btn:active { transform: scale(.95); }
  .btn.primary { background: var(--emh-accent); color: var(--text-primary-color, #fff); }
  .btn.wide { width: 100%; }
  .btn[disabled] { opacity: .4; cursor: default; }

  input[type="range"] { flex: 1 1 90px; min-width: 60px; accent-color: var(--emh-accent);
                        height: 22px; }
  .val { flex: 0 0 auto; font-size: .84rem; font-variant-numeric: tabular-nums;
         min-width: 52px; text-align: right; color: var(--primary-text-color); }

  /* --- key/value grid -------------------------------------------------- */
  .kv { display: grid; grid-template-columns: auto 1fr; gap: 6px 14px;
        font-size: .82rem; }
  .kv dt { color: var(--emh-dim); white-space: nowrap; }
  .kv dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums;
           overflow: hidden; text-overflow: ellipsis; }

  .empty { color: var(--emh-dim); font-size: .88rem; padding: 8px 0; }
  .hint { color: var(--emh-dim); font-size: .74rem; margin-top: 8px; }
  svg { display: block; width: 100%; }
`;

/* ------------------------------------------------------------ timeline svg */

/**
 * A darker rail for the part of a lane that has already happened.
 *
 * Once a lane carries history as well as plan, the now marker alone is not
 * enough: it is a single line, and the eye reads the whole lane as one kind of
 * thing. Shading the past says which half is a record and which is an
 * intention, before anything is drawn on top.
 */
function pastBand(root, until, x, t0, height) {
  if (!Number.isFinite(until) || until <= t0) return;
  svg("rect", {
    x: 0, y: 0, width: Math.max(x(until), 0), height,
    fill: "var(--emh-surface-2)", "fill-opacity": 0.6,
  }, root);
}

/**
 * A horizontal run-plan track: rail, scheduled blocks, now marker, deadline.
 *
 * Drawn in a viewBox of 1000 x `height` with preserveAspectRatio off, so the
 * track stretches to whatever width the card ends up at without the caller
 * having to know it. Times are mapped from the series' own extent rather than
 * a fixed 24 h, because the plan's horizon is a setting.
 */
function trackSvg(options) {
  const points = options.points || [];
  const height = options.height || 30;
  const labels = options.labels !== false;
  const totalH = labels ? height + 14 : height;
  const root = svg("svg", {
    viewBox: `0 0 1000 ${totalH}`,
    preserveAspectRatio: "none",
    role: "img",
  });
  // With preserveAspectRatio off, an SVG at width:100% and height:auto would
  // scale its height with the card's width -- a 1000-unit-wide viewBox at
  // 350 px renders 10 px tall. The height is a design decision here, not a
  // consequence of the width, so it is pinned.
  root.style.height = `${totalH}px`;

  const hasPoints = points.length > 1;
  const t0 = options.from !== undefined ? options.from : hasPoints ? points[0].t : NaN;
  const t1 =
    options.to !== undefined ? options.to : hasPoints ? points[points.length - 1].t : NaN;
  if (!Number.isFinite(t0) || !Number.isFinite(t1)) return root;
  const x = (t) => ((t - t0) / (t1 - t0 || 1)) * 1000;
  const color = options.color || "var(--emh-accent)";

  // The rail is drawn even for a load with nothing scheduled: an empty lane
  // in a row of full ones has to read as "this one is not running", not as a
  // lane that failed to render.
  svg("rect", {
    x: 0, y: 0, width: 1000, height,
    rx: Math.min(7, height / 2), fill: "var(--emh-surface)",
  }, root);
  pastBand(root, options.past, x, t0, height);

  // Hour ticks, drawn on the rail rather than under it: they are a reading
  // aid for the blocks, not an axis in their own right.
  const hour = 3600000;
  const step = (t1 - t0) / hour > 30 ? 6 * hour : 3 * hour;
  const first = Math.ceil(t0 / step) * step;
  for (let t = first; t < t1; t += step) {
    svg("line", {
      x1: x(t), x2: x(t), y1: 0, y2: height,
      stroke: "var(--emh-hairline)", "stroke-width": 1,
    }, root);
    if (labels) {
      svg("text", {
        x: x(t), y: totalH - 2, fill: "var(--emh-dim)",
        "font-size": 10, "text-anchor": "middle",
      }, root).textContent = formatHour(t, options.hass);
    }
  }

  for (const run of windows(points)) {
    const left = x(run.start);
    const width = Math.max(x(run.end) - left, 2);
    svg("rect", {
      x: left, y: 0, width, height,
      rx: Math.min(6, height / 2), fill: color, "fill-opacity": 0.85,
    }, root);
  }

  if (options.deadline && options.deadline >= t0 && options.deadline <= t1) {
    svg("line", {
      x1: x(options.deadline), x2: x(options.deadline), y1: -2, y2: height + 2,
      stroke: "var(--emh-warn)", "stroke-width": 2, "stroke-dasharray": "3 3",
    }, root);
  }

  const now = Date.now();
  if (now >= t0 && now <= t1) {
    svg("line", {
      x1: x(now), x2: x(now), y1: -1, y2: height + 1,
      stroke: "var(--primary-text-color)", "stroke-width": 2,
    }, root);
    svg("circle", {
      cx: x(now), cy: 0, r: 3, fill: "var(--primary-text-color)",
    }, root);
  }
  return root;
}

/* ---------------------------------------------------------- load view model */

/**
 * Everything the deferrable cards show, derived once per render.
 *
 * Kept in one place because two cards present the same load in two ways;
 * without it, each one grows its own slightly different idea of what "waiting"
 * means, and they start disagreeing on the same dashboard.
 */
function loadView(hass, load) {
  const find = (key) => stateOf(hass, load.entities[key]);
  const shouldRun = find("should_run");
  const running = find("running");
  const scheduled = find("scheduled_power");
  const nextStart = find("next_start");
  const runtime = find("runtime_today");
  const recurrence = find("recurrence");
  const requested = find("load_requested");
  const enabled = find("load_enabled");
  const hours = find("operating_hours");

  const mode = recurrence ? recurrence.state : "daily";
  const onDemand = mode === "on_demand";
  const onSurplus = mode === "surplus";

  const deadlineRaw =
    requested && requested.attributes ? requested.attributes.deadline_at : null;
  const deadline = deadlineRaw ? Date.parse(deadlineRaw) : NaN;

  const points = series(scheduled, "schedule");
  const runs = windows(points);
  const ranToday = num(runtime);
  const needed = onSurplus ? num(find("surplus_budget")) : num(hours);

  const slot =
    shouldRun && shouldRun.attributes ? shouldRun.attributes.emhass_deferrable : null;

  // A run window that has not started yet. The plan's own series is what says
  // so, rather than next_start: that sensor reports a reason instead of a time
  // in exactly the cases worth distinguishing, and a load can be scheduled
  // later today while its next start is unavailable for want of a fresh run.
  const upcoming = runs.filter((run) => run.end > Date.now());
  const nextRun = upcoming.length ? upcoming[0] : null;

  // "Planned" sits between running and idle, and it is the state a deferrable
  // load spends most of its day in: nothing to do now, but something booked.
  // Folding it into "idle" was the card saying "nothing is happening" about a
  // load that is two hours from starting.
  let status = "unknown";
  if (running && running.state === "on") status = "running";
  else if (shouldRun && shouldRun.state === "on") status = "should";
  else if (shouldRun && shouldRun.state === "off") status = nextRun ? "planned" : "idle";

  const NEXT_START_REASONS = {
    no_plan: "No plan yet",
    already_running: "Running now",
    not_scheduled: "Not scheduled",
  };
  let next = "–";
  let nextMs = NaN;
  if (isUsable(nextStart)) {
    nextMs = Date.parse(nextStart.state);
    next = formatTime(nextMs, hass);
  } else if (nextStart && nextStart.attributes && nextStart.attributes.reason) {
    next = NEXT_START_REASONS[nextStart.attributes.reason] || nextStart.attributes.reason;
  }

  return {
    find,
    name: load.name,
    slot,
    status,
    shouldRun,
    running,
    scheduled,
    scheduledW: num(scheduled),
    nextStart,
    next,
    nextMs,
    runtime,
    ranToday,
    needed,
    recurrence,
    mode,
    onDemand,
    onSurplus,
    requested,
    isRequested: Boolean(requested) && requested.state === "on",
    enabled,
    isEnabled: !enabled || enabled.state === "on",
    deadline: Number.isFinite(deadline) ? deadline : null,
    points,
    runs,
    upcoming,
    nextRun,
    reason: shouldRun && shouldRun.attributes ? shouldRun.attributes.reason : null,
  };
}

/* ------------------------------------------------------------- base class */

/**
 * Shared plumbing: shadow root built once, updated in place afterwards.
 *
 * The shipping cards rebuild their markup on every `hass` update, which is
 * fine for a chart and impossible for a control: a rebuild during a drag
 * throws away the element under the finger, and a re-render on every state
 * change in the house would do it several times a second. Subclasses
 * implement `build()` once and `update()` on each state change.
 */
class LiveCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    // The card editor calls setConfig on the same element for every keystroke,
    // and a second attachShadow throws NotSupportedError -- which red-cards
    // the whole element. So the root is reused and only the contents rebuilt.
    this._built = false;
    if (this.shadowRoot) this.shadowRoot.innerHTML = "";
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._built) {
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      this._buildRoot();
      this._built = true;
    }
    this.update(hass, first);
  }

  get hass() {
    return this._hass;
  }

  _buildRoot() {
    const style = document.createElement("style");
    style.textContent = TOKENS + (this.constructor.css || "");
    this.shadowRoot.appendChild(style);
    const card = document.createElement("ha-card");
    this.shadowRoot.appendChild(card);
    this._card = card;
    this.build(card);
  }

  /** Redraw with the state already in hand, for an answer that arrived late. */
  refresh() {
    if (this._built && this._hass) this.update(this._hass, false);
  }

  /** A minute ticker, for the cards whose text is a countdown. */
  connectedCallback() {
    if (this.constructor.ticks) {
      this._timer = window.setInterval(() => {
        if (this._hass && this._built) this.update(this._hass, false);
      }, 30000);
    }
  }

  disconnectedCallback() {
    if (this._timer) window.clearInterval(this._timer);
    this._timer = null;
  }

  build() {}

  update() {}
}

/** A stat tile whose value is replaced in place. */
function statTile(parent, key) {
  const root = tag("div", "stat", parent);
  const label = tag("div", "k", root, key);
  const value = tag("div", "v", root, "–");
  root.set = (text, newKey) => {
    value.textContent = text;
    if (newKey) label.textContent = newKey;
  };
  root.setKey = (text) => {
    label.textContent = text;
  };
  return root;
}

// Enough of the past to see what the plan has just done, and not so much that
// it costs the horizon its width. The plan card gets more of it than the
// overview card: it is the one place someone goes to read the chart in
// detail, where the overview card's job is a glance.
const DEFAULT_PLAN_HISTORY_HOURS = 4;
const DEFAULT_HISTORY_HOURS = 2;

/* ------------------------------------------- info card 2: house status */

/** A stat tile with a second line, for sections that are read-only status. */
function valueBox(parent, key, tooltip) {
  const root = tag("div", "stat", parent);
  if (tooltip) root.title = tooltip;
  tag("div", "k", root, key);
  const value = tag("div", "v", root, "–");
  const sub = tag("div", "s", root, "");
  root.set = (text, detail) => {
    value.textContent = text === undefined || text === null || text === "" ? "–" : text;
    sub.textContent = detail === undefined || detail === null ? "" : detail;
    sub.style.display = sub.textContent ? "" : "none";
  };
  return root;
}

/**
 * Sections are on unless the config says otherwise.
 *
 * Defaulting to on rather than off keeps an existing `type:`-only card looking
 * exactly as it did, and means the YAML only ever carries what someone has
 * actually turned off.
 */
function showsSection(config, key) {
  const value = config ? config[key] : undefined;
  return value === undefined ? true : value !== false;
}

/**
 * Home Assistant's own form widgets, on demand.
 *
 * `ha-form` is not exported anywhere a custom card can import it from; it is
 * pulled in by whichever built-in card editor the frontend loads first. On a
 * dashboard where none has been opened yet it is simply not defined, and an
 * editor built on it renders as an empty box. Creating a built-in card and
 * asking it for its own editor is the sanctioned way to force the load.
 *
 * Custom elements upgrade in place, so the properties this file sets before
 * the definition lands are picked up when it does.
 */
let haFormPromise = null;

function loadHaForm() {
  if (haFormPromise) return haFormPromise;
  haFormPromise = (async () => {
    if (customElements.get("ha-form")) return;
    if (typeof window.loadCardHelpers === "function") {
      const helpers = await window.loadCardHelpers();
      const card = await helpers.createCardElement({ type: "entities", entities: [] });
      if (card && card.constructor.getConfigElement) await card.constructor.getConfigElement();
    }
    await customElements.whenDefined("ha-form");
  })();
  return haFormPromise;
}

/**
 * The plumbing every card editor here shares.
 *
 * Light DOM rather than a shadow root: the editor is rendered inside Home
 * Assistant's own dialog, and `ha-form` expects to inherit that dialog's
 * typography and spacing rather than be sealed off from it.
 *
 * A subclass supplies `labels`, `helpers`, `schema(hass)`, `data()` and
 * `clean(config)`. The last is the one that matters: the form always has a
 * value for every switch and every dropdown, and writing all of them back
 * would leave a wall of `show_x: true` in the YAML that says nothing. Only
 * what differs from the default is a decision worth recording.
 */
class CardEditor extends HTMLElement {
  setConfig(config) {
    this._config = Object.assign({}, config);
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  connectedCallback() {
    loadHaForm().then(() => this._render());
    this._render();
  }

  _render() {
    if (!this._config || !this._hass) return;
    if (!this._form) {
      const form = document.createElement("ha-form");
      form.computeLabel = (schema) => this.labels[schema.name] || schema.name;
      form.computeHelper = (schema) => this.helpers[schema.name] || "";
      form.addEventListener("value-changed", (event) => this._commit(event.detail.value));
      this.appendChild(form);
      this._form = form;
    }
    this._form.hass = this._hass;
    // Rebuilt per render rather than held as a constant, because two of these
    // editors offer a dropdown of the loads that exist right now.
    this._form.schema = this.schema(this._hass);
    this._form.data = this.data();
  }

  _commit(value) {
    const config = this.clean(Object.assign({}, this._config, value));
    this._config = config;
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config },
        bubbles: true,
        composed: true,
      }),
    );
  }

  get labels() {
    return {};
  }

  get helpers() {
    return {};
  }

  data() {
    return {};
  }

  clean(config) {
    return config;
  }
}

/** Drop the section switches that are simply at their default. */
function cleanSections(config, sections, offByDefault) {
  for (const section of sections) {
    const key = section[0];
    const on = !(offByDefault && offByDefault.has(key));
    if (config[key] === on) delete config[key];
  }
  return config;
}

/** Every section of a card, as one grid of switches. */
function sectionGrid(sections) {
  return {
    name: "",
    type: "grid",
    column_min_width: "220px",
    schema: sections.map((section) => ({ name: section[0], selector: { boolean: {} } })),
  };
}


/* ------------------------------------------------------------------ exports */
/**
 * What the card bundles are allowed to reach for.
 *
 * Listed in one place rather than as `export` on each declaration, so that the
 * shared surface can be read off in one screen -- and so that adding an export
 * is a deliberate line here rather than a keyword that quietly widens it.
 */
export {
  callService,
  CardEditor,
  cleanSections,
  clipSeries,
  COLORS,
  DEFAULT_HISTORY_HOURS,
  DEFAULT_PLAN_HISTORY_HOURS,
  findHub,
  findLoads,
  formatAgo,
  formatCountdown,
  formatEnergy,
  formatHours,
  formatPower,
  formatSpan,
  formatTime,
  haptic,
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
  pastBand,
  pressButton,
  readHistory,
  sectionGrid,
  series,
  showsSection,
  stateOf,
  statTile,
  svg,
  tag,
  trackSvg,
  valueBox,
};
