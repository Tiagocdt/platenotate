#!/usr/bin/env python
"""validate.py — check the tool's OUTPUT against laws that must hold.

    python tests/validate.py                 # synthetic data, no setup
    python tests/validate.py --data <root>   # your real plates
    python tests/validate.py --stacks <dir>  # a real folder of per-position hyperstacks

WHY THIS EXISTS
---------------
The unit tests answer "does the code run and return what I told it to return". Every bug
that actually reached Tiago got past them, because each one was SILENT: the code ran,
returned something plausible, and was wrong.

    * an average projection that came out BRIGHTER than the max it was made from
      (I had run it through a helper that full-range stretches float arrays)
    * annotation labels that rendered as nothing at all, because the composer was
      looking for the database at a path that does not exist outside the dev tree
    * exports that "succeeded" and wrote no file, on a folder that refused writes
    * a z fader that showed the previous slice's frames after buffering landed

No amount of "does it run" catches those. What catches them is a LAW — a statement that
must be true of the output no matter what the input is — checked against real bytes.

    mean projection <= max projection, at every pixel        (physics, not opinion)
    projecting ONE slice returns that slice, unchanged       (identity)
    a label you asked for is VISIBLE in the pixels           (not just "no error")
    the montage has one tile per well you selected           (counting)

Each check below states its law, prints the measured numbers, and fails loudly. The
numbers are printed even when the check passes, so you can read the evidence yourself
instead of taking "ok" on trust — which is the whole point.

EVERY CHECK HERE EXISTS BECAUSE SOMETHING BROKE. Adding one is how a bug gets paid for.
"""
from __future__ import annotations
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "packaging" / "_deps"))

import numpy as np                                                     # noqa: E402

passed = failed = 0
_LAW = None


def law(text):
    """Announce the law the next checks are testing."""
    global _LAW
    _LAW = text
    print(f"\n── {text}")


def check(cond, what, evidence=""):
    """One check. Prints the MEASUREMENT either way — a green tick with no number is
    exactly the kind of reassurance this file exists to replace."""
    global passed, failed
    mark = "ok  " if cond else "FAIL"
    if cond:
        passed += 1
    else:
        failed += 1
    print(f"   {mark} {what}" + (f"   [{evidence}]" if evidence else ""))
    return cond


# ────────────────────────────────────────────────────────────── a plate to work on
def synth_plate(root: Path, plate="20260101_VALIDATE_demo", wells=("A01", "A02", "A03"),
                tps=4, zs=3):
    """A plate whose CONTENT WE KNOW, so the laws can be checked against arithmetic
    rather than against a previous run of the same code (which would only prove it is
    consistently wrong)."""
    import tifffile
    pd = root / plate
    for w in wells:
        for z in range(1, zs + 1):
            d = pd / "bf" / w / f"SL{z:02d}"
            d.mkdir(parents=True, exist_ok=True)
            for tp in range(1, tps + 1):
                # each slice is a KNOWN flat value: slice z has value 40*z. The mean over
                # z is then exactly 40*(1+..+zs)/zs and the max exactly 40*zs — both
                # computable by hand, so the check does not depend on the code under test.
                a = np.full((32, 32), 40 * z, np.uint8)
                a[0, 0] = 250                          # one bright pixel, to catch clipping
                tifffile.imwrite(d / f"VALIDATE_{w}_LO{tp:03d}_BF_SL{z:02d}.tif", a)
    return pd


def real_plates(data_root: Path, want=2):
    """[(plate_folder_name, [wells])] from real data, or [] if there is none."""
    out = []
    try:
        import model
        for pd in sorted(p for p in Path(data_root).iterdir() if p.is_dir()):
            try:
                man = model.discover_plate(pd)
            except Exception:                                          # noqa: BLE001
                continue
            if man.get("layout") == "stacks":       # exports do not render stacks (yet)
                continue
            ws = (man.get("wells") or [])[:2]
            if ws:
                out.append((pd.name, ws))
            if len(out) >= want:
                break
    except Exception:                                                  # noqa: BLE001
        pass
    return out


# ══════════════════════════════════════════════════════════════════════════════════
# STACKS — a folder of per-position hyperstacks, read in place (stacks.py)
# ══════════════════════════════════════════════════════════════════════════════════
def _synth_stacks(root: Path):
    """Every TIFF layout the stack reader claims to read, with content we KNOW.

    The independent reader is tifffile: a plane is right if it equals what tifffile
    reads from the same file — not what our own code read last time."""
    import struct
    import tifffile
    rng = np.random.default_rng(11)
    root.mkdir(parents=True, exist_ok=True)
    files = {}
    a = rng.integers(0, 256, (5, 3, 2, 48, 64), dtype=np.uint8)
    f = root / "ij8.tif"
    tifffile.imwrite(f, a, imagej=True, metadata={"axes": "TZCYX"})
    files["ImageJ 8-bit (contiguous)"] = (f, a, "TZC")
    b = rng.integers(0, 4000, (4, 2, 3, 40, 30), dtype=np.uint16)
    f = root / "ij16be.tif"
    tifffile.imwrite(f, b, imagej=True, byteorder=">",
                     metadata={"axes": "TZCYX", "Ranges": (0., 1000., 0., 1000., 0., 1000.)})
    files["ImageJ 16-bit big-endian"] = (f, b, "TZC")
    f = root / "ij_one_header.tif"                 # the shape ImageJ writes past 4 GiB:
    tifffile.imwrite(f, a, imagej=True, metadata={"axes": "TZCYX"})   # ONE page header
    with open(f, "r+b") as fh:
        h = fh.read(8)
        bo = "<" if h[:2] == b"II" else ">"
        first = struct.unpack(bo + "I", h[4:8])[0]
        fh.seek(first)
        n = struct.unpack(bo + "H", fh.read(2))[0]
        fh.seek(first + 2 + n * 12)
        fh.write(struct.pack(bo + "I", 0))         # cut the chain after page 0
    files["ImageJ single page header (>4 GiB layout)"] = (f, a, "TZC")
    c = rng.integers(0, 256, (6, 2, 3, 70, 50), dtype=np.uint8)
    f = root / "pos_8bit.ome.tif"
    tifffile.imwrite(f, c, ome=True, bigtiff=True, compression="zlib", rowsperstrip=16,
                     metadata={"axes": "TZCYX", "Channel": {"Name": ["HyLight_395-510", "475_1", "Dia"]},
                               "PhysicalSizeX": 0.6447, "TimeIncrement": 300.0})
    files["OME BigTIFF, zlib strips (XYCZT)"] = (f, c, "TZC")
    d = rng.integers(0, 60000, (3, 2, 4, 37, 41), dtype=np.uint16)
    f = root / "tiled.ome.tif"
    tifffile.imwrite(f, d, ome=True, compression="zlib", predictor=True, tile=(16, 16),
                     metadata={"axes": "TCZYX"})
    files["OME tiled + predictor, other order (XYZCT)"] = (f, d, "TCZ")
    return files


def stack_laws(root: Path):
    import importlib
    import tifffile
    import stacks
    law("A frame read from a stack IS that plane of the file (tifffile, read independently).")
    stacks.set_index_dir(root / "index")
    files = _synth_stacks(root)
    for name, (f, arr, order) in files.items():
        sf = stacks.StackFile(f)
        T, Z, C = ((arr.shape[0], arr.shape[1], arr.shape[2]) if order == "TZC"
                   else (arr.shape[0], arr.shape[2], arr.shape[1]))
        n = bad = 0
        worst = 0
        for t in range(T):
            for z in range(Z):
                for ch in range(C):
                    want = arr[t, z, ch] if order == "TZC" else arr[t, ch, z]
                    got = sf.read_plane(t, z, ch)
                    n += 1
                    if not np.array_equal(got, want):
                        bad += 1
                        worst = max(worst, int(np.abs(got.astype(int) - want.astype(int)).max()))
        check(bad == 0 and (sf.T, sf.Z, sf.C) == (T, Z, C),
              f"{name}: every plane identical", f"{n} planes, {bad} differ (worst {worst}); "
              f"T{sf.T} Z{sf.Z} C{sf.C}, {sf.kind}, contiguous={sf.contiguous_at is not None}")

    law("Reading one frame reads ONLY that frame's bytes — never the stack.")
    sf = stacks.StackFile(files["ImageJ 8-bit (contiguous)"][0])
    stacks.READ_STATS.update(calls=0, bytes=0)
    sf.read_plane(4, 2, 1)
    check(stacks.READ_STATS["bytes"] == sf.plane_bytes,
          "contiguous ImageJ: one plane = exactly one plane of bytes",
          f"read {stacks.READ_STATS['bytes']} B in {stacks.READ_STATS['calls']} call(s); plane {sf.plane_bytes} B; "
          f"file {sf.size} B")
    f = files["OME BigTIFF, zlib strips (XYCZT)"][0]
    sf = stacks.StackFile(f)
    sf.read_plane(0, 0, 0)                          # index the chain first
    with sf._cond:
        sf._cond.wait_for(lambda: sf._complete, timeout=10)
    stacks.READ_STATS.update(calls=0, bytes=0)
    sf.read_plane(5, 1, 2)
    p = sf.plane_index(5, 1, 2)
    comp = sum(sf._cnts[p])
    check(stacks.READ_STATS["bytes"] <= comp + 65536 and stacks.READ_STATS["bytes"] < sf.size / 4,
          "compressed OME: one plane = that plane's own compressed strips",
          f"read {stacks.READ_STATS['bytes']} B; its strips {comp} B; file {sf.size} B")
    idx = list((root / "index").glob("*.json.gz"))
    sf2 = stacks.StackFile(f)
    sf2.start_index()
    check(bool(idx) and sf2._complete and len(sf2._offs) == sf.n_planes,
          "the page index is saved and reused (the chain is walked once per file)",
          f"{len(idx)} index file(s); reloaded {len(sf2._offs)} of {sf.n_planes} pages")

    law("One position at two resolutions is ONE well; levels that disagree stay apart.")
    import tifffile as tf_
    pd = root / "plate"
    pd.mkdir()
    rng = np.random.default_rng(5)
    full = rng.integers(0, 256, (4, 3, 2, 64, 64), dtype=np.uint8)
    base = "20261016_181713_463_Point0000_8bit"
    tf_.imwrite(pd / f"{base}.ome.tif", full, ome=True, bigtiff=True, compression="zlib",
                metadata={"axes": "TZCYX", "Channel": {"Name": ["ex", "Dia"]}, "PhysicalSizeX": 0.6447})
    small = np.rint(full.reshape(4, 3, 2, 16, 4, 16, 4).mean((4, 6))).astype(np.uint8)
    tf_.imwrite(pd / f"{base}_bin4.tif", small, imagej=True, metadata={"axes": "TZCYX"})
    odd = "20261016_181713_463_Point0001_8bit"
    tf_.imwrite(pd / f"{odd}.ome.tif", full, ome=True, compression="zlib",
                metadata={"axes": "TZCYX", "Channel": {"Name": ["ex", "Dia"]}})
    tf_.imwrite(pd / f"{odd}_bin4.tif", small[:3], imagej=True, metadata={"axes": "TZCYX"})
    import model
    man = model.discover_plate(pd)
    w0 = man["_well_index"].get(base, {}).get("stack")
    check(man["layout"] == "stacks" and w0 is not None and len(w0.levels) == 2,
          "full + _bin4 of one position -> one well with two levels",
          f"layout {man['layout']}; wells {man['wells']}")
    check(odd in man["wells"] and f"{odd}_bin4" in man["wells"]
          and len(man["_well_index"][odd]["stack"].levels) == 1,
          "a _bin4 with a different time axis is NOT merged into its namesake",
          f"{[w for w in man['wells'] if w.startswith(odd)]}")
    check(man["frames"][base]["Dia"] == [1, 2, 3, 4] and man["channel_z"]["Dia"] == [1, 2, 3]
          and man["native"][base] == [64, 64] and abs((man["px_um"] or 0) - 0.6447) < 1e-9,
          "the manifest reports the file's own axes, size and pixel size",
          f"t {man['frames'][base]['Dia']}, z {man['channel_z']['Dia']}, native {man['native'][base]}, "
          f"µm/px {man['px_um']}")
    lv, ex = w0.choose(16)
    lv2, _ = w0.choose(64)
    check((lv.X, lv2.X) == (16, 64), "a small request reads the binned level, a zoom the full one",
          f"16 px -> {lv.X} px level, 64 px -> {lv2.X} px level")

    law("Through the real server: the frame you are served is the plane, at any size.")
    home_before = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    fake_home = root / "home"
    fake_home.mkdir()
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(fake_home)   # never the real app-data
    try:
        import threading
        import urllib.request
        server = importlib.import_module("server")
        server._save_settings({"cache_lossless": True})
        server.Handler.data_root = pd
        server.Handler.log_message = lambda *a, **k: None      # keep the evidence readable
        httpd, port = server._serve("127.0.0.1", 8941)
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        bu = f"http://127.0.0.1:{port}"

        def get(u):
            with urllib.request.urlopen(bu + u, timeout=30) as r:
                return r.read(), dict(r.headers)
        import json as _json
        cfg = _json.loads(get("/api/config")[0])
        check([p["dir"] for p in cfg["plates"]] == ["plate"],
              "a folder of stacks opened DIRECTLY is offered as the plate", f"{cfg['plates']}")
        from PIL import Image
        import io as _io

        def frame(size, tp=3, z=2, ch="Dia", well=base, plate="plate"):
            b, h = get(f"/api/frame?dir={plate}&well={well}&ch={ch}&tp={tp}&z={z}&size={size}")
            return np.asarray(Image.open(_io.BytesIO(b))).astype(int), h
        sw = server._manifest(pd, "plate")["_well_index"][base]["stack"]
        fl = sw.full
        fl.start_index()                            # what opening the plate starts
        with fl._cond:
            fl._cond.wait_for(lambda: fl._complete, timeout=20)
        got, h = frame(64)
        check(got.shape == full[2, 1, 1].shape and np.array_equal(got, full[2, 1, 1]),
              "native size: the served frame == the plane",
              f"max |diff| {int(np.abs(got - full[2, 1, 1]).max())}; level {h.get('X-Frame-Level')}")
        got, h = frame(32)
        ref = full[2, 1, 1].reshape(32, 2, 32, 2).mean((1, 3))
        check(np.abs(got - ref).max() <= 0.5 + 1e-9,
              "downsampled: the AREA MEAN of the plane, not a sample of it",
              f"max |diff| vs 2x2 mean {np.abs(got - ref).max():.2f}")
        got, h = frame(16)
        check(h.get("X-Frame-Level") == "16x16" and np.array_equal(got, small[2, 1, 1]),
              "an overview frame comes from the binned level, unchanged",
              f"level {h.get('X-Frame-Level')}, max |diff| {int(np.abs(got - small[2, 1, 1]).max())}")
        # a stand-in is never kept: make the full level 'not indexed yet'
        real_ready, real_start = fl.page_ready, fl.start_index
        fl.page_ready, fl.start_index = (lambda p: False), (lambda: None)
        cache_dir = fake_home / ".medaka_annotator" / "framecache"
        before = sum(1 for _ in cache_dir.rglob("*.*")) if cache_dir.is_dir() else 0
        got, h = frame(64, tp=4)
        after = sum(1 for _ in cache_dir.rglob("*.*")) if cache_dir.is_dir() else 0
        check(h.get("X-Frame-Exact") == "0" and "no-store" in h.get("Cache-Control", "")
              and after == before,
              "while the full level is still indexing, the binned stand-in is labelled and NOT cached",
              f"exact={h.get('X-Frame-Exact')}, Cache-Control={h.get('Cache-Control')}, "
              f"cache files {before}->{after}")
        check(h.get("X-Frame-Level") == "16x16" and got.shape == (16, 16)
              and np.array_equal(got, small[3, 1, 1]),
              "…and the label is TRUE: the pixels sent are the level the header names",
              f"header {h.get('X-Frame-Level')}, pixels {got.shape[1]}x{got.shape[0]}")
        fl.page_ready, fl.start_index = real_ready, real_start
        got, h = frame(64, tp=4)
        check(h.get("X-Frame-Exact") == "1" and np.array_equal(got, full[3, 1, 1]),
              "…and once it is indexed, the same request returns the real full-resolution plane",
              f"exact={h.get('X-Frame-Exact')}, max |diff| {int(np.abs(got - full[3, 1, 1]).max())}")

        # one fixed display map for >8-bit: the same count shows the same grey at every t
        pd16 = root / "plate16"
        pd16.mkdir()
        v = np.zeros((2, 1, 1, 8, 8), np.uint16)
        v[0, 0, 0] = np.linspace(100, 200, 64).reshape(8, 8)
        v[1, 0, 0] = np.linspace(100, 5000, 64).reshape(8, 8)
        v[0, 0, 0, 0, 0] = v[1, 0, 0, 0, 0] = 150
        tf_.imwrite(pd16 / "DE_W0001_P0001.tif", v, imagej=True,
                    metadata={"axes": "TZCYX", "Ranges": (0.0, 1000.0)})
        server.Handler.data_root = pd16
        g1, _ = frame(8, tp=1, z=1, ch="C1", well="DE_W0001_P0001", plate="plate16")
        g2, _ = frame(8, tp=2, z=1, ch="C1", well="DE_W0001_P0001", plate="plate16")
        check(g1[0, 0] == g2[0, 0] == round(150 * 255 / 1000),
              "16-bit: one count -> one grey at every timepoint (no per-frame stretch)",
              f"count 150 -> {g1[0, 0]} at t1 (frame range 100-200) and {g2[0, 0]} at t2 (100-5000); "
              f"map 0-1000 from the file")
        httpd.shutdown()
        httpd.server_close()
    finally:
        for k, val in home_before.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        stacks.close_all()


def real_stack_laws(folder: Path, planes_per_well: int = 6):
    """The same laws on a REAL folder of stacks — tifffile as the independent reader."""
    import random
    import time
    import tifffile
    import stacks
    import model
    law(f"REAL stacks ({folder}): what is served is what is in the files.")
    stacks.set_index_dir(Path(tempfile.mkdtemp(prefix="pn-stackindex-")))
    t0 = time.time()
    man = model.discover_plate(folder)
    check(man["layout"] == "stacks" and man["wells"],
          "the folder is read as a stack plate",
          f"{len(man['wells'])} wells, channels {man['channels']}, "
          f"z {len(man['channel_z'].get(man['detect_channel']) or [])}, "
          f"t {man['n_frames'].get(man['wells'][0]) if man['wells'] else 0}, "
          f"native {man['native'].get(man['wells'][0]) if man['wells'] else None}, "
          f"µm/px {man.get('px_um')}, skipped {man.get('skipped')}, {time.time() - t0:.1f}s")
    rnd = random.Random(42)
    for w in man["wells"]:
        sw = man["_well_index"][w]["stack"]
        for lv in sw.levels:
            T, Z, C = lv.T, lv.Z, lv.C
            picks = [(0, 0, 0), (T - 1, Z - 1, C - 1)] + [
                (rnd.randrange(T), rnd.randrange(Z), rnd.randrange(C))
                for _ in range(planes_per_well - 2)]
            bad, worst, ms = 0, 0, []
            # a SMALL read buffer: Python sizes it from the filesystem's block size, which
            # on the cluster's scratch makes every one of tifffile's 16,000 page-header
            # hops read ~256 KB — minutes of I/O for the reference reader alone
            with tifffile.TiffFile(open(lv.path, "rb", buffering=8192)) as tf:
                for t, z, c in picks:
                    p = lv.plane_index(t, z, c)
                    if lv.contiguous_at is not None:
                        ref = tifffile.memmap(lv.path, mode="r")
                        want = np.asarray(ref.reshape(-1, lv.Y, lv.X)[p])
                    else:
                        want = tf.pages[lv.ifd_number(p)].asarray()
                    s0 = time.time()
                    got = lv.read_plane(t, z, c, timeout=600)
                    ms.append((time.time() - s0) * 1000)
                    if not np.array_equal(got, want):
                        bad += 1
                        worst = max(worst, int(np.abs(got.astype(int) - want.astype(int)).max()))
            check(bad == 0,
                  f"{man['well_labels'].get(w, w)} {lv.X}px ({lv.kind}): planes == tifffile",
                  f"{len(picks)} planes incl. first+last, {bad} differ (worst {worst}); "
                  f"read median {sorted(ms)[len(ms) // 2]:.0f} ms")
        if len(sw.levels) > 1:                    # the two levels must show the same data
            hi, lo = sw.levels[0], sw.levels[-1]
            k = hi.X // lo.X
            d = []
            for t, z, c in [(0, lo.Z // 2, 0), (lo.T // 2, lo.Z // 2, lo.C - 1), (lo.T - 1, 0, 0)]:
                a = hi.read_plane(t, z, c, timeout=600).astype(float)
                a = a.reshape(lo.Y, k, lo.X, k).mean((1, 3))
                b = lo.read_plane(t, z, c).astype(float)
                d.append(np.abs(a - b))
            dd = np.concatenate([x.ravel() for x in d])
            check(dd.mean() < 1.0,
                  f"{man['well_labels'].get(w, w)}: the {lo.X}px level is the {hi.X}px level "
                  f"binned {k}x{k}", f"mean |diff| {dd.mean():.3f} grey levels, max {dd.max():.0f}")


# ══════════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None, help="a folder of REAL plate folders")
    ap.add_argument("--stacks", default=None,
                    help="a REAL folder of per-position hyperstacks (one file = one well)")
    ap.add_argument("--stacks-only", action="store_true",
                    help="with --stacks: skip the synthetic export laws")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="platenotate-validate-"))
    out = tmp / "out"
    if args.stacks_only:                         # just the stack laws (cluster runs)
        try:
            stack_laws(tmp / "stacks")
            if args.stacks:
                real_stack_laws(Path(args.stacks))
            print(f"\n{'=' * 72}\nvalidate: {passed} checks passed, {failed} failed\n{'=' * 72}")
            return 1 if failed else 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    try:
        import compose
        import well_hyperstack as wh                                   # noqa: F401

        root = tmp / "plates"
        pd = synth_plate(root)
        ZS, SLICE_VALS = 3, [40, 80, 120]
        TRUE_MAX, TRUE_MEAN = max(SLICE_VALS), round(sum(SLICE_VALS) / len(SLICE_VALS))

        def render(mode, wells=("A01",), bundled=False, labels=None, groups=None,
                   data=None, first=None, wl=None):
            spec = {"channels": {"bf": {"mode": mode}}, "overlay": False,
                    "labels": labels or {}}
            d = out / f"{mode}-{len(wells)}-{bool(labels)}-{bool(groups)}"
            files, notes = compose.build_composed(
                first or pd.name, wl or list(wells), spec, out_dir=d, bundled=bundled,
                tp_start=1, tp_end=1, still=True, plate_groups=groups,
                data_root=data or root, smb_root=Path("/nonexistent"))
            return files, notes

        def px(f):
            from PIL import Image
            return np.asarray(Image.open(f).convert("L")).astype(float)

        # ── LAW 1 ────────────────────────────────────────────────────────────────
        law("A projection reports the data. It does not rescale it.")
        fmax, _ = render("maxproj")
        fmean, _ = render("meanproj")
        amax, amean = px(fmax[0]), px(fmean[0])
        # the flat region (everything but the one bright corner pixel)
        bmax, bmean = np.median(amax), np.median(amean)
        check(abs(bmax - TRUE_MAX) <= 1,
              f"max of slices {SLICE_VALS} is {TRUE_MAX}",
              f"measured {bmax:.0f}")
        check(abs(bmean - TRUE_MEAN) <= 1,
              f"mean of slices {SLICE_VALS} is {TRUE_MEAN}",
              f"measured {bmean:.0f}")
        check((amean <= amax + 1).all(),
              "mean is never brighter than the max, at ANY pixel",
              f"worst excess {float((amean - amax).max()):.1f}")
        # THE bug, stated as a law: a full-range stretch would push the flat region to 255
        check(bmean < 200,
              "…and is not silently stretched to fill the range",
              f"{bmean:.0f}, a stretch would give ~255")

        # ── LAW 2 ────────────────────────────────────────────────────────────────
        law("Projecting a single slice returns that slice, unchanged.")
        one = synth_plate(tmp / "one", plate="20260101_ONE_demo", wells=("A01",), zs=1)
        for mode in ("maxproj", "meanproj"):
            f, _ = render(mode, data=tmp / "one", first=one.name, wl=["A01"])
            v = np.median(px(f[0]))
            check(abs(v - 40) <= 1, f"{mode} of one 40-valued slice is 40",
                  f"measured {v:.0f}")

        # ── LAW 3 ────────────────────────────────────────────────────────────────
        law("A label you asked for is VISIBLE in the pixels.")
        # This is the label bug as a law. It rendered no error and no label; only
        # comparing the pixels against a no-label render can tell the difference.
        import sqlite3
        import db_store
        dbp = tmp / "v.db"
        db_store.create_db(tmp, "v")
        c = sqlite3.connect(dbp)
        c.execute("insert or ignore into annotator(annotator_id,name) values(1,'v')")
        c.execute('insert into well_annotation(plate_id,well,"column",value,annotator_id,'
                  "updated) values('VALIDATE_demo','A01','injected?','Yes',1,datetime('now'))")
        c.commit()
        c.close()
        os.environ["MEDAKA_DB"] = str(dbp)
        import importlib
        import annotations as anno
        importlib.reload(anno)
        importlib.reload(compose)

        seen = anno.well_annotations(pd.name)
        check(seen.get("A01", {}).get("injected?") == "Yes",
              "the composer can read the well annotation at all",
              f"{seen.get('A01', {})}")
        plain, _ = render("maxproj", labels={})
        lab, _ = render("maxproj", labels={"well": True, "columns": ["injected?"]})
        diff = float(np.abs(px(lab[0]) - px(plain[0])).sum())
        check(diff > 0, "asking for a label CHANGES the image", f"pixel difference {diff:.0f}")
        nolab, _ = render("maxproj", labels={"columns": ["does_not_exist"]})
        check(float(np.abs(px(nolab[0]) - px(plain[0])).sum()) == 0,
              "…and a column that does not exist changes nothing (no phantom text)")

        # ── LAW 4 ────────────────────────────────────────────────────────────────
        law("A montage holds one tile per well you selected — across plates.")
        pd2 = synth_plate(root, plate="20260202_VALIDATE_other", wells=("B01", "B02"))
        one_w, _ = render("maxproj", wells=("A01",), bundled=True)
        h1, w1 = px(one_w[0]).shape
        groups = [(pd.name, ["A01", "A02", "A03"]), (pd2.name, ["B01", "B02"])]
        cross, notes = compose.build_composed(
            pd.name, ["A01"], {"channels": {"bf": {"mode": "maxproj"}}, "labels": {}},
            out_dir=out / "cross", bundled=True, tp_start=1, tp_end=1, still=True,
            plate_groups=groups, data_root=root, smb_root=Path("/nonexistent"))
        check(len(cross) == 1, "a cross-plate montage is ONE file", f"{len(cross)} file(s)")
        if cross:
            H, W = px(cross[0]).shape
            # exact, not approximate: grid_shape(5) is 2 rows x 3 cols with a 6px gap, so
            # the montage must measure exactly that. "about the right size" is how a
            # missing tile hides.
            import well_hyperstack as _wh
            rows, cols = _wh.grid_shape(5)
            expH, expW = rows * h1 + (rows - 1) * 6, cols * w1 + (cols - 1) * 6
            check((H, W) == (expH, expW),
                  f"5 wells from 2 plates tile into exactly {rows}x{cols}",
                  f"measured {H}x{W}, expected {expH}x{expW} (tile {h1}x{w1})")
            check("plates" in cross[0].name, "the filename says it spans plates",
                  cross[0].name)
        check(not [n for n in notes if "no plate folder" in n],
              "every plate in the selection resolved", f"notes={notes}")

        # ── LAW 5 ────────────────────────────────────────────────────────────────
        law("An export that reports success has actually written a file.")
        for f in (fmax + fmean + cross):
            f = Path(f)
            check(f.is_file() and f.stat().st_size > 0,
                  f"{f.name} exists and is not empty",
                  f"{f.stat().st_size if f.is_file() else 0} bytes")

        # ── LAW 6 ────────────────────────────────────────────────────────────────
        law("Exports are written BESIDE the plates, never inside a plate folder.")
        import export as ex
        plate_dirs = [p for p in root.iterdir() if p.is_dir()]
        stray = [str(f) for p in plate_dirs for f in p.rglob("*.png")]
        check(not stray, "no export landed inside a plate folder",
              f"{len(stray)} stray file(s)")
        check(callable(ex._can_write), "the export path probes writability by WRITING")
        check(ex._can_write(tmp) and not ex._can_write(Path("/proc/nope")),
              "…and that probe is right in both directions")

        # ── LAW 7 (real data, when you point it at some) ──────────────────────────
        if args.data:
            law(f"The same laws hold on REAL data ({args.data}).")
            got = real_plates(Path(args.data), want=2)
            check(bool(got), "found real plates to test", f"{[g[0] for g in got]}")
            if got:
                rp, rw = got[0]
                rmax, _ = compose.build_composed(
                    rp, rw[:1], {"channels": {"bf": {"mode": "maxproj"}}, "labels": {}},
                    out_dir=out / "real-max", bundled=False, still=True,
                    data_root=Path(args.data), smb_root=Path("/nonexistent"))
                rmean, _ = compose.build_composed(
                    rp, rw[:1], {"channels": {"bf": {"mode": "meanproj"}}, "labels": {}},
                    out_dir=out / "real-mean", bundled=False, still=True,
                    data_root=Path(args.data), smb_root=Path("/nonexistent"))
                if rmax and rmean:
                    A, B = px(rmax[0]), px(rmean[0])
                    check((B <= A + 1).all(),
                          "REAL data: mean <= max at every pixel",
                          f"max mean-pixel {A.mean():.1f} vs mean {B.mean():.1f}")
                    check(B.mean() <= A.mean(),
                          "REAL data: the average projection is the dimmer one",
                          f"{B.mean():.1f} <= {A.mean():.1f}")
                if len(got) > 1:
                    xf, xn = compose.build_composed(
                        got[0][0], got[0][1],
                        {"channels": {"bf": {"mode": "maxproj"}}, "labels": {"well": True}},
                        out_dir=out / "real-cross", bundled=True, still=True,
                        plate_groups=[(g[0], g[1]) for g in got],
                        data_root=Path(args.data), smb_root=Path("/nonexistent"))
                    check(len(xf) == 1, "REAL data: cross-plate montage is one file",
                          f"{[Path(f).name for f in xf]}")

        stack_laws(tmp / "stacks")
        if args.stacks:
            real_stack_laws(Path(args.stacks))

        print(f"\n{'=' * 72}")
        print(f"validate: {passed} checks passed, {failed} failed")
        if failed:
            print("A FAILED LAW MEANS THE OUTPUT IS WRONG — not that a test is out of date.")
        print(f"{'=' * 72}")
        return 1 if failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
