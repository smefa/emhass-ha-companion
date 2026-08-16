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
  findHub,
  formatAgo,
  formatPower,
  formatTime,
  isUsable,
  labelFor,
  loadHaForm,
  measuredBy,
  moreInfo,
  num,
  sectionGrid,
  series,
  stateOf,
  tag,
  valueBox,
} from "./emhass-core.js?v=__VERSION__";


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

/**
 * The level at and below which the rail stops being informative and starts
 * warning -- for the fill, and for the trough the plan is heading into.
 */
const SOC_LOW_PCT = 20;

/**
 * The planned battery level, with where the battery actually is on it.
 *
 * A lone fill answers "how full is it", which is the one thing a battery
 * owner can already read off their inverter. What a *plan* bar is for is the
 * comparison, so this draws four things on one rail: the level the plan has
 * for right now (the fill), the peak the plan is steering towards (a lighter
 * band carrying on from it), the lowest level it dips to on the way (a quiet
 * tick), and the measured level as a marker. A visible gap between the marker
 * and the fill is a plan running on a stale SOC -- which is invisible on any
 * single-value display. The low is the other half of the same question: a plan
 * that ends the day full can still empty the battery at 3pm, and neither the
 * fill nor the peak band ever says so.
 */
function socBar(parent, label) {
  const root = tag("div", "soc", parent);
  const head = tag("div", "meter-head", root);
  tag("span", null, head, label);
  const value = tag("span", "meter-value", head, "");
  const rail = tag("div", "rail", root);
  const reach = tag("div", "reach", rail);
  const fill = tag("div", "fill", rail);
  const dip = tag("div", "dip", rail);
  const mark = tag("div", "mark", rail);
  const key = tag("div", "soc-key", root);

  const legend = (swatch) => {
    const item = tag("span", "leg", key);
    const sw = tag("i", `sw ${swatch}`, item);
    const text = tag("span", null, item, "");
    item.set = (content, colour) => {
      text.textContent = content || "";
      item.style.display = content ? "" : "none";
      // Only the low swatch ever tints, and only to warn; the empty string
      // hands it back to the stylesheet rather than freezing today's colour.
      sw.style.background = colour || "";
    };
    return item;
  };
  const legNow = legend("now");
  const legPlan = legend("plan");
  const legLow = legend("low");
  const legPeak = legend("peak");

  const clamp = (value) => Math.max(0, Math.min(100, value));

  root.set = (info) => {
    const planned = info.planned;
    const peak = info.peak;
    const low = info.low;
    const measured = info.measured;
    const hasPlan = Number.isFinite(planned);
    // The same line the fill goes red on, so the rail never warns about the
    // level now while staying calm about an identical level an hour out.
    const isDeep = Number.isFinite(low) && low < SOC_LOW_PCT;

    fill.style.width = hasPlan ? `${clamp(planned)}%` : "0%";
    // The band only means anything as *headroom beyond the fill*; without a
    // plan there is nothing for it to extend from.
    reach.style.width = hasPlan && Number.isFinite(peak) ? `${clamp(peak)}%` : "0%";
    fill.style.background =
      hasPlan && planned < SOC_LOW_PCT ? "var(--emh-bad)" : "var(--emh-battery)";

    if (Number.isFinite(low)) {
      dip.style.left = `${clamp(low)}%`;
      dip.style.display = "";
      // A trough that goes deep is worth reading as a warning rather than as
      // one more tick, and its colour is the only thing carrying that on the
      // rail itself, since the fill is drawn from the level *now*.
      dip.style.background = isDeep ? "var(--emh-bad)" : "";
    } else {
      dip.style.display = "none";
    }

    if (Number.isFinite(measured)) {
      mark.style.left = `${clamp(measured)}%`;
      mark.style.display = "";
    } else {
      mark.style.display = "none";
    }

    value.textContent = hasPlan ? `${planned.toFixed(0)} %` : "no plan";
    legNow.set(Number.isFinite(measured) ? `now ${measured.toFixed(0)} %` : "");
    legPlan.set(hasPlan ? `plan ${planned.toFixed(0)} %` : "");
    legLow.set(
      Number.isFinite(low) ? `low ${low.toFixed(0)} %${info.lowAt ? ` · ${info.lowAt}` : ""}` : "",
      isDeep ? "var(--emh-bad)" : "",
    );
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
  ["show_soc", "Planned battery level", "The plan's level, its low and peak, and the measured one"],
  ["show_battery", "Battery tiles", "The numbers under the rail"],
  ["show_system", "System tiles", "Mode, cost function, curtailment, loads, timing"],
  ["show_rules", "Why it decided that", "The executor's own rule trace"],
];

/** How close the decision's own timestamp has to sit to the action sensor's
 *  `last_changed` for the two to count as the same run. Generous, because the
 *  state write follows the decision by however long the executor's remaining
 *  axes take, and the alternative reading -- a re-send -- is minutes away. */
const ACTION_CHANGE_TOLERANCE_MS = 15000;

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
    // Whether the *decision* changed on the run being reported, as opposed to
    // the same decision being re-sent with a new number. `applied` cannot tell
    // those apart and they are not the same news: a charge window holds one
    // action for an hour while the plan revises its power every few minutes,
    // and the executor rewrites on each revision past the 100 W deadband. On a
    // real plan that is most rows, so treating every write as a change left
    // the banner announcing news through the whole of a working charge.
    //
    // The sensor's own state is the action, so `last_changed` moves only when
    // the action does, while `at` advances every run. The run that changed the
    // action is the one where those two coincide; a re-send leaves them
    // minutes apart. A missing timestamp reads as "not a change", which lands
    // on the settled branch below -- the safer of the two to be wrong about.
    const decidedAt = Date.parse(attrs.at || "");
    const changedAt = Date.parse((action && action.last_changed) || "");
    const justChanged =
      Number.isFinite(decidedAt) &&
      Number.isFinite(changedAt) &&
      Math.abs(decidedAt - changedAt) < ACTION_CHANGE_TOLERANCE_MS;

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
    } else if (armed && !steps.length) {
      // Nothing resolved at all -- no inverter profile, or the profile defines
      // no action for what was decided. Loads are still switched; the battery
      // is not being touched. A setup gap rather than a steady state, and the
      // only one of these worth the amber: the rest are the system working,
      // and an amber that fires on those is an amber nobody reads.
      //
      // Ahead of the two below because it is a property of the install rather
      // than of the run: an install in this state would otherwise alternate
      // between amber and the change badge every time a load was switched,
      // which reads as intermittent rather than as unconfigured.
      const loadKeys =
        attrs.loads && typeof attrs.loads === "object" ? Object.keys(attrs.loads) : [];
      cls = "warn";
      icon = "mdi:shield-alert-outline";
      title = "Armed";
      sub = loadKeys.length ? "Loads only — no inverter command" : "No inverter command";
      pillText = "No command";
    } else if (armed && applied && justChanged) {
      // The plan has actually changed its mind, and the new decision went out
      // on this run. That is news; a power refresh within the same action is
      // not, and lands on the settled branch below.
      cls = "wait";
      icon = "mdi:shield-sync";
      title = "Controlling your house";
      // The label as the translation gives it: lowercasing it to fit the
      // sentence would be wrong in every language that capitalises nouns.
      sub = `New decision sent: ${labelFor(hass, action)}`;
      pillText = "Updated";
    } else if (armed) {
      // Armed, resolved, and the hardware is doing what the plan asks --
      // whether that took a write this run or not. The executor writes only
      // on a change, past the deadband, or before a timed command lapses, so
      // an untouched run means the command already in force is still the
      // right one. Both are the state you want the system to be in, and both
      // are where a working install sits nearly all the time.
      //
      // This used to read "Armed / Standby", which described the card's own
      // last few seconds rather than the house: a battery charging at full
      // power reported standby for most of the hour it charged, because the
      // command that started it predated the run being reported on.
      cls = "on";
      icon = "mdi:shield-check";
      title = "Controlling your house";
      sub = applied ? "Tracking the plan — command refreshed" : "In sync with the plan";
      pillText = "In sync";
    }
    if (ui.banner) {
      ui.banner.className = `banner ${cls}`;
      ui.bannerSq.className = `sq ${cls}`;
      ui.bannerIconEl.setAttribute("icon", icon);
      ui.bannerTitle.textContent = title;
      ui.bannerSub.textContent = sub;
      ui.bannerSub.title = sub;
      // Every state now carries a colour of its own, so the pill wears the
      // same class as the rest of the banner rather than opting out of it.
      ui.bannerPill.className = `pill ${cls}`;
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
    // The remaining horizon, bounded at midnight, so "planned high" is a peak
    // still to come *today* rather than one this morning already reached, or
    // one the plan reaches tomorrow -- a multi-day horizon otherwise lets a
    // higher point past midnight upstage today's own peak. A plan that has
    // run out entirely falls back to all of it, which at least dates itself
    // in the sub-line.
    const endOfToday = new Date(now).setHours(24, 0, 0, 0);
    const ahead = all.filter((point) => point.t >= now && point.t < endOfToday);
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
        low: low ? low.v : NaN,
        lowAt: low ? formatTime(low.t, hass) : "",
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

    // `decidedAt` is the same instant the change badge above is keyed to, and
    // is already parsed at the top of this method.
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
  .banner.warn { background: rgba(255, 166, 0, .13);
                 background: color-mix(in srgb, var(--emh-warn) 13%, transparent); }
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
  /* The trough is a tick like the measured level, one step quieter, because it
     has to stay readable wherever it lands -- inside the fill, inside the
     lighter band, or on the bare rail when the plan only climbs from here. */
  .soc .dip { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px;
              border-radius: 1px; background: var(--emh-dim);
              transition: left 500ms var(--emh-ease), background 300ms; }
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
  .soc-key .sw.low { background: var(--emh-dim); }
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

customElements.define("emhass-status-card", EmhassStatusCard);
customElements.define("emhass-status-card-editor", EmhassStatusCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-status-card",
    name: "EMHASS Companion status",
    description: "Is it in charge, what it is doing to the battery, and on what settings.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);
