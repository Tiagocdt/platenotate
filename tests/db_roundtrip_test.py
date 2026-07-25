#!/usr/bin/env python3
"""db_roundtrip_test.py — proves the DB-FIRST annotator store (db_store.py).

Two checks:

  1. FRESH temp DB round-trip — save a v3 payload carrying a slice keyframe, a
     rotation (angle) keyframe, a measurement object, a well annotation, a plate
     annotation and a range; reload and assert everything comes back. Also assert
     image_annotation holds ONLY the keyframe rows (no interpolation is ever
     materialised) and the measurement went to the `measurement` table.

  2. REAL medaka.db — load AQV04_ctbp1-1-2_as2 and assert the egg_diameter
     measurements fold into image_annotations for the measured wells (A10/A11/A12)
     at tp "1". Opened READ-ONLY so the irreplaceable DB is never written.

Run (no pytest needed)::

    /Users/tiago/miniforge3/envs/twinnet/bin/python tests/db_roundtrip_test.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import db_store  # noqa: E402

REAL_DB = Path("/Users/tiago/metameda/imaging/data/medaka.db")
PLATE = "TEST_PLATE_v3"

# ------------------------------------------------------------------ tiny reporter
_passed = 0
_failed = 0


def check(cond, name):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}")


def _payload():
    """A v3 payload exercising every column type + both keyframe kinds."""
    return {
        "schema_version": 3,
        "plate": PLATE,
        "annotator": "tester",
        "plate_columns": {"notes": {"type": "free", "values": []}},
        "plate_annotations": {"notes": "round-trip plate note"},
        "columns": {
            "line": {"type": "categorical", "values": ["cab", "pfkfb3_her7v"]},
            "valid_frames": {"type": "range", "values": []},
        },
        "annotations": {
            "A01": {"line": "cab", "valid_frames": [3, 9]},
        },
        "image_columns": {
            "slice": {"type": "categorical", "values": ["1", "2", "3", "4", "5"],
                      "fill": "forward"},
            "rotation": {"type": "angle", "values": [], "fill": "interpolate"},
            "egg_diameter": {"type": "measurement", "values": []},
        },
        "image_annotations": {
            "A01": {
                "1": {"slice": "2", "rotation": -37.5,
                      "egg_diameter": {"line": [100.0, 50.0, 300.0, 250.0],
                                       "length_px": 282.84, "length_um": 460.2}},
                "6": {"slice": "5"},
                "8": {"rotation": 12.0},
            },
        },
    }


def run_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        # create_db + detect_db
        path = db_store.create_db(td, "annots")
        check(path.name == "annots.db", "create_db adds the .db suffix")
        found = db_store.detect_db(Path(td))
        check(found is not None and Path(found).resolve() == path.resolve(),
              "detect_db finds the freshly-created DB by its schema")

        conn = db_store.open_db(path)
        db_store.save_payload(conn, PLATE, _payload())
        p = db_store.load_payload(conn, PLATE)

        # --- keyframes: slice (exact string) + rotation (float-coerced) -----------
        ia = p["image_annotations"]["A01"]
        check(ia["1"]["slice"] == "2", "slice keyframe @tp1 round-trips ('2')")
        check(ia["6"]["slice"] == "5", "slice keyframe @tp6 round-trips ('5')")
        check(float(ia["1"]["rotation"]) == -37.5, "rotation keyframe @tp1 round-trips (-37.5)")
        check(float(ia["8"]["rotation"]) == 12.0, "rotation keyframe @tp8 round-trips (12.0)")

        # --- measurement folded back into image_annotations -----------------------
        m = ia["1"].get("egg_diameter")
        check(isinstance(m, dict) and m.get("line") == [100.0, 50.0, 300.0, 250.0],
              "measurement line[] folds into image_annotations")
        check(m is not None and abs(m["length_px"] - 282.84) < 1e-6
              and abs(m["length_um"] - 460.2) < 1e-6,
              "measurement length_px/length_um round-trip")

        # --- columns present with types + fill ------------------------------------
        ic = p["image_columns"]
        check(ic["slice"]["type"] == "categorical" and ic["slice"].get("fill") == "forward",
              "slice column: categorical + forward fill")
        check(ic["rotation"]["type"] == "angle" and ic["rotation"].get("fill") == "interpolate",
              "rotation column: angle + interpolate fill")
        check(ic["egg_diameter"]["type"] == "measurement",
              "egg_diameter column: measurement type")

        # --- well annotation + range + plate annotation ---------------------------
        check(p["annotations"]["A01"]["line"] == "cab", "well annotation round-trips (line=cab)")
        check(p["annotations"]["A01"]["valid_frames"] == [3, 9],
              "range round-trips as a list [3,9]")
        check(p["plate_annotations"]["notes"] == "round-trip plate note",
              "plate annotation round-trips")

        # --- KEYFRAMES ONLY: image_annotation has exactly the 4 rows we wrote ------
        n_ia = conn.execute("SELECT COUNT(*) FROM image_annotation WHERE plate_id=?",
                            (PLATE,)).fetchone()[0]
        check(n_ia == 4, f"image_annotation holds ONLY the 4 keyframe rows (got {n_ia})")
        n_leak = conn.execute(
            "SELECT COUNT(*) FROM image_annotation WHERE plate_id=? AND \"column\"='egg_diameter'",
            (PLATE,)).fetchone()[0]
        check(n_leak == 0, "measurement did NOT leak into image_annotation")

        # --- measurement landed in the measurement table --------------------------
        n_m = conn.execute("SELECT COUNT(*) FROM measurement WHERE plate_id=?",
                           (PLATE,)).fetchone()[0]
        check(n_m == 1, f"measurement landed in the measurement table (got {n_m})")

        conn.close()


def run_realdb():
    if not REAL_DB.is_file():
        check(False, f"real medaka.db present at {REAL_DB}")
        return
    found = db_store.detect_db(REAL_DB.parent)
    check(found is not None and Path(found).resolve() == REAL_DB.resolve(),
          "detect_db recognises the real medaka.db by schema")

    # READ-ONLY: load_payload only SELECTs, so the irreplaceable DB is never written
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    try:
        payload = db_store.load_payload(conn, "AQV04_ctbp1-1-2_as2")
    finally:
        conn.close()

    ia = payload["image_annotations"]
    for w in ("A10", "A11", "A12"):
        m = (ia.get(w, {}).get("1", {}) or {}).get("egg_diameter")
        check(isinstance(m, dict), f"AQV04 {w} @tp1 has an egg_diameter measurement object")
        if isinstance(m, dict):
            check(isinstance(m.get("line"), list) and len(m["line"]) == 4 and bool(m.get("length_um")),
                  f"AQV04 {w} egg_diameter has line[4] + length_um ({m.get('length_um')} um)")


# --------------------------------------------------------------- pytest wrappers
def test_roundtrip():
    n0 = _failed
    run_roundtrip()
    assert _failed == n0


def test_realdb():
    n0 = _failed
    run_realdb()
    assert _failed == n0


if __name__ == "__main__":
    print("== fresh temp DB round-trip ==")
    run_roundtrip()
    print("\n== real medaka.db (AQV04_ctbp1-1-2_as2) ==")
    run_realdb()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
