#!/usr/bin/env python
"""desktop.py — run the medaka annotator as a NATIVE DESKTOP WINDOW.

No browser tab, no run.sh: this starts the annotator's HTTP server on a loopback
port in a background thread, then wraps it in a pywebview window (WKWebView on macOS,
WebView2 on Windows, WebKitGTK on Linux). Everything the app does — annotate, filter,
and export with the top-right job dock — happens inside this one window. The in-app
"📂 Open" gets a native folder picker via the JS bridge below.

    python desktop.py                                     # default local data root
    python desktop.py /Volumes/aulehla/…/PROCESSED        # a server (SMB) folder

Deps:  pip install pywebview imageio-ffmpeg   (macOS pulls pyobjc automatically).
Package into a transferable .app / .exe with PyInstaller — see DESKTOP.md.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server   # noqa: E402  (the existing annotator server, imported not spawned)
import version  # noqa: E402

try:
    import webview  # pywebview
except Exception:  # pragma: no cover
    sys.exit("pywebview is not installed — run:  pip install pywebview\n"
             "(on macOS this also pulls pyobjc; on Windows it uses the Edge WebView2 runtime).")


class Bridge:
    """Exposed to the page as window.pywebview.api.* — lets the in-app '📂 Open'
    button raise a NATIVE folder dialog instead of typing a path."""

    def __init__(self):
        self.window = None

    def pick_folder(self):
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else str(res)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # --selftest must be handled HERE, before any window exists: this file is the frozen
    # app's entry point, so it is what CI can actually run, and webview.start() would
    # block forever on a machine with no display.
    if "--selftest" in argv:
        ok = server.selftest()
        # os._exit, not return: this is a throwaway check, and the GUI toolkit or a
        # lingering thread must never be able to hold the process open. On a headless
        # Windows runner a hung selftest is indistinguishable from a hung app.
        sys.stdout.flush() if sys.stdout else None
        sys.stderr.flush() if sys.stderr else None
        os._exit(0 if ok else 1)
    positional = [a for a in argv if not a.startswith("-")]
    data_root = (Path(positional[0]).expanduser().resolve() if positional
                 else server.default_data_root())

    # boot the server exactly like server.main(), but keep it in-process
    server.Handler.data_root = data_root
    server._open_process_db(data_root)                      # DB-first: one WAL conn
    httpd, port = server._serve("127.0.0.1", 8765)          # picks a free port if 8765 is busy
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    print(f"PlateNotate v{version.version()} (desktop)  →  {url}\n  data root: {data_root}", flush=True)

    bridge = Bridge()
    win = webview.create_window("PlateNotate", url,
                                width=1440, height=920, min_size=(1024, 720),
                                background_color="#14161b",   # match the app bg → no white flash on open
                                js_api=bridge)
    bridge.window = win
    try:
        webview.start()                                     # blocks in the GUI loop
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main() or 0)
