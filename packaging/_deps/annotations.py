"""annotations.py — read image annotations (slice, rotation, iwamatsu_stage) and
measurements from medaka.db (the single source of truth the annotator writes),
with a screening-JSON fallback during the transition.

The film renderers import this so they read the SAME keyframes the annotator saved.
DB location: $MEDAKA_DB if set (e.g. a copy on the SMB share), else the local
imaging/data/medaka.db.
"""
from __future__ import annotations
import json
import os
import sqlite3
from pathlib import Path


def db_path():
    env = os.environ.get("MEDAKA_DB")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "medaka.db"


def _ro_conn(db=None):
    p = Path(db or db_path())
    if not p.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def image_keyframes(plate_id, well, column, db=None, screening_dir=None):
    """sorted [(tp, value)] keyframes for an image column — DB FIRST, JSON fallback.
    Only the real keyframes are stored (the in-between values are the caller's to
    interpolate). Latest-updated annotator wins per timepoint if several exist."""
    conn = _ro_conn(db)
    if conn is not None:
        try:
            rows = conn.execute(
                'SELECT timepoint, value, updated FROM image_annotation '
                'WHERE plate_id=? AND well=? AND "column"=? ORDER BY timepoint, updated',
                (plate_id, well, column)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        if rows:
            d = {}
            for t, v, _u in rows:                 # ORDER BY updated asc → newest wins per tp
                d[int(t)] = v
            return sorted(d.items())
    # ---- fallback: the plate's screening JSON ----
    if screening_dir:
        sd = Path(screening_dir)
        js = sorted(sd.glob("metadata/screening_*.json")) or sorted(sd.glob("screening_*.json"))
        if js:
            try:
                ia = (json.load(open(js[0])).get("image_annotations") or {}).get(well, {})
            except Exception:
                ia = {}
            out = [(int(t), e[column]) for t, e in ia.items()
                   if column in e and e[column] not in (None, "")]
            return sorted(out)
    return []


def measurements(plate_id, well=None, name=None, db=None):
    """measurement rows [(well, tp, name, x0,y0,x1,y1, length_px, length_um)] from the DB."""
    conn = _ro_conn(db)
    if conn is None:
        return []
    q = ('SELECT well, timepoint, name, x0,y0,x1,y1, length_px, length_um '
         'FROM measurement WHERE plate_id=?')
    args = [plate_id]
    if well:
        q += ' AND well=?'; args.append(well)
    if name:
        q += ' AND name=?'; args.append(name)
    try:
        return conn.execute(q + ' ORDER BY well, timepoint', args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def well_annotations(plate_id, db=None, screening_dir=None):
    """{well: {column: value}} well-scope annotations — DB first, screening-JSON
    fallback. Used to burn condition labels (mixture, line, …) onto montage tiles."""
    conn = _ro_conn(db)
    if conn is not None:
        try:
            rows = conn.execute(
                'SELECT well, "column", value FROM well_annotation WHERE plate_id=?',
                (plate_id,)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        if rows:
            out = {}
            for w, c, v in rows:
                out.setdefault(w, {})[c] = v
            return out
    if screening_dir:
        sd = Path(screening_dir)
        js = sorted(sd.glob("metadata/screening_*.json")) or sorted(sd.glob("screening_*.json"))
        if js:
            try:
                return json.load(open(js[0])).get("annotations", {}) or {}
            except Exception:
                pass
    return {}


def plate_meta(plate_id, db=None):
    """{date, line, cre_state, cadence_min, notes} for a plate ({} if unknown).
    `cadence_min` is what turns a timepoint index into an elapsed-time label."""
    conn = _ro_conn(db)
    if conn is None:
        return {}
    try:
        r = conn.execute('SELECT date, line, cre_state, cadence_min, notes FROM plate '
                         'WHERE plate_id=?', (plate_id,)).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    if not r:
        return {}
    return {"date": r[0], "line": r[1], "cre_state": r[2], "cadence_min": r[3], "notes": r[4]}


def pixel_size_um(plate_id, db=None):
    """µm per pixel for a plate, or None. Preference: the ratio the annotator's own
    measurements imply (length_um/length_px — already binning-corrected, so it is the
    honest number), else the acquisition `image.px_nm`/1000. Median over rows."""
    conn = _ro_conn(db)
    if conn is None:
        return None

    def _median(vals):
        vals = sorted(v for v in vals if v and v > 0)
        if not vals:
            return None
        n = len(vals)
        return vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
    try:
        rows = conn.execute('SELECT length_um, length_px FROM measurement WHERE plate_id=? '
                            'AND length_px > 0', (plate_id,)).fetchall()
        v = _median([u / p for u, p in rows if u and p])
        if v:
            return v
        rows = conn.execute('SELECT px_nm FROM image WHERE plate_id=? AND px_nm IS NOT NULL '
                            'LIMIT 5000', (plate_id,)).fetchall()
        v = _median([r[0] / 1000.0 for r in rows if r[0]])
        return v
    except sqlite3.Error:
        return None
    finally:
        conn.close()
