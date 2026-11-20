"""Gridded fields for the map overlay (temperature, wind, terrain height).

A connector returns `FetchResult.grids = {var: payload}`. The runner saves each payload here and
the API serves it as-is (gzipped JSON). Only the current and previous run are kept: each run is
its own file and `<var>.ptr.json` points at them. Grids don't
go in the observations table (millions of cells per run).

Payload contract (all keys required unless noted):

    var      "t2m" | "u10" | "v10" | "z" | ...
    unit     "degC" | "m/s" | "m"
    model    e.g. "gfs_0p25"
    run      ISO UTC of the model cycle, e.g. "2026-10-31T12:00:00Z"
    fetched  ISO UTC
    lon0, lat0   centre of the first cell (south-west corner of the grid)
    dx, dy       spacing in degrees (positive)
    nx, ny       columns (W->E), rows (S->N)
    times    list of ISO UTC valid times; [] for static fields like z
    scale, offset, nodata   value = int16 * scale + offset; int16 == nodata means missing
    data     base64 of little-endian int16, ordered [t][row S->N][col W->E] (or [row][col] if no times)
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
from pathlib import Path

from montney import paths

REQUIRED = ("var", "unit", "model", "run", "fetched", "lon0", "lat0", "dx", "dy", "nx", "ny",
            "times", "scale", "offset", "nodata", "data")
VAR_RE = re.compile(r"^[a-z0-9_]{1,32}$")


class GridError(ValueError):
    pass


def grid_dir() -> Path:
    d = paths.home() / "grids"
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate(p: dict) -> None:
    missing = [k for k in REQUIRED if k not in p]
    if missing:
        raise GridError(f"grid payload missing {missing}")
    if not VAR_RE.match(str(p["var"])):
        raise GridError(f"bad var {p['var']!r}")
    nt = max(1, len(p["times"]))
    expected = nt * int(p["nx"]) * int(p["ny"]) * 2
    got = len(base64.b64decode(p["data"]))
    if got != expected:
        raise GridError(f"{p['var']}: data is {got} bytes, expected {expected} (t*ny*nx*2)")


def _safe_name(run: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", run)[:20] or "run"


def _read_ptr(root: Path, var: str) -> dict:
    f = root / f"{var}.ptr.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save(p: dict, root: Path | None = None) -> Path:
    """Write a run to its own file, then repoint `<var>.ptr.json` at it.

    Run files are never overwritten in place (Windows refuses to replace a file that's being
    served), so a reader always sees a complete file. The pointer keeps current + previous run;
    older run files are deleted best-effort.
    """
    validate(p)
    root = root or grid_dir()
    var = p["var"]
    ptr = _read_ptr(root, var)
    # fetched is in the name too, so re-saving the same run never overwrites a file being served
    fname = f"{var}.{_safe_name(p['run'])}.{_safe_name(p['fetched'])}.json.gz"
    tmp = root / f".{fname}.tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(p, f, separators=(",", ":"))
    _replace(tmp, root / fname)
    meta = {k: p[k] for k in REQUIRED if k != "data"}
    cur = ptr.get("current")
    if cur and cur["meta"].get("run") != p["run"]:
        ptr["prev"] = cur          # a new run: the old current becomes prev
    # same run re-saved: keep prev as is; the old current file is cleaned up below
    ptr["current"] = {"file": fname, "meta": meta}
    tmp_ptr = root / f".{var}.ptr.tmp"
    tmp_ptr.write_text(json.dumps(ptr))
    _replace(tmp_ptr, root / f"{var}.ptr.json")
    keep = {ptr["current"]["file"], (ptr.get("prev") or {}).get("file")}
    for old in root.glob(f"{var}.*.json.gz"):
        if old.name not in keep:
            try:
                old.unlink()
            except OSError:
                pass  # still being served; next save retries
    return root / fname


def _replace(src: Path, dst: Path, tries: int = 5) -> None:
    import time
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.2 * (i + 1))


def path_for(var: str, prev: bool = False, root: Path | None = None) -> Path | None:
    if not VAR_RE.match(var):
        return None
    root = root or grid_dir()
    entry = _read_ptr(root, var).get("prev" if prev else "current")
    if not entry:
        return None
    f = root / entry["file"]
    return f if f.exists() else None


def catalog(root: Path | None = None) -> list[dict]:
    root = root or grid_dir()
    out = []
    for ptr_file in sorted(root.glob("*.ptr.json")):
        ptr = json.loads(ptr_file.read_text())
        for which in ("current", "prev"):
            if ptr.get(which):
                meta = dict(ptr[which]["meta"])
                meta["prev"] = which == "prev"
                meta["n_times"] = len(meta.pop("times", []))
                out.append(meta)
    return out
