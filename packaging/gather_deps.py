"""Vendor the sibling tool modules Notate imports into packaging/_deps/ so the app is
self-contained to build (incl. on CI from an annotator-only checkout). Run after editing
the engine, then commit packaging/_deps/. No-op for files not present (CI uses the
committed copies)."""
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                  # label_annotator/
DEPS = HERE / "_deps"; DEPS.mkdir(exist_ok=True)
SOURCES = {
    ROOT.parent / "hyperstack_video": ["well_hyperstack.py", "focus_cut.py", "annotations.py", "compose.py"],
    ROOT.parent / "metadata_db": ["build_db.py"],
}
n = 0
for srcdir, files in SOURCES.items():
    for f in files:
        src = srcdir / f
        if src.exists():
            shutil.copy2(src, DEPS / f); n += 1
print(f"gathered {n} sibling module(s) into {DEPS}")
