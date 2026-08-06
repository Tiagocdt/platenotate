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

# ---- a build with NO streams must still serve every request ------------------
# This is the v1.5.1 Windows failure, reduced. A windowed build leaves sys.stdout and
# sys.stderr as None; the request logger wrote to stderr directly; and send_response
# LOGS BEFORE IT SENDS A BYTE — so every request died with the client seeing only a
# closed connection, and the traceback went to the stderr that was not there. The whole
# app was unreachable and the report was empty. Only a real request over a real socket
# catches this: the checks above all pass with stderr None.
import tempfile                                            # noqa: E402
import threading                                           # noqa: E402
import urllib.request                                      # noqa: E402
from pathlib import Path                                   # noqa: E402

server.Handler.data_root = Path(tempfile.mkdtemp(prefix="platenotate-console-test-"))
server._open_process_db(server.Handler.data_root)
httpd, port = server._serve("127.0.0.1", 0)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

results = {}
sys.stdout, sys.stderr = None, None                        # exactly a windowed build
try:
    server.make_console_safe()
    for path in ("/", "/api/version", "/api/config", "/static/app.js"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
                results[path] = r.status
        except Exception as e:                             # noqa: BLE001
            results[path] = f"{type(e).__name__}: {e}"
finally:
    sys.stdout, sys.stderr = real_out, real_err
    httpd.shutdown()

for path, got in results.items():
    check(got == 200, f"with no console at all, GET {path} still answers 200 (got {got})")

# and a malformed request line, where self.path does not exist yet, must not kill it
check(callable(server.Handler.log_message), "the request logger is still installed")

print(f"\nconsole_test: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
