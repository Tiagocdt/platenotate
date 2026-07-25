"""export.py — background TIF / MP4 export JOBS for the annotator.

Wraps the hyperstack_video engine (well_hyperstack + focus_cut). A selection of
wells (possibly across plates) is grouped by plate and exported as either SINGLE
files (one per well) or a BUNDLED montage (grid placement auto by count). Supports
channel / z-slice / timepoint selection for TIF, and an "use annotated specs"
(focus_cut: best-focus slice + rotation from the DB) mode for MP4. Runs off-thread
so the browser never blocks; poll status, then download (a .zip if >1 file).
"""
from __future__ import annotations
import re
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# The engine lives in the sibling hyperstack_video/ in the dev tree; a standalone clone
# (or a frozen app) only has the vendored copies under packaging/_deps. Inserted in
# REVERSE so the dev tree ends up FIRST — otherwise a stale vendored copy shadows it.
for _p in (_HERE / "packaging" / "_deps", _HERE.parent / "hyperstack_video"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import well_hyperstack as wh          # noqa: E402
import focus_cut as fc                # noqa: E402
import compose                        # noqa: E402  (the Render-options composer)

# job_id -> {status, msg, phase, done, total, out, files, kind, label, t0, started}
_JOBS = {}


def _short(name):
    """AQVnn tag out of a dated plate folder, for compact job labels."""
    m = re.search(r"AQV\d+", name or "")
    return m.group(0) if m else (name or "?")[:14]


def _prune(keep=30):
    """Drop the oldest FINISHED jobs so the in-memory queue stays bounded."""
    if len(_JOBS) <= keep:
        return
    for k in list(_JOBS.keys())[:-keep]:
        if _JOBS[k].get("status") != "running":
            _JOBS.pop(k, None)


def _montage_mp4s(videos, out, fps):
    """Tile equal-size per-well mp4s into ONE grid montage (ffmpeg xstack)."""
    import math
    import shutil
    import subprocess
    videos = [str(v) for v in videos]
    n = len(videos)
    if n == 1:
        shutil.copy(videos[0], out)
        return Path(out)
    cols = math.ceil(math.sqrt(n))
    lay = []
    for i in range(n):
        r, c = divmod(i, cols)
        x = "0" if c == 0 else "+".join(["w0"] * c)
        y = "0" if r == 0 else "+".join(["h0"] * r)
        lay.append(f"{x}_{y}")
    cmd = [wh.ffmpeg_exe(), "-y", "-loglevel", "error"]
    for v in videos:
        cmd += ["-i", v]
    ins = "".join(f"[{i}:v]" for i in range(n))
    cmd += ["-filter_complex",
            f"{ins}xstack=inputs={n}:layout={'|'.join(lay)}:fill=black",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(fps), str(out)]
    r = subprocess.run(cmd, capture_output=True)
    return Path(out) if (r.returncode == 0 and Path(out).is_file()) else None


def _plate_dir(plate, wells, data_root, smb_root):
    """The processed-plate dir the builders will use, resolved cheaply (no per-well
    crop scan) so we can read its channel layout for label→key translation."""
    cands, _ = wh.find_plate_dirs(plate, [data_root, smb_root])
    if not cands:
        return None
    return max(cands, key=lambda c: sum(wh._well_has_bf(c, w) for w in wells))


def _channel_key_map(pd):
    """{annotator channel label (lowercased) → engine collect_well key}. The annotator
    names channels by folder/token (BF/FL, or CO6/CO2/CO4 in the new layout); the
    export engine re-keys them detection→'bf', first-signal→'fl', other-signals→
    <folder>.lower(). Both the folder name and the filename token are mapped so either
    form translates. Legacy bf/fl → identity."""
    dc, dtok, fls = wh._resolve_channels(pd)
    m = {dc.lower(): "bf", dtok.lower(): "bf"}
    for i, (fl_folder, fl_token) in enumerate(fls):
        key = "fl" if i == 0 else fl_folder.lower()
        m[fl_folder.lower()] = key
        m[fl_token.lower()] = key
    return m


def _translate_channels(channels, pd):
    """Map the annotator's channel labels to the engine's collect_well keys for THIS
    plate (identity for the legacy bf/fl layout). Unknown labels pass through. Order
    kept, de-duped — so an OLVAS export of CO6/CO2/CO4 becomes bf/fl/co4."""
    if pd is None:
        return tuple(dict.fromkeys(str(c).lower() for c in channels))
    m = _channel_key_map(pd)
    out = []
    for c in channels:
        k = m.get(str(c).lower(), str(c).lower())
        if k not in out:
            out.append(k)
    return tuple(out)


def _render_spec(spec, channels, pd):
    """The client's Render options → a compose.build_composed spec for THIS plate.

    The UI keys per-channel settings by the label it shows (BF / FL / CO6 …); the
    engine keys them by collect_well key, so translate here — in the plate's own
    layout — and keep only the channels the user actually ticked, in their order.
    Returns None when the export wants the plain (fast) raw-plane path."""
    r = dict(spec.get("render") or {})
    if not r and spec.get("use_annotations"):     # legacy checkbox → focus + rotation
        r = {"rotate": True, "channels": {c: {"mode": "focus"} for c in channels}}
    if not r:
        return None
    raw = r.get("channels") or {}
    lower = {str(k).lower(): v for k, v in raw.items()}
    m = _channel_key_map(pd) if pd is not None else {}
    per = {}
    for label in (spec.get("channels") or []):
        key = m.get(str(label).lower(), str(label).lower())
        cfg = dict(lower.get(str(label).lower()) or lower.get(key) or {})
        cfg.setdefault("mode", "focus" if key == "bf" and r.get("rotate") is None else "maxproj")
        per[key] = cfg
    if not per:
        per = {c: {"mode": "maxproj"} for c in channels}
    r["channels"] = per
    return r


def _needs_compose(spec):
    """Does this export need the composer at all? Only when the user asked for
    something raw planes can't give: an annotation track, a colour, an overlay, a
    label, or a z-mode other than 'as acquired'."""
    if spec.get("use_annotations"):
        return True
    r = spec.get("render") or {}
    if r.get("rotate") or r.get("overlay") or any((r.get("labels") or {}).values()):
        return True
    for c in (r.get("channels") or {}).values():
        if (c or {}).get("mode") in ("maxproj", "focus", "slice") or \
           (c or {}).get("cmap") not in (None, "", "gray"):
            return True
    return False


def _run(job_id, spec, data_root, smb_root):
    job = _JOBS[job_id]
    lock = threading.Lock()

    def prog(add=0, total=None, phase=None):
        """Progress sink for ONE build: the engine reports its plane/frame total once,
        then ticks add=1 per read. We track only the CURRENT build here; overall
        progress = (finished builds + this build's fraction) / total builds — so a
        many-well 'single files' export shows one smooth 0→100%, not 0→100% per well."""
        with lock:
            if total is not None:
                job["cur_total"] = int(total)
                job["cur_done"] = 0
            if phase is not None:
                job["phase"] = phase
                job["msg"] = phase
            if add:
                job["cur_done"] = job.get("cur_done", 0) + int(add)

    def build_done():
        """Mark one output build (a well/montage/channel) finished."""
        with lock:
            job["units_done"] = job.get("units_done", 0) + 1
            job["cur_total"] = 0
            job["cur_done"] = 0
    try:
        kind = spec.get("kind", "tif")
        bundled = bool(spec.get("bundled"))
        raw_channels = tuple(spec.get("channels") or ("bf", "fl"))   # annotator labels
        slices = spec.get("slices") or None
        ts, te, tstep = spec.get("tp_start"), spec.get("tp_end"), spec.get("tp_step")
        fps = int(spec.get("fps") or 20)
        use_anno = bool(spec.get("use_annotations"))
        edir = (spec.get("export_dir") or "").strip()   # Settings: where TIF/MP4 exports go

        def _dest(plate, label):                         # <export_dir>/<plate>/<label>/ | None
            return (Path(edir).expanduser() / plate / label) if edir else None

        render = spec.get("render") or {}
        composed = kind == "mp4" and _needs_compose(spec)

        by_plate = {}
        for w in (spec.get("wells") or []):
            by_plate.setdefault(w["plate"], []).append(w["well"])

        # resolve each plate's channels once, and count total output builds up front so
        # the overall bar is one smooth 0→100% across every well (not per-well).
        plate_channels, plate_render, n_builds = {}, {}, 0
        for plate, wells in by_plate.items():
            pdir = _plate_dir(plate, wells, data_root, smb_root)
            chans = _translate_channels(raw_channels, pdir)
            plate_channels[plate] = chans
            plate_render[plate] = _render_spec(spec, chans, pdir)
            if kind == "tif":
                n_builds += 1 if bundled else len(wells)
            elif composed:
                # ONE build per plate: the composer reports a single frame total that
                # already spans every well × channel group, so its fraction drives the
                # bar smoothly end-to-end (counting per file would make it stall).
                n_builds += 1
            elif use_anno:
                n_builds += len(wells)
            else:
                n_builds += max(1, len(chans))
        with lock:
            job["units_total"] = max(1, n_builds)
            job["units_done"] = 0

        outs = []
        for plate, wells in by_plate.items():
            channels = plate_channels[plate]
            prog(phase=f"{plate}: {len(wells)} well(s)…")
            if kind == "tif":
                groups = [wells] if bundled else [[w] for w in wells]
                for grp in groups:
                    out = None
                    if edir:
                        lbl = "-".join(grp) if len(grp) <= 6 else f"{len(grp)}wells"
                        out = _dest(plate, lbl) / f"{plate}_{lbl}_hyperstack.tif"
                    p = wh.build_hyperstack(plate, grp, out=out, channels=channels, slices=slices,
                                            tp_start=ts, tp_end=te, tp_step=tstep,
                                            data_root=data_root, smb_root=smb_root, progress=prog,
                                            z_mode=render.get("z_mode", "all"),
                                            rotate=bool(render.get("rotate")))
                    build_done()
                    if p:
                        outs.append(p)
            else:  # mp4
                vdir = _dest(plate, "montage" if bundled else "wells")   # None → engine default
                if composed:                                    # Render options → composer
                    vids, notes = compose.build_composed(
                        plate, wells, plate_render[plate], out_dir=vdir, fps=fps,
                        bundled=bundled, tp_start=ts, tp_end=te, tp_step=tstep,
                        data_root=data_root, smb_root=smb_root, progress=prog)
                    build_done()
                    outs += [Path(v) for v in vids]
                    with lock:                                  # surfaced in the job dock
                        job["notes"] = (job.get("notes") or []) + list(notes)
                elif use_anno:                                  # per-well annotated render
                    vids = []
                    for w in wells:
                        for v in fc.build_focus_cut(plate, w, fps=fps, tp_start=ts, tp_end=te,
                                                    out_dir=vdir, data_root=data_root,
                                                    smb_root=smb_root, progress=prog):
                            v = Path(v)
                            if v.suffix != ".mp4":              # drop the .csv sidecar
                                continue
                            if "FL_" in v.name and "fl" not in channels:
                                continue
                            if "BF_" in v.name and "bf" not in channels:
                                continue
                            vids.append(v)
                        build_done()                            # one focus-cut render = one build
                    if bundled and len(vids) > 1:               # tile into ONE montage
                        prog(phase="montaging tiles…")
                        m = _montage_mp4s(vids, vids[0].parent /
                                          f"{plate}_{'-'.join(wells)[:40]}_annotated_montage.mp4", fps)
                        outs.append(m or vids[0])
                    else:
                        outs += vids
                else:
                    vids = []
                    for ch in channels:                         # one movie per requested channel
                        vids += wh.build_video(plate, wells, out_dir=vdir, fps=fps, channel=ch,
                                               per_well=not bundled, tp_start=ts, tp_end=te,
                                               tp_step=tstep, data_root=data_root, smb_root=smb_root,
                                               progress=prog)
                        build_done()                            # one channel movie = one build
                    if bundled and len(vids) > 1:               # tile all wells×channels into one
                        prog(phase="montaging tiles…")
                        m = _montage_mp4s([Path(v) for v in vids], Path(vids[0]).parent /
                                          f"{plate}_{'-'.join(wells)[:40]}_montage.mp4", fps)
                        outs.append(m or vids[0])
                    else:
                        outs += vids

        outs = [Path(o) for o in outs if o]
        if not outs:
            job.update(status="error", msg="no output produced (check the selection)",
                       phase="no output")
            return
        if len(outs) == 1:
            job.update(status="done", msg="done", phase="done", out=str(outs[0]), files=outs)
        else:                                                   # >1 file → zip
            prog(phase=f"zipping {len(outs)} files…")
            z = outs[0].parent / f"export_{job_id[:8]}.zip"
            with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
                for o in outs:
                    zf.write(o, o.name)
            job.update(status="done", msg=f"{len(outs)} files", phase=f"done · {len(outs)} files",
                       out=str(z), files=outs)
    except Exception as e:                                      # noqa: BLE001
        import traceback
        traceback.print_exc()
        job.update(status="error", msg=str(e), phase=f"error: {e}")


def start(spec, data_root, smb_root):
    _prune()
    job_id = uuid.uuid4().hex
    kind = spec.get("kind", "tif")
    wells = spec.get("wells") or []
    plates = sorted({w.get("plate", "") for w in wells})
    plabel = _short(plates[0]) if len(plates) == 1 else f"{len(plates)} plates"
    label = (f"{kind.upper()} · {len(wells)} well{'s' if len(wells) != 1 else ''} · {plabel}"
             + (" · montage" if spec.get("bundled") else ""))
    _JOBS[job_id] = {"status": "running", "msg": "starting…", "phase": "starting…",
                     "units_total": 1, "units_done": 0, "cur_total": 0, "cur_done": 0,
                     "out": None, "files": [],
                     "kind": kind, "label": label, "bundled": bool(spec.get("bundled")),
                     "t0": time.monotonic(), "started": time.time()}
    threading.Thread(target=_run, args=(job_id, spec, data_root, smb_root), daemon=True).start()
    return job_id


def status(job_id):
    j = _JOBS.get(job_id)
    if not j:
        return {"status": "unknown"}
    out = j.get("out")
    size = None
    try:
        if out and Path(out).is_file():
            size = Path(out).stat().st_size
    except OSError:
        pass
    # overall progress = (finished builds + current build's fraction) / total builds
    ut = j.get("units_total", 1) or 1
    ud = j.get("units_done", 0)
    ct, cd = j.get("cur_total", 0), j.get("cur_done", 0)
    frac = (cd / ct) if ct else 0.0
    done, total = ud, ut                                        # report builds for a "3/24" readout
    running = j["status"] == "running"
    elapsed = time.monotonic() - j.get("t0", time.monotonic())
    pct = 1.0 if j["status"] == "done" else min(0.999, (ud + frac) / ut)
    eta = (elapsed * (1 - pct) / pct) if (running and pct > 0.02) else None
    return {"status": j["status"], "msg": j["msg"], "phase": j.get("phase"),
            "done": done, "total": total, "pct": pct, "elapsed": elapsed, "eta": eta,
            "kind": j.get("kind"), "label": j.get("label"), "started": j.get("started"),
            "out": out, "name": (Path(out).name if out else None),
            "count": len(j.get("files") or []), "size": size,
            "notes": j.get("notes") or []}   # what the composer actually found per well


def all_jobs(limit=25):
    """Every known job, newest first — drives the top-right job dock."""
    ids = list(_JOBS.keys())[-limit:]
    return [dict(status(i), id=i) for i in reversed(ids)]


def output_path(job_id):
    j = _JOBS.get(job_id)
    return Path(j["out"]) if (j and j.get("out")) else None
