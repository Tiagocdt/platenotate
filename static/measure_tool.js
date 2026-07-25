/* measure_tool.js — line-measurement plugin for the Medaka annotator.
 *
 * A self-contained, dependency-free module that adds a new image-column TYPE,
 * "measurement" (draw a line → coordinates + length). It is a PLUGIN against the
 * host's `window.AnnotatorAPI` contract — it edits no existing file and touches
 * host state only through the documented API, feature-detecting every method so
 * a missing one degrades gracefully instead of throwing.
 *
 * When a measurement column is the ACTIVE image column, the user clicks twice on
 * the image (or press-drags) to draw a line. The second click commits and saves
 * an OBJECT value for the current image:
 *
 *     image_annotations[well]["<tp>"][colName] =
 *         { line:[x0,y0,x1,y1], length_px:<float>, length_um:<float|null> }
 *
 * Coordinates are in SOURCE-image pixels (resolution-independent). `Esc` cancels
 * an in-progress line. On revisiting an annotated frame the stored line + a
 * px/µm label are redrawn.
 *
 * Dual runtime:
 *   - Browser: attaches `window.MeasureTool` and auto-installs against the host.
 *   - Node (CommonJS): exports the pure geometry/length helpers for unit tests
 *     (see static/tests/measure_tool.test.mjs). No DOM is touched at import time.
 */
'use strict';

(function (factory) {
  const api = factory();
  // Node / CommonJS: export the pure helpers (+ controller) for unit tests.
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  // Browser: expose globally and auto-wire against window.AnnotatorAPI.
  if (typeof window !== 'undefined') {
    window.MeasureTool = api;
    api._autoInit();
  }
})(function () {
  // =====================================================================
  //  PURE helpers (no DOM) — unit-tested, and reusable by the host.
  // =====================================================================
  const COLUMN_TYPE = 'measurement';
  const VERSION = '1.0.0';

  // Stable rounding so the stored JSON stays tidy and diff-friendly.
  function round2(n, dp) {
    if (n == null || typeof n !== 'number' || !isFinite(n)) return n;
    const f = Math.pow(10, dp == null ? 2 : dp);
    return Math.round(n * f) / f;
  }

  // object-fit:contain geometry: fit a natural WxH inside a box WxH, centred.
  // Returns the uniform scale and the letterbox offset (box→image, top-left).
  function containFit(natW, natH, boxW, boxH) {
    natW = +natW; natH = +natH; boxW = +boxW; boxH = +boxH;
    if (!(natW > 0 && natH > 0 && boxW > 0 && boxH > 0))
      return { scale: 1, dispW: 0, dispH: 0, offsetX: 0, offsetY: 0 };
    const scale = Math.min(boxW / natW, boxH / natH);
    const dispW = natW * scale, dispH = natH * scale;
    return { scale, dispW, dispH, offsetX: (boxW - dispW) / 2, offsetY: (boxH - dispH) / 2 };
  }

  // DISPLAY (box px, box-local) -> SOURCE (natural px). Inverse of sourceToCanvas.
  function canvasToSource(cx, cy, natW, natH, boxW, boxH) {
    const g = containFit(natW, natH, boxW, boxH);
    if (!g.scale) return { x: 0, y: 0 };
    return { x: (cx - g.offsetX) / g.scale, y: (cy - g.offsetY) / g.scale };
  }

  // SOURCE (natural px) -> DISPLAY (box px, box-local).
  function sourceToCanvas(sx, sy, natW, natH, boxW, boxH) {
    const g = containFit(natW, natH, boxW, boxH);
    return { x: sx * g.scale + g.offsetX, y: sy * g.scale + g.offsetY };
  }

  // Keep a point inside the natural-image bounds (endpoints stay on the image).
  function clampToImage(x, y, natW, natH) {
    return { x: Math.max(0, Math.min(+natW, x)), y: Math.max(0, Math.min(+natH, y)) };
  }

  function lengthPx(x0, y0, x1, y1) { return Math.hypot(x1 - x0, y1 - y0); }

  // µm from px via nm/px; null when the plate px size is unknown/invalid.
  function lengthUm(lenPx, pxSizeNm) {
    const px = Number(pxSizeNm);
    if (!isFinite(lenPx) || !(px > 0) || !isFinite(px)) return null;
    return lenPx * px / 1000;
  }

  // Build the stored measurement OBJECT from two source-pixel endpoints.
  function measurementValue(x0, y0, x1, y1, pxSizeNm) {
    const lp = lengthPx(x0, y0, x1, y1);
    const lu = lengthUm(lp, pxSizeNm);
    return {
      line: [round2(x0), round2(y0), round2(x1), round2(y1)],
      length_px: round2(lp),
      length_um: lu == null ? null : round2(lu),
    };
  }

  // =====================================================================
  //  Browser controller — all DOM access lives inside these functions,
  //  never at module-load time (so importing in Node is safe).
  // =====================================================================
  const SVGNS = 'http://www.w3.org/2000/svg';
  const ACCENT = '#e8a33d';       // theme amber (matches style.css --accent)
  const DRAG_PX = 6;              // display-px drag threshold → press-drag commit
  const MIN_LEN_PX = 1;           // reject degenerate zero-length lines

  const T = {
    api: null, installed: false,
    colName: null, active: false,
    svg: null, gLine: null,
    phase: 'idle',                // 'idle' | 'armed'
    start: null, preview: null,   // {x,y} in SOURCE px
    firstUpPending: false,
    onDown: null, onMove: null, onUp: null, onKey: null, onImgLoad: null, onResize: null, ro: null,
    panelContainer: null, panelCol: null,
    lastEvt: {},                  // dedup backstop, per event kind
  };

  // -- host-element accessors (feature-detected, with DOM fallbacks) ------
  function stageEl() {
    return (T.api && T.api.stageEl) ||
      (typeof document !== 'undefined' && document.getElementById('stage')) || null;
  }
  function imgEl() {
    return (T.api && T.api.imgEl) ||
      (typeof document !== 'undefined' && document.getElementById('bigImg')) || null;
  }
  function curWell() {
    if (T.api && typeof T.api.curWell === 'function') { try { return T.api.curWell(); } catch (e) {} }
    return (T.api && T.api.state && T.api.state.primary) != null ? T.api.state.primary : null;
  }
  function curTp() {
    if (T.api && typeof T.api.curTp === 'function') { try { return T.api.curTp(); } catch (e) {} }
    return null;
  }
  function pxSize() {
    if (T.api && typeof T.api.pxSizeNm === 'function') {
      try { const v = T.api.pxSizeNm(); return (v != null && isFinite(v) && +v > 0) ? +v : null; } catch (e) {}
    }
    return null;
  }
  function imgValueOf(col) {
    if (!T.api || typeof T.api.imgValue !== 'function' || !col) return undefined;
    const w = curWell(), tp = curTp();
    if (w == null || tp == null) return undefined;
    try { return T.api.imgValue(w, tp, col); } catch (e) { return undefined; }
  }
  // Forward-filled value (keyframes-only model): a measurement drawn on an earlier
  // frame HOLDS on later frames; the held value is DERIVED on read, never stored per
  // frame. Returns {value, startTp}: value = the measurement object, startTp = the
  // keyframe tp it came from (=== current tp means it's a real keyframe HERE).
  function imgEffectiveOf(col) {
    const none = { value: undefined, startTp: null };
    const w = curWell(), tp = curTp();
    if (w == null || tp == null || !col) return none;
    if (T.api && typeof T.api.imgEffective === 'function') {
      try { const r = T.api.imgEffective(w, col, tp); return (r && typeof r === 'object') ? r : none; } catch (e) {}
    }
    return { value: imgValueOf(col), startTp: tp };   // fallback: exact frame only
  }

  // -- installation -------------------------------------------------------
  function install(api) {
    if (!api || typeof api !== 'object') return false;
    T.api = api;
    T.installed = true;
    if (typeof api.registerTool === 'function') {
      try {
        api.registerTool(COLUMN_TYPE, {
          onActivate: activate,
          onDeactivate: deactivate,
          onImageMouseDown: (ev, pt) => hostFeed('down', ev, pt),
          onImageMouseMove: (ev, pt) => hostFeed('move', ev, pt),
          onImageMouseUp: (ev, pt) => hostFeed('up', ev, pt),
          onRender: () => redraw(),
        });
      } catch (e) { /* non-fatal */ }
    }
    if (typeof api.onColumnPanel === 'function') {
      try { api.onColumnPanel(COLUMN_TYPE, renderPanel); } catch (e) { /* non-fatal */ }
    }
    return true;
  }

  // Auto-wire against window.AnnotatorAPI whenever it appears (drop-in use).
  // The host may also call MeasureTool.install(api) explicitly (idempotent).
  function _autoInit() {
    if (typeof window === 'undefined') return;
    if (install(window.AnnotatorAPI)) return;
    let tries = 0;
    const timer = setInterval(() => {
      if (T.installed || install(window.AnnotatorAPI) || ++tries > 50) clearInterval(timer);
    }, 200);
    try {
      window.addEventListener('annotator-api-ready',
        () => { if (!T.installed) install(window.AnnotatorAPI); }, { once: true });
    } catch (e) { /* ignore */ }
  }

  // -- activation ---------------------------------------------------------
  function activate(colName) {
    if (colName) T.colName = colName;
    T.active = true;
    resetDraft();
    const svg = ensureOverlay();
    if (svg) svg.style.pointerEvents = 'auto';   // interaction surface ON while active
    wireOverlay();
    redraw();
    refreshPanel();
  }
  function deactivate() {
    T.active = false;
    resetDraft();
    unwireOverlay();
    if (T.svg) { T.svg.style.pointerEvents = 'none'; clearDraw(); }
  }
  function resetDraft() { T.phase = 'idle'; T.start = T.preview = null; T.firstUpPending = false; }

  // -- SVG overlay --------------------------------------------------------
  function ensureOverlay() {
    if (T.svg) return T.svg;
    const stage = stageEl();
    if (!stage || typeof document === 'undefined') return null;
    // #stage is position:relative in style.css; guard the general case anyway.
    try {
      const pos = window.getComputedStyle ? window.getComputedStyle(stage).position : 'relative';
      if (pos === 'static') stage.style.position = 'relative';
    } catch (e) { /* ignore */ }
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'measure-overlay');
    Object.assign(svg.style, {
      position: 'absolute', left: '0', top: '0', width: '100%', height: '100%',
      pointerEvents: 'none', zIndex: '6', overflow: 'visible', cursor: 'crosshair',
    });
    stage.appendChild(svg);
    T.svg = svg;
    T.gLine = document.createElementNS(SVGNS, 'g');
    svg.appendChild(T.gLine);
    return svg;
  }
  function clearDraw() { if (T.gLine) while (T.gLine.firstChild) T.gLine.removeChild(T.gLine.firstChild); }

  // -- geometry: displayed-image rect in STAGE-local coords ---------------
  // Two nested transforms: the <img> element box is flex-centred inside #stage,
  // and object-fit:contain letterboxes the natural image WITHIN that box.
  function geom() {
    const stage = stageEl(), img = imgEl();
    if (!stage || !img || typeof stage.getBoundingClientRect !== 'function') return null;
    const natW = img.naturalWidth || 0, natH = img.naturalHeight || 0;
    if (!(natW > 0 && natH > 0)) return null;
    const sr = stage.getBoundingClientRect(), ir = img.getBoundingClientRect();
    if (!(ir.width > 0 && ir.height > 0)) return null;
    const fit = containFit(natW, natH, ir.width, ir.height);
    return {
      natW, natH, scale: fit.scale,
      dispLeft: (ir.left - sr.left) + fit.offsetX,   // displayed image, stage-local
      dispTop: (ir.top - sr.top) + fit.offsetY,
      stageLeft: sr.left, stageTop: sr.top,
    };
  }
  function srcToStage(p, g) { return { x: g.dispLeft + p.x * g.scale, y: g.dispTop + p.y * g.scale }; }
  function clientToSource(clientX, clientY, g) {
    const sx = (clientX - g.stageLeft - g.dispLeft) / g.scale;
    const sy = (clientY - g.stageTop - g.dispTop) / g.scale;
    return clampToImage(sx, sy, g.natW, g.natH);
  }

  // -- event wiring (self-driven fallback path) ---------------------------
  function wireOverlay() {
    if (typeof document === 'undefined' || !T.svg || T.onDown) return;   // already wired
    T.onDown = (ev) => {
      if (ev.button != null && ev.button !== 0) return;
      const g = geom(); if (!g) return;
      ev.preventDefault();
      selfFeed('down', ev, clientToSource(ev.clientX, ev.clientY, g));
    };
    T.onMove = (ev) => { const g = geom(); if (!g) return; selfFeed('move', ev, clientToSource(ev.clientX, ev.clientY, g)); };
    T.onUp = (ev) => { const g = geom(); if (!g) return; selfFeed('up', ev, clientToSource(ev.clientX, ev.clientY, g)); };
    T.svg.addEventListener('mousedown', T.onDown);
    T.svg.addEventListener('mousemove', T.onMove);
    document.addEventListener('mouseup', T.onUp);
    // Capture-phase Esc so we pre-empt the app's own key handler while drawing.
    T.onKey = (ev) => { if (ev.key === 'Escape' && T.phase === 'armed') { ev.stopPropagation(); cancel(); } };
    document.addEventListener('keydown', T.onKey, true);
    // Keep the drawing aligned when the frame swaps or the box resizes.
    T.onImgLoad = () => redraw();
    const img = imgEl(); if (img) img.addEventListener('load', T.onImgLoad);
    T.onResize = () => redraw();
    window.addEventListener('resize', T.onResize);
    if (typeof ResizeObserver !== 'undefined') {
      try { T.ro = new ResizeObserver(() => redraw()); T.ro.observe(stageEl()); } catch (e) { /* ignore */ }
    }
  }
  function unwireOverlay() {
    if (typeof document === 'undefined') return;
    if (T.svg && T.onDown) T.svg.removeEventListener('mousedown', T.onDown);
    if (T.svg && T.onMove) T.svg.removeEventListener('mousemove', T.onMove);
    if (T.onUp) document.removeEventListener('mouseup', T.onUp);
    if (T.onKey) document.removeEventListener('keydown', T.onKey, true);
    const img = imgEl(); if (img && T.onImgLoad) img.removeEventListener('load', T.onImgLoad);
    if (T.onResize) window.removeEventListener('resize', T.onResize);
    if (T.ro) { try { T.ro.disconnect(); } catch (e) {} T.ro = null; }
    T.onDown = T.onMove = T.onUp = T.onKey = T.onImgLoad = T.onResize = null;
  }

  // -- host-driven path: pt already in SOURCE px (host maps canvas→source) --
  function hostFeed(kind, ev, pt) {
    if (!pt) return;
    const g = geom();
    const src = g ? clampToImage(pt.x, pt.y, g.natW, g.natH) : { x: pt.x, y: pt.y };
    if (dedup(kind, ev, src)) return;
    feed(kind, src);
  }
  function selfFeed(kind, ev, src) { if (dedup(kind, ev, src)) return; feed(kind, src); }

  // Guard against the SAME physical interaction being processed twice when both
  // the overlay's own listener and a host-dispatched handler fire for one event.
  function dedup(kind, ev, src) {
    if (ev && typeof ev === 'object') {          // shared DOM event → tag once
      const seen = ev.__measureSeen || (ev.__measureSeen = {});
      if (seen[kind]) return true;
      seen[kind] = true;
    }
    const now = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    const last = T.lastEvt[kind];                // backstop for synthetic events
    if (last && (now - last.t) < 80 && src &&
        Math.abs(src.x - last.x) < 1.5 && Math.abs(src.y - last.y) < 1.5) {
      last.t = now; return true;
    }
    if (src) T.lastEvt[kind] = { t: now, x: src.x, y: src.y };
    return false;
  }

  // -- interaction state machine (source-pixel coords) --------------------
  function feed(kind, src) {
    if (!T.active || !src) return;
    if (kind === 'down') {
      if (T.phase === 'idle') { T.start = src; T.preview = src; T.phase = 'armed'; T.firstUpPending = true; redraw(); }
      else if (T.phase === 'armed') { commit(src); }        // second click → commit
    } else if (kind === 'move') {
      if (T.phase === 'armed') { T.preview = src; redraw(); }
    } else if (kind === 'up') {
      if (T.phase === 'armed' && T.firstUpPending) {
        T.firstUpPending = false;
        // press-drag: a meaningful drag between down and up commits immediately.
        const g = geom();
        const movedDisp = g ? lengthPx(T.start.x, T.start.y, src.x, src.y) * g.scale : 0;
        if (movedDisp >= DRAG_PX) commit(src);
        // else stay armed for the classic second click.
      }
    }
  }
  function commit(endSrc) {
    const start = T.start, end = endSrc;
    resetDraft();
    if (!start || !end) { redraw(); return; }
    if (!(lengthPx(start.x, start.y, end.x, end.y) >= MIN_LEN_PX)) { redraw(); return; }
    const value = measurementValue(start.x, start.y, end.x, end.y, pxSize());
    if (T.api && typeof T.api.doAssign === 'function') {
      try { T.api.doAssign('image', T.colName, value); } catch (e) { /* non-fatal */ }
    }
    redraw();          // stored value re-renders via imgValue on the host's next render too
    refreshPanel();
  }
  function cancel() { resetDraft(); redraw(); }

  // -- drawing ------------------------------------------------------------
  function redraw() {
    if (!T.svg) return;
    clearDraw();
    if (!T.active) return;
    const g = geom(); if (!g) return;
    if (T.phase !== 'armed') {                    // stored (or forward-filled/held) line
      const eff = imgEffectiveOf(T.colName);
      const v = eff.value;
      if (v && Array.isArray(v.line) && v.line.length === 4) {
        const held = eff.startTp != null && eff.startTp !== curTp();   // derived from an earlier keyframe
        drawLine(v.line[0], v.line[1], v.line[2], v.line[3], g, held,
                 labelText(v.length_px, v.length_um) + (held ? '  ·  held from tp ' + eff.startTp : ''));
      }
    }
    if (T.phase === 'armed' && T.start && T.preview) {   // live rubber-band
      const lp = lengthPx(T.start.x, T.start.y, T.preview.x, T.preview.y);
      drawLine(T.start.x, T.start.y, T.preview.x, T.preview.y, g, true, labelText(lp, lengthUm(lp, pxSize())));
    }
  }
  function drawLine(x0, y0, x1, y1, g, preview, label) {
    const a = srcToStage({ x: x0, y: y0 }, g), b = srcToStage({ x: x1, y: y1 }, g);
    const under = mk('line'); setLine(under, a, b);
    under.setAttribute('stroke', 'rgba(0,0,0,0.55)');
    under.setAttribute('stroke-width', preview ? '3.4' : '4');
    under.setAttribute('stroke-linecap', 'round');
    T.gLine.appendChild(under);
    const line = mk('line'); setLine(line, a, b);
    line.setAttribute('stroke', ACCENT);
    line.setAttribute('stroke-width', preview ? '1.6' : '2');
    line.setAttribute('stroke-linecap', 'round');
    if (preview) line.setAttribute('stroke-dasharray', '6 4');
    T.gLine.appendChild(line);
    [a, b].forEach((p) => {
      const c = mk('circle');
      c.setAttribute('cx', p.x); c.setAttribute('cy', p.y); c.setAttribute('r', preview ? '3' : '4');
      c.setAttribute('fill', ACCENT); c.setAttribute('stroke', '#2a1b04'); c.setAttribute('stroke-width', '1');
      T.gLine.appendChild(c);
    });
    if (label) drawLabel((a.x + b.x) / 2, (a.y + b.y) / 2, label);
  }
  function drawLabel(mx, my, text) {
    const rect = mk('rect');
    rect.setAttribute('fill', 'rgba(19,18,16,0.82)');
    rect.setAttribute('stroke', 'rgba(232,163,61,0.5)'); rect.setAttribute('stroke-width', '1');
    rect.setAttribute('rx', '3');
    const t = mk('text');
    t.textContent = text;
    t.setAttribute('x', mx); t.setAttribute('y', my - 9); t.setAttribute('text-anchor', 'middle');
    t.setAttribute('font-family', 'ui-monospace, Menlo, Consolas, monospace');
    t.setAttribute('font-size', '11'); t.setAttribute('fill', '#ece4d8');
    t.setAttribute('paint-order', 'stroke');
    t.setAttribute('stroke', 'rgba(0,0,0,0.65)'); t.setAttribute('stroke-width', '0.6');
    T.gLine.appendChild(rect); T.gLine.appendChild(t);
    let bb = null; try { bb = t.getBBox(); } catch (e) { /* not yet laid out */ }
    if (bb && bb.width) {
      rect.setAttribute('x', bb.x - 5); rect.setAttribute('y', bb.y - 2);
      rect.setAttribute('width', bb.width + 10); rect.setAttribute('height', bb.height + 4);
    } else {
      const w = text.length * 6.6 + 10;
      rect.setAttribute('x', mx - w / 2); rect.setAttribute('y', my - 22);
      rect.setAttribute('width', w); rect.setAttribute('height', 15);
    }
  }
  function mk(tag) { return document.createElementNS(SVGNS, tag); }
  function setLine(el, a, b) { el.setAttribute('x1', a.x); el.setAttribute('y1', a.y); el.setAttribute('x2', b.x); el.setAttribute('y2', b.y); }
  function labelText(px, um) {
    if (px == null || !isFinite(px)) return '';
    let s = Math.round(px) + ' px';
    if (um != null && isFinite(um)) s += '  ·  ' + (um >= 100 ? Math.round(um) : (+um).toFixed(1)) + ' µm';
    return s;
  }

  // -- column panel (onColumnPanel) --------------------------------------
  function renderPanel(container, colName) {
    if (!container || typeof document === 'undefined') return;
    const cn = colName || T.colName;
    T.panelContainer = container; T.panelCol = cn;
    container.innerHTML = '';
    const sec = el('div', 'section');

    const eff = imgEffectiveOf(cn);
    const v = eff.value;
    const isKf = v && v.length_px != null && eff.startTp === curTp();   // real keyframe on THIS frame?
    const big = el('div', 'big');
    if (v && v.length_px != null) {
      big.textContent = Math.round(v.length_px) + ' px' +
        (v.length_um != null ? '   ·   ' + (+v.length_um).toFixed(1) + ' µm' : '');
    } else { big.textContent = 'not measured on this well yet'; big.classList.add('muted'); }
    sec.appendChild(big);
    if (v && v.length_px != null) {                 // keyframe-vs-derived distinction
      const badge = el('div', 'hint', isKf ? '● keyframe on this frame'
                                            : '○ held from frame ' + eff.startTp + ' — draw to set one here');
      try { badge.style.color = isKf ? '#e8a33d' : '#9a9a9a'; } catch (e) {}
      sec.appendChild(badge);
    }

    const px = pxSize();
    sec.appendChild(el('div', 'hint',
      px != null ? 'px size ' + px + ' nm/px — µm computed'
                 : 'px size unknown — set µm/px in the Plate tab (stays pixels until then)'));

    const row = el('div', 'inline');
    const clr = el('button', 'btn sm', 'clear keyframe here');
    clr.disabled = (imgValueOf(cn) === undefined);   // only a real keyframe on THIS frame is clearable
    clr.onclick = () => {
      if (T.api && typeof T.api.doAssign === 'function') { try { T.api.doAssign('image', cn, null); } catch (e) {} }
      cancel();
      renderPanel(container, cn);
    };
    row.appendChild(clr);
    sec.appendChild(row);

    sec.appendChild(el('div', 'hint', 'Click twice on the image to draw a line (or press-drag). Esc cancels.'));
    container.appendChild(sec);
  }
  function refreshPanel() { if (T.panelContainer) renderPanel(T.panelContainer, T.panelCol || T.colName); }
  function el(tag, cls, txt) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }

  // =====================================================================
  //  public surface
  // =====================================================================
  return {
    // metadata
    COLUMN_TYPE, VERSION,
    // lifecycle / integration
    install, _autoInit, activate, deactivate, redraw, renderPanel,
    // PURE helpers (also unit-tested)
    containFit, canvasToSource, sourceToCanvas, clampToImage,
    lengthPx, lengthUm, measurementValue, round2,
    // debug hook
    _T: T,
  };
});
