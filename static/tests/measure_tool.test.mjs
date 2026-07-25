/* measure_tool.test.mjs — Node unit tests for the PURE helpers in measure_tool.js.
 *
 * Run:  node static/tests/measure_tool.test.mjs
 * (from the label_annotator/ app root; no dependencies, no build step).
 *
 * measure_tool.js is a CommonJS module (no package.json → .js is CJS in Node),
 * so its default export is the object of pure helpers. Importing it does NOT
 * touch the DOM — all browser code is guarded behind `typeof window`.
 */
import assert from 'node:assert/strict';
import measureTool from '../measure_tool.js';

const {
  containFit, canvasToSource, sourceToCanvas, clampToImage,
  lengthPx, lengthUm, measurementValue, round2,
} = measureTool;

// ---- tiny test harness -----------------------------------------------------
let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ok   ' + name); }
  catch (e) { failed++; console.log('  FAIL ' + name + '\n       ' + (e && e.message)); }
}
const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) <= eps, `${a} ≈ ${b}`);
const nearPt = (p, x, y, eps = 1e-9) => { near(p.x, x, eps); near(p.y, y, eps); };

// ===========================================================================
//  contain-fit + canvas↔source mapping for a NON-SQUARE image in a WIDER box
// ===========================================================================
// Portrait image 100×200 inside a wider 400×300 box (letterboxed left/right).
test('containFit: portrait image, wider box → pillarbox offsets', () => {
  const g = containFit(100, 200, 400, 300);
  near(g.scale, 1.5);            // min(400/100, 300/200) = min(4, 1.5)
  near(g.dispW, 150); near(g.dispH, 300);
  near(g.offsetX, 125);         // (400-150)/2
  near(g.offsetY, 0);
});

test('canvasToSource: corners + centre (portrait in wider box)', () => {
  const N = [100, 200], B = [400, 300];
  nearPt(canvasToSource(125, 0, ...N, ...B), 0, 0);        // displayed top-left
  nearPt(canvasToSource(275, 300, ...N, ...B), 100, 200);  // displayed bottom-right
  nearPt(canvasToSource(200, 150, ...N, ...B), 50, 100);   // box centre → image centre
});

// Landscape image 200×100 inside 400×300 box (letterboxed top/bottom) — exercises offsetY.
test('canvasToSource: corners + centre (landscape → letterbox top/bottom)', () => {
  const N = [200, 100], B = [400, 300];
  const g = containFit(...N, ...B);
  near(g.scale, 2); near(g.offsetX, 0); near(g.offsetY, 50);
  nearPt(canvasToSource(0, 50, ...N, ...B), 0, 0);
  nearPt(canvasToSource(400, 250, ...N, ...B), 200, 100);
  nearPt(canvasToSource(200, 150, ...N, ...B), 100, 50);
});

test('sourceToCanvas is the inverse of canvasToSource', () => {
  const N = [100, 200], B = [400, 300];
  for (const [sx, sy] of [[0, 0], [100, 200], [50, 100], [17.5, 133.25]]) {
    const c = sourceToCanvas(sx, sy, ...N, ...B);
    nearPt(canvasToSource(c.x, c.y, ...N, ...B), sx, sy, 1e-6);
  }
});

test('containFit: degenerate inputs never divide by zero (finite, no NaN)', () => {
  const g = containFit(0, 0, 400, 300);   // e.g. image not loaded yet (natural 0×0)
  assert.equal(g.dispW, 0); assert.equal(g.scale, 1);   // safe identity fallback
  const p = canvasToSource(10, 10, 0, 0, 400, 300);
  assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y), 'mapping stays finite');
});

test('clampToImage keeps endpoints on the image', () => {
  nearPt(clampToImage(-5, 250, 100, 200), 0, 200);
  nearPt(clampToImage(37, 42, 100, 200), 37, 42);
  nearPt(clampToImage(999, -1, 100, 200), 100, 0);
});

// ===========================================================================
//  length_px from coords · length_um with and without a px size
// ===========================================================================
test('lengthPx = hypot(dx, dy)', () => {
  near(lengthPx(0, 0, 3, 4), 5);
  near(lengthPx(10, 10, 10, 10), 0);
  near(lengthPx(1, 2, 4, 6), 5);
});

test('lengthUm scales px by nm/px ÷ 1000 (1625 nm/px @ 4×)', () => {
  near(lengthUm(5, 1625), 8.125);       // 5 px × 1625 nm/px / 1000
  near(lengthUm(512.3, 1625), 512.3 * 1625 / 1000);
});

test('lengthUm returns null when px size unknown/invalid', () => {
  assert.equal(lengthUm(5, null), null);
  assert.equal(lengthUm(5, undefined), null);
  assert.equal(lengthUm(5, 0), null);
  assert.equal(lengthUm(5, -3), null);
  assert.equal(lengthUm(5, NaN), null);
});

// ===========================================================================
//  composite value object (what gets stored)
// ===========================================================================
test('measurementValue: object shape with px size', () => {
  const v = measurementValue(0, 0, 3, 4, 1625);
  assert.deepEqual(v.line, [0, 0, 3, 4]);
  near(v.length_px, 5);
  near(v.length_um, round2(8.125));     // 8.13 after 2-dp rounding
});

test('measurementValue: length_um is null when px size unknown', () => {
  const v = measurementValue(10, 10, 13, 14, null);
  assert.deepEqual(v.line, [10, 10, 13, 14]);
  near(v.length_px, 5);
  assert.equal(v.length_um, null);
});

test('measurementValue: coordinates rounded to 2 dp, kept in source px', () => {
  const v = measurementValue(1.234567, 2.5, 4.1, 6.987654, 1000);
  assert.deepEqual(v.line, [1.23, 2.5, 4.1, 6.99]);
});

// ---- summary ---------------------------------------------------------------
console.log(`\nmeasure_tool: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
