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
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Point ~ at a scratch dir BEFORE importing server. These tests write settings and create
# fallback databases, and the real ones live under ~/.medaka_annotator — a test that can
# rewrite the user's own annotations_dir is one bad line away from the "where did my
# database go" scare this very file exists to prevent.
_REAL_HOME = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
_FAKE_HOME = tempfile.mkdtemp(prefix="platenotate-dbtest-home-")
os.environ["HOME"] = os.environ["USERPROFILE"] = _FAKE_HOME

import db_store                                                        # noqa: E402
import server                                                          # noqa: E402

assert str(server._settings_path()).startswith(_FAKE_HOME), \
    "settings must be isolated from the real ones before any test runs"

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

    # ---- "can I write here?" must be ANSWERED, not guessed ---------------------
    # It used to be os.access(..., W_OK), which reads the POSIX permission bits. Windows
    # does not use them: it reports the read-only ATTRIBUTE and ignores the ACL actually
    # in force, so an unwritable folder answered "writable", the database was aimed
    # there, and the failure surfaced later as annotations that never saved.
    wr = tmp / "writable"; wr.mkdir()
    check(server.can_write(wr) is True, "a writable folder is reported writable")
    check(not list(wr.iterdir()), "…and the probe file is cleaned up, not left behind")
    nope = tmp / "nowrite"; nope.mkdir(); nope.chmod(0o500)
    try:
        check(server.can_write(nope) is False,
              "a folder that refuses writes is reported unwritable")
    finally:
        nope.chmod(0o700)
    check(server.can_write(tmp / "does" / "not" / "exist" / "yet") is True,
          "a folder that does not exist yet but CAN be made counts as writable")
    check(server.can_write("/proc/nonexistent-platenotate") is False,
          "an impossible path is refused rather than raising")

    # ---- a Windows share must be recognised as a share -------------------------
    # _is_network_fs shelled out to `mount`, which Windows does not have, so it raised
    # FileNotFoundError and returned False for EVERYTHING. Every Windows user on a share
    # was told it was local disk, and the database was created ON the share — where
    # SQLite's WAL is precisely what you must not use.
    real_platform = sys.platform
    try:
        sys.platform = "win32"
        check(server._is_network_fs(r"\\lab-server\plates\AQV07") is True,
              "a UNC path is a network share (it never was before)")
        check(server._is_network_fs("//lab-server/plates") is True,
              "…written either way round")
        ok = True
        try:
            server._is_network_fs(r"C:\Users\someone\plates")   # ctypes has no windll here
        except Exception:                                       # noqa: BLE001
            ok = False
        check(ok, "a drive path on a non-Windows test host is refused, not raised")
    finally:
        sys.platform = real_platform
    check(server._is_network_fs(Path.home()) is False,
          "a local folder is still local")

    # ---- and the user must be TOLD when it did not land with the images ---------
    # local_fallback used to report "was the source a network share?", which is a
    # different question and answered False for the case that actually bites: a folder
    # that is not a share but still refuses writes. The database then quietly lived
    # somewhere else with nothing on screen saying so.
    check(server._db_is_fallback(Path.home() / ".medaka_annotator" / "abc123" / "medaka.db"),
          "a database in the app's private corner is reported as a fallback")
    check(not server._db_is_fallback(tmp / "PROCESSED" / "medaka.db"),
          "…and one sitting with the images is not")

    unwritable = tmp / "ro_share"; unwritable.mkdir(); (unwritable / "AQV07").mkdir()
    unwritable.chmod(0o500)
    # No annotations_dir for this one: a folder the user CHOSE is a deliberate location,
    # not a fallback, and must not raise the warning. This case is the other one — nobody
    # chose anything and the images' folder said no.
    server._save_settings({"annotations_dir": ""})
    try:
        server._DB.update(conn=None, path=None, folder=None, needs_db=True,
                          local_fallback=False)
        server._open_process_db(unwritable)
        info = server._db_info()
        check(info["exists"] and not info["needs_db"],
              "an unwritable data root still gets a WORKING database")
        check(not str(info["db_path"]).startswith(str(unwritable)),
              "…which is not inside the folder that refused the write")
        check(info["local_fallback"] is True,
              "…and the UI is told, so the annotations are never silently elsewhere")
    finally:
        unwritable.chmod(0o700)
        try:
            if server._DB.get("conn"):
                server._DB["conn"].close()
        except Exception:                                  # noqa: BLE001
            pass

    print(f"\ndb_location_test: {passed} passed, {failed} failed")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(_FAKE_HOME, ignore_errors=True)
    for _k, _v in _REAL_HOME.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

sys.exit(1 if failed else 0)
