#!/usr/bin/env python
"""desktop.py — run the medaka annotator as a NATIVE DESKTOP WINDOW.

No browser tab, no run.sh: this starts the annotator's HTTP server on a loopback
port in a background thread, then wraps it in a pywebview window (WKWebView on macOS,
WebView2 on Windows, WebKitGTK on Linux). Everything the app does — annotate, filter,
and export with the top-right job dock — happens inside this one window. The in-app
"📂 Open" gets a native folder picker via the JS bridge below.

    python desktop.py                                     # default local data root
    python desktop.py /Volumes/aulehla/…/PROCESSED        # a server (SMB) folder

When the native window CANNOT be opened — a missing GUI toolkit, a .NET runtime that
refuses to load, see _load_dotnet() for the Windows saga — the app opens in the default
browser instead of dying. This is a local web app: a browser is a perfectly good frame
for it, and a running app beats a crash dialog every time.

Deps:  pip install pywebview imageio-ffmpeg   (macOS pulls pyobjc automatically).
Package into a transferable .app / .exe with PyInstaller — see docs/DESKTOP.md.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server   # noqa: E402  (the existing annotator server, imported not spawned)
import version  # noqa: E402

WINDOWS = sys.platform == "win32"


class Bridge:
    """Exposed to the page as window.pywebview.api.* — lets the in-app '📂 Open'
    button raise a NATIVE folder dialog instead of typing a path."""

    def __init__(self):
        self.window = None

    def pick_folder(self):
        # pywebview 6 deprecated the FOLDER_DIALOG constant in favour of the enum, and
        # merely READING the old name logs a warning; 5.x has no enum. Ask for whichever
        # this build has.
        import webview
        kind = getattr(webview, "FileDialog", None)
        kind = kind.FOLDER if kind is not None else webview.FOLDER_DIALOG
        res = self.window.create_file_dialog(kind)
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else str(res)


# ───────────────────────────────────────────────────── Windows: let .NET load our DLL
def _bundle_root() -> Path | None:
    """The folder this app was unpacked into, or None when running from a checkout."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)


def unblock_bundle(root: Path | None = None, budget_s: float = 20.0) -> int:
    """Strip Windows' "this came from the internet" mark off OUR OWN files.

    Every file extracted from a downloaded .zip carries a ``:Zone.Identifier`` stream
    (the "Mark of the Web"), and the .NET Framework flatly REFUSES to load an assembly
    that carries one. That is what killed v1.4.x on Windows: the native window needs
    pywebview → pythonnet → ``Python.Runtime.dll``, .NET declined to load it, and the
    app died before it ever drew a pixel:

        RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
        C:\\Users\\…\\Downloads\\PlateNotate-Windows\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

    Right-click the zip → Properties → Unblock, *before* extracting, prevents it. Nobody
    knows to do that. Deleting the stream is the same operation, so do it ourselves — on
    our own files, inside our own folder, and nowhere else. Returns how many marks were
    removed (0 on every non-Windows machine, and on a copy that was never marked).
    """
    if not WINDOWS:
        return 0
    root = root or _bundle_root()
    if root is None:
        return 0
    deadline = time.monotonic() + budget_s
    cleared = 0
    # The .exe lives one level above _internal/ and carries a mark of its own (that is
    # the SmartScreen "Windows protected your PC" banner), so start there.
    targets = [Path(sys.executable)] if getattr(sys, "frozen", False) else []
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            targets.extend(Path(dirpath) / f for f in filenames)
            if time.monotonic() > deadline:
                break
    except OSError:
        pass
    for path in targets:
        if time.monotonic() > deadline:
            break
        try:
            # An alternate data stream is addressed as "<file>:<stream>" and deleted
            # like any other file. There is no cheap way to ask whether one exists, so
            # just try: a missing stream is one failed syscall, not a problem.
            os.remove(f"{path}:Zone.Identifier")
            cleared += 1
        except OSError:
            pass                                # no mark, or a copy we may not write to
    return cleared


# .NET's own escape hatch for the case we cannot fix by unblocking — a copy on a network
# share, on read-only media, or one a policy keeps re-marking. It only applies to a
# non-root AppDomain, which is why _load_dotnet() names a domain when it passes this.
_CLR_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <runtime>
    <!-- PlateNotate ships Python.Runtime.dll inside its own folder. If Windows decided
         that folder is "remote", load it anyway: it is the same file we built. -->
    <loadFromRemoteSources enabled="true" />
  </runtime>
</configuration>
"""


def _clr_config() -> Path | None:
    """Write the AppDomain config next to the app's other state; None if we cannot."""
    try:
        path = Path.home() / ".medaka_annotator" / "platenotate.clr.config"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_CLR_CONFIG, encoding="utf-8")
        return path
    except OSError:
        return None


def _load_dotnet() -> str | None:
    """Get ``import clr`` working on Windows. Returns None on success, else why not.

    pywebview's Windows backend is the only one that needs a .NET runtime, and it has
    exactly one recovery attempt of its own::

        try:
            import clr
        except Exception:
            os.environ['PYTHONNET_RUNTIME'] = 'coreclr'
            import clr

    which cannot work: the first failure already cached a runtime object in
    ``pythonnet._RUNTIME``, and ``pythonnet.load()`` short-circuits on it, so the retry
    re-raises the *first* runtime's error and the environment variable is never read.
    The user's traceback lands on the second ``import clr`` — pywebview's own fallback —
    with the .NET Framework message. Replacing the cached runtime is the whole fix.

    So: try the three runtimes in order of how likely they are to exist, and hand each
    one a runtime object rather than a hint.
    """
    if not WINDOWS:
        return None
    try:
        import pythonnet
    except Exception as exc:                    # noqa: BLE001
        return f"pythonnet is not available ({type(exc).__name__}: {exc})"

    tried = []

    # 1. .NET Framework, root AppDomain — what every Windows machine has, and what works
    #    once the assembly is no longer marked as downloaded.
    try:
        pythonnet.load()
        return None
    except Exception as exc:                    # noqa: BLE001
        tried.append(f"netfx: {exc}")

    # 2. .NET Framework again, in a private AppDomain that is allowed to load "remote"
    #    assemblies — for the copies unblock_bundle() could not rewrite.
    cfg = _clr_config()
    if cfg is not None:
        try:
            import clr_loader
            pythonnet.set_runtime(clr_loader.get_netfx(domain="platenotate",
                                                       config_file=str(cfg)))
            pythonnet.load()
            return None
        except Exception as exc:                # noqa: BLE001
            tried.append(f"netfx+loadFromRemoteSources: {exc}")

    # 3. .NET (Core), if this machine happens to have one installed. This is the runtime
    #    pywebview meant to fall back to.
    try:
        import clr_loader
        pythonnet.set_runtime(clr_loader.get_coreclr())
        pythonnet.load()
        return None
    except Exception as exc:                    # noqa: BLE001
        tried.append(f"coreclr: {exc}")

    return "no .NET runtime would load — " + " | ".join(tried)


# ───────────────────────────────────────────────────────────── talking to the user
def _message_box(title: str, text: str) -> bool:
    """Show a modal dialog and wait for OK. True if the user actually saw it.

    A windowed build has ``sys.stdout is None`` and CPython's ``print`` silently
    discards everything — for three releases "the app does not start" was the entire
    user-facing error message. On Windows a message box needs nothing but user32, which
    is always there; that makes it the one channel that cannot itself fail to load.
    """
    if WINDOWS:
        try:
            import ctypes
            MB_OK, MB_ICONINFORMATION, MB_SETFOREGROUND = 0x0, 0x40, 0x10000
            ctypes.windll.user32.MessageBoxW(
                None, text, title, MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND)
            return True
        except Exception:                       # noqa: BLE001
            pass
    return False


def _say(msg: str) -> None:
    """Print without ever being the reason the app stops (see server.make_console_safe)."""
    try:
        print(msg, flush=True)
    except Exception:                           # noqa: BLE001
        pass


# ────────────────────────────────────────────────────────────────── the two front ends
def _gui_backend() -> tuple[object | None, str | None, str | None]:
    """Import the GUI backend for this platform and say which engine it settled on.

    Returns (module, renderer, error). pywebview does this import itself a moment later
    and will find it cached, so the only thing forced early is knowing the answer — which
    is what lets us choose the browser BEFORE a useless window appears.

    Windows only. Linux has two candidate backends and pywebview's own ordering between
    them is worth keeping; macOS has never needed the help.
    """
    if not WINDOWS:
        return None, None, None
    try:
        import webview.platforms.winforms as backend
    except Exception as exc:                    # noqa: BLE001
        return None, None, (f"the Windows window backend would not load "
                            f"({type(exc).__name__}: {exc})")
    return backend, getattr(backend, "renderer", None), None


def _start_native_window(url: str) -> str | None:
    """Open the app in its own window and block until it closes.

    Returns None once the window has been closed normally, or a one-line reason it could
    not be opened — every failure is a reason, never an exception: the caller's job is to
    fall back to the browser, and an app that exits because of its window frame is worse
    than an app in a browser tab.
    """
    if os.environ.get("PLATENOTATE_BROWSER"):
        return "PLATENOTATE_BROWSER is set"
    cleared = unblock_bundle()
    if cleared:
        _say(f"  unblocked {cleared} bundled files (Windows marks downloads as remote)")
    err = _load_dotnet()
    if err:
        return err
    try:
        import webview
    except Exception as exc:                    # noqa: BLE001
        return f"pywebview is not available ({type(exc).__name__}: {exc})"

    _backend, renderer, err = _gui_backend()
    if err:
        return err
    if renderer == "mshtml":
        # pywebview picks the engine at import time and silently settles for MSHTML —
        # Internet Explorer 11 — when the Edge WebView2 runtime is missing. This UI is
        # modern JavaScript, so that window would open onto a broken page: a worse
        # outcome than not opening it, and a much more confusing one to report.
        return "this computer has no Edge WebView2 runtime, and Internet Explorer cannot render PlateNotate"

    bridge = Bridge()
    try:
        win = webview.create_window("PlateNotate", url,
                                    width=1440, height=920, min_size=(1024, 720),
                                    background_color="#14161b",   # no white flash
                                    js_api=bridge)
        bridge.window = win
        webview.start()                                     # blocks in the GUI loop
        return None
    except Exception as exc:                    # noqa: BLE001
        return f"the window could not be opened ({type(exc).__name__}: {exc})"


def _run_in_browser(url: str, reason: str) -> None:
    """Serve the app in the default browser, and stay alive while it is being used.

    The server is already running on a daemon thread, so "stay alive" needs a main-thread
    block that the user can also END — otherwise quitting PlateNotate would mean Task
    Manager. The dialog is that block: it says what happened, where the app is, and
    closing it quits.
    """
    _say(f"native window unavailable: {reason}")
    _say(f"opening in your browser instead: {url}")
    try:
        webbrowser.open(url)
    except Exception:                           # noqa: BLE001
        pass
    shown = _message_box(
        "PlateNotate is running",
        "PlateNotate could not open its own window on this computer, so it opened in "
        "your web browser instead. Everything works the same way — annotate, filter and "
        "export as usual.\n\n"
        f"    {url}\n\n"
        "If no browser opened, paste that address into one.\n\n"
        "Leave this message open while you work: closing it quits PlateNotate.\n\n"
        f"(Reason: {reason})")
    if not shown:
        # No dialog to block on — hold the main thread instead. On macOS and Linux this
        # is a terminal launch, where Ctrl-C is the expected way out.
        _say("  press Ctrl-C to quit")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


# ─────────────────────────────────────────────────────────────────────── CI's tripwire
def probe_gui() -> tuple[bool, str]:
    """Import the platform's GUI backend WITHOUT opening a window.

    This is the line that crashed on Windows — winforms.py's module-level ``import clr``
    — and nothing in CI ever ran it: ``--selftest`` exits before the window exists, and
    the launch test stops one call short of it. A headless runner cannot check that a
    window RENDERS, but it can absolutely check that the toolkit behind it will load,
    which is the failure that actually shipped. Twice.
    """
    cleared = unblock_bundle()
    err = _load_dotnet()
    if err:
        return False, err
    name = {"win32": "webview.platforms.winforms",
            "darwin": "webview.platforms.cocoa"}.get(sys.platform,
                                                     "webview.platforms.gtk")
    try:
        import importlib
        backend = importlib.import_module(name)
    except Exception:                           # noqa: BLE001
        return False, f"{name} would not import:\n" + traceback.format_exc()
    # Reported, not required: which engine the backend settled on depends on what is
    # installed on THIS machine, and a runner without the WebView2 runtime would fail a
    # build over a condition the app now handles by opening the browser instead.
    engine = getattr(backend, "renderer", None)
    return True, (f"{name} imported (unblocked {cleared} files"
                  + (f", engine={engine}" if engine else "") + ")")


# ──────────────────────────────────────────────────────────────────────────────── main
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
    if "--gui-probe" in argv or os.environ.get("PLATENOTATE_GUI_PROBE"):
        ok, detail = probe_gui()
        # stderr, and a log file: a windowed build discards stdout, and this runs in CI
        # precisely to explain a failure nobody can reproduce interactively.
        line = f"gui probe: {'PASS' if ok else 'FAIL'} — {detail}"
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:                       # noqa: BLE001
            pass
        try:
            Path("platenotate-guiprobe.log").write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass
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
    # ASCII only: this line ran before the console was ever proven writable, and an
    # unencodable arrow here is what stopped the Windows app from starting at all.
    _say(f"PlateNotate v{version.version()} (desktop) - {url}")
    _say(f"  data root: {data_root}")

    if os.environ.get("PLATENOTATE_NO_GUI"):
        # Everything a real launch does, except opening the window: the banner, the
        # server, the database. --selftest returns long before this, so without it CI
        # never touched the code path that actually broke on Windows.
        _say("PLATENOTATE_NO_GUI: launch path OK")
        httpd.shutdown()
        return 0

    try:
        reason = _start_native_window(url)
        if reason:
            _run_in_browser(url, reason)
    finally:
        httpd.shutdown()
    return 0


def _report_crash(tb: str) -> None:
    """Turn "Failed to execute script 'desktop'" into something someone can act on.

    PyInstaller's own crash dialog shows a traceback with no context and no way to keep
    it; on a headless machine it shows nothing and waits forever for a click. Write the
    whole thing where it can be found and read the user the first useful line.
    """
    log = Path.home() / ".medaka_annotator" / "platenotate-crash.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"PlateNotate v{version.version()}  {sys.platform}\n\n{tb}",
                       encoding="utf-8")
        where = str(log)
    except OSError:
        where = "(the crash log could not be written)"
    try:
        sys.stderr.write(tb + "\n")
        sys.stderr.flush()
    except Exception:                           # noqa: BLE001
        pass
    last = tb.strip().splitlines()[-1] if tb.strip() else "unknown error"
    _message_box("PlateNotate could not start",
                 f"PlateNotate hit an error it could not recover from:\n\n{last}\n\n"
                 f"The full report was saved to:\n{where}\n\n"
                 "Sending that file to whoever gave you the app is the fastest way to "
                 "get it fixed.")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except SystemExit:
        raise
    except BaseException:                       # noqa: BLE001 — nothing may escape here
        _report_crash(traceback.format_exc())
        sys.exit(1)
