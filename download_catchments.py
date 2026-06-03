"""
FROM SNOW TO RESERVOIR - HydroBASINS Catchment Delineation
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Derives the drainage basin (Einzugsgebiet) ABOVE each dam from HydroBASINS
(HydroSHEDS family, same source as HydroRIVERS). The dam is the pour-point: we
locate the sub-basin containing it and walk the basin topology upstream
(HYBAS_ID / NEXT_DOWN), exactly as download_rivers.py walks the river network,
then union all upstream sub-basins into one catchment polygon per AOI.

The catchment polygon (static_data/catchments.geojson) drives two things:
  1. its bounding box (+ small buffer) becomes the new clip_box in aoi_config.py
     -> the satellite download is trimmed to the basin instead of a coarse box;
  2. the polygon masks the analysis statistics in extract_timeseries.py, so
     snow/glacier/water are counted only inside the reservoir's drainage basin
     (e.g. the Kazbek glaciers drain north into the Terek, NOT the Zhinvali, and
     are correctly excluded).

HydroBASINS: Lehner & Grill 2013, WWF / McGill, derived from HydroSHEDS.
Free for scientific, educational and commercial use.

Output: static_data/catchments.geojson  (+ printed clip_box for aoi_config.py)

Usage:
    python download_catchments.py                 # default level, save + report
    python download_catchments.py --level 9       # pick a HydroBASINS level
    python download_catchments.py --probe         # report area/bbox for levels 7-12,
                                                  #   do not save (pick the best level)
"""

import argparse
import zipfile
from collections import defaultdict, deque
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, box as shp_box

from aoi_config import AOIS, STATIC_DIR, CATCHMENTS_GEOJSON

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

# HydroBASINS standard (with lakes) for the Europe + Middle East region (covers
# the Caucasus) - one zip holds all levels 1-12. Public download, no login.
HYBAS_URL = "https://data.hydrosheds.org/file/hydrobasins/standard/hybas_eu_lev01-12_v1c.zip"
HYBAS_ZIP = STATIC_DIR / "hybas_eu.zip"

# Finer level = tighter watershed contour but more sub-basins to union. Level 9
# resolves these dam catchments well while still snapping the dam near a
# sub-basin outlet; --probe to compare 7-12.
DEFAULT_LEVEL = 9

# Buffer (degrees) added around the catchment bbox to form the clip_box, so the
# download does not clip pixels right at the watershed edge (~0.02 deg ~= 2 km).
BBOX_BUFFER_DEG = 0.02


def hybas_shp_glob(level: int) -> str:
    return f"hybas_eu_lev{level:02d}_v1c.shp"


# ─────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────

def download_hybas(level: int) -> Path | None:
    """Download + unpack the HydroBASINS Europe zip if the level shp is missing."""
    STATIC_DIR.mkdir(exist_ok=True)

    existing = list(STATIC_DIR.rglob(hybas_shp_glob(level)))
    if existing:
        print(f"HydroBASINS lev{level:02d} already present: {existing[0]}")
        return existing[0]

    print(f"Downloading HydroBASINS EU (all levels, ~1 zip)...")
    print(f"URL: {HYBAS_URL}")
    import requests
    try:
        resp = requests.get(HYBAS_URL, stream=True, timeout=600)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(HYBAS_ZIP, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"\r  {downloaded / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print(f"\nDownload complete ({downloaded / 1e6:.1f} MB)")
    except Exception as e:
        print(f"ERROR downloading HydroBASINS: {e}")
        return None

    print("Unpacking...")
    with zipfile.ZipFile(HYBAS_ZIP, "r") as zf:
        zf.extractall(STATIC_DIR)
    HYBAS_ZIP.unlink()

    existing = list(STATIC_DIR.rglob(hybas_shp_glob(level)))
    if existing:
        print(f"Shapefile ready: {existing[0]}")
        return existing[0]
    print(f"ERROR: lev{level:02d} shapefile not found after unpack.")
    return None


# ─────────────────────────────────────────────
# DELINEATION
# ─────────────────────────────────────────────

def upstream_basins(gdf: gpd.GeoDataFrame, dam_lon: float, dam_lat: float) -> gpd.GeoDataFrame:
    """Return all sub-basins upstream of (and including) the one at the dam.

    HydroBASINS topology: each sub-basin (HYBAS_ID) flows into NEXT_DOWN (0 if
    terminal). We find the sub-basin containing the dam (the catchment outlet),
    then BFS upstream against the flow - the same walk as download_rivers.py.
    """
    dam = Point(dam_lon, dam_lat)
    containing = gdf[gdf.contains(dam)]
    if not containing.empty:
        outlet = containing.iloc[0]
    else:
        # Fall back to the nearest sub-basin (projected distance, UTM 38N).
        gdf_utm = gdf.to_crs("EPSG:32638")
        dam_utm = gpd.GeoSeries([dam], crs="EPSG:4326").to_crs("EPSG:32638").iloc[0]
        outlet = gdf.loc[gdf_utm.geometry.distance(dam_utm).idxmin()]
    outlet_id = outlet["HYBAS_ID"]

    flows_into = defaultdict(list)
    for hid, nd in zip(gdf["HYBAS_ID"], gdf["NEXT_DOWN"]):
        flows_into[nd].append(hid)

    keep = {outlet_id}
    queue = deque([outlet_id])
    while queue:
        cur = queue.popleft()
        for up in flows_into.get(cur, []):
            if up not in keep:
                keep.add(up)
                queue.append(up)

    return gdf[gdf["HYBAS_ID"].isin(keep)]


def catchment_for(gdf: gpd.GeoDataFrame, name: str, dam) -> gpd.GeoDataFrame | None:
    """Delineate, dissolve and report the catchment polygon for one AOI."""
    dam_lon, dam_lat = dam
    basins = upstream_basins(gdf, dam_lon, dam_lat)
    if basins.empty:
        print(f"  {name}: no upstream basins found")
        return None

    dissolved = basins.dissolve().reset_index(drop=True)
    dissolved["aoi"] = name
    dissolved = dissolved[["aoi", "geometry"]]

    area_km2 = dissolved.to_crs("EPSG:32638").area.iloc[0] / 1e6
    minx, miny, maxx, maxy = dissolved.total_bounds
    contains_dam = dissolved.contains(Point(dam_lon, dam_lat)).iloc[0]
    clip_box = (round(minx - BBOX_BUFFER_DEG, 4), round(miny - BBOX_BUFFER_DEG, 4),
                round(maxx + BBOX_BUFFER_DEG, 4), round(maxy + BBOX_BUFFER_DEG, 4))

    print(f"  {name}: {len(basins)} sub-basins, area {area_km2:.0f} km2, "
          f"contains dam: {contains_dam}")
    print(f"    bbox        {tuple(round(v, 4) for v in (minx, miny, maxx, maxy))}")
    print(f"    clip_box    {clip_box}   <- paste into aoi_config.py")
    return dissolved


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run_level(level: int, save: bool):
    shp = download_hybas(level)
    if not shp:
        return
    print(f"\nLevel {level:02d}: reading {shp.name}...")
    full = gpd.read_file(shp)
    if full.crs is None or full.crs.to_epsg() != 4326:
        full = full.to_crs("EPSG:4326")

    parts = []
    for name, cfg in AOIS.items():
        # Pre-filter to a generous window around the dam to speed up contains/BFS.
        dam_lon, dam_lat = cfg["dam"]
        window = shp_box(dam_lon - 1.5, dam_lat - 1.5, dam_lon + 1.5, dam_lat + 1.5)
        local = full[full.intersects(window)]
        catch = catchment_for(local, name, cfg["dam"])
        if catch is not None:
            parts.append(catch)

    if save and parts:
        combined = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")
        combined.to_file(CATCHMENTS_GEOJSON, driver="GeoJSON")
        print(f"\nSaved: {CATCHMENTS_GEOJSON} ({len(combined)} catchments, level {level:02d})")
        print("Next: update clip_box in aoi_config.py with the values above, then probe_coverage.py")


def main():
    ap = argparse.ArgumentParser(description="HydroBASINS catchment delineation")
    ap.add_argument("--level", type=int, default=DEFAULT_LEVEL,
                    help=f"HydroBASINS level 1-12 (default {DEFAULT_LEVEL})")
    ap.add_argument("--probe", action="store_true",
                    help="report area/bbox for levels 7-12 without saving")
    args = ap.parse_args()

    print("=" * 60)
    print("HydroBASINS - Catchment Delineation (Einzugsgebiet)")
    print("=" * 60)

    if args.probe:
        for lev in range(7, 13):
            run_level(lev, save=False)
    else:
        run_level(args.level, save=True)


if __name__ == "__main__":
    main()
