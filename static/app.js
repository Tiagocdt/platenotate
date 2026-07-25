/* Medaka annotator — vanilla-JS frontend (no framework, no build step).
 *
 * The client owns the live annotation state (state.payload, a v3 dict) plus
 * undo/redo; every change funnels through mutate(), which snapshots for undo,
 * re-renders, and schedules a debounced autosave POST. The server only
 * discovers layout, serves crop PNGs, and validates+writes the JSON.
 */
'use strict';

// ------------------------------------------------------------------ tiny utils
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const elt = (tag, cls, txt) => { const e = document.createElement(tag);
  if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const clone = o => JSON.parse(JSON.stringify(o));
function hashStr(s){ let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h); }
async function jget(u){ const r = await fetch(u); if (!r.ok) throw new Error((await r.json().catch(()=>({}))).error || r.status); return r.json(); }
async function jpost(u, b){ const r = await fetch(u, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b) });
  if (!r.ok) throw new Error((await r.json().catch(()=>({}))).error || r.status); return r.json(); }

// value colouring — mirrors the legacy screen.py so tags read consistently.
const SEMANTIC = { dead:'#d62728', injected:'#2ca02c', mgold_pos:'#2ca02c', alive:'#2ca02c',
  wt:'#7f7f7f', healthy:'#7f7f7f', unsure:'#ff7f0e', good:'#81c995', bad:'#f28b82',
  'true':'#f28b82', 'false':'#48576b', yes:'#2ca02c', no:'#7f7f7f' };
const PALETTE = ['#8ab4f8','#f28b82','#fdd663','#81c995','#c58af9','#78d9ec','#ff8bcb','#c9a26b','#a5b1c2','#e39ff6'];
function valueColor(col, value){
  if (value == null) return '#556270';
  if (col.type === 'range') return '#e8a33d';   // amber, matching the theme accent
  const k = String(value).toLowerCase();
  if (SEMANTIC[k]) return SEMANTIC[k];
  const base = k.split('/')[0]; if (SEMANTIC[base]) return SEMANTIC[base];
  const i = (col.values || []).indexOf(value);
  return PALETTE[(i >= 0 ? i : hashStr(k)) % PALETTE.length];
}
const fmtVal = v => Array.isArray(v) ? `[${v[0]}–${v[1]}]` : String(v);

// ------------------------------------------------------------------ state
const state = {
  cfg: null, plateDir: '', manifest: null, payload: null,
  me: '',                          // the current annotator (session identity; see only your own)
  arrowMode: 'wells',              // what ← → control: 'wells' | 'z' | 'rotation' | 'frame'
  sel: new Set(), primary: null,
  channel: 'BF', frameIdx: 0, playing: false, playTimer: null, zview: null, rotview: null, zrec: false, rotrec: false,
  scope: 'well', activeCol: null, imageMode: true,
  gridChannel: 'BF', gridFrac: 0.5, page: 0, perPage: 96,
  undo: [], redo: [], dirty: false, saveTimer: null,
  cells: new Map(), rangeDrag: null,
  filter: { active: false, constraints: [], data: null, results: [] },
  filterSel: new Set(),            // cross-plate selection in filter mode ("plate|well" keys)
  _filterSig: '',                  // guard: only rebuild the filter grid when results change
};

// column helpers per scope
const colsKey = { plate:'plate_columns', well:'columns', image:'image_columns' };
const annKey  = { plate:'plate_annotations', well:'annotations', image:'image_annotations' };

// ---- plugin tool registry (rotation / measurement image tools) ----
// Plugins register against window.AnnotatorAPI (set up at the bottom of this
// file). `toolHandlers[type]` = the image-interaction handler set; `toolPanels`
// [type] = the per-column panel renderer; `activeToolCol` = the ONE image column
// whose tool currently owns pointer input on #stage (or null).
const toolHandlers = {};   // columnType -> { onActivate,onDeactivate,onImageMouseDown/Move/Up,onRender }
const toolPanels = {};     // columnType -> fn(container, colName)
let activeToolCol = null;  // active image-column NAME, or null

// ------------------------------------------------------------------ boot
async function boot(){
  wireStatic();
  try { document.body.tabIndex = -1; window.focus(); document.body.focus(); } catch (_){}  // keys work sans first-click
  try { state.me = localStorage.getItem('annotator') || ''; } catch (_){}  // remembered identity
  state.cfg = await jget('/api/config');
  if (state.me){ const a = $('#annotator'); if (a) a.value = state.me; }
  initVersionChip();
  // DB is auto-created; if the data folder is a network share the DB is kept local,
  // so surface where annotations are actually being saved.
  if (state.cfg.db){
    const d = state.cfg.db;
    if (d.local_fallback) setStatus('annotations saved to a LOCAL database (this share isn\'t written to)', 'saved');
    if (d.db_path) { const sb = $('#saveBtn'); if (sb) sb.title = 'Save now (s) — DB: ' + d.db_path; }
  }
  // annotator name suggestions (known annotators from the DB)
  const dl = $('#annotatorList');
  if (dl){ dl.innerHTML = ''; (state.cfg.annotators || []).forEach(n => {
    const o = elt('option'); o.value = n; dl.appendChild(o); }); }
  state.settings = state.cfg.settings || { annotations_dir:'', export_dir:'', formats:{db:true, csv:true, json:true} };
  const sel = $('#plateSelect');
  sel.innerHTML = '';
  // placeholder first, so NOTHING auto-loads — you pick the plate you actually want
  const ph = elt('option'); ph.value = ''; ph.textContent = '— choose a plate —'; sel.appendChild(ph);
  state.cfg.plates.forEach(p => {
    const o = elt('option'); o.value = p.dir;
    o.textContent = p.dir + (p.annotated ? '  ✓' : '');
    sel.appendChild(o);
  });
  // only auto-load when a plate is named explicitly in the URL (?plate=…)
  const qp = new URLSearchParams(location.search).get('plate');
  let target = null;
  if (qp){
    const hit = state.cfg.plates.find(p => p.dir === qp) || state.cfg.plates.find(p => p.dir.startsWith(qp));
    if (hit) target = hit.dir;
  }
  if (target){ sel.value = target; await loadPlate(target); }
  else if (!state.cfg.plates.length){ sel.value = '';
    $('#panelBody').innerHTML = '<p class="muted">No plate folders found under the data root.</p>'; }
  else { sel.value = ''; showEmptyState(); }
}

function showLoading(dir){ const o = $('#loadOverlay'); if (!o) return;
  const m = $('#loadMsg'); if (m) m.textContent = 'loading ' + (dir || 'plate') + '…'; o.hidden = false; }
function hideLoading(){ const o = $('#loadOverlay'); if (o) o.hidden = true; }
function showEmptyState(){                              // no plate chosen yet
  const g = $('#grid'); if (g) g.innerHTML =
    '<div class="muted" style="grid-column:1/-1;padding:26px;text-align:center">Choose a plate above to start annotating.</div>';
  const dw = $('#detailWell'); if (dw) dw.textContent = '—';
  const bi = $('#bigImg'); if (bi) bi.removeAttribute('src');
  const pb = $('#panelBody'); if (pb) pb.innerHTML = '<p class="muted">Choose a plate above to start annotating.</p>';
  const pl = $('#pageLabel'); if (pl) pl.textContent = '—';
}

async function loadPlate(dir){
  if (!dir) return;                                         // no plate chosen → nothing to load
  const seq = (state.loadSeq = (state.loadSeq || 0) + 1);   // guard against a stale (slow SMB) response
  stopPlay();
  showLoading(dir);
  try {
    const man = await jget('/api/plate?dir=' + encodeURIComponent(dir)
      + (state.me ? '&annotator=' + encodeURIComponent(state.me) : ''));   // only my annotations
    if (seq !== state.loadSeq) return;                      // a newer selection superseded this load
    state.manifest = man;
    state.plateDir = man.plate;
    state.payload = man.payload;
    if (state.me) state.payload.annotator = state.me;       // edits save under ME, not last-toucher
    seedScopes();
    // pick channel + primary — the detection channel (bf_channel) is the default view
    state.channel = man.detect_channel || (man.channels.includes('BF') ? 'BF' : man.channels[0]);
    state.gridChannel = state.channel;
    state.zview = null; state.rotview = null;   // fresh plate: faders follow the annotated keyframes
    state.page = 0;
    state.primary = man.wells[0] || null;
    state.sel = new Set(state.primary ? [state.primary] : []);
    state.activeCol = Object.keys(state.payload.columns)[0] || null;
    state.imageMode = true;
    state.undo = []; state.redo = []; state.dirty = false;
    $('#annotator').value = state.me || state.payload.annotator || '';
    buildChannelButtons();
    buildGridChannelOptions();
    renderAll();
    setStatus('loaded ' + man.plate, '');
  } catch (e){
    if (seq === state.loadSeq) setStatus('failed to load ' + dir + ': ' + (e.message || e), 'err');
  } finally {
    if (seq === state.loadSeq) hideLoading();               // ALWAYS hide (unless a newer load owns it)
  }
}

// Seed convenience columns (never marks dirty; saved only on a real edit).
function seedScopes(){
  const d = state.cfg.defaults || {};
  // plate: the temp/date/start/notes fields live here — always offer them.
  if (Object.keys(state.payload.plate_columns).length === 0 && d.plate && d.plate.columns)
    for (const [n, spec] of Object.entries(d.plate.columns))
      state.payload.plate_columns[n] = { type: spec.type || 'free', values: spec.values ? [...spec.values] : [] };
  // well: only seed if there is truly nothing anywhere (no file, no registry).
  const regWell = state.cfg.suggestions.well || {};
  if (Object.keys(state.payload.columns).length === 0 && Object.keys(regWell).length === 0 && d.well)
    for (const [n, spec] of Object.entries(d.well.columns))
      state.payload.columns[n] = { type: spec.type || 'categorical', values: spec.values ? [...spec.values] : [],
        ...(spec.default != null ? { default: spec.default } : {}) };
}

// ------------------------------------------------------------------ mutate / undo / save
function mutate(fn, opts = {}){
  state.undo.push(clone(state.payload));
  if (state.undo.length > 120) state.undo.shift();
  state.redo = [];
  fn();
  state.dirty = true;
  updateUndoButtons();
  if (opts.render !== false) renderAfterMutate(opts.only);
  scheduleSave();
}
function renderAfterMutate(only){
  if (!only || only.includes('panel')) renderPanel();
  if (!only || only.includes('badges')) renderGridBadges();
  if (!only || only.includes('detail')) updateRangeBar();
}
function undo(){ if (!state.undo.length) return; state.redo.push(clone(state.payload));
  state.payload = state.undo.pop(); afterHistory(); }
function redo(){ if (!state.redo.length) return; state.undo.push(clone(state.payload));
  state.payload = state.redo.pop(); afterHistory(); }
function afterHistory(){ state.dirty = true; updateUndoButtons();
  if (state.activeCol && !state.payload.columns[state.activeCol])
    state.activeCol = Object.keys(state.payload.columns)[0] || null;
  $('#annotator').value = state.payload.annotator || '';
  renderPanel(); renderGridBadges(); updateRangeBar(); scheduleSave(); }
function updateUndoButtons(){ $('#undoBtn').disabled = !state.undo.length; $('#redoBtn').disabled = !state.redo.length; }

function scheduleSave(){ clearTimeout(state.saveTimer); setStatus('editing…', 'saving');
  state.saveTimer = setTimeout(saveNow, 800); }
async function saveNow(){
  clearTimeout(state.saveTimer);
  if (!state.payload) return;
  setStatus('saving…', 'saving');
  try {
    const r = await jpost('/api/save', { dir: state.plateDir, payload: state.payload });
    state.dirty = false;
    const t = (r.updated || '').replace('T', ' ').slice(0, 19);
    setStatus('saved ' + t, 'saved');
  } catch (e){ setStatus('save failed: ' + e.message, 'err'); }
}
function setStatus(txt, cls){ const s = $('#saveStatus'); s.textContent = txt; s.className = 'status ' + (cls || ''); }

// ------------------------------------------------------------------ render orchestration
function renderAll(){ renderGrid(); renderDetail(); renderPanel(); updateUndoButtons(); }

// ---- grid ----------------------------------------------------------------
function pageWells(){ const w = state.manifest.wells;
  const pp = state.perPage; const start = state.page * pp; return w.slice(start, start + pp); }
function repFrame(well, ch){
  const f = state.manifest.frames[well] || {};
  let tps = f[ch] || [];
  if (!tps.length){ const other = state.manifest.channels.find(c => c !== ch); tps = (other && f[other]) || []; ch = other; }
  if (!tps.length) return null;
  const tp = tps[Math.min(tps.length - 1, Math.round(state.gridFrac * (tps.length - 1)))];
  return { ch, tp };
}
// does the given channel have real z-slices? (old FL is flat → no z-slider)
function channelHasZ(ch){
  const cz = state.manifest && state.manifest.channel_z;
  return !!(cz && cz[ch] && cz[ch].length);
}
function frameURL(well, ch, tp, size, z){
  let u = `/api/frame?dir=${encodeURIComponent(state.plateDir)}&well=${encodeURIComponent(well)}&ch=${ch}&tp=${tp}&size=${size}`;
  if (channelHasZ(ch) && z != null) u += `&z=${z}`;   // z applies within the selected channel
  return u;
}
// the z-slice that the 'slice' keyframes forward-fill to at a given timepoint (or null)
function sliceAt(tp){
  if (tp == null || !state.primary) return null;
  if (!(state.payload.image_columns && state.payload.image_columns['slice'])) return null;
  const e = imgEffective(state.primary, 'slice', tp);
  const z = parseInt(e.value);
  return Number.isFinite(z) ? z : null;
}
function frameURLd(dir, well, ch, tp, size){
  return `/api/frame?dir=${encodeURIComponent(dir)}&well=${encodeURIComponent(well)}&ch=${ch}&tp=${tp}&size=${size}`;
}
// z + rotation faders. Both are IMAGE-scope controls, so they appear only on the
// Image tab. The z-slider browses/sets focus; the rotation slider sets rotation.
// Both faders are ALWAYS draggable to look; each records keyframes only while its
// button (#zrec / #rotrec) is switched on. Visible whenever a well is loaded — not
// gated to the Image tab — so you can browse z / rotation from any tab.
function updateFaders(){ updateZslider(); updateRotslider(); }
function updateZslider(){
  const row = $('#zrow'); if (!row) return;
  const zs = (state.manifest && state.manifest.channel_z && state.manifest.channel_z[state.channel]) || [];
  if (zs.length < 2 || state.primary == null){ row.hidden = true; return; }
  row.hidden = false;
  const rec = $('#zrec'); if (rec) rec.classList.toggle('on', !!state.zrec);
  const lo = zs[0], hi = zs[zs.length - 1];
  if (state.zview != null) state.zview = Math.min(hi, Math.max(lo, state.zview));  // keep in range across channels
  let z = state.zview != null ? state.zview : sliceAt(curTp());
  if (z == null) z = zs[Math.floor(zs.length / 2)];
  z = Math.min(hi, Math.max(lo, z));
  const sl = $('#zslider'); sl.min = lo; sl.max = hi; sl.value = z;
  $('#zval').textContent = `${z} / ${hi}` + (state.zrec ? ' rec' : '');
}
function updateRotslider(){
  const row = $('#rotrow'); if (!row) return;
  if (state.primary == null){ row.hidden = true; return; }
  row.hidden = false;
  const rec = $('#rotrec'); if (rec) rec.classList.toggle('on', !!state.rotrec);
  const deg = state.rotview != null ? state.rotview
            : Math.round(imgInterpolate(state.primary, 'rotation', curTp()) || 0);
  $('#rotslider').value = ((deg % 360) + 360) % 360;
  $('#rotval').textContent = `${Math.round(deg)}°` + (state.rotrec ? ' rec' : '');
}
function stepZ(d){
  const zs = (state.manifest.channel_z && state.manifest.channel_z[state.channel]) || [];
  if (!zs.length) return;
  const cur = state.zview != null ? state.zview : sliceAt(curTp());
  let idx = cur != null ? zs.indexOf(cur) : -1;
  if (idx < 0) idx = Math.floor(zs.length / 2);
  idx = Math.min(zs.length - 1, Math.max(0, idx + d));
  state.zview = zs[idx]; updateBigImg(); updateFrameInfo();
}
function renderGrid(){
  if (state.filter.active) return renderFilterGrid();
  const g = $('#grid'); g.innerHTML = ''; state.cells.clear();
  const wells = pageWells();
  const cols = state.manifest.layout === 'flat' ? Math.min(12, Math.ceil(Math.sqrt(wells.length))) : 12;
  g.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  for (const w of wells){
    const cell = elt('div', 'cell'); cell.dataset.well = w;
    const rep = repFrame(w, state.gridChannel);
    const img = elt('img'); img.loading = 'lazy'; img.draggable = false;
    if (rep) img.src = frameURL(w, rep.ch, rep.tp, 130);
    cell.appendChild(img);
    cell.appendChild(elt('span', 'wlab', w));
    cell.appendChild(elt('span', 'badge'));
    const dot = elt('span', 'imgdot'); cell.appendChild(dot);
    // rubber-band may start on a cell too; a plain (no-drag) mousedown falls
    // through to the click handler, which selects just this well.
    cell.addEventListener('click', e => onCellClick(w, e));
    g.appendChild(cell);
    state.cells.set(w, cell);
  }
  updatePageLabel();
  renderGridBadges();
}
function updatePageLabel(){
  const total = state.manifest.wells.length, pp = state.perPage;
  const pages = Math.max(1, Math.ceil(total / pp));
  $('#pageLabel').textContent = `${state.page + 1}/${pages} · ${total} wells`;
  $('#pagePrev').disabled = state.page <= 0;
  $('#pageNext').disabled = state.page >= pages - 1;
}
// ---- cross-plate filter: keep wells matching constraints, from every plate ----
async function openFilter(){
  if (!state.filter.data){ setStatus('loading wells…', 'saving');
    state.filter.data = await jget('/api/wells_all'); setStatus('', ''); }
  state.filter.plates = state.filter.plates || new Set();
  renderFilterPlates();
  renderSavedFilters();
  renderFilterRows();
  $('#filterModal').hidden = false;
}
function addAnnRow(){
  const cols = Object.keys(state.filter.data.columns);
  state.filter.constraints.push({ kind: 'ann', col: cols[0] || '', op: 'is', val: '' });
  renderFilterRows();
}
function addMeasRow(){
  const m = (state.filter.data.measurements || [])[0] || '';
  state.filter.constraints.push({ kind: 'meas', name: m, agg: 'all', op: '>', val: null });
  renderFilterRows();
}

// ---- saved filters: name a plate set + constraints and get it back later ----
function renderSavedFilters(){
  const sel = $('#filterSaved'); if (!sel) return;
  const saved = (state.filter.data && state.filter.data.filters) || {};
  sel.innerHTML = '';
  const none = elt('option'); none.value = ''; none.textContent = '—'; sel.appendChild(none);
  for (const name of Object.keys(saved).sort()){
    const o = elt('option'); o.value = name; o.textContent = name; sel.appendChild(o);
  }
  sel.value = state.filter.savedName || '';
}
function loadSavedFilter(name){
  const f = ((state.filter.data || {}).filters || {})[name];
  if (!f) return;
  state.filter.plates = new Set(f.plates || []);
  state.filter.constraints = JSON.parse(JSON.stringify(f.constraints || []));
  state.filter.savedName = name;
  $('#filterName').value = name;
  renderFilterPlates(); renderFilterRows();
  $('#filterStatus').textContent = 'loaded "' + name + '"';
}
async function saveFilter(){
  const name = ($('#filterName').value || '').trim();
  if (!name){ $('#filterStatus').textContent = 'give the filter a name first'; return; }
  const body = { plates: [...(state.filter.plates || [])], constraints: state.filter.constraints };
  try {
    await jpost('/api/settings', { filters: { [name]: body } });
    state.filter.data.filters = state.filter.data.filters || {};
    state.filter.data.filters[name] = body;
    state.filter.savedName = name;
    renderSavedFilters();
    $('#filterStatus').textContent = 'saved "' + name + '"';
  } catch (e){ $('#filterStatus').textContent = 'save failed: ' + e; }
}
async function deleteFilter(){
  const name = $('#filterSaved').value || state.filter.savedName;
  if (!name) return;
  try {
    await jpost('/api/settings', { filters: { [name]: null } });
    delete state.filter.data.filters[name];
    state.filter.savedName = '';
    renderSavedFilters();
    $('#filterStatus').textContent = 'deleted "' + name + '"';
  } catch (e){ $('#filterStatus').textContent = 'delete failed: ' + e; }
}
// ---- plate picker: any SUBSET of plates (empty selection = search them all) ----
function renderFilterPlates(){
  const box = $('#filterPlates'); if (!box) return;
  box.innerHTML = '';
  const seen = new Map();
  for (const w of (state.filter.data.wells || [])){
    const k = w.short || w.plate;
    if (k) seen.set(k, (seen.get(k) || 0) + 1);
  }
  state.filter.plates = state.filter.plates || new Set();
  for (const k of [...seen.keys()].sort()){
    const on = state.filter.plates.has(k);
    const b = elt('button', 'pchip' + (on ? ' on' : ''), k);
    b.appendChild(elt('i', null, String(seen.get(k))));
    b.onclick = () => {
      if (state.filter.plates.has(k)) state.filter.plates.delete(k);
      else state.filter.plates.add(k);
      renderFilterPlates(); updateFilterCount();
    };
    box.appendChild(b);
  }
  const n = state.filter.plates.size;
  box.appendChild(elt('span', 'muted', n ? `  ${n} of ${seen.size} plates` : '  all plates'));
}

// Numeric comparators shared by annotation and measurement constraints.
const F_OPS = [['>', '>'], ['>=', '≥'], ['<', '<'], ['<=', '≤'],
               ['between', 'between'], ['==', '=']];
// HOW a well's several measured timepoints are reduced to one yes/no. 'all' is the
// "…always" case; it is TRUE for a well measured once — one timepoint is still every
// timepoint that was measured. `min n` is there when you want to demand more.
const F_AGGS = [['all', 'at every measured timepoint'], ['any', 'at any timepoint'],
                ['mean', 'mean of'], ['min', 'smallest'], ['max', 'largest'],
                ['first', 'first'], ['last', 'last']];

function fnum(x){ const v = parseFloat(x); return Number.isFinite(v) ? v : null; }
function fcmp(v, op, a, b){
  if (v == null || a == null) return false;
  switch (op){
    case '>': return v > a;
    case '>=': return v >= a;
    case '<': return v < a;
    case '<=': return v <= a;
    case '==': return v === a;
    case 'between': return b != null && v >= Math.min(a, b) && v <= Math.max(a, b);
    default: return false;
  }
}

function renderFilterRows(){
  const wrap = $('#filterRows'); wrap.innerHTML = '';
  const cols = state.filter.data.columns;
  const mnames = state.filter.data.measurements || [];
  state.filter.constraints.forEach((c, i) => {
    const row = elt('div', 'frow');
    const del = elt('button', 'del', '✕');
    del.onclick = () => { state.filter.constraints.splice(i, 1); renderFilterRows(); };
    if (c.kind === 'meas'){
      row.appendChild(elt('span', 'ftag meas', 'measure'));
      const ns = elt('select');
      for (const n of mnames){ const o = elt('option'); o.value = n; o.textContent = n; ns.appendChild(o); }
      ns.value = c.name || mnames[0] || '';
      const ag = elt('select');
      for (const [v, lab] of F_AGGS){ const o = elt('option'); o.value = v; o.textContent = lab; ag.appendChild(o); }
      ag.value = c.agg || 'all';
      const op = elt('select');
      for (const [v, lab] of F_OPS){ const o = elt('option'); o.value = v; o.textContent = lab; op.appendChild(o); }
      op.value = c.op || '>';
      const v1 = elt('input'); v1.type = 'number'; v1.className = 'fval'; v1.value = c.val ?? '';
      const v2 = elt('input'); v2.type = 'number'; v2.className = 'fval'; v2.value = c.val2 ?? '';
      const nmin = elt('input'); nmin.type = 'number'; nmin.className = 'fval nmin';
      nmin.title = 'require at least this many measured timepoints (blank = 1)';
      nmin.placeholder = 'min n'; nmin.value = c.minN ?? '';
      const sync = () => {
        v2.hidden = op.value !== 'between';
        Object.assign(c, { name: ns.value, agg: ag.value, op: op.value,
          val: fnum(v1.value), val2: fnum(v2.value), minN: fnum(nmin.value) });
        updateFilterCount();
      };
      ns.onchange = ag.onchange = op.onchange = sync;
      v1.oninput = v2.oninput = nmin.oninput = sync;
      row.append(ns, ag, op, v1, v2, elt('span', 'muted', 'µm'), nmin, del);
      wrap.appendChild(row);
      sync();
      return;
    }
    // ---- annotation constraint ----
    row.appendChild(elt('span', 'ftag', 'annot'));
    const cs = elt('select');
    for (const name of Object.keys(cols)){ const o = elt('option'); o.value = name; o.textContent = name; cs.appendChild(o); }
    cs.value = c.col;
    const vs = elt('select');
    const num = elt('input'); num.type = 'number'; num.className = 'fval';
    const num2 = elt('input'); num2.type = 'number'; num2.className = 'fval';
    const ops = elt('select');
    for (const [v, lab] of [['is', '='], ['not', '≠'], ['set', 'is set'], ['unset', 'is unset'],
                            ...F_OPS]){
      const o = elt('option'); o.value = v; o.textContent = lab; ops.appendChild(o);
    }
    ops.value = c.op || 'is';
    const fill = () => {
      vs.innerHTML = '';
      for (const v of (cols[cs.value]?.values || [])){
        const o = elt('option'); o.value = v; o.textContent = v; vs.appendChild(o);
      }
      if (c.val != null && [...vs.options].some(o => o.value === String(c.val))) vs.value = String(c.val);
    };
    const sync = () => {
      const numeric = !['is', 'not', 'set', 'unset'].includes(ops.value);
      vs.hidden = numeric || ops.value === 'set' || ops.value === 'unset';
      num.hidden = !numeric;
      num2.hidden = ops.value !== 'between';
      Object.assign(c, { kind: 'ann', col: cs.value, op: ops.value,
        val: numeric ? fnum(num.value) : vs.value,
        val2: fnum(num2.value) });
      updateFilterCount();
    };
    fill();
    if (c.val != null && fnum(c.val) != null) num.value = c.val;
    if (c.val2 != null) num2.value = c.val2;
    cs.onchange = () => { fill(); sync(); };
    ops.onchange = vs.onchange = sync;
    num.oninput = num2.oninput = sync;
    row.append(cs, ops, vs, num, num2, del);
    wrap.appendChild(row);
    sync();
  });
  if (!state.filter.constraints.length)
    wrap.appendChild(elt('div', 'muted', 'no constraints — every well in the chosen plates matches'));
  updateFilterCount();
}

function annPass(w, c){
  const v = w.ann ? w.ann[c.col] : undefined;
  switch (c.op || 'is'){
    case 'set': return v != null && v !== '';
    case 'unset': return v == null || v === '';
    case 'not': return String(v) !== String(c.val);
    case 'is': return String(v) === String(c.val);
    default: return fcmp(fnum(v), c.op, c.val, c.val2);      // numeric on a free column
  }
}
function measPass(w, c){
  const m = (w.meas || {})[c.name];
  if (!m) return false;                                      // never measured → excluded
  if (c.minN && m.n < c.minN) return false;
  const vals = m.vals && m.vals.length ? m.vals : [m.mean];
  const test = v => fcmp(v, c.op, c.val, c.val2);
  if (c.agg === 'all') return vals.every(test);
  if (c.agg === 'any') return vals.some(test);
  return test(m[c.agg]);
}
function filterMatches(w){
  const plate = w.short || w.plate;
  const ps = state.filter.plates;
  if (ps && ps.size && !ps.has(plate)) return false;
  return state.filter.constraints.every(c => {
    if (c.kind === 'meas') return c.name && c.val != null ? measPass(w, c) : true;
    if (!c.col) return true;
    if (['is', 'not'].includes(c.op || 'is') && (c.val == null || c.val === '')) return true;
    if (!['set', 'unset', 'is', 'not'].includes(c.op) && c.val == null) return true;
    return annPass(w, c);
  });
}
function computeFilter(){
  const res = (state.filter.data.wells || []).filter(filterMatches);
  res.sort((a, b) => b.nann - a.nann || a.short.localeCompare(b.short) || a.well.localeCompare(b.well));
  return res;
}
function updateFilterCount(){
  const res = computeFilter();
  const nm = res.filter(w => w.meas && Object.keys(w.meas).length).length;
  const measured = state.filter.constraints.some(c => c.kind === 'meas');
  $('#filterCount').textContent = res.length + ' wells match'
    + (measured ? ` · ${nm} measured` : '');
}
function applyFilter(){
  state.filter.results = computeFilter();
  state.filter.active = true;
  state.filterSel = new Set(); state._filterSig = '';   // fresh selection + force a grid rebuild
  $('#filterModal').hidden = true; $('#clearFilterBtn').hidden = false;
  renderGrid();
  setStatus(`filtered: ${state.filter.results.length} wells across plates`, '');
}
function clearFilter(){ state.filter.active = false; state.filterSel = new Set(); state._filterSig = '';
  $('#clearFilterBtn').hidden = true; renderGrid(); }
// download the matching plate+well ids as JSON (consumed by well_hyperstack.py --from-json)
function exportFilter(){
  const res = computeFilter();
  if (!res.length){ setStatus('no wells match — nothing to export', 'err'); return; }
  const by_plate = {};
  for (const w of res){ (by_plate[w.plate] = by_plate[w.plate] || []).push(w.well); }
  for (const p in by_plate) by_plate[p].sort();
  const payload = {
    created: new Date().toISOString(),
    filter: state.filter.constraints.filter(c => c.col).map(c => ({ column: c.col, value: c.val })),
    n: res.length,
    by_plate,
    wells: res.map(w => ({ plate: w.plate, short: w.short, well: w.well, nann: w.nann, ann: w.ann })),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a = elt('a'); a.href = URL.createObjectURL(blob);
  a.download = 'wells_filter.json'; document.body.appendChild(a); a.click();
  a.remove(); URL.revokeObjectURL(a.href);
  setStatus(`exported ${res.length} wells → wells_filter.json`, 'saved');
}

// ---- TIF/MP4 export of the selected wells (or the active filter's wells) ----
function exportWells(){
  if (state.filter.active){                              // exactly the clicked / rubber-banded wells
    if (state.filterSel.size)
      return [...state.filterSel].map(k => { const [plate, well] = k.split('|'); return { plate, well }; });
    if (state.primary) return [{ plate: state.plateDir, well: state.primary }];   // fallback: the open one
    return [];
  }
  return [...state.sel].map(w => ({ plate: state.plateDir, well: w }));
}
// Render options ------------------------------------------------------------
// PLANE = which z the frame shows for a channel. 'focus' replays the slice keyframes
// as a continuous track (a focus pull); 'maxproj' flattens the stack; 'slice' pins one.
const PLANE_MODES = [['maxproj', 'max projection'], ['focus', 'annotated focus'],
                     ['mid', 'middle slice'], ['slice', 'one slice']];
const CMAPS = ['gray', 'invert', 'green', 'magenta', 'cyan', 'red', 'blue', 'yellow',
               'orange', 'amber', 'violet', 'ice', 'sepia', 'magma', 'viridis'];
const DEFAULT_CMAPS = ['gray', 'green', 'magenta', 'cyan', 'yellow', 'red'];

// One row per INCLUDED channel: plane mode (+ slice number) and colour. Rebuilt when
// the channel ticks change, so it never offers a channel that isn't being exported.
function renderPlaneRows(){
  const box = $('#exPlanes'); if (!box) return;
  const on = $$('#exChannels .exCh').filter(c => c.checked).map(c => c.value);
  const prev = state.exRender.channels || {};
  box.innerHTML = '';
  on.forEach((ch, i) => {
    const cur = prev[ch] || {};
    const row = elt('div', 'prow');
    row.appendChild(elt('b', 'pch', ch));
    const ms = elt('select');
    for (const [v, lab] of PLANE_MODES){ const o = elt('option'); o.value = v; o.textContent = lab; ms.appendChild(o); }
    ms.value = cur.mode || 'maxproj';
    const zin = elt('input'); zin.type = 'number'; zin.placeholder = 'z'; zin.className = 'zpick';
    if (cur.z != null) zin.value = cur.z;
    const cs = elt('select');
    for (const c of CMAPS){ const o = elt('option'); o.value = c; o.textContent = c; cs.appendChild(o); }
    cs.value = cur.cmap || DEFAULT_CMAPS[i % DEFAULT_CMAPS.length];
    const sync = () => {
      zin.hidden = ms.value !== 'slice';
      state.exRender.channels[ch] = { mode: ms.value, cmap: cs.value,
        z: zin.value === '' ? null : parseInt(zin.value, 10) };
      updateRenderNote();
    };
    ms.onchange = cs.onchange = zin.oninput = sync;
    row.append(ms, zin, elt('span', 'muted', 'colour'), cs);
    box.appendChild(row);
    sync();
  });
  for (const k in state.exRender.channels) if (!on.includes(k)) delete state.exRender.channels[k];
  const ov = $('#exOverlayLab');
  if (ov) ov.style.display = (on.length > 1 && state.exportKind !== 'tif') ? '' : 'none';
  if (on.length < 2) $('#exOverlay').checked = false;
  updateRenderNote();
}

// A plain-language summary of what will be rendered — plus the honest warning when a
// requested annotation track doesn't exist on this plate yet (it degrades, not fails).
function updateRenderNote(){
  const n = $('#exRenderNote'); if (!n) return;
  const tif = state.exportKind === 'tif';
  const bits = [];
  if (tif){
    const zm = $('#exZMode') ? $('#exZMode').value : 'all';
    bits.push(zm === 'all' ? 'every z-slice as acquired' : 'Z collapsed to one plane');
    bits.push('a hyperstack is data — nothing is burned in');
  } else {
    const chans = Object.entries(state.exRender.channels);
    if (chans.length) bits.push(chans.map(([k, v]) => `${k} ${v.mode}`).join(' · '));
    if ($('#exOverlay').checked) bits.push('composited into one movie');
    else if (chans.length > 1) bits.push('one movie per channel');
  }
  if ($('#exRotate').checked) bits.push('rotation applied');
  const needFocus = tif ? ($('#exZMode') && $('#exZMode').value === 'focus')
                        : Object.values(state.exRender.channels).some(c => c.mode === 'focus');
  const warn = [];
  if (needFocus && !anyImgCol('slice')) warn.push('no focus (slice) keyframes on this plate — falls back to a fixed slice');
  if ($('#exRotate').checked && !anyImgCol('rotation')) warn.push('no rotation keyframes on this plate — 0°');
  n.textContent = bits.join(' · ') + (warn.length ? '   ⚠ ' + warn.join('; ') : '');
  n.classList.toggle('warn', warn.length > 0);
}
// does ANY well on this plate carry keyframes for an image column?
function anyImgCol(col){
  const ia = (state.payload && state.payload.image_annotations) || {};
  for (const w in ia) for (const tp in ia[w]){
    const v = ia[w][tp][col];
    if (v != null && v !== '') return true;
  }
  return false;
}

function collectRender(){
  const labels = {};
  $$('#exLabelBlock .exLab').forEach(c => { if (c.checked) labels[c.value] = true; });
  labels.columns = $$('#exLabCols input').filter(c => c.checked).map(c => c.value);
  labels.corner = $('#exLabCorner').value;
  const sz = parseInt($('#exLabSize').value, 10);
  if (Number.isFinite(sz)) labels.size = sz;
  labels.colour = $('#exLabColour').value;
  if ($('#exLabPerTile').checked){ labels.time_per_tile = true; labels.scalebar_per_tile = true; }
  const r = { rotate: $('#exRotate').checked, overlay: $('#exOverlay').checked,
              channels: state.exRender.channels, labels };
  if (state.exportKind === 'tif'){
    r.labels = {};                              // a hyperstack is data — never burn text in
    r.overlay = false;
    r.z_mode = $('#exZMode').value;
    const z = parseInt($('#exZSlice').value, 10);
    if (Number.isFinite(z)) r.slices = [z];
  }
  return r;
}

function openExport(kind){
  state.exportKind = kind;
  state.exRender = state.exRender || { channels: {} };
  const ws = exportWells();
  $('#exportTitle').textContent = 'Export ' + kind.toUpperCase();
  const plates = new Set(ws.map(w => w.plate)).size;
  $('#exportWho').textContent = ws.length + ' well' + (ws.length === 1 ? '' : 's')
    + (state.filter.active ? ' selected' + (plates > 1 ? ` across ${plates} plates` : '')
                           : ' from ' + (state.plateDir || 'this plate'));
  $('#exMp4Row').style.display = (kind === 'mp4') ? '' : 'none';
  // channel checkboxes, one per discovered channel (BF, FL, and any extra fluorescence)
  const cbox = $('#exChannels'); cbox.innerHTML = '';
  state.manifest.channels.forEach(ch => {
    const lab = elt('label'); const cb = elt('input');
    cb.type = 'checkbox'; cb.checked = true; cb.className = 'exCh'; cb.value = ch;
    cb.onchange = renderPlaneRows;
    lab.appendChild(cb); lab.appendChild(document.createTextNode(' ' + ch));
    cbox.appendChild(lab); cbox.appendChild(document.createTextNode('  '));
  });
  // TIF has ONE shared Z axis across every channel, so it gets a single z-mode; MP4
  // shows one 2-D plane per frame, so each channel picks its own plane and colour.
  const tif = kind === 'tif';
  $('#exTifZRow').hidden = !tif;
  $('#exPlanes').hidden = tif;
  $('#exLabelBlock').hidden = tif;
  // well-scope annotation columns offered as tile labels (mixture, line, …)
  const lc = $('#exLabCols'); lc.innerHTML = '';
  const cols = Object.keys((state.payload && state.payload.columns) || {});
  if (!cols.length) lc.textContent = '— no well columns yet';
  for (const c of cols){
    const lab = elt('label'); const cb = elt('input');
    cb.type = 'checkbox'; cb.value = c; cb.onchange = updateRenderNote;
    lab.appendChild(cb); lab.appendChild(document.createTextNode(' ' + c));
    lc.appendChild(lab); lc.appendChild(document.createTextNode(' '));
  }
  renderPlaneRows();
  $('#exportStatus').textContent = '';
  $('#exportRun').disabled = ws.length === 0;
  $('#exportModal').hidden = false;
}
async function runExport(){
  const ws = exportWells();
  if (!ws.length) return;
  const channels = $$('#exChannels .exCh').filter(c => c.checked).map(c => c.value.toLowerCase());
  if (!channels.length){ $('#exportStatus').textContent = 'pick at least one channel'; return; }
  const num = id => { const v = parseInt(($(id).value || '').trim(), 10); return Number.isFinite(v) ? v : null; };
  const spec = {
    kind: state.exportKind,
    bundled: ($('input[name=exBundle]:checked') || {}).value === 'bundled',
    wells: ws, channels,
    tp_start: num('#exTpStart'), tp_end: num('#exTpEnd'), tp_step: num('#exTpStep'),
    fps: num('#exFps') || 20, render: collectRender(),
  };
  if (spec.render.slices) spec.slices = spec.render.slices;   // TIF: the single z to keep
  $('#exportRun').disabled = true; $('#exportStatus').textContent = 'queued…';
  try {
    await startExportJob(spec);                              // hand off to the job dock
    $('#exportModal').hidden = true;
    openJobDock(true);
    setStatus('export queued — see Jobs (top-right)', 'saved');
  } catch (e){ $('#exportStatus').textContent = 'failed: ' + e; }
  $('#exportRun').disabled = false;
}

// ---- export job dock (top-right progress queue) ---------------------------
// A running export is a background job on the server; this polls /api/export-jobs
// and renders one progress card per job so a whole-plate export shows real progress
// instead of a spinner. Jobs survive the modal being closed.
const jobUI = { jobs: {}, order: [], cleared: new Set(), timer: null, open: false };

async function startExportJob(spec){
  const { job_id } = await jpost('/api/export', spec);
  jobUI.cleared.delete(job_id);
  if (!jobUI.order.includes(job_id)) jobUI.order.unshift(job_id);
  jobUI.jobs[job_id] = { id: job_id, status: 'running', pct: 0, done: 0, total: 0,
    label: (spec.kind || 'tif').toUpperCase() + ' · ' + spec.wells.length +
      ' well' + (spec.wells.length === 1 ? '' : 's'), phase: 'starting…' };
  renderJobs();
  startJobPolling();
  return job_id;
}

function startJobPolling(){
  if (jobUI.timer) return;
  const tick = async () => {
    let running = false;
    try {
      const r = await jget('/api/export-jobs');
      for (const j of (r.jobs || [])){
        if (jobUI.cleared.has(j.id)) continue;
        jobUI.jobs[j.id] = j;
        if (!jobUI.order.includes(j.id)) jobUI.order.push(j.id);
        if (j.status === 'running') running = true;
      }
    } catch (e){ /* transient — keep last known state */ }
    renderJobs();
    jobUI.timer = running ? setTimeout(tick, 900) : null;
  };
  jobUI.timer = setTimeout(tick, 250);
}

function openJobDock(open){
  jobUI.open = (open === undefined) ? !jobUI.open : open;
  const caret = $('#jobCaret'); if (caret) caret.textContent = jobUI.open ? '▴' : '▾';
  renderJobs();
}

function clearDoneJobs(){
  for (const id of jobUI.order.slice()){
    const j = jobUI.jobs[id];
    if (j && j.status !== 'running'){ jobUI.cleared.add(id); delete jobUI.jobs[id]; }
  }
  jobUI.order = jobUI.order.filter(id => !jobUI.cleared.has(id));
  renderJobs();
}

function _fmtDur(s){ s = Math.max(0, Math.round(s || 0)); if (s < 60) return s + 's';
  return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's'; }
function _fmtSize(b){ const mb = (b || 0) / 1e6;
  return mb >= 1000 ? (mb / 1000).toFixed(1) + ' GB' : Math.max(0, Math.round(mb)) + ' MB'; }

function renderJobs(){
  const toggle = $('#jobToggle'); if (!toggle) return;
  const ids = jobUI.order.filter(id => jobUI.jobs[id] && !jobUI.cleared.has(id));
  const running = ids.filter(id => jobUI.jobs[id].status === 'running').length;
  toggle.hidden = ids.length === 0;                    // the Jobs button lives in the top bar
  toggle.classList.toggle('busy', running > 0);
  const lbl = $('#jobToggleLabel');
  if (lbl) lbl.textContent = running ? (running + ' running…') : ('Jobs (' + ids.length + ')');
  const panel = $('#jobPanel'); if (panel) panel.hidden = !jobUI.open || ids.length === 0;
  const list = $('#jobList'); if (!list || !jobUI.open) return;
  list.textContent = '';
  for (const id of ids){
    const j = jobUI.jobs[id];
    const st = j.status === 'done' ? 'done' : j.status === 'error' ? 'error' : 'run';
    const pct = Math.round(100 * (j.status === 'done' ? 1 : (j.pct || 0)));
    const card = elt('div', 'jobCard ' + st);
    const head = elt('div', 'jt');
    head.appendChild(elt('b', null, j.label || j.kind || 'export'));
    head.appendChild(elt('span', 'jpc', j.status === 'done' ? '✓' : j.status === 'error' ? 'failed' : pct + '%'));
    card.appendChild(head);
    const unit = (j.status === 'running' && j.total > 1) ? ` · ${j.done}/${j.total}` : '';
    card.appendChild(elt('div', 'jobPhase', (j.phase || j.msg || j.status) + unit));
    const bar = elt('div', 'jobBar'); const fill = elt('i'); fill.style.width = pct + '%';
    bar.appendChild(fill); card.appendChild(bar);
    const meta = elt('div', 'jobMeta');
    let left = '';
    if (j.status === 'running') left = _fmtDur(j.elapsed) + (j.eta != null ? ' · ~' + _fmtDur(j.eta) + ' left' : '');
    else if (j.status === 'done') left = _fmtDur(j.elapsed) + ' · ' + _fmtSize(j.size) + (j.count > 1 ? ' · ' + j.count + ' files' : '');
    else left = j.msg || 'error';
    meta.appendChild(elt('span', null, left));
    if (j.status === 'done'){
      const dl = elt('a', 'lnk', 'download');
      dl.href = '/api/export-download?id=' + encodeURIComponent(id); dl.setAttribute('download', '');
      meta.appendChild(dl);
    }
    card.appendChild(meta);
    if (j.status === 'done' && j.out) card.appendChild(elt('div', 'jobPath', j.out));
    // what the renderer actually FOUND per well (e.g. "no slice keyframes → fixed SL3").
    // Silent fallbacks are how an export quietly stops matching what you annotated.
    if ((j.notes || []).length){
      const nb = elt('div', 'jobNotes');
      for (const n of j.notes.slice(0, 12)) nb.appendChild(elt('div', null, n));
      if (j.notes.length > 12) nb.appendChild(elt('div', 'muted', `+${j.notes.length - 12} more`));
      card.appendChild(nb);
    }
    list.appendChild(card);
  }
}
// filter-grid thumbnail timepoint: the frame-fader fraction mapped onto the
// well's own length (server clamps to the nearest real frame).
function filterTp(r){
  const n = r.n_tps || 120;
  return Math.max(1, Math.round(1 + state.gridFrac * (n - 1)));
}
function renderFilterGrid(){
  const res = state.filter.results;
  const sig = res.map(r => r.plate + '|' + r.well).join(',');
  if (sig === state._filterSig && state.cells.size){    // results unchanged (e.g. a plate loaded)
    renderGridBadges(); updateFilterSelInfo(); return;  // → just refresh highlights, no image refetch
  }
  state._filterSig = sig;
  const g = $('#grid'); g.innerHTML = ''; state.cells.clear();
  g.style.gridTemplateColumns = 'repeat(12, 1fr)';
  $('#pageLabel').textContent = `${res.length} matched · sorted by #annotations`;
  $('#pagePrev').disabled = $('#pageNext').disabled = true;
  for (const r of res){
    const cell = elt('div', 'cell'); cell.dataset.well = r.well; cell.dataset.plate = r.plate;
    const img = elt('img'); img.loading = 'lazy'; img.draggable = false;
    img.src = frameURLd(r.plate, r.well, state.gridChannel, filterTp(r), 130);
    cell.appendChild(img);
    cell.appendChild(Object.assign(elt('span','plate-lab'), { textContent: `${r.short} ${r.well}` }));
    cell.appendChild(Object.assign(elt('span','nann'), { textContent: r.nann }));
    cell.onclick = e => onFilterCellClick(r, e);
    g.appendChild(cell);
    state.cells.set(r.plate + '|' + r.well, cell);
  }
  renderGridBadges(); updateFilterSelInfo();
}
// Filter selection spans plates: ⇧/⌘-click or rubber-band builds a cross-plate set
// (highlight + export), while a plain click also loads that well's plate to annotate it.
function onFilterCellClick(r, e){
  state.arrowMode = 'wells';
  if (state.rubberDidDrag){ state.rubberDidDrag = false; return; }
  const key = r.plate + '|' + r.well;
  if (e.shiftKey || e.metaKey || e.ctrlKey){            // toggle in the selection — NO reload
    if (state.filterSel.has(key) && state.filterSel.size > 1) state.filterSel.delete(key);
    else state.filterSel.add(key);
    renderGridBadges(); updateFilterSelInfo(); return;
  }
  state.filterSel = new Set([key]);                     // plain click = just this one, and annotate it
  loadFilterWell(r.plate, r.well);
}
function updateFilterSelInfo(){
  const n = state.filterSel.size;
  if (!n){ $('#selInfo').textContent = `${state.filter.results.length} matched — click to annotate; `
    + `⇧-click or rubber-band to select across plates`; return; }
  const plates = new Set([...state.filterSel].map(k => k.split('|')[0])).size;
  $('#selInfo').textContent = `${n} selected across ${plates} plate${plates===1?'':'s'} — ⤓ exports exactly these`;
}
async function loadFilterWell(plate, well){
  state.arrowMode = 'wells';                             // ← → now move through the filtered selection
  state.filterSel = new Set([plate + '|' + well]);
  if (state.plateDir !== plate){
    if (state.dirty) await saveNow();
    await loadPlate(plate);                 // flicker-free: renderFilterGrid's sig guard skips rebuild
  }
  setPrimary(well, true);
  state.scope = 'image'; state.imageMode = true;
  renderPanel();
  renderGridBadges(); updateFilterSelInfo();
}

function renderGridBadges(){
  const filt = state.filter.active;
  const col = (!filt && state.activeCol) ? state.payload.columns[state.activeCol] : null;
  const ia = state.payload.image_annotations || {};
  for (const [, cell] of state.cells){
    const w = cell.dataset.well;
    if (filt){                                 // filter cells: cross-plate selection highlight
      cell.classList.toggle('selected', state.filterSel.has(cell.dataset.plate + '|' + w));
      cell.classList.toggle('primary', cell.dataset.plate === state.plateDir && state.primary === w);
      continue;                                // filter cells carry their own label, no badge/dot
    }
    cell.classList.toggle('selected', state.sel.has(w));
    cell.classList.toggle('primary', state.primary === w);
    // dot: ORANGE if this well has an Iwamatsu stage keyframe, teal if only other image annotations
    const frames = ia[w] ? Object.values(ia[w]) : [];
    const hasStage = frames.some(e => e && 'iwamatsu_stage' in e);
    cell.classList.toggle('hasstage', hasStage);
    cell.classList.toggle('hasimg', frames.length > 0 && !hasStage);
    const badge = cell.querySelector('.badge');
    const v = col ? (state.payload.annotations[w] || {})[state.activeCol] : undefined;
    if (v == null){ badge.textContent = ''; badge.style.background = 'transparent'; }
    else { badge.textContent = fmtVal(v); badge.style.background = valueColor(col, v); }
  }
}
function onCellClick(w, e){
  state.arrowMode = 'wells';                             // ← → now move between wells
  if (state.rubberDidDrag){ state.rubberDidDrag = false; return; }
  if (e.shiftKey || e.metaKey || e.ctrlKey){
    if (state.sel.has(w) && state.sel.size > 1) state.sel.delete(w); else state.sel.add(w);
  } else state.sel = new Set([w]);
  setPrimary(w, false);
  renderGridBadges(); renderPanel();
}
function setPrimary(w, resnap){
  state.primary = w;
  if (resnap) state.sel = new Set([w]);
  clampFrame();
  renderDetail();
  if (state.scope === 'image') renderPanel();
}

// rubber-band selection over the empty grid area
function wireRubber(){
  const wrap = $('#gridWrap'), rubber = $('#rubber');
  let sx, sy, add;
  wrap.addEventListener('mousedown', e => {
    if (e.button !== 0) return;
    e.preventDefault();                 // stop the browser's native image-drag / text-select
    sx = e.clientX; sy = e.clientY; add = e.shiftKey || e.metaKey;
    state.rubberDidDrag = false;
    const move = ev => {
      const dx = Math.abs(ev.clientX - sx), dy = Math.abs(ev.clientY - sy);
      if (dx < 4 && dy < 4) return;
      state.rubberDidDrag = true;
      const wrapR = wrap.getBoundingClientRect();
      const x0 = Math.min(sx, ev.clientX), y0 = Math.min(sy, ev.clientY);
      const x1 = Math.max(sx, ev.clientX), y1 = Math.max(sy, ev.clientY);
      rubber.hidden = false;
      rubber.style.left = (x0 - wrapR.left + wrap.scrollLeft) + 'px';
      rubber.style.top = (y0 - wrapR.top + wrap.scrollTop) + 'px';
      rubber.style.width = (x1 - x0) + 'px'; rubber.style.height = (y1 - y0) + 'px';
      const filt = state.filter.active;
      const hit = new Set(add ? (filt ? state.filterSel : state.sel) : []);
      for (const [, cell] of state.cells){
        const r = cell.getBoundingClientRect();
        if (r.right >= x0 && r.left <= x1 && r.bottom >= y0 && r.top <= y1)
          hit.add(filt ? (cell.dataset.plate + '|' + cell.dataset.well) : cell.dataset.well);  // cross-plate in filter mode
      }
      if (filt) state.filterSel = hit; else state.sel = hit;
      renderGridBadges();
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      rubber.hidden = true;
      if (state.rubberDidDrag){
        if (state.filter.active){ renderGridBadges(); updateFilterSelInfo(); }   // cross-plate: highlight + count only
        else { if (state.sel.size) setPrimary([...state.sel][0], false); renderPanel(); }
      }
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
}

// ---- detail / scrubber ---------------------------------------------------
function curTps(){ return (state.manifest && (state.manifest.frames[state.primary] || {})[state.channel]) || []; }
function clampFrame(){ const n = curTps().length; if (!n){ state.frameIdx = 0; return; }
  state.frameIdx = Math.max(0, Math.min(n - 1, state.frameIdx)); }
function curTp(){ const t = curTps(); return t.length ? t[state.frameIdx] : null; }
// Warm the server-side cache for this well's frames in the background (fire-and-forget),
// so scrubbing over a slow share is fast. Fires once per well change.
function prefetchWell(well){
  if (!well || !state.plateDir) return;
  jget(`/api/prefetch?dir=${encodeURIComponent(state.plateDir)}&well=${encodeURIComponent(well)}&size=600`).catch(() => {});
}
function renderDetail(){
  $('#detailWell').textContent = state.primary || '—';
  $$('.chBtn').forEach(b => b.classList.toggle('active', b.dataset.ch === state.channel));
  if (state.primary && state.primary !== state._pfWell){ state._pfWell = state.primary; prefetchWell(state.primary); }
  const tps = curTps();
  const scrub = $('#scrub');
  scrub.max = Math.max(0, tps.length - 1); scrub.value = state.frameIdx;
  updateBigImg(); updateFrameInfo(); updateRangeBar();
}
// z shown for the selected channel: the z-slider preview if the user is browsing,
// else the annotated 'slice' keyframe (null => server serves the middle slice).
function curZ(tp){
  if (!channelHasZ(state.channel)) return null;
  return state.zview != null ? state.zview : sliceAt(tp);
}
function updateBigImg(){
  const tp = curTp();
  const img = $('#bigImg');
  updateFaders();
  if (tp == null){ img.removeAttribute('src'); renderTools(); return; }
  img.src = frameURL(state.primary, state.channel, tp, 600, curZ(tp));
  // prefetch neighbours for a smooth fader (each at its own annotated slice)
  const tps = curTps();
  [state.frameIdx - 1, state.frameIdx + 1, state.frameIdx + 2].forEach(i => {
    if (i >= 0 && i < tps.length){ const im = new Image(); im.src = frameURL(state.primary, state.channel, tps[i], 600, curZ(tps[i])); }
  });
  renderTools();   // let every registered plugin tool repaint for (well, tp)
  // rotation transform — a live preview while dragging, else the interpolated keyframe
  if (img && img.style){
    const rot = state.rotview != null ? state.rotview : imgInterpolate(state.primary, 'rotation', tp);
    img.style.transformOrigin = 'center center';
    img.style.transform = rot ? ('rotate(' + rot + 'deg)') : '';
  }
}
// Broadcast the current (well, tp) to every registered tool so it can redraw
// (rotation transform / measurement overlay). Called at the end of updateBigImg.
function renderTools(){
  const w = state.primary, tp = curTp();
  for (const type in toolHandlers){
    const h = toolHandlers[type];
    if (h && typeof h.onRender === 'function'){ try { h.onRender(w, tp); } catch (e){} }
  }
}
function updateFrameInfo(){
  const tps = curTps(), tp = curTp();
  if (tp == null){ $('#frameInfo').textContent = 'no frames'; return; }
  const iv = Number(state.manifest.autofill.timepoint_interval_min) || 0;
  const mins = iv ? ` · +${((tp - 1) * iv)} min` : '';
  const z = curZ(tp);
  const sl = z != null ? ` · slice ${z}` : '';
  const stg = stageAt(tp);                      // effective (held) Iwamatsu stage
  const st = stg ? ` · ${stg}` : '';
  $('#frameInfo').textContent = `tp ${tp} / ${tps[tps.length - 1]} (${state.frameIdx + 1}/${tps.length})${mins}${st}${sl}`;
}
// the Iwamatsu stage the keyframes forward-fill to at a given timepoint (or null)
function stageAt(tp){
  if (tp == null || !state.primary) return null;
  if (!(state.payload.image_columns && state.payload.image_columns['iwamatsu_stage'])) return null;
  const e = imgEffective(state.primary, 'iwamatsu_stage', tp);
  return e.value || null;
}
function setFrame(i){ const n = curTps().length; if (!n) return;
  state.frameIdx = ((i % n) + n) % n; $('#scrub').value = state.frameIdx;
  updateBigImg(); updateFrameInfo();
  if (state.scope === 'image') renderPanel();
  else if (state.scope === 'well') updateRangeLive(); }
// keep the range control's live "playhead → tp N" readout in sync while scrubbing
function updateRangeLive(){
  const ph = document.querySelector('.rangebox .rng-ph'); if (!ph) return;
  const tp = curTp(); ph.textContent = tp != null ? `playhead → tp ${tp}` : '';
}
function togglePlay(){ state.playing ? stopPlay() : startPlay(); }
function startPlay(){ if (curTps().length < 2) return; state.playing = true; $('#playBtn').textContent = '❚❚';
  state.playTimer = setInterval(() => setFrame(state.frameIdx + 1), 120); }
function stopPlay(){ state.playing = false; $('#playBtn').textContent = '▶'; clearInterval(state.playTimer);
  syncGridToDetail(); }                                  // catch the thumbnails up to where you paused
// The detail scrubber drives the miniature grid's timepoint too (so one fader does both). To
// stay smooth we DON'T repaint 96 thumbnails every playback tick — only when playback is halted
// (on pause, or when you release the scrubber).
function syncGridToDetail(){
  if (state.playing || !state.plateDir) return;
  const tps = curTps(); if (tps.length < 2) return;
  state.gridFrac = state.frameIdx / (tps.length - 1);   // map the primary's frame → a dev fraction
  refreshGridThumbs();
}
function refreshGridThumbs(){
  for (const [, cell] of state.cells){
    const img = cell.querySelector('img'); if (!img) continue;
    const w = cell.dataset.well;
    if (state.filter.active){
      const r = state.filter.results.find(x => x.plate === cell.dataset.plate && x.well === w);
      if (r) img.src = frameURLd(r.plate, r.well, state.gridChannel, filterTp(r), 130);
    } else { const rep = repFrame(w, state.gridChannel); if (rep) img.src = frameURL(w, rep.ch, rep.tp, 130); }
  }
}

// range overlay on the scrubber (shows the active range column for the primary well)
function activeRangeCol(){
  // in well scope, the active column if it's a range; else the first range col
  if (state.scope === 'well' && state.activeCol){
    const c = state.payload.columns[state.activeCol]; if (c && c.type === 'range') return state.activeCol;
  }
  return Object.entries(state.payload.columns).find(([, c]) => c.type === 'range')?.[0] || null;
}
function updateRangeBar(){
  if (!state.plateDir) return;                          // no plate loaded (e.g. Settings tab)
  const bar = $('#rangeBar'), hs = $('#rhStart'), he = $('#rhEnd'), wrap = $('#scrubWrap');
  const name = activeRangeCol();
  const tps = curTps();
  const rng = name && state.primary ? (state.payload.annotations[state.primary] || {})[name] : null;
  if (!rng || tps.length < 2){ bar.hidden = hs.hidden = he.hidden = true; return; }
  const idxOf = tp => { let best = 0, bd = Infinity;
    tps.forEach((t, i) => { const d = Math.abs(t - tp); if (d < bd){ bd = d; best = i; } }); return best; };
  const a = idxOf(rng[0]) / (tps.length - 1), b = idxOf(rng[1]) / (tps.length - 1);
  const W = wrap.clientWidth;
  bar.hidden = hs.hidden = he.hidden = false;
  bar.style.left = (Math.min(a, b) * W) + 'px';
  bar.style.width = (Math.abs(b - a) * W) + 'px';
  hs.style.left = (a * W) + 'px';       // draggable start/end handles sit on the edges
  he.style.left = (b * W) + 'px';
}

// ------------------------------------------------------------------ panel
function renderPanel(){
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.scope === state.scope));
  const body = $('#panelBody'); body.innerHTML = '';
  if (!state.payload){ body.textContent = '—'; return; }
  if (state.scope === 'well') renderWell(body);
  else if (state.scope === 'plate') renderPlate(body);
  else renderImage(body);
}

// Settings tab: where annotations/exports are stored, which formats, and help.
function openSettings(){                                // Settings lives in a top-bar modal now
  const b = $('#settingsBody'); if (!b) return;
  b.innerHTML = ''; renderSettings(b);
  const m = $('#settingsModal'); if (m) m.hidden = false;
}
function renderSettings(body){
  const s = state.settings || (state.cfg && state.cfg.settings) ||
            { annotations_dir:'', export_dir:'', formats:{db:true, csv:true, json:true} };
  s.formats = s.formats || { db:true, csv:true, json:true };
  state.settings = s;
  let t = null;
  const push = () => { clearTimeout(t); t = setTimeout(() => {
    jpost('/api/settings', s).then(r => { if (r && r.db) state.cfg.db = r.db;
      setStatus('settings saved', 'saved'); }).catch(e => setStatus('settings failed: ' + e, 'err'));
  }, 300); };

  const sec = (title, hint) => { const d = elt('div','section');
    d.appendChild(elt('h4', null, title)); if (hint) d.appendChild(elt('div','muted', hint));
    body.appendChild(d); return d; };

  const s1 = sec('Where annotations are saved',
    'The database, CSV and JSON all go here. Leave empty to keep them next to your data.');
  s1.appendChild(pathRow(s.annotations_dir || '', v => { s.annotations_dir = v; push(); }));

  const s2 = sec('Save annotations as', 'CSV opens in Excel; JSON is the full record; the database powers the pipeline tools.');
  const fr = elt('div','inline'); fr.style.marginTop = '4px';
  [['db','Database (.db)'],['csv','CSV'],['json','JSON']].forEach(([k, lab]) => {
    const l = elt('label'); const cb = elt('input'); cb.type = 'checkbox'; cb.checked = !!s.formats[k];
    cb.onchange = () => { s.formats[k] = cb.checked; push(); };
    l.appendChild(cb); l.appendChild(document.createTextNode(' ' + lab)); l.style.marginRight = '14px';
    fr.appendChild(l);
  });
  s2.appendChild(fr);

  const s3 = sec('Where TIF / MP4 exports go',
    'Organised into per-well / montage folders here. Leave empty to save next to the plate.');
  s3.appendChild(pathRow(s.export_dir || '', v => { s.export_dir = v; push(); }));

  const sc = sec('Connected folders — all writing to ONE database',
    'Every folder listed here saves into the same database shown below — your laptop, the '
    + 'server share, an external drive. The same plate under a different folder name is matched '
    + 'automatically, so the names need not be identical.');
  const conn = elt('div', 'muted', 'loading…'); conn.style.marginTop = '4px'; sc.appendChild(conn);
  jget('/api/connections').then(c => {
    conn.innerHTML = '';
    const db = elt('div'); db.style.marginBottom = '4px';
    db.innerHTML = '<b>Database:</b> <code>' + (c.active_db || '(none)') + '</code> — '
                 + (c.annotation_count || 0) + ' annotations';
    conn.appendChild(db);
    const roots = c.data_roots || [];
    if (roots.length){
      roots.forEach(r => { const li = elt('div', 'muted');
        li.innerHTML = '↳ <code>' + r.path + '</code>' + (r.label ? ' — ' + r.label : '');
        conn.appendChild(li); });
    } else {
      conn.appendChild(elt('div', 'muted', 'No folders connected yet — open one to link it here.'));
    }
  }).catch(() => { conn.textContent = '(could not load connections)'; });

  const s4 = sec('Help', null);
  const hb = elt('button', 'btn sm', 'Keyboard shortcuts & help');
  hb.onclick = () => { const h = $('#help'); if (h) h.hidden = false; };
  s4.appendChild(hb);

  // ---- version + update ----------------------------------------------------
  // run.sh already fast-forwards on launch; this is for when the app has been open a
  // while, and it is also the honest place to show what commit you are actually running.
  const s5 = sec('Version', null);
  const vline = elt('div', 'muted mono', 'v' + ((state.cfg && state.cfg.version) || '?'));
  s5.appendChild(vline);
  const row = elt('div', 'inline'); row.style.marginTop = '6px';
  const chk = elt('button', 'btn sm', 'Check for updates');
  const upd = elt('button', 'btn sm primary', 'Update now'); upd.hidden = true;
  const msg = elt('span', 'muted');
  const show = v => {
    const g = v.git || {};
    vline.textContent = 'v' + v.version + (g.sha ? '  ·  ' + g.sha + (g.branch ? ' (' + g.branch + ')' : '') : '')
      + (g.dirty ? '  ·  local edits' : '');
    upd.hidden = !v.update_available;
    msg.textContent = !g.sha ? 'not a git checkout — update by downloading a new build'
      : v.update_available ? v.behind + ' new commit' + (v.behind === 1 ? '' : 's') + ' waiting'
      : 'up to date';
  };
  chk.onclick = async () => {
    msg.textContent = 'checking…';
    try { show(await jget('/api/version?fetch=1')); }
    catch (e){ msg.textContent = 'check failed: ' + e; }
  };
  upd.onclick = async () => {
    msg.textContent = 'updating…';
    try {
      const r = await jpost('/api/update', {});
      msg.textContent = r.msg || (r.ok ? 'updated' : 'update failed');
      show(r);
      if (r.restart) setStatus('updated — restart PlateNotate to load the new version', 'saved');
    } catch (e){ msg.textContent = 'update failed: ' + e; }
  };
  row.append(chk, upd, msg);
  s5.appendChild(row);
  jget('/api/version').then(show).catch(() => {});      // free: uses the last fetch
}
// The version chip in the top bar. Free on boot (it reads the last fetch), and it turns
// amber the moment the checkout is behind — so an out-of-date copy is visible, not silent.
function initVersionChip(){
  const chip = $('#verChip'); if (!chip) return;
  const v = (state.cfg && state.cfg.version) || '';
  chip.textContent = v ? 'v' + v : '';
  chip.onclick = () => {
    const st = $('#settingsToggle'); if (st && st.onclick) st.onclick();
    else { const m = $('#settingsModal'); if (m){ m.hidden = false; renderSettings($('#settingsBody')); } }
  };
  jget('/api/version').then(r => {
    chip.textContent = 'v' + r.version + (r.update_available ? ' ↑' : '');
    chip.classList.toggle('upd', !!r.update_available);
    if (r.update_available)
      chip.title = r.behind + ' new commit' + (r.behind === 1 ? '' : 's') + ' available — open Settings to update';
  }).catch(() => {});
}

// a folder path input + Browse (native picker in the desktop app; type-a-path in a browser)
function pathRow(value, onset){
  const row = elt('div', 'inline'); row.style.marginTop = '6px';
  const inp = elt('input'); inp.type = 'text'; inp.value = value || ''; inp.placeholder = '(default)';
  inp.style.cssText = 'flex:1;min-width:180px';
  inp.onchange = () => onset(inp.value.trim());
  const br = elt('button', 'btn sm', 'Browse…');
  br.onclick = async () => {
    const api = window.pywebview && window.pywebview.api;
    if (api && api.pick_folder){
      try { const p = await api.pick_folder(); if (p){ inp.value = p; onset(p); } } catch (e){}
    } else { inp.focus(); setStatus('type a folder path here (the native picker is desktop-app only)', ''); }
  };
  row.append(inp, br);
  return row;
}

// selection summary of a well-column's value across the current selection
function selSummary(name){
  const wells = [...state.sel]; if (!wells.length) return { kind:'none' };
  const vals = wells.map(w => (state.payload.annotations[w] || {})[name]);
  const first = JSON.stringify(vals[0] ?? null);
  if (vals.every(v => JSON.stringify(v ?? null) === first))
    return vals[0] == null ? { kind:'unset' } : { kind:'one', value: vals[0] };
  return { kind:'mixed' };
}
// unified "current value" for a column at any scope (well = across the selection;
// plate = the single record; image = the primary well's CURRENT frame).
function currentVal(scope, name){
  if (scope === 'well') return selSummary(name);
  let v;
  if (scope === 'plate') v = state.payload.plate_annotations[name];
  else { const tp = curTp();
    v = tp != null ? ((state.payload.image_annotations[state.primary] || {})[String(tp)] || {})[name] : undefined; }
  return v == null ? { kind:'unset' } : { kind:'one', value: v };
}

function renderWell(body){
  // selection header
  const wells = [...state.sel];
  const h = elt('div', 'section');
  const sumTxt = wells.length ? `${wells.length} well${wells.length>1?'s':''} selected — assign a value below` :
    'click or rubber-band wells to select';
  h.innerHTML = `<h4>Selection</h4><div class="muted">${sumTxt}</div>`;
  body.appendChild(h);

  // columns
  const sec = elt('div', 'section'); sec.appendChild(Object.assign(elt('h4'), { textContent:'Well columns' }));
  const cols = state.payload.columns;
  if (!Object.keys(cols).length) sec.appendChild(elt('div', 'muted', 'no columns yet — add one below'));
  for (const [name, col] of Object.entries(cols)) sec.appendChild(colRow('well', name, col));
  body.appendChild(sec);

  // add column + suggestions
  body.appendChild(addColumnForm('well'));
  body.appendChild(suggestions('well'));
  $('#selInfo').textContent = wells.length ? `selected: ${wells.slice(0,12).join(', ')}${wells.length>12?' …':''}` : 'no wells selected';
}

// one column row with its value chips / range / free control
function colRow(scope, name, col){
  const row = elt('div', 'colrow');
  if (scope === 'well' && name === state.activeCol) row.classList.add('active');
  const head = elt('div', 'colname');
  head.innerHTML = `<span>${name}</span><span class="coltype">${col.type}</span>`;
  if (scope === 'well') head.title = 'make active (digit keys assign its values)';
  if (scope === 'well') head.onclick = () => { state.activeCol = name; renderPanel(); renderGridBadges(); };
  row.appendChild(head);

  const vals = elt('div', 'colvals');
  const assign = (v) => doAssign(scope, name, v);

  if (col.type === 'categorical' || col.type === 'binary'){
    const summary = currentVal(scope, name);
    (col.values || []).forEach((v, i) => {
      const chip = elt('div', 'chip'); chip.title = scope === 'well' ? `key ${i+1}` : '';
      chip.appendChild(Object.assign(elt('span','dot'), { style:`background:${valueColor(col, v)}` }));
      chip.appendChild(document.createTextNode(v));
      const isSel = summary.kind === 'one' && summary.value === v;
      if (isSel) chip.classList.add('sel');
      const x = elt('span','x','×'); x.title = 'delete value'; x.onclick = ev => { ev.stopPropagation(); deleteValue(scope, name, v); };
      chip.onclick = () => assign(v);
      chip.appendChild(x);
      vals.appendChild(chip);
    });
    if (scope === 'well' && selSummary(name).kind === 'mixed')
      vals.appendChild(elt('span', 'chip mixed', 'mixed'));
    // add value
    const add = elt('div', 'chip add', '+ value');
    add.onclick = () => { const v = prompt(`New value for “${name}”:`); if (v && v.trim()) addValue(scope, name, v.trim()); };
    vals.appendChild(add);
    // clear
    const clr = elt('div', 'chip clear', 'clear');
    clr.onclick = () => assign(null);
    vals.appendChild(clr);
    if (col.type === 'binary') vals.appendChild(binaryDefault(scope, name, col));
  }
  else if (col.type === 'range'){
    vals.appendChild(rangeControl(scope, name, col));
  }
  else { // free
    vals.appendChild(freeControl(scope, name));
  }
  row.appendChild(vals);

  // actions
  const act = elt('div', 'colactions');
  const ren = elt('button', 'iconbtn', 'rename'); ren.onclick = () => renameColumn(scope, name);
  const del = elt('button', 'iconbtn', 'delete'); del.onclick = () => deleteColumn(scope, name);
  act.append(ren, del); row.appendChild(act);
  return row;
}

function binaryDefault(scope, name, col){
  const wrap = elt('span', 'inline');
  const lbl = elt('span', 'muted', 'default:');
  const s = elt('select');
  ['(none)', ...(col.values||[])].forEach(v => { const o = elt('option'); o.value = v === '(none)' ? '' : v; o.textContent = v; s.appendChild(o); });
  s.value = col.default || '';
  s.onchange = () => mutate(() => { if (s.value) state.payload[colsKey[scope]][name].default = s.value;
    else delete state.payload[colsKey[scope]][name].default; });
  wrap.append(lbl, s);
  if (scope === 'well' && col.default){
    const fill = elt('button', 'iconbtn', '→ fill unset');
    fill.title = 'set every well without a value to the default';
    fill.onclick = () => fillDefault(name, col.default);
    wrap.appendChild(fill);
  }
  return wrap;
}

function rangeControl(scope, name, col){
  const box = elt('div', 'rangebox');
  if (scope !== 'well'){ box.appendChild(elt('span','muted','(range applies at the well level)')); return box; }
  const sum = selSummary(name);
  const cur = sum.kind === 'one' ? sum.value : null;
  // current value + a LIVE playhead readout (updateRangeLive keeps it in sync)
  const top = elt('div', 'inline');
  const label = elt('span', 'big', cur ? `[${cur[0]} – ${cur[1]}]` : (sum.kind === 'mixed' ? 'mixed' : 'not set'));
  const ph = elt('span', 'muted rng-ph');
  const tp = curTp(); ph.textContent = tp != null ? `playhead → tp ${tp}` : '';
  top.append(label, ph);

  // buttons use the LIVE current frame (no stale tp baked into the label)
  const row = elt('div', 'inline');
  const setStart = elt('button','btn sm', '⟝ set start');
  const setEnd   = elt('button','btn sm', 'set end ⟞');
  setStart.onclick = () => setRangeEdge(name, 'start');
  setEnd.onclick   = () => setRangeEdge(name, 'end');
  const clr = elt('button','btn sm ghost','clear'); clr.onclick = () => doAssign('well', name, null);
  row.append(setStart, setEnd, clr);

  box.append(top, row, elt('div','hint',
    'scrub the trajectory to a frame, then set start / end — once set, drag the amber handles on the scrubber to fine-tune. Applies to all selected wells.'));
  return box;
}

function freeControl(scope, name){
  const inp = elt('input'); inp.type = 'text'; inp.style.minWidth = '220px';
  const s = currentVal(scope, name);
  inp.value = s.kind === 'one' ? s.value : '';
  inp.placeholder = s.kind === 'mixed' ? 'mixed — type to overwrite' : 'type a value';
  inp.onchange = () => doAssign(scope, name, inp.value.trim() || null);
  return inp;
}

// ---- assignment primitives ----
function doAssign(scope, name, value){
  if (scope === 'well'){
    const wells = [...state.sel];
    if (!wells.length){ setStatus('select wells first', 'err'); return; }
    mutate(() => {
      for (const w of wells){
        const e = state.payload.annotations[w] || (state.payload.annotations[w] = {});
        if (value == null) delete e[name]; else e[name] = value;
        if (!Object.keys(e).length) delete state.payload.annotations[w];
      }
      if (value != null) registerVal('well', name, value);
    });
  } else if (scope === 'plate'){
    mutate(() => { if (value == null) delete state.payload.plate_annotations[name];
      else { state.payload.plate_annotations[name] = value; registerVal('plate', name, value); }
      if (name === 'annotator') { state.payload.annotator = value || ''; $('#annotator').value = value || ''; } });
  } else { // image: current primary well + current timepoint
    const w = state.primary, tp = curTp();
    if (w == null || tp == null){ setStatus('no frame', 'err'); return; }
    mutate(() => {
      const ww = state.payload.image_annotations[w] || (state.payload.image_annotations[w] = {});
      const e = ww[String(tp)] || (ww[String(tp)] = {});
      if (value == null) delete e[name]; else e[name] = value;
      if (!Object.keys(e).length) delete ww[String(tp)];
      if (!Object.keys(ww).length) delete state.payload.image_annotations[w];
      if (value != null) registerVal('image', name, value);
    });
  }
}
function registerVal(scope, name, value){
  const col = state.payload[colsKey[scope]][name];
  if (col && (col.type === 'categorical' || col.type === 'binary') && typeof value === 'string'
      && !col.values.includes(value)) col.values.push(value);
}

function addValue(scope, name, v){ mutate(() => { const c = state.payload[colsKey[scope]][name];
  if (c && !c.values.includes(v)) c.values.push(v); }); }
function deleteValue(scope, name, v){
  mutate(() => {
    const c = state.payload[colsKey[scope]][name]; if (!c) return;
    c.values = c.values.filter(x => x !== v);
    if (c.default === v) delete c.default;
    // strip the value from any annotation that used it
    stripValue(scope, name, v);
  });
}
function stripValue(scope, name, v){
  if (scope === 'well') for (const [w, e] of Object.entries(state.payload.annotations)){
    if (e[name] === v){ delete e[name]; if (!Object.keys(e).length) delete state.payload.annotations[w]; } }
  else if (scope === 'plate'){ if (state.payload.plate_annotations[name] === v) delete state.payload.plate_annotations[name]; }
  else for (const [w, ww] of Object.entries(state.payload.image_annotations))
    for (const [tp, e] of Object.entries(ww)){ if (e[name] === v){ delete e[name];
      if (!Object.keys(e).length) delete ww[tp]; if (!Object.keys(ww).length) delete state.payload.image_annotations[w]; } }
}
function fillDefault(name, def){ mutate(() => { for (const w of state.manifest.wells){
  const e = state.payload.annotations[w] || (state.payload.annotations[w] = {});
  if (e[name] == null) e[name] = def; } }); }

function setRangeEdge(name, which){
  const tp = curTp(); if (tp == null) return;
  const wells = [...state.sel]; if (!wells.length){ setStatus('select wells first','err'); return; }
  mutate(() => { for (const w of wells){
    const e = state.payload.annotations[w] || (state.payload.annotations[w] = {});
    let r = Array.isArray(e[name]) ? e[name].slice() : [tp, tp];
    if (which === 'start') r[0] = tp; else r[1] = tp;
    e[name] = [Math.min(r[0], r[1]), Math.max(r[0], r[1])];
  } });
}

// ---- add-column form + suggestions ----
function addColumnForm(scope){
  const f = elt('div', 'addform');
  const nm = elt('input'); nm.type = 'text'; nm.placeholder = 'new column name'; nm.size = 14;
  const ty = elt('select');
  // angle / measurement are image-tool column types — only offer them in the image scope.
  const types = state.cfg.column_types.filter(t => (t === 'angle' || t === 'measurement') ? scope === 'image' : true);
  types.forEach(t => { const o = elt('option'); o.value = t; o.textContent = t; ty.appendChild(o); });
  const vv = elt('input'); vv.type = 'text'; vv.placeholder = 'values, comma-separated'; vv.size = 20;
  const upd = () => { vv.style.display = (ty.value === 'categorical' || ty.value === 'binary') ? '' : 'none';
    if (ty.value === 'binary' && !vv.value) vv.value = 'true,false'; };
  ty.onchange = upd; upd();
  const btn = elt('button', 'btn sm primary', '+ column');
  btn.onclick = () => {
    const name = nm.value.trim(); if (!name) return;
    if (state.payload[colsKey[scope]][name]){ setStatus('column exists', 'err'); return; }
    const values = (ty.value === 'categorical' || ty.value === 'binary')
      ? vv.value.split(',').map(s => s.trim()).filter(Boolean) : [];
    addColumn(scope, name, ty.value, values);
    nm.value = ''; vv.value = '';
  };
  f.append(elt('span','muted','add:'), nm, ty, vv, btn);
  return f;
}
function addColumn(scope, name, type, values){
  mutate(() => {
    const spec = { type, values: (type === 'categorical' || type === 'binary') ? [...values] : [] };
    if (type === 'binary' && spec.values.length >= 2) spec.default = spec.values[1]; // conventional false-default
    if (type === 'angle') spec.fill = 'interpolate';    // angle keyframes ease (smoothstep) between boundaries
    state.payload[colsKey[scope]][name] = spec;
    if (scope === 'well') state.activeCol = name;
  });
}
function renameColumn(scope, name){
  const nn = prompt(`Rename column “${name}” to:`, name); if (!nn || nn.trim() === name) return;
  const nu = nn.trim();
  if (state.payload[colsKey[scope]][nu]){ setStatus('name in use', 'err'); return; }
  mutate(() => {
    const ck = colsKey[scope]; const cols = state.payload[ck];
    const reord = {}; for (const [k, v] of Object.entries(cols)) reord[k === name ? nu : k] = v;
    state.payload[ck] = reord;
    if (scope === 'well'){ for (const e of Object.values(state.payload.annotations)) if (name in e){ e[nu] = e[name]; delete e[name]; }
      if (state.activeCol === name) state.activeCol = nu; }
    else if (scope === 'plate'){ const a = state.payload.plate_annotations; if (name in a){ a[nu] = a[name]; delete a[name]; } }
    else for (const ww of Object.values(state.payload.image_annotations)) for (const e of Object.values(ww)) if (name in e){ e[nu] = e[name]; delete e[name]; }
  });
}
function deleteColumn(scope, name){
  if (!confirm(`Delete column “${name}” and all its values?`)) return;
  mutate(() => {
    delete state.payload[colsKey[scope]][name];
    if (scope === 'well'){ for (const [w, e] of Object.entries(state.payload.annotations)){ delete e[name];
      if (!Object.keys(e).length) delete state.payload.annotations[w]; }
      if (state.activeCol === name) state.activeCol = Object.keys(state.payload.columns)[0] || null; }
    else if (scope === 'plate') delete state.payload.plate_annotations[name];
    else for (const [w, ww] of Object.entries(state.payload.image_annotations)){ for (const [tp, e] of Object.entries(ww)){ delete e[name];
      if (!Object.keys(e).length) delete ww[tp]; } if (!Object.keys(ww).length) delete state.payload.image_annotations[w]; }
  });
}
function suggestions(scope){
  const s = elt('div', 'section');
  const reg = state.cfg.suggestions[scope] || {};
  const defs = (state.cfg.defaults[scope] && state.cfg.defaults[scope].columns) || {};
  const have = state.payload[colsKey[scope]];
  const merged = {};
  for (const [n, spec] of Object.entries(defs)) if (!(n in have)) merged[n] = { type: spec.type || 'categorical', values: spec.values || [] };
  for (const [n, spec] of Object.entries(reg)) if (!(n in have) && !(n in merged)) merged[n] = { type: spec.type || 'categorical', values: spec.values || [] };
  if (!Object.keys(merged).length) return s;
  s.appendChild(elt('h4', null, 'Suggestions (from other plates)'));
  const wrap = elt('div', 'sugg');
  for (const [n, spec] of Object.entries(merged)){
    const p = elt('button', 'pill', `+ ${n} · ${spec.type}`);
    p.onclick = () => addColumn(scope, n, spec.type, spec.values || []);
    wrap.appendChild(p);
  }
  s.appendChild(wrap);
  return s;
}

// ---- plate tab ----
function renderPlate(body){
  const bar = elt('div', 'section inline');
  const af = elt('button', 'btn sm', 'Autofill from metadata');
  af.onclick = autofillPlate;
  bar.append(elt('h4', null, 'Plate fields'), af);
  body.appendChild(bar);
  const cols = state.payload.plate_columns;
  if (!Object.keys(cols).length) body.appendChild(elt('div','muted','no plate fields — add one below'));
  for (const [name, col] of Object.entries(cols)) body.appendChild(colRow('plate', name, col));
  body.appendChild(addColumnForm('plate'));
  body.appendChild(suggestions('plate'));
}
function autofillPlate(){
  const af = state.manifest.autofill || {};
  mutate(() => {
    for (const [k, v] of Object.entries(af)){
      if (v === '' || v == null) continue;
      if (state.payload.plate_columns[k] && state.payload.plate_annotations[k] == null)
        state.payload.plate_annotations[k] = String(v);
    }
  });
  setStatus('autofilled plate fields', '');
}

// ---- Image tab: KEYFRAME / forward-fill staging + slice ----
// image_annotations[well][tp][col] stores only KEYFRAMES (boundaries) for a
// forward-fill column; the effective value at a frame is the most recent
// keyframe at or before it. Setting a value places/moves/removes a boundary.
function imgKeyframes(well, col){
  const ww = state.payload.image_annotations[well] || {};
  const kf = {};
  for (const tp in ww){ if (ww[tp][col] != null) kf[+tp] = ww[tp][col]; }
  return kf;                                       // {tpInt: value}
}
function imgEffective(well, col, tp){              // forward-filled value at tp
  const kf = imgKeyframes(well, col);
  const le = Object.keys(kf).map(Number).filter(k => k <= tp).sort((a, b) => a - b);
  if (!le.length) return { value: null, startTp: null };
  const s = le[le.length - 1];
  return { value: kf[s], startTp: s };
}
function writeImgKeyframes(well, col, kf){
  const ia = state.payload.image_annotations, ww = ia[well];
  if (ww) for (const tp of Object.keys(ww)){ delete ww[tp][col]; if (!Object.keys(ww[tp]).length) delete ww[tp]; }
  for (const tp of Object.keys(kf)){
    const w2 = ia[well] || (ia[well] = {}), t = String(tp);
    (w2[t] || (w2[t] = {}))[col] = kf[tp];
  }
  if (ia[well] && !Object.keys(ia[well]).length) delete ia[well];
}
function canonicalizeKf(kf){                        // drop boundaries equal to the previous one
  let prev;
  for (const k of Object.keys(kf).map(Number).sort((a, b) => a - b)){
    if (kf[k] === prev) delete kf[k]; else prev = kf[k];
  }
  return kf;
}
// click value V on the current frame: set a boundary, or re-anchor / toggle it off
function setImageKeyframe(col, value){
  const well = state.primary, tp = curTp();
  if (well == null || tp == null){ setStatus('no frame', 'err'); return; }
  mutate(() => {
    const kf = imgKeyframes(well, col);
    const le = Object.keys(kf).map(Number).filter(k => k <= tp).sort((a, b) => a - b);
    const rs = le.length ? le[le.length - 1] : null;   // boundary governing this frame
    const eff = rs != null ? kf[rs] : null;
    if (value == null){ if (rs != null) delete kf[rs]; }          // clear this run
    else if (eff === value){
      if (rs === tp) delete kf[tp];                                // click same value here → toggle off
      else { if (rs != null) delete kf[rs]; kf[tp] = value; }      // → re-anchor this run to here
    } else kf[tp] = value;                                         // new boundary (dups pruned below)
    canonicalizeKf(kf);
    writeImgKeyframes(well, col, kf);
    if (value != null) registerVal('image', col, value);
  }, { only: ['panel', 'badges'] });
  if (col === 'slice'){ state.zview = null; updateBigImg(); }  // annotating a slice snaps the z view back to it
  updateFrameInfo();                            // stage + slice both show in the readout
}

// Commit the current z as the 'slice' focus keyframe at this timepoint — this is how
// the z-slider annotates focus (always SET, never toggle-off). canonicalizeKf drops it
// if it's redundant with the held value, so it never creates a needless boundary.
function commitSlice(z){
  const well = state.primary, tp = curTp();
  if (well == null || tp == null || !Number.isFinite(z)) return;
  ensureImageCols();                            // make sure the 'slice' column exists
  mutate(() => {
    const kf = imgKeyframes(well, 'slice');
    kf[tp] = String(z);
    canonicalizeKf(kf);
    writeImgKeyframes(well, 'slice', kf);
    registerVal('image', 'slice', String(z));
  }, { only: ['panel', 'badges'] });
  state.zview = null;                            // now follow the committed keyframe
  updateBigImg(); updateFrameInfo();
}

// Commit the current angle as the 'rotation' keyframe at this timepoint (the rotation
// fader's counterpart to commitSlice). Interpolation between keyframes takes the
// short way around the circle (see imgInterpolate / rot_tool).
function commitRotation(deg){
  const well = state.primary, tp = curTp();
  if (well == null || tp == null || !Number.isFinite(deg)) return;
  ensureImageCols();                             // ensure the 'rotation' (angle) column exists
  mutate(() => {
    const kf = imgKeyframes(well, 'rotation');
    kf[tp] = String(((deg % 360) + 360) % 360);
    canonicalizeKf(kf);
    writeImgKeyframes(well, 'rotation', kf);
  }, { only: ['panel', 'badges'] });
  state.rotview = null;
  updateBigImg(); updateFrameInfo();
}

function renderImage(body){
  if (!state.primary){ body.appendChild(elt('div','muted','select a well to annotate its frames')); return; }
  // image-level is always on — it just shows this frame's annotations (or none yet).
  body.appendChild(elt('h4', 'section', `Image — ${state.primary}`));
  const tp = curTp();
  body.appendChild(Object.assign(elt('div','section big'),
    { textContent: tp==null ? 'no frame' : `Frame tp ${tp}  (${state.frameIdx+1}/${curTps().length})` }));

  ensureImageCols();
  const cols = state.payload.image_columns;
  // 1) forward-fill keyframe columns (iwamatsu_stage) — categorical UI. 'slice' (focus)
  //    is now set with the z-slider under the image, so its button UI is removed here.
  for (const [name, col] of Object.entries(cols)){
    if (name === 'slice') continue;                 // focus set via z-fader — keyframes shown below
    if (col.fill === 'forward') body.appendChild(fillColumn(name, col, tp));
  }
  // focus (z) + rotation are set with the faders under the image; show their RECORDED
  // keyframes here so they stay visible / removable from the Image tab.
  if (cols['slice']) body.appendChild(faderKeyframeSection('slice', 'Focus keyframes — z-slider', ''));
  if (cols['rotation']) body.appendChild(faderKeyframeSection('rotation', 'Rotation keyframes — rotation slider', '°', true));
  // 2) plugin tool columns (measurement) — rendered by the plugin's own panel. The
  //    'angle' (rotation) tool is now the fader under the image, so skip its dial here.
  for (const [name, col] of Object.entries(cols)){
    if (col.type === 'angle') continue;
    if (col.fill !== 'forward' && toolPanels[col.type]) body.appendChild(toolColumnSection(name, col));
  }
  // 3) remaining plain per-frame columns — categorical/range/free UI
  const pts = Object.entries(cols).filter(([, c]) => c.fill !== 'forward' && !toolPanels[c.type]);
  if (pts.length){
    const sec = elt('div','section'); sec.appendChild(elt('h4', null, 'Other image columns (this frame only)'));
    for (const [name, col] of pts) sec.appendChild(colRow('image', name, col));
    body.appendChild(sec);
  }
  body.appendChild(addColumnForm('image'));
}
// A tool column (angle/measurement): header + activate toggle + the plugin's own
// panel (dial / measurement readout). Activating it makes #stage route pointer
// input to this tool; only one tool column is active at a time.
function toolColumnSection(name, col){
  const active = activeToolCol === name;
  const sec = elt('div', 'section toolcol' + (active ? ' active' : ''));
  const head = elt('div', 'toolhead');
  const title = elt('span', 'colname'); title.textContent = name;
  const ty = elt('span', 'coltype'); ty.textContent = col.type;
  const btn = elt('button', 'btn sm toolbtn' + (active ? ' active' : ''),
    active ? '● drawing on image' : '○ activate on image');
  btn.title = active ? 'click to stop routing image clicks to this column'
                     : 'route image drag/clicks to this column (' + col.type + ')';
  btn.onclick = () => { if (activeToolCol === name) deactivateImageTool(); else activateImageTool(name); };
  head.append(title, ty, btn);
  sec.appendChild(head);
  // the plugin renders its controls here
  const panelWrap = elt('div', 'toolpanel');
  const fn = toolPanels[col.type];
  if (fn){ try { fn(panelWrap, name); } catch (e){ panelWrap.appendChild(elt('div','muted','tool panel error: ' + e.message)); } }
  else panelWrap.appendChild(elt('div','muted','no tool registered for type “' + col.type + '”'));
  sec.appendChild(panelWrap);
  // rename / delete
  const act = elt('div', 'colactions');
  const ren = elt('button', 'iconbtn', 'rename'); ren.onclick = () => renameColumn('image', name);
  const del = elt('button', 'iconbtn', 'delete');
  del.onclick = () => { if (activeToolCol === name) deactivateImageTool(); deleteColumn('image', name); };
  act.append(ren, del); sec.appendChild(act);
  return sec;
}
function ensureImageCols(){
  const d = (state.cfg.defaults.image || {}).columns || {};
  for (const key of ['iwamatsu_stage', 'slice']){
    if (state.payload.image_columns[key] || !d[key]) continue;
    let values = d[key].values ? [...d[key].values] : [];
    if (key === 'iwamatsu_stage' && !values.length)
      values = ((state.cfg.iwamatsu_stages.stages) || []).map(s => s.value);
    if (key === 'slice' && (state.manifest.z_slices || []).length)
      values = state.manifest.z_slices.map(String);   // the plate's real z-range (e.g. 1..7)
    state.payload.image_columns[key] = { type:'categorical', values, fill:'forward' };
  }
  // Seed the rotation keyframe column (angle · smoothstep interpolate) so the
  // rot_tool plugin is available immediately, alongside slice / iwamatsu_stage.
  if (!state.payload.image_columns['rotation'])
    state.payload.image_columns['rotation'] = { type:'angle', values:[], fill:'interpolate' };
}
// one forward-fill column: effective readout + value picker + keyframe strip
function fillColumn(name, col, tp){
  const sec = elt('div', 'section');
  const eff = tp != null ? imgEffective(state.primary, name, tp) : { value:null, startTp:null };
  sec.appendChild(elt('h4', null,
    name === 'iwamatsu_stage' ? 'Iwamatsu stage (keyframe)' :
    name === 'slice' ? 'Slice (keyframe)' : name + ' (keyframe)'));
  const here = elt('div', 'muted');
  if (eff.value == null) here.textContent = 'here: — not set —';
  else here.innerHTML = `here: <b>${eff.value}</b> ` +
    (eff.startTp === tp ? '<span class="mono">(set on this frame — click again to clear)</span>'
                        : `<span class="mono">(held from tp ${eff.startTp} — click a value to change from here)</span>`);
  sec.appendChild(here);

  if (name === 'iwamatsu_stage'){
    const grid = elt('div', 'stagegrid');
    const stages = (state.cfg.iwamatsu_stages.stages) || [];
    const byVal = Object.fromEntries(stages.map(s => [s.value, s]));
    (col.values.length ? col.values : stages.map(s => s.value)).forEach(v => {
      const meta = byVal[v] || { name:'' };
      const b = elt('button', 'stagebtn' + (eff.value === v ? ' sel' : ''));
      b.innerHTML = `<span class="n">${v}</span><span class="nm">${meta.name||''}</span>`;
      b.title = meta.name ? `${v} — ${meta.name}` : v;
      b.onclick = () => setImageKeyframe(name, v);
      grid.appendChild(b);
    });
    sec.appendChild(grid);
  } else {
    const row = elt('div', 'colvals');
    (col.values || []).forEach(v => {
      const chip = elt('div', 'chip' + (eff.value === v ? ' sel' : ''), v);
      chip.onclick = () => setImageKeyframe(name, v);
      row.appendChild(chip);
    });
    const add = elt('div', 'chip add', '+ value');
    add.onclick = () => { const v = prompt(`New value for “${name}”:`);
      if (v && v.trim()) mutate(() => { if (!col.values.includes(v.trim())) col.values.push(v.trim()); }); };
    row.appendChild(add);
    sec.appendChild(row);
  }
  sec.appendChild(keyframeStrip(name));
  return sec;
}
// Focus (z) and rotation are SET with the faders under the image, but their recorded
// keyframes are shown here (Image tab) so they're visible, jump-to-able and removable.
function faderKeyframeSection(name, title, unit, interp){
  const sec = elt('div', 'section');
  sec.appendChild(elt('h4', null, title));
  const tp = curTp();
  let val = null, startTp = null;
  if (tp != null){
    if (interp){
      if (Object.keys(imgKeyframes(state.primary, name)).length)
        val = Math.round(imgInterpolate(state.primary, name, tp));
    } else { const eff = imgEffective(state.primary, name, tp); val = eff.value; startTp = eff.startTp; }
  }
  const here = elt('div', 'muted');
  if (val == null) here.innerHTML = 'here: <span class="mono">— none —</span> · drag the '
    + 'fader under the image and turn its ● button on to record a keyframe';
  else here.innerHTML = 'here: <b>' + val + (unit || '') + '</b>'
    + (startTp === tp ? ' <span class="mono">(set on this frame)</span>'
       : startTp != null ? ' <span class="mono">(held from tp ' + startTp + ')</span>' : '');
  sec.appendChild(here);
  sec.appendChild(keyframeStrip(name));
  return sec;
}
function keyframeStrip(col){
  const wrap = elt('div', 'stamped');
  const kf = imgKeyframes(state.primary, col);
  const keys = Object.keys(kf).map(Number).sort((a, b) => a - b);
  if (!keys.length){ wrap.appendChild(elt('span','muted','no keyframes yet')); return wrap; }
  wrap.appendChild(elt('span', 'muted', 'boundaries:'));
  for (const t of keys){
    const chip = elt('div', 'stamp', `tp ${t}: ${kf[t]}`);
    chip.title = 'jump to this frame';
    chip.onclick = () => { const i = curTps().indexOf(t); if (i >= 0) setFrame(i); };
    const x = elt('span', 'x', '✕'); x.title = 'remove this boundary';
    x.onclick = e => { e.stopPropagation(); mutate(() => {
      const k = imgKeyframes(state.primary, col); delete k[t]; canonicalizeKf(k);
      writeImgKeyframes(state.primary, col, k); }, { only:['panel','badges'] }); };
    chip.appendChild(x);
    wrap.appendChild(chip);
  }
  return wrap;
}

// ------------------------------------------------------------------ channel buttons / options
function buildChannelButtons(){
  const box = $('.chToggle'); box.innerHTML = '';
  state.manifest.channels.forEach(ch => {
    const b = elt('button', 'btn sm chBtn' + (ch === state.channel ? ' active' : ''), ch);
    b.dataset.ch = ch;
    b.onclick = () => { if (state.channel === ch) return; state.channel = ch; clampFrame(); renderDetail(); };
    box.appendChild(b);
  });
}
function buildGridChannelOptions(){
  const s = $('#gridChannel'); s.innerHTML = '';
  state.manifest.channels.forEach(ch => { const o = elt('option'); o.value = ch; o.textContent = ch; s.appendChild(o); });
  s.value = state.gridChannel;
}

// ------------------------------------------------------------------ static wiring
function wireStatic(){
  $('#plateSelect').onchange = e => { if (state.filter.active) clearFilter(); loadPlate(e.target.value); };
  // The annotator is a SESSION identity: switching it saves any pending edits under the OLD
  // name, remembers the NEW one, and reloads the plate so you see only YOUR annotations.
  const commitAnnotator = async e => {
    const name = e.target.value.trim();
    if (name === state.me) return;
    if (state.dirty) { try { await saveNow(); } catch (_){} }   // pending edits belong to old me
    state.me = name;
    try { localStorage.setItem('annotator', name); } catch (_){}
    if (state.payload){ state.payload.annotator = name;
      if (state.payload.plate_columns && state.payload.plate_columns.annotator)
        state.payload.plate_annotations.annotator = name || undefined; }
    setStatus(name ? ('annotator: ' + name + ' — showing only your annotations')
                   : 'annotator cleared — showing all', 'saved');
    if (state.plateDir) await loadPlate(state.plateDir);        // re-isolate to this annotator
  };
  $('#annotator').onchange = commitAnnotator;
  $('#annotator').onkeydown = e => { if (e.key === 'Enter'){ commitAnnotator(e); e.target.blur(); } };  // Enter = set
  $('#undoBtn').onclick = undo; $('#redoBtn').onclick = redo;
  $('#saveBtn').onclick = saveNow;
  { const hb = $('#helpBtn'); if (hb) hb.onclick = () => $('#help').hidden = false; }  // help now lives in Settings; topbar button removed
  $('#helpClose').onclick = () => $('#help').hidden = true;
  $('#help').onclick = e => { if (e.target.id === 'help') $('#help').hidden = true; };
  $('#filterBtn').onclick = () => openFilter().catch(e => setStatus('filter: ' + e.message, 'err'));
  $('#clearFilterBtn').onclick = clearFilter;
  $('#filterClose').onclick = () => $('#filterModal').hidden = true;
  $('#filterModal').onclick = e => { if (e.target.id === 'filterModal') $('#filterModal').hidden = true; };
  $('#filterAddRow').onclick = addAnnRow;
  $('#filterApply').onclick = applyFilter;
  $('#filterExport').onclick = exportFilter;
  const _fc = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };
  _fc('#filterAddMeas', addMeasRow);
  _fc('#fPlatesAll', () => {
    for (const w of (state.filter.data.wells || [])) state.filter.plates.add(w.short || w.plate);
    renderFilterPlates(); updateFilterCount();
  });
  _fc('#fPlatesNone', () => { state.filter.plates.clear(); renderFilterPlates(); updateFilterCount(); });
  _fc('#filterSave', saveFilter);
  _fc('#filterDelete', deleteFilter);
  const fs = $('#filterSaved');
  if (fs) fs.onchange = () => { if (fs.value) loadSavedFilter(fs.value); };
  const _oc = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };   // null-safe bind
  _oc('#tifBtn', () => openExport('tif'));
  _oc('#mp4Btn', () => openExport('mp4'));
  _oc('#exportRun', runExport);
  _oc('#jobToggle', () => openJobDock());
  _oc('#jobClear', clearDoneJobs);
  _oc('#exportClose', () => { $('#exportModal').hidden = true; });
  _oc('#exportModal', e => { if (e.target.id === 'exportModal') $('#exportModal').hidden = true; });
  // Render controls: every one of them re-writes the summary line under the block, so
  // you can read what the export will actually do before you start it.
  const _on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
  _on('#exRotate', 'change', updateRenderNote);
  _on('#exOverlay', 'change', updateRenderNote);
  _on('#exZMode', 'change', () => {
    const one = $('#exZMode').value === 'slice';
    $('#exZSlice').hidden = !one;
    updateRenderNote();
  });
  _oc('#openFolderBtn', () => { $('#folderPath').value = (state.cfg && state.cfg.data_root) || ''; $('#folderStatus').textContent = ''; $('#folderModal').hidden = false; });
  // native folder picker — only in the desktop app. pywebview injects window.pywebview
  // asynchronously and fires `pywebviewready`, so wire it now AND on that event.
  const enableNativePicker = () => {
    const b = $('#folderBrowse');
    if (!b || !(window.pywebview && window.pywebview.api)) return;
    b.hidden = false;
    b.onclick = async () => {
      try {
        const p = await window.pywebview.api.pick_folder();
        if (p){ $('#folderPath').value = p; $('#folderStatus').textContent = ''; }
      } catch (e){ $('#folderStatus').textContent = 'picker failed: ' + e; }
    };
  };
  enableNativePicker();
  window.addEventListener('pywebviewready', enableNativePicker);
  _oc('#folderClose', () => { $('#folderModal').hidden = true; });
  _oc('#folderModal', e => { if (e.target.id === 'folderModal') $('#folderModal').hidden = true; });
  _oc('#settingsToggle', openSettings);
  _oc('#settingsClose', () => { $('#settingsModal').hidden = true; });
  _oc('#settingsModal', e => { if (e.target.id === 'settingsModal') $('#settingsModal').hidden = true; });
  _oc('#folderOpen', async () => {
    const path = ($('#folderPath').value || '').trim();
    if (!path){ $('#folderStatus').textContent = 'enter a path'; return; }
    $('#folderStatus').textContent = 'opening…';
    try {
      const r = await jpost('/api/open-folder', { path });
      if (!(r && r.ok)){ $('#folderStatus').textContent = (r && r.error) || 'failed'; return; }
      if (r.foreign_db && r.foreign_db.count){         // this folder carries its OWN database
        const st = $('#folderStatus');
        st.innerHTML = 'This folder has its <b>own database</b> with <b>' + r.foreign_db.count
          + '</b> annotation(s) not in the one you’re using.<br>'
          + '<button id="fMerge" class="primary" style="margin-top:6px">Merge them in (safe, keeps newest)</button> '
          + '<button id="fIgnore" style="margin-top:6px">Ignore &amp; use my database</button>';
        $('#fMerge').onclick = async () => {
          st.textContent = 'merging…';
          try { await jpost('/api/merge-folder', { db: r.foreign_db.path, path }); }
          catch (e){ st.textContent = 'merge failed: ' + e; return; }
          location.reload();
        };
        $('#fIgnore').onclick = () => location.reload();
        return;
      }
      location.reload();                               // clean case: reboot against the new folder
    } catch (e){ $('#folderStatus').textContent = 'failed: ' + e; }
  });

  $$('.tab').forEach(t => t.onclick = () => { state.scope = t.dataset.scope; renderPanel(); updateRangeBar(); updateFaders(); });

  $('#pagePrev').onclick = () => { if (state.page>0){ state.page--; renderGrid(); } };
  $('#pageNext').onclick = () => { state.page++; renderGrid(); };
  $('#perPage').onchange = e => { state.perPage = Number(e.target.value); state.page = 0; renderGrid(); };
  $('#gridChannel').onchange = e => { state.gridChannel = e.target.value; renderGrid(); };
  $('#blockInput').onkeydown = e => { if (e.key === 'Enter') applyBlock(e.target.value); };

  $$('.chBtn').forEach(b => b.onclick = () => { state.channel = b.dataset.ch; clampFrame(); renderDetail(); });
  $('#playBtn').onclick = () => { state.arrowMode = 'frame'; togglePlay(); };       // ← → now step frames
  const $scrub = $('#scrub');
  $scrub.oninput = e => { stopPlay(); setFrame(Number(e.target.value)); };
  $scrub.addEventListener('pointerdown', () => { state.arrowMode = 'frame'; });   // ← → now step frames
  $scrub.onchange = () => { if (!state.playing) syncGridToDetail(); };            // release → move all thumbnails here
  { const zsl = $('#zslider'); if (zsl){
      zsl.addEventListener('pointerdown', () => { state.arrowMode = 'z'; });        // ← → now nudge focus
      zsl.oninput = e => { state.zview = Number(e.target.value); updateBigImg(); updateFrameInfo(); };  // drag = live preview (look)
      zsl.onchange = e => { if (state.zrec) commitSlice(Number(e.target.value)); };                     // release = record ONLY if on
  } }
  { const zrb = $('#zrec'); if (zrb) zrb.onclick = () => { state.zrec = !state.zrec; updateFaders(); }; }
  { const rsl = $('#rotslider'); if (rsl){
      rsl.addEventListener('pointerdown', () => { state.arrowMode = 'rotation'; });  // ← → now nudge rotation
      rsl.oninput = e => { state.rotview = Number(e.target.value); updateBigImg(); updateRotslider(); }; // drag = live preview (look)
      rsl.onchange = e => { if (state.rotrec) commitRotation(Number(e.target.value)); };                 // release = record ONLY if on
  } }
  { const rrb = $('#rotrec'); if (rrb) rrb.onclick = () => { state.rotrec = !state.rotrec; updateFaders(); }; }
  wireRubber(); wireRangeHandles(); wireKeys();
  window.addEventListener('beforeunload', e => { if (state.dirty){ saveNow(); } });
}
function applyBlock(spec){
  try {
    const wells = parseBlockClient(spec).filter(w => state.manifest.frames[w]);
    if (!wells.length){ setStatus('no matching wells', 'err'); return; }
    state.sel = new Set(wells); setPrimary(wells[0], false);
    renderGridBadges(); renderPanel();
  } catch (err){ setStatus('bad block: ' + err.message, 'err'); }
}
// a compact client mirror of model.parse_well_range for the common forms
function parseBlockClient(spec){
  const norm = p => { const m = /^([A-Za-z])(\d{1,2})$/.exec(p.trim()); if (!m) return null;
    const r = m[1].toUpperCase().charCodeAt(0)-65, c = +m[2]-1; if (r<0||r>7||c<0||c>11) return null;
    return String.fromCharCode(65+r) + String(c+1).padStart(2,'0'); };
  const s = spec.trim(), low = s.toLowerCase();
  const out = [];
  if (low.startsWith('col')){ const rest = low.replace(/^col(umns?|s)?/,''); for (const c of intSet(rest,1,12)) for (let r=0;r<8;r++) out.push(String.fromCharCode(65+r)+String(c).padStart(2,'0')); return out; }
  if (low.startsWith('row')){ const rest = s.replace(/^rows?/i,'').toUpperCase(); for (const r of rowSet(rest)) for (let c=1;c<=12;c++) out.push(String.fromCharCode(65+r)+String(c).padStart(2,'0')); return out; }
  if (s.includes(',') && !s.includes(':') && !s.includes('-')) return s.split(',').map(norm).filter(Boolean);
  const sep = s.includes(':') ? ':' : (s.includes('-') ? '-' : null);
  if (!sep){ const p = norm(s); if (!p) throw new Error('well'); return [p]; }
  const [a,b] = s.split(sep).map(norm); if (!a||!b) throw new Error('corner');
  const [ra,ca] = [a.charCodeAt(0)-65, +a.slice(1)-1], [rb,cb] = [b.charCodeAt(0)-65, +b.slice(1)-1];
  for (let r=Math.min(ra,rb);r<=Math.max(ra,rb);r++) for (let c=Math.min(ca,cb);c<=Math.max(ca,cb);c++) out.push(String.fromCharCode(65+r)+String(c+1).padStart(2,'0'));
  return out;
}
function intSet(rest,lo,hi){ const out=new Set(); for (const t of rest.split(/[,\s]+/).filter(Boolean)){ if (t.includes('-')){ const [a,b]=t.split('-').map(Number); for (let v=Math.min(a,b);v<=Math.max(a,b);v++) out.add(v); } else out.add(+t); } return [...out].filter(v=>v>=lo&&v<=hi); }
function rowSet(rest){ const out=new Set(); for (const t of rest.split(/[,\s]+/).filter(Boolean)){ if (t.includes('-')){ const [a,b]=t.split('-'); for (let v=a.charCodeAt(0)-65;v<=b.charCodeAt(0)-65;v++) out.add(v); } else out.add(t.charCodeAt(0)-65); } return [...out].filter(v=>v>=0&&v<8); }

// draggable range handles on the scrubber — adjust valid_frames by dragging the
// amber handles that appear once a range is set. Live-preview while dragging,
// committed as a single undo step on release. Applies to all selected wells.
function wireRangeHandles(){
  const wrap = $('#scrubWrap');
  for (const which of ['start', 'end']){
    const handle = $(which === 'start' ? '#rhStart' : '#rhEnd');
    handle.addEventListener('mousedown', e => {
      const name = activeRangeCol();
      if (state.scope !== 'well' || !name || !state.sel.size) return;
      e.preventDefault(); e.stopPropagation();
      const tps = curTps(); if (tps.length < 2) return;
      const snap = clone(state.payload);
      const move = ev => {
        const wr = wrap.getBoundingClientRect();
        const frac = Math.max(0, Math.min(1, (ev.clientX - wr.left) / wr.width));
        const idx = Math.round(frac * (tps.length - 1)); const tp = tps[idx];
        state.frameIdx = idx; $('#scrub').value = idx; updateBigImg(); updateFrameInfo();
        for (const w of state.sel){
          const en = state.payload.annotations[w] || (state.payload.annotations[w] = {});
          let rr = Array.isArray(en[name]) ? en[name].slice() : [tp, tp];
          if (which === 'start') rr[0] = tp; else rr[1] = tp;
          en[name] = [Math.min(rr[0], rr[1]), Math.max(rr[0], rr[1])];
        }
        updateRangeBar(); updateRangeLive(); renderGridBadges();
      };
      const up = () => {
        document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up);
        const after = state.payload; state.payload = snap;   // commit as one undo step
        mutate(() => { state.payload = after; }, { only:['panel','badges'] });
      };
      document.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
    });
  }
}

// ------------------------------------------------------------------ keyboard
function wireKeys(){
  document.addEventListener('keydown', e => {
    if (e.target.matches('input, select, textarea')){ if (e.key === 'Escape') e.target.blur(); return; }
    if ($('#help').hidden === false && e.key === 'Escape'){ $('#help').hidden = true; return; }
    if (!state.plateDir) return;                        // no plate loaded yet → ignore shortcuts
    const k = e.key;
    if (k >= '1' && k <= '9'){ assignNth(+k - 1); e.preventDefault(); return; }
    switch (k){
      case 'Tab': cycleActive(e.shiftKey ? -1 : 1); e.preventDefault(); break;
      case ' ': togglePlay(); e.preventDefault(); break;
      case '[': stopPlay(); setFrame(state.frameIdx - 1); break;
      case ']': stopPlay(); setFrame(state.frameIdx + 1); break;
      case ',': stepZ(-1); break;                 // browse z-slices of the selected channel
      case '.': stepZ(1); break;
      case 'c': { const c = state.manifest.channels; const i = c.indexOf(state.channel);
        state.channel = c[(i+1)%c.length]; clampFrame(); renderDetail(); break; }
      case 'ArrowLeft':  arrowNav(-1, false); e.preventDefault(); break;
      case 'ArrowRight': arrowNav(1, false);  e.preventDefault(); break;
      case 'ArrowUp':    arrowNav(-1, true);  e.preventDefault(); break;
      case 'ArrowDown':  arrowNav(1, true);   e.preventDefault(); break;
      case 'Backspace': if (state.scope==='well' && state.activeCol) doAssign('well', state.activeCol, null); e.preventDefault(); break;
      case 'z': if (e.metaKey||e.ctrlKey){ e.shiftKey?redo():undo(); } else undo(); break;
      case 'Z': redo(); break;
      case 's': if (e.metaKey||e.ctrlKey) e.preventDefault(); saveNow(); break;
    }
  });
}
function assignNth(i){
  if (state.scope === 'well' && state.activeCol){ const c = state.payload.columns[state.activeCol];
    if (c && (c.type==='categorical'||c.type==='binary') && c.values[i] != null) doAssign('well', state.activeCol, c.values[i]); }
}
function cycleActive(step){
  const names = Object.keys(state.payload.columns); if (!names.length) return;
  let i = names.indexOf(state.activeCol); i = ((i + step) % names.length + names.length) % names.length;
  state.activeCol = names[i]; renderPanel(); renderGridBadges();
}
// Arrow keys act on WHATEVER you last clicked: a well thumbnail → move between wells
// (through the filtered selection when a filter is active); a fader → nudge that fader.
function arrowNav(dir, big){
  switch (state.arrowMode){
    case 'z':        arrowZ(dir); break;
    case 'rotation': arrowRot(dir, big); break;
    case 'frame':    stopPlay(); setFrame(state.frameIdx + dir * (big ? 10 : 1)); break;
    default:         movePrimary(big ? dir * 12 : dir);   // 'wells'
  }
}
function arrowZ(dir){
  stepZ(dir);                                             // move the z view (look)
  if (state.zrec){ const z = state.zview; if (z != null) commitSlice(z); }
  updateFaders();
}
function arrowRot(dir, big){
  const step = big ? 10 : 1;
  let deg = state.rotview != null ? state.rotview
          : Math.round(imgInterpolate(state.primary, 'rotation', curTp()) || 0);
  deg = ((deg + dir * step) % 360 + 360) % 360;
  state.rotview = deg; updateBigImg(); updateRotslider();
  if (state.rotrec) commitRotation(deg);
}
function movePrimary(delta){
  if (state.filter.active && state.filter.results.length) return moveFilterPrimary(delta);
  const wells = state.manifest.wells; let i = wells.indexOf(state.primary);
  if (i < 0) i = 0; i = Math.max(0, Math.min(wells.length - 1, i + delta));
  const w = wells[i]; // page-follow
  const pp = state.perPage; const pg = Math.floor(i / pp);
  if (pg !== state.page){ state.page = pg; renderGrid(); }
  state.sel = new Set([w]); setPrimary(w, false); renderGridBadges(); renderPanel();
}
// Navigate the FILTERED selection (may cross plates) instead of the whole plate.
function moveFilterPrimary(delta){
  const res = state.filter.results;
  let i = res.findIndex(r => r.plate === state.plateDir && r.well === state.primary);
  if (i < 0) i = 0;
  i = Math.max(0, Math.min(res.length - 1, i + (delta > 0 ? 1 : -1) * Math.max(1, Math.abs(delta) >= 12 ? 12 : 1)));
  const r = res[i]; if (r) loadFilterWell(r.plate, r.well);
}

// ------------------------------------------------------------------ AnnotatorAPI (plugin surface)
// The rotation / measurement tools are drop-in plugins that talk to the app only
// through window.AnnotatorAPI. Everything below builds that surface, routes #stage
// pointer input to the ONE active tool column, and lets tools redraw each frame.

// smoothstep-interpolated NUMBER for an image column between its keyframes at tp.
// Mirrors hyperstack_video/focus_cut.py::build_focus_track(ease='smoothstep'):
// hold before first / after last; between (t0,v0),(t1,v1): u=(tp-t0)/(t1-t0);
// w=u*u*(3-2u); v0+(v1-v0)*w. Stored values are Number()-coerced (JSON may stringify).
function imgInterpolate(well, col, tp){
  const kfObj = imgKeyframes(well, col);                     // {tpInt: value}
  const kf = Object.keys(kfObj)
    .map(k => [Number(k), Number(kfObj[k])])
    .filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]))
    .sort((a, b) => a[0] - b[0]);
  const n = kf.length;
  if (!n) return 0;
  const t = Number(tp);
  if (!Number.isFinite(t)) return kf[0][1];
  if (t <= kf[0][0]) return kf[0][1];
  if (t >= kf[n - 1][0]) return kf[n - 1][1];
  let i = 0; while (i < n - 1 && kf[i + 1][0] <= t) i++;
  const t0 = kf[i][0], v0 = kf[i][1], t1 = kf[i + 1][0], v1 = kf[i + 1][1];
  if (t1 === t0) return v0;
  const u = (t - t0) / (t1 - t0), w = u * u * (3 - 2 * u);
  // angle columns take the SHORTEST route around the circle (0↔360 = no move)
  if ((((state.payload || {}).image_columns || {})[col] || {}).type === 'angle'){
    const d = ((v1 - v0 + 540) % 360) - 180;      // shortest signed delta in (-180,180]
    return ((v0 + d * w) % 360 + 360) % 360;       // normalise to [0,360)
  }
  return v0 + (v1 - v0) * w;
}
// API keyframe reader — sorted PAIR form [[tp,value],…] (the contract shape;
// the internal imgKeyframes returns a {tp:value} map, so adapt it here).
function apiImgKeyframes(well, col){
  const kf = imgKeyframes(well, col);
  return Object.keys(kf).map(Number).sort((a, b) => a - b).map(t => [t, kf[t]]);
}
// nm/px for the plate (measurement → µm), or null. Prefer the plate annotation
// acq_px_size_nm; else the loaded manifest's autofill (forward-compat).
// The camera acquires 2x2-BINNED (1024 from a 2048 sensor), so the true image pixel is 2x the
// unbinned acq_px_size_nm / objective value. PX_BINNING is applied to those auto sources — but NOT
// to a user-set um_per_px, which is taken as the final µm/px. Fixed 2026-07-13 (was halving sizes).
const PX_BINNING = 2;
function pxSizeNm(){
  const pa = (state.payload && state.payload.plate_annotations) || {};
  const upp = Number(pa['um_per_px']);              // user-set µm/px in the Plate tab — wins (true µm/px)
  if (Number.isFinite(upp) && upp > 0) return upp * 1000;    // µm/px → nm/px
  let v = Number(pa['acq_px_size_nm']);
  if (Number.isFinite(v) && v > 0) return v * PX_BINNING;    // unbinned objective px → real 2x2-binned px
  const af = (state.manifest && state.manifest.autofill) || {};
  v = Number(af['acq_px_size_nm'] != null ? af['acq_px_size_nm'] : af['px_size_nm']);
  return (Number.isFinite(v) && v > 0) ? v * PX_BINNING : null;
}

// ---- tool router: map #stage pointer coords → SOURCE-image px, route to the active tool ----
// client (x,y) → source-image px: #bigImg's box (getBoundingClientRect) + the
// object-fit:contain letterbox of the natural image within that box. Clamped.
function mapClientToSource(clientX, clientY){
  const img = document.getElementById('bigImg');
  if (!img || typeof img.getBoundingClientRect !== 'function') return { x: 0, y: 0 };
  const box = img.getBoundingClientRect();
  const natW = img.naturalWidth || 0, natH = img.naturalHeight || 0;
  const boxW = box.width, boxH = box.height;
  if (!(natW > 0 && natH > 0 && boxW > 0 && boxH > 0)) return { x: 0, y: 0 };
  const scale = Math.min(boxW / natW, boxH / natH);
  const offX = (boxW - natW * scale) / 2, offY = (boxH - natH * scale) / 2;
  let x = (clientX - box.left - offX) / scale, y = (clientY - box.top - offY) / scale;
  x = Math.max(0, Math.min(natW, x)); y = Math.max(0, Math.min(natH, y));
  return { x, y };
}
function activeToolHandlers(){
  if (activeToolCol == null) return null;
  const col = (state.payload.image_columns || {})[activeToolCol];
  return col ? toolHandlers[col.type] : null;
}
// Make `name` the active tool column (deactivating any previous one).
function activateImageTool(name){
  const col = (state.payload.image_columns || {})[name];
  const h = col ? toolHandlers[col.type] : null;
  if (!h){ setStatus('no tool for this column', 'err'); return; }
  if (activeToolCol === name) return;
  deactivateImageTool();
  activeToolCol = name;
  const stage = document.getElementById('stage');
  if (stage && stage.classList){ stage.classList.add('tool-active'); stage.classList.add('tool-' + col.type); }
  try { if (typeof h.onActivate === 'function') h.onActivate(name); } catch (e){}
  if (state.scope === 'image') renderPanel();
}
function deactivateImageTool(){
  if (activeToolCol == null) return;
  const col = (state.payload.image_columns || {})[activeToolCol];
  const h = col ? toolHandlers[col.type] : null;
  const stage = document.getElementById('stage');
  if (stage && stage.classList){ stage.classList.remove('tool-active'); if (col) stage.classList.remove('tool-' + col.type); }
  try { if (h && typeof h.onDeactivate === 'function') h.onDeactivate(); } catch (e){}
  activeToolCol = null;
}
// ONE set of listeners on #stage: while a tool is active, map to source px and
// forward. (Plugins also self-attach their own listeners for drags that leave
// the stage; they de-dup / are idempotent, so double delivery is safe.)
function wireStageTools(){
  const stage = document.getElementById('stage');
  if (!stage || typeof stage.addEventListener !== 'function' || stage._toolsWired) return;
  stage._toolsWired = true;
  const forward = method => ev => {
    const h = activeToolHandlers(); if (!h) return;
    const pt = mapClientToSource(ev.clientX, ev.clientY);
    if (typeof h[method] === 'function'){ try { h[method](ev, pt); } catch (e){} }
  };
  stage.addEventListener('mousedown', forward('onImageMouseDown'));
  stage.addEventListener('mousemove', forward('onImageMouseMove'));
  stage.addEventListener('mouseup', forward('onImageMouseUp'));
}

// Build the public API surface and hand it to any waiting plugins.
const AnnotatorAPI = {
  get state(){ return state; },                                   // the live app state obj
  curTp,                                                          // current timepoint (int) or null
  curWell(){ return state.primary; },
  get stageEl(){ return document.getElementById('stage'); },
  get imgEl(){ return document.getElementById('bigImg'); },
  mutate,                                                         // snapshot+render+autosave writer
  doAssign,                                                       // (scope,name,value) — image scope writes per-frame
  imgKeyframes: apiImgKeyframes,                                  // (well,col) -> sorted [[tp,value],…]
  setImageKeyframe,                                               // (col,value) at current (well,tp)
  imgValue(well, tp, col){
    const ww = (state.payload && state.payload.image_annotations) ? state.payload.image_annotations[well] : null;
    const e = ww && ww[String(tp)];
    return e ? e[col] : undefined;
  },
  imgInterpolate,                                                 // (well,col,tp) -> smoothstep number
  imgEffective,                                                   // (well,col,tp) -> {value,startTp} forward-filled (held)
  pxSizeNm,                                                       // -> nm/px number or null
  registerTool(columnType, handlers){ toolHandlers[columnType] = handlers || {}; },
  onColumnPanel(columnType, renderFn){ toolPanels[columnType] = renderFn; },
  // convenience for the host UI / tests
  activateImageTool, deactivateImageTool, get activeToolCol(){ return activeToolCol; },
  arrowNav, movePrimary, renderGridBadges, exportWells,          // navigation + export (tested)
};
window.AnnotatorAPI = AnnotatorAPI;
wireStageTools();
// Announce readiness (plugins also poll + auto-install on load, so order is moot).
try {
  if (typeof Event === 'function'){
    document.dispatchEvent(new Event('annotator:ready'));
    window.dispatchEvent(new Event('annotator:ready'));
    window.dispatchEvent(new Event('annotator-api-ready'));
  }
} catch (e){}
// Defensive explicit install if a plugin was already parsed before us.
try { if (window.RotTool && window.RotTool.install) window.RotTool.install(AnnotatorAPI); } catch (e){}
try { if (window.MeasureTool && window.MeasureTool.install) window.MeasureTool.install(AnnotatorAPI); } catch (e){}

// small debug hook (harmless) — lets tests drive the keyframe logic directly
window._dbg = { state, curTp, setFrame, setImageKeyframe, imgEffective, imgKeyframes,
  AnnotatorAPI, activateImageTool, deactivateImageTool, imgInterpolate, pxSizeNm, renderTools,
  filterMatches, annPass, measPass, fcmp, computeFilter,        // filter predicates
  collectRender, openExport, renderPlaneRows, updateRenderNote, // render spec builders
  get toolHandlers(){ return toolHandlers; }, get toolPanels(){ return toolPanels; },
  get activeToolCol(){ return activeToolCol; } };

boot().catch(e => { document.body.innerHTML = '<pre style="padding:20px;color:#f0616d">Failed to start: ' + e.message + '</pre>'; });
