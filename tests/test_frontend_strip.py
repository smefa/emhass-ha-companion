"""Exercise strip controls and service calls using the real card JS under Node."""

from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

CARDS = (
    Path(__file__).parents[1]
    / "custom_components/emhass_companion/frontend/emhass-deferrable-cards.js"
)

SCRIPT = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
class Element {
  constructor() {
    this.style = {}; this.children = []; this.attrs = {}; this.events = {};
    this.offsetHeight = 48;
    this.classList = { toggle: (k, on) => { this[k] = on; } };
  }
  appendChild(el) { this.children.push(el); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(k, fn) { this.events[k] = fn; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const document = { createElement: () => new Element() };
const tag = (name, cls, parent, text) => {
  const el = new Element(); el.tag = name; el.textContent = text;
  if (parent) parent.appendChild(el);
  return el;
};
class LiveCard {}
class CardEditor {}
const calls = [];
const haptic = () => {};
const moreInfo = () => {};
const num = s => s && !['unknown', 'unavailable'].includes(s.state) ? Number(s.state) : NaN;
const formatHours = h => `${h} h`;
const states = {
  load_requested: {state:'off'}, run_now: {state:'unknown'},
  run_within: {state:'10', attributes:{min:1, max:48, step:0.5}},
  operating_hours: {state:'2', attributes:{min:0, max:24, step:0.25}},
};
const load = {entities: Object.fromEntries(Object.keys(states).map(k=>[k, `test.${k}`]))};
const resolveLoad = () => load;
const view = {
  name:'Dishwasher', status:'idle', onDemand:true, onSurplus:false,
  isRequested:false, points:Array.from({length:48}, (_,i)=>({t:i*1800000, v:i===47?1000:0})),
  runs:[{start:47*1800000, end:47*1800000}],
  find:k=>states[k],
};
const loadView = () => view;
const STATUS_META = {idle:{text:'Idle', cls:'', icon:'mdi:sleep', sq:''}};
const subtitleFor = () => 'Waiting';
const pressButton = (hass, id) => calls.push(['press', id]);
const toggleEntity = (hass, id) => calls.push(['toggle', id]);
const setNumber = (hass, id, value) => calls.push(['number', id, value]);
const DEFERRABLE_LABELS = {}, DEFERRABLE_HELPERS = {};
const loadField = () => ({});
const sectionGrid = sections => sections;
const cleanSections = (config, sections, off) => {
  for (const [key] of sections) if(config[key] === !off.has(key)) delete config[key];
};
eval(source.slice(source.indexOf('const STRIP_CONTROLS'),
                  source.indexOf('EmhassDeferrableStripCard.ticks')) +
     source.slice(source.indexOf('const STRIP_LABELS'),
                  source.indexOf('customElements.define')) + `
const card = new EmhassDeferrableStripCard();
card._config = {}; card._hass = {}; card.build(new Element()); card.update({});
const ui = card._ui;
assert.equal(ui.hoursButton.hidden, true);
assert.equal(ui.withinButton.hidden, false);
assert.equal(ui.horizon.textContent, '24 h');
assert.equal(ui.buckets.children.at(-1).attrs['data-on'], '', 'final schedule step is included');
assert.equal(ui.requestButton.enabled, false);
assert.equal(ui.runButton.enabled, true, 'a never-pressed HA button is available');
view.isRequested = true; states.run_now.state = '2026-11-14'; card.update({});
assert.equal(ui.requestButton.enabled, true);
assert.equal(ui.runButton.enabled, true);
ui.requestButton.events.click(); ui.runButton.events.click();
assert.deepEqual(calls, [['toggle','test.load_requested'], ['press','test.run_now']]);
ui.chevron.events.click();
assert.equal(ui.drawer.inert, false);
ui.withinButton.events.click();
const deadlineDialog = ui.drawerBody.children.find(el=>el.tag==='dialog' && el.attrs['aria-label']==='Run within');
const deadlineInput = deadlineDialog.children.find(el=>el.tag==='input');
assert.equal(Number(deadlineInput.min), 2, 'deadline cannot precede operating hours');
deadlineInput.value = '12.5'; deadlineInput.events.change();
assert.deepEqual(calls.at(-1), ['number','test.run_within',12.5]);
deadlineDialog.close();
card._config.show_hours_needed = true; card.update({});
assert.equal(ui.hoursButton.hidden, false);
ui.hoursButton.events.click();
const dialog = ui.drawerBody.children.find(el=>el.tag==='dialog' && el.attrs['aria-label']==='Hours needed');
const input = dialog.children.find(el=>el.tag==='input');
assert.equal(input.step, 0.25);
input.value = '3.25'; input.events.input(); card.update({});
assert.equal(input.value, '3.25', 'live update preserves an open edit');
input.events.change();
assert.deepEqual(calls.at(-1), ['number','test.operating_hours',3.25]);
states.operating_hours.state='unavailable'; card.update({});
assert.equal(ui.hoursButton.disabled, true); assert.equal(dialog.open, false);
for (const [key] of STRIP_CONTROLS) card._config[key] = false;
card.update({});
assert.equal(ui.chevron.hidden, true);
assert.equal(card._expanded, false); assert.equal(ui.drawer.inert, true);
card._config = {}; card.update({});
assert.equal(ui.chevron.hidden, false);
view.onDemand=false; view.onSurplus=false; delete load.entities.run_now;
card.update({}); assert.equal(ui.chevron.hidden, true);
const editor = new EmhassDeferrableStripCardEditor(); editor._config={};
assert.equal(editor.data().show_hours_needed, false);
assert.equal(editor.data().show_run_now, true);
assert.deepEqual(editor.clean({show_hours_needed:false, show_run_now:true}), {});
assert.deepEqual(editor.clean({show_hours_needed:true, show_run_now:false}),
                 {show_hours_needed:true, show_run_now:false});
`);
"""


def test_strip_controls() -> None:
    subprocess.run(["node", "-e", SCRIPT, str(CARDS)], check=True, text=True)
