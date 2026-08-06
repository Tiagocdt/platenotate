# Notate — desktop app & packaging

Notate is the annotator (`label_annotator/`) as a **native, no-terminal desktop app**.
It runs the local server in a background thread inside a pywebview window, and freezes
into a self-contained bundle (Python + ffmpeg + all deps) that collaborators run with a
double-click — no Python, no install.

## Run it (dev)

```bash
cd ~/metameda/imaging/tools/label_annotator
python desktop.py                                     # native window
python desktop.py /Volumes/aulehla/…/PROCESSED        # a server (SMB) folder
```
or double-click `run_app.command` (macOS). Deps: `pip install pywebview imageio-ffmpeg`.

## Where things are stored (Settings tab)

The **Settings** tab (⚙, top of the panel) sets, once:
- **Annotations folder** — a *visible* folder (default: next to your data, not the hidden
  `~/.medaka_annotator/`). The **DB + CSV + JSON** all go here.
- **Formats** — Database (.db) / CSV / JSON, all on by default. CSV opens in Excel; JSON
  is the full record; SQLite powers the pipeline tools. Written on every Save.
- **Exports folder** — where TIF/MP4 exports go, auto-organised into
  `<folder>/<plate>/<wells-or-montage>/` with proper names. Empty = next to the plate.
- Keyboard help lives here too.

Backend: `server._load_settings`/`_save_settings` (`~/.medaka_annotator/settings.json`),
`_write_side_exports` (CSV/JSON on save), `db_store.payload_to_csv`. A chosen annotations
folder re-points the DB there (`_resolve_db_location`), so nobody has to touch SQLite.

## Build a shareable app

**One recipe, one app per OS** (PyInstaller can't cross-compile). Everything is in
`packaging/`:
- `notate.spec` — the PyInstaller recipe (windowed `Notate.app` / `Notate/` folder, no
  terminal; bundles imagecodecs + the imageio-ffmpeg binary + the static UI + the sibling
  engine modules).
- `gather_deps.py` — vendors the sibling modules (`well_hyperstack`, `focus_cut`,
  `annotations`, `build_db`) into `packaging/_deps/` so the build is self-contained even
  from an annotator-only checkout. **Run it and commit `_deps/` before a release.**
- `build.sh` — build for the current OS (`gather_deps` → pip install → PyInstaller).

Local macOS build (verified — produces a launchable `Notate.app`, ~127 MB):
```bash
python3 -m venv /tmp/notate-venv && source /tmp/notate-venv/bin/activate
pip install -r requirements-desktop.txt pyinstaller
python packaging/gather_deps.py
pyinstaller --noconfirm --clean packaging/notate.spec        # → dist/Notate.app
```

## All three OSes via GitHub Actions (CI)

`.github/workflows/build-notate.yml` builds **macOS + Windows + Linux** from the one spec
and uploads each as a downloadable artifact. Trigger it manually (Actions → Run workflow)
or push a `v*` tag.

**Prereqs:** the repo must contain the annotator's files at the root **plus** the
vendored `packaging/_deps/` (committed). If your shareable repo is annotator-only, that's
all it needs. If the annotator sits under a subpath, set `working-directory` in the
workflow.

**Signing:** the app is unsigned. First launch: macOS → right-click → Open → Open (once);
Windows → "More info → Run anyway". For frictionless distribution, sign + notarize
(macOS Developer ID / Windows code-signing cert) — a later step.

### The three checks CI runs against the frozen bundle

| mode | what it proves | why it exists |
|---|---|---|
| `PlateNotate --selftest` | the server boots, serves every page, and every lazy import resolves | a bundle that compiles but dies on launch is worse than no bundle |
| `PLATENOTATE_NO_GUI=1 PlateNotate` | the real launch path — banner, database, server — runs | `--selftest` exits before any of it; a cp1252 arrow in the banner once shipped a dead app |
| `PlateNotate --gui-probe` | the **GUI toolkit behind the window loads** | nothing in CI had ever loaded it, and it is what broke on Windows in 1.4.x |

The Windows probe runs on a bundle CI has deliberately stamped with `Zone.Identifier` on
every `.dll`/`.pyd`/`.exe` first. That is what a browser download plus Explorer's "Extract
All" leaves behind, and .NET refuses to load an assembly carrying it — a runner building
its own files locally never sees the mark, which is exactly how a green build shipped an
app that could not open its window. See `desktop.py:unblock_bundle`.

A headless runner cannot check that a window *renders*. Everything short of that is
checked here.

### When the window cannot be opened

`desktop.py` never lets that be fatal: it opens the app in the default browser and keeps
serving, with a dialog that explains and quits when closed. `PLATENOTATE_BROWSER=1`
forces that path for testing. Unhandled exceptions go to
`~/.medaka_annotator/platenotate-crash.log` and into a readable dialog rather than
PyInstaller's crash box.

## Data

The app bundles **no data**. Collaborators point it at their own folder of processed
plates via "📂 Open" (a local copy or a mounted share), and set where annotations/exports
go in Settings.
