"""
FROM SNOW TO RESERVOIR - Direct Cloud Download to Google Drive
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Pipeline:
  1. earthaccess searches OPERA granules from NASA
  2. Existing Drive files fetched once per folder (no per-file API calls)
  3. rioxarray clips to AOI bbox in-memory (no local download)
  4. Quality filter: reservoir center point must be covered (not NoData)
  5. Retry with exponential backoff for unstable NASA connections
  6. Clipped GeoTIFF uploaded to Google Drive

Drive folder structure (all under DRIVE_ROOT_FOLDER_ID):
  s1/enguri/    <- OPERA_L3_DSWX-S1_V1  B01_WTR
  s1/zhinvali/
  hls/enguri/   <- OPERA_L3_DSWX-HLS_V1 B01_WTR + B09_CLOUD
  hls/zhinvali/

Requirements:
    pip install earthaccess rioxarray rasterio pydrive2 tqdm python-dotenv
"""

import io
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import earthaccess
import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform
import rioxarray as rxr
from dotenv import load_dotenv
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from tqdm import tqdm

load_dotenv()
warnings.filterwarnings("ignore", category=FutureWarning, module="earthaccess")

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

DRIVE_ROOT_FOLDER_ID = "1EdYn2RbULuEYj8dnPbK9Zshia6G50ssE"

AOI_1 = {
    "name": "enguri",
    "label": "Enguri Talsperre + Svaneti",
    "bbox": (41.70, 42.55, 42.80, 43.15),
    "clip_box": (41.70, 42.55, 42.80, 43.15),
    "reservoir_point": (42.032, 42.753),
}

AOI_2 = {
    "name": "zhinvali",
    "label": "Zhinvali Talsperre + Gergeti",
    "bbox": (44.30, 42.00, 45.15, 42.80),
    "clip_box": (44.30, 42.00, 45.15, 42.80),
    "reservoir_point": (44.771, 42.133),
}

DATE_START = "2024-08-01"
DATE_END   = datetime.today().strftime("%Y-%m-%d")

NODATA      = 255
MAX_WORKERS = 2  # reduced from 4 to prevent RAM overload on HLS mosaics
MAX_RETRIES = 3

COLLECTIONS = [
    # "OPERA_L3_DSWX-S1_V1" temporarily disabled - resume HLS-only after crash
    # {
    #     "short_name":    "OPERA_L3_DSWX-S1_V1",
    #     "layers":        ["B01_WTR"],
    #     "drive_subfolder": "s1",
    # },
    {
        "short_name":    "OPERA_L3_DSWX-HLS_V1",
        "layers":        ["B01_WTR", "B09_CLOUD"],
        "drive_subfolder": "hls",
    },
]


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
    """Fetch all filenames with pagination (Drive API returns max 100 per page).
    pydrive2 supports iterating over ListFile to walk all pages automatically.
    """
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
# QUALITY FILTER
# ─────────────────────────────────────────────

def reservoir_is_covered(data: bytes, aoi: dict) -> bool:
    """Reservoir center point must be within raster bounds and not NoData."""
    res_lon, res_lat = aoi["reservoir_point"]
    with rasterio.open(io.BytesIO(data)) as src:
        if src.crs and not src.crs.is_geographic:
            xs, ys = warp_transform("EPSG:4326", src.crs, [res_lon], [res_lat])
            res_x, res_y = xs[0], ys[0]
        else:
            res_x, res_y = res_lon, res_lat

        b = src.bounds
        if not (b.left <= res_x <= b.right and b.bottom <= res_y <= b.top):
            return False
        row, col = src.index(res_x, res_y)
        if not (0 <= row < src.height and 0 <= col < src.width):
            return False
        return src.read(1)[row, col] != NODATA


# ─────────────────────────────────────────────
# DATA PROCESSING
# ─────────────────────────────────────────────

def extract_date_from_filename(filename: str) -> str:
    m = re.search(r"_(\d{8})T", filename)
    return m.group(1) if m else "unknown"


def extract_layer(filename: str, layers: list[str]) -> str | None:
    for layer in layers:
        if layer in filename:
            return layer
    return None


def download_and_clip(fs, url: str, clip_box: tuple) -> bytes | None:
    """Download, clip and return bytes. Retries with exponential backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            with fs.open(url) as f:
                da = rxr.open_rasterio(f, masked=True)

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
                wait = 2 ** attempt
                time.sleep(wait)
            else:
                print(f"   ERROR after {MAX_RETRIES} attempts: {Path(url).name}: {e}")
                return None


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────

def process_aoi(aoi: dict, collection: dict, drive: GoogleDrive):
    # Drive folder: s1|hls (at root) / enguri|zhinvali
    sub_id  = get_or_create_folder(drive, collection["drive_subfolder"], "root")
    aoi_id  = get_or_create_folder(drive, aoi["name"], sub_id)

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

    fs = earthaccess.get_fsspec_https_session()

    urls_to_process = []
    for granule in granules:
        for link in granule.data_links():
            fname = Path(link).name
            layer = extract_layer(fname, collection["layers"])
            if not layer:
                continue
            date_str = extract_date_from_filename(fname)
            out_name = f"{aoi['name']}_{date_str}_{layer}_clipped.tif"
            if out_name not in existing:
                urls_to_process.append((link, out_name, layer))

    n_skip = len(granules) * len(collection["layers"]) - len(urls_to_process)
    print(f"  -> {len(urls_to_process)} new files to process ({n_skip} skipped)")

    uploaded = skipped_filter = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download_and_clip, fs, url, aoi["clip_box"]): (out_name, layer)
            for url, out_name, layer in urls_to_process
        }

        for future in tqdm(as_completed(futures), total=len(futures),
                           desc=f"  {aoi['name']}"):
            out_name, layer = futures[future]
            data = future.result()

            if data is None:
                skipped_filter += 1
                continue

            # Only check reservoir coverage for B01_WTR
            if layer == "B01_WTR" and not reservoir_is_covered(data, aoi):
                skipped_filter += 1
                continue

            upload_bytes_to_drive(drive, data, out_name, aoi_id)
            uploaded += 1

    print(f"  {aoi['name']}: {uploaded} uploaded, "
          f"{n_skip} already in Drive, "
          f"{skipped_filter} skipped (filter/error)")


def main():
    print("=" * 60)
    print("FROM SNOW TO RESERVOIR - Download to Google Drive")
    print(f"Period   : {DATE_START} -> {DATE_END}")
    print(f"Products : {', '.join(c['short_name'] for c in COLLECTIONS)}")
    print(f"Filter   : reservoir center point must be covered")
    print(f"Retries  : {MAX_RETRIES} attempts with exponential backoff")
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
    print("Google Drive OK\n")

    for collection in COLLECTIONS:
        print(f"\n{'='*60}")
        print(f"Collection: {collection['short_name']}")
        print(f"Layers    : {', '.join(collection['layers'])}")
        print(f"{'='*60}")
        for aoi in [AOI_1, AOI_2]:
            print(f"\nAOI: {aoi['label']}")
            print(f"  Reservoir: {aoi['reservoir_point']}")
            process_aoi(aoi, collection, drive)

    print("\nDone. Data available in Google Drive.")


if __name__ == "__main__":
    main()
