#!/usr/bin/env python3
"""annotation_app/server.py — a tiny, zero-dependency backend for the annotator.

Uses only the Python standard library plus Pillow (already in the ``twinnet``
env) to decode the crop TIFFs into browser-friendly PNGs on demand — browsers
can't render TIFF, and a light backend gives a smooth per-well fader and the
"share with another annotator" case, without any build step or pip install.

Run::

    conda activate twinnet
    python annotation_app/server.py                 # pick a plate in the browser
    python annotation_app/server.py AQV04            # open focused on a plate (prefix ok)
    python annotation_app/server.py --data-root DIR --port 8765

API (all JSON except /api/frame which is a PNG)::

    GET  /                       -> the app (index.html)
    GET  /static/<f>             -> app.js / style.css
    GET  /api/config             -> iwamatsu stages, seed defaults, registry, plate list
    GET  /api/plate?dir=NAME     -> manifest (wells/frames/channels/autofill) + saved payload
    GET  /api/frame?dir=&well=&ch=&tp=&size=   -> a PNG of one crop (cached, resized)
    POST /api/save   {dir, payload}            -> atomic v3 write + registry merge

The client owns the live annotation state and undo/redo; the server just
discovers layout, serves images, and validates+saves. See model.py.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import time
import errno
import threading
import argparse
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import model
import db_store
import version

class _NullStream:
    """Somewhere for output to go when there is nowhere for output to go.

    A windowed build has no console, and PyInstaller leaves ``sys.stdout``/``sys.stderr``
    as **None**. CPython's ``print`` quietly tolerates that; ``sys.stderr.write(...)`` does
    not — it is an ``AttributeError``. That asymmetry cost a release. The HTTP request
    logger writes to stderr directly, and ``send_response`` logs BEFORE it sends a single
    byte, so *every request in the app* died with the client seeing nothing but a closed
    connection (``RemoteDisconnected``) and no traceback anywhere, because the traceback
    also goes to the stderr that is not there.

    Standing in for the missing stream fixes the whole class of it: nothing that writes a
    message can raise, wherever in the codebase it lives and whoever writes it next.
    """
    encoding = "utf-8"
    errors = "replace"
    closed = False

    def write(self, text):
        return len(text) if isinstance(text, str) else 0

    def writelines(self, lines):
        for _ in lines:
            pass

    def flush(self):
        pass

    def close(self):
        pass

    def isatty(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def fileno(self):
        raise io.UnsupportedOperation("this build has no console")


def make_console_safe():
    """Never let a console message crash the app.

    Two ways a message has managed to kill this app, both fixed here:

    * Windows consoles default to cp1252, which cannot encode the arrows, ellipses, µ and
      warning signs this codebase prints — and an unencodable character raises
      UnicodeEncodeError from inside `print`, which in a windowed build surfaces as
      "Failed to execute script 'desktop'" and no app at all. A launch banner is not worth
      a crash: switch the streams to UTF-8, and if even that is refused, keep the console's
      own encoding but replace anything it cannot render.
    * A windowed build has **no streams at all**. Give the app somewhere to write instead
      of leaving None in place for the next `.write` to trip over (see _NullStream).
    """
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                  # noqa: BLE001
            try:
                stream.reconfigure(errors="replace")       # at least stop it raising
            except Exception:                              # noqa: BLE001
                pass


make_console_safe()

APP_DIR = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)          # running from a packaged .app / .exe
# In the source tree, plates live under imaging/data/AQ-EMBL/ (server.py is at
# imaging/tools/label_annotator/). A packaged app is somewhere else entirely — it has no
# business guessing a path inside its own bundle — so it starts at the folder you last
# opened, and otherwise at your home folder, where "📂 Open" takes over.
DEFAULT_DATA_ROOT = APP_DIR.parents[1] / "data" / "AQ-EMBL"


def default_data_root() -> Path:
    """The folder to open on launch: the one last opened, else the dev-tree default,
    else home. Never a path inside the bundle."""
    last = (_load_settings().get("last_data_root") or "").strip()
    if last and Path(last).is_dir():
        return Path(last)
    if not FROZEN and DEFAULT_DATA_ROOT.is_dir():
        return DEFAULT_DATA_ROOT
    return Path.home()

# ------------------------------------------------------------------ DB-first store
# medaka.db is the single source of truth for annotations (not per-plate JSON).
# One process-wide connection, opened in WAL and GUARDED by _DB_LOCK because this is
# a ThreadingHTTPServer (each request runs on its own thread; sqlite3 needs one owner
# or a lock). Resolved at startup; if no DB is found the UI is told (needs_db) so it
# can prompt for a name -> POST /api/create-db.
_DB = {"conn": None, "path": None, "folder": None, "needs_db": True, "local_fallback": False}
_DB_LOCK = threading.Lock()


def _registry_path() -> Path:
    return Path.home() / ".medaka_annotator" / "registry.json"


def _load_registry() -> dict:
    try:
        return json.loads(_registry_path().read_text())
    except Exception:
        return {}


def _save_registry(reg: dict):
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(p)


def _norm(p) -> str:
    try:
        return str(Path(p).resolve())
    except Exception:
        return str(Path(p))


def _registry_lookup(data_root):
    """A DB explicitly LINKED to `data_root` (or to a folder that contains it), or None.
    Lets a local plate folder and its SMB-share twin resolve to the SAME local DB —
    open either and annotations land in one place. Longest matching root wins."""
    reg = _load_registry()
    if not reg:
        return None
    target = _norm(data_root)
    best = None
    for root, db in reg.items():
        r = _norm(root)
        if target == r or target.startswith(r.rstrip("/") + "/"):
            if best is None or len(r) > len(best[0]):
                best = (r, db)
    if best and Path(best[1]).exists():
        return Path(best[1])
    return None


def link_root(data_root, db_path):
    """Register `data_root` → `db_path` so opening that folder (local OR a share mount)
    always uses this DB. Persisted in ~/.medaka_annotator/registry.json."""
    reg = _load_registry()
    reg[_norm(data_root)] = str(Path(db_path).resolve())
    _save_registry(reg)
    return reg


# ------------------------------------------------------------------ settings
# Persisted user prefs: WHERE annotations go (a visible folder, not the hidden dot-dir),
# WHICH formats to write (db/csv/json), and where TIF/MP4 exports go. Lives in app-data.
_SETTINGS_DEFAULTS = {
    "annotations_dir": "",   # visible folder for the DB + CSV + JSON; "" = auto-resolve (legacy)
    "formats": {"db": True, "csv": True, "json": True},
    "export_dir": "",        # folder for TIF/MP4 exports; "" = next to the plate (legacy)
    "filters": {},           # saved cross-plate filters: {name: {plates, constraints, …}}
    "last_data_root": "",    # the folder last opened — a packaged app reopens it
    "cache_gb": 0,           # on-disk cache cap in GB; 0 = adapt to free disk space
    "cache_lossless": False, # True = cache PNG instead of JPEG (bigger, exact)
    "cache_quality": 88,     # JPEG quality when not lossless
}


def _settings_path() -> Path:
    return Path.home() / ".medaka_annotator" / "settings.json"


def _load_settings() -> dict:
    s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _SETTINGS_DEFAULTS.items()}
    try:
        saved = json.loads(_settings_path().read_text())
        for k, v in (saved or {}).items():
            if k == "formats" and isinstance(v, dict):
                s["formats"].update({fk: bool(fv) for fk, fv in v.items()})
            elif k in s:
                s[k] = v
    except Exception:
        pass
    return s


def _save_settings(patch: dict) -> dict:
    s = _load_settings()
    for k, v in (patch or {}).items():
        if k == "formats" and isinstance(v, dict):
            s["formats"].update({fk: bool(fv) for fk, fv in v.items()})
        elif k == "filters" and isinstance(v, dict):
            # merge, and let an explicit null delete one saved filter by name
            for fk, fv in v.items():
                if fv is None:
                    s["filters"].pop(fk, None)
                else:
                    s["filters"][fk] = fv
        elif k in _SETTINGS_DEFAULTS:
            s[k] = v
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(p)
    _cache_settings_memo["val"] = None      # a new cache limit must take effect NOW
    return s


def _resolve_db_location(data_root: Path):
    """Find the annotator DB for a plate data-root.

    **The database belongs WITH THE IMAGES.** Opening a folder of plates uses (or
    creates) the database in that folder — so a folder is self-describing, and moving
    or sharing it carries its annotations along.

    Priority:
      1. a REGISTRY LINK for this exact data-root — explicit, per-folder, so a local
         folder and its SMB twin can be pointed at one shared database on purpose;
      2. an existing database in the data-root (or its parent);
      3. a NEW database in the data-root, if it can be written to;
      4. only if it cannot: `annotations_dir` from Settings, else a local fallback.

    `annotations_dir` used to sit at position 0 and win over everything. That made the
    LAST folder you saved into follow you to every folder you opened next: you would
    open a fresh plate folder and still be reading — and writing — the previous
    database, columns and all. It is now the fallback for a folder you cannot write to
    (a read-only share), not a default that overrides where the images live.

    Returns (folder_to_create_in, existing_db_path_or_None).
    """
    data_root = Path(data_root)
    linked = _registry_lookup(data_root)
    if linked is not None:                               # explicit, per-folder
        return linked.parent, linked
    for folder in (data_root, data_root.parent):         # the images' own folder wins
        found = db_store.detect_db(folder)
        if found is not None:
            return folder, found
    # nothing yet: create beside the images (the dir holding AQ-EMBL, if that's the shape)
    create_folder = data_root.parent if data_root.name == "AQ-EMBL" else data_root
    if os.access(create_folder, os.W_OK):
        return create_folder, None
    adir = (_load_settings().get("annotations_dir") or "").strip()
    if adir:                                             # can't write next to the images
        folder = Path(adir).expanduser()
        return folder, db_store.detect_db(folder)
    return create_folder, None                           # _autocreate_db falls back locally


def _is_network_fs(path) -> bool:
    """Best-effort: True if `path` sits on a network filesystem (SMB/NFS/AFP), where an
    SQLite WAL DB is unreliable — so we keep the annotator DB on local disk instead
    (aulehla is also ~full and corrupts writes). Falls back to False (treat as local)."""
    try:
        import subprocess
        target = str(Path(path).resolve())
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
        best_mp, best_fs = "", ""
        for line in out.splitlines():
            m = re.search(r" on (.+?) \(([^,)]+)", line) or re.search(r" on (.+?) type (\S+)", line)
            if not m:
                continue
            mp, fs = m.group(1), m.group(2)
            if (target == mp or target.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best_mp):
                best_mp, best_fs = mp, fs
        return any(k in best_fs.lower() for k in ("smb", "nfs", "afp", "cifs", "webdav", "fuse"))
    except Exception:
        return False


def _local_db_dir(source) -> Path:
    """A stable LOCAL home for the annotator DB of a given source folder (e.g. an SMB
    mount), keyed by the source path so re-opening the same share reuses the same DB."""
    import hashlib
    key = hashlib.sha1(str(Path(source).resolve()).encode()).hexdigest()[:10]
    return Path.home() / ".medaka_annotator" / key


def _autocreate_db(folder, net) -> Path | None:
    """Create (or reuse) medaka.db for `folder`. On a network share (`net`) — or if
    `folder` isn't writable — the DB is kept LOCALLY. Sets _DB['folder']; returns the
    DB Path, or None if even the local fallback fails."""
    targets = [_local_db_dir(folder)] if net else [Path(folder), _local_db_dir(folder)]
    for target in targets:
        try:
            path = db_store.detect_db(target) or db_store.create_db(target, "medaka")
            if db_store.detect_db(target) is not None:      # verify the schema actually wrote
                _DB["folder"] = Path(target)
                return path
        except Exception as e:                              # read-only / disk full / SMB lock
            print(f"  ! DB unavailable in {target} ({e})", file=sys.stderr)
    return None


def _open_process_db(data_root: Path, auto_create: bool = True):
    """Resolve + open the process-wide DB connection. A REGISTRY LINK wins (share and
    local twin share one DB); otherwise use an existing DB, else CREATE one — kept local
    when the source is a network share. Never raises: if even the local fallback fails,
    needs_db stays True and saves surface a clear error."""
    data_root = Path(data_root)
    adir = (_load_settings().get("annotations_dir") or "").strip()
    net = False if adir else _is_network_fs(data_root)   # a chosen annotations dir is local
    folder, path = _resolve_db_location(data_root)
    _DB["folder"] = folder
    if path is None and auto_create:
        path = _autocreate_db(folder, net)
    if path is not None:
        _DB["conn"] = db_store.open_db(path, check_same_thread=False)
        _DB["path"], _DB["needs_db"], _DB["local_fallback"] = path, False, net
    else:
        _DB["conn"], _DB["path"], _DB["needs_db"], _DB["local_fallback"] = None, None, True, False
    return _DB


def _db_info() -> dict:
    """DB state for the client (config + db-status + open-folder)."""
    return {"db_path": str(_DB["path"]) if _DB["path"] else None,
            "exists": _DB["conn"] is not None,
            "needs_db": _DB["needs_db"],
            "folder": str(_DB["folder"]) if _DB["folder"] else None,
            "local_fallback": bool(_DB.get("local_fallback"))}


def _write_side_exports(plate_name) -> dict:
    """Write CSV / JSON copies of a plate's annotations to the visible annotations folder
    (per Settings.formats). SQLite stays the source of truth; these are the human-friendly
    no-SQLite-needed copies. Returns {fmt: path}."""
    s = _load_settings()
    fmts = s.get("formats") or {}
    if _DB["conn"] is None or not (fmts.get("csv") or fmts.get("json")):
        return {}
    out_dir = Path((s.get("annotations_dir") or "").strip() or _DB["folder"] or ".").expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with _DB_LOCK:
            payload = db_store.export_json(_DB["conn"], plate_name)
    except Exception as e:                              # noqa: BLE001
        print(f"  ! side-export skipped ({e})", file=sys.stderr)
        return {}
    pid = db_store.canon_plate(plate_name)
    written = {}
    try:
        if fmts.get("json"):
            p = out_dir / f"{pid}.json"
            p.write_text(json.dumps(payload, indent=2)); written["json"] = str(p)
        if fmts.get("csv"):
            p = out_dir / f"{pid}_annotations.csv"
            p.write_text(db_store.payload_to_csv(payload, plate_name)); written["csv"] = str(p)
    except Exception as e:                              # noqa: BLE001
        print(f"  ! side-export write failed ({e})", file=sys.stderr)
    return written


def _db_load_payload(plate_name: str, annotator: str = None) -> dict:
    """The plate's v3 payload from the DB (or a fresh skeleton if no DB yet). When an
    annotator is given, only THEIR annotations are returned (each person sees their own)."""
    if _DB["conn"] is None:
        return model.fresh_payload(plate_name)
    with _DB_LOCK:
        return db_store.load_payload(_DB["conn"], plate_name, annotator or None)

# ------------------------------------------------------------------ image decode
try:
    from PIL import Image
except ImportError:                      # pragma: no cover - clearer error
    sys.exit("Pillow is required: `conda activate twinnet` (or `pip install pillow`).")


class _LRU(OrderedDict):
    """A thread-safe in-RAM LRU of decoded frames, bounded by BYTES.

    It used to be bounded by COUNT (1500 frames), which says nothing about memory: 1500
    frames is ~75 MB of 600 px JPEGs but ~600 MB of 1024 px PNGs. A cache whose size
    depends on which view you happened to open is not a budget. Bytes it is.
    """
    def __init__(self, cap_bytes=192 * 1024 ** 2):
        super().__init__()
        self.cap_bytes = cap_bytes
        self.nbytes = 0
        self.lock = threading.Lock()

    @staticmethod
    def _size(val):
        if isinstance(val, tuple):        # (bytes, mime)
            return len(val[0]) if val and isinstance(val[0], (bytes, bytearray)) else 0
        return len(val) if isinstance(val, (bytes, bytearray)) else 0

    def peek(self, key):
        """The value if it is already decoded, else None — and never a read of any kind.
        Lets a caller answer from memory WITHOUT entering the guarded store path, so a
        stalled share cannot slow down frames the app is already holding."""
        with self.lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
        return None

    def get_or(self, key, make):
        with self.lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
        val = make()                      # decode outside the lock
        n = self._size(val)
        with self.lock:
            if key in self:               # another thread won the race
                self.nbytes -= self._size(self[key])
            self[key] = val
            self.nbytes += n
            self.move_to_end(key)
            while self.nbytes > self.cap_bytes and len(self) > 1:
                _k, v = self.popitem(last=False)
                self.nbytes -= self._size(v)
        return val

    def stats(self):
        with self.lock:
            return {"bytes": self.nbytes, "frames": len(self), "cap_bytes": self.cap_bytes}


# ~192 MB: a few hundred frames of the current view, sized so the app stays comfortable
# on a laptop. The BIG cache is on disk; this one only saves a re-decode.
_PNG_CACHE = _LRU(cap_bytes=192 * 1024 ** 2)

# ---------------------------------------------------------------- 16-bit display
# A 16-bit crop holds physical camera counts (the pipeline's --fl-scale raw16),
# so the display stretch must be ONE map for the whole plate. Stretching each
# frame between its own min and max — which this used to do — makes a fading
# signal look constant and a constant one look like it is changing, i.e. it
# silently destroys exactly the comparison the images were kept 16-bit for.
_DISPLAY_RANGE_CACHE: dict = {}


def _display_range(path: Path):
    """(lo, hi) for a 16-bit crop: one constant map per plate, or None.

    Walks up from the crop file looking for the plate's own calibration
    (`plate_calibration.json` / `plate_metadata.json`) and takes the channel's
    percentiles. Returns None when there is no calibration to be found, in
    which case the caller falls back to a per-frame stretch — acceptable for an
    arbitrary standalone TIFF, never for a time course.
    """
    channel = None
    for p in path.parents:
        if p.name.startswith("CO") and p.name[2:].isdigit():
            channel = p.name
        key = str(p)
        if key in _DISPLAY_RANGE_CACHE:
            rng = _DISPLAY_RANGE_CACHE[key]
            return rng.get(channel) if rng else None
        found = None
        for name in ("plate_calibration.json", "plate_metadata.json"):
            fp = p / name
            if not fp.exists():
                continue
            try:
                d = json.loads(fp.read_text())
            except (OSError, ValueError):
                continue
            fc = d.get("fluorescence_calibration") or {}
            cal = (d.get("channel_calibrations") or fc.get("channel_calibrations")
                   or d.get("fl_calibrations") or fc.get("fl_calibrations") or {})
            if cal:
                found = {ch: (v.get("p_low"), v.get("p_high"))
                         for ch, v in cal.items()
                         if v.get("p_low") is not None
                         and v.get("p_high") is not None}
                break
        if found is not None:
            _DISPLAY_RANGE_CACHE[key] = found
            return found.get(channel)
        if p.parent == p:
            break
    return None


def _to_jpeg(path: Path, size: int, quality: int = 88) -> bytes:
    """The same display frame as `_to_png`, JPEG-encoded — ~2.9x smaller, which is what
    lets the cache hold whole wells instead of evicting them. Display only: annotations
    and measurements are stored in image coordinates, which the encoding never touches."""
    png = _to_png(path, size)
    im = Image.open(io.BytesIO(png))
    if im.mode not in ("L", "RGB"):
        im = im.convert("L")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=int(quality), optimize=True)
    return out.getvalue()


def _to_png(path: Path, size: int) -> bytes:
    """Decode a crop to an 8-bit grayscale PNG, longest edge <= ``size`` px.

    8-bit crops pass straight through. 16-bit crops are stretched with the
    PLATE's constant display range so brightness stays comparable between
    timepoints and wells; only when no calibration can be found does it fall
    back to a per-frame min-max."""
    im = Image.open(path)
    im.seek(0)
    if im.mode not in ("L", "I;16", "I", "F", "RGB", "RGBA"):
        im = im.convert("L")
    if im.mode in ("I;16", "I", "F"):
        import numpy as np
        a = np.asarray(im).astype("float32")
        rng = _display_range(path)
        if rng:
            lo, hi = float(rng[0]), float(rng[1])
        else:
            lo, hi = float(a.min()), float(a.max())
        a = (a - lo) / (hi - lo) * 255.0 if hi > lo else a * 0
        im = Image.fromarray(a.clip(0, 255).astype("uint8"), "L")
    elif im.mode in ("RGB", "RGBA"):
        im = im.convert("L")
    w, h = im.size
    scale = min(1.0, size / max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                       Image.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------ persistent frame cache
# Over a slow share, decoding each frame is the bottleneck. We keep the display-res PNG
# on local disk keyed by (source path + mtime + size), so each frame is fetched/decoded
# from the share only ONCE; after that it's local. Capped, oldest-evicted.
# A 600 px display PNG is ~183 KB — three quarters of the 243 KB source TIF, so the
# cache barely compressed anything and a 4 GB cap held only ~32 wells of ONE slice.
# It ran permanently full: every well you opened evicted the one before, so coming back
# re-read the whole trajectory from the share. JPEG at q88 is ~64 KB for the same frame
# (2.9x more frames in the same space) and this is a DISPLAY cache — measurements are
# taken in image coordinates, which JPEG does not touch. Lossless is one setting away.
_CACHE_DEFAULT_GB = 20          # the most it will ever take, however big the disk
_CACHE_TOTAL_FRACTION = 0.02    # …or this share of the WHOLE disk, whichever is smaller
_CACHE_MIN_GB = 4               # under this it cannot hold a plate; it only thrashes
_CACHE_RESERVE_GB = 10          # disk that stays free no matter what the cache wants
_CACHE_DEFAULT_QUALITY = 88
_cache_writes = [0]

# hit/miss/eviction counters, so a cache that is holding nothing useful can be SEEN
# rather than merely felt as "it got slow again". Reset when the process restarts.
_cache_stats = {"hit_ram": 0, "hit_disk": 0, "miss": 0,
                "evicted": 0, "evicted_bytes": 0, "evictions": 0}
_stats_lock = threading.Lock()


def _bump(key: str, n: int = 1):
    with _stats_lock:
        _cache_stats[key] = _cache_stats.get(key, 0) + n


def _default_cache_gb() -> float:
    """A disk budget the machine can spare, that does NOT collapse as the disk fills.

    This used to be a tenth of the FREE space, which has two bad properties. The cache's
    own files count as "used", so the budget shrank as the cache grew — it chased its own
    tail downwards. And it shrank fastest exactly when a full disk makes re-reading a
    share most painful: measured on a 96 %-full disk it had fallen to **4.1 GB**, which
    holds about twenty wells, so opening a second plate evicted the first and every frame
    went back to the network at ~28 ms apiece.

    A share of the disk's TOTAL size does not move, so the budget is stable. The reserve
    is what protects the machine: whatever the arithmetic says, `_CACHE_RESERVE_GB` stays
    free, and on a genuinely full disk the cache shrinks to nothing rather than making the
    problem worse.
    """
    try:
        du = shutil.disk_usage(Path.home())
        total, free = du.total / 1024 ** 3, du.free / 1024 ** 3
    except Exception:                                      # noqa: BLE001
        return float(_CACHE_MIN_GB)
    cap = min(float(_CACHE_DEFAULT_GB), total * _CACHE_TOTAL_FRACTION)
    cap = max(cap, float(_CACHE_MIN_GB))                   # aim for at least this…
    cap = min(cap, max(0.0, free - _CACHE_RESERVE_GB))     # …but never break the reserve
    return max(0.5, cap)


_CACHE_SETTINGS_TTL = 2.0                                  # seconds
_cache_settings_memo = {"at": 0.0, "val": None}


def _cache_settings():
    """(cap_bytes, lossless, quality) — memoised, because this is on the per-FRAME path.

    `_load_settings()` re-reads and re-parses the settings file every call, and a frame
    request asks twice. Cheap on a local disk, but it is pure waste in the hot loop.
    `_save_settings` clears the memo, so a change in Settings still takes effect at once.
    """
    now = time.monotonic()
    memo = _cache_settings_memo
    if memo["val"] is not None and now - memo["at"] < _CACHE_SETTINGS_TTL:
        return memo["val"]
    s = _load_settings()
    raw = s.get("cache_gb")
    try:
        cap = float(raw) if raw not in (None, "", 0) else _default_cache_gb()
    except (TypeError, ValueError):
        cap = _default_cache_gb()
    lossless = bool(s.get("cache_lossless"))
    try:
        q = int(s.get("cache_quality") or _CACHE_DEFAULT_QUALITY)
    except (TypeError, ValueError):
        q = _CACHE_DEFAULT_QUALITY
    val = (max(0.5, cap) * 1024 ** 3, lossless, max(50, min(100, q)))
    memo["val"], memo["at"] = val, now
    return val


def _frame_cache_dir() -> Path:
    return Path.home() / ".medaka_annotator" / "framecache"


def cache_usage() -> dict:
    """{bytes, files, cap_bytes} — what the cache is actually holding right now."""
    d = _frame_cache_dir()
    total = files = 0
    if d.is_dir():
        for f in d.rglob("*.*"):
            try:
                total += f.stat().st_size
                files += 1
            except OSError:
                pass
    cap, _lossless, _q = _cache_settings()
    try:
        free = shutil.disk_usage(_frame_cache_dir().parent).free
    except Exception:                                      # noqa: BLE001
        free = None
    with _stats_lock:
        st = dict(_cache_stats)
    served = st["hit_ram"] + st["hit_disk"] + st["miss"]
    st["served"] = served
    st["hit_rate"] = (st["hit_ram"] + st["hit_disk"]) / served if served else None
    # A cache that is full, evicting, and still missing is not caching anything: it is
    # re-reading the share for every frame while charging you the disk space. Say so,
    # because from the outside that is indistinguishable from "the app got slow again".
    st["thrashing"] = bool(st["evictions"] and served >= 200 and (st["hit_rate"] or 0) < 0.5)
    st["stalls"] = _IO["stalls"]
    st["busy_rejects"] = _IO["busy"]
    st["stalled_now"] = io_stalled()
    return {"bytes": total, "files": files, "cap_bytes": int(cap), "free_bytes": free,
            "stats": st,
            "ram": _PNG_CACHE.stats()}          # the in-memory cache is separate + small


def _cached_png(path: Path, size: int):
    """The display bytes for one frame, cached on local disk. Returns (bytes, mime).

    Named for history: it serves JPEG unless the lossless setting is on. The cache key
    carries the format+quality, so flipping that setting cannot serve you a stale image
    of the other kind."""
    import hashlib
    cap, lossless, q = _cache_settings()
    fmt = "png" if lossless else "jpg"
    mime = "image/png" if lossless else "image/jpeg"
    try:
        mt = int(path.stat().st_mtime)
    except OSError:
        mt = 0
    tag = "png" if lossless else f"q{q}"
    key = hashlib.sha1(f"{path}|{mt}|{size}|{tag}".encode()).hexdigest()
    fp = _frame_cache_dir() / key[:2] / (key + "." + fmt)
    try:
        b = fp.read_bytes()
        fp.touch()                        # LRU: freshen mtime on a hit
        _bump("hit_disk")
        return b, mime
    except OSError:
        pass
    _bump("miss")                         # this one costs a read from the image store
    b = _to_png(path, size) if lossless else _to_jpeg(path, size, q)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_suffix(".tmp")
        tmp.write_bytes(b); tmp.replace(fp)
        _cache_writes[0] += 1
        if _cache_writes[0] % 400 == 0:   # occasional background eviction
            threading.Thread(target=_evict_cache, daemon=True).start()
    except OSError:
        pass
    return b, mime


def _evict_cache(cap: int = None):
    if cap is None:
        cap, _lossless, _q = _cache_settings()
    d = _frame_cache_dir()
    if not d.is_dir():
        return
    files, total = [], 0
    for f in d.rglob("*.*"):              # both .png and .jpg live here
        try:
            st = f.stat(); files.append((st.st_mtime, st.st_size, f)); total += st.st_size
        except OSError:
            pass
    if total <= cap:
        return
    _bump("evictions")
    gone = freed = 0
    for _mt, sz, f in sorted(files):      # oldest first
        if total <= cap:
            break
        try:
            f.unlink(); total -= sz; gone += 1; freed += sz
        except OSError:
            pass
    _bump("evicted", gone)
    _bump("evicted_bytes", freed)


# ---- the image store may stop answering, and that must not stop the app -----------
# Every frame is read from wherever the plates live, and that is usually an SMB share.
# When a share stalls, macOS blocks the read in an uninterruptible syscall — there is no
# timeout and no way to cancel it. A browser opens about six connections to one host, so
# six stuck reads are enough for the whole UI to stop loading images and never recover,
# while the process sits there looking perfectly healthy.
#
# We cannot unblock a stuck syscall. We CAN refuse to spend the whole app on it: do the
# read on a bounded pool, give up waiting after `_IO_TIMEOUT_S`, and once every slot is
# occupied by a stuck read, fail immediately instead of joining the queue. The HTTP
# connection is freed either way, so the UI stays alive and says so — and when the mount
# comes back the stuck reads finish, the slots free themselves, and service resumes with
# nothing to restart.
_IO_WORKERS = 8
# 10 s is ~360x the measured median read from a healthy SMB share (27.7 ms) — far above
# any legitimately slow read, and short enough that the browser's handful of connections
# come back while you are still looking at the window rather than after you gave up.
_IO_TIMEOUT_S = 10.0            # a read this slow is a stall, not a slow disk
_IO_QUEUE_WAIT_S = 1.0          # how long to wait for a free slot before giving up
_IO = {"pool": None, "slots": threading.Semaphore(_IO_WORKERS),
       "stalled_at": 0.0, "stalls": 0, "busy": 0}
_IO_LOCK = threading.Lock()


def _io_pool():
    with _IO_LOCK:
        if _IO["pool"] is None:
            from concurrent.futures import ThreadPoolExecutor
            _IO["pool"] = ThreadPoolExecutor(max_workers=_IO_WORKERS,
                                             thread_name_prefix="store")
    return _IO["pool"]


def io_stalled() -> bool:
    """True if a read gave up recently — prefetch uses this to stop piling on."""
    return (time.monotonic() - _IO["stalled_at"]) < 30.0


def guarded_read(make, timeout: float = _IO_TIMEOUT_S):
    """Run `make()` against the image store. Returns (value, None) or (None, reason)."""
    from concurrent.futures import TimeoutError as _FTimeout
    if not _IO["slots"].acquire(timeout=_IO_QUEUE_WAIT_S):
        # every slot is held by a read that has not come back: do not add to the pile
        with _IO_LOCK:
            _IO["busy"] += 1
            _IO["stalled_at"] = time.monotonic()
        return None, "the image store is not responding"

    def run():
        try:
            return make()
        finally:
            _IO["slots"].release()        # a stuck read holds its slot, which is correct

    fut = _io_pool().submit(run)
    try:
        return fut.result(timeout=timeout), None
    except _FTimeout:
        with _IO_LOCK:
            _IO["stalls"] += 1
            _IO["stalled_at"] = time.monotonic()
        return None, f"the image store did not answer within {timeout:.0f}s"
    except Exception as e:                # noqa: BLE001 — a real read error, not a stall
        return None, f"{type(e).__name__}: {e}"


# ---- prefetch: ONE bounded, cancellable pool for the whole process ----------------
# Every well selection used to spawn its own thread with a 20-worker pool and no way to
# stop it. Clicking through a plate stacked hundreds of concurrent reads onto a share
# that is already the bottleneck — which is how a slow mount took the whole app down.
# Now: one pool, a fixed ceiling, and a generation counter that makes queued work for
# a well you have already navigated away from evaporate instead of running.
_PREFETCH_WORKERS = 12
_PREFETCH = {"gen": 0, "pool": None}
_PREFETCH_LOCK = threading.Lock()


def _prefetch_pool():
    from concurrent.futures import ThreadPoolExecutor
    if _PREFETCH["pool"] is None:
        _PREFETCH["pool"] = ThreadPoolExecutor(max_workers=_PREFETCH_WORKERS,
                                               thread_name_prefix="prefetch")
    return _PREFETCH["pool"]


def _focus_z_map(plate_id: str, well: str, tps):
    """{tp: z} the VIEWER will ask for — the `slice` keyframes forward-filled, exactly
    as the client's sliceAt() does. Prefetching the middle slice instead was a silent
    100% cache miss on every well that has focus keyframes: the frames warmed were not
    the frames requested, so playback re-read each one from the share."""
    try:
        rows = []
        if _DB["conn"] is not None:
            with _DB_LOCK:
                rows = _DB["conn"].execute(
                    'SELECT timepoint, value FROM image_annotation '
                    'WHERE plate_id=? AND well=? AND "column"=? ORDER BY timepoint',
                    (db_store.canon_plate(plate_id), well, "slice")).fetchall()
    except Exception:                                   # noqa: BLE001
        rows = []
    anchors = []
    for t, v in rows:
        try:
            anchors.append((int(t), int(str(v).strip())))
        except (TypeError, ValueError):
            pass
    if not anchors:
        return {}
    out, i, cur = {}, 0, None
    for tp in sorted(tps):
        while i < len(anchors) and anchors[i][0] <= tp:
            cur = anchors[i][1]
            i += 1
        if cur is not None:
            out[tp] = cur
    return out


def _prefetch_well(man: dict, well: str, size: int, plate_id: str = "",
                   start_tp: int = None, gen: int = 0, depth: str = "view",
                   z_window: int = 60):
    """Warm the frame cache for one well, at the depth the work actually needs.

    depth='view'  — ONE plane per timepoint: the z each frame will be displayed at.
                    This is what browsing and playing a trajectory needs, and it is
                    ~1/nz of the reads. Whole-well, ordered outward from the frame you
                    are on, so playback runs ahead of the reads instead of racing them.
    depth='stack' — additionally every z-slice within `z_window` timepoints of where you
                    are, for when you start setting focus keyframes and are stepping
                    through z rather than through time.

    Both are the same bounded pool and the same generation check, so escalating to
    'stack' cannot pile up on top of the 'view' pass — the newer request supersedes it.
    """
    entry = (man.get("_well_index") or {}).get(well)
    if not entry:
        return
    chans = man.get("channels", [])
    det = man.get("detect_channel") or (chans[0] if chans else None)
    order = ([det] if det else []) + [c for c in chans if c != det]
    jobs = []
    for ch in order[:2]:
        tps = list((man.get("frames", {}).get(well, {}) or {}).get(ch, []))
        zmap = _focus_z_map(plate_id, well, tps) if plate_id else {}
        if start_tp is not None and tps:                # nearest-first: play order
            tps.sort(key=lambda t: (abs(t - start_tp), t))
        for tp in tps:
            fp = _frame_path(man, well, ch, tp, zmap.get(tp))
            if fp is not None:
                jobs.append(fp)
        if depth == "stack":
            zs = (man.get("channel_z", {}) or {}).get(ch) or []
            near = [t for t in tps
                    if start_tp is None or abs(t - start_tp) <= z_window]
            for tp in near:                             # the z-stack around where you are
                for z in zs:
                    if z == zmap.get(tp):
                        continue                        # already queued by the view pass
                    fp = _frame_path(man, well, ch, tp, z)
                    if fp is not None:
                        jobs.append(fp)

    def one(fp):
        if gen != _PREFETCH["gen"]:                     # a newer well was selected
            return
        if io_stalled():                                # the share is already in trouble:
            return                                      # do not queue more work onto it
        try:
            _cached_png(fp, size)          # warms disk; bytes discarded
        except OSError:                                 # share vanished mid-read: the
            pass                                        # UI must not care, and must not stall
    pool = _prefetch_pool()
    for fp in jobs:
        if gen != _PREFETCH["gen"] or io_stalled():
            break                                       # guessing ahead is never worth
        try:                                            # making a stalled share worse
            pool.submit(one, fp)
        except RuntimeError:                            # pool shutting down
            break


# ------------------------------------------------------------------ plate cache
_MANIFEST_CACHE: dict = {}
_MANIFEST_LOCK = threading.Lock()


def _resolve_dir(data_root: Path, dir_arg: str) -> Path:
    """A plate 'dir' may be an absolute path, a folder name under data_root, or a
    PREFIX of one (first match wins) — matching screen.py's convenience."""
    if not dir_arg:
        raise FileNotFoundError("no plate given")
    p = Path(dir_arg)
    if p.is_dir():
        return p.resolve()
    cand = data_root / dir_arg
    if cand.is_dir():
        return cand.resolve()
    matches = sorted(d for d in data_root.iterdir()
                     if d.is_dir() and d.name.startswith(dir_arg))
    if matches:
        return matches[0].resolve()
    raise FileNotFoundError(f"no plate folder matching {dir_arg!r} under {data_root}")


def _manifest(data_root: Path, dir_arg: str) -> dict:
    plate_dir = _resolve_dir(data_root, dir_arg)
    key = str(plate_dir)
    with _MANIFEST_LOCK:
        if key in _MANIFEST_CACHE:
            return _MANIFEST_CACHE[key]
    man = model.discover_plate(plate_dir)
    with _MANIFEST_LOCK:
        _MANIFEST_CACHE[key] = man
    return man


def _frame_path(man: dict, well: str, ch: str, tp: int, z: int = None):
    entry = man["_well_index"].get(well)
    if not entry:
        return None
    # a specific z-slice of a per-z channel (old BF → 'bf_z'; new per-channel → '<ch>_z')
    zk = "bf_z" if ch == "BF" else ch + "_z"
    zdk = "bf_z_dir" if ch == "BF" else ch + "_z_dir"
    if z is not None and zk in entry:
        base = (entry.get(zdk) or {}).get(z)
        frames = (entry.get(zk) or {}).get(z, {})
        fname = frames.get(tp) or (frames[_nearest_tp(frames, tp)] if frames else None)
        if base and fname:
            return Path(base) / fname
        # requested z absent → fall through to the flat/middle default below
    frames = entry.get(ch, {})
    fname = frames.get(tp)
    if fname is None and frames:                   # clamp to the nearest available frame
        fname = frames[_nearest_tp(frames, tp)]
    if not fname:
        return None
    dir_key = {"BF": "bf_dir", "FL": "fl_dir", "IMG": "img_dir"}.get(ch, ch + "_dir")
    base = entry.get(dir_key)
    return Path(base) / fname if base else None


def _nearest_tp(frames: dict, tp: int) -> int:
    return min(frames, key=lambda t: abs(t - tp))


def _screening(plate_dir: Path) -> Path:
    """Canonical screening JSON path: <plate>/metadata/screening_<plate>.json, with a
    fallback to a legacy plate-root copy. Returns a Path (may not exist — used for saving)."""
    cands = (sorted((plate_dir / "metadata").glob("screening_*.json"))
             or sorted(plate_dir.glob("screening_*.json")))
    return cands[0] if cands else plate_dir / "metadata" / f"screening_{plate_dir.name}.json"


def _list_plates(data_root: Path) -> list:
    """Folders under data_root that look like a processed plate (or hold crops)."""
    out = []
    if not data_root.is_dir():
        return out
    for d in sorted(data_root.iterdir()):
        if not d.is_dir():
            continue
        # old bf/ + crops/ + screening/ plates, OR a new per-channel plate (its
        # plate_metadata.json at the root distinguishes it from a lone channel folder).
        if ((d / "bf").is_dir() or (d / "crops").is_dir()
                or (d / "screening").is_dir()
                or (d / "plate_metadata.json").exists()
                or _screening(d).exists()):
            out.append({"dir": d.name, "annotated": _screening(d).exists()})
    return out


def _short_id(name: str) -> str:
    m = re.search(r"AQV\d+|[Vv]\d+", name)
    return m.group(0) if m else name.split("_")[0]


def _plate_ntps(pd: Path) -> int:
    """Timepoint count for a plate, from the first well's first z-slice — old bf/ OR
    the new per-channel layout (falls back to the first channel folder)."""
    root = pd / "bf"
    if not root.is_dir():
        cdirs = model._channel_dirs(pd)
        root = cdirs[0][1] if cdirs else None
    if root is None or not root.is_dir():
        return 0
    for wd in sorted(root.glob("*")):
        if not wd.is_dir():
            continue
        sls = sorted(wd.glob("SL*"))
        n = sum(1 for _ in (sls[0].glob("*.tif") if sls else wd.glob("*.tif")))
        if n:
            return n
    return 0


def _measure_summary() -> dict:
    """{canonical plate_id: {well: {name: {n,min,max,mean,first,last}}}} over the whole DB.

    Summarised server-side (one query, a few hundred rows) so the filter can ask
    "egg_diameter above 1400 µm at EVERY annotated timepoint" without shipping raw
    measurements. `n` is the count of ANNOTATED timepoints — most wells have exactly
    one (a size is usually measured once), which is why 'all' must not mean 'needs
    many'; the filter surfaces `n` so a one-off measurement still counts."""
    if _DB["conn"] is None:
        return {}
    try:
        with _DB_LOCK:
            rows = _DB["conn"].execute(
                'SELECT plate_id, well, name, timepoint, length_um, length_px '
                'FROM measurement ORDER BY plate_id, well, name, timepoint').fetchall()
    except Exception:                                    # noqa: BLE001
        return {}
    acc = {}
    for plate, well, name, _tp, um, px in rows:
        v = um if um not in (None, 0) else px
        if v is None:
            continue
        acc.setdefault(plate, {}).setdefault(well, {}).setdefault(name, []).append(float(v))
    out = {}
    for plate, wells in acc.items():
        for well, names in wells.items():
            for name, vals in names.items():
                out.setdefault(plate, {}).setdefault(well, {})[name] = {
                    "n": len(vals), "min": min(vals), "max": max(vals),
                    "mean": sum(vals) / len(vals), "first": vals[0], "last": vals[-1],
                    # the raw values too (a well has 1–2, so this stays tiny) — 'at every
                    # timepoint' and 'between' can then be evaluated exactly, not from stats
                    "vals": [round(v, 2) for v in vals[:64]]}
    return out


def _wells_all(data_root: Path) -> dict:
    """Every well across every plate, with its well-level annotations + count, plus
    the UNION of well columns/values — so the client can filter across plates
    (e.g. injected?=Yes AND line=pfkfb3 AND viability=alive) and sort by how many
    annotations each well has."""
    plates, wells = [], []
    colvals, coltypes = {}, {}
    meas = _measure_summary()
    for pd in sorted(Path(data_root).iterdir()):
        if not pd.is_dir():
            continue
        # same plate signals as _list_plates (old bf/crops/screening OR new per-channel)
        if not ((pd / "bf").is_dir() or (pd / "crops").is_dir() or (pd / "screening").is_dir()
                or (pd / "plate_metadata.json").exists() or _screening(pd).exists()):
            continue
        short = _short_id(pd.name)
        # DB-FIRST: annotations live in medaka.db, not the per-plate screening JSON. Load
        # this plate's well-scope columns/annotations from the DB (legacy JSON fallback).
        payload = None
        if _DB["conn"] is not None:
            try:
                with _DB_LOCK:
                    payload = db_store.load_payload(_DB["conn"], pd.name)
            except Exception:               # noqa: BLE001
                payload = None
        if payload is None:
            _s = _screening(pd)
            if _s.exists():
                try:
                    payload = json.load(open(_s))
                except (OSError, json.JSONDecodeError):
                    payload = None
        cols = (payload or {}).get("columns", {}) or {}       # WELL-scope columns (global registry)
        ann = (payload or {}).get("annotations", {}) or {}    # {well: {col: value}}
        for cn, spec in cols.items():
            coltypes.setdefault(cn, (spec or {}).get("type", "categorical"))
            s = colvals.setdefault(cn, set())
            for v in ((spec or {}).get("values") or []):
                s.add(v)
        n_tps = _plate_ntps(pd)          # lets the filter grid's frame-fader map to a real tp
        pmeas = meas.get(db_store.canon_plate(pd.name), {})
        # a well with measurements but no annotations must still be filterable
        keys = list(dict.fromkeys(list(ann.keys()) + list(pmeas.keys())))
        plates.append({"dir": pd.name, "short": short, "n_wells": len(ann), "n_tps": n_tps,
                       "n_measured": len(pmeas)})
        for well in keys:
            e = ann.get(well, {})
            for c, v in e.items():
                if not isinstance(v, (list, tuple)):
                    colvals.setdefault(c, set()).add(v)
            w = {"plate": pd.name, "short": short, "well": well,
                 "ann": e, "nann": len(e), "n_tps": n_tps}
            if pmeas.get(well):
                w["meas"] = pmeas[well]
            wells.append(w)
    columns = {cn: {"type": coltypes.get(cn, "categorical"),
                    "values": sorted(colvals.get(cn, []), key=str)} for cn in colvals}
    mnames = sorted({n for p in meas.values() for w in p.values() for n in w})
    return {"plates": plates, "columns": columns, "wells": wells,
            "measurements": mnames, "filters": _load_settings().get("filters", {})}


def _version_info(fetch: bool = False) -> dict:
    """What version this is, and whether the checkout it runs from is behind its remote.
    `fetch=1` contacts the remote (a couple of seconds); the default is free and answers
    from the last fetch, which run.sh refreshes on every launch."""
    st = version.git_state(fetch=fetch)
    info = {"version": version.version(), "git": st,
            "behind": st.get("behind", 0), "update_available": bool(st.get("behind", 0))}
    # No checkout (a packaged app) → ask GitHub for the newest release instead, but only
    # on an explicit check. `git` stays empty so the client knows it can't self-update.
    if fetch and not st:
        rel = version.latest_release()
        if rel:
            info["release"] = rel
            info["update_available"] = bool(rel.get("newer"))
    return info


def _load_config(data_root: Path) -> dict:
    stages = _read_json(APP_DIR / "iwamatsu_stages.json", {})
    defaults = _read_json(APP_DIR / "defaults.json", {})
    # fold the iwamatsu stage values into the image-scope seed column
    stage_vals = [s.get("value") for s in stages.get("stages", []) if s.get("value")]
    try:
        img_cols = defaults.setdefault("image", {}).setdefault("columns", {})
        if "iwamatsu_stage" in img_cols and not img_cols["iwamatsu_stage"].get("values"):
            img_cols["iwamatsu_stage"]["values"] = stage_vals
    except AttributeError:
        pass
    return {
        "data_root": str(data_root),
        "version": version.version(),
        "plates": _list_plates(data_root),
        "iwamatsu_stages": stages,
        "defaults": defaults,
        "suggestions": model.registry_suggestions(),
        "column_types": list(model.COLUMN_TYPES),
        # DB-first status so the UI can prompt for a name when there's no DB yet
        "db_path": str(_DB["path"]) if _DB["path"] else None,
        "needs_db": _DB["needs_db"],
    }


def _read_json(path: Path, fallback):
    try:
        return json.load(open(path))
    except (OSError, json.JSONDecodeError):
        return fallback


def _client_manifest(man: dict, plate_name: str, annotator: str = None) -> dict:
    """The plate response: manifest minus the server-only _well_index, plus the
    plate's v3 payload loaded FROM the DB (not per-plate JSON)."""
    out = {k: v for k, v in man.items() if not k.startswith("_")}
    out["payload"] = _db_load_payload(plate_name, annotator)
    out["db_path"] = str(_DB["path"]) if _DB["path"] else None
    out["needs_db"] = _DB["needs_db"]
    return out


# ------------------------------------------------------------------ HTTP handler
class Handler(BaseHTTPRequestHandler):
    data_root = DEFAULT_DATA_ROOT
    server_version = "PlateNotate/" + version.version()

    def log_message(self, fmt, *args):            # quieter console
        """A log line must never be able to fail a request.

        `send_response` calls this BEFORE writing a single byte, so anything raised here
        closes the connection with no response at all — the browser gets
        `RemoteDisconnected` and every page of the app fails at once, with the traceback
        going to the same missing stream that caused it. `self.path` also does not exist
        yet when the request line itself was malformed.
        """
        try:
            if "/api/frame" not in self.path:
                sys.stderr.write("  %s\n" % (fmt % args))
        except Exception:                         # noqa: BLE001
            pass

    # -- helpers ------------------------------------------------------------
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, msg, extra=None):
        self._send(code, {"error": msg}, extra=extra)

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._serve_static("index.html")
            if u.path.startswith("/static/"):
                return self._serve_static(u.path[len("/static/"):])
            if u.path == "/api/cache":
                return self._send(200, cache_usage())
            if u.path == "/api/version":
                return self._send(200, _version_info(q.get("fetch", ["0"])[0] == "1"))
            if u.path == "/api/config":
                cfg = _load_config(self.data_root)
                cfg["db"] = _db_info()
                cfg["annotators"] = db_store.list_annotators(_DB["conn"]) if _DB["conn"] else []
                cfg["settings"] = _load_settings()
                return self._send(200, cfg)
            if u.path == "/api/settings":
                return self._send(200, _load_settings())
            if u.path == "/api/connections":
                return self._api_connections()
            if u.path == "/api/plate":
                return self._api_plate(q)
            if u.path == "/api/wells_all":
                return self._send(200, _wells_all(self.data_root))
            if u.path == "/api/prefetch":
                return self._api_prefetch(q)
            if u.path == "/api/frame":
                return self._api_frame(q)
            if u.path == "/api/export-json":
                return self._api_export_json(q)
            if u.path == "/api/db-status":
                return self._api_db_status()
            if u.path == "/api/export-status":
                return self._api_export_status(q)
            if u.path == "/api/export-jobs":
                return self._api_export_jobs()
            if u.path == "/api/export-download":
                return self._api_export_download(q)
            return self._err(404, f"no route {u.path}")
        except FileNotFoundError as e:
            return self._err(404, str(e))
        except BrokenPipeError:
            return                                  # client navigated away mid-image
        except Exception as e:                      # never crash the server on one bad request
            return self._err(500, f"{type(e).__name__}: {e}")

    def _serve_static(self, rel):
        rel = rel.split("?")[0].lstrip("/")
        if ".." in rel:
            return self._err(400, "bad path")
        # index.html sits in APP_DIR; the rest under static/
        path = (APP_DIR / rel) if rel == "index.html" else (APP_DIR / "static" / rel)
        if not path.is_file():
            return self._err(404, f"no file {rel}")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(path.suffix, "application/octet-stream")
        # no-store on the code so edits always load (avoids stale JS); the logo is
        # immutable per build, so let the browser keep it and stop re-fetching it.
        cache = "public, max-age=86400" if path.suffix in (".png", ".svg", ".ico") \
            else "no-store, max-age=0"
        self._send(200, path.read_bytes(), ctype, extra={"Cache-Control": cache})

    def _api_plate(self, q):
        dir_arg = (q.get("dir") or [""])[0]
        who = (q.get("annotator") or [""])[0]         # isolate to this annotator's rows
        man = _manifest(self.data_root, dir_arg)
        plate_name = Path(man["plate_dir"]).name
        self._send(200, _client_manifest(man, plate_name, who))

    def _api_prefetch(self, q):
        """Warm the frame cache for a well's frames in the background (fire-and-forget),
        so scrubbing a share is fast. The client calls this when a well is selected."""
        dir_arg = (q.get("dir") or [""])[0]
        well = (q.get("well") or [""])[0]
        try:
            size = max(16, min(1024, int((q.get("size") or ["600"])[0])))
        except ValueError:
            size = 600
        try:
            start_tp = int((q.get("tp") or ["0"])[0]) or None
        except ValueError:
            start_tp = None
        depth = (q.get("depth") or ["view"])[0]
        if depth not in ("view", "stack"):
            depth = "view"
        if well:
            man = _manifest(self.data_root, dir_arg)
            with _PREFETCH_LOCK:
                _PREFETCH["gen"] += 1                    # everything queued before this dies
                gen = _PREFETCH["gen"]
            plate = Path(man["plate_dir"]).name
            threading.Thread(target=_prefetch_well,
                             args=(man, well, size, plate, start_tp, gen, depth),
                             daemon=True).start()
        self._send(200, {"ok": True})

    def _api_frame(self, q):
        dir_arg = (q.get("dir") or [""])[0]
        well = (q.get("well") or [""])[0]
        ch = (q.get("ch") or ["BF"])[0]
        try:
            tp = int((q.get("tp") or ["1"])[0])
            size = max(16, min(1024, int((q.get("size") or ["560"])[0])))
        except ValueError:
            return self._err(400, "tp/size must be integers")
        zq = (q.get("z") or [""])[0]
        z = int(zq) if zq.isdigit() else None       # a chosen BF z-slice, or None = middle
        man = _manifest(self.data_root, dir_arg)
        fp = _frame_path(man, well, ch, tp, z)
        if fp is None and z is not None:            # fall back to the middle slice
            fp = _frame_path(man, well, ch, tp)
        if fp is None:
            return self._err(404, f"no frame {well}/{ch}/{tp}")
        key = (str(fp), size, _cache_settings()[1:])
        hit = _PNG_CACHE.peek(key)
        if hit is not None:                         # already decoded: never touch the store
            _bump("hit_ram")
            body, mime = hit
            return self._send(200, body, mime,
                              extra={"Cache-Control": "public, max-age=86400"})
        # Everything below can touch the share, INCLUDING the is_file() probe — a stalled
        # mount blocks that too — so the whole lot goes through the guard.
        def read():
            if not fp.is_file():
                return None
            return _PNG_CACHE.get_or(key, lambda: _cached_png(fp, size))

        got, why = guarded_read(read)
        if why:
            # 503, not a hang. The browser frees the connection and the UI keeps working;
            # Retry-After stops it hammering a share that is already in trouble.
            return self._err(503, why, extra={"Retry-After": "5"})
        if got is None:
            return self._err(404, f"no frame {well}/{ch}/{tp}")
        body, mime = got
        self._send(200, body, mime, extra={"Cache-Control": "public, max-age=86400"})

    # -- export (TIF / MP4) -------------------------------------------------
    SMB_PROCESSED = "/Volumes/aulehla/Tiago/AQ-EMBL/PROCESSED"   # SMB fallback for crops

    def _api_export(self):
        import export
        try:
            spec = self._read_json_body()
        except json.JSONDecodeError:
            return self._err(400, "invalid JSON body")
        if not spec.get("wells"):
            return self._err(400, "no wells selected")
        spec["export_dir"] = (_load_settings().get("export_dir") or "").strip()   # Settings override
        jid = export.start(spec, self.data_root, self.SMB_PROCESSED)
        self._send(200, {"job_id": jid})

    def _api_export_status(self, q):
        import export
        self._send(200, export.status((q.get("id") or [""])[0]))

    def _api_export_jobs(self):
        import export
        self._send(200, {"jobs": export.all_jobs()})

    def _api_export_download(self, q):
        import export
        p = export.output_path((q.get("id") or [""])[0])
        if not p or not p.is_file():
            return self._err(404, "no export file (still running or unknown id?)")
        ctype = ("application/zip" if p.suffix == ".zip"
                 else "image/tiff" if p.suffix in (".tif", ".tiff") else "video/mp4")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
        self.end_headers()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                self.wfile.write(chunk)

    def _api_open_folder(self):
        """Switch the whole app to a different data folder (e.g. an SMB mount of the
        server) at runtime — detect/open its medaka.db and return the new config."""
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            return self._err(400, "invalid JSON body")
        raw = (body.get("path") or "").strip()
        if not raw:
            return self._err(400, "no path given")
        path = Path(raw).expanduser()
        if not path.is_dir():
            return self._err(400, f"not a folder: {path}")
        Handler.data_root = path
        _save_settings({"last_data_root": str(path)})  # reopen here next launch
        _open_process_db(path)                        # resolve the DB (Settings/registry/detect)
        active = _DB.get("path")
        if active:                                    # remember this folder feeds the DB
            db_store.record_data_root(active, path)
        # Surface a DIFFERENT database sitting in the opened folder that we are NOT using —
        # so its annotations can't be silently bypassed. Non-blocking hint for the UI.
        foreign = None
        own = db_store.detect_db(path) or db_store.detect_db(path.parent)
        if own and active and _norm(own) != _norm(active):
            n = db_store.annotation_count(own)
            if n > 0:
                foreign = {"path": str(own), "count": n}
        self._send(200, {"ok": True, "data_root": str(path),
                         "config": _load_config(path), "db": _db_info(),
                         "foreign_db": foreign})

    def _api_merge_folder(self):
        """Merge a folder's OWN database into the active one (non-destructive, latest-wins),
        then link the folder so it uses the active DB from now on."""
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            return self._err(400, "invalid JSON body")
        src = (body.get("db") or "").strip()
        active = _DB.get("path")
        if not src or not Path(src).is_file():
            return self._err(400, "no source database")
        if not active:
            return self._err(400, "no active database to merge into")
        if _norm(src) == _norm(active):
            return self._err(400, "source and active database are the same")
        with _DB_LOCK:
            counts = db_store.merge_db(src, active)
        folder = (body.get("path") or "").strip()
        if folder:
            link_root(folder, active)                 # from now on this folder uses the active DB
        return self._send(200, {"ok": True, "merged": counts, "db": _db_info()})

    def _api_connections(self):
        """What connects to the active database: the folders it collects from (recorded IN
        the DB) and the app-level registry links. Powers the Settings 'Connected folders'."""
        active = _DB.get("path")
        roots = db_store.list_data_roots(active) if active else []
        return self._send(200, {
            "active_db": str(active) if active else None,
            "annotation_count": db_store.annotation_count(active) if active else 0,
            "data_roots": roots,
            "registry": _load_registry(),
        })

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/save":
                return self._api_save()
            if u.path == "/api/create-db":
                return self._api_create_db()
            if u.path == "/api/export":
                return self._api_export()
            if u.path == "/api/open-folder":
                return self._api_open_folder()
            if u.path == "/api/merge-folder":
                return self._api_merge_folder()
            if u.path == "/api/update":
                return self._send(200, dict(version.pull(), **_version_info()))
            if u.path == "/api/settings":
                return self._api_settings()
            if u.path == "/api/pick-folder":
                return self._api_pick_folder()
            return self._err(404, f"no route {u.path}")
        except Exception as e:
            return self._err(500, f"{type(e).__name__}: {e}")

    def _api_settings(self):
        """Update settings.

        Changing the annotations folder no longer COPIES the current database there.
        It used to, so that choosing a folder could never silently start an empty DB and
        hide your existing annotations — but the cure was worse: picking any folder
        wrote your entire database into it. Pointing the app at a colleague's share put
        a 355 MB copy of every plate on their server folder, silently.

        Instead the client is TOLD what the new folder holds — an existing database, or
        none — and copying is an explicit action (`copy_db`) the user asks for.
        """
        try:
            patch = self._read_json_body()
        except json.JSONDecodeError:
            return self._err(400, "invalid JSON body")
        before = (_load_settings().get("annotations_dir") or "").strip()
        cur_db = _DB.get("path")                        # the DB currently in use (has the data)
        want_copy = bool(patch.pop("copy_db", False))   # explicit "bring my annotations"
        s = _save_settings(patch)
        after = (s.get("annotations_dir") or "").strip()
        moved, found = None, None
        if after != before and after:
            dest = Path(after).expanduser()
            existing = db_store.detect_db(dest) if dest.is_dir() else None
            found = str(existing) if existing else None
            if want_copy and existing is None and cur_db and Path(cur_db).is_file():
                try:
                    dest.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy2(cur_db, dest / "medaka.db")
                    moved = str(dest / "medaka.db")
                except Exception as e:                  # noqa: BLE001
                    print(f"  ! could not copy the DB to {after} ({e})", file=sys.stderr)
        if after != before:
            _open_process_db(self.data_root)
        self._send(200, {"ok": True, "settings": s, "db": _db_info(),
                         "db_moved_to": moved, "existing_db": found,
                         # what the client should say: a fresh folder starts EMPTY now
                         "will_create_new": bool(after and after != before and not found
                                                 and not moved)})

    def _api_pick_folder(self):
        """Native folder picker (desktop app only). The pywebview bridge normally handles
        this in-page; this is a fallback the frontend can call when the bridge is absent."""
        self._send(200, {"path": None, "note": "use the desktop app's native picker or type a path"})

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _api_save(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            return self._err(400, "invalid JSON body")
        dir_arg = body.get("dir") or ""
        payload = body.get("payload") or {}
        man = _manifest(self.data_root, dir_arg)
        plate_name = Path(man["plate_dir"]).name
        if _DB["conn"] is None:                        # auto-create on first save (belt & braces)
            _open_process_db(self.data_root)
        if _DB["conn"] is None:
            return self._err(409, "could not create a database here — the folder is read-only "
                                  "or full. Open a writable/local folder and try again.")
        # Persist to medaka.db (this annotator's rows) and reload ONLY this annotator's
        # rows — each person sees and edits their own work, never another's.
        who = payload.get("annotator") or "tiago"
        with _DB_LOCK:
            clean = db_store.save_payload(_DB["conn"], plate_name, payload,
                                          default_annotator="tiago")
            reloaded = db_store.load_payload(_DB["conn"], plate_name,
                                             clean.get("annotator") or who)
        reloaded["annotator"] = clean.get("annotator") or reloaded.get("annotator", "")
        side = _write_side_exports(plate_name)         # CSV / JSON copies per Settings
        self._send(200, {"ok": True, "db_path": str(_DB["path"]),
                         "updated": clean["updated"], "payload": reloaded, "side": side})

    def _api_export_json(self, q):
        """The plate's v3 payload as a downloadable JSON (portability export)."""
        dir_arg = (q.get("dir") or [""])[0]
        man = _manifest(self.data_root, dir_arg)
        plate_name = Path(man["plate_dir"]).name
        if _DB["conn"] is None:
            return self._err(409, "no database yet — nothing to export")
        with _DB_LOCK:
            payload = db_store.export_json(_DB["conn"], plate_name)
        body = json.dumps(payload, indent=2).encode()
        self._send(200, body, "application/json", extra={
            "Content-Disposition": f'attachment; filename="screening_{plate_name}.json"'})

    def _api_db_status(self):
        self._send(200, _db_info())

    def _api_create_db(self):
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            return self._err(400, "invalid JSON body")
        name = (body.get("name") or "").strip() or "medaka"
        folder = _DB["folder"] or self.data_root
        path = db_store.create_db(folder, name)
        with _DB_LOCK:
            if _DB["conn"] is not None:
                try:
                    _DB["conn"].close()
                except Exception:
                    pass
            _DB["conn"] = db_store.open_db(path, check_same_thread=False)
            _DB["path"], _DB["needs_db"] = path, False
        self._send(200, {"ok": True, "db_path": str(path), "needs_db": False})


def _serve(host, port, tries=20):
    """Bind the first free port at or after ``port`` (so a leftover server on the
    default port never blocks a fresh launch). Returns (httpd, actual_port)."""
    for p in range(port, port + tries):
        try:
            srv = ThreadingHTTPServer((host, p), Handler)
            return srv, srv.server_address[1]     # ask the SOCKET, not the loop: port 0
        except OSError as e:                      # means "any free port", and only the
            #                                       socket knows which one it got.
            if e.errno == errno.EADDRINUSE:
                continue
            raise
    sys.exit(f"no free port in {port}..{port + tries - 1} — free one or pass --port")


def selftest() -> bool:
    """Boot the real server on a scratch folder, fetch the pages a browser needs, and
    say whether the build is alive. This is what CI runs against the FROZEN app: a
    bundle that compiles but dies on launch is worse than no bundle, and only running
    the shipped binary catches a missing data file or a broken hidden import.

    Every line goes to STDERR and to a log file, never to stdout: a windowed Windows
    build has ``sys.stdout is None``, and CPython's ``print`` silently discards output
    in that case — so a stdout-only report would leave a failing build undiagnosable.
    """
    import shutil
    import tempfile
    import traceback
    import urllib.request

    log = Path("platenotate-selftest.log")
    try:
        log.write_text("")                              # start clean
    except OSError:
        pass

    def say(msg):
        """Append EVERY line the moment it happens. A check that hangs (a frozen import
        that stalls, a library that goes looking on the network) leaves a log ending at
        the exact step it died on — a report written only at the end would be empty,
        which is precisely how the first Windows failure hid itself."""
        try:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except Exception:                               # noqa: BLE001 — no stderr either
            pass
        try:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
                fh.flush()
        except OSError:
            pass

    ok = True
    say(f"PlateNotate selftest — v{version.version()}  frozen={FROZEN}  {sys.platform}")
    # mkdtemp + ignore_errors, NOT TemporaryDirectory: on Windows the scratch folder
    # still holds the open SQLite file, and a cleanup that raises inside a WINDOWED
    # build opens a PyInstaller crash dialog — which on a headless runner waits for a
    # click that never comes. Teardown here must be incapable of raising.
    tmp = tempfile.mkdtemp(prefix="platenotate-selftest-")
    httpd = None
    try:
        Handler.data_root = Path(tmp)
        try:
            _open_process_db(Handler.data_root)
            httpd, port = _serve("127.0.0.1", 0)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
        except Exception:                               # noqa: BLE001
            say("FAIL server did not start:\n" + traceback.format_exc())
            return False
        base = f"http://127.0.0.1:{port}"
        checks = [("/", b"PlateNotate"), ("/static/app.js", b"AnnotatorAPI"),
                  ("/static/style.css", b"--accent"), ("/static/favicon.png", b"PNG"),
                  ("/api/config", b"data_root"), ("/api/version", b"version")]
        for path, needle in checks:
            try:
                with urllib.request.urlopen(base + path, timeout=20) as r:
                    body = r.read()                     # `with`: release the socket, so
                if needle in body:                      # no handler thread lingers
                    say(f"  ok   {path}  ({len(body)} bytes)")
                else:
                    ok = False
                    say(f"  FAIL {path}: served {len(body)} bytes without {needle!r}")
            except Exception as e:                      # noqa: BLE001
                ok = False
                say(f"  FAIL {path}: {type(e).__name__}: {e}")
        # the export engine is imported lazily at runtime, so prove it loads in the
        # bundle too — a missing hiddenimport only shows up when someone hits Export
        for mod in ("export", "compose", "well_hyperstack", "focus_cut", "annotations",
                    "build_db", "imagecodecs", "tifffile", "imageio_ffmpeg"):
            try:
                __import__(mod)
                say(f"  ok   import {mod}")
            except Exception:                           # noqa: BLE001
                ok = False
                say(f"  FAIL import {mod}:\n" + traceback.format_exc())
        try:
            import well_hyperstack as _wh
            ff = _wh.ffmpeg_exe()
            here = Path(ff).exists()
            say(f"  {'ok  ' if here else 'FAIL'} ffmpeg: {ff}")
            ok = ok and here
        except Exception:                               # noqa: BLE001
            ok = False
            say("  FAIL ffmpeg lookup:\n" + traceback.format_exc())
    except Exception:                                   # noqa: BLE001 — never a dialog
        ok = False
        say("FAIL unexpected:\n" + traceback.format_exc())
    finally:
        say("  …tearing down")
        for step, fn in (("http shutdown", lambda: httpd and httpd.shutdown()),
                         ("db close", lambda: _DB["conn"] and _DB["conn"].close()),
                         ("tmp cleanup", lambda: shutil.rmtree(tmp, ignore_errors=True))):
            try:
                fn()
            except Exception as e:                      # noqa: BLE001
                say(f"  (ignored during {step}: {type(e).__name__}: {e})")
    say(("selftest: PASS v" if ok else "selftest: FAIL v") + version.version())
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="Medaka 3-level image annotator (web).")
    ap.add_argument("plate", nargs="?", default="",
                    help="plate folder name/prefix to open focused (optional)")
    ap.add_argument("--data-root", default=None,
                    help="root holding plate folders (default: the folder last opened)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't auto-open a browser")
    ap.add_argument("--selftest", action="store_true",
                    help="boot, fetch the UI and exit 0/1 — the packaged build's smoke test")
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() else 1
    Handler.data_root = Path(args.data_root or default_data_root()).resolve()
    _open_process_db(Handler.data_root)          # DB-first: one WAL conn for the process
    httpd, port = _serve(args.host, args.port)
    if port != args.port:
        print(f"  (port {args.port} was busy — using {port})")
    focus = ""
    if args.plate:
        try:
            focus = "?plate=" + _resolve_dir(Handler.data_root, args.plate).name
        except FileNotFoundError:
            print(f"(note: no plate matched {args.plate!r}; opening the picker)")
    url = f"http://{args.host}:{port}/{focus}"
    print(f"\n  Medaka annotator  →  {url}")
    print(f"  data root: {Handler.data_root}")
    if _DB["path"]:
        print(f"  database:  {_DB['path']}  (DB-first)")
    else:
        print(f"  database:  none yet in {_DB['folder']} — the UI will prompt to create one")
    print("  Ctrl-C to stop.\n", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
