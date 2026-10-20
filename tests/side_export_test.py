#!/usr/bin/env python3
"""side_export_test.py — CSV/JSON side copies are opt-in and never land in the data root.

Every save used to write `<plate>.json` + `<plate>_annotations.csv` beside the plate
folders, because Settings.formats defaulted csv/json to ON and the destination was the
database's own folder. Pinned here:

  * defaults: nothing but the database is written;
  * switched on: both files land in `<root>/PlateNotate exports/annotations/`, and the
    root itself gains no .csv/.json;
  * an explicit `annotations_dir` still wins.

    python tests/side_export_test.py
"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import db_store  # noqa: E402
import server    # noqa: E402

passed = failed = 0


def check(cond, name):
    global passed, failed
    if cond:
        passed += 1; print(f"  ok   {name}")
    else:
        failed += 1; print(f"  FAIL {name}")


def files(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    dbp = root / "medaka.db"
    conn = db_store.open_db(dbp)
    server._DB.update({"conn": conn, "path": dbp, "folder": root, "needs_db": False})
    PLATE = "AQV99_test"

    server._load_settings = lambda: {k: (dict(v) if isinstance(v, dict) else v)
                                     for k, v in server._SETTINGS_DEFAULTS.items()}
    d = server._SETTINGS_DEFAULTS["formats"]
    check(d.get("csv") is False and d.get("json") is False, "defaults: csv and json are OFF")
    before = files(root)
    out = server._write_side_exports(PLATE)
    check(out == {} and files(root) == before, f"defaults: no side files written ({files(root)})")

    server._load_settings = lambda: {"annotations_dir": "", "formats": {"db": True, "csv": True, "json": True}}
    out = server._write_side_exports(PLATE)
    sub = root / "PlateNotate exports" / "annotations"
    check(set(out) == {"csv", "json"}, f"on: both formats reported {sorted(out)}")
    check(all(Path(p).parent == sub and Path(p).is_file() for p in out.values()),
          f"on: files land in {sub.relative_to(root)} -> {[Path(p).name for p in out.values()]}")
    root_junk = [p.name for p in root.iterdir() if p.suffix in (".csv", ".json")]
    check(root_junk == [], f"on: data root gained no csv/json ({root_junk})")

    other = root / "elsewhere"
    server._load_settings = lambda: {"annotations_dir": str(other), "formats": {"db": True, "csv": True, "json": False}}
    out = server._write_side_exports(PLATE)
    check(list(out) == ["csv"] and Path(out["csv"]).parent == other, f"explicit annotations_dir wins -> {out}")
    conn.close()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
