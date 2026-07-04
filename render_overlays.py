"""
FROM SNOW TO RESERVOIR - Raster Overlay Pre-Rendering
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Pre-renders the DSWx scenes as small, coloured PNG overlays so the dashboard can
step through them on a date slider WITHOUT doing any raster work at runtime (the
laptop is weak and the TIFs are large). All the heavy lifting -
read from the store, mosaic, classify, downsample - happens once here; the app
only loads finished PNGs via folium.ImageOverlay.

For each AOI and sensor it renders exactly the dates that made it into the final
timeseries parquet (so the scenes match the charts), colouring the mosaic:

  S1 (water):
    water (1-5)              -> blue
  HLS (snow / ice):
    water (1-5)              -> blue
    seasonal snow (252)      -> white
    snow on glacier (252)    -> light blue
    bare glacier ice         -> teal
    cloud (253) / NoData     -> transparent
  Outside the catchment      -> transparent (so only the basin is painted)

Output:
    static_data/overlays/{site}/{sensor}/{YYYYMMDD}.png
    static_data/overlays/{site}/{sensor}/bounds.json   (one [[S,W],[N,E]] box)

Usage:
    python extract_timeseries.py     # first, to produce the parquets + cache
    python render_overlays.py                 # all AOIs + both sensors
    python render_overlays.py enguri s1       # filter by AOI and/or sensor
    python render_overlays.py --refresh       # re-render existing PNGs too
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image
from rasterio.enums import Resampling

from aoi_config import AOIS, AOI_1, AOI_2
from extract_timeseries import (
    parse_filename, mosaic_tiles, load_catchment, load_glacier_mask,
    find_rgi, rasterize_glaciers,
    NODATA, WATER_VALUES, SNOW_VALUE, CLOUD_WTR_VALUE, DATA_ROOT,
)
# Local tile store (filesystem under PIPELINE_LOCAL_DIR).
from storage import get_store, ROOT

OVERLAY_DIR = Path("static_data") / "overlays"
SENSORS     = ["s1", "hls"]
# Web map needs nothing close to native 30 m; cap the longer side at this many
# pixels so the PNGs stay small (fast to load, light on the laptop).
MAX_DIM = 900

# RGBA colours. Tuned for clear separation on the light CartoDB-positron basemap:
# glaciers share the violet identity of the static map (light = snow on them, dark
# = bare ice), seasonal snow is cyan (white is invisible on the white basemap), and
# water stays blue. Glacier violet matches the static map (fill #cfc6e8 / #7e6fb8).
C_WATER          = (31, 111, 192, 245)    # vivid blue, like the reservoir polygon
C_SNOW_SEASONAL  = (90, 200, 230, 240)    # cyan = fresh/seasonal snow (pops on white)
C_SNOW_GLACIER   = (142, 124, 195, 245)   # medium violet = snow on a glacier (stands
                                          # out from the cyan snow field)
C_BARE_ICE       = (94, 75, 139, 250)     # dark violet = exposed glacier ice (melt)
TRANSPARENT      = (0, 0, 0, 0)


def _parquet_dates(site: str, sensor: str) -> list[str]:
    """The dates (YYYYMMDD) that made it into the final timeseries - so the
    overlays line up exactly with what the charts show (dedup already applied)."""
    stem = f"{site}_s1_timeseries" if sensor == "s1" else f"{site}_timeseries"
    parquet_path = Path(f"{stem}.parquet")
    if not parquet_path.exists():
        print(f"  no {parquet_path} - run extract_timeseries.py first; skipping {site}/{sensor}")
        return []
    timeseries_df = pd.read_parquet(parquet_path)
    return [pd.Timestamp(d).strftime("%Y%m%d") for d in timeseries_df["date"]]


def _downsample(raster):
    """Reproject to a coarser grid (nearest, so class values stay intact) with the
    longer side capped at MAX_DIM. Bounds stay the AOI clip_box."""
    _, height, width = raster.shape
    scale = min(1.0, MAX_DIM / max(height, width))
    if scale >= 1.0:
        return raster
    new_height, new_width = max(1, int(height * scale)), max(1, int(width * scale))
    return raster.rio.reproject(raster.rio.crs, shape=(new_height, new_width),
                                resampling=Resampling.nearest, nodata=NODATA)


def _classify_s1(wtr: np.ndarray, catchment_mask: np.ndarray) -> np.ndarray:
    """RGBA image: water blue, everything else transparent."""
    rgba = np.zeros((*wtr.shape, 4), dtype=np.uint8)
    water = np.isin(wtr, list(WATER_VALUES)) & catchment_mask
    rgba[water] = C_WATER
    return rgba


def _classify_hls(wtr: np.ndarray, catchment_mask: np.ndarray,
                  glacier_mask: np.ndarray) -> np.ndarray:
    """RGBA image: water / seasonal snow / snow-on-glacier / bare ice; cloud and
    NoData transparent; outside the catchment transparent."""
    rgba = np.zeros((*wtr.shape, 4), dtype=np.uint8)
    usable = (wtr != NODATA) & (wtr != CLOUD_WTR_VALUE) & catchment_mask

    water         = np.isin(wtr, list(WATER_VALUES)) & usable
    snow          = (wtr == SNOW_VALUE) & usable
    snow_seasonal = snow & ~glacier_mask
    snow_glacier  = snow & glacier_mask
    bare_ice      = glacier_mask & usable & ~snow & ~water

    rgba[bare_ice]      = C_BARE_ICE
    rgba[snow_seasonal] = C_SNOW_SEASONAL
    rgba[snow_glacier]  = C_SNOW_GLACIER
    rgba[water]         = C_WATER
    return rgba


def _list_wtr_by_date(store, folder_id: str) -> dict[str, list]:
    """Group the B01_WTR tiles in a store folder by acquisition date."""
    by_date: dict[str, list] = {}
    for tile in store.list_tifs(folder_id):
        parsed = parse_filename(tile["title"])
        if not parsed:
            continue
        _, date_str, layer = parsed
        if "B01_WTR" in layer:
            by_date.setdefault(date_str, []).append(tile)
    return by_date


def render_site_sensor(store, aoi: dict, sensor: str, sensor_root: str,
                       refresh: bool):
    site = aoi["name"]
    dates = _parquet_dates(site, sensor)
    if not dates:
        return

    out_dir = OVERLAY_DIR / site / sensor
    out_dir.mkdir(parents=True, exist_ok=True)
    min_lon, min_lat, max_lon, max_lat = aoi["clip_box"]
    (out_dir / "bounds.json").write_text(json.dumps(
        {"bounds": [[min_lat, min_lon], [max_lat, max_lon]]}))

    site_folder = store.get_folder_id(site, sensor_root)
    if not site_folder:
        print(f"  Store folder not found for {site}/{sensor} - skipping")
        return
    by_date = _list_wtr_by_date(store, site_folder)

    catchment = load_catchment(site)
    glaciers = None
    if sensor == "hls":
        rgi = find_rgi()
        glaciers = load_glacier_mask(rgi, aoi["clip_box"]) if rgi else None

    print(f"  {site}/{sensor}: {len(dates)} scenes to render")
    rendered = skipped = 0
    for index, date_str in enumerate(dates, 1):
        png_path = out_dir / f"{date_str}.png"
        if png_path.exists() and not refresh:
            skipped += 1
            continue
        tiles = by_date.get(date_str)
        if not tiles:
            print(f"  [{index:>3}/{len(dates)}] {date_str}: no tiles in store - skip")
            continue
        try:
            mosaic = mosaic_tiles([store.read_bytes(tile) for tile in tiles],
                                  NODATA, aoi["clip_box"])
            if mosaic is None:
                print(f"  [{index:>3}/{len(dates)}] {date_str}: no readable tiles - skip")
                continue
            mosaic = _downsample(mosaic)
            wtr = mosaic.values[0]
            catchment_mask = (rasterize_glaciers(catchment, mosaic.rio.crs,
                                                 mosaic.rio.transform(), wtr.shape)
                              if catchment is not None and not catchment.empty
                              else np.ones(wtr.shape, dtype=bool))
            if sensor == "s1":
                rgba = _classify_s1(wtr, catchment_mask)
            else:
                glacier_mask = rasterize_glaciers(glaciers, mosaic.rio.crs,
                                                  mosaic.rio.transform(), wtr.shape)
                rgba = _classify_hls(wtr, catchment_mask, glacier_mask)
            Image.fromarray(rgba, "RGBA").save(png_path, optimize=True)
            rendered += 1
            print(f"  [{index:>3}/{len(dates)}] {date_str}: "
                  f"{wtr.shape[1]}x{wtr.shape[0]} px -> {png_path.name}")
        except Exception as error:
            print(f"  [{index:>3}/{len(dates)}] {date_str}: ERROR {error}")
    print(f"  {site}/{sensor}: {rendered} rendered, {skipped} already present")


def main():
    parser = argparse.ArgumentParser(description="Pre-render DSWx scenes as PNG overlays.")
    parser.add_argument("filters", nargs="*",
                        help="optional AOI (enguri/zhinvali) and/or sensor (s1/hls)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-render PNGs that already exist")
    args = parser.parse_args()

    filters = [token.lower() for token in args.filters]
    want_aoi    = next((t for t in filters if t in {x["name"] for x in AOIS.values()}), None)
    want_sensor = next((t for t in filters if t in SENSORS), None)

    store = get_store()
    opera_root = store.get_folder_id(DATA_ROOT, ROOT)
    if not opera_root:
        print(f"'{DATA_ROOT}' folder not found - run the download scripts first")
        sys.exit(1)

    sensor_roots = {sensor: store.get_folder_id(sensor, opera_root) for sensor in SENSORS}

    for aoi in [AOI_1, AOI_2]:
        if want_aoi and aoi["name"] != want_aoi:
            continue
        print(f"\n=== {aoi['label']} ===")
        for sensor in SENSORS:
            if want_sensor and sensor != want_sensor:
                continue
            sensor_root = sensor_roots.get(sensor)
            if not sensor_root:
                print(f"  '{sensor}' folder not found under {DATA_ROOT} - skip")
                continue
            render_site_sensor(store, aoi, sensor, sensor_root, args.refresh)

    print(f"\nDone. Overlays in {OVERLAY_DIR}/")


if __name__ == "__main__":
    main()
