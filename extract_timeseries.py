"""
FROM SNOW TO RESERVOIR - Time Series Extraction
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Reads clipped GeoTIFFs from the tile store and computes per-date area statistics.
Requires RGI v7 Region 12 shapefile in static_data/ for glacier stats.
Run download_glaciers.py first to fetch the shapefile.

DSWx pixel values (B01_WTR, S1 + HLS):
  1-5=water, 252=snow/ice, 253=cloud/cloud-shadow (HLS only), 255=NoData
Cloud masking for HLS uses the WTR layer's own 253 flag (no separate B09 layer).

Output per AOI:
  {site}_s1_timeseries.csv/.parquet   - water area from DSWx-S1 (AOI-wide +
                                        reservoir_area_km2 if reservoirs.geojson
                                        exists, see derive_reservoir.py)
  {site}_timeseries.csv/.parquet      - water + snow + glacier stats from DSWx-HLS

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
# Local tile store (filesystem under PIPELINE_LOCAL_DIR).
from storage import get_store, ROOT

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print("pandas not installed - parquet output disabled")

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

# AOI definition (clip_box + s1_anchor) and DATA_ROOT live in aoi_config.py.
# s1_anchor = one date (YYYYMMDD) of the chosen Sentinel-1 relative orbit; the S1
# section below keeps ONLY dates sharing this orbit's 12-day phase (orbit_phase).
from aoi_config import AOIS, AOI_1, AOI_2, DATA_ROOT, CATCHMENTS_GEOJSON  # noqa: F401

NODATA          = 255
WATER_VALUES    = {1, 2, 3, 4, 5}
SNOW_VALUE      = 252
CLOUD_WTR_VALUE = 253  # WTR layer's own cloud/cloud-shadow flag (replaces B09)
MAX_CLOUD_PCT  = 30.0
# HLS (optical): skip scenes with < this % valid (non-NoData) CATCHMENT pixels.
# Set to 85 (not 95) for the catchment AOIs: the eastern Svaneti headwater tip of
# the Enguri basin sits at the edge of many Sentinel-2/Landsat swaths, so ~12% is
# NoData on many dates (all tiles present - acquisition geometry, not a missing
# tile). 85 recovers the genuinely clear partial dates (Enguri 24 -> 47 usable
# days) while still rejecting near-empty scenes. Going lower barely helps: most of
# the remaining partial dates are ALSO cloud-covered (the true limiter here is
# cloud, ~70-75% of dates), so they would just move to the cloud-skip bucket. The
# residual coverage bias on the recovered days is removed by the *_est columns
# (snow normalized to the observed area, scaled to the full basin).
MIN_VALID_PCT  = 85.0
# S1 (SAR): we anchor to one relative orbit. A date enters the series if EITHER
# the whole catchment is fully imaged (>= S1_MIN_VALID_PCT) OR the reservoir itself
# is fully observed (>= RESERVOIR_MIN_COVER). The catchment-full path keeps the
# AOI-wide water_km2; the reservoir-only path recovers the anchor-orbit cycles
# whose SAR swath misses the eastern Svaneti headwaters but still fully image the
# western lake (catchment cov ~59%, reservoir cov ~100%) - those carry a valid
# reservoir_area_km2 while their water_km2 is NaN'd (not basin-comparable).
# Genuinely partial orbits that also miss the lake are still skipped.
S1_MIN_VALID_PCT  = 90.0
S1_REPEAT_DAYS    = 12    # Sentinel-1 ground-track repeat cycle (orbit dedup)
S1_FULL_COVER_PCT = 90.0  # catchment-full threshold (dedup partial filter; same value)
# Reservoir guard / gate: if less than this % of the reservoir footprint itself has
# valid pixels on a date, reservoir_area_km2 is set to NaN (the lake is under-
# observed, so a low value would be a false drawdown). At or above it, the date is
# kept for the reservoir series even when the wider catchment is only partly imaged.
RESERVOIR_MIN_COVER = 95.0

STATIC_DIR = Path("static_data")
CACHE_DIR  = STATIC_DIR / "cache"
OUTPUT_DIR = Path(".")
SITES      = ["enguri", "zhinvali"]

# AOIs straddle MGRS/UTM tile boundaries, so a single tile never covers the AOI.
# Dates with fewer tiles are skipped before any download (cheap pre-filter).
MIN_TILES  = 2

RGI_SHP_GLOB = "RGI2000-v7.0-G-12_caucasus*middle_east.shp"

# Reservoir footprint (S1-derived envelope from derive_reservoir.py). Used to
# measure water area INSIDE the reservoir specifically, separate from the
# AOI-wide water (which also includes rivers).
RESERVOIR_GEOJSON = STATIC_DIR / "reservoirs.geojson"


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
        glaciers_gdf = gpd.read_file(shp_path, bbox=(min_lon, min_lat, max_lon, max_lat))
        if glaciers_gdf.empty:
            print(f"  No glaciers found in AOI {clip_box}")
            return None
        if glaciers_gdf.crs is None or glaciers_gdf.crs.to_epsg() != 4326:
            glaciers_gdf = glaciers_gdf.to_crs("EPSG:4326")
        print(f"  {len(glaciers_gdf)} glacier polygons loaded for AOI")
        return glaciers_gdf
    except Exception as error:
        print(f"  ERROR loading RGI: {error}")
        return None


def load_reservoir(site: str) -> gpd.GeoDataFrame | None:
    """Load the S1-derived reservoir polygon for one site (derive_reservoir.py)."""
    if not RESERVOIR_GEOJSON.exists():
        return None
    try:
        reservoir_gdf = gpd.read_file(RESERVOIR_GEOJSON)
        reservoir_gdf = reservoir_gdf[reservoir_gdf["aoi"] == site]
        if reservoir_gdf.empty:
            return None
        if reservoir_gdf.crs is None or reservoir_gdf.crs.to_epsg() != 4326:
            reservoir_gdf = reservoir_gdf.to_crs("EPSG:4326")
        return reservoir_gdf
    except Exception as error:
        print(f"  ERROR loading reservoir polygon: {error}")
        return None


def load_catchment(site: str) -> gpd.GeoDataFrame | None:
    """Load the HydroBASINS catchment polygon for one site (download_catchments.py).
    Used to mask the statistics to the reservoir's drainage basin so snow/glacier/
    water are counted only inside the drainage basin."""
    if not CATCHMENTS_GEOJSON.exists():
        return None
    try:
        catchment_gdf = gpd.read_file(CATCHMENTS_GEOJSON)
        catchment_gdf = catchment_gdf[catchment_gdf["aoi"] == site]
        if catchment_gdf.empty:
            return None
        if catchment_gdf.crs is None or catchment_gdf.crs.to_epsg() != 4326:
            catchment_gdf = catchment_gdf.to_crs("EPSG:4326")
        return catchment_gdf
    except Exception as error:
        print(f"  ERROR loading catchment polygon: {error}")
        return None


def rasterize_glaciers(glaciers: gpd.GeoDataFrame, src_crs, transform, shape: tuple) -> np.ndarray:
    """Rasterize glacier polygons to match a raster's grid. Returns boolean mask."""
    if glaciers is None or glaciers.empty:
        return np.zeros(shape, dtype=bool)

    # Reproject to raster CRS if needed
    raster_epsg = src_crs.to_epsg() if src_crs else 4326
    if raster_epsg and raster_epsg != 4326:
        glaciers_proj = glaciers.to_crs(f"EPSG:{raster_epsg}")
    else:
        glaciers_proj = glaciers

    geoms = [geom for geom in glaciers_proj.geometry if geom is not None and geom.is_valid]
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


def _catchment_mask(catchment: gpd.GeoDataFrame | None, wtr_da, shape: tuple) -> np.ndarray:
    """Boolean mask of the catchment polygon on the raster grid. Returns an
    all-True mask when no catchment is given, so callers behave exactly as
    before (no masking) when catchments.geojson is absent."""
    if catchment is None or catchment.empty:
        return np.ones(shape, dtype=bool)
    return rasterize_glaciers(catchment, wtr_da.rio.crs,
                              wtr_da.rio.transform(), shape)


# Folder navigation, tile listing and byte reads go through the storage backend
# (see storage.py): get_store(), store.get_folder_id(...), store.list_tifs(...),
# store.read_bytes(f).


# ─────────────────────────────────────────────
# PIXEL ANALYSIS
# ─────────────────────────────────────────────

def extract_s1_stats(wtr_da, reservoir: gpd.GeoDataFrame | None = None,
                     catchment: gpd.GeoDataFrame | None = None) -> dict:
    """Water area from a mosaicked DSWx-S1 B01_WTR DataArray (EPSG:4326).
    SAR is cloud-independent, so no cloud filter applies. Always returns the
    stats incl. valid_px_pct; the caller decides whether coverage is sufficient.

    If a catchment polygon is given, water is counted ONLY inside the drainage
    basin and valid_px_pct is the fraction of CATCHMENT pixels that have data
    (not the bbox), so the coverage filter stays meaningful even though the
    clip_box is larger than the basin.

    If a reservoir polygon is given, also report reservoir_area_km2 = water
    pixels INSIDE the reservoir footprint - separate from the AOI-wide water
    (which also includes rivers). This is the level-relevant signal and is
    far less noisy than the AOI total (no valley-floor speckle, orbit-robust)."""
    wtr = wtr_da.values[0]
    pixel_km2 = _pixel_size_km2_da(wtr_da)

    catchment_mask = _catchment_mask(catchment, wtr_da, wtr.shape)
    n_catchment    = int(np.sum(catchment_mask)) or wtr.size

    valid      = (wtr != NODATA) & catchment_mask
    valid_pct  = float(np.sum(valid)) / n_catchment * 100
    water_mask = np.isin(wtr, list(WATER_VALUES)) & catchment_mask

    stats = {
        "water_km2":    float(np.sum(water_mask) * pixel_km2),
        "valid_px_pct": round(valid_pct, 2),
    }

    if reservoir is not None and not reservoir.empty:
        reservoir_mask  = rasterize_glaciers(reservoir, wtr_da.rio.crs,
                                             wtr_da.rio.transform(), wtr.shape)
        n_reservoir     = int(np.sum(reservoir_mask))
        reservoir_valid = (wtr != NODATA) & reservoir_mask
        reservoir_cover = (float(np.sum(reservoir_valid)) / n_reservoir * 100) if n_reservoir else 0.0
        stats["reservoir_valid_pct"] = round(reservoir_cover, 2)
        if reservoir_cover < RESERVOIR_MIN_COVER:
            # Lake itself under-observed this date -> a low area would be a false
            # drawdown. Report NaN for the reservoir AND for water_km2: the lake is
            # the dominant AOI water body, so the AOI-wide water count is unreliable
            # on exactly these dates too (the missing area is water-dense, and water
            # cannot be area-normalized like snow since it is not evenly distributed).
            stats["reservoir_area_km2"] = float("nan")
            stats["water_km2"] = float("nan")
        else:
            stats["reservoir_area_km2"] = float(np.sum(water_mask & reservoir_mask) * pixel_km2)

    return stats


def orbit_phase(date_str: str, anchor: str) -> int:
    """12-day repeat phase of a date relative to an anchor date (both YYYYMMDD).
    Dates of the same Sentinel-1 relative orbit share the same phase. Phase 0
    means the date sits on the anchor's orbit."""
    date_ordinal = datetime.strptime(date_str, "%Y%m%d").date().toordinal()
    anchor_ordinal = datetime.strptime(anchor, "%Y%m%d").date().toordinal()
    return (date_ordinal - anchor_ordinal) % S1_REPEAT_DAYS


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
    #    EXCEPTION: a catchment-partial date whose RESERVOIR is fully observed is
    #    kept - it still carries a valid reservoir_area_km2 (its water_km2 was
    #    already NaN'd at extract time). This recovers the anchor-orbit cycles whose
    #    SAR swath misses the eastern headwaters but fully images the western lake.
    rows = [row for row in rows
            if row["valid_px_pct"] >= S1_FULL_COVER_PCT
            or row.get("reservoir_valid_pct", 0) >= RESERVOIR_MIN_COVER]
    if not rows:
        return []

    # 2. Group the full-coverage scenes by 12-day repeat phase (= relative orbit
    #    for a single satellite) and keep the track that best spans the period.
    ref_ordinal = min(row["date"] for row in rows).toordinal()
    by_phase: dict[int, list[dict]] = {}
    for row in rows:
        phase = (row["date"].toordinal() - ref_ordinal) % S1_REPEAT_DAYS
        by_phase.setdefault(phase, []).append(row)

    def span_days(phase_rows: list[dict]) -> int:
        return (max(r["date"] for r in phase_rows) - min(r["date"] for r in phase_rows)).days

    def median_cov(phase_rows: list[dict]) -> float:
        coverages = sorted(r["valid_px_pct"] for r in phase_rows)
        return coverages[len(coverages) // 2]

    best_phase = max(by_phase, key=lambda p: (len(by_phase[p]), span_days(by_phase[p])))
    kept = sorted(by_phase[best_phase], key=lambda r: r["date"])
    print(f"    orbit dedup: {n_before} -> {len(kept)} scenes (dropped partials, "
          f"kept 1 of {len(by_phase)} full-coverage orbits, phase {best_phase}, "
          f"median cov {median_cov(kept):.1f}%)")
    return kept


def _open_da(tile_data: bytes):
    """Open GeoTIFF bytes as an in-memory rioxarray DataArray (fully loaded)."""
    with MemoryFile(tile_data) as memfile:
        with memfile.open() as dataset:
            return rioxarray.open_rasterio(dataset).load()


def mosaic_tiles(tile_bytes: list[bytes], nodata: int, clip_box: tuple):
    """Reproject every tile to EPSG:4326, merge into one mosaic, pad to full AOI.

    Different MGRS tiles can sit in different UTM zones, so we reproject each to
    a common geographic CRS before merging. Padding to the AOI bbox makes the
    valid-pixel percentage AOI-relative (not tile-relative).
    """
    tile_arrays = []
    for single_tile_bytes in tile_bytes:
        try:
            tile_raster = _open_da(single_tile_bytes)
            tile_raster = tile_raster.rio.reproject("EPSG:4326",
                                                    resampling=Resampling.nearest,
                                                    nodata=nodata)
            tile_arrays.append(tile_raster)
        except Exception as error:
            print(f"(tile skip: {error})", end=" ")
    if not tile_arrays:
        return None

    merged = tile_arrays[0] if len(tile_arrays) == 1 else merge_arrays(tile_arrays, nodata=nodata)
    # Clip to exactly the AOI first: tiles from different UTM zones reproject to
    # an oversized bounding box with NoData corners, which would otherwise deflate
    # the valid-pixel percentage. Then pad back up in case data is smaller than AOI.
    merged = merged.rio.clip_box(clip_box[0], clip_box[1], clip_box[2], clip_box[3])
    merged = merged.rio.pad_box(clip_box[0], clip_box[1], clip_box[2], clip_box[3],
                                constant_values=nodata)
    return merged


def _pixel_size_km2_da(raster) -> float:
    """Approximate pixel area (km2) for a geographic (EPSG:4326) DataArray."""
    transform = raster.rio.transform()
    res_x, res_y = abs(transform.a), abs(transform.e)
    left, bottom, right, top = raster.rio.bounds()
    center_lat = (bottom + top) / 2
    return (res_x * 111.32 * np.cos(np.radians(center_lat))) * (res_y * 110.574)


def extract_hls_stats(wtr_da, glaciers: gpd.GeoDataFrame | None,
                      catchment: gpd.GeoDataFrame | None = None) -> dict:
    """
    Water + snow/glacier stats from a mosaicked DSWx-HLS B01_WTR DataArray.
    wtr_da is an EPSG:4326 mosaic (output of mosaic_tiles).

    Cloud masking uses the WTR layer's own quality flag (value 253 =
    cloud/cloud-shadow), the product's authoritative determination - no separate
    B09 layer is needed. Always returns the stats incl. valid_px_pct and
    cloud_cover_percent; the caller decides whether coverage / cloud are acceptable.

    If a catchment polygon is given, all stats (water, snow, glacier) are counted
    ONLY inside the drainage basin, and valid_px_pct / cloud_cover_percent are
    catchment-relative - so the Kazbek glaciers (outside the Zhinvali basin) and
    the empty corners of the larger clip_box do not enter the numbers.
    """
    wtr       = wtr_da.values[0]
    pixel_km2 = _pixel_size_km2_da(wtr_da)
    transform = wtr_da.rio.transform()
    raster_crs = wtr_da.rio.crs
    shape     = wtr.shape

    catchment_mask = _catchment_mask(catchment, wtr_da, shape)
    n_catchment    = int(np.sum(catchment_mask)) or wtr.size

    valid = (wtr != NODATA) & catchment_mask   # has data (not fill); incl. cloud-masked 253
    cloud = (wtr == CLOUD_WTR_VALUE) & catchment_mask  # product's own cloud/shadow flag

    n_valid = int(np.sum(valid))
    valid_pct = float(n_valid) / n_catchment * 100
    cloud_pct = float(np.sum(cloud & valid)) / max(n_valid, 1) * 100

    usable      = valid & ~cloud
    water_mask  = np.isin(wtr, list(WATER_VALUES)) & usable
    snow_mask   = (wtr == SNOW_VALUE) & usable

    glacier_mask = rasterize_glaciers(glaciers, raster_crs, transform, shape)

    snow_on_glacier   = snow_mask & glacier_mask
    snow_seasonal     = snow_mask & ~glacier_mask
    glacier_total_km2 = float(np.sum(glacier_mask & usable) * pixel_km2)
    bare_ice_km2      = max(0.0, glacier_total_km2 - float(np.sum(snow_on_glacier) * pixel_km2))

    # Coverage/cloud-corrected seasonal snow. seasonal_snow_km2 is the raw count
    # and is biased LOW whenever part of the basin is NoData (swath edge) or cloud,
    # because that area is implicitly counted as snow-free. seasonal_snow_frac is
    # the snow share of the OBSERVED (valid, cloud-free) non-glacier basin, and
    # seasonal_snow_km2_est scales that share back to the FULL non-glacier basin -
    # i.e. it fills the unobserved part with the observed snow rate. This makes the
    # snow series comparable across dates with different coverage. (Assumes the
    # unobserved area has a similar snow rate; the MIN_VALID_PCT / MAX_CLOUD_PCT
    # floors keep that extrapolation modest.)
    nonglacier_full = catchment_mask & ~glacier_mask
    obs_land        = usable & ~glacier_mask
    n_obs_land      = int(np.sum(obs_land))
    n_full_land     = int(np.sum(nonglacier_full))
    seasonal_snow_frac    = float(np.sum(snow_seasonal)) / n_obs_land if n_obs_land else 0.0
    seasonal_snow_km2_est = seasonal_snow_frac * float(n_full_land * pixel_km2)

    return {
        "water_area_km2":          float(np.sum(water_mask) * pixel_km2),
        "seasonal_snow_km2":       float(np.sum(snow_seasonal) * pixel_km2),
        "seasonal_snow_frac":      round(seasonal_snow_frac, 4),
        "seasonal_snow_km2_est":   round(seasonal_snow_km2_est, 2),
        "snow_on_glacier_km2":     float(np.sum(snow_on_glacier) * pixel_km2),
        "bare_ice_km2":            bare_ice_km2,
        "glacier_total_km2":       glacier_total_km2,
        "obs_land_pct":            round(float(n_obs_land) / (n_full_land or 1) * 100, 2),
        "cloud_cover_percent":     round(cloud_pct, 2),
        "valid_px_pct":            round(valid_pct, 2),
    }


# ─────────────────────────────────────────────
# PER-DATE CACHE (resume across runs)
# ─────────────────────────────────────────────
# Computing a date means reading + mosaicking tiles from the store (the slow
# part). We cache the per-date result - including skipped dates and the reason -
# so re-runs only touch dates not seen before. Pass refresh=True to recompute.

def load_cache(name: str) -> dict:
    cache_path = CACHE_DIR / f"{name}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            return {}
    return {}


def save_cache(name: str, cache: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{name}.json").write_text(json.dumps(cache))


def _needs_recompute(entry: dict, sensor: str) -> bool:
    """For --recompute: does this cached date need a fresh store read+compute, or
    can we trust its cached skip decision? We only re-read dates that produce (or
    would now produce) an output row - 'ok' dates and dates that newly pass the
    CURRENT thresholds. Cloud skips and genuine below-threshold partials are kept
    as-is (no expensive store read), since they never contribute a row."""
    if entry.get("status") == "ok":
        return True
    reason = entry.get("reason")
    if reason == "coverage":
        thr = S1_MIN_VALID_PCT if sensor == "s1" else MIN_VALID_PCT
        if entry.get("valid_px_pct", 0) >= thr:
            return True
        # S1: a catchment-partial date that nonetheless fully observed the reservoir
        # now yields a reservoir-only row (see the gate in run_pipeline), so it must
        # be re-read too rather than trusted as a permanent skip.
        return (sensor == "s1"
                and entry.get("reservoir_valid_pct", 0) >= RESERVOIR_MIN_COVER)
    if reason == "cloud":
        return entry.get("cloud_cover_percent", 100) <= MAX_CLOUD_PCT
    return False  # few_tiles / no_tiles: cheap pre-read skip, keep


def prepare_cache(name: str, sensor: str, refresh: bool, recompute: bool) -> dict:
    """Cache to start the run from. --refresh wipes it (full recompute). --recompute
    keeps it but drops the entries that need a fresh read (ok / now-qualifying), so
    those get reprocessed while known-skip dates are never read from the store again."""
    if refresh:
        return {}
    cache = load_cache(name)
    if recompute:
        before = len(cache)
        cache = {date_str: entry for date_str, entry in cache.items()
                 if not _needs_recompute(entry, sensor)}
        print(f"  recompute: re-reading {before - len(cache)} dates, "
              f"keeping {len(cache)} cached skip-decisions (no store read)")
    return cache


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
    with csv_path.open("w", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved: {csv_path} ({len(rows)} rows)")

    if HAS_PANDAS:
        output_df = pd.DataFrame(rows)[fieldnames]
        output_df["date"] = pd.to_datetime(output_df["date"])
        parquet_path = OUTPUT_DIR / f"{stem}.parquet"
        output_df.to_parquet(parquet_path, index=False)
        print(f"  Saved: {parquet_path}")


def parse_filename(title: str) -> tuple[str, str, str] | None:
    """Parse site, date, layer from a filename.
    Handles both new MGRS-tagged names (site_date_TILE_layer_clipped.tif)
    and old names without a tile id (site_date_layer_clipped.tif).
    """
    match = re.match(
        r"(enguri|zhinvali)_(\d{8})_(?:T\d{2}[A-Z]{3}_)?(B\d+[\w\-]+?)_clipped\.tif",
        title,
    )
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main(skip_s1: bool = False, skip_hls: bool = False, refresh: bool = False,
         recompute: bool = False):
    store = get_store()

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

    # Top-level OPERA_DSWx folder holding hls/ and s1/
    opera_root = store.get_folder_id(DATA_ROOT, ROOT)
    if not opera_root:
        print(f"  '{DATA_ROOT}' folder not found - run download_hls.py / download_s1.py first")
        return

    # ── DSWx-S1 ──────────────────────────────
    if skip_s1:
        print("\n--- DSWx-S1 skipped (--skip-s1) ---")
    print("\n--- DSWx-S1 B01_WTR ---" if not skip_s1 else "", end="")
    s1_root = store.get_folder_id("s1", opera_root) if not skip_s1 else None

    for aoi in ([] if skip_s1 else [AOI_1, AOI_2]):
        site = aoi["name"]
        rows: list[dict] = []

        reservoir = load_reservoir(site)
        if reservoir is not None:
            print(f"  reservoir polygon loaded for {site}")
        catchment = load_catchment(site)
        if catchment is not None:
            print(f"  catchment polygon loaded for {site} (stats masked to basin)")

        site_folder = store.get_folder_id(site, s1_root) if s1_root else None
        if not site_folder:
            print(f"  Folder not found for {site} (S1) - skipping")
            continue

        wtr_tiles = [tile for tile in store.list_tifs(site_folder)
                     if "B01_WTR" in tile["title"]]

        # Group ALL tiles per date (multiple MGRS tiles cover the AOI)
        wtr_by_date: dict[str, list] = {}
        for tile in wtr_tiles:
            parsed = parse_filename(tile["title"])
            if not parsed:
                continue
            _, date_str, _ = parsed
            wtr_by_date.setdefault(date_str, []).append(tile)

        dates = sorted(wtr_by_date.keys())

        # Keep only the chosen relative orbit (one 12-day phase). The other
        # phases would be dropped by dedup_single_orbit anyway, so pre-filtering
        # here means we download just that orbit (~1/12 of the dates) instead of
        # all of them, and the series is anchored to the exact chosen cycle.
        anchor = aoi.get("s1_anchor")
        if anchor:
            dates = [d for d in dates if orbit_phase(d, anchor) == 0]

        cache = prepare_cache(f"{site}_s1", "s1", refresh, recompute)
        print(f"  {site}: {len(wtr_tiles)} B01_WTR files, {len(dates)} dates "
              f"on the {anchor} orbit ({len(cache)} cached)" if anchor else
              f"  {site}: {len(wtr_tiles)} B01_WTR files, {len(dates)} dates "
              f"({len(cache)} cached)")

        for index, date_str in enumerate(dates, 1):
            wtr_files = wtr_by_date[date_str]
            n_tiles = len(wtr_files)
            prefix = f"  [{index:>3}/{len(dates)}] {site}_s1_{date_str} ({n_tiles} tiles) ..."
            cached = cache.get(date_str)
            if cached is not None:
                # Recompute "ok" dates that predate the reservoir polygon so the
                # new reservoir_area_km2 gets filled in; otherwise honour the cache.
                stale = (reservoir is not None and cached.get("status") == "ok"
                         and "reservoir_area_km2" not in cached)
                if not stale:
                    continue
            print(prefix, end=" ", flush=True)
            if n_tiles < MIN_TILES:
                print(f"skipped (only {n_tiles} tile < {MIN_TILES}, cannot cover AOI)")
                cache[date_str] = {"status": "skip", "reason": "few_tiles"}
                save_cache(f"{site}_s1", cache)
                continue
            try:
                wtr_mosaic = mosaic_tiles([store.read_bytes(tile) for tile in wtr_files],
                                          NODATA, aoi["clip_box"])
                if wtr_mosaic is None:
                    print("skipped (no readable tiles)")
                    cache[date_str] = {"status": "skip", "reason": "no_tiles"}
                    save_cache(f"{site}_s1", cache)
                    continue
                stats = extract_s1_stats(wtr_mosaic, reservoir, catchment)
                # Reservoir-decoupled gate: keep a date if EITHER the whole
                # catchment is fully imaged OR the reservoir itself is fully
                # observed. On the anchor orbit the eastern Svaneti headwaters fall
                # outside the SAR swath on many cycles (catchment cov ~59%) while the
                # western reservoir is still 100% seen - those dates carry a valid
                # reservoir_area_km2 and must not be dropped. The catchment-wide
                # water_km2, however, is only comparable when the basin is fully
                # imaged, so it is NaN'd on the partial-catchment dates.
                catchment_full = stats["valid_px_pct"] >= S1_MIN_VALID_PCT
                reservoir_seen = stats.get("reservoir_valid_pct", 0) >= RESERVOIR_MIN_COVER
                if not (catchment_full or reservoir_seen):
                    print(f"skipped (coverage {stats['valid_px_pct']:.1f}% < {S1_MIN_VALID_PCT}%, "
                          f"reservoir {stats.get('reservoir_valid_pct', 0):.1f}% < {RESERVOIR_MIN_COVER}%)")
                    cache[date_str] = {"status": "skip", "reason": "coverage", **stats}
                else:
                    if not catchment_full:
                        stats["water_km2"] = float("nan")
                    cache[date_str] = {"status": "ok", **stats}
                    reservoir_area = stats.get("reservoir_area_km2")
                    reservoir_str = (f"  reservoir={reservoir_area:.2f} km2"
                                     if reservoir_area is not None else "")
                    print(f"water={stats['water_km2']:.2f} km2{reservoir_str}  "
                          f"(cov {stats['valid_px_pct']:.1f}%)")
                save_cache(f"{site}_s1", cache)
            except Exception as error:
                print(f"ERROR: {error}")  # not cached -> retried next run

        rows = cache_rows(cache)
        # The cache may still hold ok-dates of other phases from earlier full
        # runs; restrict the series to the chosen orbit before writing.
        if anchor:
            rows = [r for r in rows
                    if orbit_phase(r["date"].strftime("%Y%m%d"), anchor) == 0]
        if rows:
            rows = dedup_single_orbit(rows)
            fieldnames = ["date", "water_km2", "valid_px_pct"]
            if any("reservoir_area_km2" in r for r in rows):
                fieldnames[2:2] = ["reservoir_area_km2", "reservoir_valid_pct"]
            save_outputs(rows, fieldnames, f"{site}_s1_timeseries")

    # ── DSWx-HLS ─────────────────────────────
    if skip_hls:
        print("\n--- DSWx-HLS skipped (--skip-hls) ---")
    print("\n--- DSWx-HLS B01_WTR (cloud mask = WTR 253) ---" if not skip_hls else "", end="")
    hls_root = store.get_folder_id("hls", opera_root) if not skip_hls else None

    if not skip_hls and not hls_root:
        print("  HLS folder not found - run download_hls.py first")
        return

    for aoi in ([] if skip_hls else [AOI_1, AOI_2]):
        site      = aoi["name"]
        glaciers  = glacier_masks.get(site)
        catchment = load_catchment(site)
        if catchment is not None:
            print(f"  catchment polygon loaded for {site} (stats masked to basin)")
        rows: list[dict] = []

        site_folder = store.get_folder_id(site, hls_root)
        if not site_folder:
            print(f"  Folder not found for {site} (HLS)")
            continue

        hls_tiles = store.list_tifs(site_folder)

        # Group ALL WTR tiles per date (multiple MGRS tiles cover the AOI).
        # Cloud masking comes from the WTR layer itself (value 253), so the
        # B09_CLOUD layer is no longer needed.
        wtr_by_date: dict[str, list] = {}
        for tile in hls_tiles:
            parsed = parse_filename(tile["title"])
            if not parsed:
                continue
            _, date_str, layer = parsed
            if "B01_WTR" in layer:
                wtr_by_date.setdefault(date_str, []).append(tile)

        dates = sorted(wtr_by_date.keys())
        cache = prepare_cache(f"{site}_hls", "hls", refresh, recompute)
        print(f"  {site}: {len(dates)} WTR dates ({len(cache)} cached)")

        for index, date_str in enumerate(dates, 1):
            wtr_files = wtr_by_date[date_str]
            n_tiles = len(wtr_files)
            prefix = f"  [{index:>3}/{len(dates)}] {site}_hls_{date_str} ({n_tiles} tiles) ..."
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
                wtr_mosaic = mosaic_tiles([store.read_bytes(tile) for tile in wtr_files],
                                          NODATA, aoi["clip_box"])
                if wtr_mosaic is None:
                    print("skipped (no readable tiles)")
                    cache[date_str] = {"status": "skip", "reason": "no_tiles"}
                    save_cache(f"{site}_hls", cache)
                    continue

                stats = extract_hls_stats(wtr_mosaic, glaciers, catchment)
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
            except Exception as error:
                print(f"ERROR: {error}")  # not cached -> retried next run

        rows = cache_rows(cache)
        if rows:
            save_outputs(
                rows,
                ["date", "water_area_km2", "seasonal_snow_km2", "seasonal_snow_frac",
                 "seasonal_snow_km2_est", "snow_on_glacier_km2", "bare_ice_km2",
                 "glacier_total_km2", "obs_land_pct", "cloud_cover_percent", "valid_px_pct"],
                f"{site}_timeseries",
            )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract DSWx time series from the tile store.")
    parser.add_argument("--skip-s1", action="store_true", help="skip the DSWx-S1 section")
    parser.add_argument("--skip-hls", action="store_true", help="skip the DSWx-HLS section")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the per-date cache and recompute everything")
    parser.add_argument("--recompute", action="store_true",
                        help="reprocess only 'ok' / now-qualifying dates; keep cached "
                             "skip decisions (cloud / below-threshold) without re-reading "
                             "them from the store - much faster after a logic/threshold change")
    args = parser.parse_args()
    main(skip_s1=args.skip_s1, skip_hls=args.skip_hls,
         refresh=args.refresh, recompute=args.recompute)
