# Changelog

All notable changes to PlateNotate. Versions are `MAJOR.MINOR.PATCH`; the number in
`VERSION` is what the app reports, and `run.sh` fast-forwards a git checkout to the
newest commit on launch.

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
- `tests/compose_test.py` (29 assertions); the JS harness grew from 76 to 102.

## [1.0.0] — 2026-07-23

Baseline: the three-scope annotator (plate / well / image) with keyframed image columns,
the rotation and measurement image-tool plugins, the SQLite store, the cross-plate
filter, background TIF/MP4 export with a job dock, Settings, the desktop window
(`desktop.py`) and the PyInstaller packaging recipe.
