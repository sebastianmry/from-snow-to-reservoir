"""
FROM SNOW TO RESERVOIR - Time Series Extraction
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Reads clipped GeoTIFFs from Google Drive and computes per-date area statistics.
Requires RGI v7 Region 12 shapefile in static_data/ for glacier stats.
Run download_glaciers.py first to fetch the shapefile.

DSWx pixel values:
  B01_WTR (S1 + HLS):  1-5=water, 252=snow/ice, 255=NoData
  B09_CLOUD (HLS):     3=cloud shadow, 4=cloud, 255=NoData

Output per AOI:
  {site}_s1_timeseries.csv/.parquet   - water area from DSWx-S1
  {site}_hls_timeseries.csv/.parquet  - water + snow + glacier stats from DSWx-HLS

Usage:
    python download_glaciers.py   # once, to fetch RGI shapefile
    python extract_timeseries.py
"""

import io
import re
import csv
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.io import MemoryFile
from rasterio.enums import Resampling
import rioxarray  # noqa: F401  (registers the .rio accessor)
from rioxarray.merge import merge_arrays
import geopandas as gpd
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print("pandas not installed - parquet output disabled")

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

DRIVE_ROOT_FOLDER_ID = "1EdYn2RbULuEYj8dnPbK9Zshia6G50ssE"

AOI_1 = {
    "name": "enguri",
    "label": "Enguri Talsperre + Svaneti",
    "clip_box": (41.70, 42.55, 42.80, 43.15),   # (min_lon, min_lat, max_lon, max_lat)
}

AOI_2 = {
    "name": "zhinvali",
    "label": "Zhinvali Talsperre + Gergeti",
    "clip_box": (44.30, 42.00, 45.15, 42.80),
}

NODATA         = 255
WATER_VALUES   = {1, 2, 3, 4, 5}
SNOW_VALUE     = 252
CLOUD_VALUES   = {3, 4}
MAX_CLOUD_PCT  = 30.0
MIN_VALID_PCT  = 99.0  # skip scenes where less than 99% of AOI pixels are valid (not NoData)

STATIC_DIR = Path("static_data")
OUTPUT_DIR = Path(".")
SITES      = ["enguri", "zhinvali"]

RGI_SHP_GLOB = "RGI2000-v7.0-G-12_caucasus*middle_east.shp"


# ─────────────────────────────────────────────
# RGI LOOKUP (download via download_glaciers.py)
# ─────────────────────────────────────────────

def find_rgi() -> Path | None:
    """Look for RGI shapefile in static_data/. Run download_glaciers.py to fetch it."""
    existing = list(STATIC_DIR.rglob(RGI_SHP_GLOB))
    if existing:
        print(f"  RGI shapefile found: {existing[0]}")
        return existing[0]
    print("  RGI shapefile not found. Run download_glaciers.py first.")
    print("  Continuing without glacier stats.")
    return None


def load_glacier_mask(shp_path: Path, clip_box: tuple) -> gpd.GeoDataFrame | None:
    """Load RGI glaciers clipped to AOI bounding box."""
    min_lon, min_lat, max_lon, max_lat = clip_box
    try:
        gdf = gpd.read_file(shp_path, bbox=(min_lon, min_lat, max_lon, max_lat))
        if gdf.empty:
            print(f"  No glaciers found in AOI {clip_box}")
            return None
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        print(f"  {len(gdf)} glacier polygons loaded for AOI")
        return gdf
    except Exception as e:
        print(f"  ERROR loading RGI: {e}")
        return None


def rasterize_glaciers(glaciers: gpd.GeoDataFrame, src_crs, transform, shape: tuple) -> np.ndarray:
    """Rasterize glacier polygons to match a raster's grid. Returns boolean mask."""
    if glaciers is None or glaciers.empty:
        return np.zeros(shape, dtype=bool)

    # Reproject to raster CRS if needed
    raster_epsg = src_crs.to_epsg() if src_crs else 4326
    if raster_epsg and raster_epsg != 4326:
        gdf_proj = glaciers.to_crs(f"EPSG:{raster_epsg}")
    else:
        gdf_proj = glaciers

    geoms = [geom for geom in gdf_proj.geometry if geom is not None and geom.is_valid]
    if not geoms:
        return np.zeros(shape, dtype=bool)

    mask = rasterize(
        [(geom, 1) for geom in geoms],
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype=np.uint8,
    )
    return mask.astype(bool)


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

def authenticate() -> GoogleDrive:
    gauth = GoogleAuth()
    gauth.LoadCredentialsFile("gdrive_credentials.json")
    if gauth.credentials is None:
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()
    gauth.SaveCredentialsFile("gdrive_credentials.json")
    return GoogleDrive(gauth)


# ─────────────────────────────────────────────
# DRIVE HELPERS
# ─────────────────────────────────────────────

def get_folder_id(drive: GoogleDrive, name: str, parent_id: str) -> str | None:
    query = (
        f"title='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = drive.ListFile({"q": query}).GetList()
    return results[0]["id"] if results else None


def list_tifs_in_folder(drive: GoogleDrive, folder_id: str) -> list[dict]:
    return drive.ListFile(
        {"q": f"'{folder_id}' in parents and trashed=false and title contains '.tif'"}
    ).GetList()


def read_bytes(drive_file) -> bytes:
    return drive_file.GetContentIOBuffer().read()


# ─────────────────────────────────────────────
# PIXEL ANALYSIS
# ─────────────────────────────────────────────

def pixel_size_km2(src) -> float:
    res_x = abs(src.transform.a)
    res_y = abs(src.transform.e)
    if src.crs and src.crs.is_geographic:
        center_lat = (src.bounds.top + src.bounds.bottom) / 2
        return (res_x * 111.32 * np.cos(np.radians(center_lat))) * (res_y * 110.574)
    return (res_x * res_y) / 1e6


def extract_s1_stats(data: bytes) -> dict:
    """Water area from DSWx-S1 B01_WTR."""
    with rasterio.open(io.BytesIO(data)) as src:
        arr = src.read(1)
        px  = pixel_size_km2(src)
    valid = arr != NODATA
    return {
        "water_km2":    float(np.sum(np.isin(arr, list(WATER_VALUES))) * px),
        "valid_px_pct": round(float(np.sum(valid)) / arr.size * 100, 2),
    }


def _open_da(data: bytes):
    """Open GeoTIFF bytes as an in-memory rioxarray DataArray (fully loaded)."""
    with MemoryFile(data) as mf:
        with mf.open() as ds:
            return rioxarray.open_rasterio(ds).load()


def mosaic_tiles(tile_bytes: list[bytes], nodata: int, clip_box: tuple):
    """Reproject every tile to EPSG:4326, merge into one mosaic, pad to full AOI.

    Different MGRS tiles can sit in different UTM zones, so we reproject each to
    a common geographic CRS before merging. Padding to the AOI bbox makes the
    valid-pixel percentage AOI-relative (not tile-relative).
    """
    arrays = []
    for data in tile_bytes:
        try:
            da = _open_da(data)
            da = da.rio.reproject("EPSG:4326", resampling=Resampling.nearest, nodata=nodata)
            arrays.append(da)
        except Exception as e:
            print(f"(tile skip: {e})", end=" ")
    if not arrays:
        return None

    merged = arrays[0] if len(arrays) == 1 else merge_arrays(arrays, nodata=nodata)
    # Pad to the full AOI extent so coverage is measured against the whole AOI
    merged = merged.rio.pad_box(clip_box[0], clip_box[1], clip_box[2], clip_box[3],
                                constant_values=nodata)
    return merged


def _pixel_size_km2_da(da) -> float:
    """Approximate pixel area (km2) for a geographic (EPSG:4326) DataArray."""
    t = da.rio.transform()
    res_x, res_y = abs(t.a), abs(t.e)
    left, bottom, right, top = da.rio.bounds()
    center_lat = (bottom + top) / 2
    return (res_x * 111.32 * np.cos(np.radians(center_lat))) * (res_y * 110.574)


def extract_hls_stats(wtr_da, cloud_da, glaciers: gpd.GeoDataFrame | None) -> dict | None:
    """
    Water + snow/glacier stats from a mosaicked DSWx-HLS B01_WTR DataArray.
    wtr_da / cloud_da are EPSG:4326 mosaics (output of mosaic_tiles).
    Returns None if AOI coverage < MIN_VALID_PCT or cloud cover > MAX_CLOUD_PCT.
    """
    wtr       = wtr_da.values[0]
    px        = _pixel_size_km2_da(wtr_da)
    transform = wtr_da.rio.transform()
    raster_crs = wtr_da.rio.crs
    shape     = wtr.shape

    valid = wtr != NODATA
    cloud = np.zeros_like(wtr, dtype=bool)

    if cloud_da is not None:
        # Align CLOUD mosaic onto the WTR grid (same extent/resolution)
        cloud_aligned = cloud_da.rio.reproject_match(wtr_da, resampling=Resampling.nearest)
        cloud_arr = cloud_aligned.values[0]
        cloud = np.isin(cloud_arr, list(CLOUD_VALUES))

    n_valid = int(np.sum(valid))
    valid_pct = float(n_valid) / wtr.size * 100
    cloud_pct = float(np.sum(cloud & valid)) / max(n_valid, 1) * 100

    if valid_pct < MIN_VALID_PCT:
        return None
    if cloud_pct > MAX_CLOUD_PCT:
        return None

    usable      = valid & ~cloud
    water_mask  = np.isin(wtr, list(WATER_VALUES)) & usable
    snow_mask   = (wtr == SNOW_VALUE) & usable

    glacier_mask = rasterize_glaciers(glaciers, raster_crs, transform, shape)

    snow_on_glacier   = snow_mask & glacier_mask
    snow_seasonal     = snow_mask & ~glacier_mask
    glacier_total_km2 = float(np.sum(glacier_mask & usable) * px)
    bare_ice_km2      = max(0.0, glacier_total_km2 - float(np.sum(snow_on_glacier) * px))

    return {
        "water_area_km2":      float(np.sum(water_mask) * px),
        "seasonal_snow_km2":   float(np.sum(snow_seasonal) * px),
        "snow_on_glacier_km2": float(np.sum(snow_on_glacier) * px),
        "bare_ice_km2":        bare_ice_km2,
        "glacier_total_km2":   glacier_total_km2,
        "cloud_cover_percent": round(cloud_pct, 2),
        "valid_px_pct":        round(valid_pct, 2),
    }


# ─────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────

def save_outputs(rows: list[dict], fieldnames: list[str], stem: str):
    rows.sort(key=lambda r: r["date"])
    csv_path = OUTPUT_DIR / f"{stem}.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved: {csv_path} ({len(rows)} rows)")

    if HAS_PANDAS:
        df = pd.DataFrame(rows)[fieldnames]
        df["date"] = pd.to_datetime(df["date"])
        pq_path = OUTPUT_DIR / f"{stem}.parquet"
        df.to_parquet(pq_path, index=False)
        print(f"  Saved: {pq_path}")


def parse_filename(title: str) -> tuple[str, str, str] | None:
    """Parse site, date, layer from a filename.
    Handles both new MGRS-tagged names (site_date_TILE_layer_clipped.tif)
    and old names without a tile id (site_date_layer_clipped.tif).
    """
    m = re.match(
        r"(enguri|zhinvali)_(\d{8})_(?:T\d{2}[A-Z]{3}_)?(B\d+[\w\-]+?)_clipped\.tif",
        title,
    )
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("Authenticating with Google Drive...")
    drive = authenticate()

    # ── RGI glacier data ─────────────────────
    print("\n--- RGI v7 Glacier Data ---")
    rgi_shp = find_rgi()

    glacier_masks: dict[str, gpd.GeoDataFrame | None] = {}
    for aoi in [AOI_1, AOI_2]:
        if rgi_shp:
            print(f"  Loading glaciers for {aoi['name']}...")
            glacier_masks[aoi["name"]] = load_glacier_mask(rgi_shp, aoi["clip_box"])
        else:
            glacier_masks[aoi["name"]] = None

    # ── DSWx-S1 ──────────────────────────────
    print("\n--- DSWx-S1 B01_WTR ---")
    s1_root = get_folder_id(drive, "s1", "root")

    for aoi in [AOI_1, AOI_2]:
        site = aoi["name"]
        rows: list[dict] = []

        # Try new structure first, fall back to old
        site_folder = None
        if s1_root:
            site_folder = get_folder_id(drive, site, s1_root)
        if not site_folder:
            site_folder = get_folder_id(drive, site, DRIVE_ROOT_FOLDER_ID)
        if not site_folder:
            print(f"  Folder not found for {site} (S1)")
            continue

        files = [f for f in list_tifs_in_folder(drive, site_folder)
                 if "B01_WTR" in f["title"]]
        print(f"  {site}: {len(files)} B01_WTR files")

        for i, f in enumerate(files, 1):
            meta = parse_filename(f["title"])
            if not meta:
                continue
            _, date_str, _ = meta
            print(f"  [{i:>3}/{len(files)}] {f['title']} ...", end=" ", flush=True)
            try:
                stats = extract_s1_stats(read_bytes(f))
                rows.append({"date": datetime.strptime(date_str, "%Y%m%d").date(), **stats})
                print(f"water={stats['water_km2']:.2f} km2")
            except Exception as e:
                print(f"ERROR: {e}")

        if rows:
            save_outputs(rows, ["date", "water_km2", "valid_px_pct"],
                         f"{site}_s1_timeseries")

    # ── DSWx-HLS ─────────────────────────────
    print("\n--- DSWx-HLS B01_WTR + B09_CLOUD ---")
    hls_root = get_folder_id(drive, "hls", "root")

    if not hls_root:
        print("  HLS folder not found - run download_to_drive.py first")
        return

    for aoi in [AOI_1, AOI_2]:
        site     = aoi["name"]
        glaciers = glacier_masks.get(site)
        rows: list[dict] = []

        site_folder = get_folder_id(drive, site, hls_root)
        if not site_folder:
            print(f"  Folder not found for {site} (HLS)")
            continue

        files = list_tifs_in_folder(drive, site_folder)

        # Group ALL tiles per date (multiple MGRS tiles cover the AOI)
        wtr_by_date:   dict[str, list] = {}
        cloud_by_date: dict[str, list] = {}
        for f in files:
            meta = parse_filename(f["title"])
            if not meta:
                continue
            _, date_str, layer = meta
            if "B01_WTR" in layer:
                wtr_by_date.setdefault(date_str, []).append(f)
            elif "B09_CLOUD" in layer:
                cloud_by_date.setdefault(date_str, []).append(f)

        dates = sorted(wtr_by_date.keys())
        print(f"  {site}: {len(dates)} WTR dates, {len(cloud_by_date)} CLOUD dates")

        for i, date_str in enumerate(dates, 1):
            wtr_files   = wtr_by_date[date_str]
            cloud_files = cloud_by_date.get(date_str, [])
            n_tiles = len(wtr_files)
            print(f"  [{i:>3}/{len(dates)}] {site}_hls_{date_str} ({n_tiles} tiles) ...",
                  end=" ", flush=True)
            try:
                # Mosaic all WTR (and CLOUD) tiles for this date into one AOI raster
                wtr_mosaic = mosaic_tiles([read_bytes(f) for f in wtr_files],
                                          NODATA, aoi["clip_box"])
                if wtr_mosaic is None:
                    print("skipped (no readable tiles)")
                    continue
                cloud_mosaic = None
                if cloud_files:
                    cloud_mosaic = mosaic_tiles([read_bytes(f) for f in cloud_files],
                                                NODATA, aoi["clip_box"])

                stats = extract_hls_stats(wtr_mosaic, cloud_mosaic, glaciers)
                if stats is None:
                    print(f"skipped (coverage < {MIN_VALID_PCT}% or cloud > {MAX_CLOUD_PCT}%)")
                    continue
                rows.append({"date": datetime.strptime(date_str, "%Y%m%d").date(), **stats})
                print(f"water={stats['water_area_km2']:.2f}  "
                      f"snow_seas={stats['seasonal_snow_km2']:.1f}  "
                      f"snow_glac={stats['snow_on_glacier_km2']:.1f}  "
                      f"bare_ice={stats['bare_ice_km2']:.1f}  "
                      f"cloud={stats['cloud_cover_percent']:.0f}%")
            except Exception as e:
                print(f"ERROR: {e}")

        if rows:
            save_outputs(
                rows,
                ["date", "water_area_km2", "seasonal_snow_km2", "snow_on_glacier_km2",
                 "bare_ice_km2", "glacier_total_km2", "cloud_cover_percent", "valid_px_pct"],
                f"{site}_timeseries",
            )


if __name__ == "__main__":
    main()
