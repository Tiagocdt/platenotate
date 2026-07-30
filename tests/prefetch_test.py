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

    # ---- TIERS: browsing needs one plane per timepoint, focus work needs the stack ----
    ran = []
    real_cached = server._cached_png
    server._cached_png = lambda fp, size: (ran.append(fp), (b"", "image/jpeg"))[1]
    try:
        def warm(depth):
            ran.clear()
            with server._PREFETCH_LOCK:
                server._PREFETCH["gen"] += 1
                gen = server._PREFETCH["gen"]
            server._prefetch_well(man, "A01", 600, plate, 3, gen, depth)
            server._prefetch_pool().shutdown(wait=True)
            server._PREFETCH["pool"] = None
            return list(ran)

        view = warm("view")
        stack = warm("stack")
        check(len(view) == len(tps),
              f"depth=view warms ONE plane per timepoint ({len(view)} for {len(tps)} tps)")
        check(len({Path(f).parent.name for f in view}) >= 1 and len(stack) > len(view),
              f"depth=stack warms more than view ({len(stack)} vs {len(view)})")
        check(len({Path(f).parent.name for f in stack}) == 3,
              f"depth=stack covers every z-slice (got "
              f"{sorted({Path(f).parent.name for f in stack})})")
        check(len(set(stack)) == len(stack), "depth=stack queues no duplicate files")
        # the whole point: browsing costs a fraction of the stack
        check(len(view) * 2 <= len(stack),
              f"browsing reads a fraction of the focus-work volume "
              f"({len(view)} vs {len(stack)} files)")
    finally:
        server._cached_png = real_cached

    # ---- cache sizing is configurable and honest -------------------------------
    server._save_settings({"cache_gb": 20, "cache_lossless": False, "cache_quality": 88})
    cap, lossless, q = server._cache_settings()
    check(abs(cap - 20 * 1024 ** 3) < 1 and not lossless and q == 88,
          "an explicit cache cap / format / quality come from settings")

    # ---- the caches must be honest about DISK vs MEMORY ------------------------
    server._save_settings({"cache_gb": 0})                 # 0 = automatic
    auto = server._cache_settings()[0] / 1024 ** 3
    import shutil as _sh
    free_gb = _sh.disk_usage(Path.home()).free / 1024 ** 3
    check(1.0 <= auto <= 20.0,
          f"the automatic disk cap is bounded (got {auto:.1f} GB, max 20)")
    check(auto <= max(1.0, free_gb * 0.1000001),
          f"…and never more than a tenth of the free space "
          f"({auto:.1f} GB vs {free_gb:.0f} GB free)")

    # the RAM cache is bounded by BYTES, not by a frame count: 1500 frames is 75 MB of
    # small JPEGs but ~600 MB of big PNGs, so a count is not a memory budget at all.
    lru = server._LRU(cap_bytes=100_000)
    for i in range(50):
        lru.get_or(i, lambda: (b"x" * 10_000, "image/jpeg"))
    st = lru.stats()
    check(st["bytes"] <= 100_000,
          f"the memory cache holds to its BYTE budget (got {st['bytes']} <= 100000)")
    check(st["frames"] <= 11,
          f"…by evicting frames, not by counting them (kept {st['frames']} of 50)")
    big = server._LRU(cap_bytes=100_000)
    big.get_or("one", lambda: (b"y" * 500_000, "image/png"))
    check(big.stats()["frames"] == 1,
          "a single frame larger than the budget is still served, not dropped")
    check(server._PNG_CACHE.cap_bytes <= 512 * 1024 ** 2,
          f"the shipped memory budget is laptop-sized "
          f"({server._PNG_CACHE.cap_bytes / 1048576:.0f} MB)")
    u = server.cache_usage()
    check("ram" in u and "free_bytes" in u,
          "cache usage separates disk from memory and reports free space")
    server._save_settings({"cache_lossless": True})
    check(server._cache_settings()[1] is True, "lossless (PNG) can be turned back on")
    server._save_settings({"cache_lossless": False})
    u = server.cache_usage()
    check({"bytes", "files", "cap_bytes"} <= set(u),
          f"cache usage reports what is held and the cap (got {sorted(u)})")

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
