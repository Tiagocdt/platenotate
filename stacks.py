#!/usr/bin/env python3
"""stacks.py — a folder of per-position hyperstack TIFFs, read as a plate.

One file = one well (a microscope position). Each file is a 5-D hyperstack —
time x z x channel x Y x X — which is how a Nikon Ti2 or a Zeiss LSM session is
actually stored once its channels are merged, instead of the one-TIFF-per-frame
layout the crop pipeline writes.

WHY THIS EXISTS
---------------
PlateNotate used to read only one image file per (channel, well, z, timepoint). A
Ti2 session therefore had to be EXPLODED into ~160,000 small files before it could
be looked at — and only at 576 px, because the full 2304 px frames would have been
sixteen times the bytes again. This module reads the stacks where they are, one
plane at a time:

  * An ImageJ hyperstack written uncompressed is ONE contiguous block after its
    first page header: plane k starts at  offset0 + k * plane_bytes. Reading a
    plane is a single positioned read of exactly that plane, at any file size.
    (ImageJ files over 4 GiB keep only ONE page header and the planes are found
    by this same arithmetic — it is also how ImageJ itself reads them.)
  * An OME-TIFF has one page header (IFD) per plane, chained through the file,
    and its planes are compressed. The chain is walked ONCE, in the background,
    and every plane's byte position is saved to a small local index; after that
    a plane is one read plus the decompression of that plane's own strips.

NEVER A WHOLE STACK. The files are 1-42 GB. The only bytes read for a frame are
that frame's — `tests/validate.py` counts them.

RESOLUTION LEVELS
-----------------
The same position may be stored twice: `<name>.ome.tif` at full resolution and
`<name>_bin4.tif` 4x4-binned. Both are ONE well (`<name>`): the display reads the
smallest level that still has the pixels the screen needs, and the full level as
soon as you zoom in. Two files are only treated as levels of one position when
their time, z and channel counts agree and one is an exact integer binning of the
other — otherwise they stay separate wells, each under its own name.

This module is standard library at import time (header parsing, discovery), so
`model.py` can use it without numpy; pixel decoding imports numpy/PIL lazily.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import threading
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Optional

TIFF_EXTS = (".tif", ".tiff")
INDEX_VERSION = 1

# ------------------------------------------------------------------ positioned reads
# os.pread is thread-safe (no shared file position), which matters: the server reads
# frames on a pool of threads, and two of them asking for planes of the SAME file must
# not move each other's file pointer. Windows has no os.pread, so there each file gets
# a lock around seek+read instead.
#
# Handles are cached, and only ever closed while NO read is using them. Closing one
# that another thread is about to read would be worse than an error: the OS hands the
# same descriptor number to the next file opened, and the late read then returns that
# OTHER file's bytes — a wrong plane, silently. Hence the reference count.
from collections import OrderedDict

_FD_LOCK = threading.Lock()
_FDS: "OrderedDict" = OrderedDict()   # key -> [handle, users]
_FD_MAX = 64
# Every byte read goes through _pread, so a test can count them: "a frame reads only
# its own plane" is a law, and this is how it is measured.
READ_STATS = {"calls": 0, "bytes": 0}


def _close_handle(h):
    try:
        (os.close(h) if isinstance(h, int) else h[0].close())
    except OSError:
        pass


def _pread(path: str, offset: int, n: int, key=None) -> bytes:
    """Read n bytes at offset. `key` (path, size, mtime) keeps a REPLACED file from
    being read through a handle that still points at the old one."""
    key = key or (path,)
    with _FD_LOCK:
        ent = _FDS.get(key)
        if ent is None:
            if len(_FDS) >= _FD_MAX:                 # close idle handles, oldest first
                for k in [k for k, e in _FDS.items() if e[1] == 0][: len(_FDS) - _FD_MAX + 1]:
                    _close_handle(_FDS.pop(k)[0])
            h = (os.open(path, os.O_RDONLY) if hasattr(os, "pread")
                 else (open(path, "rb"), threading.Lock()))
            if isinstance(h, int) and hasattr(os, "posix_fadvise"):
                # Every read here is exactly what is needed — a page header, one plane —
                # and the next one is megabytes away (planes of one channel are C*Z
                # planes apart). Kernel read-ahead would fetch hundreds of KB of other
                # planes per header while walking a page chain: tell it not to.
                try:
                    os.posix_fadvise(h, 0, 0, os.POSIX_FADV_RANDOM)
                except OSError:
                    pass
            ent = _FDS[key] = [h, 0]
        else:
            _FDS.move_to_end(key)
        ent[1] += 1
    try:
        h = ent[0]
        if isinstance(h, int):
            out = os.pread(h, n, offset)
        else:                                        # pragma: no cover - Windows
            with h[1]:
                h[0].seek(offset)
                out = h[0].read(n)
    finally:
        with _FD_LOCK:
            ent[1] -= 1
    READ_STATS["calls"] += 1
    READ_STATS["bytes"] += len(out)
    return out


def close_all():
    """Close every idle cached handle (tests; and before files are replaced)."""
    with _FD_LOCK:
        for k in [k for k, e in _FDS.items() if e[1] == 0]:
            _close_handle(_FDS.pop(k)[0])


# ------------------------------------------------------------------ TIFF structure
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8,
              13: 4, 16: 8, 17: 8, 18: 8}
_TYPE_FMT = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d",
             13: "I", 16: "Q", 17: "q", 18: "Q"}

T_WIDTH, T_LENGTH, T_BPS, T_COMPRESSION, T_PHOTOMETRIC = 256, 257, 258, 259, 262
T_DESCRIPTION, T_STRIPOFFSETS, T_SPP, T_ROWSPERSTRIP, T_STRIPBYTES = 270, 273, 277, 278, 279
T_XRES, T_PLANAR, T_RESUNIT, T_PREDICTOR = 282, 284, 296, 317
T_TILEW, T_TILEH, T_TILEOFFSETS, T_TILEBYTES, T_SAMPLEFORMAT = 322, 323, 324, 325, 339
T_IJ_COUNTS, T_IJ_DATA = 50838, 50839

_WANT_ALL = {T_WIDTH, T_LENGTH, T_BPS, T_COMPRESSION, T_PHOTOMETRIC, T_DESCRIPTION,
             T_STRIPOFFSETS, T_SPP, T_ROWSPERSTRIP, T_STRIPBYTES, T_XRES, T_PLANAR,
             T_RESUNIT, T_PREDICTOR, T_TILEW, T_TILEH, T_TILEOFFSETS, T_TILEBYTES,
             T_SAMPLEFORMAT, T_IJ_COUNTS, T_IJ_DATA}
# what the page walker needs from every page after the first: where its pixels are,
# plus enough to prove it has the same shape as page 0
_WANT_PAGE = {T_WIDTH, T_LENGTH, T_BPS, T_COMPRESSION, T_SPP,
              T_STRIPOFFSETS, T_STRIPBYTES, T_TILEOFFSETS, T_TILEBYTES}


def read_header(path: str, key=None):
    """(byteorder '<'|'>', bigtiff, offset of the first IFD) — or ValueError."""
    h = _pread(path, 0, 16, key)
    if len(h) < 8 or h[:2] not in (b"II", b"MM"):
        raise ValueError("not a TIFF file")
    bo = "<" if h[:2] == b"II" else ">"
    magic = struct.unpack(bo + "H", h[2:4])[0]
    if magic == 42:
        return bo, False, struct.unpack(bo + "I", h[4:8])[0]
    if magic == 43 and len(h) >= 16:
        return bo, True, struct.unpack(bo + "Q", h[8:16])[0]
    raise ValueError("not a TIFF file")


def read_ifd(path: str, off: int, bo: str, big: bool, want=_WANT_ALL, chunk: int = 4096,
             key=None):
    """Parse one IFD at `off` -> ({tag: value}, next IFD offset).

    ONE read of `chunk` bytes usually covers the whole IFD *and* the arrays it points
    to (tifffile and ImageJ both write a page's strip tables right after its IFD), so
    walking a chain of 16,000 pages costs 16,000 reads, not 50,000. Values outside the
    chunk are fetched separately. Arrays come back as tuples, scalars as numbers,
    ASCII as str, UNDEFINED/BYTE blobs as bytes.
    """
    blk = _pread(path, off, chunk, key)
    cnt_fmt, cnt_sz, ent_sz, ptr_fmt, inline = (
        ("Q", 8, 20, "Q", 8) if big else ("H", 2, 12, "I", 4))
    if len(blk) < cnt_sz:
        raise ValueError(f"IFD at {off} is past the end of the file")
    n = struct.unpack(bo + cnt_fmt, blk[:cnt_sz])[0]
    need = cnt_sz + n * ent_sz + inline
    if n > 100000:
        raise ValueError(f"IFD at {off} claims {n} entries — not a TIFF page")
    if len(blk) < need:
        blk = _pread(path, off, need, key)
        if len(blk) < need:
            raise ValueError(f"IFD at {off} is truncated")
    tags = {}
    for i in range(n):
        e = blk[cnt_sz + i * ent_sz: cnt_sz + (i + 1) * ent_sz]
        if big:
            tag, typ, count = struct.unpack(bo + "HHQ", e[:12])
            val = e[12:20]
        else:
            tag, typ, count = struct.unpack(bo + "HHI", e[:8])
            val = e[8:12]
        if tag not in want:
            continue
        size = _TYPE_SIZE.get(typ, 1) * count
        if size <= inline:
            data = val[:size]
        else:
            voff = struct.unpack(bo + ptr_fmt, val[:inline])[0]
            rel = voff - off
            data = (blk[rel:rel + size] if 0 <= rel and rel + size <= len(blk)
                    else _pread(path, voff, size, key))
        tags[tag] = _decode_value(typ, count, data, bo)
    nxt = struct.unpack(bo + ptr_fmt, blk[cnt_sz + n * ent_sz: need])[0]
    return tags, nxt


def _decode_value(typ, count, data, bo):
    if typ == 2:                                             # ASCII
        return data.split(b"\0", 1)[0].decode("utf-8", "replace")
    if typ in (1, 7) and count > 4:                          # a blob (IJ metadata)
        return bytes(data)
    if typ in (5, 10):                                       # (SIGNED) RATIONAL
        f = "I" if typ == 5 else "i"
        v = struct.unpack(bo + f * (2 * count), data)
        pairs = tuple((v[2 * i], v[2 * i + 1]) for i in range(count))
        return pairs[0] if count == 1 else pairs
    f = _TYPE_FMT.get(typ)
    if f is None:
        return bytes(data)
    v = struct.unpack(bo + f * count, data)
    return v[0] if count == 1 else v


def _as_tuple(v):
    if v is None:
        return ()
    return v if isinstance(v, tuple) else (v,)


# ------------------------------------------------------------------ ImageJ metadata
def _ij_description(desc: str) -> dict:
    out = {}
    for line in (desc or "").splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = {"true": True, "false": False}.get(v.lower(), v)
    return out


def _ij_metadata(blob: bytes, counts, bo: str) -> dict:
    """ImageJ's private IJMetadata tag: Info, per-plane Labels, display Ranges.

    Layout: a header of 'IJIJ' + (type, count) pairs, then the entries back to back;
    the MetaDataByteCounts tag gives each entry's length. Strings are UTF-16 and
    numbers are doubles, both in the TIFF's own byte order — and in a little-endian
    file the four-letter type codes therefore read backwards ('ofni' = 'info').
    """
    counts = _as_tuple(counts)
    if not blob or not counts or blob[:4] not in (b"IJIJ", b"JIJI"):
        return {}
    hsize = counts[0]
    if hsize < 12 or hsize > 804:
        return {}
    ntypes = (hsize - 4) // 8
    head = struct.unpack(bo + "4sI" * ntypes, blob[4:4 + ntypes * 8])
    utf = "utf-16-be" if bo == ">" else "utf-16-le"
    pos, k, out = hsize, 0, {}
    for code, n in zip(head[::2], head[1::2]):
        if bo == "<":
            code = code[::-1]
        vals = []
        for _ in range(n):
            k += 1
            if k >= len(counts):
                return out
            seg = blob[pos:pos + counts[k]]
            pos += counts[k]
            if code in (b"info", b"labl", b"prop"):
                vals.append(seg.decode(utf, "replace"))
            elif code == b"rang":
                vals.append(struct.unpack(bo + "d" * (len(seg) // 8), seg))
            else:
                vals.append(None)                    # LUTs, ROIs, overlays: not needed
        name = {b"info": "Info", b"labl": "Labels", b"rang": "Ranges",
                b"prop": "Properties"}.get(code)
        if name:
            out[name] = vals[0] if (n == 1 and name != "Labels") else vals
    return out


def _channel_names_from_labels(labels, nc: int) -> list:
    """ImageJ per-plane labels -> one name per channel.

    Labels are per PLANE ('T001 SL1 HyLight_395-510', 'T001 z1 ex405'); the first
    `nc` of them are the channels of the first (t, z). What they share — 'T001 SL1'
    — is the position, what differs is the channel. With exactly `nc` labels they
    are the channel names already."""
    labels = [str(x).strip() for x in (labels or [])]
    if len(labels) < nc or nc < 1:
        return []
    first = labels[:nc]
    if nc == 1:
        toks = first[0].split()
        return [toks[-1]] if toks else []
    split = [s.split() for s in first]
    lead = 0
    while all(len(t) > lead + 1 for t in split) and len({t[lead] for t in split}) == 1:
        lead += 1
    names = [" ".join(t[lead:]) for t in split]
    return names if all(names) and len(set(names)) == nc else []


# ------------------------------------------------------------------ OME metadata
_UNIT_UM = {"µm": 1.0, "um": 1.0, "micron": 1.0, "microns": 1.0, "nm": 1e-3, "mm": 1e3,
            "cm": 1e4, "m": 1e6, "Å": 1e-4, "pm": 1e-6}
_UNIT_S = {"s": 1.0, "sec": 1.0, "ms": 1e-3, "µs": 1e-6, "us": 1e-6, "min": 60.0,
           "h": 3600.0, "hr": 3600.0}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _ome(desc: str, own_name: str) -> dict:
    """The first Image's Pixels: sizes, plane order, channel names, physical units,
    and the plane -> IFD map (None = the standard one: IFD i is plane i)."""
    root = ET.fromstring(desc)
    pix = next((el for el in root.iter() if _local(el.tag) == "Pixels"), None)
    if pix is None:
        raise ValueError("OME-XML without a Pixels element")
    a = pix.attrib
    size = {d: int(a.get("Size" + d, "1")) for d in "XYZCT"}
    order = a.get("DimensionOrder", "XYCZT")
    if sorted(order) != sorted("XYZCT") or not order.startswith("XY"):
        raise ValueError(f"unsupported DimensionOrder {order!r}")
    chans = [el for el in pix if _local(el.tag) == "Channel"]
    if any(int(c.attrib.get("SamplesPerPixel", "1")) > 1 for c in chans):
        raise ValueError("RGB (multi-sample) OME channels are not supported")
    names = [c.attrib.get("Name") or "" for c in chans]

    def phys(key, units, default_unit):
        v = a.get(key)
        if v is None:
            return None
        try:
            return float(v) * units.get(a.get(key + "Unit", default_unit), 1.0)
        except ValueError:
            return None

    px_um = phys("PhysicalSizeX", _UNIT_UM, "µm")
    z_um = phys("PhysicalSizeZ", _UNIT_UM, "µm")
    dt_s = None
    if a.get("TimeIncrement") is not None:
        try:
            dt_s = float(a["TimeIncrement"]) * _UNIT_S.get(a.get("TimeIncrementUnit", "s"), 1.0)
        except ValueError:
            dt_s = None
    total = size["Z"] * size["C"] * size["T"]
    tds = [el for el in pix if _local(el.tag) == "TiffData"]
    mapping = None
    for td in tds:
        for u in td:
            fn = u.attrib.get("FileName")
            if _local(u.tag) == "UUID" and fn and Path(fn).name != own_name:
                raise ValueError("this OME-TIFF keeps planes in other files "
                                 f"({fn}) — multi-file OME is not supported")
    simple = (len(tds) <= 1 and all(k not in td.attrib for td in tds
                                    for k in ("FirstT", "FirstZ", "FirstC"))
              and all(int(td.attrib.get("IFD", "0")) == 0 for td in tds))
    if not simple:
        mapping = {}
        stride = _strides(order, size)
        for td in tds:
            at = td.attrib
            ifd = int(at.get("IFD", "0"))
            n = int(at.get("PlaneCount", "1" if "IFD" in at else str(total)))
            start = (int(at.get("FirstT", "0")) * stride["T"]
                     + int(at.get("FirstZ", "0")) * stride["Z"]
                     + int(at.get("FirstC", "0")) * stride["C"])
            for i in range(n):
                mapping[start + i] = ifd + i
    return {"size": size, "order": order, "names": names, "px_um": px_um,
            "z_um": z_um, "dt_s": dt_s, "mapping": mapping}


def _strides(order: str, size: dict) -> dict:
    """Plane-number stride of each of Z, C, T for an OME DimensionOrder: 'XYCZT' has
    C fastest (stride 1), then Z (stride SizeC), then T."""
    out, s = {}, 1
    for d in order[2:]:
        out[d] = s
        s *= size[d]
    return out


# ------------------------------------------------------------------ one stack file
_DTYPE = {(1, 8): "u1", (1, 16): "u2", (1, 32): "u4", (2, 8): "i1", (2, 16): "i2",
          (2, 32): "i4", (3, 32): "f4", (3, 64): "f8"}
_INDEX_DIR = {"path": None}


def set_index_dir(path) -> None:
    """Where page indexes of compressed stacks are kept (the server sets this to a
    folder in the user's app-data; nothing is ever written next to the images)."""
    _INDEX_DIR["path"] = Path(path) if path else None


class StackFile:
    """One hyperstack TIFF: its shape, its channels, and where each plane lives.

    Construction reads only the first page header (plus, for ImageJ, a second one to
    prove the planes are contiguous). Raises ValueError for anything it cannot read
    honestly — a wrong guess about a file's layout would serve the wrong plane with
    no error, which is the one outcome this must never have.
    """

    def __init__(self, path):
        self.path = str(path)
        st = os.stat(self.path)
        self.size, self.mtime_ns = st.st_size, st.st_mtime_ns
        self._key = (self.path, self.size, self.mtime_ns)
        self.bo, self.big, first = read_header(self.path, self._key)
        tags, nxt = read_ifd(self.path, first, self.bo, self.big, key=self._key)
        self.first_ifd, self._ifd1 = first, nxt
        self.X, self.Y = int(tags.get(T_WIDTH, 0)), int(tags.get(T_LENGTH, 0))
        bps = _as_tuple(tags.get(T_BPS, 1))
        self.spp = int(tags.get(T_SPP, 1))
        fmt = _as_tuple(tags.get(T_SAMPLEFORMAT, 1))[0]
        if self.spp != 1:
            raise ValueError(f"{self.spp} samples per pixel (RGB) — only single-channel "
                             "planes are supported")
        if (fmt, bps[0]) not in _DTYPE:
            raise ValueError(f"unsupported pixel type ({bps[0]}-bit, format {fmt})")
        self.dtype = self.bo + _DTYPE[(fmt, bps[0])] if bps[0] > 8 else _DTYPE[(fmt, bps[0])]
        self.itemsize = bps[0] // 8
        self.plane_bytes = self.X * self.Y * self.itemsize
        self.compression = int(tags.get(T_COMPRESSION, 1))
        self.predictor = int(tags.get(T_PREDICTOR, 1))
        if T_TILEOFFSETS in tags:
            self.tiled = True
            self.tw, self.th = int(tags[T_TILEW]), int(tags[T_TILEH])
            self._seg0 = (_as_tuple(tags[T_TILEOFFSETS]), _as_tuple(tags[T_TILEBYTES]))
        else:
            self.tiled = False
            self.rps = min(int(tags.get(T_ROWSPERSTRIP, self.Y) or self.Y), self.Y)
            self._seg0 = (_as_tuple(tags.get(T_STRIPOFFSETS)),
                          _as_tuple(tags.get(T_STRIPBYTES)))
        desc = tags.get(T_DESCRIPTION) or ""
        self.kind, self.names, self.ranges, self.info = "plain", [], None, ""
        self.px_um = self.z_um = self.dt_s = None
        self.T = self.Z = self.C = 1
        self._map = None                                  # plane -> IFD number (OME)
        self._order = "CZT"                               # c fastest, then z, then t
        if desc.lstrip().startswith("<?xml") or "<OME" in desc[:512]:
            self._init_ome(desc)
        elif desc.startswith("ImageJ="):
            self._init_imagej(desc, tags)
        self.n_planes = self.T * self.Z * self.C
        if self.X <= 0 or self.Y <= 0:
            raise ValueError("page 0 has no width/height")
        # contiguous = every plane is uncompressed and they follow one another
        self.contiguous_at = self._contiguous_offset() if self.kind == "imagej" else None
        if self.n_planes == 1 and self.contiguous_at is None:
            self.contiguous_at = self._contiguous_offset(single=True)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._offs: list = []               # per indexed page: tuple of segment offsets
        self._cnts: list = []               # ...and byte counts
        self._complete = self.contiguous_at is not None
        self._walker = None
        self._error = None
        self._display_cache: dict = {}

    # -- header interpretation ----------------------------------------------------
    def _init_imagej(self, desc, tags):
        md = _ij_description(desc)
        images = int(md.get("images", 1) or 1)
        if images <= 1 and not md.get("hyperstack"):
            return                                        # a single ImageJ image
        self.kind = "imagej"
        C, Z, T = int(md.get("channels", 1)), int(md.get("slices", 1)), int(md.get("frames", 1))
        if C * Z * T != images:
            # ImageJ only writes the axes it has; an unlabelled count is ImageJ's own
            # default reading of a stack: slices.
            if "frames" not in md and images % (C * Z) == 0:
                T = images // (C * Z)
            elif "slices" not in md and images % (C * T) == 0:
                Z = images // (C * T)
            else:
                C, Z, T = 1, images, 1
        self.C, self.Z, self.T = C, Z, T
        ij = _ij_metadata(tags.get(T_IJ_DATA) or b"", tags.get(T_IJ_COUNTS), self.bo)
        self.info = ij.get("Info") or ""
        self.names = _channel_names_from_labels(ij.get("Labels"), C)
        rng = ij.get("Ranges")
        if isinstance(rng, (list, tuple)) and rng and isinstance(rng[0], (list, tuple)):
            rng = rng[0]
        if rng and len(rng) >= 2 * C:
            self.ranges = [(float(rng[2 * i]), float(rng[2 * i + 1])) for i in range(C)]
        elif C == 1 and "min" in md and "max" in md:
            self.ranges = [(float(md["min"]), float(md["max"]))]
        unit = str(md.get("unit", "")).replace("\\u00B5", "µ")
        xr = tags.get(T_XRES)
        if xr and unit and unit.lower() not in ("pixel", "pixels"):
            num, den = xr if isinstance(xr, tuple) else (xr, 1)
            if num:
                per = _UNIT_UM.get(unit, _UNIT_UM.get(unit.lower()))
                if per:
                    self.px_um = (den / num) * per
        if md.get("spacing"):
            self.z_um = float(md["spacing"]) * (_UNIT_UM.get(unit, 1.0) if unit else 1.0)
        if md.get("finterval"):
            self.dt_s = float(md["finterval"])

    def _init_ome(self, desc):
        om = _ome(desc, Path(self.path).name)
        self.kind = "ome"
        s = om["size"]
        if (s["X"], s["Y"]) != (self.X, self.Y):
            raise ValueError(f"OME says {s['X']}x{s['Y']} but page 0 is {self.X}x{self.Y}")
        self.T, self.Z, self.C = s["T"], s["Z"], s["C"]
        self._order = om["order"][2:]
        self._strides = _strides(om["order"], s)
        self._map = om["mapping"]
        self.names = om["names"] if len(om["names"]) == self.C else []
        self.px_um, self.z_um, self.dt_s = om["px_um"], om["z_um"], om["dt_s"]

    def _contiguous_offset(self, single=False):
        """offset of plane 0 if planes are stored uncompressed back to back, else None.

        PROVEN, not assumed: page 0's strips must be one unbroken run of exactly one
        plane, the file must be long enough for every plane, and — when a second page
        header exists — its pixels must start exactly one plane after page 0's."""
        if self.compression != 1 or self.tiled:
            return None
        offs, cnts = self._seg0
        if not offs or len(offs) != len(cnts) or sum(cnts) != self.plane_bytes:
            return None
        if any(offs[i] + cnts[i] != offs[i + 1] for i in range(len(offs) - 1)):
            return None
        o0 = offs[0]
        if o0 + self.n_planes * self.plane_bytes > self.size:
            return None
        if not single and self._ifd1:
            try:
                t1, _ = read_ifd(self.path, self._ifd1, self.bo, self.big, want=_WANT_PAGE,
                                 key=self._key)
            except (ValueError, OSError):
                return None
            o1 = _as_tuple(t1.get(T_STRIPOFFSETS))
            if not o1 or o1[0] != o0 + self.plane_bytes:
                return None
        return o0

    # -- plane numbering ----------------------------------------------------------
    def plane_index(self, t: int, z: int, c: int) -> int:
        """0-based (t, z, c) -> plane number in the file's own order."""
        if not (0 <= t < self.T and 0 <= z < self.Z and 0 <= c < self.C):
            raise IndexError(f"plane t={t} z={z} c={c} outside T={self.T} Z={self.Z} C={self.C}")
        if self.kind == "ome":
            s = self._strides
            return t * s["T"] + z * s["Z"] + c * s["C"]
        return (t * self.Z + z) * self.C + c                      # ImageJ: CZT

    def ifd_number(self, plane: int) -> int:
        return self._map[plane] if self._map is not None else plane

    # -- the page index (compressed / non-contiguous files) ---------------------------
    def _index_key(self) -> str:
        real = os.path.realpath(self.path)
        return hashlib.sha1(f"{real}|{self.size}|{self.mtime_ns}|v{INDEX_VERSION}"
                            .encode()).hexdigest()

    def _index_file(self):
        d = _INDEX_DIR["path"]
        return None if d is None else d / (self._index_key() + ".json.gz")

    def _load_index(self) -> bool:
        import gzip
        p = self._index_file()
        if p is None:
            return False
        try:
            data = json.loads(gzip.decompress(p.read_bytes()))
        except (OSError, ValueError, EOFError):
            return False
        cnts = data.get("counts")
        if data.get("layout") == "runs":            # each page's segments back to back
            offs = []
            for first, cc in zip(data.get("first") or [], cnts or []):
                o, run = first, []
                for n in cc:
                    run.append(o)
                    o += n
                offs.append(run)
        else:
            offs = data.get("offsets")
        need = (max(self.ifd_number(q) for q in range(self.n_planes)) + 1
                if self._map is not None else self.n_planes)
        if not isinstance(offs, list) or len(offs) != len(cnts or []) or len(offs) < need:
            return False
        self._offs = [tuple(o) for o in offs]
        self._cnts = [tuple(c) for c in cnts]
        self._complete = True
        return True

    def _save_index(self):
        """Keep the walk. Its result is tiny next to the walk itself (a minute of reads
        on a network share for a 16,000-plane file): when every page's segments lie
        back to back — as tifffile writes them — only the first offset and the byte
        counts are stored, about 1 MB for 16,000 pages."""
        import gzip
        p = self._index_file()
        if p is None:
            return
        runs = all(o[i] + c[i] == o[i + 1] for o, c in zip(self._offs, self._cnts)
                   for i in range(len(o) - 1))
        body = {"path": self.path, "size": self.size, "pages": len(self._offs),
                "version": INDEX_VERSION, "counts": self._cnts}
        if runs:
            body.update(layout="runs", first=[o[0] for o in self._offs])
        else:
            body.update(layout="segments", offsets=self._offs)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(json.dumps(body, separators=(",", ":")).encode(), 6))
            tmp.replace(p)
        except OSError:
            pass                                   # an unsaved index is only slower next time

    def start_index(self):
        """Begin walking the page chain in the background (idempotent). Pages become
        readable in order as the walk passes them, so the start of a time course is
        available within a second even while the end is still being found."""
        if self._complete or self._error:
            return
        with self._lock:
            if self._walker is not None or self._complete:
                return
            if self._load_index():
                self._cond.notify_all()
                return
            self._walker = threading.Thread(target=self._walk, daemon=True,
                                            name="stack-index")
            self._walker.start()

    def _walk(self):
        want = max(self.ifd_number(p) for p in range(self.n_planes)) + 1 \
            if self._map is not None else self.n_planes
        off, k = self.first_ifd, 0
        segs0 = self._seg0
        try:
            while off and k < want:
                if k == 0:
                    offs, cnts = segs0
                    nxt = self._ifd1
                else:
                    t, nxt = read_ifd(self.path, off, self.bo, self.big, want=_WANT_PAGE,
                                      key=self._key)
                    if (int(t.get(T_WIDTH, 0)), int(t.get(T_LENGTH, 0))) != (self.X, self.Y) \
                            or int(t.get(T_COMPRESSION, 1)) != self.compression:
                        raise ValueError(f"page {k} has a different shape or compression "
                                         "than page 0 — not one hyperstack")
                    if self.tiled:
                        offs, cnts = _as_tuple(t.get(T_TILEOFFSETS)), _as_tuple(t.get(T_TILEBYTES))
                    else:
                        offs, cnts = _as_tuple(t.get(T_STRIPOFFSETS)), _as_tuple(t.get(T_STRIPBYTES))
                    if not offs or len(offs) != len(cnts):
                        raise ValueError(f"page {k} has no pixel data")
                with self._lock:
                    self._offs.append(tuple(offs))
                    self._cnts.append(tuple(cnts))
                    if k % 64 == 0:
                        self._cond.notify_all()
                off, k = nxt, k + 1
            if k < want:
                with self._lock:
                    self._error = (f"the file holds {k} pages but its header promises "
                                   f"{want} — truncated?")
            else:
                # saved BEFORE it is declared complete: "complete" must mean the next
                # process will find the index too (a reader woken by "complete" that
                # looked for the file a moment early found nothing — the law caught it)
                self._save_index()
            with self._lock:
                self._complete = True
                self._cond.notify_all()
        except Exception as e:                     # noqa: BLE001 — reported to every reader
            with self._lock:
                self._error = f"{type(e).__name__}: {e}"
                self._complete = True
                self._cond.notify_all()

    def page_ready(self, plane: int) -> bool:
        if self.contiguous_at is not None:
            return True
        return self.ifd_number(plane) < len(self._offs)

    @property
    def index_progress(self):
        """(pages indexed, pages needed, complete, error)"""
        if self.contiguous_at is not None:
            return self.n_planes, self.n_planes, True, None
        return len(self._offs), self.n_planes, self._complete, self._error

    def _segments(self, plane: int, timeout: float):
        ifd = self.ifd_number(plane)
        if ifd >= len(self._offs):
            self.start_index()
            with self._cond:
                self._cond.wait_for(lambda: ifd < len(self._offs) or self._complete,
                                    timeout=timeout)
        if ifd < len(self._offs):
            return self._offs[ifd], self._cnts[ifd]
        if self._error:
            raise ValueError(self._error)
        raise TimeoutError(f"still indexing {Path(self.path).name} "
                           f"({len(self._offs)}/{self.n_planes} pages) — one-time, try again")

    # -- reading a plane ----------------------------------------------------------
    def read_plane(self, t: int, z: int, c: int, timeout: float = 8.0):
        """The (Y, X) numpy array of one plane, exactly as stored — no scaling."""
        import numpy as np
        p = self.plane_index(t, z, c)
        if self.contiguous_at is not None:
            buf = _pread(self.path, self.contiguous_at + p * self.plane_bytes,
                         self.plane_bytes, self._key)
            if len(buf) != self.plane_bytes:
                raise ValueError(f"plane {p} is cut short ({len(buf)} of {self.plane_bytes} bytes)")
            return np.frombuffer(buf, dtype=self.dtype).reshape(self.Y, self.X).astype(
                self.dtype[-2:] if self.itemsize > 1 else self.dtype, copy=False)
        offs, cnts = self._segments(p, timeout)
        return self._decode(offs, cnts)

    def _decode(self, offs, cnts):
        import numpy as np
        native = self.dtype[-2:] if self.itemsize > 1 else self.dtype
        out = np.zeros((self.Y, self.X), dtype=native)
        lo, hi = min(offs), max(o + n for o, n in zip(offs, cnts))
        if hi - lo <= 1.25 * sum(cnts) + 65536:          # one read covers every segment
            blk = _pread(self.path, lo, hi - lo, self._key)
            raw = [blk[o - lo:o - lo + n] for o, n in zip(offs, cnts)]
        else:
            raw = [_pread(self.path, o, n, self._key) for o, n in zip(offs, cnts)]
        if self.tiled:
            across = -(-self.X // self.tw)
            for j, seg in enumerate(raw):
                a = self._unpack(seg, self.th, self.tw)
                r0, c0 = (j // across) * self.th, (j % across) * self.tw
                h, w = min(self.th, self.Y - r0), min(self.tw, self.X - c0)
                if h > 0 and w > 0:
                    out[r0:r0 + h, c0:c0 + w] = a[:h, :w]
        else:
            for j, seg in enumerate(raw):
                r0 = j * self.rps
                rows = min(self.rps, self.Y - r0)
                if rows <= 0:
                    break
                out[r0:r0 + rows] = self._unpack(seg, rows, self.X)
        return out

    def _unpack(self, seg: bytes, rows: int, cols: int):
        import numpy as np
        data = _decompress(self.compression, seg, rows * cols * self.itemsize)
        need = rows * cols * self.itemsize
        if len(data) < need:
            raise ValueError(f"a segment decompressed to {len(data)} bytes, expected {need}")
        a = np.frombuffer(data[:need], dtype=self.dtype).reshape(rows, cols)
        if self.itemsize > 1:
            a = a.astype(self.dtype[-2:])                     # to native byte order
        if self.predictor == 2:                               # horizontal differencing
            a = np.cumsum(a, axis=1, dtype=a.dtype)
        elif self.predictor not in (1, 0):
            raise ValueError(f"TIFF predictor {self.predictor} is not supported")
        return a


def _decompress(code: int, seg: bytes, size_hint: int) -> bytes:
    if code == 1:
        return seg
    if code in (8, 32946):                                    # Adobe / old deflate
        return zlib.decompress(seg)
    if code == 34925:
        import lzma
        return lzma.decompress(seg)
    try:
        import imagecodecs
    except ImportError:                                       # pragma: no cover
        imagecodecs = None
    fn = {5: "lzw_decode", 50000: "zstd_decode", 32773: "packbits_decode",
          34887: "lerc_decode", 50001: "webp_decode"}.get(code)
    if imagecodecs is not None and fn and hasattr(imagecodecs, fn):
        return bytes(getattr(imagecodecs, fn)(seg))
    raise ValueError(f"TIFF compression {code} is not supported")


# ------------------------------------------------------------------ grouping into wells
_LEVEL_RE = re.compile(r"(?i)[_-]bin(\d+)$")


def well_base(path) -> str:
    """The well a file belongs to: its name without .tif/.ome and a trailing _binN.

    `X_8bit.ome.tif` and `X_8bit_bin4.tif` are the same position at two resolutions,
    so both are well `X_8bit`."""
    name = Path(path).name
    for ext in (".ome.tiff", ".ome.tif", ".tiff", ".tif"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    return _LEVEL_RE.sub("", name)


def _stack_files(folder: Path):
    try:
        return sorted(p for p in Path(folder).iterdir()
                      if p.is_file() and p.suffix.lower() in TIFF_EXTS
                      and not p.name.startswith("."))
    except OSError:
        return []


def sniff(path) -> bool:
    """Is this file a hyperstack (more than one plane, ImageJ or OME)? Reads one
    header — cheap enough to ask of a handful of files per folder."""
    try:
        bo, big, first = read_header(str(path))
        tags, _ = read_ifd(str(path), first, bo, big, want={T_DESCRIPTION})
    except (OSError, ValueError, struct.error):
        return False
    d = tags.get(T_DESCRIPTION) or ""
    if d.startswith("ImageJ="):
        md = _ij_description(d)
        return int(md.get("images", 1) or 1) > 1
    if "<OME" in d[:2048] or d.lstrip().startswith("<?xml"):
        m = re.search(r"<Pixels[^>]*>", d)
        if not m:
            return False
        sz = {k: int(v) for k, v in re.findall(r'Size([ZCT])="(\d+)"', m.group(0))}
        return sz.get("Z", 1) * sz.get("C", 1) * sz.get("T", 1) > 1
    return False


_REGISTRY: dict = {}
_REGISTRY_LOCK = threading.Lock()


def open_stack(path) -> "StackFile":
    """The StackFile for `path`, shared across re-discoveries while the file is
    unchanged — so re-opening a plate keeps its page index (and any walk in
    progress) instead of starting another one."""
    path = str(path)
    st = os.stat(path)
    with _REGISTRY_LOCK:
        sf = _REGISTRY.get(path)
        if sf is not None and (sf.size, sf.mtime_ns) == (st.st_size, st.st_mtime_ns):
            return sf
    sf = StackFile(path)
    with _REGISTRY_LOCK:
        _REGISTRY[path] = sf
    return sf


def is_stack_dir(folder, peek: int = 6) -> bool:
    """A folder is a stack plate if any of the first few TIFFs in it is a hyperstack.

    Scans lazily and stops early: this is asked of every folder in a data root, and
    a flat folder of 100,000 crops must not be listed in full just to say "no"."""
    n = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith(".") or not e.name.lower().endswith(TIFF_EXTS):
                    continue
                try:
                    if not e.is_file():
                        continue
                except OSError:
                    continue
                if sniff(e.path):
                    return True
                n += 1
                if n >= peek:
                    return False
    except OSError:
        return False
    return False


_BG = {"thread": None, "queue": [], "lock": threading.Lock()}


def index_in_background(files, concurrency: int = 2) -> None:
    """Walk the page chains of these stacks, `concurrency` files at a time, in order.

    Contiguous files need no index and are skipped. A file already being walked (a
    zoomed-in frame asked for it) is waited for, not walked twice. Daemon thread:
    closing the app never waits for it — the walk is resumable, it simply starts over."""
    todo = [f for f in files if f.contiguous_at is None and not f._complete]
    if not todo:
        return
    with _BG["lock"]:
        known = {id(f) for f in _BG["queue"]}
        _BG["queue"].extend(f for f in todo if id(f) not in known)
        if _BG["thread"] is not None and _BG["thread"].is_alive():
            return

        def run():
            active = []
            while True:
                with _BG["lock"]:
                    active = [f for f in active if not f._complete]
                    while len(active) < concurrency and _BG["queue"]:
                        f = _BG["queue"].pop(0)
                        if not f._complete:
                            f.start_index()
                            active.append(f)
                    if not active and not _BG["queue"]:
                        _BG["thread"] = None
                        return
                with active[0]._cond:
                    active[0]._cond.wait(timeout=0.5)

        _BG["thread"] = threading.Thread(target=run, daemon=True, name="stack-index-queue")
        _BG["thread"].start()


class StackWell:
    """One position: one file, or the same data at several resolutions."""

    def __init__(self, well: str, levels: list):
        self.well = well
        self.levels = sorted(levels, key=lambda s: -(s.X * s.Y))   # [0] = full resolution
        self.full = self.levels[0]

    @property
    def native(self):
        return self.full.X, self.full.Y

    def choose(self, size: int, plane_of=None):
        """(level, exact) for a frame whose long edge should be about `size` px.

        The smallest level that still has the pixels (a 576 px level answers a 600 px
        request: the screen cannot tell, and it is 16x fewer bytes than the full
        frame). If that level's page is not indexed yet, the best level below it that
        IS readable stands in — `exact=False`, so the caller does not cache the stand-in
        as if it were the real thing."""
        want = None
        for lv in reversed(self.levels):                      # smallest first
            if max(lv.X, lv.Y) >= 0.9 * size:
                want = lv
                break
        if want is None:
            want = self.full
        if plane_of is None or want.page_ready(plane_of(want)):
            return want, True
        want.start_index()
        i = self.levels.index(want)
        for lv in self.levels[i + 1:]:
            if lv.page_ready(plane_of(lv)):
                return lv, False
        return want, True                                     # nothing better: wait for it


def _common_tokens(names):
    toks = [n.split("_") for n in names]
    lead = 0
    while all(len(t) > lead for t in toks) and len({t[lead] for t in toks}) == 1:
        lead += 1
    trail = 0
    while all(len(t) > lead + trail for t in toks) and \
            len({t[len(t) - 1 - trail] for t in toks}) == 1:
        trail += 1
    return lead, trail


def short_labels(wells) -> dict:
    """Display names: the part of each well id that differs between them.

    `20261016_181713_463_Point0000_8bit` → `Point0000`. DISPLAY ONLY — the id itself
    (the whole stem) stays the key annotations are stored under, because a key derived
    from whatever else happens to be in the folder would change the day a file is
    added, and orphan every annotation made before."""
    wells = list(wells)
    if len(wells) < 2:
        return {w: w for w in wells}
    lead, trail = _common_tokens(wells)
    out = {}
    for w in wells:
        t = w.split("_")
        s = "_".join(t[lead:len(t) - trail if trail else None])
        out[w] = s or w
    if len(set(out.values())) != len(out):
        return {w: w for w in wells}
    return out


_DETECT_RE = re.compile(r"(?i)^(dia|bf|brightfield|bright[-_ ]?field|trans|transmitted|"
                        r"dic|phase|ph|ndd)")


def discover(plate_dir) -> dict:
    """Index a folder of stacks. Header reads only — no pixels.

    Returns {"wells": {id: StackWell}, "channels": [...], "detect_channel": str,
             "chan_index": {id: {label: c}}, "labels": {id: short},
             "skipped": [(file, why)], "meta": plate_metadata.json or {}}"""
    plate_dir = Path(plate_dir)
    meta = {}
    for p in (plate_dir / "plate_metadata.json", plate_dir / "metadata" / "plate_metadata.json"):
        if p.is_file():
            try:
                meta = json.loads(p.read_text())
            except (OSError, ValueError):
                meta = {}
            break
    relabel = meta.get("channel_labels") or {}
    files, skipped = [], []
    for p in _stack_files(plate_dir):
        try:
            sf = open_stack(p)
        except (OSError, ValueError, struct.error, ET.ParseError) as e:
            skipped.append((p.name, str(e)))
            continue
        if sf.n_planes == 1:                       # a still (a quicklook, a montage
            skipped.append((p.name, "a single image, not a stack"))   # snapshot): not a well
            continue
        files.append(sf)
    groups: dict = {}
    for sf in files:
        groups.setdefault(well_base(sf.path), []).append(sf)
    wells = {}
    for base, members in groups.items():
        ok = _levels_agree(members)
        if ok:
            wells[base] = StackWell(base, members)
        else:                                           # not one position: keep apart
            for sf in members:
                name = Path(sf.path).name
                stem = name[: -len(".ome.tif")] if name.lower().endswith(".ome.tif") else Path(name).stem
                wid = stem if stem not in wells else name
                wells[wid] = StackWell(wid, [sf])
    channels, chan_index = [], {}
    for wid in sorted(wells):
        sf = wells[wid].full
        names = sf.names if len(sf.names) == sf.C else [f"C{i + 1}" for i in range(sf.C)]
        idx = {}
        for c, raw in enumerate(names):
            lab = str(relabel.get(raw, raw))
            idx[lab] = c
            if lab not in channels:
                channels.append(lab)
        chan_index[wid] = idx
    bf = meta.get("bf_channel")
    det = bf if bf in channels else next((c for c in channels if _DETECT_RE.match(c)),
                                         channels[0] if channels else None)
    order = ([det] if det else []) + [c for c in channels if c != det]
    return {"wells": wells, "channels": order, "detect_channel": det,
            "chan_index": chan_index, "labels": short_labels(sorted(wells)),
            "skipped": skipped, "meta": meta}


def _levels_agree(members) -> bool:
    """Several files with one well name are levels of ONE position only if T, Z, C and
    the pixel type agree and each smaller level is an exact integer binning of the
    full one. Anything else is two different things that happen to share a name."""
    if len(members) == 1:
        return True
    full = max(members, key=lambda s: s.X * s.Y)
    for m in members:
        if (m.T, m.Z, m.C) != (full.T, full.Z, full.C):
            return False
        if m is full:
            continue
        if full.X % m.X or full.Y % m.Y or full.X // m.X != full.Y // m.Y:
            return False
    sizes = [(m.X, m.Y) for m in members]
    return len(set(sizes)) == len(sizes)


# ------------------------------------------------------------------ display
def display_map(sf: StackFile, c: int, plate_cal: Optional[dict], label: str):
    """(lo, hi, source) — ONE constant map per channel for a >8-bit stack, or None.

    Never per frame. Stretching each frame to its own min..max makes a fading signal
    look constant — it silently destroys the comparison over time that the data was
    kept 16-bit for. In order of preference: the plate's own calibration file, the
    display range stored in the file (ImageJ Ranges — for the 780 stacks that is the
    0.1/99.9 % map pooled over every plane of every position), else one map measured
    from a sample of this file's planes and then held fixed."""
    if sf.itemsize == 1 and sf.dtype in ("u1",):
        return None
    if plate_cal and label in plate_cal:
        lo, hi = plate_cal[label]
        return float(lo), float(hi), "plate calibration"
    if sf.ranges and c < len(sf.ranges):
        lo, hi = sf.ranges[c]
        if hi > lo:
            return lo, hi, "the file's own display range"
    key = ("sample", c)
    if key not in sf._display_cache:
        import numpy as np
        ts = sorted({int(round(x)) for x in np.linspace(0, sf.T - 1, min(sf.T, 8))})
        vals = [sf.read_plane(t, sf.Z // 2, c, timeout=30.0)[::4, ::4].ravel() for t in ts]
        v = np.concatenate(vals).astype(np.float64)
        lo, hi = (float(x) for x in np.percentile(v, [0.1, 99.9]))
        sf._display_cache[key] = (lo, hi if hi > lo else lo + 1, f"sampled from {len(ts)} planes")
    return sf._display_cache[key]


class PlaneRef:
    """A frame of a stack plate, standing in for the file path a crop plate has.

    The server's caches key on `cache_id(size)`, which names the resolution LEVEL that
    will actually be read — so a frame served from the binned level while the full one
    is still being indexed is never mistaken, later, for the full-resolution frame.

    `pinned(size)` fixes that level for one request: the level is decided ONCE and
    everything after it — the cache key, the "exact" header, the pixels — uses the same
    decision. Deciding it twice raced with the background index: the header could say
    "binned stand-in" while the body was already the full-resolution plane."""

    def __init__(self, sw: StackWell, t: int, z: int, c: int, label: str = "",
                 plate_cal: Optional[dict] = None, _pin=None):
        self.sw, self.t, self.z, self.c, self.label = sw, t, z, c, label
        self.plate_cal = plate_cal
        self._pin = _pin                       # (size, level, exact) or None

    def _plane(self, lv):
        return lv.plane_index(self.t, self.z, self.c)

    def level_for(self, size: int):
        if self._pin is not None and self._pin[0] == size:
            return self._pin[1], self._pin[2]
        return self.sw.choose(size, self._plane)

    def pinned(self, size: int) -> "PlaneRef":
        lv, exact = self.level_for(size)
        return PlaneRef(self.sw, self.t, self.z, self.c, self.label, self.plate_cal,
                        _pin=(size, lv, exact))

    def cache_id(self, size: int) -> str:
        lv, _exact = self.level_for(size)
        return f"{lv.path}#t{self.t}z{self.z}c{self.c}"

    def exact(self, size: int) -> bool:
        return self.level_for(size)[1]

    def stat(self):
        return os.stat(self.sw.full.path)

    def is_file(self) -> bool:
        return all(os.path.isfile(lv.path) for lv in self.sw.levels)

    def __str__(self):
        return f"{self.sw.full.path}#t{self.t}z{self.z}c{self.c}"

    def render(self, size: int):
        """(PIL 8-bit image with long edge <= size, level used, exact).

        8-bit planes pass through unchanged at their own size; wider types go through
        `display_map`. Downsampling is an AREA MEAN computed in floating point and
        rounded ONCE — what binning is. Not a bilinear sample, which at 4x aliases the
        image into noise, and not PIL's 8-bit box filter either: that filters rows and
        then columns and rounds after each pass, which is off by up to a whole grey
        level (the law in tests/validate.py caught it)."""
        import numpy as np
        from PIL import Image
        lv, exact = self.level_for(size)
        a = lv.read_plane(self.t, self.z, self.c)
        W, H = lv.X, lv.Y
        scale = min(1.0, size / max(W, H))
        w, h = max(1, int(round(W * scale))), max(1, int(round(H * scale)))
        if a.dtype == np.uint8 and scale >= 1.0:
            return Image.fromarray(a), lv, exact
        f = a.astype(np.float32)
        if scale < 1.0:
            f = np.asarray(Image.fromarray(f).resize((w, h), Image.BOX), dtype=np.float32)
        if a.dtype != np.uint8:
            m = display_map(lv, self.c, self.plate_cal, self.label)
            lo, hi = m[0], m[1]
            f = (f - lo) * (255.0 / (hi - lo))
        return Image.fromarray(np.clip(np.rint(f), 0, 255).astype(np.uint8)), lv, exact
