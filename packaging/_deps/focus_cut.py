#!/usr/bin/env python
"""focus_cut.py — auto-cut a well's trajectory so it is ALWAYS in focus.

Reads the per-timepoint `slice` image-annotations (the best-focus z the annotator
marked in the label annotator) and builds a CONTINUOUS focus track over every
timepoint, EASING (smoothstep) across the gaps so a big focus change glides in
instead of jump-cutting. Renders two channels for the edit:

  • BF  — focus-tracked brightfield: at each frame the z is the (fractional) focus
          value, rendered as a blend of the two nearest z-slices → a focus-pull look.
  • FL  — the FL max-projection: gray by default (colour it live in Resolve) or
          baked to a single-hue tint / matplotlib colormap via --fl-cmap.

    python tools/hyperstack_video/focus_cut.py <plate> <well> [<well> …]
        [--fl-cmap gray|green|cyan|magenta|red|blue|yellow|orange|magma|viridis|…]
        [--ease smoothstep|linear|hold] [--fps 20] [--smooth 0] [--preview]

Outputs into <plate>/processed/detailed/<well>/:
  <plate>_<well>_BF_focustrack.mp4
  <plate>_<well>_FL_<cmap>.mp4
  <plate>_<well>_focustrack.csv        (tp, focus_z, iwamatsu_stage)  ← feeds staging
  <plate>_<well>_focuscut_preview.mp4  (--preview: BF | FL side-by-side)

The beat-synced BF fade / shutter is NOT baked here — these are clean channels for
DaVinci to composite (opacity keyframes on the BF track drive the fade/shutter).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

import well_hyperstack as wh          # reuse crop loaders (_read/_fit) + plate resolve

# single-hue tints: gray -> RGB weight (kept subtle so highlights don't clip to white)
TINTS = {
    "green": (0.15, 1.0, 0.30), "cyan": (0.0, 1.0, 1.0), "magenta": (1.0, 0.0, 1.0),
    "red": (1.0, 0.12, 0.12), "blue": (0.30, 0.45, 1.0), "yellow": (1.0, 0.95, 0.20),
    "orange": (1.0, 0.55, 0.05),
}


# ------------------------------------------------------------- annotations
def _screening(pd):
    js = sorted(pd.glob("metadata/screening_*.json")) or sorted(pd.glob("screening_*.json"))
    if not js:
        return {}
    try:
        return json.load(open(js[0]))
    except Exception:
        return {}


def _anchors(pd, well, col):
    """sorted [(tp, value)] for a per-image column, from medaka.db (JSON fallback).
    Reads the keyframes the annotator saved — slice (int), iwamatsu_stage (str),
    rotation (float degrees) — via the shared annotations accessor."""
    import annotations as anno
    out = []
    for t, x in anno.image_keyframes(pd.name, well, col, screening_dir=pd):
        if x is None or str(x).strip() == "":
            continue
        if col == "slice":
            if str(x).strip().isdigit():
                out.append((int(t), int(x)))
        elif col == "rotation":
            try:
                out.append((int(t), float(x)))
            except (TypeError, ValueError):
                pass
        else:
            out.append((int(t), str(x)))
    return sorted(out)


# ------------------------------------------------------------- focus track
def _smoothstep(u):
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3 - 2 * u)


def build_focus_track(anchors, tps, ease="smoothstep", smooth=0):
    """Continuous focus z for every tp, interpolated between (tp, z) anchors.
    ease='smoothstep' glides in & out of each anchor; 'linear' is a straight ramp;
    'hold' steps to the previous anchor (hard cuts). Ends are held flat."""
    if not anchors:
        return None
    at = [t for t, _ in anchors]
    az = [float(z) for _, z in anchors]
    f = {}
    for tp in tps:
        if tp <= at[0]:
            f[tp] = az[0]
        elif tp >= at[-1]:
            f[tp] = az[-1]
        else:
            i = int(np.searchsorted(at, tp, side="right")) - 1
            t0, t1, z0, z1 = at[i], at[i + 1], az[i], az[i + 1]
            if ease == "hold":
                f[tp] = z0
            else:
                u = (tp - t0) / (t1 - t0)
                w = _smoothstep(u) if ease == "smoothstep" else u
                f[tp] = z0 + (z1 - z0) * w
    if smooth and smooth >= 3 and smooth % 2 == 1:      # odd-window median denoise
        ts = sorted(tps)
        vals = np.array([f[t] for t in ts])
        half = smooth // 2
        sm = vals.copy()
        for k in range(len(vals)):
            sm[k] = np.median(vals[max(0, k - half):k + half + 1])
        f = {t: float(sm[k]) for k, t in enumerate(ts)}
    return f


def _forward_fill(tps, anchors):
    """value at each tp = the last anchor at or before it (for stage labels)."""
    st = sorted(anchors)
    out, i, cur = {}, 0, ""
    for tp in sorted(tps):
        while i < len(st) and st[i][0] <= tp:
            cur = st[i][1]
            i += 1
        out[tp] = cur
    return out


# ------------------------------------------------------------- frame builders
def _bf_at(bf, tp, z, zs, H, W):
    p = bf.get((tp, z))
    if p is None:                                        # nearest z present for this tp
        avail = [zz for zz in zs if (tp, zz) in bf]
        if not avail:
            return np.zeros((H, W), np.uint8)
        p = bf[(tp, min(avail, key=lambda zz: abs(zz - z)))]
    return wh._fit(wh._read(p), H, W)


def _bf_focus_frame(bf, tp, fz, zs, H, W):
    """brightfield at fractional focus fz — blend of the two nearest z-slices."""
    fz = min(max(fz, zs[0]), zs[-1])
    z0 = int(np.floor(fz))
    frac = fz - z0
    a = _bf_at(bf, tp, z0, zs, H, W)
    if frac < 1e-6:
        return a
    b = _bf_at(bf, tp, min(z0 + 1, zs[-1]), zs, H, W)
    return (a.astype(np.float32) * (1 - frac) + b.astype(np.float32) * frac).astype(np.uint8)


def _fl_frame(fl, tp, H, W, cmap):
    p = fl.get(tp)
    g = wh._fit(wh._read(p), H, W) if p is not None else np.zeros((H, W), np.uint8)
    if cmap == "gray":
        return g
    if cmap in TINTS:
        w = np.array(TINTS[cmap], np.float32)
        return np.clip(g[..., None].astype(np.float32) * w, 0, 255).astype(np.uint8)
    import matplotlib.cm as mcm                          # only when a named colormap is asked
    rgb = mcm.get_cmap(cmap)(g.astype(np.float32) / 255.0)[..., :3]
    return (rgb * 255).astype(np.uint8)


def _side_by_side(a, b, gap=6):
    def rgb(x):
        return x if x.ndim == 3 else np.repeat(x[..., None], 3, 2)
    a3, b3 = rgb(a), rgb(b)
    H = max(a3.shape[0], b3.shape[0])
    c = np.zeros((H, a3.shape[1] + gap + b3.shape[1], 3), np.uint8)
    c[:a3.shape[0], :a3.shape[1]] = a3
    c[:b3.shape[0], a3.shape[1] + gap:] = b3
    return c


# ------------------------------------------------------------- encoder (gray or RGB)
def _encode(frames, out, fps):
    it = iter(frames)
    first = next(it, None)
    if first is None:
        return None
    is_rgb = first.ndim == 3
    H, W = first.shape[:2]
    We, He = W + (W & 1), H + (H & 1)                    # yuv420p needs even dims

    def pad(fr):
        if fr.shape[:2] == (He, We):
            return fr
        z = np.zeros((He, We, 3) if is_rgb else (He, We), np.uint8)
        z[:H, :W] = fr[:H, :W]
        return z

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(
        [wh.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo",
         "-pix_fmt", "rgb24" if is_rgb else "gray", "-s", f"{We}x{He}", "-r", str(fps),
         "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE)
    for fr in (first, *it):
        ff.stdin.write(np.ascontiguousarray(pad(fr), dtype=np.uint8).tobytes())
    ff.stdin.close()
    ff.wait()
    return out if ff.returncode == 0 else None


# ------------------------------------------------------------- driver
def build_focus_cut(plate, well, fl_cmap="gray", ease="smoothstep", fps=20,
                    preview=False, smooth=0, tp_start=None, tp_end=None, label=None,
                    data_root=wh.DEFAULT_DATA_ROOT, smb_root=wh.DEFAULT_SMB_PROCESSED,
                    progress=None, out_dir=None):
    pd, per_well, wells, on_smb = wh.resolve_wells(plate, [well], data_root, smb_root)
    if pd is None:
        return []
    well = wells[0]
    bf, fl = per_well[well]
    tps = sorted({tp for (tp, _z) in bf})
    if tp_start is not None:
        tps = [t for t in tps if t >= tp_start]
    if tp_end is not None:
        tps = [t for t in tps if t <= tp_end]
    if not tps:
        print(f"  ! {well}: no timepoints in window [{tp_start},{tp_end}]", file=sys.stderr)
        return []
    tag = f"{well}_{label}" if label else well
    zs = sorted({z for (_tp, z) in bf})
    sample = wh._read(next(iter(bf.values())))
    H, W = sample.shape[:2]

    anchors = _anchors(pd, well, "slice")
    if not anchors:
        print(f"  ! {well}: no per-timepoint 'slice' anchors — cannot focus-track", file=sys.stderr)
        return []
    focus = build_focus_track(anchors, tps, ease, smooth)
    stages = _anchors(pd, well, "iwamatsu_stage")
    jumps = sum(1 for i in range(len(anchors) - 1) if abs(anchors[i + 1][1] - anchors[i][1]) >= 2)
    print(f"{plate}/{well}: {len(tps)} tp, {len(anchors)} focus anchors "
          f"(SL{anchors[0][1]}→SL{anchors[-1][1]}, {jumps} big jump(s)), ease={ease}, "
          f"FL={fl_cmap}" + (f", {len(stages)} stage anchors" if stages else ""))

    if out_dir:
        dest = Path(out_dir); dest.mkdir(parents=True, exist_ok=True)
    else:
        dest, _ = wh._detailed_out(pd, [well], on_smb, data_root)
    written = []
    nw = wh._workers(on_smb)
    if progress:
        progress(total=len(tps) * (3 if preview else 2),
                 phase=f"{pd.name}/{well}: focus-tracked render, {len(tps)} frames")

    bf_out = dest / f"{pd.name}_{tag}_BF_focustrack.mp4"
    bf_frames = wh._ordered_prefetch(
        tps, lambda tp: _bf_focus_frame(bf, tp, focus[tp], zs, H, W), nw, progress)
    if _encode(bf_frames, bf_out, fps):
        written.append(bf_out)

    fl_out = dest / f"{pd.name}_{tag}_FL_{fl_cmap}.mp4"
    fl_frames = wh._ordered_prefetch(
        tps, lambda tp: _fl_frame(fl, tp, H, W, fl_cmap), nw, progress)
    if _encode(fl_frames, fl_out, fps):
        written.append(fl_out)

    # sidecar: per-tp focus + forward-filled stage (also the stage-cut source of truth)
    csv = dest / f"{pd.name}_{tag}_focustrack.csv"
    sm = _forward_fill(tps, stages)
    csv.write_text("tp,focus_z,iwamatsu_stage\n" +
                   "".join(f"{tp},{focus[tp]:.3f},{sm.get(tp, '')}\n" for tp in tps))
    written.append(csv)

    if preview:
        pv_cmap = fl_cmap if fl_cmap != "gray" else "green"
        pv = dest / f"{pd.name}_{tag}_focuscut_preview.mp4"
        pv_frames = wh._ordered_prefetch(
            tps, lambda tp: _side_by_side(_bf_focus_frame(bf, tp, focus[tp], zs, H, W),
                                          _fl_frame(fl, tp, H, W, pv_cmap)), nw, progress)
        if _encode(pv_frames, pv, fps):
            written.append(pv)

    for o in written:
        print(f"wrote {o}")
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plate")
    ap.add_argument("wells", nargs="+", help="one or more wells (space- or comma-separated)")
    ap.add_argument("--fl-cmap", default="gray",
                    help="FL colour: gray (default), a tint (green/cyan/magenta/red/blue/"
                         "yellow/orange) or any matplotlib colormap (magma, viridis, …)")
    ap.add_argument("--ease", default="smoothstep", choices=["smoothstep", "linear", "hold"])
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--smooth", type=int, default=0,
                    help="odd-window median denoise of the focus track (e.g. 5); 0 = off")
    ap.add_argument("--preview", action="store_true", help="also write a BF|FL side-by-side mp4")
    ap.add_argument("--tp-start", type=int, help="window: first timepoint (for a detail cut)")
    ap.add_argument("--tp-end", type=int, help="window: last timepoint")
    ap.add_argument("--label", help="filename infix for a windowed clip, e.g. eye_st17-18")
    ap.add_argument("--data-root", default=str(wh.DEFAULT_DATA_ROOT))
    ap.add_argument("--smb-root", default=wh.DEFAULT_SMB_PROCESSED)
    a = ap.parse_args()

    wells = [w for tok in a.wells for w in str(tok).replace(",", " ").split()]
    for w in wells:
        build_focus_cut(a.plate, w.upper(), a.fl_cmap, a.ease, a.fps, a.preview,
                        a.smooth, a.tp_start, a.tp_end, a.label, a.data_root, a.smb_root)


if __name__ == "__main__":
    main()
