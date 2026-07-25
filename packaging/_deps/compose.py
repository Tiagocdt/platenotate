#!/usr/bin/env python
"""compose.py — annotation-aware frame composer for movies & montages.

`well_hyperstack.build_video` renders ONE raw plane per frame. This module is the
richer path behind PlateNotate's Render options: it decides, per channel, WHICH
z-plane to show (a max-projection, the annotated focus track, one slice, the
middle), optionally rotates every frame by the annotated rotation track, tints and
overlays channels into one composite, and burns per-tile labels + a scale bar onto
the montage.

Everything it reads comes from the annotations the app already saves (medaka.db,
screening-JSON fallback), so the export matches what you see in the viewer:

  focus     `slice` keyframes  → continuous fractional focus track (smoothstep)
  rotation  `rotation` keyframes → angular smoothstep, SHORTEST way around the circle
            (identical to static/rot_tool.js, so the video matches the preview)
  stage     `iwamatsu_stage` keyframes → forward-filled label per timepoint
  labels    well-scope annotations (mixture, line, …) + plate cadence for elapsed time

Nothing here ever hard-fails on a missing annotation: every track degrades (focus →
modal best-focus slice → middle slice; rotation → 0°; stage → blank) and reports what
it actually used in `notes`, which the caller surfaces in the job dock.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np

import well_hyperstack as wh
import focus_cut as fc

# ---------------------------------------------------------------- colour
# Single-hue tints: gray → RGB weights. Kept below 1.0 on the off-channels so a
# bright embryo tints instead of clipping to white.
TINTS = {
    "gray": None,                       # leave as luminance
    "green": (0.15, 1.0, 0.30), "cyan": (0.0, 1.0, 1.0), "magenta": (1.0, 0.0, 1.0),
    "red": (1.0, 0.12, 0.12), "blue": (0.30, 0.45, 1.0), "yellow": (1.0, 0.95, 0.20),
    "orange": (1.0, 0.55, 0.05), "amber": (1.0, 0.78, 0.42), "violet": (0.65, 0.35, 1.0),
    "sepia": (1.0, 0.87, 0.70), "ice": (0.72, 0.88, 1.0),
}
# channel index → default tint when the user hasn't picked one (BF stays gray)
DEFAULT_TINTS = ["gray", "green", "magenta", "cyan", "yellow", "red"]


def _u8(a):
    """Any crop dtype → uint8 (crops are 8-bit; 16-bit sources are byte-shifted)."""
    if a.dtype == np.uint8:
        return a
    if a.dtype == np.uint16:
        return (a >> 8).astype(np.uint8)
    a = a.astype(np.float32)
    hi = float(a.max()) or 1.0
    return np.clip(a * (255.0 / hi), 0, 255).astype(np.uint8)


def _rgb(a):
    return a if a.ndim == 3 else np.repeat(a[..., None], 3, 2)


def tint(gray, cmap):
    """Apply a colour to a grayscale plane. 'gray' → unchanged 2-D; 'invert' →
    inverted 2-D (a classic brightfield look); a named tint or any matplotlib
    colormap → RGB."""
    if not cmap or cmap == "gray":
        return gray
    if cmap == "invert":
        return 255 - gray
    if cmap in TINTS and TINTS[cmap]:
        w = np.array(TINTS[cmap], np.float32)
        return np.clip(gray[..., None].astype(np.float32) * w, 0, 255).astype(np.uint8)
    try:                                        # any matplotlib colormap (magma, viridis…)
        import matplotlib.cm as mcm
        rgb = mcm.get_cmap(cmap)(gray.astype(np.float32) / 255.0)[..., :3]
        return (rgb * 255).astype(np.uint8)
    except Exception:                           # noqa: BLE001 — unknown name → leave gray
        return gray


def screen(a, b):
    """Screen blend, 1-(1-a)(1-b) — the non-clipping way to overlay two channels
    (Fiji's composite look): bright pixels add without saturating to white."""
    a3 = _rgb(a).astype(np.float32) / 255.0
    b3 = _rgb(b).astype(np.float32) / 255.0
    return np.clip((1 - (1 - a3) * (1 - b3)) * 255.0, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- geometry
def rotate(im, deg):
    """Rotate about the tile centre WITHOUT changing its size, matching the viewer.

    The browser previews rotation with CSS `rotate(+deg)`, which turns CLOCKWISE on
    screen; PIL's `Image.rotate(+deg)` turns counter-clockwise — hence the negation.
    Bilinear, corners filled black. Same size in = same size out, so tiles still grid."""
    if not deg or abs(float(deg)) < 1e-3:
        return im
    from PIL import Image
    pil = Image.fromarray(im)
    out = pil.rotate(-float(deg), resample=Image.BILINEAR, expand=False,
                     fillcolor=(0, 0, 0) if im.ndim == 3 else 0)
    return np.asarray(out)


# ---------------------------------------------------------------- tracks
def angle_track(anchors, tps):
    """Continuous rotation (degrees) per timepoint from `rotation` keyframes.

    Byte-for-byte the same rule as static/rot_tool.js `interpolate()`: smoothstep
    between the bracketing keyframes, taking the SHORTEST signed route around the
    circle so 350°→10° is a +20° nudge, not a -340° spin. Ends are held flat."""
    if not anchors:
        return None
    kf = sorted((int(t), float(v)) for t, v in anchors)
    at = [t for t, _ in kf]
    out = {}
    for tp in tps:
        if tp <= at[0]:
            out[tp] = kf[0][1] % 360
        elif tp >= at[-1]:
            out[tp] = kf[-1][1] % 360
        else:
            i = int(np.searchsorted(at, tp, side="right")) - 1
            (t0, v0), (t1, v1) = kf[i], kf[i + 1]
            u = (tp - t0) / (t1 - t0) if t1 != t0 else 0.0
            w = u * u * (3 - 2 * u)                       # smoothstep
            d = ((v1 - v0 + 540) % 360) - 180             # shortest signed delta
            out[tp] = (v0 + d * w) % 360
    return out


def _plane_modes(chd, key):
    """Which plane modes this channel can actually serve on this plate."""
    zk = "bf" if key == "bf" else key + "_z"
    return "perz" if zk in chd else "flat"


def _z_index(chd, key):
    zk = "bf" if key == "bf" else key + "_z"
    return chd.get(zk, {})


class WellRender:
    """Everything needed to draw ONE well: its plane index per channel, its focus /
    rotation / stage tracks, and the labels to burn. Built once, reused per frame."""

    def __init__(self, pd, well, chd, tps, spec, H, W, wann=None, cadence=None):
        self.pd, self.well, self.chd, self.tps = pd, well, chd, tps
        self.H, self.W = H, W
        self.spec = spec
        self.notes = []
        self.zs = sorted({z for (_t, z) in chd.get("bf", {})})
        self.mid_z = self.zs[len(self.zs) // 2] if self.zs else 1

        # --- focus track (only built when some channel asks for mode 'focus') ---
        self.focus = None
        if any((c or {}).get("mode") == "focus" for c in spec.get("channels", {}).values()):
            anchors = fc._anchors(pd, well, "slice")
            if anchors:
                self.focus = fc.build_focus_track(anchors, tps, spec.get("ease", "smoothstep"))
                self.notes.append(f"focus: {len(anchors)} slice keyframe(s)")
            else:                                          # graceful, never a hard fail
                best = wh.best_focus_slices(pd, [well]).get(well)
                z = best if best is not None else self.mid_z
                self.focus = {t: float(z) for t in tps}
                self.notes.append(f"focus: no slice keyframes → fixed SL{z}")

        # --- rotation track ---
        self.rot = None
        if spec.get("rotate"):
            ra = fc._anchors(pd, well, "rotation")
            if ra:
                self.rot = angle_track(ra, tps)
                self.notes.append(f"rotation: {len(ra)} keyframe(s)")
            else:
                self.notes.append("rotation: none saved → 0°")

        # --- labels ---
        lb = spec.get("labels") or {}
        self.stage = None
        if lb.get("stage"):
            sa = fc._anchors(pd, well, "iwamatsu_stage")
            self.stage = fc._forward_fill(tps, sa) if sa else {}
            if not sa:
                self.notes.append("stage: no keyframes")
        self.wann = wann or {}
        self.cadence = cadence

    # ---- plane selection -------------------------------------------------
    def plane(self, key, tp, cfg):
        """The 2-D uint8 plane for one channel at one timepoint, per its mode."""
        mode = (cfg or {}).get("mode") or "maxproj"
        kind = _plane_modes(self.chd, key)
        if kind == "flat":                                 # legacy FL: one frame per tp
            p = self.chd.get(key, {}).get(tp)
            return (wh._fit(_u8(wh._read(p)), self.H, self.W) if p is not None
                    else np.zeros((self.H, self.W), np.uint8))
        idx = _z_index(self.chd, key)
        zs = sorted({z for (t, z) in idx if t == tp}) or sorted({z for (_t, z) in idx})
        if not zs:
            return np.zeros((self.H, self.W), np.uint8)
        if mode == "maxproj":
            acc = None
            for z in zs:
                p = idx.get((tp, z))
                if p is None:
                    continue
                a = wh._fit(_u8(wh._read(p)), self.H, self.W)
                acc = a if acc is None else np.maximum(acc, a)
            return acc if acc is not None else np.zeros((self.H, self.W), np.uint8)
        if mode == "focus" and self.focus is not None:
            if key == "bf":                                # fractional blend of two slices
                return _u8(fc._bf_focus_frame(self.chd["bf"], tp, self.focus.get(tp, self.mid_z),
                                              self.zs or zs, self.H, self.W))
            z = int(round(self.focus.get(tp, self.mid_z)))
        elif mode == "slice":
            z = int((cfg or {}).get("z") or self.mid_z)
        else:                                              # 'mid' (and focus on a flat chan)
            z = zs[len(zs) // 2]
        z = min(zs, key=lambda zz: abs(zz - z))            # nearest present slice
        p = idx.get((tp, z))
        return (wh._fit(_u8(wh._read(p)), self.H, self.W) if p is not None
                else np.zeros((self.H, self.W), np.uint8))

    # ---- one composed tile ----------------------------------------------
    def tile(self, tp, keys):
        """Compose this well's tile at `tp`: selected channels → tint → (overlay) →
        rotate → labels. Returns 2-D (gray) or 3-D (RGB) uint8."""
        chans = self.spec.get("channels", {})
        overlay = bool(self.spec.get("overlay")) and len(keys) > 1
        parts = []
        for i, k in enumerate(keys):
            cfg = chans.get(k) or {}
            g = self.plane(k, tp, cfg)
            parts.append(tint(g, cfg.get("cmap") or DEFAULT_TINTS[i % len(DEFAULT_TINTS)]))
        if overlay:
            out = parts[0]
            for p in parts[1:]:
                out = screen(out, p)
        else:
            out = parts[0]
        if self.rot is not None:
            out = rotate(out, self.rot.get(tp, 0.0))
        return out

    # ---- label text ------------------------------------------------------
    def tile_lines(self, tp):
        lb = self.spec.get("labels") or {}
        lines = []
        if lb.get("well"):
            head = self.well
            if lb.get("plate"):
                head = f"{_short(self.pd.name)} {head}"
            lines.append(head)
        elif lb.get("plate"):
            lines.append(_short(self.pd.name))
        if lb.get("stage") and self.stage:
            s = self.stage.get(tp) or ""
            if s:
                lines.append(s)
        for col in (lb.get("columns") or []):
            v = self.wann.get(col)
            if v not in (None, ""):
                lines.append(f"{v}" if lb.get("bare_columns") else f"{col}: {v}")
        if lb.get("rotation") and self.rot is not None:
            lines.append(f"{self.rot.get(tp, 0):.0f}°")
        if lb.get("focus") and self.focus is not None:
            lines.append(f"SL{self.focus.get(tp, 0):.1f}")
        return lines


def _short(name):
    m = re.search(r"AQV\d+", name or "")
    return m.group(0) if m else (name or "")[:12]


def time_label(tp, tps, cadence_min):
    """'t42 · 6h50' — the frame index plus elapsed time when the plate's cadence is
    known (cadence_min from the DB's plate row)."""
    txt = f"t{tp}"
    if cadence_min:
        mins = (tp - (tps[0] if tps else 1)) * float(cadence_min)
        txt += f" · {int(mins // 60)}h{int(mins % 60):02d}"
    return txt


# ---------------------------------------------------------------- drawing
_FONT_CACHE = {}


def _font(size):
    """A TrueType face at `size`, from the first font present on this machine.
    Pillow ≥10.1's sized default is the portable fallback (frozen apps ship no fonts)."""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    from PIL import ImageFont
    cands = ["/System/Library/Fonts/Supplemental/Arial.ttf",
             "/System/Library/Fonts/Helvetica.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf"]
    f = None
    for c in cands:
        try:
            f = ImageFont.truetype(c, size)
            break
        except Exception:                                   # noqa: BLE001
            continue
    if f is None:
        try:
            f = ImageFont.load_default(size=size)            # Pillow ≥ 10.1
        except TypeError:
            f = ImageFont.load_default()
    _FONT_CACHE[size] = f
    return f


def draw_lines(im, lines, corner="tl", size=14, colour=(255, 255, 255), pad=4):
    """Burn text lines into a corner of a frame. Always RGB out (text needs colour),
    with a dark stroke so it stays readable over both black background and a bright
    embryo. Mutating a copy — the caller's plane is left alone."""
    if not lines:
        return im
    from PIL import Image, ImageDraw
    pil = Image.fromarray(_rgb(im))
    d = ImageDraw.Draw(pil)
    f = _font(size)
    lh = size + 3
    H, W = pil.height, pil.width
    y = pad if corner in ("tl", "tr") else H - pad - lh * len(lines)
    for i, ln in enumerate(lines):
        try:
            w = int(d.textlength(ln, font=f))
        except AttributeError:                              # very old Pillow
            w = size // 2 * len(ln)
        x = pad if corner in ("tl", "bl") else W - pad - w
        d.text((x, y + i * lh), ln, fill=tuple(colour), font=f,
               stroke_width=max(1, size // 10), stroke_fill=(0, 0, 0))
    return np.asarray(pil)


def draw_scalebar(im, um_per_px, size=14, colour=(255, 255, 255), pad=8, frac=0.22):
    """A 1/2/5×10ⁿ µm bar spanning ~`frac` of the width, bottom-right, with a caption."""
    if not um_per_px or um_per_px <= 0:
        return im
    from PIL import Image, ImageDraw
    H, W = im.shape[:2]
    target_um = W * frac * um_per_px
    exp = math.floor(math.log10(target_um)) if target_um > 0 else 0
    nice = min([1, 2, 5, 10], key=lambda m: abs(m * 10 ** exp - target_um)) * 10 ** exp
    bar_px = int(round(nice / um_per_px))
    if bar_px < 8 or bar_px > W - 2 * pad:
        return im
    pil = Image.fromarray(_rgb(im))
    d = ImageDraw.Draw(pil)
    f = _font(size)
    x1, y1 = W - pad, H - pad
    x0, th = x1 - bar_px, max(2, size // 5)
    d.rectangle([x0 - 1, y1 - th - 1, x1 + 1, y1 + 1], fill=(0, 0, 0))
    d.rectangle([x0, y1 - th, x1, y1], fill=tuple(colour))
    cap = f"{nice:g} µm" if nice < 1000 else f"{nice / 1000:g} mm"
    try:
        cw = int(d.textlength(cap, font=f))
    except AttributeError:
        cw = size // 2 * len(cap)
    d.text((x1 - cw, y1 - th - size - 3), cap, fill=tuple(colour), font=f,
           stroke_width=max(1, size // 10), stroke_fill=(0, 0, 0))
    return np.asarray(pil)


# ---------------------------------------------------------------- driver
def build_composed(plate, wells_in, spec, out_dir=None, fps=20, bundled=True,
                   grid=None, gap=6, tp_start=None, tp_end=None, tp_step=None,
                   data_root=wh.DEFAULT_DATA_ROOT, smb_root=wh.DEFAULT_SMB_PROCESSED,
                   progress=None):
    """Render mp4(s) with the full Render spec. Returns ([Path], [note]).

    spec = {channels:{key:{mode,z,cmap}}, rotate, overlay, labels:{…}, ease}
    `bundled` tiles every well into ONE montage; otherwise one file per well.
    With overlay=False and several channels, each channel gets its own file."""
    pd, per_well, wells, on_smb = wh.resolve_wells(plate, wells_in, data_root, smb_root)
    if pd is None:
        return [], [f"no plate folder matching {plate!r}"]

    tps = sorted({tp for d in per_well.values() for (tp, _z) in d.get("bf", {})})
    if tp_start is not None:
        tps = [t for t in tps if t >= tp_start]
    if tp_end is not None:
        tps = [t for t in tps if t <= tp_end]
    if tp_step and tp_step > 1:
        tps = tps[::tp_step]
    if not tps:
        return [], [f"no timepoints in [{tp_start},{tp_end}]"]

    present = set().union(*(d.keys() for d in per_well.values()))
    keys = [k for k in (spec.get("channels") or {}) if k in present]
    keys = (["bf"] if "bf" in keys else []) + [k for k in keys if k != "bf"]
    if not keys:
        return [], [f"none of the requested channels exist here (have: "
                    f"{sorted(k for k in present if not k.endswith('_z'))})"]

    sample = _u8(wh._read(next(iter(per_well[wells[0]]["bf"].values()))))
    H, W = sample.shape[:2]

    import annotations as anno
    wann = anno.well_annotations(pd.name, screening_dir=pd)
    meta = anno.plate_meta(pd.name)
    lb = spec.get("labels") or {}
    um_px = None
    if lb.get("scalebar"):
        um_px = lb.get("um_per_px") or anno.pixel_size_um(pd.name)

    notes = []
    rends = {}
    for w in wells:
        r = WellRender(pd, w, per_well[w], tps, spec, H, W,
                       wann=wann.get(w, {}), cadence=meta.get("cadence_min"))
        rends[w] = r
        for n in r.notes:
            notes.append(f"{w}: {n}")

    lsize = int(lb.get("size") or max(11, round(H * 0.045)))
    lcorner = lb.get("corner") or "tl"
    lcol = _parse_colour(lb.get("colour") or lb.get("color") or "#ffffff")
    show_time = bool(lb.get("timepoint") or lb.get("time"))
    # Colour-ness must be decided ONCE for the whole movie: the encoder locks its pixel
    # format on the first frame, so a label that only appears at frame 50 (a stage that
    # starts blank, say) would otherwise turn a gray stream into RGB mid-write.
    rgb_out = (bool(spec.get("overlay")) and len(keys) > 1) or any(
        (c or {}).get("cmap") not in (None, "", "gray", "invert")
        for c in (spec.get("channels") or {}).values()) or bool(
        um_px or show_time or any(lb.get(k) for k in
                                  ("well", "plate", "stage", "rotation", "focus"))
        or (lb.get("columns") or []))

    def tile_of(w, tp, ks):
        t = rends[w].tile(tp, ks)
        lines = rends[w].tile_lines(tp)
        if bundled and show_time and lb.get("time_per_tile"):
            lines = lines + [time_label(tp, tps, meta.get("cadence_min"))]
        if lines:
            t = draw_lines(t, lines, lcorner, lsize, lcol)
        if um_px and (not bundled or lb.get("scalebar_per_tile")):
            t = draw_scalebar(t, um_px, lsize, lcol)
        return _rgb(t) if rgb_out else t

    def stamp(fr, tp):
        """The frame-wide overlays: one time stamp (opposite corner to the tile
        labels, so they never collide) and — on a montage — one shared scale bar.
        `tp` is passed in, never held in shared state: frames compose on a thread
        pool, so a mutable 'current timepoint' would race and mislabel frames."""
        if show_time and not lb.get("time_per_tile"):
            fr = draw_lines(fr, [time_label(tp, tps, meta.get("cadence_min"))],
                            _opposite(lcorner), lsize, lcol)
        if um_px and bundled and len(wells) > 1 and not lb.get("scalebar_per_tile"):
            fr = draw_scalebar(fr, um_px, lsize, lcol)
        return fr

    def montage_of(tp, ks):
        rows, cols = wh.grid_shape(len(wells), wh._parse_grid(grid))
        tiles = [tile_of(w, tp, ks) for w in wells]
        is_rgb = rgb_out or any(t.ndim == 3 for t in tiles)
        g = gap if len(wells) > 1 else 0
        Hc, Wc = rows * H + (rows - 1) * g, cols * W + (cols - 1) * g
        fr = np.zeros((Hc, Wc, 3) if is_rgb else (Hc, Wc), np.uint8)
        for i, t in enumerate(tiles):
            r, c = divmod(i, cols)
            fr[r * (H + g):r * (H + g) + H, c * (W + g):c * (W + g) + W] = _rgb(t) if is_rgb else t
        return stamp(fr, tp)

    def single_of(w, tp, ks):
        return stamp(tile_of(w, tp, ks), tp)

    dest = (Path(out_dir) if out_dir else wh._detailed_out(pd, wells, on_smb, data_root)[0])
    dest.mkdir(parents=True, exist_ok=True)
    nw = wh._workers(on_smb)
    groups = [keys] if spec.get("overlay") else [[k] for k in keys]
    units = (1 if bundled else len(wells)) * len(groups)
    if progress:
        progress(total=len(tps) * units,
                 phase=f"{pd.name}: composing {len(wells)} well(s) × {len(tps)} frames")

    written = []
    for ks in groups:
        chtag = "+".join(k.upper() for k in ks) if len(ks) > 1 else ks[0].upper()
        mtag = (spec.get("channels", {}).get(ks[0]) or {}).get("mode", "maxproj")
        if bundled:
            tag = "-".join(wells) if len(wells) <= 6 else f"{len(wells)}wells"
            o = dest / f"{pd.name}_{tag}_{chtag}_{mtag}.mp4"
            frames = wh._ordered_prefetch(tps, lambda t, _k=ks: montage_of(t, _k), nw, progress)
            if fc._encode(frames, o, fps):
                written.append(o)
        else:
            for w in wells:
                o = dest / f"{pd.name}_{w}_{chtag}_{mtag}.mp4"
                frames = wh._ordered_prefetch(
                    tps, lambda t, _w=w, _k=ks: single_of(_w, t, _k), nw, progress)
                if fc._encode(frames, o, fps):
                    written.append(o)
    for o in written:
        print(f"wrote {o}")
    return written, notes


def _opposite(corner):
    return {"tl": "tr", "tr": "tl", "bl": "br", "br": "bl"}.get(corner, "tr")


def _parse_colour(c):
    c = str(c or "#ffffff").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except (ValueError, IndexError):
        return (255, 255, 255)
