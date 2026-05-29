"""
FROM SNOW TO RESERVOIR - Direct Cloud Download to Google Drive
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Pipeline:
  1. earthaccess searches OPERA granules directly from NASA
  2. rioxarray clips to AOI bbox (no full download)
  3. Coverage filter: skip files with < MIN_COVERAGE_PCT valid pixels
  4. Clipped GeoTIFF uploaded directly to Google Drive

Collections:
  - OPERA_L3_DSWX-S1_V1  : B01_WTR  (water classification, SAR-based)
  - OPERA_L3_DSWX-HLS_V1 : B03_SNOW (snow/ice classification, optical)

Requirements:
    pip install earthaccess rioxarray rasterio pydrive2 tqdm python-dotenv
"""

import io
import re
from datetime import datetime
from pathlib import Path

import earthaccess
import numpy as np
import rioxarray as rxr
from dotenv import load_dotenv
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from tqdm import tqdm

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

AOI_1 = {
    "name": "enguri",
    "label": "Enguri Talsperre + Svaneti",
    "bbox": (41.70, 42.55, 42.70, 43.05),       # (W, S, E, N) for earthaccess
    "clip_box": (41.70, 42.55, 42.70, 43.05),   # (min_lon, min_lat, max_lon, max_lat)
}

AOI_2 = {
    "name": "zhinvali",
    "label": "Zhinvali Talsperre + Gergeti",
    "bbox": (44.55, 42.00, 45.10, 42.55),
    "clip_box": (44.55, 42.00, 45.10, 42.55),
}

DATE_START = "2024-08-01"
DATE_END   = datetime.today().strftime("%Y-%m-%d")

NODATA = 255
MIN_COVERAGE_PCT = 90.0  # skip files with less than this % of valid pixels

# Collections and their layers to download
COLLECTIONS = [
    {
        "short_name":   "OPERA_L3_DSWX-S1_V1",
        "layers":       ["B01_WTR"],
        "drive_folder": "opera_dswx_s1",
    },
    {
        "short_name":   "OPERA_L3_DSWX-HLS_V1",
        "layers":       ["B03_SNOW"],
        "drive_folder": "opera_dswx_hls",
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


def get_or_create_folder(drive: GoogleDrive, name: str, parent_id: str = "root") -> str:
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


def file_exists_in_drive(drive: GoogleDrive, filename: str, folder_id: str) -> bool:
    query = f"title='{filename}' and '{folder_id}' in parents and trashed=false"
    return len(drive.ListFile({"q": query}).GetList()) > 0


def upload_bytes_to_drive(drive: GoogleDrive, data: bytes, filename: str, folder_id: str) -> str:
    f = drive.CreateFile({
        "title": filename,
        "parents": [{"id": folder_id}],
        "mimeType": "image/tiff",
    })
    f.content = io.BytesIO(data)
    f.Upload()
    return f["id"]


# ─────────────────────────────────────────────
# DATA PROCESSING
# ─────────────────────────────────────────────

def extract_date_from_filename(filename: str) -> str:
    m = re.search(r"_(\d{8})T", filename)
    return m.group(1) if m else "unknown"


def extract_layer_from_filename(filename: str, layers: list[str]) -> str | None:
    for layer in layers:
        if layer in filename:
            return layer
    return None


def compute_coverage(data: bytes) -> float:
    """Return fraction of valid (non-NoData) pixels as percentage."""
    buf = io.BytesIO(data)
    import rasterio
    with rasterio.open(buf) as src:
        arr = src.read(1)
    valid = int(np.sum(arr != NODATA))
    return valid / arr.size * 100.0


def process_granule(fs, url: str, clip_box: tuple) -> bytes | None:
    try:
        with fs.open(url) as f:
            da = rxr.open_rasterio(f, masked=True)

        clipped = da.rio.clip_box(
            minx=clip_box[0],
            miny=clip_box[1],
            maxx=clip_box[2],
            maxy=clip_box[3],
            crs="EPSG:4326",
        )

        if clipped.size == 0:
            return None

        buf = io.BytesIO()
        clipped.rio.to_raster(buf, driver="GTiff", compress="deflate")
        return buf.getvalue()

    except Exception as e:
        print(f"   ERROR: {Path(url).name}: {e}")
        return None


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────

def process_aoi(aoi: dict, collection: dict, drive: GoogleDrive):
    root_id = get_or_create_folder(drive, collection["drive_folder"])
    aoi_folder_id = get_or_create_folder(drive, aoi["name"], root_id)

    print(f"\n  Searching {collection['short_name']} {DATE_START} -> {DATE_END}...")
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
            layer = extract_layer_from_filename(fname, collection["layers"])
            if layer:
                urls_to_process.append((link, fname, layer))

    print(f"  -> {len(urls_to_process)} files to process ({', '.join(collection['layers'])})")

    uploaded = 0
    skipped_existing = 0
    skipped_coverage = 0

    for url, fname, layer in tqdm(urls_to_process, desc=f"  {aoi['name']}"):
        date_str = extract_date_from_filename(fname)
        out_name = f"{aoi['name']}_{date_str}_{layer}_clipped.tif"

        if file_exists_in_drive(drive, out_name, aoi_folder_id):
            skipped_existing += 1
            continue

        data = process_granule(fs, url, aoi["clip_box"])
        if data is None:
            continue

        coverage = compute_coverage(data)
        if coverage < MIN_COVERAGE_PCT:
            skipped_coverage += 1
            continue

        upload_bytes_to_drive(drive, data, out_name, aoi_folder_id)
        uploaded += 1

    print(f"  {aoi['name']}: {uploaded} uploaded, "
          f"{skipped_existing} already in Drive, "
          f"{skipped_coverage} skipped (coverage < {MIN_COVERAGE_PCT}%)")


def main():
    print("=" * 60)
    print("FROM SNOW TO RESERVOIR - Download to Google Drive")
    print(f"Period  : {DATE_START} -> {DATE_END}")
    print(f"Filter  : >= {MIN_COVERAGE_PCT}% valid pixels")
    print(f"Products: {', '.join(c['short_name'] for c in COLLECTIONS)}")
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
        print(f"{'='*60}")
        for aoi in [AOI_1, AOI_2]:
            print(f"\nAOI: {aoi['label']}")
            process_aoi(aoi, collection, drive)

    print("\nDone. Data available in Google Drive.")


if __name__ == "__main__":
    main()
