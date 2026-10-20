#!/usr/bin/env python
"""manifest_cache_test.py — a plate that GREW on disk must not keep its old index.

`_MANIFEST_CACHE` was written once per plate path and never invalidated: no TTL,
no mtime check, no endpoint to clear it. So a plate that gained frames after the
server first indexed it kept serving the old timepoint list until somebody
restarted the process — silently, with nothing in the log and no error in the UI.

Reported as "it still shows only the 144 timepoints for half of the wells", hours
after a resumed crop run had written all 288 to disk.

Discovery globs every z-slice folder of every well, so it cannot run on the
per-frame image path. The rule pinned here is therefore: **opening a plate
re-checks the disk, serving a frame does not.**

  * a first open discovers;
  * re-opening an unchanged plate does NOT re-discover (the fingerprint holds);
  * the image path always uses the cache, even when the plate has changed;
  * re-opening a plate that gained frames DOES re-discover;
  * a new well under a channel counts as a change;
  * writing under processed/ (hyperstacks) does NOT — those are not frames.

    python tests/manifest_cache_test.py
"""
from __future__ import annotations
import sys
import tempfile
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import server                                                    # noqa: E402

root = pathlib.Path(tempfile.mkdtemp())
plate = root / "PLATE_X"
sl = plate / "CO6" / "B02" / "SL002"
sl.mkdir(parents=True)
(plate / "plate_metadata.json").write_text("{}")


def frames(a, b):
    for tp in range(a, b):
        (sl / f"PLATE_X_B02_LO{tp:03d}_CO6_SL002.tif").write_bytes(b"x")


frames(1, 4)

calls = {"n": 0}


def fake_discover(p):
    """Stand-in for model.discover_plate: counts calls, reports what is on disk."""
    calls["n"] += 1
    n = len(list((pathlib.Path(p) / "CO6" / "B02" / "SL002").glob("*.tif")))
    return {"plate_dir": str(p), "n_tp": n}


server.model.discover_plate = fake_discover

fails = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


m = server._manifest(root, "PLATE_X", revalidate=True)
check("first open discovers (3 timepoints)", m["n_tp"] == 3 and calls["n"] == 1)

server._manifest(root, "PLATE_X", revalidate=True)
check("re-open with no change does NOT re-discover", calls["n"] == 1)

m = server._manifest(root, "PLATE_X")
check("image path uses the cache", calls["n"] == 1 and m["n_tp"] == 3)

frames(4, 7)                       # the crop run resumed and wrote more timepoints

m = server._manifest(root, "PLATE_X")
check("image path stays cached (no stat on the hot path)",
      calls["n"] == 1 and m["n_tp"] == 3)

m = server._manifest(root, "PLATE_X", revalidate=True)
check("re-opening the plate picks up the new frames (6 timepoints)",
      m["n_tp"] == 6 and calls["n"] == 2)

(plate / "CO6" / "B03" / "SL002").mkdir(parents=True)
server._manifest(root, "PLATE_X", revalidate=True)
check("a new well re-discovers", calls["n"] == 3)

(plate / "processed" / "detailed" / "B02").mkdir(parents=True)
before = calls["n"]
server._manifest(root, "PLATE_X", revalidate=True)
check("writing under processed/ does not re-discover", calls["n"] == before)

print("FAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
