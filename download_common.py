"""
FROM SNOW TO RESERVOIR - Shared download logic
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Common code used by download_hls.py and download_s1.py:
  1. earthaccess searches OPERA granules from NASA
  2. Footprint pre-filter: only dates whose tile footprints cover the AOI
     are downloaded at all (skips partial-coverage dates before any download)
  3. rioxarray clips each tile to the AOI bbox in-memory
  4. MGRS-tagged GeoTIFF written to the tile store
  5. Full-AOI mosaic + quality filtering happens later in extract_timeseries.py

Store folder structure:
  OPERA_DSWx/
    hls/enguri/      <- OPERA_L3_DSWX-HLS_V1  B01_WTR
    hls/zhinvali/
    s1/enguri/       <- OPERA_L3_DSWX-S1_V1   B01_WTR
    s1/zhinvali/
"""

import io
import re
import sys
import time
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import earthaccess
import rioxarray as rxr
from rasterio.io import MemoryFile
from dotenv import load_dotenv
from shapely.geometry import Polygon, box as shp_box
from shapely.ops import unary_union
from tqdm import tqdm

load_dotenv()
warnings.filterwarnings("ignore", category=FutureWarning, module="earthaccess")

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

# AOI definition (bbox / clip_box / DATA_ROOT) is centralized in aoi_config.py
from aoi_config import AOI_LIST as AOIS, AOI_1, AOI_2, DATA_ROOT  # noqa: F401
# Local tile store (filesystem under PIPELINE_LOCAL_DIR).
from storage import get_store, ROOT

DATE_START = "2024-08-01"
DATE_END   = datetime.today().strftime("%Y-%m-%d")

NODATA      = 255
MAX_WORKERS = 2     # conservative to avoid RAM overload / crashes
MAX_RETRIES = 3     # PODAAC occasionally returns 5xx; retry a few times. During a
# server outage retrying more is futile (fail fast, recover via a later resume run).
# (connect, read) timeout in seconds for each HTTP request. The read timeout is
# the key fix against hangs: a stalled socket aborts instead of blocking forever.
# Kept short so a dead file is abandoned quickly instead of freezing the bar for
# minutes (a healthy ~13 MB tile downloads well within 60 s).
HTTP_TIMEOUT = (10, 60)
# HTTP status codes worth retrying (transient server-side / rate-limit errors).
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Footprint pre-filter: keep a date only if the union of its tile footprints
# covers at least this fraction of the AOI (skips partial-coverage dates).
FOOTPRINT_MIN_COVER = 0.99

# Sentinel-1 ground-track repeat cycle. For S1 we additionally keep only the
# dates that share the AOI's chosen relative orbit (s1_anchor's 12-day phase),
# so the download pulls just that one orbit (~1/4 of the dates) instead of all
# of them - the same orbit extract_timeseries.py anchors the series to.
S1_REPEAT_DAYS = 12


def orbit_phase(date_str: str, anchor: str) -> int:
    """12-day repeat phase of a date relative to an anchor (both YYYYMMDD).
    Dates of the same S1 relative orbit share the same phase; phase 0 == anchor."""
    date_ordinal = datetime.strptime(date_str, "%Y%m%d").date().toordinal()
    anchor_ordinal = datetime.strptime(anchor, "%Y%m%d").date().toordinal()
    return (date_ordinal - anchor_ordinal) % S1_REPEAT_DAYS


# Folder navigation, listing, upload and download of tiles all go through the
# storage backend (see storage.py): get_store(), store.ensure_folder(...),
# store.existing_names(...), store.write(...).


# ─────────────────────────────────────────────
# FILENAME / GRANULE HELPERS
# ─────────────────────────────────────────────

def extract_date_from_filename(filename: str) -> str:
    match = re.search(r"_(\d{8})T", filename)
    return match.group(1) if match else "unknown"


def extract_tile_id(filename: str) -> str:
    """MGRS tile ID, e.g. T37TGH. Lets multiple tiles per date coexist."""
    match = re.search(r"_(T\d{2}[A-Z]{3})_", filename)
    return match.group(1) if match else "TXXXXX"


def extract_layer(filename: str, layers: list[str]) -> str | None:
    for layer in layers:
        if layer in filename:
            return layer
    return None


def granule_date(granule) -> str:
    for link in granule.data_links():
        date_str = extract_date_from_filename(Path(link).name)
        if date_str != "unknown":
            return date_str
    return "unknown"


def granule_footprint(granule):
    """Build a shapely geometry from the granule's UMM spatial extent.
    Returns None if it cannot be parsed (caller treats that as fail-open)."""
    try:
        geo = granule["umm"]["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]
    except Exception:
        return None

    polygons = []
    for gpolygon in geo.get("GPolygons", []):
        points = gpolygon.get("Boundary", {}).get("Points", [])
        coords = [(point["Longitude"], point["Latitude"]) for point in points]
        if len(coords) >= 3:
            polygons.append(Polygon(coords))
    for rectangle in geo.get("BoundingRectangles", []):
        try:
            polygons.append(shp_box(
                rectangle["WestBoundingCoordinate"], rectangle["SouthBoundingCoordinate"],
                rectangle["EastBoundingCoordinate"], rectangle["NorthBoundingCoordinate"],
            ))
        except Exception:
            continue

    if not polygons:
        return None
    return unary_union(polygons)


def covered_dates(granules, aoi: dict) -> set[str]:
    """Per date, union the tile footprints and keep only dates whose footprints
    cover >= FOOTPRINT_MIN_COVER of the AOI. Fail-open if footprints unparsable."""
    aoi_poly = shp_box(*aoi["bbox"])
    aoi_area = aoi_poly.area

    granules_by_date: dict[str, list] = defaultdict(list)
    for granule in granules:
        granules_by_date[granule_date(granule)].append(granule)

    kept_dates = set()
    for date_str, date_granules in granules_by_date.items():
        if date_str == "unknown":
            continue
        footprints = [fp for fp in (granule_footprint(g) for g in date_granules)
                      if fp is not None]
        if not footprints:
            kept_dates.add(date_str)  # cannot judge -> keep (download, let extract filter)
            continue
        cover = unary_union(footprints).intersection(aoi_poly).area / aoi_area
        if cover >= FOOTPRINT_MIN_COVER:
            kept_dates.add(date_str)
    return kept_dates


def download_and_clip(session, url: str, clip_box: tuple) -> bytes | None:
    """Download a granule via an authenticated requests session, clip to the AOI
    bbox in-memory and return GeoTIFF bytes. Retries transient errors with
    exponential backoff. The HTTP_TIMEOUT read deadline prevents a stalled socket
    from hanging a worker forever (the previous fsspec path had no read timeout)."""
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, timeout=HTTP_TIMEOUT)
            if response.status_code in RETRYABLE_STATUS:
                raise IOError(f"HTTP {response.status_code}")
            response.raise_for_status()

            # Open the downloaded bytes as an in-memory raster, then clip.
            with MemoryFile(response.content) as memfile:
                with memfile.open() as dataset:
                    raster = rxr.open_rasterio(dataset, masked=True)
                    clipped_raster = raster.rio.clip_box(
                        minx=clip_box[0], miny=clip_box[1],
                        maxx=clip_box[2], maxy=clip_box[3],
                        crs="EPSG:4326",
                    )
                    if clipped_raster.size == 0:
                        return None
                    output_buffer = io.BytesIO()
                    clipped_raster.rio.to_raster(output_buffer, driver="GTiff",
                                                 compress="deflate")
                    return output_buffer.getvalue()
        except Exception as error:
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(60, 2 ** (attempt + 1)))  # 2,4,8,16,32s
            else:
                print(f"   ERROR after {MAX_RETRIES} attempts: {Path(url).name}: {error}")
                return None


# ─────────────────────────────────────────────
# PIPELINE
# ─────────────────────────────────────────────

def search_granules(**kwargs):
    """earthaccess.search_data with retry + exponential backoff. NASA's CMR
    occasionally returns a transient 5xx ("An Internal Error has occurred.");
    a few retries keep the unattended weekly CI run from failing on a momentary
    NASA-side hiccup (the tile download already retries the same way)."""
    for attempt in range(MAX_RETRIES):
        try:
            return earthaccess.search_data(**kwargs)
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = min(60, 2 ** (attempt + 1))  # 2, 4, 8, ...
                print(f"   CMR search failed ({e}); retry in {wait}s "
                      f"({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                raise


def process_aoi(aoi: dict, collection: dict, store):
    # Layout: OPERA_DSWx / hls|s1 / enguri|zhinvali
    parent_id = store.ensure_folder(DATA_ROOT, ROOT)
    sub_id    = store.ensure_folder(collection["subfolder"], parent_id)
    aoi_id    = store.ensure_folder(aoi["name"], sub_id)

    print("  Fetching existing files from store...")
    existing = store.existing_names(aoi_id)
    print(f"  -> {len(existing)} files already in store")

    print(f"  Searching {collection['short_name']} {DATE_START} -> {DATE_END}...")
    granules = search_granules(
        short_name=collection["short_name"],
        bounding_box=aoi["bbox"],
        temporal=(DATE_START, DATE_END),
        count=-1,
    )
    print(f"  -> {len(granules)} granules found")
    if not granules:
        return

    # Footprint pre-filter: only keep dates that (geometrically) cover the AOI
    keep_dates = covered_dates(granules, aoi)
    n_dates_total = len({granule_date(g) for g in granules} - {"unknown"})
    print(f"  -> {len(keep_dates)}/{n_dates_total} dates cover >= "
          f"{FOOTPRINT_MIN_COVER*100:.0f}% of AOI (rest skipped before download)")

    # S1 orbit pre-filter: keep only the AOI's chosen relative orbit so we download
    # one consistent look geometry (~1/4 of the dates), not every orbit. Applied
    # only for collections flagged orbit_filter (S1); HLS keeps all dates.
    anchor = aoi.get("s1_anchor")
    if collection.get("orbit_filter") and anchor:
        before = len(keep_dates)
        keep_dates = {d for d in keep_dates if orbit_phase(d, anchor) == 0}
        print(f"  -> orbit filter (anchor {anchor}, 12-day phase 0): "
              f"{len(keep_dates)}/{before} dates kept (one relative orbit)")

    # Authenticated requests session (supports a hard read timeout, unlike the
    # fsspec session) - shared across the download threads.
    session = earthaccess.get_requests_https_session()

    urls_to_process = []
    for granule in granules:
        if granule_date(granule) not in keep_dates:
            continue
        for link in granule.data_links():
            fname = Path(link).name
            layer = extract_layer(fname, collection["layers"])
            if not layer:
                continue
            date_str = extract_date_from_filename(fname)
            tile_id  = extract_tile_id(fname)
            out_name = f"{aoi['name']}_{date_str}_{tile_id}_{layer}_clipped.tif"
            if out_name not in existing:
                urls_to_process.append((link, out_name, layer))

    print(f"  -> {len(urls_to_process)} new files to download")

    uploaded = skipped = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download_and_clip, session, url, aoi["clip_box"]): (out_name, layer)
            for url, out_name, layer in urls_to_process
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc=f"  {aoi['name']}"):
            out_name, layer = futures[future]
            tile_bytes = future.result()
            if tile_bytes is None:
                skipped += 1
                continue
            store.write(aoi_id, out_name, tile_bytes)
            uploaded += 1

    print(f"  {aoi['name']}: {uploaded} uploaded, {skipped} skipped (empty/error)")


def run(collection: dict):
    """Entry point used by download_hls.py / download_s1.py."""
    print("=" * 60)
    print("FROM SNOW TO RESERVOIR - Download OPERA tiles")
    print(f"Product  : {collection['short_name']}")
    print(f"Layers   : {', '.join(collection['layers'])}")
    print(f"Period   : {DATE_START} -> {DATE_END}")
    print(f"Coverage : footprint pre-filter >= {FOOTPRINT_MIN_COVER*100:.0f}% AOI, "
          f"all MGRS tiles, MGRS-tagged names")
    print(f"Store    : {DATA_ROOT}/{collection['subfolder']}/<aoi>/")
    print("=" * 60)

    print("\nNASA Earthdata Login...")
    try:
        earthaccess.login(strategy="netrc")
    except Exception as login_error:
        # Without a terminal (e.g. the weekly CI run) the interactive prompt has
        # no stdin and dies with a cryptic EOFError, masking the real cause. Only
        # fall back to it when a real terminal is attached; otherwise re-raise the
        # actual login/network error so the failure is diagnosable.
        if not sys.stdin.isatty():
            raise RuntimeError(
                "Earthdata login via .netrc failed and no terminal is available "
                "for an interactive prompt. Check the EARTHDATA_USERNAME / "
                "EARTHDATA_PASSWORD secrets and that urs.earthdata.nasa.gov is "
                f"reachable. Original error: {login_error}"
            ) from login_error
        print("  No _netrc found - enter credentials:")
        earthaccess.login(strategy="interactive", persist=True)
    print("NASA Login OK")

    print("\nOpening tile store...")
    store = get_store()
    print("Store ready")

    for aoi in AOIS:
        print(f"\nAOI: {aoi['label']}")
        process_aoi(aoi, collection, store)

    print("\nDone. Tiles written to the store.")
