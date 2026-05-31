"""
FROM SNOW TO RESERVOIR - Time Series Extraction
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Reads clipped GeoTIFFs from Google Drive and computes per-date area statistics.
Requires RGI v7 Region 12 shapefile in static_data/ for glacier stats.
Run download_glaciers.py first to fetch the shapefile.

DSWx pixel values (B01_WTR, S1 + HLS):
  1-5=water, 252=snow/ice, 253=cloud/cloud-shadow (HLS only), 255=NoData
Cloud masking for HLS uses the WTR layer's own 253 flag (no separate B09 layer).

Output per AOI:
  {site}_s1_timeseries.csv/.parquet   - water area from DSWx-S1
  {site}_hls_timeseries.csv/.parquet  - water + snow + glacier stats from DSWx-HLS

Usage:
    python download_glaciers.py   # once, to fetch RGI shapefile
    python extract_timeseries.py
"""

import re
import csv
import json
from datetime import datetime
from pathlib import Path

import numpy as np
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

# Parent folder in Google Drive holding the hls/ and s1/ subfolders
DRIVE_PARENT = "OPERA_DSWx"

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

NODATA          = 255
WATER_VALUES    = {1, 2, 3, 4, 5}
SNOW_VALUE      = 252
CLOUD_WTR_VALUE = 253  # WTR layer's own cloud/cloud-shadow flag (replaces B09)
MAX_CLOUD_PCT  = 30.0
MIN_VALID_PCT  = 95.0  # HLS (optical): skip scenes with < 95% valid AOI pixels
# S1 (SAR) is masked much more aggressively in steep terrain (radar layover /
# shadow in the Caucasus), so the same 95% would reject almost everything.
# Lower threshold + the actual coverage is printed so it can be tuned.
S1_MIN_VALID_PCT = 70.0
S1_REPEAT_DAYS   = 12    # Sentinel-1 ground-track repeat cycle (orbit dedup)
S1_FULL_COVER_PCT = 90.0  # orbit counts as full-AOI coverage (dedup prefers these)

STATIC_DIR = Path("static_data")
CACHE_DIR  = STATIC_DIR / "cache"
OUTPUT_DIR = Path(".")
SITES      = ["enguri", "zhinvali"]

# AOIs straddle MGRS/UTM tile boundaries, so a single tile never covers the AOI.
# Dates with fewer tiles are skipped before any download (cheap pre-filter).
MIN_TILES  = 2

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

def extract_s1_stats(wtr_da) -> dict:
    """Water area from a mosaicked DSWx-S1 B01_WTR DataArray (EPSG:4326).
    SAR is cloud-independent, so no cloud filter applies. Always returns the
    stats incl. valid_px_pct; the caller decides whether coverage is sufficient."""
    wtr = wtr_da.values[0]
    px  = _pixel_size_km2_da(wtr_da)

    valid     = wtr != NODATA
    valid_pct = float(np.sum(valid)) / wtr.size * 100

    return {
        "water_km2":    float(np.sum(np.isin(wtr, list(WATER_VALUES))) * px),
        "valid_px_pct": round(valid_pct, 2),
    }


def dedup_single_orbit(rows: list[dict]) -> list[dict]:
    """Reduce S1 dates to a single consistent relative orbit (one look geometry).

    Each Sentinel-1 relative orbit repeats exactly every S1_REPEAT_DAYS, so dates
    of the same orbit share the same phase = ordinal % S1_REPEAT_DAYS. We group by
    that phase and keep the orbit that best covers the whole period (most scenes,
    longest time span). This removes the geometry-dependent sawtooth caused by
    mixing ascending/descending tracks, and drops the offset S1C track (kept for
    consistency, not density). Coverage no longer distinguishes tracks after the
    AOI clip, so phase - not coverage - is the robust orbit fingerprint.
    """
    if len(rows) < 2:
        return rows
    n_before = len(rows)

    # 1. Drop genuinely partial scenes FIRST. A partial track (e.g. ~79% AOI)
    #    passes the loose extraction threshold but must not enter the series.
    #    Doing this before phase-grouping matters: with S1A+S1C, a partial and a
    #    full track can share the same 12-day phase, so filtering by phase median
    #    alone would still keep the partial scenes of the chosen phase.
    rows = [r for r in rows if r["valid_px_pct"] >= S1_FULL_COVER_PCT]
    if not rows:
        return []

    # 2. Group the full-coverage scenes by 12-day repeat phase (= relative orbit
    #    for a single satellite) and keep the track that best spans the period.
    ref = min(r["date"] for r in rows).toordinal()
    by_phase: dict[int, list[dict]] = {}
    for r in rows:
        phase = (r["date"].toordinal() - ref) % S1_REPEAT_DAYS
        by_phase.setdefault(phase, []).append(r)

    def span_days(items: list[dict]) -> int:
        return (max(i["date"] for i in items) - min(i["date"] for i in items)).days

    def median_cov(items: list[dict]) -> float:
        vals = sorted(i["valid_px_pct"] for i in items)
        return vals[len(vals) // 2]

    best_phase = max(by_phase, key=lambda p: (len(by_phase[p]), span_days(by_phase[p])))
    kept = sorted(by_phase[best_phase], key=lambda r: r["date"])
    print(f"    orbit dedup: {n_before} -> {len(kept)} scenes (dropped partials, "
          f"kept 1 of {len(by_phase)} full-coverage orbits, phase {best_phase}, "
          f"median cov {median_cov(kept):.1f}%)")
    return kept


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
    # Clip to exactly the AOI first: tiles from different UTM zones reproject to
    # an oversized bounding box with NoData corners, which would otherwise deflate
    # the valid-pixel percentage. Then pad back up in case data is smaller than AOI.
    merged = merged.rio.clip_box(clip_box[0], clip_box[1], clip_box[2], clip_box[3])
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


def extract_hls_stats(wtr_da, glaciers: gpd.GeoDataFrame | None) -> dict:
    """
    Water + snow/glacier stats from a mosaicked DSWx-HLS B01_WTR DataArray.
    wtr_da is an EPSG:4326 mosaic (output of mosaic_tiles).

    Cloud masking uses the WTR layer's own quality flag (value 253 =
    cloud/cloud-shadow), the product's authoritative determination - no separate
    B09 layer is needed. Always returns the stats incl. valid_px_pct and
    cloud_cover_percent; the caller decides whether coverage / cloud are acceptable.
    """
    wtr       = wtr_da.values[0]
    px        = _pixel_size_km2_da(wtr_da)
    transform = wtr_da.rio.transform()
    raster_crs = wtr_da.rio.crs
    shape     = wtr.shape

    valid = wtr != NODATA          # has data (not fill); includes cloud-masked 253
    cloud = wtr == CLOUD_WTR_VALUE  # product's own cloud/cloud-shadow flag

    n_valid = int(np.sum(valid))
    valid_pct = float(n_valid) / wtr.size * 100
    cloud_pct = float(np.sum(cloud & valid)) / max(n_valid, 1) * 100

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
# PER-DATE CACHE (resume across runs)
# ─────────────────────────────────────────────
# Computing a date means downloading + mosaicking tiles from Drive (the slow
# part). We cache the per-date result - including skipped dates and the reason -
# so re-runs only touch dates not seen before. Pass refresh=True to recompute.

def load_cache(name: str) -> dict:
    p = CACHE_DIR / f"{name}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_cache(name: str, cache: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{name}.json").write_text(json.dumps(cache))


def cache_rows(cache: dict) -> list[dict]:
    """Build time-series rows from the 'ok' entries of a cache."""
    rows = []
    for date_str, entry in cache.items():
        if entry.get("status") != "ok":
            continue
        stats = {k: v for k, v in entry.items() if k not in ("status", "reason")}
        rows.append({"date": datetime.strptime(date_str, "%Y%m%d").date(), **stats})
    return rows


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

def main(skip_s1: bool = False, skip_hls: bool = False, refresh: bool = False):
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

    # Parent OPERA_DSWx folder holding hls/ and s1/
    opera_root = get_folder_id(drive, DRIVE_PARENT, "root")
    if not opera_root:
        print(f"  '{DRIVE_PARENT}' folder not found - run download_hls.py / download_s1.py first")
        return

    # ── DSWx-S1 ──────────────────────────────
    if skip_s1:
        print("\n--- DSWx-S1 skipped (--skip-s1) ---")
    print("\n--- DSWx-S1 B01_WTR ---" if not skip_s1 else "", end="")
    s1_root = get_folder_id(drive, "s1", opera_root) if not skip_s1 else None

    for aoi in ([] if skip_s1 else [AOI_1, AOI_2]):
        site = aoi["name"]
        rows: list[dict] = []

        site_folder = get_folder_id(drive, site, s1_root) if s1_root else None
        if not site_folder:
            print(f"  Folder not found for {site} (S1) - skipping")
            continue

        files = [f for f in list_tifs_in_folder(drive, site_folder)
                 if "B01_WTR" in f["title"]]

        # Group ALL tiles per date (multiple MGRS tiles cover the AOI)
        wtr_by_date: dict[str, list] = {}
        for f in files:
            meta = parse_filename(f["title"])
            if not meta:
                continue
            _, date_str, _ = meta
            wtr_by_date.setdefault(date_str, []).append(f)

        dates = sorted(wtr_by_date.keys())
        cache = {} if refresh else load_cache(f"{site}_s1")
        print(f"  {site}: {len(files)} B01_WTR files, {len(dates)} dates "
              f"({len(cache)} cached)")

        for i, date_str in enumerate(dates, 1):
            wtr_files = wtr_by_date[date_str]
            n_tiles = len(wtr_files)
            prefix = f"  [{i:>3}/{len(dates)}] {site}_s1_{date_str} ({n_tiles} tiles) ..."
            if date_str in cache:
                continue
            print(prefix, end=" ", flush=True)
            if n_tiles < MIN_TILES:
                print(f"skipped (only {n_tiles} tile < {MIN_TILES}, cannot cover AOI)")
                cache[date_str] = {"status": "skip", "reason": "few_tiles"}
                save_cache(f"{site}_s1", cache)
                continue
            try:
                wtr_mosaic = mosaic_tiles([read_bytes(f) for f in wtr_files],
                                          NODATA, aoi["clip_box"])
                if wtr_mosaic is None:
                    print("skipped (no readable tiles)")
                    cache[date_str] = {"status": "skip", "reason": "no_tiles"}
                    save_cache(f"{site}_s1", cache)
                    continue
                stats = extract_s1_stats(wtr_mosaic)
                if stats["valid_px_pct"] < S1_MIN_VALID_PCT:
                    print(f"skipped (coverage {stats['valid_px_pct']:.1f}% < {S1_MIN_VALID_PCT}%)")
                    cache[date_str] = {"status": "skip", "reason": "coverage", **stats}
                else:
                    cache[date_str] = {"status": "ok", **stats}
                    print(f"water={stats['water_km2']:.2f} km2  (cov {stats['valid_px_pct']:.1f}%)")
                save_cache(f"{site}_s1", cache)
            except Exception as e:
                print(f"ERROR: {e}")  # not cached -> retried next run

        rows = cache_rows(cache)
        if rows:
            rows = dedup_single_orbit(rows)
            save_outputs(rows, ["date", "water_km2", "valid_px_pct"],
                         f"{site}_s1_timeseries")

    # ── DSWx-HLS ─────────────────────────────
    if skip_hls:
        print("\n--- DSWx-HLS skipped (--skip-hls) ---")
    print("\n--- DSWx-HLS B01_WTR (cloud mask = WTR 253) ---" if not skip_hls else "", end="")
    hls_root = get_folder_id(drive, "hls", opera_root) if not skip_hls else None

    if not skip_hls and not hls_root:
        print("  HLS folder not found - run download_hls.py first")
        return

    for aoi in ([] if skip_hls else [AOI_1, AOI_2]):
        site     = aoi["name"]
        glaciers = glacier_masks.get(site)
        rows: list[dict] = []

        site_folder = get_folder_id(drive, site, hls_root)
        if not site_folder:
            print(f"  Folder not found for {site} (HLS)")
            continue

        files = list_tifs_in_folder(drive, site_folder)

        # Group ALL WTR tiles per date (multiple MGRS tiles cover the AOI).
        # Cloud masking comes from the WTR layer itself (value 253), so the
        # B09_CLOUD layer is no longer needed.
        wtr_by_date: dict[str, list] = {}
        for f in files:
            meta = parse_filename(f["title"])
            if not meta:
                continue
            _, date_str, layer = meta
            if "B01_WTR" in layer:
                wtr_by_date.setdefault(date_str, []).append(f)

        dates = sorted(wtr_by_date.keys())
        cache = {} if refresh else load_cache(f"{site}_hls")
        print(f"  {site}: {len(dates)} WTR dates ({len(cache)} cached)")

        for i, date_str in enumerate(dates, 1):
            wtr_files = wtr_by_date[date_str]
            n_tiles = len(wtr_files)
            prefix = f"  [{i:>3}/{len(dates)}] {site}_hls_{date_str} ({n_tiles} tiles) ..."
            if date_str in cache:
                continue
            print(prefix, end=" ", flush=True)
            if n_tiles < MIN_TILES:
                print(f"skipped (only {n_tiles} tile < {MIN_TILES}, cannot cover AOI)")
                cache[date_str] = {"status": "skip", "reason": "few_tiles"}
                save_cache(f"{site}_hls", cache)
                continue
            try:
                # Mosaic all WTR tiles for this date into one AOI raster
                wtr_mosaic = mosaic_tiles([read_bytes(f) for f in wtr_files],
                                          NODATA, aoi["clip_box"])
                if wtr_mosaic is None:
                    print("skipped (no readable tiles)")
                    cache[date_str] = {"status": "skip", "reason": "no_tiles"}
                    save_cache(f"{site}_hls", cache)
                    continue

                stats = extract_hls_stats(wtr_mosaic, glaciers)
                if stats["valid_px_pct"] < MIN_VALID_PCT:
                    print(f"skipped (coverage {stats['valid_px_pct']:.1f}% < {MIN_VALID_PCT}%)")
                    cache[date_str] = {"status": "skip", "reason": "coverage", **stats}
                elif stats["cloud_cover_percent"] > MAX_CLOUD_PCT:
                    print(f"skipped (cloud {stats['cloud_cover_percent']:.1f}% > {MAX_CLOUD_PCT}%)")
                    cache[date_str] = {"status": "skip", "reason": "cloud", **stats}
                else:
                    cache[date_str] = {"status": "ok", **stats}
                    print(f"water={stats['water_area_km2']:.2f}  "
                          f"snow_seas={stats['seasonal_snow_km2']:.1f}  "
                          f"snow_glac={stats['snow_on_glacier_km2']:.1f}  "
                          f"bare_ice={stats['bare_ice_km2']:.1f}  "
                          f"cloud={stats['cloud_cover_percent']:.0f}%")
                save_cache(f"{site}_hls", cache)
            except Exception as e:
                print(f"ERROR: {e}")  # not cached -> retried next run

        rows = cache_rows(cache)
        if rows:
            save_outputs(
                rows,
                ["date", "water_area_km2", "seasonal_snow_km2", "snow_on_glacier_km2",
                 "bare_ice_km2", "glacier_total_km2", "cloud_cover_percent", "valid_px_pct"],
                f"{site}_timeseries",
            )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract DSWx time series from Google Drive.")
    parser.add_argument("--skip-s1", action="store_true", help="skip the DSWx-S1 section")
    parser.add_argument("--skip-hls", action="store_true", help="skip the DSWx-HLS section")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the per-date cache and recompute everything")
    args = parser.parse_args()
    main(skip_s1=args.skip_s1, skip_hls=args.skip_hls, refresh=args.refresh)
