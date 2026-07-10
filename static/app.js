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
  sel: new Set(), primary: null,
  channel: 'BF', frameIdx: 0, playing: false, playTimer: null,
  scope: 'well', activeCol: null, imageMode: false,
  gridChannel: 'BF', gridFrac: 0.5, page: 0, perPage: 96,
  undo: [], redo: [], dirty: false, saveTimer: null,
  cells: new Map(), rangeDrag: null,
  filter: { active: false, constraints: [], data: null, results: [] },
};

// column helpers per scope
const colsKey = { plate:'plate_columns', well:'columns', image:'image_columns' };
const annKey  = { plate:'plate_annotations', well:'annotations', image:'image_annotations' };

// ------------------------------------------------------------------ boot
async function boot(){
  wireStatic();
  state.cfg = await jget('/api/config');
  const sel = $('#plateSelect');
  sel.innerHTML = '';
  state.cfg.plates.forEach(p => {
    const o = elt('option'); o.value = p.dir;
    o.textContent = p.dir + (p.annotated ? '  ✓' : '');
    sel.appendChild(o);
  });
  const qp = new URLSearchParams(location.search).get('plate');
  let target = qp || (state.cfg.plates[0] && state.cfg.plates[0].dir);
  if (qp) { // resolve prefix against the list
    const hit = state.cfg.plates.find(p => p.dir === qp) || state.cfg.plates.find(p => p.dir.startsWith(qp));
    if (hit) target = hit.dir;
  }
  if (target) { sel.value = target; await loadPlate(target); }
  else $('#panelBody').innerHTML = '<p class="muted">No plate folders found under the data root.</p>';
}

async function loadPlate(dir){
  stopPlay();
  const man = await jget('/api/plate?dir=' + encodeURIComponent(dir));
  state.manifest = man;
  state.plateDir = man.plate;
  state.payload = man.payload;
  seedScopes();
  // pick channel + primary
  state.channel = man.channels.includes('BF') ? 'BF' : man.channels[0];
  state.gridChannel = state.channel;
  state.page = 0;
  state.primary = man.wells[0] || null;
  state.sel = new Set(state.primary ? [state.primary] : []);
  state.activeCol = Object.keys(state.payload.columns)[0] || null;
  state.imageMode = false;
  state.undo = []; state.redo = []; state.dirty = false;
  $('#annotator').value = state.payload.annotator || '';
  buildChannelButtons();
  buildGridChannelOptions();
  renderAll();
  setStatus('loaded ' + man.plate, '');
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
function frameURL(well, ch, tp, size, z){
  let u = `/api/frame?dir=${encodeURIComponent(state.plateDir)}&well=${encodeURIComponent(well)}&ch=${ch}&tp=${tp}&size=${size}`;
  if (ch === 'BF' && z != null) u += `&z=${z}`;
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
  if (!state.filter.constraints.length){
    const cols = Object.keys(state.filter.data.columns);
    state.filter.constraints = [{ col: cols[0] || '', val: '*' }];
  }
  renderFilterRows();
  $('#filterModal').hidden = false;
}
function renderFilterRows(){
  const wrap = $('#filterRows'); wrap.innerHTML = '';
  const cols = state.filter.data.columns;
  state.filter.constraints.forEach((c, i) => {
    const row = elt('div', 'frow');
    const cs = elt('select');
    for (const name of Object.keys(cols)){ const o = elt('option'); o.value = name; o.textContent = name; cs.appendChild(o); }
    cs.value = c.col;
    const vs = elt('select');
    const fill = () => { vs.innerHTML = ''; const any = elt('option'); any.value = '*'; any.textContent = '(any value)'; vs.appendChild(any);
      for (const v of (cols[cs.value]?.values || [])){ const o = elt('option'); o.value = v; o.textContent = v; vs.appendChild(o); }
      vs.value = c.val || '*'; };
    fill();
    cs.onchange = () => { c.col = cs.value; c.val = '*'; fill(); updateFilterCount(); };
    vs.onchange = () => { c.val = vs.value; updateFilterCount(); };
    const del = elt('button', 'del', '✕');
    del.onclick = () => { state.filter.constraints.splice(i, 1);
      if (!state.filter.constraints.length) state.filter.constraints.push({ col: Object.keys(cols)[0], val: '*' });
      renderFilterRows(); };
    row.append(cs, elt('span','muted','='), vs, del);
    wrap.appendChild(row);
  });
  updateFilterCount();
}
function filterMatches(w){
  return state.filter.constraints.every(c => {
    if (!c.col) return true;
    const v = w.ann[c.col];
    return c.val === '*' ? v != null : String(v) === String(c.val);
  });
}
function computeFilter(){
  const res = (state.filter.data.wells || []).filter(filterMatches);
  res.sort((a, b) => b.nann - a.nann || a.short.localeCompare(b.short) || a.well.localeCompare(b.well));
  return res;
}
function updateFilterCount(){ $('#filterCount').textContent = computeFilter().length + ' wells match'; }
function applyFilter(){
  state.filter.results = computeFilter();
  state.filter.active = true;
  $('#filterModal').hidden = true; $('#clearFilterBtn').hidden = false;
  renderGrid();
  setStatus(`filtered: ${state.filter.results.length} wells across plates`, '');
}
function clearFilter(){ state.filter.active = false; $('#clearFilterBtn').hidden = true; renderGrid(); }
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
// filter-grid thumbnail timepoint: the frame-fader fraction mapped onto the
// well's own length (server clamps to the nearest real frame).
function filterTp(r){
  const n = r.n_tps || 120;
  return Math.max(1, Math.round(1 + state.gridFrac * (n - 1)));
}
function renderFilterGrid(){
  const g = $('#grid'); g.innerHTML = ''; state.cells.clear();
  g.style.gridTemplateColumns = 'repeat(12, 1fr)';
  const res = state.filter.results;
  $('#pageLabel').textContent = `${res.length} matched · sorted by #annotations`;
  $('#pagePrev').disabled = $('#pageNext').disabled = true;
  for (const r of res){
    const cell = elt('div', 'cell'); cell.dataset.well = r.well; cell.dataset.plate = r.plate;
    const img = elt('img'); img.loading = 'lazy'; img.draggable = false;
    img.src = frameURLd(r.plate, r.well, state.gridChannel, filterTp(r), 130);
    cell.appendChild(img);
    cell.appendChild(Object.assign(elt('span','plate-lab'), { textContent: `${r.short} ${r.well}` }));
    cell.appendChild(Object.assign(elt('span','nann'), { textContent: r.nann }));
    if (state.plateDir === r.plate && state.primary === r.well) cell.classList.add('primary');
    cell.onclick = () => loadFilterWell(r.plate, r.well);
    g.appendChild(cell);
    state.cells.set(r.plate + '|' + r.well, cell);
  }
  $('#selInfo').textContent = `${res.length} wells — click one to annotate it`;
}
async function loadFilterWell(plate, well){
  if (state.plateDir !== plate){
    if (state.dirty) await saveNow();
    await loadPlate(plate);                 // renderGrid stays in filter mode (active)
  }
  setPrimary(well, true);
  state.scope = 'image'; state.imageMode = true;
  renderPanel();
  for (const [, cell] of state.cells) cell.classList.remove('primary');
  const c = state.cells.get(plate + '|' + well); if (c) c.classList.add('primary');
}

function renderGridBadges(){
  if (state.filter.active) return;            // filter grid manages its own cells
  const col = state.activeCol ? state.payload.columns[state.activeCol] : null;
  for (const [w, cell] of state.cells){
    cell.classList.toggle('selected', state.sel.has(w));
    cell.classList.toggle('primary', state.primary === w);
    cell.classList.toggle('hasimg', !!(state.payload.image_annotations[w] &&
      Object.keys(state.payload.image_annotations[w]).length));
    const badge = cell.querySelector('.badge');
    const v = col ? (state.payload.annotations[w] || {})[state.activeCol] : undefined;
    if (v == null){ badge.textContent = ''; badge.style.background = 'transparent'; }
    else { badge.textContent = fmtVal(v); badge.style.background = valueColor(col, v); }
  }
}
function onCellClick(w, e){
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
      const hit = new Set(add ? state.sel : []);
      for (const [w, cell] of state.cells){
        const r = cell.getBoundingClientRect();
        if (r.right >= x0 && r.left <= x1 && r.bottom >= y0 && r.top <= y1) hit.add(w);
      }
      state.sel = hit;
      renderGridBadges();
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      rubber.hidden = true;
      if (state.rubberDidDrag){ if (state.sel.size) setPrimary([...state.sel][0], false); renderPanel(); }
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
}

// ---- detail / scrubber ---------------------------------------------------
function curTps(){ return (state.manifest.frames[state.primary] || {})[state.channel] || []; }
function clampFrame(){ const n = curTps().length; if (!n){ state.frameIdx = 0; return; }
  state.frameIdx = Math.max(0, Math.min(n - 1, state.frameIdx)); }
function curTp(){ const t = curTps(); return t.length ? t[state.frameIdx] : null; }
function renderDetail(){
  $('#detailWell').textContent = state.primary || '—';
  $$('.chBtn').forEach(b => b.classList.toggle('active', b.dataset.ch === state.channel));
  const tps = curTps();
  const scrub = $('#scrub');
  scrub.max = Math.max(0, tps.length - 1); scrub.value = state.frameIdx;
  updateBigImg(); updateFrameInfo(); updateRangeBar();
}
function updateBigImg(){
  const tp = curTp();
  const img = $('#bigImg');
  if (tp == null){ img.removeAttribute('src'); return; }
  img.src = frameURL(state.primary, state.channel, tp, 600, sliceAt(tp));
  // prefetch neighbours for a smooth fader (each at its own annotated slice)
  const tps = curTps();
  [state.frameIdx - 1, state.frameIdx + 1, state.frameIdx + 2].forEach(i => {
    if (i >= 0 && i < tps.length){ const im = new Image(); im.src = frameURL(state.primary, state.channel, tps[i], 600, sliceAt(tps[i])); }
  });
}
function updateFrameInfo(){
  const tps = curTps(), tp = curTp();
  if (tp == null){ $('#frameInfo').textContent = 'no frames'; return; }
  const iv = Number(state.manifest.autofill.timepoint_interval_min) || 0;
  const mins = iv ? ` · +${((tp - 1) * iv)} min` : '';
  const z = state.channel === 'BF' ? sliceAt(tp) : null;
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
function stopPlay(){ state.playing = false; $('#playBtn').textContent = '▶'; clearInterval(state.playTimer); }

// range overlay on the scrubber (shows the active range column for the primary well)
function activeRangeCol(){
  // in well scope, the active column if it's a range; else the first range col
  if (state.scope === 'well' && state.activeCol){
    const c = state.payload.columns[state.activeCol]; if (c && c.type === 'range') return state.activeCol;
  }
  return Object.entries(state.payload.columns).find(([, c]) => c.type === 'range')?.[0] || null;
}
function updateRangeBar(){
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
  const ty = elt('select'); state.cfg.column_types.forEach(t => { const o = elt('option'); o.value = t; o.textContent = t; ty.appendChild(o); });
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
  if (col === 'slice') updateBigImg();          // the slice changes the displayed z-plane
  updateFrameInfo();                            // stage + slice both show in the readout
}

function renderImage(body){
  if (!state.primary){ body.appendChild(elt('div','muted','select a well to annotate its frames')); return; }
  const top = elt('div', 'section inline');
  const tog = elt('button', 'btn sm ' + (state.imageMode ? 'active' : ''), state.imageMode ? '● image-level ON' : '○ enable image-level');
  tog.onclick = () => { state.imageMode = !state.imageMode; renderPanel(); };
  top.append(elt('h4', null, `Image — ${state.primary}`), tog);
  body.appendChild(top);
  if (!state.imageMode){ body.appendChild(elt('div','muted','Turn it on, scrub the trajectory, and set a stage / slice — it holds until the next change you make.')); return; }
  const tp = curTp();
  body.appendChild(Object.assign(elt('div','section big'),
    { textContent: tp==null ? 'no frame' : `Frame tp ${tp}  (${state.frameIdx+1}/${curTps().length})` }));

  ensureImageCols();
  const cols = state.payload.image_columns;
  for (const [name, col] of Object.entries(cols)){
    if (col.fill === 'forward') body.appendChild(fillColumn(name, col, tp));
  }
  const pts = Object.entries(cols).filter(([, c]) => c.fill !== 'forward');
  if (pts.length){
    const sec = elt('div','section'); sec.appendChild(elt('h4', null, 'Other image columns (this frame only)'));
    for (const [name, col] of pts) sec.appendChild(colRow('image', name, col));
    body.appendChild(sec);
  }
  body.appendChild(addColumnForm('image'));
}
function ensureImageCols(){
  const d = (state.cfg.defaults.image || {}).columns || {};
  for (const key of ['iwamatsu_stage', 'slice']){
    if (state.payload.image_columns[key] || !d[key]) continue;
    let values = d[key].values ? [...d[key].values] : [];
    if (key === 'iwamatsu_stage' && !values.length)
      values = ((state.cfg.iwamatsu_stages.stages) || []).map(s => s.value);
    state.payload.image_columns[key] = { type:'categorical', values, fill:'forward' };
  }
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
  $('#annotator').onchange = e => mutate(() => { state.payload.annotator = e.target.value.trim();
    if (state.payload.plate_columns.annotator) state.payload.plate_annotations.annotator = e.target.value.trim() || undefined; }, { only:['badges'] });
  $('#undoBtn').onclick = undo; $('#redoBtn').onclick = redo;
  $('#saveBtn').onclick = saveNow;
  $('#helpBtn').onclick = () => $('#help').hidden = false;
  $('#helpClose').onclick = () => $('#help').hidden = true;
  $('#help').onclick = e => { if (e.target.id === 'help') $('#help').hidden = true; };
  $('#filterBtn').onclick = () => openFilter().catch(e => setStatus('filter: ' + e.message, 'err'));
  $('#clearFilterBtn').onclick = clearFilter;
  $('#filterClose').onclick = () => $('#filterModal').hidden = true;
  $('#filterModal').onclick = e => { if (e.target.id === 'filterModal') $('#filterModal').hidden = true; };
  $('#filterAddRow').onclick = () => { const cols = Object.keys(state.filter.data.columns);
    state.filter.constraints.push({ col: cols[0], val: '*' }); renderFilterRows(); };
  $('#filterApply').onclick = applyFilter;
  $('#filterExport').onclick = exportFilter;

  $$('.tab').forEach(t => t.onclick = () => { state.scope = t.dataset.scope; renderPanel(); updateRangeBar(); });

  $('#pagePrev').onclick = () => { if (state.page>0){ state.page--; renderGrid(); } };
  $('#pageNext').onclick = () => { state.page++; renderGrid(); };
  $('#perPage').onchange = e => { state.perPage = Number(e.target.value); state.page = 0; renderGrid(); };
  $('#gridChannel').onchange = e => { state.gridChannel = e.target.value; renderGrid(); };
  $('#gridFrac').onchange = e => { state.gridFrac = Number(e.target.value)/100; renderGrid(); };
  $('#blockInput').onkeydown = e => { if (e.key === 'Enter') applyBlock(e.target.value); };

  $$('.chBtn').forEach(b => b.onclick = () => { state.channel = b.dataset.ch; clampFrame(); renderDetail(); });
  $('#playBtn').onclick = togglePlay;
  $('#scrub').oninput = e => { stopPlay(); setFrame(Number(e.target.value)); };
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
    const k = e.key;
    if (k >= '1' && k <= '9'){ assignNth(+k - 1); e.preventDefault(); return; }
    switch (k){
      case 'Tab': cycleActive(e.shiftKey ? -1 : 1); e.preventDefault(); break;
      case ' ': togglePlay(); e.preventDefault(); break;
      case '[': stopPlay(); setFrame(state.frameIdx - 1); break;
      case ']': stopPlay(); setFrame(state.frameIdx + 1); break;
      case 'c': { const c = state.manifest.channels; const i = c.indexOf(state.channel);
        state.channel = c[(i+1)%c.length]; clampFrame(); renderDetail(); break; }
      case 'ArrowLeft': movePrimary(-1); e.preventDefault(); break;
      case 'ArrowRight': movePrimary(1); e.preventDefault(); break;
      case 'ArrowUp': movePrimary(-12); e.preventDefault(); break;
      case 'ArrowDown': movePrimary(12); e.preventDefault(); break;
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
function movePrimary(delta){
  const wells = state.manifest.wells; let i = wells.indexOf(state.primary);
  if (i < 0) i = 0; i = Math.max(0, Math.min(wells.length - 1, i + delta));
  const w = wells[i]; // page-follow
  const pp = state.perPage; const pg = Math.floor(i / pp);
  if (pg !== state.page){ state.page = pg; renderGrid(); }
  state.sel = new Set([w]); setPrimary(w, false); renderGridBadges(); renderPanel();
}

// small debug hook (harmless) — lets tests drive the keyframe logic directly
window._dbg = { state, curTp, setFrame, setImageKeyframe, imgEffective, imgKeyframes };

boot().catch(e => { document.body.innerHTML = '<pre style="padding:20px;color:#f0616d">Failed to start: ' + e.message + '</pre>'; });
