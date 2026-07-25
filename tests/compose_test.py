#!/usr/bin/env python
"""compose_test.py — the render engine's pure logic, no image data needed.

Covers the parts that silently produce a WRONG-looking movie rather than an error:

  * the rotation track must match static/rot_tool.js exactly (smoothstep, shortest
    way round the circle) — otherwise the export disagrees with the viewer preview;
  * PIL rotates counter-clockwise while CSS rotates clockwise, so the sign must be
    flipped — a mirrored spin is easy to ship and hard to notice;
  * tints/overlays must not clip, and a tile must keep its size so a montage grids;
  * the scale bar must pick a round number that fits.

    python tests/compose_test.py
"""
from __future__ import annotations
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE.parent / "packaging" / "_deps", HERE.parent.parent / "hyperstack_video"):
    if p.is_dir():
        sys.path.insert(0, str(p))

import numpy as np                                                     # noqa: E402
import compose                                                         # noqa: E402

passed = failed = 0


def check(cond, name):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def near(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ---------------------------------------------------------------- angle track
tps = list(range(0, 11))
t = compose.angle_track([(0, 0.0), (10, 90.0)], tps)
check(near(t[0], 0.0) and near(t[10], 90.0), "angle: ends sit exactly on the keyframes")
check(near(t[5], 45.0), "angle: smoothstep is symmetric — the midpoint is the mean")
check(t[2] < 18.0 and t[8] > 72.0, "angle: eases in and out (not a linear ramp)")

# the shortest way around the circle: 350° → 10° is +20°, never -340°
t = compose.angle_track([(0, 350.0), (10, 10.0)], tps)
check(near(t[5], 0.0, 1e-6) or near(t[5], 360.0, 1e-6),
      "angle: 350°→10° crosses through 0°, the short way")
check(all(0 <= v < 360 for v in t.values()), "angle: every value normalised to [0,360)")

t = compose.angle_track([(3, 42.0)], tps)
check(all(near(v, 42.0) for v in t.values()), "angle: a single keyframe holds flat everywhere")
check(compose.angle_track([], tps) is None, "angle: no keyframes → no track (caller falls back)")

# ends are held, never extrapolated past the last keyframe
t = compose.angle_track([(2, 10.0), (4, 20.0)], tps)
check(near(t[0], 10.0) and near(t[10], 20.0), "angle: outside the keyframes the ends hold")

# --------------------------------------------------------------- rotate sign
# A bright pixel above-left of centre must land above-RIGHT after +90°, because the
# viewer's CSS rotate(+deg) turns clockwise on screen.
img = np.zeros((64, 64), np.uint8)
img[16, 16] = 255                                     # up-left quadrant
r = compose.rotate(img, 90)
ys, xs = np.nonzero(r > 100)
check(len(ys) > 0 and ys.mean() < 32 and xs.mean() > 32,
      "rotate: +90° turns CLOCKWISE, matching the CSS preview")
check(r.shape == img.shape, "rotate: the tile keeps its size (montage tiles stay aligned)")
check(compose.rotate(img, 0) is img, "rotate: 0° is a no-op, not a resample")

# --------------------------------------------------------------- colour
g = np.full((8, 8), 200, np.uint8)
check(compose.tint(g, "gray").ndim == 2, "tint: gray stays a single-channel plane")
check(int(compose.tint(g, "invert")[0, 0]) == 55, "tint: invert is 255 - value")
gr = compose.tint(g, "green")
check(gr.ndim == 3 and gr[0, 0, 1] >= gr[0, 0, 0] and gr[0, 0, 1] >= gr[0, 0, 2],
      "tint: green weights the green channel highest")
check(compose.tint(g, "not-a-colormap").ndim == 2, "tint: an unknown colour degrades to gray")

a = np.full((4, 4), 200, np.uint8)
b = np.full((4, 4), 200, np.uint8)
s = compose.screen(a, b)
check(s.max() <= 255 and s.min() >= 200,
      "screen: overlaying two bright channels brightens without overflowing")
check(compose.screen(np.zeros((4, 4), np.uint8), b).max() == 200,
      "screen: a black channel leaves the other one unchanged")

# --------------------------------------------------------------- labels + bar
lab = compose.draw_lines(np.zeros((64, 200), np.uint8), ["A04", "st24"], "tl", 14)
check(lab.ndim == 3, "labels: text forces RGB (so the movie's pixel format is stable)")
check(lab.max() > 0, "labels: something was actually drawn")
check(compose.draw_lines(np.zeros((64, 200), np.uint8), [], "tl", 14).ndim == 2,
      "labels: no lines → the frame is left untouched (still gray)")

bar = compose.draw_scalebar(np.zeros((120, 400, 3), np.uint8), 3.25, 12)
check(bar.max() > 0, "scalebar: drawn when µm/px is known")
check((compose.draw_scalebar(np.zeros((120, 400, 3), np.uint8), None, 12).max() == 0),
      "scalebar: skipped (not faked) when the pixel size is unknown")

# --------------------------------------------------------------- misc
check(compose._parse_colour("#49c5cf") == (73, 197, 207), "colour: #rrggbb parses")
check(compose._parse_colour("#fff") == (255, 255, 255), "colour: #rgb shorthand expands")
check(compose._parse_colour("nonsense") == (255, 255, 255), "colour: garbage falls back to white")
check(compose._opposite("tl") == "tr", "labels: the time stamp goes to the opposite corner")
check(compose.time_label(5, [1], 10) == "t5 · 0h40",
      "time: cadence turns a timepoint into elapsed time")
check(compose.time_label(5, [1], None) == "t5", "time: no cadence → just the timepoint index")
u16 = np.full((4, 4), 4096, np.uint16)
check(compose._u8(u16).dtype == np.uint8, "dtype: 16-bit sources are reduced to 8-bit")

print(f"\ncompose_test: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
