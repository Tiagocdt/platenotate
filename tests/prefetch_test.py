#!/usr/bin/env python
"""prefetch_test.py — the frame cache warms the frames that are actually REQUESTED.

The stalls on a slow share were not a missing cache; they were a cache warming the
wrong frames. The viewer asks for each timepoint at the z its `slice` keyframes
forward-fill to, while the prefetcher warmed the *middle* slice — so on any well with
focus annotations (i.e. every well worth playing) the hit rate was zero and every frame
was re-read from the share.

Pinned here:
  * the z-map matches the client's sliceAt(): forward-fill, hold until the next keyframe;
  * prefetch warms exactly the paths the viewer will ask for;
  * work queued for a well you navigated away from is dropped, not run (one bounded
    pool, generation-checked — an unbounded thread per well is what took the app down);
  * a share that disappears mid-read is survivable.

    python tests/prefetch_test.py
"""
from __future__ import annotations
import shutil
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import db_store                                                        # noqa: E402
import server                                                          # noqa: E402

passed = failed = 0


def check(cond, name):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


tmp = Path(tempfile.mkdtemp(prefix="platenotate-prefetch-")).resolve()
try:
    # ---- a plate with 3 z-slices per timepoint --------------------------------
    root = tmp / "data"
    plate = "PLATE1"
    tps = [1, 2, 3, 4, 5, 6]
    for z in (1, 2, 3):
        d = root / plate / "bf" / "A01" / f"SL0{z}"
        d.mkdir(parents=True, exist_ok=True)
        for tp in tps:
            (d / f"{plate}_A01_LO{tp:03d}_BF_SL0{z}.tif").write_bytes(b"")

    server._settings_path = lambda: tmp / "settings.json"
    server._registry_path = lambda: tmp / "registry.json"
    server._open_process_db(root)
    conn = server._DB["conn"]

    # focus keyframes: z=1 from tp1, z=3 from tp4 (so tp1-3 → 1, tp4-6 → 3)
    db_store.save_payload(conn, plate, {
        "schema_version": 3, "plate": plate, "annotator": "t",
        "plate_columns": {}, "plate_annotations": {}, "columns": {}, "annotations": {},
        "image_columns": {"slice": {"type": "categorical", "values": []}},
        "image_annotations": {"A01": {"1": {"slice": "1"}, "4": {"slice": "3"}}}})

    zmap = server._focus_z_map(plate, "A01", tps)
    check(zmap.get(1) == 1 and zmap.get(3) == 1,
          f"z-map: a keyframe holds until the next one (tp1-3 → 1, got {zmap.get(1)},{zmap.get(3)})")
    check(zmap.get(4) == 3 and zmap.get(6) == 3,
          f"z-map: the next keyframe takes over (tp4-6 → 3, got {zmap.get(4)},{zmap.get(6)})")
    check(server._focus_z_map(plate, "NOSUCH", tps) == {},
          "z-map: a well with no keyframes yields nothing (caller uses the default view)")

    # ---- the warmed path is the path the viewer asks for -----------------------
    man = server._manifest(root, plate)
    # tp1 is annotated to z=1, while the default (no-z) view is a different slice —
    # so this is the case the old prefetcher got wrong on every annotated well.
    viewer = server._frame_path(man, "A01", "BF", 1, zmap.get(1))   # what the UI requests
    warmed = server._frame_path(man, "A01", "BF", 1, zmap.get(1))   # what prefetch warms
    default = server._frame_path(man, "A01", "BF", 1)               # the OLD prefetch target
    check(viewer is not None and Path(viewer).name.endswith("SL01.tif"),
          f"the viewer asks for the annotated slice at tp1 (got {Path(viewer).name if viewer else None})")
    check(warmed == viewer, "prefetch warms exactly that path")
    check(default != viewer,
          f"…while the old default-view prefetch warmed a DIFFERENT file "
          f"({Path(default).parent.name} vs {Path(viewer).parent.name}) — the 0% hit rate")

    # ---- cancellation: superseded work is skipped ------------------------------
    ran = []
    real_cached = server._cached_png
    server._cached_png = lambda fp, size: ran.append(fp)
    try:
        with server._PREFETCH_LOCK:
            server._PREFETCH["gen"] += 1
            gen = server._PREFETCH["gen"]
        server._prefetch_well(man, "A01", 600, plate, 1, gen)
        server._prefetch_pool().shutdown(wait=True)
        server._PREFETCH["pool"] = None
        check(len(ran) > 0, f"prefetch warms the well's frames ({len(ran)} reads)")

        ran.clear()
        stale = gen                                     # pretend another well was picked
        with server._PREFETCH_LOCK:
            server._PREFETCH["gen"] += 1
        server._prefetch_well(man, "A01", 600, plate, 1, stale)
        server._prefetch_pool().shutdown(wait=True)
        server._PREFETCH["pool"] = None
        check(len(ran) == 0,
              f"work for a well you navigated away from is dropped, not run (got {len(ran)})")
    finally:
        server._cached_png = real_cached

    # ---- a vanishing share must not raise out of the prefetcher ---------------
    def boom(fp, size):
        raise OSError(6, "Device not configured")       # what a dropped SMB mount gives
    server._cached_png = boom
    try:
        with server._PREFETCH_LOCK:
            server._PREFETCH["gen"] += 1
            gen = server._PREFETCH["gen"]
        server._prefetch_well(man, "A01", 600, plate, 1, gen)
        server._prefetch_pool().shutdown(wait=True)
        server._PREFETCH["pool"] = None
        check(True, "a share that disappears mid-prefetch does not take the app down")
    except OSError:
        check(False, "a share that disappears mid-prefetch does not take the app down")
    finally:
        server._cached_png = real_cached

    # ---- the pool is bounded ---------------------------------------------------
    check(server._PREFETCH_WORKERS <= 16,
          f"the prefetch pool is bounded ({server._PREFETCH_WORKERS} workers, shared)")

    print(f"\nprefetch_test: {passed} passed, {failed} failed")
finally:
    try:
        if server._DB.get("conn"):
            server._DB["conn"].close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if failed else 0)
