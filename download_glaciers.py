"""
FROM SNOW TO RESERVOIR - RGI v7 Glacier Data Download
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Downloads the Randolph Glacier Inventory v7 Region 12 (Caucasus and Middle East)
shapefile from NSIDC via authenticated NASA Earthdata HTTPS session.

Output: static_data/RGI2000-v7.0-G-12_caucasus-middle_east.shp (+ sidecar files)

Run once before extract_timeseries.py:
    python download_glaciers.py
"""

import re
import zipfile
from pathlib import Path

import earthaccess

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

STATIC_DIR = Path("static_data")

RGI_NSIDC_BASE = "https://daacdata.apps.nsidc.org/pub/DATASETS/nsidc0770_rgi_v7/"
RGI_FILENAME   = "RGI2000-v7.0-G-12_caucasus-middle_east.zip"

# Candidate URL paths to try in order
RGI_URL_CANDIDATES = [
    RGI_NSIDC_BASE + "regional_files/RGI2000-v7.0-G/" + RGI_FILENAME,
    RGI_NSIDC_BASE + "RGI2000-v7.0-G/" + RGI_FILENAME,
    RGI_NSIDC_BASE + "data/" + RGI_FILENAME,
    RGI_NSIDC_BASE + RGI_FILENAME,
]
RGI_ZIP      = STATIC_DIR / "rgi_region12.zip"
RGI_SHP_GLOB = "RGI2000-v7.0-G-12_caucasus*middle_east.shp"


# ─────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────

def download_rgi() -> Path | None:
    """Download and unpack RGI v7 Region 12 from NSIDC.
    Uses NASA Earthdata credentials (same account as the download scripts).
    Returns path to .shp file or None on failure.
    """
    STATIC_DIR.mkdir(exist_ok=True)

    existing = list(STATIC_DIR.rglob(RGI_SHP_GLOB))
    if existing:
        print(f"RGI shapefile already present: {existing[0]}")
        return existing[0]

    print("Downloading RGI v7 Region 12 from NSIDC...")

    session = earthaccess.get_requests_https_session()

    # Browse the RGI2000-v7.0-G/ folder (glacier outlines) and list actual files
    glacier_folder_url = RGI_NSIDC_BASE + "regional_files/RGI2000-v7.0-G/"
    print(f"  Browsing {glacier_folder_url}")

    working_url = None
    try:
        listing_response = session.get(glacier_folder_url, timeout=30)
        # Find all zip filenames in the directory listing
        zip_names = re.findall(r'href="([^"]+\.zip)"', listing_response.text)
        print(f"  Zip files in folder: {len(zip_names)}")
        # Find the Region 12 file (contains "-12_" or "caucasus")
        for zip_name in zip_names:
            filename = zip_name.split("/")[-1]
            if "-12_" in filename or "caucasus" in filename.lower():
                working_url = glacier_folder_url + filename
                print(f"  Found Region 12: {filename}")
                break
        if not working_url:
            print("  Region 12 not found. All zip files:")
            for zip_name in zip_names:
                print(f"    {zip_name.split('/')[-1]}")
    except Exception as error:
        print(f"  Could not browse folder: {error}")

    if not working_url:
        print("\nERROR: Could not find RGI Region 12 zip on NSIDC.")
        print("Manual download: https://nsidc.org/data/nsidc-0770/versions/7")
        print("Unpack Region 12 zip into static_data/")
        return None

    try:
        response = session.get(working_url, stream=True, timeout=300)
        response.raise_for_status()
        total_bytes = int(response.headers.get("content-length", 0))
        downloaded_bytes = 0
        with RGI_ZIP.open("wb") as out_file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                out_file.write(chunk)
                downloaded_bytes += len(chunk)
                if total_bytes:
                    print(f"\r  {downloaded_bytes / 1e6:.1f} / {total_bytes / 1e6:.1f} MB",
                          end="", flush=True)
        print(f"\nDownload complete ({downloaded_bytes / 1e6:.1f} MB)")
    except Exception as error:
        print(f"ERROR downloading: {error}")
        return None

    print("Unpacking...")
    with zipfile.ZipFile(RGI_ZIP, "r") as archive:
        archive.extractall(STATIC_DIR)
    RGI_ZIP.unlink()

    shp_paths = list(STATIC_DIR.rglob(RGI_SHP_GLOB))
    if shp_paths:
        print(f"Done. Shapefile ready: {shp_paths[0]}")
        return shp_paths[0]

    print("ERROR: Shapefile not found after unpack.")
    return None


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 50)
    print("RGI v7 Region 12 - Glacier Data Download")
    print("=" * 50)

    print("\nNASA Earthdata Login...")
    try:
        earthaccess.login(strategy="netrc")
    except Exception:
        print("  No _netrc found - enter credentials:")
        earthaccess.login(strategy="interactive", persist=True)
    print("Login OK\n")

    shp_path = download_rgi()
    if shp_path:
        print("\nReady for extract_timeseries.py")
    else:
        print("\nFailed - glacier stats will be skipped in extract_timeseries.py")


if __name__ == "__main__":
    main()
