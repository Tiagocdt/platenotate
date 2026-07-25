/* rot_tool.js — ROTATION-keyframe tool for the medaka label-annotator.
 *
 * A self-contained, dependency-free, vanilla-JS PLUGIN. It edits NO existing
 * file: it registers itself against a host-provided `window.AnnotatorAPI`
 * (every call feature-detected, so a missing/absent method never throws) and
 * drives the image-tab "angle" column.
 *
 * DATA MODEL
 *   image_annotations[well]["<tp>"]["rotation"] = <degrees:float>   is a KEYFRAME.
 *   Between keyframes the displayed angle INTERPOLATES with the same smoothstep
 *   "fade" used by hyperstack_video/focus_cut.py::build_focus_track():
 *       u = (tp-t0)/(t1-t0);  w = u*u*(3-2u);  value = v0 + (v1-v0)*w
 *   Ends are held flat (before the first / after the last keyframe = constant).
 *   Column type = "angle", fill = "interpolate".
 *
 * SIGN CONVENTION — CLOCKWISE-POSITIVE.
 *   Sweeping the pointer clockwise around the image centre increases the angle.
 *   The screen y-axis points down, so `Math.atan2(dy,dx)` grows clockwise, and
 *   CSS `transform: rotate(+deg)` also turns clockwise — the on-screen turn, the
 *   stored number, and what any renderer must apply all share ONE sign. Stored
 *   keyframe values are normalised to the half-open range (-180, 180].
 *
 * TESTABILITY
 *   The pure core (`interpolate`, `normalizeDeg`) is exported via `module.exports`
 *   for the Node unit test; all browser wiring is guarded by `typeof window`, so
 *   requiring this file in Node runs no DOM code.
 */
'use strict';

// ============================================================ pure core (tested)

// Normalise degrees into the half-open range (-180, 180].
function normalizeDeg(deg) {
  const n = Number(deg);
  if (!Number.isFinite(n)) return 0;
  let d = n % 360;            // (-360, 360)
  if (d > 180) d -= 360;      // fold the top half down
  if (d <= -180) d += 360;    // fold the bottom half up
  return d === 0 ? 0 : d;     // squash -0 to 0
}

/* Smoothstep interpolation over sparse keyframes — a faithful mirror of
 * focus_cut.build_focus_track(ease='smoothstep'). `keyframes` is the
 * [[tp,value], ...] form returned by API.imgKeyframes (any order accepted;
 * values may arrive as strings after a JSON round-trip, so they are coerced).
 *   - empty / non-array  -> 0
 *   - single keyframe    -> that value everywhere (constant)
 *   - before first / after last -> held flat at the end value
 *   - between two        -> v0 + (v1-v0) * smoothstep(u)
 */
function interpolate(keyframes, tp) {
  if (!Array.isArray(keyframes) || keyframes.length === 0) return 0;
  const kf = keyframes
    .map((p) => [Number(p[0]), Number(p[1])])
    .filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]))
    .sort((a, b) => a[0] - b[0]);
  const n = kf.length;
  if (n === 0) return 0;
  const t = Number(tp);
  if (!Number.isFinite(t)) return kf[0][1];
  if (t <= kf[0][0]) return kf[0][1];
  if (t >= kf[n - 1][0]) return kf[n - 1][1];
  let i = 0;
  while (i < n - 1 && kf[i + 1][0] <= t) i++;       // bracketing pair kf[i], kf[i+1]
  const t0 = kf[i][0], v0 = kf[i][1], t1 = kf[i + 1][0], v1 = kf[i + 1][1];
  if (t1 === t0) return v0;
  const u = (t - t0) / (t1 - t0);
  const w = u * u * (3 - 2 * u);                     // smoothstep
  // rotation is angular: take the SHORTEST route around the circle (0↔360 = no move)
  const d = ((v1 - v0 + 540) % 360) - 180;           // shortest signed delta in (-180,180]
  return ((v0 + d * w) % 360 + 360) % 360;           // normalise to [0,360)
}

// ============================================================ browser glue

function initBrowser(win, doc) {
  const TYPE = 'angle';            // registered image-column TYPE
  const COL_DEFAULT = 'rotation';  // conventional column NAME (overridden by onActivate)
  const RAD = 180 / Math.PI;

  const S = {                      // module-private state
    col: COL_DEFAULT,             // active column name
    active: false,                // is the angle column the active image column?
    drag: null,                   // active rotate gesture, or null
    ui: null,                     // refs into the last-rendered panel
    installed: false,
  };

  const getAPI = () => win.AnnotatorAPI || null;
  const colName = () => S.col || COL_DEFAULT;

  // ---- current well / timepoint (feature-detected) ----
  function curWell() {
    const A = getAPI(); if (!A) return null;
    if (typeof A.curWell === 'function') { try { const w = A.curWell(); if (w != null) return w; } catch (e) {} }
    return A.state ? A.state.primary : null;
  }
  function curTp() {
    const A = getAPI(); if (!A || typeof A.curTp !== 'function') return null;
    try { return A.curTp(); } catch (e) { return null; }
  }

  // ---- keyframe reads (prefer the API, fall back to raw payload) ----
  function keyframesFor(well, col) {
    const A = getAPI(); if (!A) return [];
    if (typeof A.imgKeyframes === 'function') {
      try { const k = A.imgKeyframes(well, col); if (Array.isArray(k)) return k; } catch (e) {}
    }
    const pay = A.state && A.state.payload;
    const ww = pay && pay.image_annotations && pay.image_annotations[well];
    if (!ww) return [];
    const out = [];
    for (const tp in ww) {
      const v = ww[tp] && ww[tp][col];
      if (v == null || v === '') continue;
      const nv = Number(v), nt = Number(tp);
      if (Number.isFinite(nv) && Number.isFinite(nt)) out.push([nt, nv]);
    }
    return out.sort((a, b) => a[0] - b[0]);
  }
  // Interpolated angle at (well, tp): host imgInterpolate first, else our pure fn.
  function angleAt(well, tp) {
    if (well == null || tp == null) return 0;
    const A = getAPI();
    if (A && typeof A.imgInterpolate === 'function') {
      try { const v = Number(A.imgInterpolate(well, colName(), tp)); if (Number.isFinite(v)) return v; } catch (e) {}
    }
    return interpolate(keyframesFor(well, colName()), tp);
  }

  // ---- CSS transform on #bigImg ----
  function applyTransform(deg) {
    const A = getAPI(); const img = A && A.imgEl;
    if (img && img.style) img.style.transform = 'rotate(' + (Number(deg) || 0) + 'deg)';
  }
  function clearTransform() {
    const A = getAPI(); const img = A && A.imgEl;
    if (img && img.style) img.style.transform = '';
  }

  // ---- geometry ----
  function centerOf(el) {
    if (!el || typeof el.getBoundingClientRect !== 'function') return { x: 0, y: 0 };
    const r = el.getBoundingClientRect();          // stable under centre-rotation
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  // screen-space pointer angle in degrees about c (y-down => clockwise-positive)
  function pointerDeg(ev, c) {
    return Math.atan2(ev.clientY - c.y, ev.clientX - c.x) * RAD;
  }

  // ---- shared rotate gesture (image drag AND panel dial) ----
  function startRotate(ev, centerFn) {
    if (S.drag) return;                                     // one gesture at a time
    if (!ev || (ev.button != null && ev.button !== 0)) return;
    const A = getAPI(); if (!A) return;
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
    const base = angleAt(curWell(), curTp());
    const onMove = (e) => moveRotate(e);
    const onUp = (e) => endRotate(e);
    S.drag = { base, lastP: pointerDeg(ev, centerFn()), accum: 0, live: base, centerFn, moved: false, onMove, onUp };
    doc.addEventListener('mousemove', onMove, true);
    doc.addEventListener('mouseup', onUp, true);
    applyTransform(base);
  }
  function moveRotate(ev) {
    const d = S.drag; if (!d || !ev) return;
    if (typeof ev.preventDefault === 'function') ev.preventDefault();   // suppress text-select while turning
    const p = pointerDeg(ev, d.centerFn());
    const delta = ((p - d.lastP + 540) % 360) - 180;        // unwrap increment to (-180,180]
    if (delta !== 0) d.moved = true;
    d.accum += delta;                                       // NOT normalised mid-drag -> smooth preview
    d.lastP = p;
    d.live = d.base + d.accum;
    applyTransform(d.live);
    updatePanelLive(d.live);
  }
  function endRotate(ev) {
    const d = S.drag; if (!d) return;
    cancelDrag();
    if (d.moved) commitAngle(normalizeDeg(d.live));         // normalise only the stored value
    else applyTransform(angleAt(curWell(), curTp()));       // click without a turn: restore
  }
  function cancelDrag() {
    const d = S.drag; if (!d) return;
    S.drag = null;
    doc.removeEventListener('mousemove', d.onMove, true);
    doc.removeEventListener('mouseup', d.onUp, true);
  }

  // ---- writes ----
  // Set a keyframe at the current (well, tp). Uses the host's setImageKeyframe
  // (as specified) and falls back to a direct mutate() write if it is absent.
  function commitAngle(deg) {
    const A = getAPI(); if (!A) return;
    const value = normalizeDeg(deg);
    if (typeof A.setImageKeyframe === 'function') {
      try { A.setImageKeyframe(colName(), value); return; } catch (e) {}
    }
    if (typeof A.mutate === 'function') {
      const well = curWell(), tp = curTp();
      if (well == null || tp == null) return;
      A.mutate(() => {
        const ia = A.state.payload.image_annotations || (A.state.payload.image_annotations = {});
        const ww = ia[well] || (ia[well] = {});
        (ww[String(tp)] || (ww[String(tp)] = {}))[colName()] = value;
      });
    }
  }
  function deleteKeyframe(tp) {
    const A = getAPI(); if (!A || typeof A.mutate !== 'function') return;
    const well = curWell(), col = colName(), key = String(tp);
    A.mutate(() => {
      const ia = A.state.payload.image_annotations; const ww = ia && ia[well]; if (!ww) return;
      if (ww[key]) { delete ww[key][col]; if (!Object.keys(ww[key]).length) delete ww[key]; }
      if (!Object.keys(ww).length) delete ia[well];
    });
  }
  function clearWell() {
    const A = getAPI(); if (!A || typeof A.mutate !== 'function') return;
    const well = curWell(), col = colName();
    A.mutate(() => {
      const ia = A.state.payload.image_annotations; const ww = ia && ia[well]; if (!ww) return;
      for (const key of Object.keys(ww)) { delete ww[key][col]; if (!Object.keys(ww[key]).length) delete ww[key]; }
      if (!Object.keys(ww).length) delete ia[well];
    });
  }
  function nudge(delta) { commitAngle(angleAt(curWell(), curTp()) + delta); }

  // ---- jump to a timepoint (best effort; documented API method preferred) ----
  function gotoTp(tp) {
    const A = getAPI();
    for (const m of ['gotoTp', 'setTp', 'jumpToTp', 'setFrameByTp']) {
      if (A && typeof A[m] === 'function') { try { A[m](tp); return; } catch (e) {} }
    }
    // graceful fallback: the app's debug hook indexes by FRAME position, so map tp->index
    const dbg = win._dbg;
    if (dbg && typeof dbg.setFrame === 'function' && dbg.state && dbg.state.manifest) {
      try {
        const st = dbg.state, frames = (st.manifest.frames || {})[curWell()] || {};
        const list = frames[st.channel] || frames.BF || Object.values(frames)[0] || [];
        const i = list.indexOf(Number(tp));
        if (i >= 0) dbg.setFrame(i);
      } catch (e) {}
    }
  }

  // ============================================================ panel UI
  function renderPanel(container, col) {
    if (!container) return;
    if (col) S.col = col;
    ensureCss();
    container.innerHTML = '';
    const well = curWell(), tp = curTp();
    const cur = angleAt(well, tp);

    const sec = mk('div', 'section');
    sec.appendChild(mk('h4', null, 'Rotation (keyframe · interpolated)'));

    // dial + numeric field + fine buttons
    const row = mk('div', 'inline rot-row');
    const dial = buildDial(cur);
    row.appendChild(dial.el);
    const num = mk('input', 'rot-num'); num.type = 'number'; num.step = '0.1';
    num.value = fmt(cur); num.title = 'degrees — clockwise positive';
    num.addEventListener('change', () => { const v = parseFloat(num.value); if (Number.isFinite(v)) commitAngle(v); });
    num.addEventListener('keydown', (e) => { if (e.key === 'Enter') num.blur(); });
    row.append(num, mk('span', 'muted', '°'));
    const dec = mk('button', 'btn sm', '−1'); dec.title = 'Shift = −0.1°';
    dec.addEventListener('click', (e) => nudge(e.shiftKey ? -0.1 : -1));
    const inc = mk('button', 'btn sm', '+1'); inc.title = 'Shift = +0.1°';
    inc.addEventListener('click', (e) => nudge(e.shiftKey ? 0.1 : 1));
    row.append(dec, inc);
    sec.appendChild(row);

    // status: keyframe here vs interpolated
    const here = keyframesFor(well, colName()).some((p) => p[0] === Number(tp));
    sec.appendChild(mk('div', 'hint rot-status',
      tp == null ? 'no frame'
      : here ? 'keyframe on this frame (tp ' + tp + ') — drag the image or dial to adjust, or ✕ in the list to remove'
             : 'interpolated here — drag the image or dial (or edit the field) to drop a keyframe at tp ' + tp));

    sec.appendChild(mk('div', 'hint',
      'Drag on the image to rotate about its centre. Clockwise is positive. The angle fades (smoothstep) '
      + 'between keyframes; before the first / after the last it is held flat.'));

    sec.appendChild(buildList(well));

    const clr = mk('div', 'inline'); clr.style.marginTop = '6px';
    const cb = mk('button', 'btn sm ghost', 'clear rotation for this well');
    cb.addEventListener('click', () => { if (win.confirm('Remove ALL rotation keyframes for ' + well + '?')) clearWell(); });
    clr.appendChild(cb);
    sec.appendChild(clr);

    container.appendChild(sec);
    S.ui = { num: num, needle: dial.needle };
  }

  function buildList(well) {
    const wrap = mk('div', 'stamped');
    const kfs = keyframesFor(well, colName());
    if (!kfs.length) { wrap.appendChild(mk('span', 'muted', 'no keyframes yet — drag the image to add one')); return wrap; }
    wrap.appendChild(mk('span', 'muted', 'keyframes:'));
    for (const pair of kfs) {
      const t = pair[0], v = pair[1];
      const chip = mk('span', 'stamp', 'tp ' + t + ': ' + fmt(v) + '°');
      chip.title = 'jump to this frame';
      chip.addEventListener('click', () => gotoTp(t));
      const x = mk('span', 'x', '✕'); x.title = 'delete keyframe';
      x.addEventListener('click', (e) => { e.stopPropagation(); deleteKeyframe(t); });
      chip.appendChild(x);
      wrap.appendChild(chip);
    }
    return wrap;
  }

  // Small SVG dial. Needle: 0deg points up (12 o'clock), clockwise-positive.
  function buildDial(deg) {
    const NS = 'http://www.w3.org/2000/svg';
    const size = 58, cx = size / 2, cy = size / 2, R = size / 2 - 6;
    const svg = doc.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'rot-dial');
    svg.setAttribute('width', size); svg.setAttribute('height', size);
    svg.setAttribute('viewBox', '0 0 ' + size + ' ' + size);
    const ring = doc.createElementNS(NS, 'circle');
    ring.setAttribute('cx', cx); ring.setAttribute('cy', cy); ring.setAttribute('r', R);
    ring.setAttribute('class', 'rot-dial-ring');
    const tick0 = doc.createElementNS(NS, 'line');            // 12 o'clock reference
    tick0.setAttribute('x1', cx); tick0.setAttribute('y1', cy - R);
    tick0.setAttribute('x2', cx); tick0.setAttribute('y2', cy - R + 5);
    tick0.setAttribute('class', 'rot-dial-tick');
    const needle = doc.createElementNS(NS, 'line');
    needle.setAttribute('class', 'rot-dial-needle');
    const hub = doc.createElementNS(NS, 'circle');
    hub.setAttribute('cx', cx); hub.setAttribute('cy', cy); hub.setAttribute('r', 2.5);
    hub.setAttribute('class', 'rot-dial-hub');
    svg.append(ring, tick0, needle, hub);
    svg._geo = { cx: cx, cy: cy, R: R };
    setNeedle(needle, cx, cy, R, deg);
    svg.addEventListener('mousedown', (ev) => startRotate(ev, () => centerOf(svg)));
    return { el: svg, needle: needle };
  }
  function setNeedle(needle, cx, cy, R, deg) {
    const th = (Number(deg) || 0) / RAD;                      // 0 = up, clockwise-positive
    needle.setAttribute('x1', cx); needle.setAttribute('y1', cy);
    needle.setAttribute('x2', cx + R * Math.sin(th));
    needle.setAttribute('y2', cy - R * Math.cos(th));
  }
  // live-update the field + dial while dragging (panel is not re-rendered mid-drag)
  function updatePanelLive(deg) {
    const ui = S.ui; if (!ui) return;
    if (ui.num && ui.num.isConnected && doc.activeElement !== ui.num) ui.num.value = fmt(deg);
    if (ui.needle && ui.needle.isConnected) {
      const svg = ui.needle.ownerSVGElement, g = svg && svg._geo;
      if (g) setNeedle(ui.needle, g.cx, g.cy, g.R, deg);
    }
  }

  function mk(tag, cls, txt) { const e = doc.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
  function fmt(v) { const n = Number(v) || 0; return (Math.round(n * 10) / 10).toFixed(1); }

  function ensureCss() {
    if (doc.getElementById('rot-tool-css')) return;
    const css =
      '.rot-row{gap:8px;align-items:center}'
      + '.rot-num{width:74px;font-family:var(--mono,ui-monospace,monospace);text-align:right}'
      + '.rot-dial{cursor:grab;touch-action:none;flex:0 0 auto;user-select:none}'
      + '.rot-dial:active{cursor:grabbing}'
      + '.rot-dial-ring{fill:var(--panel2,#221f1a);stroke:var(--line,#332e27);stroke-width:1.5}'
      + '.rot-dial-tick{stroke:var(--muted,#9a9086);stroke-width:1.5}'
      + '.rot-dial-needle{stroke:var(--accent,#e8a33d);stroke-width:2;stroke-linecap:round}'
      + '.rot-dial-hub{fill:var(--accent,#e8a33d)}';
    const st = doc.createElement('style'); st.id = 'rot-tool-css'; st.textContent = css;
    (doc.head || doc.documentElement).appendChild(st);
  }

  // ============================================================ registration
  const handlers = {
    onActivate: function (colNm) {
      S.col = colNm || COL_DEFAULT; S.active = true;
      const A = getAPI(); const img = A && A.imgEl;
      if (img && img.style) { img.style.transformOrigin = 'center center'; img.style.cursor = 'grab'; }
      applyTransform(angleAt(curWell(), curTp()));
    },
    onDeactivate: function () {
      S.active = false; cancelDrag();
      const A = getAPI(); const img = A && A.imgEl;
      if (img && img.style) img.style.cursor = '';
      clearTransform();                       // required: reset transform on deactivate
      S.ui = null; S.col = COL_DEFAULT;
    },
    onImageMouseDown: function (ev, pt) {
      if (!S.active) return;
      const A = getAPI(); if (!A || !A.imgEl) return;
      startRotate(ev, () => centerOf(A.imgEl));
    },
    onImageMouseMove: function (ev, pt) { if (S.drag) moveRotate(ev); },
    onImageMouseUp: function (ev, pt) { if (S.drag) endRotate(ev); },
    onRender: function (well, tp) {
      if (S.drag) { applyTransform(S.drag.live); return; }
      const w = well != null ? well : curWell();
      const t = tp != null ? tp : curTp();
      applyTransform(angleAt(w, t));
    },
  };

  function install(API) {
    API = API || getAPI();
    if (S.installed || !API) return S.installed;
    if (typeof API.registerTool === 'function') { try { API.registerTool(TYPE, handlers); } catch (e) {} }
    if (typeof API.onColumnPanel === 'function') { try { API.onColumnPanel(TYPE, renderPanel); } catch (e) {} }
    S.installed = true;
    return true;
  }

  // public surface (also handy for debugging in the console)
  const pub = { interpolate: interpolate, normalizeDeg: normalizeDeg, install: install, handlers: handlers, _state: S };
  win.RotTool = pub;

  // Auto-install now if the API is already present; otherwise poll briefly and
  // also honour an optional `annotator:ready` event — so script order is moot.
  if (!install(getAPI())) {
    let tries = 0;
    const iv = win.setInterval(() => { if (install(getAPI()) || ++tries > 100) win.clearInterval(iv); }, 50);
    if (doc.addEventListener) doc.addEventListener('annotator:ready', () => install(getAPI()), { once: true });
  }
  return pub;
}

// ============================================================ entry / export
if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  if (!window.__ROT_TOOL_LOADED__) { window.__ROT_TOOL_LOADED__ = true; initBrowser(window, document); }
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { interpolate: interpolate, normalizeDeg: normalizeDeg };
}
