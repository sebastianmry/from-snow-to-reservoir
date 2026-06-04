"""
FROM SNOW TO RESERVOIR - HydroBASINS Catchment Delineation
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Derives the drainage basin ABOVE each dam from HydroBASINS
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

import requests
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

    print("Downloading HydroBASINS EU (all levels, ~1 zip)...")
    print(f"URL: {HYBAS_URL}")
    try:
        response = requests.get(HYBAS_URL, stream=True, timeout=600)
        response.raise_for_status()
        total_bytes = int(response.headers.get("content-length", 0))
        downloaded_bytes = 0
        with HYBAS_ZIP.open("wb") as out_file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                out_file.write(chunk)
                downloaded_bytes += len(chunk)
                if total_bytes:
                    print(f"\r  {downloaded_bytes / 1e6:.1f} / {total_bytes / 1e6:.1f} MB",
                          end="", flush=True)
        print(f"\nDownload complete ({downloaded_bytes / 1e6:.1f} MB)")
    except Exception as error:
        print(f"ERROR downloading HydroBASINS: {error}")
        return None

    print("Unpacking...")
    with zipfile.ZipFile(HYBAS_ZIP, "r") as archive:
        archive.extractall(STATIC_DIR)
    HYBAS_ZIP.unlink()

    shp_paths = list(STATIC_DIR.rglob(hybas_shp_glob(level)))
    if shp_paths:
        print(f"Shapefile ready: {shp_paths[0]}")
        return shp_paths[0]
    print(f"ERROR: lev{level:02d} shapefile not found after unpack.")
    return None


# ─────────────────────────────────────────────
# DELINEATION
# ─────────────────────────────────────────────

def upstream_basins(basins_gdf: gpd.GeoDataFrame, dam_lon: float, dam_lat: float) -> gpd.GeoDataFrame:
    """Return all sub-basins upstream of (and including) the one at the dam.

    HydroBASINS topology: each sub-basin (HYBAS_ID) flows into NEXT_DOWN (0 if
    terminal). We find the sub-basin containing the dam (the catchment outlet),
    then BFS upstream against the flow - the same walk as download_rivers.py.
    """
    dam_point = Point(dam_lon, dam_lat)
    containing_gdf = basins_gdf[basins_gdf.contains(dam_point)]
    if not containing_gdf.empty:
        outlet = containing_gdf.iloc[0]
    else:
        # Fall back to the nearest sub-basin (projected distance, UTM 38N).
        basins_utm = basins_gdf.to_crs("EPSG:32638")
        dam_utm = gpd.GeoSeries([dam_point], crs="EPSG:4326").to_crs("EPSG:32638").iloc[0]
        outlet = basins_gdf.loc[basins_utm.geometry.distance(dam_utm).idxmin()]
    outlet_id = outlet["HYBAS_ID"]

    flows_into = defaultdict(list)
    for basin_id, next_down_id in zip(basins_gdf["HYBAS_ID"], basins_gdf["NEXT_DOWN"]):
        flows_into[next_down_id].append(basin_id)

    upstream_ids = {outlet_id}
    queue = deque([outlet_id])
    while queue:
        current_id = queue.popleft()
        for upstream_id in flows_into.get(current_id, []):
            if upstream_id not in upstream_ids:
                upstream_ids.add(upstream_id)
                queue.append(upstream_id)

    return basins_gdf[basins_gdf["HYBAS_ID"].isin(upstream_ids)]


def catchment_for(basins_gdf: gpd.GeoDataFrame, name: str, dam) -> gpd.GeoDataFrame | None:
    """Delineate, dissolve and report the catchment polygon for one AOI."""
    dam_lon, dam_lat = dam
    basins = upstream_basins(basins_gdf, dam_lon, dam_lat)
    if basins.empty:
        print(f"  {name}: no upstream basins found")
        return None

    dissolved_gdf = basins.dissolve().reset_index(drop=True)
    dissolved_gdf["aoi"] = name
    dissolved_gdf = dissolved_gdf[["aoi", "geometry"]]

    area_km2 = dissolved_gdf.to_crs("EPSG:32638").area.iloc[0] / 1e6
    minx, miny, maxx, maxy = dissolved_gdf.total_bounds
    contains_dam = dissolved_gdf.contains(Point(dam_lon, dam_lat)).iloc[0]
    clip_box = (round(minx - BBOX_BUFFER_DEG, 4), round(miny - BBOX_BUFFER_DEG, 4),
                round(maxx + BBOX_BUFFER_DEG, 4), round(maxy + BBOX_BUFFER_DEG, 4))

    print(f"  {name}: {len(basins)} sub-basins, area {area_km2:.0f} km2, "
          f"contains dam: {contains_dam}")
    print(f"    bbox        {tuple(round(v, 4) for v in (minx, miny, maxx, maxy))}")
    print(f"    clip_box    {clip_box}   <- paste into aoi_config.py")
    return dissolved_gdf


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run_level(level: int, save: bool):
    shp_path = download_hybas(level)
    if not shp_path:
        return
    print(f"\nLevel {level:02d}: reading {shp_path.name}...")
    full_gdf = gpd.read_file(shp_path)
    if full_gdf.crs is None or full_gdf.crs.to_epsg() != 4326:
        full_gdf = full_gdf.to_crs("EPSG:4326")

    catchment_parts = []
    for name, aoi_cfg in AOIS.items():
        # Pre-filter to a generous window around the dam to speed up contains/BFS.
        dam_lon, dam_lat = aoi_cfg["dam"]
        window = shp_box(dam_lon - 1.5, dam_lat - 1.5, dam_lon + 1.5, dam_lat + 1.5)
        local_gdf = full_gdf[full_gdf.intersects(window)]
        catchment_gdf = catchment_for(local_gdf, name, aoi_cfg["dam"])
        if catchment_gdf is not None:
            catchment_parts.append(catchment_gdf)

    if save and catchment_parts:
        combined_gdf = gpd.GeoDataFrame(pd.concat(catchment_parts, ignore_index=True),
                                        crs="EPSG:4326")
        combined_gdf.to_file(CATCHMENTS_GEOJSON, driver="GeoJSON")
        print(f"\nSaved: {CATCHMENTS_GEOJSON} ({len(combined_gdf)} catchments, level {level:02d})")
        print("Next: update clip_box in aoi_config.py with the values above, then probe_coverage.py")


def main():
    parser = argparse.ArgumentParser(description="HydroBASINS catchment delineation")
    parser.add_argument("--level", type=int, default=DEFAULT_LEVEL,
                        help=f"HydroBASINS level 1-12 (default {DEFAULT_LEVEL})")
    parser.add_argument("--probe", action="store_true",
                        help="report area/bbox for levels 7-12 without saving")
    args = parser.parse_args()

    print("=" * 60)
    print("HydroBASINS - Catchment Delineation")
    print("=" * 60)

    if args.probe:
        for level in range(7, 13):
            run_level(level, save=False)
    else:
        run_level(args.level, save=True)


if __name__ == "__main__":
    main()
