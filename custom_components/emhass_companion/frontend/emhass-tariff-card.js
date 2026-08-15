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
  formatCountdown,
  isUsable,
  loadHaForm,
  moreInfo,
  num,
  sectionGrid,
  showsSection,
  stateOf,
  svg,
  tag,
  valueBox,
} from "./emhass-core.js?v=__VERSION__";

/** Where the headroom arc starts turning red, as a percentage of the period's
 *  own peak -- see `renderDemand` below. */
const DEFAULT_HEADROOM_WARN_PCT = 10;

/**
 * A worked example, drawn from the Göteborg Energi profile this integration
 * ships built in (135 kr/kW/month, mean of the top 3, 07:00-20:00 Nov-Mar) --
 * real numbers rather than invented ones, so someone who later configures
 * that exact profile sees this card confirm what it already showed them.
 *
 * Used only in place of a real read, whenever neither the band nor the
 * demand-charge sensors exist at all -- see `tariffView` and "No tariff
 * configured yet" in planning/tariff_card_plan.md.
 */
const SAMPLE = {
  band: { name: "höglast", adder: 0.34, multiplier: NaN, nextBand: "låglast", untilMs: (2 * 3600 + 14 * 60) * 1000 },
  rate: { effective: 45, unit: "SEK/kW", sheetRate: 135, rateBasis: "month" },
  period: { kw: 6.4, daysRemaining: 11 },
  headroomKw: 1.8,
};

/** "×1.15 +0.34", "−0.10", or "" -- whichever of the two a band actually sets. */
function formatAdder(adder, multiplier) {
  const parts = [];
  if (Number.isFinite(multiplier) && Math.abs(multiplier - 1) > 0.001) {
    parts.push(`×${multiplier.toFixed(2)}`);
  }
  if (Number.isFinite(adder) && Math.abs(adder) > 0.001) {
    parts.push(`${adder > 0 ? "+" : "−"}${Math.abs(adder).toFixed(2)}`);
  }
  return parts.join(" ");
}

/**
 * A headroom gauge: a semicircular arc, filled toward *how much room is left*
 * rather than toward a fixed 100 %, with the live figure in its centre.
 *
 * A rail like the status card's SOC bar reads as a timeline, which a demand
 * charge is not -- it is a single number to stay under. `strokeDashoffset` is
 * animated rather than the path itself, the same trick the SOC bar uses on
 * `width`, so the fill transitions instead of jumping between reads.
 */
function headroomArc(parent) {
  const wrap = tag("div", "arc-wrap", parent);
  const root = svg("svg", { viewBox: "0 0 200 112", role: "img" }, wrap);
  const arcPath = "M20,100 A80,80 0 0 1 180,100";
  svg("path", { d: arcPath, class: "arc-track" }, root);
  const fill = svg("path", { d: arcPath, class: "arc-fill" }, root);
  const length = Math.PI * 80;
  fill.style.strokeDasharray = `${length}`;
  fill.style.strokeDashoffset = `${length}`;
  const value = tag("div", "arc-value", wrap, "–");
  const sub = tag("div", "arc-sub", wrap, "");

  wrap.set = (pct, valueText, subText, warn) => {
    const clamped = Math.max(0, Math.min(100, pct));
    fill.style.strokeDashoffset = Number.isFinite(pct) ? `${length * (1 - clamped / 100)}` : `${length}`;
    fill.classList.toggle("warn", Boolean(warn));
    value.textContent = valueText;
    sub.textContent = subText;
  };
  return wrap;
}

/**
 * Everything the card renders, read from real entities or from `SAMPLE`.
 *
 * One shape either way, so the render functions below never know which of
 * the two they were handed -- the sample state is drawn by the exact same
 * code as a live one, not a second copy of it that can quietly drift.
 */
function tariffView(hass, hub, demo) {
  if (demo) {
    return {
      band: {
        name: SAMPLE.band.name,
        adder: SAMPLE.band.adder,
        multiplier: SAMPLE.band.multiplier,
        nextBand: SAMPLE.band.nextBand,
        nextChangeMs: Date.now() + SAMPLE.band.untilMs,
        entity: null,
      },
      demand: {
        periodKw: SAMPLE.period.kw,
        daysRemaining: SAMPLE.period.daysRemaining,
        headroomKw: SAMPLE.headroomKw,
        periodEntity: null,
        headroomEntity: null,
        active: true,
        reason: "",
        targetKw: SAMPLE.period.kw,
        rateEntity: null,
        targetEntity: null,
      },
      rate: {
        effective: SAMPLE.rate.effective,
        unit: SAMPLE.rate.unit,
        sheetRate: SAMPLE.rate.sheetRate,
        rateBasis: SAMPLE.rate.rateBasis,
        entity: null,
      },
    };
  }

  const bandState = stateOf(hass, hub["sensor.network_tariff_band"]);
  const bandAttrs = bandState && bandState.attributes ? bandState.attributes : {};
  const rateState = stateOf(hass, hub["sensor.demand_charge_rate"]);
  const rateAttrs = rateState && rateState.attributes ? rateState.attributes : {};
  const periodState = stateOf(hass, hub["sensor.period_peak"]);
  const periodAttrs = periodState && periodState.attributes ? periodState.attributes : {};
  const headroomState = stateOf(hass, hub["sensor.peak_headroom"]);
  const targetState = stateOf(hass, hub["number.peak_target"]);

  return {
    band: {
      name: isUsable(bandState) ? bandState.state : null,
      adder: Number(bandAttrs.adder),
      multiplier: Number(bandAttrs.multiplier),
      nextBand: bandAttrs.next_band || null,
      nextChangeMs: bandAttrs.next_change ? Date.parse(bandAttrs.next_change) : NaN,
      entity: hub["sensor.network_tariff_band"],
    },
    demand: {
      periodKw: isUsable(periodState) ? num(periodState) : NaN,
      daysRemaining: Number(periodAttrs.days_remaining),
      headroomKw: isUsable(headroomState) ? num(headroomState) : NaN,
      periodEntity: hub["sensor.period_peak"],
      headroomEntity: hub["sensor.peak_headroom"],
      active: rateAttrs.active === true,
      reason: rateAttrs.reason ? String(rateAttrs.reason) : "",
      targetKw: isUsable(targetState) ? num(targetState) : NaN,
      rateEntity: hub["sensor.demand_charge_rate"],
      targetEntity: hub["number.peak_target"],
    },
    rate: {
      effective: isUsable(rateState) ? num(rateState) : NaN,
      unit: rateState && rateState.attributes ? rateState.attributes.unit_of_measurement : null,
      sheetRate: Number(rateAttrs.sheet_rate),
      rateBasis: rateAttrs.rate_basis ? String(rateAttrs.rate_basis) : "",
      entity: hub["sensor.demand_charge_rate"],
    },
  };
}

function renderBand(ui, band) {
  if (!ui.band) return;
  const adderText = formatAdder(band.adder, band.multiplier);
  ui.bandTitle.textContent = band.name ? `${band.name}${adderText ? ` (${adderText})` : ""}` : "–";
  ui.bandSub.textContent =
    band.nextBand && Number.isFinite(band.nextChangeMs)
      ? `${band.nextBand} in ${formatCountdown(band.nextChangeMs - Date.now())}`
      : "";
  ui.bandEntity = band.entity;
}

/** How much of the period's own peak is still headroom, as a percentage --
 *  the arc's fill, and what `headroom_warn_pct` is compared against. */
function headroomPct(demand) {
  if (!(demand.periodKw > 0) || !Number.isFinite(demand.headroomKw)) return NaN;
  return (demand.headroomKw / demand.periodKw) * 100;
}

function renderDemand(ui, demand, warnPct) {
  if (!ui.arc) return;
  const pct = headroomPct(demand);
  ui.arc.set(
    pct,
    Number.isFinite(demand.headroomKw) ? `${demand.headroomKw.toFixed(1)} kW` : "–",
    "to a new peak",
    Number.isFinite(pct) && pct < warnPct,
  );
  ui.headroomEntity = demand.headroomEntity;

  ui.periodLine.textContent = `${
    Number.isFinite(demand.periodKw) ? demand.periodKw.toFixed(1) : "–"
  } kW this period${Number.isFinite(demand.daysRemaining) ? ` · ${demand.daysRemaining} days left` : ""}`;
  ui.periodEntity = demand.periodEntity;

  // The two pills are mutually exclusive readings of the same fact: whether
  // the demand charge is actually being priced right now (see
  // EmhassCoordinator.demand_charge_pricing) or EMHASS falls back to the
  // windowed hard cap instead -- see network_tariffs.md, "Version gating".
  if (demand.active) {
    ui.pill.className = "pill on";
    ui.pillText.textContent = "Priced now";
    ui.pill.title = "";
    ui.pillEntity = demand.rateEntity;
  } else {
    ui.pill.className = "pill warn";
    ui.pillText.textContent = `Holding to peak target · ${
      Number.isFinite(demand.targetKw) ? demand.targetKw.toFixed(1) : "–"
    } kW`;
    ui.pill.title = demand.reason;
    ui.pillEntity = demand.targetEntity;
  }
}

function renderRate(ui, rate) {
  if (!ui.rateBox) return;
  ui.rateBox.set(
    Number.isFinite(rate.effective) ? `${rate.effective.toFixed(2)}${rate.unit ? ` ${rate.unit}` : ""}` : "–",
  );
  const sheetUnit = rate.unit ? `${rate.unit}${rate.rateBasis ? `/${rate.rateBasis}` : ""}` : rate.rateBasis;
  ui.sheetBox.set(
    Number.isFinite(rate.sheetRate)
      ? `${rate.sheetRate.toFixed(2)}${sheetUnit ? ` ${sheetUnit}` : ""}`
      : "–",
  );
  ui.rateEntity = rate.entity;
}

/**
 * The tariff sensors, in one place: the current network band, and the
 * demand-charge headroom, period aggregate and rate -- none of which is
 * anywhere but the entity list otherwise. See
 * planning/tariff_card_plan.md.
 *
 * Never blank. Most installs never configure a network profile at all, so a
 * card added before one is the common case, not the exception -- it draws
 * `SAMPLE` instead, ribboned and desaturated, until `sensor.*_
 * network_tariff_band` and `sensor.*_period_peak` both resolve. `show_demo:
 * false` restores a blank card for that state instead.
 */
class EmhassTariffCard extends LiveCard {
  static getStubConfig() {
    return { type: "custom:emhass-tariff-card" };
  }

  static async getConfigElement() {
    await loadHaForm();
    return document.createElement("emhass-tariff-card-editor");
  }

  getCardSize() {
    const layout = this._layout || {};
    let size = 1;
    if (layout.isDemo) size += 1;
    if (layout.band) size += 1;
    if (layout.demand) size += 3;
    if (layout.rate) size += 1;
    return size;
  }

  _headroomWarnPct() {
    const value = Number(this._config.headroom_warn_pct);
    return Number.isFinite(value) ? value : DEFAULT_HEADROOM_WARN_PCT;
  }

  build(card) {
    const ui = {};
    this._ui = ui;

    const hub = findHub(this._hass);
    const hasBand = Boolean(hub["sensor.network_tariff_band"]);
    const hasDemand = Boolean(hub["sensor.period_peak"]);
    const isDemo = !hasBand && !hasDemand && showsSection(this._config, "show_demo");
    const layout = {
      isDemo,
      band: showsSection(this._config, "show_band") && (hasBand || isDemo),
      demand: showsSection(this._config, "show_demand") && (hasDemand || isDemo),
    };
    layout.rate = layout.demand && showsSection(this._config, "show_rate");
    this._layout = layout;

    const pad = tag("div", "pad", card);
    if (layout.isDemo) {
      tag("div", "ribbon", pad, "Sample data — pick a network tariff to see your own");
    }
    const body = tag("div", layout.isDemo ? "body demo" : "body", pad);

    if (layout.band) {
      ui.band = tag("div", "tariff-banner", body);
      ui.bandTitle = tag("div", "band-title", ui.band, "");
      ui.bandSub = tag("div", "band-sub", ui.band, "");
      ui.band.addEventListener("click", () => moreInfo(this, ui.bandEntity));
    }

    if (layout.band && layout.demand) {
      tag("div", "section", body, "Demand charge");
    }

    if (layout.demand) {
      const demand = tag("div", "demand", body);
      ui.arc = headroomArc(demand);
      ui.arc.addEventListener("click", () => moreInfo(this, ui.headroomEntity));
      ui.periodLine = tag("div", "period-line", demand, "");
      ui.periodLine.addEventListener("click", () => moreInfo(this, ui.periodEntity));
      ui.pill = tag("div", "pill", demand);
      tag("span", "dot", ui.pill);
      ui.pillText = tag("span", null, ui.pill, "");
      ui.pill.addEventListener("click", () => moreInfo(this, ui.pillEntity));
    }

    if (layout.rate) {
      const stats = tag("div", "stats rate-row", body);
      ui.rateBox = valueBox(stats, "Effective rate");
      ui.sheetBox = valueBox(stats, "Sheet rate");
      ui.rateBox.classList.add("tap");
      ui.sheetBox.classList.add("tap");
      ui.rateBox.addEventListener("click", () => moreInfo(this, ui.rateEntity));
      ui.sheetBox.addEventListener("click", () => moreInfo(this, ui.rateEntity));
    }
  }

  update(hass) {
    this._hub = findHub(hass);
    const view = tariffView(hass, this._hub, this._layout.isDemo);
    renderBand(this._ui, view.band);
    renderDemand(this._ui, view.demand, this._headroomWarnPct());
    renderRate(this._ui, view.rate);
  }
}

EmhassTariffCard.ticks = true;
EmhassTariffCard.css = `
  .ribbon { display: flex; align-items: center; font-size: .74rem; font-weight: 500;
            color: var(--emh-accent); background: rgba(3, 169, 244, .12);
            background: color-mix(in srgb, var(--emh-accent) 12%, transparent);
            border-radius: 10px; padding: 7px 10px; margin-bottom: 10px; }
  /* Same technique as a disabled control elsewhere in the Companion
     (see TOKENS' .row.disabled): quiet enough not to be mistaken for a real
     read, present enough to still show the card's shape. */
  .body.demo { opacity: .45; pointer-events: none; }

  .tariff-banner { padding: 12px; border-radius: var(--emh-radius); background: var(--emh-surface);
                    cursor: pointer; transition: background 160ms; }
  .tariff-banner:hover { background: var(--emh-surface-2); }
  .band-title { font-size: 1.02rem; font-weight: 500; }
  .band-sub { font-size: .78rem; color: var(--emh-dim); margin-top: 2px; }

  .section { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em;
             color: var(--emh-dim); margin: 16px 0 8px 0; }

  /* --- headroom arc ---------------------------------------------------- */
  .arc-wrap { position: relative; max-width: 260px; margin: 4px auto 0 auto; cursor: pointer; }
  .arc-track { fill: none; stroke: var(--emh-surface-2); stroke-width: 14; stroke-linecap: round; }
  .arc-fill { fill: none; stroke: var(--emh-battery); stroke-width: 14; stroke-linecap: round;
              transition: stroke-dashoffset 500ms var(--emh-ease), stroke 300ms; }
  .arc-fill.warn { stroke: var(--emh-bad); }
  .arc-value { position: absolute; left: 0; right: 0; top: 56%; transform: translateY(-50%);
               text-align: center; font-size: 1.3rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .arc-sub { position: absolute; left: 0; right: 0; top: 78%; transform: translateY(-50%);
             text-align: center; font-size: .72rem; color: var(--emh-dim); }

  .period-line { text-align: center; font-size: .82rem; color: var(--emh-dim);
                 margin-top: 2px; cursor: pointer; }
  .demand .pill { margin: 10px auto 0 auto; width: fit-content; cursor: pointer; }

  .rate-row { margin-top: 16px; }
`;

/* ---------------------------------------------------- tariff visual editor */

const TARIFF_SECTIONS = [
  ["show_band", "Band banner", "The current band, its adder, and the countdown to the next one"],
  ["show_demand", "Demand section", "The headroom arc, the period's aggregate, and the pricing/fallback pill"],
  ["show_rate", "Rate row", "The effective rate against the sheet rate, for checking the conversion by eye"],
  ["show_demo", "Sample data", "Shown in place of a blank card before a network tariff is configured"],
];

const TARIFF_LABELS = { headroom_warn_pct: "Headroom warning threshold" };
const TARIFF_HELPERS = {
  headroom_warn_pct:
    "The arc turns red once headroom drops below this percentage of the period's own peak.",
};
for (const section of TARIFF_SECTIONS) {
  TARIFF_LABELS[section[0]] = section[1];
  TARIFF_HELPERS[section[0]] = section[2];
}

const TARIFF_SCHEMA = [
  sectionGrid(TARIFF_SECTIONS),
  {
    name: "",
    type: "grid",
    column_min_width: "220px",
    schema: [
      {
        name: "headroom_warn_pct",
        selector: { number: { min: 0, max: 100, mode: "box", unit_of_measurement: "%" } },
      },
    ],
  },
];

const TARIFF_KEYS = TARIFF_SECTIONS.map((section) => section[0]);

class EmhassTariffCardEditor extends CardEditor {
  get labels() {
    return TARIFF_LABELS;
  }

  get helpers() {
    return TARIFF_HELPERS;
  }

  schema() {
    return TARIFF_SCHEMA;
  }

  data() {
    const value = Number(this._config.headroom_warn_pct);
    const data = {
      headroom_warn_pct: Number.isFinite(value) ? value : DEFAULT_HEADROOM_WARN_PCT,
    };
    for (const key of TARIFF_KEYS) data[key] = showsSection(this._config, key);
    return data;
  }

  clean(config) {
    for (const key of TARIFF_KEYS) {
      if (config[key] === true) delete config[key];
    }
    if (Number(config.headroom_warn_pct) === DEFAULT_HEADROOM_WARN_PCT) delete config.headroom_warn_pct;
    return config;
  }
}

customElements.define("emhass-tariff-card", EmhassTariffCard);
customElements.define("emhass-tariff-card-editor", EmhassTariffCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "emhass-tariff-card",
    name: "EMHASS Companion tariff",
    description: "The network tariff band, and the demand-charge headroom, aggregate and rate, in one place.",
    preview: true,
    documentationURL: "https://github.com/smefa/emhass-ha-companion",
  },
);
