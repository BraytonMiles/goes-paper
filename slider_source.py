#!/usr/bin/env python3
"""
slider_source.py — low-latency GOES-West frames from CIRA SLIDER.

Why this exists: NASA GIBS carries roughly 40 minutes of processing latency on
GeoColor. CIRA publishes the same CIRA-produced GeoColor within ~10-20 minutes,
and serves it as raw borderless tiles.

The catch is that SLIDER tiles are in the satellite's own fixed-grid
(geostationary) projection, not Web Mercator, so a naive crop would be both
skewed and in the wrong place. This module does a proper inverse warp: for every
output pixel it computes lat/lon, forward-projects that through the GOES-R
geostationary model to a source scan-angle pixel, and bilinearly samples. The
result is north-up and drop-in compatible with the GIBS path.

Projection constants are the published GOES-R ABI fixed-grid values, and the
maths is checked against the one point whose answer is known exactly: the
subsatellite point must land dead centre of the full disk.

    python3 slider_source.py --selftest      # offline; no network needed
"""

import io
import json
import math
import re

import numpy as np
from PIL import Image

# --------------------------------------------------------------------------
# GOES-R ABI fixed grid, full disk, 1 km  (GOES-R Product User Guide vol.3)
# --------------------------------------------------------------------------
FD_SIZE   = 10848            # pixels per side at 1 km
FD_SCALE  = 2.8e-05          # radians per pixel
FD_OFF    = 0.151844         # |scan angle| at the first pixel centre
SAT_LON   = -137.0           # GOES-18, operational GOES-West
SAT_H     = 42164160.0       # satellite distance from Earth centre, metres
R_EQ      = 6378137.0
R_POL     = 6356752.31414

# SLIDER tiling, verified by probing: full_disk geocolor tops out at zoom 4,
# a 16x16 grid of 678 px tiles -> 16 * 678 = 10848, exactly the 1 km grid.
FD_ZOOM   = 4
FD_TILES  = 16
TILE_PX   = 678

BASE = "https://slider.cira.colostate.edu"
SAT  = "goes-18"
SECTOR = "full_disk"
PRODUCT = "geocolor"

# Tile files are named {row}_{col}.png. If a fetched frame ever comes back
# transposed or showing open ocean where land belongs, flip this once.
TILE_ROW_FIRST = True


# --------------------------------------------------------------------------
# Geostationary projection
# --------------------------------------------------------------------------
def geos_forward(lat_deg, lon_deg):
    """lat/lon -> (col, row) in the full-disk fixed grid. Accepts arrays.

    Returns (col, row, visible). Points over the Earth's limb come back with
    visible=False and meaningless col/row.
    """
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    lam0 = math.radians(SAT_LON)

    e2 = 1.0 - (R_POL ** 2) / (R_EQ ** 2)

    # geodetic -> geocentric latitude
    phi_c = np.arctan((R_POL ** 2 / R_EQ ** 2) * np.tan(lat))
    r_c = R_POL / np.sqrt(1.0 - e2 * np.cos(phi_c) ** 2)

    dlon = lon - lam0
    s_x = SAT_H - r_c * np.cos(phi_c) * np.cos(dlon)
    s_y = -r_c * np.cos(phi_c) * np.sin(dlon)
    s_z = r_c * np.sin(phi_c)

    # limb test: beyond this the ray misses the ellipsoid
    visible = (SAT_H * (SAT_H - s_x)) >= (s_y ** 2 + (R_EQ ** 2 / R_POL ** 2) * s_z ** 2)

    norm = np.sqrt(s_x ** 2 + s_y ** 2 + s_z ** 2)
    y = np.arctan(s_z / s_x)                      # N/S elevation angle
    x = np.arcsin(np.clip(-s_y / norm, -1.0, 1.0))  # E/W scanning angle

    col = (x + FD_OFF) / FD_SCALE
    row = (FD_OFF - y) / FD_SCALE
    return col, row, visible


def geos_inverse(col, row):
    """(col, row) -> (lat, lon) in degrees. Inverse of geos_forward."""
    x = np.asarray(col, dtype=np.float64) * FD_SCALE - FD_OFF
    y = FD_OFF - np.asarray(row, dtype=np.float64) * FD_SCALE
    lam0 = math.radians(SAT_LON)

    a = np.sin(x) ** 2 + np.cos(x) ** 2 * (
        np.cos(y) ** 2 + (R_EQ ** 2 / R_POL ** 2) * np.sin(y) ** 2)
    b = -2.0 * SAT_H * np.cos(x) * np.cos(y)
    c = SAT_H ** 2 - R_EQ ** 2

    disc = b ** 2 - 4 * a * c
    with np.errstate(invalid="ignore"):
        r_s = (-b - np.sqrt(disc)) / (2 * a)

    s_x = r_s * np.cos(x) * np.cos(y)
    s_y = -r_s * np.sin(x)
    s_z = r_s * np.cos(x) * np.sin(y)

    lat = np.arctan((R_EQ ** 2 / R_POL ** 2) * s_z /
                    np.sqrt((SAT_H - s_x) ** 2 + s_y ** 2))
    lon = lam0 - np.arctan(s_y / (SAT_H - s_x))
    return np.degrees(lat), np.degrees(lon)


# --------------------------------------------------------------------------
# Mercator helpers (match the GIBS path so output framing is identical)
# --------------------------------------------------------------------------
def _mercator_grid(lat_c, lon_c, width_km, out_w, out_h):
    """Lat/lon of every output pixel for a north-up Web Mercator frame."""
    span = (width_km * 1000.0) / math.cos(math.radians(lat_c))
    hx = span / 2.0
    hy = hx * (out_h / out_w)
    cx = math.radians(lon_c) * R_EQ
    cy = R_EQ * math.log(math.tan(math.pi / 4 + math.radians(lat_c) / 2))

    xs = np.linspace(cx - hx, cx + hx, out_w)
    ys = np.linspace(cy + hy, cy - hy, out_h)          # top row first
    X, Y = np.meshgrid(xs, ys)

    lon = np.degrees(X / R_EQ)
    lat = np.degrees(2.0 * np.arctan(np.exp(Y / R_EQ)) - math.pi / 2)
    return lat, lon


# --------------------------------------------------------------------------
# Tiles
# --------------------------------------------------------------------------
def latest_timestamp(session=None):
    """Newest frame CIRA has published, as a YYYYMMDDHHMMSS string."""
    import requests
    s = session or requests
    url = f"{BASE}/data/json/{SAT}/{SECTOR}/{PRODUCT}/latest_times.json"
    r = s.get(url, timeout=30, headers={"User-Agent": "goes-epaper/1.0"})
    r.raise_for_status()
    times = json.loads(r.text).get("timestamps_int", [])
    if not times:
        raise RuntimeError("SLIDER returned no timestamps")
    return str(max(int(t) for t in times))


def _tile_url(ts, trow, tcol):
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", ts)
    y, mo, d = m.groups()
    a, b = (trow, tcol) if TILE_ROW_FIRST else (tcol, trow)
    return (f"{BASE}/data/imagery/{y}/{mo}/{d}/{SAT}---{SECTOR}/{PRODUCT}/"
            f"{ts}/{FD_ZOOM:02d}/{a:03d}_{b:03d}.png")


def _fetch_mosaic(ts, c0, c1, r0, r1, session=None):
    """Fetch only the tiles covering [c0,c1)x[r0,r1); return (array, origin)."""
    import requests
    s = session or requests

    tc0, tc1 = c0 // TILE_PX, (c1 - 1) // TILE_PX
    tr0, tr1 = r0 // TILE_PX, (r1 - 1) // TILE_PX
    tc0, tr0 = max(tc0, 0), max(tr0, 0)
    tc1, tr1 = min(tc1, FD_TILES - 1), min(tr1, FD_TILES - 1)

    h = (tr1 - tr0 + 1) * TILE_PX
    w = (tc1 - tc0 + 1) * TILE_PX
    mosaic = np.zeros((h, w, 3), dtype=np.uint8)

    for tr in range(tr0, tr1 + 1):
        for tc in range(tc0, tc1 + 1):
            url = _tile_url(ts, tr, tc)
            resp = s.get(url, timeout=60, headers={"User-Agent": "goes-epaper/1.0"})
            resp.raise_for_status()
            tile = np.asarray(Image.open(io.BytesIO(resp.content)).convert("RGB"))
            oy = (tr - tr0) * TILE_PX
            ox = (tc - tc0) * TILE_PX
            mosaic[oy:oy + tile.shape[0], ox:ox + tile.shape[1]] = tile

    return mosaic, (tc0 * TILE_PX, tr0 * TILE_PX)


def _bilinear(src, cols, rows):
    h, w = src.shape[:2]
    c0 = np.floor(cols).astype(np.int64)
    r0 = np.floor(rows).astype(np.int64)
    fc = (cols - c0)[..., None]
    fr = (rows - r0)[..., None]
    c0 = np.clip(c0, 0, w - 2)
    r0 = np.clip(r0, 0, h - 2)

    a = src[r0, c0].astype(np.float32)
    b = src[r0, c0 + 1].astype(np.float32)
    c = src[r0 + 1, c0].astype(np.float32)
    d = src[r0 + 1, c0 + 1].astype(np.float32)
    top = a + (b - a) * fc
    bot = c + (d - c) * fc
    return np.clip(top + (bot - top) * fr, 0, 255).astype(np.uint8)


def fetch_region(lat, lon, width_km, out_w, out_h, session=None):
    """A north-up Web Mercator frame from the newest SLIDER full disk."""
    ts = latest_timestamp(session)

    glat, glon = _mercator_grid(lat, lon, width_km, out_w, out_h)
    cols, rows, visible = geos_forward(glat, glon)
    if not visible.all():
        raise RuntimeError("requested region is partly over the Earth's limb")

    c0 = int(math.floor(cols.min())) - 2
    c1 = int(math.ceil(cols.max())) + 2
    r0 = int(math.floor(rows.min())) - 2
    r1 = int(math.ceil(rows.max())) + 2

    mosaic, (ox, oy) = _fetch_mosaic(ts, c0, c1, r0, r1, session)
    px = _bilinear(mosaic, cols - ox, rows - oy)
    return Image.fromarray(px, "RGB"), ts


# --------------------------------------------------------------------------
# Offline checks
# --------------------------------------------------------------------------
def selftest():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")

    # 1. The subsatellite point must land exactly at the centre of the disk.
    c, r, vis = geos_forward(0.0, SAT_LON)
    centre = (FD_SIZE - 1) / 2.0
    check("nadir maps to disk centre", abs(c - centre) < 1.0 and abs(r - centre) < 1.0,
          f"col={float(c):.2f} row={float(r):.2f} (want {centre:.2f})")
    check("nadir is visible", bool(vis))

    # 2. Forward then inverse must return the original coordinates.
    pts = [(34.0522, -118.2437), (0.0, -137.0), (45.0, -100.0), (-20.0, -160.0)]
    worst = 0.0
    for la, lo in pts:
        c, r, v = geos_forward(la, lo)
        la2, lo2 = geos_inverse(c, r)
        worst = max(worst, abs(float(la2) - la), abs(float(lo2) - lo))
    check("forward/inverse round trip", worst < 1e-6, f"max err {worst:.2e} deg")

    # 3. Points beyond the limb must be rejected, not silently wrapped.
    _, _, v = geos_forward(0.0, 40.0)          # opposite side of the planet
    check("limb rejection", not bool(v))

    # 4. Geometry sanity: east of nadir must increase column, north must
    #    decrease row.
    ce, _, _ = geos_forward(0.0, -130.0)
    cw, _, _ = geos_forward(0.0, -144.0)
    _, rn, _ = geos_forward(10.0, -137.0)
    _, rs, _ = geos_forward(-10.0, -137.0)
    check("east increases column", float(ce) > float(cw))
    check("north decreases row", float(rn) < float(rs))

    # 5. Los Angeles should sit well inside the disk, and the tile window for a
    #    600 km frame should be small enough to be cheap.
    lat, lon = _mercator_grid(34.0522, -118.2437, 600.0, 1600, 1200)
    cols, rows, vis = geos_forward(lat, lon)
    check("LA frame fully visible", bool(vis.all()))
    span_c = cols.max() - cols.min()
    span_r = rows.max() - rows.min()
    check("LA frame lands inside the grid",
          0 < cols.min() and cols.max() < FD_SIZE and
          0 < rows.min() and rows.max() < FD_SIZE,
          f"col {cols.min():.0f}-{cols.max():.0f} row {rows.min():.0f}-{rows.max():.0f}")

    tc = int(cols.max()) // TILE_PX - int(cols.min()) // TILE_PX + 1
    tr = int(rows.max()) // TILE_PX - int(rows.min()) // TILE_PX + 1
    check("tile fetch is cheap", tc * tr <= 6,
          f"{tc}x{tr} = {tc*tr} tiles, source span {span_c:.0f}x{span_r:.0f} px")

    # 6. Effective ground resolution over LA, given the off-nadir view.
    gsd_km = 600.0 / span_c
    check("plausible ground sampling", 0.8 < gsd_km < 3.0, f"{gsd_km:.2f} km/px")

    # 7. A synthetic source whose pixels encode their own coordinates must warp
    #    back to those same coordinates — end-to-end proof of the resampler.
    c0, c1 = int(cols.min()) - 2, int(cols.max()) + 3
    r0, r1 = int(rows.min()) - 2, int(rows.max()) + 3
    yy, xx = np.mgrid[r0:r1, c0:c1].astype(np.float32)
    synth = np.zeros((r1 - r0, c1 - c0, 3), dtype=np.uint8)
    synth[..., 0] = (xx % 255).astype(np.uint8)
    synth[..., 1] = (yy % 255).astype(np.uint8)
    got = _bilinear(synth, cols - c0, rows - r0)
    want_r = (cols % 255).astype(np.uint8)
    err = np.abs(got[..., 0].astype(int) - want_r.astype(int))
    err = np.minimum(err, 255 - err)            # ignore the modulo wrap seam
    check("resampler samples the right pixels", err.mean() < 1.0,
          f"mean err {err.mean():.3f} counts")

    print("\n" + ("all checks passed" if ok else "FAILURES PRESENT"))
    return ok


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
