<p align="center">
  <img src="assets/logo-320.png" alt="PlateNotate" width="150">
</p>

<h1 align="center">PlateNotate</h1>

<p align="center">
  <b>Annotate plate-based microscopy time-lapses — plate, well and frame —<br>
  then export exactly what you annotated as a movie, montage or TIF hyperstack.</b>
</p>

<p align="center">
  <a href="https://github.com/Tiagocdt/platenotate/releases/latest"><b>⤓ Download the app</b></a>
  &nbsp;·&nbsp; macOS · Windows · Linux &nbsp;·&nbsp; no Python, no terminal
</p>

PlateNotate is a desktop app for screening plates of embryos, organoids, colonies —
anything imaged well-by-well over time. Every field is a **column you define**, so
nothing about any one organism is hard-wired; the medaka fields it ships with are just
editable suggestions.

```
segment  →  ANNOTATE (this tool)  →  export / analyse
```

## Install

**[Download the latest release](https://github.com/Tiagocdt/platenotate/releases/latest)**,
unzip, and double-click. Nothing else to install — Python, the image libraries and
ffmpeg are all inside the app.

| Your computer | Download | First launch |
|---|---|---|
| **macOS** | `PlateNotate-macOS.zip` | **Right-click the app → Open**, then confirm |
| **Windows** | `PlateNotate-Windows.zip` | Unzip, run `PlateNotate.exe` → "More info" → "Run anyway" |
| **Linux** | `PlateNotate-Linux.tar.gz` | Extract, run `./PlateNotate` |

The app is **not code-signed** — that needs a paid Apple/Microsoft developer
certificate — so your computer warns you the first time. On macOS you must
**right-click → Open** on that first launch; plain double-clicking a downloaded
unsigned app is refused outright, with no way through from the dialog. If macOS still
blocks it, run once:

```bash
xattr -dr com.apple.quarantine /Applications/PlateNotate.app
```

On first run, click **📂 Open** and point it at the folder holding your plate folders.
It reopens there next time.

Nothing leaves your machine: PlateNotate is a local app over your own files — no
account, no upload, no network access.

<details>
<summary><b>Run from source instead</b> (for developing, or on a machine you can't install to)</summary>

```bash
git clone https://github.com/Tiagocdt/platenotate.git
cd platenotate
pip install -r requirements-desktop.txt
python server.py --data-root /path/to/your/plates   # browser, http://127.0.0.1:8765/
python desktop.py                                    # or a native window
```

`./run.sh` is the everyday launcher: it fast-forwards the checkout, stops any previous
instance, and opens the browser. `python server.py --selftest` boots the app headless
and checks it serves — the same check CI runs against every packaged build.

To build the app yourself: `bash packaging/build.sh` inside a venv made from
`requirements-desktop.txt` (see [`docs/DESKTOP.md`](docs/DESKTOP.md)). PyInstaller
cannot cross-compile, so each platform's build must run on that platform — which is
what the GitHub Actions workflow does on every version tag.
</details>

## What your data should look like

Layout discovery is deliberately forgiving. Any of these work:

| Layout | Shape |
|---|---|
| per-channel (current) | `<plate>/<channel>/<well>/SL0N/*_LO<tp>_<CH>_SL0N.tif` |
| legacy v2 | `<plate>/bf/<well>/SL0N/…` + `<plate>/fl/<well>/…` |
| legacy v1 | `<plate>/screening/…` or `<plate>/crops/…` |
| flat | a bare folder of images — one "well" per image |

Timepoints are parsed from `_LO<NNN>_`, z-slices from `SL0N`, channels from the folder
name (`plate_metadata.json` names the detection channel if present). Any number of
channels is supported end-to-end — the viewer, the grid, and every export.

## The three scopes

| Scope | Keyed by | Examples |
|---|---|---|
| **Plate** | the folder | `incubation_temp_c`, `date`, `annotator`, `notes` — **Autofill** reads the plate metadata |
| **Well** | `well` | `mixture`, `viability` (binary), `line`, `valid_frames` (range) |
| **Image** | `(well, timepoint)` | developmental `stage`, `slice` (focus), `rotation`, measurements |

Column types: **categorical** · **binary** (two values + a default) · **range** (a span
over timepoints) · **angle** · **free**. Image columns are **keyframed**: set a value on
one frame and it holds until the next frame you set — only the boundaries are stored.

## What you can do

- **Grid** — every well as a thumbnail; rubber-band or click to select; a frame fader
  moves the whole plate through development.
- **Detail** — the selected well large, channel toggles, a scrubber through its
  trajectory, plus **z** and **rot** faders that record focus / rotation keyframes.
- **Measure** — drag a line on the image; it's stored in µm using the plate's pixel size.
- **Filter across plates** — pick any subset of plates and AND together annotation and
  **measurement** constraints (e.g. *egg_diameter above 1400 µm at every timepoint it was
  measured*), then save the filter by name.
- **Export** — TIF hyperstack or MP4, one file per well or a tiled montage, with a
  **Render** block that decides what each frame actually shows (below).
- Undo/redo, autosave, keyboard-first operation (`?` in Settings lists the keys).

## Render: the export shows what you annotated

Every export option maps to something you set in the app:

| Option | Effect |
|---|---|
| **plane per channel** | max projection · **annotated focus track** · one slice · middle slice |
| **apply rotation** | turns every frame by your `rotation` keyframes — smoothstep, shortest way round the circle, exactly as the viewer previews it |
| **overlay channels** | composites the selected channels into one colour movie (screen blend) instead of one file each |
| **colour** | per channel: gray, inverted, a tint (green/magenta/cyan/amber/…) or any matplotlib colormap |
| **labels** | burn well · plate · stage · timepoint & elapsed time · angle · z · any well annotation, plus a **scale bar** in real µm, onto every tile |
| **TIF z-mode** | keep every slice, or collapse Z to a max projection / the focus track / one slice |

A TIF hyperstack is data, so nothing is ever burned into it — labels are an MP4 option
only. If a track you asked for doesn't exist for a well, the export **degrades and tells
you** (in the job's notes) rather than failing: no focus keyframes → a fixed best-focus
slice; no rotation keyframes → 0°.

Exports run as background jobs with a progress dock, and survive closing the dialog.

## Output

Annotations live in a SQLite database (`medaka.db` by default) and are mirrored to CSV
and a `screening_<plate>.json` (schema v3) — pick the formats in Settings.

```json
{ "schema_version": 3, "plate": "…", "annotator": "…",
  "plate_columns": {…}, "plate_annotations": {…},
  "columns": { "mixture": {"type":"categorical","values":["ctrl","ko"]} },
  "annotations": { "A01": {"mixture":"ctrl","valid_frames":[30,169]} },
  "image_columns": {…},
  "image_annotations": { "A01": { "168": {"stage":"st24","slice":"3","rotation":-37.5} } } }
```

## Layout

| Path | What |
|---|---|
| `server.py` | stdlib HTTP backend: layout discovery, TIFF→PNG, JSON API, atomic save |
| `model.py` | data model: three scopes, v0→v3 migration, well/frame discovery |
| `db_store.py` | the SQLite store (the source of truth) |
| `export.py` | background TIF/MP4 export jobs |
| `version.py`, `VERSION` | version + the fast-forward self-update |
| `index.html`, `static/` | the GUI (vanilla JS, no build) — `rot_tool.js` / `measure_tool.js` are plugins |
| `packaging/` | PyInstaller recipe + the vendored engine modules a standalone clone needs |
| `assets/` | the logo, and the `.icns` / `.ico` app icons built from it |
| `.github/workflows/` | builds + smoke-tests the app on all three platforms, and publishes the release |
| `tests/` | `js_harness.mjs` (headless GUI), `compose_test.py` (render engine), `db_roundtrip_test.py` |
| `docs/` | desktop packaging, the image-tool plugin API, and design history |

The frame composer, hyperstack builder and focus-track renderer live in
`packaging/_deps/` (vendored from the sibling `hyperstack_video/` tool in the author's
full imaging tree, so this repo runs standalone).

## Tests

```bash
node tests/js_harness.mjs        # 102 headless GUI assertions, no browser
python tests/compose_test.py     # 33 render-engine assertions, no image data
python tests/db_roundtrip_test.py
```

## Adapting it to your experiment

- `defaults.json` — the seed columns offered per scope. Edit freely.
- `iwamatsu_stages.json` — the developmental stage list (medaka, Iwamatsu 2004,
  transcribed from a public reproduction — **verify against the paper before
  publishing**). Replace it with your own staging series.
- Suggestions accumulate in `annotation_schema.json` as you work.

## Licence

MIT — see [`LICENSE`](LICENSE).
