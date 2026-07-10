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
import re
import sys
import errno
import threading
import argparse
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import model

APP_DIR = Path(__file__).resolve().parent
# plates live under imaging/data/AQ-EMBL/ (server.py is at imaging/tools/label_annotator/)
DEFAULT_DATA_ROOT = APP_DIR.parents[1] / "data" / "AQ-EMBL"

# ------------------------------------------------------------------ image decode
try:
    from PIL import Image
except ImportError:                      # pragma: no cover - clearer error
    sys.exit("Pillow is required: `conda activate twinnet` (or `pip install pillow`).")


class _LRU(OrderedDict):
    """A tiny thread-safe LRU byte cache for decoded PNGs."""
    def __init__(self, cap=1200):
        super().__init__()
        self.cap = cap
        self.lock = threading.Lock()

    def get_or(self, key, make):
        with self.lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
        val = make()                     # decode outside the lock
        with self.lock:
            self[key] = val
            self.move_to_end(key)
            while len(self) > self.cap:
                self.popitem(last=False)
        return val


_PNG_CACHE = _LRU(cap=1500)


def _to_png(path: Path, size: int) -> bytes:
    """Decode a crop to an 8-bit grayscale PNG, longest edge <= ``size`` px.

    v2 crops are already 8-bit; the 16-bit branch (min-max stretch) keeps the
    tool usable on arbitrary scientific TIFFs in the general/flat case."""
    im = Image.open(path)
    im.seek(0)
    if im.mode not in ("L", "I;16", "I", "F", "RGB", "RGBA"):
        im = im.convert("L")
    if im.mode in ("I;16", "I", "F"):
        import numpy as np
        a = np.asarray(im).astype("float32")
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
    if ch == "BF" and z is not None:               # a specific brightfield z-slice
        base = (entry.get("bf_z_dir") or {}).get(z)
        frames = (entry.get("bf_z") or {}).get(z, {})
        fname = frames.get(tp) or (frames[_nearest_tp(frames, tp)] if frames else None)
        return Path(base) / fname if (base and fname) else None
    frames = entry.get(ch, {})
    fname = frames.get(tp)
    if fname is None and frames:                   # clamp to the nearest available frame
        fname = frames[_nearest_tp(frames, tp)]
    if not fname:
        return None
    dir_key = {"BF": "bf_dir", "FL": "fl_dir", "IMG": "img_dir"}.get(ch, "bf_dir")
    base = entry.get(dir_key)
    return Path(base) / fname if base else None


def _nearest_tp(frames: dict, tp: int) -> int:
    return min(frames, key=lambda t: abs(t - tp))


def _list_plates(data_root: Path) -> list:
    """Folders under data_root that look like a processed plate (or hold crops)."""
    out = []
    if not data_root.is_dir():
        return out
    for d in sorted(data_root.iterdir()):
        if not d.is_dir():
            continue
        if ((d / "bf").is_dir() or (d / "crops").is_dir()
                or (d / "screening").is_dir()
                or any(d.glob("screening_*.json"))):
            n_tags = 1 if any(d.glob("screening_*.json")) else 0
            out.append({"dir": d.name, "annotated": bool(n_tags)})
    return out


def _short_id(name: str) -> str:
    m = re.search(r"AQV\d+|[Vv]\d+", name)
    return m.group(0) if m else name.split("_")[0]


def _plate_ntps(pd: Path) -> int:
    """Timepoint count for a plate, from the first well's first z-slice."""
    bf = pd / "bf"
    if not bf.is_dir():
        return 0
    for wd in sorted(bf.glob("*")):
        if not wd.is_dir():
            continue
        sls = sorted(wd.glob("SL*"))
        n = sum(1 for _ in (sls[0].glob("*.tif") if sls else wd.glob("*.tif")))
        if n:
            return n
    return 0


def _wells_all(data_root: Path) -> dict:
    """Every well across every plate, with its well-level annotations + count, plus
    the UNION of well columns/values — so the client can filter across plates
    (e.g. injected?=Yes AND line=pfkfb3 AND viability=alive) and sort by how many
    annotations each well has."""
    plates, wells = [], []
    colvals, coltypes = {}, {}
    for pd in sorted(Path(data_root).iterdir()):
        if not pd.is_dir() or not (pd / "bf").is_dir():
            continue
        scr = sorted(pd.glob("screening_*.json"))
        short = _short_id(pd.name)
        if not scr:
            plates.append({"dir": pd.name, "short": short, "n_wells": 0})
            continue
        try:
            d = json.load(open(scr[0]))
        except (OSError, json.JSONDecodeError):
            continue
        cols = d.get("columns", {}) or {}
        ann = d.get("annotations", {}) or {}
        for cn, spec in cols.items():
            coltypes.setdefault(cn, (spec or {}).get("type", "categorical"))
            s = colvals.setdefault(cn, set())
            for v in ((spec or {}).get("values") or []):
                s.add(v)
        n_tps = _plate_ntps(pd)          # lets the filter grid's frame-fader map to a real tp
        plates.append({"dir": pd.name, "short": short, "n_wells": len(ann), "n_tps": n_tps})
        for well, e in ann.items():
            for c, v in e.items():
                if not isinstance(v, (list, tuple)):
                    colvals.setdefault(c, set()).add(v)
            wells.append({"plate": pd.name, "short": short, "well": well,
                          "ann": e, "nann": len(e), "n_tps": n_tps})
    columns = {cn: {"type": coltypes.get(cn, "categorical"),
                    "values": sorted(colvals.get(cn, []))} for cn in colvals}
    return {"plates": plates, "columns": columns, "wells": wells}


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
        "plates": _list_plates(data_root),
        "iwamatsu_stages": stages,
        "defaults": defaults,
        "suggestions": model.registry_suggestions(),
        "column_types": list(model.COLUMN_TYPES),
    }


def _read_json(path: Path, fallback):
    try:
        return json.load(open(path))
    except (OSError, json.JSONDecodeError):
        return fallback


def _client_manifest(man: dict, path: Path, plate_name: str) -> dict:
    """The plate response: manifest minus the server-only _well_index, plus the
    loaded (migrated) annotation payload."""
    out = {k: v for k, v in man.items() if not k.startswith("_")}
    out["payload"] = model.load_payload(path, plate_name)
    out["screening_file"] = str(path)
    return out


# ------------------------------------------------------------------ HTTP handler
class Handler(BaseHTTPRequestHandler):
    data_root = DEFAULT_DATA_ROOT
    server_version = "MedakaAnnotator/1.0"

    def log_message(self, fmt, *args):            # quieter console
        if "/api/frame" not in self.path:
            sys.stderr.write("  %s\n" % (fmt % args))

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

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._serve_static("index.html")
            if u.path.startswith("/static/"):
                return self._serve_static(u.path[len("/static/"):])
            if u.path == "/api/config":
                return self._send(200, _load_config(self.data_root))
            if u.path == "/api/plate":
                return self._api_plate(q)
            if u.path == "/api/wells_all":
                return self._send(200, _wells_all(self.data_root))
            if u.path == "/api/frame":
                return self._api_frame(q)
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
        }.get(path.suffix, "application/octet-stream")
        # no-store: never cache the frontend, so edits always load (avoids stale JS)
        self._send(200, path.read_bytes(), ctype,
                   extra={"Cache-Control": "no-store, max-age=0"})

    def _api_plate(self, q):
        dir_arg = (q.get("dir") or [""])[0]
        man = _manifest(self.data_root, dir_arg)
        plate_dir = Path(man["plate_dir"])
        plate_name = plate_dir.name
        scr = plate_dir / f"screening_{plate_name}.json"
        self._send(200, _client_manifest(man, scr, plate_name))

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
        if fp is None or not fp.is_file():
            return self._err(404, f"no frame {well}/{ch}/{tp}")
        png = _PNG_CACHE.get_or((str(fp), size), lambda: _to_png(fp, size))
        self._send(200, png, "image/png",
                   extra={"Cache-Control": "public, max-age=86400"})

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/save":
                return self._api_save()
            return self._err(404, f"no route {u.path}")
        except Exception as e:
            return self._err(500, f"{type(e).__name__}: {e}")

    def _api_save(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._err(400, "invalid JSON body")
        dir_arg = body.get("dir") or ""
        payload = body.get("payload") or {}
        man = _manifest(self.data_root, dir_arg)
        plate_dir = Path(man["plate_dir"])
        plate_name = plate_dir.name
        scr = plate_dir / f"screening_{plate_name}.json"
        clean = model.save_payload(scr, payload, plate_name)
        self._send(200, {"ok": True, "screening_file": str(scr),
                         "updated": clean["updated"], "payload": clean})


def _serve(host, port, tries=20):
    """Bind the first free port at or after ``port`` (so a leftover server on the
    default port never blocks a fresh launch). Returns (httpd, actual_port)."""
    for p in range(port, port + tries):
        try:
            return ThreadingHTTPServer((host, p), Handler), p
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                continue
            raise
    sys.exit(f"no free port in {port}..{port + tries - 1} — free one or pass --port")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Medaka 3-level image annotator (web).")
    ap.add_argument("plate", nargs="?", default="",
                    help="plate folder name/prefix to open focused (optional)")
    ap.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT),
                    help=f"root holding plate folders (default {DEFAULT_DATA_ROOT})")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't auto-open a browser")
    args = ap.parse_args(argv)

    Handler.data_root = Path(args.data_root).resolve()
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
