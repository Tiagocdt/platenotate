#!/usr/bin/env python
"""console_test.py — printing must never be able to stop the app from starting.

A Windows console is cp1252 by default. Printing a character it cannot encode raises
UnicodeEncodeError from inside `print`, and in a windowed build that surfaces as
"Failed to execute script 'desktop'" with no app at all — which is exactly what a
single arrow in the launch banner did.

Two defences, both pinned here:
  * `make_console_safe()` switches the streams to UTF-8 (falling back to
    errors="replace"), so ANY message survives ANY console;
  * the launch banner itself is plain ASCII, because it runs before anything has
    proven the console is writable.

    python tests/console_test.py
"""
from __future__ import annotations
import io
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

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


# ---- the exact failure, reproduced on a cp1252 stream ------------------------
raw = io.BytesIO()
cp = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
try:
    cp.write("PlateNotate v1.3.0 (desktop)  →  http://127.0.0.1:8765/\n")
    cp.flush()
    check(False, "a cp1252 console rejects the old arrow banner")
except UnicodeEncodeError:
    check(True, "a cp1252 console rejects the old arrow banner (the Windows crash)")

# ---- make_console_safe() makes the same write survive ------------------------
raw2 = io.BytesIO()
cp2 = io.TextIOWrapper(raw2, encoding="cp1252", errors="strict")
real_out, real_err = sys.stdout, sys.stderr
try:
    sys.stdout = cp2
    sys.stderr = cp2
    server.make_console_safe()
    ok = True
    try:
        print("PlateNotate → … µm ⚠")     # arrow, ellipsis, micro, warning
        sys.stdout.flush()
    except UnicodeEncodeError:
        ok = False
finally:
    sys.stdout, sys.stderr = real_out, real_err
check(ok, "after make_console_safe() every character the app prints survives")

# ---- a stream that refuses reconfigure must not raise ------------------------


class Stubborn:
    def reconfigure(self, **kw):
        raise OSError("not reconfigurable")


try:
    sys.stdout, sys.stderr = Stubborn(), Stubborn()
    server.make_console_safe()
    survived = True
except Exception:                                          # noqa: BLE001
    survived = False
finally:
    sys.stdout, sys.stderr = real_out, real_err
check(survived, "a stream that refuses to reconfigure is left alone, not fatal")

sys.stdout, sys.stderr = real_out, real_err
try:
    sys.stdout, sys.stderr = None, None
    server.make_console_safe()
    survived = True
except Exception:                                          # noqa: BLE001
    survived = False
finally:
    sys.stdout, sys.stderr = real_out, real_err
check(survived, "no stdout at all (a windowed build) is handled")

# ---- the banner is ASCII, so it cannot fail before the fix even applies ------
# desktop.py emits through _say() (a print that cannot raise) as well as print(), so
# check both — a banner that moved behind a helper must not slip out of this net.
SAYS = r"\b(?:print|_say)\("
desktop_src = (HERE.parent / "desktop.py").read_text(encoding="utf-8")
banner_lines = [ln for ln in desktop_src.splitlines()
                if re.search(SAYS, ln) and "PlateNotate v" in ln]
check(bool(banner_lines), "the launch banner line was found")
check(all(ln.isascii() for ln in banner_lines),
      f"the launch banner is pure ASCII ({len(banner_lines)} line(s))")

# every line that emits text in desktop.py stays ASCII
non_ascii = [ln.strip() for ln in desktop_src.splitlines()
             if re.search(SAYS, ln) and not ln.isascii()]
check(not non_ascii, f"nothing desktop.py prints carries non-ASCII text ({non_ascii[:1]})")

# the message boxes go through user32's wide-character API, but the app also writes
# them to a log and to stderr, so keep them printable on a cp1252 console too.
dialog_text = re.findall(r"_message_box\((.*?)\n\s*(?:\)|\"|')", desktop_src, re.S)
check(all(t.isascii() for t in dialog_text),
      f"the error dialogs are ASCII ({len(dialog_text)} found)")

print(f"\nconsole_test: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
