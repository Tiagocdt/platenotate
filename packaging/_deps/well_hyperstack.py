#!/usr/bin/env python
"""well_hyperstack.py — export wells as Fiji hyperstacks OR movies (one tool).

SELECTION
    <plate> <well> [<well> …]         direct (wells space- or comma-separated)
    --from-json wells_filter.json     from the annotator's cross-plate filter:
                                      {"by_plate": {"<plate>": ["A01","B03",…], …}}

OUTPUT  (default = TIF hyperstack; --movie = mp4)
    (default)   one multi-dim TIF hyperstack — all z-slices + BF & FL channels.
                Several wells → a tiled montage; C/Z/T sliders are shared across
                every well (channel/focus/time are global, not per-well).
    --movie     an mp4. A movie shows ONE 2-D plane per frame, so pick the plane:
                  --slice N   a specific BF z-slice
                  --fl        the FL max-projection
                  (default)   each well's ANNOTATED best-focus slice — the
                              screening `slice` image-annotation — else middle BF.
                --fps N (default 20).  --all-slices = one movie per z-slice + FL.
    --per-well  one output per WELL instead of one montage.

LAYOUT (montage): --grid RxC, --gap PX.  Plate id is matched as a SUBSTRING of the
dated folder ('AQV06' → '20260624_AQV06_…'); falls back to the SMB mount if the
plate/well isn't pulled locally yet (output then saved locally).
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import tifffile as tf

try:
    import imagecodecs                      # pure-C TIF decode; releases the GIL end-to-end
except Exception:                           # pragma: no cover
    imagecodecs = None

DEFAULT_SMB_PROCESSED = "/Volumes/aulehla/Tiago/AQ-EMBL/PROCESSED"
# default data root = the "data" sibling of this tool's parent, resolved through any
# symlink — so the tool keeps working wherever the imaging folder is moved/renamed.
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "AQ-EMBL"


# ----------------------------------------------------------------- low-level I/O
def _gray(im):
    return im[..., 0] if im.ndim == 3 else im         # crops are RGB-triplicated


def _read(fp):
    """Decode one crop → 2-D array. `imagecodecs.tiff_decode` on the raw bytes is a
    pure-C path that holds no GIL for the whole decode, so it parallelises ~8× better
    than tifffile.imread (whose per-file tag parsing is Python/GIL-bound and caps a
    thread pool at ~2×). Falls back to tifffile for anything the fast path rejects."""
    if imagecodecs is not None:
        try:
            with open(fp, "rb") as fh:
                return _gray(imagecodecs.tiff_decode(fh.read()))
        except Exception:                   # multi-page / odd TIF → let tifffile handle it
            pass
    return _gray(tf.imread(fp))


def _fit(im, H, W):
    """Pad/crop a frame to (H, W) so mismatched tiles never break a montage."""
    if im.shape[:2] == (H, W):
        return im
    out = np.zeros((H, W), im.dtype)
    hh, ww = min(im.shape[0], H), min(im.shape[1], W)
    out[:hh, :ww] = im[:hh, :ww]
    return out


# --------------------------------------------------------- parallel read helpers
def _workers(on_smb=False):
    """Threads for the read pool. LZW decode releases the GIL, so local reads scale
    with cores; an SMB source is latency-bound, so oversubscribe to hide the round
    trips. Override with the HS_WORKERS env var."""
    env = os.environ.get("HS_WORKERS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    cpu = os.cpu_count() or 8
    return 24 if on_smb else max(2, min(12, cpu - 2))


def _ordered_prefetch(items, fn, workers, progress=None, lookahead=None):
    """Yield fn(item) for each item IN ORDER while keeping up to `lookahead` calls
    running on a thread pool — the costly TIF reads/decodes overlap, but the consumer
    (memmap write / ffmpeg pipe) still receives results one at a time, in order, on
    the calling thread. `progress(add=1)` fires as each item is handed off. This is
    the single biggest export speed-up: reads were the bottleneck, done one-at-a-time."""
    items = list(items)
    if not items:
        return
    lookahead = lookahead or max(workers * 3, 8)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        q = deque()
        i = 0
        while i < min(lookahead, len(items)):
            q.append(ex.submit(fn, items[i])); i += 1
        while q:
            r = q.popleft().result()
            if i < len(items):
                q.append(ex.submit(fn, items[i])); i += 1
            if progress:
                progress(add=1)
            yield r


def ffmpeg_exe():
    """Path to an ffmpeg binary. Preference: $ANNOTATOR_FFMPEG, the binary frozen INTO
    this app, then imageio-ffmpeg's own lookup, then the system PATH. The bundled build
    is what makes a packaged app portable — collaborators need no Homebrew/apt ffmpeg.

    The frozen-bundle path is checked FIRST, by plain file lookup: inside a packaged app
    the binary is right there next to us, and `get_ffmpeg_exe()` is free to go searching
    the system (or worse, the network) instead — which on a headless machine is a stall,
    not an error."""
    env = os.environ.get("ANNOTATOR_FFMPEG")
    if env and Path(env).exists():
        return env
    base = getattr(sys, "_MEIPASS", None)               # set only in a PyInstaller build
    if base:
        bindir = Path(base) / "imageio_ffmpeg" / "binaries"
        if bindir.is_dir():
            for p in sorted(bindir.glob("ffmpeg*")):
                if p.is_file():
                    return str(p)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                                   # noqa: BLE001
        return shutil.which("ffmpeg") or "ffmpeg"


def _resolve_channels(pd):
    """(detect_folder, detect_token, [(fl_folder, fl_token), …]) resolving BOTH
    layouts. Legacy: bf/ + fl/ (tokens BF/FL). New per-channel: <bf_channel>/ +
    the other channel folders (tokens = channel ids), from plate_metadata.json."""
    if (pd / "bf").is_dir():
        fls = [("fl", "FL")] if (pd / "fl").is_dir() else []
        return "bf", "BF", fls
    meta = {}
    mp = pd / "plate_metadata.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text())
        except Exception:
            pass
    chan_dirs = [d.name for d in sorted(pd.iterdir())
                 if d.is_dir() and next(d.glob("*/SL*"), None) is not None]
    bf_ch = meta.get("bf_channel") or (chan_dirs[0] if chan_dirs else "bf")
    fls = meta.get("fl_channels") or [c for c in chan_dirs if c != bf_ch]
    return bf_ch, bf_ch, [(c, c) for c in fls]


def _well_has_bf(plate_dir, well):
    dc, _, _ = _resolve_channels(plate_dir)
    d = plate_dir / dc / well
    return d.is_dir() and next(d.glob("*/*.tif"), None) is not None


def find_plate_dirs(plate, roots):
    """Processed-plate folders matching `plate` (exact or substring), roots in order."""
    found, tried = [], []
    for root in roots:
        root = Path(root)
        tried.append(str(root))
        if not root.is_dir():
            continue
        exact = root / plate
        if exact.is_dir() and exact not in found:
            found.append(exact)
        for p in sorted(root.glob(f"*{plate}*")):
            if p.is_dir() and p not in found:
                found.append(p)
    return found, tried


def collect_well(pd, well):
    """(bf{(tp,z):path}, fl{tp:path}, …) for one well — legacy bf/fl OR the new
    per-channel layout. BF = the detection channel per-slice. Each signal channel
    contributes one flat frame per timepoint: its loose max-projection (…_SLMX.tif)
    if present, else the middle z-slice. Keyed 'bf' + 'fl' (first signal channel)
    so the downstream movie/hyperstack builder is unchanged; extra channels keep
    their own key so a 3rd/4th channel isn't dropped."""
    dc, dtok, fls = _resolve_channels(pd)
    out = {}
    bf = {}
    for fp in (pd / dc / well).glob("*/*.tif"):
        m = re.search(rf"_LO(\d+)_{re.escape(dtok)}_SL0*(\d+)", fp.name)
        if m:
            bf[(int(m.group(1)), int(m.group(2)))] = fp
    if bf:
        out["bf"] = bf
    for i, (fl_folder, fl_token) in enumerate(fls):
        wd = pd / fl_folder / well
        if not wd.is_dir():
            continue
        key = "fl" if i == 0 else fl_folder.lower()
        # per-z index '<key>_z' {(tp,z): path} — lets the TIF hyperstack keep this
        # channel's REAL z-slices. Only the new per-channel layout has SL*/ subfolders
        # here; the legacy flat fl/ has none, so this stays empty (→ flat, as before).
        zmap = {}
        for fp in wd.glob("*/*.tif"):
            m = re.search(rf"_LO(\d+)_{re.escape(fl_token)}_SL0*(\d+)", fp.name)
            if m:
                zmap[(int(m.group(1)), int(m.group(2)))] = fp
        if zmap:
            out[key + "_z"] = zmap
        # flat frame per timepoint (for MOVIES, which show one 2-D plane): loose
        # max-projection if present, else the middle z-slice.
        frames = {}
        for fp in wd.glob("*_SLMX.tif"):           # loose max-projection
            m = re.search(r"_LO(\d+)", fp.name)
            if m:
                frames[int(m.group(1))] = fp
        if not frames:                              # fall back to middle z-slice
            sls = sorted(wd.glob("SL*"))
            mid = sls[len(sls) // 2] if sls else None
            if mid:
                for fp in mid.glob("*.tif"):
                    m = re.search(r"_LO(\d+)", fp.name)
                    if m:
                        frames[int(m.group(1))] = fp
        if not frames and zmap:                     # derive a flat middle-z from the per-z index
            zz = sorted({z for (_t, z) in zmap})
            midz = zz[len(zz) // 2]
            frames = {t: p for (t, z), p in zmap.items() if z == midz}
        if frames:
            out[key] = frames
    return out


def best_focus_slices(pd, wells):
    """{well: z} best-focus slice — the modal `slice` keyframe from medaka.db (the
    annotator's source of truth), screening-JSON fallback. {} if none."""
    import annotations as anno
    out = {}
    for w in wells:
        vals = [int(v) for (_tp, v) in anno.image_keyframes(pd.name, w, "slice", screening_dir=pd)
                if str(v).isdigit()]
        if vals:
            out[w] = Counter(vals).most_common(1)[0][0]
    return out


# ----------------------------------------------------------------- selection
def resolve_wells(plate, wells_in, data_root, smb_root):
    """Resolve a plate + well list to (plate_dir, {well:(bf,fl)}, [wells], on_smb).
    Returns (None, None, None, None) with a printed warning if nothing matched, so a
    batch over many plates skips the missing ones instead of aborting."""
    wells = []
    for tok in wells_in:                               # flatten space/comma, upper, de-dup
        for w in str(tok).replace(",", " ").split():
            w = w.strip().upper()
            if w and w not in wells:
                wells.append(w)

    data_root = Path(data_root)
    cands, tried = find_plate_dirs(plate, [data_root, smb_root])
    if not cands:
        print(f"  ! no plate folder matching {plate!r} under: {', '.join(tried)}", file=sys.stderr)
        return None, None, None, None
    pd = max(cands, key=lambda c: sum(_well_has_bf(c, w) for w in wells))
    if sum(_well_has_bf(pd, w) for w in wells) == 0:
        print(f"  ! none of {wells} have BF crops in {[c.name for c in cands]}", file=sys.stderr)
        return None, None, None, None
    on_smb = not str(pd.resolve()).startswith(str(data_root.resolve()))
    print(f"source: {pd}" + ("  [SMB mount — reads may be slow]" if on_smb else ""))

    per_well = {}
    for w in wells:
        if not _well_has_bf(pd, w):
            print(f"  ! well {w} not present in this source — skipping", file=sys.stderr)
            continue
        per_well[w] = collect_well(pd, w)
    wells = [w for w in wells if w in per_well]
    if not wells:
        print("  ! no requested wells had crops", file=sys.stderr)
        return None, None, None, None
    return pd, per_well, wells, on_smb


def _parse_grid(s):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)[xX](\d+)", s.strip())
    if not m:
        sys.exit(f"--grid must look like 2x2, got {s!r}")
    return int(m.group(1)), int(m.group(2))


def grid_shape(n, override=None):
    if override:
        r, c = override
        if r * c < n:
            sys.exit(f"--grid {r}x{c} has {r * c} cells < {n} wells")
        return r, c
    if n <= 3:
        return 1, n                                    # duo / triple = single row
    cols = math.ceil(math.sqrt(n))
    return math.ceil(n / cols), cols                   # near-square for 4+


def _detailed_out(pd, wells, on_smb, data_root):
    """Destination dir <plate>/processed/detailed/<label>/ (a local mirror if the
    plate is SMB-sourced), plus an annotations.txt describing the well(s) from the
    plate's screening JSON. Returns (dir, label)."""
    label = "-".join(wells) if len(wells) <= 6 else f"{len(wells)}wells"
    base = pd if not on_smb else (Path(data_root) / pd.name)
    dest = Path(base) / "processed" / "detailed" / label
    dest.mkdir(parents=True, exist_ok=True)
    ann = {}
    js = sorted(pd.glob("metadata/screening_*.json")) or sorted(pd.glob("screening_*.json"))
    if js:
        try:
            ann = json.load(open(js[0])).get("annotations", {})
        except Exception:
            pass
    note = [f"# plate: {pd.name}", f"# wells: {', '.join(wells)}", ""]
    for w in wells:
        a = ann.get(w, {})
        note.append(f"{w}: " + (", ".join(f"{k}={v}" for k, v in a.items()) if a else "(no annotations)"))
    (dest / "annotations.txt").write_text("\n".join(note) + "\n")
    return dest, label


# ------------------------------------------------- annotation-aware plane sources
def _annotation_tracks(pd, wells, tps, z_mode, rotate):
    """{well: {'focus': {tp: z}, 'rot': {tp: deg}}} — only what this export asks for.
    Imported lazily so a plain TIF export never touches the annotation DB."""
    if z_mode != "focus" and not rotate:
        return {}
    import compose
    import focus_cut as fc
    out = {}
    for w in wells:
        e = {}
        if z_mode == "focus":
            anc = fc._anchors(pd, w, "slice")
            if anc:
                e["focus"] = fc.build_focus_track(anc, tps)
            else:                                   # no keyframes → the modal best focus
                best = best_focus_slices(pd, [w]).get(w)
                if best is not None:
                    e["focus"] = {t: float(best) for t in tps}
        if rotate:
            anc = fc._anchors(pd, w, "rotation")
            if anc:
                e["rot"] = compose.angle_track(anc, tps)
        out[w] = e
    return out


def _collapse_src(idxmap, tp, zs_src, z_mode, focus, slices):
    """The read plan for ONE collapsed (well, channel, tp) plane, or None if the
    timepoint has no slices: ('max',[paths]) | ('blend',(a,b,frac)) | ('plane',path)."""
    have = sorted(z for (t, z) in idxmap if t == tp and (not zs_src or z in set(zs_src)))
    if not have:
        return None
    if z_mode == "maxproj":
        return ("max", [idxmap[(tp, z)] for z in have])
    if z_mode == "slice":
        want = (list(slices) or [have[len(have) // 2]])[0] if slices else have[len(have) // 2]
        return ("plane", idxmap[(tp, min(have, key=lambda z: abs(z - want)))])
    fz = (focus or {}).get(tp)                      # 'focus'
    if fz is None:
        return ("plane", idxmap[(tp, have[len(have) // 2])])
    fz = min(max(float(fz), have[0]), have[-1])
    z0 = int(fz)
    frac = fz - z0
    a = idxmap.get((tp, min(have, key=lambda z: abs(z - z0))))
    if frac < 1e-6:
        return ("plane", a)
    b = idxmap.get((tp, min(have, key=lambda z: abs(z - (z0 + 1)))))
    return ("blend", (a, b, frac)) if b is not None else ("plane", a)


def _read_src(src, H, W):
    """Decode one plane source into a 2-D array (see `_collapse_src`)."""
    kind, arg = src
    if kind == "plane":
        return _read(arg)
    if kind == "max":
        acc = None
        for p in arg:
            a = _fit(_read(p), H, W)
            acc = a if acc is None else np.maximum(acc, a)
        return acc
    a, b, frac = arg                                 # 'blend' — fractional focus
    pa = _fit(_read(a), H, W)
    pb = _fit(_read(b), H, W)
    out = pa.astype(np.float32) * (1 - frac) + pb.astype(np.float32) * frac
    return out.astype(pa.dtype)


# ----------------------------------------------------------------- TIF hyperstack
def build_hyperstack(plate, wells_in, out=None, grid=None, gap=6,
                     channels=("bf", "fl"), slices=None,
                     tp_start=None, tp_end=None, tp_step=None,
                     data_root=DEFAULT_DATA_ROOT, smb_root=DEFAULT_SMB_PROCESSED,
                     progress=None, z_mode="all", rotate=False):
    """One multi-dimensional TIF hyperstack (montage of the wells). Returns Path|None.
    channels = which of ('bf','fl') to include; slices = subset of z (else all);
    tp_start/tp_end/tp_step = timepoint window/stride (else all).

    z_mode collapses the Z axis, so the stack Fiji opens is the plane you meant:
      'all'      every z-slice, as acquired (default — unchanged behaviour)
      'maxproj'  Z=1, the max-projection over the z-slices of each timepoint
      'focus'    Z=1, the annotated focus track (fractional blend of two slices)
      'slice'    Z=1, the single z given in `slices`
    rotate=True turns every plane by the well's annotated rotation track, matching
    the viewer. Both write real pixels — this is data, so nothing is ever labelled."""
    pd, per_well, wells, on_smb = resolve_wells(plate, wells_in, data_root, smb_root)
    if pd is None:
        return None

    tps = sorted({tp for d in per_well.values() for (tp, _z) in d.get("bf", {})})
    if tp_start is not None:
        tps = [t for t in tps if t >= tp_start]
    if tp_end is not None:
        tps = [t for t in tps if t <= tp_end]
    if tp_step and tp_step > 1:
        tps = tps[::tp_step]
    # z-slices: the detection channel drives focus, but EVERY per-z channel contributes,
    # so a fluorescence channel keeps its real slices instead of one max-proj plane.
    zset_all = set()
    for d in per_well.values():
        zset_all.update(z for (_tp, z) in d.get("bf", {}))
        for k, v in d.items():
            if k.endswith("_z"):
                zset_all.update(z for (_tp, z) in v)
    zs = sorted(zset_all)
    if slices:
        zs = [z for z in zs if z in set(slices)]
    collapse = z_mode in ("maxproj", "focus", "slice")
    if collapse:
        zs_src, zs = zs, [zs[len(zs) // 2] if zs else 1]   # Z=1 out; zs_src = what to read
    # Keep only channels actually present, bf first. A channel with a '<c>_z' index is
    # written per-slice; one with only a flat frame is broadcast across Z (old FL).
    present = set().union(*(d.keys() for d in per_well.values())) if per_well else set()
    chans = [c for c in channels if c in present]
    chans = (["bf"] if "bf" in chans else []) + [c for c in chans if c != "bf"]
    if not (chans and zs and tps):
        print(f"  ! nothing to export (channels={list(channels)} slices={slices} "
              f"tps={len(tps)})", file=sys.stderr)
        return None
    ti_of = {tp: i for i, tp in enumerate(tps)}
    zi_of = {z: i for i, z in enumerate(zs)}
    ci_of = {c: i for i, c in enumerate(chans)}
    tpset, zset = set(tps), set(zs)
    T, Z, C, n = len(tps), len(zs), len(chans), len(wells)

    sample = _read(next(iter(next(iter(per_well.values()))["bf"].values())))
    H, W = sample.shape[:2]
    dtype = sample.dtype

    rows, cols = grid_shape(n, _parse_grid(grid))
    g = gap if n > 1 else 0
    Hc, Wc = rows * H + (rows - 1) * g, cols * W + (cols - 1) * g
    mb = T * Z * C * Hc * Wc / 1e6
    print(f"{plate}: {n} well(s) {wells} -> grid {rows}x{cols}, T={T} Z={Z} C={C} "
          f"({'+'.join(chans)}), tile {W}x{H} -> canvas {Wc}x{Hc}  (~{mb:.0f} MB)")
    if mb > 4000:
        print(f"  ! large output (~{mb / 1000:.1f} GB); consider fewer wells/timepoints", file=sys.stderr)

    label = "-".join(wells) if n <= 6 else f"{n}wells"
    base = f"{pd.name}_{label}_hyperstack.tif"
    if out:
        outp = Path(out)
        outp.parent.mkdir(parents=True, exist_ok=True)
    else:
        dest, _ = _detailed_out(pd, wells, on_smb, data_root)   # <plate>/processed/detailed/<label>/
        outp = dest / base

    # DISK-BACKED (memmap): even a multi-GB hyperstack is written straight to the
    # file and never lives in RAM — so a whole plate / many timepoints won't OOM.
    canvas = tf.memmap(str(outp), shape=(T, Z, C, Hc, Wc), dtype=dtype,
                       imagej=True, metadata={"axes": "TZCYX"})
    # Flat read plan: one entry per source TIF. A per-z channel (bf, or any signal
    # channel with a '<c>_z' index) → one plane per (tp,z); a flat-only channel (old FL)
    # → its single frame broadcast across all Z. Reads run on a thread pool (LZW decode
    # releases the GIL); each decoded plane is written into the memmap here on the
    # calling thread — disjoint slices, in order — so the file stays sound.
    # A job's source is one of: a single plane, the max over a timepoint's slices, or a
    # fractional blend of the two slices bracketing the annotated focus — so the same
    # read/write loop serves z_mode='all' and every collapsed mode.
    tracks = _annotation_tracks(pd, wells, tps, z_mode, rotate)
    jobs = []
    for idx, w in enumerate(wells):
        r, c = divmod(idx, cols)
        y0, x0 = r * (H + g), c * (W + g)
        chd = per_well[w]
        rot = (tracks.get(w) or {}).get("rot")
        for cname in chans:
            ci = ci_of[cname]
            zk = "bf" if cname == "bf" else cname + "_z"
            if zk in chd:                              # per-z channel → real z-slices
                if collapse:
                    idxmap = chd[zk]
                    for tp in tps:
                        src = _collapse_src(idxmap, tp, zs_src, z_mode,
                                            (tracks.get(w) or {}).get("focus"), slices)
                        if src:
                            jobs.append((src, ti_of[tp], ci, 0, y0, x0,
                                         (rot or {}).get(tp, 0.0)))
                    continue
                for (tp, z), p in chd[zk].items():
                    if tp in tpset and z in zset:
                        jobs.append((("plane", p), ti_of[tp], ci, zi_of[z], y0, x0,
                                     (rot or {}).get(tp, 0.0)))
            else:                                      # flat channel → broadcast across Z
                for tp, p in chd.get(cname, {}).items():
                    if tp in tpset:
                        jobs.append((("plane", p), ti_of[tp], ci,
                                     0 if collapse else None, y0, x0,
                                     (rot or {}).get(tp, 0.0)))
    nw = _workers(on_smb)
    if progress:
        progress(total=len(jobs),
                 phase=f"{pd.name}: reading {len(jobs)} planes · {n} well{'s' if n != 1 else ''}")

    def _load(job):
        src, ti, ci, zi, y0, x0, deg = job
        img = _fit(_read_src(src, H, W), H, W)
        if deg:
            import compose
            img = compose.rotate(img, deg)
        return (img, ti, ci, zi, y0, x0)

    for img, ti, ci, zi, y0, x0 in _ordered_prefetch(jobs, _load, nw, progress=progress):
        if zi is None:
            canvas[ti, :, ci, y0:y0 + H, x0:x0 + W] = img   # max-proj replicated over Z
        else:
            canvas[ti, zi, ci, y0:y0 + H, x0:x0 + W] = img
    canvas.flush()
    del canvas                                          # close the memmap
    if progress:
        progress(phase=f"wrote {outp.name}")
    print(f"wrote {outp}  [{nw} read threads]")
    return outp


# ----------------------------------------------------------------- movie
def _plane_label(kind):
    return f"BF_SL{kind[1]:02d}" if kind[0] == "bf" else kind[0].upper() + "_maxproj"


def _plane_path(chd, kind, tp):
    if kind[0] == "bf":
        return chd.get("bf", {}).get((tp, kind[1]))
    return chd.get(kind[0], {}).get(tp)


def _render(frames, out, fps):
    """Encode an iterable of equal-size 2-D uint8 frames to an mp4 via ffmpeg.
    Streams (low memory). Returns the output Path, or None on failure/empty."""
    it = iter(frames)
    first = next(it, None)
    if first is None:
        return None
    H, W = first.shape[:2]
    We, He = W + (W & 1), H + (H & 1)                  # libx264/yuv420p need even dims

    def _even(fr):
        if fr.shape[:2] == (He, We):
            return fr
        pad = np.zeros((He, We), np.uint8)
        pad[:H, :W] = fr[:H, :W]
        return pad

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(
        [ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{We}x{He}", "-r", str(fps), "-i", "-",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE)
    for fr in (first, *it):
        ff.stdin.write(np.ascontiguousarray(_even(fr), dtype=np.uint8).tobytes())
    ff.stdin.close()
    ff.wait()
    return out if ff.returncode == 0 else None


def build_video(plate, wells_in, out_dir=None, fps=20, slice_z=None, fl=False,
                all_slices=False, grid=None, gap=6, per_well=False,
                tp_start=None, tp_end=None, tp_step=None, channel=None,
                data_root=DEFAULT_DATA_ROOT, smb_root=DEFAULT_SMB_PROCESSED,
                progress=None):
    """Render mp4 movie(s) for the wells. One 2-D plane per frame. Returns [Path].
    tp_start/tp_end/tp_step limit the timepoints (else all)."""
    _ff = ffmpeg_exe()
    if not (Path(_ff).exists() or shutil.which(_ff)):
        sys.exit("ffmpeg not found — install ffmpeg, or `pip install imageio-ffmpeg`")
    pd, wells_data, wells, _on_smb = resolve_wells(plate, wells_in, data_root, smb_root)
    if pd is None:
        return []
    tps = sorted({tp for d in wells_data.values() for (tp, _z) in d.get("bf", {})})
    if tp_start is not None:
        tps = [t for t in tps if t >= tp_start]
    if tp_end is not None:
        tps = [t for t in tps if t <= tp_end]
    if tp_step and tp_step > 1:
        tps = tps[::tp_step]
    zs = sorted({z for d in wells_data.values() for (_tp, z) in d.get("bf", {})})
    mid_z = zs[len(zs) // 2]
    # default output → <plate>/processed/detailed/<well|label>/ (+ annotations.txt);
    # --out (out_dir) is an explicit override.
    def _odir(ws):
        if out_dir:
            d = Path(out_dir); d.mkdir(parents=True, exist_ok=True); return d
        return _detailed_out(pd, ws, _on_smb, data_root)[0]

    if slice_z is not None and slice_z not in zs:
        near = min(zs, key=lambda z: abs(z - slice_z))
        print(f"  ! slice {slice_z} absent (have {zs}); using nearest {near}", file=sys.stderr)
        slice_z = near

    # choose the plane kind for each well
    best = ({} if (fl or channel not in (None, "bf") or slice_z is not None)
            else best_focus_slices(pd, wells))

    def kind_for(w):
        if channel and channel != "bf":       # explicit named channel (e.g. a 3rd fluorescence)
            return (channel,)
        if fl:
            return ("fl",)
        if slice_z is not None:
            return ("bf", slice_z)
        return ("bf", best.get(w, mid_z))

    sample = _read(next(iter(wells_data[wells[0]]["bf"].values())))
    H, W = sample.shape[:2]
    written = []
    nw = _workers(_on_smb)

    def _load_plane(w, kind, t):
        p = _plane_path(wells_data[w], kind, t)
        return _fit(_read(p), H, W) if p is not None else np.zeros((H, W), np.uint8)

    def _tile_at(kinds, t):
        rows, cols = grid_shape(len(wells), _parse_grid(grid))
        g = gap if len(wells) > 1 else 0
        Hc, Wc = rows * H + (rows - 1) * g, cols * W + (cols - 1) * g
        canvas = np.zeros((Hc, Wc), np.uint8)
        for idx, w in enumerate(wells):
            r, c = divmod(idx, cols)
            canvas[r * (H + g):r * (H + g) + H, c * (W + g):c * (W + g) + W] = _load_plane(w, kinds[w], t)
        return canvas

    # ------- render according to mode (reads prefetched on a thread pool) -------
    if all_slices:                                     # one movie per z-slice + FL, per well
        extra = sorted(set().union(*(d.keys() for d in wells_data.values())) - {"bf"})
        planes = [("bf", z) for z in zs] + [(c,) for c in extra]
        if progress:
            progress(total=len(wells) * len(planes) * len(tps),
                     phase=f"{pd.name}: {len(wells)} well(s) × {len(planes)} plane(s), {len(tps)} frames")
        for w in wells:
            for k in planes:
                o = _odir([w]) / f"{pd.name}_{w}_{_plane_label(k)}.mp4"
                frames = _ordered_prefetch(tps, lambda t, _w=w, _k=k: _load_plane(_w, _k, t), nw, progress)
                if _render(frames, o, fps):
                    written.append(o)
        label = "all-slices per well"
    elif per_well:
        if progress:
            progress(total=len(wells) * len(tps),
                     phase=f"{pd.name}: {len(wells)} well(s), {len(tps)} frames each")
        for w in wells:
            k = kind_for(w)
            o = _odir([w]) / f"{pd.name}_{w}_{_plane_label(k)}.mp4"
            frames = _ordered_prefetch(tps, lambda t, _w=w, _k=k: _load_plane(_w, _k, t), nw, progress)
            if _render(frames, o, fps):
                written.append(o)
        label = "per well"
    else:                                              # montage (single mp4)
        kinds = {w: kind_for(w) for w in wells}
        labels = {_plane_label(k) for k in kinds.values()}
        mlabel = labels.pop() if len(labels) == 1 else "BF_bestfocus"
        rows, cols = grid_shape(len(wells), _parse_grid(grid))
        tag = "-".join(wells) if len(wells) <= 6 else f"{len(wells)}wells"
        o = _odir(wells) / f"{pd.name}_{tag}_{mlabel}.mp4"
        print(f"{plate}: {len(wells)} well(s) -> {rows}x{cols} montage movie, "
              f"plane={mlabel}, T={len(tps)} @ {fps}fps")
        if progress:
            progress(total=len(tps),
                     phase=f"{pd.name}: {len(wells)}-well montage, {len(tps)} frames")
        frames = _ordered_prefetch(tps, lambda t: _tile_at(kinds, t), nw, progress)
        if _render(frames, o, fps):
            written.append(o)
        label = "montage"

    for o in written:
        print(f"wrote {o}")
    print(f"{plate}: {len(written)} movie(s) [{label}] -> processed/detailed/")
    return written


# ----------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plate", nargs="?")
    ap.add_argument("wells", nargs="*", help="one or more wells (space- or comma-separated)")
    ap.add_argument("--from-json", help='annotator filter: {"by_plate": {plate: [wells]}}')
    ap.add_argument("--movie", action="store_true", help="render mp4(s) instead of a TIF hyperstack")
    ap.add_argument("--fps", type=int, default=20, help="movie frame rate (default 20)")
    ap.add_argument("--slice", type=int, dest="slice_z", help="movie: use this BF z-slice")
    ap.add_argument("--fl", action="store_true", help="movie: use the FL max-projection")
    ap.add_argument("--all-slices", action="store_true",
                    help="movie: one mp4 per z-slice + FL (per well)")
    ap.add_argument("--per-well", action="store_true", help="one output per WELL, not a montage")
    ap.add_argument("--z-mode", default="all", choices=["all", "maxproj", "focus", "slice"],
                    help="TIF: keep every z-slice (default), or collapse Z to a max "
                         "projection / the annotated focus track / the --slice plane")
    ap.add_argument("--rotate", action="store_true",
                    help="TIF: turn every plane by the annotated rotation track")
    ap.add_argument("--grid", help="force montage layout ROWSxCOLS, e.g. 2x2")
    ap.add_argument("--gap", type=int, default=6, help="black pixels between montage tiles")
    ap.add_argument("--out", help="output file (single TIF) or directory (movies)")
    ap.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    ap.add_argument("--smb-root", default=DEFAULT_SMB_PROCESSED,
                    help="server mount searched when the plate/well isn't under --data-root")
    a = ap.parse_args()

    # build the selection: {plate: [wells]}
    if a.from_json:
        by_plate = (json.load(open(a.from_json)).get("by_plate") or {})
        if not by_plate:
            sys.exit("--from-json: file has no 'by_plate' mapping")
        n = sum(len(v) for v in by_plate.values())
        print(f"{a.from_json}: {n} wells across {len(by_plate)} plate(s)")
    else:
        if not a.plate or not a.wells:
            sys.exit("give: <plate> <well> [<well> …]   (or --from-json FILE)")
        by_plate = {a.plate: a.wells}

    # KEYWORDS, not positions: both builders have optional args (channels/slices,
    # tp window, z_mode/rotate) between `gap` and the roots, so a positional call
    # silently lands --data-root in tp_start and dies on the first comparison.
    for plate, wells in by_plate.items():
        if a.movie:
            build_video(plate, wells, out_dir=a.out, fps=a.fps, slice_z=a.slice_z, fl=a.fl,
                        all_slices=a.all_slices, grid=a.grid, gap=a.gap, per_well=a.per_well,
                        data_root=a.data_root, smb_root=a.smb_root)
        elif a.per_well:
            flat = [w for tok in wells for w in str(tok).replace(",", " ").split()]
            for w in flat:
                build_hyperstack(plate, [w], grid=a.grid, gap=a.gap, z_mode=a.z_mode,
                                 rotate=a.rotate, slices=([a.slice_z] if a.slice_z else None),
                                 data_root=a.data_root, smb_root=a.smb_root)
        else:
            out = a.out if (not a.from_json) else None
            build_hyperstack(plate, wells, out=out, grid=a.grid, gap=a.gap, z_mode=a.z_mode,
                             rotate=a.rotate, slices=([a.slice_z] if a.slice_z else None),
                             data_root=a.data_root, smb_root=a.smb_root)


if __name__ == "__main__":
    main()
