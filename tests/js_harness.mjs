/* js_harness.mjs — headless integration test for the AnnotatorAPI plugin surface.
 *
 * Runs the REAL static/app.js + static/rot_tool.js + static/measure_tool.js in a
 * shared vm context against a hand-rolled (jsdom-free) DOM stub and a stubbed
 * server, then:
 *   1. boots the app with a synthetic plate (proves no crash blanks the viewer),
 *   2. builds window.AnnotatorAPI and installs both plugins,
 *   3. activates the seeded `rotation` (angle) column, drags on #stage → asserts a
 *      numeric `rotation` keyframe was written AND #bigImg.style.transform was set,
 *   4. activates a `measurement` column, two clicks on #stage → asserts a
 *      {line,length_px,length_um} object was written via doAssign,
 *   5. re-checks the forward-fill `slice` keyframe behaviour is intact,
 *   6. renders the Well / Plate / Image tabs without throwing.
 *
 * Run:  node tests/js_harness.mjs      (from the label_annotator/ app root)
 * No dependencies, no build step.
 */
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(HERE, '..');
const STATIC = path.join(APP_ROOT, 'static');

// ------------------------------------------------------------------ tiny assert
let passed = 0, failed = 0;
function ok(cond, name){ if (cond){ passed++; console.log('  ok   ' + name); } else { failed++; console.log('  FAIL ' + name); } }
function eq(a, b, name){ ok(a === b, name + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ============================================================ DOM stub
class StubText {
  constructor(t, doc){ this.nodeType = 3; this._doc = doc; this.textContent = String(t); this.parentNode = null; }
}
class StubElement {
  constructor(tag, ns, doc){
    this.tagName = String(tag || '').toUpperCase();
    this.nodeName = this.tagName;
    this.namespaceURI = ns || 'http://www.w3.org/1999/xhtml';
    this._doc = doc;
    this.children = [];
    this.parentNode = null;
    this._attrs = {};
    this.dataset = {};
    this.style = {};
    this._classes = new Set();
    this._className = '';
    this._id = '';
    this._text = '';
    this._html = '';
    this._listeners = {};
    this.value = '';
    this.hidden = false; this.disabled = false; this.draggable = false;
    this.title = ''; this.placeholder = ''; this.type = ''; this.size = 0;
    this.loading = ''; this.src = '';
    this.naturalWidth = 0; this.naturalHeight = 0;
    this.clientWidth = 0; this.clientHeight = 0;
    this.onclick = null; this.onchange = null; this.oninput = null; this.onkeydown = null;
    this._rect = { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
  }
  // ---- identity / classes ----
  get id(){ return this._id; }
  set id(v){ this._id = String(v); }
  get className(){ return this._className; }
  set className(v){ this._className = String(v || ''); this._classes = new Set(this._className.split(/\s+/).filter(Boolean)); }
  get classList(){
    const self = this;
    return {
      add(...c){ c.forEach(x => self._classes.add(x)); self._sync(); },
      remove(...c){ c.forEach(x => self._classes.delete(x)); self._sync(); },
      toggle(c, force){ const has = self._classes.has(c); const add = force === undefined ? !has : !!force;
        if (add) self._classes.add(c); else self._classes.delete(c); self._sync(); return add; },
      contains(c){ return self._classes.has(c); },
    };
  }
  _sync(){ this._className = [...this._classes].join(' '); }
  matches(sel){ return String(sel).split(',').some(s => matchOne(this, s.trim())); }
  get isConnected(){ return true; }
  // ---- text / html ----
  get textContent(){ return this._text; }
  set textContent(v){ this._text = v == null ? '' : String(v); this.children = []; this._html = ''; }
  get innerHTML(){ return this._html; }
  set innerHTML(v){ this._html = v == null ? '' : String(v); this.children = []; this._text = ''; }
  // ---- attributes ----
  setAttribute(k, v){ this._attrs[k] = String(v); if (k === 'class') this.className = String(v); if (k === 'id') this._id = String(v); }
  setAttributeNS(_ns, k, v){ this.setAttribute(k, v); }
  getAttribute(k){ return k in this._attrs ? this._attrs[k] : null; }
  removeAttribute(k){ delete this._attrs[k]; }
  hasAttribute(k){ return k in this._attrs; }
  // ---- tree ----
  get firstChild(){ return this.children[0] || null; }
  appendChild(c){ if (c == null) return c; if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  append(...cs){ for (const c of cs){ if (typeof c === 'string') this.appendChild(new StubText(c, this._doc)); else this.appendChild(c); } }
  removeChild(c){ const i = this.children.indexOf(c); if (i >= 0){ this.children.splice(i, 1); c.parentNode = null; } return c; }
  insertBefore(n, ref){ const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(n); else this.children.splice(i, 0, n); n.parentNode = this; return n; }
  remove(){ if (this.parentNode) this.parentNode.removeChild(this); }
  // ---- queries ----
  querySelector(sel){ return queryAll(this, sel)[0] || null; }
  querySelectorAll(sel){ return queryAll(this, sel); }
  // ---- events ----
  addEventListener(type, fn, opts){ (this._listeners[type] || (this._listeners[type] = [])).push(fn); }
  removeEventListener(type, fn){ const l = this._listeners[type]; if (l) this._listeners[type] = l.filter(x => x !== fn); }
  dispatchEvent(ev){ ev.target = ev.target || this; ev.currentTarget = this;
    for (const fn of (this._listeners[ev.type] || []).slice()){ try { fn.call(this, ev); } catch (e){ console.error('listener error [' + ev.type + ']:', e && e.stack || e); throw e; } }
    return true; }
  // ---- geometry ----
  getBoundingClientRect(){ return this._rect; }
  getBBox(){ return { x: 0, y: 0, width: (this._text ? this._text.length * 6 : 20), height: 12 }; }
}
function descendants(el){
  const out = [];
  const stack = el.children ? [...el.children] : [];
  while (stack.length){ const c = stack.shift(); if (c && c.tagName){ out.push(c); if (c.children && c.children.length) stack.unshift(...c.children); } }
  return out;
}
function matchOne(el, token){
  token = token.trim();
  if (!token || token === '*') return true;
  if (token[0] === '#') return el._id === token.slice(1);
  if (token[0] === '.') return el._classes && el._classes.has(token.slice(1));
  return el.tagName === token.toUpperCase();
}
function queryAll(root, sel){
  const out = [];
  for (const group of String(sel).split(',')){
    const parts = group.trim().split(/\s+/).filter(Boolean);
    if (!parts.length) continue;
    let ctx = [root];
    for (const part of parts){
      const next = [];
      for (const c of ctx) for (const d of descendants(c)) if (matchOne(d, part) && !next.includes(d)) next.push(d);
      ctx = next;
    }
    for (const e of ctx) if (!out.includes(e)) out.push(e);
  }
  return out;
}

// ---- document ----
function makeDocument(){
  const doc = {
    _listeners: {},
    activeElement: null,
    createElement(tag){ return new StubElement(tag, 'http://www.w3.org/1999/xhtml', this); },
    createElementNS(ns, tag){ return new StubElement(tag, ns, this); },
    createTextNode(t){ return new StubText(t, this); },
    getElementById(id){
      const root = this.documentElement;
      for (const el of [root, ...descendants(root)]) if (el._id === id) return el;
      return null;
    },
    querySelector(sel){ return queryAll(this.documentElement, sel)[0] || null; },
    querySelectorAll(sel){ return queryAll(this.documentElement, sel); },
    addEventListener(type, fn){ (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    removeEventListener(type, fn){ const l = this._listeners[type]; if (l) this._listeners[type] = l.filter(x => x !== fn); },
    dispatchEvent(ev){ ev.target = ev.target || this; for (const fn of (this._listeners[ev.type] || []).slice()) { try { fn.call(this, ev); } catch (e){} } return true; },
  };
  doc.documentElement = new StubElement('html', undefined, doc);
  doc.head = new StubElement('head', undefined, doc);
  doc.body = new StubElement('body', undefined, doc);
  doc.documentElement.appendChild(doc.head);
  doc.documentElement.appendChild(doc.body);
  return doc;
}

// Build the elements app.js queries by id/class (index.html skeleton, minimal).
function buildPage(doc){
  const body = doc.body;
  const mk = (tag, opts = {}) => {
    const e = doc.createElement(tag);
    if (opts.id) e.id = opts.id;
    if (opts.cls) e.className = opts.cls;
    if (opts.ds) Object.assign(e.dataset, opts.ds);
    (opts.parent || body).appendChild(e);
    return e;
  };
  // top bar
  // NB: no 'helpBtn' — it was removed from the top bar (help lives in Settings), so the
  // harness omits it too, to catch any unguarded $('#helpBtn') wiring at boot.
  ['plateSelect', 'annotator', 'undoBtn', 'redoBtn', 'saveBtn', 'filterBtn', 'clearFilterBtn'].forEach(id => mk('button', { id }));
  mk('span', { id: 'saveStatus' });
  // left column
  ['pagePrev', 'pageLabel', 'pageNext', 'perPage', 'gridChannel', 'gridFrac', 'blockInput'].forEach(id => mk('div', { id }));
  const gridWrap = mk('div', { id: 'gridWrap' });
  mk('div', { id: 'grid', parent: gridWrap });
  mk('div', { id: 'rubber', parent: gridWrap });
  mk('div', { id: 'selInfo' });
  // detail
  const detail = mk('div', { id: 'detail' });
  mk('b', { id: 'detailWell', parent: detail });
  const chToggle = mk('span', { cls: 'chToggle', parent: detail });
  mk('button', { cls: 'btn sm chBtn', ds: { ch: 'BF' }, parent: chToggle });
  mk('button', { cls: 'btn sm chBtn', ds: { ch: 'FL' }, parent: chToggle });
  mk('span', { id: 'frameInfo', parent: detail });
  const stage = mk('div', { id: 'stage', parent: detail });
  stage._rect = { left: 0, top: 0, width: 600, height: 420, right: 600, bottom: 420 };
  const bigImg = mk('img', { id: 'bigImg', parent: stage });
  bigImg.naturalWidth = 600; bigImg.naturalHeight = 600;
  bigImg.removeAttribute = function(){ this.src = ''; };
  bigImg._rect = { left: 0, top: 0, width: 600, height: 600, right: 600, bottom: 600 };
  const scrubWrap = mk('div', { id: 'scrubWrap', parent: detail });
  scrubWrap.clientWidth = 600;
  mk('input', { id: 'scrub', parent: scrubWrap });
  mk('div', { id: 'rangeBar', parent: scrubWrap });
  mk('div', { id: 'rhStart', parent: scrubWrap });
  mk('div', { id: 'rhEnd', parent: scrubWrap });
  mk('button', { id: 'playBtn', parent: detail });
  const zrow = mk('div', { id: 'zrow', parent: detail });
  mk('input', { id: 'zslider', parent: zrow });
  mk('span', { id: 'zval', parent: zrow });
  mk('button', { id: 'zrec', parent: zrow });
  const rotrow = mk('div', { id: 'rotrow', parent: detail });
  mk('button', { id: 'rotrec', parent: rotrow });
  mk('input', { id: 'rotslider', parent: rotrow });
  mk('span', { id: 'rotval', parent: rotrow });
  // Settings is a top-bar modal now (not a panel tab)
  const setM = mk('div', { id: 'settingsModal' }); setM.hidden = true;
  mk('button', { id: 'settingsToggle' }); mk('button', { id: 'settingsClose', parent: setM });
  mk('div', { id: 'settingsBody', parent: setM });
  // panel
  const panel = mk('div', { id: 'panel' });
  const tabs = mk('div', { cls: 'tabs', parent: panel });
  mk('button', { cls: 'tab active', ds: { scope: 'well' }, parent: tabs });
  mk('button', { cls: 'tab', ds: { scope: 'plate' }, parent: tabs });
  mk('button', { cls: 'tab', ds: { scope: 'image' }, parent: tabs });
  mk('div', { id: 'panelBody', parent: panel });
  // modals
  const help = mk('div', { id: 'help' }); help.hidden = true;
  mk('button', { id: 'helpClose', parent: help });
  const fm = mk('div', { id: 'filterModal' }); fm.hidden = true;
  ['filterClose', 'filterRows', 'filterCount', 'filterAddRow', 'filterApply', 'filterExport',
   'filterPlates', 'fPlatesAll', 'fPlatesNone', 'filterName', 'filterSave', 'filterDelete',
   'filterStatus', 'filterAddMeas'].forEach(id => mk('div', { id, parent: fm }));
  mk('select', { id: 'filterSaved', parent: fm });
  // export modal — the Render block the montage/movie options live in (v1.1)
  const em = mk('div', { id: 'exportModal' }); em.hidden = true;
  ['exportTitle', 'exportWho', 'exMp4Row', 'exChannels', 'exPlanes', 'exTifZRow',
   'exLabCols', 'exportStatus', 'exOverlayLab', 'exRenderNote'].forEach(id => mk('div', { id, parent: em }));
  mk('button', { id: 'exportRun', parent: em });
  mk('button', { id: 'exportClose', parent: em });
  ['exZMode', 'exLabCorner'].forEach(id => mk('select', { id, parent: em }));
  ['exZSlice', 'exLabSize', 'exLabColour', 'exTpStart', 'exTpEnd', 'exTpStep', 'exFps',
   'exRotate', 'exOverlay', 'exLabPerTile'].forEach(id => mk('input', { id, parent: em }));
  doc.getElementById('exZMode').value = 'all';
  doc.getElementById('exLabCorner').value = 'tl';
  doc.getElementById('exLabColour').value = '#ffffff';
  const lblBlock = mk('div', { id: 'exLabelBlock', parent: em });
  for (const v of ['well', 'plate', 'stage', 'time', 'rotation', 'focus', 'scalebar']){
    const cb = mk('input', { cls: 'exLab', parent: lblBlock });
    cb.value = v; cb.checked = (v === 'well'); cb.type = 'checkbox';
  }
}

// ============================================================ synthetic server
const CONFIG = {
  data_root: '/fake',
  plates: [{ dir: 'PLATE1', annotated: false }],
  iwamatsu_stages: { stages: [{ value: 'St24', name: 'late gastrula' }, { value: 'St25', name: 'early neurula' }] },
  defaults: {
    plate: { columns: { notes: { type: 'free' } } },
    well: { columns: { viability: { type: 'binary', values: ['alive', 'dead'], default: null } } },
    image: { columns: {
      iwamatsu_stage: { type: 'categorical', values: ['St24', 'St25'], fill: 'forward' },
      slice: { type: 'categorical', values: ['1', '2', '3', '4', '5'], fill: 'forward' },
    } },
  },
  suggestions: { well: {}, plate: {}, image: {} },
  column_types: ['categorical', 'binary', 'range', 'free', 'angle', 'measurement'],
};
function freshPayload(){
  return {
    schema_version: 3, plate: 'PLATE1', annotator: '', created: '2026-07-13T00:00:00', updated: '2026-07-13T00:00:00',
    plate_columns: {}, plate_annotations: { acq_px_size_nm: 1625 },   // px size → measurement µm
    columns: {}, annotations: {}, image_columns: {}, image_annotations: {},
  };
}
const MANIFEST = {
  plate: 'PLATE1', payload: freshPayload(),
  channels: ['BF', 'FL'], detect_channel: 'BF',
  channel_z: { BF: [1, 2, 3, 4, 5], FL: [] }, z_slices: [1, 2, 3, 4, 5],
  wells: ['A01', 'A02'], layout: 'plate',
  frames: { A01: { BF: [1, 2, 3, 4, 5], FL: [1, 3, 5] }, A02: { BF: [1, 2, 3], FL: [1, 3] } },
  autofill: { date: '2026-07-13', start_time: '10:00:00', incubation_temp_c: 26, timepoint_interval_min: 10 },
};
function mkRes(data){ return { ok: true, status: 200, json: () => Promise.resolve(data) }; }
function fetchStub(url){
  const u = String(url);
  if (u.startsWith('/api/config')) return Promise.resolve(mkRes(CONFIG));
  if (u.startsWith('/api/plate')) return Promise.resolve(mkRes(JSON.parse(JSON.stringify(MANIFEST))));
  if (u.startsWith('/api/save')) return Promise.resolve(mkRes({ updated: new Date().toISOString() }));
  if (u.startsWith('/api/wells_all')) return Promise.resolve(mkRes({ columns: {}, wells: [] }));
  return Promise.resolve(mkRes({}));
}

// ============================================================ sandbox / globals
const doc = makeDocument();
buildPage(doc);

const sandbox = {
  document: doc,
  console,
  fetch: fetchStub,
  setTimeout, clearTimeout, setInterval, clearInterval,
  performance, URLSearchParams,
  Math, JSON, Date, Object, Array, Number, String, Boolean, Promise, Set, Map, RegExp, Error, isNaN, parseInt, parseFloat,
  location: { search: '?plate=PLATE1', href: 'http://localhost/?plate=PLATE1' },  // boot no longer auto-loads a default
  Image: class Image { constructor(){ this.src = ''; } },
  ResizeObserver: class ResizeObserver { constructor(cb){ this.cb = cb; } observe(){} unobserve(){} disconnect(){} },
  Event: class Event { constructor(type){ this.type = type; } },
  getComputedStyle(){ return { position: 'relative' }; },
  confirm(){ return true; },
  prompt(){ return 'x'; },
  requestAnimationFrame(fn){ return setTimeout(() => fn(Date.now()), 0); },
  // window-level event surface (plugins/app add annotator-api-ready / beforeunload / resize)
  _wl: {},
  addEventListener(type, fn){ (this._wl[type] || (this._wl[type] = [])).push(fn); },
  removeEventListener(type, fn){ const l = this._wl[type]; if (l) this._wl[type] = l.filter(x => x !== fn); },
  dispatchEvent(ev){ for (const fn of (this._wl[ev.type] || []).slice()){ try { fn.call(this, ev); } catch (e){} } return true; },
};
vm.createContext(sandbox);
sandbox.window = sandbox;          // window IS the global (as in a browser)
sandbox.globalThis = sandbox;

function run(file){ const code = fs.readFileSync(path.join(STATIC, file), 'utf8'); vm.runInContext(code, sandbox, { filename: file }); }

// ============================================================ helpers to drive it
function mouse(el, type, clientX, clientY){
  const ev = { type, clientX, clientY, button: 0, shiftKey: false,
    preventDefault(){}, stopPropagation(){} };
  el.dispatchEvent(ev);
}
const tick = () => new Promise(r => setTimeout(r, 0));

// ============================================================ MAIN
(async function main(){
  // 1) Load the app + both plugins into the shared context (order = index.html).
  run('app.js');
  run('rot_tool.js');
  run('measure_tool.js');

  const API = sandbox.AnnotatorAPI;
  const dbg = sandbox._dbg;
  ok(!!API, 'window.AnnotatorAPI is defined after app.js loads');
  ok(!!(sandbox.RotTool && sandbox.MeasureTool), 'both plugin globals are present');

  // 2) Let boot() finish (config + plate fetch + first render).
  await tick(); await tick(); await tick();

  ok(!!(API && API.state && API.state.payload), 'boot completed — payload loaded (viewer did not blank)');
  eq(API.curWell(), 'A01', 'curWell() = primary well after boot');
  eq(API.curTp(), 1, 'curTp() = first timepoint after boot');

  // plugins installed against the API
  ok(typeof dbg.toolHandlers.angle === 'object', 'rot_tool registered a tool for type "angle"');
  ok(typeof dbg.toolHandlers.measurement === 'object', 'measure_tool registered a tool for type "measurement"');
  ok(typeof dbg.toolPanels.angle === 'function', 'angle column-panel renderer registered');
  ok(typeof dbg.toolPanels.measurement === 'function', 'measurement column-panel renderer registered');

  // API surface completeness
  for (const m of ['state', 'curTp', 'curWell', 'stageEl', 'imgEl', 'mutate', 'doAssign', 'imgKeyframes',
    'setImageKeyframe', 'imgValue', 'imgInterpolate', 'pxSizeNm', 'registerTool', 'onColumnPanel']){
    ok(m in API, 'AnnotatorAPI provides `' + m + '`');
  }
  eq(API.pxSizeNm(), 3250, 'pxSizeNm() applies 2x2 binning to plate acq_px_size_nm (1625 unbinned -> 3250 real)');

  // per-channel z: BF has z-slices (z-slider), FL is flat (no z in the frame URL)
  ok(sandbox.channelHasZ('BF') === true, 'channelHasZ(BF) = true (per-z channel)');
  ok(sandbox.channelHasZ('FL') === false, 'channelHasZ(FL) = false (flat channel)');
  ok(/[?&]z=3(&|$)/.test(sandbox.frameURL('A01', 'BF', 1, 600, 3)), 'frameURL adds &z= for a per-z channel');
  ok(!/[?&]z=/.test(sandbox.frameURL('A01', 'FL', 1, 600, 3)), 'frameURL omits &z= for a flat channel');
  eq(API.state.channel, 'BF', 'default channel = manifest.detect_channel');

  // z + rotation faders are ALWAYS visible when a well is loaded (browse on any tab);
  // recording is gated by their toggle button.
  sandbox.updateBigImg();
  ok(doc.getElementById('zrow').hidden === false, 'z-slider visible on the Well tab (always draggable to look)');
  eq(Number(doc.getElementById('zslider').max), 5, 'z-slider max = last z-slice');
  ok(doc.getElementById('rotrow').hidden === false, 'rotation fader visible');
  ok(!API.state.zrec, 'z record toggle starts OFF');
  doc.getElementById('zrec').onclick(); ok(API.state.zrec === true, 'clicking the z button turns recording ON');
  doc.getElementById('zrec').onclick();  // back off
  ok(typeof sandbox.commitRotation === 'function', 'commitRotation (rotation fader commit) is defined');
  sandbox.stepZ(1);
  ok(typeof API.state.zview === 'number', 'stepZ sets a browse z (state.zview)');

  // 3) Open the Image tab (seeds rotation/slice/iwamatsu, renders tool panels).
  const state = API.state;
  state.imageMode = true;
  const imageTab = [...doc.querySelectorAll('.tab')].find(t => t.dataset.scope === 'image');
  ok(!!imageTab, 'Image tab element exists');
  imageTab.onclick();                                  // → scope=image, renderPanel()

  const rotCol = state.payload.image_columns.rotation;
  ok(!!rotCol, 'rotation image column seeded');
  eq(rotCol && rotCol.type, 'angle', 'rotation column type = angle');
  eq(rotCol && rotCol.fill, 'interpolate', 'rotation column fill = interpolate');
  eq(state.payload.image_columns.slice && state.payload.image_columns.slice.fill, 'forward', 'slice column still forward-fill');

  // ---- ROTATION: activate + drag on #stage ----------------------------------
  API.activateImageTool('rotation');
  eq(API.activeToolCol, 'rotation', 'rotation column is the active tool column');
  ok(API.stageEl._classes.has('tool-active'), '#stage got the tool-active class');

  const stage = API.stageEl, imgEl = API.imgEl;
  // centre of #bigImg is (300,300); start due-east, sweep clockwise to due-south = +90°
  mouse(stage, 'mousedown', 400, 300);
  mouse(stage, 'mousemove', 300, 400);
  mouse(stage, 'mouseup', 300, 400);

  const rotVal = ((state.payload.image_annotations.A01 || {})['1'] || {}).rotation;
  ok(typeof rotVal === 'number', 'a numeric rotation keyframe was written into image_annotations');
  ok(Math.abs(rotVal - 90) < 1e-6, 'rotation keyframe ≈ +90° (clockwise)  (got ' + rotVal + ')');
  ok(typeof imgEl.style.transform === 'string' && /rotate\(/.test(imgEl.style.transform),
    '#bigImg.style.transform was set to a rotate(...)  (got "' + imgEl.style.transform + '")');
  // interpolation API agrees with the stored keyframe
  ok(Math.abs(API.imgInterpolate('A01', 'rotation', 1) - 90) < 1e-6, 'imgInterpolate holds the single keyframe value');

  // ---- SLICE forward-fill still works (regression guard) --------------------
  dbg.setFrame(2);                                     // tp 3
  dbg.setImageKeyframe('slice', '3');
  eq(dbg.imgEffective('A01', 'slice', 3).value, '3', 'slice keyframe effective at its own frame');
  eq(dbg.imgEffective('A01', 'slice', 4).value, '3', 'slice keyframe forward-fills to a later frame');
  eq(dbg.imgEffective('A01', 'slice', 1).value, null, 'slice keyframe does NOT back-fill before it');
  ok(!('rotation' in (dbg.imgKeyframes('A01', 'slice'))), 'slice keyframes are independent of rotation');

  // z-slider commit: annotates the 'slice' focus keyframe at the current frame (tp 3)
  sandbox.commitSlice(4);
  eq(dbg.imgEffective('A01', 'slice', 3).value, '4', 'z-slider commitSlice sets the slice keyframe (always-set)');

  // rotation interpolates the SHORTEST way around the circle (10↔350 crosses 0, not 180)
  sandbox.writeImgKeyframes('A02', 'rotation', { 1: '10', 3: '350' });
  const midA = API.imgInterpolate('A02', 'rotation', 2);
  ok(midA < 5 || midA > 355, 'rotation takes the short route (≈0/360, not 180)  (got ' + midA.toFixed(1) + ')');

  // ---- MEASUREMENT: add a column, activate, two clicks ----------------------
  API.mutate(() => { state.payload.image_columns.egg_diameter = { type: 'measurement', values: [] }; });
  ok(!!state.payload.image_columns.egg_diameter, 'measurement column added');

  dbg.setFrame(0);                                     // back to tp 1
  API.activateImageTool('egg_diameter');               // deactivates rotation
  eq(API.activeToolCol, 'egg_diameter', 'measurement column is now the active tool column');
  ok(API.stageEl._classes.has('tool-measurement'), '#stage got the tool-measurement class');

  // click 1 at (100,100), release (no drag), click 2 at (300,300) → commit
  mouse(stage, 'mousedown', 100, 100);
  mouse(stage, 'mouseup', 100, 100);
  mouse(stage, 'mousedown', 300, 300);
  mouse(stage, 'mouseup', 300, 300);

  const mv = ((state.payload.image_annotations.A01 || {})['1'] || {}).egg_diameter;
  ok(mv && typeof mv === 'object', 'a measurement object was written via doAssign');
  ok(mv && Array.isArray(mv.line) && mv.line.length === 4, 'measurement.line = [x0,y0,x1,y1]  (got ' + JSON.stringify(mv && mv.line) + ')');
  ok(mv && typeof mv.length_px === 'number' && mv.length_px > 1, 'measurement.length_px is a positive number  (got ' + (mv && mv.length_px) + ')');
  ok(mv && Math.abs(mv.length_px - Math.hypot(200, 200)) < 0.5, 'length_px ≈ hypot(200,200)  (got ' + (mv && mv.length_px) + ')');
  ok(mv && typeof mv.length_um === 'number', 'measurement.length_um computed (px size known)  (got ' + (mv && mv.length_um) + ')');

  // rotation keyframe survived the measurement write (independent columns/frame)
  ok(typeof ((state.payload.image_annotations.A01 || {})['1'] || {}).rotation === 'number',
    'rotation keyframe still present alongside the measurement');

  // ---- tabs render without throwing (well / plate / image / settings) -------
  let tabErr = null;
  try {
    for (const scope of ['well', 'plate', 'image']){
      const t = [...doc.querySelectorAll('.tab')].find(x => x.dataset.scope === scope);
      t.onclick();
    }
  } catch (e){ tabErr = e; }
  ok(!tabErr, 'Well / Plate / Image tabs all render without throwing' + (tabErr ? '  → ' + tabErr.message : ''));
  ok(doc.getElementById('panelBody').children.length > 0, 'panel body has rendered content');
  // Settings opens as a top-bar modal and builds its sections (paths, formats, help)
  sandbox.openSettings();
  ok(doc.getElementById('settingsModal').hidden === false, 'Settings modal opens from the top bar');
  ok(doc.getElementById('settingsBody').querySelectorAll('.section').length >= 3, 'Settings modal renders its sections');

  // ---- arrow-key context: ← → act on whatever you last clicked --------------
  {
    const st = API.state;
    st.filter.active = false;
    st.arrowMode = 'wells';                              // clicked a well → arrows move wells
    const w0 = st.primary;
    API.arrowNav(1, false);
    ok(st.manifest.wells.length <= 1 || st.primary !== w0, 'arrowNav(wells) moves the primary well');
    st.arrowMode = 'frame';                              // clicked the scrubber → arrows step frames
    const pw = st.primary; API.arrowNav(1, false);
    eq(st.primary, pw, 'arrowNav(frame) leaves the well unchanged');
    st.arrowMode = 'z';                                  // clicked the z fader → arrows nudge focus
    const pw2 = st.primary; API.arrowNav(1, false);
    eq(st.primary, pw2, 'arrowNav(z) leaves the well unchanged');
    st.arrowMode = 'rotation';                           // clicked the rotation fader → arrows nudge rotation
    st.rotview = 0; API.arrowNav(1, false);
    ok(typeof st.rotview === 'number' && st.rotview !== 0, 'arrowNav(rotation) changes the rotation view');
    st.arrowMode = 'wells'; st.rotview = null;
  }
  // ---- Image tab displays the recorded fader keyframes ----------------------
  {
    const imgTab = [...doc.querySelectorAll('.tab')].find(t => t.dataset.scope === 'image');
    imgTab.onclick();
    const h4 = [...doc.getElementById('panelBody').querySelectorAll('h4')].map(h => h.textContent || '');
    ok(h4.some(t => /Focus keyframes/.test(t)), 'Image tab shows a "Focus keyframes" section (z-slider)');
    ok(h4.some(t => /Rotation keyframes/.test(t)), 'Image tab shows a "Rotation keyframes" section');
  }

  // ---- export binds to the SELECTION, cross-plate in filter mode ------------
  {
    const st = API.state;
    // normal mode: export = the selected wells on the loaded plate
    st.filter.active = false;
    st.sel = new Set(['A01', 'A02']);
    let ws = API.exportWells();
    eq(ws.length, 2, 'normal export = the selected wells');
    ok(ws.every(w => w.plate === st.plateDir), 'normal export wells all carry the loaded plate');
    // filter mode: export = exactly the cross-plate filterSel, NOT all results
    st.filter.active = true;
    st.filter.results = [{ plate: 'P1', well: 'A01' }, { plate: 'P1', well: 'A02' }, { plate: 'P2', well: 'B03' }];
    st.filterSel = new Set(['P1|A01', 'P2|B03']);
    ws = API.exportWells();
    eq(ws.length, 2, 'filter export = only the selected wells (not all 3 results)');
    ok(ws.some(w => w.plate === 'P1' && w.well === 'A01') && ws.some(w => w.plate === 'P2' && w.well === 'B03'),
       'filter export spans plates (P1/A01 + P2/B03)');
    st.filter.active = false; st.filterSel = new Set();
  }

  // ---- FILTER v1.2: plate subsets + measurement constraints ------------------
  {
    const D = sandbox.window._dbg, st = API.state;
    const W = (short, well, ann, meas) => ({ plate: short + '_x', short, well, ann: ann || {}, nann: Object.keys(ann || {}).length, meas });
    st.filter.data = {
      columns: { line: { type: 'categorical', values: ['cab', 'pfkfb3'] },
                 clutch_n: { type: 'free', values: [] } },
      measurements: ['egg_diameter'],
      wells: [
        // measured ONCE (the common case — a size annotated at a single timepoint)
        W('AQV02', 'A01', { line: 'cab' }, { egg_diameter: { n: 1, min: 1500, max: 1500, mean: 1500, first: 1500, last: 1500, vals: [1500] } }),
        // measured twice, one of them BELOW the threshold
        W('AQV04', 'B02', { line: 'cab' }, { egg_diameter: { n: 2, min: 1300, max: 1500, mean: 1400, first: 1500, last: 1300, vals: [1500, 1300] } }),
        // never measured
        W('AQV05', 'C03', { line: 'pfkfb3', clutch_n: '7' }, undefined),
      ],
      filters: {},
    };
    st.filter.plates = new Set();
    const names = () => D.computeFilter().map(w => w.short + '/' + w.well).sort();

    // plate SUBSET (the "only three of the plates" ask) — empty set = all plates
    st.filter.constraints = [];
    eq(names().length, 3, 'filter: no plate chips selected = every plate');
    st.filter.plates = new Set(['AQV02', 'AQV04']);
    eq(names().join(), 'AQV02/A01,AQV04/B02', 'filter: plate chips restrict to that subset');
    st.filter.plates = new Set();

    // measurement, "…beyond X ALWAYS" → every measured timepoint must pass
    st.filter.constraints = [{ kind: 'meas', name: 'egg_diameter', agg: 'all', op: '>', val: 1400 }];
    eq(names().join(), 'AQV02/A01', 'filter: >1400 at EVERY timepoint keeps the single-tp well, drops the one that dips');
    st.filter.constraints[0].agg = 'any';
    eq(names().join(), 'AQV02/A01,AQV04/B02', 'filter: "at any timepoint" keeps the well that passes once');

    // a well measured ONCE still satisfies "always" — the correction the sparse data needs
    st.filter.constraints = [{ kind: 'meas', name: 'egg_diameter', agg: 'all', op: '>', val: 1400 }];
    ok(D.measPass(st.filter.data.wells[0], st.filter.constraints[0]),
       'filter: one annotated timepoint counts as "every timepoint"');
    // …unless you explicitly demand more of them
    st.filter.constraints[0].minN = 2;
    ok(!D.measPass(st.filter.data.wells[0], st.filter.constraints[0]),
       'filter: min n = 2 excludes the well measured only once');
    delete st.filter.constraints[0].minN;

    // between + never-measured wells are excluded, not silently kept
    st.filter.constraints = [{ kind: 'meas', name: 'egg_diameter', agg: 'any', op: 'between', val: 1250, val2: 1350 }];
    eq(names().join(), 'AQV04/B02', 'filter: between keeps only the well with a value inside the band');
    ok(!D.measPass(st.filter.data.wells[2], st.filter.constraints[0]),
       'filter: a well that was never measured cannot match a measurement constraint');

    // annotation ops: equality, negation, set/unset, and numeric on a free column
    st.filter.constraints = [{ kind: 'ann', col: 'line', op: 'is', val: 'cab' }];
    eq(names().join(), 'AQV02/A01,AQV04/B02', 'filter: annotation = value');
    st.filter.constraints = [{ kind: 'ann', col: 'line', op: 'not', val: 'cab' }];
    eq(names().join(), 'AQV05/C03', 'filter: annotation ≠ value');
    st.filter.constraints = [{ kind: 'ann', col: 'clutch_n', op: 'set' }];
    eq(names().join(), 'AQV05/C03', 'filter: "is set" finds the annotated well');
    st.filter.constraints = [{ kind: 'ann', col: 'clutch_n', op: '>', val: 5 }];
    eq(names().join(), 'AQV05/C03', 'filter: numeric > on a free-text column');
    st.filter.constraints = [{ kind: 'ann', col: 'clutch_n', op: '>', val: 9 }];
    eq(names().length, 0, 'filter: numeric > that nothing satisfies returns nothing');

    // several constraints AND together, plate subset included
    st.filter.plates = new Set(['AQV04']);
    st.filter.constraints = [{ kind: 'ann', col: 'line', op: 'is', val: 'cab' },
                             { kind: 'meas', name: 'egg_diameter', agg: 'any', op: '>', val: 1400 }];
    eq(names().join(), 'AQV04/B02', 'filter: plate subset AND annotation AND measurement');
    st.filter.plates = new Set(); st.filter.constraints = []; st.filter.data = null;
  }

  // ---- RENDER v1.1: the export spec the montage engine receives ---------------
  {
    const D = sandbox.window._dbg;
    D.openExport('mp4');
    ok(!doc.getElementById('exPlanes').hidden, 'render: MP4 shows the per-channel plane rows');
    ok(doc.getElementById('exTifZRow').hidden, 'render: MP4 hides the TIF z-mode row');
    const rows = doc.getElementById('exPlanes').querySelectorAll('.prow');
    eq(rows.length, MANIFEST.channels.length, 'render: one plane row per discovered channel');
    doc.getElementById('exRotate').checked = true;
    let spec = D.collectRender();
    ok(spec.rotate === true, 'render: rotation flag reaches the spec');
    ok(Object.keys(spec.channels).length === MANIFEST.channels.length, 'render: every channel carries a plane mode');
    ok(Object.values(spec.channels).every(c => c.mode && c.cmap), 'render: each channel has both a mode and a colour');
    ok(spec.labels.well === true, 'render: the well label is on by default');

    D.openExport('tif');
    ok(!doc.getElementById('exTifZRow').hidden, 'render: TIF shows the shared z-mode row');
    ok(doc.getElementById('exLabelBlock').hidden, 'render: TIF hides the label block');
    doc.getElementById('exZMode').value = 'maxproj';
    spec = D.collectRender();
    eq(spec.z_mode, 'maxproj', 'render: TIF z-mode reaches the spec');
    eq(Object.keys(spec.labels).length, 0, 'render: a TIF hyperstack never carries burned-in labels');
    ok(spec.overlay === false, 'render: TIF never overlays channels');
    doc.getElementById('exportModal').hidden = true;
  }

  // ---- cleanup timers so Node exits promptly --------------------------------
  try { clearTimeout(state.saveTimer); } catch (e){}
  try { clearInterval(state.playTimer); } catch (e){}

  console.log('\njs_harness: ' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('\nHARNESS CRASH:', e && e.stack || e); process.exit(2); });
