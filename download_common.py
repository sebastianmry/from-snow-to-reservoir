"""
FROM SNOW TO RESERVOIR - Shared download logic
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Common code used by download_hls.py and download_s1.py:
  1. earthaccess searches OPERA granules from NASA
  2. Footprint pre-filter: only dates whose tile footprints cover the AOI
     are downloaded at all (skips partial-coverage dates before any download)
  3. rioxarray clips each tile to the AOI bbox in-memory
  4. MGRS-tagged GeoTIFF uploaded to Google Drive
  5. Full-AOI mosaic + quality filtering happens later in extract_timeseries.py

Drive folder structure:
  OPERA_DSWx/
    hls/enguri/      <- OPERA_L3_DSWX-HLS_V1  B01_WTR + B09_CLOUD
    hls/zhinvali/
    s1/enguri/       <- OPERA_L3_DSWX-S1_V1   B01_WTR
    s1/zhinvali/
"""

import io
import re
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
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from shapely.geometry import Polygon, box as shp_box
from shapely.ops import unary_union
from tqdm import tqdm

load_dotenv()
warnings.filterwarnings("ignore", category=FutureWarning, module="earthaccess")

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

# AOI definition (bbox / clip_box / DRIVE_PARENT) is centralized in aoi_config.py
from aoi_config import AOI_LIST as AOIS, AOI_1, AOI_2, DRIVE_PARENT  # noqa: F401

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
    d = datetime.strptime(date_str, "%Y%m%d").date().toordinal()
    a = datetime.strptime(anchor, "%Y%m%d").date().toordinal()
    return (d - a) % S1_REPEAT_DAYS


# ─────────────────────────────────────────────
# GOOGLE DRIVE
# ─────────────────────────────────────────────

def get_drive() -> GoogleDrive:
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


def get_or_create_folder(drive: GoogleDrive, name: str, parent_id: str) -> str:
    query = (
        f"title='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = drive.ListFile({"q": query}).GetList()
    if results:
        return results[0]["id"]
    folder = drive.CreateFile({
        "title": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [{"id": parent_id}],
    })
    folder.Upload()
    return folder["id"]


def get_existing_filenames(drive: GoogleDrive, folder_id: str) -> set[str]:
    """All filenames in a folder, paginated (Drive returns max 100 per page)."""
    filenames = set()
    for page in drive.ListFile({
        "q": f"'{folder_id}' in parents and trashed=false",
        "maxResults": 1000,
    }):
        for f in page:
            filenames.add(f["title"])
    return filenames


def upload_bytes_to_drive(drive: GoogleDrive, data: bytes, filename: str, folder_id: str):
    f = drive.CreateFile({
        "title": filename,
        "parents": [{"id": folder_id}],
        "mimeType": "image/tiff",
    })
    f.content = io.BytesIO(data)
    f.Upload()


# ─────────────────────────────────────────────
# FILENAME / GRANULE HELPERS
# ─────────────────────────────────────────────

def extract_date_from_filename(filename: str) -> str:
    m = re.search(r"_(\d{8})T", filename)
    return m.group(1) if m else "unknown"


def extract_tile_id(filename: str) -> str:
    """MGRS tile ID, e.g. T37TGH. Lets multiple tiles per date coexist."""
    m = re.search(r"_(T\d{2}[A-Z]{3})_", filename)
    return m.group(1) if m else "TXXXXX"


def extract_layer(filename: str, layers: list[str]) -> str | None:
    for layer in layers:
        if layer in filename:
            return layer
    return None


def granule_date(granule) -> str:
    for link in granule.data_links():
        d = extract_date_from_filename(Path(link).name)
        if d != "unknown":
            return d
    return "unknown"


def granule_footprint(granule):
    """Build a shapely geometry from the granule's UMM spatial extent.
    Returns None if it cannot be parsed (caller treats that as fail-open)."""
    try:
        geo = granule["umm"]["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]
    except Exception:
        return None

    polys = []
    for gp in geo.get("GPolygons", []):
        pts = gp.get("Boundary", {}).get("Points", [])
        coords = [(p["Longitude"], p["Latitude"]) for p in pts]
        if len(coords) >= 3:
            polys.append(Polygon(coords))
    for br in geo.get("BoundingRectangles", []):
        try:
            polys.append(shp_box(
                br["WestBoundingCoordinate"], br["SouthBoundingCoordinate"],
                br["EastBoundingCoordinate"], br["NorthBoundingCoordinate"],
            ))
        except Exception:
            continue

    if not polys:
        return None
    return unary_union(polys)


def covered_dates(granules, aoi: dict) -> set[str]:
    """Per date, union the tile footprints and keep only dates whose footprints
    cover >= FOOTPRINT_MIN_COVER of the AOI. Fail-open if footprints unparsable."""
    aoi_poly = shp_box(*aoi["bbox"])
    aoi_area = aoi_poly.area

    by_date: dict[str, list] = defaultdict(list)
    for g in granules:
        by_date[granule_date(g)].append(g)

    keep = set()
    for date_str, gs in by_date.items():
        if date_str == "unknown":
            continue
        fps = [fp for fp in (granule_footprint(g) for g in gs) if fp is not None]
        if not fps:
            keep.add(date_str)  # cannot judge -> keep (download, let extract filter)
            continue
        cover = unary_union(fps).intersection(aoi_poly).area / aoi_area
        if cover >= FOOTPRINT_MIN_COVER:
            keep.add(date_str)
    return keep


def download_and_clip(session, url: str, clip_box: tuple) -> bytes | None:
    """Download a granule via an authenticated requests session, clip to the AOI
    bbox in-memory and return GeoTIFF bytes. Retries transient errors with
    exponential backoff. The HTTP_TIMEOUT read deadline prevents a stalled socket
    from hanging a worker forever (the previous fsspec path had no read timeout)."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code in RETRYABLE_STATUS:
                raise IOError(f"HTTP {resp.status_code}")
            resp.raise_for_status()

            # Open the downloaded bytes as an in-memory raster, then clip.
            with MemoryFile(resp.content) as mf:
                with mf.open() as ds:
                    da = rxr.open_rasterio(ds, masked=True)
                    clipped = da.rio.clip_box(
                        minx=clip_box[0], miny=clip_box[1],
                        maxx=clip_box[2], maxy=clip_box[3],
                        crs="EPSG:4326",
                    )
                    if clipped.size == 0:
                        return None
                    buf = io.BytesIO()
                    clipped.rio.to_raster(buf, driver="GTiff", compress="deflate")
                    return buf.getvalue()
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(60, 2 ** (attempt + 1)))  # 2,4,8,16,32s
            else:
                print(f"   ERROR after {MAX_RETRIES} attempts: {Path(url).name}: {e}")
                return None


# ─────────────────────────────────────────────
# PIPELINE
# ─────────────────────────────────────────────

def process_aoi(aoi: dict, collection: dict, drive: GoogleDrive):
    # Drive: OPERA_DSWx / hls|s1 / enguri|zhinvali
    parent_id = get_or_create_folder(drive, DRIVE_PARENT, "root")
    sub_id    = get_or_create_folder(drive, collection["drive_subfolder"], parent_id)
    aoi_id    = get_or_create_folder(drive, aoi["name"], sub_id)

    print(f"  Fetching existing files from Drive...")
    existing = get_existing_filenames(drive, aoi_id)
    print(f"  -> {len(existing)} files already in Drive")

    print(f"  Searching {collection['short_name']} {DATE_START} -> {DATE_END}...")
    granules = earthaccess.search_data(
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
            data = future.result()
            if data is None:
                skipped += 1
                continue
            upload_bytes_to_drive(drive, data, out_name, aoi_id)
            uploaded += 1

    print(f"  {aoi['name']}: {uploaded} uploaded, {skipped} skipped (empty/error)")


def run(collection: dict):
    """Entry point used by download_hls.py / download_s1.py."""
    print("=" * 60)
    print("FROM SNOW TO RESERVOIR - Download to Google Drive")
    print(f"Product  : {collection['short_name']}")
    print(f"Layers   : {', '.join(collection['layers'])}")
    print(f"Period   : {DATE_START} -> {DATE_END}")
    print(f"Coverage : footprint pre-filter >= {FOOTPRINT_MIN_COVER*100:.0f}% AOI, "
          f"all MGRS tiles, MGRS-tagged names")
    print(f"Drive    : {DRIVE_PARENT}/{collection['drive_subfolder']}/<aoi>/")
    print("=" * 60)

    print("\nNASA Earthdata Login...")
    try:
        earthaccess.login(strategy="netrc")
    except Exception:
        print("  No _netrc found - enter credentials:")
        earthaccess.login(strategy="interactive", persist=True)
    print("NASA Login OK")

    print("\nGoogle Drive Login...")
    drive = get_drive()
    print("Google Drive OK")

    for aoi in AOIS:
        print(f"\nAOI: {aoi['label']}")
        process_aoi(aoi, collection, drive)

    print("\nDone. Data available in Google Drive.")
