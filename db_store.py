#!/usr/bin/env python3
"""db_store.py — the DB-FIRST persistence layer for the medaka annotator.

The annotator used to load/save each plate from a per-plate ``screening_<plate>.json``.
This module makes ``medaka.db`` (the plan-25 image database, built by
``metadata_db/build_db.py``) the single source of truth instead: the server loads a
plate's v3 payload FROM the DB and saves it straight BACK, so every tool and every
annotator share one queryable store.

It is a thin adapter over ``build_db``'s write API — it never re-implements the
schema or the UPSERTs, only imports and calls them:

    open_db / create_db / detect_db  -- connection + discovery
    load_payload(conn, plate_id)     -- DB rows            -> v3 payload (frontend contract)
    save_payload(conn, plate_id, p)  -- v3 payload         -> DB rows (this annotator's)
    export_json(conn, plate_id)      -- the v3 payload dict (portability export)

KEYFRAMES ONLY
--------------
``image_annotation`` stores only the frames actually annotated (the keyframes the
user set). Interpolated / forward-filled values are NEVER materialised into the DB;
interpolation is derived on read by the frontend/renderers. ``save_payload`` writes
exactly what the payload carries, and ``load_payload`` returns exactly those rows.

The v3 payload shape (kept byte-for-byte compatible with model.py) is::

    {schema_version:3, plate, annotator,
     plate_columns:{name:{type,values,default?,fill?}}, plate_annotations:{col:val},
     columns:{...},        annotations:{well:{col:val}},
     image_columns:{...},  image_annotations:{well:{"<tp>":{col:val}}}}

Range values are ``[start,end]``; measurement values are objects
``{line:[x0,y0,x1,y1], length_px, length_um}`` (folded in from the ``measurement`` table).
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

_DATE_PREFIX = re.compile(r"^\d{8}[_-]")


def canon_plate(plate_id) -> str:
    """Canonical plate key = the folder name with any leading YYYYMMDD_ date prefix
    stripped, so the SAME plate opened via a dated share folder (20260512_AQV04_…)
    and an undated local folder (AQV04_…) resolves to ONE id — annotations follow the
    plate, not the folder-name variant."""
    return _DATE_PREFIX.sub("", str(plate_id or ""))

# ------------------------------------------------------------------ imports
# build_db lives in the sibling metadata_db/ tool in the full imaging tree; a standalone
# clone of THIS repo (or a frozen app) only carries the vendored copy under
# packaging/_deps. Both go on the path, and model.py sits next to us — so the app
# imports the same way from any cwd. Order matters: inserted LAST = searched FIRST, so
# the live sibling wins over a stale vendored copy when both exist.
_HERE = Path(__file__).resolve().parent
for _p in (_HERE / "packaging" / "_deps", _HERE.parent / "metadata_db", _HERE):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build_db          # the DB schema + write API we USE (never re-implement)  # noqa: E402
import model             # normalize_payload + the canonical v3 skeleton          # noqa: E402

# tables a "correctly-set-up" annotator DB must have (distinguishes medaka.db from
# an arbitrary .db that merely shares the extension)
_REQUIRED_TABLES = {"column_def", "annotator", "image_annotation"}

# column_def.level  ->  payload key that holds that scope's columns
_LEVEL_TO_KEY = {"plate": "plate_columns", "well": "columns", "image": "image_columns"}
# payload key (per scope)  ->  (level, columns-key) used when saving
_SAVE_LEVELS = (("plate", "plate_columns"), ("well", "columns"), ("image", "image_columns"))


# ============================================================ connection / discovery
def open_db(path, check_same_thread=True):
    """Open ``path`` (creating the schema if absent). Default == ``build_db.connect(path)``.

    The server passes ``check_same_thread=False`` for its single process-wide,
    lock-guarded connection, because ``ThreadingHTTPServer`` serves each request on
    its own thread. That variant mirrors ``build_db.connect`` exactly (same PRAGMAs,
    the same imported ``build_db.SCHEMA``) — it just drops sqlite3's per-thread guard.
    """
    if check_same_thread:
        return build_db.connect(path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")      # concurrent readers (e.g. the build_db CLI)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(build_db.SCHEMA)          # CREATE ... IF NOT EXISTS: no-op on an existing DB
    conn.commit()
    return conn


def _has_annotator_schema(db_path) -> bool:
    """True iff ``db_path`` is a readable SQLite file carrying the annotator tables.

    Uses a READ-ONLY connection so probing never CREATES the tables (which
    ``build_db.connect`` would, making every ``.db`` look valid)."""
    try:
        conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        return _REQUIRED_TABLES.issubset(names)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def detect_db(folder):
    """Return the Path of a properly-set-up annotator ``.db`` inside ``folder``.

    Prefers ``medaka.db``; otherwise the first ``*.db`` whose schema HAS the
    annotator tables (column_def + annotator + image_annotation). Returns None
    when the folder has no such DB (so the caller can prompt to create one)."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    candidates = []
    medaka = folder / "medaka.db"
    if medaka.is_file():
        candidates.append(medaka)
    for p in sorted(folder.glob("*.db")):
        if p != medaka:
            candidates.append(p)
    for p in candidates:
        if _has_annotator_schema(p):
            return p
    return None


def create_db(folder, name):
    """Create ``<folder>/<name>.db`` with the full schema and return its Path.

    ``name`` is reduced to a bare filename and given a ``.db`` suffix if missing;
    ``build_db.connect`` builds every table (CREATE ... IF NOT EXISTS)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(str(name or "medaka")).name          # never let a name escape the folder
    if not name.endswith(".db"):
        name += ".db"
    path = folder / name
    conn = build_db.connect(path)
    conn.close()
    return path


# ============================================================ value (de)serialisation
def _value_to_text(val):
    """Serialise a payload value to the TEXT stored in the EAV tables. Ranges (and any
    list/tuple) become compact JSON '[a,b]', matching build_db's own encoding; every
    other scalar becomes its string form. Measurements never come here (they go to the
    measurement table)."""
    if isinstance(val, (list, tuple)):
        return json.dumps(list(val), separators=(",", ":"))
    return str(val)


def _maybe_range(text, is_range):
    """Parse a stored '[a,b]' back into a list for range columns; leave anything
    else as the raw string (the frontend coerces float-ish / rotation values)."""
    if is_range and isinstance(text, str):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return text
        if isinstance(parsed, list):
            return parsed
    return text


# ============================================================ load: DB -> v3 payload
def load_payload(conn, plate_id, annotator=None):
    """Build the v3 payload for ``plate_id`` FROM the DB (the frontend contract).

    * columns / image_columns / plate_columns come from the GLOBAL column_def
      registry (a column created once is visible on every plate).
    * annotations / plate_annotations / image_annotations come from this plate's
      EAV rows; measurements are FOLDED INTO image_annotations as
      ``{line, length_px, length_um}`` objects so the measure tool displays them.
    * image_annotation rows are the KEYFRAMES exactly as stored — no interpolation.
    * ``annotator`` given → ONLY that person's rows (isolation: each annotator sees
      and edits only their own work). None → every annotator merged, latest wins.
    """
    plate_id = canon_plate(plate_id)
    payload = model.fresh_payload(plate_id)

    # annotator isolation: resolve the id once; -1 = a name with no rows yet (fresh view)
    aid = None
    if annotator:
        r = conn.execute("SELECT annotator_id FROM annotator WHERE name=? COLLATE NOCASE",
                         (annotator,)).fetchone()
        aid = r[0] if r else -1
    afrag = " AND annotator_id=?" if aid is not None else ""     # SQL fragment
    apar = (aid,) if aid is not None else ()                     # its bound param

    # --- column_def -> the three column maps (global; not filtered by plate) -------
    for name, level, ctype, values_json, default_val, fill in conn.execute(
            'SELECT name, level, type, values_json, default_val, "fill" FROM column_def'):
        key = _LEVEL_TO_KEY.get(level)
        if key is None:
            continue
        try:
            values = json.loads(values_json) if values_json else []
        except (TypeError, ValueError):
            values = []
        entry = {"type": ctype, "values": values}
        if default_val is not None:                 # drop None fields
            entry["default"] = default_val
        if fill is not None:
            entry["fill"] = fill
        payload[key][name] = entry

    def _is_range(colkey, col):
        return (payload[colkey].get(col) or {}).get("type") == "range"

    # --- well_annotation -> annotations{well:{col:val}} (latest updated wins) ------
    for well, col, value in conn.execute(
            'SELECT well, "column", value FROM well_annotation WHERE plate_id=?' + afrag +
            ' ORDER BY updated', (plate_id, *apar)):
        payload["annotations"].setdefault(well, {})[col] = \
            _maybe_range(value, _is_range("columns", col))

    # --- plate_annotation -> plate_annotations{col:val} ---------------------------
    for col, value in conn.execute(
            'SELECT "column", value FROM plate_annotation WHERE plate_id=?' + afrag +
            ' ORDER BY updated', (plate_id, *apar)):
        payload["plate_annotations"][col] = \
            _maybe_range(value, _is_range("plate_columns", col))

    # --- image_annotation -> image_annotations{well:{tp:{col:val}}} KEYFRAMES ------
    # values are left as the stored strings (the frontend coerces angle/float).
    for well, tp, col, value in conn.execute(
            'SELECT well, timepoint, "column", value FROM image_annotation '
            'WHERE plate_id=?' + afrag + ' ORDER BY updated', (plate_id, *apar)):
        payload["image_annotations"].setdefault(well, {}) \
            .setdefault(str(tp), {})[col] = value

    # --- measurement -> folded into image_annotations as line objects -------------
    for well, tp, name, x0, y0, x1, y1, lpx, lum in conn.execute(
            "SELECT well, timepoint, name, x0, y0, x1, y1, length_px, length_um "
            "FROM measurement WHERE plate_id=?" + afrag + " ORDER BY updated",
            (plate_id, *apar)):
        payload["image_annotations"].setdefault(well, {}) \
            .setdefault(str(tp), {})[name] = {
                "line": [x0, y0, x1, y1], "length_px": lpx, "length_um": lum}

    # --- annotator hint: who most recently touched this plate (UX prefill) ---------
    row = conn.execute(
        "SELECT a.name FROM ("
        "  SELECT annotator_id, updated FROM well_annotation  WHERE plate_id=? "
        "  UNION ALL SELECT annotator_id, updated FROM plate_annotation WHERE plate_id=? "
        "  UNION ALL SELECT annotator_id, updated FROM image_annotation WHERE plate_id=? "
        "  UNION ALL SELECT annotator_id, updated FROM measurement      WHERE plate_id=? "
        ") x JOIN annotator a ON a.annotator_id = x.annotator_id "
        "ORDER BY x.updated DESC LIMIT 1",
        (plate_id, plate_id, plate_id, plate_id)).fetchone()
    if row and row[0]:
        payload["annotator"] = row[0]

    return payload


# ============================================================ save: v3 payload -> DB
def _delete_annotator_rows(conn, plate_id, annotator_id):
    """Clear only THIS annotator's annotation rows for the plate (so cells they
    removed disappear) — never touching other annotators' rows, nor the image/well/
    plate/mix metadata that build_db owns."""
    for t in ("well_annotation", "plate_annotation", "image_annotation", "measurement"):
        conn.execute(f"DELETE FROM {t} WHERE plate_id=? AND annotator_id=?",
                     (plate_id, annotator_id))


def save_payload(conn, plate_id, payload, default_annotator="tiago"):
    """Persist a v3 payload for ``plate_id`` as ``payload['annotator']``'s rows.

    Steps: normalize (model.normalize_payload) -> resolve the annotator id ->
    delete that annotator's existing rows for the plate -> register every column
    into the global column_def registry -> UPSERT well / plate / image annotations
    and measurements. Returns the normalized payload that was written.
    """
    plate_id = canon_plate(plate_id)
    payload = model.normalize_payload(payload, plate_id)
    aid = build_db.get_or_create_annotator(conn, payload.get("annotator") or default_annotator)

    _delete_annotator_rows(conn, plate_id, aid)

    # --- column defs (global registry), all three scopes --------------------------
    for level, key in _SAVE_LEVELS:
        for name, spec in (payload.get(key) or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            build_db.upsert_column_def(
                conn, name, level, spec.get("type", "categorical"),
                spec.get("values"), spec.get("default"), spec.get("fill"),
                created_by=aid)

    # --- well annotations ---------------------------------------------------------
    for well, entry in (payload.get("annotations") or {}).items():
        for col, val in entry.items():
            build_db.upsert_well_annotation(conn, plate_id, well, col,
                                            _value_to_text(val), aid)

    # --- plate annotations --------------------------------------------------------
    for col, val in (payload.get("plate_annotations") or {}).items():
        build_db.upsert_plate_annotation(conn, plate_id, col, _value_to_text(val), aid)

    # --- image annotations (keyframes) + measurements -----------------------------
    img_cols = payload.get("image_columns") or {}
    for well, per_tp in (payload.get("image_annotations") or {}).items():
        for tp, entry in per_tp.items():
            try:
                tpi = int(tp)
            except (TypeError, ValueError):
                continue
            for col, val in entry.items():
                ctype = (img_cols.get(col) or {}).get("type")
                is_measurement = ctype == "measurement" or (
                    isinstance(val, dict) and "line" in val)
                if is_measurement:
                    line = (val.get("line") if isinstance(val, dict) else None) or []
                    coords = tuple((list(line) + [None, None, None, None])[:4])
                    build_db.upsert_measurement(
                        conn, plate_id, well, tpi, col, coords,
                        (val or {}).get("length_px"), (val or {}).get("length_um"), aid)
                else:
                    build_db.upsert_image_annotation(
                        conn, plate_id, well, tpi, col, _value_to_text(val), aid)

    conn.commit()
    return payload


# ============================================================ portability export
def export_json(conn, plate_id):
    """The v3 payload dict for ``plate_id`` — same shape a screening JSON carried,
    for a 'download this plate' portability button."""
    return load_payload(conn, plate_id)


def _csv_val(v):
    if isinstance(v, (dict, list, tuple)):              # measurements / ranges → compact JSON
        return json.dumps(v if not isinstance(v, tuple) else list(v), separators=(",", ":"))
    return "" if v is None else str(v)


def payload_to_csv(payload, plate_id) -> str:
    """A tidy long-format CSV of every annotation in a plate's payload — one row per
    (scope, well, timepoint, column, value). Opens straight in Excel; no SQLite needed."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["plate", "scope", "well", "timepoint", "column", "value", "annotator"])
    pid = canon_plate(plate_id)
    who = (payload.get("annotator") or "")
    for col, val in (payload.get("plate_annotations") or {}).items():
        w.writerow([pid, "plate", "", "", col, _csv_val(val), who])
    for well, entry in (payload.get("annotations") or {}).items():
        for col, val in entry.items():
            w.writerow([pid, "well", well, "", col, _csv_val(val), who])
    for well, per_tp in (payload.get("image_annotations") or {}).items():
        for tp, entry in per_tp.items():
            for col, val in entry.items():
                w.writerow([pid, "image", well, tp, col, _csv_val(val), who])
    return buf.getvalue()


_ANNOT_TABLES = (
    ("well_annotation",  ["plate_id", "well", "column", "annotator_id"]),
    ("plate_annotation", ["plate_id", "column", "annotator_id"]),
    ("image_annotation", ["plate_id", "well", "timepoint", "column", "annotator_id"]),
    ("measurement",      ["plate_id", "well", "timepoint", "name", "annotator_id"]),
)


def record_data_root(db_path, root_path, label: str = "") -> None:
    """Record that `root_path` is one of the folders whose plates feed THIS database.
    Stored inside the DB (a `data_root` table) so the database is self-describing and
    portable: whoever opens it can see every location it collects annotations from."""
    try:
        conn = build_db.connect(Path(db_path))
    except sqlite3.Error:
        return
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS data_root ("
                     "path TEXT PRIMARY KEY, label TEXT, last_seen TEXT)")
        now = datetime.datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO data_root(path, label, last_seen) VALUES(?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET last_seen=excluded.last_seen, "
            "label=CASE WHEN excluded.label<>'' THEN excluded.label ELSE data_root.label END",
            (str(root_path), label, now))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def list_data_roots(db_path) -> list:
    """The folders recorded as feeding this database (self-describing manifest)."""
    try:
        c = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = c.execute("SELECT path, label, last_seen FROM data_root "
                         "ORDER BY last_seen DESC").fetchall()
        return [{"path": p, "label": l or "", "last_seen": t or ""} for p, l, t in rows]
    except sqlite3.Error:
        return []
    finally:
        c.close()


def annotation_count(db_path) -> int:
    """Total annotation rows in a DB (well+plate+image+measurement), or 0 if unreadable."""
    try:
        c = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    n = 0
    try:
        for tbl, _ in _ANNOT_TABLES:
            try:
                n += c.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
            except sqlite3.Error:
                pass
    finally:
        c.close()
    return n


def merge_db(src_path, dst_path) -> dict:
    """Merge annotations from src DB INTO dst DB — NON-destructive to src. Per cell the
    latest `updated` wins (dst keeps its own if newer). Annotators are matched BY NAME
    (ids remapped, created in dst as needed) and column defs are brought over. Plate ids
    are already canonical (date-prefix stripped), so the same plate under different folder
    names merges correctly. Returns {table: rows_written}."""
    src = sqlite3.connect(f"file:{Path(src_path)}?mode=ro", uri=True)
    dst = build_db.connect(Path(dst_path))
    counts = {}
    try:
        # annotator id remap: src id -> dst id (by name)
        id_map = {}
        try:
            for sid, name in src.execute("SELECT annotator_id, name FROM annotator"):
                id_map[sid] = build_db.get_or_create_annotator(dst, name or "unknown")
        except sqlite3.Error:
            pass
        # column defs (global registry) — add any src column not already in dst
        try:
            for name, level, ctype, values_json, default_val, fill in src.execute(
                    'SELECT name, level, type, values_json, default_val, "fill" FROM column_def'):
                try:
                    vals = json.loads(values_json) if values_json else None
                except (TypeError, ValueError):
                    vals = None
                build_db.upsert_column_def(dst, name, level, ctype, vals, default_val, fill,
                                           created_by=id_map.get(0, 0))
        except sqlite3.Error:
            pass
        # the four annotation tables — latest-updated wins
        for tbl, keycols in _ANNOT_TABLES:
            try:
                cols = [d[1] for d in src.execute(f'PRAGMA table_info("{tbl}")')]
            except sqlite3.Error:
                continue
            ai = cols.index("annotator_id") if "annotator_id" in cols else None
            written = 0
            for row in src.execute(f'SELECT * FROM "{tbl}"'):
                r = dict(zip(cols, row))
                if ai is not None:
                    r["annotator_id"] = id_map.get(r.get("annotator_id"), r.get("annotator_id"))
                key = tuple(r[k] for k in keycols)
                where = " AND ".join(f'"{k}"=?' for k in keycols)
                ex = dst.execute(f'SELECT updated FROM "{tbl}" WHERE {where}', key).fetchone()
                if ex is None or str(r.get("updated") or "") > str(ex[0] or ""):
                    qc = ",".join(f'"{c}"' for c in cols)
                    ph = ",".join("?" * len(cols))
                    dst.execute(f'INSERT OR REPLACE INTO "{tbl}" ({qc}) VALUES ({ph})',
                                tuple(r[c] for c in cols))
                    written += 1
            counts[tbl] = written
        dst.commit()
    finally:
        src.close()
        dst.close()
    return counts


def list_annotators(conn) -> list:
    """Known annotator names (for the name-suggestion dropdown)."""
    try:
        return [r[0] for r in conn.execute(
            "SELECT name FROM annotator WHERE name IS NOT NULL AND name != '' ORDER BY name")]
    except sqlite3.Error:
        return []
