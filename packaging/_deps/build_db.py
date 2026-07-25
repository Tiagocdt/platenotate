#!/usr/bin/env python
"""build_db.py — the medaka image DATABASE BUILDER (plan 25).

Every image becomes one row connected to everything we know about it — imaging
metadata (per image), injection mix / genes / guides (per well, via mix),
line / Cre-state / cadence (per plate), and screening annotations (per well) —
in a single queryable SQLite store, ``/Users/tiago/MedakaNet/medaka.db``.

Data model — three levels, one key::

    plate  ──1:N──▶  well  ──1:N──▶  image (= well × timepoint × channel × z)

Ingest is PER-PLATE and IDEMPOTENT: re-ingesting a plate DELETEs then re-INSERTs
all of its rows inside a single transaction, so re-running never duplicates.

Sources (all optional; whatever exists is ingested):
  1. experiment YAML   experiments/<plate>.yaml       -> plate + mix + guide rows
  2. frame_metadata CSV data/<dir>/<name>_frame_metadata.csv -> image + well rows
  3. screening JSON     data/<dir>/screening_<plate>.json    -> well_annotation rows

The annotation JSON contract (see ``parse_screening``) is read at v2 and legacy
v1/v0 are migrated on the fly, so the sibling annotation tool's v2 output ingests
without any change here.

Two ways to get the wide "future of the CSV" flat file:
  * SQL VIEW ``image_full`` = image ⋈ well ⋈ mix ⋈ plate — FIXED columns only
    (pure SQL cannot pivot the EAV ``well_annotation`` table into dynamic columns).
  * Python ``export_full`` / the ``to-csv`` command — the FULL pivot: image_full
    PLUS one extra column per distinct ``well_annotation.column`` name.

CLI::

    build_db.py ingest --plate <folder> [--csv P] [--experiment P] [--screening P]
    build_db.py ingest-all
    build_db.py to-csv --plate P --out FILE
    build_db.py query "SQL"
    build_db.py stats

Uses only the standard library (sqlite3 / csv / json) plus PyYAML for the small
experiment files (auto-``pip install``ed if missing).
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ------------------------------------------------------------------ locations
# build_db.py lives at imaging/tools/metadata_db/  ->  data is imaging/data
DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
DEFAULT_DB = DATA_ROOT / "medaka.db"


def _yaml():
    """Import PyYAML, installing it into the running interpreter if absent.

    The experiment files are simple, so PyYAML is sufficient; the twinnet env
    already ships it, so the install path is a no-op fallback for other envs.
    """
    try:
        import yaml
        return yaml
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml"])
        import yaml
        return yaml


# ------------------------------------------------------------------ schema
SCHEMA = r"""
CREATE TABLE IF NOT EXISTS plate(
    plate_id    TEXT PRIMARY KEY,   -- = folder / plate name
    folder      TEXT,
    date        TEXT,
    line        TEXT,
    cre_state   TEXT,
    cadence_min INTEGER,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS mix(
    mix_id    INTEGER PRIMARY KEY,
    plate_id  TEXT,
    name      TEXT,
    role      TEXT,
    cas9_conc TEXT
);

CREATE TABLE IF NOT EXISTS guide(
    guide_id    INTEGER PRIMARY KEY,
    mix_id      INTEGER,
    gene        TEXT,
    name        TEXT,
    protospacer TEXT,
    conc        TEXT
);

CREATE TABLE IF NOT EXISTS well(
    plate_id TEXT,
    well     TEXT,
    mix_name TEXT,
    PRIMARY KEY(plate_id, well)
);

CREATE TABLE IF NOT EXISTS image(
    image_id     INTEGER PRIMARY KEY,
    plate_id     TEXT,
    well         TEXT,
    timepoint    INTEGER,
    channel      TEXT,
    z            INTEGER,
    px_nm        REAL,
    temp_c       REAL,
    stage_x      TEXT,
    stage_y      TEXT,
    stage_z      TEXT,
    timestamp    TEXT,
    raw_filename TEXT,
    crop_path    TEXT      -- path to the segmented crop, RELATIVE to the plate's
                           -- processed dir, e.g. 'bf/A01/SL01/<file>.tif'
);

-- WHO made an annotation: a human ('tiago') or a tool ('auto:orient','focus_cut').
CREATE TABLE IF NOT EXISTS annotator(
    annotator_id INTEGER PRIMARY KEY,
    name         TEXT UNIQUE,
    created      TEXT
);

-- GLOBAL cross-plate column registry — THE shared column set (one row per
-- name+level). A column defined here is visible on every plate; this is what
-- makes "create a column once, it appears on all plates" true.
CREATE TABLE IF NOT EXISTS column_def(
    name        TEXT,
    level       TEXT,          -- 'plate' | 'well' | 'image'
    type        TEXT,          -- categorical|binary|range|free|angle|measurement
    values_json TEXT,          -- JSON array of allowed values; '[]' when free-form
    default_val TEXT,
    "fill"      TEXT,          -- 'forward' | 'interpolate' | NULL (keyframe columns)
    created     TEXT,
    created_by  INTEGER,       -- annotator_id who first created the column
    PRIMARY KEY(name, level)
);

-- EAV annotations, now PROVENANCED (annotator_id) + timestamped (updated) so
-- multiple annotators/tools coexist and re-runs UPSERT in place. Range values
-- are stored as JSON text '[start,end]'.
CREATE TABLE IF NOT EXISTS well_annotation(
    plate_id     TEXT,
    well         TEXT,
    "column"     TEXT,
    value        TEXT,
    annotator_id INTEGER DEFAULT 0,
    updated      TEXT,
    PRIMARY KEY(plate_id, well, "column", annotator_id)
);

CREATE TABLE IF NOT EXISTS plate_annotation(
    plate_id     TEXT,
    "column"     TEXT,
    value        TEXT,
    annotator_id INTEGER DEFAULT 0,
    updated      TEXT,
    PRIMARY KEY(plate_id, "column", annotator_id)
);

CREATE TABLE IF NOT EXISTS image_annotation(
    plate_id     TEXT,
    well         TEXT,
    timepoint    INTEGER,
    "column"     TEXT,
    value        TEXT,
    annotator_id INTEGER DEFAULT 0,
    updated      TEXT,
    PRIMARY KEY(plate_id, well, timepoint, "column", annotator_id)
);

-- Measurements (draw-a-line): first-class + queryable; length_um via px size.
CREATE TABLE IF NOT EXISTS measurement(
    plate_id     TEXT,
    well         TEXT,
    timepoint    INTEGER,
    name         TEXT,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    length_px    REAL,
    length_um    REAL,
    annotator_id INTEGER DEFAULT 0,
    updated      TEXT,
    PRIMARY KEY(plate_id, well, timepoint, name, annotator_id)
);

CREATE INDEX IF NOT EXISTS idx_image_plate_well   ON image(plate_id, well);
CREATE INDEX IF NOT EXISTS idx_wellann_col_value  ON well_annotation("column", value);
CREATE INDEX IF NOT EXISTS idx_imgann_plate_well  ON image_annotation(plate_id, well, timepoint);
CREATE INDEX IF NOT EXISTS idx_measure_plate_well ON measurement(plate_id, well, timepoint);

-- Fixed-column join (image ⋈ well ⋈ mix ⋈ plate). Dynamic annotation columns
-- live in well_annotation and are pivoted only by the Python export_full().
CREATE VIEW IF NOT EXISTS image_full AS
SELECT
    i.image_id, i.plate_id, i.well, i.timepoint, i.channel, i.z,
    i.px_nm, i.temp_c, i.stage_x, i.stage_y, i.stage_z, i.timestamp, i.raw_filename,
    i.crop_path,
    w.mix_name              AS mix_name,
    m.role                  AS mix_role,
    m.cas9_conc             AS mix_cas9_conc,
    p.date                  AS plate_date,
    p.line                  AS plate_line,
    p.cre_state             AS cre_state,
    p.cadence_min           AS cadence_min
FROM image i
LEFT JOIN well  w ON w.plate_id = i.plate_id AND w.well    = i.well
LEFT JOIN mix   m ON m.plate_id = i.plate_id AND m.name    = w.mix_name
LEFT JOIN plate p ON p.plate_id = i.plate_id;
"""


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")      # concurrent readers while a tool writes
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------- write API (DB-first)
# These make medaka.db a WRITE target for every tool: idempotent UPSERTs keyed by
# (…, annotator_id) so re-running a tool updates in place and different annotators
# (humans and tools like 'auto:orient') coexist. Import build_db and call these.
def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_or_create_annotator(conn, name):
    """annotator_id for a human ('tiago') or a tool ('auto:orient'); made on first use.
    Case-insensitive, so 'Tiago' and 'tiago' collapse to one annotator (first spelling wins)."""
    name = str(name or "unknown").strip()
    row = conn.execute("SELECT annotator_id FROM annotator WHERE lower(name)=lower(?)",
                       (name,)).fetchone()
    if row:
        return row[0]
    conn.execute("INSERT INTO annotator(name, created) VALUES (?,?)", (name, _now()))
    return conn.execute("SELECT annotator_id FROM annotator WHERE name=?", (name,)).fetchone()[0]


def upsert_column_def(conn, name, level, ctype, values=None, default=None, fill=None, created_by=None):
    """Register/refresh a column in the GLOBAL registry (visible on every plate).
    created_by (annotator_id) records who first created it and is preserved on update."""
    conn.execute(
        'INSERT INTO column_def(name, level, type, values_json, default_val, "fill", created, created_by)'
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(name, level) DO UPDATE SET type=excluded.type,"
        '   values_json=excluded.values_json, default_val=excluded.default_val, "fill"=excluded."fill"',
        (name, level, ctype, json.dumps(values or []), default, fill, _now(), created_by))


def upsert_well_annotation(conn, plate_id, well, column, value, annotator_id):
    conn.execute(
        'INSERT INTO well_annotation(plate_id, well, "column", value, annotator_id, updated)'
        " VALUES (?,?,?,?,?,?)"
        ' ON CONFLICT(plate_id, well, "column", annotator_id)'
        "   DO UPDATE SET value=excluded.value, updated=excluded.updated",
        (plate_id, well, column, value, annotator_id, _now()))


def upsert_plate_annotation(conn, plate_id, column, value, annotator_id):
    conn.execute(
        'INSERT INTO plate_annotation(plate_id, "column", value, annotator_id, updated)'
        " VALUES (?,?,?,?,?)"
        ' ON CONFLICT(plate_id, "column", annotator_id)'
        "   DO UPDATE SET value=excluded.value, updated=excluded.updated",
        (plate_id, column, value, annotator_id, _now()))


def upsert_image_annotation(conn, plate_id, well, timepoint, column, value, annotator_id):
    conn.execute(
        'INSERT INTO image_annotation(plate_id, well, timepoint, "column", value, annotator_id, updated)'
        " VALUES (?,?,?,?,?,?,?)"
        ' ON CONFLICT(plate_id, well, timepoint, "column", annotator_id)'
        "   DO UPDATE SET value=excluded.value, updated=excluded.updated",
        (plate_id, well, timepoint, column, value, annotator_id, _now()))


def upsert_measurement(conn, plate_id, well, timepoint, name, coords, length_px, length_um, annotator_id):
    x0, y0, x1, y1 = coords
    conn.execute(
        "INSERT INTO measurement(plate_id, well, timepoint, name, x0,y0,x1,y1,"
        " length_px, length_um, annotator_id, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(plate_id, well, timepoint, name, annotator_id) DO UPDATE SET"
        "   x0=excluded.x0, y0=excluded.y0, x1=excluded.x1, y1=excluded.y1,"
        "   length_px=excluded.length_px, length_um=excluded.length_um, updated=excluded.updated",
        (plate_id, well, timepoint, name, x0, y0, x1, y1, length_px, length_um, annotator_id, _now()))


# ------------------------------------------------------------------ converters
def _int(v):
    v = ("" if v is None else str(v)).strip()
    if v == "":
        return None
    try:
        return int(v)
    except ValueError:
        try:
            return int(float(v))
        except ValueError:
            return None


def _float(v):
    v = ("" if v is None else str(v)).strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _text(v):
    if v is None:
        return None
    v = str(v)
    return v if v != "" else None


# ------------------------------------------------------------------ parse: experiment YAML
def parse_experiment(path):
    """Parse an experiment YAML into plate/mix/guide/well_mix structures.

    Returns dict:
      plate      -> {date, line, cre_state, cadence_min, notes}
      mixes      -> [ {name, role, cas9_conc, guides:[{gene,name,protospacer,conc}]}, ... ]
      well_mix   -> {well: mix_name}
      plate_id   -> the file's declared plate name (for id resolution)
    """
    yaml = _yaml()
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    plate = {
        "date":        _text(data.get("date")),
        "line":        _text(data.get("line")),
        "cre_state":   _text(data.get("cre_state")),
        "cadence_min": _int(data.get("cadence_min")),
        "notes":       _text(data.get("notes")),
    }

    mixes = []
    raw_mixes = data.get("injection_mixes") or {}
    for name, spec in raw_mixes.items():
        spec = spec or {}
        guides = []
        for g in spec.get("guides", []) or []:
            g = g or {}
            guides.append({
                "gene":        _text(g.get("gene")),
                "name":        _text(g.get("name")),
                "protospacer": _text(g.get("protospacer")),
                "conc":        _text(g.get("conc_ng_per_ul")),
            })
        mixes.append({
            "name":      _text(name),
            "role":      _text(spec.get("role")),
            "cas9_conc": _text(spec.get("cas9_conc_ng_per_ul")),
            "guides":    guides,
        })

    well_mix = {}
    for well, mixname in (data.get("well_mix") or {}).items():
        if mixname:
            well_mix[str(well)] = str(mixname)

    return {
        "plate":    plate,
        "mixes":    mixes,
        "well_mix": well_mix,
        "plate_id": _text(data.get("plate")),
    }


# ------------------------------------------------------------------ parse: screening JSON
def parse_screening(path):
    """Read a screening JSON at the v2 contract, migrating legacy v1/v0.

    v2 (what the annotation tool writes):
        {"schema_version":2, "plate":..,
         "columns":{name:{"type":"categorical|binary|range|free","values":[..]}},
         "annotations":{well:{col:value, ..}}}   -- range value = [start,end] ints
    v1 (migrate): {"plate","buckets","mixtures","annotations":{well:{phenotype,mixture}}}
                  -> columns 'phenotype' and 'mixture'
    v0 (migrate): {"plate","buckets","annotations":{well:"label"}}
                  -> 'phenotype' column, well -> {phenotype: label}

    Returns (version, ann_rows, mixture_map):
      version     -> 2 | 1 | 0
      ann_rows    -> [(well, column, value_text), ...]  (range stored as '[start,end]')
      mixture_map -> {well: mix_name} taken from a 'mixture' column, if present
    """
    with open(path) as f:
        data = json.load(f)

    ann = data.get("annotations", {}) or {}
    rows = []
    mixture_map = {}

    is_v2 = (data.get("schema_version") == 2) or ("columns" in data)

    if is_v2:
        version = 2
        columns = data.get("columns", {}) or {}
        for well, entry in ann.items():
            if not isinstance(entry, dict):
                continue
            for col, val in entry.items():
                ctype = (columns.get(col) or {}).get("type")
                if ctype == "range" or isinstance(val, (list, tuple)):
                    if val is None:
                        continue
                    value_text = json.dumps(list(val), separators=(",", ":"))  # '[start,end]'
                else:
                    if val is None or val == "":
                        continue
                    value_text = str(val)
                rows.append((str(well), str(col), value_text))
                if col == "mixture":
                    mixture_map[str(well)] = value_text
        return version, rows, mixture_map

    # legacy: distinguish v1 (dict entries) from v0 (bare string labels)
    is_v1 = any(isinstance(v, dict) for v in ann.values())
    if is_v1:
        version = 1
        for well, entry in ann.items():
            if not isinstance(entry, dict):
                continue
            for col, val in entry.items():
                if val is None or val == "":
                    continue
                value_text = str(val)
                rows.append((str(well), str(col), value_text))
                if col == "mixture":
                    mixture_map[str(well)] = value_text
    else:
        version = 0
        for well, label in ann.items():
            if label is None or label == "":
                continue
            rows.append((str(well), "phenotype", str(label)))

    return version, rows, mixture_map


def parse_screening_levels(path):
    """Read the v3 PLATE- and IMAGE-level annotations from a screening JSON.

    These are additive to the well-level EAV that ``parse_screening`` reads and
    are absent from v0/v1/v2 files (which yield empty lists). Returns::

        (plate_rows, image_rows)
        plate_rows -> [("column", value_text), ...]
        image_rows -> [(well, timepoint, "column", value_text), ...]

    Range values are serialised as JSON '[start,end]', matching well_annotation.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return [], []
    if not isinstance(data, dict):
        return [], []

    def _vt(val, ctype=None):
        if ctype == "range" or isinstance(val, (list, tuple)):
            return None if val is None else json.dumps(list(val), separators=(",", ":"))
        if val is None or val == "":
            return None
        return str(val)

    pcols = data.get("plate_columns") or {}
    plate_rows = []
    for col, val in (data.get("plate_annotations") or {}).items():
        t = _vt(val, (pcols.get(col) or {}).get("type"))
        if t is not None:
            plate_rows.append((str(col), t))

    icols = data.get("image_columns") or {}
    image_rows = []
    for well, per_tp in (data.get("image_annotations") or {}).items():
        if not isinstance(per_tp, dict):
            continue
        for tp, entry in per_tp.items():
            if not isinstance(entry, dict):
                continue
            try:
                tpi = int(tp)
            except (TypeError, ValueError):
                continue
            for col, val in entry.items():
                t = _vt(val, (icols.get(col) or {}).get("type"))
                if t is not None:
                    image_rows.append((str(well), tpi, str(col), t))
    return plate_rows, image_rows


# ------------------------------------------------------------------ auto-find
def find_csv(plate_id, root=DATA_ROOT):
    d = root / "AQ-EMBL" / plate_id
    if d.is_dir():
        cands = sorted((d / "metadata").glob("*_frame_metadata.csv")) or sorted(d.glob("*_frame_metadata.csv"))
        if cands:
            return cands[0]
    return None


def find_screening(plate_id, root=DATA_ROOT):
    p = root / "AQ-EMBL" / plate_id / "metadata" / f"screening_{plate_id}.json"
    if not p.is_file():
        p = root / "AQ-EMBL" / plate_id / f"screening_{plate_id}.json"
    return p if p.exists() else None


def find_manual_size(plate_id, root=DATA_ROOT):
    p = root / "AQ-EMBL" / plate_id / "metadata" / f"{plate_id}_manual_size.json"
    return p if p.exists() else None


def parse_manual_size(path):
    """Manual egg-diameter LINES from a <plate>_manual_size.json →
    [(well, (x0,y0,x1,y1) in native px, length_px, length_um, slice_z), ...].
    The stored `line` is normalized 0-1; convert to native px using the native image
    size implied by line_native_px ÷ the normalized line length."""
    import math
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for well, m in (d.get("wells") or {}).items():
        line = m.get("line")
        dia_um = m.get("manual_diameter_um")
        if not (isinstance(line, (list, tuple)) and len(line) == 4 and dia_um):
            continue
        x0, y0, x1, y1 = (float(v) for v in line)
        nlen = math.hypot(x1 - x0, y1 - y0) or 1.0
        lpx = float(m["line_native_px"]) if m.get("line_native_px") else nlen
        native = lpx / nlen                              # implied native image size (px)
        coords = (x0 * native, y0 * native, x1 * native, y1 * native)
        out.append((str(well), coords, lpx, float(dia_um), m.get("slice_z")))
    return out


def find_experiment(plate_id, root=DATA_ROOT):
    p = root / "experiments" / f"{plate_id}.yaml"
    return p if p.exists() else None


# ------------------------------------------------------------------ delete (idempotency)
def _delete_plate(conn, plate_id, ann_annotator_id=None):
    """Clear a plate's rows for a clean rebuild. Metadata (image/well/mix/guide/
    plate) is always cleared. Annotation rows are cleared only for the given
    annotator when ``ann_annotator_id`` is set — so re-importing one annotator's
    JSON never wipes another annotator's or a tool's DB-native annotations
    (``None`` = clear every annotator, the legacy full rebuild)."""
    conn.execute("DELETE FROM image WHERE plate_id = ?", (plate_id,))
    conn.execute("DELETE FROM well  WHERE plate_id = ?", (plate_id,))
    ann_tables = ("well_annotation", "plate_annotation", "image_annotation", "measurement")
    if ann_annotator_id is None:
        for t in ann_tables:
            conn.execute(f"DELETE FROM {t} WHERE plate_id = ?", (plate_id,))
    else:
        for t in ann_tables:
            conn.execute(f"DELETE FROM {t} WHERE plate_id = ? AND annotator_id = ?",
                         (plate_id, ann_annotator_id))
    conn.execute(
        "DELETE FROM guide WHERE mix_id IN (SELECT mix_id FROM mix WHERE plate_id = ?)",
        (plate_id,),
    )
    conn.execute("DELETE FROM mix   WHERE plate_id = ?", (plate_id,))
    conn.execute("DELETE FROM plate WHERE plate_id = ?", (plate_id,))


# ------------------------------------------------------------------ per-plate ingest
def _scan_crops(data_dir):
    """Index a plate's segmented crops so each frame can point at its crop file.

    Scans <data_dir>/bf and /fl and returns two lookups of paths RELATIVE to
    data_dir (so the same value is valid whether the crops sit on the server
    under AQ-EMBL/PROCESSED/<plate>/ or locally under data/<plate>/):
        bf_crops[(well, timepoint, z)] -> 'bf/<well>/SL0z/<file>.tif'  (per z-slice)
        fl_crops[(well, timepoint)]    -> 'fl/<well>/<file>.tif'       (max-proj, shared over z)
    Returns ({}, {}) if the plate's crops aren't present locally.
    """
    import re
    bf_crops, fl_crops = {}, {}
    if data_dir is None:
        return bf_crops, fl_crops
    data_dir = Path(data_dir)
    bf_root = data_dir / "bf"
    if bf_root.is_dir():
        for fp in bf_root.glob("*/*/*.tif"):                # bf/<well>/SL0z/<file>
            m = re.search(r"_LO(\d+)_BF_SL0*(\d+)", fp.name)
            if not m:
                continue
            bf_crops[(fp.parent.parent.name, int(m.group(1)), int(m.group(2)))] = \
                str(fp.relative_to(data_dir))
    fl_root = data_dir / "fl"
    if fl_root.is_dir():
        for fp in fl_root.glob("*/*.tif"):                  # fl/<well>/<file>
            m = re.search(r"_LO(\d+)_FL", fp.name)
            if not m:
                continue
            fl_crops[(fp.parent.name, int(m.group(1)))] = str(fp.relative_to(data_dir))
    return bf_crops, fl_crops


def _image_rows(csv_path, plate_id, wells_out, bf_crops=None, fl_crops=None,
                bf_channel="CO6", fl_channel="CO3"):
    """Stream image tuples from a frame_metadata CSV, collecting distinct wells
    into ``wells_out`` as a side effect. If crop lookups are given, attach each
    frame's segmented crop path (BF crops are per z-slice; FL crops are the
    max-projection shared across z)."""
    bf_crops = bf_crops or {}
    fl_crops = fl_crops or {}
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            well = row.get("well")
            if not well:
                continue
            wells_out.add(well)
            tp = _int(row.get("timepoint_LO"))
            ch = _text(row.get("channel_CO"))
            z = _int(row.get("z_slice_SL"))
            if ch == fl_channel:
                crop = fl_crops.get((well, tp))
            elif ch == bf_channel:
                crop = bf_crops.get((well, tp, z))
            else:                                            # unknown channel: best effort
                crop = bf_crops.get((well, tp, z)) or fl_crops.get((well, tp))
            yield (
                plate_id, well, tp, ch, z,
                _float(row.get("px_size_nm")),
                _float(row.get("temp_C")),
                _text(row.get("stage_X")),
                _text(row.get("stage_Y")),
                _text(row.get("stage_Z")),
                _text(row.get("timestamp_T")),
                _text(row.get("raw_filename")),
                crop,
            )


def ingest_plate(conn, plate_id, csv_path=None, experiment=None, screening=None,
                 manual_size=None, root=DATA_ROOT, auto=True, verbose=True):
    """Ingest one plate from whatever of {csv, experiment, screening} is given.

    Omitted sources are auto-discovered (when ``auto``). The whole plate is
    rebuilt atomically: every existing row for ``plate_id`` is deleted and
    re-inserted inside one transaction, so re-running is idempotent.
    """
    plate_id = str(plate_id)

    # resolve the data dir + auto-find omitted sources
    data_dir = Path(csv_path).parent if csv_path else None
    if auto:
        if csv_path is None:
            csv_path = find_csv(plate_id, root)
            if csv_path is not None:
                data_dir = Path(csv_path).parent
        if experiment is None:
            experiment = find_experiment(plate_id, root)
        if screening is None:
            screening = find_screening(plate_id, root)
        if manual_size is None:
            manual_size = find_manual_size(plate_id, root)
    if data_dir is None:
        d = root / "AQ-EMBL" / plate_id
        data_dir = d if d.is_dir() else None

    exp = parse_experiment(experiment) if experiment else None
    scr = parse_screening(screening) if screening else None  # (version, rows, mixture_map)
    scr_raw = {}
    if screening:
        try:
            scr_raw = json.load(open(screening))
        except Exception:
            scr_raw = {}
    ann_name = scr_raw.get("annotator") or "tiago"

    counts = {"image": 0, "well": 0, "mix": 0, "guide": 0, "annotation": 0,
              "plate_ann": 0, "image_ann": 0, "measurement": 0}

    with conn:  # single transaction: BEGIN ... COMMIT (rollback on error)
        aid = get_or_create_annotator(conn, ann_name)
        _delete_plate(conn, plate_id, ann_annotator_id=aid)

        # --- column_def: register this plate's columns into the GLOBAL registry ---
        for level, key in (("well", "columns"), ("image", "image_columns"),
                           ("plate", "plate_columns")):
            for cname, cdef in (scr_raw.get(key) or {}).items():
                cdef = cdef if isinstance(cdef, dict) else {}
                upsert_column_def(conn, cname, level, cdef.get("type", "categorical"),
                                  cdef.get("values"), cdef.get("default"), cdef.get("fill"),
                                  created_by=aid)

        # --- plate row (merge experiment fields over the folder-only default) ---
        folder = data_dir.name if data_dir is not None else plate_id
        p = exp["plate"] if exp else {}
        conn.execute(
            "INSERT INTO plate(plate_id, folder, date, line, cre_state, cadence_min, notes)"
            " VALUES (?,?,?,?,?,?,?)",
            (plate_id, folder, p.get("date"), p.get("line"), p.get("cre_state"),
             p.get("cadence_min"), p.get("notes")),
        )

        # --- mixes + guides (experiment) ---
        if exp:
            for m in exp["mixes"]:
                cur = conn.execute(
                    "INSERT INTO mix(plate_id, name, role, cas9_conc) VALUES (?,?,?,?)",
                    (plate_id, m["name"], m["role"], m["cas9_conc"]),
                )
                mix_id = cur.lastrowid
                counts["mix"] += 1
                for g in m["guides"]:
                    conn.execute(
                        "INSERT INTO guide(mix_id, gene, name, protospacer, conc)"
                        " VALUES (?,?,?,?,?)",
                        (mix_id, g["gene"], g["name"], g["protospacer"], g["conc"]),
                    )
                    counts["guide"] += 1

        # --- images (CSV, streamed) + distinct wells ---
        csv_wells = set()
        if csv_path:
            bf_crops, fl_crops = _scan_crops(data_dir)
            conn.executemany(
                "INSERT INTO image(plate_id, well, timepoint, channel, z, px_nm, temp_c,"
                " stage_x, stage_y, stage_z, timestamp, raw_filename, crop_path)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _image_rows(csv_path, plate_id, csv_wells, bf_crops, fl_crops),
            )
            counts["image"] = conn.execute(
                "SELECT COUNT(*) FROM image WHERE plate_id = ?", (plate_id,)
            ).fetchone()[0]

        # --- well_annotation (screening, migrated) — provenanced upserts ---
        mixture_map = {}
        if scr:
            _version, ann_rows, mixture_map = scr
            for (w, col, val) in ann_rows:
                upsert_well_annotation(conn, plate_id, w, col, val, aid)
            counts["annotation"] = len(ann_rows)

        # --- plate_annotation + image_annotation (screening v3; additive) ---
        if screening:
            plate_rows, image_rows = parse_screening_levels(screening)
            for (col, val) in plate_rows:
                upsert_plate_annotation(conn, plate_id, col, val, aid)
            for (w, tp, col, val) in image_rows:
                upsert_image_annotation(conn, plate_id, w, tp, col, val, aid)
            counts["plate_ann"] = len(plate_rows)
            counts["image_ann"] = len(image_rows)

        # --- measurements: manual egg-size LINES -> measurement table -----------
        # No per-frame index in the source (egg size is well-level, drawn early on
        # SL01), so store at the representative timepoint 1.
        if manual_size:
            ms_rows = parse_manual_size(manual_size)
            if ms_rows:
                upsert_column_def(conn, "egg_diameter", "image", "measurement", created_by=aid)
                for (well, coords, lpx, lum, _sl) in ms_rows:
                    upsert_measurement(conn, plate_id, well, 1, "egg_diameter",
                                       coords, lpx, lum, aid)
                counts["measurement"] = len(ms_rows)

        # --- wells: union of all sources; mix_name from screening then experiment ---
        wells = {}
        for w in csv_wells:
            wells.setdefault(w, None)
        for (w, _c, _v) in (scr[1] if scr else []):
            wells.setdefault(w, None)
        for w, mx in mixture_map.items():           # screening 'mixture' column
            wells[w] = mx
        if exp:
            for w, mx in exp["well_mix"].items():    # experiment layout overrides
                wells[w] = mx
        conn.executemany(
            "INSERT OR REPLACE INTO well(plate_id, well, mix_name) VALUES (?,?,?)",
            ((plate_id, w, mx) for w, mx in wells.items()),
        )
        counts["well"] = len(wells)

    if verbose:
        srcs = []
        if csv_path:    srcs.append(f"csv={Path(csv_path).name}")
        if experiment:  srcs.append(f"exp={Path(experiment).name}")
        if screening:   srcs.append(f"scr={Path(screening).name}(v{scr[0]})")
        extra = ""
        if counts["plate_ann"] or counts["image_ann"]:
            extra = f" plate_ann={counts['plate_ann']} image_ann={counts['image_ann']}"
        if counts["measurement"]:
            extra += f" measurement={counts['measurement']}"
        print(f"[{plate_id}] {'  '.join(srcs) or 'no sources'}  ->  "
              f"image={counts['image']} well={counts['well']} mix={counts['mix']} "
              f"guide={counts['guide']} annotation={counts['annotation']}{extra}")
    return counts


def ingest_all(conn, root=DATA_ROOT, verbose=True):
    """Walk experiments/*.yaml + data/*/*_frame_metadata.csv and ingest each
    distinct plate once (auto-finding its other sources)."""
    plate_ids = {}  # preserve insertion order

    for yml in sorted((root / "experiments").glob("*.yaml")):
        try:
            pid = parse_experiment(yml)["plate_id"] or yml.stem
        except Exception:
            pid = yml.stem
        plate_ids.setdefault(str(pid), None)

    for csv_path in sorted((root / "AQ-EMBL").glob("*/metadata/*_frame_metadata.csv")):
        plate_ids.setdefault(csv_path.parent.parent.name, None)   # <plate>/metadata/<csv>
    for csv_path in sorted((root / "AQ-EMBL").glob("*/*_frame_metadata.csv")):     # legacy: csv at plate root
        plate_ids.setdefault(csv_path.parent.name, None)

    # also pick up plates that only have a screening JSON (dir name = plate id)
    for scr in sorted((root / "AQ-EMBL").glob("*/metadata/screening_*.json")):
        plate_ids.setdefault(scr.parent.parent.name, None)
    for scr in sorted((root / "AQ-EMBL").glob("*/screening_*.json")):        # legacy: screening at plate root
        plate_ids.setdefault(scr.parent.name, None)

    for pid in plate_ids:
        ingest_plate(conn, pid, root=root, auto=True, verbose=verbose)
    return list(plate_ids)


# ------------------------------------------------------------------ full pivot export
def export_full(conn, plate=None, out=None):
    """The FULL "future of the CSV": image_full (fixed columns) PLUS one column
    per distinct ``well_annotation.column`` name, pivoted in Python (pure SQL
    cannot pivot the EAV table into dynamic columns).

    ``plate`` restricts to one plate (and to that plate's annotation columns).
    If ``out`` is given, writes a CSV there and returns (header, n_rows);
    otherwise returns (header, rows) with rows as a list of lists.
    """
    params, where = [], ""
    if plate:
        where, params = " WHERE plate_id = ?", [plate]

    cur = conn.execute(
        f"SELECT * FROM image_full{where} ORDER BY plate_id, well, timepoint, channel, z",
        params,
    )
    base_cols = [d[0] for d in cur.description]

    # distinct annotation column names in scope
    ann_cols = [r[0] for r in conn.execute(
        f'SELECT DISTINCT "column" FROM well_annotation{where} ORDER BY "column"', params
    )]
    # (plate_id, well) -> {column: value}
    ann_map = {}
    for pid, well, col, val in conn.execute(
        f'SELECT plate_id, well, "column", value FROM well_annotation{where}', params
    ):
        ann_map.setdefault((pid, well), {})[col] = val

    # build output header, prefixing any annotation name that collides with a base column
    seen = set(base_cols)
    out_ann = []  # (source_column, output_name)
    for c in ann_cols:
        name = c if c not in seen else f"anno_{c}"
        seen.add(name)
        out_ann.append((c, name))
    header = base_cols + [n for _, n in out_ann]

    pi = base_cols.index("plate_id")
    wi = base_cols.index("well")

    def gen():
        for r in cur:
            am = ann_map.get((r[pi], r[wi]), {})
            yield list(r) + [am.get(c) for c, _ in out_ann]

    if out:
        n = 0
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            for row in gen():
                w.writerow(row)
                n += 1
        return header, n
    return header, list(gen())


# ------------------------------------------------------------------ stats
def stats(conn):
    tables = ["plate", "mix", "guide", "well", "image", "well_annotation",
              "plate_annotation", "image_annotation", "measurement",
              "annotator", "column_def"]
    print("== totals ==")
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:16s} {n}")
    print("== per plate ==")
    for (pid, line, cre) in conn.execute(
        "SELECT plate_id, line, cre_state FROM plate ORDER BY plate_id"
    ):
        img = conn.execute("SELECT COUNT(*) FROM image WHERE plate_id=?", (pid,)).fetchone()[0]
        wel = conn.execute("SELECT COUNT(*) FROM well WHERE plate_id=?", (pid,)).fetchone()[0]
        mix = conn.execute("SELECT COUNT(*) FROM mix WHERE plate_id=?", (pid,)).fetchone()[0]
        gui = conn.execute(
            "SELECT COUNT(*) FROM guide WHERE mix_id IN (SELECT mix_id FROM mix WHERE plate_id=?)",
            (pid,)).fetchone()[0]
        ann = conn.execute(
            "SELECT COUNT(*) FROM well_annotation WHERE plate_id=?", (pid,)).fetchone()[0]
        acols = [r[0] for r in conn.execute(
            'SELECT DISTINCT "column" FROM well_annotation WHERE plate_id=? ORDER BY "column"',
            (pid,))]
        meta = " ".join(x for x in (f"line={line}" if line else "",
                                    f"{cre}" if cre else "") if x)
        print(f"  {pid}")
        print(f"      {meta}  image={img} well={wel} mix={mix} guide={gui} "
              f"annotation={ann}" + (f"  cols={acols}" if acols else ""))


def run_query(conn, sql):
    cur = conn.execute(sql)
    if cur.description is None:
        conn.commit()
        print(f"(ok, {cur.rowcount} rows affected)")
        return
    w = csv.writer(sys.stdout)
    w.writerow([d[0] for d in cur.description])
    for row in cur:
        w.writerow(row)


# ------------------------------------------------------------------ CLI
def build_parser():
    ap = argparse.ArgumentParser(
        prog="build_db.py",
        description="Build/query the medaka image database (plan 25).",
    )
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help=f"SQLite file (default {DEFAULT_DB})")
    ap.add_argument("--root", default=str(DATA_ROOT),
                    help="MedakaNet root for auto-finding sources")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="ingest one plate (auto-finds omitted sources)")
    p.add_argument("--plate", help="plate folder / id (else derived from --experiment/--csv)")
    p.add_argument("--csv", help="frame_metadata.csv path")
    p.add_argument("--experiment", help="experiment .yaml path")
    p.add_argument("--screening", help="screening_<plate>.json path")

    sub.add_parser("ingest-all", help="walk experiments/ + data/ and ingest every plate")

    p = sub.add_parser("to-csv", help="export the full pivoted image_full to CSV")
    p.add_argument("--plate", help="restrict to one plate")
    p.add_argument("--out", required=True, help="output CSV path")

    p = sub.add_parser("query", help="run arbitrary SQL and print CSV")
    p.add_argument("sql", help="SQL statement")

    sub.add_parser("stats", help="print table + per-plate counts")
    return ap


def resolve_plate_id(args):
    if getattr(args, "plate", None):
        return args.plate
    if getattr(args, "experiment", None):
        pid = parse_experiment(args.experiment)["plate_id"]
        return pid or Path(args.experiment).stem
    if getattr(args, "csv", None):
        return Path(args.csv).resolve().parent.name
    return None


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    conn = connect(args.db)
    try:
        if args.cmd == "ingest":
            pid = resolve_plate_id(args)
            if not pid:
                sys.exit("error: give --plate, or --experiment/--csv to derive it")
            ingest_plate(conn, pid, csv_path=args.csv, experiment=args.experiment,
                         screening=args.screening, root=root, auto=True)
        elif args.cmd == "ingest-all":
            ingest_all(conn, root=root)
        elif args.cmd == "to-csv":
            header, n = export_full(conn, plate=args.plate, out=args.out)
            print(f"wrote {n} rows x {len(header)} cols -> {args.out}")
            print(f"columns: {header}")
        elif args.cmd == "query":
            run_query(conn, args.sql)
        elif args.cmd == "stats":
            stats(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
