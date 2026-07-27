# Changelog

All notable changes to PlateNotate. Versions are `MAJOR.MINOR.PATCH`; the number in
`VERSION` is what the app reports, and `run.sh` fast-forwards a git checkout to the
newest commit on launch.

## [1.2.2] — 2026-07-27

### Fixed — the Windows smoke test hung instead of failing

With `Start-Process -Wait` the windowed `.exe` never returned, stalling the job. A
headless runner has nobody to dismiss a PyInstaller crash dialog, and a GUI-subsystem
process gives PowerShell nothing to wait on. The step is now **bounded** (`WaitForExit`
with a timeout, kill on hang, `timeout-minutes` as a backstop) and judges the run by the
`selftest: PASS` line in the log the app writes itself — so a hang fails fast and says
so, instead of burning the job.

`--selftest` also now closes the server socket and exits through `os._exit`, so no
lingering thread or GUI runtime can keep the process alive after the check is done.

## [1.2.1] — 2026-07-27

### Fixed — the Windows build's smoke test could not report anything

The v1.2.0 tag built cleanly on all three platforms, but the Windows smoke test exited
non-zero with an empty report, so the release job never ran. Two causes, both about
*seeing* the failure rather than the app itself (Windows served all six URLs fine):

- A windowed Windows build has `sys.stdout is None`, and CPython's `print` **silently
  discards** output in that case — so the entire selftest report vanished.
  It now writes to stderr *and* to `platenotate-selftest.log`, which CI prints.
- A GUI-subsystem `.exe` does not block PowerShell, so `$LASTEXITCODE` was not the
  app's exit code. CI now uses `Start-Process -Wait -PassThru` and reads `.ExitCode`.

The selftest also got stricter while it was being fixed: it now imports every module
the app loads lazily (`export`, `compose`, `well_hyperstack`, `focus_cut`,
`annotations`, `build_db`, `imagecodecs`, `tifffile`, `imageio_ffmpeg`), reports a full
traceback for any that fail, and checks the bundled ffmpeg binary is really there —
which is exactly the class of breakage that only appears inside a frozen bundle.

## [1.2.0] — 2026-07-27

### A real app you download and double-click

Until now the repo only contained *instructions* for building a desktop app, and CI only
uploaded Actions artifacts — which expire after 90 days and need a GitHub login, so they
were useless as a download link. Anyone wanting to use PlateNotate still had to install
Python, `pip install pywebview`, and run a command. That is fixed:

- **Every version tag now publishes a GitHub Release** with `PlateNotate-macOS.zip`,
  `PlateNotate-Windows.zip` and `PlateNotate-Linux.tar.gz` attached. Download, unzip,
  double-click. Python, Pillow/numpy/tifffile/imagecodecs and ffmpeg are all inside.
- **`--selftest`** boots the app headless, fetches the pages a browser needs and imports
  the export engine. CI runs it against the **frozen** bundle on all three platforms, so
  a build that compiles but dies on launch can never be released. It is also the fastest
  way to check a local build: `python server.py --selftest`.
- **The packaged app no longer guesses a data root inside its own bundle.** It opens the
  folder you last used (`last_data_root`), else your home folder, and "📂 Open" takes it
  from there.
- **Check for updates works without git.** A packaged app has no checkout to compare
  against, so the check asks the GitHub Releases API and offers a download link;
  a source checkout still fast-forwards itself as before. Only on an explicit click —
  no background polling.
- Linux builds now install the GTK/WebKit packages pywebview needs.

### Logo

The PlateNotate mark is now the browser-tab favicon, the mark in the app's top bar, the
macOS/Windows app icon (`assets/icon.icns` / `icon.ico`, generated from `assets/logo.png`)
and the README header.

### Fixed

- `_serve(host, 0)` reported port `0` instead of the port the socket actually got, so
  anything asking it for a URL — including the new selftest — built an unreachable
  address. It now reads the bound port from the socket.
- Static handler serves `.png` / `.svg` / `.ico` with the right content type (the
  favicon was being sent as `application/octet-stream`), and caches images for a day
  while keeping the code uncached.

## [1.1.2] — 2026-07-25

### Fixed — `well_hyperstack.py`'s command line was broken

Both builders were called with POSITIONAL arguments, but each has optional parameters
(`channels`/`slices`, the timepoint window, and now `z_mode`/`rotate`) sitting between
`gap` and the data roots — so `--data-root` landed in `tp_start` and any CLI run died
with `'>=' not supported between instances of 'int' and 'str'`. Now called by keyword.
The app was never affected; it has always used keyword arguments.

Also exposed on the CLI: `--z-mode all|maxproj|focus|slice` and `--rotate`.

## [1.1.1] — 2026-07-25

### Fixed — dated plate folders found no annotations in the database

Rows are keyed by the **canonical** plate id (the folder name with any leading
`YYYYMMDD_` stripped), but the renderers are handed the folder name. For a dated
folder such as `20260627_AQV07_…` every database lookup therefore missed:

- keyframes and well annotations quietly came from the screening JSON instead of the
  database — the same values today, but not the source of truth, and stale the moment
  the two diverge;
- `plate_meta` and `pixel_size_um` have no JSON fallback, so montages of a dated plate
  had **no elapsed-time label and no scale bar at all**, with nothing to say why.

`annotations.plate_keys()` now tries both ids in every accessor (`image_keyframes`,
`measurements`, `well_annotations`, `plate_meta`, `pixel_size_um`). AQV07 montages now
carry `t5 · 0h40` and a 1 mm bar. Undated folders are unaffected — one lookup, as before.

## [1.1.0] — 2026-07-25

The first released version. Everything below was built in one session on top of the
1.0.0 baseline, so it ships as a single commit; from here on each version gets its own
commit and tag.

### Render options for movies and montages

Every option maps to something already saved in the app.

- **Plane per channel** — max projection, the **annotated focus track**, one z-slice, or
  the middle slice. Max projection now works for *every* channel, brightfield included,
  instead of only whatever `_SLMX` file happened to exist on disk.
- **Rotation is applied.** `rotation` keyframes are interpolated with the same rule as
  the viewer (smoothstep, shortest way round the circle) and baked into every frame.
  **This never worked before**: the "use my annotations (focus + rotation)" checkbox
  read only the focus column, so rotation was silently dropped from every export.
- **No hard failure on missing annotations.** A well with rotation keyframes but no
  focus keyframes used to abort the whole job with *"no output produced"*. Each track
  now degrades independently (focus → modal best-focus slice → middle slice;
  rotation → 0°) and the job reports what it actually used, per well, in the job dock.
- **Channel overlay** — composite the selected channels into one colour movie (screen
  blend) instead of one movie per channel.
- **Colour per channel** — gray, inverted, a tint (green/magenta/cyan/red/blue/yellow/
  orange/amber/violet/ice/sepia) or any matplotlib colormap.
- **Labels on every tile** — well, plate, developmental stage (forward-filled from the
  keyframes), timepoint and elapsed time (from the plate's cadence), angle, z, and any
  well-level annotation column; plus a **scale bar** in real µm, sized from the plate's
  own measurements. Corner, size and colour are configurable.
- **TIF hyperstacks** gained `z_mode` (`all` / `maxproj` / `focus` / `slice`) and
  rotation. Labels are deliberately *not* offered for TIF — it's quantitative data.

### Filter across plates — subsets, measurements, saved filters

- **Plate subset.** The single-plate dropdown is now a chip picker: search any
  combination of plates (no chips selected = all of them), with each plate's well count
  on its chip.
- **Measurement constraints.** Filter on a measurement (e.g. `egg_diameter`) with
  `> ≥ < ≤ between =` and a reduction over the timepoints it was measured at:
  **at every measured timepoint** (the "…always" case), at any timepoint, or the
  mean / smallest / largest / first / last.
  - A well measured at **one** timepoint satisfies "at every measured timepoint" — one
    timepoint is still every timepoint that exists. Most sizes are annotated once, and
    they must not be silently dropped.
  - `min n` demands a minimum number of measured timepoints when you *do* want more.
  - A well that was never measured never matches a measurement constraint.
- **Annotation operators.** Beyond `=`: `≠`, *is set*, *is unset*, and numeric
  `> ≥ < ≤ between` for columns holding numbers.
- **Saved filters.** Name a plate set + constraints and reload or delete it later
  (stored in `~/.medaka_annotator/settings.json`).
- Wells carrying measurements but no annotations are now filterable too.

### Version & self-update

- `VERSION` + `version.py`; the version shows in the top bar (amber when an update is
  waiting) and in Settings.
- `run.sh` fast-forwards the checkout on launch — skipped when there are local changes,
  `NO_UPDATE=1` opts out. Settings has *Check for updates* / *Update now*.
- `GET /api/version`, `POST /api/update`.

### Engine

- New `compose.py`: the annotation-aware frame composer (tracks, plane selection, tint,
  overlay, labels, scale bar, tiling).
- New shared accessors `well_annotations()`, `plate_meta()`, `pixel_size_um()` — µm/px
  derived from your own measurements, falling back to the acquisition metadata.
- Fixed: a stale vendored copy under `packaging/_deps/` could shadow the live engine.

### Repo

- Runs from a **standalone clone**: `db_store` and `export` fall back to the vendored
  modules in `packaging/_deps/`, so `import build_db` / `import compose` no longer need
  the author's full imaging tree.
- Public README, MIT `LICENSE`, `docs/` (desktop packaging, image-tool plugin API,
  design history), tightened `.gitignore` (no databases or annotations).
- `tests/compose_test.py` (33 assertions); the JS harness grew from 76 to 102.

## [1.0.0] — 2026-07-23

Baseline: the three-scope annotator (plate / well / image) with keyframed image columns,
the rotation and measurement image-tool plugins, the SQLite store, the cross-plate
filter, background TIF/MP4 export with a job dock, Settings, the desktop window
(`desktop.py`) and the PyInstaller packaging recipe.
