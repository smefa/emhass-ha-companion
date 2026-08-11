/**
 * EMHASS Companion dashboard cards.
 *
 * Deliberately plain custom elements with inline SVG: no Lit, no charting
 * library, no build step. A bundler would mean an npm toolchain in CI, a
 * committed artefact that can drift from its source, and Node as a
 * prerequisite for contributors -- none of which these cards justify. It also
 * means nothing is fetched at runtime, so the cards work on an isolated
 * network and cannot be broken by a CDN.
 *
 * ES2017 syntax only -- no optional chaining and no nullish coalescing, both
 * of which defeat the pure-Python syntax check in tests/test_packaging.py,
 * which is the only automated checking this file gets at all.
 *
 * Colours come from Home Assistant's own theme variables wherever one exists,
 * so every card follows the active theme rather than inventing a second
 * palette that only matches the default one.
 *
 * Two generations of card live here. The plan and deferrable cards rebuild
 * their markup on every `hass` update, which is fine for a chart; the five
 * that came later extend `LiveCard`, build their DOM once and update it in
 * place, because a rebuild during a drag throws away the element under the
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

/**
 * The same fact as `formatAgo`, in a stat tile's width.
 *
 * `Intl.RelativeTimeFormat` writes sentences -- "3 minutes ago", "1 hour ago"
 * -- and a tile 84 px wide renders that as "3 minutes a…". The unit is
 * abbreviated and the "ago" dropped, because the tile it is used on already
 * carries the wall-clock time on its second line, which is what "ago" would
 * otherwise be needed to disambiguate.
 */
function formatAgoShort(ms) {
  if (!Number.isFinite(ms)) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h`;
  return `${Math.round(hours / 24)} d`;
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

function resolveLoad(hass, wanted) {
  const loads = findLoads(hass);
  if (!loads.length) return null;
  if (!wanted) return loads[0];
  const needle = String(wanted).toLowerCase();
  return (
    loads.find((load) => load.name.toLowerCase() === needle || load.id === wanted) || null
  );
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

function toggleEntity(hass, entityId) {
  const domain = entityId.split(".")[0];
  return callService(hass, domain, "toggle", { entity_id: entityId });
}

function pressButton(hass, entityId) {
  return callService(hass, "button", "press", { entity_id: entityId });
}

function setNumber(hass, entityId, value) {
  return callService(hass, "number", "set_value", { entity_id: entityId, value });
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

// Enough of the past to see what the plan has just done, and not so much that
// it costs the horizon its width. The plan card gets more of it than the
// overview card: it is the one place someone goes to read the chart in
// detail, where the overview card's job is a glance.
const DEFAULT_PLAN_HISTORY_HOURS = 4;
const DEFAULT_HISTORY_HOURS = 2;

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

/* ------------------------------------------- info card 2: house status */

/** A stat tile with a second line, for sections that are read-only status. */
function valueBox(parent, key) {
  const root = tag("div", "stat", parent);
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
 * The planned battery level, with where the battery actually is on it.
 *
 * A lone fill answers "how full is it", which is the one thing a battery
 * owner can already read off their inverter. What a *plan* bar is for is the
 * comparison, so this draws three things on one rail: the level the plan has
 * for right now (the fill), the peak the plan is steering towards (a lighter
 * band carrying on from it), and the measured level as a marker. A visible
 * gap between the marker and the fill is a plan running on a stale SOC --
 * which is invisible on any single-value display.
 */
function socBar(parent, label) {
  const root = tag("div", "soc", parent);
  const head = tag("div", "meter-head", root);
  tag("span", null, head, label);
  const value = tag("span", "meter-value", head, "");
  const rail = tag("div", "rail", root);
  const reach = tag("div", "reach", rail);
  const fill = tag("div", "fill", rail);
  const mark = tag("div", "mark", rail);
  const key = tag("div", "soc-key", root);

  const legend = (swatch) => {
    const item = tag("span", "leg", key);
    tag("i", `sw ${swatch}`, item);
    const text = tag("span", null, item, "");
    item.set = (content) => {
      text.textContent = content || "";
      item.style.display = content ? "" : "none";
    };
    return item;
  };
  const legNow = legend("now");
  const legPlan = legend("plan");
  const legPeak = legend("peak");

  const clamp = (value) => Math.max(0, Math.min(100, value));

  root.set = (info) => {
    const planned = info.planned;
    const peak = info.peak;
    const measured = info.measured;
    const hasPlan = Number.isFinite(planned);

    fill.style.width = hasPlan ? `${clamp(planned)}%` : "0%";
    // The band only means anything as *headroom beyond the fill*; without a
    // plan there is nothing for it to extend from.
    reach.style.width = hasPlan && Number.isFinite(peak) ? `${clamp(peak)}%` : "0%";
    fill.style.background = hasPlan && planned < 20 ? "var(--emh-bad)" : "var(--emh-battery)";

    if (Number.isFinite(measured)) {
      mark.style.left = `${clamp(measured)}%`;
      mark.style.display = "";
    } else {
      mark.style.display = "none";
    }

    value.textContent = hasPlan ? `${planned.toFixed(0)} %` : "no plan";
    legNow.set(Number.isFinite(measured) ? `now ${measured.toFixed(0)} %` : "");
    legPlan.set(hasPlan ? `plan ${planned.toFixed(0)} %` : "");
    legPeak.set(
      Number.isFinite(peak)
        ? `peak ${peak.toFixed(0)} %${info.peakAt ? ` · ${info.peakAt}` : ""}`
        : "",
    );
  };
  return root;
}

/**
 * Blocks the status card is drawn from, in the order they appear.
 *
 * One list drives all three of them: what `build()` puts on the card, what the
 * visual editor offers, and which keys are worth writing into the YAML -- the
 * same arrangement the overview card uses, and for the same reason: a block
 * added here shows up in the editor without the editor being touched.
 */
const STATUS_SECTIONS = [
  ["show_banner", "Control banner", "Whether the Companion is actually in charge"],
  ["show_decision", "Battery decision", "What it decided, why, and at what power"],
  ["show_soc", "Planned battery level", "The plan's level, its peak, and the measured one"],
  ["show_battery", "Battery tiles", "The numbers under the rail"],
  ["show_system", "System tiles", "Mode, cost function, curtailment, loads, timing"],
  ["show_rules", "Why it decided that", "The executor's own rule trace"],
];

/** Every tile, as `[section, key, label, description]`. */
const STATUS_TILES = [
  ["show_battery", "show_level", "Level now", "Measured level, and its gap to the plan"],
  ["show_battery", "show_power", "Battery power", "What the plan has the battery doing now"],
  ["show_battery", "show_target", "End target", "The end-SOC target being steered for"],
  ["show_battery", "show_plan_end", "Plan ends at", "Where the plan actually lands, and when"],
  ["show_battery", "show_low", "Planned low", "The lowest level still to come"],
  ["show_battery", "show_high", "Planned high", "The highest level still to come"],
  ["show_system", "show_mode", "Mode", "The mode the Companion is running in"],
  ["show_system", "show_cost", "Optimising for", "The cost function EMHASS is solving"],
  ["show_system", "show_curtail", "Curtailment", "Whether export is capped, and at what"],
  ["show_system", "show_loads", "Loads on", "How many deferrable loads are switched on"],
  ["show_system", "show_optim", "Optim status", "How the last optimisation run ended"],
  ["show_system", "show_decided", "Decided", "How long ago the decision was taken"],
  ["show_system", "show_commands", "Commands", "How many service calls it resolved to"],
];

/**
 * The parts that are off until asked for.
 *
 * Everything else defaults to on, so a `type:`-only card keeps looking the way
 * it always did and the YAML only ever carries what someone turned off. The
 * command count is the exception: it answers a question ("what would actually
 * be sent") that only matters while an inverter profile is being set up, and
 * it was the least-read tile on the card for everyone else.
 */
const STATUS_OFF_BY_DEFAULT = new Set(["show_commands"]);

function showsStatusPart(config, key) {
  const value = config ? config[key] : undefined;
  if (value === undefined) return !STATUS_OFF_BY_DEFAULT.has(key);
  return value !== false;
}

/** Set a value box that the config may have removed from the card entirely. */
function setBox(box, value, detail) {
  if (box) box.set(value, detail);
}

/**
 * Tile-width words for the states a select publishes in sentence form.
 *
 * The tiles are 84 px wide at their narrowest, so "Maximize self-consumption"
 * arrives as "Maximize self-con…" -- which is not a value, it is a promise of
 * one. These are the same states, written to fit. Anything not listed keeps
 * the entity's own translated label.
 */
const SHORT_STATES = {
  profit: "Max profit",
  cost: "Min cost",
  "self-consumption": "Self-use",
  self_consume: "Self-use",
};

function shortLabel(hass, stateObj) {
  if (!stateObj) return "–";
  const short = SHORT_STATES[stateObj.state];
  return short ? short : labelFor(hass, stateObj);
}

/** How a run ended, in a tile's width. */
const OPTIM_STATUS_LABELS = { ok: "OK", "no-run": "Never run" };

/**
 * What the Companion is currently doing to the house -- read only.
 *
 * Deliberately has no switch on it. The dry-run gate and the mode are the two
 * settings most likely to be changed by accident from a phone in a pocket,
 * and a dashboard that is glanced at far more often than it is operated is
 * the wrong place to keep them. What is left is the question that actually
 * gets asked: is it in charge right now, and what is it doing with the
 * battery. Every value here is also a link into the entity behind it, so the
 * settings are one tap away rather than absent.
 *
 * Which of those blocks is drawn is a choice, made in the card's own visual
 * editor: the card answers several questions at once, and a dashboard that
 * only asks one of them should not have to carry the rest.
 */
class EmhassStatusCard extends LiveCard {
  static getStubConfig() {
    return { type: "custom:emhass-status-card" };
  }

  /** The visual editor, with Home Assistant's own form widgets pulled in. */
  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-status-card-editor");
  }

  getCardSize() {
    const shows = (key) => showsStatusPart(this._config, key);
    let size = 1;
    if (shows("show_banner")) size += 1;
    if (shows("show_decision")) size += 1;
    if (shows("show_soc")) size += 2;
    if (this._tilesIn("show_battery").length) size += 2;
    if (this._tilesIn("show_system").length) size += 2;
    return size;
  }

  /** The tiles of one section that the config leaves on -- section included. */
  _tilesIn(section) {
    if (!showsStatusPart(this._config, section)) return [];
    return STATUS_TILES.filter(
      (tile) => tile[0] === section && showsStatusPart(this._config, tile[1]),
    );
  }

  build(card) {
    const ui = {};
    this._ui = ui;
    // Boxes live in a map keyed by their config key, so `update` can write to
    // one without knowing whether it was built: `setBox` no-ops on undefined.
    ui.box = {};
    const pad = tag("div", "pad", card);
    const shows = (key) => showsStatusPart(this._config, key);

    if (shows("show_banner")) {
      ui.banner = tag("div", "banner", pad);
      const bannerIcon = tag("div", "sq", ui.banner);
      ui.bannerIconEl = document.createElement("ha-icon");
      bannerIcon.appendChild(ui.bannerIconEl);
      ui.bannerSq = bannerIcon;
      bannerIcon.addEventListener("click", () =>
        moreInfo(this, this._hub["switch.control_enabled"]),
      );
      const grow = tag("div", "grow", ui.banner);
      ui.bannerTitle = tag("div", "name", grow, "");
      ui.bannerSub = tag("div", "sub", grow, "");
      ui.bannerPill = tag("div", "pill", ui.banner);
      tag("span", "dot", ui.bannerPill);
      ui.bannerPillText = tag("span", null, ui.bannerPill, "");
    }

    const batteryTiles = this._tilesIn("show_battery");
    if (shows("show_decision") || shows("show_soc") || batteryTiles.length) {
      tag("div", "section", pad, "Battery");
    }

    if (shows("show_decision")) {
      const decision = tag("div", "decision", pad);
      ui.decisionIconWrap = tag("div", "sq big", decision);
      ui.decisionIcon = document.createElement("ha-icon");
      ui.decisionIconWrap.appendChild(ui.decisionIcon);
      ui.decisionIconWrap.addEventListener("click", () =>
        moreInfo(this, this._hub["sensor.battery_action"]),
      );
      const dgrow = tag("div", "grow", decision);
      ui.decisionText = tag("div", "decision-text", dgrow, "–");
      ui.decisionWhy = tag("div", "sub", dgrow, "");
      // Two lines rather than one: the number alone cannot say whether it is
      // the power that was commanded or the power flowing, and those are
      // different figures on exactly the decision that confuses people.
      const powerWrap = tag("div", "decision-power", decision);
      ui.decisionPower = tag("div", "dp-v", powerWrap, "");
      ui.decisionPowerNote = tag("div", "dp-c", powerWrap, "");
    }

    if (shows("show_soc")) ui.soc = socBar(pad, "Planned battery level");

    if (batteryTiles.length) {
      const stats = tag("div", "stats", pad);
      for (const tile of batteryTiles) ui.box[tile[1]] = valueBox(stats, tile[2]);
    }

    const systemTiles = this._tilesIn("show_system");
    if (systemTiles.length) {
      tag("div", "section", pad, "System");
      const stats = tag("div", "stats", pad);
      for (const tile of systemTiles) ui.box[tile[1]] = valueBox(stats, tile[2]);
    }

    ui.problems = tag("div", "problems", pad);
  }

  update(hass) {
    const ui = this._ui;
    this._hub = findHub(hass);
    const hub = this._hub;
    const control = stateOf(hass, hub["switch.control_enabled"]);
    const action = stateOf(hass, hub["sensor.battery_action"]);
    const mode = stateOf(hass, hub["select.system_mode"]);
    const cost = stateOf(hass, hub["select.cost_fun"]);
    const soc = stateOf(hass, hub["sensor.battery_soc"]);
    const target = stateOf(hass, hub["sensor.end_soc_target"]);
    const power = stateOf(hass, hub["sensor.battery_power"]);
    const optim = stateOf(hass, hub["sensor.optimization_status"]);
    const attrs = action && action.attributes ? action.attributes : {};
    // The service calls the decision resolved to. An empty list means there
    // was nothing to send in the first place -- no inverter profile, or an
    // action the profile does not define -- which is a different situation
    // from a command that was withheld.
    const steps = Array.isArray(attrs.steps) ? attrs.steps : [];

    /* --- the gate, as a state rather than a switch --- */
    // The switch is the authority, but the decision sensor carries the same
    // flag: with the switch hidden or renamed the banner still tells the truth.
    const armed = control
      ? control.state === "on"
      : attrs.control_enabled === true;
    const applied = attrs.applied === true;
    const failed = Boolean(attrs.error);

    let cls = "";
    let icon = "mdi:eye-outline";
    let title = "Dry run";
    let sub = "Computed, not applied";
    let pillText = "Watching";
    if (failed) {
      cls = "bad";
      icon = "mdi:alert-circle-outline";
      title = "Control failed";
      sub = "The last command did not go through";
      pillText = "Error";
    } else if (armed && applied) {
      cls = "on";
      icon = "mdi:shield-check";
      title = "Controlling your house";
      sub = "Decisions are being sent to your hardware";
      pillText = "Active";
    } else if (armed) {
      // Armed but nothing applied is the ordinary idle case, not a fault:
      // colouring it green would make the green meaningless. It has two quite
      // different causes, though, and "nothing to apply right now" covered
      // both without explaining either. A resolved command that was not sent
      // means the house is already in the state the plan wants, which is the
      // normal steady state; no command at all means there is no inverter
      // profile behind the decision, which is a setup gap worth naming.
      cls = "wait";
      icon = "mdi:shield-outline";
      title = "Armed";
      sub = steps.length
        ? "In sync with the plan"
        : "No inverter command";
      pillText = "Standby";
    }
    if (ui.banner) {
      ui.banner.className = `banner ${cls}`;
      ui.bannerSq.className = `sq ${cls}`;
      ui.bannerIconEl.setAttribute("icon", icon);
      ui.bannerTitle.textContent = title;
      ui.bannerSub.textContent = sub;
      ui.bannerSub.title = sub;
      ui.bannerPill.className = `pill ${cls === "wait" ? "" : cls}`;
      ui.bannerPillText.textContent = pillText;
    }

    /* --- what it decided --- */
    const watts = isUsable(power) ? num(power) : NaN;
    if (ui.decisionText) {
      const actionIcons = {
        self_consume: "mdi:home-lightning-bolt",
        force_charge: "mdi:battery-charging",
        force_discharge: "mdi:battery-minus",
        idle: "mdi:pause",
      };
      ui.decisionIcon.setAttribute(
        "icon",
        actionIcons[action ? action.state : ""] || "mdi:battery",
      );
      ui.decisionIconWrap.className = `sq big ${
        action && action.state === "force_charge"
          ? "wait"
          : action && action.state === "force_discharge"
            ? "on"
            : ""
      }`;
      ui.decisionText.textContent = labelFor(hass, action);
      // The executor tacks the gate's state onto its own reason. The line below
      // already leads with it and the banner says it a third time, so drop the
      // suffix here. If the backend ever rewords it the only cost is the
      // redundancy coming back.
      const reason = attrs.reason
        ? String(attrs.reason)
            .replace(/\s*\(control disabled, not applied\)\s*$/, "")
            .replace(/_/g, " ")
        : "";
      // `applied` means "a command went out on this run", which is not the same
      // question as "is this decision in force". A battery that has been
      // self-consuming for an hour is suppressed as unchanged every run, so a
      // bare "Not applied" would read as a fault on exactly the system that is
      // working. Split the false case by *why* nothing was sent instead.
      ui.decisionWhy.textContent = `${
        failed
          ? "Failed"
          : applied
            ? "Sent to your inverter"
            : !armed
              ? "Dry run — not sent"
              : steps.length
                ? "Already in effect"
                : "Nothing to send"
      }${reason ? ` · ${reason}` : ""}`;

      // What the battery is measurably doing wins, whenever the card has been
      // pointed at a sensor for it. The two runners-up are both accounts of an
      // intention rather than an outcome: `power_w` is the *target* the last
      // command carried, and a target outlives the command -- a decision taken
      // at the top of the quarter hour is still the last decision after the
      // executor has stopped the battery, so a stale "2.7 kW" sits next to a
      // battery at rest. `power_w` is also absent by design for a
      // self-consumption decision, whose whole point is handing the battery
      // back to the inverter to follow the house: the executor sends a zero
      // meaning "not my number", which printed as power reads as a battery
      // doing nothing at the moment it is usually working hardest.
      //
      // Direction is stated only when the sign convention is actually known,
      // which is exactly when the Companion is the source: it is told which
      // way its battery power sensor counts, and a sensor named on the card is
      // not. Guessing it labels a charging battery as discharging, which is
      // worse than saying only how hard.
      const targetW = Number(attrs.power_w);
      // The card's own option wins, but a sensor named here says nothing about
      // which way round it counts -- only the Companion's own setting carries
      // that, which is why direction below is stated only when it is the source.
      const live = this._config.power_entity
        ? { entity: this._config.power_entity }
        : measuredBy(hass, hub, "sensor.battery_power");
      const liveW = num(stateOf(hass, live ? live.entity : null));
      let powerText = "";
      let powerNote = "";
      if (Number.isFinite(liveW)) {
        powerText = formatPower(Math.abs(liveW));
        const signed = typeof live.invert === "boolean" ? (live.invert ? -liveW : liveW) : NaN;
        // NaN falls through both comparisons to the bare "now", which is the
        // honest answer when the convention is unknown.
        powerNote = signed > 1 ? "discharging now" : signed < -1 ? "charging now" : "now";
      } else if (Number.isFinite(targetW) && Math.abs(targetW) >= 1) {
        powerText = formatPower(Math.abs(targetW));
        powerNote = "target";
      } else if (Number.isFinite(watts)) {
        powerText = formatPower(Math.abs(watts));
        powerNote = watts > 1 ? "planned out" : watts < -1 ? "planned in" : "planned";
      }
      ui.decisionPower.textContent = powerText;
      ui.decisionPowerNote.textContent = powerNote;
    }

    /* --- the battery level, planned against measured --- */
    const now = Date.now();
    const all = series(soc);
    // The remaining horizon, so "planned high" is a peak still to come rather
    // than one this morning already reached. A plan that has run out entirely
    // falls back to all of it, which at least dates itself in the sub-line.
    const ahead = all.filter((point) => point.t >= now);
    const points = ahead.length ? ahead : all;
    let low = null;
    let high = null;
    for (const point of points) {
      if (!low || point.v < low.v) low = point;
      if (!high || point.v > high.v) high = point;
    }

    // The Companion already has to know this one -- it is what it sends EMHASS
    // as the plan's starting level -- so the card asks it rather than making
    // the same sensor be named twice. The card option stays as an override.
    const socEntity =
      this._config.soc_entity || (measuredBy(hass, hub, "sensor.battery_soc") || {}).entity;
    const measured = num(stateOf(hass, socEntity));
    if (ui.soc) {
      ui.soc.set({
        planned: isUsable(soc) ? num(soc) : NaN,
        peak: high ? high.v : NaN,
        peakAt: high ? formatTime(high.t, hass) : "",
        measured,
      });
    }

    // The measured level as a number as well as a marker. The marker answers
    // "is the plan running on the truth" by eye; this answers "by how much",
    // which is the number you need before deciding the plan is worth ignoring.
    const plannedNow = isUsable(soc) ? num(soc) : NaN;
    const drift = measured - plannedNow;
    setBox(
      ui.box.show_level,
      Number.isFinite(measured) ? `${measured.toFixed(0)} %` : "–",
      !Number.isFinite(measured)
        ? "no battery level sensor set"
        : !Number.isFinite(drift)
          ? ""
          : Math.abs(drift) < 1
            ? "matches the plan"
            : `${drift > 0 ? "+" : "−"}${Math.abs(drift).toFixed(0)} % vs plan`,
    );

    setBox(
      ui.box.show_power,
      Number.isFinite(watts) ? formatPower(Math.abs(watts)) : "–",
      // The plan's sign convention, spelled out: nobody reads "-1.5 kW" as
      // charging without being told, and the tile has room to say it.
      Number.isFinite(watts)
        ? watts > 1
          ? "discharging"
          : watts < -1
            ? "charging"
            : "idle"
        : "",
    );
    const targetAttrs = target && target.attributes ? target.attributes : {};
    setBox(
      ui.box.show_target,
      isUsable(target) ? `${num(target).toFixed(0)} %` : "–",
      targetAttrs.mode ? String(targetAttrs.mode).replace(/_/g, " ") : "",
    );
    // Where the plan actually lands, next to the target it was steering for.
    // The two agreeing is the normal case; the two disagreeing is the whole
    // reason the end-SOC target is worth having a card row at all.
    const last = all.length ? all[all.length - 1] : null;
    setBox(
      ui.box.show_plan_end,
      last ? `${last.v.toFixed(0)} %` : "–",
      last ? formatTime(last.t, hass) : "",
    );
    setBox(ui.box.show_low, low ? `${low.v.toFixed(0)} %` : "–", low ? formatTime(low.t, hass) : "");
    setBox(
      ui.box.show_high,
      high ? `${high.v.toFixed(0)} %` : "–",
      high ? formatTime(high.t, hass) : "",
    );

    /* --- the settings, as values rather than controls --- */
    setBox(ui.box.show_mode, shortLabel(hass, mode));
    setBox(ui.box.show_cost, shortLabel(hass, cost));
    const curtailW = Number(attrs.curtail_w);
    setBox(
      ui.box.show_curtail,
      attrs.curtail === true ? formatPower(Number.isFinite(curtailW) ? curtailW : NaN) : "Off",
      attrs.curtail === true ? "solar capped" : "",
    );
    // The battery is not the only thing the executor touches: it also switches
    // the deferrable loads, and nothing else on this card admits that. Counted
    // rather than named, because the decision carries subentry ids and turning
    // those back into names is the load cards' job.
    const decided = attrs.loads && typeof attrs.loads === "object" ? attrs.loads : {};
    const ids = Object.keys(decided);
    const on = ids.filter((id) => decided[id] === true).length;
    setBox(ui.box.show_loads, ids.length ? String(on) : "–", ids.length ? `of ${ids.length}` : "no loads");

    // How the last EMHASS run itself ended. Every other value on this card is
    // downstream of it: a decision taken off an infeasible or failed run is
    // still reported confidently, and this is the tile that says not to trust
    // it. The health card has the full account; this is the one line of it.
    const optimAttrs = optim && optim.attributes ? optim.attributes : {};
    const optimLabel = optim
      ? OPTIM_STATUS_LABELS[optim.state] || labelFor(hass, optim)
      : "–";
    setBox(
      ui.box.show_optim,
      optimAttrs.error_message ? "Failed" : optimAttrs.infeasible === true ? "Infeasible" : optimLabel,
      optimAttrs.error_message
        ? String(optimAttrs.error_message)
        : optimAttrs.action
          ? String(optimAttrs.action).replace(/_/g, "-")
          : "",
    );

    // What "sent to your inverter" actually amounts to. On a dry run this is
    // the useful half of the card: the service calls are resolved in full and
    // can be read off before the gate is ever opened.
    setBox(
      ui.box.show_commands,
      String(steps.length),
      steps.length ? (applied ? "sent" : "resolved, not sent") : "no inverter action",
    );

    const decidedAt = attrs.at ? Date.parse(attrs.at) : NaN;
    setBox(
      ui.box.show_decided,
      formatAgoShort(decidedAt),
      Number.isFinite(decidedAt) ? formatTime(decidedAt, hass) : "",
    );

    this._problems(attrs);
  }

  _problems(attrs) {
    const ui = this._ui;
    ui.problems.textContent = "";
    // The error is not part of the rule trace and is not hideable with it: a
    // card configured down to a single line still has to say when the thing
    // it is reporting on has failed.
    if (attrs.error) {
      const box = tag("div", "note bad", ui.problems);
      const icon = document.createElement("ha-icon");
      icon.setAttribute("icon", "mdi:alert-circle");
      box.appendChild(icon);
      tag("span", null, box, String(attrs.error));
    }
    if (!showsStatusPart(this._config, "show_rules")) return;
    const rules = Array.isArray(attrs.rules) ? attrs.rules : [];
    // The executor's own account of why it landed where it did. It is the
    // closest thing to the removed service-call list that is still status
    // rather than instruction, and it is the only place it surfaces at all.
    for (const rule of rules) {
      const box = tag("div", "note", ui.problems);
      const icon = document.createElement("ha-icon");
      icon.setAttribute("icon", "mdi:ray-start-arrow");
      box.appendChild(icon);
      tag("span", null, box, String(rule));
    }
  }
}

EmhassStatusCard.ticks = true;
EmhassStatusCard.css = `
  .banner { display: flex; align-items: center; gap: 12px; padding: 12px;
            border-radius: var(--emh-radius); background: var(--emh-surface);
            transition: background 300ms; }
  .banner.on { background: rgba(67, 160, 71, .13);
               background: color-mix(in srgb, var(--emh-ok) 13%, transparent); }
  .banner.bad { background: rgba(219, 68, 55, .13);
                background: color-mix(in srgb, var(--emh-bad) 13%, transparent); }
  .banner .grow { flex: 1; min-width: 0; }
  .section { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em;
             color: var(--emh-dim); margin: 16px 0 6px 0; }
  .decision { display: flex; align-items: center; gap: 12px; }
  .decision .grow { flex: 1; min-width: 0; }
  .sq.big { width: 46px; height: 46px; border-radius: 15px; }
  .sq.big ha-icon { --mdc-icon-size: 25px; }
  .decision-text { font-size: 1.05rem; font-weight: 500; }
  .decision-power { flex: 0 0 auto; text-align: right; }
  .decision-power .dp-v { font-size: 1.05rem; font-weight: 500;
                          font-variant-numeric: tabular-nums; color: var(--emh-dim); }
  /* Whether the figure above is the commanded target or the power actually
     flowing. Small enough to stay out of the way, present enough that the
     number is never read as the wrong one of the two. */
  .decision-power .dp-c { font-size: .66rem; color: var(--emh-dim); opacity: .8;
                          text-transform: uppercase; letter-spacing: .04em; }

  /* --- planned battery level ------------------------------------------ */
  .soc { margin-top: 14px; }
  .meter-head { display: flex; justify-content: space-between; font-size: .74rem;
                color: var(--emh-dim); margin-bottom: 5px; }
  .meter-value { font-variant-numeric: tabular-nums; color: var(--primary-text-color); }
  .soc .rail { position: relative; height: 10px; border-radius: 999px;
               background: var(--emh-surface-2); overflow: hidden; }
  .soc .reach, .soc .fill { position: absolute; top: 0; bottom: 0; left: 0; width: 0;
                            border-radius: 999px;
                            transition: width 500ms var(--emh-ease), background 300ms; }
  .soc .reach { background: rgba(77, 182, 172, .32);
                background: color-mix(in srgb, var(--emh-battery) 32%, transparent); }
  .soc .fill { background: var(--emh-battery); }
  /* The measured level rides on top of both bands: the whole point of it is
     to be readable against the planned one it disagrees with. */
  .soc .mark { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px;
               border-radius: 1px; background: var(--primary-text-color);
               transition: left 500ms var(--emh-ease); }
  .soc-key { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 7px;
             font-size: .72rem; color: var(--emh-dim); }
  .soc-key .leg { display: inline-flex; align-items: center; gap: 5px; }
  .soc-key .sw { width: 8px; height: 8px; border-radius: 2px; }
  .soc-key .sw.now { background: var(--primary-text-color); }
  .soc-key .sw.plan { background: var(--emh-battery); }
  .soc-key .sw.peak { background: rgba(77, 182, 172, .32);
                      background: color-mix(in srgb, var(--emh-battery) 32%, transparent); }

  /* --- value boxes ------------------------------------------------------ */
  .stat .s { font-size: .68rem; color: var(--emh-dim); margin-top: 1px;
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .problems { margin-top: 14px; display: grid; gap: 6px; }
  .note { display: flex; gap: 8px; align-items: flex-start; font-size: .8rem;
          padding: 8px 10px; border-radius: 10px; background: var(--emh-surface);
          color: var(--emh-dim); }
  .note ha-icon { --mdc-icon-size: 18px; flex: 0 0 auto; }
  .note.bad { color: var(--emh-bad); background: rgba(219, 68, 55, .12);
              background: color-mix(in srgb, var(--emh-bad) 12%, transparent); }
`;

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
    read: (c) => ({ k: "Solar", v: formatPower(num(c.pv)) }),
  },
  house: {
    label: "House now",
    read: (c) => ({ k: "House", v: formatPower(num(c.house)) }),
  },
  grid: {
    label: "Grid now",
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
    read: (c) => ({
      k: "Charge level",
      v: isUsable(c.soc) ? `${num(c.soc).toFixed(0)} %` : "–",
    }),
  },
  end_soc: {
    label: "End charge target",
    read: (c) => ({
      k: "Target at end",
      v: isUsable(c.endSoc) ? `${num(c.endSoc).toFixed(0)} %` : "–",
    }),
  },
  price: {
    label: "Import price now",
    read: (c) => ({ k: "Price now", v: isUsable(c.buy) ? num(c.buy).toFixed(3) : "–" }),
  },
  sell_price: {
    label: "Export price now",
    read: (c) => ({ k: "Export price", v: isUsable(c.sell) ? num(c.sell).toFixed(3) : "–" }),
  },
  cost: {
    label: "Planned cost",
    read: (c) => ({ k: "Plan cost", v: isUsable(c.cost) ? num(c.cost).toFixed(2) : "–" }),
  },
  solar_planned: {
    label: "Solar in the plan",
    read: (c) => ({ k: "Solar planned", v: formatEnergy(c.solarEnergy) }),
  },
  charge_planned: {
    label: "Charging in the plan",
    read: (c) => ({ k: "Charging planned", v: formatEnergy(c.chargeEnergy) }),
  },
  surplus: {
    label: "Spare solar",
    read: (c) => ({
      k: "Spare solar",
      v: isUsable(c.surplusEnergy) ? formatEnergy(num(c.surplusEnergy)) : "–",
    }),
  },
  loads: {
    label: "Loads running",
    read: (c) => ({
      k: "Loads on",
      v: c.loadsTotal ? `${c.loadsOn} of ${c.loadsTotal}` : "–",
    }),
  },
  age: {
    label: "Plan age",
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
        const tile = statTile(ui.stats, TILE_METRICS[metric].label);
        tile.metric = metric;
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
      ui.ribbon = tag("div", "ribbon", ui.priceRow.lane);
      ui.ribbonScale = tag("div", "scale", pad);
      ui.priceRow.addEventListener("click", () => moreInfo(this, this._hubId("sensor.buy_price")));
    }

    if (shows("show_surplus")) ui.surplusNote = tag("div", "surplus", pad);

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
   * What solar and the battery actually did, from the recorder.
   *
   * The plan is no use behind the present: an MPC run starts at the timestep
   * it was made in, so the moment the window reaches into the past the two
   * lanes would start with two hours of blank rail. The recorder has the
   * answer, and it is one call.
   *
   * Read from the Companion's own sensors by default, whose sign convention is
   * known -- positive is discharge -- and which need no configuration to work.
   * `solar_entity` and `battery_entity` point it at the house's own meters
   * instead, which is the difference between "what was forecast" and "what
   * happened".
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
    const cost = stateOf(hass, hub["sensor.plan_cost"]);
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
    const prices = series(buy);
    const solarPoints = mergeHistory(history.solar, planSolar);
    const batteryPoints = mergeHistory(history.battery, planBattery);
    const socPoints = series(soc);
    const loads = findLoads(hass).map((load) => loadView(hass, load));
    // A rolling window rather than the extent of the data: the price series
    // alone reaches back to last midnight, and eleven hours of spent morning
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
        ui.solarRow.setFigure(formatPower(solar.peak));
        // Which record the shaded part is, named rather than implied: a
        // forecast and a meter reading look identical once they are drawn.
        ui.solarRow.title = this._config.solar_entity
          ? `Past: measured, from ${this._config.solar_entity}`
          : "Past: the forecast the plan was using at the time";
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
        ui.batteryRow.title = this._config.battery_entity
          ? `Past: measured, from ${this._config.battery_entity}`
          : "Past: the battery power the plan had for that moment";
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

/* --------------------------------------------------- status visual editor */

const STATUS_LABELS = {
  soc_entity: "Battery level sensor",
  power_entity: "Battery power sensor",
};
const STATUS_HELPERS = {
  soc_entity:
    "Left empty, the Companion's own battery level sensor is used -- it already has one, since it is what the plan starts from.",
  power_entity:
    "Left empty, the Companion's own battery power sensor is used, which also lets this card say whether the battery is charging or discharging rather than only how hard.",
};
for (const section of STATUS_SECTIONS) {
  STATUS_LABELS[section[0]] = section[1];
  STATUS_HELPERS[section[0]] = section[2];
}
for (const tile of STATUS_TILES) {
  STATUS_LABELS[tile[1]] = tile[2];
  STATUS_HELPERS[tile[1]] = tile[3];
}

/** The switches for one section's tiles, as a grid inside a fold. */
function statusTileSchema(section, title, icon) {
  return {
    name: "",
    type: "expandable",
    title,
    icon,
    schema: [
      {
        name: "",
        type: "grid",
        column_min_width: "220px",
        schema: STATUS_TILES.filter((tile) => tile[0] === section).map((tile) => ({
          name: tile[1],
          selector: { boolean: {} },
        })),
      },
    ],
  };
}

const STATUS_SCHEMA = [
  // Neither sensor is published by this integration -- it is configured with
  // them but does not mirror them -- so the card has to be told, and a picker
  // is the difference between that being a setting and being a trap.
  {
    name: "",
    type: "grid",
    column_min_width: "260px",
    schema: [
      { name: "soc_entity", selector: { entity: { filter: { domain: "sensor" } } } },
      { name: "power_entity", selector: { entity: { filter: { domain: "sensor" } } } },
    ],
  },
  sectionGrid(STATUS_SECTIONS),
  // Folded away, because the sections above are the choice most people make
  // and thirteen more switches in front of them would bury it.
  statusTileSchema("show_battery", "Battery tiles", "mdi:battery-70"),
  statusTileSchema("show_system", "System tiles", "mdi:cog-outline"),
];

/** Every key the editor owns, and whether it is on when nothing is written. */
const STATUS_KEYS = [
  ...STATUS_SECTIONS.map((section) => section[0]),
  ...STATUS_TILES.map((tile) => tile[1]),
];

/** Which parts of the status card to draw, as switches. */
class EmhassStatusCardEditor extends CardEditor {
  get labels() {
    return STATUS_LABELS;
  }

  get helpers() {
    return STATUS_HELPERS;
  }

  schema() {
    return STATUS_SCHEMA;
  }

  data() {
    const data = {
      soc_entity: this._config.soc_entity || "",
      power_entity: this._config.power_entity || "",
    };
    for (const key of STATUS_KEYS) data[key] = showsStatusPart(this._config, key);
    return data;
  }

  /**
   * The command count is the one switch that starts *off*, so for that key a
   * decision worth recording is recording it when it is on.
   */
  clean(config) {
    for (const key of STATUS_KEYS) {
      if (config[key] === !STATUS_OFF_BY_DEFAULT.has(key)) delete config[key];
    }
    if (!config.soc_entity) delete config.soc_entity;
    if (!config.power_entity) delete config.power_entity;
    return config;
  }
}

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

/* ------------------------------------------------------------- registration */

customElements.define("emhass-plan-card", EmhassPlanCard);
customElements.define("emhass-plan-card-editor", EmhassPlanCardEditor);
customElements.define("emhass-deferrable-card", EmhassDeferrableCard);
customElements.define("emhass-deferrable-card-editor", EmhassDeferrableCardEditor);
customElements.define("emhass-deferrable-swipe-card", EmhassDeferrableSwipeCard);
customElements.define("emhass-deferrable-swipe-card-editor", EmhassDeferrableSwipeCardEditor);
customElements.define("emhass-deferrable-strip-card", EmhassDeferrableStripCard);
customElements.define("emhass-deferrable-strip-card-editor", EmhassDeferrableStripCardEditor);
customElements.define("emhass-health-card", EmhassHealthCard);
customElements.define("emhass-health-card-editor", EmhassHealthCardEditor);
customElements.define("emhass-status-card", EmhassStatusCard);
customElements.define("emhass-status-card-editor", EmhassStatusCardEditor);
customElements.define("emhass-overview-card", EmhassOverviewCard);
customElements.define("emhass-overview-card-editor", EmhassOverviewCardEditor);

const DOCS = "https://github.com/smefa/emhass-ha-companion";

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-plan-card",
    name: "EMHASS Companion plan",
    description:
      "The optimisation plan: solar, consumption, grid, battery, prices and scheduled loads.",
    preview: true,
    documentationURL: DOCS,
  },
  {
    type: "emhass-overview-card",
    name: "EMHASS Companion overview",
    description: "Prices, solar, the battery and every load on one shared time axis.",
    preview: true,
    documentationURL: DOCS,
  },
  {
    type: "emhass-health-card",
    name: "EMHASS Companion health",
    description: "Optimiser health: freshness, timings, warnings and re-run buttons.",
    preview: true,
    documentationURL: DOCS,
  },
  {
    type: "emhass-status-card",
    name: "EMHASS Companion status",
    description: "Is it in charge, what it is doing to the battery, and on what settings.",
    preview: true,
    documentationURL: DOCS,
  },
  {
    type: "emhass-deferrable-card",
    name: "EMHASS Companion deferrable load",
    description: "One deferrable load: whether it should run, and when the plan has it running.",
    preview: true,
    documentationURL: DOCS,
  },
  {
    type: "emhass-deferrable-swipe-card",
    name: "EMHASS Companion load (swipe)",
    description: "One load, three swipeable pages: now, control, plan.",
    preview: true,
    documentationURL: DOCS,
  },
  {
    type: "emhass-deferrable-strip-card",
    name: "EMHASS Companion load (strip)",
    description: "One load in a single compact row, with the whole day behind it.",
    preview: true,
    documentationURL: DOCS,
  },
);

console.info(
  "%c EMHASS-COMPANION %c cards loaded ",
  "color:white;background:#03a9f4;font-weight:700",
  "color:#03a9f4;background:white;font-weight:700",
);
