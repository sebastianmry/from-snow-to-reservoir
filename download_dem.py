"""
FROM SNOW TO RESERVOIR - Copernicus DEM GLO-30 Download & Clip
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Downloads the Copernicus DEM GLO-30 (30 m global digital elevation model)
1-degree tiles covering each AOI from the public AWS Open Data bucket
(no login), merges them and clips to the AOI bbox.

The DEM is the elevation reference for the reservoir WATER LEVEL step
(extract_timeseries.py): at the water's edge the surface elevation equals the
shoreline's DEM height (INFLOS approach, Poterek et al. 2025), so per date the
S1 shoreline is intersected with this DEM to read the lake level (m a.s.l.).

Source: Copernicus DEM GLO-30, ESA / Airbus, via AWS Open Data
(s3://copernicus-dem-30m, public). Tiles are EPSG:4326 COGs, 1 deg x 1 deg.

Output: static_data/{aoi}_dem.tif

Run once before the water-level run of extract_timeseries.py:
    python download_dem.py
"""

import math
from pathlib import Path

import requests
import rioxarray  # noqa: F401  (registers .rio accessor)
from rioxarray.merge import merge_arrays

STATIC_DIR = Path("static_data")
TILE_DIR = STATIC_DIR / "dem_tiles"

# AOI bbox (min_lon, min_lat, max_lon, max_lat)
AOIS = {
    "enguri":   (41.70, 42.55, 42.80, 43.15),
    "zhinvali": (44.30, 42.00, 45.15, 42.80),
}

BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"


def tile_name(lat: int, lon: int) -> str:
    """COG tile folder/file stem for the integer SW corner (GLO-30 = COG_10)."""
    ns = f"N{lat:02d}_00" if lat >= 0 else f"S{abs(lat):02d}_00"
    ew = f"E{lon:03d}_00" if lon >= 0 else f"W{abs(lon):03d}_00"
    return f"Copernicus_DSM_COG_10_{ns}_{ew}_DEM"


def tiles_for_bbox(bbox: tuple) -> list[tuple[int, int]]:
    """Integer (lat, lon) SW corners of the 1-degree tiles covering the bbox."""
    min_lon, min_lat, max_lon, max_lat = bbox
    lats = range(math.floor(min_lat), math.floor(max_lat) + 1)
    lons = range(math.floor(min_lon), math.floor(max_lon) + 1)
    return [(lat, lon) for lat in lats for lon in lons]


def download_tile(lat: int, lon: int) -> Path | None:
    """Download one GLO-30 tile to TILE_DIR (skip if present)."""
    TILE_DIR.mkdir(parents=True, exist_ok=True)
    stem = tile_name(lat, lon)
    dst = TILE_DIR / f"{stem}.tif"
    if dst.exists():
        return dst
    url = f"{BUCKET}/{stem}/{stem}.tif"
    print(f"  downloading {stem} ...", end=" ", flush=True)
    try:
        r = requests.get(url, stream=True, timeout=600)
        if r.status_code != 200:
            print(f"HTTP {r.status_code} (tile may not exist over water) - skipped")
            return None
        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                f.write(chunk)
        print(f"{dst.stat().st_size / 1e6:.0f} MB")
        return dst
    except Exception as e:
        print(f"ERROR {e}")
        return None


def build_aoi_dem(name: str, bbox: tuple):
    """Download the covering tiles, clip each to the AOI, merge, save."""
    print(f"\n--- {name} ---")
    pieces = []
    for lat, lon in tiles_for_bbox(bbox):
        path = download_tile(lat, lon)
        if path is None:
            continue
        da = rioxarray.open_rasterio(path).load()
        # Clip each tile to its intersection with the AOI before merging
        # (keeps memory low; tiles outside the bbox contribute nothing).
        try:
            da = da.rio.clip_box(*bbox)
        except Exception:
            continue  # tile does not actually overlap the bbox
        pieces.append(da)

    if not pieces:
        print(f"  no DEM tiles for {name}")
        return

    merged = pieces[0] if len(pieces) == 1 else merge_arrays(pieces)
    merged = merged.rio.clip_box(*bbox)
    out = STATIC_DIR / f"{name}_dem.tif"
    merged.rio.to_raster(out)
    h, w = merged.values[0].shape
    print(f"  saved {out}  ({w}x{h} px, "
          f"elev {float(merged.min()):.0f}-{float(merged.max()):.0f} m)")


def main():
    print("=" * 55)
    print("Copernicus DEM GLO-30 - Download & Clip")
    print("=" * 55)
    for name, bbox in AOIS.items():
        build_aoi_dem(name, bbox)
    print("\nReady for extract_timeseries.py (reservoir water level)")


if __name__ == "__main__":
    main()
