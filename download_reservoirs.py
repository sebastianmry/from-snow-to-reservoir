"""
FROM SNOW TO RESERVOIR - HydroLAKES Reservoir Polygons
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Downloads HydroLAKES v1.0 (global lake/reservoir polygons, HydroSHEDS family)
and extracts the exact polygon of each study reservoir (Enguri, Zhinvali).
These polygons are only a SEED: HydroLAKES (derived from a ~2000 water mask)
underestimates the reservoirs badly (Enguri 4.85 km2 vs. real ~13 km2 - it
captures only the lower pool, not the long valley arms). derive_reservoir.py
takes this seed and grows it to the true footprint from the actual S1 maximum
water extent, writing the consumed static_data/reservoirs.geojson.

HydroLAKES: Messager et al. 2016, WWF / McGill. CC-BY 4.0.

Output: static_data/reservoirs_hydrolakes.geojson (seed for derive_reservoir.py)

Run once, then derive_reservoir.py:
    python download_reservoirs.py
    python derive_reservoir.py
"""

import zipfile
from pathlib import Path

import requests
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

STATIC_DIR = Path("static_data")

# HydroLAKES v1.0 polygons (global, ~820 MB) - public download, no login
LAKES_URL  = "https://data.hydrosheds.org/file/hydrolakes/HydroLAKES_polys_v10_shp.zip"
LAKES_ZIP  = STATIC_DIR / "hydrolakes.zip"
LAKES_SHP_GLOB = "HydroLAKES_polys_v10.shp"

OUTPUT_GEOJSON = STATIC_DIR / "reservoirs_hydrolakes.geojson"

# AOI bbox (min_lon, min_lat, max_lon, max_lat) + dam point (lon, lat) come from
# aoi_config.py. The dam point selects the correct reservoir polygon (lake at dam).
from aoi_config import AOIS


# ─────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────

def download_lakes_shp() -> Path | None:
    """Download and unpack HydroLAKES polygon shapefile if not present."""
    STATIC_DIR.mkdir(exist_ok=True)

    existing = list(STATIC_DIR.rglob(LAKES_SHP_GLOB))
    if existing:
        print(f"HydroLAKES shapefile already present: {existing[0]}")
        return existing[0]

    print("Downloading HydroLAKES v1.0 polygons (~820 MB, one-time)...")
    print(f"URL: {LAKES_URL}")

    try:
        response = requests.get(LAKES_URL, stream=True, timeout=1800)
        response.raise_for_status()
        total_bytes = int(response.headers.get("content-length", 0))
        downloaded_bytes = 0
        with LAKES_ZIP.open("wb") as out_file:
            for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                out_file.write(chunk)
                downloaded_bytes += len(chunk)
                if total_bytes:
                    print(f"\r  {downloaded_bytes / 1e6:.0f} / {total_bytes / 1e6:.0f} MB",
                          end="", flush=True)
        print(f"\nDownload complete ({downloaded_bytes / 1e6:.0f} MB)")
    except Exception as error:
        print(f"ERROR downloading HydroLAKES: {error}")
        return None

    print("Unpacking (large file, may take a moment)...")
    with zipfile.ZipFile(LAKES_ZIP, "r") as archive:
        archive.extractall(STATIC_DIR)
    LAKES_ZIP.unlink()

    shp_paths = list(STATIC_DIR.rglob(LAKES_SHP_GLOB))
    if shp_paths:
        print(f"Shapefile ready: {shp_paths[0]}")
        return shp_paths[0]

    print("ERROR: Shapefile not found after unpack.")
    return None


# ─────────────────────────────────────────────
# EXTRACT RESERVOIR POLYGONS
# ─────────────────────────────────────────────

def extract_reservoirs(shp_path: Path):
    """Pick the reservoir polygon at each dam, tag per AOI, save as GeoJSON."""
    reservoir_parts = []
    for name, aoi_cfg in AOIS.items():
        bbox = aoi_cfg["bbox"]
        dam_lon, dam_lat = aoi_cfg["dam"]
        print(f"  Extracting reservoir for {name}...")

        lakes_gdf = gpd.read_file(shp_path, bbox=bbox)
        if lakes_gdf.empty:
            print(f"  No lakes found in AOI {name}")
            continue
        if lakes_gdf.crs is None or lakes_gdf.crs.to_epsg() != 4326:
            lakes_gdf = lakes_gdf.to_crs("EPSG:4326")

        dam_point = Point(dam_lon, dam_lat)
        # Prefer the polygon that contains the dam point; else the nearest one.
        containing_gdf = lakes_gdf[lakes_gdf.contains(dam_point)]
        if not containing_gdf.empty:
            chosen_gdf = containing_gdf.copy()
        else:
            # Distance on a projected CRS (UTM 38N) - distance() on a geographic
            # CRS warns and is inaccurate. The result index maps back to lakes_gdf.
            lakes_utm = lakes_gdf.to_crs("EPSG:32638")
            dam_utm = gpd.GeoSeries([dam_point], crs="EPSG:4326").to_crs("EPSG:32638").iloc[0]
            nearest_idx = lakes_utm.geometry.distance(dam_utm).idxmin()
            chosen_gdf = lakes_gdf.loc[[nearest_idx]].copy()

        chosen_gdf["aoi"] = name
        lake_name = chosen_gdf.iloc[0].get("Lake_name", "")
        lake_area = chosen_gdf.iloc[0].get("Lake_area", float("nan"))
        print(f"    -> '{lake_name or 'unnamed'}', HydroLAKES area {lake_area} km2")

        keep_cols = ["aoi", "geometry"]
        for col in ("Lake_name", "Lake_area", "Hylak_id"):
            if col in chosen_gdf.columns:
                keep_cols.insert(-1, col)
        reservoir_parts.append(chosen_gdf[keep_cols])

    if not reservoir_parts:
        print("ERROR: No reservoir polygons extracted.")
        return

    combined_gdf = gpd.GeoDataFrame(pd.concat(reservoir_parts, ignore_index=True),
                                    crs="EPSG:4326")
    combined_gdf.to_file(OUTPUT_GEOJSON, driver="GeoJSON")
    print(f"Saved: {OUTPUT_GEOJSON} ({len(combined_gdf)} polygons)")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 55)
    print("HydroLAKES - Reservoir Polygon Extraction")
    print("=" * 55)

    shp_path = download_lakes_shp()
    if not shp_path:
        print("\nFailed - reservoir polygons unavailable.")
        return

    print("\nExtracting reservoir polygons...")
    extract_reservoirs(shp_path)
    print("\nReady for extract_timeseries.py")


if __name__ == "__main__":
    main()
