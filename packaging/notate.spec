# notate.spec — PyInstaller recipe for Notate (the desktop annotator).
# Build:  pyinstaller packaging/notate.spec   (from the label_annotator/ dir, inside a
#         slim venv: requirements-desktop.txt + pyinstaller). See packaging/build.sh.
#
# ONEDIR bundle → a windowed app with NO terminal. macOS gets a .app via the BUNDLE block.
# The app depends on two sibling tool dirs (../hyperstack_video, ../metadata_db); they're
# put on pathex + listed as hiddenimports so PyInstaller freezes them into the app.

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files

APP = "PlateNotate"
ROOT = Path(".").resolve()                 # the label_annotator/ dir
ENGINE = ROOT.parent / "hyperstack_video"  # well_hyperstack / focus_cut / annotations (dev tree)
METADB = ROOT.parent / "metadata_db"       # build_db (dev tree)
DEPS = ROOT / "packaging" / "_deps"        # vendored copies (committed; used on CI checkouts)
ASSETS = ROOT / "assets"                   # logo → app icon (.icns on macOS, .ico on Windows)
ICON = str(ASSETS / ("icon.icns" if sys.platform == "darwin" else "icon.ico"))

# imagecodecs ships many compiled decoders — collect them all or the fast TIF path fails.
ic_datas, ic_bins, ic_hidden = collect_all("imagecodecs")
# imageio-ffmpeg carries the ffmpeg binary as package data (so no system ffmpeg needed).
ff_datas = collect_data_files("imageio_ffmpeg", include_py_files=False)

datas = [
    (str(ROOT / "static"), "static"),
    (str(ROOT / "index.html"), "."),
    (str(ROOT / "iwamatsu_stages.json"), "."),
    (str(ROOT / "defaults.json"), "."),
    (str(ROOT / "annotation_schema.json"), "."),
    (str(ROOT / "VERSION"), "."),
    (str(ASSETS / "logo.png"), "assets"),
] + ic_datas + ff_datas

# app + sibling modules that are imported lazily / via sys.path (so Analysis may miss them)
hiddenimports = [
    "server", "model", "db_store", "export", "version",
    "well_hyperstack", "focus_cut", "compose", "annotations", "build_db",
    "numpy", "tifffile", "imagecodecs", "PIL.Image", "imageio_ffmpeg", "webview",
] + ic_hidden

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT), str(DEPS), str(ENGINE), str(METADB)],
    binaries=ic_bins,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["torch", "torchvision", "matplotlib", "scipy", "pandas", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP, console=False,  # windowed, no terminal
          icon=ICON)
coll = COLLECT(exe, a.binaries, a.datas, name=APP)
app = BUNDLE(coll, name=f"{APP}.app", icon=ICON,
             bundle_identifier="de.trindade.platenotate",
             info_plist={"NSHighResolutionCapable": True,
                         "CFBundleName": APP, "CFBundleDisplayName": APP})
