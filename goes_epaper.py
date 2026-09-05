#!/usr/bin/env python3
"""
goes_epaper.py — live GOES-West imagery, chroma-boosted and dithered for the
Seeed Studio XIAO ePaper DIY Kit EE02 (13.3" Spectra 6, 1600x1200, six colours).

Pulls a borderless GOES-West GeoColor frame from CIRA SLIDER (~10-20 min old,
with NASA GIBS at ~40 min as fallback), boosts chroma and contrast for a reflective e-paper panel, error-diffusion dithers it to the six
Spectra 6 primaries, and writes three artefacts:

    out/latest_preview.png  full-colour, post-enhancement (for eyeballing on a monitor)
    out/latest.bmp          24-bit BMP containing ONLY the six palette RGBs
    out/latest.bin          packed 4bpp, two pixels per byte (high nibble = left pixel)

Run `--serve` to also expose them over HTTP so the ESP32-S3 can poll, and
`--loop` to refresh on the GOES cadence.

    pip install pillow numpy requests

    python3 goes_epaper.py --once                     # one frame, write files
    python3 goes_epaper.py --loop --serve --port 8080 # the actual deployment
    python3 goes_epaper.py --once --self-test         # no network; synthetic input

The device then fetches  http://<host>:8080/latest.bin  (940 KB, nothing to decode)
or http://<host>:8080/latest.bmp if you prefer the BMP path.
"""

import argparse
import hashlib
import io
import math
import os
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

# ----------------------------------------------------------------------------
# Panel
# ----------------------------------------------------------------------------
PANEL_W, PANEL_H = 1600, 1200          # EE02 13.3" landscape (native 1200x1600)

# Spectra 6 primaries.
#
# Two tables, deliberately. DITHER_RGB approximates what the panel's inks
# ACTUALLY reflect — muted, not saturated — so the error-diffusion maths lands
# on perceptually correct choices. OUTPUT_RGB is the canonical saturated value
# written into the BMP, so that whatever nearest-colour mapping runs on the
# device snaps to the intended ink with zero ambiguity.
#
# Order defines the palette index. Adjust INDEX_CODE if your firmware disagrees.
PALETTE = [
    # name      dither RGB (measured-ish)   output RGB (canonical)   panel nibble
    ("black",   (  0,   0,   0),            (  0,   0,   0),         0x0),
    ("white",   (255, 255, 255),            (255, 255, 255),         0x1),
    ("yellow",  (233, 200,  50),            (255, 255,   0),         0x2),
    ("red",     (180,  50,  45),            (255,   0,   0),         0x3),
    ("blue",    ( 55,  70, 150),            (  0,   0, 255),         0x5),
    ("green",   ( 70, 125,  70),            (  0, 255,   0),         0x6),
]
DITHER_RGB = [p[1] for p in PALETTE]
OUTPUT_RGB = [p[2] for p in PALETTE]
INDEX_CODE = [p[3] for p in PALETTE]

# ----------------------------------------------------------------------------
# Scene — Los Angeles centred
# ----------------------------------------------------------------------------
LAT, LON = 34.0522, -118.2437
WIDTH_KM = 600.0                        # true ground width of the frame
LAYER = "GOES-West_ABI_GeoColor"
GIBS = "https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi"
R_EARTH = 6378137.0

# ----------------------------------------------------------------------------
# Look — tuned for reflective e-paper, which is dim and low-contrast
# ----------------------------------------------------------------------------
SATURATION = 1.85    # chroma multiplier in linear light. 1.0 = untouched
CONTRAST   = 1.22    # S-curve strength around mid grey
BRIGHTNESS = 1.06    # lift, because reflective panels read darker than a monitor
SHARPEN    = 0.55    # unsharp amount; recovers edges that dithering smears


def mercator_bbox(lat, lon, width_km, aspect):
    """EPSG:3857 bbox centred on lat/lon with a true ground width of width_km."""
    span = (width_km * 1000.0) / math.cos(math.radians(lat))   # mercator inflation
    hx = span / 2.0
    hy = hx / aspect
    cx = math.radians(lon) * R_EARTH
    cy = R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return (cx - hx, cy - hy, cx + hx, cy + hy)


def gibs_url(width=PANEL_W, height=PANEL_H, layer=LAYER,
             lat=LAT, lon=LON, width_km=WIDTH_KM, time_str=None):
    bbox = mercator_bbox(lat, lon, width_km, width / height)
    params = [
        ("SERVICE", "WMS"), ("VERSION", "1.3.0"), ("REQUEST", "GetMap"),
        ("LAYERS", layer), ("CRS", "EPSG:3857"),
        ("BBOX", ",".join(f"{v:.0f}" for v in bbox)),
        ("WIDTH", str(width)), ("HEIGHT", str(height)),
        ("FORMAT", "image/png"),
    ]
    if time_str:
        params.append(("TIME", time_str))
    return GIBS + "?" + "&".join(f"{k}={v}" for k, v in params)


def fetch(url, timeout=90):
    import requests
    r = requests.get(url, timeout=timeout,
                     headers={"User-Agent": "goes-epaper/1.0"})
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


# ----------------------------------------------------------------------------
# Enhancement
# ----------------------------------------------------------------------------
def _srgb_to_linear(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055)


def enhance(img, saturation=SATURATION, contrast=CONTRAST,
            brightness=BRIGHTNESS, sharpen=SHARPEN):
    """Chroma boost + contrast, done in linear light so colours stay clean.

    Saturation is applied as a scaling of each channel away from luminance,
    which preserves hue far better than an HSV multiply and does not blow
    skies or cloud tops to pure primaries.
    """
    a = np.asarray(img, dtype=np.float32) / 255.0
    lin = _srgb_to_linear(a)

    # --- chroma ---
    lum = (lin * np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)).sum(
        axis=2, keepdims=True)
    lin = lum + (lin - lum) * saturation
    lin = np.clip(lin, 0.0, 1.0)

    out = _linear_to_srgb(lin)

    # --- brightness then S-curve contrast about mid grey ---
    out = np.clip(out * brightness, 0.0, 1.0)
    out = np.clip((out - 0.5) * contrast + 0.5, 0.0, 1.0)

    img = Image.fromarray((out * 255.0 + 0.5).astype(np.uint8), "RGB")

    # --- unsharp: dithering eats fine structure, so pre-compensate ---
    if sharpen > 0:
        from PIL import ImageFilter
        img = img.filter(ImageFilter.UnsharpMask(
            radius=2, percent=int(sharpen * 100), threshold=2))
    return img


# ----------------------------------------------------------------------------
# Dither
# ----------------------------------------------------------------------------
def _palette_image(rgb_list):
    pal = Image.new("P", (1, 1))
    flat = []
    for rgb in rgb_list:
        flat.extend(rgb)
    flat.extend([0, 0, 0] * (256 - len(rgb_list)))
    pal.putpalette(flat)
    return pal


def dither(img, dither_rgb=DITHER_RGB):
    """Floyd-Steinberg to the six primaries. Returns a uint8 index array."""
    pal = _palette_image(dither_rgb)
    q = img.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG)
    idx = np.asarray(q, dtype=np.uint8)
    # quantize can emit indices past our palette if Pillow pads; clamp defensively
    return np.clip(idx, 0, len(dither_rgb) - 1)


def indices_to_rgb(idx, output_rgb=OUTPUT_RGB):
    lut = np.array(output_rgb, dtype=np.uint8)
    return Image.fromarray(lut[idx], "RGB")


def pack_4bpp(idx, index_code=INDEX_CODE):
    """Two pixels per byte, high nibble = left pixel."""
    lut = np.array(index_code, dtype=np.uint8)
    codes = lut[idx]
    h, w = codes.shape
    if w % 2:
        codes = np.pad(codes, ((0, 0), (0, 1)), constant_values=index_code[1])
    hi = codes[:, 0::2]
    lo = codes[:, 1::2]
    return ((hi << 4) | lo).astype(np.uint8).tobytes()


# ----------------------------------------------------------------------------
# Synthetic input, so the pipeline is testable with no network
# ----------------------------------------------------------------------------
def synthetic(w=PANEL_W, h=PANEL_H):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = xx / w, yy / h
    ocean = np.stack([0.06 + 0.05 * v, 0.16 + 0.10 * v, 0.34 + 0.14 * u], -1)
    land = np.stack([0.42 + 0.18 * u, 0.36 + 0.10 * v, 0.18 + 0.05 * u], -1)
    mask = (v > 0.35 + 0.18 * np.sin(u * 5.0)).astype(np.float32)[..., None]
    base = ocean * (1 - mask) + land * mask
    swirl = np.exp(-(((u - 0.55) ** 2 + (v - 0.42) ** 2) / 0.02))
    band = 0.5 + 0.5 * np.sin(28 * (u + v) + 6 * swirl)
    cloud = np.clip(swirl * band * 1.5, 0, 1)[..., None]
    img = np.clip(base * (1 - cloud) + cloud * 0.95, 0, 1)
    return Image.fromarray((img * 255).astype(np.uint8), "RGB")


# ----------------------------------------------------------------------------
# Frame production
# ----------------------------------------------------------------------------
def acquire(source="slider"):
    """Get the freshest frame available. Returns (image, description).

    SLIDER first: CIRA publishes GeoColor within ~10-20 minutes, where GIBS
    carries roughly 40 minutes of processing latency. GIBS stays as the
    fallback because it is a plain WMS call with no projection work, so it
    keeps the panel fed if CIRA changes its tile layout.
    """
    if source == "slider":
        try:
            import slider_source
            img, ts = slider_source.fetch_region(
                LAT, LON, WIDTH_KM, PANEL_W, PANEL_H)
            pretty = (f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} "
                      f"{ts[8:10]}:{ts[10:12]}:{ts[12:14]}Z")
            return img, f"SLIDER full_disk {pretty}"
        except Exception as e:
            print(f"    !! SLIDER failed ({type(e).__name__}: {e}); "
                  f"falling back to GIBS", file=sys.stderr)
    url = gibs_url()
    return fetch(url), "GIBS " + url


def produce(outdir, self_test=False, source="slider", **look):
    os.makedirs(outdir, exist_ok=True)
    if self_test:
        src = synthetic()
        origin = "synthetic self-test frame"
    else:
        src, origin = acquire(source)

    if src.size != (PANEL_W, PANEL_H):
        src = src.resize((PANEL_W, PANEL_H), Image.LANCZOS)

    boosted = enhance(src, **look)
    idx = dither(boosted)
    flat = indices_to_rgb(idx)
    blob = pack_4bpp(idx)

    boosted.save(os.path.join(outdir, "latest_preview.png"))
    flat.save(os.path.join(outdir, "latest.bmp"))
    with open(os.path.join(outdir, "latest.bin"), "wb") as f:
        f.write(blob)
    digest = hashlib.sha256(blob).hexdigest()[:16]
    with open(os.path.join(outdir, "latest.sha"), "w") as f:
        f.write(digest + "\n")

    counts = np.bincount(idx.ravel(), minlength=len(PALETTE))
    mix = ", ".join(f"{PALETTE[i][0]} {100*c/idx.size:.1f}%"
                    for i, c in enumerate(counts))
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {origin}")
    print(f"    {PANEL_W}x{PANEL_H}  bin={len(blob)}B  sha={digest}")
    print(f"    palette mix: {mix}")
    return digest


def serve(outdir, port):
    handler = partial(SimpleHTTPRequestHandler, directory=outdir)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"    serving {outdir} on http://0.0.0.0:{port}/  "
          f"(latest.bin, latest.bmp, latest.sha)")
    return httpd


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--outdir", default="out")
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=600, help="seconds (default 600)")
    p.add_argument("--serve", action="store_true")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--self-test", action="store_true",
                   help="use a synthetic frame instead of the network")
    p.add_argument("--source", choices=["slider", "gibs"], default="slider",
                   help="slider = CIRA, ~10-20 min old (default); "
                        "gibs = NASA, ~40 min old but simpler")
    p.add_argument("--saturation", type=float, default=SATURATION)
    p.add_argument("--contrast", type=float, default=CONTRAST)
    p.add_argument("--brightness", type=float, default=BRIGHTNESS)
    p.add_argument("--sharpen", type=float, default=SHARPEN)
    p.add_argument("--print-url", action="store_true")
    a = p.parse_args()

    if a.print_url:
        print(gibs_url())
        return

    look = dict(saturation=a.saturation, contrast=a.contrast,
                brightness=a.brightness, sharpen=a.sharpen)

    if a.serve:
        os.makedirs(a.outdir, exist_ok=True)
        serve(a.outdir, a.port)

    if a.loop:
        while True:
            try:
                produce(a.outdir, a.self_test, a.source, **look)
            except Exception as e:                      # keep the frame server up
                print(f"    !! {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(a.interval)
    else:
        produce(a.outdir, a.self_test, a.source, **look)
        if a.serve:
            print("    --serve without --loop; Ctrl-C to stop")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
