# annotation_app — web-based image annotator (replaces `screen.py`)

A tiny, self-contained web tool for annotating the processed plates at three
levels — **plate**, **well**, and **image/frame** — with a real GUI (buttons,
dropdowns, rubber-band multi-select, a trajectory fader). It writes a
`screening_<plate>.json` (schema **v3**) that `twinnet_clean/tools/build_db.py`
ingests into `medaka.db`.

It is **general-purpose**: every field at every level is a *column you define*
(type `categorical | binary | range | free`). Nothing about medaka is hard-wired
— the medaka fields (temperature, mixture, viability, Iwamatsu stage, …) are just
editable **suggestions**, so the same tool works for any plate/well/frame
annotation task.

```
segment  →  ANNOTATE (this tool)  →  build (medaka.db)
```

## Run it

```bash
conda activate twinnet                 # provides Pillow/numpy (the only needs)
cd ~/MedakaNet
python annotation_app/server.py                 # opens a browser; pick a plate
python annotation_app/server.py AQV04            # open focused on a plate (prefix ok)
python annotation_app/server.py --data-root DIR --port 8765 --no-browser
```

No build step, no `npm`, no framework, no extra `pip install` — a stdlib HTTP
server plus Pillow (already in the `twinnet` env). To let another annotator use
it, run it on a shared machine and give them the URL, or they clone the repo and
run the same command.

## The three levels (one idea, three scopes)

| Level | Keyed by | Examples (seed suggestions) |
|-------|----------|------------------------------|
| **Plate** | whole folder | `incubation_temp_c`, `date`, `start_time`, `annotator`, `notes` — **Autofill** pulls these from `plate_metadata.json` + the frames' median `temp_C` |
| **Well** (main) | `well` | `mixture`, `viability` (binary), `injection_quality` (binary), `line`, `valid_frames` (range) |
| **Image** | `(well, timepoint)` | `iwamatsu_stage` (Iwamatsu 2004) + `slice` — **keyframe / forward-fill** columns (see below) |

Column types: **categorical** (open value set) · **binary** (two values + a
default) · **range** (`[start,end]` over timepoints, e.g. `valid_frames`) ·
**free** (text/number).

## What you can do

- **Grid** (left): thumbnails of every well; **rubber-band drag** or click to
  select; the badge shows the *active* column's value. Pagination + per-page for
  non-96 / multi-plate sets. A `grid`/`frame` control moves all thumbnails to a
  point in development.
- **Detail** (upper right): the selected well's big image, **BF/FL toggle**, and
  a **scrubber/fader** through its trajectory (play/pause, `[`/`]`).
- **Annotation panel** (lower right): create columns/values entirely in the GUI;
  click a value to assign it to **all selected wells**; binary columns carry a
  **default** (and a one-click *fill unset → default*); set a `valid_frames`
  **range by dragging** the handles on the scrubber; suggestion chips surface
  columns/values used on other plates.
- **Image tab — keyframe staging & slice**: `iwamatsu_stage` and `slice` are
  *forward-fill* columns. Set a stage on a frame and it **holds until the next
  frame you stage** (only the boundaries are stored). Clicking a value that a
  frame already shows **re-anchors** that run's start to the current frame
  (earlier *or* later); clicking on the boundary frame **toggles it off**. The
  keyframe strip lists the boundaries (jump / ✕). `slice` works the same and
  allows a slice to recur.
- **Cross-plate filter** (🔎 Filter): filter wells across **all** plates by
  AND constraints (e.g. `injected?=Yes` · `viability=alive` · `line=…`); the
  matches replace the grid, labelled by plate and **sorted by #annotations**;
  click one to load its plate and annotate it (Image tab). **⬇ Export JSON**
  downloads `wells_filter.json` (`{by_plate:{plate:[wells]}, …}`) — feed it to
  `tools/well_hyperstack.py --from-json wells_filter.json` (add `--per-well`
  for one hyperstack per well).
- **Undo/redo**, an **annotator** field, and **autosave** on every change.

Keyboard: `1–9` assign the active column's Nth value · `Tab` next column ·
`←→↑↓` move well · `Space` play/pause · `c` BF/FL · `Backspace` clear · `z` /
`Shift+Z` undo/redo · `s` save. (`?` in the top bar shows this.)

## Output — `screening_<plate>.json` (v3)

The **well-level** `columns` + `annotations` keep the exact v2 shape, so
`build_db.py` ingests a v3 file unchanged; the plate/image levels are additive.

```json
{ "schema_version": 3, "plate": "...", "annotator": "...", "created": "...", "updated": "...",
  "plate_columns": { "incubation_temp_c": {"type":"free"} },
  "plate_annotations": { "incubation_temp_c": "26" },
  "columns":     { "mixture": {"type":"categorical","values":["oca2_ctrl","ctbp1_g1"]},
                   "valid_frames": {"type":"range","values":[]} },
  "annotations": { "A01": {"mixture":"oca2_ctrl","valid_frames":[30,169]} },
  "image_columns":     { "iwamatsu_stage": {"type":"categorical","values":["st1", "..."]} },
  "image_annotations": { "A01": { "168": {"iwamatsu_stage":"st24"} } } }
```

`build_db.py` reads the well level (EAV `well_annotation`) and, from v3, also
fills `plate_annotation` and `image_annotation`. Re-ingest is idempotent:
`python twinnet_clean/tools/build_db.py ingest --plate <folder>`.

## Files

| File | What |
|------|------|
| `server.py` | stdlib HTTP backend: layout discovery, TIFF→PNG (Pillow, cached), JSON API, atomic save |
| `model.py` | pure-stdlib data model: 3-scope columns, v0/v1/v2→v3 migration, registry, well/frame discovery |
| `index.html`, `static/app.js`, `static/style.css` | the GUI (vanilla JS, no build) |
| `iwamatsu_stages.json` | canonical Iwamatsu (2004) stage list (source noted inside) — **editable** |
| `defaults.json` | per-scope seed column suggestions — **editable** |

## Notes & adaptability

- **Layout discovery** is robust: v2 (`bf/<well>/SL0N/` + `fl/<well>/`), legacy v1
  (`screening/` or `crops/`), and a **flat** fallback (a bare folder of images,
  one "well" per image) — timepoints are parsed from `_LO<NNN>_`, z from `SL0N`.
  If multiple z-slices exist, the middle slice is shown.
- Suggestions accumulate in the shared registry
  `~/MedakaNet/annotation_schema.json` (the same one `screen.py` uses).
- The Iwamatsu list was transcribed from a public reproduction of Iwamatsu
  (2004); **verify against the paper** before publishing, and edit
  `iwamatsu_stages.json` freely.
