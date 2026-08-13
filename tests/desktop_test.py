#!/usr/bin/env python
"""desktop_test.py — the app must open, or explain itself. Never neither.

v1.4.x died on Windows with

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
    C:\\Users\\…\\Downloads\\PlateNotate-Windows\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

because a file extracted from a downloaded .zip carries Windows' Mark of the Web and the
.NET Framework refuses to load an assembly that does. pywebview has a fallback for this
and it cannot work: it re-imports ``clr`` after setting PYTHONNET_RUNTIME, but
``pythonnet.load()`` short-circuits on the runtime object it already cached, so the retry
re-raises the FIRST runtime's error. That is the shape pinned here.

Three properties, all testable off Windows:
  * unblocking only ever deletes ``<file>:Zone.Identifier`` — never a file;
  * every .NET recovery step installs a NEW runtime before retrying (the upstream bug);
  * no failure anywhere in the launch path raises — it returns a reason, and the caller
    falls back to the browser.

    python tests/desktop_test.py
"""
from __future__ import annotations
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import desktop                                                        # noqa: E402

passed = failed = 0


def check(cond, name):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


class temp_home:
    """Point ~ at a scratch dir so the CLR config and crash log land in the bin."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="platenotate-desktop-test-")
        self.saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.dir
        return Path(self.dir)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


class as_windows:
    """Run a block down the Windows branch on whatever machine the tests run on."""

    def __enter__(self):
        self.saved = desktop.WINDOWS
        desktop.WINDOWS = True

    def __exit__(self, *exc):
        desktop.WINDOWS = self.saved


# ══ unblocking cannot delete anything but a Zone.Identifier stream ══════════════
check(desktop.unblock_bundle(Path("/nonexistent")) == 0,
      "unblock_bundle is a no-op off Windows")

with tempfile.TemporaryDirectory() as td:
    tree = Path(td)
    (tree / "sub").mkdir()
    files = [tree / "Python.Runtime.dll", tree / "app.exe", tree / "sub" / "data.pyd"]
    for f in files:
        f.write_bytes(b"x")
    removed = []
    real_remove = os.remove

    def spy_remove(path, *a, **kw):
        removed.append(str(path))
        return real_remove(path, *a, **kw)

    os.remove = spy_remove
    try:
        with as_windows():
            desktop.unblock_bundle(tree)
    finally:
        os.remove = real_remove

    check(len(removed) >= len(files),
          f"every bundled file is offered for unblocking ({len(removed)} paths)")
    check(all(p.endswith(":Zone.Identifier") for p in removed),
          "the ONLY thing ever deleted is a ':Zone.Identifier' stream")
    check(all(Path(p.rsplit(":Zone.Identifier", 1)[0]).exists() for p in removed),
          "every stream deleted belongs to a real file inside the bundle")
    check(all(f.exists() for f in files),
          "unblocking left every actual file in place")

    with as_windows():
        check(desktop.unblock_bundle(tree, budget_s=-1) == 0,
              "an expired time budget stops the walk instead of stalling the launch")

check(desktop._bundle_root() is None, "a source checkout has no bundle to unblock")

# ══ the .NET recovery ladder ═══════════════════════════════════════════════════
check(desktop._load_dotnet() is None, "no .NET is needed off Windows")

NETFX_ERROR = RuntimeError(
    "Failed to resolve Python.Runtime.Loader.Initialize from "
    r"C:\Users\x\Downloads\PlateNotate-Windows\_internal\pythonnet\runtime"
    r"\Python.Runtime.dll")


class FakePythonnet:
    """pythonnet's real contract: load() uses whatever runtime was last installed, and
    the DEFAULT (nothing installed) is .NET Framework in the root AppDomain."""

    def __init__(self, works):
        self.works = works               # 'netfx' | 'remote' | 'coreclr' | None
        self.installed = []              # runtimes handed to set_runtime, in order
        self.loads = 0

    def load(self):
        self.loads += 1
        if (self.installed[-1] if self.installed else "netfx") != self.works:
            raise NETFX_ERROR
    def set_runtime(self, runtime, **params):
        self.installed.append(runtime)


class FakeClrLoader:
    def get_netfx(self, domain=None, config_file=None):
        # loadFromRemoteSources is ignored in the root domain — clr_loader says so in
        # get_netfx's own docstring — so a config with no domain would be a silent no-op.
        assert domain, "the remote-sources config needs a NON-root AppDomain"
        assert config_file, "no config file was passed"
        return "remote"

    def get_coreclr(self, **kw):
        return "coreclr"


def run_ladder(works):
    saved = {k: sys.modules.get(k) for k in ("pythonnet", "clr_loader")}
    fake = FakePythonnet(works)
    sys.modules["pythonnet"], sys.modules["clr_loader"] = fake, FakeClrLoader()
    try:
        with as_windows(), temp_home():
            return fake, desktop._load_dotnet()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


fake, err = run_ladder("netfx")
check(err is None and fake.loads == 1 and fake.installed == [],
      "an unblocked machine loads .NET Framework directly, with no retries")

fake, err = run_ladder("remote")
check(err is None and fake.installed == ["remote"],
      "a still-marked copy retries in an AppDomain that allows remote assemblies")

fake, err = run_ladder("coreclr")
check(err is None and fake.installed == ["remote", "coreclr"],
      "with no usable .NET Framework it falls through to .NET Core")
# THE upstream bug: pywebview sets PYTHONNET_RUNTIME=coreclr and re-imports clr, but
# pythonnet.load() returns the cached runtime's error, so the env var is never read.
check(fake.installed[-1] == "coreclr" and fake.loads == 3,
      "each retry INSTALLS a new runtime — re-calling load() alone repeats the error")

fake, err = run_ladder(None)
check(isinstance(err, str) and all(w in err for w in
                                   ("netfx", "loadFromRemoteSources", "coreclr")),
      "when nothing loads, the reason names every runtime that was tried")
check(err is not None and "Python.Runtime.Loader.Initialize" in err,
      "the reason carries the .NET error the user would otherwise never see")

saved_pn = sys.modules.get("pythonnet")
sys.modules["pythonnet"] = None                      # makes `import pythonnet` raise
try:
    with as_windows():
        err = desktop._load_dotnet()
finally:
    if saved_pn is None:
        sys.modules.pop("pythonnet", None)
    else:
        sys.modules["pythonnet"] = saved_pn
check(isinstance(err, str) and "pythonnet" in err,
      "a bundle with no pythonnet at all reports it instead of raising")

with temp_home() as home:
    cfg = desktop._clr_config()
    text = cfg.read_text(encoding="utf-8") if cfg else ""
    check(cfg is not None and 'loadFromRemoteSources enabled="true"' in text,
          "the AppDomain config really does enable remote sources")

# ══ no failure in the launch path may raise ════════════════════════════════════
saved_wv = sys.modules.get("webview")
sys.modules["webview"] = None                        # makes `import webview` raise
try:
    reason = desktop._start_native_window("http://127.0.0.1:1/")
finally:
    if saved_wv is None:
        sys.modules.pop("webview", None)
    else:
        sys.modules["webview"] = saved_wv
check(isinstance(reason, str) and "pywebview" in reason,
      "a bundle without pywebview returns a reason, it does not crash")


class BoomWebview:
    def create_window(self, *a, **kw):
        raise RuntimeError("no display")

    def start(self, *a, **kw):
        raise AssertionError("start() must not be reached")


saved_wv = sys.modules.get("webview")
sys.modules["webview"] = BoomWebview()
try:
    reason = desktop._start_native_window("http://127.0.0.1:1/")
finally:
    if saved_wv is None:
        sys.modules.pop("webview", None)
    else:
        sys.modules["webview"] = saved_wv
check(isinstance(reason, str) and "no display" in reason,
      "a window that will not open returns the toolkit's own words")

saved_backend = desktop._gui_backend
desktop._gui_backend = lambda: (object(), "mshtml", None)
saved_wv = sys.modules.get("webview")
sys.modules["webview"] = BoomWebview()          # create_window must never be reached
try:
    reason = desktop._start_native_window("http://127.0.0.1:1/")
finally:
    desktop._gui_backend = saved_backend
    if saved_wv is None:
        sys.modules.pop("webview", None)
    else:
        sys.modules["webview"] = saved_wv
check(isinstance(reason, str) and "WebView2" in reason,
      "no WebView2 runtime -> the browser, not a window running Internet Explorer")

os.environ["PLATENOTATE_BROWSER"] = "1"
try:
    check(desktop._start_native_window("http://x/") == "PLATENOTATE_BROWSER is set",
          "PLATENOTATE_BROWSER forces the browser without touching the toolkit")
finally:
    os.environ.pop("PLATENOTATE_BROWSER", None)

saved_probe = desktop._load_dotnet
desktop._load_dotnet = lambda: "no .NET runtime would load"
try:
    ok, detail = desktop.probe_gui()
finally:
    desktop._load_dotnet = saved_probe
check(ok is False and "no .NET runtime" in detail,
      "the CI probe fails loudly when the GUI toolkit cannot load")

# ══ the browser fallback actually serves, and can be quit ══════════════════════
opened, boxed = [], []
saved_open, saved_box = desktop.webbrowser.open, desktop._message_box
desktop.webbrowser.open = lambda url: opened.append(url)
desktop._message_box = lambda title, text: (boxed.append((title, text)), True)[1]
try:
    desktop._run_in_browser("http://127.0.0.1:8765/", "the window could not be opened")
finally:
    desktop.webbrowser.open, desktop._message_box = saved_open, saved_box
check(opened == ["http://127.0.0.1:8765/"], "the fallback opens the default browser")
check(len(boxed) == 1 and "http://127.0.0.1:8765/" in boxed[0][1],
      "the dialog tells the user where the app is")
check("quits PlateNotate" in boxed[0][1],
      "the dialog is also the Quit button — otherwise it is Task Manager or nothing")

# ══ end to end: a window that will not open still leaves a running app ═════════
with tempfile.TemporaryDirectory() as td, temp_home():
    calls = []
    saved_native, saved_browser = desktop._start_native_window, desktop._run_in_browser
    desktop._start_native_window = lambda url: "the window could not be opened (test)"
    desktop._run_in_browser = lambda url, reason: calls.append((url, reason))
    try:
        rc = desktop.main([td])
    finally:
        desktop._start_native_window = saved_native
        desktop._run_in_browser = saved_browser
    check(rc == 0, "main() returns cleanly when the window cannot be opened")
    check(len(calls) == 1 and calls[0][0].startswith("http://127.0.0.1:"),
          f"the running server is handed to the browser instead ({calls[:1]})")

# ══ closing the window must actually quit the app ═════════════════════════════
# The reported symptom: while a plate is loading (minutes, over a share) the app cannot
# be closed except by Force Quit. Cause: ThreadPoolExecutor registers an atexit hook that
# JOINS every worker it ever started, and those workers are not daemons. PlateNotate runs
# two such pools — 12 prefetch, 8 image-store — and being inside a slow read is their
# normal state during a load. So the window closed, main() returned, and Python then sat
# waiting for reads that had minutes left to run.
import subprocess                                                     # noqa: E402

PROOF = r"""
import sys, threading, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, %r)
stuck = threading.Event()
pool = ThreadPoolExecutor(max_workers=2)
pool.submit(lambda: stuck.wait(8))         # a worker inside a slow read
t0 = time.monotonic()
%s
"""
HERE_S = str(HERE.parent)

t0 = time.monotonic()
plain = subprocess.run([sys.executable, "-c", PROOF % (HERE_S, "sys.exit(0)")],
                       capture_output=True, timeout=60)
plain_s = time.monotonic() - t0
check(plain_s > 5,
      f"REPRODUCED: sys.exit() waits for the stuck worker ({plain_s:.1f}s, not instant)")

t0 = time.monotonic()
fixed = subprocess.run([sys.executable, "-c",
                        PROOF % (HERE_S, "import desktop; desktop._exit_now(0)")],
                       capture_output=True, timeout=60)
fixed_s = time.monotonic() - t0
check(fixed.returncode == 0, f"_exit_now leaves with the right exit code ({fixed.returncode})")
check(fixed_s < 5,
      f"…and leaves AT ONCE, not when the read finishes ({fixed_s:.1f}s vs {plain_s:.1f}s)")

# ══ a crash is written down and read out ══════════════════════════════════════
with temp_home() as home:
    boxed = []
    saved_box = desktop._message_box
    desktop._message_box = lambda title, text: (boxed.append(text), True)[1]
    try:
        try:
            raise ValueError("something specific went wrong")
        except ValueError:
            desktop._report_crash(traceback.format_exc())
        survived = True
    except Exception:                                          # noqa: BLE001
        survived = False
    finally:
        desktop._message_box = saved_box
    log = home / ".medaka_annotator" / "platenotate-crash.log"
    check(survived, "reporting a crash cannot itself crash")
    check(log.exists() and "something specific went wrong" in log.read_text(),
          "the full traceback is saved where it can be sent on")
    check(bool(boxed) and "something specific went wrong" in boxed[0],
          "the user is shown the error, not 'Failed to execute script'")

print(f"\ndesktop_test: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
