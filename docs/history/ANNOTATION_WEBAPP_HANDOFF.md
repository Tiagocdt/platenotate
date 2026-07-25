> ✅ **BUILT (2026-07-08).** This app now lives in **`annotation_app/`** — a
> zero-dependency stdlib HTTP backend (`server.py`, TIFF→PNG via Pillow) +
> vanilla-JS frontend, with all three levels (plate/well/image), rubber-band
> select, a trajectory fader, and Iwamatsu image-staging. It writes
> `screening_<plate>.json` at **schema v3** (well level byte-compatible with v2),
> and `twinnet_clean/tools/build_db.py` was extended with additive
> `plate_annotation` + `image_annotation` tables. Run:
> `python annotation_app/server.py [PLATE]`. See `annotation_app/README.md`.
> The section below is the original spec it was built against.

# HANDOFF — Medaka annotation **web app** (build in a fresh session)

> Purpose of this file: everything a new session needs to build a **web-based** image-annotation
> tool that replaces the matplotlib `screen.py`. It must be **self-explanatory** (other annotators
> can use it), **adaptable** (folder structures + file naming), handle **three annotation layers
> (plate / well / image)**, and output a JSON the existing database builder ingests. Real GUI —
> buttons, dropdowns, rubber-band multi-select — **no terminal prompts**.

---

## 1. Where this fits (the pipeline)
`segment → ANNOTATE (this tool) → build (medaka.db)`
- **Upstream:** raw ACQUIFER frames → segmentation (`twinnet_clean/cluster/run_pipeline.sh`) → a
  **processed plate folder**: `bf/<well>/SL0z/*.tif`, `fl/<well>/*.tif`, `<plate>_hyperstack.tif`,
  `<plate>_BF_gray.mp4`, `<plate>_frame_metadata.csv`, `plate_metadata.json`, `plate_raw_index.json`.
- **This tool:** opens that folder → shows wells + trajectories → annotator creates columns/values
  and tags wells (+ optionally plate + individual images) → writes `screening_<plate>.json`.
- **Downstream:** `twinnet_clean/tools/build_db.py` ingests `screening_<plate>.json` into `medaka.db`
  (EAV table `well_annotation`). Design doc: `metameda/plans/25_image_database.md`.

## 2. Why replace `screen.py` (real pain points — see the pasted session log below)
- **Column/value editing runs in the TERMINAL** (the `e` menu prints prompts to stdout, reads stdin) —
  which defeats having a GUI. This is the #1 reason for the rebuild.
- No **rubber-band** multi-well select (you must type ranges like `A01:D12`).
- **Binary columns don't default** — e.g. an `empty` column: you want *unselected = false, selected =
  true*, but the tool leaves unselected wells simply unannotated.
- matplotlib is limiting for real controls (buttons / dropdowns / menus / drag).
- Registry suggestions can cause **wrong-value errors**: Tiago accidentally applied AQV07's
  `ctbp1_ctbp1l_dKO` to **AQV04**, which was actually `ctbp1_g1` (single guide). The UI must make the
  *active plate's* values obvious and creating a new value trivial, so this can't happen silently.

## 3. Three annotation layers (all three in the tool)
1. **Plate-level** (whole folder): incubation temperature, date, start time, notes, annotator.
   Some auto-fill from `plate_metadata.json` / first-frame timestamp (offer, let the user confirm).
2. **Well-level** (main): user-defined columns — e.g. `mixture` (injection), `viability` (dead/alive),
   `injection_quality` (good/bad — a *pre-screen*; the real readout is mGOLD intensity later), `line`,
   `dev_timing`, and a **`valid_frames` RANGE** (a good timepoint window chosen on the trajectory).
3. **Image-level** (opt-in per well): toggle on, fade through the well's development, and **stage a
   specific frame with Iwamatsu stages**. Output keyed by (well, timepoint). This doubles as a fast
   staging tool feeding the developmental-timing model.

## 4. UI / UX (keep the spatial structure Tiago likes; responsive for other sizes)
Full-screen layout:
- **LEFT — overview grid.** 8×12 for a plate, BUT the input may be an **arbitrary set of wells (even
  across plates)**, so: **pagination** (next/prev, configurable wells-per-page). Each cell = a
  thumbnail (middle z-slice, one representative frame) + a small badge showing the *active column's*
  value. **Rubber-band drag selects multiple cells.**
- **UPPER-RIGHT — detail view** of the selected well + a **scrubber/fader** through its development
  beneath it (slider over timepoints; use the full trajectory if you can keep it smooth, else a
  subsampled set or the per-well mp4).
- **BELOW THAT — the annotation panel (the heart — really design this):**
  - lists **existing columns** (none if none) with the current value for the selection;
  - **"+ column"** → name field + **type dropdown** (categorical / binary / range / free) + values;
  - value **chips / dropdowns** to assign; **binary = a toggle defaulting to false**;
  - a selection (rubber-band or click) + one click assigns a value to **all** selected wells;
  - **range** type = drag start/end on the trajectory scrubber (for `valid_frames` / good-quality);
  - registry suggestions shown as add-able chips (previously-used columns/values);
  - the **Iwamatsu image-staging** control appears when image-level is toggled.
- Undo/redo, autosave, an **annotator name** field.

## 5. Input format — be FLEXIBLE
- Primary: the v2 processed layout `bf/<well>/SL0z/*.tif` (+ optional `fl/<well>/*.tif`, hyperstack,
  mp4, `*_frame_metadata.csv`). **If multiple z-slices, display the middle slice.**
- Be adaptable to slightly different structures + naming — don't hard-code the exact crop-name regex.
  Discover wells + frames robustly: parse `_LO(\d+)_` = timepoint, `SL0?(\d+)` = z from filenames;
  accept flat or nested; fall back to `crops/` or `screening/` dirs. `screen.py::_detect_layout` +
  its bf/fl globbing is a starting point.
- May be a **subset of wells** or **multiple plates merged** — never assume 96 wells / one plate.
- If present, load an existing `screening_<plate>.json` (edit mode) + the global registry
  `~/MedakaNet/annotation_schema.json` for suggestions.

## 6. Image handling — the technical crux (browsers can't show TIFF)
The crops are 8-bit TIFFs. Pick an approach:
- **Client-side decode** (UTIF.js / geotiff.js → `<canvas>`): fully portable (open an HTML file), but
  decoding hundreds of frames/well can lag → lazy-decode + cache, use the middle slice, and for the
  fader pre-decode a downsampled set or use `<plate>_BF_gray.mp4` / a per-well webp.
- **Light local backend** (FastAPI/Flask + Pillow) serving crops as PNG/JPEG on demand + a JSON API
  listing wells/frames: simplest for a smooth trajectory fader and for hosting to other annotators.
- **Pre-render** (nice addition): add a stage to `run_pipeline.sh` that emits a small per-well
  preview webp/mp4 so the fader is instant.
**Recommendation:** a small FastAPI backend + JS frontend for performance and the "share with other
annotators" case; keep client-side as an option for zero-install portability. This is an open decision.

## 7. Output schema — extend v2 → v3, stay `build_db`-compatible
Current v2 (well-level) that `build_db.py` reads today:
```json
{"schema_version":2,"plate":P,"columns":{name:{"type","values"}},"annotations":{well:{col:val}}}
```
Extend to three levels + metadata, **keeping the well-level shape unchanged** so `build_db` keeps working:
```json
{"schema_version":3,
 "plate":P, "annotator":"tiago", "created":"<iso8601>",
 "plate_annotations":{"incubation_temp_c":26,"date":"2026-06-27","start_time":"16:58"},
 "columns":{name:{"type","values","default":null}},
 "annotations":{well:{col:val}},                       // well-level — build_db ingests THIS
 "image_annotations":{well:{"<timepoint>":{"iwamatsu_stage":"st24"}}},
 "valid_frames":{well:[start,end]}                     // or as a range column in annotations
}
```
**Coordinate with `build_db.py`:** it ingests `columns`+`annotations` (well-level, EAV). The new
`plate_annotations` + `image_annotations` need small additions there (a `plate_annotation` and an
`image_annotation` table) to land in the DB — flag as a follow-up task; the tool can emit v3 now and
build_db v3-awareness is a separate small change.

## 8. Iwamatsu staging (image layer)
Medaka developmental staging = **Iwamatsu (2004), stages 1–40**. The tool lets the annotator, while
fading through a well, mark "this frame = stage N". **Source the canonical stage list + names from the
reference — do NOT fabricate stage numbers/names.** (Iwamatsu, T. 2004, *Mech. Dev.* 121:605–618.)

## 9. Assets to study / reuse
- `twinnet_clean/tools/screen.py` — current tool. Its **`ScreeningModel` class is the data model**
  (columns/values/annotations, migration v0/v1→v2, registry, undo, atomic save) — **reuse the logic,
  replace the UI**. `_detect_layout` + bf/fl globbing = layout discovery.
- `twinnet_clean/tools/build_db.py` — downstream contract (`parse_screening` v2 reader, EAV schema).
  Keep output ingestible.
- `~/MedakaNet/annotation_schema.json` — the cross-plate registry (columns/values union) → suggestions.
- `metameda/plans/25_image_database.md` — DB design (3 levels, EAV, `image_full`).
- Example data: `data/AQV04_ctbp1-1-2_as2/` and `data/AQV03_ctbp2a-2_as1/` (real bf/fl + `screening_*.json`).
- `~/MedakaNet/HOW_TO_USE.md` — current workflow. This file's Appendix A = a real annotation session log.

## 10. Acceptance criteria
- Open a processed plate folder in the browser → well grid + a scrubbable per-well trajectory.
- Create a column (name + type + values) **entirely via GUI** (no terminal); **binary defaults to false**.
- **Rubber-band** select multiple wells → assign a value in one action.
- Set a `valid_frames` range by dragging on the trajectory.
- Toggle image-level → assign an Iwamatsu stage to a specific frame, and/or the **best-focus z-slice** (image column `slice`, value = the SL number). This becomes `well_hyperstack.py`'s default movie plane.
- Enter plate-level fields (temp / date / start) + annotator name.
- Output `screening_<plate>.json` (v3) that `build_db.py` ingests without error (well-level ≥).
- **Export the current filter/selection → `wells_filter.json`** = `{"by_plate": {"<plate>": ["A01","B03",…], …}}` (just the well ids per plate; keep the key `by_plate`). Consumed by `tools/well_hyperstack.py --from-json FILE [--movie]` to render TIFF hyperstacks or movies of exactly the filtered embryos. This closes the filter→render loop.
- Adaptable: works on a slightly different layout/naming; handles a non-96 / multi-plate set with pagination.
- Not laggy on a ~430-timepoint well.

## 11. Open decisions for the new session
- Client-side (File System Access API + UTIF.js) vs **local backend (FastAPI + Pillow)** vs desktop
  (Tauri/Electron). [Backend recommended for the fader + sharing.]
- Framework: React / Svelte / vanilla.
- Delivery to other annotators (hosted service? packaged folder they run locally?).
- Whether `run_pipeline.sh` should pre-render per-well preview videos (perf win).

---

## Appendix A — real annotation session (the pain, verbatim)
Tiago ran `python twinnet_clean/tools/screen.py AQV04`. It loaded 96 wells, then to add columns it
dropped into **terminal prompts** ("choice > n", "new column name >", "type [categorical/binary/...]
>", "comma-separated values >"), and to set a well's value it prompted "value (name, or index) >".
Result: annotating in the terminal, not the GUI; a binary `empty` column with no false-default; and a
`mixture` value picked from the registry that was wrong for the plate. That whole flow is what the web
app must replace with on-screen controls. (Full log is in the chat that produced this handoff.)

## Appendix B — data-model quick reference (from `screen.py::ScreeningModel`)
- column: `{name: {"type": categorical|binary|range|free, "values": [...]}}`
- annotation: `{well: {column: value}}`; range value = `[start,end]` ints; untagged wells omitted.
- registry (`annotation_schema.json`): `{"columns": {name: {"type","values"}}}` unioned across plates.
- migration handled for old string/`{phenotype,mixture}` files. Atomic save (tmp + replace).
