"""
FROM SNOW TO RESERVOIR - HydroLAKES Reservoir Polygons
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Downloads HydroLAKES v1.0 (global lake/reservoir polygons, HydroSHEDS family)
and extracts the exact polygon of each study reservoir (Enguri, Zhinvali).
These polygons let extract_timeseries.py measure water area INSIDE the
reservoir footprint specifically, instead of all water in the AOI - the
basis for later water-level estimation (area -> level via DEM hypsometry,
combined with SAR for cloud-independent water extent).

HydroLAKES: Messager et al. 2016, WWF / McGill. CC-BY 4.0.

Output: static_data/reservoirs.geojson

Run once before extract_timeseries.py:
    python download_reservoirs.py
"""

import zipfile
from pathlib import Path

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

OUTPUT_GEOJSON = STATIC_DIR / "reservoirs.geojson"

# AOI bbox (min_lon, min_lat, max_lon, max_lat) + dam point (lon, lat).
# The dam point selects the correct reservoir polygon (the lake at the dam).
AOIS = {
    "enguri":   {"bbox": (41.70, 42.55, 42.80, 43.15), "dam": (42.032, 42.753)},
    "zhinvali": {"bbox": (44.30, 42.00, 45.15, 42.80), "dam": (44.771, 42.133)},
}


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
        import requests
        resp = requests.get(LAKES_URL, stream=True, timeout=1800)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(LAKES_ZIP, "wb") as f:
            for chunk in resp.iter_content(chunk_size=4 * 1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"\r  {downloaded / 1e6:.0f} / {total / 1e6:.0f} MB", end="", flush=True)
        print(f"\nDownload complete ({downloaded / 1e6:.0f} MB)")
    except Exception as e:
        print(f"ERROR downloading HydroLAKES: {e}")
        return None

    print("Unpacking (large file, may take a moment)...")
    with zipfile.ZipFile(LAKES_ZIP, "r") as zf:
        zf.extractall(STATIC_DIR)
    LAKES_ZIP.unlink()

    existing = list(STATIC_DIR.rglob(LAKES_SHP_GLOB))
    if existing:
        print(f"Shapefile ready: {existing[0]}")
        return existing[0]

    print("ERROR: Shapefile not found after unpack.")
    return None


# ─────────────────────────────────────────────
# EXTRACT RESERVOIR POLYGONS
# ─────────────────────────────────────────────

def extract_reservoirs(shp_path: Path):
    """Pick the reservoir polygon at each dam, tag per AOI, save as GeoJSON."""
    parts = []
    for name, cfg in AOIS.items():
        bbox = cfg["bbox"]
        dam_lon, dam_lat = cfg["dam"]
        print(f"  Extracting reservoir for {name}...")

        gdf = gpd.read_file(shp_path, bbox=bbox)
        if gdf.empty:
            print(f"  No lakes found in AOI {name}")
            continue
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        dam = Point(dam_lon, dam_lat)
        # Prefer the polygon that contains the dam point; else the nearest one
        containing = gdf[gdf.contains(dam)]
        if not containing.empty:
            chosen = containing.copy()
        else:
            nearest_idx = gdf.geometry.distance(dam).idxmin()
            chosen = gdf.loc[[nearest_idx]].copy()

        chosen["aoi"] = name
        lake_name = chosen.iloc[0].get("Lake_name", "")
        lake_area = chosen.iloc[0].get("Lake_area", float("nan"))
        print(f"    -> '{lake_name or 'unnamed'}', HydroLAKES area {lake_area} km2")

        keep = ["aoi", "geometry"]
        for col in ("Lake_name", "Lake_area", "Hylak_id"):
            if col in chosen.columns:
                keep.insert(-1, col)
        parts.append(chosen[keep])

    if not parts:
        print("ERROR: No reservoir polygons extracted.")
        return

    combined = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")
    combined.to_file(OUTPUT_GEOJSON, driver="GeoJSON")
    print(f"Saved: {OUTPUT_GEOJSON} ({len(combined)} polygons)")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 55)
    print("HydroLAKES - Reservoir Polygon Extraction")
    print("=" * 55)

    shp = download_lakes_shp()
    if not shp:
        print("\nFailed - reservoir polygons unavailable.")
        return

    print("\nExtracting reservoir polygons...")
    extract_reservoirs(shp)
    print("\nReady for extract_timeseries.py")


if __name__ == "__main__":
    main()
