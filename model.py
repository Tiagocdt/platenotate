#!/usr/bin/env python3
"""annotation_app/model.py — layout discovery + the 3-level annotation data model.

Pure standard library (no matplotlib / cv2 / numpy), so it stays importable in a
tiny stdlib HTTP server. Image decoding lives in ``server.py``.

Three annotation SCOPES, one column abstraction
-----------------------------------------------
The tool is a general image annotator: every field, at every level, is a
USER-DEFINED COLUMN with a type — nothing about medaka is hard-coded in the
engine (the medaka fields are only *seed suggestions* in ``defaults.json`` and
the cross-plate registry). The same four column types apply at all three scopes:

  * categorical — an open set of string values      (line = cab | pfkfb3_her7v)
  * binary      — (conventionally) two values         (viability = alive | dead)
  * range       — an [start, end] integer window        (valid_frames over tps)
  * free        — arbitrary free text                     (a note, a temperature)

Scopes:
  * plate  — one record for the whole folder      (temp / date / start / notes …)
  * well   — the main layer, keyed by well          {well: {col: value}}
  * image  — opt-in, keyed by (well, timepoint)      {well: {tp: {col: value}}}

On-disk schema v3 (see ``fresh_payload`` / ``normalize_payload``)
-----------------------------------------------------------------
The WELL-level ``columns`` + ``annotations`` keep the exact v2 shape, so the
downstream builder ``twinnet_clean/tools/build_db.py`` ingests a v3 file
UNCHANGED (its reader treats any file carrying a ``columns`` key as v2 and
ignores the extra plate/image keys). ``schema_version`` is 3; everything else
is additive.

Server contract (the client owns the live state + undo/redo)
------------------------------------------------------------
  * ``discover_plate(dir)``      -> manifest (wells, frames, channels, autofill)
  * ``load_payload(path, plate)``-> a v3 dict (migrating v0/v1/v2 in memory)
  * ``normalize_payload(...)``   -> a clean, canonical v3 dict
  * ``save_payload(...)``        -> atomic write + merge into the global registry
  * ``registry_suggestions(...)``-> prior columns/values, per scope
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

COLUMN_TYPES = ("categorical", "binary", "range", "free")
SCOPES = ("plate", "well", "image")

# The cross-plate recommendation registry (UNION of every column/value ever
# used) — shared with the legacy screen.py so suggestions carry across tools.
DEFAULT_REGISTRY_PATH = Path.home() / "MedakaNet" / "annotation_schema.json"
# Registry key used for each scope's column bucket. "columns" (no prefix) is the
# well scope, matching the existing registry written by screen.py.
_REG_KEY = {"plate": "plate_columns", "well": "columns", "image": "image_columns"}


# ============================================================ well-position utils
_PLATE_POS_RE = re.compile(r"^([A-Za-z])(\d{1,2})$")


def _norm_pos(pos: str) -> Optional[str]:
    """Normalise a free-typed well ('a1', 'A01', 'H12') -> 'A01' form, or None.

    Accepts an 8x12 plate grid (rows A-H, cols 1-12). Non-grid identifiers (an
    arbitrary label) are left to the caller; this only canonicalises real
    plate positions so v1 ('A1') and v2 ('A01') plates share a tag schema.
    """
    if not pos:
        return None
    m = _PLATE_POS_RE.match(pos.strip())
    if not m:
        return None
    row = ord(m.group(1).upper()) - ord("A")
    col = int(m.group(2)) - 1
    if not (0 <= row < 8 and 0 <= col < 12):
        return None
    return f"{chr(ord('A') + row)}{col + 1:02d}"


def _pos_to_grid(pos: str):
    m = _PLATE_POS_RE.match(pos)
    if not m:
        raise ValueError(f"bad plate position: {pos!r}")
    return ord(m.group(1).upper()) - ord("A"), int(m.group(2)) - 1


def _grid_to_pos(row: int, col: int) -> str:
    return f"{chr(ord('A') + row)}{col + 1:02d}"


def _parse_int_set(rest: str, lo: int, hi: int, what: str) -> set:
    out = set()
    for tok in re.split(r"[,\s]+", rest.strip()):
        if not tok:
            continue
        if "-" in tok:
            a, _, b = tok.partition("-")
            try:
                a, b = int(a), int(b)
            except ValueError:
                raise ValueError(f"bad {what} range: {tok!r}")
            out.update(range(min(a, b), max(a, b) + 1))
        else:
            try:
                out.add(int(tok))
            except ValueError:
                raise ValueError(f"bad {what}: {tok!r}")
    bad = [v for v in out if not (lo <= v <= hi)]
    if bad:
        raise ValueError(f"{what}(s) out of range 1..{hi}: {sorted(bad)}")
    return out


def _parse_row_set(rest: str) -> set:
    out = set()
    for tok in re.split(r"[,\s]+", rest.strip()):
        if not tok:
            continue
        if "-" in tok:
            a, _, b = tok.partition("-")
            a, b = a.strip().upper(), b.strip().upper()
            if len(a) != 1 or len(b) != 1 or not a.isalpha() or not b.isalpha():
                raise ValueError(f"bad row range: {tok!r}")
            ia, ib = ord(a) - ord("A"), ord(b) - ord("A")
            out.update(range(min(ia, ib), max(ia, ib) + 1))
        else:
            t = tok.upper()
            if len(t) != 1 or not t.isalpha():
                raise ValueError(f"bad row: {tok!r}")
            out.add(ord(t) - ord("A"))
    bad = [chr(ord("A") + v) for v in out if not (0 <= v < 8)]
    if bad:
        raise ValueError(f"row(s) out of range A..H: {bad}")
    return out


def parse_well_range(spec: str) -> list:
    """Parse a block-of-wells spec into a list of 'A01'-form wells.

    Forms (case-insensitive): 'B07' · 'A01:D12' / 'A1-D12' (corner:corner) ·
    'cols 1-6' / 'col 3' · 'rows A-D' / 'row B' · 'A01,B03,H12'. This backs the
    optional "type a block" convenience in the UI; the primary selection path is
    rubber-band drag, which needs no parsing.
    """
    if not spec or not spec.strip():
        raise ValueError("empty range")
    s = spec.strip()
    low = s.lower()
    if low.startswith(("col", "column")):
        rest = re.sub(r"^(columns|column|cols|col)", "", low).strip()
        cols = _parse_int_set(rest, 1, 12, "column")
        return sorted(_grid_to_pos(r, c - 1) for c in cols for r in range(8))
    if low.startswith(("row",)):
        rest = re.sub(r"^(rows|row)", "", low).strip()
        rows = _parse_row_set(rest)
        return sorted(_grid_to_pos(r, c) for r in rows for c in range(12))
    if "," in s and ":" not in s and "-" not in s:
        out = []
        for tok in s.split(","):
            p = _norm_pos(tok)
            if p is None:
                raise ValueError(f"bad well in list: {tok!r}")
            out.append(p)
        return sorted(set(out))
    sep = ":" if ":" in s else ("-" if "-" in s else None)
    if sep is None:
        p = _norm_pos(s)
        if p is None:
            raise ValueError(f"unrecognised range/well: {spec!r}")
        return [p]
    a, _, b = s.partition(sep)
    pa, pb = _norm_pos(a), _norm_pos(b)
    if pa is None or pb is None:
        raise ValueError(f"bad corner in range {spec!r} (need e.g. A01:D12)")
    ra, ca = _pos_to_grid(pa)
    rb, cb = _pos_to_grid(pb)
    r0, r1 = sorted((ra, rb))
    c0, c1 = sorted((ca, cb))
    return sorted(_grid_to_pos(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1))


# ============================================================ value coercion
def _coerce_range(val):
    """Coerce into a sorted [start, end] int pair, or None."""
    if not isinstance(val, (list, tuple)) or len(val) != 2:
        return None
    try:
        a, b = int(val[0]), int(val[1])
    except (TypeError, ValueError):
        return None
    return [min(a, b), max(a, b)]


def _coerce_value(ctype, val):
    """Canonical form for a value given its column type: range -> [start,end];
    everything else -> a non-empty stripped string, or None to drop it."""
    if val is None:
        return None
    if ctype == "range" or isinstance(val, (list, tuple)):
        return _coerce_range(val)
    s = val.strip() if isinstance(val, str) else str(val).strip()
    return s or None


# ============================================================ layout discovery
_LO_RE = re.compile(r"_LO0*(\d+)_", re.IGNORECASE)          # timepoint token
_LO_LOOSE_RE = re.compile(r"[_-]LO0*(\d+)", re.IGNORECASE)   # fallback (--LO001--)
_SL_RE = re.compile(r"SL0*(\d+)", re.IGNORECASE)            # z-slice token


def parse_timepoint(name: str) -> Optional[int]:
    """Timepoint (LO number) from a crop filename, robust to naming variants."""
    m = _LO_RE.search(name) or _LO_LOOSE_RE.search(name)
    return int(m.group(1)) if m else None


def parse_z(name: str) -> Optional[int]:
    m = _SL_RE.search(name)
    return int(m.group(1)) if m else None


def _well_meta_position(well_dir: Path) -> Optional[str]:
    """Plate position from a well dir's metadata.json (normalised to 'A01')."""
    p = well_dir / "metadata.json"
    if not p.exists():
        return None
    try:
        with open(p) as f:
            pos = json.load(f).get("plate_position")
    except (OSError, json.JSONDecodeError):
        return None
    return _norm_pos(pos) or pos


def detect_layout(plate_dir: Path) -> str:
    """'v2' (bf/<pos>/SL0N/ + fl/<pos>/), 'v1_screening', 'v1_crops', or 'flat'.

    'flat' = the degenerate general case: a folder of images with no bf/fl split
    (each image treated as one 'well' with a single frame). Keeps the tool usable
    outside the medaka pipeline."""
    if (plate_dir / "bf").is_dir():
        return "v2"
    if (plate_dir / "screening").is_dir() and any((plate_dir / "screening").iterdir()):
        return "v1_screening"
    if (plate_dir / "crops").is_dir():
        return "v1_crops"
    return "flat"


_IMG_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}


def _middle_slice_dir(well_dir: Path) -> Optional[Path]:
    """The middle z-slice dir under a v2 well (prefer SL03, else the median SL*)."""
    sl03 = well_dir / "SL03"
    if sl03.is_dir():
        return sl03
    sl_dirs = sorted(well_dir.glob("SL*"))
    if sl_dirs:
        return sl_dirs[len(sl_dirs) // 2]
    return None


def _sorted_frames(files):
    """Sort image files by timepoint (falling back to name), return
    [(timepoint, filename), ...]. Files without a timepoint token are numbered
    by position so a flat folder still works."""
    out = []
    for i, fp in enumerate(sorted(files, key=lambda p: p.name)):
        tp = parse_timepoint(fp.name)
        out.append((tp if tp is not None else i + 1, fp.name))
    out.sort(key=lambda t: t[0])
    return out


def discover_plate(plate_dir: Path) -> dict:
    """Discover wells, frames and channels under a processed plate folder.

    Returns a manifest dict::

        {"plate": <folder name>, "layout": "v2"|...,
         "wells": ["A01", ...],                       # sorted
         "channels": ["BF","FL"] | ["IMG"],
         "frames": {well: {"BF": [tp,...], "FL": [tp,...]}},   # timepoints present
         "n_frames": {well: int},
         "autofill": {date, start_time, incubation_temp_c, line, guide, assay,
                      timepoint_interval_min},         # best-effort, may be partial
        }

    The frame *lists* let the client build the scrubber; images are fetched
    lazily by (well, channel, timepoint) from the server.
    """
    plate_dir = Path(plate_dir)
    layout = detect_layout(plate_dir)
    wells: dict = {}          # pos -> {"BF": {tp:fname}, "FL": {tp:fname}, "bf_dir","fl_dir"}
    channels = ["BF", "FL"]

    if layout == "v2":
        bf_root, fl_root = plate_dir / "bf", plate_dir / "fl"
        for d in sorted(p for p in bf_root.iterdir() if p.is_dir()):
            pos = _well_meta_position(d) or _norm_pos(d.name)
            if pos is None:
                continue
            mid = _middle_slice_dir(d)
            bf_frames = _sorted_frames(mid.glob("*.tif")) if mid else []
            # index EVERY z-slice dir (SL01..SL05) so the viewer can show a chosen
            # slice and follow the per-frame 'slice' keyframes during playback.
            bf_z, bf_z_dir = {}, {}
            for sl in sorted(d.glob("SL*")):
                m = re.match(r"SL0*(\d+)", sl.name)
                if not m:
                    continue
                bf_z[int(m.group(1))] = dict(_sorted_frames(sl.glob("*.tif")))
                bf_z_dir[int(m.group(1))] = str(sl)
            fl_d = fl_root / d.name
            fl_frames = _sorted_frames(fl_d.glob("*.tif")) if fl_d.is_dir() else []
            wells[pos] = {
                "BF": dict(bf_frames), "FL": dict(fl_frames),
                "bf_dir": str(mid) if mid else None,
                "fl_dir": str(fl_d) if fl_d.is_dir() else None,
                "bf_z": bf_z, "bf_z_dir": bf_z_dir,
            }
    elif layout in ("v1_screening", "v1_crops"):
        root = plate_dir / ("screening" if layout == "v1_screening" else "crops")
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            pos = _well_meta_position(d) or _norm_pos(d.name)
            if pos is None:
                continue
            # v1: BF/FL distinguished by channel token in the flat well dir.
            imgs = [p for p in d.iterdir() if p.suffix.lower() in _IMG_EXTS]
            bf = [p for p in imgs if re.search(r"CO6|_BF", p.name, re.I)]
            fl = [p for p in imgs if re.search(r"CO3|_FL", p.name, re.I)]
            wells[pos] = {
                "BF": dict(_sorted_frames(bf or imgs)),
                "FL": dict(_sorted_frames(fl)),
                "bf_dir": str(d), "fl_dir": str(d),
            }
    else:  # flat: a bare folder of images, one 'well' per image, single frame
        channels = ["IMG"]
        imgs = sorted(p for p in plate_dir.iterdir()
                      if p.suffix.lower() in _IMG_EXTS)
        for i, fp in enumerate(imgs):
            pos = fp.stem
            wells[pos] = {"IMG": {1: fp.name}, "img_dir": str(plate_dir)}

    autofill = _plate_autofill(plate_dir)
    ordered = sorted(wells.keys())
    frames = {w: {ch: sorted(wells[w].get(ch, {}).keys()) for ch in channels}
              for w in ordered}
    n_frames = {w: max((len(wells[w].get(ch, {})) for ch in channels), default=0)
                for w in ordered}
    z_slices = sorted({z for w in ordered for z in wells[w].get("bf_z", {})})
    return {
        "plate": plate_dir.name,
        "plate_dir": str(plate_dir),
        "layout": layout,
        "channels": channels,
        "wells": ordered,
        "frames": frames,
        "n_frames": n_frames,
        "z_slices": z_slices,   # available BF z-slices, e.g. [1,2,3,4,5]
        "autofill": autofill,
        "_well_index": wells,   # server-side use (path lookup); not sent to client
    }


def _plate_autofill(plate_dir: Path) -> dict:
    """Best-effort plate-level field auto-fill from plate_metadata.json + the
    frame_metadata CSV (median temperature, start time). Every value is a
    SUGGESTION the annotator confirms; missing sources -> empty fields."""
    out = {"date": "", "start_time": "", "incubation_temp_c": "",
           "line": "", "guide": "", "assay": "", "timepoint_interval_min": ""}
    meta_p = plate_dir / "plate_metadata.json"
    if meta_p.exists():
        try:
            m = json.load(open(meta_p))
            date = str(m.get("date") or "")
            if date:
                # "2026-05-21 12:37:37" -> date + start_time
                parts = date.split()
                out["date"] = parts[0]
                out["start_time"] = parts[1] if len(parts) > 1 else ""
            for k in ("line", "guide", "assay", "timepoint_interval_min"):
                if m.get(k) not in (None, ""):
                    out[k] = m.get(k)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    # median incubation temperature from the frame metadata (temp_C column)
    csvs = sorted(plate_dir.glob("*_frame_metadata.csv"))
    if csvs:
        try:
            import csv as _csv
            temps = []
            with open(csvs[0], newline="") as f:
                for i, row in enumerate(_csv.DictReader(f)):
                    t = row.get("temp_C")
                    if t:
                        try:
                            temps.append(float(t))
                        except ValueError:
                            pass
                    if i > 5000:            # a sample is plenty for the median
                        break
            if temps:
                temps.sort()
                med = temps[len(temps) // 2]
                out["incubation_temp_c"] = round(med, 1)
        except (OSError, ValueError):
            pass
    return out


# ============================================================ annotation payload (v3)
def fresh_payload(plate_name: str) -> dict:
    """An empty, canonical v3 payload."""
    return {
        "schema_version": 3,
        "plate": plate_name,
        "annotator": "",
        "created": _now(),
        "updated": _now(),
        "plate_columns": {},
        "plate_annotations": {},
        "columns": {},
        "annotations": {},
        "image_columns": {},
        "image_annotations": {},
    }


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def load_payload(path: Path, plate_name: str) -> dict:
    """Load a screening JSON as a v3 payload, migrating legacy v0/v1/v2 in
    memory. A missing/corrupt file yields a fresh skeleton (never raises)."""
    path = Path(path)
    if not path.exists():
        return fresh_payload(plate_name)
    try:
        data = json.load(open(path))
    except (OSError, json.JSONDecodeError):
        return fresh_payload(plate_name)

    out = fresh_payload(plate_name)
    out["plate"] = data.get("plate") or plate_name
    ver = data.get("schema_version")

    if ver == 3 or "image_annotations" in data or "plate_annotations" in data:
        for k in ("annotator", "created", "updated"):
            if data.get(k):
                out[k] = data[k]
        out["plate_columns"] = _clean_columns(data.get("plate_columns"))
        out["plate_annotations"] = _clean_flat_record(data.get("plate_annotations"),
                                                      out["plate_columns"])
        out["columns"] = _clean_columns(data.get("columns"))
        out["annotations"] = _clean_well_annos(data.get("annotations"), out["columns"])
        out["image_columns"] = _clean_columns(data.get("image_columns"))
        out["image_annotations"] = _clean_image_annos(data.get("image_annotations"),
                                                      out["image_columns"])
        return out

    is_v2 = (ver == 2) or isinstance(data.get("columns"), dict)
    if is_v2:
        out["columns"] = _clean_columns(data.get("columns"))
        out["annotations"] = _clean_well_annos(data.get("annotations"), out["columns"])
        return out

    # legacy v0/v1 -> a 'phenotype' (+ 'mixture') well column
    _migrate_legacy_into(out, data)
    return out


def _clean_columns(cols) -> dict:
    """Validate a {name:{type,values,default?}} column map."""
    out = {}
    if not isinstance(cols, dict):
        return out
    for name, spec in cols.items():
        if not isinstance(name, str) or not name.strip():
            continue
        spec = spec if isinstance(spec, dict) else {}
        typ = spec.get("type")
        if typ not in COLUMN_TYPES:
            typ = "range" if typ == "range" else "categorical"
        entry = {"type": typ, "values": []}
        if typ in ("categorical", "binary"):
            seen = []
            for v in (spec.get("values") or []):
                if isinstance(v, str) and v.strip() and v.strip() not in seen:
                    seen.append(v.strip())
            entry["values"] = seen
        if "default" in spec and spec["default"] not in (None, ""):
            entry["default"] = _coerce_value(typ, spec["default"])
        # 'fill: forward' marks a keyframe column (image staging / slice): stored
        # values are boundaries and the effective value forward-fills between them.
        if spec.get("fill") == "forward":
            entry["fill"] = "forward"
        out[name.strip()] = entry
    return out


def _ensure_col(cols: dict, name: str, sample_val=None):
    """Fold in a column referenced by an annotation but not declared."""
    if name not in cols:
        typ = "range" if isinstance(sample_val, (list, tuple)) else "categorical"
        cols[name] = {"type": typ, "values": []}
    return cols[name]


def _clean_flat_record(rec, cols: dict) -> dict:
    """Plate-scope: a flat {col: value} record."""
    out = {}
    if not isinstance(rec, dict):
        return out
    for col, val in rec.items():
        spec = _ensure_col(cols, col, val)
        cv = _coerce_value(spec["type"], val)
        if cv is None:
            continue
        out[col] = cv
        _register_value(spec, cv)
    return out


def _clean_well_annos(ann, cols: dict) -> dict:
    """Well-scope: {well: {col: value}} — the v2-compatible block."""
    out = {}
    if not isinstance(ann, dict):
        return out
    for well, entry in ann.items():
        if not isinstance(entry, dict):
            continue
        key = _norm_pos(well) or well
        clean = {}
        for col, val in entry.items():
            spec = _ensure_col(cols, col, val)
            cv = _coerce_value(spec["type"], val)
            if cv is None:
                continue
            clean[col] = cv
            _register_value(spec, cv)
        if clean:
            out[key] = clean
    return out


def _clean_image_annos(ann, cols: dict) -> dict:
    """Image-scope: {well: {timepoint: {col: value}}}."""
    out = {}
    if not isinstance(ann, dict):
        return out
    for well, per_tp in ann.items():
        if not isinstance(per_tp, dict):
            continue
        key = _norm_pos(well) or well
        wout = {}
        for tp, entry in per_tp.items():
            if not isinstance(entry, dict):
                continue
            try:
                tpk = str(int(tp))
            except (TypeError, ValueError):
                continue
            clean = {}
            for col, val in entry.items():
                spec = _ensure_col(cols, col, val)
                cv = _coerce_value(spec["type"], val)
                if cv is None:
                    continue
                clean[col] = cv
                _register_value(spec, cv)
            if clean:
                wout[tpk] = clean
        if wout:
            out[key] = wout
    return out


def _register_value(spec: dict, cv):
    """Fold a used value into its column's value list (categorical/binary)."""
    if spec["type"] in ("categorical", "binary") and isinstance(cv, str):
        if cv not in spec["values"]:
            spec["values"].append(cv)


def _migrate_legacy_into(out: dict, data: dict):
    raw = data.get("annotations") if isinstance(data.get("annotations"), dict) else {}
    buckets = [b for b in (data.get("buckets") or []) if b]
    has_mix = (isinstance(data.get("mixtures"), list)
               or any(isinstance(v, dict) for v in raw.values()))
    cols = {"phenotype": {"type": "categorical", "values": list(buckets)}}
    if has_mix:
        cols["mixture"] = {"type": "categorical",
                           "values": [m for m in (data.get("mixtures") or []) if m]}
    ann = {}
    for well, val in raw.items():
        key = _norm_pos(well) or well
        if isinstance(val, str):
            ph, mx = (val.strip() or None), None
        elif isinstance(val, dict):
            ph, mx = (val.get("phenotype") or None), (val.get("mixture") or None)
        else:
            ph = mx = None
        entry = {}
        if ph:
            entry["phenotype"] = ph
            _register_value(cols["phenotype"], ph)
        if has_mix and mx:
            entry["mixture"] = mx
            _register_value(cols["mixture"], mx)
        if entry:
            ann[key] = entry
    out["columns"] = cols
    out["annotations"] = ann


def normalize_payload(payload: dict, plate_name: str) -> dict:
    """Coerce an arbitrary posted payload into a clean, canonical v3 dict:
    validate columns, drop empty values, sort wells/timepoints, refresh
    ``updated``. This is what save writes and what build_db reads."""
    if not isinstance(payload, dict):
        payload = {}
    out = fresh_payload(plate_name)
    out["plate"] = payload.get("plate") or plate_name
    out["annotator"] = str(payload.get("annotator") or "")
    out["created"] = payload.get("created") or out["created"]

    out["plate_columns"] = _clean_columns(payload.get("plate_columns"))
    out["plate_annotations"] = _clean_flat_record(payload.get("plate_annotations"),
                                                  out["plate_columns"])
    out["columns"] = _clean_columns(payload.get("columns"))
    out["annotations"] = _clean_well_annos(payload.get("annotations"), out["columns"])
    out["image_columns"] = _clean_columns(payload.get("image_columns"))
    out["image_annotations"] = _clean_image_annos(payload.get("image_annotations"),
                                                  out["image_columns"])
    # sort for stable, diff-friendly files
    out["annotations"] = {w: out["annotations"][w] for w in sorted(out["annotations"])}
    out["image_annotations"] = {
        w: {tp: out["image_annotations"][w][tp]
            for tp in sorted(out["image_annotations"][w], key=int)}
        for w in sorted(out["image_annotations"])
    }
    out["updated"] = _now()
    return out


def save_payload(path: Path, payload: dict, plate_name: str,
                 registry_path: Optional[Path] = DEFAULT_REGISTRY_PATH) -> dict:
    """Normalize, atomically write to ``path``, and merge every scope's columns
    into the global registry. Returns the normalized payload actually written."""
    path = Path(path)
    clean = normalize_payload(payload, plate_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(clean, f, indent=2)
    tmp.replace(path)                       # atomic-ish: crash never truncates
    if registry_path is not None:
        _merge_registry(Path(registry_path), clean)
    return clean


# ============================================================ registry (suggestions)
def _merge_registry(registry_path: Path, clean: dict):
    """Union this plate's columns + values into the global registry, per scope.
    Types are recorded on first sight and never clobbered."""
    reg = _load_registry(registry_path)
    changed = False
    for scope in SCOPES:
        src = clean.get(_payload_cols_key(scope), {})
        bucket = reg.setdefault(_REG_KEY[scope], {})
        for name, spec in src.items():
            entry = bucket.get(name)
            if entry is None:
                bucket[name] = {"type": spec["type"], "values": list(spec.get("values", []))}
                changed = True
                continue
            vals = entry.setdefault("values", [])
            for v in spec.get("values", []):
                if v not in vals:
                    vals.append(v)
                    changed = True
    if changed:
        try:
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = registry_path.with_suffix(registry_path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(reg, f, indent=2)
            tmp.replace(registry_path)
        except OSError:
            pass


def _payload_cols_key(scope: str) -> str:
    return {"plate": "plate_columns", "well": "columns", "image": "image_columns"}[scope]


def _load_registry(registry_path: Path) -> dict:
    if registry_path is None or not Path(registry_path).exists():
        return {}
    try:
        data = json.load(open(registry_path))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def registry_suggestions(registry_path: Optional[Path] = DEFAULT_REGISTRY_PATH) -> dict:
    """Prior columns/values per scope, for the 'add-able suggestion' chips::

        {"plate": {name:{type,values}}, "well": {...}, "image": {...}}
    """
    reg = _load_registry(Path(registry_path)) if registry_path else {}
    return {scope: (reg.get(_REG_KEY[scope]) or {}) for scope in SCOPES}
