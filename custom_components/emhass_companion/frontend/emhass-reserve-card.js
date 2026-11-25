/**
 * Battery reserve card for EMHASS 0.19.0+ `soc_floor`.
 *
 * Lets the user schedule a temporary minimum battery SoC (e.g. storm prep)
 * using one-click presets or a custom window, shows active/scheduled status
 * and countdowns, and calls `emhass_companion.set_battery_reserve` /
 * `emhass_companion.clear_battery_reserve`.
 */

import {
  CardEditor,
  COLORS,
  LiveCard,
  callService,
  findHub,
  formatCountdown,
  formatTime,
  haptic,
  loadHaForm,
  moreInfo,
  stateOf,
  tag,
} from "./emhass-core.js?v=__VERSION__";

const DOMAIN = "emhass_companion";
const DEFAULT_TARGET_SOC = 80;
const HOUR_MS = 3600000;

function toLocalInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function parseLocalInputValue(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function clamp(value, lo, hi) {
  return Math.min(Math.max(value, lo), hi);
}

function roundToQuarterHour(date) {
  const ms = 15 * 60 * 1000;
  return new Date(Math.ceil(date.getTime() / ms) * ms);
}

function zonedParts(date, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const map = {};
  for (let i = 0; i < parts.length; i += 1) {
    map[parts[i].type] = Number(parts[i].value);
  }
  return map;
}

function zonedClockToDate(year, month, day, hour, minute, timeZone) {
  const targetUtc = Date.UTC(year, month - 1, day, hour, minute, 0, 0);
  let guess = targetUtc;
  for (let i = 0; i < 2; i += 1) {
    const p = zonedParts(new Date(guess), timeZone);
    const seenUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, 0, 0);
    guess += targetUtc - seenUtc;
  }
  return new Date(guess);
}

function nextClockTime(hour, minute, timeZone) {
  const now = new Date();
  if (timeZone) {
    try {
      const p = zonedParts(now, timeZone);
      let target = zonedClockToDate(p.year, p.month, p.day, hour, minute || 0, timeZone);
      if (target.getTime() <= now.getTime() + 10 * 60 * 1000) {
        const nextDay = new Date(Date.UTC(p.year, p.month - 1, p.day + 1));
        target = zonedClockToDate(
          nextDay.getUTCFullYear(),
          nextDay.getUTCMonth() + 1,
          nextDay.getUTCDate(),
          hour,
          minute || 0,
          timeZone
        );
      }
      return target;
    } catch (_err) {
      // Fall back to the browser's local timezone if timeZone is unsupported.
    }
  }
  const target = new Date(now);
  target.setHours(hour, minute || 0, 0, 0);
  if (target.getTime() <= now.getTime() + 10 * 60 * 1000) {
    target.setDate(target.getDate() + 1);
  }
  return target;
}

class EmhassReserveCard extends LiveCard {
  static getConfigElement() {
    loadHaForm();
    return document.createElement("emhass-reserve-card-editor");
  }

  static getStubConfig() {
    return {};
  }

  getCardSize() {
    return 5;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: 5, min_rows: 4 };
  }

  build(card) {
    this._userEdited = false;
    this._busy = false;
    this._entityId = null;

    const wrap = tag("div", "wrap", card);

    const head = tag("div", "head", wrap);
    this._title = tag("div", "title", head, this._config.title || "Battery reserve");
    this._title.addEventListener("click", () => {
      if (this._entityId) moreInfo(this, this._entityId);
    });
    this._pill = tag("span", "pill", head, "Off");

    this._unsupported = tag(
      "div",
      "banner warn hidden",
      wrap,
      "Battery reserve requires EMHASS 0.19.0 or newer."
    );

    this._missing = tag(
      "div",
      "banner hidden",
      wrap,
      "No EMHASS Companion battery reserve sensor found. Enable a battery in EMHASS Companion first."
    );

    this._statusBox = tag("div", "banner hidden", wrap);
    const statusRow = tag("div", "banner-row", this._statusBox);
    this._statusMain = tag("div", "", statusRow);
    this._clearBtn = tag("button", "btn danger", statusRow, "Clear");
    this._clearBtn.type = "button";
    this._clearBtn.addEventListener("click", () => this._clearReserve());
    this._statusMeta = tag("div", "meta", this._statusBox);

    const presets = tag("div", "presets", wrap);
    this._presets = presets;
    this._addPreset(presets, "Storm prep (+4h → +16h)", () => {
      const start = roundToQuarterHour(new Date(Date.now() + 4 * HOUR_MS));
      const end = new Date(start.getTime() + 12 * HOUR_MS);
      this._fillForm(80, start, end, "Storm prep");
    });
    this._addPreset(presets, "Tonight (20:00 → 08:00)", () => {
      const tz =
        this._hass && this._hass.config && this._hass.config.time_zone
          ? this._hass.config.time_zone
          : null;
      const start = nextClockTime(20, 0, tz);
      const end = new Date(start.getTime() + 12 * HOUR_MS);
      this._fillForm(80, start, end, "Overnight reserve");
    });
    this._addPreset(presets, "Hold now (6h)", () => {
      const end = roundToQuarterHour(new Date(Date.now() + 6 * HOUR_MS));
      this._fillForm(80, null, end, "Hold reserve");
    });

    const form = tag("form", "form", wrap);
    this._form = form;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      this._submitReserve();
    });

    const socField = tag("div", "field", form);
    const socLabel = tag("label", "", socField);
    tag("span", "", socLabel, "Target minimum SoC");
    this._socOut = tag("span", "soc-value", socLabel, `${DEFAULT_TARGET_SOC}%`);
    this._socInput = tag("input", "", socField);
    this._socInput.type = "range";
    this._socInput.min = "10";
    this._socInput.max = "100";
    this._socInput.step = "1";
    this._socInput.value = String(DEFAULT_TARGET_SOC);
    this._socInput.addEventListener("input", () => {
      this._userEdited = true;
      this._socOut.textContent = `${this._socInput.value}%`;
    });

    const timesRow = tag("div", "field-row", form);
    const startField = tag("div", "field", timesRow);
    tag("label", "", startField, "Start (blank = now)");
    this._startInput = tag("input", "", startField);
    this._startInput.type = "datetime-local";
    this._startInput.addEventListener("input", () => {
      this._userEdited = true;
    });

    const endField = tag("div", "field", timesRow);
    tag("label", "", endField, "End");
    this._endInput = tag("input", "", endField);
    this._endInput.type = "datetime-local";
    this._endInput.required = true;
    this._endInput.addEventListener("input", () => {
      this._userEdited = true;
    });

    const reasonField = tag("div", "field", form);
    tag("label", "", reasonField, "Reason (optional)");
    this._reasonInput = tag("input", "", reasonField);
    this._reasonInput.type = "text";
    this._reasonInput.placeholder = "Storm warning";
    this._reasonInput.maxLength = 80;
    this._reasonInput.addEventListener("input", () => {
      this._userEdited = true;
    });

    const actions = tag("div", "actions", form);
    this._submitBtn = tag("button", "btn", actions, "Set reserve");
    this._submitBtn.type = "submit";
    this._replanBtn = tag("button", "btn secondary", actions, "Replan now");
    this._replanBtn.type = "button";
    this._replanBtn.addEventListener("click", () => this._replanNow());

    this._feedback = tag("div", "feedback", wrap);

    // Seed default window (+4h -> +16h) so the form is ready with one click.
    const defaultStart = roundToQuarterHour(new Date(Date.now() + 4 * HOUR_MS));
    const defaultEnd = new Date(defaultStart.getTime() + 12 * HOUR_MS);
    this._startInput.value = toLocalInputValue(defaultStart);
    this._endInput.value = toLocalInputValue(defaultEnd);
  }

  _addPreset(parent, label, onClick) {
    const btn = tag("button", "chip", parent, label);
    btn.type = "button";
    btn.addEventListener("click", () => {
      haptic("selection");
      onClick();
    });
  }

  _fillForm(targetSoc, start, end, reason) {
    this._userEdited = true;
    const min = Number(this._socInput.min) || 10;
    const max = Number(this._socInput.max) || 100;
    const clamped = Math.round(clamp(targetSoc, min, max));
    this._socInput.value = String(clamped);
    this._socOut.textContent = `${this._socInput.value}%`;
    this._startInput.value = start ? toLocalInputValue(start) : "";
    this._endInput.value = toLocalInputValue(end);
    this._reasonInput.value = reason || "";
    this._setFeedback("", false);
  }

  _setFeedback(message, isOk) {
    this._feedback.textContent = message;
    this._feedback.classList.toggle("ok", Boolean(isOk));
  }

  _setBusy(busy) {
    this._busy = busy;
    this._submitBtn.disabled = busy;
    this._clearBtn.disabled = busy;
    this._replanBtn.disabled = busy;
  }

  async _submitReserve() {
    if (!this._hass || this._busy) return;
    const endDate = parseLocalInputValue(this._endInput.value);
    if (!endDate) {
      this._setFeedback("Choose an end time.", false);
      return;
    }
    const startDate = parseLocalInputValue(this._startInput.value);
    if (startDate && endDate.getTime() <= startDate.getTime()) {
      this._setFeedback("End time must be after start time.", false);
      return;
    }
    if (endDate.getTime() <= Date.now()) {
      this._setFeedback("End time must be in the future.", false);
      return;
    }

    const data = {
      target_soc: Number(this._socInput.value),
      end_time: endDate.toISOString(),
    };
    if (startDate) data.start_time = startDate.toISOString();
    const reason = this._reasonInput.value.trim();
    if (reason) data.reason = reason;

    this._setBusy(true);
    this._setFeedback("", false);
    try {
      await callService(this._hass, DOMAIN, "set_battery_reserve", data);
      this._userEdited = false;
      this._setFeedback("Reserve saved — replanning…", true);
    } catch (err) {
      this._setFeedback((err && err.message) || "Could not set battery reserve.", false);
    } finally {
      this._setBusy(false);
    }
  }

  async _clearReserve() {
    if (!this._hass || this._busy) return;
    this._setBusy(true);
    this._setFeedback("", false);
    try {
      await callService(this._hass, DOMAIN, "clear_battery_reserve", {});
      this._userEdited = false;
      this._setFeedback("Reserve cleared.", true);
    } catch (err) {
      this._setFeedback((err && err.message) || "Could not clear battery reserve.", false);
    } finally {
      this._setBusy(false);
    }
  }

  async _replanNow() {
    if (!this._hass || this._busy) return;
    this._setBusy(true);
    this._setFeedback("Running MPC optimisation…", true);
    try {
      await callService(this._hass, DOMAIN, "run_mpc", {});
      this._setFeedback("Plan updated.", true);
    } catch (err) {
      this._setFeedback((err && err.message) || "Could not run MPC optimisation.", false);
    } finally {
      this._setBusy(false);
    }
  }

  update(hass) {
    const hub = findHub(hass);
    this._entityId = this._config.entity || hub["sensor.battery_reserve"] || null;
    const entity = stateOf(hass, this._entityId);

    if (!entity) {
      this._missing.classList.remove("hidden");
      this._unsupported.classList.add("hidden");
      this._statusBox.classList.add("hidden");
      this._presets.classList.add("hidden");
      this._form.classList.add("hidden");
      this._pill.textContent = "Unavailable";
      this._pill.dataset.state = "off";
      return;
    }

    this._missing.classList.add("hidden");
    this._presets.classList.remove("hidden");
    this._form.classList.remove("hidden");

    const state = entity.state || "off";
    const attrs = entity.attributes || {};
    const supported = attrs.supported !== false;
    this._unsupported.classList.toggle("hidden", supported);

    const socMin = Number.isFinite(Number(attrs.soc_min)) ? Number(attrs.soc_min) : 10;
    const socMax = Number.isFinite(Number(attrs.soc_max)) ? Number(attrs.soc_max) : 100;
    const sliderMax = Math.floor(socMax);
    const sliderMin = Math.min(Math.floor(socMin) + 1, sliderMax);
    this._socInput.min = String(sliderMin);
    this._socInput.max = String(sliderMax);

    const startMs = attrs.start_time ? Date.parse(attrs.start_time) : NaN;
    const endMs = attrs.end_time ? Date.parse(attrs.end_time) : NaN;
    if (!this._userEdited) {
      const desired =
        attrs.target_soc != null
          ? Number(attrs.target_soc)
          : clamp(DEFAULT_TARGET_SOC, sliderMin, sliderMax);
      const rounded = Math.round(clamp(desired, sliderMin, sliderMax));
      this._socInput.value = String(rounded);
      this._socOut.textContent = `${this._socInput.value}%`;
      if (state === "active" || state === "scheduled") {
        if (Number.isFinite(startMs)) {
          this._startInput.value = toLocalInputValue(new Date(startMs));
        }
        if (Number.isFinite(endMs)) {
          this._endInput.value = toLocalInputValue(new Date(endMs));
        }
        this._reasonInput.value = attrs.reason || "";
      }
    }

    const labels = { off: "Off", scheduled: "Scheduled", active: "Active" };
    this._pill.textContent = labels[state] || state;
    this._pill.dataset.state = state;

    if (state === "active" || state === "scheduled") {
      this._statusBox.classList.remove("hidden");
      const target = attrs.target_soc != null ? `${Math.round(Number(attrs.target_soc))}%` : "—";
      const startStr = Number.isFinite(startMs) ? formatTime(startMs, hass) : "";
      const endStr = Number.isFinite(endMs) ? formatTime(endMs, hass) : "";
      let prefix;
      if (state === "active" && attrs.holding_below_target && attrs.current_soc != null) {
        prefix = `Holding at ${Math.round(Number(attrs.current_soc))}% (below ${target} target)`;
      } else {
        prefix = state === "active" ? `Holding ≥ ${target}` : `Scheduled ≥ ${target}`;
      }
      this._statusMain.textContent = `${prefix} · ${startStr} → ${endStr}`;

      const parts = [];
      if (state === "scheduled" && Number.isFinite(startMs)) {
        parts.push(`starts in ${formatCountdown(startMs - Date.now())}`);
      } else if (state === "active" && Number.isFinite(endMs)) {
        parts.push(`ends in ${formatCountdown(endMs - Date.now())}`);
      }
      if (attrs.reason) parts.push(attrs.reason);
      this._statusMeta.textContent = parts.join(" · ");
    } else {
      this._statusBox.classList.add("hidden");
    }
  }
}

EmhassReserveCard.ticks = true;
EmhassReserveCard.css = `
  .wrap {
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .title {
    font-size: 16px;
    font-weight: 500;
    color: var(--primary-text-color);
    cursor: pointer;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 500;
    background: var(--em-surface, rgba(127, 127, 127, 0.14));
    color: var(--secondary-text-color);
  }
  .pill[data-state="active"] {
    background: color-mix(in srgb, ${COLORS.battery} 22%, transparent);
    color: ${COLORS.battery};
  }
  .pill[data-state="scheduled"] {
    background: color-mix(in srgb, ${COLORS.pv} 22%, transparent);
    color: ${COLORS.pv};
  }
  .banner {
    padding: 10px 12px;
    border-radius: var(--em-radius, 12px);
    background: var(--em-surface, rgba(127, 127, 127, 0.1));
    font-size: 13px;
    line-height: 1.4;
    color: var(--primary-text-color);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .banner.warn {
    background: color-mix(in srgb, var(--emh-warn, #ffa600) 16%, transparent);
    color: var(--primary-text-color);
  }
  .banner .meta {
    font-size: 12px;
    color: var(--secondary-text-color);
  }
  .banner-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }
  .presets {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    border: 1px solid var(--divider-color, rgba(127, 127, 127, 0.25));
    background: transparent;
    color: var(--primary-text-color);
    border-radius: 999px;
    padding: 5px 11px;
    font: inherit;
    font-size: 12px;
    cursor: pointer;
  }
  .chip:hover {
    background: var(--em-surface, rgba(127, 127, 127, 0.12));
  }
  .form {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .field-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }
  @media (max-width: 440px) {
    .field-row {
      grid-template-columns: 1fr;
    }
  }
  label {
    font-size: 12px;
    color: var(--secondary-text-color);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .soc-value {
    font-weight: 600;
    color: var(--primary-text-color);
    font-variant-numeric: tabular-nums;
  }
  input[type="range"] {
    width: 100%;
    accent-color: ${COLORS.battery};
  }
  input[type="datetime-local"],
  input[type="text"] {
    width: 100%;
    box-sizing: border-box;
    padding: 7px 9px;
    border-radius: 8px;
    border: 1px solid var(--divider-color, rgba(127, 127, 127, 0.3));
    background: var(--card-background-color, transparent);
    color: var(--primary-text-color);
    font: inherit;
    font-size: 13px;
  }
  .actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .btn {
    border: none;
    border-radius: 10px;
    padding: 8px 14px;
    font: inherit;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    background: var(--primary-color, ${COLORS.battery});
    color: var(--text-primary-color, #fff);
  }
  .btn.secondary {
    background: var(--em-surface, rgba(127, 127, 127, 0.16));
    color: var(--primary-text-color);
  }
  .btn.danger {
    background: color-mix(in srgb, var(--emh-bad, #db4437) 18%, transparent);
    color: var(--emh-bad, #db4437);
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .feedback {
    font-size: 12px;
    color: var(--emh-bad, #db4437);
    min-height: 16px;
  }
  .feedback.ok {
    color: var(--secondary-text-color);
  }
  .hidden {
    display: none !important;
  }
`;

const RESERVE_SCHEMA = [
  { name: "title", selector: { text: {} } },
  { name: "entity", selector: { entity: { domain: "sensor" } } },
];

const RESERVE_LABELS = {
  title: "Title",
  entity: "Battery reserve sensor (optional)",
};

class EmhassReserveCardEditor extends CardEditor {
  get labels() {
    return RESERVE_LABELS;
  }

  schema() {
    return RESERVE_SCHEMA;
  }

  data() {
    return {
      title: this._config.title || "",
      entity: this._config.entity || "",
    };
  }

  clean(config) {
    if (!config.title) delete config.title;
    if (!config.entity) delete config.entity;
    return config;
  }
}

customElements.define("emhass-reserve-card", EmhassReserveCard);
customElements.define("emhass-reserve-card-editor", EmhassReserveCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "emhass-reserve-card",
  name: "EMHASS Companion battery reserve",
  description: "Schedule a temporary minimum battery SoC (EMHASS 0.19.0+ soc_floor), e.g. before a storm.",
  preview: true,
  documentationURL: "https://github.com/smefa/emhass-ha-companion",
});
