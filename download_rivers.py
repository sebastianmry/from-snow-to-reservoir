"""
FROM SNOW TO RESERVOIR - HydroRIVERS Download & Clip
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Downloads HydroRIVERS v10 (Europe and Middle East) from HydroSHEDS,
clips it to the two AOI bounding boxes, and saves a compact GeoJSON
for the Streamlit dashboard.

HydroRIVERS: WWF / McGill University, derived from HydroSHEDS (SRTM 15 arc-sec).
Free for scientific, educational and commercial use.

Output: static_data/georgia_rivers.geojson

Run once before app.py:
    python download_rivers.py
"""

import zipfile
from collections import defaultdict, deque
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, box as shp_box
import pandas as pd
import requests

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

STATIC_DIR = Path("static_data")

# HydroRIVERS v10 Europe and Middle East (covers the Caucasus) - public download
RIVERS_URL  = "https://data.hydrosheds.org/file/HydroRIVERS/HydroRIVERS_v10_eu_shp.zip"
RIVERS_ZIP  = STATIC_DIR / "hydrorivers_eu.zip"
RIVERS_SHP_GLOB = "HydroRIVERS_v10_eu.shp"

OUTPUT_GEOJSON = STATIC_DIR / "georgia_rivers.geojson"

# AOI bbox (min_lon, min_lat, max_lon, max_lat) + dam point (lon, lat) come from
# aoi_config.py. The dam point defines the catchment outlet: only rivers UPSTREAM
# of it (i.e. that feed the reservoir) are kept.
from aoi_config import AOIS

# ORD_FLOW is the logarithmic flow-order class (lower value = larger river).
# Keep classes <= this threshold. 8 includes the small glacial-fed brooks
# (drawn thin in the dashboard, see _river_weight); only the very smallest
# headwater trickles (9+) are dropped. The upstream-of-dam filter already
# restricts to the reservoir catchment, i.e. segments at or above reservoir
# elevation.
MAX_FLOW_ORDER = 8


# ─────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────

def download_rivers_shp() -> Path | None:
    """Download and unpack HydroRIVERS Europe shapefile if not present."""
    STATIC_DIR.mkdir(exist_ok=True)

    existing = list(STATIC_DIR.rglob(RIVERS_SHP_GLOB))
    if existing:
        print(f"HydroRIVERS shapefile already present: {existing[0]}")
        return existing[0]

    print("Downloading HydroRIVERS v10 Europe (~68 MB)...")
    print(f"URL: {RIVERS_URL}")

    try:
        response = requests.get(RIVERS_URL, stream=True, timeout=600)
        response.raise_for_status()
        total_bytes = int(response.headers.get("content-length", 0))
        downloaded_bytes = 0
        with RIVERS_ZIP.open("wb") as out_file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                out_file.write(chunk)
                downloaded_bytes += len(chunk)
                if total_bytes:
                    print(f"\r  {downloaded_bytes / 1e6:.1f} / {total_bytes / 1e6:.1f} MB",
                          end="", flush=True)
        print(f"\nDownload complete ({downloaded_bytes / 1e6:.1f} MB)")
    except Exception as error:
        print(f"ERROR downloading HydroRIVERS: {error}")
        return None

    print("Unpacking...")
    with zipfile.ZipFile(RIVERS_ZIP, "r") as archive:
        archive.extractall(STATIC_DIR)
    RIVERS_ZIP.unlink()

    shp_paths = list(STATIC_DIR.rglob(RIVERS_SHP_GLOB))
    if shp_paths:
        print(f"Shapefile ready: {shp_paths[0]}")
        return shp_paths[0]

    print("ERROR: Shapefile not found after unpack.")
    return None


# ─────────────────────────────────────────────
# CLIP & EXPORT
# ─────────────────────────────────────────────

def upstream_of_dam(rivers_gdf: gpd.GeoDataFrame, dam_lon: float, dam_lat: float) -> gpd.GeoDataFrame:
    """Keep only river segments upstream of the dam (the reservoir catchment).

    Uses the HydroRIVERS flow topology: every segment (HYRIV_ID) flows into the
    segment given by NEXT_DOWN. We locate the segment nearest the dam, then walk
    the network upstream (against the flow) and keep everything that drains to it.
    """
    if "HYRIV_ID" not in rivers_gdf.columns or "NEXT_DOWN" not in rivers_gdf.columns:
        print("  (no flow topology fields - skipping upstream filter)")
        return rivers_gdf

    # Segment nearest the dam = catchment outlet. Compute the distance on a
    # projected CRS (UTM 38N) - distance() on a geographic CRS warns and is
    # inaccurate. The index maps back to the original (4326) gdf.
    dam_point = Point(dam_lon, dam_lat)
    rivers_utm = rivers_gdf.to_crs("EPSG:32638")
    dam_utm = gpd.GeoSeries([dam_point], crs="EPSG:4326").to_crs("EPSG:32638").iloc[0]
    outlet_id = rivers_gdf.loc[rivers_utm.geometry.distance(dam_utm).idxmin(), "HYRIV_ID"]

    # Reverse adjacency: which segments flow INTO each segment
    flows_into = defaultdict(list)
    for segment_id, next_down_id in zip(rivers_gdf["HYRIV_ID"], rivers_gdf["NEXT_DOWN"]):
        flows_into[next_down_id].append(segment_id)

    # BFS upstream from the outlet
    upstream_ids = {outlet_id}
    queue = deque([outlet_id])
    while queue:
        current_id = queue.popleft()
        for upstream_id in flows_into.get(current_id, []):
            if upstream_id not in upstream_ids:
                upstream_ids.add(upstream_id)
                queue.append(upstream_id)

    return rivers_gdf[rivers_gdf["HYRIV_ID"].isin(upstream_ids)]


def clip_and_export(shp_path: Path):
    """For each AOI: keep upstream-of-dam rivers, clip to AOI, tag, save as GeoJSON."""
    river_parts = []
    for name, aoi_cfg in AOIS.items():
        bbox = aoi_cfg["bbox"]
        dam_lon, dam_lat = aoi_cfg["dam"]
        print(f"  Processing rivers for {name}...")
        rivers_gdf = gpd.read_file(shp_path, bbox=bbox)
        if rivers_gdf.empty:
            print(f"  No rivers found in AOI {name}")
            continue

        if rivers_gdf.crs is None or rivers_gdf.crs.to_epsg() != 4326:
            rivers_gdf = rivers_gdf.to_crs("EPSG:4326")

        # 1) Keep only the catchment upstream of the dam
        before_count = len(rivers_gdf)
        rivers_gdf = upstream_of_dam(rivers_gdf, dam_lon, dam_lat)
        print(f"    upstream-of-dam: {len(rivers_gdf)}/{before_count} segments")

        # 2) Drop the smallest tributaries to reduce clutter
        if "ORD_FLOW" in rivers_gdf.columns:
            rivers_gdf = rivers_gdf[rivers_gdf["ORD_FLOW"] <= MAX_FLOW_ORDER]

        # 3) Clip geometries exactly to the AOI box (no overhang past the border)
        rivers_gdf = gpd.clip(rivers_gdf, shp_box(*bbox))
        rivers_gdf = rivers_gdf[~rivers_gdf.geometry.is_empty & rivers_gdf.geometry.notna()]
        if rivers_gdf.empty:
            print(f"  No rivers left for {name} after clip")
            continue

        rivers_gdf = rivers_gdf.copy()
        rivers_gdf["aoi"] = name
        keep_cols = ["aoi", "geometry"]
        if "ORD_FLOW" in rivers_gdf.columns:
            keep_cols.insert(1, "ORD_FLOW")
        river_parts.append(rivers_gdf[keep_cols])
        print(f"  {len(rivers_gdf)} river segments kept for {name}")

    if not river_parts:
        print("ERROR: No river data to export.")
        return

    combined_gdf = gpd.GeoDataFrame(pd.concat(river_parts, ignore_index=True),
                                    crs="EPSG:4326")
    combined_gdf.to_file(OUTPUT_GEOJSON, driver="GeoJSON")
    print(f"Saved: {OUTPUT_GEOJSON} ({len(combined_gdf)} segments total)")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 50)
    print("HydroRIVERS v10 - River Data Download & Clip")
    print("=" * 50)

    shp_path = download_rivers_shp()
    if not shp_path:
        print("\nFailed - dashboard will fall back to simplified river lines.")
        return

    print("\nClipping to AOIs...")
    clip_and_export(shp_path)
    print("\nReady for app.py")


if __name__ == "__main__":
    main()
