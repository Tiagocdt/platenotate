# MEASURE_TOOL_INTEGRATION.md

Integration guide for **`static/measure_tool.js`** — the line-measurement tool
(a new image-column **type `measurement`**) for the Medaka annotator.

The tool is a **self-contained, dependency-free, vanilla-JS plugin**. It edits no
existing file. It talks to the host only through a documented global
`window.AnnotatorAPI`, feature-detecting every method so a missing one degrades
gracefully instead of throwing.

Implements HANDOFF `§3 Feature B — measurement column type` and Appendix A
refinement 3 (measurement value is an OBJECT riding the plain per-frame
`doAssign('image', …)` path).

---

## 1. Files added (only these)

| File | Purpose |
|---|---|
| `static/measure_tool.js` | The plugin: pure geometry/length helpers + the browser controller (SVG overlay, click-click line draw, panel controls). |
| `static/tests/measure_tool.test.mjs` | Node unit tests for the pure helpers (contain-fit mapping, `length_px`, `length_um`). |
| `MEASURE_TOOL_INTEGRATION.md` | This document. |

Run the tests (from the app root, no deps/build):

```bash
node static/tests/measure_tool.test.mjs      # → "measure_tool: 12 passed, 0 failed"
```

---

## 2. Add the script tag

In `index.html`, load it **after** `app.js` (so `window.AnnotatorAPI` — set up by
the host during boot — can be discovered), right before `</body>`:

```html
<script src="/static/app.js"></script>
<script src="/static/measure_tool.js"></script>   <!-- ADD THIS LINE -->
```

`server.py` already serves anything under `static/` (`_serve_static`,
`server.py:339`), so no server change is needed to deliver the file.

**Wiring order is not fragile.** On load the module publishes `window.MeasureTool`
and auto-installs: it installs immediately if `window.AnnotatorAPI` already exists,
otherwise it polls briefly (≤10 s) and also listens once for a
`window` event `'annotator-api-ready'`. The host may instead install explicitly
(idempotent):

```js
window.MeasureTool.install(AnnotatorAPI);
// or, after you assign window.AnnotatorAPI:
window.dispatchEvent(new Event('annotator-api-ready'));
```

---

## 3. The `AnnotatorAPI` methods this tool uses

Every call is feature-detected (`typeof api.x === 'function'`); absent methods are
skipped, never called blindly.

| API member | How the tool uses it |
|---|---|
| `API.registerTool('measurement', handlers)` | Primary registration. Handlers provided: `onActivate(colName)`, `onDeactivate()`, `onImageMouseDown(ev,pt)`, `onImageMouseMove(ev,pt)`, `onImageMouseUp(ev,pt)`, `onRender(well,tp)`. |
| `API.onColumnPanel('measurement', renderFn)` | Renders the per-column panel controls (current length px+µm, **clear** button, hint). |
| `API.stageEl` / `API.imgEl` | Mounts the SVG overlay inside `#stage`; reads `#bigImg` natural size + box for source↔display mapping. Falls back to `document.getElementById('stage'|'bigImg')`. |
| `API.curWell()` / `API.curTp()` | Which (well, timepoint) to read/write. Falls back to `API.state.primary`. |
| `API.pxSizeNm()` | nm/px for `length_um`. Returns a number or `null`. |
| `API.doAssign('image', colName, value)` | Commits the measurement object (`value`) or clears it (`value = null`). |
| `API.imgValue(well, tp, col)` | Reads the stored measurement back to redraw the line + label on revisit and to fill the panel. |
| `API.state` | Read-only fallback for `primary` well. |
| `API.mutate(fn)` | Not called directly — the tool routes all writes through `doAssign`, which itself wraps `mutate` in the host. |

`pt = {x, y}` is in **SOURCE-image pixels** in the mouse handlers (the host has
already mapped canvas→source). Coordinates are clamped to the image bounds.

---

## 4. What the host must provide (the `AnnotatorAPI` shim)

`window.AnnotatorAPI` does not exist in `app.js` yet — add a thin shim over the
existing internals. Mapping to today's `app.js` (verified line refs):

```js
window.AnnotatorAPI = {
  get state(){ return { payload: state.payload, primary: state.primary, plateDir: state.plateDir }; },
  curTp,                                   // app.js:402
  curWell: () => state.primary,
  get stageEl(){ return document.getElementById('stage'); },   // index.html:71
  get imgEl(){  return document.getElementById('bigImg'); },   // index.html:72
  mutate,                                  // app.js:113
  doAssign,                                // app.js:640 (scope 'image' → :656-667)
  imgValue: (well, tp, col) =>
    ((state.payload.image_annotations[well] || {})[String(tp)] || {})[col],
  pxSizeNm,                                // see §7
  registerTool(type, h){ (this._tools ||= {})[type] = h; },
  onColumnPanel(type, fn){ (this._panels ||= {})[type] = fn; },
};
```

Then, at the four existing hook points, notify the active tool:

1. **Activate / deactivate** — when the active *image* column changes (e.g. the
   user selects a `measurement` column in the Image tab, `renderImage`
   `app.js:864`): if the column's type has a registered tool, call
   `tool.onActivate(colName)`; when leaving it, `tool.onDeactivate()`.
2. **Render** — after `updateBigImg()` / `setFrame()` (`app.js:411,440`), call
   `activeTool?.onRender(state.primary, curTp())` so the stored line redraws on a
   new frame/well.
3. **Column panel** — in `colRow` / `renderImage`, for a `measurement` column call
   `AnnotatorAPI._panels.measurement(container, colName)` to render its controls.
4. **Mouse events (optional).** The tool's overlay already captures pointer input
   itself (see §5), so **wiring `onImageMouseDown/Move/Up` is optional.** If you
   prefer the host to own input, attach listeners to `#stage`, map client→source
   with the exported helper, and dispatch:

   ```js
   // window.MeasureTool.canvasToSource is a global (no import/build step)
   const box = imgEl.getBoundingClientRect();
   const src = window.MeasureTool.canvasToSource(
       ev.clientX - box.left, ev.clientY - box.top,
       imgEl.naturalWidth, imgEl.naturalHeight, box.width, box.height);
   activeTool.onImageMouseDown(ev, src);   // src = {x,y} in source px
   ```

   Driving both paths is safe: the tool **de-duplicates** a single physical event
   handled twice (shared-event tag + a short time/space backstop).

**Minimum viable integration = hooks 1–3** (activate/deactivate, render, panel).
The overlay self-drives clicks, so you can skip hook 4 entirely.

---

## 5. Interaction model (what the tool does when active)

- On `onActivate(colName)` it mounts (once) an absolutely-positioned `<svg>`
  overlay inside `#stage` (`#stage` is `position:relative; overflow:hidden`,
  `style.css:107` — a ready mount), sets its `pointer-events:auto` and cursor to
  crosshair. On `onDeactivate()` → `pointer-events:none` and the overlay clears.
- **Click 1** = start; **mouse-move** shows a live dashed rubber-band line with a
  px/µm label; **Click 2** = commit. A **press-drag** (mousedown → move ≥ 6 px →
  mouseup) also commits, for speed. `Esc` cancels an in-progress line
  (capture-phase, so it pre-empts the app's own `Escape` handler).
- On commit the tool computes the length and calls
  `API.doAssign('image', colName, { line, length_px, length_um })`.
- `onRender(well, tp)` (and the image's own `load` event, and stage resize via
  `ResizeObserver`/`window resize`) redraw the **stored** line for the active
  column so it reappears when you revisit an annotated frame.
- Degenerate zero-length lines (`length_px < 1`) are ignored.

Visuals match the app register: amber accent (`#e8a33d`) line with a dark
contrast underlay, small endpoint handles, and a mono px/µm label on a translucent
chip — no gradients/emojis.

---

## 6. The stored value (data model)

Image-scope, per-frame, keyed by column name — the shape from HANDOFF §3 /
Appendix A(3):

```json
"image_annotations": {
  "A04": {
    "1": {
      "egg_diameter": { "line": [x0, y0, x1, y1], "length_px": 512.31, "length_um": 832.5 }
    }
  }
}
```

- `line` = `[x0,y0,x1,y1]` in **source-image pixels** (rounded to 2 dp),
  resolution-independent.
- `length_px = hypot(x1-x0, y1-y0)` (2 dp).
- `length_um = length_px * pxSizeNm / 1000`, or **`null`** when the px size is
  unknown.
- Multiple `measurement` columns may coexist (e.g. `egg_diameter`, `head_length`)
  — each stores its own named line per image.

**Client-side persistence already works**: `doAssign('image', …)` stores the value
object as-is in `image_annotations` (`app.js:656-667`), and the client keeps it
verbatim. The only blocker is the server-side normalizer — see §8.

---

## 7. `pxSizeNm()` — where the plate px size comes from

Return **nm/px** as a number (e.g. `1625.0` at 4×), or `null`. Suggested lookup
(first hit wins):

1. Plate annotation `acq_px_size_nm` — `state.payload.plate_annotations['acq_px_size_nm']`
   (a plate column; per HANDOFF §3 and the `plate_annotations` bucket).
2. The plate's `*_frame_metadata.csv` column `px_size_nm` (median), mirroring how
   `model._plate_autofill` reads that CSV (`model.py:393`). The DB already carries
   this per image as `image.px_nm` (`build_db.py:171`+, `image_full` view).

```js
function pxSizeNm(){
  const v = Number((state.payload.plate_annotations || {})['acq_px_size_nm']);
  return Number.isFinite(v) && v > 0 ? v : null;
}
```

If unknown, the tool stores `length_px` only and `length_um = null` (and the panel
says "px size unknown — µm not stored").

---

## 8. Backend: accept the object value

### 8a. `column_def` shape (type `measurement`, level `image`)

A measurement column is just an image column whose type is `measurement`:

```jsonc
// state.payload.image_columns
"egg_diameter": { "type": "measurement", "values": [] }   // no value list
```

To make the type selectable in the "+ column" form it must be in the engine's type
list (`/api/config` derives `column_types` from `model.COLUMN_TYPES`,
`server.py:266`; the form reads `state.cfg.column_types`, `app.js:713`):

```python
# model.py:48
COLUMN_TYPES = ("categorical", "binary", "range", "free", "angle", "measurement")
```

> **Status — already present in the working tree (verified `model.py:48`).** The
> rotation workstream added `angle` + `measurement`. Listed here so a clean
> checkout knows it is a prerequisite.

`_clean_columns` (`model.py:479`) then keeps `type:"measurement"` instead of
coercing it to `categorical`. `_register_value` only appends string values for
`categorical`/`binary`, so an object value never pollutes a value list.

`build_db.py` already registers image columns into the global **`column_def`**
table with exactly this type (its comment lists `…|angle|measurement`,
`build_db.py:141`; `upsert_column_def` at `:246`, called from the import loop
`:664-669`). So the shared-registry side is ready.

### 8b. `model._clean_image_annos` must accept the object

> **Status — already implemented in the working tree (verified `model.py:196`).**
> `_coerce_value` now short-circuits `ctype == "measurement"` and returns the dict
> unchanged (`return val if isinstance(val, dict) and val else None`), so the
> object round-trips through `normalize_payload → _clean_image_annos`
> (`model.py:553-582`). No further change needed for save. The reference
> implementation below (a stricter validator) is kept for a clean checkout or if
> you want structural validation of `line`.

Without that change, `_coerce_value` would stringify anything that isn't a
range/list, corrupting a measurement dict to `"{'line': …}"` on save. The stricter
reference form:

```python
# model.py — extend _coerce_value(); add _coerce_measurement()
def _coerce_value(ctype, val):
    if val is None:
        return None
    if ctype == "measurement" or (isinstance(val, dict) and "line" in val):
        return _coerce_measurement(val)
    if ctype == "range" or isinstance(val, (list, tuple)):
        return _coerce_range(val)
    s = val.strip() if isinstance(val, str) else str(val).strip()
    return s or None

def _coerce_measurement(val):
    """A {line:[x0,y0,x1,y1], length_px, length_um} object, validated."""
    import math
    if not isinstance(val, dict):
        return None
    line = val.get("line")
    if not (isinstance(line, (list, tuple)) and len(line) == 4):
        return None
    try:
        x0, y0, x1, y1 = (float(c) for c in line)
    except (TypeError, ValueError):
        return None
    lp = val.get("length_px")
    try:
        lp = float(lp) if lp is not None else math.hypot(x1 - x0, y1 - y0)
    except (TypeError, ValueError):
        lp = math.hypot(x1 - x0, y1 - y0)
    lu = val.get("length_um")
    try:
        lu = float(lu) if lu is not None else None
    except (TypeError, ValueError):
        lu = None
    return {"line": [x0, y0, x1, y1], "length_px": lp, "length_um": lu}
```

This is the **one required backend change** for save/round-trip to work. (The
client already stores and re-reads the object; the server just needs to stop
coercing it to a string.)

### 8c. `build_db.py`: serialize + route to the `measurement` table

The DB foundation already exists (verified): the `measurement` table
(`build_db.py:183-194`, PK `(plate_id,well,timepoint,name,annotator_id)`) and
`upsert_measurement(...)` (`:283`). Two small additions make the importer populate
it:

1. In `parse_screening_levels._vt` (`build_db.py:472`), serialize a dict as JSON so
   `image_annotation.value` carries a uniform blob:

   ```python
   def _vt(val, ctype=None):
       if isinstance(val, dict):                       # measurement object
           return json.dumps(val, separators=(",", ":"))
       if ctype == "range" or isinstance(val, (list, tuple)):
           return None if val is None else json.dumps(list(val), separators=(",", ":"))
       if val is None or val == "":
           return None
       return str(val)
   ```

2. In the plate import loop (`build_db.py:721-729`), after
   `upsert_image_annotation`, also upsert measurements. Scan the raw screening JSON
   for image values that are dicts with a 4-element `line` and call the existing
   helper:

   ```python
   for well, per_tp in (screening_json.get("image_annotations") or {}).items():
       for tp, entry in (per_tp or {}).items():
           for name, val in (entry or {}).items():
               if isinstance(val, dict) and isinstance(val.get("line"), (list, tuple)) \
                       and len(val["line"]) == 4:
                   x0, y0, x1, y1 = val["line"]
                   upsert_measurement(conn, plate_id, well, int(tp), name,
                                      (x0, y0, x1, y1),
                                      val.get("length_px"), val.get("length_um"), aid)
   ```

   (`upsert_measurement` takes `coords=(x0,y0,x1,y1)`; it unpacks them into
   `x0,y0,x1,y1`, `build_db.py:283-291`.)

No schema migration is required — the tables and indexes already exist
(`idx_measure_plate_well`, `:199`).

---

## 9. Exported surface (for hosts / tests)

`window.MeasureTool` = `require('./measure_tool.js')`:

- Pure helpers (unit-tested, DOM-free): `containFit`, `canvasToSource`,
  `sourceToCanvas`, `clampToImage`, `lengthPx`, `lengthUm`, `measurementValue`,
  `round2`.
- Lifecycle: `install(api)`, `activate(colName)`, `deactivate()`, `redraw()`,
  `renderPanel(container, colName)`, `COLUMN_TYPE` (`"measurement"`), `VERSION`.

`canvasToSource(cx, cy, natW, natH, boxW, boxH)` and its inverse implement the
`object-fit:contain` letterbox math (`scale = min(boxW/natW, boxH/natH)`, centered
offsets). `cx,cy` are **box-local** display px; the result is source px.

---

## 10. Assumptions & notes

- **`object-fit: contain`** on `#bigImg` (`style.css:110`) is the letterbox model
  the mapping assumes. The controller composes *two* transforms: the flex-centered
  `<img>` box within `#stage`, then the contain-letterbox of the natural image
  within that box — so coordinates are correct even though `#bigImg` is centered
  and may not fill `#stage`.
- **Source px are stored**, not display px, so measurements are independent of the
  600-px server render size (`updateBigImg`, `app.js:415`) and of window size.
  The displayed image must correspond to the same source frame the coordinates were
  taken on (it does: one `#bigImg` per (well, tp)).
- **Single straight line** per column per image (HANDOFF §10 "Start: single
  line"). Polyline/area is out of scope.
- **No auto-rotation / angle type** here — that is HANDOFF §2 (Feature A), a
  separate module; only `measurement` is implemented. `angle` is included in the
  suggested `COLUMN_TYPES` edit for forward-compat but is otherwise untouched.
- **De-dup** guards against the overlay's own listeners and a host-dispatched
  handler both firing for one physical event; either path alone also works.
- **Coordinates & lengths are rounded** to 2 dp at storage time for tidy JSON; the
  pure `lengthPx`/`lengthUm` helpers return unrounded values (rounding happens only
  in `measurementValue` / at commit).
- The tool never writes to the DB directly (SQLite writes are the backend's job,
  HANDOFF §5 "backend owns all writes"); it only calls `doAssign`, which the host
  routes through its existing autosave → `POST /api/save` → `model.save_payload`.
```
