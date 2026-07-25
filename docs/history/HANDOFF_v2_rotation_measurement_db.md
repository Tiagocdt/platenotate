# HANDOFF — Annotator v2: rotation keyframes · measurements · one database

> **Status: SPEC (2026‑07‑11).** Written for a fresh session to build. Nothing here is built yet.
> Extends the **already‑built** annotator in `imaging/tools/label_annotator/`
> (stdlib HTTP `server.py` + Pillow TIFF→PNG + vanilla‑JS `static/app.js`, three
> annotation levels, schema v3; see `ANNOTATION_WEBAPP_HANDOFF.md` for the v1 build).
> The four features below are **one coherent change**: rotation and measurement are
> new *image columns*; "shared columns" and "everything in medaka.db" are the *same*
> idea — a global column registry that lives in the database.

---

## 0. Why (Tiago's words, distilled)

1. **Rotation is a manual‑keyframe job, not an auto one (for now).** Automatic body‑axis
   detection looks bad (see §6); classical CV is defeated by oil droplets + the curved,
   wrapping embryo. So: let the annotator **rotate images by hand in the image tab**, drop
   **keyframes**, and **interpolate ("fade") between them** — exactly the pattern that already
   works for `slice` focus. This is fast over the favorite wells and always looks right.
2. **Measurements** (egg size, any distance) belong in the image tab as a **new column
   *type*** — select it, draw a line, get coordinates + length saved per image.
3. **Columns must be shared across all plates** under the data root. Today each plate's
   `screening_*.json` carries its own `columns`, so they diverge (`Favorite` vs `Favorites`,
   `cab` vs `Cab`, …). A new column should appear on **every** plate (blank is fine).
4. **medaka.db should be the single source of truth**, not JSON scattered per plate. All
   tools read/write a designated place in the DB, **multiple annotators** coexist, and
   re‑running any tool **updates in place** (idempotent) instead of duplicating.

Insight that ties 3+4 together: the "shared column registry" is just a **`column_def`
table in medaka.db**. Create a column → insert one row → every plate sees it. So build the
DB‑first model (§5) and shared columns (§4) come for free.

---

## 1. Current state (verified) — what you are extending

- **App**: `label_annotator/server.py` (stdlib `http.server`, serves TIFF→PNG via Pillow),
  `static/app.js` (~57 KB, vanilla JS, the whole UI), `index.html`, `static/style.css`.
  Data model logic in `model.py`. Run: `python label_annotator/server.py [PLATE]`.
- **Three annotation levels**, stored in `metadata/screening_<plate>.json` (schema v3):
  - `columns: {name: {type, values, default?}}` — the plate's column definitions.
  - `annotations: {well: {column: value}}` — **well** level (build_db ingests this).
  - `image_annotations: {well: {"<timepoint>": {column: value}}}` — **per‑image**; this is
    where **`slice`** (best‑focus z, "1".."5") and **`iwamatsu_stage`** ("st0".."st45")
    already live. **Rotation and measurement go here too.**
  - `plate_annotations: {column: value}` — plate level (temp, date, annotator, acq_*).
- **Column types today** (`annotation_schema.json`): `categorical`, `binary`, `range`, `free`.
  The schema is split into `columns` (well), `plate_columns`, `image_columns` (has
  `iwamatsu_stage`, `slice`). This global file is the closest thing to a shared registry —
  but it has drifted (contains BOTH `Favorite` and `Favorites`).
- **Database**: `metadata_db/build_db.py` builds `imaging/data/medaka.db` — EAV tables
  `well_annotation` / `image_annotation` / `plate_annotation` (the annotation column is the
  quoted identifier `"column"`), a `well`/`image`/`plate` core, and an `image_full` view.
  **Today it is one‑way: JSON → DB, rebuilt from the JSONs. No tool writes annotations INTO
  the DB.** That is exactly what we flip.
  > The architecture‑mapping agent's precise report (exact `app.js` function names, server
  > routes, and full DB schema with line refs) will be appended as **Appendix A** — build
  > against that, but the shapes above are confirmed.

- **The interpolation already exists**: `hyperstack_video/focus_cut.py::build_focus_track()`
  reads the sparse per‑timepoint `slice` keyframes and interpolates them across all frames
  (smoothstep / linear / hold). **Rotation reuses this verbatim.** Any renderer that wants a
  per‑frame rotation reads the `rotation` keyframes and calls the same interpolation.

---

## 2. Feature A — interactive rotation with keyframes (image tab)

**Goal:** in the image tab, the annotator rotates the displayed frame; each rotation is a
**keyframe** written as an image annotation `image_annotations[well][tp]["rotation"] = <deg>`;
the render pipeline interpolates ("fades") rotation between keyframes so the embryo turns
smoothly. This is the manual, always‑correct replacement for auto‑rotation.

**Data model** — a new **image column `rotation`**, type `angle` (new numeric type):
```json
"image_annotations": { "A04": { "130": { "slice":"4", "rotation": -37.0 },
                                 "246": { "rotation": 12.5 } } }
```
- Value = **degrees** (float, CW+ or CCW+ — pick one, document it, match the renderer).
- Sparse: only keyframed timepoints are stored (like `slice`).
- Interpolation is the **consumer's** job (renderer), NOT stored per frame — identical to
  `slice`. Default interpolation = **smoothstep** with `linear`/`hold` options, reusing
  `focus_cut.build_focus_track()` (generalise it to any numeric image column, or copy it).

**UI (image tab, `app.js`):**
- The frame is drawn on a `<canvas>` (or an `<img>` in a rotating wrapper). Add a rotation
  affordance: **drag on the image to rotate about centre** (angle from centre to pointer),
  **+ a fine control** (dial or slider + numeric field + `←/→` arrow keys for ±1°, `Shift`
  for ±0.1°). Snapping to 0/90/180/270 with a modifier is a nice touch.
- **On change → write a keyframe** at the current timepoint (debounced). Show a small toast
  "keyframe @ tp130 = −37°".
- **Scrubber shows keyframes** as ticks (like markers). As the annotator scrubs *between*
  keyframes, **preview the interpolated rotation live** so "fade between each other" is
  visible in the tool, not just at render. (Compute the interpolation client‑side, mirroring
  the server/renderer formula so WYSIWYG.)
- Delete a keyframe (click its tick → delete). "Clear rotation for this well."
- **Ends held flat** (before first / after last keyframe = constant), same as focus.

**Consumers to update** (so the film actually rotates): `focus_cut.py`, `embryo_film.py`,
`well_hyperstack.py`, and the new `orient.py` should read `rotation` keyframes and rotate
each output frame by the interpolated angle. Because focus already round‑trips `slice`
through `build_focus_track`, add a sibling `build_rotation_track` (or parametrise the
existing one by column name) and apply `cv2.warpAffine` about the frame centre. **One
interpolation implementation, two columns (`slice`, `rotation`).**

---

## 3. Feature B — measurement column type (draw a line → coords + length)

**Goal:** a new column **category/type = `measurement`**. When a measurement column is the
active image annotation, the annotator **clicks twice on the image to draw a line**; on the
second click the **endpoint coordinates + length** are saved under that column's name for the
current image. Works on every image. First use case: **egg size** (diameter).

**Data model** — image column, type `measurement`, value = an object:
```json
"image_annotations": { "A04": { "1": {
    "egg_diameter": { "line": [x0,y0,x1,y1], "length_px": 512.3, "length_um": 832.5 } } } }
```
- `x0..y1` in **source‑image pixels** (map from canvas coords; account for any display
  zoom/scale — store source px so it's resolution‑independent).
- `length_px = hypot(x1-x0, y1-y0)`; `length_um = length_px * px_size_nm / 1000`.
  `px_size_nm` comes from plate metadata (`acq_px_size_nm`, 1625 nm/px at 4×) or
  `*_frame_metadata.csv` — read it, don't hard‑code; if unknown, store px only.
- Multiple measurement **columns** allowed (e.g. `egg_diameter`, `head_length`), each its own
  named line per image.

**UI (image tab):**
- Creating a column: extend "+ column" with the new type `measurement` (name only; no values).
- When a `measurement` column is **selected/active**, cursor → crosshair; **click 1 = start,
  click 2 = end**; draw the line + a length label live (px and µm). Second click **saves**.
- Re‑drawing overwrites; a small "clear" removes it for this image. Show the stored line when
  revisiting an annotated frame. `Esc` cancels an in‑progress line.
- Optional niceties: snap to nearest bright edge; show the last line as a faint ghost on the
  next frame to speed serial measuring.

---

## 4. Feature C — shared columns across all plates (merge + one registry)

**Goal:** every plate under the data root shows the **same column set** (values may be blank);
creating a column adds it **everywhere**. Kill the `Favorite`/`Favorites` split.

**Scope** = "a folder at the same height as the database" = the folder that contains
`medaka.db` (`imaging/data/`), i.e. **all plates under `imaging/data/AQ-EMBL/`** share one
registry. (Keep it generic: registry scope = the directory holding `medaka.db`.)

**Mechanism** = the **`column_def` table in medaka.db** (see §5). The annotator loads its
column set from `column_def` (filtered by level), not from the per‑plate JSON. "+ column"
inserts into `column_def` → instantly global.

**One‑time MERGE migration (write a script, keep a backup of every JSON first):**
- Case‑fold / de‑duplicate synonymous columns and values across all `screening_*.json` and
  `annotation_schema.json`:
  - `Favorite` + `Favorites` → **one** canonical (recommend `favorite`, binary, default
    false). Re‑point every annotation.
  - `line`: `Cab`→`cab`, `PFKFB3-hom_…`→`pfkfb3_her7v`, etc. `mixture`: `Oca2`→`oca2_ctrl`,
    `ctbp1-ctbp1l-dKO`→`ctbp1_ctbp1l_dKO`. (Confirm each mapping with Tiago — don't guess
    biology; the divergence list is in Appendix A.)
- Emit the merged union into `column_def`. Verify counts (no annotations dropped).
- **This is destructive to the JSONs → back up `metadata/` for every plate first**, and never
  `rm` a plate dir (workspace rule).

---

## 5. Feature D — medaka.db as the single source of truth

**Goal:** annotations live **in the DB**; tools read/write it; multiple annotators coexist;
re‑running a tool **upserts** (idempotent). JSON becomes an *export/interchange*, not truth.

**Proposed schema (additive to today's tables):**
```sql
-- global column registry (Feature C lives here)
CREATE TABLE column_def (
  name TEXT, level TEXT CHECK(level IN ('plate','well','image')),
  type TEXT,                     -- categorical|binary|range|free|angle|measurement
  values_json TEXT, default_val TEXT, created TEXT,
  PRIMARY KEY (name, level));

CREATE TABLE annotator (id INTEGER PRIMARY KEY, name TEXT UNIQUE, created TEXT);

-- annotations become writable + provenanced (annotator + timestamp), UPSERT-keyed
ALTER well_annotation  ADD annotator_id INT; ADD updated TEXT;   -- key (plate_id,well,"column",annotator_id)
ALTER image_annotation ADD annotator_id INT; ADD updated TEXT;   -- key (plate_id,well,timepoint,"column",annotator_id)
ALTER plate_annotation ADD annotator_id INT; ADD updated TEXT;   -- key (plate_id,"column",annotator_id)

-- measurements get a first-class, queryable table (in addition to the generic value)
CREATE TABLE measurement (
  plate_id INT, well TEXT, timepoint INT, name TEXT,
  x0 REAL,y0 REAL,x1 REAL,y1 REAL, length_px REAL, length_um REAL,
  annotator_id INT, updated TEXT,
  PRIMARY KEY (plate_id,well,timepoint,name,annotator_id));
```
- **Idempotency**: every write is `INSERT … ON CONFLICT(<natural key>) DO UPDATE`. Re‑running
  a tool (annotator save, `orient` auto‑rotation, `focus_cut`) updates rows in place.
- **Multiple annotators / tools**: `annotator` is any source — humans (`tiago`) *and* tools
  (`auto:orient`, `focus_cut`). So an auto‑rotation guess and a hand keyframe can coexist;
  renderers pick a preferred annotator (add an `image_full`‑style view that prefers human
  over auto, newest `updated` wins within a source).
- **Rotation** = `image_annotation` rows with `"column"='rotation'`, value = degrees.
  **Slice / stage** already fit. **Measurement** = the dedicated table (+ optionally a JSON
  blob in `image_annotation` for uniform reads).

**Migration path (sequence):**
1. Add `column_def`, `annotator`, `measurement`; add provenance columns; add upsert helpers
   to `build_db.py` (turn it from "rebuild" into a **library** other tools import).
2. Run the §4 merge; import every existing `screening_*.json` into the DB as annotator
   `tiago`. Verify against the JSONs (counts per (well,column)).
3. **Switch the annotator to DB‑first**: `server.py` load = read `column_def` + annotations
   from DB; save = upsert to DB. Keep a **"Export screening JSON"** button for portability /
   back‑compat, but DB is truth.
4. **Point the renderers at the DB** (`focus_cut`, `orient`, `well_hyperstack`, `embryo_film`,
   `plate_status`): read `slice`/`rotation`/`iwamatsu_stage` from `image_annotation`
   (JSON fallback during transition). One tiny `annotations.py` accessor module they all import.
5. Deprecate JSON‑as‑truth once parity is verified; keep JSON export.

**Risks / decisions:** SQLite + a browser tool = the backend owns all writes (no direct
client DB access); concurrent annotators → serialise writes in `server.py` (WAL mode, short
transactions). Decide whether tool‑generated (`auto:*`) annotations are visible in the
annotator UI (recommend: shown, greyed, "promote to mine" to accept).

---

## 6. Body‑axis auto‑rotation — the research track (parallel, not blocking)

Manual keyframes (§2) are the near‑term answer. In parallel, a literature/methods search is
running for a **proper** automatic embryo body‑axis detector (and whether **DINOv2 attention**
can do it — we already have ViT‑S/14 embeddings). **Its ranked recommendation will be appended
as Appendix B.** Likely candidates to evaluate: an embryo **segmentation** model (mask →
image‑moments axis, robust to droplets), **eye‑landmark** detection late (paired pigmented
spots → head + head→tail axis), and **DINO unsupervised localization** (LOST/TokenCut‑style:
ViT features → foreground the embryo → PCA the salient region → axis). Whatever wins, it
writes `rotation` keyframes as annotator `auto:<method>`, and the human corrects them in the
same UI — so §2 and §6 share one data path.

What was tried and **failed** (so nobody repeats it): classical Otsu‑egg + gradient‑texture
mask + PCA axis, with Hough oil‑droplet removal (`hyperstack_video/orient.py`). Droplet
removal fixed the *mask* but the PCA/centroid pose is not a reliable canonical axis (curved,
wrapping embryo; radial early). FL‑centroid fails because the Her7‑Venus reporter is a
*travelling* segmentation‑clock domain, not a pose landmark.

---

## 7. Build sequence (suggested)

1. **DB foundation** (§5.1): `column_def`, `annotator`, `measurement`, provenance + upsert
   helpers; make `build_db.py` importable.
2. **Merge + import** (§4, §5.2): dedupe columns/values, load JSON → DB, verify.
3. **Annotator DB‑first** (§5.3): load/save against DB; JSON export button.
4. **Rotation keyframes** (§2): UI + `rotation` image column + client‑side interpolation
   preview; generalise `build_focus_track` → renderers rotate.
5. **Measurement type** (§3): `measurement` column + line‑draw UI + `measurement` table.
6. **Point renderers at DB** (§5.4). 7. Deprecate JSON‑as‑truth (§5.5).
8. Fold in Appendix B's auto body‑axis when it lands (writes `rotation` as `auto:*`).

Each step ships independently; the app keeps working throughout.

## 8. Acceptance criteria
- Create a column once → it appears on **every** plate under `imaging/data/` (blank allowed).
  `Favorite`/`Favorites` are **one** column; no annotations lost in the merge.
- Image tab: **drag‑rotate** a frame → keyframe saved; scrubbing between keyframes shows a
  **smooth interpolated** rotation; a rendered clip (`focus_cut`/`well_hyperstack`) is rotated
  to match.
- Image tab: pick a **measurement** column → **click‑click** draws a line → length in px + µm
  saved and reloads on revisit.
- All of the above **persist in medaka.db**; re‑running the annotator or a tool **updates in
  place** (no dupes); a second annotator's values coexist and are distinguishable.
- JSON export still round‑trips for portability.

## 9. Assets to study / reuse
- This app: `label_annotator/{server.py,model.py,static/app.js,index.html,annotation_schema.json}`
  + `ANNOTATION_WEBAPP_HANDOFF.md` (v1 spec) — **Appendix A** will pin exact functions/routes.
- Interpolation to reuse: `hyperstack_video/focus_cut.py::build_focus_track` (+ `_forward_fill`).
- DB: `metadata_db/build_db.py` (EAV schema, `image_full`), `plans/25_image_database.md`.
- Renderers that must read the new columns: `hyperstack_video/{focus_cut,orient,embryo_film,well_hyperstack}.py`, `plate_status/scan.py`.
- Naming: Tiago wants a **better name than "label annotator"** — propose one (e.g. *Medaka
  Annotator* / *Screener*) when you touch it.

## 10. Open decisions for the build session
- Rotation sign convention + whether to bake rotation into rendered clips or expose as a
  DaVinci‑side transform (recommend: bake into the source clips so the film is WYSIWYG).
- Measurement: single line only, or also polyline/area later? (Start: single line.)
- Auto `auto:*` annotations visible+promotable in the UI (recommend yes).
- SQLite concurrency model (WAL + backend‑serialised writes) and whether the JSON export
  stays a first‑class feature or a temporary bridge.

---

## Appendix A — annotator + DB architecture (exact refs; build against these)

**Data flow today:** app reads/writes **one JSON per plate** (`metadata/screening_<plate>.json`,
schema v3) = source of truth. `medaka.db` is a **derived, rebuildable mirror**
(`build_db.py` reads JSON/YAML/CSV → INSERTs). **No tool writes annotations into the DB; the
app never touches the DB.** `plate_status/scan.py` opens it **read‑only** (`scan.py:197`,
`mode=ro`) — the precedent for DB reads.

**⚠️ Design refinements the map forced (read first):**
1. **Rotation must INTERPOLATE, `slice` does not.** `slice`/`iwamatsu_stage` are
   **forward‑fill** keyframe columns (`fill:"forward"`, `model.py:502‑503`; read by
   `imgEffective` = hold‑last, `app.js:819‑825`). Your rotation "fade between keyframes"
   needs a **new fill mode `interpolate`** (smoothstep/linear/hold) and a client reader
   `imgInterpolated(well,col,tp)` that lerps between the bracketing keyframes — mirror
   `focus_cut.build_focus_track()` so the in‑tool preview == the render. Write with the same
   `setImageKeyframe('rotation', deg)` machinery (`app.js:843‑862`); only the *read* differs.
2. **The image is a plain `<img id="bigImg">`, not a canvas** (`index.html:71‑73`), with **no
   transform/zoom/pan/rotate and no listeners** today. `#stage` is `position:relative;
   overflow:hidden` (`style.css:107‑110`) — a ready mount. So: rotation = CSS
   `transform: rotate()` on `#bigImg`; line‑draw = an absolutely‑positioned **SVG/canvas
   overlay** on `#stage`. Both interaction layers are net‑new.
3. **Measurement value is an OBJECT** (`{line,length_px,length_um}`). It rides the plain
   per‑frame path `doAssign('image', name, value)` (`app.js:656‑667`) → writes
   `image_annotations[well][tp][name]`. But `_clean_image_annos` (`model.py:553‑582`)
   currently expects scalars — **extend it to accept the measurement object** (and teach
   `build_db.parse_screening_levels` to serialise it, e.g. JSON text in `image_annotation.value`
   or the dedicated `measurement` table).
4. **`build_db` is delete‑then‑insert per plate** (`_delete_plate` `:423‑434` then re‑INSERT
   inside one txn `:538`) — idempotent by *wholesale replace*, not row upsert, and only
   JSON→DB. DB‑first (§5) therefore needs a **real write path + true upserts** (new), not a
   tweak to this. Keep `build_db` as the importer/back‑fill.
5. **Clean up first:** `AQV04_ctbp1-1-2_as2` has a **stale duplicate screening JSON at the
   plate root** (23 img‑ann wells vs metadata's 21) — an orphan that drifts. All consumers
   prefer `metadata/`; delete the root copy during the merge (back it up first).

**Column model** — `COLUMN_TYPES=("categorical","binary","range","free")` (`model.py:48`),
`SCOPES=("plate","well","image")` (`model.py:49`); column def = `{type,values[],default?,fill?}`
(`_clean_columns` `model.py:479‑505`). Types reach the client via `/api/config`
(`server.py:266`) → add‑column form (`app.js:710‑737`, `addColumn`). New types to add:
**`angle`** (rotation) and **`measurement`**. Suggestions come from `defaults.json` +
`annotation_schema.json` (the union‑on‑save registry, `_merge_registry` `model.py:670‑697`);
neither enforces a shared set — that's why plates diverge (§F below).

**Screening JSON v3 keys** (`fresh_payload` `model.py:418‑432`; live order): `schema_version,
plate, annotator, created, updated, plate_columns, plate_annotations, columns, annotations,
image_columns, image_annotations`. Shapes: `plate_annotations` = flat `{col:val}`;
`annotations` = `{well:{col:val}}` (range=`[s,e]`); **`image_annotations` =
`{well:{"<tp>":{col:val}}}`** (tp keys are strings, only keyframes stored). `slice` +
`iwamatsu_stage` confirmed live here.

**Key JS handlers** (image tab): render `updateBigImg()` (`app.js:411‑421`, sets
`img.src=frameURL(...,sliceAt(tp))`); change tp `setFrame(i)` (`app.js:440‑444`; scrubber
`#scrub` `:1008`, keys `[`/`]` `:1086`); keyframe write `setImageKeyframe(col,val)`
(`app.js:843‑862`); forward‑fill read `imgEffective` (`:819`); plain per‑frame write
`doAssign('image',...)` (`:656`); all edits → `mutate()` (`:113`) → debounced `scheduleSave`
(`:139`) → `saveNow` (`:141`). Debug hook `window._dbg` (`:1120`).

**API** (stdlib `BaseHTTPRequestHandler`): `GET /api/config` (`server.py:249`), **`GET
/api/plate?dir=NAME`** load (`:357`, → `model.load_payload` `model.py:439`), **`GET
/api/frame?dir&well&ch&tp&size&z`** PNG (`:365`, LRU), **`POST /api/save {dir,payload}`**
(`:396` → always writes `metadata/screening_<plate>.json` `:408`, atomic tmp+replace,
merges registry). Client owns all state/undo.

**DB schema** (`build_db.py:74‑179`): `plate, mix, guide, well, image` core;
EAV `well_annotation(plate_id,well,"column",value)` PK(plate_id,well,"column")` `:129`;
`plate_annotation(plate_id,"column",value)` `:141`;
`image_annotation(plate_id,well,timepoint,"column",value)` PK incl timepoint `:148`
(`column` is quoted — reserved word). View `image_full` (`:163`) joins image+well+mix+plate
(fixed cols; EAV pivot done in Python `export_full` `:669`). **Any new image column (rotation,
measurement) lands in `image_annotation` with zero schema change — but only after a
`build_db` re‑run**; there is no live write‑through today (that's §5's job).

**F. Column divergence (concrete):** WELL cols by plate — AQV02/03: phenotype,viability,line,
position_quality,injected?; AQV04: +empty,mixture,valid_frames,**Favorite**; AQV05:
empty,position_quality,…; AQV06: base 4; AQV07: +injection_quality,mixture,**Favorites**.
Image cols (slice,iwamatsu_stage) only on AQV04/05/07. Value dups in the registry: `line`
cab/**Cab**, pfkfb3_her7v/**PFKFB3‑hom_…**; `mixture` oca2_ctrl/**Oca2**,
ctbp1_ctbp1l_dKO/**ctbp1‑ctbp1l‑dKO**. Plate cols diverge 5/6/10. → the §4 merge list.

---

## Appendix B — body‑axis auto‑detection method (verified literature review)

**Bottom line:** there is **no turnkey tool** that outputs a per‑frame body‑axis angle for
in‑chorion fish embryos. Tellingly, **EmbryoNet and TwinNet — your own lineage (Müller lab) —
deliberately avoid pose estimation**: EmbryoNet gets rotation‑invariance by running each crop
8× (flips + ±90°) and averaging. So first sanity‑check: for a *film* you genuinely need the
explicit angle (visual canonical pose), so pose estimation is warranted — but for any
*classification/embedding* use, augmentation is cheaper than a pose estimator.

**Your DINO intuition is the #1 recommendation — and it's documented to work on microscopy.**
The pipeline "DINOv2 patch features → foreground the embryo → axis" is exactly the
unsupervised‑object‑localization family (DINO, Caron ICCV'21 → LOST → **TokenCut**), and
**DINOSim** (napari plugin) demonstrates zero‑shot DINOv2 segmentation on electron‑microscopy
images. We already have DINOv2 ViT‑S/14 patch features for these plates.

**Ranked route to a reliable per‑frame axis** (decompose: undirected axis = easy‑ish;
head/tail polarity = needs landmarks; radial early stages = partly ill‑posed). Exploit two
levers we uniquely have: **DINOv2 features already computed** + **430–715 sequential frames**
(embryo turns slowly → temporal continuity fixes 180° flips and noise).

1. **DINOv2 foreground → SECOND‑MOMENT axis → temporal smoothing** *(lowest effort, reuses our
   embeddings)*. Normalized‑Cut on patch tokens (TokenCut) or reference‑patch similarity
   (napari‑DINOSim) → embryo foreground mask → **ellipse / second‑moment principal axis** —
   NOT raw PCA. (Published confirmation of our exact failure: *J. Comput. Biol.*
   10.1089/cmb.2018.0165 — PCA A–P axes shift under incomplete segmentation; ellipse/2nd‑moment
   fitting is the hardened form.) Then smooth the angle over time. **Feature‑space foreground
   rejects oil droplets far better than our Otsu+Hough.** Gives an *undirected* axis. ~1–2 days.
2. **micro‑sam (microscopy‑tuned SAM) or Cellpose → mask → axis.** If Rank‑1 masks are ragged
   (coarse ~41×41 grid). Few prompts / light finetune. **If the FL reporter marks the body,
   segment on FL — much easier than BF.**
3. **Eye‑landmark polarity** to turn the undirected axis into a *directed* head‑up pose:
   DeepLabCut/SLEAP (annotate ~100–300 frames, 2–4 keypoints = eyes + yolk/tail) or
   FishInspector (eye/otolith/notochord landmarks; zebrafish‑tuned, lateral, mid/late).
   Combine: mask gives the axis line, eyes resolve which end is the head; propagate polarity
   backward in time.
4. **Direct sin/cos angle‑regression CNN** (Fischer/Dosovitskiy/Brox GCPR'15), self‑supervised
   by known augmentation rotations + a temporal‑smoothness loss, anchored by a few hundred
   hand‑canonicalized frames — which the §2 rotation‑keyframe tool will produce for free.

**Hard limits:** oil droplets → solved implicitly by Ranks 1–2. Genuinely radial early /
animal‑pole‑down (axial) views → the in‑plane axis is **undefined**; no method escapes this —
only temporal propagation from the first stage where the axis emerges helps.

**Integration:** whatever wins **writes `rotation` keyframes as annotator `auto:<method>`**
into the same image‑annotation store (§2/§5); the human accepts/corrects them in the annotator
UI. So auto and manual rotation share one data path — build §2 first, bolt Rank‑1 on later.

**Verified sources:** TokenCut github.com/YangtaoWANG95/TokenCut (arXiv 2209.00383) ·
napari‑DINOSim github.com/AAitorG/napari‑DINOSim (bioRxiv 2025.03.09.642092) · DINO Caron ICCV
2021 · micro‑sam github.com/computational-cell-analytics/micro-sam (PMID 39939717) ·
FishInspector github.com/sscholz-UFZ/FishInspector (PMC6358258) · EmbryoNet PMC10250202 ·
PCA‑instability / ellipse fitting 10.1089/cmb.2018.0165. (All retrieved + checked; the agent
omitted anything it could not verify.)
