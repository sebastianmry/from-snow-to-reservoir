"""
FROM SNOW TO RESERVOIR - Reservoir Footprint from S1 Water Extent
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

HydroLAKES underestimates the study reservoirs (Enguri 4.85 km2 vs. real
~13 km2 - it captures only the lower pool, not the long valley arms). This
script derives the true reservoir footprint from the data we already have:
the Sentinel-1 (SAR, cloud-independent) maximum/persistent water extent.

Approach (one S1 pass per AOI, low memory - one mosaic in RAM at a time):
  1. Mosaic every S1 B01_WTR date to EPSG:4326 (reuses extract_timeseries),
     keep only full-coverage scenes (>= S1_FULL_COVER_PCT) on a fixed grid.
  2. Accumulate a per-pixel water-frequency map = water_obs / valid_obs.
  3. Persistent-water mask = freq >= FREQ_THRESH (pool that is water most of
     the time + its shrinking margins; rejects speckle and transient flooding).
  4. Anchor to the real reservoir: keep the connected component(s) that touch
     the HydroLAKES seed polygon (download_reservoirs.py). Close gaps, fill holes.
  5. Vectorize to a polygon -> static_data/reservoirs.geojson (consumed by
     extract_timeseries.py for reservoir_area_km2 and by app.py for the map).
  Also writes static_data/{site}_s1_waterfreq.tif for sanity-checking.

Run once, after the S1 download and download_reservoirs.py:
    python download_reservoirs.py     # HydroLAKES seed
    python derive_reservoir.py        # grow seed to true S1 footprint
"""

import numpy as np
import geopandas as gpd
import pandas as pd
from tqdm import tqdm
from shapely.geometry import shape as shapely_shape, Point
from shapely.ops import unary_union
from scipy import ndimage
from rasterio.features import shapes as rio_shapes, rasterize
from rasterio.enums import Resampling

# Reuse the proven store + mosaic machinery from the extraction pipeline.
from extract_timeseries import (
    parse_filename, mosaic_tiles,
    NODATA, WATER_VALUES, MIN_TILES, S1_FULL_COVER_PCT,
    STATIC_DIR, DATA_ROOT, AOI_1, AOI_2,
)
# Local tile store (filesystem under PIPELINE_LOCAL_DIR).
from storage import get_store, ROOT

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

# Dam points (lon, lat) - anchor + fallback if the seed polygon misses water.
DAMS = {
    "enguri":   (42.032, 42.753),
    "zhinvali": (44.771, 42.133),
}

# Occurrence-based water masking (cf. JRC Global Surface Water, Pekel et al. 2016):
# a pixel belongs to the reservoir if it was water in >= FREQ_THRESH of valid obs.
# 0.25 = "regularly inundated" (captures the fluctuating pool, rejects speckle).
# Sensitivity-checked: envelope area varies only ~+-9% (Enguri) / ~+-5% (Zhinvali)
# across thresholds 0.10-0.50, smooth & monotonic with no jump -> no river leakage,
# so 0.25 is a robust mid-range operating point, not a sensitive magic number.
FREQ_THRESH    = 0.25   # pixel counts as reservoir if water in >= 25% of valid obs
MIN_OBS_FRAC   = 0.30   # require at least this fraction of scenes to have seen the pixel
MIN_OBS_ABS    = 3      # ...but never fewer than this many observations
CLOSE_ITERS    = 2      # binary closing to bridge small gaps before labelling
SEED_DILATE_ITERS = 4   # dilate the seed before component selection (px tolerance)
UTM_EPSG       = "EPSG:32638"  # UTM 38N - both AOIs, for honest km2

SEED_GEOJSON   = STATIC_DIR / "reservoirs_hydrolakes.geojson"
OUTPUT_GEOJSON = STATIC_DIR / "reservoirs.geojson"


# ─────────────────────────────────────────────
# WATER-FREQUENCY ACCUMULATION
# ─────────────────────────────────────────────

def build_water_frequency(store, site_folder, clip_box):
    """One pass over the S1 dates -> (freq, valid_count, reference_mosaic).

    All mosaics are aligned to the first full-coverage scene's grid via
    reproject_match, so the counts accumulate pixel-for-pixel. Only one mosaic
    is held in memory at a time; the accumulators are the only persistent arrays.
    """
    wtr_tiles = [tile for tile in store.list_tifs(site_folder)
                 if "B01_WTR" in tile["title"]]

    by_date: dict[str, list] = {}
    for tile in wtr_tiles:
        parsed = parse_filename(tile["title"])
        if parsed:
            by_date.setdefault(parsed[1], []).append(tile)

    dates = sorted(by_date)
    print(f"    {len(wtr_tiles)} B01_WTR files, {len(dates)} dates")

    reference = None
    water_count = valid_count = None
    n_used = 0

    progress = tqdm(dates, desc="    S1 dates", unit="date")
    for date_str in progress:
        tiles = by_date[date_str]
        if len(tiles) < MIN_TILES:
            continue
        try:
            mosaic = mosaic_tiles([store.read_bytes(tile) for tile in tiles], NODATA, clip_box)
        except Exception as error:
            progress.write(f"      {date_str} mosaic error: {error}")
            continue
        if mosaic is None:
            continue

        first_band = mosaic.values[0]
        valid_pct = float(np.sum(first_band != NODATA)) / first_band.size * 100
        if valid_pct < S1_FULL_COVER_PCT:
            continue  # partial orbit - would bias the envelope

        if reference is None:
            reference = mosaic
            water_count = np.zeros(first_band.shape, dtype=np.int32)
            valid_count = np.zeros(first_band.shape, dtype=np.int32)
        else:
            mosaic = mosaic.rio.reproject_match(reference, resampling=Resampling.nearest)

        band = mosaic.values[0]
        valid = band != NODATA
        water = np.isin(band, list(WATER_VALUES)) & valid
        water_count += water
        valid_count += valid
        n_used += 1
        progress.set_postfix(used=n_used)

    if reference is None:
        return None, None, None, 0

    freq = water_count / np.maximum(valid_count, 1)
    return freq, valid_count, reference, n_used


# ─────────────────────────────────────────────
# ENVELOPE EXTRACTION
# ─────────────────────────────────────────────

def seed_mask_on_grid(reference, clip_box, site):
    """Rasterize the HydroLAKES seed polygon (+ dam point) onto the grid."""
    transform = reference.rio.transform()
    shape = reference.values[0].shape
    mask = np.zeros(shape, dtype=bool)

    if SEED_GEOJSON.exists():
        seed = gpd.read_file(SEED_GEOJSON)
        seed = seed[seed["aoi"] == site]
        if not seed.empty:
            if seed.crs is None or seed.crs.to_epsg() != 4326:
                seed = seed.to_crs("EPSG:4326")
            seed_geoms = [geom for geom in seed.geometry
                          if geom is not None and geom.is_valid]
            if seed_geoms:
                mask = rasterize([(geom, 1) for geom in seed_geoms], out_shape=shape,
                                 transform=transform, fill=0,
                                 dtype=np.uint8).astype(bool)

    # Always include the dam pixel as a fallback anchor.
    dam_lon, dam_lat = DAMS[site]
    col_float, row_float = ~transform * (dam_lon, dam_lat)
    row_idx, col_idx = int(row_float), int(col_float)
    if 0 <= row_idx < shape[0] and 0 <= col_idx < shape[1]:
        mask[row_idx, col_idx] = True
    return mask


def extract_envelope(freq, valid_count, reference, n_used, clip_box, site):
    """Persistent-water mask -> seed-anchored connected component -> polygon."""
    min_obs = max(MIN_OBS_ABS, int(MIN_OBS_FRAC * n_used))
    persistent = (valid_count >= min_obs) & (freq >= FREQ_THRESH)
    if not persistent.any():
        print(f"    {site}: no persistent water found (min_obs={min_obs}) - skipped")
        return None

    # Bridge small gaps (narrow channels, single-pixel speckle holes).
    closed = ndimage.binary_closing(persistent, iterations=CLOSE_ITERS)

    # Keep only components touching the (dilated) seed - the actual reservoir,
    # not unrelated water bodies elsewhere in the AOI.
    seed = seed_mask_on_grid(reference, clip_box, site)
    seed_dilated = ndimage.binary_dilation(seed, iterations=SEED_DILATE_ITERS)

    labels, _n_labels = ndimage.label(closed)
    keep_ids = set(np.unique(labels[seed_dilated & (labels > 0)]))
    keep_ids.discard(0)
    if not keep_ids:
        print(f"    {site}: seed touches no persistent water - skipped")
        return None

    envelope = np.isin(labels, list(keep_ids))
    envelope = ndimage.binary_fill_holes(envelope)  # solid reservoir interior

    transform = reference.rio.transform()
    envelope_geoms = [shapely_shape(geom) for geom, value in
                      rio_shapes(envelope.astype(np.uint8), mask=envelope, transform=transform)
                      if value == 1]
    if not envelope_geoms:
        return None
    envelope_poly = unary_union(envelope_geoms)

    area_km2 = gpd.GeoSeries([envelope_poly], crs="EPSG:4326").to_crs(UTM_EPSG).area.iloc[0] / 1e6
    print(f"    {site}: envelope from {n_used} scenes (min_obs={min_obs}), "
          f"{int(envelope.sum())} px -> {area_km2:.2f} km2")
    return {"aoi": site, "source": "s1_envelope", "n_obs": n_used,
            "freq_thresh": FREQ_THRESH, "area_km2": round(float(area_km2), 3),
            "geometry": envelope_poly}


def save_waterfreq_tif(freq, reference, site):
    """Write the raw water-frequency map for visual sanity-checking."""
    freq_raster = reference.copy()
    freq_raster.values[0] = (freq * 100).astype(np.float32)
    freq_raster = freq_raster.astype("float32")
    out_path = STATIC_DIR / f"{site}_s1_waterfreq.tif"
    freq_raster.rio.to_raster(out_path)
    print(f"    wrote {out_path}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 55)
    print("Reservoir footprint from S1 water extent")
    print("=" * 55)

    store = get_store()
    opera_root = store.get_folder_id(DATA_ROOT, ROOT)
    if not opera_root:
        print(f"'{DATA_ROOT}' folder not found - run download_s1.py first")
        return
    s1_root = store.get_folder_id("s1", opera_root)
    if not s1_root:
        print("s1 folder not found - run download_s1.py first")
        return

    records = []
    for aoi in [AOI_1, AOI_2]:
        site = aoi["name"]
        print(f"\n--- {site} ---")
        site_folder = store.get_folder_id(site, s1_root)
        if not site_folder:
            print(f"    folder not found for {site} - skipping")
            continue

        freq, valid_count, reference, n_used = build_water_frequency(
            store, site_folder, aoi["clip_box"])
        if reference is None:
            print(f"    {site}: no full-coverage S1 scenes - skipped")
            continue

        save_waterfreq_tif(freq, reference, site)
        record = extract_envelope(freq, valid_count, reference, n_used,
                                  aoi["clip_box"], site)
        if record:
            records.append(record)

    if not records:
        print("\nNo reservoir envelopes derived.")
        return

    reservoirs_gdf = gpd.GeoDataFrame(pd.DataFrame(records), geometry="geometry",
                                      crs="EPSG:4326")
    reservoirs_gdf.to_file(OUTPUT_GEOJSON, driver="GeoJSON")
    print(f"\nSaved: {OUTPUT_GEOJSON} ({len(reservoirs_gdf)} polygons)")
    print("Next: python extract_timeseries.py  (adds reservoir_area_km2)")


if __name__ == "__main__":
    main()
