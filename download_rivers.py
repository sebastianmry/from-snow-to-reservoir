"""
FROM SNOW TO RESERVOIR - HydroRIVERS Download & Clip
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

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
from pathlib import Path

import geopandas as gpd
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

# AOI bounding boxes (min_lon, min_lat, max_lon, max_lat) - same as the pipeline
AOIS = {
    "enguri":   (41.70, 42.55, 42.80, 43.15),
    "zhinvali": (44.30, 42.00, 45.15, 42.80),
}

# Only keep major rivers: ORD_FLOW is the logarithmic flow-order class.
# Lower value = larger river. Keep classes <= this threshold to avoid clutter.
MAX_FLOW_ORDER = 6


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

    print(f"Downloading HydroRIVERS v10 Europe (~68 MB)...")
    print(f"URL: {RIVERS_URL}")

    try:
        resp = requests.get(RIVERS_URL, stream=True, timeout=600)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(RIVERS_ZIP, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"\r  {downloaded / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print(f"\nDownload complete ({downloaded / 1e6:.1f} MB)")
    except Exception as e:
        print(f"ERROR downloading HydroRIVERS: {e}")
        return None

    print("Unpacking...")
    with zipfile.ZipFile(RIVERS_ZIP, "r") as zf:
        zf.extractall(STATIC_DIR)
    RIVERS_ZIP.unlink()

    existing = list(STATIC_DIR.rglob(RIVERS_SHP_GLOB))
    if existing:
        print(f"Shapefile ready: {existing[0]}")
        return existing[0]

    print("ERROR: Shapefile not found after unpack.")
    return None


# ─────────────────────────────────────────────
# CLIP & EXPORT
# ─────────────────────────────────────────────

def clip_and_export(shp_path: Path):
    """Clip rivers to both AOIs, tag each feature with its AOI, save as GeoJSON."""
    parts = []
    for name, bbox in AOIS.items():
        print(f"  Clipping rivers for {name}...")
        gdf = gpd.read_file(shp_path, bbox=bbox)
        if gdf.empty:
            print(f"  No rivers found in AOI {name}")
            continue

        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        # Filter to major rivers only (ORD_FLOW: lower = larger river)
        if "ORD_FLOW" in gdf.columns:
            gdf = gdf[gdf["ORD_FLOW"] <= MAX_FLOW_ORDER]

        gdf = gdf.copy()
        gdf["aoi"] = name
        # Keep only the columns we need for the map
        keep = ["aoi", "geometry"]
        if "ORD_FLOW" in gdf.columns:
            keep.insert(1, "ORD_FLOW")
        parts.append(gdf[keep])
        print(f"  {len(gdf)} river segments kept for {name}")

    if not parts:
        print("ERROR: No river data to export.")
        return

    combined = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")
    combined.to_file(OUTPUT_GEOJSON, driver="GeoJSON")
    print(f"Saved: {OUTPUT_GEOJSON} ({len(combined)} segments total)")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 50)
    print("HydroRIVERS v10 - River Data Download & Clip")
    print("=" * 50)

    shp = download_rivers_shp()
    if not shp:
        print("\nFailed - dashboard will fall back to simplified river lines.")
        return

    print("\nClipping to AOIs...")
    clip_and_export(shp)
    print("\nReady for app.py")


if __name__ == "__main__":
    main()
