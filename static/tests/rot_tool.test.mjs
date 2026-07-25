/* rot_tool.test.mjs — unit tests for the pure core of static/rot_tool.js.
 *
 * Run either of:
 *     node static/tests/rot_tool.test.mjs
 *     node --test static/tests/
 *
 * rot_tool.js is a classic browser script that also exports its pure helpers via
 * CommonJS `module.exports`; with no package.json in the tree Node loads it as
 * CJS, so the browser wiring never runs here. Default-import gives module.exports.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import RotTool from '../rot_tool.js';

const { interpolate, normalizeDeg } = RotTool;

// smoothstep weight, for cross-checking against the reference formula
const smooth = (u) => u * u * (3 - 2 * u);

test('empty / non-array keyframes => 0', () => {
  assert.equal(interpolate([], 5), 0);
  assert.equal(interpolate(undefined, 5), 0);
  assert.equal(interpolate(null, 5), 0);
});

test('single keyframe is constant everywhere', () => {
  assert.equal(interpolate([[5, 42]], 0), 42);
  assert.equal(interpolate([[5, 42]], 5), 42);
  assert.equal(interpolate([[5, 42]], 999), 42);
  assert.equal(interpolate([[5, -12.5]], -999), -12.5);
});

test('two keyframes: ends held flat', () => {
  const kf = [[0, 0], [10, 100]];
  assert.equal(interpolate(kf, -5), 0);   // before first
  assert.equal(interpolate(kf, 0), 0);    // at first
  assert.equal(interpolate(kf, 10), 100); // at last
  assert.equal(interpolate(kf, 25), 100); // after last
});

test('two keyframes: smoothstep midpoint = mean (w(0.5)=0.5)', () => {
  assert.equal(interpolate([[0, 0], [10, 100]], 5), 50);
  assert.equal(interpolate([[0, -30], [10, 30]], 5), 0);
  assert.equal(interpolate([[4, 10], [8, 90]], 6), 50);
});

test('two keyframes: smoothstep quarter (u=0.25 => w=0.15625)', () => {
  const w = smooth(0.25);
  assert.ok(Math.abs(w - 0.15625) < 1e-12);
  assert.ok(Math.abs(interpolate([[0, 0], [10, 100]], 2.5) - 15.625) < 1e-9);
  // and the symmetric three-quarter point: w(0.75) = 0.84375
  assert.ok(Math.abs(interpolate([[0, 0], [10, 100]], 7.5) - 84.375) < 1e-9);
});

test('smoothstep is genuinely eased, not linear', () => {
  // at the quarter point a linear ramp would give 25; smoothstep gives 15.625
  assert.notEqual(interpolate([[0, 0], [10, 100]], 2.5), 25);
});

test('three keyframes bracket to the correct segment', () => {
  const kf = [[0, 0], [10, 100], [20, 0]];
  assert.equal(interpolate(kf, 0), 0);
  assert.equal(interpolate(kf, 10), 100);  // exact middle keyframe
  assert.equal(interpolate(kf, 20), 0);
  assert.equal(interpolate(kf, 5), 50);    // first segment midpoint
  assert.equal(interpolate(kf, 15), 50);   // second segment midpoint
});

test('unsorted keyframes are sorted internally', () => {
  assert.equal(interpolate([[10, 100], [0, 0]], 5), 50);
});

test('string values are coerced (server may stringify floats on save)', () => {
  assert.equal(interpolate([['0', '0'], ['10', '100']], 5), 50);
  assert.equal(interpolate([['0', '-30.0'], ['10', '30']], 5), 0);
});

test('matches the focus_cut build_focus_track formula, modulo the circle', () => {
  // reference: u=(tp-t0)/(t1-t0); w=u*u*(3-2u); value=v0+(v1-v0)*w
  // Rotation is ANGULAR, so interpolate() takes the shortest route and normalises the
  // result to [0,360). Under 180° apart the route is the same as the linear one — the
  // only difference is the wrap, so compare mod 360. (compose.angle_track, which bakes
  // rotation into exports, uses this same rule so video and viewer agree.)
  // Endpoints are returned as SAVED (a keyframe of -22.5 reads back as -22.5); only the
  // interpolated values in between are wrapped. Both drive the same visible rotation.
  const t0 = 3, t1 = 17, v0 = -22.5, v1 = 61.0;
  const wrap = d => ((d % 360) + 360) % 360;
  assert.equal(interpolate([[t0, v0], [t1, v1]], t0), v0, 'first keyframe reads back raw');
  assert.equal(interpolate([[t0, v0], [t1, v1]], t1), v1, 'last keyframe reads back raw');
  for (const tp of [5, 8, 10, 12]) {
    const u = (tp - t0) / (t1 - t0);
    const ref = wrap(v0 + (v1 - v0) * smooth(u));
    assert.ok(Math.abs(interpolate([[t0, v0], [t1, v1]], tp) - ref) < 1e-9, 'tp=' + tp);
  }
});

test('normalizeDeg folds into (-180, 180]', () => {
  assert.equal(normalizeDeg(0), 0);
  assert.equal(normalizeDeg(45), 45);
  assert.equal(normalizeDeg(190), -170);
  assert.equal(normalizeDeg(-190), 170);
  assert.equal(normalizeDeg(360), 0);
  assert.equal(normalizeDeg(-360), 0);
  assert.equal(normalizeDeg(540), 180);
  assert.equal(normalizeDeg(180), 180);    // upper bound is inclusive
  assert.equal(normalizeDeg(-180), 180);   // lower bound folds up
  assert.equal(normalizeDeg(720 + 45), 45);
  assert.equal(normalizeDeg(-720 - 30), -30);
});

test('normalizeDeg guards non-finite input', () => {
  assert.equal(normalizeDeg(NaN), 0);
  assert.equal(normalizeDeg(Infinity), 0);
  assert.equal(normalizeDeg(undefined), 0);
  assert.equal(Object.is(normalizeDeg(-0), 0), true);   // no negative zero
});
