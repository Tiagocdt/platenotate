#!/usr/bin/env python
"""validate.py — check the tool's OUTPUT against laws that must hold.

    python tests/validate.py                 # synthetic data, no setup
    python tests/validate.py --data <root>   # your real plates

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
            ws = (man.get("wells") or [])[:2]
            if ws:
                out.append((pd.name, ws))
            if len(out) >= want:
                break
    except Exception:                                                  # noqa: BLE001
        pass
    return out


# ══════════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None, help="a folder of REAL plate folders")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="platenotate-validate-"))
    out = tmp / "out"
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
