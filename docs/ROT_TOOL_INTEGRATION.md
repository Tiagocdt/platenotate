# ROT_TOOL_INTEGRATION — rotation-keyframe plugin

Drop-in **rotation** tool for the label-annotator's image tab. The user drags the
current frame to rotate it about its centre; each rotation is a **keyframe**
(`image_annotations[well]["<tp>"]["rotation"] = <degrees>`), and the angle
**interpolates** (smoothstep "fade") between keyframes so scrubbing is WYSIWYG.

It is a **plugin**: it edits **no existing file**. It registers against a
host-provided `window.AnnotatorAPI` and feature-detects every method, so a
missing/absent method never throws.

## Files added (only these)
- `static/rot_tool.js` — the tool (vanilla JS, no build, no deps).
- `static/tests/rot_tool.test.mjs` — Node unit test of the pure core.
- `ROT_TOOL_INTEGRATION.md` — this file.

## Run the tests
```
cd imaging/tools/label_annotator/static
node tests/rot_tool.test.mjs          # 12/12 pass, exit 0
# or:  node --test tests/rot_tool.test.mjs
```

---

## 1. The one line to add to `index.html`
After the existing app script (order does **not** matter — see §4):
```html
<script src="/static/app.js"></script>
<script src="/static/rot_tool.js"></script>   <!-- add this line -->
```

## 2. The column definition it needs
Column **name `rotation`**, **level `image`**, **type `angle`**, **fill `interpolate`**.

**Now (JSON path):** add one seed row to `defaults.json` under `image.columns` so
the column is offered/created (mirrors the existing `slice` / `iwamatsu_stage`
rows):
```json
"rotation": {
  "type": "angle",
  "values": [],
  "fill": "interpolate",
  "hint": "manual body-axis rotation, degrees (clockwise +). KEYFRAME column: drag the image to rotate; the angle fades (smoothstep) between keyframes, held flat at the ends."
}
```

**Future (DB path, HANDOFF_v2 §5):** the same thing as a `column_def` row:
```sql
INSERT INTO column_def (name, level, type, values_json, default_val, created)
VALUES ('rotation', 'image', 'angle', '[]', NULL, datetime('now'));
```

## 3. Exact `AnnotatorAPI` methods used
`window.AnnotatorAPI` (`API`) is consumed as follows. **Required** = the tool is
useful only if present; **Optional** = a graceful fallback exists.

| Method | Use | Req/Opt |
|---|---|---|
| `API.registerTool("angle", handlers)` | registers the image-interaction handlers (below) | Required (no image drag without it) |
| `API.onColumnPanel("angle", renderFn)` | renders the panel controls (dial, field, ±buttons, keyframe list, clear) | Required for the panel |
| `API.imgEl` | the `<img id="bigImg">`; the tool sets `imgEl.style.transform = 'rotate(Ndeg)'` | Required for the visual |
| `API.curWell()` / `API.curTp()` | current well id / timepoint (int) | Required (falls back to `API.state.primary` for the well) |
| `API.setImageKeyframe("rotation", deg)` | commits a keyframe at (current well, tp) on drag mouse-up, field commit, ±buttons | Required (falls back to `API.mutate`) |
| `API.imgInterpolate(well, "rotation", tp)` | smoothstep value used every render (`onRender`) and to seed a drag | Optional (falls back to the tool's own pure `interpolate`) |
| `API.imgKeyframes(well, "rotation")` | sorted `[[tp,value],…]` for the keyframe list + interpolation | Optional (falls back to reading `API.state.payload.image_annotations`) |
| `API.mutate(fn)` | delete-a-keyframe and clear-well writes; also the `setImageKeyframe` fallback | Required for delete/clear |
| `API.state` (`{payload, primary, plateDir}`) | payload read/edit for the fallbacks; `primary` as the well fallback | Required |
| `API.stageEl` | not written to; present in the contract, reserved (rotation uses `imgEl`) | — |

`handlers` passed to `registerTool("angle", …)`:
`onActivate(colName)`, `onDeactivate()`, `onImageMouseDown(ev, pt)`,
`onImageMouseMove(ev, pt)`, `onImageMouseUp(ev, pt)`, `onRender(well, tp)`.
The tool uses `ev.clientX/clientY` for the rotation angle (rotation about the
element-box centre is scale- and rotation-invariant, so `pt` — source-pixel
coords — is accepted but not needed here).

**Host contract the tool relies on:**
1. **Call `onRender(well, tp)` on every image render** (scrub, `[`/`]`, keyframe
   write, well change). This is what repaints the interpolated angle — required
   for WYSIWYG. Requirement met literally: it sets
   `imgEl.style.transform = 'rotate(' + API.imgInterpolate(well,'rotation',tp) + 'deg)'`.
2. **`imgKeyframes` returns the sorted pair form** `[[tp,value],…]` (the contract's
   shape) — note this differs from app.js's internal `imgKeyframes`, which returns
   `{tp:value}`; the API shim must return pairs.
3. Route image mouse events to the handlers, and call `onActivate/onDeactivate`,
   only while `angle` is the **active image column**.

---

## 4. Wiring is order-independent
`rot_tool.js` installs the moment it can: it calls `install(window.AnnotatorAPI)`
immediately, and if the API is not there yet it (a) polls briefly (~5 s) and
(b) listens once for a `annotator:ready` event. The host may also install
explicitly: `window.RotTool.install(API)`. So the `<script>` can sit before or
after `app.js`, and the API can be defined lazily.

## 5. Sign convention & range
- **Clockwise-positive.** Screen y is down, so `Math.atan2(dy,dx)` grows clockwise
  and CSS `rotate(+deg)` turns clockwise — the on-screen turn, the stored number,
  and what a renderer must apply share one sign. A renderer (`cv2.warpAffine`,
  ffmpeg) must therefore rotate **clockwise for positive degrees**.
- Stored keyframe values are normalised to **(-180, 180]**. Mid-drag the preview
  is un-normalised (smooth), and the committed value is normalised — visually
  identical (differs only by a multiple of 360°), so there is no snap.

## 6. Interpolation = focus_cut smoothstep (must stay in lockstep with the renderer)
The tool's `interpolate(keyframes, tp)` is a faithful mirror of
`hyperstack_video/focus_cut.py::build_focus_track(ease="smoothstep")`:
`u=(tp-t0)/(t1-t0); w=u*u*(3-2u); value=v0+(v1-v0)*w`, ends held flat, single
keyframe = constant, empty = 0. For the rendered film to match the in-tool
preview, add a **`build_rotation_track`** (or parametrise `build_focus_track` by
column name) that reads the `rotation` keyframes and applies the same formula,
then `warpAffine` about the frame centre (HANDOFF_v2 §2 "Consumers to update").

---

## 7. Assumptions & edge cases
- **Persistence without host-side model changes (graceful degradation).**
  `model.py` today has `COLUMN_TYPES=("categorical","binary","range","free")` and
  `_clean_columns` keeps only `fill=="forward"`, and `_coerce_value` stringifies a
  categorical value. So **without host edits**, on save the column's declared
  `type` becomes `categorical`, `fill:"interpolate"` is dropped, and the numeric
  keyframe value is stored as a **string** (e.g. `"12.5"`). The plugin tolerates
  this — `interpolate` and all reads `Number()`-coerce, and it works whether the
  value is a number or a string. **For clean persistence** (numeric value, correct
  `type`/`fill` round-trip), extend the host: add `"angle"` to `COLUMN_TYPES`,
  preserve `fill=="interpolate"` in `_clean_columns`, and let `_coerce_value` keep
  `angle` numeric. None of that is required for the tool to function.
- **`build_db` sees rotation for free** as an `image_annotation` row (quoted
  `"column"='rotation'`) after a rebuild — no schema change (HANDOFF_v2 App. A §5).
- **Angular seam.** Interpolation is plain-numeric (matching the renderer), so a
  keyframe pair straddling ±180 (e.g. 170°→-170°) sweeps the long way. This is
  inherent to mirroring `build_focus_track` exactly; keep successive keyframes
  within <180° of each other, or add a shortest-path pass to *both* tool and
  renderer if desired.
- **Rotation clips at the stage edges.** `#stage` is `overflow:hidden`, and a
  contained image rotated off-axis overflows its box, so corners are clipped
  (expected; the handoff accepts CSS `rotate` on `#bigImg`). A fit-to-stage
  down-scale could be added but was left out for fidelity.
- **Preview only while active.** `onDeactivate` resets `imgEl.style.transform`, so
  rotation is shown only when the `angle` column is the active image column
  (per spec). Renderers still bake it regardless.
- **Jump-to-tp** in the keyframe list uses `API.gotoTp/setTp/jumpToTp/setFrameByTp`
  if present, else the app's `window._dbg.setFrame` (mapping tp→frame index via
  the manifest), else it is a no-op. **Delete** and **clear-well** go through
  `API.mutate` and always work.
- **Event robustness.** The drag attaches its own document `mousemove`/`mouseup`
  (capture) on mousedown, so it survives the pointer leaving the image; the
  registered `onImageMouseMove/Up` calls are idempotent duplicates (safe whether
  or not the host also forwards them). Re-entrant `mousedown` is guarded.
- **Double-load guarded** by `window.__ROT_TOOL_LOADED__`; CSS injected once
  (`<style id="rot-tool-css">`), reusing the app's `--accent/--panel/--line` vars.

## 8. Acceptance (maps to HANDOFF_v2 §8)
- Drag-rotate a frame → keyframe saved. ✓ (verified headless: a rightward→downward
  pointer sweep writes `+90°`)
- Scrubbing between keyframes shows a smooth interpolated rotation. ✓ (`onRender`
  → `imgInterpolate`, smoothstep)
- A rendered clip matches — **pending the renderer change in §6** (`build_rotation_track`).
