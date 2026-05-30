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

# AOI bbox (min_lon, min_lat, max_lon, max_lat) + dam point (lon, lat).
# The dam point defines the catchment outlet: only rivers UPSTREAM of it
# (i.e. that feed the reservoir) are kept.
AOIS = {
    "enguri":   {"bbox": (41.70, 42.55, 42.80, 43.15), "dam": (42.032, 42.753)},
    "zhinvali": {"bbox": (44.30, 42.00, 45.15, 42.80), "dam": (44.771, 42.133)},
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

def upstream_of_dam(gdf: gpd.GeoDataFrame, dam_lon: float, dam_lat: float) -> gpd.GeoDataFrame:
    """Keep only river segments upstream of the dam (the reservoir catchment).

    Uses the HydroRIVERS flow topology: every segment (HYRIV_ID) flows into the
    segment given by NEXT_DOWN. We locate the segment nearest the dam, then walk
    the network upstream (against the flow) and keep everything that drains to it.
    """
    if "HYRIV_ID" not in gdf.columns or "NEXT_DOWN" not in gdf.columns:
        print("  (no flow topology fields - skipping upstream filter)")
        return gdf

    # Segment nearest the dam = catchment outlet
    dam = Point(dam_lon, dam_lat)
    outlet_id = gdf.loc[gdf.geometry.distance(dam).idxmin(), "HYRIV_ID"]

    # Reverse adjacency: which segments flow INTO each segment
    flows_into = defaultdict(list)
    for hid, nd in zip(gdf["HYRIV_ID"], gdf["NEXT_DOWN"]):
        flows_into[nd].append(hid)

    # BFS upstream from the outlet
    keep = {outlet_id}
    queue = deque([outlet_id])
    while queue:
        cur = queue.popleft()
        for up in flows_into.get(cur, []):
            if up not in keep:
                keep.add(up)
                queue.append(up)

    return gdf[gdf["HYRIV_ID"].isin(keep)]


def clip_and_export(shp_path: Path):
    """For each AOI: keep upstream-of-dam rivers, clip to AOI, tag, save as GeoJSON."""
    parts = []
    for name, cfg in AOIS.items():
        bbox = cfg["bbox"]
        dam_lon, dam_lat = cfg["dam"]
        print(f"  Processing rivers for {name}...")
        gdf = gpd.read_file(shp_path, bbox=bbox)
        if gdf.empty:
            print(f"  No rivers found in AOI {name}")
            continue

        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        # 1) Keep only the catchment upstream of the dam
        before = len(gdf)
        gdf = upstream_of_dam(gdf, dam_lon, dam_lat)
        print(f"    upstream-of-dam: {len(gdf)}/{before} segments")

        # 2) Drop the smallest tributaries to reduce clutter
        if "ORD_FLOW" in gdf.columns:
            gdf = gdf[gdf["ORD_FLOW"] <= MAX_FLOW_ORDER]

        # 3) Clip geometries exactly to the AOI box (no overhang past the border)
        gdf = gpd.clip(gdf, shp_box(*bbox))
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
        if gdf.empty:
            print(f"  No rivers left for {name} after clip")
            continue

        gdf = gdf.copy()
        gdf["aoi"] = name
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
