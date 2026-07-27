#!/usr/bin/env python
"""db_location_test.py — WHERE annotations get saved, and what a new folder inherits.

The rules being pinned here were all violated at once by a single bug report:

  * the database belongs **with the images** — opening a folder of plates uses the
    database in that folder, not the last folder you happened to save into;
  * opening a folder that has no database gives you a **genuinely new, empty** one —
    not the previous database's columns, and certainly not a copy of its contents;
  * changing the annotations folder never copies your database anywhere unless you
    explicitly ask (`copy_db`). A silent copy once put a 355 MB database holding every
    plate onto a colleague's shared server folder.

    python tests/db_location_test.py
"""
from __future__ import annotations
import json
import shutil
import sys
import tempfile
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


def make_plate(root: Path, plate="PLATE1", well="A01"):
    """The minimum on disk for the app to see one plate with one well."""
    d = root / plate / "bf" / well / "SL01"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{plate}_{well}_LO001_BF_SL01.tif").write_bytes(b"")
    return root / plate


def payload_with(plate, columns, annotations):
    p = {"schema_version": 3, "plate": plate, "annotator": "tester",
         "plate_columns": {}, "plate_annotations": {},
         "columns": columns, "annotations": annotations,
         "image_columns": {}, "image_annotations": {}}
    return p


# .resolve(): macOS hands out /var/... which resolves to /private/var/..., and the
# registry stores resolved paths — comparing the two forms would fail spuriously.
tmp = Path(tempfile.mkdtemp(prefix="platenotate-dbloc-")).resolve()
try:
    # ---------------------------------------------------------------- setup
    old_root = tmp / "old_data"                       # an established folder, with a DB
    new_root = tmp / "new_data"                       # a fresh folder, no DB anywhere
    make_plate(old_root, "OLDPLATE")
    make_plate(new_root, "NEWPLATE")
    settings_file = tmp / "settings.json"
    server._settings_path = lambda: settings_file      # keep the real user's settings safe
    server._registry_path = lambda: tmp / "registry.json"

    # the old folder gets a database with several columns and some annotations
    server._open_process_db(old_root)
    old_db = server._DB["path"]
    check(old_db is not None and Path(old_db).parent == old_root,
          "a new database is created IN the folder that holds the plates")
    db_store.save_payload(server._DB["conn"], "OLDPLATE", payload_with(
        "OLDPLATE",
        {"line": {"type": "categorical", "values": ["cab"]},
         "viability": {"type": "binary", "values": ["alive", "dead"]},
         "mixture": {"type": "categorical", "values": ["ctrl"]}},
        {"A01": {"line": "cab", "viability": "alive"}}))
    n_old = server._DB["conn"].execute("SELECT COUNT(*) FROM column_def").fetchone()[0]
    check(n_old == 3, f"the old database has its 3 columns (got {n_old})")

    # ------------------------------------------- the reported bug, before and after
    # Simulate what made it happen: the last-saved folder is remembered in settings.
    server._save_settings({"annotations_dir": str(old_root)})

    server._open_process_db(new_root)                  # open a DIFFERENT folder of plates
    new_db = server._DB["path"]
    check(Path(new_db) != Path(old_db),
          "opening another folder does NOT keep using the previous database")
    check(Path(new_db).parent == new_root,
          "the new folder's database is created inside that folder")

    cols = server._DB["conn"].execute("SELECT name FROM column_def").fetchall()
    check(len(cols) == 0,
          f"a brand-new database starts with NO columns (got {len(cols)}: "
          f"{[c[0] for c in cols]})")
    anns = server._DB["conn"].execute("SELECT COUNT(*) FROM well_annotation").fetchone()[0]
    check(anns == 0, f"…and no annotations (got {anns})")

    # annotate ONE column here — that must be the only thing this database contains
    db_store.save_payload(server._DB["conn"], "NEWPLATE", payload_with(
        "NEWPLATE", {"my_new_column": {"type": "categorical", "values": ["yes"]}},
        {"A01": {"my_new_column": "yes"}}))
    names = sorted(r[0] for r in
                   server._DB["conn"].execute("SELECT name FROM column_def"))
    check(names == ["my_new_column"],
          f"after one annotation the new database holds exactly that column (got {names})")

    # the old database is untouched by any of this
    oc = db_store.open_db(old_db)
    n = oc.execute("SELECT COUNT(*) FROM column_def").fetchone()[0]
    a = oc.execute("SELECT COUNT(*) FROM well_annotation").fetchone()[0]
    oc.close()
    check(n == 3 and a == 2, f"the original database is unchanged ({n} columns, {a} annotations)")

    # ------------------------------------------- an existing database is still found
    server._open_process_db(old_root)
    check(Path(server._DB["path"]) == Path(old_db),
          "reopening the old folder finds its own database again")
    n = server._DB["conn"].execute("SELECT COUNT(*) FROM column_def").fetchone()[0]
    check(n == 3, f"…with its own 3 columns, not the new folder's 1 (got {n})")

    # ------------------------------------------- an explicit registry link still wins
    linked_root = tmp / "linked_data"
    make_plate(linked_root, "LINKEDPLATE")
    server.link_root(linked_root, old_db)
    folder, found = server._resolve_db_location(linked_root)
    check(found is not None and Path(found) == Path(old_db),
          "an explicit registry link still points a folder at a shared database")

    # ------------------------------------------- annotations_dir is an override, not a default
    server._save_settings({"annotations_dir": str(old_root)})
    fresh_root = tmp / "fresh_data"
    make_plate(fresh_root, "FRESHPLATE")
    folder, found = server._resolve_db_location(fresh_root)
    check(Path(folder) == fresh_root and found is None,
          "a folder with no database of its own creates one THERE, not in annotations_dir")

    (fresh_root / "medaka.db").unlink(missing_ok=True)
    # annotations_dir is the fallback for a folder you CANNOT write to (a read-only
    # share), not an override of where the images live.
    only_dir = tmp / "central"
    only_dir.mkdir()
    db_store.create_db(only_dir, "medaka")
    server._save_settings({"annotations_dir": str(only_dir)})
    ro_root = tmp / "readonly_data"
    ro_root.mkdir()
    ro_root.chmod(0o500)                               # readable, not writable
    try:
        folder, found = server._resolve_db_location(ro_root)
        check(found is not None and Path(found).parent == only_dir,
              "annotations_dir IS used when the images' folder cannot be written to")
    finally:
        ro_root.chmod(0o700)

    print(f"\ndb_location_test: {passed} passed, {failed} failed")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if failed else 0)
