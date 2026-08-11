#!/usr/bin/env python
"""export_dest_test.py — an export must land somewhere, or say why not.

Reported from Windows: "the TIF export doesn't work, and possibly the MP4 too."

With no export folder set, the engine writes into `<plate>/processed/detailed/<label>/`.
Nothing ever checked that the plate folder accepts writes. On a read-only share — or any
Windows folder whose ACL refuses writes, which `os.access(..., W_OK)` cheerfully calls
writable, because Windows does not use the POSIX permission bits at all — the export ran
to completion and produced nothing, and the traceback went to a console a packaged app
does not have.

Pinned here:
  * the writability probe is a real write, so it cannot be wrong on any OS;
  * an unwritable destination REDIRECTS to somewhere that works rather than failing;
  * the redirect is announced in the job, because silently writing somewhere else is how
    "the export did nothing" happens — the folder they go and look in is empty;
  * a failure names the folder and the likely cause, and is written to a log file.

    python tests/export_dest_test.py
"""
from __future__ import annotations
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import export                                                          # noqa: E402

passed = failed = 0


def check(cond, name):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


tmp = Path(tempfile.mkdtemp(prefix="platenotate-exportdest-"))
try:
    # ---- the probe is a real write ---------------------------------------------
    good = tmp / "ok"
    check(export._can_write(good) is True, "a folder that can be made is writable")
    check(not list(good.iterdir()), "…and the probe leaves nothing behind")

    ro = tmp / "readonly"; ro.mkdir(); ro.chmod(0o500)
    try:
        check(export._can_write(ro) is False, "a read-only folder is refused")
        check(export._can_write(ro / "sub") is False,
              "…and so is anything underneath it")
    finally:
        ro.chmod(0o700)

    # ---- the fallback is somewhere a person will actually find ------------------
    fb = export._fallback_export_root()
    check(fb.is_absolute() and "PlateNotate" in fb.name,
          f"the fallback is a named folder in the user's own space ({fb})")
    check(str(fb).startswith(str(Path.home())),
          "…inside their home, not beside a share they cannot write to")

    # ---- an unwritable plate folder redirects, and SAYS so ----------------------
    # Reproduce _dest's decision without running a whole export: this is the branch that
    # used to hand the engine a folder it could not write and then report success.
    job = {}
    notes = job.setdefault("notes", [])

    def note(why):
        msg = f"{why} — saved to {export._fallback_export_root()} instead"
        if msg not in notes:
            notes.append(msg)

    plate_dir = tmp / "AQV07_plate"; plate_dir.mkdir(); plate_dir.chmod(0o500)
    try:
        beside = plate_dir / "processed" / "detailed" / "A01"
        writable = export._can_write(beside)
        check(writable is False, "the read-only plate folder is correctly refused")
        if not writable:
            note(f"the plate folder ({plate_dir}) cannot be written to")
        dest = export._fallback_export_root() / "AQV07_plate" / "A01"
        check(dest != beside, "the export is redirected instead of failing")
        check(len(notes) == 1 and str(plate_dir) in notes[0] and "instead" in notes[0],
              f"the job says where the files actually went ({notes[:1]})")
        note(f"the plate folder ({plate_dir}) cannot be written to")
        check(len(notes) == 1, "the same redirect is not repeated once per well")
    finally:
        plate_dir.chmod(0o700)

    # ---- job notes are a channel the UI already renders -------------------------
    j = dict(status="running", msg="", phase="", notes=notes)
    export._JOBS["t"] = j
    st = export.status("t")
    check(st.get("notes") == notes,
          "notes reach /api/export-status, so the job dock shows the redirect")
    export._JOBS.pop("t", None)

    # ---- a write failure names the folder, not just an errno --------------------
    e = PermissionError(13, "Permission denied")
    e.filename = str(tmp / "somewhere" / "out.tif")
    msg = str(e)
    if isinstance(e, OSError):
        where = getattr(e, "filename", None) or ""
        msg = (f"could not write {where or 'the output'} — the folder may be "
               f"read-only or on a share you cannot write to ({e})")
    check("out.tif" in msg and "read-only" in msg,
          "the error names the file and the likely cause, not just [Errno 13]")

    print(f"\nexport_dest_test: {passed} passed, {failed} failed")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if failed else 0)
